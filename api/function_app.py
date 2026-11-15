import json
import hashlib
import logging
import os
import re
from dataclasses import dataclass, field
from http.cookies import CookieError, SimpleCookie

import azure.functions as func

from api.domain import ApiError, require_uuid4
from api.service import (
    build_store,
    create_checkout,
    create_record,
    delete_record,
    dev_activate_paid,
    export_records,
    get_session,
    list_records,
    process_stripe_event,
    request_verification,
    resolve_identity,
    update_record,
    verify_email,
)

app = func.FunctionApp(http_auth_level=func.AuthLevel.ANONYMOUS)
_store = None


@dataclass
class ApiResponse:
    body: dict
    status_code: int = 200
    headers: dict = field(default_factory=dict)


def _get_store():
    global _store
    if _store is None:
        _store = build_store()
    return _store


def _request_data(request):
    try:
        form = request.form
        idx = require_uuid4(form.get("idx"), "リクエストID")
        sid = require_uuid4(form.get("sid"), "ブラウザID")
        button_id = form.get("n")
        if not isinstance(button_id, str) or not re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,100}", button_id):
            raise ApiError(400, "invalid_request", "リクエスト元のボタンIDが正しくありません。")
        raw_payload = form.get("payload", "{}")
        payload = json.loads(raw_payload)
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        return {"idx": idx, "sid": sid, "button_id": button_id}, payload
    except ApiError:
        raise
    except (ValueError, TypeError, json.JSONDecodeError) as exc:
        raise ApiError(400, "invalid_request", "フォームデータまたはJSONが正しくありません。") from exc


def _cookie(request):
    cookies = SimpleCookie()
    try:
        cookies.load(request.headers.get("Cookie", ""))
    except CookieError as exc:
        raise ApiError(400, "invalid_cookie", "ログインCookieの形式が正しくありません。") from exc
    session_cookie = cookies.get("qrs_session")
    return session_cookie.value if session_cookie else None


def _session_cookie(token, clear=False):
    value = "" if clear else token
    attributes = ["qrs_session=" + value, "Path=/", "HttpOnly", "SameSite=Lax"]
    if clear:
        attributes.append("Max-Age=0")
    else:
        attributes.append("Max-Age=2592000")
    if os.getenv("APP_ENV", "production").lower() != "development":
        attributes.append("Secure")
    return "; ".join(attributes)


def _json_response(body, status_code=200, headers=None):
    response_headers = {
        "Cache-Control": "no-store",
        "X-Content-Type-Options": "nosniff",
    }
    if headers:
        response_headers.update(headers)
    return func.HttpResponse(
        body=json.dumps(body, ensure_ascii=False, separators=(",", ":")),
        status_code=status_code,
        mimetype="application/json",
        charset="utf-8",
        headers=response_headers,
    )


def _execute(request, handler):
    request_id = None
    try:
        context, payload = _request_data(request)
        request_id = context["idx"]
        result = handler(_get_store(), context, payload, request)
        if isinstance(result, ApiResponse):
            return _json_response(result.body, result.status_code, result.headers)
        return _json_response({"ok": True, **result})
    except ApiError as exc:
        return _json_response(
            {"ok": False, "error": {"code": exc.code, "message": exc.message}},
            exc.status_code,
        )
    except Exception:
        logging.exception("Request processing failed (request_id=%s)", request_id or "unavailable")
        return _json_response(
            {
                "ok": False,
                "error": {
                    "code": "internal_error",
                    "message": "処理に失敗しました。時間をおいて再試行してください。",
                },
            },
            500,
        )


