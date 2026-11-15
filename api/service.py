import base64
import csv
import hashlib
import io
import json
import logging
import os
import secrets
import uuid
from datetime import datetime, timedelta, timezone
from urllib.parse import urlparse

from api.domain import (
    ApiError,
    account_view,
    normalize_email,
    normalize_record,
    parse_iso,
    plan_for,
    public_record,
    record_limit,
    to_iso,
    utc_now,
)

SESSION_LIFETIME = timedelta(days=30)
VERIFICATION_LIFETIME = timedelta(minutes=10)
MAX_PAGE_SIZE = 50
MAX_RECORDS_PER_IMPORT = 100


def build_store():
    connection_string = os.getenv("AZURE_TABLE_STORAGE_CONNECTION_STRING")
    if connection_string:
        from api.table_store import AzureTableStore

        return AzureTableStore(connection_string)

    from api.storage import SQLiteStore

    database_path = os.getenv("QR_DATABASE_PATH", "data/qrcode-stocker.sqlite3")
    return SQLiteStore(database_path)


def _environment():
    return os.getenv("APP_ENV", "production").strip().lower()


def _send_verification_email(email, code):
    required = (
        "AWS_ACCESS_KEY_ID",
        "AWS_SECRET_ACCESS_KEY",
        "SENDER_EMAIL_ADDRESS",
    )
    missing = [key for key in required if not os.getenv(key)]
    if missing:
        raise ApiError(
            503,
            "email_not_configured",
            "メール送信が設定されていません。開発モードでは確認コードを画面に表示します。",
        )
    import boto3

    client = boto3.client(
        "ses",
        region_name=os.getenv("AWS_REGION", "us-east-1"),
        aws_access_key_id=os.environ["AWS_ACCESS_KEY_ID"],
        aws_secret_access_key=os.environ["AWS_SECRET_ACCESS_KEY"],
    )
    message = {
        "Subject": {"Data": "QRコードストッカー 確認コード", "Charset": "UTF-8"},
        "Body": {
            "Text": {
                "Data": f"確認コード: {code}\n有効期限は10分です。",
                "Charset": "UTF-8",
            }
        },
    }
    request = {
        "Source": os.environ["SENDER_EMAIL_ADDRESS"],
        "Destination": {"ToAddresses": [email]},
        "Message": message,
    }
    configuration_set = os.getenv("SES_CONFIGURATION_SET_NAME")
    if configuration_set:
        request["ConfigurationSetName"] = configuration_set
    try:
        client.send_email(**request)
    except Exception as exc:
        logging.exception("SES verification email could not be sent")
        raise ApiError(502, "email_delivery_failed", "確認メールを送信できませんでした。時間をおいて再試行してください。") from exc


def request_verification(store, sid, payload, now=None):
    now = now or utc_now()
    email = normalize_email(payload.get("email"))
    action = payload.get("action", "register")
    if action not in ("register", "login"):
        raise ApiError(400, "invalid_action", "登録またはログインを選択してください。")
    owner = store.get_owner_by_email(email)
    if action == "register" and owner:
        raise ApiError(409, "email_exists", "このメールアドレスは登録済みです。ログインしてください。")
    if action == "login" and owner is None:
        raise ApiError(404, "account_not_found", "アカウントが見つかりません。先に登録してください。")

    code = f"{secrets.randbelow(1_000_000):06d}"
    salt = secrets.token_hex(16)
    code_hash = hashlib.sha256((salt + code).encode("utf-8")).hexdigest()
    challenge = {
        "email": email,
        "code_hash": code_hash,
        "code_salt": salt,
        "action": action,
        "owner_id": owner["owner_id"] if owner else str(uuid.uuid4()),
        "sid": sid,
        "expires_at": to_iso(now + VERIFICATION_LIFETIME),
    }
    store.create_challenge(challenge, now)

    response = {
        "ok": True,
        "expiresInSeconds": int(VERIFICATION_LIFETIME.total_seconds()),
        "message": "確認コードを送信しました。",
    }
    if _environment() == "development":
        response["developmentCode"] = code
        response["message"] = "開発用確認コードを表示しました。"
    else:
        _send_verification_email(email, code)
    return response


def _import_records(value, now):
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_RECORDS_PER_IMPORT:
        raise ApiError(400, "invalid_import", "引き継げる記録は1ブラウザあたり100件までです。")
    normalized = []
    seen = set()
    for item in value:
        record = normalize_record(item, now)
        if record["id"] in seen:
            continue
        seen.add(record["id"])
        normalized.append(record)
    return normalized


