"""
MailProvider 抽象层
支持 Mail.tm / MoeMail / DuckMail / 自定义 API
"""

from __future__ import annotations

import itertools
import random
import re
import secrets
import string
import time
import threading
from abc import ABC, abstractmethod
from typing import Any, Dict, List, Optional, Tuple, Callable
from urllib.parse import parse_qs, urlparse

import httpx

from .logger import get_logger

logger = get_logger(__name__)
_RETRYABLE_STATUS_CODES = frozenset({429, 500, 502, 503, 504})
_REQUEST_RETRY_BACKOFF = 0.2
_DEFAULT_TIMEOUT = httpx.Timeout(30.0, connect=15.0)
_STOP_AWARE_REQUEST_TIMEOUT_SECONDS = 5.0


def _interruptible_timeout(timeout: Any, stop_event: Optional[threading.Event] = None) -> Any:
    if stop_event is None or not stop_event.is_set():
        return timeout
    try:
        numeric_timeout = float(timeout)
    except (TypeError, ValueError):
        return timeout
    return max(1.0, min(numeric_timeout, _STOP_AWARE_REQUEST_TIMEOUT_SECONDS))


def _wait_with_stop(delay_seconds: float, stop_event: Optional[threading.Event] = None) -> bool:
    delay = max(0.0, float(delay_seconds or 0.0))
    if delay <= 0:
        return not (stop_event and stop_event.is_set())
    if stop_event is None:
        time.sleep(delay)
        return True
    return not stop_event.wait(delay)


def _normalize_proxy_url(proxy: str) -> str:
    value = str(proxy or "").strip()
    if not value:
        return ""
    if "://" in value:
        return value
    if ":" in value:
        return f"http://{value}"
    return ""


def _build_http_transport(proxy: str = "", retries: int = 0) -> httpx.HTTPTransport:
    return httpx.HTTPTransport(
        verify=False,
        proxy=_normalize_proxy_url(proxy) or None,
        retries=max(0, int(retries)),
    )


def _request_with_status_retries(
    request: Callable[..., httpx.Response],
    method: str,
    url: str,
    retry_total: int,
    **kwargs,
) -> httpx.Response:
    response: Optional[httpx.Response] = None
    for attempt in range(retry_total + 1):
        response = request(method, url, **kwargs)
        if response.status_code not in _RETRYABLE_STATUS_CODES or attempt >= retry_total:
            return response
        time.sleep(_REQUEST_RETRY_BACKOFF * (2 ** attempt))
    if response is None:
        raise RuntimeError("HTTP request did not produce a response")
    return response


class _MailHttpClient(httpx.Client):
    def __init__(
        self,
        proxy: str = "",
        proxy_selector: Optional[Callable[[], str]] = None,
        proxy_reporter: Optional[Callable[[str, bool, Any], None]] = None,
    ):
        self._default_proxy = _normalize_proxy_url(proxy)
        self._selected_proxy = self._resolve_proxy(proxy_selector)
        self._proxy_reporter = proxy_reporter
        self._retry_total = 0 if proxy_selector else 2
        self._fallback_client: Optional[httpx.Client] = None
        super().__init__(
            transport=_build_http_transport(self._selected_proxy, retries=self._retry_total),
            timeout=_DEFAULT_TIMEOUT,
            follow_redirects=True,
        )
        if self._selected_proxy and self._default_proxy and self._selected_proxy != self._default_proxy:
            self._fallback_client = httpx.Client(
                transport=_build_http_transport(self._default_proxy, retries=self._retry_total),
                timeout=_DEFAULT_TIMEOUT,
                follow_redirects=True,
            )

    def _resolve_proxy(self, proxy_selector: Optional[Callable[[], str]]) -> str:
        if proxy_selector is not None:
            try:
                selected_proxy = _normalize_proxy_url(proxy_selector() or "")
            except Exception:
                selected_proxy = ""
            if selected_proxy:
                return selected_proxy
        return self._default_proxy

    def _report_proxy_result(self, ok: bool, detail: Any = None) -> None:
        if not self._selected_proxy or self._proxy_reporter is None:
            return
        try:
            self._proxy_reporter(self._selected_proxy, ok, detail)
        except Exception:
            pass

    def request(self, method: str, url: str, **kwargs) -> httpx.Response:
        try:
            response = _request_with_status_retries(
                super().request,
                method,
                url,
                retry_total=self._retry_total,
                **kwargs,
            )
            if response.status_code in _RETRYABLE_STATUS_CODES or response.status_code == 407:
                self._report_proxy_result(False, f"HTTP {response.status_code}")
            else:
                self._report_proxy_result(True)
            return response
        except httpx.HTTPError:
            self._report_proxy_result(False, "mail provider proxy request failed")
            if self._fallback_client is None:
                raise
            return _request_with_status_retries(
                self._fallback_client.request,
                method,
                url,
                retry_total=self._retry_total,
                **kwargs,
            )

    def close(self) -> None:
        if self._fallback_client is not None:
            self._fallback_client.close()
        super().close()


