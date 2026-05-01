import json
import os
import re
import sys
import time
import uuid
import math
import random
import string
import secrets
import socket
import hashlib
import base64
import threading
import argparse
import queue
import tempfile
from http.cookies import SimpleCookie
from datetime import datetime, timezone, timedelta
from pathlib import Path
from urllib.parse import urlparse, parse_qs, urlencode, quote
from dataclasses import dataclass
from typing import Any, Dict, Optional, Callable
import urllib.parse
import urllib.request
import urllib.error

from curl_cffi import requests

from .token_health import (
    build_token_result as _health_build_token_result,
    check_access_token as _health_check_access_token,
)
from .logger import get_logger, log_event

logger = get_logger(__name__)

# ==========================================
# 日志事件发射器
# ==========================================


class EventEmitter:
    """
    将注册流程中的日志事件发射到队列，供 SSE 消费。
    同时支持 CLI 模式（通过 loguru 输出到统一日志）。
    """

    def __init__(
        self,
        q: Optional[queue.Queue] = None,
        cli_mode: bool = False,
        defaults: Optional[Dict[str, Any]] = None,
    ):
        self._q = q
        self._cli_mode = cli_mode
        self._defaults = dict(defaults or {})

    def emit(self, level: str, message: str, step: str = "", **extra: Any) -> None:
        """
        level: "info" | "success" | "error" | "warn"
        step:  可选的流程阶段标识，如 "check_proxy" / "create_email" 等
        """
        ts = datetime.now().strftime("%H:%M:%S")
        event = {
            "ts": ts,
            "level": level,
            "message": message,
            "step": step,
        }
        if self._defaults:
            event.update(self._defaults)
        if extra:
            event.update({k: v for k, v in extra.items() if v is not None})
        if self._cli_mode:
            log_event(level, message, component=__name__, **{k: v for k, v in event.items() if k not in {"ts", "level", "message"}})
        if self._q is not None:
            try:
                self._q.put_nowait(event)
            except queue.Full:
                pass

    def bind(self, **defaults: Any) -> "EventEmitter":
        merged = dict(self._defaults)
        merged.update({k: v for k, v in defaults.items() if v is not None})
        return EventEmitter(q=self._q, cli_mode=self._cli_mode, defaults=merged)

    def info(self, msg: str, step: str = "", **extra: Any) -> None:
        self.emit("info", msg, step, **extra)

    def success(self, msg: str, step: str = "", **extra: Any) -> None:
        self.emit("success", msg, step, **extra)

    def error(self, msg: str, step: str = "", **extra: Any) -> None:
        self.emit("error", msg, step, **extra)

    def warn(self, msg: str, step: str = "", **extra: Any) -> None:
        self.emit("warn", msg, step, **extra)


# 默认 CLI 发射器（兼容直接运行）
_cli_emitter = EventEmitter(cli_mode=True)


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


# ==========================================
# Mail.tm 临时邮箱 API
# ==========================================

MAILTM_BASE = "https://api.mail.tm"
DEFAULT_PROXY_POOL_URL = "https://github.com/proxifly/free-proxy-list/blob/main/proxies/countries/US/data.txt"
DEFAULT_PROXY_POOL_AUTH_MODE = "query"
DEFAULT_PROXY_POOL_API_KEY = "19c0ec43-8f76-4c97-81bc-bcda059eeba4"
DEFAULT_PROXY_POOL_COUNT = 1
DEFAULT_PROXY_POOL_COUNTRY = "US"
DEFAULT_HTTP_VERSION = "v2"
H3_PROXY_ERROR_HINT = "HTTP/3 is not supported over an HTTP proxy"
TRANSIENT_TLS_ERROR_HINTS = (
    "curl: (28)",
    "curl: (35)",
    "TLS connect error",
    "OPENSSL_internal:invalid library",
    "SSL_ERROR_SYSCALL",
    "The handshake operation timed out",
    "handshake timed out",
    "Connection timed out",
)
TRANSIENT_TLS_RETRY_COUNT = 2
POOL_RELAY_RETRIES = 2
POOL_PROXY_FETCH_RETRIES = 3
POOL_RELAY_REQUEST_RETRIES = 2
DEFAULT_PROXY_POOL_BAD_TTL_SECONDS = 180
DEFAULT_PROXY_POOL_TCP_CHECK_ENABLED = True
DEFAULT_PROXY_POOL_TCP_CHECK_TIMEOUT_SECONDS = 1.2
DEFAULT_PROXY_POOL_PREFER_STABLE_PROXY = True
DEFAULT_PROXY_POOL_VALIDATE_TIMEOUT_SECONDS = 6.0
DEFAULT_PROXY_POOL_VALIDATE_TEST_URL = "https://auth.openai.com/"
_PROXY_BAD_STATUS_CODES = frozenset({407, 429, 500, 502, 503, 504})
_STOP_AWARE_REQUEST_TIMEOUT_SECONDS = 5.0
_REQUEST_PROXY_POOLS_LOCK = threading.RLock()
_REQUEST_PROXY_POOLS: dict[str, "_RequestProxyPoolState"] = {}

OPENAI_AUTH_BASE = "https://auth.openai.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
    "AppleWebKit/537.36 (KHTML, like Gecko) "
    "Chrome/145.0.0.0 Safari/537.36"
)
COMMON_HEADERS = {
    "accept": "application/json",
    "accept-language": "en-US,en;q=0.9",
    "origin": OPENAI_AUTH_BASE,
    "user-agent": USER_AGENT,
    "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "empty",
    "sec-fetch-mode": "cors",
    "sec-fetch-site": "same-origin",
}
NAVIGATE_HEADERS = {
    "accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/avif,image/webp,*/*;q=0.8",
    "accept-language": "en-US,en;q=0.9",
    "user-agent": USER_AGENT,
    "sec-ch-ua": '"Google Chrome";v="145", "Not?A_Brand";v="8", "Chromium";v="145"',
    "sec-ch-ua-mobile": "?0",
    "sec-ch-ua-platform": '"Windows"',
    "sec-fetch-dest": "document",
    "sec-fetch-mode": "navigate",
    "sec-fetch-site": "same-origin",
    "sec-fetch-user": "?1",
    "upgrade-insecure-requests": "1",
}


def _generate_datadog_trace() -> Dict[str, str]:
    trace_id = str(random.getrandbits(64))
    parent_id = str(random.getrandbits(64))
    trace_hex = format(int(trace_id), "016x")
    parent_hex = format(int(parent_id), "016x")
    return {
        "traceparent": f"00-0000000000000000{trace_hex}-{parent_hex}-01",
        "tracestate": "dd=s:1;o:rum",
        "x-datadog-origin": "rum",
        "x-datadog-parent-id": parent_id,
        "x-datadog-sampling-priority": "1",
        "x-datadog-trace-id": trace_id,
    }


def _random_password(length: int = 16) -> str:
    chars = string.ascii_letters + string.digits + "!@#$%"
    pwd = list(
        secrets.choice(string.ascii_uppercase)
        + secrets.choice(string.ascii_lowercase)
        + secrets.choice(string.digits)
        + secrets.choice("!@#$%")
        + "".join(secrets.choice(chars) for _ in range(max(4, length) - 4))
    )
    random.shuffle(pwd)
    return "".join(pwd)


def _random_account_profile() -> Dict[str, str]:
    names = (
        "Alex",
        "Chris",
        "Jordan",
        "Taylor",
        "Sam",
        "Morgan",
        "Jamie",
        "Avery",
        "Casey",
        "Riley",
    )
    age_days = random.randint(18 * 365, 40 * 365)
    birthdate = (datetime.now(timezone.utc) - timedelta(days=age_days)).strftime("%Y-%m-%d")
    return {
        "name": random.choice(names),
        "birthdate": birthdate,
    }


def _build_openai_headers(
    referer: str,
    device_id: str,
    *,
    sentinel_token: str = "",
    accept: str = "application/json",
    content_type: Optional[str] = "application/json",
) -> Dict[str, str]:
    headers = dict(COMMON_HEADERS)
    headers["referer"] = referer
    headers["oai-device-id"] = device_id
    headers.update(_generate_datadog_trace())
    if accept:
        headers["accept"] = accept
    if content_type:
        headers["content-type"] = content_type
    if sentinel_token:
        headers["openai-sentinel-token"] = sentinel_token
    return headers


class _SentinelTokenGenerator:
    MAX_ATTEMPTS = 500000
    ERROR_PREFIX = "wQ8Lk5FbGpA2NcR9dShT6gYjU7VxZ4D"

    def __init__(self, device_id: Optional[str] = None):
        self.device_id = device_id or str(uuid.uuid4())
        self.requirements_seed = str(random.random())
        self.sid = str(uuid.uuid4())

    @staticmethod
    def _fnv1a_32(text: str) -> str:
        h = 2166136261
        for ch in text:
            h ^= ord(ch)
            h = (h * 16777619) & 0xFFFFFFFF
        h ^= (h >> 16)
        h = (h * 2246822507) & 0xFFFFFFFF
        h ^= (h >> 13)
        h = (h * 3266489909) & 0xFFFFFFFF
        h ^= (h >> 16)
        h &= 0xFFFFFFFF
        return format(h, "08x")

    @staticmethod
    def _base64_encode(data: Any) -> str:
        js = json.dumps(data, separators=(",", ":"), ensure_ascii=False)
        return base64.b64encode(js.encode("utf-8")).decode("ascii")

    def _get_config(self) -> list[Any]:
        now = datetime.now(timezone.utc).strftime(
            "%a %b %d %Y %H:%M:%S GMT+0000 (Coordinated Universal Time)"
        )
        perf_now = random.uniform(1000, 50000)
        time_origin = time.time() * 1000 - perf_now
        return [
            "1920x1080",
            now,
            4294705152,
            random.random(),
            USER_AGENT,
            "https://sentinel.openai.com/sentinel/20260124ceb8/sdk.js",
            None,
            None,
            "en-US",
            "en-US,en",
            random.random(),
            "vendorSub−undefined",
            "location",
            "Object",
            perf_now,
            self.sid,
            "",
            random.choice([4, 8, 12, 16]),
            time_origin,
        ]

    def _run_check(
        self,
        start_time: float,
        seed: str,
        difficulty: str,
        config: list[Any],
        nonce: int,
    ) -> Optional[str]:
        config[3] = nonce
        config[9] = round((time.time() - start_time) * 1000)
        data = self._base64_encode(config)
        hash_hex = self._fnv1a_32(seed + data)
        if hash_hex[: len(difficulty)] <= difficulty:
            return data + "~S"
        return None

    def generate_requirements_token(self) -> str:
        cfg = self._get_config()
        cfg[3] = 1
        cfg[9] = round(random.uniform(5, 50))
        return "gAAAAAC" + self._base64_encode(cfg)

    def generate_token(self, seed: Optional[str] = None, difficulty: Optional[str] = None) -> str:
        if seed is None:
            seed = self.requirements_seed
            difficulty = difficulty or "0"
        cfg = self._get_config()
        start = time.time()
        for i in range(self.MAX_ATTEMPTS):
            result = self._run_check(start, seed, difficulty or "0", cfg, i)
            if result:
                return "gAAAAAB" + result
        return "gAAAAAB" + self.ERROR_PREFIX + self._base64_encode(str(None))