def verify_email(store, sid, payload, now=None):
    now = now or utc_now()
    email = normalize_email(payload.get("email"))
    code = payload.get("code")
    if not isinstance(code, str) or len(code) != 6 or not code.isdigit():
        raise ApiError(400, "invalid_verification", "6桁の確認コードを入力してください。")
    session_token = secrets.token_urlsafe(32)
    session_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
    session_expires_at = to_iso(now + SESSION_LIFETIME)
    owner_id = store.verify_email(
        email,
        sid,
        code,
        session_hash,
        session_expires_at,
        now,
    )
    transfer = store.transfer_guest_records(sid, owner_id, [], now)
    summary = store.account_summary(owner_id, now)
    return {
        "sessionToken": session_token,
        "authenticated": True,
        "account": account_view(summary, now),
        "transfer": transfer,
        "developmentMode": _environment() == "development",
    }


def resolve_identity(store, sid, session_token=None, now=None):
    now = now or utc_now()
    if session_token:
        token_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        owner_id = store.authenticate(token_hash, sid, now)
        if owner_id is None:
            raise ApiError(401, "unauthorized", "ログイン状態の有効期限が切れました。もう一度ログインしてください。")
        store.expire_paid(owner_id, now)
        return owner_id, True
    owner_id = f"guest:{sid}"
    store.ensure_guest(owner_id, now)
    return owner_id, False


def get_session(store, sid, session_token=None, now=None):
    now = now or utc_now()
    invalid_cookie = False
    transfer = None
    if session_token:
        token_hash = hashlib.sha256(session_token.encode("utf-8")).hexdigest()
        owner_id = store.authenticate(token_hash, sid, now)
        if owner_id:
            store.expire_paid(owner_id, now)
            authenticated = True
            transfer = store.transfer_guest_records(sid, owner_id, [], now)
        else:
            owner_id = f"guest:{sid}"
            store.ensure_guest(owner_id, now)
            authenticated = False
            invalid_cookie = True
    else:
        owner_id = f"guest:{sid}"
        store.ensure_guest(owner_id, now)
        authenticated = False
    summary = store.account_summary(owner_id, now)
    return {
        "authenticated": authenticated,
        "account": account_view(summary, now),
        "transfer": transfer,
        "developmentMode": _environment() == "development",
        "clearCookie": invalid_cookie,
    }


def create_record(store, owner_id, payload, now=None):
    now = now or utc_now()
    record = normalize_record(payload, now)
    result = store.create_record(owner_id, record, now)
    return {
        "saved": True,
        "record": public_record(result["record"]),
        "recordState": result["record"]["state"],
        "activeCount": result["active_count"],
        "limit": result["limit"],
        "plan": result["plan"],
        "logicalDeleted": result["logical_deleted"],
        "duplicate": result["duplicate"],
    }


def list_records(store, owner_id, payload, now=None):
    now = now or utc_now()
    query = payload.get("query", "")
    if not isinstance(query, str) or len(query) > 200:
        raise ApiError(400, "invalid_query", "検索語は200文字以内で入力してください。")
    cursor = payload.get("cursor")
    if cursor is not None and not isinstance(cursor, str):
        raise ApiError(400, "invalid_cursor", "一覧カーソルが正しくありません。")
    page_size = payload.get("pageSize", MAX_PAGE_SIZE)
    if not isinstance(page_size, int) or isinstance(page_size, bool):
        page_size = MAX_PAGE_SIZE
    page_size = max(1, min(MAX_PAGE_SIZE, page_size))
    result = store.list_records(owner_id, query.strip(), cursor, page_size, now)
    return {
        "records": [public_record(record) for record in result["records"]],
        "nextCursor": result["next_cursor"],
        "activeCount": result["active_count"],
        "limit": result["limit"],
        "plan": result["plan"],
    }


def update_record(store, owner_id, payload, now=None):
    record_id = payload.get("id")
    content = payload.get("content")
    note = payload.get("note", "")
    if not isinstance(content, str) or not content.strip() or len(content) > 8192:
        raise ApiError(400, "invalid_content", "QRコードの内容は1〜8192文字で入力してください。")
    if note is None:
        note = ""
    if not isinstance(note, str) or len(note) > 2000:
        raise ApiError(400, "invalid_note", "メモは2000文字以内で入力してください。")
    try:
        record_id = str(uuid.UUID(record_id))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ApiError(400, "invalid_id", "記録IDの形式が正しくありません。") from exc
    row = store.update_record(owner_id, record_id, content, note.strip(), now)
    return {"updated": True, "record": public_record(row)}