@app.route(route="v1/Health", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def Health(req: func.HttpRequest) -> func.HttpResponse:
    return _execute(
        req,
        lambda store, context, payload, req: {"status": "ok", "version": "mvp"},
    )


@app.route(route="v1/RequestVerification", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def RequestVerification(req: func.HttpRequest) -> func.HttpResponse:
    return _execute(
        req,
        lambda store, context, payload, req: request_verification(
            store,
            context["sid"],
            payload,
        ),
    )


@app.route(route="v1/VerifyEmail", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def VerifyEmail(req: func.HttpRequest) -> func.HttpResponse:
    def handler(store, context, payload, req):
        result = verify_email(store, context["sid"], payload)
        token = result.pop("sessionToken")
        return ApiResponse(
            {"ok": True, **result},
            headers={"Set-Cookie": _session_cookie(token)},
        )

    return _execute(req, handler)


@app.route(route="v1/GetSession", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def GetSession(req: func.HttpRequest) -> func.HttpResponse:
    def handler(store, context, payload, req):
        result = get_session(store, context["sid"], _cookie(req))
        headers = {"Set-Cookie": _session_cookie("", clear=True)} if result.pop("clearCookie") else None
        return ApiResponse({"ok": True, **result}, headers=headers or {})

    return _execute(req, handler)


@app.route(route="v1/Logout", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def Logout(req: func.HttpRequest) -> func.HttpResponse:
    def handler(store, context, payload, req):
        token = _cookie(req)
        if token:
            owner_id = store.authenticate(
                hashlib.sha256(token.encode("utf-8")).hexdigest(),
                context["sid"],
            )
            if owner_id:
                store.logout(
                    hashlib.sha256(token.encode("utf-8")).hexdigest(),
                    context["sid"],
                )
        return ApiResponse(
            {"ok": True, "authenticated": False},
            headers={"Set-Cookie": _session_cookie("", clear=True)},
        )

    return _execute(req, handler)


@app.route(route="v1/CreateQr", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def CreateQr(req: func.HttpRequest) -> func.HttpResponse:
    def handler(store, context, payload, req):
        owner_id, _ = resolve_identity(store, context["sid"], _cookie(req))
        return create_record(store, owner_id, payload.get("record"))

    return _execute(req, handler)


@app.route(route="v1/GetQr", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def GetQr(req: func.HttpRequest) -> func.HttpResponse:
    def handler(store, context, payload, req):
        owner_id, _ = resolve_identity(store, context["sid"], _cookie(req))
        return list_records(store, owner_id, payload)

    return _execute(req, handler)


@app.route(route="v1/UpdateQr", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def UpdateQr(req: func.HttpRequest) -> func.HttpResponse:
    def handler(store, context, payload, req):
        owner_id, authenticated = resolve_identity(store, context["sid"], _cookie(req))
        if not authenticated:
            raise ApiError(401, "unauthorized", "記録を編集するにはログインしてください。")
        return update_record(store, owner_id, payload)

    return _execute(req, handler)


@app.route(route="v1/DeleteQr", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def DeleteQr(req: func.HttpRequest) -> func.HttpResponse:
    def handler(store, context, payload, req):
        owner_id, _ = resolve_identity(store, context["sid"], _cookie(req))
        return delete_record(store, owner_id, payload)

    return _execute(req, handler)


@app.route(route="v1/ExportQr", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def ExportQr(req: func.HttpRequest) -> func.HttpResponse:
    def handler(store, context, payload, req):
        owner_id, authenticated = resolve_identity(store, context["sid"], _cookie(req))
        if not authenticated:
            raise ApiError(401, "unauthorized", "エクスポートするにはログインしてください。")
        return export_records(store, owner_id, payload)

    return _execute(req, handler)


@app.route(route="v1/CreateCheckout", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def CreateCheckout(req: func.HttpRequest) -> func.HttpResponse:
    def handler(store, context, payload, req):
        owner_id, authenticated = resolve_identity(store, context["sid"], _cookie(req))
        if not authenticated:
            raise ApiError(401, "unauthorized", "Paidプランの申込みにはログインしてください。")
        return create_checkout(store, owner_id)

    return _execute(req, handler)


@app.route(route="v1/DevActivatePaid", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def DevActivatePaid(req: func.HttpRequest) -> func.HttpResponse:
    def handler(store, context, payload, req):
        owner_id, authenticated = resolve_identity(store, context["sid"], _cookie(req))
        if not authenticated:
            raise ApiError(401, "unauthorized", "Paidプランのテストにはログインしてください。")
        return dev_activate_paid(store, owner_id, payload.get("active") is not False)

    return _execute(req, handler)


@app.route(route="v1/StripeWebhook", methods=["POST"], auth_level=func.AuthLevel.ANONYMOUS)
def StripeWebhook(req: func.HttpRequest) -> func.HttpResponse:
    request_id = None
    try:
        import stripe

        webhook_secret = os.getenv("STRIPE_WEBHOOK_SECRET", "")
        secret_key = os.getenv("STRIPE_SECRET_KEY", "")
        if not webhook_secret or not secret_key.startswith("sk_test_"):
            raise ApiError(503, "stripe_webhook_not_configured", "Stripeテストモードのwebhook設定がありません。")
        signature = req.headers.get("Stripe-Signature", "")
        if not signature:
            raise ApiError(400, "stripe_signature_missing", "Stripe署名がありません。")
        try:
            event = stripe.Webhook.construct_event(
                req.get_body(),
                signature,
                webhook_secret,
            )
        except (ValueError, stripe.SignatureVerificationError) as exc:
            raise ApiError(400, "stripe_signature_invalid", "Stripe webhookの署名を検証できませんでした。") from exc
        result = process_stripe_event(_get_store(), dict(event))
        return _json_response({"ok": True, **result})
    except ApiError as exc:
        return _json_response(
            {"ok": False, "error": {"code": exc.code, "message": exc.message}},
            exc.status_code,
        )
    except Exception:
        logging.exception("Stripe webhook processing failed (event_id=%s)", request_id or "unknown")
        return _json_response(
            {
                "ok": False,
                "error": {
                    "code": "stripe_webhook_failed",
                    "message": "Stripe webhookの処理に失敗しました。",
                },
            },
            500,
        )