def _build_sentinel_token(
    device_id: str,
    flow: str = "authorize_continue",
    emitter: Optional[EventEmitter] = None,
    post_func: Optional[Callable[..., Any]] = None,
) -> Optional[str]:
    gen = _SentinelTokenGenerator(device_id=device_id)
    body = {"p": gen.generate_requirements_token(), "id": device_id, "flow": flow}
    headers = {
        "Content-Type": "text/plain;charset=UTF-8",
        "Referer": "https://sentinel.openai.com/backend-api/sentinel/frame.html",
        "User-Agent": USER_AGENT,
        "Origin": "https://sentinel.openai.com",
        "sec-ch-ua": '"Not:A-Brand";v="99", "Google Chrome";v="145", "Chromium";v="145"',
        "sec-ch-ua-mobile": "?0",
        "sec-ch-ua-platform": '"Windows"',
    }
    sender = post_func or requests.post
    try:
        resp = sender(
            "https://sentinel.openai.com/backend-api/sentinel/req",
            headers=headers,
            data=json.dumps(body),
        )
    except Exception as exc:
        if emitter:
            emitter.error(f"Sentinel 请求异常: {exc}", step="sentinel")
        return None
    if resp.status_code != 200:
        if emitter:
            body_preview = str(resp.text or "")[:200].replace("\n", " ")
            emitter.error(
                f"Sentinel 返回异常: {resp.status_code}, body={body_preview}",
                step="sentinel",
            )
        return None
    try:
        data = resp.json()
    except Exception:
        if emitter:
            body_preview = str(resp.text or "")[:200].replace("\n", " ")
            emitter.error(f"Sentinel 响应解析失败: {body_preview}", step="sentinel")
        return None
    if not isinstance(data, dict):
        return None
    c_value = str(data.get("token") or "")
    pow_data = data.get("proofofwork", {}) or {}
    if isinstance(pow_data, dict) and pow_data.get("required") and pow_data.get("seed"):
        p_value = gen.generate_token(
            seed=str(pow_data.get("seed")),
            difficulty=str(pow_data.get("difficulty", "0")),
        )
    else:
        p_value = gen.generate_requirements_token()
    return json.dumps({"p": p_value, "t": "", "c": c_value, "id": device_id, "flow": flow})


def _is_transient_tls_error(exc: Exception | str) -> bool:
    message = str(exc or "").lower()
    return any(hint.lower() in message for hint in TRANSIENT_TLS_ERROR_HINTS)


def _call_with_http_fallback(request_func, url: str, **kwargs: Any):
    """
    curl_cffi 在某些站点可能优先尝试 H3，遇到 HTTP 代理不支持时自动降级到 HTTP/1.1 重试。
    对 curl TLS 握手异常（如 curl: (35)）也进行有限重试，并优先降级到 HTTP/1.1。
    """
    try:
        return request_func(url, **kwargs)
    except Exception as exc:
        message = str(exc)
        if H3_PROXY_ERROR_HINT in message:
            retry_kwargs = dict(kwargs)
            retry_kwargs["http_version"] = "v1"
            return request_func(url, **retry_kwargs)
        if not _is_transient_tls_error(message):
            raise

        last_exc: Exception = exc
        candidate_kwargs_list = [dict(kwargs)]
        if str(kwargs.get("http_version") or "").strip().lower() != "v1":
            retry_kwargs = dict(kwargs)
            retry_kwargs["http_version"] = "v1"
            candidate_kwargs_list.append(retry_kwargs)

        for candidate_kwargs in candidate_kwargs_list:
            for attempt in range(TRANSIENT_TLS_RETRY_COUNT):
                time.sleep(min(0.35 * (attempt + 1), 1.0))
                try:
                    return request_func(url, **candidate_kwargs)
                except Exception as retry_exc:
                    last_exc = retry_exc
                    retry_message = str(retry_exc)
                    if H3_PROXY_ERROR_HINT in retry_message and str(candidate_kwargs.get("http_version") or "").strip().lower() != "v1":
                        candidate_kwargs = dict(candidate_kwargs)
                        candidate_kwargs["http_version"] = "v1"
                        continue
                    if not _is_transient_tls_error(retry_message):
                        raise
        raise last_exc

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


def _to_proxies_dict(proxy_value: str) -> Optional[Dict[str, str]]:
    normalized = _normalize_proxy_value(proxy_value)
    if not normalized:
        return None
    return {"http": normalized, "https": normalized}


def _build_proxy_from_host_port(host: Any, port: Any, proxy_type: Any = "") -> str:
    host_value = str(host or "").strip()
    port_value = str(port or "").strip()
    if not host_value or not port_value:
        return ""
    proxy_type_value = str(proxy_type or "").strip().lower()
    if proxy_type_value in ("socks5", "socks", "shadowsocks"):
        return _normalize_proxy_value(f"socks5://{host_value}:{port_value}")
    return _normalize_proxy_value(f"http://{host_value}:{port_value}")


def _pool_host_from_api_url(api_url: str) -> str:
    raw = str(api_url or "").strip()
    if not raw:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        return str(parsed.hostname or "").strip()
    except Exception:
        return ""


def _normalize_proxy_pool_source_url(api_url: str, country: str = DEFAULT_PROXY_POOL_COUNTRY) -> str:
    value = str(api_url or "").strip()
    country_value = str(country or DEFAULT_PROXY_POOL_COUNTRY).strip().upper() or DEFAULT_PROXY_POOL_COUNTRY
    if not value:
        return DEFAULT_PROXY_POOL_URL.replace("/US/", f"/{country_value}/")

    github_blob_match = re.match(
        r"^https?://github\.com/proxifly/free-proxy-list/blob/([^/]+)/proxies/countries/([^/]+)/data\.txt$",
        value,
        re.IGNORECASE,
    )
    if github_blob_match:
        branch = github_blob_match.group(1)
        return (
            f"https://raw.githubusercontent.com/proxifly/free-proxy-list/"
            f"{branch}/proxies/countries/{country_value}/data.txt"
        )

    github_raw_match = re.match(
        r"^https?://raw\.githubusercontent\.com/proxifly/free-proxy-list/([^/]+)/proxies/countries/([^/]+)/data\.txt$",
        value,
        re.IGNORECASE,
    )
    if github_raw_match:
        branch = github_raw_match.group(1)
        return (
            f"https://raw.githubusercontent.com/proxifly/free-proxy-list/"
            f"{branch}/proxies/countries/{country_value}/data.txt"
        )

    return value


def _is_text_proxy_list_source(api_url: str) -> bool:
    normalized = _normalize_proxy_pool_source_url(api_url)
    lowered = normalized.lower()
    return lowered.endswith(".txt") or "/proxies/countries/" in lowered


def _pool_relay_url_from_fetch_url(api_url: str) -> str:
    raw = str(api_url or "").strip()
    if not raw:
        return ""
    if _is_text_proxy_list_source(raw):
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        parsed = urlparse(raw)
        scheme = parsed.scheme or "https"
        netloc = parsed.netloc
        if not netloc:
            return ""
        return f"{scheme}://{netloc}/api/relay"
    except Exception:
        return ""


def _trace_via_pool_relay(
    pool_cfg: Dict[str, Any],
    stop_event: Optional[threading.Event] = None,
) -> str:
    relay_url = _pool_relay_url_from_fetch_url(str(pool_cfg.get("api_url") or ""))
    if not relay_url:
        raise RuntimeError("代理池 relay 地址解析失败")

    api_key = str(pool_cfg.get("api_key") or DEFAULT_PROXY_POOL_API_KEY).strip() or DEFAULT_PROXY_POOL_API_KEY
    country = str(pool_cfg.get("country") or DEFAULT_PROXY_POOL_COUNTRY).strip().upper() or DEFAULT_PROXY_POOL_COUNTRY
    timeout = int(pool_cfg.get("timeout_seconds") or 10)
    timeout = max(8, min(timeout, 30))
    timeout = _interruptible_timeout(timeout, stop_event)

    params = {
        "api_key": api_key,
        "url": "https://cloudflare.com/cdn-cgi/trace",
        "country": country,
    }
    retry_count = max(1, int(pool_cfg.get("relay_retries") or POOL_RELAY_RETRIES))
    last_error = ""
    for i in range(retry_count):
        if stop_event and stop_event.is_set():
            raise RuntimeError("代理池 relay 请求已取消")
        try:
            resp = _call_with_http_fallback(
                requests.get,
                relay_url,
                params=params,
                impersonate="chrome",
                timeout=timeout,
            )
            if resp.status_code == 200:
                return str(resp.text or "")
            last_error = f"HTTP {resp.status_code}"
        except Exception as exc:
            last_error = str(exc)
        if i < retry_count - 1:
            if not _wait_with_stop(min(0.3 * (i + 1), 1.0), stop_event):
                raise RuntimeError("代理池 relay 请求已取消")
    raise RuntimeError(f"代理池 relay 请求失败: {last_error or 'unknown error'}")


def _parse_cloudflare_trace_text(trace_text: str) -> Dict[str, str]:
    text = str(trace_text or "")
    loc_re = re.search(r"^loc=(.+)$", text, re.MULTILINE)
    ip_re = re.search(r"^ip=(.+)$", text, re.MULTILINE)
    return {
        "loc": str(loc_re.group(1) if loc_re else "").strip(),
        "ip": str(ip_re.group(1) if ip_re else "").strip(),
    }