def delete_record(store, owner_id, payload, now=None):
    try:
        record_id = str(uuid.UUID(payload.get("id")))
    except (ValueError, TypeError, AttributeError) as exc:
        raise ApiError(400, "invalid_id", "記録IDの形式が正しくありません。") from exc
    result = store.delete_record(owner_id, record_id, now)
    return {
        "deleted": result["deleted"],
        "alreadyDeleted": result["already_deleted"],
        "activeCount": result["active_count"],
    }


def _spreadsheet_safe(value):
    value = str(value or "")
    if value.startswith(("=", "+", "-", "@", "\t", "\r")):
        return "'" + value
    return value


def export_records(store, owner_id, payload, now=None):
    now = now or utc_now()
    summary = store.account_summary(owner_id, now)
    if summary["owner"]["kind"] != "user" or plan_for(summary["owner"], now) != "paid":
        raise ApiError(403, "paid_required", "エクスポートはPaidプランで利用できます。")
    export_format = payload.get("format")
    if export_format not in ("csv", "xlsx"):
        raise ApiError(400, "invalid_export_format", "CSVまたはXLSXを選択してください。")
    rows = store.list_all_active(owner_id, now)
    headers = ("記録ID", "読み取り日時", "QRコード内容", "メモ")
    if export_format == "csv":
        output = io.StringIO(newline="")
        writer = csv.writer(output, lineterminator="\r\n")
        writer.writerow(headers)
        for row in rows:
            writer.writerow(
                [
                    _spreadsheet_safe(row["id"]),
                    _spreadsheet_safe(row["read_at"]),
                    _spreadsheet_safe(row["content"]),
                    _spreadsheet_safe(row.get("note", "")),
                ]
            )
        data = ("\ufeff" + output.getvalue()).encode("utf-8")
        file_name = f"qr-records-{now.strftime('%Y%m%d-%H%M%S')}.csv"
        mime_type = "text/csv; charset=utf-8"
    else:
        from openpyxl import Workbook

        workbook = Workbook(write_only=True)
        worksheet = workbook.create_sheet("QR記録")
        worksheet.append(headers)
        for row in rows:
            worksheet.append(
                [
                    _spreadsheet_safe(row["id"]),
                    _spreadsheet_safe(row["read_at"]),
                    _spreadsheet_safe(row["content"]),
                    _spreadsheet_safe(row.get("note", "")),
                ]
            )
        output = io.BytesIO()
        workbook.save(output)
        data = output.getvalue()
        file_name = f"qr-records-{now.strftime('%Y%m%d-%H%M%S')}.xlsx"
        mime_type = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"
    return {
        "fileName": file_name,
        "mimeType": mime_type,
        "data": base64.b64encode(data).decode("ascii"),
        "recordCount": len(rows),
    }


def create_checkout(store, owner_id, now=None):
    owner = store.account_summary(owner_id, now)["owner"]
    if owner.get("kind") != "user":
        raise ApiError(401, "unauthorized", "Paidプランの申込みにはメール認証が必要です。")
    secret_key = os.getenv("STRIPE_SECRET_KEY", "")
    price_id = os.getenv("STRIPE_PRICE_ID", "")
    success_url = os.getenv("STRIPE_SUCCESS_URL", "")
    cancel_url = os.getenv("STRIPE_CANCEL_URL", "")
    if not all((secret_key, price_id, success_url, cancel_url)):
        raise ApiError(503, "stripe_not_configured", "Stripeテストモードの設定がありません。")
    if not secret_key.startswith("sk_test_"):
        raise ApiError(503, "stripe_test_key_required", "このMVPではStripeテストモードのキーのみ利用できます。")
    for candidate in (success_url, cancel_url):
        parsed = urlparse(candidate)
        if parsed.scheme not in ("https", "http") or not parsed.netloc:
            raise ApiError(503, "stripe_url_invalid", "Stripeの戻り先URLが正しく設定されていません。")

    import stripe

    stripe.api_key = secret_key
    try:
        args = {
            "mode": "subscription",
            "line_items": [{"price": price_id, "quantity": 1}],
            "client_reference_id": owner_id,
            "metadata": {"account_id": owner_id},
            "subscription_data": {"metadata": {"account_id": owner_id}},
            "success_url": success_url,
            "cancel_url": cancel_url,
        }
        if owner.get("stripe_customer_id"):
            args["customer"] = owner["stripe_customer_id"]
        else:
            args["customer_email"] = owner["email"]
        checkout = stripe.checkout.Session.create(**args)
    except stripe.StripeError as exc:
        logging.exception("Stripe test checkout creation failed")
        raise ApiError(502, "stripe_checkout_failed", "Stripeテストチェックアウトを作成できませんでした。") from exc
    return {"checkoutUrl": checkout.url}


