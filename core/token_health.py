from __future__ import annotations

import base64
import json
import time
from typing import Any, Callable, Dict, Optional

from curl_cffi import requests

TOKEN_URL = "https://auth.openai.com/oauth/token"
MODELS_URL = "https://api.openai.com/v1/models"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

DELETED_KEYWORDS = (
    "user_not_found",
    "account_deactivated",
    "account_deleted",
    "user_deactivated",
    "account not found",
    "deleted",
    "deactivated",
    "banned",
    "disabled",
    "suspended",
)


def _normalize_proxy_value(proxy_value: Any) -> str:
    value = str(proxy_value or "").strip().strip('"').strip("'")
    if not value:
        return ""
    if value.startswith("{") or value.startswith("[") or value.startswith("<"):
        return ""
    if "://" in value:
        return value
    if ":" not in value:
        return ""
    return f"http://{value}"


def _to_proxies_dict(proxy_value: Any) -> Optional[Dict[str, str]]:
    normalized = _normalize_proxy_value(proxy_value)
    if not normalized:
        return None
    return {"http": normalized, "https": normalized}


def _jwt_claims_no_verify(id_token: str) -> Dict[str, Any]:
    if not id_token or id_token.count(".") < 2:
        return {}
    payload_b64 = id_token.split(".")[1]
    pad = "=" * ((4 - (len(payload_b64) % 4)) % 4)
    try:
        payload = base64.urlsafe_b64decode((payload_b64 + pad).encode("ascii"))
        return json.loads(payload.decode("utf-8"))
    except Exception:
        return {}


def _contains_deleted_keyword(text: str) -> bool:
    text_lower = str(text or "").lower()
    return any(keyword in text_lower for keyword in DELETED_KEYWORDS)


def _to_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def build_token_record(
    token_payload: Dict[str, Any],
    *,
    fallback_email: str = "",
    fallback_account_id: str = "",
) -> Dict[str, Any]:
    access_token = str(token_payload.get("access_token") or "").strip()
    refresh_token = str(token_payload.get("refresh_token") or "").strip()
    id_token = str(token_payload.get("id_token") or "").strip()
    expires_in = _to_int(token_payload.get("expires_in"))

    missing_fields = [
        field_name
        for field_name, field_value in (
            ("access_token", access_token),
            ("refresh_token", refresh_token),
            ("id_token", id_token),
        )
        if not field_value
    ]
    if missing_fields:
        raise ValueError(f"token exchange missing fields: {', '.join(missing_fields)}")

    claims = _jwt_claims_no_verify(id_token)
    email = str(claims.get("email") or fallback_email or "").strip()
    auth_claims = claims.get("https://api.openai.com/auth") or {}
    account_id = str(
        auth_claims.get("chatgpt_account_id") or fallback_account_id or ""
    ).strip()
    if not email or not account_id:
        raise ValueError("token exchange missing email/account_id in id_token")

    now = int(time.time())
    expires_at = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(now + max(expires_in, 0))
    )
    now_rfc3339 = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(now))

    return {
        "id_token": id_token,
        "access_token": access_token,
        "refresh_token": refresh_token,
        "account_id": account_id,
        "last_refresh": now_rfc3339,
        "expires_at": expires_at,
        "email": email,
        "type": "codex",
        "expired": expires_at,
    }


def build_token_result(
    token_payload: Dict[str, Any],
    *,
    fallback_email: str = "",
    fallback_account_id: str = "",
) -> str:
    return json.dumps(
        build_token_record(
            token_payload,
            fallback_email=fallback_email,
            fallback_account_id=fallback_account_id,
        ),
        ensure_ascii=False,
        separators=(",", ":"),
    )