def _probe_cloudflare_trace_with_proxy_rotation(
    request_get: Callable[..., Any],
    next_proxy_value: Callable[[], str],
    report_proxy_result: Optional[Callable[[str, bool, Any], None]] = None,
    *,
    max_attempts: int = 1,
    timeout: int = 10,
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    attempts = max(1, int(max_attempts or 1))
    last_error: Optional[Exception] = None

    for attempt in range(attempts):
        if stop_event and stop_event.is_set():
            raise RuntimeError("cloudflare trace probe cancelled")
        selected_proxy = _normalize_proxy_value(next_proxy_value())
        request_kwargs: Dict[str, Any] = {
            "proxies": _to_proxies_dict(selected_proxy),
            "http_version": DEFAULT_HTTP_VERSION,
            "timeout": _interruptible_timeout(timeout, stop_event),
        }
        try:
            resp = _call_with_http_fallback(
                request_get,
                "https://cloudflare.com/cdn-cgi/trace",
                **request_kwargs,
            )
            status_code = int(resp.status_code or 0)
            if status_code != 200:
                raise RuntimeError(f"HTTP {status_code}")

            trace_text = str(resp.text or "")
            parsed = _parse_cloudflare_trace_text(trace_text)
            loc = parsed.get("loc") or ""
            if loc in ("CN", "HK"):
                raise RuntimeError(f"所在地不支持 ({loc})")

            if report_proxy_result is not None and selected_proxy:
                report_proxy_result(selected_proxy, True)
            return {
                "trace_text": trace_text,
                "proxy": selected_proxy,
                "loc": loc,
                "ip": parsed.get("ip") or "",
                "attempt": attempt + 1,
            }
        except Exception as exc:
            if report_proxy_result is not None and selected_proxy:
                report_proxy_result(selected_proxy, False, exc)
            last_error = exc
            if attempt >= attempts - 1:
                raise

    if last_error is not None:
        raise last_error
    raise RuntimeError("cloudflare trace probe failed")


def _extract_proxy_from_obj(obj: Any, relay_host: str = "") -> str:
    if isinstance(obj, str):
        return _normalize_proxy_value(obj)
    if isinstance(obj, (list, tuple)):
        for item in obj:
            proxy = _extract_proxy_from_obj(item, relay_host)
            if proxy:
                return proxy
        return ""
    if isinstance(obj, dict):
        local_port = obj.get("local_port")
        if local_port in (None, ""):
            local_port = obj.get("localPort")
        if local_port not in (None, ""):
            # ZenProxy 文档中的 local_port 是代理绑定端口，优先使用 api_url 主机名。
            if relay_host:
                proxy = _normalize_proxy_value(f"http://{relay_host}:{local_port}")
                if proxy:
                    return proxy
            proxy = _normalize_proxy_value(f"http://127.0.0.1:{local_port}")
            if proxy:
                return proxy

        host = str(obj.get("ip") or obj.get("host") or obj.get("server") or "").strip()
        port = str(obj.get("port") or "").strip()
        proxy_type = obj.get("type") or obj.get("protocol") or obj.get("scheme") or ""
        if host and port:
            proxy = _build_proxy_from_host_port(host, port, proxy_type)
            if proxy:
                return proxy

        for key in ("proxy", "proxy_url", "url", "value", "result", "data", "proxy_list", "list", "proxies"):
            if key in obj:
                proxy = _extract_proxy_from_obj(obj.get(key), relay_host)
                if proxy:
                    return proxy

        for value in obj.values():
            proxy = _extract_proxy_from_obj(value, relay_host)
            if proxy:
                return proxy
    return ""


def _proxy_tcp_reachable(proxy_url: str, timeout_seconds: float = 1.2) -> bool:
    value = str(proxy_url or "").strip()
    if not value:
        return False
    if "://" not in value:
        value = "http://" + value
    try:
        parsed = urlparse(value)
        host = str(parsed.hostname or "").strip()
        port = int(parsed.port or 0)
    except Exception:
        return False
    if not host or port <= 0:
        return False
    try:
        with socket.create_connection((host, port), timeout=timeout_seconds):
            return True
    except Exception:
        return False


def _proxy_http_reachable(
    proxy_url: str,
    *,
    timeout_seconds: float = DEFAULT_PROXY_POOL_VALIDATE_TIMEOUT_SECONDS,
    test_url: str = DEFAULT_PROXY_POOL_VALIDATE_TEST_URL,
) -> bool:
    proxy_value = _normalize_proxy_value(proxy_url)
    if not proxy_value:
        return False
    try:
        resp = _call_with_http_fallback(
            requests.get,
            str(test_url or DEFAULT_PROXY_POOL_VALIDATE_TEST_URL),
            allow_redirects=False,
            proxies={"http": proxy_value, "https": proxy_value},
            http_version=DEFAULT_HTTP_VERSION,
            impersonate="chrome",
            timeout=max(1.0, float(timeout_seconds or DEFAULT_PROXY_POOL_VALIDATE_TIMEOUT_SECONDS)),
        )
    except Exception:
        return False
    status_code = int(resp.status_code or 0)
    return 200 <= status_code < 500


def _fetch_proxies_from_pool(pool_cfg: Dict[str, Any]) -> list[str]:
    enabled = bool(pool_cfg.get("enabled"))
    if not enabled:
        return []

    country = str(pool_cfg.get("country") or DEFAULT_PROXY_POOL_COUNTRY).strip().upper() or DEFAULT_PROXY_POOL_COUNTRY
    api_url = _normalize_proxy_pool_source_url(
        str(pool_cfg.get("api_url") or DEFAULT_PROXY_POOL_URL).strip() or DEFAULT_PROXY_POOL_URL,
        country=country,
    )
    auth_mode = str(pool_cfg.get("auth_mode") or DEFAULT_PROXY_POOL_AUTH_MODE).strip().lower()
    if auth_mode not in ("header", "query"):
        auth_mode = DEFAULT_PROXY_POOL_AUTH_MODE
    api_key = str(pool_cfg.get("api_key") or DEFAULT_PROXY_POOL_API_KEY).strip() or DEFAULT_PROXY_POOL_API_KEY
    relay_host = str(pool_cfg.get("relay_host") or "").strip()
    if not relay_host:
        relay_host = _pool_host_from_api_url(api_url)
    try:
        count = int(pool_cfg.get("count") or DEFAULT_PROXY_POOL_COUNT)
    except (TypeError, ValueError):
        count = DEFAULT_PROXY_POOL_COUNT
    count = max(1, min(count, 20))
    timeout = int(pool_cfg.get("timeout_seconds") or 10)
    timeout = max(3, min(timeout, 30))

    if _is_text_proxy_list_source(api_url):
        resp = _call_with_http_fallback(
            requests.get,
            api_url,
            http_version=DEFAULT_HTTP_VERSION,
            impersonate="chrome",
            timeout=timeout,
        )
        if resp.status_code != 200:
            raise RuntimeError(f"代理列表请求失败: HTTP {resp.status_code}")

        candidates: list[str] = []
        seen: set[str] = set()
        for raw_line in str(resp.text or "").splitlines():
            normalized = _normalize_proxy_value(raw_line.strip())
            if not normalized or normalized in seen:
                continue
            seen.add(normalized)
            candidates.append(normalized)
        if not candidates:
            raise RuntimeError("代理列表中未找到可用代理")
        random.shuffle(candidates)
        return candidates[:count] if count > 0 else candidates

    headers: Dict[str, str] = {}
    params: Dict[str, str] = {"count": str(count), "country": country}
    if auth_mode == "query":
        params["api_key"] = api_key
    else:
        headers["Authorization"] = f"Bearer {api_key}"

    resp = _call_with_http_fallback(
        requests.get,
        api_url,
        headers=headers or None,
        params=params or None,
        http_version=DEFAULT_HTTP_VERSION,
        impersonate="chrome",
        timeout=timeout,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"代理池请求失败: HTTP {resp.status_code}")

    candidates: list[str] = []
    seen: set[str] = set()

    def _append_candidate(value: Any) -> None:
        normalized = _normalize_proxy_value(value)
        if not normalized or normalized in seen:
            return
        seen.add(normalized)
        candidates.append(normalized)

    try:
        payload = resp.json()
    except Exception:
        payload = None

    if isinstance(payload, dict):
        proxies = payload.get("proxies")
        if isinstance(proxies, list):
            for item in proxies:
                extracted = _extract_proxy_from_obj(item, relay_host)
                if extracted:
                    _append_candidate(extracted)
        if not candidates:
            extracted = _extract_proxy_from_obj(payload, relay_host)
            if extracted:
                _append_candidate(extracted)
    elif isinstance(payload, list):
        for item in payload:
            extracted = _extract_proxy_from_obj(item, relay_host)
            if extracted:
                _append_candidate(extracted)

    if not candidates:
        for raw_line in str(resp.text or "").splitlines():
            _append_candidate(raw_line.strip())

    if not candidates:
        raise RuntimeError("代理池响应中未找到可用代理")
    return candidates


def _fetch_proxy_from_pool(pool_cfg: Dict[str, Any]) -> str:
    proxies = _fetch_proxies_from_pool(pool_cfg)
    return proxies[0] if proxies else ""


def _proxy_pool_signature(pool_cfg: Dict[str, Any], fallback_proxy: str = "") -> str:
    country = str(pool_cfg.get("country") or DEFAULT_PROXY_POOL_COUNTRY).strip().upper() or DEFAULT_PROXY_POOL_COUNTRY
    signature_payload = {
        "api_url": _normalize_proxy_pool_source_url(
            str(pool_cfg.get("api_url") or DEFAULT_PROXY_POOL_URL).strip() or DEFAULT_PROXY_POOL_URL,
            country=country,
        ),
        "auth_mode": str(pool_cfg.get("auth_mode") or DEFAULT_PROXY_POOL_AUTH_MODE).strip().lower() or DEFAULT_PROXY_POOL_AUTH_MODE,
        "api_key": str(pool_cfg.get("api_key") or DEFAULT_PROXY_POOL_API_KEY).strip() or DEFAULT_PROXY_POOL_API_KEY,
        "count": int(pool_cfg.get("count") or DEFAULT_PROXY_POOL_COUNT or 1),
        "country": country,
        "fetch_retries": int(pool_cfg.get("fetch_retries") or POOL_PROXY_FETCH_RETRIES or 1),
        "bad_ttl_seconds": int(pool_cfg.get("bad_ttl_seconds") or DEFAULT_PROXY_POOL_BAD_TTL_SECONDS or 1),
        "tcp_check_enabled": bool(pool_cfg.get("tcp_check_enabled", DEFAULT_PROXY_POOL_TCP_CHECK_ENABLED)),
        "tcp_check_timeout_seconds": float(pool_cfg.get("tcp_check_timeout_seconds") or DEFAULT_PROXY_POOL_TCP_CHECK_TIMEOUT_SECONDS),
        "prefer_stable_proxy": bool(pool_cfg.get("prefer_stable_proxy", DEFAULT_PROXY_POOL_PREFER_STABLE_PROXY)),
        "stable_proxy": _normalize_proxy_value(pool_cfg.get("stable_proxy") or ""),
        "fallback_proxy": _normalize_proxy_value(fallback_proxy),
    }
    return json.dumps(signature_payload, ensure_ascii=False, sort_keys=True)


class _RequestProxyPoolState:
    def __init__(self, pool_cfg: Dict[str, Any], fallback_proxy: str = ""):
        self._lock = threading.RLock()
        self._proxies: list[str] = []
        self._index = 0
        self._bad_until: Dict[str, float] = {}
        self._last_error = ""
        self._last_fetched_count = 0
        self._last_valid_count = 0
        self._stable_proxy = ""
        self._configure(pool_cfg, fallback_proxy)

    def _configure(self, pool_cfg: Dict[str, Any], fallback_proxy: str = "") -> None:
        self._pool_cfg = dict(pool_cfg or {})
        self._fallback_proxy = _normalize_proxy_value(fallback_proxy)
        self._fetch_retries = max(1, int(self._pool_cfg.get("fetch_retries") or POOL_PROXY_FETCH_RETRIES))
        self._bad_ttl_seconds = max(10, int(self._pool_cfg.get("bad_ttl_seconds") or DEFAULT_PROXY_POOL_BAD_TTL_SECONDS))
        self._tcp_check_enabled = bool(self._pool_cfg.get("tcp_check_enabled", DEFAULT_PROXY_POOL_TCP_CHECK_ENABLED))
        self._tcp_check_timeout_seconds = max(
            0.5,
            float(self._pool_cfg.get("tcp_check_timeout_seconds") or DEFAULT_PROXY_POOL_TCP_CHECK_TIMEOUT_SECONDS),
        )
        self._validate_timeout_seconds = max(
            1.0,
            float(
                self._pool_cfg.get("validate_timeout_seconds")
                or DEFAULT_PROXY_POOL_VALIDATE_TIMEOUT_SECONDS
            ),
        )
        self._validate_test_url = str(
            self._pool_cfg.get("validate_test_url") or DEFAULT_PROXY_POOL_VALIDATE_TEST_URL
        ).strip() or DEFAULT_PROXY_POOL_VALIDATE_TEST_URL
        self._prefer_stable_proxy = bool(
            self._pool_cfg.get("prefer_stable_proxy", DEFAULT_PROXY_POOL_PREFER_STABLE_PROXY)
        )
        stable_proxy = _normalize_proxy_value(self._pool_cfg.get("stable_proxy") or "")
        if stable_proxy:
            self._stable_proxy = stable_proxy

    def refresh_config(self, pool_cfg: Dict[str, Any], fallback_proxy: str = "") -> None:
        with self._lock:
            self._configure(pool_cfg, fallback_proxy)

    def _clear_expired_bad_locked(self) -> None:
        now = time.time()
        expired = [proxy for proxy, until in self._bad_until.items() if until <= now]
        for proxy in expired:
            self._bad_until.pop(proxy, None)

    def _is_available_locked(self, proxy_value: str) -> bool:
        if not proxy_value:
            return False
        self._clear_expired_bad_locked()
        return float(self._bad_until.get(proxy_value) or 0.0) <= time.time()

    def _refresh_candidates_locked(self) -> list[str]:
        last_error = ""
        fetched: list[str] = []
        for _ in range(self._fetch_retries):
            try:
                fetched = _fetch_proxies_from_pool(self._pool_cfg)
                if fetched:
                    break
            except Exception as exc:
                last_error = str(exc)

        valid: list[str] = []
        for proxy_value in fetched:
            if self._tcp_check_enabled and not _proxy_tcp_reachable(
                proxy_value,
                timeout_seconds=self._tcp_check_timeout_seconds,
            ):
                last_error = f"代理池代理不可达: {proxy_value}"
                continue
            if not _proxy_http_reachable(
                proxy_value,
                timeout_seconds=self._validate_timeout_seconds,
                test_url=self._validate_test_url,
            ):
                last_error = f"代理池代理 HTTPS 校验失败: {proxy_value}"
                continue
            valid.append(proxy_value)

        self._last_fetched_count = len(fetched)
        self._last_valid_count = len(valid)
        if valid:
            self._proxies = list(valid)
            self._index = 0
            self._last_error = ""
        else:
            self._last_error = last_error or "代理池无可用代理"
        return valid

    def next_proxy(self) -> str:
        with self._lock:
            self._clear_expired_bad_locked()
            if self._prefer_stable_proxy and self._is_available_locked(self._stable_proxy):
                return self._stable_proxy

            available = [proxy for proxy in self._proxies if self._is_available_locked(proxy)]
            if available:
                proxy_value = available[self._index % len(available)]
                self._index = (self._index + 1) % len(available)
                return proxy_value

            available = [
                proxy for proxy in self._refresh_candidates_locked()
                if self._is_available_locked(proxy)
            ]
            if available:
                proxy_value = available[self._index % len(available)]
                self._index = (self._index + 1) % len(available)
                return proxy_value
            if self._fallback_proxy:
                return self._fallback_proxy
            raise RuntimeError(self._last_error or "代理池无可用代理")

    def report_bad(self, proxy_value: str, error: Any = None) -> None:
        normalized = _normalize_proxy_value(proxy_value)
        if not normalized:
            return
        with self._lock:
            self._bad_until[normalized] = time.time() + self._bad_ttl_seconds
            if normalized == self._stable_proxy:
                self._stable_proxy = ""
            if error is not None:
                self._last_error = str(error)

    def report_success(self, proxy_value: str) -> None:
        normalized = _normalize_proxy_value(proxy_value)
        if not normalized:
            return
        with self._lock:
            self._stable_proxy = normalized
            self._bad_until.pop(normalized, None)
            self._last_error = ""

    def get_last_error(self) -> str:
        with self._lock:
            return str(self._last_error or "")

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            self._clear_expired_bad_locked()
            return {
                "stable_proxy": self._stable_proxy,
                "proxy_count": len(self._proxies),
                "fetched_count": self._last_fetched_count,
                "valid_count": self._last_valid_count,
                "bad_count": len(self._bad_until),
                "last_error": self._last_error,
            }


def _get_request_proxy_pool_state(
    pool_cfg: Dict[str, Any],
    fallback_proxy: str = "",
) -> _RequestProxyPoolState:
    signature = _proxy_pool_signature(pool_cfg, fallback_proxy=fallback_proxy)
    with _REQUEST_PROXY_POOLS_LOCK:
        state = _REQUEST_PROXY_POOLS.get(signature)
        if state is None:
            state = _RequestProxyPoolState(pool_cfg, fallback_proxy=fallback_proxy)
            _REQUEST_PROXY_POOLS[signature] = state
        else:
            state.refresh_config(pool_cfg, fallback_proxy=fallback_proxy)
        return state


def _resolve_request_proxies(
    default_proxies: Any = None,
    proxy_selector: Optional[Callable[[], Any]] = None,
) -> Any:
    if not proxy_selector:
        return default_proxies
    try:
        selected = proxy_selector()
        if selected is not None:
            return selected
    except Exception:
        pass
    return default_proxies


def _mailtm_headers(*, token: str = "", use_json: bool = False) -> Dict[str, str]:
    headers = {"Accept": "application/json"}
    if use_json:
        headers["Content-Type"] = "application/json"
    if token:
        headers["Authorization"] = f"Bearer {token}"
    return headers


def _mailtm_domains(proxies: Any = None) -> list[str]:
    resp = _call_with_http_fallback(
        requests.get,
        f"{MAILTM_BASE}/domains",
        headers=_mailtm_headers(),
        proxies=proxies,
        http_version=DEFAULT_HTTP_VERSION,
        impersonate="chrome",
        timeout=15,
    )
    if resp.status_code != 200:
        raise RuntimeError(f"获取 Mail.tm 域名失败，状态码: {resp.status_code}")

    data = resp.json()
    domains = []
    if isinstance(data, list):
        items = data
    elif isinstance(data, dict):
        items = data.get("hydra:member") or data.get("items") or []
    else:
        items = []

    for item in items:
        if not isinstance(item, dict):
            continue
        domain = str(item.get("domain") or "").strip()
        is_active = item.get("isActive", True)
        is_private = item.get("isPrivate", False)
        if domain and is_active and not is_private:
            domains.append(domain)

    return domains


def get_email_and_token(
    proxies: Any = None,
    emitter: EventEmitter = _cli_emitter,
    proxy_selector: Optional[Callable[[], Any]] = None,
) -> tuple[str, str]:
    """创建 Mail.tm 邮箱并获取 Bearer Token"""
    try:
        domains = _mailtm_domains(_resolve_request_proxies(proxies, proxy_selector))
        if not domains:
            emitter.error("Mail.tm 没有可用域名", step="create_email")
            return "", ""
        domain = random.choice(domains)

        for _ in range(5):
            local = f"oc{secrets.token_hex(5)}"
            email = f"{local}@{domain}"
            password = secrets.token_urlsafe(18)

            create_resp = _call_with_http_fallback(
                requests.post,
                f"{MAILTM_BASE}/accounts",
                headers=_mailtm_headers(use_json=True),
                json={"address": email, "password": password},
                proxies=_resolve_request_proxies(proxies, proxy_selector),
                http_version=DEFAULT_HTTP_VERSION,
                impersonate="chrome",
                timeout=15,
            )

            if create_resp.status_code not in (200, 201):
                continue

            token_resp = _call_with_http_fallback(
                requests.post,
                f"{MAILTM_BASE}/token",
                headers=_mailtm_headers(use_json=True),
                json={"address": email, "password": password},
                proxies=_resolve_request_proxies(proxies, proxy_selector),
                http_version=DEFAULT_HTTP_VERSION,
                impersonate="chrome",
                timeout=15,
            )

            if token_resp.status_code == 200:
                token = str(token_resp.json().get("token") or "").strip()
                if token:
                    return email, token

        emitter.error("Mail.tm 邮箱创建成功但获取 Token 失败", step="create_email")
        return "", ""
    except Exception as e:
        emitter.error(f"请求 Mail.tm API 出错: {e}", step="create_email")
        return "", ""


def get_oai_code(
    token: str, email: str, proxies: Any = None, emitter: EventEmitter = _cli_emitter,
    stop_event: Optional[threading.Event] = None,
    proxy_selector: Optional[Callable[[], Any]] = None,
) -> str:
    """使用 Mail.tm Token 轮询获取 OpenAI 验证码"""
    url_list = f"{MAILTM_BASE}/messages"
    regex = r"(?<!\d)(\d{6})(?!\d)"
    seen_ids: set[str] = set()

    emitter.info(f"正在等待邮箱 {email} 的验证码...", step="wait_otp")

    for i in range(40):
        if stop_event and stop_event.is_set():
            return ""
        try:
            request_timeout = _interruptible_timeout(15, stop_event)
            resp = _call_with_http_fallback(
                requests.get,
                url_list,
                headers=_mailtm_headers(token=token),
                proxies=_resolve_request_proxies(proxies, proxy_selector),
                http_version=DEFAULT_HTTP_VERSION,
                impersonate="chrome",
                timeout=request_timeout,
            )
            if resp.status_code != 200:
                if not _wait_with_stop(3, stop_event):
                    return ""
                continue

            data = resp.json()
            if isinstance(data, list):
                messages = data
            elif isinstance(data, dict):
                messages = data.get("hydra:member") or data.get("messages") or []
            else:
                messages = []

            for msg in messages:
                if not isinstance(msg, dict):
                    continue
                msg_id = str(msg.get("id") or "").strip()
                if not msg_id or msg_id in seen_ids:
                    continue

                read_resp = _call_with_http_fallback(
                    requests.get,
                    f"{MAILTM_BASE}/messages/{msg_id}",
                    headers=_mailtm_headers(token=token),
                    proxies=_resolve_request_proxies(proxies, proxy_selector),
                    http_version=DEFAULT_HTTP_VERSION,
                    impersonate="chrome",
                    timeout=request_timeout,
                )
                if read_resp.status_code != 200:
                    continue
                seen_ids.add(msg_id)

                mail_data = read_resp.json()
                sender = str(
                    ((mail_data.get("from") or {}).get("address") or "")
                ).lower()
                subject = str(mail_data.get("subject") or "")
                intro = str(mail_data.get("intro") or "")
                text = str(mail_data.get("text") or "")
                html = mail_data.get("html") or ""
                if isinstance(html, list):
                    html = "\n".join(str(x) for x in html)
                content = "\n".join([subject, intro, text, str(html)])

                if "openai" not in sender and "openai" not in content.lower():
                    continue

                m = re.search(regex, content)
                if m:
                    emitter.success(f"验证码已到达: {m.group(1)}", step="wait_otp")
                    return m.group(1)
        except Exception:
            pass

        # 每轮等待时输出进度
        if (i + 1) % 5 == 0:
            emitter.info(f"已等待 {(i+1)*3} 秒，继续轮询...", step="wait_otp")
        if not _wait_with_stop(3, stop_event):
            return ""

    emitter.error("超时，未收到验证码", step="wait_otp")
    return ""


# ==========================================
# OAuth 授权与辅助函数
# ==========================================

AUTH_URL = "https://auth.openai.com/oauth/authorize"
TOKEN_URL = "https://auth.openai.com/oauth/token"
CLIENT_ID = "app_EMoamEEZ73f0CkXaXp7hrann"

DEFAULT_REDIRECT_URI = f"http://localhost:1455/auth/callback"
DEFAULT_SCOPE = "openid email profile offline_access"


def _b64url_no_pad(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")


def _sha256_b64url_no_pad(s: str) -> str:
    return _b64url_no_pad(hashlib.sha256(s.encode("ascii")).digest())


def _random_state(nbytes: int = 16) -> str:
    return secrets.token_urlsafe(nbytes)


def _pkce_verifier() -> str:
    return secrets.token_urlsafe(64)


def _parse_callback_url(callback_url: str) -> Dict[str, str]:
    candidate = callback_url.strip()
    if not candidate:
        return {"code": "", "state": "", "error": "", "error_description": ""}

    if "://" not in candidate:
        if candidate.startswith("?"):
            candidate = f"http://localhost{candidate}"
        elif any(ch in candidate for ch in "/?#") or ":" in candidate:
            candidate = f"http://{candidate}"
        elif "=" in candidate:
            candidate = f"http://localhost/?{candidate}"

    parsed = urllib.parse.urlparse(candidate)
    query = urllib.parse.parse_qs(parsed.query, keep_blank_values=True)
    fragment = urllib.parse.parse_qs(parsed.fragment, keep_blank_values=True)

    for key, values in fragment.items():
        if key not in query or not query[key] or not (query[key][0] or "").strip():
            query[key] = values

    def get1(k: str) -> str:
        v = query.get(k, [""])
        return (v[0] or "").strip()

    code = get1("code")
    state = get1("state")
    error = get1("error")
    error_description = get1("error_description")

    if code and not state and "#" in code:
        code, state = code.split("#", 1)

    if not error and error_description:
        error, error_description = error_description, ""

    return {
        "code": code,
        "state": state,
        "error": error,
        "error_description": error_description,
    }


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


def _decode_jwt_segment(seg: str) -> Dict[str, Any]:
    raw = (seg or "").strip()
    if not raw:
        return {}
    pad = "=" * ((4 - (len(raw) % 4)) % 4)
    try:
        decoded = base64.urlsafe_b64decode((raw + pad).encode("ascii"))
        return json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}


def _to_int(v: Any) -> int:
    try:
        return int(v)
    except (TypeError, ValueError):
        return 0


def _post_form(
    url: str,
    data: Dict[str, str],
    timeout: int = 30,
    proxy: str = "",
    stop_event: Optional[threading.Event] = None,
) -> Dict[str, Any]:
    body = urllib.parse.urlencode(data).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=body,
        method="POST",
        headers={
            "Content-Type": "application/x-www-form-urlencoded",
            "Accept": "application/json",
        },
    )
    handlers = []
    normalized_proxy = _normalize_proxy_value(proxy)
    if normalized_proxy:
        handlers.append(urllib.request.ProxyHandler({"http": normalized_proxy, "https": normalized_proxy}))
    opener = urllib.request.build_opener(*handlers)
    last_error: Optional[Exception] = None
    max_attempts = TRANSIENT_TLS_RETRY_COUNT + 1
    for attempt in range(max_attempts):
        if stop_event and stop_event.is_set():
            raise RuntimeError("token exchange cancelled")
        try:
            request_timeout = _interruptible_timeout(timeout, stop_event)
            with opener.open(req, timeout=request_timeout) as resp:
                raw = resp.read()
                if resp.status != 200:
                    raise RuntimeError(
                        f"token exchange failed: {resp.status}: {raw.decode('utf-8', 'replace')}"
                    )
                return json.loads(raw.decode("utf-8"))
        except urllib.error.HTTPError as exc:
            raw = exc.read()
            raise RuntimeError(
                f"token exchange failed: {exc.code}: {raw.decode('utf-8', 'replace')}"
            ) from exc
        except Exception as exc:
            if not _is_transient_tls_error(exc) or attempt >= max_attempts - 1:
                raise
            last_error = exc
            if not _wait_with_stop(min(0.35 * (attempt + 1), 1.0), stop_event):
                raise RuntimeError("token exchange cancelled") from exc
    if last_error is not None:
        raise last_error
    raise RuntimeError("token exchange failed without response")


