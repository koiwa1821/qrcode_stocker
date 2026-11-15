import base64
import json
import re
import uuid
from datetime import datetime, timedelta, timezone


class ApiError(Exception):
    def __init__(self, status_code, code, message):
        super().__init__(message)
        self.status_code = status_code
        self.code = code
        self.message = message


def utc_now():
    return datetime.now(timezone.utc)


def to_iso(value):
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%S.%fZ")


def parse_iso(value, field_name="日時"):
    if not isinstance(value, str) or not value:
        raise ApiError(400, "invalid_datetime", f"{field_name}を指定してください。")
    try:
        parsed = datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError as exc:
        raise ApiError(400, "invalid_datetime", f"{field_name}の形式が正しくありません。") from exc
    if parsed.tzinfo is None:
        raise ApiError(400, "invalid_datetime", f"{field_name}にはタイムゾーンが必要です。")
    return parsed.astimezone(timezone.utc)


def normalize_email(value):
    if not isinstance(value, str):
        raise ApiError(400, "invalid_email", "有効なメールアドレスを入力してください。")
    email = value.strip().lower()
    if len(email) > 254 or not re.fullmatch(r"[^@\s]+@[^@\s]+\.[^@\s]+", email):
        raise ApiError(400, "invalid_email", "有効なメールアドレスを入力してください。")
    return email


def require_uuid4(value, field_name):
    if not isinstance(value, str):
        raise ApiError(400, "invalid_id", f"{field_name}の形式が正しくありません。")
    try:
        parsed = uuid.UUID(value)
    except (ValueError, AttributeError) as exc:
        raise ApiError(400, "invalid_id", f"{field_name}の形式が正しくありません。") from exc
    if parsed.version != 4:
        raise ApiError(400, "invalid_id", f"{field_name}にはUUID v4を指定してください。")
    return str(parsed)


def normalize_record(value, now=None):
    if not isinstance(value, dict):
        raise ApiError(400, "invalid_record", "記録データが正しくありません。")
    record_id = require_uuid4(value.get("id"), "記録ID")
    content = value.get("content")
    if not isinstance(content, str) or not content.strip() or len(content) > 8192:
        raise ApiError(400, "invalid_content", "QRコードの内容は1〜8192文字で入力してください。")
    note = value.get("note", "")
    if note is None:
        note = ""
    if not isinstance(note, str) or len(note) > 2000:
        raise ApiError(400, "invalid_note", "メモは2000文字以内で入力してください。")
    read_at = parse_iso(value.get("readAt"), "読み取り日時")
    if now is not None and read_at > now + timedelta(minutes=5):
        raise ApiError(400, "future_read_time", "読み取り日時が未来になっています。")
    return {
        "id": record_id,
        "content": content,
        "read_at": to_iso(read_at),
        "note": note.strip(),
    }


def is_paid(owner, now=None):
    status = (owner or {}).get("subscription_status", "free")
    if status not in ("active", "trialing"):
        return False
    paid_until = (owner or {}).get("paid_until")
    if not paid_until:
        return True
    current_time = now or utc_now()
    try:
        return parse_iso(paid_until, "契約終了日時") > current_time
    except ApiError:
        return False


def plan_for(owner, now=None):
    return "paid" if is_paid(owner, now) else "free"


def record_limit(owner, now=None):
    if (owner or {}).get("kind") == "guest":
        return 100
    return 10000 if is_paid(owner, now) else 500


def device_limit(owner, now=None):
    return 100 if is_paid(owner, now) else 2


def encode_cursor(read_at, record_id, query):
    value = json.dumps(
        {"read_at": read_at, "id": record_id, "query": query},
        separators=(",", ":"),
        ensure_ascii=True,
    ).encode("utf-8")
    return base64.urlsafe_b64encode(value).decode("ascii").rstrip("=")


def decode_cursor(value, query):
    if value in (None, ""):
        return None
    if not isinstance(value, str) or len(value) > 2048:
        raise ApiError(400, "invalid_cursor", "一覧カーソルが正しくありません。")
    try:
        padded = value + ("=" * (-len(value) % 4))
        decoded = json.loads(base64.urlsafe_b64decode(padded.encode("ascii")))
        cursor_id = require_uuid4(decoded["id"], "カーソルID")
        read_at = parse_iso(decoded["read_at"], "カーソル日時")
        if decoded.get("query", "") != query:
            raise ValueError("search query mismatch")
        return {"read_at": to_iso(read_at), "id": cursor_id}
    except (ValueError, KeyError, TypeError, json.JSONDecodeError) as exc:
        raise ApiError(400, "invalid_cursor", "一覧カーソルが正しくありません。") from exc


def public_record(row):
    return {
        "id": row["id"],
        "content": row["content"],
        "readAt": row["read_at"],
        "note": row.get("note", ""),
    }


def account_view(summary, now=None):
    owner = summary["owner"]
    paid = is_paid(owner, now)
    return {
        "id": owner["owner_id"],
        "email": owner.get("email"),
        "plan": "paid" if paid else "free",
        "subscriptionStatus": owner.get("subscription_status", "free"),
        "paidUntil": owner.get("paid_until"),
        "activeCount": summary["active_count"],
        "recordLimit": record_limit(owner, now),
        "authenticatedBrowserCount": summary["device_count"],
        "browserLimit": device_limit(owner, now) if owner.get("kind") == "user" else None,
    }