def _build_client(
    proxy: str = "",
    proxy_selector: Optional[Callable[[], str]] = None,
    proxy_reporter: Optional[Callable[[str, bool, Any], None]] = None,
) -> httpx.Client:
    return _MailHttpClient(proxy, proxy_selector, proxy_reporter)


def _extract_code(content: str) -> Optional[str]:
    if not content:
        return None
    m = re.search(r"background-color:\s*#F3F3F3[^>]*>[\s\S]*?(\d{6})[\s\S]*?</p>", content)
    if m:
        return m.group(1)
    for pat in [
        r"Verification code:?\s*(\d{6})",
        r"code is\s*(\d{6})",
        r"Subject:.*?(\d{6})",
        r">\s*(\d{6})\s*<",
        r"(?<![#&])\b(\d{6})\b",
    ]:
        for code in re.findall(pat, content, re.IGNORECASE):
            return code
    return None


def _stringify_message_part(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return "\n".join(part for part in (_stringify_message_part(item) for item in value) if part)
    if isinstance(value, dict):
        return "\n".join(part for part in (_stringify_message_part(item) for item in value.values()) if part)
    return str(value)


def _merge_message_content(*parts: Any) -> str:
    return "\n".join(
        content for content in (_stringify_message_part(part) for part in parts) if content
    )


def _extract_raw_email_content(raw_message: Any) -> str:
    raw_text = _stringify_message_part(raw_message)
    if not raw_text:
        return ""

    import email as _email_mod
    from email import policy

    parsed = _email_mod.message_from_string(raw_text, policy=policy.default)
    body = parsed.get_body(preferencelist=("plain", "html"))
    if body:
        content = _stringify_message_part(body.get_content())
        if content:
            return content

    for part in parsed.walk():
        if part.get_content_type() not in ("text/plain", "text/html"):
            continue
        content = _stringify_message_part(part.get_content())
        if content:
            return content

    return raw_text


# ==================== 抽象基类 ====================

class MailProvider(ABC):
    @abstractmethod
    def create_mailbox(
        self,
        proxy: str = "",
        proxy_selector: Optional[Callable[[], str]] = None,
        proxy_reporter: Optional[Callable[[str, bool, Any], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Tuple[str, str]:
        """返回 (email, auth_credential)，auth_credential 是 bearer token 或 email_id"""

    @abstractmethod
    def wait_for_otp(
        self,
        auth_credential: str,
        email: str,
        proxy: str = "",
        proxy_selector: Optional[Callable[[], str]] = None,
        proxy_reporter: Optional[Callable[[str, bool, Any], None]] = None,
        timeout: int = 120,
        stop_event: Optional[threading.Event] = None,
    ) -> str:
        """轮询获取6位验证码，超时返回空字符串"""

    def test_connection(self, proxy: str = "") -> Tuple[bool, str]:
        """测试 API 连通性，返回 (success, message)"""
        try:
            email, cred = self.create_mailbox(proxy)
            if email and cred:
                return True, f"成功创建测试邮箱: {email}"
            return False, "创建邮箱失败，请检查配置"
        except Exception as e:
            return False, f"连接失败: {e}"

    def close(self):
        pass


# ==================== Mail.tm ====================

class MailTmProvider(MailProvider):
    def __init__(self, api_base: str = "https://api.mail.tm"):
        self.api_base = api_base.rstrip("/")

    def _headers(self, token: str = "", use_json: bool = False) -> Dict[str, str]:
        h: Dict[str, str] = {"Accept": "application/json"}
        if use_json:
            h["Content-Type"] = "application/json"
        if token:
            h["Authorization"] = f"Bearer {token}"
        return h

    def _get_domains(
        self,
        client: httpx.Client,
        stop_event: Optional[threading.Event] = None,
    ) -> List[str]:
        resp = client.get(
            f"{self.api_base}/domains",
            headers=self._headers(),
            timeout=_interruptible_timeout(15, stop_event),
        )
        if resp.status_code != 200:
            return []
        data = resp.json()
        items = data if isinstance(data, list) else (data.get("hydra:member") or data.get("items") or [])
        domains = []
        for item in items:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or "").strip()
            if domain and item.get("isActive", True) and not item.get("isPrivate", False):
                domains.append(domain)
        return domains

    def create_mailbox(
        self,
        proxy: str = "",
        proxy_selector: Optional[Callable[[], str]] = None,
        proxy_reporter: Optional[Callable[[str, bool, Any], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Tuple[str, str]:
        with _build_client(proxy, proxy_selector, proxy_reporter) as client:
            domains = self._get_domains(client, stop_event=stop_event)
            if not domains:
                return "", ""
            domain = random.choice(domains)

            for _ in range(5):
                if stop_event and stop_event.is_set():
                    return "", ""
                local = f"oc{secrets.token_hex(5)}"
                email = f"{local}@{domain}"
                password = secrets.token_urlsafe(18)
                request_timeout = _interruptible_timeout(15, stop_event)

                resp = client.post(
                    f"{self.api_base}/accounts",
                    headers=self._headers(use_json=True),
                    json={"address": email, "password": password},
                    timeout=request_timeout,
                )
                if resp.status_code not in (200, 201):
                    continue

                token_resp = client.post(
                    f"{self.api_base}/token",
                    headers=self._headers(use_json=True),
                    json={"address": email, "password": password},
                    timeout=request_timeout,
                )
                if token_resp.status_code == 200:
                    token = str(token_resp.json().get("token") or "").strip()
                    if token:
                        return email, token
        return "", ""

    def wait_for_otp(
        self,
        auth_credential: str,
        email: str,
        proxy: str = "",
        proxy_selector: Optional[Callable[[], str]] = None,
        proxy_reporter: Optional[Callable[[str, bool, Any], None]] = None,
        timeout: int = 120,
        stop_event: Optional[threading.Event] = None,
    ) -> str:
        with _build_client(proxy, proxy_selector, proxy_reporter) as client:
            seen_ids: set = set()
            start = time.time()

            while time.time() - start < timeout:
                if stop_event and stop_event.is_set():
                    return ""
                try:
                    request_timeout = _interruptible_timeout(15, stop_event)
                    resp = client.get(
                        f"{self.api_base}/messages",
                        headers=self._headers(token=auth_credential),
                        timeout=request_timeout,
                    )
                    if resp.status_code != 200:
                        if not _wait_with_stop(3, stop_event):
                            return ""
                        continue

                    data = resp.json()
                    messages = data if isinstance(data, list) else (
                        data.get("hydra:member") or data.get("messages") or []
                    )

                    for msg in messages:
                        if not isinstance(msg, dict):
                            continue
                        msg_id = str(msg.get("id") or msg.get("@id") or "").strip()
                        if not msg_id or msg_id in seen_ids:
                            continue

                        if msg_id.startswith("/messages/"):
                            msg_id = msg_id.split("/")[-1]

                        detail_resp = client.get(
                            f"{self.api_base}/messages/{msg_id}",
                            headers=self._headers(token=auth_credential),
                            timeout=request_timeout,
                        )
                        if detail_resp.status_code != 200:
                            continue
                        seen_ids.add(msg_id)

                        mail_data = detail_resp.json()
                        sender = str(((mail_data.get("from") or {}).get("address") or "")).lower()
                        content = _merge_message_content(
                            mail_data.get("subject"),
                            mail_data.get("intro"),
                            mail_data.get("text"),
                            mail_data.get("html"),
                        )

                        if "openai" not in sender and "openai" not in content.lower():
                            continue

                        code = _extract_code(content)
                        if code:
                            return code
                except Exception as exc:
                    logger.warning("Mail.tm 轮询验证码失败: {}", exc)
                if not _wait_with_stop(3, stop_event):
                    return ""
        return ""


# ==================== MoeMail ====================

class MoeMailProvider(MailProvider):
    def __init__(self, api_base: str, api_key: str):
        self.api_base = api_base.rstrip("/")
        self.api_key = api_key

    def _headers(self) -> Dict[str, str]:
        return {"X-API-Key": self.api_key}

    def _get_domain(
        self,
        client: httpx.Client,
        stop_event: Optional[threading.Event] = None,
    ) -> Optional[str]:
        try:
            resp = client.get(
                f"{self.api_base}/api/config",
                headers=self._headers(),
                timeout=_interruptible_timeout(10, stop_event),
            )
            if resp.status_code == 200:
                data = resp.json()
                domains_str = data.get("emailDomains", "")
                if domains_str:
                    domains = [d.strip() for d in domains_str.split(",") if d.strip()]
                    if domains:
                        return random.choice(domains)
        except Exception as exc:
            logger.warning("MoeMail 读取域名配置失败: {}", exc)
        return None

    def create_mailbox(
        self,
        proxy: str = "",
        proxy_selector: Optional[Callable[[], str]] = None,
        proxy_reporter: Optional[Callable[[str, bool, Any], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Tuple[str, str]:
        with _build_client(proxy, proxy_selector, proxy_reporter) as client:
            if stop_event and stop_event.is_set():
                return "", ""
            domain = self._get_domain(client, stop_event=stop_event)
            if not domain:
                return "", ""

            chars = string.ascii_lowercase + string.digits
            prefix = "".join(random.choice(chars) for _ in range(random.randint(8, 13)))

            try:
                request_timeout = _interruptible_timeout(15, stop_event)
                resp = client.post(
                    f"{self.api_base}/api/emails/generate",
                    json={"name": prefix, "domain": domain, "expiryTime": 0},
                    headers=self._headers(), timeout=request_timeout,
                )
                if resp.status_code not in (200, 201):
                    return "", ""
                data = resp.json()
                email_id = data.get("id")
                email = data.get("email")
                if email_id and email:
                    return email, str(email_id)
            except Exception as exc:
                logger.warning("MoeMail 创建邮箱失败: {}", exc)
        return "", ""

    def wait_for_otp(
        self,
        auth_credential: str,
        email: str,
        proxy: str = "",
        proxy_selector: Optional[Callable[[], str]] = None,
        proxy_reporter: Optional[Callable[[str, bool, Any], None]] = None,
        timeout: int = 120,
        stop_event: Optional[threading.Event] = None,
    ) -> str:
        with _build_client(proxy, proxy_selector, proxy_reporter) as client:
            email_id = auth_credential
            start = time.time()

            while time.time() - start < timeout:
                if stop_event and stop_event.is_set():
                    return ""
                try:
                    request_timeout = _interruptible_timeout(15, stop_event)
                    resp = client.get(
                        f"{self.api_base}/api/emails/{email_id}",
                        headers=self._headers(), timeout=request_timeout,
                    )
                    if resp.status_code == 200:
                        messages = resp.json().get("messages") or []
                        for msg in messages:
                            if not isinstance(msg, dict):
                                continue
                            msg_id = msg.get("id")
                            if not msg_id:
                                continue
                            detail_resp = client.get(
                                f"{self.api_base}/api/emails/{email_id}/{msg_id}",
                                headers=self._headers(), timeout=request_timeout,
                            )
                            if detail_resp.status_code == 200:
                                detail = detail_resp.json()
                                msg_obj = detail.get("message") or {}
                                content = _merge_message_content(
                                    msg_obj.get("content"),
                                    msg_obj.get("html"),
                                )
                                if not content:
                                    content = _merge_message_content(
                                        detail.get("text"),
                                        detail.get("html"),
                                    )
                                code = _extract_code(content)
                                if code:
                                    return code
                except Exception as exc:
                    logger.warning("MoeMail 轮询验证码失败: {}", exc)
                if not _wait_with_stop(3, stop_event):
                    return ""
        return ""


# ==================== DuckMail ====================

class DuckMailProvider(MailProvider):
    def __init__(self, api_base: str = "https://api.duckmail.sbs", bearer_token: str = ""):
        self.api_base = api_base.rstrip("/")
        self.bearer_token = bearer_token

    def _auth_headers(
        self,
        token: str = "",
        use_json: bool = False,
        use_provider_token: bool = False,
    ) -> Dict[str, str]:
        h: Dict[str, str] = {"Accept": "application/json"}
        if use_json:
            h["Content-Type"] = "application/json"
        auth_token = str(token or "").strip()
        if not auth_token and use_provider_token:
            auth_token = str(self.bearer_token or "").strip()
        if auth_token:
            h["Authorization"] = f"Bearer {auth_token}"
        return h

    @staticmethod
    def _next_page_number(view: Any) -> Optional[int]:
        if not isinstance(view, dict):
            return None
        next_ref = str(view.get("hydra:next") or "").strip()
        if not next_ref:
            return None
        try:
            values = parse_qs(urlparse(next_ref).query).get("page") or []
            page = int(str(values[0]).strip())
        except Exception:
            return None
        return page if page > 1 else None

    @staticmethod
    def _extract_domains(payload: Any) -> List[str]:
        items = payload if isinstance(payload, list) else (
            payload.get("hydra:member") or payload.get("items") or payload.get("data") or []
        )
        domains: List[str] = []
        for item in items if isinstance(items, list) else []:
            if not isinstance(item, dict):
                continue
            domain = str(item.get("domain") or item.get("name") or "").strip()
            if not domain:
                continue
            if item.get("isActive") is False or item.get("isVerified") is False:
                continue
            domains.append(domain)
        return domains

    def _get_domains(
        self,
        client: httpx.Client,
        stop_event: Optional[threading.Event] = None,
    ) -> List[str]:
        domains: List[str] = []
        seen_pages: set[int] = set()
        page = 1

        while page not in seen_pages:
            if stop_event and stop_event.is_set():
                return []
            seen_pages.add(page)
            resp = client.get(
                f"{self.api_base}/domains",
                params={"page": page},
                headers=self._auth_headers(use_provider_token=True),
                timeout=_interruptible_timeout(15, stop_event),
            )
            if resp.status_code != 200:
                return []

            data = resp.json()
            domains.extend(self._extract_domains(data))
            if isinstance(data, list):
                break

            next_page = self._next_page_number(data.get("hydra:view") if isinstance(data, dict) else None)
            if not next_page:
                break
            page = next_page

        return list(dict.fromkeys(domains))

    def create_mailbox(
        self,
        proxy: str = "",
        proxy_selector: Optional[Callable[[], str]] = None,
        proxy_reporter: Optional[Callable[[str, bool, Any], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Tuple[str, str]:
        with _build_client(proxy, proxy_selector, proxy_reporter) as client:
            try:
                domains = self._get_domains(client, stop_event=stop_event)
                if not domains:
                    return "", ""

                for _ in range(5):
                    if stop_event and stop_event.is_set():
                        return "", ""
                    domain = random.choice(domains)
                    local = f"oc{secrets.token_hex(5)}"
                    email = f"{local}@{domain}"
                    password = secrets.token_urlsafe(18)
                    request_timeout = _interruptible_timeout(30, stop_event)

                    resp = client.post(
                        f"{self.api_base}/accounts",
                        json={"address": email, "password": password},
                        headers=self._auth_headers(use_json=True, use_provider_token=True),
                        timeout=request_timeout,
                    )
                    if resp.status_code == 409:
                        continue
                    if resp.status_code not in (200, 201):
                        return "", ""

                    if not _wait_with_stop(0.5, stop_event):
                        return "", ""
                    token_resp = client.post(
                        f"{self.api_base}/token",
                        json={"address": email, "password": password},
                        headers=self._auth_headers(use_json=True),
                        timeout=request_timeout,
                    )
                    if token_resp.status_code == 200:
                        mail_token = str(token_resp.json().get("token") or "").strip()
                        if mail_token:
                            return email, mail_token
            except Exception as exc:
                logger.warning("DuckMail 创建邮箱失败: {}", exc)
        return "", ""

    def wait_for_otp(
        self,
        auth_credential: str,
        email: str,
        proxy: str = "",
        proxy_selector: Optional[Callable[[], str]] = None,
        proxy_reporter: Optional[Callable[[str, bool, Any], None]] = None,
        timeout: int = 120,
        stop_event: Optional[threading.Event] = None,
    ) -> str:
        with _build_client(proxy, proxy_selector, proxy_reporter) as client:
            seen_ids: set = set()
            start = time.time()

            while time.time() - start < timeout:
                if stop_event and stop_event.is_set():
                    return ""
                try:
                    request_timeout = _interruptible_timeout(30, stop_event)
                    resp = client.get(
                        f"{self.api_base}/messages",
                        headers=self._auth_headers(auth_credential),
                        timeout=request_timeout,
                    )
                    if resp.status_code == 200:
                        data = resp.json()
                        messages = data.get("hydra:member") or data.get("member") or data.get("data") or []
                        for msg in (messages if isinstance(messages, list) else []):
                            if not isinstance(msg, dict):
                                continue
                            msg_id = msg.get("id") or msg.get("@id")
                            if not msg_id or msg_id in seen_ids:
                                continue
                            raw_id = str(msg_id).split("/")[-1] if str(msg_id).startswith("/") else str(msg_id)

                            detail_resp = client.get(
                                f"{self.api_base}/messages/{raw_id}",
                                headers=self._auth_headers(auth_credential),
                                timeout=request_timeout,
                            )
                            if detail_resp.status_code == 200:
                                seen_ids.add(msg_id)
                                detail = detail_resp.json()
                                content = _merge_message_content(
                                    detail.get("subject") or msg.get("subject"),
                                    detail.get("text"),
                                    detail.get("html"),
                                )
                                code = _extract_code(content)
                                if code:
                                    return code
                except Exception as exc:
                    logger.warning("DuckMail 轮询验证码失败: {}", exc)
                if not _wait_with_stop(3, stop_event):
                    return ""
        return ""


# ==================== Cloudflare Temp Email ====================

class CloudflareTempEmailProvider(MailProvider):
    def __init__(self, api_base: str = "", admin_password: str = "", domain: str = ""):
        self.api_base = api_base.rstrip("/")
        self.admin_password = admin_password
        self.domain = str(domain).strip()
        # 使用线程本地 token，避免多线程下邮箱 token 串用。
        self._tls = threading.local()

    def _get_random_domain(self) -> str:
        if not self.domain:
            return ""
        # 尝试按照 JSON 数组解析
        if self.domain.startswith("[") and self.domain.endswith("]"):
            try:
                import json
                domain_list = json.loads(self.domain)
                if isinstance(domain_list, list) and domain_list:
                    return random.choice([str(d).strip() for d in domain_list if str(d).strip()])
            except Exception:
                pass
        # 按照逗号分隔解析
        if "," in self.domain:
            parts = [d.strip() for d in self.domain.split(",") if d.strip()]
            if parts:
                return random.choice(parts)
        return self.domain

    @staticmethod
    def _message_matches_email(msg: Dict[str, Any], target_email: str) -> bool:
        target = str(target_email or "").strip().lower()
        if not target:
            return True

        def _extract_text_candidates(value: Any) -> List[str]:
            out: List[str] = []
            if isinstance(value, str):
                out.append(value)
            elif isinstance(value, dict):
                for k in ("address", "email", "name", "value"):
                    if value.get(k):
                        out.extend(_extract_text_candidates(value.get(k)))
            elif isinstance(value, list):
                for item in value:
                    out.extend(_extract_text_candidates(item))
            return out

        candidates: List[str] = []
        for key in ("to", "mailTo", "receiver", "receivers", "address", "email", "envelope_to"):
            if key in msg:
                candidates.extend(_extract_text_candidates(msg.get(key)))
        if not candidates:
            return True
        target_lower = target.lower()
        for raw in candidates:
            text = str(raw or "").strip().lower()
            if not text:
                continue
            if target_lower in text:
                return True
        return False

    def create_mailbox(
        self,
        proxy: str = "",
        proxy_selector: Optional[Callable[[], str]] = None,
        proxy_reporter: Optional[Callable[[str, bool, Any], None]] = None,
        stop_event: Optional[threading.Event] = None,
    ) -> Tuple[str, str]:
        if not self.api_base or not self.admin_password or not self.domain:
            return "", ""

        with _build_client(proxy, proxy_selector, proxy_reporter) as client:
            try:
                if stop_event and stop_event.is_set():
                    return "", ""
                # 生成5位字母 + 1-3位数字 + 1-3位字母的随机名
                letters1 = ''.join(random.choices(string.ascii_lowercase, k=5))
                numbers = ''.join(random.choices(string.digits, k=random.randint(1, 3)))
                letters2 = ''.join(random.choices(string.ascii_lowercase, k=random.randint(1, 3)))
                name = letters1 + numbers + letters2

                target_domain = self._get_random_domain()
                if not target_domain:
                    return "", ""

                resp = client.post(
                    f"{self.api_base}/admin/new_address",
                    json={
                        "enablePrefix": True,
                        "name": name,
                        "domain": target_domain,
                    },
                    headers={
                        "x-admin-auth": self.admin_password,
                        "Content-Type": "application/json"
                    },
                    timeout=_interruptible_timeout(30, stop_event),
                )
                if resp.status_code == 200:
                    data = resp.json()
                    email = data.get("address")
                    jwt_token = data.get("jwt")
                    if email and jwt_token:
                        self._tls.jwt_token = jwt_token
                        return email, jwt_token
            except Exception as exc:
                logger.warning("Cloudflare 临时邮箱创建失败: {}", exc)
        return "", ""

    def wait_for_otp(
        self,
        auth_credential: str,
        email: str,
        proxy: str = "",
        proxy_selector: Optional[Callable[[], str]] = None,
        proxy_reporter: Optional[Callable[[str, bool, Any], None]] = None,
        timeout: int = 120,
        stop_event: Optional[threading.Event] = None,
    ) -> str:
        token = str(auth_credential or "").strip() or str(getattr(self._tls, "jwt_token", "") or "").strip()
        if not token:
            return ""
        provider_logger = logger.bind(provider_name="cloudflare_temp_email")
        provider_logger.debug(
            "Cloudflare 临时邮箱开始轮询验证码: api_base={}, timeout={}s",
            self.api_base,
            timeout,
        )
        with _build_client(proxy, proxy_selector, proxy_reporter) as client:
            seen_ids: set = set()
            start = time.time()
            poll_count = 0

            while time.time() - start < timeout:
                if stop_event and stop_event.is_set():
                    provider_logger.info("Cloudflare 临时邮箱轮询已停止: stop_event 已触发")
                    return ""
                try:
                    request_timeout = _interruptible_timeout(30, stop_event)
                    poll_count += 1
                    url = f"{self.api_base}/api/mails?limit=10&offset=0"
                    resp = client.get(
                        url,
                        headers={
                            "Authorization": f"Bearer {token}",
                            "Content-Type": "application/json"
                        },
                        timeout=request_timeout,
                    )
                    provider_logger.debug(
                        "Cloudflare 临时邮箱轮询#{}: status={}, response_chars={}",
                        poll_count,
                        resp.status_code,
                        len(str(resp.text or "")),
                    )
                    if resp.status_code == 200:
                        try:
                            data = resp.json()
                        except Exception as je:
                            provider_logger.warning("Cloudflare 临时邮箱响应 JSON 解析失败: {}", je)
                            if not _wait_with_stop(3, stop_event):
                                return ""
                            continue
                        # API 返回字典 {"results": [...], "count": 0}，需正确提取
                        if isinstance(data, dict):
                            messages = data.get("results") or []
                        elif isinstance(data, list):
                            messages = data
                        else:
                            messages = []
                        provider_logger.debug("Cloudflare 临时邮箱收到 {} 条候选邮件", len(messages))
                        for msg in messages:
                            if not isinstance(msg, dict):
                                continue
                            if not self._message_matches_email(msg, email):
                                continue
                            msg_id = msg.get("id")
                            if not msg_id or msg_id in seen_ids:
                                continue
                            seen_ids.add(msg_id)

                            content = _merge_message_content(
                                msg.get("text"),
                                msg.get("html"),
                            )
                            # Cloudflare Temp Email 将邮件原文放在 raw 字段（MIME 格式）
                            if not content and msg.get("raw"):
                                try:
                                    content = _extract_raw_email_content(msg.get("raw"))
                                except Exception as parse_err:
                                    provider_logger.warning("Cloudflare 临时邮箱 MIME 解析失败，回退原始文本: {}", parse_err)
                                    content = _stringify_message_part(msg.get("raw"))
                            provider_logger.debug("Cloudflare 临时邮箱正在处理邮件 id={}", msg_id)
                            code = _extract_code(content)
                            if code:
                                provider_logger.success("Cloudflare 临时邮箱已提取到验证码")
                                return code
                except Exception as e:
                    provider_logger.warning("Cloudflare 临时邮箱轮询异常: {}", e)
                if not _wait_with_stop(3, stop_event):
                    return ""
        provider_logger.warning("Cloudflare 临时邮箱轮询超时，未获取到验证码")
        return ""


# ==================== 多提供商路由 ====================


class MultiMailRouter:
    """线程安全的多邮箱提供商路由器，支持轮询/随机/容错策略"""

    def __init__(self, config: Dict[str, Any]):
        providers_list: List[str] = config.get("mail_providers") or []
        provider_configs: Dict[str, Dict] = config.get("mail_provider_configs") or {}
        self.strategy: str = config.get("mail_strategy", "round_robin")

        if not providers_list:
            providers_list = ["mailtm"]

        self._provider_names: List[str] = []
        self._providers: Dict[str, MailProvider] = {}
        self._failures: Dict[str, int] = {}
        self._lock = threading.RLock()
        self._counter = itertools.count()

        for name in providers_list:
            try:
                p = create_provider_by_name(name, provider_configs.get(name, {}))
                self._provider_names.append(name)
                self._providers[name] = p
                self._failures[name] = 0
            except Exception as e:
                logger.warning("创建邮箱提供商 {} 失败: {}", name, e)

        if not self._providers:
            if providers_list:
                raise RuntimeError(f"邮箱提供商配置无效: {', '.join(str(n) for n in providers_list)}")
            fallback = create_provider_by_name("mailtm", {})
            self._provider_names = ["mailtm"]
            self._providers = {"mailtm": fallback}
            self._failures = {"mailtm": 0}

    def next_provider(self) -> Tuple[str, MailProvider]:
        with self._lock:
            names = self._provider_names
            if not names:
                raise RuntimeError("无可用邮箱提供商")

            if self.strategy == "random":
                name = random.choice(names)
            elif self.strategy == "failover":
                name = min(names, key=lambda n: self._failures.get(n, 0))
            else:
                idx = next(self._counter) % len(names)
                name = names[idx]
            return name, self._providers[name]

    def providers(self) -> List[Tuple[str, MailProvider]]:
        with self._lock:
            return [(n, self._providers[n]) for n in self._provider_names]

    def report_success(self, provider_name: str) -> None:
        with self._lock:
            self._failures[provider_name] = max(0, self._failures.get(provider_name, 0) - 1)

    def report_failure(self, provider_name: str) -> None:
        with self._lock:
            self._failures[provider_name] = self._failures.get(provider_name, 0) + 1


# ==================== 工厂函数 ====================


def create_provider_by_name(provider_type: str, mail_cfg: Dict[str, Any]) -> MailProvider:
    """根据提供商名称和单独配置创建实例"""
    provider_type = provider_type.lower().strip()
    api_base = str(mail_cfg.get("api_base", "")).strip()

    if provider_type == "moemail":
        return MoeMailProvider(
            api_base=api_base or "https://your-moemail-api.example.com",
            api_key=str(mail_cfg.get("api_key", "")).strip(),
        )
    elif provider_type == "duckmail":
        return DuckMailProvider(
            api_base=api_base or "https://api.duckmail.sbs",
            bearer_token=str(mail_cfg.get("bearer_token", "")).strip(),
        )
    elif provider_type == "cloudflare_temp_email":
        return CloudflareTempEmailProvider(
            api_base=api_base,
            admin_password=str(mail_cfg.get("admin_password", "")).strip(),
            domain=str(mail_cfg.get("domain", "")).strip(),
        )
    elif provider_type == "mailtm":
        return MailTmProvider(api_base=api_base or "https://api.mail.tm")
    raise ValueError(f"未知邮箱提供商: {provider_type}")