def _build_token_result(token_payload: Dict[str, Any]) -> str:
    return _health_build_token_result(token_payload)


def _validate_registration_token(
    token_json: str,
    *,
    proxy: str = "",
    emitter: Optional[EventEmitter] = None,
) -> bool:
    try:
        token_data = json.loads(token_json)
    except Exception as exc:
        if emitter is not None:
            emitter.error(f"Token 结果解析失败: {exc}", step="get_token")
        return False
    if not isinstance(token_data, dict):
        if emitter is not None:
            emitter.error("Token 结果格式异常", step="get_token")
        return False

    access_token = str(token_data.get("access_token") or "").strip()
    verify_result = _health_check_access_token(access_token, proxy=proxy, max_tries=2)
    verify_status = str(verify_result.get("status") or "")
    verify_error = str(verify_result.get("error") or "")

    if verify_status == "alive":
        if emitter is not None:
            emitter.success("新账号 API 验证通过", step="get_token")
        return True
    if verify_status in ("geo_blocked", "error"):
        if emitter is not None:
            emitter.warn(
                f"新账号 API 验证结果不确定，保留本次注册结果: {verify_error or verify_status}",
                step="get_token",
            )
        return True
    if emitter is not None:
        emitter.error(f"新账号 API 验证失败: {verify_error or verify_status}", step="get_token")
    return False