def _stripe_datetime(timestamp):
    if timestamp is None:
        return None
    try:
        return to_iso(datetime.fromtimestamp(int(timestamp), tz=timezone.utc))
    except (ValueError, TypeError, OverflowError) as exc:
        raise ApiError(400, "stripe_event_invalid", "Stripeの契約期間が正しくありません。") from exc


def process_stripe_event(store, event):
    event_type = event.get("type")
    event_id = event.get("id")
    event_created = int(event.get("created", 0))
    data = event.get("data", {}).get("object", {})
    owner_id = None
    customer_id = data.get("customer")
    subscription_id = None
    status = None
    paid_until = None

    if event_type == "checkout.session.completed":
        subscription_id = data.get("subscription")
        metadata = data.get("metadata") or {}
        owner_id = metadata.get("account_id") or data.get("client_reference_id")
        if not subscription_id:
            return {"received": True, "processed": False}
        secret_key = os.getenv("STRIPE_SECRET_KEY", "")
        if not secret_key.startswith("sk_test_"):
            raise ApiError(503, "stripe_test_key_required", "Stripeテストモードの設定がありません。")
        import stripe

        stripe.api_key = secret_key
        try:
            subscription = stripe.Subscription.retrieve(subscription_id)
        except stripe.StripeError as exc:
            logging.exception("Stripe subscription lookup failed")
            raise ApiError(502, "stripe_subscription_lookup_failed", "Stripe契約状態を確認できませんでした。") from exc
        subscription_data = dict(subscription)
        metadata = subscription_data.get("metadata") or metadata
        owner_id = owner_id or metadata.get("account_id")
        customer_id = subscription_data.get("customer") or customer_id
        subscription_id = subscription_data.get("id") or subscription_id
        status = subscription_data.get("status", "free")
        paid_until = _stripe_datetime(subscription_data.get("current_period_end"))
    elif event_type in (
        "customer.subscription.created",
        "customer.subscription.updated",
        "customer.subscription.deleted",
    ):
        subscription_id = data.get("id")
        owner_id = (data.get("metadata") or {}).get("account_id")
        status = data.get("status", "free")
        paid_until = _stripe_datetime(data.get("current_period_end"))
    else:
        return {"received": True, "processed": False}

    if not owner_id and customer_id:
        owner = store.get_owner_by_customer(customer_id)
        owner_id = owner["owner_id"] if owner else None
    if not owner_id:
        return {"received": True, "processed": False}

    result = store.set_subscription(
        owner_id=owner_id,
        event_id=event_id,
        event_created=event_created,
        status=status,
        paid_until=paid_until,
        customer_id=customer_id,
        subscription_id=subscription_id,
    )
    return {
        "received": True,
        "processed": not result.get("ignored", False),
        "logicalDeleted": result.get("logical_deleted", []),
    }


def dev_activate_paid(store, owner_id, active, now=None):
    if _environment() != "development":
        raise ApiError(404, "not_found", "このAPIは利用できません。")
    owner = store.account_summary(owner_id, now)["owner"]
    if owner.get("kind") != "user":
        raise ApiError(401, "unauthorized", "Paidプランのテストにはメール認証が必要です。")
    current_time = now or utc_now()
    until = current_time + timedelta(days=30) if active else current_time
    event_id = f"local-mock:{uuid.uuid4()}"
    result = store.set_subscription(
        owner_id=owner_id,
        event_id=event_id,
        event_created=int(current_time.timestamp()),
        status="active" if active else "canceled",
        paid_until=to_iso(until),
        now=current_time,
    )
    summary = store.account_summary(owner_id, current_time)
    return {
        "account": account_view(summary, current_time),
        "logicalDeleted": result.get("logical_deleted", []),
    }