def check_access_token(
    access_token: str,
    *,
    proxy: str = "",
    max_tries: int = 3,
    request_get: Optional[Callable[..., Any]] = None,
    sleep_func: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    if not access_token:
        return {"status": "expired", "error": "access_token 为空"}

    requester = request_get or requests.get
    proxies = _to_proxies_dict(proxy)
    last_error = ""

    for attempt in range(1, max_tries + 1):
        try:
            resp = requester(
                MODELS_URL,
                headers={
                    "Authorization": f"Bearer {access_token}",
                    "Accept": "application/json",
                },
                proxies=proxies,
                impersonate="chrome",
                timeout=20,
            )
            if int(resp.status_code or 0) == 200:
                return {"status": "alive", "error": ""}

            body = str(getattr(resp, "text", "") or "")
            body_lower = body.lower()

            if int(resp.status_code or 0) == 401:
                if _contains_deleted_keyword(body):
                    return {"status": "deleted", "error": f"HTTP 401: {body[:200]}"}
                return {"status": "expired", "error": "HTTP 401: token 已过期"}

            if int(resp.status_code or 0) == 403:
                if _contains_deleted_keyword(body):
                    return {"status": "deleted", "error": f"HTTP 403: {body[:200]}"}
                if "insufficient permissions" in body_lower or "missing scopes" in body_lower:
                    return {"status": "alive", "error": ""}
                if "country" in body_lower or "unsupported" in body_lower:
                    return {"status": "geo_blocked", "error": "HTTP 403: 地区限制"}
                return {"status": "expired", "error": f"HTTP 403: {body[:200]}"}

            last_error = f"HTTP {resp.status_code}: {body[:200]}"
        except Exception as exc:
            last_error = str(exc)

        if attempt < max_tries:
            sleep_func(min(0.8 * attempt, 2.0))

    return {
        "status": "error",
        "error": f"重试 {max_tries} 次后仍失败: {last_error}",
    }


def refresh_token_payload(
    refresh_token: str,
    *,
    proxy: str = "",
    max_tries: int = 3,
    request_post: Optional[Callable[..., Any]] = None,
    sleep_func: Callable[[float], None] = time.sleep,
) -> Dict[str, Any]:
    if not refresh_token:
        return {
            "status": "token_invalid",
            "token_payload": None,
            "error": "refresh_token 为空",
        }

    requester = request_post or requests.post
    proxies = _to_proxies_dict(proxy)
    last_error = ""

    for attempt in range(1, max_tries + 1):
        try:
            resp = requester(
                TOKEN_URL,
                data={
                    "grant_type": "refresh_token",
                    "client_id": CLIENT_ID,
                    "refresh_token": refresh_token,
                },
                headers={
                    "Content-Type": "application/x-www-form-urlencoded",
                    "Accept": "application/json",
                },
                proxies=proxies,
                impersonate="chrome",
                timeout=30,
            )
            if int(resp.status_code or 0) == 200:
                return {
                    "status": "alive",
                    "token_payload": resp.json(),
                    "error": "",
                }

            error_body = str(getattr(resp, "text", "") or "")
            error_data: Dict[str, Any] = {}
            try:
                error_data = resp.json()
            except Exception:
                error_data = {}

            error_code = str(error_data.get("error", "") or "")
            error_desc = str(error_data.get("error_description", "") or "")
            full_error = f"{error_code}: {error_desc} (HTTP {resp.status_code})"

            if _contains_deleted_keyword(error_body):
                return {"status": "deleted", "token_payload": None, "error": full_error}

            if error_code == "invalid_grant":
                return {
                    "status": "token_invalid",
                    "token_payload": None,
                    "error": full_error,
                }

            last_error = full_error
        except Exception as exc:
            last_error = str(exc)

        if attempt < max_tries:
            sleep_func(min(0.8 * attempt, 2.0))

    return {
        "status": "error",
        "token_payload": None,
        "error": f"重试 {max_tries} 次后仍失败: {last_error}",
    }


def probe_token_health(
    refresh_token: str,
    *,
    access_token: str = "",
    proxy: str = "",
    max_tries: int = 3,
    request_post: Optional[Callable[..., Any]] = None,
    request_get: Optional[Callable[..., Any]] = None,
    sleep_func: Callable[[float], None] = time.sleep,
    fallback_email: str = "",
    fallback_account_id: str = "",
) -> Dict[str, Any]:
    refresh_result = refresh_token_payload(
        refresh_token,
        proxy=proxy,
        max_tries=max_tries,
        request_post=request_post,
        sleep_func=sleep_func,
    )
    refresh_status = str(refresh_result.get("status") or "")

    if refresh_status == "alive":
        token_payload = refresh_result.get("token_payload")
        if isinstance(token_payload, dict):
            verify_result = check_access_token(
                str(token_payload.get("access_token") or ""),
                proxy=proxy,
                max_tries=max_tries,
                request_get=request_get,
                sleep_func=sleep_func,
            )
            verify_status = str(verify_result.get("status") or "")
            if verify_status in ("alive", "geo_blocked"):
                return {
                    "status": "alive",
                    "error": str(verify_result.get("error") or ""),
                    "token_payload": token_payload,
                    "token_data": build_token_record(
                        token_payload,
                        fallback_email=fallback_email,
                        fallback_account_id=fallback_account_id,
                    ),
                    "verification_status": verify_status,
                    "used_refresh": True,
                }
            if verify_status == "deleted":
                return {
                    "status": "deleted",
                    "error": f"refresh 成功但账号已停用: {verify_result.get('error')}",
                    "token_payload": None,
                    "token_data": None,
                    "verification_status": verify_status,
                    "used_refresh": True,
                }
            return {
                "status": "token_invalid",
                "error": f"refresh 成功但 API 验证失败: {verify_result.get('error')}",
                "token_payload": None,
                "token_data": None,
                "verification_status": verify_status,
                "used_refresh": True,
            }

    if refresh_status == "deleted":
        return {
            "status": "deleted",
            "error": str(refresh_result.get("error") or ""),
            "token_payload": None,
            "token_data": None,
            "verification_status": "",
            "used_refresh": False,
        }

    fallback_access_token = str(access_token or "").strip()
    if fallback_access_token:
        verify_result = check_access_token(
            fallback_access_token,
            proxy=proxy,
            max_tries=max_tries,
            request_get=request_get,
            sleep_func=sleep_func,
        )
        verify_status = str(verify_result.get("status") or "")
        if verify_status == "alive":
            return {
                "status": "alive",
                "error": "",
                "token_payload": None,
                "token_data": None,
                "verification_status": verify_status,
                "used_refresh": False,
            }
        if verify_status == "deleted":
            return {
                "status": "deleted",
                "error": str(verify_result.get("error") or ""),
                "token_payload": None,
                "token_data": None,
                "verification_status": verify_status,
                "used_refresh": False,
            }
        if verify_status == "geo_blocked":
            return {
                "status": "geo_blocked",
                "error": (
                    f"refresh 失败 ({refresh_result.get('error') or 'unknown'}), "
                    f"API 地区限制 ({verify_result.get('error') or 'unknown'})"
                ),
                "token_payload": None,
                "token_data": None,
                "verification_status": verify_status,
                "used_refresh": False,
            }

    return {
        "status": refresh_status if refresh_status in ("token_invalid", "error") else "error",
        "error": str(refresh_result.get("error") or "未知错误"),
        "token_payload": None,
        "token_data": None,
        "verification_status": "",
        "used_refresh": False,
    }