def _write_text_atomic(file_path: str, content: str) -> None:
    directory = os.path.dirname(file_path) or "."
    os.makedirs(directory, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=".tmp_", suffix=".json", dir=directory)
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, file_path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


@dataclass(frozen=True)
class OAuthStart:
    auth_url: str
    state: str
    code_verifier: str
    redirect_uri: str


def generate_oauth_url(
    *, redirect_uri: str = DEFAULT_REDIRECT_URI, scope: str = DEFAULT_SCOPE
) -> OAuthStart:
    state = _random_state()
    code_verifier = _pkce_verifier()
    code_challenge = _sha256_b64url_no_pad(code_verifier)

    params = {
        "client_id": CLIENT_ID,
        "response_type": "code",
        "redirect_uri": redirect_uri,
        "scope": scope,
        "state": state,
        "code_challenge": code_challenge,
        "code_challenge_method": "S256",
        "prompt": "login",
        "screen_hint": "signup",
        "id_token_add_organizations": "true",
        "codex_cli_simplified_flow": "true",
    }
    auth_url = f"{AUTH_URL}?{urllib.parse.urlencode(params)}"
    return OAuthStart(
        auth_url=auth_url,
        state=state,
        code_verifier=code_verifier,
        redirect_uri=redirect_uri,
    )


def submit_callback_url(
    *,
    callback_url: str,
    expected_state: str,
    code_verifier: str,
    redirect_uri: str = DEFAULT_REDIRECT_URI,
    proxy: str = "",
    stop_event: Optional[threading.Event] = None,
) -> str:
    cb = _parse_callback_url(callback_url)
    if cb["error"]:
        desc = cb["error_description"]
        raise RuntimeError(f"oauth error: {cb['error']}: {desc}".strip())

    if not cb["code"]:
        raise ValueError("callback url missing ?code=")
    if not cb["state"]:
        raise ValueError("callback url missing ?state=")
    if cb["state"] != expected_state:
        raise ValueError("state mismatch")

    token_resp = _post_form(
        TOKEN_URL,
        {
            "grant_type": "authorization_code",
            "client_id": CLIENT_ID,
            "code": cb["code"],
            "redirect_uri": redirect_uri,
            "code_verifier": code_verifier,
        },
        proxy=proxy,
        stop_event=stop_event,
    )

    return _build_token_result(token_resp)


# ==========================================
# 核心注册逻辑
# ==========================================

from .local_tokens import save_local_token_text


def run(
    proxy: Optional[str],
    emitter: EventEmitter = _cli_emitter,
    stop_event: Optional[threading.Event] = None,
    mail_provider=None,
    proxy_pool_config: Optional[Dict[str, Any]] = None,
) -> Optional[str]:
    static_proxy = _normalize_proxy_value(proxy)
    static_proxies: Any = _to_proxies_dict(static_proxy)

    pool_cfg_raw = proxy_pool_config or {}
    pool_cfg = {
        "enabled": bool(pool_cfg_raw.get("enabled", False)),
        "api_url": str(pool_cfg_raw.get("api_url") or DEFAULT_PROXY_POOL_URL).strip() or DEFAULT_PROXY_POOL_URL,
        "auth_mode": str(pool_cfg_raw.get("auth_mode") or DEFAULT_PROXY_POOL_AUTH_MODE).strip().lower() or DEFAULT_PROXY_POOL_AUTH_MODE,
        "api_key": str(pool_cfg_raw.get("api_key") or DEFAULT_PROXY_POOL_API_KEY).strip() or DEFAULT_PROXY_POOL_API_KEY,
        "count": pool_cfg_raw.get("count", DEFAULT_PROXY_POOL_COUNT),
        "country": str(pool_cfg_raw.get("country") or DEFAULT_PROXY_POOL_COUNTRY).strip().upper() or DEFAULT_PROXY_POOL_COUNTRY,
        "timeout_seconds": int(pool_cfg_raw.get("timeout_seconds") or 10),
        "fetch_retries": int(pool_cfg_raw.get("fetch_retries") or POOL_PROXY_FETCH_RETRIES),
        "bad_ttl_seconds": int(pool_cfg_raw.get("bad_ttl_seconds") or DEFAULT_PROXY_POOL_BAD_TTL_SECONDS),
        "tcp_check_enabled": bool(pool_cfg_raw.get("tcp_check_enabled", DEFAULT_PROXY_POOL_TCP_CHECK_ENABLED)),
        "tcp_check_timeout_seconds": float(
            pool_cfg_raw.get("tcp_check_timeout_seconds") or DEFAULT_PROXY_POOL_TCP_CHECK_TIMEOUT_SECONDS
        ),
        "prefer_stable_proxy": bool(
            pool_cfg_raw.get("prefer_stable_proxy", DEFAULT_PROXY_POOL_PREFER_STABLE_PROXY)
        ),
        "stable_proxy": _normalize_proxy_value(pool_cfg_raw.get("stable_proxy") or ""),
    }
    if pool_cfg["auth_mode"] not in ("header", "query"):
        pool_cfg["auth_mode"] = DEFAULT_PROXY_POOL_AUTH_MODE
    try:
        pool_cfg["count"] = max(1, min(int(pool_cfg.get("count") or DEFAULT_PROXY_POOL_COUNT), 20))
    except (TypeError, ValueError):
        pool_cfg["count"] = DEFAULT_PROXY_POOL_COUNT
    try:
        pool_cfg["fetch_retries"] = max(1, min(int(pool_cfg.get("fetch_retries") or POOL_PROXY_FETCH_RETRIES), 10))
    except (TypeError, ValueError):
        pool_cfg["fetch_retries"] = POOL_PROXY_FETCH_RETRIES
    try:
        pool_cfg["bad_ttl_seconds"] = max(
            10,
            min(int(pool_cfg.get("bad_ttl_seconds") or DEFAULT_PROXY_POOL_BAD_TTL_SECONDS), 3600),
        )
    except (TypeError, ValueError):
        pool_cfg["bad_ttl_seconds"] = DEFAULT_PROXY_POOL_BAD_TTL_SECONDS
    pool_cfg["tcp_check_enabled"] = bool(pool_cfg.get("tcp_check_enabled", DEFAULT_PROXY_POOL_TCP_CHECK_ENABLED))
    try:
        pool_cfg["tcp_check_timeout_seconds"] = max(
            0.5,
            min(float(pool_cfg.get("tcp_check_timeout_seconds") or DEFAULT_PROXY_POOL_TCP_CHECK_TIMEOUT_SECONDS), 10.0),
        )
    except (TypeError, ValueError):
        pool_cfg["tcp_check_timeout_seconds"] = DEFAULT_PROXY_POOL_TCP_CHECK_TIMEOUT_SECONDS

    proxy_runtime = _get_request_proxy_pool_state(pool_cfg, fallback_proxy=static_proxy)
    pool_fail_streak = 0
    warned_fallback = False

    def _next_proxy_value() -> str:
        nonlocal pool_fail_streak, warned_fallback
        if pool_cfg["enabled"]:
            try:
                selected = proxy_runtime.next_proxy()
                if selected == static_proxy and proxy_runtime.get_last_error():
                    if not warned_fallback:
                        emitter.warn(
                            f"代理池不可用，回退固定代理: {proxy_runtime.get_last_error()}",
                            step="check_proxy",
                        )
                        warned_fallback = True
                else:
                    pool_fail_streak = 0
                    warned_fallback = False
                return selected
            except Exception:
                pass
            pool_fail_streak += 1
            error_text = proxy_runtime.get_last_error() or "unknown error"
            if static_proxy:
                if not warned_fallback:
                    emitter.warn(f"代理池不可用，回退固定代理: {error_text}", step="check_proxy")
                    warned_fallback = True
                return static_proxy
            if pool_fail_streak <= 3:
                emitter.warn(f"代理池不可用: {error_text}", step="check_proxy")
            return ""
        return static_proxy

    def _next_proxies() -> Any:
        proxy_value = _next_proxy_value()
        return _to_proxies_dict(proxy_value)

    def _report_pool_proxy_result(proxy_value: str, ok: bool, detail: Any = None) -> None:
        normalized = _normalize_proxy_value(proxy_value)
        if not pool_cfg["enabled"] or not normalized or normalized == static_proxy:
            return
        if ok:
            proxy_runtime.report_success(normalized)
        else:
            proxy_runtime.report_bad(normalized, detail)

    def _request_with_direct_proxy(request_func, url: str, **kwargs: Any):
        selected_proxy = _next_proxy_value()
        kwargs["proxies"] = _to_proxies_dict(selected_proxy)
        try:
            resp = _call_with_http_fallback(request_func, url, **kwargs)
        except Exception as exc:
            _report_pool_proxy_result(selected_proxy, False, exc)
            raise
        status_code = int(resp.status_code or 0)
        if status_code in _PROXY_BAD_STATUS_CODES:
            _report_pool_proxy_result(selected_proxy, False, f"HTTP {status_code}")
        elif selected_proxy:
            _report_pool_proxy_result(selected_proxy, True)
        return resp

    s = requests.Session(impersonate="chrome")
    pool_relay_url = _pool_relay_url_from_fetch_url(str(pool_cfg.get("api_url") or ""))
    pool_relay_enabled = bool(pool_cfg["enabled"] and pool_relay_url)
    relay_cookie_jar: Dict[str, str] = {}
    pool_relay_api_key = str(pool_cfg.get("api_key") or DEFAULT_PROXY_POOL_API_KEY).strip() or DEFAULT_PROXY_POOL_API_KEY
    pool_relay_country = str(pool_cfg.get("country") or DEFAULT_PROXY_POOL_COUNTRY).strip().upper() or DEFAULT_PROXY_POOL_COUNTRY
    relay_fallback_warned = False
    relay_bypass_openai_hosts = False
    openai_relay_probe_done = False
    mail_proxy_selector = _next_proxy_value if pool_cfg["enabled"] else None
    mail_proxies_selector = _next_proxies if pool_cfg["enabled"] else None

    def _fallback_proxies_for_relay_failure() -> Any:
        if static_proxy:
            return _to_proxies_dict(static_proxy)
        if pool_cfg["enabled"]:
            return _next_proxies()
        return None

    def _target_host(target_url: str) -> str:
        return str(urlparse(str(target_url or "")).hostname or "").strip().lower()

    def _is_openai_like_host(host: str) -> bool:
        return bool(host) and (host.endswith("openai.com") or host.endswith("chatgpt.com"))

    def _should_bypass_relay_for_target(target_url: str) -> bool:
        host = _target_host(target_url)
        return relay_bypass_openai_hosts and _is_openai_like_host(host)

    def _warn_relay_fallback(reason: str, target_url: str) -> None:
        nonlocal relay_fallback_warned, relay_bypass_openai_hosts
        host = _target_host(target_url) or str(target_url or "?")
        if _is_openai_like_host(host):
            relay_bypass_openai_hosts = True
        if relay_fallback_warned:
            return
        if static_proxy:
            emitter.warn(f"代理池 relay 对 {host} 不可用，回退固定代理: {reason}", step="check_proxy")
        else:
            emitter.warn(f"代理池 relay 对 {host} 不可用，回退直连代理: {reason}", step="check_proxy")
        relay_fallback_warned = True

    def _update_relay_cookie_jar(resp: Any) -> None:
        try:
            for k, v in (resp.cookies or {}).items():
                key = str(k or "").strip()
                if key:
                    relay_cookie_jar[key] = str(v or "")
        except Exception:
            pass
        set_cookie_values: list[str] = []
        try:
            values = resp.headers.get_list("set-cookie")  # type: ignore[attr-defined]
            if values:
                set_cookie_values.extend(str(v or "") for v in values if str(v or "").strip())
        except Exception:
            pass
        if not set_cookie_values:
            try:
                set_cookie_raw = str(resp.headers.get("set-cookie") or "")
                if set_cookie_raw.strip():
                    set_cookie_values.append(set_cookie_raw)
            except Exception:
                pass
        for set_cookie_raw in set_cookie_values:
            try:
                parsed_cookie = SimpleCookie()
                parsed_cookie.load(set_cookie_raw)
                for k, morsel in parsed_cookie.items():
                    key = str(k or "").strip()
                    if key:
                        relay_cookie_jar[key] = str(morsel.value or "")
            except Exception:
                pass
        try:
            for k, v in relay_cookie_jar.items():
                s.cookies.set(k, v)
        except Exception:
            pass

    def _request_via_pool_relay(method: str, target_url: str, **kwargs: Any):
        if not pool_relay_enabled:
            raise RuntimeError("代理池 relay 未启用")
        relay_retries_override = kwargs.pop("_relay_retries", None)
        relay_params = {
            "api_key": pool_relay_api_key,
            "url": str(target_url),
            "method": str(method or "GET").upper(),
            "country": pool_relay_country,
        }
        target_params = kwargs.pop("params", None)
        if target_params:
            query_text = urlencode(target_params, doseq=True)
            if query_text:
                separator = "&" if "?" in relay_params["url"] else "?"
                relay_params["url"] = f"{relay_params['url']}{separator}{query_text}"

        headers = dict(kwargs.pop("headers", {}) or {})
        if relay_cookie_jar and not any(str(k).lower() == "cookie" for k in headers.keys()):
            headers["Cookie"] = "; ".join(f"{k}={v}" for k, v in relay_cookie_jar.items())
        kwargs.pop("proxies", None)
        kwargs.setdefault("impersonate", "chrome")
        kwargs.setdefault("http_version", DEFAULT_HTTP_VERSION)
        kwargs.setdefault("timeout", 20)

        method_upper = relay_params["method"]
        retry_count = max(
            1,
            int(
                relay_retries_override
                if relay_retries_override is not None
                else (pool_cfg.get("relay_request_retries") or POOL_RELAY_REQUEST_RETRIES)
            ),
        )
        last_error = ""
        for i in range(retry_count):
            try:
                resp = _call_with_http_fallback(
                    lambda relay_endpoint, **call_kwargs: requests.request(method_upper, relay_endpoint, **call_kwargs),
                    pool_relay_url,
                    params=relay_params,
                    headers=headers or None,
                    **kwargs,
                )
                _update_relay_cookie_jar(resp)
                if resp.status_code >= 500 or resp.status_code == 429:
                    last_error = f"HTTP {resp.status_code}"
                    if i < retry_count - 1:
                        time.sleep(min(0.4 * (i + 1), 1.2))
                        continue
                return resp
            except Exception as exc:
                last_error = str(exc)
                if i < retry_count - 1:
                    time.sleep(min(0.4 * (i + 1), 1.2))
        raise RuntimeError(f"代理池 relay 请求失败: {last_error or 'unknown error'}")

    def _ensure_openai_relay_ready() -> None:
        nonlocal openai_relay_probe_done
        if not pool_relay_enabled or relay_bypass_openai_hosts or openai_relay_probe_done:
            return
        openai_relay_probe_done = True
        probe_url = "https://auth.openai.com/"
        try:
            probe_resp = _request_via_pool_relay(
                "GET",
                probe_url,
                timeout=5,
                allow_redirects=False,
                _relay_retries=1,
            )
            status = int(probe_resp.status_code or 0)
            if status < 200 or status >= 400:
                raise RuntimeError(f"HTTP {status}")
            emitter.info("代理池 relay OpenAI 预检通过", step="check_proxy")
        except Exception as exc:
            _warn_relay_fallback(f"{exc} (OpenAI 预检)", probe_url)

    def _session_get(url: str, **kwargs: Any):
        if pool_relay_enabled and not _should_bypass_relay_for_target(url):
            kwargs.setdefault("timeout", 20)
            kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
            try:
                relay_resp = _request_via_pool_relay("GET", url, **kwargs)
                if relay_resp.status_code < 500 and relay_resp.status_code != 429:
                    return relay_resp
                raise RuntimeError(f"HTTP {relay_resp.status_code}")
            except Exception as exc:
                _warn_relay_fallback(str(exc), url)
                kwargs.setdefault("http_version", DEFAULT_HTTP_VERSION)
                kwargs.setdefault("timeout", 20)
                kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
                return _request_with_direct_proxy(s.get, url, **kwargs)
        if pool_relay_enabled and _should_bypass_relay_for_target(url):
            kwargs.setdefault("http_version", DEFAULT_HTTP_VERSION)
            kwargs.setdefault("timeout", 20)
            kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
            return _request_with_direct_proxy(s.get, url, **kwargs)
        kwargs.setdefault("http_version", DEFAULT_HTTP_VERSION)
        kwargs.setdefault("timeout", 15)
        kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
        return _request_with_direct_proxy(s.get, url, **kwargs)

    def _session_post(url: str, **kwargs: Any):
        if pool_relay_enabled and not _should_bypass_relay_for_target(url):
            kwargs.setdefault("timeout", 20)
            kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
            try:
                relay_resp = _request_via_pool_relay("POST", url, **kwargs)
                if relay_resp.status_code < 500 and relay_resp.status_code != 429:
                    return relay_resp
                raise RuntimeError(f"HTTP {relay_resp.status_code}")
            except Exception as exc:
                _warn_relay_fallback(str(exc), url)
                kwargs.setdefault("http_version", DEFAULT_HTTP_VERSION)
                kwargs.setdefault("timeout", 20)
                kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
                return _request_with_direct_proxy(s.post, url, **kwargs)
        if pool_relay_enabled and _should_bypass_relay_for_target(url):
            kwargs.setdefault("http_version", DEFAULT_HTTP_VERSION)
            kwargs.setdefault("timeout", 20)
            kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
            return _request_with_direct_proxy(s.post, url, **kwargs)
        kwargs.setdefault("http_version", DEFAULT_HTTP_VERSION)
        kwargs.setdefault("timeout", 15)
        kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
        return _request_with_direct_proxy(s.post, url, **kwargs)

    def _raw_get(url: str, **kwargs: Any):
        if pool_relay_enabled and not _should_bypass_relay_for_target(url):
            kwargs.setdefault("timeout", 20)
            kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
            try:
                relay_resp = _request_via_pool_relay("GET", url, **kwargs)
                if relay_resp.status_code < 500 and relay_resp.status_code != 429:
                    return relay_resp
                raise RuntimeError(f"HTTP {relay_resp.status_code}")
            except Exception as exc:
                _warn_relay_fallback(str(exc), url)
                kwargs.setdefault("http_version", DEFAULT_HTTP_VERSION)
                kwargs.setdefault("impersonate", "chrome")
                kwargs.setdefault("timeout", 20)
                kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
                return _request_with_direct_proxy(requests.get, url, **kwargs)
        if pool_relay_enabled and _should_bypass_relay_for_target(url):
            kwargs.setdefault("http_version", DEFAULT_HTTP_VERSION)
            kwargs.setdefault("impersonate", "chrome")
            kwargs.setdefault("timeout", 20)
            kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
            return _request_with_direct_proxy(requests.get, url, **kwargs)
        kwargs.setdefault("http_version", DEFAULT_HTTP_VERSION)
        kwargs.setdefault("impersonate", "chrome")
        kwargs.setdefault("timeout", 15)
        kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
        return _request_with_direct_proxy(requests.get, url, **kwargs)

    def _raw_post(url: str, **kwargs: Any):
        if pool_relay_enabled and not _should_bypass_relay_for_target(url):
            kwargs.setdefault("timeout", 20)
            kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
            try:
                relay_resp = _request_via_pool_relay("POST", url, **kwargs)
                if relay_resp.status_code < 500 and relay_resp.status_code != 429:
                    return relay_resp
                raise RuntimeError(f"HTTP {relay_resp.status_code}")
            except Exception as exc:
                _warn_relay_fallback(str(exc), url)
                kwargs.setdefault("http_version", DEFAULT_HTTP_VERSION)
                kwargs.setdefault("impersonate", "chrome")
                kwargs.setdefault("timeout", 20)
                kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
                return _request_with_direct_proxy(requests.post, url, **kwargs)
        if pool_relay_enabled and _should_bypass_relay_for_target(url):
            kwargs.setdefault("http_version", DEFAULT_HTTP_VERSION)
            kwargs.setdefault("impersonate", "chrome")
            kwargs.setdefault("timeout", 20)
            kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
            return _request_with_direct_proxy(requests.post, url, **kwargs)
        kwargs.setdefault("http_version", DEFAULT_HTTP_VERSION)
        kwargs.setdefault("impersonate", "chrome")
        kwargs.setdefault("timeout", 15)
        kwargs["timeout"] = _interruptible_timeout(kwargs.get("timeout"), stop_event)
        return _request_with_direct_proxy(requests.post, url, **kwargs)

    def _submit_callback_url_via_pool_relay(
        *,
        callback_url: str,
        expected_state: str,
        code_verifier: str,
        redirect_uri: str = DEFAULT_REDIRECT_URI,
    ) -> str:
        cb = _parse_callback_url(callback_url)
        if cb["error"]:
            desc = cb["error_description"]
            raise RuntimeError(f"oauth error: {cb['error']}: {desc}".strip())
        if not cb["code"]:
            raise ValueError("callback url missing ?code=")
        if not cb["state"]:
            raise ValueError("callback url missing ?state=")
        if cb["state"] != expected_state:
            raise ValueError("state mismatch")

        token_resp = _request_via_pool_relay(
            "POST",
            TOKEN_URL,
            headers={
                "Content-Type": "application/x-www-form-urlencoded",
                "Accept": "application/json",
            },
            data=urllib.parse.urlencode(
                {
                    "grant_type": "authorization_code",
                    "client_id": CLIENT_ID,
                    "code": cb["code"],
                    "redirect_uri": redirect_uri,
                    "code_verifier": code_verifier,
                }
            ),
            timeout=30,
        )
        if token_resp.status_code != 200:
            raise RuntimeError(
                f"token exchange failed: {token_resp.status_code}: {str(token_resp.text or '')[:240]}"
            )
        try:
            token_json = token_resp.json()
        except Exception:
            token_json = json.loads(str(token_resp.text or "{}"))

        return _build_token_result(token_json)

    def _stopped() -> bool:
        return stop_event is not None and stop_event.is_set()

    try:
        # ------- 步骤1：网络环境检查 -------
        emitter.info("正在检查网络环境...", step="check_proxy")
        try:
            trace_text = ""
            relay_used = False
            trace_probe_result: Dict[str, Any] = {}
            if pool_relay_enabled:
                try:
                    trace_text = _trace_via_pool_relay(pool_cfg, stop_event=stop_event)
                    relay_used = True
                except Exception as e:
                    if static_proxy:
                        emitter.warn(f"代理池 relay 检查失败，回退固定代理: {e}", step="check_proxy")
                    else:
                        emitter.warn(f"代理池 relay 检查失败，尝试直连代理: {e}", step="check_proxy")
            if not trace_text:
                trace_probe_result = _probe_cloudflare_trace_with_proxy_rotation(
                    s.get,
                    _next_proxy_value,
                    _report_pool_proxy_result,
                    max_attempts=3 if pool_cfg["enabled"] else 1,
                    timeout=10,
                    stop_event=stop_event,
                )
                trace_text = str(trace_probe_result.get("trace_text") or "")
            trace = trace_text
            trace_meta = _parse_cloudflare_trace_text(trace)
            loc = trace_probe_result.get("loc") or trace_meta.get("loc") or None
            current_ip = str(trace_probe_result.get("ip") or trace_meta.get("ip") or "").strip()
            if relay_used:
                emitter.info("代理池 relay 连通检查成功", step="check_proxy")
            elif pool_cfg["enabled"] and int(trace_probe_result.get("attempt") or 1) > 1:
                emitter.warn(
                    f"网络检查已切换代理并成功通过 (第 {int(trace_probe_result.get('attempt') or 1)} 次)",
                    step="check_proxy",
                )
            emitter.info(f"当前 IP 所在地: {loc}", step="check_proxy")
            if current_ip:
                emitter.info(f"当前出口 IP: {current_ip}", step="check_proxy")
            if loc == "CN" or loc == "HK":
                emitter.error("检查代理哦 — 所在地不支持 (CN/HK)", step="check_proxy")
                return None
            emitter.success("网络环境检查通过", step="check_proxy")
            _ensure_openai_relay_ready()
        except Exception as e:
            emitter.error(f"网络连接检查失败: {e}", step="check_proxy")
            return None

        if _stopped():
            return None

        # ------- 步骤2：创建临时邮箱 -------
        if mail_provider is not None:
            emitter.info("正在创建临时邮箱...", step="create_email")
            try:
                email, dev_token = mail_provider.create_mailbox(
                    proxy=static_proxy,
                    proxy_selector=mail_proxy_selector,
                    proxy_reporter=_report_pool_proxy_result,
                    stop_event=stop_event,
                )
            except TypeError:
                email, dev_token = mail_provider.create_mailbox(proxy=static_proxy)
        else:
            emitter.info("正在创建 Mail.tm 临时邮箱...", step="create_email")
            email, dev_token = get_email_and_token(
                static_proxies,
                emitter,
                proxy_selector=mail_proxies_selector,
            )
        if not email or not dev_token:
            emitter.error("临时邮箱创建失败", step="create_email")
            return None
        emitter.success(f"临时邮箱创建成功: {email}", step="create_email")

        if _stopped():
            return None

        # ------- 步骤3：生成 OAuth URL，获取 Device ID -------
        emitter.info("正在生成 OAuth 授权链接...", step="oauth_init")
        oauth = generate_oauth_url()
        url = oauth.auth_url

        did = s.cookies.get("oai-did") or relay_cookie_jar.get("oai-did") or ""
        if not did:
            did = str(uuid.uuid4())
            relay_cookie_jar["oai-did"] = did
            try:
                s.cookies.set("oai-did", did)
                s.cookies.set("oai-did", did, domain=".auth.openai.com")
                s.cookies.set("oai-did", did, domain="auth.openai.com")
            except Exception:
                pass

        resp = _session_get(url, timeout=20, headers=NAVIGATE_HEADERS, allow_redirects=True)
        emitter.info(f"OAuth 初始化状态: {resp.status_code}", step="oauth_init")
        if resp.status_code >= 400:
            emitter.error(f"OAuth 初始化失败，状态码: {resp.status_code}", step="oauth_init")
            return None
        did = s.cookies.get("oai-did") or relay_cookie_jar.get("oai-did") or did
        if not did:
            did_m = re.search(r"oai-did=([0-9a-fA-F-]{20,})", str(resp.text or ""))
            if did_m:
                did = did_m.group(1)
        if not did:
            emitter.warn(f"未从响应提取到 oai-did，已使用临时 Device ID: {did}", step="oauth_init")
        else:
            emitter.info(f"Device ID: {did}", step="oauth_init")

        has_login_session = bool(getattr(s.cookies, "get", None) and s.cookies.get("login_session"))
        if not has_login_session:
            has_login_session = "login_session" in relay_cookie_jar
        if not has_login_session:
            emitter.error("OAuth 初始化未获取 login_session cookie", step="oauth_init")
            return None

        if _stopped():
            return None

        # ------- 步骤4：获取 Sentinel Token -------
        emitter.info("正在获取 Sentinel Token...", step="sentinel")
        sentinel = _build_sentinel_token(did, flow="authorize_continue", emitter=emitter, post_func=_raw_post)
        if not sentinel:
            emitter.error("Sentinel Token 获取失败", step="sentinel")
            return None
        emitter.success("Sentinel Token 获取成功", step="sentinel")

        if _stopped():
            return None

        # ------- 步骤5：初始化注册会话 -------
        _email_retry_max = 3
        for _email_attempt in range(_email_retry_max):
            emitter.info("正在初始化注册会话...", step="signup")
            continue_resp = _session_post(
                "https://auth.openai.com/api/accounts/authorize/continue",
                headers=_build_openai_headers(
                    "https://auth.openai.com/create-account",
                    did,
                    sentinel_token=sentinel,
                ),
                json={"username": {"kind": "email", "value": email}, "screen_hint": "signup"},
            )
            if continue_resp.status_code == 200:
                break

            resp_text = str(continue_resp.text or "")
            if "unsupported_email" in resp_text and _email_attempt < _email_retry_max - 1:
                emitter.warn(
                    f"邮箱 {email} 被 OpenAI 拒绝（不支持的域名），正在重新生成邮箱... (第{_email_attempt + 1}次重试)",
                    step="signup",
                )
                if mail_provider is not None:
                    try:
                        email, dev_token = mail_provider.create_mailbox(
                            proxy=static_proxy,
                            proxy_selector=mail_proxy_selector,
                            proxy_reporter=_report_pool_proxy_result,
                            stop_event=stop_event,
                        )
                    except TypeError:
                        email, dev_token = mail_provider.create_mailbox(proxy=static_proxy)
                else:
                    email, dev_token = get_email_and_token(
                        static_proxies,
                        emitter,
                        proxy_selector=mail_proxies_selector,
                    )
                if not email or not dev_token:
                    emitter.error("重新创建邮箱失败", step="signup")
                    return None
                emitter.info(f"已切换到新邮箱: {email}", step="signup")
                continue

            emitter.error(
                f"注册会话初始化失败（状态码 {continue_resp.status_code}）: {resp_text[:220]}",
                step="signup",
            )
            return None

        # ------- 步骤6：提交注册 -------
        emitter.info("正在提交注册表单...", step="signup")
        sentinel_gen = _SentinelTokenGenerator(device_id=did)
        openai_password = _random_password(16)
        signup_resp = _session_post(
            "https://auth.openai.com/api/accounts/user/register",
            headers=_build_openai_headers(
                "https://auth.openai.com/create-account/password",
                did,
                sentinel_token=sentinel_gen.generate_token(),
            ),
            json={"username": email, "password": openai_password},
        )
        emitter.info(f"注册表单提交状态: {signup_resp.status_code}", step="signup")
        if signup_resp.status_code not in (200, 201, 302):
            emitter.error(
                f"注册表单提交失败（状态码 {signup_resp.status_code}）: {str(signup_resp.text or '')[:220]}",
                step="signup",
            )
            return None

        # ------- 步骤7：发送 OTP 验证码 -------
        emitter.info("正在发送邮箱验证码...", step="send_otp")
        otp_headers = dict(NAVIGATE_HEADERS)
        otp_headers["referer"] = "https://auth.openai.com/create-account/password"
        otp_resp = _session_get(
            "https://auth.openai.com/api/accounts/email-otp/send",
            headers=otp_headers,
            allow_redirects=True,
        )
        page_resp = _session_get(
            "https://auth.openai.com/email-verification",
            headers=otp_headers,
            allow_redirects=True,
        )
        emitter.info(
            f"验证码发送状态: send={otp_resp.status_code}, page={page_resp.status_code}",
            step="send_otp",
        )
        if otp_resp.status_code >= 400 or page_resp.status_code >= 400:
            body_preview = str(otp_resp.text or "")[:200].replace("\n", " ")
            emitter.warn(
                f"send_otp 异常: send={otp_resp.status_code}, page={page_resp.status_code}, body={body_preview}",
                step="send_otp",
            )
        if otp_resp.status_code >= 400:
            emitter.error(f"验证码发送失败（状态码 {otp_resp.status_code}），跳过本轮", step="send_otp")
            return None

        if _stopped():
            return None

        # ------- 步骤8：轮询邮箱拿验证码 -------
        if mail_provider is not None:
            try:
                code = mail_provider.wait_for_otp(
                    dev_token,
                    email,
                    proxy=static_proxy,
                    proxy_selector=mail_proxy_selector,
                    proxy_reporter=_report_pool_proxy_result,
                    stop_event=stop_event,
                )
            except TypeError:
                code = mail_provider.wait_for_otp(
                    dev_token,
                    email,
                    proxy=static_proxy,
                    stop_event=stop_event,
                )
        else:
            code = get_oai_code(
                dev_token,
                email,
                static_proxies,
                emitter,
                stop_event,
                proxy_selector=mail_proxies_selector,
            )
        if not code:
            return None

        if _stopped():
            return None

        # ------- 步骤9：提交验证码 -------
        emitter.info("正在验证 OTP...", step="verify_otp")
        code_body = f'{{"code":"{code}"}}'
        code_resp = _session_post(
            "https://auth.openai.com/api/accounts/email-otp/validate",
            headers=_build_openai_headers(
                "https://auth.openai.com/email-verification",
                did,
            ),
            data=code_body,
        )
        emitter.info(f"验证码校验状态: {code_resp.status_code}", step="verify_otp")
        if code_resp.status_code != 200:
            emitter.error(
                f"验证码校验失败（状态码 {code_resp.status_code}）: {str(code_resp.text or '')[:220]}",
                step="verify_otp",
            )
            return None

        if _stopped():
            return None

        # ------- 步骤10：创建账户 -------
        emitter.info("正在创建账户信息...", step="create_account")
        create_account_body = json.dumps(_random_account_profile())
        create_account_resp = _session_post(
            "https://auth.openai.com/api/accounts/create_account",
            headers=_build_openai_headers(
                "https://auth.openai.com/about-you",
                did,
            ),
            data=create_account_body,
        )
        create_account_status = create_account_resp.status_code
        emitter.info(f"账户创建状态: {create_account_status}", step="create_account")

        if create_account_status != 200:
            emitter.error(create_account_resp.text, step="create_account")
            return None

        emitter.success("账户创建成功！", step="create_account")

        if _stopped():
            return None

        # ------- 步骤10：解析 Workspace -------
        emitter.info("正在解析 Workspace 信息...", step="workspace")
        auth_cookie = s.cookies.get("oai-client-auth-session") or relay_cookie_jar.get("oai-client-auth-session") or ""
        if not auth_cookie:
            emitter.error("未能获取到授权 Cookie", step="workspace")
            return None

        auth_json = _decode_jwt_segment(auth_cookie.split(".")[0])
        workspaces = auth_json.get("workspaces") or []
        if not workspaces:
            emitter.error("授权 Cookie 里没有 workspace 信息", step="workspace")
            return None
        workspace_id = str((workspaces[0] or {}).get("id") or "").strip()
        if not workspace_id:
            emitter.error("无法解析 workspace_id", step="workspace")
            return None

        select_body = f'{{"workspace_id":"{workspace_id}"}}'
        select_resp = _session_post(
            "https://auth.openai.com/api/accounts/workspace/select",
            headers={
                "referer": "https://auth.openai.com/sign-in-with-chatgpt/codex/consent",
                "content-type": "application/json",
            },
            data=select_body,
        )

        if select_resp.status_code != 200:
            emitter.error(f"选择 workspace 失败，状态码: {select_resp.status_code}", step="workspace")
            emitter.error(select_resp.text, step="workspace")
            return None

        emitter.success(f"Workspace 选择成功: {workspace_id}", step="workspace")

        # ------- 步骤11：跟踪重定向，获取 Token -------
        emitter.info("正在获取最终 OAuth Token...", step="get_token")
        continue_url = str((select_resp.json() or {}).get("continue_url") or "").strip()
        if not continue_url:
            emitter.error("workspace/select 响应里缺少 continue_url", step="get_token")
            return None

        current_url = continue_url
        for _ in range(6):
            if _stopped():
                return None
            final_resp = _session_get(current_url, allow_redirects=False, timeout=15)
            location = final_resp.headers.get("Location") or ""

            if final_resp.status_code not in [301, 302, 303, 307, 308]:
                break
            if not location:
                break

            next_url = urllib.parse.urljoin(current_url, location)
            if "code=" in next_url and "state=" in next_url:
                if pool_relay_enabled and not _should_bypass_relay_for_target(TOKEN_URL):
                    try:
                        result = _submit_callback_url_via_pool_relay(
                            callback_url=next_url,
                            code_verifier=oauth.code_verifier,
                            redirect_uri=oauth.redirect_uri,
                            expected_state=oauth.state,
                        )
                    except Exception as exc:
                        _warn_relay_fallback(str(exc), TOKEN_URL)
                        result = submit_callback_url(
                            callback_url=next_url,
                            code_verifier=oauth.code_verifier,
                            redirect_uri=oauth.redirect_uri,
                            expected_state=oauth.state,
                            proxy=static_proxy,
                            stop_event=stop_event,
                        )
                else:
                    result = submit_callback_url(
                        callback_url=next_url,
                        code_verifier=oauth.code_verifier,
                        redirect_uri=oauth.redirect_uri,
                        expected_state=oauth.state,
                        proxy=(static_proxy if pool_relay_enabled else _next_proxy_value()),
                        stop_event=stop_event,
                    )
                validation_proxy = static_proxy if static_proxy else ""
                if not validation_proxy and not pool_relay_enabled:
                    validation_proxy = _next_proxy_value()
                if not _validate_registration_token(
                    result,
                    proxy=validation_proxy,
                    emitter=emitter,
                ):
                    try:
                        s.close()
                    except Exception:
                        pass
                    return None
                emitter.success("Token 获取成功！", step="get_token")
                try:
                    s.close()
                except Exception:
                    pass
                return result
            current_url = next_url

        emitter.error("未能在重定向链中捕获到最终 Callback URL", step="get_token")
        try:
            s.close()
        except Exception:
            pass
        return None

    except Exception as e:
        emitter.error(f"运行时发生错误: {e}", step="runtime")
        try:
            s.close()
        except Exception:
            pass
        return None

# ==========================================
# CLI 入口（兼容直接运行）
# ==========================================


def main() -> None:
    parser = argparse.ArgumentParser(description="OpenAI 账号池编排器脚本")
    parser.add_argument(
        "--proxy", default=None, help="代理地址，如 http://127.0.0.1:7890"
    )
    parser.add_argument("--once", action="store_true", help="只运行一次")
    parser.add_argument("--sleep-min", type=int, default=5, help="循环模式最短等待秒数")
    parser.add_argument(
        "--sleep-max", type=int, default=30, help="循环模式最长等待秒数"
    )
    args = parser.parse_args()

    # Detect target platform from environment variable (set by main.py)
    target = os.getenv('DS_REGISTER_TARGET', 'openai').lower()

    sleep_min = max(1, args.sleep_min)
    sleep_max = max(sleep_min, args.sleep_max)

    count = 0

    if target == 'deepseek':
        logger.info("DeepSeek 账号注册 - CLI 模式")
    else:
        logger.info("OpenAI 账号池编排器 - CLI 模式")

    while True:
        count += 1

        if target == 'deepseek':
            logger.info("开始第 {} 次 DeepSeek 注册流程", count)
        else:
            logger.info("开始第 {} 次注册流程", count)

        try:
            if target == 'deepseek':
                # DeepSeek registration
                from .deepseek_register import run_deepseek

                # Load ds2api config from sync_config.json if available
                ds2api_config = None
                try:
                    config_file = Path(__file__).parent.parent / "data" / "sync_config.json"
                    if config_file.exists():
                        with open(config_file, "r", encoding="utf-8") as f:
                            cfg = json.load(f)
                            ds2api_enabled = bool(cfg.get("deepseek_ds2api_enabled", False))
                            ds2api_url = str(cfg.get("deepseek_ds2api_url", "") or "").strip()
                            ds2api_admin_key = str(cfg.get("deepseek_ds2api_admin_key", "") or "").strip()

                            if ds2api_enabled and ds2api_url and ds2api_admin_key:
                                ds2api_config = {
                                    "enabled": True,
                                    "url": ds2api_url,
                                    "admin_key": ds2api_admin_key,
                                }
                                logger.info("ds2api 上传已启用: {}", ds2api_url)
                except Exception as e:
                    logger.warning("读取 ds2api 配置失败: {}", e)

                result = run_deepseek(
                    proxy=args.proxy,
                    emitter=_cli_emitter,
                    ds2api_config=ds2api_config,
                )

                if result:
                    logger.success(
                        "DeepSeek 注册成功 - 邮箱: {}, 密码: {}, Token: {}..., 上传: {}",
                        result.get("email"),
                        result.get("password"),
                        result.get("token", "")[:10],
                        "是" if result.get("uploaded") else "否",
                    )
                else:
                    logger.warning("本次 DeepSeek 注册失败。")
            else:
                # OpenAI registration
                token_json = run(args.proxy)

                if token_json:
                    try:
                        t_data = json.loads(token_json)
                        fname_email = t_data.get("email", "unknown").replace("@", "_")
                    except Exception:
                        fname_email = "unknown"
                        t_data = {}

                    file_name = f"token_{fname_email}_{time.time_ns()}.json"
                    save_local_token_text(token_json, filename=file_name)

                    logger.success("Token 已保存至本地 SQLite 池: {}", file_name)
                else:
                    logger.warning("本次注册失败。")

        except Exception:
            logger.exception("CLI 运行时发生未捕获异常")

        if args.once:
            break

        wait_time = random.randint(sleep_min, sleep_max)
        logger.info("休息 {} 秒...", wait_time)
        time.sleep(wait_time)


if __name__ == "__main__":
    main()

