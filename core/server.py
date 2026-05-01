"""
FastAPI 后端服务
提供 REST API + SSE 实时日志推送
"""

import asyncio
import copy
import json
import re
import os
import queue
import random
import signal
import threading
import tempfile
import time
import urllib.request
import urllib.error
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from contextlib import asynccontextmanager
from datetime import datetime
from pathlib import Path
from typing import Any, AsyncGenerator, Dict, List, Optional

from fastapi import FastAPI, HTTPException, Request
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, StreamingResponse
from fastapi.staticfiles import StaticFiles
import httpx
from pydantic import BaseModel, Field

from . import __version__, CONFIG_FILE, STATE_FILE, STATIC_DIR, LOGS_DIR
from .local_tokens import (
    UPLOAD_PLATFORMS,
    delete_local_token,
    get_local_token_record,
    get_local_token_records_by_filenames,
    import_local_token_payload,
    list_local_token_filenames,
    load_local_token_identity_keys,
    load_local_token_records,
    mark_token_uploaded_platform,
    read_local_token_inventory,
    save_local_token_text,
    set_token_uploaded_platform,
    sub2api_identity_keys,
)
from .logger import get_logger, set_runtime_logging_config, setup_logger
from .register import EventEmitter, run, _fetch_proxy_from_pool
from .mail_providers import MultiMailRouter
from .pool_maintainer import Sub2ApiMaintainer

logger = get_logger(__name__)

# ==========================================
# 同步配置（内存持久化到 data/sync_config.json）
# ==========================================

# CONFIG_FILE 已从包 __init__.py 导入


_config_lock = threading.RLock()
_service_shutdown_event = threading.Event()
_service_shutdown_lock = threading.Lock()
_sub2api_accounts_cache_lock = threading.Lock()
_sub2api_accounts_cache: Dict[str, Any] = {
    "signature": "",
    "ts": 0.0,
    "inventory": None,
}
_worker_signal_handlers_lock = threading.Lock()
_worker_signal_handlers_installed = False
_worker_previous_signal_handlers: Dict[int, Any] = {}
_worker_shutdown_dispatch_lock = threading.Lock()
_worker_shutdown_dispatch_started = False
_RUNTIME_RESTART_REQUIRED_FIELDS = (
    "service_name",
    "process_name",
    "listen_host",
    "listen_port",
    "reload_enabled",
)
_RUNTIME_IMMEDIATE_FIELDS = (
    "debug_logging",
    "anonymous_mode",
    "log_dir",
    "log_level",
    "file_log_level",
    "log_rotation",
    "log_retention_days",
)

SUB2API_MAINTAIN_ACTION_DEFAULTS: Dict[str, bool] = {
    "refresh_abnormal_accounts": True,
    "delete_abnormal_accounts": True,
    "dedupe_duplicate_accounts": True,
}
DEFAULT_SUB2API_GROUP_IDS: List[int] = [2, 4]
SUB2API_IMPORT_SNAPSHOT_TIMEOUT_SECONDS = 20
SUB2API_RUNTIME_GAP_RECHECK_SECONDS = 10
SUB2API_BATCH_SYNC_WORKERS = 6
DEFAULT_PROXY_POOL_API_URL = "https://raw.githubusercontent.com/proxifly/free-proxy-list/refs/heads/main/proxies/countries/US/data.txt"
TASK_STOP_WAIT_TIMEOUT_SECONDS = 15.0


def _worker_shutdown_signal_values() -> List[int]:
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    return signals


def _begin_service_shutdown() -> bool:
    with _service_shutdown_lock:
        already_requested = _service_shutdown_event.is_set()
        _service_shutdown_event.set()
    if already_requested:
        return False

    try:
        _state.broadcast({
            "level": "info",
            "message": "收到服务关闭请求，正在停止任务与后台维护线程...",
            "step": "shutdown",
        })
    except Exception:
        pass

    try:
        _state.stop_task()
    except Exception:
        pass

    try:
        _stop_sub2api_auto_maintain()
    except Exception:
        pass
    return True


def _dispatch_service_shutdown_from_signal() -> None:
    global _worker_shutdown_dispatch_started
    with _worker_shutdown_dispatch_lock:
        if _worker_shutdown_dispatch_started:
            return
        _worker_shutdown_dispatch_started = True

    def _run() -> None:
        try:
            _begin_service_shutdown()
        finally:
            global _worker_shutdown_dispatch_started
            with _worker_shutdown_dispatch_lock:
                _worker_shutdown_dispatch_started = False

    thread = threading.Thread(target=_run, name="service-shutdown-signal", daemon=True)
    thread.start()


def _worker_signal_handler(signum: int, frame: Any) -> None:
    _dispatch_service_shutdown_from_signal()
    previous = _worker_previous_signal_handlers.get(int(signum))
    if callable(previous) and previous is not _worker_signal_handler:
        previous(signum, frame)


def _install_worker_signal_handlers() -> None:
    global _worker_signal_handlers_installed
    if threading.current_thread() is not threading.main_thread():
        return

    with _worker_signal_handlers_lock:
        if _worker_signal_handlers_installed:
            return
        for sig in _worker_shutdown_signal_values():
            try:
                _worker_previous_signal_handlers[int(sig)] = signal.getsignal(sig)
                signal.signal(sig, _worker_signal_handler)
            except (OSError, ValueError):
                continue
        _worker_signal_handlers_installed = True


def _restore_worker_signal_handlers() -> None:
    global _worker_signal_handlers_installed, _worker_shutdown_dispatch_started
    if threading.current_thread() is not threading.main_thread():
        return

    with _worker_signal_handlers_lock:
        if not _worker_signal_handlers_installed:
            return
        previous_handlers = dict(_worker_previous_signal_handlers)
        _worker_previous_signal_handlers.clear()
        _worker_signal_handlers_installed = False

    for sig, handler in previous_handlers.items():
        try:
            signal.signal(sig, handler)
        except (OSError, ValueError):
            continue

    with _worker_shutdown_dispatch_lock:
        _worker_shutdown_dispatch_started = False


def _parse_cloudflare_trace(text: str) -> Dict[str, Any]:
    trace_text = str(text or "")
    loc_m = re.search(r"^loc=(.+)$", trace_text, re.MULTILINE)
    loc = loc_m.group(1) if loc_m else "?"
    supported = loc not in ("CN", "HK")
    return {
        "loc": loc,
        "supported": supported,
        "error": None if supported else f"所在地不支持: {loc}",
    }


def _request_cloudflare_trace_via_proxy(proxy: str, timeout: int = 8) -> str:
    from curl_cffi import requests as cffi_req

    proxies = {"http": proxy, "https": proxy} if proxy else None
    try:
        resp = cffi_req.get(
            "https://cloudflare.com/cdn-cgi/trace",
            proxies=proxies,
            http_version="v2",
            impersonate="chrome",
            timeout=timeout,
        )
    except Exception as exc:
        if "HTTP/3 is not supported over an HTTP proxy" not in str(exc):
            raise
        resp = cffi_req.get(
            "https://cloudflare.com/cdn-cgi/trace",
            proxies=proxies,
            http_version="v1",
            impersonate="chrome",
            timeout=timeout,
        )
    return str(resp.text or "")


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in ("1", "true", "yes", "on"):
        return True
    if text in ("0", "false", "no", "off", ""):
        return False
    return default


def _normalize_sub2api_maintain_actions(raw: Any) -> Dict[str, bool]:
    source = raw if isinstance(raw, dict) else {}
    return {
        key: _as_bool(source.get(key, default), default=default)
        for key, default in SUB2API_MAINTAIN_ACTION_DEFAULTS.items()
    }


def _normalize_sub2api_group_ids(
    raw: Any,
    *,
    default_if_missing: Optional[List[int]] = None,
) -> List[int]:
    if raw is None:
        source_items = list(default_if_missing or [])
    elif isinstance(raw, list):
        source_items = list(raw)
    elif isinstance(raw, tuple):
        source_items = list(raw)
    elif isinstance(raw, str):
        source_items = [item for item in re.split(r"[\s,，]+", raw.strip()) if item]
    else:
        source_items = [raw]

    result: List[int] = []
    seen: set[int] = set()
    for item in source_items:
        try:
            value = int(item)
        except (TypeError, ValueError):
            continue
        if value <= 0 or value in seen:
            continue
        seen.add(value)
        result.append(value)
    return result


def _get_sub2api_group_ids(cfg: Optional[Dict[str, Any]] = None) -> List[int]:
    config = cfg if cfg is not None else _get_sync_config()
    default_ids = DEFAULT_SUB2API_GROUP_IDS if "sub2api_group_ids" not in config else []
    return _normalize_sub2api_group_ids(
        config.get("sub2api_group_ids"),
        default_if_missing=default_ids,
    )


def _get_sub2api_maintain_actions(cfg: Optional[Dict[str, Any]] = None) -> Dict[str, bool]:
    config = cfg if cfg is not None else _get_sync_config()
    return _normalize_sub2api_maintain_actions(config.get("sub2api_maintain_actions"))


def _describe_sub2api_maintain_actions(actions: Optional[Dict[str, bool]] = None) -> str:
    normalized = _normalize_sub2api_maintain_actions(actions)
    labels: List[str] = []
    if normalized["refresh_abnormal_accounts"]:
        labels.append("异常测活")
    if normalized["delete_abnormal_accounts"]:
        labels.append("异常清理")
    if normalized["dedupe_duplicate_accounts"]:
        labels.append("重复清理")
    return "、".join(labels) if labels else "无动作"


def _format_sub2api_maintain_result_message(result: Dict[str, Any], *, auto: bool = False) -> str:
    prefix = "自动维护" if auto else "维护完成"
    actions_text = _describe_sub2api_maintain_actions(result.get("actions"))
    return (
        f"[Sub2Api] {prefix}({actions_text}): 异常 {result.get('error_count', 0)}, "
        f"恢复 {result.get('refreshed', 0)}, "
        f"判死 {result.get('probe_deleted', 0) + result.get('probe_token_invalid', 0)}, "
        f"待人工 {result.get('probe_indeterminate', 0)}, "
        f"重复组 {result.get('duplicate_groups', 0)}, "
        f"删除 {result.get('deleted_ok', 0)}(失败 {result.get('deleted_fail', 0)}), "
        f"耗时 {round((result.get('duration_ms', 0) or 0) / 1000, 2)}s"
    )


def _format_sub2api_pool_status_summary(status: Optional[Dict[str, Any]]) -> str:
    if not isinstance(status, dict):
        return "池状态未知"
    threshold = int(status.get("threshold", 0) or 0)
    candidates = int(status.get("candidates", 0) or 0)
    error_count = int(status.get("error_count", 0) or 0)
    gap = max(0, threshold - candidates) if threshold > 0 else 0
    if status.get("error"):
        return f"池状态异常: {status.get('error')}"
    return f"正常 {candidates}/{threshold}，异常 {error_count}，缺口 {gap}"


def _clear_sub2api_accounts_cache() -> None:
    with _sub2api_accounts_cache_lock:
        _sub2api_accounts_cache["signature"] = ""
        _sub2api_accounts_cache["ts"] = 0.0
        _sub2api_accounts_cache["inventory"] = None


def _build_sub2api_accounts_cache_signature(cfg: Optional[Dict[str, Any]] = None) -> str:
    config = cfg or _get_sync_config()
    signature_payload = {
        "base_url": str(config.get("base_url", "") or "").strip(),
        "email": str(config.get("email", "") or "").strip().lower(),
        "sub2api_min_candidates": int(config.get("sub2api_min_candidates", 200) or 200),
    }
    return json.dumps(signature_payload, ensure_ascii=False, sort_keys=True)


def _get_sub2api_accounts_inventory_snapshot(
    sm: Sub2ApiMaintainer,
    cfg: Optional[Dict[str, Any]] = None,
    *,
    timeout: int = 15,
    ttl_seconds: int = 12,
    page: int = 1,
    page_size: int = 20,
    status: str = "all",
    keyword: str = "",
) -> Dict[str, Any]:
    if _should_use_live_sub2api_account_page(status=status, keyword=keyword):
        return _build_live_sub2api_accounts_snapshot(
            sm,
            page=page,
            page_size=page_size,
            status=status,
            keyword=keyword,
            timeout=timeout,
        )

    signature = _build_sub2api_accounts_cache_signature(cfg)
    now = time.time()
    with _sub2api_accounts_cache_lock:
        cached_signature = str(_sub2api_accounts_cache.get("signature") or "")
        cached_ts = float(_sub2api_accounts_cache.get("ts") or 0.0)
        cached_inventory = _sub2api_accounts_cache.get("inventory")
        if (
            cached_signature == signature
            and isinstance(cached_inventory, dict)
            and (now - cached_ts) <= ttl_seconds
        ):
            return copy.deepcopy(cached_inventory)

    inventory = sm.list_account_inventory(timeout=timeout)
    with _sub2api_accounts_cache_lock:
        _sub2api_accounts_cache["signature"] = signature
        _sub2api_accounts_cache["ts"] = now
        _sub2api_accounts_cache["inventory"] = copy.deepcopy(inventory)
    return inventory


def _get_sub2api_accounts_cached_snapshot(
    cfg: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    signature = _build_sub2api_accounts_cache_signature(cfg)
    with _sub2api_accounts_cache_lock:
        cached_signature = str(_sub2api_accounts_cache.get("signature") or "")
        cached_inventory = _sub2api_accounts_cache.get("inventory")
        if cached_signature == signature and isinstance(cached_inventory, dict):
            return copy.deepcopy(cached_inventory)
    return None


def _build_sub2api_accounts_error_snapshot(
    error_message: str,
    *,
    cached_snapshot: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    snapshot = dict(cached_snapshot or {})
    snapshot.setdefault("total", 0)
    snapshot.setdefault("error_count", 0)
    snapshot.setdefault("duplicate_groups", 0)
    snapshot.setdefault("duplicate_accounts", 0)
    snapshot.setdefault("items", [])
    snapshot["error"] = str(error_message or "").strip() or "Sub2Api 账号列表加载失败"
    snapshot["stale"] = bool(cached_snapshot)
    return snapshot


def _filter_sub2api_account_items(items: List[Dict[str, Any]], status: str = "all", keyword: str = "") -> List[Dict[str, Any]]:
    normalized_status = str(status or "all").strip().lower() or "all"
    keyword_norm = str(keyword or "").strip().lower()
    abnormal_statuses = {"error", "disabled"}
    filtered: List[Dict[str, Any]] = []

    for item in items:
        item_status = str(item.get("status") or "").strip().lower()
        is_abnormal = item_status in abnormal_statuses
        is_duplicate = bool(item.get("is_duplicate"))

        if normalized_status == "normal" and is_abnormal:
            continue
        if normalized_status == "abnormal" and not is_abnormal:
            continue
        if normalized_status == "error" and item_status != "error":
            continue
        if normalized_status == "disabled" and item_status != "disabled":
            continue
        if normalized_status == "duplicate" and not is_duplicate:
            continue

        if keyword_norm:
            email = str(item.get("email") or "").lower()
            name = str(item.get("name") or "").lower()
            account_id = str(item.get("id") or "").lower()
            if keyword_norm not in email and keyword_norm not in name and keyword_norm not in account_id:
                continue

        filtered.append(item)

    return filtered


def _paginate_sub2api_account_items(
    items: List[Dict[str, Any]], page: int = 1, page_size: int = 20,
) -> Dict[str, Any]:
    safe_page_size = max(10, min(int(page_size or 20), 100))
    total = len(items)
    total_pages = max(1, (total + safe_page_size - 1) // safe_page_size)
    safe_page = max(1, min(int(page or 1), total_pages))
    start = (safe_page - 1) * safe_page_size
    end = start + safe_page_size
    return {
        "items": items[start:end],
        "page": safe_page,
        "page_size": safe_page_size,
        "filtered_total": total,
        "total_pages": total_pages,
    }


def _should_use_live_sub2api_account_page(status: str = "all", keyword: str = "") -> bool:
    normalized_status = str(status or "all").strip().lower() or "all"
    if str(keyword or "").strip():
        return False
    return normalized_status in ("all", "normal", "error", "disabled")


def _write_json_atomic(path: Path, payload: Dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp_path = tempfile.mkstemp(prefix=f".{path.stem}_", suffix=path.suffix, dir=str(path.parent))
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(tmp_path, path)
    finally:
        try:
            if os.path.exists(tmp_path):
                os.remove(tmp_path)
        except OSError:
            pass


def _load_sync_config() -> Dict[str, Any]:
    if CONFIG_FILE.exists():
        try:
            return json.loads(CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {
        "base_url": "", "bearer_token": "", "account_name": "AutoReg", "auto_sync": False,
        "service_name": "OpenAI Pool Orchestrator",
        "process_name": "openai-pool",
        "listen_host": "0.0.0.0",
        "listen_port": 18421,
        "reload_enabled": False,
        "debug_logging": False,
        "anonymous_mode": False,
        "log_dir": str(LOGS_DIR),
        "log_level": "INFO",
        "file_log_level": "DEBUG",
        "log_rotation": "1 day",
        "log_retention_days": 7,
        "mail_providers": ["mailtm"],
        "mail_provider_configs": {"mailtm": {"api_base": "https://api.mail.tm"}},
        "mail_strategy": "round_robin",
        "sub2api_min_candidates": 200,
        "sub2api_group_ids": copy.deepcopy(DEFAULT_SUB2API_GROUP_IDS),
        "sub2api_auto_maintain": False,
        "sub2api_maintain_interval_minutes": 30,
        "sub2api_maintain_actions": copy.deepcopy(SUB2API_MAINTAIN_ACTION_DEFAULTS),
        "proxy": "",
        "auto_register": False,
        "proxy_pool_enabled": False,
        "proxy_pool_api_url": DEFAULT_PROXY_POOL_API_URL,
        "proxy_pool_auth_mode": "query",
        "proxy_pool_api_key": "",
        "proxy_pool_count": 1,
        "proxy_pool_country": "US",
        "proxy_pool_fetch_retries": 3,
        "proxy_pool_bad_ttl_seconds": 180,
        "proxy_pool_tcp_check_enabled": True,
        "proxy_pool_tcp_check_timeout_seconds": 1.2,
        "proxy_pool_prefer_stable_proxy": True,
        "proxy_pool_stable_proxy": "",
    }


def _normalize_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    """标准化当前配置结构并执行基础类型校验。"""
    source = copy.deepcopy(cfg or {})
    raw_providers = source.get("mail_providers")
    providers = raw_providers if isinstance(raw_providers, list) else []
    providers = [str(n).strip().lower() for n in providers if str(n).strip()]
    if not providers:
        providers = ["mailtm"]

    raw_cfgs = source.get("mail_provider_configs")
    provider_cfgs = raw_cfgs if isinstance(raw_cfgs, dict) else {}
    for name in providers:
        if name not in provider_cfgs or not isinstance(provider_cfgs.get(name), dict):
            provider_cfgs[name] = {}

    strategy = str(source.get("mail_strategy", "round_robin") or "round_robin").strip().lower()
    if strategy not in ("round_robin", "random", "failover"):
        strategy = "round_robin"
    log_level = str(source.get("log_level", "INFO") or "INFO").strip().upper() or "INFO"
    if log_level not in {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}:
        log_level = "INFO"
    file_log_level = str(source.get("file_log_level", "DEBUG") or "DEBUG").strip().upper() or "DEBUG"
    if file_log_level not in {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}:
        file_log_level = "DEBUG"
    log_dir = str(source.get("log_dir", str(LOGS_DIR)) or str(LOGS_DIR)).strip() or str(LOGS_DIR)
    log_rotation = str(source.get("log_rotation", "1 day") or "1 day").strip() or "1 day"

    default_group_ids = DEFAULT_SUB2API_GROUP_IDS if "sub2api_group_ids" not in source else []
    normalized: Dict[str, Any] = {
        "base_url": str(source.get("base_url", "") or "").strip(),
        "bearer_token": str(source.get("bearer_token", "") or "").strip(),
        "email": str(source.get("email", "") or "").strip(),
        "password": str(source.get("password", "") or "").strip(),
        "account_name": str(source.get("account_name", "AutoReg") or "").strip() or "AutoReg",
        "auto_sync": _as_bool(source.get("auto_sync", False), default=False),
        "service_name": str(source.get("service_name", "OpenAI Pool Orchestrator") or "").strip() or "OpenAI Pool Orchestrator",
        "process_name": str(source.get("process_name", "openai-pool") or "").strip() or "openai-pool",
        "listen_host": str(source.get("listen_host", "0.0.0.0") or "").strip() or "0.0.0.0",
        "reload_enabled": _as_bool(source.get("reload_enabled", False), default=False),
        "debug_logging": _as_bool(source.get("debug_logging", False), default=False),
        "anonymous_mode": _as_bool(source.get("anonymous_mode", False), default=False),
        "log_dir": log_dir,
        "log_level": log_level,
        "file_log_level": file_log_level,
        "log_rotation": log_rotation,
        "mail_providers": providers,
        "mail_provider_configs": provider_cfgs,
        "mail_strategy": strategy,
        "sub2api_min_candidates": max(1, int(source.get("sub2api_min_candidates", 200) or 200)),
        "sub2api_group_ids": _normalize_sub2api_group_ids(
            source.get("sub2api_group_ids"),
            default_if_missing=default_group_ids,
        ),
        "sub2api_auto_maintain": _as_bool(source.get("sub2api_auto_maintain", False), default=False),
        "sub2api_maintain_interval_minutes": max(5, int(source.get("sub2api_maintain_interval_minutes", 30) or 30)),
        "sub2api_maintain_actions": _normalize_sub2api_maintain_actions(source.get("sub2api_maintain_actions")),
        "proxy": str(source.get("proxy", "") or "").strip(),
        "auto_register": _as_bool(source.get("auto_register", False), default=False),
        "multithread": _as_bool(source.get("multithread", False), default=False),
        "proxy_pool_enabled": _as_bool(source.get("proxy_pool_enabled", False), default=False),
        "proxy_pool_api_url": str(source.get("proxy_pool_api_url", DEFAULT_PROXY_POOL_API_URL) or "").strip() or DEFAULT_PROXY_POOL_API_URL,
        "proxy_pool_api_key": str(source.get("proxy_pool_api_key", "") or "").strip(),
        "proxy_pool_country": str(source.get("proxy_pool_country", "US") or "US").strip().upper() or "US",
        "proxy_pool_tcp_check_enabled": _as_bool(
            source.get("proxy_pool_tcp_check_enabled", True),
            default=True,
        ),
        "proxy_pool_prefer_stable_proxy": _as_bool(
            source.get("proxy_pool_prefer_stable_proxy", True),
            default=True,
        ),
        "proxy_pool_stable_proxy": str(source.get("proxy_pool_stable_proxy", "") or "").strip(),
    }
    try:
        normalized["thread_count"] = max(1, min(int(source.get("thread_count", 3) or 3), 10))
    except (ValueError, TypeError):
        normalized["thread_count"] = 3
    try:
        normalized["listen_port"] = max(1, min(int(source.get("listen_port", 18421) or 18421), 65535))
    except (ValueError, TypeError):
        normalized["listen_port"] = 18421
    try:
        normalized["log_retention_days"] = max(1, min(int(source.get("log_retention_days", 7) or 7), 3650))
    except (TypeError, ValueError):
        normalized["log_retention_days"] = 7
    proxy_pool_auth_mode = str(source.get("proxy_pool_auth_mode", "query") or "").strip().lower()
    if proxy_pool_auth_mode not in ("header", "query"):
        proxy_pool_auth_mode = "query"
    normalized["proxy_pool_auth_mode"] = proxy_pool_auth_mode
    try:
        normalized["proxy_pool_count"] = max(1, min(int(source.get("proxy_pool_count", 1) or 1), 20))
    except (TypeError, ValueError):
        normalized["proxy_pool_count"] = 1
    try:
        normalized["proxy_pool_fetch_retries"] = max(
            1,
            min(int(source.get("proxy_pool_fetch_retries", 3) or 3), 10),
        )
    except (TypeError, ValueError):
        normalized["proxy_pool_fetch_retries"] = 3
    try:
        normalized["proxy_pool_bad_ttl_seconds"] = max(
            10,
            min(int(source.get("proxy_pool_bad_ttl_seconds", 180) or 180), 3600),
        )
    except (TypeError, ValueError):
        normalized["proxy_pool_bad_ttl_seconds"] = 180
    try:
        normalized["proxy_pool_tcp_check_timeout_seconds"] = max(
            0.5,
            min(float(source.get("proxy_pool_tcp_check_timeout_seconds", 1.2) or 1.2), 10.0),
        )
    except (TypeError, ValueError):
        normalized["proxy_pool_tcp_check_timeout_seconds"] = 1.2
    return normalized


def _pool_relay_url_from_fetch_url(api_url: str) -> str:
    raw = str(api_url or "").strip()
    if not raw:
        return ""
    lowered = raw.lower()
    if lowered.endswith(".txt") or "/proxies/countries/" in lowered:
        return ""
    if "://" not in raw:
        raw = "https://" + raw
    try:
        from urllib.parse import urlparse
        parsed = urlparse(raw)
        scheme = parsed.scheme or "https"
        netloc = parsed.netloc
        if not netloc:
            return ""
        return f"{scheme}://{netloc}/api/relay"
    except Exception:
        return ""


def _get_sync_config() -> Dict[str, Any]:
    with _config_lock:
        return copy.deepcopy(_sync_config)


def _set_sync_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    global _sync_config
    normalized = _normalize_config(cfg)
    with _config_lock:
        _write_json_atomic(CONFIG_FILE, normalized)
        _sync_config = normalized
        set_runtime_logging_config(
            debug_logging=normalized.get("debug_logging", False),
            anonymous_mode=normalized.get("anonymous_mode", False),
            log_level=normalized.get("log_level"),
            file_log_level=normalized.get("file_log_level"),
            log_dir=normalized.get("log_dir"),
            log_rotation=normalized.get("log_rotation"),
            log_retention_days=normalized.get("log_retention_days"),
        )
        setup_logger(
            normalized.get("log_dir") or LOGS_DIR,
            debug_mode=bool(normalized.get("debug_logging", False)),
            log_level=normalized.get("log_level"),
            file_log_level=normalized.get("file_log_level"),
            log_rotation=normalized.get("log_rotation", "1 day"),
            log_retention_days=int(normalized.get("log_retention_days", 7) or 7),
            force=True,
        )
        return copy.deepcopy(_sync_config)


def _save_sync_config(cfg: Dict[str, Any]) -> Dict[str, Any]:
    return _set_sync_config(cfg)


_sync_config = _normalize_config(_load_sync_config())
set_runtime_logging_config(
    debug_logging=_sync_config.get("debug_logging", False),
    anonymous_mode=_sync_config.get("anonymous_mode", False),
    log_level=_sync_config.get("log_level"),
    file_log_level=_sync_config.get("file_log_level"),
    log_dir=_sync_config.get("log_dir"),
    log_rotation=_sync_config.get("log_rotation"),
    log_retention_days=_sync_config.get("log_retention_days"),
)
setup_logger(
    _sync_config.get("log_dir") or LOGS_DIR,
    debug_mode=bool(_sync_config.get("debug_logging", False)),
    log_level=_sync_config.get("log_level"),
    file_log_level=_sync_config.get("file_log_level"),
    log_rotation=_sync_config.get("log_rotation", "1 day"),
    log_retention_days=int(_sync_config.get("log_retention_days", 7) or 7),
    force=True,
)


def _is_auto_sync_enabled(cfg: Optional[Dict[str, Any]] = None) -> bool:
    config = cfg if cfg is not None else _get_sync_config()
    return _as_bool(config.get("auto_sync", False), default=False)


def _push_refresh_token(base_url: str, bearer: str, refresh_token: str) -> Dict[str, Any]:
    """
    调用 Sub2Api 平台 API 提交单个 refresh_token。
    返回 {ok: bool, status: int, body: str}
    """
    url = base_url.rstrip("/") + "/api/v1/admin/openai/refresh-token"
    payload = json.dumps({"refresh_token": refresh_token}).encode("utf-8")
    req = urllib.request.Request(
        url,
        data=payload,
        method="POST",
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {bearer}",
            "Accept": "application/json",
        },
    )
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            body = resp.read().decode("utf-8", "replace")
            return {"ok": True, "status": resp.status, "body": body}
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", "replace")
        return {"ok": False, "status": exc.code, "body": body}
    except Exception as e:
        return {"ok": False, "status": 0, "body": str(e)}


# ==========================================
# 统计数据持久化
# ==========================================

# STATE_FILE 已从包 __init__.py 导入


def _load_state() -> Dict[str, int]:
    if STATE_FILE.exists():
        try:
            return json.loads(STATE_FILE.read_text(encoding="utf-8"))
        except Exception:
            pass
    return {"success": 0, "fail": 0}


def _save_state(success: int, fail: int) -> None:
    try:
        _write_json_atomic(STATE_FILE, {"success": success, "fail": fail})
    except Exception:
        pass


# ==========================================
# 应用初始化
# ==========================================


@asynccontextmanager
async def _lifespan(_: FastAPI):
    _service_shutdown_event.clear()
    _install_worker_signal_handlers()
    cfg = _get_sync_config()
    if cfg.get("sub2api_auto_maintain"):
        _start_sub2api_auto_maintain()
    try:
        logger.info("FastAPI 服务已启动")
        yield
    finally:
        try:
            request_service_shutdown()
        finally:
            _restore_worker_signal_handlers()
        logger.info("FastAPI 服务已关闭")


app = FastAPI(
    title="OpenAI Pool Orchestrator",
    version=__version__,
    lifespan=_lifespan,
)


def _resolve_request_id(request: Request) -> str:
    incoming = str(
        request.headers.get("x-request-id")
        or request.headers.get("x-correlation-id")
        or ""
    ).strip()
    if incoming:
        return incoming[:64]
    return uuid.uuid4().hex[:12]


def _resolve_client_ip(request: Request) -> str:
    forwarded_for = str(request.headers.get("x-forwarded-for") or "").strip()
    if forwarded_for:
        return forwarded_for.split(",")[0].strip()
    if request.client and request.client.host:
        return str(request.client.host)
    return ""


@app.middleware("http")
async def request_trace_middleware(request: Request, call_next):
    request_id = _resolve_request_id(request)
    client_ip = _resolve_client_ip(request)
    request.state.request_id = request_id
    request_logger = logger.bind(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        client_ip=client_ip,
    )
    started_at = time.perf_counter()

    with logger.contextualize(
        request_id=request_id,
        method=request.method,
        path=request.url.path,
        client_ip=client_ip,
    ):
        request_logger.debug("HTTP 请求开始")
        try:
            response = await call_next(request)
        except Exception:
            duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
            request_logger.exception("HTTP 请求异常结束，耗时 {}ms", duration_ms)
            raise

    duration_ms = round((time.perf_counter() - started_at) * 1000, 2)
    response.headers["X-Request-ID"] = request_id
    if response.status_code >= 500:
        request_logger.error("HTTP 请求完成: status={}, duration_ms={}", response.status_code, duration_ms)
    elif response.status_code >= 400:
        request_logger.warning("HTTP 请求完成: status={}, duration_ms={}", response.status_code, duration_ms)
    else:
        request_logger.debug("HTTP 请求完成: status={}, duration_ms={}", response.status_code, duration_ms)
    return response

# STATIC_DIR 已从包 __init__.py 导入
STATIC_DIR.mkdir(exist_ok=True)

# ==========================================
# 任务状态管理
# ==========================================


class TaskState:
    """全局任务状态，支持多 Worker 运行快照与结构化 SSE 事件。"""

    _WORKER_STEP_DEFINITIONS = {
        "check_proxy": "网络检查",
        "create_email": "创建邮箱",
        "oauth_init": "OAuth 初始化",
        "sentinel": "Sentinel Token",
        "signup": "提交注册",
        "send_otp": "发送验证码",
        "wait_otp": "等待验证码",
        "verify_otp": "验证 OTP",
        "create_account": "创建账户",
        "workspace": "选择 Workspace",
        "get_token": "获取 Token",
        "saved": "保存 Token",
        "sync": "同步 Sub2Api",
        "retry": "等待重试",
        "wait": "等待下一轮",
        "dedupe": "重复检测",
        "runtime": "运行异常",
        "auto_stop": "自动停止",
        "stopping": "停止中",
        "stopped": "已停止",
        "mode": "上传策略",
        "shutdown": "服务关闭",
    }
    _REGISTRATION_STEPS = frozenset({
        "check_proxy", "create_email", "oauth_init", "sentinel",
        "signup", "send_otp", "wait_otp", "verify_otp",
        "create_account", "workspace", "get_token",
    })

    def __init__(self) -> None:
        self.status: str = "stopped"
        self.stop_event = threading.Event()
        self.thread: Optional[threading.Thread] = None
        self._worker_threads: Dict[int, threading.Thread] = {}
        self._task_lock = threading.RLock()
        self._sse_queues: list[tuple[asyncio.AbstractEventLoop, asyncio.Queue]] = []
        self._sse_lock = threading.Lock()

        _s = _load_state()
        self.success_count: int = int(_s.get("success", 0) or 0)
        self.fail_count: int = int(_s.get("fail", 0) or 0)
        self.current_proxy: str = ""
        self.worker_count: int = 0
        self.upload_mode: str = "sub2api"
        self.target_count: int = 0
        self._target_slots_reserved: int = 0
        self.run_success_count: int = 0
        self.run_fail_count: int = 0
        self.platform_success_count: Dict[str, int] = {name: 0 for name in UPLOAD_PLATFORMS}
        self.platform_fail_count: Dict[str, int] = {name: 0 for name in UPLOAD_PLATFORMS}
        self.platform_backlog_count: Dict[str, int] = {name: 0 for name in UPLOAD_PLATFORMS}
        self._upload_queues: Dict[str, queue.Queue] = {}

        self.run_id: Optional[str] = None
        self.revision: int = 0
        self.created_at: Optional[str] = None
        self.started_at: Optional[str] = None
        self.finished_at: Optional[str] = None
        self.stop_reason: str = ""
        self.last_error: str = ""
        self.completion_semantics: str = "registration_only"
        self._focus_worker_id: Optional[int] = None
        self._worker_runtime: Dict[int, Dict[str, Any]] = {}
        self._last_task_event_state: Optional[Dict[str, Any]] = None
        self._last_stats_event_state: Optional[Dict[str, Any]] = None

    def _now_iso(self) -> str:
        return datetime.now().isoformat(timespec="seconds")

    def _new_run_id(self) -> str:
        return uuid.uuid4().hex[:12]

    def _next_revision_locked(self) -> int:
        self.revision += 1
        return self.revision

    def _has_live_run_locked(self) -> bool:
        if self.status in {"starting", "running", "stopping"}:
            return True
        if self.thread and self.thread.is_alive():
            return True
        return any(thread.is_alive() for thread in self._worker_threads.values())

    def can_start_new_task(self) -> bool:
        with self._task_lock:
            return not self._has_live_run_locked()

    def has_live_run(self) -> bool:
        with self._task_lock:
            return self._has_live_run_locked()

    def _task_progress_locked(self) -> Dict[str, Any]:
        target = max(0, int(self.target_count or 0))
        reserved = max(int(self._target_slots_reserved or 0), int(self.run_success_count or 0))
        completed = max(0, int(self.run_success_count or 0))
        in_flight = max(0, reserved - completed)
        remaining = max(0, target - completed) if target > 0 else 0
        startable_remaining = max(0, target - reserved) if target > 0 else 0
        percent = 0.0
        if target > 0:
            percent = round(min(100.0, (completed / target) * 100), 1)
        return {
            "enabled": target > 0,
            "current": completed,
            "target": target,
            "reserved": reserved,
            "in_flight": in_flight,
            "remaining": remaining,
            "startable_remaining": startable_remaining,
            "percent": percent,
        }

    def _emit_task_snapshot_locked(self, *, bump_revision: bool = False) -> None:
        self._emit_task_updated_locked(bump_revision=bump_revision, force=True)
        snapshot = self._status_snapshot_locked()
        self._remember_stats_event_state_locked(snapshot["stats"])
        self._emit_event_locked("snapshot", {"snapshot": snapshot})
    def _signal_stop_locked(self, reason: str, *, transition_status: Optional[str] = None) -> bool:
        already_stopping = self.stop_event.is_set()
        if reason and not self.stop_reason:
            self.stop_reason = reason
        if transition_status:
            self.status = transition_status
        if already_stopping:
            return False
        self.stop_event.set()
        self._emit_task_snapshot_locked(bump_revision=True)
        return True

    def request_stop(
        self,
        reason: str,
        *,
        message: str = "",
        step: str = "stopping",
        level: str = "info",
        transition_status: Optional[str] = None,
    ) -> bool:
        with self._task_lock:
            changed = self._signal_stop_locked(reason, transition_status=transition_status)
        if changed and message:
            self.broadcast({
                "ts": datetime.now().strftime("%H:%M:%S"),
                "level": level,
                "message": message,
                "step": step,
            })
        return changed

    def wait_for_idle(self, timeout: Optional[float] = None) -> bool:
        deadline = None if timeout is None else time.monotonic() + max(0.0, float(timeout))
        while True:
            with self._task_lock:
                if not self._has_live_run_locked():
                    return True
                monitor_thread = self.thread
                worker_threads = list(self._worker_threads.values())

            join_target = monitor_thread if monitor_thread and monitor_thread.is_alive() else None
            if join_target is None:
                join_target = next((thread for thread in worker_threads if thread.is_alive()), None)
            if join_target is None:
                with self._task_lock:
                    return not self._has_live_run_locked()

            if deadline is None:
                join_timeout = 0.2
            else:
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    return False
                join_timeout = min(0.2, remaining)
            join_target.join(timeout=join_timeout)

    def _completion_semantics_locked(self) -> str:
        return "requires_postprocess" if _is_auto_sync_enabled() else "registration_only"

    def _empty_worker_runtime_locked(self, worker_id: int, worker_label: Optional[str] = None) -> Dict[str, Any]:
        return {
            "worker_id": worker_id,
            "worker_label": worker_label or f"W{worker_id}",
            "status": "starting",
            "phase": "prepare",
            "attempt": 0,
            "mail_provider": "",
            "account_email": "",
            "current_step": "",
            "message": "",
            "updated_at": self._now_iso(),
            "steps": [],
        }

    def _empty_runtime_snapshot_locked(self) -> Dict[str, Any]:
        workers = [
            copy.deepcopy(runtime)
            for _, runtime in sorted(self._worker_runtime.items(), key=lambda item: item[0])
        ]
        return {
            "run_id": self.run_id,
            "revision": self.revision,
            "completion_semantics": self.completion_semantics,
            "focus_worker_id": self._focus_worker_id,
            "aggregate": self._aggregate_runtime_locked(),
            "workers": workers,
        }

    def _task_snapshot_locked(self) -> Dict[str, Any]:
        return {
            "run_id": self.run_id,
            "revision": self.revision,
            "status": self.status,
            "worker_count": self.worker_count,
            "upload_mode": self.upload_mode,
            "completion_semantics": self.completion_semantics,
            "target_count": self.target_count,
            "progress": self._task_progress_locked(),
            "created_at": self.created_at,
            "started_at": self.started_at,
            "finished_at": self.finished_at,
            "stop_reason": self.stop_reason,
            "last_error": self.last_error,
            "proxy": self.current_proxy,
        }

    def _stats_snapshot_locked(self) -> Dict[str, Any]:
        platform = {}
        for name in UPLOAD_PLATFORMS:
            success = int(self.platform_success_count.get(name, 0) or 0)
            fail = int(self.platform_fail_count.get(name, 0) or 0)
            backlog = int(self.platform_backlog_count.get(name, 0) or 0)
            platform[name] = {
                "success": success,
                "fail": fail,
                "backlog": backlog,
                "total": success + fail,
            }
        return {
            "lifetime": {
                "success": self.success_count,
                "fail": self.fail_count,
                "total": self.success_count + self.fail_count,
            },
            "run": {
                "success": self.run_success_count,
                "fail": self.run_fail_count,
                "total": self.run_success_count + self.run_fail_count,
            },
            "platform": platform,
            "success": self.success_count,
            "fail": self.fail_count,
            "total": self.success_count + self.fail_count,
        }

    @staticmethod
    def _task_event_state(snapshot: Dict[str, Any]) -> Dict[str, Any]:
        state = copy.deepcopy(snapshot)
        state.pop("revision", None)
        return state

    def _remember_task_event_state_locked(self, snapshot: Dict[str, Any]) -> None:
        self._last_task_event_state = self._task_event_state(snapshot)

    def _remember_stats_event_state_locked(self, snapshot: Dict[str, Any]) -> None:
        self._last_stats_event_state = copy.deepcopy(snapshot)

    def _emit_task_updated_locked(self, *, bump_revision: bool = False, force: bool = False) -> bool:
        snapshot = self._task_snapshot_locked()
        event_state = self._task_event_state(snapshot)
        if not force and self._last_task_event_state == event_state:
            return False
        self._emit_event_locked(
            "task.updated",
            {"task": snapshot},
            bump_revision=bump_revision,
        )
        self._last_task_event_state = event_state
        return True

    def _emit_stats_updated_locked(self, *, force: bool = False) -> bool:
        snapshot = self._stats_snapshot_locked()
        if not force and self._last_stats_event_state == snapshot:
            return False
        self._emit_event_locked("stats.updated", {"stats": snapshot})
        self._last_stats_event_state = copy.deepcopy(snapshot)
        return True
    def _status_snapshot_locked(self) -> Dict[str, Any]:
        return {
            "task": self._task_snapshot_locked(),
            "runtime": self._empty_runtime_snapshot_locked(),
            "stats": self._stats_snapshot_locked(),
            "server_time": self._now_iso(),
        }

    def get_status_snapshot(self) -> Dict[str, Any]:
        with self._task_lock:
            return self._status_snapshot_locked()

    def subscribe(self) -> asyncio.Queue:
        loop = asyncio.get_running_loop()
        q: asyncio.Queue = asyncio.Queue(maxsize=500)
        with self._sse_lock:
            self._sse_queues.append((loop, q))
        return q

    def unsubscribe(self, q: asyncio.Queue) -> None:
        with self._sse_lock:
            self._sse_queues = [(loop, queue_obj) for loop, queue_obj in self._sse_queues if queue_obj is not q]

    def _enqueue_sse_payload(self, payload: Dict[str, Any]) -> None:
        with self._sse_lock:
            subscribers = list(self._sse_queues)
        for loop, q in subscribers:
            def _enqueue(target_q: asyncio.Queue = q, data: Dict[str, Any] = payload) -> None:
                try:
                    target_q.put_nowait(copy.deepcopy(data))
                except asyncio.QueueFull:
                    pass
            try:
                loop.call_soon_threadsafe(_enqueue)
            except RuntimeError:
                continue

    def _emit_event_locked(self, event_type: str, payload: Optional[Dict[str, Any]] = None, *, bump_revision: bool = False) -> Dict[str, Any]:
        if bump_revision:
            self._next_revision_locked()
        event_payload: Dict[str, Any] = {
            "type": event_type,
            "run_id": self.run_id,
            "revision": self.revision,
        }
        if payload:
            event_payload.update(payload)
        self._enqueue_sse_payload(event_payload)
        return event_payload

    def _sync_status_from_workers_locked(self) -> None:
        if self.status in {"stopping", "stopped", "finished"}:
            return
        workers = list(self._worker_runtime.values())
        if not workers:
            return
        statuses = {str(worker.get("status") or "") for worker in workers}
        if any(status == "failed" for status in statuses):
            self.status = "failed"
            return
        if statuses and statuses.issubset({"succeeded", "stopped"}):
            self.status = "finished"
            return
        self.status = "running"

    def _finalize_worker_runtimes_locked(self, final_status: str) -> None:
        status = str(final_status or "").strip().lower()
        if status != "stopped":
            return
        updated_at = self._now_iso()
        for runtime in self._worker_runtime.values():
            runtime["status"] = "stopped"
            runtime["phase"] = "finish"
            runtime["current_step"] = "stopped"
            runtime["message"] = "任务已停止"
            runtime["updated_at"] = updated_at
            self._upsert_worker_step_locked(
                runtime,
                step_id="stopped",
                level="info",
                message="任务已停止",
                updated_at=updated_at,
            )

    def _worker_status_from_step(self, step: str, level: str) -> str:
        s = str(step or "").strip().lower()
        lv = str(level or "").strip().lower()
        if s in {"stopping"}:
            return "stopping"
        if s in {"stopped", "auto_stop"}:
            return "stopped"
        if s in {"retry", "wait"}:
            return "waiting"
        if s == "runtime" or lv == "error":
            return "failed"
        if s in {"sync", "saved"}:
            return "postprocessing"
        if s in {"start", "dedupe", "mode"}:
            return "preparing"
        if s in self._REGISTRATION_STEPS:
            if s == "get_token" and lv == "success":
                return "succeeded" if self.completion_semantics == "registration_only" else "postprocessing"
            return "registering"
        return "running" if self.status in {"running", "starting"} else self.status

    def _worker_phase_from_step(self, step: str) -> str:
        s = str(step or "").strip().lower()
        if s in {"start", "dedupe", "mode"}:
            return "prepare"
        if s in self._REGISTRATION_STEPS:
            return "register"
        if s in {"saved", "sync", "retry", "wait"}:
            return "postprocess"
        if s in {"stopping", "stopped", "auto_stop", "shutdown"}:
            return "finish"
        return "prepare"

    def _extract_email_from_event(self, event: Dict[str, Any]) -> str:
        direct_email = str(event.get("account_email") or "").strip()
        if direct_email:
            return direct_email
        message = str(event.get("message") or "")
        match = re.search(r"([A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,})", message)
        return match.group(1) if match else ""

    def _upsert_worker_step_locked(self, runtime: Dict[str, Any], *, step_id: str, level: str, message: str, updated_at: str) -> Dict[str, Any]:
        label = self._WORKER_STEP_DEFINITIONS.get(step_id, step_id or "运行步骤")
        raw_status = str(level or "info").strip().lower()
        if raw_status == "success":
            status = "done"
        elif raw_status == "error":
            status = "error"
        elif step_id in {"wait", "retry"}:
            status = "active"
        else:
            status = "active"
        steps: List[Dict[str, Any]] = runtime.setdefault("steps", [])
        current = None
        for item in steps:
            if item.get("step_id") == step_id:
                current = item
                break
        if current is None:
            current = {
                "step_id": step_id,
                "id": step_id,
                "label": label,
                "status": status,
                "message": message,
                "started_at": updated_at,
                "finished_at": updated_at if status in {"done", "error", "skipped"} else None,
                "updated_at": updated_at,
            }
            steps.append(current)
        else:
            current["label"] = label
            current["status"] = status
            current["message"] = message
            current["updated_at"] = updated_at
            current.setdefault("started_at", updated_at)
            if status in {"done", "error", "skipped"}:
                current["finished_at"] = updated_at
            else:
                current["finished_at"] = None
        return copy.deepcopy(current)

    def _update_runtime_from_event_locked(self, event: Dict[str, Any]) -> Optional[Dict[str, Any]]:
        raw_worker_id = event.get("worker_id")
        try:
            worker_id = int(raw_worker_id)
        except (TypeError, ValueError):
            return None

        runtime = self._worker_runtime.get(worker_id)
        if runtime is None:
            runtime = self._empty_worker_runtime_locked(worker_id, str(event.get("worker_label") or f"W{worker_id}"))
            self._worker_runtime[worker_id] = runtime

        updated_at = str(event.get("iso_ts") or event.get("updated_at") or self._now_iso())
        runtime["updated_at"] = updated_at
        runtime["worker_label"] = str(event.get("worker_label") or runtime.get("worker_label") or f"W{worker_id}")

        attempt = event.get("attempt")
        if attempt not in (None, ""):
            try:
                runtime["attempt"] = int(attempt)
            except (TypeError, ValueError):
                pass

        mail_provider = str(event.get("mail_provider") or "").strip()
        if mail_provider:
            runtime["mail_provider"] = mail_provider

        email = self._extract_email_from_event(event)
        if email:
            runtime["account_email"] = email
            runtime["email"] = email

        message = str(event.get("message") or "").strip()
        if message:
            runtime["message"] = message

        step = str(event.get("step") or "").strip().lower()
        level = str(event.get("level") or "info").strip().lower()
        step_patch = None
        if step:
            runtime["current_step"] = step
            runtime["phase"] = self._worker_phase_from_step(step)
            runtime["status"] = self._worker_status_from_step(step, level)
            step_patch = self._upsert_worker_step_locked(runtime, step_id=step, level=level, message=message, updated_at=updated_at)
            if step == "start":
                runtime["steps"] = [step_patch]
            if step in {"stopped", "auto_stop"}:
                runtime["status"] = "stopped"
            elif step == "runtime" or (level == "error" and step not in {"retry", "wait"}):
                runtime["status"] = "failed"
        else:
            runtime["status"] = "running" if self.status not in {"stopping", "stopped"} else self.status

        if self._focus_worker_id is None or self._focus_worker_id == worker_id:
            self._focus_worker_id = worker_id
        elif runtime["status"] in {"registering", "postprocessing", "failed", "waiting"}:
            self._focus_worker_id = worker_id

        self._sync_status_from_workers_locked()
        return {
            "worker": copy.deepcopy(runtime),
            "step": step_patch,
        }

    def _aggregate_runtime_locked(self) -> Dict[str, Any]:
        agg: Dict[str, Any] = {
            "total": 0,
            "starting": 0,
            "preparing": 0,
            "registering": 0,
            "postprocessing": 0,
            "waiting": 0,
            "stopping": 0,
            "stopped": 0,
            "failed": 0,
            "succeeded": 0,
            "last_updated_at": None,
        }
        for runtime in self._worker_runtime.values():
            agg["total"] += 1
            status = str(runtime.get("status") or "").strip().lower()
            if status in agg:
                agg[status] += 1
            updated_at = runtime.get("updated_at")
            if updated_at and (agg["last_updated_at"] is None or str(updated_at) > str(agg["last_updated_at"])):
                agg["last_updated_at"] = updated_at
        return agg

    def broadcast(self, event: Dict[str, Any]) -> None:
        with self._task_lock:
            payload = dict(event)
            payload.setdefault("ts", datetime.now().strftime("%H:%M:%S"))
            payload.setdefault("iso_ts", self._now_iso())
            event_type = str(payload.get("type") or "").strip()
            if event_type:
                self._emit_event_locked(event_type, payload, bump_revision=event_type != "heartbeat")
                return

            runtime_patch = self._update_runtime_from_event_locked(payload)
            self._emit_event_locked(
                "log.appended",
                {
                    "log": {
                        "ts": payload.get("ts", ""),
                        "level": payload.get("level", "info"),
                        "message": payload.get("message", ""),
                        "step": payload.get("step", ""),
                        "worker_id": payload.get("worker_id"),
                        "worker_label": payload.get("worker_label"),
                    }
                },
                bump_revision=True,
            )
            if runtime_patch:
                if runtime_patch.get("step"):
                    self._emit_event_locked(
                        "worker.step.updated",
                        {
                            "worker_id": runtime_patch["worker"].get("worker_id"),
                            "worker": runtime_patch["worker"],
                            "step": runtime_patch["step"],
                            "focus_worker_id": self._focus_worker_id,
                        },
                    )
                else:
                    self._emit_event_locked("worker.updated", {"worker": runtime_patch["worker"]})
                self._emit_task_updated_locked()
                self._emit_stats_updated_locked()
    def _make_emitter(self, *, run_id: Optional[str] = None) -> EventEmitter:
        thread_q: queue.Queue = queue.Queue(maxsize=500)

        def _bridge() -> None:
            while True:
                try:
                    event = thread_q.get(timeout=0.2)
                    if event is None:
                        break
                    try:
                        self.broadcast(event)
                    except Exception as exc:
                        logger.opt(exception=exc).warning("日志桥接事件已丢弃")
                except queue.Empty:
                    if self.stop_event.is_set() and thread_q.empty():
                        break

        bridge_thread = threading.Thread(target=_bridge, daemon=True)
        bridge_thread.start()
        self._bridge_thread = bridge_thread
        self._bridge_q = thread_q
        defaults = {"run_id": run_id} if run_id else {}
        return EventEmitter(q=thread_q, cli_mode=True, defaults=defaults)

    def _stop_bridge(self) -> None:
        if hasattr(self, "_bridge_q"):
            try:
                self._bridge_q.put_nowait(None)
            except queue.Full:
                pass

    def start_task(
        self,
        proxy: str,
        worker_count: int = 1,
        target_count: int = 0,
        sub2api_target_count: Optional[int] = None,
    ) -> None:
        sub2api_target = None if sub2api_target_count is None else max(0, int(sub2api_target_count))
        config_snapshot = _get_sync_config()
        try:
            mail_router = MultiMailRouter(config_snapshot)
        except Exception as exc:
            raise RuntimeError(str(exc)) from exc
        auto_sync_enabled = _is_auto_sync_enabled(config_snapshot)

        with self._task_lock:
            if self._has_live_run_locked():
                raise RuntimeError("任务正在运行或停止中")
            n = max(1, min(int(worker_count or 1), 10))
            now = self._now_iso()
            self.run_id = self._new_run_id()
            self.revision = 0
            self.status = "starting"
            self.stop_event.clear()
            self.current_proxy = proxy
            self.worker_count = n
            self.upload_mode = "sub2api"
            self.target_count = max(0, target_count)
            self._target_slots_reserved = 0
            self.run_success_count = 0
            self.run_fail_count = 0
            self.platform_success_count = {name: 0 for name in UPLOAD_PLATFORMS}
            self.platform_fail_count = {name: 0 for name in UPLOAD_PLATFORMS}
            self.platform_backlog_count = {name: 0 for name in UPLOAD_PLATFORMS}
            self._upload_queues = {}
            self._worker_threads = {}
            self._worker_runtime = {
                wid: self._empty_worker_runtime_locked(wid)
                for wid in range(1, n + 1)
            }
            self._focus_worker_id = 1 if n > 0 else None
            self.created_at = now
            self.started_at = now
            self.finished_at = None
            self.stop_reason = ""
            self.last_error = ""
            self.completion_semantics = "requires_postprocess" if auto_sync_enabled else "registration_only"
            self._last_task_event_state = None
            self._last_stats_event_state = None
            self._emit_task_snapshot_locked(bump_revision=True)

        emitter = self._make_emitter(run_id=self.run_id)
        logger.bind(run_id=self.run_id).info(
            "任务启动: worker_count={}, target_count={}, auto_sync={}, proxy_pool_enabled={}",
            n,
            self.target_count,
            auto_sync_enabled,
            bool(config_snapshot.get("proxy_pool_enabled", False)),
        )
        upload_remaining: Dict[str, Optional[int]] = {"sub2api": sub2api_target}
        seen_runtime_identities: set[str] = load_local_token_identity_keys()
        seen_runtime_identities_lock = threading.RLock()
        sub2api_sync_maintainer = (
            _get_sub2api_maintainer(config_snapshot)
            if auto_sync_enabled and sub2api_target is not None
            else None
        )
        sub2api_gap_lock = threading.Lock()
        sub2api_gap_state: Dict[str, Any] = {
            "last_checked": 0.0,
            "last_gap": sub2api_target,
            "stop_logged": False,
            "last_error": "",
        }

        def _apply_sub2api_gap_budget(gap: int) -> None:
            normalized_gap = max(0, int(gap or 0))
            with self._task_lock:
                remain = upload_remaining.get("sub2api")
                if remain is not None:
                    upload_remaining["sub2api"] = min(remain, normalized_gap)

        def _finish_due_to_sub2api_gap(em: "EventEmitter", prefix: str) -> None:
            should_log = False
            with sub2api_gap_lock:
                if not sub2api_gap_state.get("stop_logged"):
                    sub2api_gap_state["stop_logged"] = True
                    should_log = True
            if should_log:
                em.success(
                    f"{prefix}检测到 Sub2Api 缺口已补满，停止后续上传",
                    step="auto_stop",
                )
            with self._task_lock:
                if not self.stop_reason:
                    self.stop_reason = "sub2api_gap_satisfied"
            self.stop_event.set()

        def _refresh_sub2api_gap_budget(
            em: "EventEmitter",
            prefix: str,
            *,
            force: bool = False,
        ) -> Optional[int]:
            if not auto_sync_enabled or sub2api_target is None or sub2api_sync_maintainer is None:
                return None

            now_ts = time.time()
            with sub2api_gap_lock:
                cached_gap = sub2api_gap_state.get("last_gap")
                last_checked = float(sub2api_gap_state.get("last_checked") or 0.0)
                cached_error = str(sub2api_gap_state.get("last_error") or "")
            if (
                not force
                and isinstance(cached_gap, int)
                and (now_ts - last_checked) < SUB2API_RUNTIME_GAP_RECHECK_SECONDS
            ):
                _apply_sub2api_gap_budget(cached_gap)
                if cached_gap <= 0:
                    _finish_due_to_sub2api_gap(em, prefix)
                return cached_gap

            try:
                latest_gap = max(0, int(sub2api_sync_maintainer.calculate_gap()))
            except Exception as exc:
                error_text = str(exc)
                should_warn = error_text != cached_error
                with sub2api_gap_lock:
                    sub2api_gap_state["last_error"] = error_text
                if should_warn:
                    em.warn(
                        f"{prefix}实时复核 Sub2Api 缺口失败，沿用当前预算: {error_text}",
                        step="sync",
                    )
                return None

            with sub2api_gap_lock:
                sub2api_gap_state["last_checked"] = now_ts
                sub2api_gap_state["last_gap"] = latest_gap
                sub2api_gap_state["last_error"] = ""
            _apply_sub2api_gap_budget(latest_gap)
            if latest_gap <= 0:
                _finish_due_to_sub2api_gap(em, prefix)
            return latest_gap

        def _reserve_upload_slot(platform: str) -> bool:
            with self._task_lock:
                remain = upload_remaining.get(platform)
                if remain is None:
                    return True
                if remain <= 0:
                    return False
                upload_remaining[platform] = remain - 1
                return True

        def _release_upload_slot(platform: str) -> None:
            with self._task_lock:
                remain = upload_remaining.get(platform)
                if remain is not None:
                    upload_remaining[platform] = remain + 1

        def _wait_for_upload_slot(
            platform: str,
            em: Optional["EventEmitter"] = None,
            prefix: str = "",
        ) -> bool:
            while not self.stop_event.is_set():
                if platform == "sub2api" and em is not None:
                    _refresh_sub2api_gap_budget(em, prefix)
                    if self.stop_event.is_set():
                        return False
                if _reserve_upload_slot(platform):
                    return True
                self.stop_event.wait(0.2)
            return False

        def _record_platform_result(platform: str, ok: bool) -> None:
            if platform not in UPLOAD_PLATFORMS:
                return
            with self._task_lock:
                if ok:
                    self.platform_success_count[platform] = self.platform_success_count.get(platform, 0) + 1
                else:
                    self.platform_fail_count[platform] = self.platform_fail_count.get(platform, 0) + 1

        def _reserve_target_slot() -> bool:
            with self._task_lock:
                if self.target_count <= 0:
                    return True
                if self._target_slots_reserved >= self.target_count:
                    return False
                self._target_slots_reserved += 1
                return True

        def _release_target_slot() -> None:
            with self._task_lock:
                floor = max(0, int(self.run_success_count or 0))
                if self.target_count > 0 and self._target_slots_reserved > floor:
                    self._target_slots_reserved -= 1

        def _wait_for_target_slot() -> bool:
            while not self.stop_event.is_set():
                if _reserve_target_slot():
                    return True
                self.stop_event.wait(0.2)
            return False

        def _register_runtime_identity(email: str, refresh_token: str) -> bool:
            keys = sub2api_identity_keys(email=email, refresh_token=refresh_token)
            if not keys:
                return True
            with seen_runtime_identities_lock:
                for key in keys:
                    if key in seen_runtime_identities:
                        return False
                seen_runtime_identities.update(keys)
            return True

        def _apply_final_result(email: str, prefix: str, ok: bool) -> None:
            if ok:
                with self._task_lock:
                    self.success_count += 1
                    self.run_success_count += 1
                    _save_state(self.success_count, self.fail_count)
                    should_stop = self.target_count > 0 and self.run_success_count >= self.target_count
                if should_stop:
                    self.request_stop(
                        "target_reached",
                        message=f"{prefix}本轮已达目标 {self.target_count} 个，自动停止",
                        step="auto_stop",
                        level="success",
                    )
            else:
                with self._task_lock:
                    self.fail_count += 1
                    self.run_fail_count += 1
                    _save_state(self.success_count, self.fail_count)
                emitter.error(f"{prefix}平台上传未完成，本次不计入成功: {email}", step="retry")

        def _auto_sync(file_name: str, email: str, em: "EventEmitter") -> bool:
            cfg = config_snapshot
            if not _is_auto_sync_enabled(cfg):
                return True
            base_url = cfg.get("base_url", "").strip()
            bearer = cfg.get("bearer_token", "").strip()
            group_ids = _get_sub2api_group_ids(cfg)
            if not base_url or not bearer:
                em.error("自动同步配置缺少平台地址或 Token，请先保存配置", step="sync")
                return False

            em.info(f"正在自动同步 {email}...", step="sync")
            token_record = get_local_token_record(file_name, include_content=True)
            token_data = token_record.get("content") if isinstance(token_record, dict) else None
            if not isinstance(token_data, dict):
                em.error("自动同步异常: 读取本地 Token 失败: 记录不存在或内容无效", step="sync")
                return False

            last_status = 0
            last_body = ""
            for attempt in range(3):
                try:
                    result = _push_account_api_with_dedupe(
                        base_url=base_url,
                        bearer=bearer,
                        email=email,
                        token_data=token_data,
                        group_ids=group_ids,
                        check_before=(attempt == 0),
                        check_after=True,
                    )
                    last_status = int(result.get("status") or 0)
                    last_body = str(result.get("body") or "")
                    if result.get("ok"):
                        if not mark_token_uploaded_platform(file_name, "sub2api"):
                            em.warn(f"自动同步成功但本地标记失败: {email}", step="sync")
                        _clear_sub2api_accounts_cache()
                        reason = str(result.get("reason") or "")
                        if reason == "updated_existing_before_create":
                            em.success(
                                f"自动同步命中已存在账号并更新凭据: {email} (id={result.get('existing_id', '-')})",
                                step="sync",
                            )
                        elif reason == "exists_before_create_update_failed":
                            em.warn(
                                f"自动同步命中已存在账号但更新失败，保持远端现状: {email} "
                                f"(id={result.get('existing_id', '-')}, status={result.get('update_status', '-')}) "
                                f"{str(result.get('update_body') or '')[:120]}",
                                step="sync",
                            )
                        elif result.get("skipped"):
                            em.success(f"自动同步成功: {email}", step="sync")
                        else:
                            em.success(f"自动同步成功: {email}", step="sync")
                        return True
                except Exception as e:
                    last_status = 0
                    last_body = str(e)
                if attempt < 2:
                    time.sleep(2 ** attempt)

            em.error(f"自动同步失败({last_status}): {last_body[:120]}", step="sync")
            return False

        def _upload_to_sub2api(file_name: str, email: str, refresh_token: str, prefix: str) -> bool:
            if not auto_sync_enabled:
                return True
            if not refresh_token:
                emitter.error(f"{prefix}缺少 refresh_token，无法自动同步: {email}", step="sync")
                return False
            return _auto_sync(file_name, email, emitter)

        def _worker_loop(worker_id: int) -> None:
            worker_label = f"W{worker_id}"
            prefix = f"[{worker_label}] " if n > 1 else ""
            worker_emitter = emitter.bind(worker_id=worker_id, worker_label=worker_label)
            count = 0
            while not self.stop_event.is_set():
                if not _wait_for_target_slot():
                    break
                slot_should_release = True
                count += 1
                provider_name, provider = mail_router.next_provider()
                attempt_emitter = worker_emitter.bind(mail_provider=provider_name)
                attempt_emitter.info(
                    f"{prefix}>>> 第 {count} 次注册 (邮箱: {provider_name}) <<<",
                    step="start",
                    attempt=count,
                )
                try:
                    token_json = run(
                        proxy=proxy or None,
                        emitter=attempt_emitter,
                        stop_event=self.stop_event,
                        mail_provider=provider,
                        proxy_pool_config={
                            "enabled": bool(config_snapshot.get("proxy_pool_enabled", False)),
                            "api_url": str(config_snapshot.get("proxy_pool_api_url", "")).strip(),
                            "auth_mode": str(config_snapshot.get("proxy_pool_auth_mode", "query")).strip().lower(),
                            "api_key": str(config_snapshot.get("proxy_pool_api_key", "")).strip(),
                            "count": config_snapshot.get("proxy_pool_count", 1),
                            "country": str(config_snapshot.get("proxy_pool_country", "US") or "US").strip().upper(),
                            "fetch_retries": config_snapshot.get("proxy_pool_fetch_retries", 3),
                            "bad_ttl_seconds": config_snapshot.get("proxy_pool_bad_ttl_seconds", 180),
                            "tcp_check_enabled": bool(config_snapshot.get("proxy_pool_tcp_check_enabled", True)),
                            "tcp_check_timeout_seconds": config_snapshot.get("proxy_pool_tcp_check_timeout_seconds", 1.2),
                            "prefer_stable_proxy": bool(config_snapshot.get("proxy_pool_prefer_stable_proxy", True)),
                            "stable_proxy": str(config_snapshot.get("proxy_pool_stable_proxy", "") or "").strip(),
                        },
                    )

                    if self.stop_event.is_set() and not token_json:
                        break

                    if token_json:
                        mail_router.report_success(provider_name)
                        try:
                            t_data = json.loads(token_json)
                            fname_email = t_data.get("email", "unknown").replace("@", "_")
                            refresh_token = str(t_data.get("refresh_token", "") or "").strip()
                            email = str(t_data.get("email", "unknown") or "unknown").strip()
                        except Exception:
                            fname_email = "unknown"
                            refresh_token = ""
                            email = "unknown"

                        if not _register_runtime_identity(email, refresh_token):
                            attempt_emitter.warn(
                                f"{prefix}检测到重复账号（同邮箱/refresh_token），已跳过: {email}",
                                step="dedupe",
                                account_email=email,
                            )
                            continue

                        file_name = f"token_{fname_email}_{time.time_ns()}.json"
                        save_local_token_text(token_json, filename=file_name)

                        attempt_emitter.success(
                            f"{prefix}Token 已保存: {file_name}",
                            step="saved",
                            account_email=email,
                        )
                        self.broadcast({
                            "ts": datetime.now().strftime("%H:%M:%S"),
                            "level": "token_saved",
                            "message": file_name,
                            "step": "saved",
                            "worker_id": worker_id,
                            "worker_label": worker_label,
                            "mail_provider": provider_name,
                            "attempt": count,
                            "account_email": email,
                        })

                        if auto_sync_enabled:
                            if not _wait_for_upload_slot("sub2api", attempt_emitter, prefix):
                                continue

                            sub2api_ok = _upload_to_sub2api(file_name, email, refresh_token, prefix)
                            _record_platform_result("sub2api", sub2api_ok)
                            if not sub2api_ok:
                                _release_upload_slot("sub2api")
                            _apply_final_result(email, prefix, sub2api_ok)
                            slot_should_release = not sub2api_ok
                        else:
                            _apply_final_result(email, prefix, True)
                            slot_should_release = False
                    else:
                        mail_router.report_failure(provider_name)
                        with self._task_lock:
                            self.fail_count += 1
                            self.run_fail_count += 1
                            self.last_error = f"注册失败: worker={worker_id}"
                            _save_state(self.success_count, self.fail_count)
                            self.status = "running"
                        attempt_emitter.error(f"{prefix}本次注册失败，稍后重试...", step="retry")

                except Exception as e:
                    mail_router.report_failure(provider_name)
                    with self._task_lock:
                        self.fail_count += 1
                        self.run_fail_count += 1
                        self.last_error = str(e)
                        _save_state(self.success_count, self.fail_count)
                    attempt_emitter.error(f"{prefix}发生未捕获异常: {e}", step="runtime")
                finally:
                    if slot_should_release:
                        _release_target_slot()

                if self.stop_event.is_set():
                    break

                wait = random.randint(5, 30)
                attempt_emitter.info(f"{prefix}休息 {wait} 秒后继续...", step="wait")
                self.stop_event.wait(wait)

        def _monitor() -> None:
            with self._task_lock:
                workers = list(self._worker_threads.values())
            for t in workers:
                t.join()

            emitter.info("所有Worker已停止", step="stopped")
            self._stop_bridge()
            with self._task_lock:
                self._worker_threads.clear()
                self.worker_count = 0
                self._target_slots_reserved = self.run_success_count
                self._upload_queues = {}
                self.platform_backlog_count = {name: 0 for name in UPLOAD_PLATFORMS}
                self.finished_at = self._now_iso()
                if self.status == "stopping":
                    self.status = "stopped"
                    self.stop_reason = self.stop_reason or "manual_stop"
                elif self.status == "failed":
                    pass
                elif self.run_fail_count > 0 and self.run_success_count == 0:
                    self.status = "failed"
                    self.stop_reason = self.stop_reason or "run_failed"
                else:
                    self.status = "finished"
                if self.status == "stopped":
                    self._finalize_worker_runtimes_locked("stopped")
                self._sync_status_from_workers_locked()
                self._emit_event_locked("task.finished", {"task": self._task_snapshot_locked()}, bump_revision=True)
                self._emit_event_locked("snapshot", {"snapshot": self._status_snapshot_locked()})

        for wid in range(1, n + 1):
            t = threading.Thread(target=_worker_loop, args=(wid,), daemon=True)
            with self._task_lock:
                self._worker_threads[wid] = t
            t.start()

        with self._task_lock:
            self.status = "running"
            self._emit_task_snapshot_locked(bump_revision=True)

        self.thread = threading.Thread(target=_monitor, daemon=True)
        self.thread.start()

    def stop_task(self) -> None:
        if self.status not in {"starting", "running", "failed"}:
            return
        self.request_stop(
            "manual_stop",
            message="收到停止请求，等待当前注册流程收尾...",
            step="stopping",
            level="info",
            transition_status="stopping",
        )


_state = TaskState()


def request_service_shutdown(*, wait_for_idle: bool = True) -> None:
    """供外部启动器调用，通知服务进入收尾停止流程。"""
    _begin_service_shutdown()

    if not wait_for_idle:
        return

    try:
        stopped = _state.wait_for_idle(timeout=TASK_STOP_WAIT_TIMEOUT_SECONDS)
        if not stopped:
            logger.warning(
                "服务关闭等待注册任务超时，仍有线程未完成收尾 (timeout={}s)",
                TASK_STOP_WAIT_TIMEOUT_SECONDS,
            )
    except Exception:
        pass


def _get_sub2api_maintainer(cfg: Optional[Dict[str, Any]] = None) -> Optional[Sub2ApiMaintainer]:
    cfg = cfg or _get_sync_config()
    base_url = str(cfg.get("base_url", "")).strip()
    bearer = str(cfg.get("bearer_token", "")).strip()
    email = str(cfg.get("email", "")).strip()
    password = str(cfg.get("password", "")).strip()
    if not base_url:
        return None
    if not bearer and not (email and password):
        return None
    return Sub2ApiMaintainer(
        base_url=base_url,
        bearer_token=bearer,
        min_candidates=int(cfg.get("sub2api_min_candidates", 200)),
        email=email,
        password=password,
        openai_proxy=str(cfg.get("proxy", "") or "").strip(),
        group_ids=_get_sub2api_group_ids(cfg),
    )


# ==========================================
# API 路由
# ==========================================


class StartRequest(BaseModel):
    proxy: str = ""
    worker_count: int = 1


class ProxyCheckRequest(BaseModel):
    proxy: str = ""


class ProxyPoolTestRequest(BaseModel):
    enabled: bool = False
    api_url: str = DEFAULT_PROXY_POOL_API_URL
    auth_mode: str = "query"  # "header" | "query"
    api_key: str = ""
    count: int = 1
    country: str = "US"
    fetch_retries: int = 3
    bad_ttl_seconds: int = 180
    tcp_check_enabled: bool = True
    tcp_check_timeout_seconds: float = 1.2
    prefer_stable_proxy: bool = True
    stable_proxy: str = ""


class ProxyPoolConfigRequest(BaseModel):
    proxy_pool_enabled: bool = False
    proxy_pool_api_url: str = DEFAULT_PROXY_POOL_API_URL
    proxy_pool_auth_mode: str = "query"  # "header" | "query"
    proxy_pool_api_key: str = ""
    proxy_pool_count: int = 1
    proxy_pool_country: str = "US"
    proxy_pool_fetch_retries: int = 3
    proxy_pool_bad_ttl_seconds: int = 180
    proxy_pool_tcp_check_enabled: bool = True
    proxy_pool_tcp_check_timeout_seconds: float = 1.2
    proxy_pool_prefer_stable_proxy: bool = True
    proxy_pool_stable_proxy: str = ""


class ProxySaveRequest(BaseModel):
    proxy: str = ""


class RuntimeConfigRequest(BaseModel):
    service_name: str = "OpenAI Pool Orchestrator"
    process_name: str = "openai-pool"
    listen_host: str = "0.0.0.0"
    listen_port: int = 18421
    reload_enabled: bool = False
    debug_logging: bool = False
    anonymous_mode: bool = False
    log_dir: str = str(LOGS_DIR)
    log_level: str = "INFO"
    file_log_level: str = "DEBUG"
    log_rotation: str = "1 day"
    log_retention_days: int = 7


class SyncConfigRequest(BaseModel):
    base_url: str          # Sub2Api 平台地址
    bearer_token: str = ""  # 管理员 JWT（可选）
    email: str = ""        # 管理员邮箱
    password: str = ""     # 管理员密码
    account_name: str = "AutoReg"
    auto_sync: bool = True
    sub2api_min_candidates: int = 200
    sub2api_group_ids: List[int] = Field(default_factory=lambda: copy.deepcopy(DEFAULT_SUB2API_GROUP_IDS))
    sub2api_auto_maintain: bool = False
    sub2api_maintain_interval_minutes: int = 30
    sub2api_maintain_actions: Dict[str, bool] = Field(default_factory=dict)
    multithread: bool = False
    thread_count: int = 3
    auto_register: bool = False



class TokenImportRequest(BaseModel):
    payload: Any


@app.get("/", response_class=HTMLResponse)
async def index() -> HTMLResponse:
    html_path = STATIC_DIR / "index.html"
    if html_path.exists():
        return HTMLResponse(content=html_path.read_text(encoding="utf-8"))
    return HTMLResponse("<h1>前端文件未找到</h1>", status_code=404)


@app.post("/api/start")
async def api_start(req: StartRequest) -> Dict[str, Any]:
    try:
        _state.start_task(req.proxy, req.worker_count)
    except RuntimeError as e:
        raise HTTPException(status_code=409, detail=str(e))
    snapshot = _state.get_status_snapshot()
    return {
        "run_id": snapshot["task"].get("run_id"),
        "task": snapshot["task"],
        "runtime": snapshot["runtime"],
        "stats": snapshot["stats"],
        "server_time": snapshot["server_time"],
    }


@app.post("/api/stop")
async def api_stop() -> Dict[str, Any]:
    if not _state.has_live_run():
        raise HTTPException(status_code=409, detail="没有正在运行的任务")
    _state.stop_task()
    return _state.get_status_snapshot()


@app.post("/api/proxy/save")
async def api_save_proxy(req: ProxySaveRequest) -> Dict[str, str]:
    cfg = _get_sync_config()
    cfg["proxy"] = req.proxy.strip()
    _save_sync_config(cfg)
    return {"status": "saved"}


@app.get("/api/proxy")
async def api_get_proxy() -> Dict[str, Any]:
    cfg = _get_sync_config()
    return {"proxy": cfg.get("proxy", "")}


@app.get("/api/status")
async def api_status() -> Dict[str, Any]:
    return _state.get_status_snapshot()


@app.get("/api/tokens")
async def api_tokens(
    page: int = 1,
    page_size: int = 20,
    status: str = "all",
    keyword: str = "",
    include_content: bool = True,
) -> Dict[str, Any]:
    return await run_in_threadpool(
        lambda: read_local_token_inventory(
            status=status,
            keyword=keyword,
            page=page,
            page_size=page_size,
            include_content=bool(include_content),
        )
    )


@app.post("/api/tokens/import")
async def api_import_tokens(req: TokenImportRequest) -> Dict[str, Any]:
    def _import_tokens() -> Dict[str, Any]:
        try:
            return import_local_token_payload(req.payload)
        except ValueError as exc:
            raise HTTPException(status_code=400, detail=str(exc))

    return await run_in_threadpool(_import_tokens)


@app.post("/api/tokens/import-missing-from-sub2api")
async def api_import_missing_tokens_from_sub2api() -> Dict[str, Any]:
    return await run_in_threadpool(_import_missing_sub2api_accounts_to_local_tokens)


@app.post("/api/tokens/reconcile")
async def api_reconcile_tokens() -> Dict[str, Any]:
    def _reconcile() -> Dict[str, Any]:
        cfg = _get_sync_config()
        sm = _get_sub2api_maintainer(cfg)
        if not sm:
            raise HTTPException(status_code=400, detail="请先完成当前 Sub2Api 平台配置")

        local_records = load_local_token_records(include_content=False)
        remote_accounts = sm._list_all_accounts(timeout=20, page_size=100)
        remote_keys: set[str] = set()
        for item in remote_accounts:
            email, refresh_token = _sub2api_account_identity(item if isinstance(item, dict) else {})
            remote_keys.update(sub2api_identity_keys(email, refresh_token))

        matched = 0
        updated_synced = 0
        updated_unsynced = 0
        already_synced = 0
        already_unsynced = 0
        examples: List[Dict[str, Any]] = []

        for item in local_records:
            email = str(item.get("email") or "").strip()
            refresh_token = str(item.get("refresh_token") or "").strip()
            filename = str(item.get("filename") or "")
            local_keys = sub2api_identity_keys(email, refresh_token)
            remote_exists = any(key in remote_keys for key in local_keys)
            currently_synced = "sub2api" in set(item.get("uploaded_platforms") or [])

            if remote_exists:
                matched += 1

            if remote_exists and currently_synced:
                already_synced += 1
            elif remote_exists and not currently_synced:
                if filename and set_token_uploaded_platform(filename, "sub2api", True):
                    updated_synced += 1
                if len(examples) < 20:
                    examples.append({
                        "filename": filename,
                        "email": email,
                        "action": "marked_synced",
                    })
            elif not remote_exists and currently_synced:
                if filename and set_token_uploaded_platform(filename, "sub2api", False):
                    updated_unsynced += 1
                if len(examples) < 20:
                    examples.append({
                        "filename": filename,
                        "email": email,
                        "action": "cleared_synced",
                    })
            else:
                already_unsynced += 1

        return {
            "local_total": len(local_records),
            "remote_total": len(remote_accounts),
            "matched": matched,
            "missing_remote": max(0, len(local_records) - matched),
            "updated_synced": updated_synced,
            "updated_unsynced": updated_unsynced,
            "already_synced": already_synced,
            "already_unsynced": already_unsynced,
            "examples": examples,
        }

    return await run_in_threadpool(_reconcile)


@app.delete("/api/tokens/{filename}")
async def api_delete_token(filename: str) -> Dict[str, str]:
    # 安全过滤：防止路径穿越
    if "/" in filename or "\\" in filename or ".." in filename:
        raise HTTPException(status_code=400, detail="非法文件名")
    if not delete_local_token(filename):
        raise HTTPException(status_code=404, detail="文件不存在")
    return {"status": "deleted"}


@app.get("/api/sync-config")
async def api_get_sync_config() -> Dict[str, Any]:
    """获取当前同步配置（脱敏）"""
    cfg = _get_sync_config()
    password = str(cfg.get("password", "") or "")
    cfg["password_preview"] = "********" if password else ""
    cfg["password"] = ""  # 不回传密码
    token = cfg.get("bearer_token", "")
    cfg["bearer_token_preview"] = token[:12] + "..." if len(token) > 12 else (token or "")
    cfg["bearer_token"] = ""  # 不回传完整 token
    proxy_pool_api_key = str(cfg.get("proxy_pool_api_key", ""))
    cfg["proxy_pool_api_key_configured"] = bool(proxy_pool_api_key)
    cfg["proxy_pool_api_key_preview"] = ""
    cfg["proxy_pool_api_key"] = ""
    # 脱敏 mail_provider_configs
    raw_configs = cfg.get("mail_provider_configs") or {}
    safe_configs: Dict[str, Dict] = {}
    for pname, pcfg in raw_configs.items():
        if not isinstance(pcfg, dict):
            continue
        sc = dict(pcfg)
        for secret_key in ("bearer_token", "api_key", "admin_password"):
            val = str(sc.get(secret_key, ""))
            if val:
                sc[f"{secret_key}_preview"] = (val[:8] + "...") if len(val) > 8 else val
                sc.pop(secret_key, None)
        safe_configs[pname] = sc
    cfg["mail_provider_configs"] = safe_configs
    cfg["sub2api_group_ids"] = _get_sub2api_group_ids(cfg)
    cfg["auto_sync"] = _is_auto_sync_enabled(cfg)
    return cfg


@app.get("/api/runtime-config")
async def api_get_runtime_config() -> Dict[str, Any]:
    cfg = _get_sync_config()
    return {
        "service_name": str(cfg.get("service_name", "OpenAI Pool Orchestrator") or "OpenAI Pool Orchestrator"),
        "process_name": str(cfg.get("process_name", "openai-pool") or "openai-pool"),
        "listen_host": str(cfg.get("listen_host", "0.0.0.0") or "0.0.0.0"),
        "listen_port": int(cfg.get("listen_port", 18421) or 18421),
        "reload_enabled": bool(cfg.get("reload_enabled", False)),
        "debug_logging": bool(cfg.get("debug_logging", False)),
        "anonymous_mode": bool(cfg.get("anonymous_mode", False)),
        "log_dir": str(cfg.get("log_dir", str(LOGS_DIR)) or str(LOGS_DIR)),
        "log_level": str(cfg.get("log_level", "INFO") or "INFO").upper(),
        "file_log_level": str(cfg.get("file_log_level", "DEBUG") or "DEBUG").upper(),
        "log_rotation": str(cfg.get("log_rotation", "1 day") or "1 day"),
        "log_retention_days": int(cfg.get("log_retention_days", 7) or 7),
        "restart_required_fields": list(_RUNTIME_RESTART_REQUIRED_FIELDS),
        "immediate_apply_fields": list(_RUNTIME_IMMEDIATE_FIELDS),
    }


@app.get("/api/proxy-pool/config")
async def api_get_proxy_pool_config() -> Dict[str, Any]:
    cfg = _get_sync_config()
    api_url = str(cfg.get("proxy_pool_api_url", DEFAULT_PROXY_POOL_API_URL) or "").strip()
    if not api_url:
        api_url = DEFAULT_PROXY_POOL_API_URL
    auth_mode = str(cfg.get("proxy_pool_auth_mode", "query") or "").strip().lower()
    if auth_mode not in ("header", "query"):
        auth_mode = "query"
    try:
        count = max(1, min(int(cfg.get("proxy_pool_count", 1) or 1), 20))
    except (TypeError, ValueError):
        count = 1
    country = str(cfg.get("proxy_pool_country", "US") or "US").strip().upper() or "US"
    try:
        fetch_retries = max(1, min(int(cfg.get("proxy_pool_fetch_retries", 3) or 3), 10))
    except (TypeError, ValueError):
        fetch_retries = 3
    try:
        bad_ttl_seconds = max(10, min(int(cfg.get("proxy_pool_bad_ttl_seconds", 180) or 180), 3600))
    except (TypeError, ValueError):
        bad_ttl_seconds = 180
    tcp_check_enabled = bool(cfg.get("proxy_pool_tcp_check_enabled", True))
    try:
        tcp_check_timeout_seconds = max(
            0.5,
            min(float(cfg.get("proxy_pool_tcp_check_timeout_seconds", 1.2) or 1.2), 10.0),
        )
    except (TypeError, ValueError):
        tcp_check_timeout_seconds = 1.2
    prefer_stable_proxy = bool(cfg.get("proxy_pool_prefer_stable_proxy", True))
    stable_proxy = str(cfg.get("proxy_pool_stable_proxy", "") or "").strip()
    api_key = str(cfg.get("proxy_pool_api_key", "") or "").strip()
    return {
        "proxy_pool_enabled": bool(cfg.get("proxy_pool_enabled", False)),
        "proxy_pool_api_url": api_url,
        "proxy_pool_auth_mode": auth_mode,
        "proxy_pool_api_key": "",
        "proxy_pool_api_key_preview": "",
        "proxy_pool_api_key_configured": bool(api_key),
        "proxy_pool_count": count,
        "proxy_pool_country": country,
        "proxy_pool_fetch_retries": fetch_retries,
        "proxy_pool_bad_ttl_seconds": bad_ttl_seconds,
        "proxy_pool_tcp_check_enabled": tcp_check_enabled,
        "proxy_pool_tcp_check_timeout_seconds": tcp_check_timeout_seconds,
        "proxy_pool_prefer_stable_proxy": prefer_stable_proxy,
        "proxy_pool_stable_proxy": stable_proxy,
    }


@app.post("/api/proxy-pool/config")
async def api_set_proxy_pool_config(req: ProxyPoolConfigRequest) -> Dict[str, Any]:
    cfg = _get_sync_config()
    proxy_pool_auth_mode = str(req.proxy_pool_auth_mode or "query").strip().lower()
    if proxy_pool_auth_mode not in ("header", "query"):
        proxy_pool_auth_mode = "query"

    proxy_pool_api_url = str(req.proxy_pool_api_url or DEFAULT_PROXY_POOL_API_URL).strip()
    if not proxy_pool_api_url:
        proxy_pool_api_url = DEFAULT_PROXY_POOL_API_URL

    proxy_pool_api_key = req.proxy_pool_api_key.strip() if req.proxy_pool_api_key else ""
    if not proxy_pool_api_key:
        proxy_pool_api_key = str(cfg.get("proxy_pool_api_key", "") or "").strip()

    try:
        proxy_pool_count = max(1, min(int(req.proxy_pool_count), 20))
    except (TypeError, ValueError):
        proxy_pool_count = 1
    proxy_pool_country = str(req.proxy_pool_country or "US").strip().upper() or "US"
    try:
        proxy_pool_fetch_retries = max(1, min(int(req.proxy_pool_fetch_retries), 10))
    except (TypeError, ValueError):
        proxy_pool_fetch_retries = 3
    try:
        proxy_pool_bad_ttl_seconds = max(10, min(int(req.proxy_pool_bad_ttl_seconds), 3600))
    except (TypeError, ValueError):
        proxy_pool_bad_ttl_seconds = 180
    try:
        proxy_pool_tcp_check_timeout_seconds = max(
            0.5,
            min(float(req.proxy_pool_tcp_check_timeout_seconds), 10.0),
        )
    except (TypeError, ValueError):
        proxy_pool_tcp_check_timeout_seconds = 1.2

    cfg.update({
        "proxy_pool_enabled": bool(req.proxy_pool_enabled),
        "proxy_pool_api_url": proxy_pool_api_url,
        "proxy_pool_auth_mode": proxy_pool_auth_mode,
        "proxy_pool_api_key": proxy_pool_api_key,
        "proxy_pool_count": proxy_pool_count,
        "proxy_pool_country": proxy_pool_country,
        "proxy_pool_fetch_retries": proxy_pool_fetch_retries,
        "proxy_pool_bad_ttl_seconds": proxy_pool_bad_ttl_seconds,
        "proxy_pool_tcp_check_enabled": bool(req.proxy_pool_tcp_check_enabled),
        "proxy_pool_tcp_check_timeout_seconds": proxy_pool_tcp_check_timeout_seconds,
        "proxy_pool_prefer_stable_proxy": bool(req.proxy_pool_prefer_stable_proxy),
        "proxy_pool_stable_proxy": str(req.proxy_pool_stable_proxy or "").strip(),
    })
    _save_sync_config(cfg)
    return {"status": "saved"}


@app.post("/api/runtime-config")
async def api_set_runtime_config(req: RuntimeConfigRequest) -> Dict[str, Any]:
    cfg = _get_sync_config()
    before = {key: cfg.get(key) for key in (*_RUNTIME_RESTART_REQUIRED_FIELDS, *_RUNTIME_IMMEDIATE_FIELDS)}
    cfg.update({
        "service_name": req.service_name.strip() or "OpenAI Pool Orchestrator",
        "process_name": req.process_name.strip() or "openai-pool",
        "listen_host": req.listen_host.strip() or "0.0.0.0",
        "listen_port": max(1, min(int(req.listen_port), 65535)),
        "reload_enabled": bool(req.reload_enabled),
        "debug_logging": bool(req.debug_logging),
        "anonymous_mode": bool(req.anonymous_mode),
        "log_dir": req.log_dir.strip() or str(LOGS_DIR),
        "log_level": str(req.log_level or "INFO").strip().upper() or "INFO",
        "file_log_level": str(req.file_log_level or "DEBUG").strip().upper() or "DEBUG",
        "log_rotation": req.log_rotation.strip() or "1 day",
        "log_retention_days": max(1, min(int(req.log_retention_days), 3650)),
    })
    saved = _save_sync_config(cfg)

    restart_required_fields = [
        field
        for field in _RUNTIME_RESTART_REQUIRED_FIELDS
        if before.get(field) != saved.get(field)
    ]
    immediate_applied_fields = [
        field
        for field in _RUNTIME_IMMEDIATE_FIELDS
        if before.get(field) != saved.get(field)
    ]
    return {
        "status": "saved",
        "restart_required": bool(restart_required_fields),
        "restart_required_fields": restart_required_fields,
        "immediate_applied_fields": immediate_applied_fields,
    }


def _verify_sub2api_login(base_url: str, email: str, password: str) -> Dict[str, Any]:
    """通过 HTTP API 验证 Sub2Api 平台登录凭据是否正确"""
    from curl_cffi import requests as cffi_req

    # 自动补全协议（优先 https://）
    url = base_url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    login_url = url.rstrip("/") + "/api/v1/auth/login"
    try:
        resp = cffi_req.post(
            login_url,
            json={"email": email, "password": password},
            impersonate="chrome",
            timeout=15,
        )
        raw_body = resp.text
        if resp.status_code != 200:
            try:
                err_body = json.loads(raw_body)
                err_msg = err_body.get("message") or err_body.get("error") or raw_body[:200]
            except json.JSONDecodeError:
                err_msg = raw_body[:200]
            return {"ok": False, "error": f"登录失败(HTTP {resp.status_code}): {err_msg}"}
        try:
            body = json.loads(raw_body)
        except json.JSONDecodeError:
            return {"ok": False, "error": f"服务器返回非 JSON 格式: {raw_body[:200]}"}

        token = (
            body.get("token")
            or body.get("access_token")
            or (body.get("data") or {}).get("token")
            or (body.get("data") or {}).get("access_token")
            or ""
        )
        return {"ok": True, "token": token}
    except Exception as e:
        return {"ok": False, "error": f"请求异常: {e}"}


def _verify_sub2api_token(base_url: str, bearer_token: str) -> Dict[str, Any]:
    from curl_cffi import requests as cffi_req

    url = base_url.strip()
    if not url.startswith(("http://", "https://")):
        url = "https://" + url

    verify_url = url.rstrip("/") + "/api/v1/admin/dashboard/stats"
    try:
        resp = cffi_req.get(
            verify_url,
            headers={
                "Authorization": f"Bearer {bearer_token}",
                "Accept": "application/json",
            },
            params={"timezone": "Asia/Shanghai"},
            impersonate="chrome",
            timeout=15,
        )
        if resp.status_code != 200:
            return {"ok": False, "error": f"Bearer Token 验证失败: HTTP {resp.status_code}"}
        return {"ok": True}
    except Exception as e:
        return {"ok": False, "error": f"Bearer Token 验证异常: {e}"}


@app.post("/api/sync-config")
async def api_set_sync_config(req: SyncConfigRequest) -> Dict[str, Any]:
    """保存同步配置（先验证登录凭据）"""
    cfg = _get_sync_config()
    new_base_url = req.base_url.strip()
    if new_base_url and not new_base_url.startswith(("http://", "https://")):
        new_base_url = "https://" + new_base_url
    new_email = req.email.strip() or str(cfg.get("email", "") or "").strip()
    new_password = req.password.strip() if req.password else str(cfg.get("password", "") or "").strip()
    bearer_token = req.bearer_token.strip() or str(cfg.get("bearer_token", "") or "").strip()

    if not new_base_url:
        raise HTTPException(status_code=400, detail="请填写平台地址")

    verified_token = bearer_token
    if new_email and new_password:
        verify = await run_in_threadpool(_verify_sub2api_login, new_base_url, new_email, new_password)
        if not verify["ok"]:
            raise HTTPException(status_code=400, detail=verify["error"])
        verified_token = str(verify.get("token") or "").strip() or bearer_token
    elif bearer_token:
        verify = await run_in_threadpool(_verify_sub2api_token, new_base_url, bearer_token)
        if not verify["ok"]:
            raise HTTPException(status_code=400, detail=verify["error"])
    else:
        raise HTTPException(status_code=400, detail="请填写 Bearer Token 或邮箱和密码")

    cfg.update({
        "base_url": new_base_url,
        "bearer_token": verified_token,
        "email": new_email,
        "password": new_password,
        "account_name": req.account_name.strip(),
        "auto_sync": req.auto_sync,
        "sub2api_min_candidates": max(1, req.sub2api_min_candidates),
        "sub2api_group_ids": _normalize_sub2api_group_ids(req.sub2api_group_ids),
        "sub2api_auto_maintain": req.sub2api_auto_maintain,
        "sub2api_maintain_interval_minutes": max(5, req.sub2api_maintain_interval_minutes),
        "sub2api_maintain_actions": _normalize_sub2api_maintain_actions(req.sub2api_maintain_actions),
        "multithread": req.multithread,
        "thread_count": max(1, min(req.thread_count, 10)),
        "auto_register": req.auto_register,
    })
    _save_sync_config(cfg)
    _clear_sub2api_accounts_cache()

    # 先停再启，确保旧线程已退出
    _stop_sub2api_auto_maintain()
    if req.sub2api_auto_maintain:
        _start_sub2api_auto_maintain()

    return {"status": "saved", "verified": True}



class Sub2ApiLoginRequest(BaseModel):
    base_url: str
    email: str
    password: str


@app.post("/api/sub2api-login")
async def api_sub2api_login(req: Sub2ApiLoginRequest) -> Dict[str, Any]:
    """用账号密码登录 Sub2Api 平台，自动获取并保存 Bearer Token"""
    def _login() -> Dict[str, Any]:
        cfg = _get_sync_config()
        base_url = req.base_url.strip()
        if not base_url:
            raise HTTPException(status_code=400, detail="请填写平台地址")
        if not base_url.startswith(("http://", "https://")):
            base_url = "https://" + base_url

        login_url = base_url.rstrip("/") + "/api/v1/auth/login"
        payload = json.dumps({"email": req.email, "password": req.password}).encode("utf-8")
        request = urllib.request.Request(
            login_url,
            data=payload,
            method="POST",
            headers={"Content-Type": "application/json", "Accept": "application/json"},
        )
        try:
            with urllib.request.urlopen(request, timeout=15) as resp:
                raw_body = resp.read().decode("utf-8")
                try:
                    body = json.loads(raw_body)
                except json.JSONDecodeError:
                    raise HTTPException(status_code=502, detail=f"服务器返回非 JSON 格式: {raw_body[:200]}")
        except urllib.error.HTTPError as exc:
            raw = exc.read().decode("utf-8", "replace")
            try:
                err_body = json.loads(raw)
                err_msg = err_body.get("message") or err_body.get("error") or raw[:200]
            except json.JSONDecodeError:
                err_msg = raw[:200]
            raise HTTPException(status_code=exc.code, detail=f"登录失败: {err_msg}")
        except HTTPException:
            raise
        except Exception as e:
            raise HTTPException(status_code=500, detail=f"请求异常: {e}")

        token = (
            body.get("token")
            or body.get("access_token")
            or (body.get("data") or {}).get("token")
            or (body.get("data") or {}).get("access_token")
            or ""
        )
        if not token:
            raise HTTPException(status_code=502, detail=f"响应中未找到 token 字段: {str(body)[:300]}")

        cfg["base_url"] = base_url
        cfg["bearer_token"] = token
        _save_sync_config(cfg)
        return {"ok": True, "token_preview": token[:16] + "..."}

    return await run_in_threadpool(_login)


@app.post("/api/check-proxy")
async def api_check_proxy(req: ProxyCheckRequest) -> Dict[str, Any]:
    """检测代理是否可用（通过 Cloudflare Trace）"""
    def _check() -> Dict[str, Any]:
        proxy = req.proxy.strip()
        try:
            parsed = _parse_cloudflare_trace(_request_cloudflare_trace_via_proxy(proxy, timeout=8))
            return {"ok": bool(parsed["supported"]), "loc": parsed["loc"], "error": parsed["error"]}
        except Exception as e:
            return {"ok": False, "loc": None, "error": str(e)}

    return await run_in_threadpool(_check)


@app.post("/api/proxy-pool/test")
async def api_proxy_pool_test(req: ProxyPoolTestRequest) -> Dict[str, Any]:
    """测试代理池取号：返回取到的代理与可选 loc 探测结果"""
    def _test() -> Dict[str, Any]:
        cfg_snapshot = _get_sync_config()
        auth_mode = str(req.auth_mode or "query").strip().lower()
        if auth_mode not in ("header", "query"):
            auth_mode = "query"
        api_url = str(req.api_url or DEFAULT_PROXY_POOL_API_URL).strip() or DEFAULT_PROXY_POOL_API_URL
        api_key = req.api_key.strip() if req.api_key else str(cfg_snapshot.get("proxy_pool_api_key", "")).strip()
        try:
            count = max(1, min(int(req.count or cfg_snapshot.get("proxy_pool_count", 1)), 20))
        except (TypeError, ValueError):
            count = 1
        country = str(req.country or cfg_snapshot.get("proxy_pool_country", "US") or "US").strip().upper() or "US"
        try:
            fetch_retries = max(1, min(int(req.fetch_retries or cfg_snapshot.get("proxy_pool_fetch_retries", 3)), 10))
        except (TypeError, ValueError):
            fetch_retries = 3
        try:
            bad_ttl_seconds = max(
                10,
                min(int(req.bad_ttl_seconds or cfg_snapshot.get("proxy_pool_bad_ttl_seconds", 180)), 3600),
            )
        except (TypeError, ValueError):
            bad_ttl_seconds = 180
        try:
            tcp_check_timeout_seconds = max(
                0.5,
                min(
                    float(
                        req.tcp_check_timeout_seconds
                        or cfg_snapshot.get("proxy_pool_tcp_check_timeout_seconds", 1.2)
                    ),
                    10.0,
                ),
            )
        except (TypeError, ValueError):
            tcp_check_timeout_seconds = 1.2
        stable_proxy = str(
            req.stable_proxy or cfg_snapshot.get("proxy_pool_stable_proxy", "") or ""
        ).strip()

        cfg = {
            "enabled": bool(req.enabled),
            "api_url": api_url,
            "auth_mode": auth_mode,
            "api_key": api_key,
            "count": count,
            "country": country,
            "timeout_seconds": 10,
            "fetch_retries": fetch_retries,
            "bad_ttl_seconds": bad_ttl_seconds,
            "tcp_check_enabled": bool(req.tcp_check_enabled),
            "tcp_check_timeout_seconds": tcp_check_timeout_seconds,
            "prefer_stable_proxy": bool(req.prefer_stable_proxy),
            "stable_proxy": stable_proxy,
        }
        if not cfg["enabled"]:
            return {"ok": False, "error": "代理池未启用"}
        if not cfg["api_key"] and not str(api_url).lower().endswith(".txt") and "/proxies/countries/" not in str(api_url).lower():
            return {"ok": False, "error": "API Key 为空"}

        try:
            from curl_cffi import requests as cffi_req

            relay_url = _pool_relay_url_from_fetch_url(api_url)
            if relay_url:
                relay_params = {"api_key": api_key, "url": "https://cloudflare.com/cdn-cgi/trace", "country": country}
                try:
                    relay_resp = cffi_req.get(relay_url, params=relay_params, http_version="v2", impersonate="chrome", timeout=8)
                except Exception as exc:
                    if "HTTP/3 is not supported over an HTTP proxy" not in str(exc):
                        raise
                    relay_resp = cffi_req.get(relay_url, params=relay_params, http_version="v1", impersonate="chrome", timeout=8)
                if relay_resp.status_code == 200:
                    relay_parsed = _parse_cloudflare_trace(relay_resp.text)
                    if not relay_parsed["supported"]:
                        return {
                            "ok": False,
                            "proxy": "(relay)",
                            "relay_used": True,
                            "relay_url": relay_url,
                            "count": count,
                            "country": country,
                            "stable_proxy": stable_proxy,
                            "loc": relay_parsed["loc"],
                            "supported": relay_parsed["supported"],
                            "error": str(relay_parsed["error"] or "relay 所在地不支持"),
                            "trace_error": None,
                        }
                    return {
                        "ok": True,
                        "proxy": "(relay)",
                        "relay_used": True,
                        "relay_url": relay_url,
                        "count": count,
                        "country": country,
                        "stable_proxy": stable_proxy,
                        "loc": relay_parsed["loc"],
                        "supported": relay_parsed["supported"],
                        "trace_error": None,
                    }

            proxy = _fetch_proxy_from_pool(cfg)
            try:
                parsed = _parse_cloudflare_trace(_request_cloudflare_trace_via_proxy(proxy, timeout=8))
            except Exception as e:
                trace_error = str(e)
                return {
                    "ok": False,
                    "proxy": proxy,
                    "relay_used": False,
                    "count": count,
                    "country": country,
                    "stable_proxy": stable_proxy,
                    "loc": None,
                    "supported": None,
                    "error": f"已取到代理 {proxy}，但 HTTPS 连通性检测失败: {trace_error}",
                    "trace_error": trace_error,
                }

            if not parsed["supported"]:
                return {
                    "ok": False,
                    "proxy": proxy,
                    "relay_used": False,
                    "count": count,
                    "country": country,
                    "stable_proxy": stable_proxy,
                    "loc": parsed["loc"],
                    "supported": parsed["supported"],
                    "error": f"已取到代理 {proxy}，但所在地不支持: {parsed['loc']}",
                    "trace_error": None,
                }

            return {
                "ok": True,
                "proxy": proxy,
                "relay_used": False,
                "count": count,
                "country": country,
                "stable_proxy": stable_proxy,
                "loc": parsed["loc"],
                "supported": parsed["supported"],
                "trace_error": None,
            }
        except Exception as e:
            return {"ok": False, "error": str(e)}

    return await run_in_threadpool(_test)


@app.get("/api/logs")
async def api_logs(request: Request) -> StreamingResponse:
    """SSE 实时结构化事件流"""

    async def event_generator() -> AsyncGenerator[str, None]:
        q = _state.subscribe()
        last_heartbeat = time.monotonic()
        try:
            snapshot = _state.get_status_snapshot()
            connected = {
                "type": "connected",
                "message": "日志连接成功",
                "run_id": snapshot["task"].get("run_id"),
                "revision": snapshot["task"].get("revision", 0),
                "snapshot": snapshot,
            }
            yield f"event: connected\ndata: {json.dumps(connected, ensure_ascii=False)}\n\n"
            while True:
                if _service_shutdown_event.is_set():
                    break
                if await request.is_disconnected():
                    break
                try:
                    event = await asyncio.wait_for(q.get(), timeout=1.0)
                    event_type = str(event.get("type") or "message")
                    yield f"event: {event_type}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"
                    if _service_shutdown_event.is_set() or str(event.get("step") or "").strip().lower() == "shutdown":
                        break
                except asyncio.TimeoutError:
                    if _service_shutdown_event.is_set():
                        break
                    now = time.monotonic()
                    if now - last_heartbeat >= 15:
                        last_heartbeat = now
                        heartbeat = {
                            "type": "heartbeat",
                            "run_id": _state.run_id,
                            "revision": _state.revision,
                            "server_time": datetime.now().isoformat(timespec="seconds"),
                        }
                        yield f"event: heartbeat\ndata: {json.dumps(heartbeat, ensure_ascii=False)}\n\n"
                except asyncio.CancelledError:
                    break
                except Exception:
                    break
        finally:
            _state.unsubscribe(q)

    return StreamingResponse(
        event_generator(),
        media_type="text/event-stream",
        headers={
            "Cache-Control": "no-cache",
            "X-Accel-Buffering": "no",
        },
    )



class TokenSyncToSub2ApiRequest(BaseModel):
    filenames: List[str] = []  # 空列表 = 同步全部


def _decode_jwt_payload(token: str) -> Dict[str, Any]:
    """解析 JWT payload（不验签）"""
    try:
        parts = token.split(".")
        if len(parts) != 3:
            return {}
        payload = parts[1]
        pad = 4 - len(payload) % 4
        if pad != 4:
            payload += "=" * pad
        import base64 as _b64
        decoded = _b64.urlsafe_b64decode(payload.encode("ascii"))
        return json.loads(decoded.decode("utf-8"))
    except Exception:
        return {}


def _build_account_payload(
    email: str,
    token_data: Dict[str, Any],
    group_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """参考 chatgpt_register.py 构建 /api/v1/admin/accounts 所需 payload"""
    access_token  = token_data.get("access_token", "")
    refresh_token = token_data.get("refresh_token", "")
    id_token      = token_data.get("id_token", "")

    at_payload = _decode_jwt_payload(access_token) if access_token else {}
    at_auth    = at_payload.get("https://api.openai.com/auth") or {}
    chatgpt_account_id = at_auth.get("chatgpt_account_id", "") or token_data.get("account_id", "")
    chatgpt_user_id    = at_auth.get("chatgpt_user_id", "")
    exp_timestamp      = at_payload.get("exp", 0)
    expires_at = exp_timestamp if isinstance(exp_timestamp, int) and exp_timestamp > 0 else int(time.time()) + 863999

    it_payload = _decode_jwt_payload(id_token) if id_token else {}
    it_auth    = it_payload.get("https://api.openai.com/auth") or {}
    organization_id = it_auth.get("organization_id", "")
    if not organization_id:
        orgs = it_auth.get("organizations") or []
        if orgs:
            organization_id = (orgs[0] or {}).get("id", "")
    normalized_group_ids = _normalize_sub2api_group_ids(
        group_ids,
        default_if_missing=DEFAULT_SUB2API_GROUP_IDS,
    )

    return {
        "name": email,
        "notes": "",
        "platform": "openai",
        "type": "oauth",
        "credentials": {
            "access_token": access_token,
            "refresh_token": refresh_token,
            "expires_in": 863999,
            "expires_at": expires_at,
            "chatgpt_account_id": chatgpt_account_id,
            "chatgpt_user_id": chatgpt_user_id,
            "organization_id": organization_id,
        },
        "extra": {"email": email},
        "proxy_id": None,
        "concurrency": 10,
        "priority": 1,
        "rate_multiplier": 1,
        "group_ids": normalized_group_ids,
        "expires_at": None,
        "auto_pause_on_expired": True,
    }


def _push_account_api(
    base_url: str,
    bearer: str,
    email: str,
    token_data: Dict[str, Any],
    group_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """调用 /api/v1/admin/accounts 提交完整账号信息"""
    from curl_cffi import requests as cffi_req
    url = base_url.rstrip("/") + "/api/v1/admin/accounts"
    payload = _build_account_payload(email, token_data, group_ids=group_ids)
    try:
        resp = cffi_req.post(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Referer": base_url.rstrip("/") + "/admin/accounts",
            },
            impersonate="chrome",
            timeout=20,
        )
        return {"ok": resp.status_code in (200, 201), "status": resp.status_code, "body": resp.text[:300]}
    except Exception as e:
        return {"ok": False, "status": 0, "body": str(e)}


def _update_sub2api_account_api(
    base_url: str,
    bearer: str,
    account_id: int,
    email: str,
    token_data: Dict[str, Any],
    group_ids: Optional[List[int]] = None,
) -> Dict[str, Any]:
    """
    命中已存在账号后，更新其凭据，避免“存在即跳过”导致账号长期不刷新。
    """
    from curl_cffi import requests as cffi_req

    url = base_url.rstrip("/") + f"/api/v1/admin/accounts/{int(account_id)}"
    create_payload = _build_account_payload(email, token_data, group_ids=group_ids)
    credentials = create_payload.get("credentials") if isinstance(create_payload.get("credentials"), dict) else {}
    extra = create_payload.get("extra") if isinstance(create_payload.get("extra"), dict) else {}
    payload = {
        "name": str(email or "").strip(),
        "credentials": credentials,
        "extra": extra,
        "concurrency": create_payload.get("concurrency", 10),
        "priority": create_payload.get("priority", 1),
        "group_ids": create_payload.get("group_ids", []),
        "status": "active",
        "auto_pause_on_expired": True,
    }
    try:
        resp = cffi_req.put(
            url,
            json=payload,
            headers={
                "Authorization": f"Bearer {bearer}",
                "Content-Type": "application/json",
                "Accept": "application/json, text/plain, */*",
                "Referer": base_url.rstrip("/") + "/admin/accounts",
            },
            impersonate="chrome",
            timeout=20,
        )
        return {"ok": resp.status_code in (200, 201), "status": resp.status_code, "body": resp.text[:300]}
    except Exception as e:
        return {"ok": False, "status": 0, "body": str(e)}


def _extract_sub2api_page_payload(body: Any) -> Dict[str, Any]:
    if isinstance(body, dict):
        data = body.get("data")
        if isinstance(data, dict):
            return data
        return body
    return {}


def _parse_sub2api_account_timestamp(raw: Any) -> float:
    text = str(raw or "").strip()
    if not text:
        return 0.0
    iso_text = text[:-1] + "+00:00" if text.endswith("Z") else text
    try:
        return datetime.fromisoformat(iso_text).timestamp()
    except Exception:
        pass
    for fmt in ("%Y-%m-%d %H:%M:%S", "%Y-%m-%dT%H:%M:%S"):
        try:
            return datetime.strptime(text, fmt).timestamp()
        except Exception:
            continue
    return 0.0


def _sub2api_account_sort_key(item: Dict[str, Any]) -> tuple[float, int]:
    updated = _parse_sub2api_account_timestamp(item.get("updated_at") or item.get("updatedAt"))
    try:
        account_id = int(item.get("id") or 0)
    except (TypeError, ValueError):
        account_id = 0
    return (updated, account_id)


def _coerce_sub2api_account_mapping(raw: Any) -> Dict[str, Any]:
    if isinstance(raw, dict):
        return raw
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return {}
        try:
            parsed = json.loads(text)
        except Exception:
            return {}
        if isinstance(parsed, dict):
            return parsed
    return {}


def _sub2api_account_identity(item: Dict[str, Any]) -> tuple[str, str]:
    extra = _coerce_sub2api_account_mapping(item.get("extra"))
    credentials = _coerce_sub2api_account_mapping(item.get("credentials"))
    raw_email = str(extra.get("email") or item.get("email") or "").strip()
    if not raw_email:
        raw_name = str(item.get("name") or "").strip()
        if "@" in raw_name:
            raw_email = raw_name
    email = raw_email.lower()
    refresh_token = str(credentials.get("refresh_token") or item.get("refresh_token") or "").strip()
    return email, refresh_token


def _build_sub2api_account_list_item(raw_item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    try:
        account_id = int(raw_item.get("id") or 0)
    except (TypeError, ValueError):
        return None
    if account_id <= 0:
        return None

    email, _ = _sub2api_account_identity(raw_item)
    status = str(raw_item.get("status") or "").strip().lower() or "unknown"
    return {
        "id": account_id,
        "name": str(raw_item.get("name") or "").strip(),
        "email": email or str(raw_item.get("name") or "").strip(),
        "status": status,
        "updated_at": raw_item.get("updated_at") or raw_item.get("updatedAt") or "",
        "created_at": raw_item.get("created_at") or raw_item.get("createdAt") or "",
        "is_duplicate": False,
        "duplicate_group_size": 0,
        "duplicate_keep": False,
        "duplicate_delete_candidate": False,
        "duplicate_emails": [],
    }


def _build_live_sub2api_accounts_snapshot(
    sm: Sub2ApiMaintainer,
    *,
    page: int = 1,
    page_size: int = 20,
    status: str = "all",
    keyword: str = "",
    timeout: int = 15,
) -> Dict[str, Any]:
    normalized_status = str(status or "all").strip().lower() or "all"
    safe_page = max(1, int(page or 1))
    safe_page_size = max(10, min(int(page_size or 20), 100))
    live_page = sm.list_accounts(
        page=safe_page,
        page_size=safe_page_size,
        timeout=timeout,
        status=normalized_status,
        search=keyword,
    )

    items: List[Dict[str, Any]] = []
    abnormal_count = 0
    for raw_item in live_page.get("items") or []:
        if not isinstance(raw_item, dict):
            continue
        item = _build_sub2api_account_list_item(raw_item)
        if item is None:
            continue
        if str(item.get("status") or "").strip().lower() in {"error", "disabled"}:
            abnormal_count += 1
        items.append(item)

    filtered_items = _filter_sub2api_account_items(items, status=normalized_status, keyword=keyword)
    try:
        remote_total = int(live_page.get("total") or 0)
    except (TypeError, ValueError):
        remote_total = 0
    filtered_total = max(remote_total, len(filtered_items))
    current_page = max(1, int(live_page.get("page") or safe_page))
    effective_page_size = max(10, min(int(live_page.get("page_size") or safe_page_size), 100))
    total_pages = max(1, (filtered_total + effective_page_size - 1) // effective_page_size)
    return {
        "total": filtered_total,
        "candidate_count": 0,
        "error_count": filtered_total if normalized_status in {"error", "disabled"} else abnormal_count,
        "duplicate_groups": 0,
        "duplicate_accounts": 0,
        "items": filtered_items,
        "page": min(current_page, total_pages),
        "page_size": effective_page_size,
        "filtered_total": filtered_total,
        "total_pages": total_pages,
        "error": "",
        "stale": False,
        "_paged": True,
    }


def _build_sub2api_identity_index(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    index: Dict[str, Dict[str, Any]] = {}
    for item in items:
        if not isinstance(item, dict):
            continue
        email, refresh_token = _sub2api_account_identity(item)
        for key in sub2api_identity_keys(email=email, refresh_token=refresh_token):
            current = index.get(key)
            if current is None or _sub2api_account_sort_key(item) >= _sub2api_account_sort_key(current):
                index[key] = item
    return index


def _find_sub2api_account_in_index(
    identity_index: Dict[str, Dict[str, Any]],
    email: str,
    refresh_token: str,
) -> Optional[Dict[str, Any]]:
    matched: Optional[Dict[str, Any]] = None
    for key in sub2api_identity_keys(email=email, refresh_token=refresh_token):
        candidate = identity_index.get(key)
        if candidate is None:
            continue
        if matched is None or _sub2api_account_sort_key(candidate) >= _sub2api_account_sort_key(matched):
            matched = candidate
    return matched


def _build_sub2api_index_item(
    email: str,
    refresh_token: str,
    *,
    account_id: Optional[int] = None,
) -> Dict[str, Any]:
    item: Dict[str, Any] = {
        "name": str(email or "").strip(),
        "extra": {"email": str(email or "").strip().lower()},
        "credentials": {"refresh_token": str(refresh_token or "").strip()},
        "updated_at": datetime.now().isoformat(timespec="seconds"),
    }
    if isinstance(account_id, int) and account_id > 0:
        item["id"] = account_id
    return item


def _build_local_token_payload_from_sub2api_account(item: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(item, dict):
        return None

    email, refresh_token = _sub2api_account_identity(item)
    if not email and not refresh_token:
        return None

    credentials = _coerce_sub2api_account_mapping(item.get("credentials"))
    token_data: Dict[str, Any] = {}
    if isinstance(credentials, dict):
        token_data.update(credentials)

    if email:
        token_data["email"] = email
    if refresh_token:
        token_data["refresh_token"] = refresh_token

    account_id = str(
        token_data.get("chatgpt_account_id")
        or token_data.get("account_id")
        or item.get("account_id")
        or ""
    ).strip()
    if account_id and not str(token_data.get("account_id") or "").strip():
        token_data["account_id"] = account_id

    expires_at_ts = _parse_sub2api_account_timestamp(
        token_data.get("expires_at") or item.get("expires_at")
    )
    if expires_at_ts > 0 and not str(token_data.get("expired") or "").strip():
        token_data["expired"] = datetime.fromtimestamp(expires_at_ts).isoformat(timespec="seconds")

    uploaded_ts = str(
        item.get("updated_at")
        or item.get("updatedAt")
        or item.get("created_at")
        or item.get("createdAt")
        or datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    ).strip()
    token_data["uploaded_platforms"] = ["sub2api"]
    token_data["uploaded_at"] = {"sub2api": uploaded_ts}
    return token_data


def _import_missing_sub2api_accounts_to_local_tokens() -> Dict[str, Any]:
    from .local_tokens import save_local_token

    cfg = _get_sync_config()
    sm = _get_sub2api_maintainer(cfg)
    if not sm:
        raise HTTPException(status_code=400, detail="请先配置 Sub2Api 平台地址和 Bearer Token")

    remote_accounts = [
        item
        for item in sm._list_all_accounts(
            timeout=SUB2API_IMPORT_SNAPSHOT_TIMEOUT_SECONDS,
            page_size=500,
        )
        if isinstance(item, dict)
    ]
    remote_accounts.sort(key=_sub2api_account_sort_key, reverse=True)

    local_keys = load_local_token_identity_keys(max_files=max(20000, len(remote_accounts) * 4))
    imported = 0
    skipped_existing = 0
    skipped_invalid = 0
    examples: List[Dict[str, Any]] = []

    for item in remote_accounts:
        email, refresh_token = _sub2api_account_identity(item)
        identity_keys = sub2api_identity_keys(email=email, refresh_token=refresh_token)
        if not identity_keys:
            skipped_invalid += 1
            continue
        if any(key in local_keys for key in identity_keys):
            skipped_existing += 1
            continue

        token_data = _build_local_token_payload_from_sub2api_account(item)
        if not isinstance(token_data, dict):
            skipped_invalid += 1
            continue

        saved = save_local_token(token_data)
        imported += 1
        local_keys.update(identity_keys)
        if len(examples) < 20:
            examples.append({
                "filename": str(saved.get("filename") or ""),
                "email": str(saved.get("email") or email or ""),
                "refresh_token_present": bool(refresh_token),
            })

    return {
        "remote_total": len(remote_accounts),
        "imported": imported,
        "skipped_existing": skipped_existing,
        "skipped_invalid": skipped_invalid,
        "examples": examples,
    }


def _sync_token_files_to_sub2api(
    filenames: Optional[List[str]] = None,
) -> Dict[str, Any]:
    cfg = _get_sync_config()
    sm = _get_sub2api_maintainer(cfg)
    if not sm:
        raise HTTPException(status_code=400, detail="请先配置 Sub2Api 平台地址和 Bearer Token")

    base_url = str(cfg.get("base_url", "") or "").strip()
    bearer = str(cfg.get("bearer_token", "") or "").strip()
    group_ids = _get_sub2api_group_ids(cfg)
    if not base_url or not bearer:
        raise HTTPException(status_code=400, detail="请先配置 Sub2Api 平台地址和 Bearer Token")

    fnames = list(filenames or [])
    if not fnames:
        fnames = list_local_token_filenames()
    valid_fnames = [
        fname for fname in fnames
        if fname and "/" not in fname and "\\" not in fname and ".." not in fname
    ]

    remote_accounts = sm._list_all_accounts(
        timeout=SUB2API_IMPORT_SNAPSHOT_TIMEOUT_SECONDS,
        page_size=500,
    )
    identity_index = _build_sub2api_identity_index(remote_accounts)
    token_records = get_local_token_records_by_filenames(valid_fnames, include_content=True)

    results: List[Dict[str, Any]] = []
    identity_index_lock = threading.Lock()
    identity_locks_guard = threading.Lock()
    identity_locks: Dict[str, threading.Lock] = {}

    def _get_identity_locks(keys: List[str]) -> List[threading.Lock]:
        if not keys:
            return []
        with identity_locks_guard:
            for key in keys:
                if key not in identity_locks:
                    identity_locks[key] = threading.Lock()
            return [identity_locks[key] for key in sorted(keys)]

    def _sync_one(index: int, fname: str) -> Dict[str, Any]:
        token_record = token_records.get(fname)
        token_data = token_record.get("content") if isinstance(token_record, dict) else None
        if not isinstance(token_data, dict):
            return {"index": index, "file": fname, "ok": False, "error": "文件不存在"}

        email = str(token_data.get("email", fname) or fname)
        refresh_token = str(token_data.get("refresh_token") or "").strip()
        local_keys = sub2api_identity_keys(email=email, refresh_token=refresh_token)
        acquired_locks = _get_identity_locks(local_keys)

        for lock in acquired_locks:
            lock.acquire()
        try:
            with identity_index_lock:
                existing = _find_sub2api_account_in_index(identity_index, email, refresh_token)

            result: Dict[str, Any]
            existing_id = None
            try:
                existing_id = int((existing or {}).get("id") or 0)
            except (TypeError, ValueError):
                existing_id = None

            if existing_id is not None and existing_id > 0:
                update_result = _update_sub2api_account_api(
                    base_url=base_url,
                    bearer=bearer,
                    account_id=existing_id,
                    email=email,
                    token_data=token_data,
                    group_ids=group_ids,
                )
                if update_result.get("ok"):
                    result = {
                        "ok": True,
                        "status": int(update_result.get("status") or 200),
                        "body": "existing account updated",
                        "skipped": False,
                        "reason": "updated_existing_from_snapshot",
                        "existing_id": existing_id,
                    }
                else:
                    result = _push_account_api_with_dedupe(
                        base_url=base_url,
                        bearer=bearer,
                        email=email,
                        token_data=token_data,
                        group_ids=group_ids,
                        check_before=True,
                        check_after=True,
                    )
            elif existing is not None:
                result = _push_account_api_with_dedupe(
                    base_url=base_url,
                    bearer=bearer,
                    email=email,
                    token_data=token_data,
                    group_ids=group_ids,
                    check_before=True,
                    check_after=True,
                )
            else:
                result = _push_account_api_with_dedupe(
                    base_url=base_url,
                    bearer=bearer,
                    email=email,
                    token_data=token_data,
                    group_ids=group_ids,
                    check_before=False,
                    check_after=True,
                )

            if result.get("ok"):
                mark_token_uploaded_platform(fname, "sub2api")
                resolved_existing_id = result.get("existing_id")
                try:
                    normalized_id = int(resolved_existing_id) if resolved_existing_id is not None else existing_id
                except (TypeError, ValueError):
                    normalized_id = existing_id
                index_item = _build_sub2api_index_item(
                    email=email,
                    refresh_token=refresh_token,
                    account_id=normalized_id if isinstance(normalized_id, int) and normalized_id > 0 else None,
                )
                with identity_index_lock:
                    for key in local_keys:
                        identity_index[key] = index_item

            return {
                "index": index,
                "file": fname,
                "email": email,
                **result,
            }
        except Exception as e:
            return {"index": index, "file": fname, "ok": False, "error": str(e)}
        finally:
            for lock in reversed(acquired_locks):
                lock.release()

    for index, fname in enumerate(fnames):
        if "/" in fname or "\\" in fname or ".." in fname:
            results.append({"index": index, "file": fname, "ok": False, "error": "非法文件名"})

    pending = [
        (index, fname)
        for index, fname in enumerate(fnames)
        if fname and "/" not in fname and "\\" not in fname and ".." not in fname
    ]
    if pending:
        max_workers = max(1, min(SUB2API_BATCH_SYNC_WORKERS, len(pending)))
        if max_workers == 1:
            results.extend(_sync_one(index, fname) for index, fname in pending)
        else:
            with ThreadPoolExecutor(max_workers=max_workers) as executor:
                futures = [
                    executor.submit(_sync_one, index, fname)
                    for index, fname in pending
                ]
                for future in as_completed(futures):
                    results.append(future.result())

    results.sort(key=lambda item: int(item.get("index", 0)))
    any_remote_success = any(bool(item.get("ok")) for item in results)

    if any_remote_success:
        _clear_sub2api_accounts_cache()

    return {
        "total": len(results),
        "ok": sum(1 for item in results if item.get("ok") and not item.get("skipped")),
        "skipped": sum(1 for item in results if item.get("skipped")),
        "fail": sum(1 for item in results if not item.get("ok")),
        "results": results,
        "remote_total": len(remote_accounts),
    }


def _sub2api_item_matches_identity(item: Dict[str, Any], email: str, refresh_token: str) -> bool:
    email_norm = str(email or "").strip().lower()
    refresh_token_norm = str(refresh_token or "").strip()

    item_email, item_refresh_token = _sub2api_account_identity(item)
    name = str(item.get("name") or "").strip().lower()

    if refresh_token_norm and item_refresh_token and item_refresh_token == refresh_token_norm:
        return True
    if email_norm and (name == email_norm or item_email == email_norm):
        return True
    return False


def _find_existing_sub2api_account(
    base_url: str,
    bearer: str,
    email: str,
    refresh_token: str,
    max_pages: int = 8,
) -> Optional[Dict[str, Any]]:
    """
    在 Sub2Api 端查找是否已存在同一身份账号（email / refresh_token）。
    说明：
    - 主查 email（search 参数），并在返回项里再次精确匹配；
    - 若首次未命中，且提供了 refresh_token，会在有限页内继续扫一遍做 token 精确匹配。
    """
    from curl_cffi import requests as cffi_req

    url = base_url.rstrip("/") + "/api/v1/admin/accounts"
    email_norm = str(email or "").strip().lower()
    refresh_token_norm = str(refresh_token or "").strip()
    if not email_norm and not refresh_token_norm:
        return None

    headers = {
        "Authorization": f"Bearer {bearer}",
        "Accept": "application/json, text/plain, */*",
    }

    page_size = 100
    page = 1
    scanned_without_search = 0

    while page <= max_pages:
        params: Dict[str, Any] = {
            "page": page,
            "page_size": page_size,
            "platform": "openai",
            "type": "oauth",
        }
        if email_norm:
            params["search"] = email_norm

        try:
            resp = cffi_req.get(
                url,
                params=params,
                headers=headers,
                impersonate="chrome",
                timeout=15,
            )
            if resp.status_code != 200:
                return None
            body = resp.json()
        except Exception:
            return None

        data = _extract_sub2api_page_payload(body)
        items = data.get("items") if isinstance(data.get("items"), list) else []
        for item in items:
            if isinstance(item, dict) and _sub2api_item_matches_identity(item, email_norm, refresh_token_norm):
                return item

        total_raw = data.get("total")
        try:
            total = int(total_raw) if total_raw is not None else 0
        except (TypeError, ValueError):
            total = 0
        if len(items) < page_size or (total > 0 and page * page_size >= total):
            break
        page += 1

    # search=xxx 未命中时，额外做有限页扫描，用 refresh_token 做兜底精确匹配
    if refresh_token_norm:
        page = 1
        while page <= 3:
            params = {
                "page": page,
                "page_size": page_size,
                "platform": "openai",
                "type": "oauth",
            }
            try:
                resp = cffi_req.get(
                    url,
                    params=params,
                    headers=headers,
                    impersonate="chrome",
                    timeout=15,
                )
                if resp.status_code != 200:
                    return None
                body = resp.json()
            except Exception:
                return None

            data = _extract_sub2api_page_payload(body)
            items = data.get("items") if isinstance(data.get("items"), list) else []
            for item in items:
                if isinstance(item, dict) and _sub2api_item_matches_identity(item, "", refresh_token_norm):
                    return item

            scanned_without_search += len(items)
            if len(items) < page_size or scanned_without_search >= 300:
                break
            page += 1

    return None


def _push_account_api_with_dedupe(
    base_url: str,
    bearer: str,
    email: str,
    token_data: Dict[str, Any],
    group_ids: Optional[List[int]] = None,
    check_before: bool = True,
    check_after: bool = True,
) -> Dict[str, Any]:
    """
    上传前后做远端查重，避免重复创建同一账号。
    返回结构在 _push_account_api 基础上补充:
    - skipped: bool
    - reason: str
    - existing_id: Optional[int]
    """
    refresh_token = str(token_data.get("refresh_token") or "").strip()
    existing: Optional[Dict[str, Any]] = None

    if check_before:
        existing = _find_existing_sub2api_account(base_url, bearer, email, refresh_token)
        if existing is not None:
            existing_id = existing.get("id")
            existing_int = None
            try:
                existing_int = int(existing_id)
            except (TypeError, ValueError):
                existing_int = None
            if existing_int is not None and existing_int > 0:
                update_result = _update_sub2api_account_api(
                    base_url=base_url,
                    bearer=bearer,
                    account_id=existing_int,
                    email=email,
                    token_data=token_data,
                    group_ids=group_ids,
                )
                if update_result.get("ok"):
                    return {
                        "ok": True,
                        "status": int(update_result.get("status") or 200),
                        "body": "existing account updated",
                        "skipped": False,
                        "reason": "updated_existing_before_create",
                        "existing_id": existing_int,
                    }
                return {
                    "ok": False,
                    "status": int(update_result.get("status") or 0),
                    "body": "existing account update failed",
                    "skipped": False,
                    "reason": "exists_before_create_update_failed",
                    "existing_id": existing_int,
                    "update_status": int(update_result.get("status") or 0),
                    "update_body": str(update_result.get("body") or "")[:240],
                }
            return {
                "ok": True,
                "status": 200,
                "body": "account already exists",
                "skipped": True,
                "reason": "exists_before_create",
                "existing_id": existing_id,
            }

    result = _push_account_api(base_url, bearer, email, token_data, group_ids=group_ids)
    if result.get("ok"):
        result["skipped"] = False
        return result

    if check_after:
        existing = _find_existing_sub2api_account(base_url, bearer, email, refresh_token)
        if existing is not None:
            return {
                "ok": True,
                "status": int(result.get("status") or 200),
                "body": "request failed but account exists",
                "skipped": True,
                "reason": "exists_after_create",
                "existing_id": existing.get("id"),
            }

    result.setdefault("skipped", False)
    return result


@app.post("/api/tokens/sync-to-sub2api")
async def api_sync_tokens_to_sub2api(req: TokenSyncToSub2ApiRequest) -> Dict[str, Any]:
    """通过 HTTP API 将本地 Token 同步到 Sub2Api 平台"""
    def _sync_tokens() -> Dict[str, Any]:
        sync_result = _sync_token_files_to_sub2api(req.filenames)
        for result in sync_result.get("results") or []:
            email = str(result.get("email") or result.get("file") or "")
            if result.get("ok"):
                reason = str(result.get("reason") or "")
                if reason in {"updated_existing_before_create", "updated_existing_from_snapshot"}:
                    _state.broadcast({
                        "ts": datetime.now().strftime("%H:%M:%S"),
                        "level": "success",
                        "message": f"[API] {email}: 命中已存在账号并更新凭据 (id={result.get('existing_id', '-')})",
                        "step": "sync",
                    })
                elif result.get("skipped"):
                    _state.broadcast({
                        "ts": datetime.now().strftime("%H:%M:%S"),
                        "level": "success",
                        "message": f"[API] {email}: 同步成功",
                        "step": "sync",
                    })
                else:
                    _state.broadcast({
                        "ts": datetime.now().strftime("%H:%M:%S"),
                        "level": "success",
                        "message": f"[API] {email}: 导入成功",
                        "step": "sync",
                    })
            else:
                error_body = str(result.get("body") or result.get("error") or "")[:100]
                _state.broadcast({
                    "ts": datetime.now().strftime("%H:%M:%S"),
                    "level": "error",
                    "message": f"[API] {email}: 导入失败({result.get('status', 0)}) {error_body}",
                    "step": "sync",
                })
        return sync_result

    return await run_in_threadpool(_sync_tokens)


# ==========================================
# Mail 配置 API
# ==========================================


class MailConfigRequest(BaseModel):
    mail_providers: List[str] = Field(default_factory=list)
    mail_provider_configs: Dict[str, Dict[str, str]] = {}
    mail_strategy: str = "round_robin"


@app.get("/api/mail/config")
async def api_get_mail_config() -> Dict[str, Any]:
    cfg = _get_sync_config()
    raw_configs = cfg.get("mail_provider_configs") or {}
    safe_configs: Dict[str, Dict] = {}
    for pname, pcfg in raw_configs.items():
        sc = dict(pcfg)
        for secret_key in ("bearer_token", "api_key", "admin_password"):
            val = str(sc.get(secret_key, ""))
            if val:
                sc[f"{secret_key}_preview"] = (val[:8] + "...") if len(val) > 8 else val
                sc.pop(secret_key, None)
        safe_configs[pname] = sc

    return {
        "mail_providers": cfg.get("mail_providers", []),
        "mail_provider_configs": safe_configs,
        "mail_strategy": cfg.get("mail_strategy", "round_robin"),
    }


@app.post("/api/mail/config")
async def api_set_mail_config(req: MailConfigRequest) -> Dict[str, Any]:
    cfg = _get_sync_config()
    normalized_providers = [str(name).strip().lower() for name in req.mail_providers if str(name).strip()]
    if not normalized_providers:
        raise HTTPException(status_code=400, detail="请至少选择一个邮箱提供商")
    cfg["mail_providers"] = normalized_providers
    cfg["mail_strategy"] = req.mail_strategy or "round_robin"

    next_configs: Dict[str, Dict[str, str]] = {}
    for pname in normalized_providers:
        pcfg = req.mail_provider_configs.get(pname) or {}
        next_configs[pname] = {
            str(k): str(v).strip() for k, v in (pcfg or {}).items()
        }
    cfg["mail_provider_configs"] = next_configs

    _save_sync_config(cfg)
    return {"status": "saved"}


@app.post("/api/mail/test")
async def api_mail_test() -> Dict[str, Any]:
    try:
        cfg = _get_sync_config()
        router = MultiMailRouter(cfg)
        results = []
        proxy = str(cfg.get("proxy") or _state.current_proxy or "").strip()
        for pname, provider in router.providers():
            ok, msg = await run_in_threadpool(provider.test_connection, proxy)
            results.append({"provider": pname, "ok": ok, "message": msg})
        all_ok = all(r["ok"] for r in results)
        return {"ok": all_ok, "results": results, "message": "全部通过" if all_ok else "部分失败"}
    except Exception as e:
        return {"ok": False, "message": str(e)}


def _try_auto_register() -> None:
    """检查 Sub2Api 池状态，若不足则自动启动注册补充"""
    ts = datetime.now().strftime("%H:%M:%S")
    cfg = _get_sync_config()
    if not cfg.get("auto_register"):
        _state.broadcast({
            "ts": ts, "level": "info",
            "message": "[AUTO] 自动注册未开启，跳过（请勾选「池不足自动注册」并保存代理）",
            "step": "auto_register",
        })
        return
    proxy = str(cfg.get("proxy", "") or "").strip()
    proxy_pool_enabled = bool(cfg.get("proxy_pool_enabled", False))
    if not proxy and not proxy_pool_enabled:
        _state.broadcast({
            "ts": ts, "level": "warn",
            "message": "[AUTO] 跳过自动注册：未配置固定代理且代理池未启用，请先配置",
            "step": "auto_register",
        })
        return
    if not _state.can_start_new_task():
        _state.broadcast({
            "ts": ts, "level": "info",
            "message": f"[AUTO] 跳过自动注册：当前状态 {_state.status}",
            "step": "auto_register",
        })
        return
    sub2api_gap = 0
    api_error = False
    sm = _get_sub2api_maintainer(cfg)
    if sm and _is_auto_sync_enabled(cfg):
        try:
            sub2api_gap = sm.calculate_gap()
        except Exception as e:
            api_error = True
            _state.broadcast({
                "ts": ts, "level": "warn",
                "message": f"[AUTO] Sub2Api 池状态查询失败，稍后重试: {e}",
                "step": "auto_register",
            })
    elif sm:
        _state.broadcast({
            "ts": ts, "level": "info",
            "message": "[AUTO] Sub2Api 自动同步未开启，跳过自动补号",
            "step": "auto_register",
        })
    gap = sub2api_gap
    if api_error and gap <= 0:
        return
    if gap <= 0:
        _state.broadcast({
            "ts": ts, "level": "info",
            "message": f"[AUTO] 池已充足，无需补充注册（Sub2Api 缺口 {sub2api_gap}）",
            "step": "auto_register",
        })
        return
    multithread = bool(cfg.get("multithread", False))
    thread_count = int(cfg.get("thread_count", 3))
    try:
        _state.start_task(
            proxy,
            worker_count=thread_count if multithread else 1,
            target_count=gap,
            sub2api_target_count=sub2api_gap if sm and _is_auto_sync_enabled(cfg) else 0,
        )
        _state.broadcast({
            "ts": ts, "level": "success",
            "message": (
                f"[AUTO] 自动注册已启动：计划补充 {gap}，线程 "
                f"{thread_count if multithread else 1}，Sub2Api 缺口 {sub2api_gap}"
            ),
            "step": "auto_register",
        })
    except RuntimeError as e:
        _state.broadcast({
            "ts": ts, "level": "warn",
            "message": f"[AUTO] 自动注册启动失败：{e}",
            "step": "auto_register",
        })


# ==========================================
# Sub2Api 池维护 API & 自动维护
# ==========================================

_sub2api_auto_maintain_thread: Optional[threading.Thread] = None
_sub2api_auto_maintain_stop: Optional[threading.Event] = None
_sub2api_auto_maintain_ctl_lock = threading.Lock()
_sub2api_maintain_lock = threading.Lock()


class Sub2ApiDedupeRequest(BaseModel):
    dry_run: bool = True
    timeout: int = 20


class Sub2ApiAccountActionRequest(BaseModel):
    account_ids: List[int] = Field(default_factory=list)
    timeout: int = 30


class Sub2ApiExceptionHandleRequest(Sub2ApiAccountActionRequest):
    delete_unresolved: bool = True


@app.get("/api/sub2api/accounts")
async def api_sub2api_accounts(
    page: int = 1,
    page_size: int = 20,
    status: str = "all",
    keyword: str = "",
) -> Dict[str, Any]:
    sm = _get_sub2api_maintainer()
    if not sm:
        return {"configured": False, "error": "Sub2Api 未配置", "items": []}
    cfg = _get_sync_config()
    try:
        snapshot = await run_in_threadpool(
            lambda: _get_sub2api_accounts_inventory_snapshot(
                sm,
                cfg,
                page=page,
                page_size=page_size,
                status=status,
                keyword=keyword,
            )
        )
    except Exception as exc:
        cached_snapshot = _get_sub2api_accounts_cached_snapshot(cfg)
        snapshot = _build_sub2api_accounts_error_snapshot(
            (
                "Sub2Api 账号列表加载超时，已返回缓存数据"
                if cached_snapshot and isinstance(exc, httpx.TimeoutException)
                else "Sub2Api 账号列表加载超时，请稍后重试"
                if isinstance(exc, httpx.TimeoutException)
                else "Sub2Api 账号列表加载失败，已返回缓存数据"
                if cached_snapshot
                else f"Sub2Api 账号列表加载失败: {exc}"
            ),
            cached_snapshot=cached_snapshot,
        )
    if bool(snapshot.get("_paged")):
        paged = {
            "items": list(snapshot.get("items") or []),
            "page": max(1, int(snapshot.get("page") or page or 1)),
            "page_size": max(10, min(int(snapshot.get("page_size") or page_size or 20), 100)),
            "filtered_total": int(snapshot.get("filtered_total") or snapshot.get("total") or 0),
            "total_pages": max(1, int(snapshot.get("total_pages") or 1)),
        }
    else:
        filtered_items = _filter_sub2api_account_items(
            list(snapshot.get("items") or []),
            status=status,
            keyword=keyword,
        )
        paged = _paginate_sub2api_account_items(filtered_items, page=page, page_size=page_size)
    return {
        "configured": True,
        "total": int(snapshot.get("total", 0)),
        "error_count": int(snapshot.get("error_count", 0)),
        "duplicate_groups": int(snapshot.get("duplicate_groups", 0)),
        "duplicate_accounts": int(snapshot.get("duplicate_accounts", 0)),
        "items": paged["items"],
        "page": paged["page"],
        "page_size": paged["page_size"],
        "filtered_total": paged["filtered_total"],
        "total_pages": paged["total_pages"],
        "status": str(status or "all"),
        "keyword": str(keyword or ""),
        "error": str(snapshot.get("error") or ""),
        "stale": bool(snapshot.get("stale", False)),
    }


@app.post("/api/sub2api/accounts/probe")
async def api_sub2api_accounts_probe(req: Sub2ApiAccountActionRequest) -> Dict[str, Any]:
    sm = _get_sub2api_maintainer()
    if not sm:
        raise HTTPException(status_code=400, detail="Sub2Api 未配置")
    if not req.account_ids:
        raise HTTPException(status_code=400, detail="请先选择至少一个账号")
    if not _sub2api_maintain_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Sub2Api 账号任务已在执行中")
    try:
        timeout = max(5, int(req.timeout))
        _state.broadcast({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "level": "info",
            "message": f"[Sub2Api] 开始账号测活：目标 {len(req.account_ids)}，超时 {timeout}s",
            "step": "sub2api_accounts_probe",
        })
        result = await run_in_threadpool(
            lambda: sm.probe_accounts(req.account_ids, timeout=timeout)
        )
        _state.broadcast({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "level": "info",
            "message": (
                f"[Sub2Api] 账号测活: 请求 {result.get('requested', 0)}, "
                f"刷新成功 {result.get('refreshed_ok', 0)}, "
                f"恢复 {result.get('recovered', 0)}, "
                f"仍异常 {result.get('still_abnormal', 0)}"
            ),
            "step": "sub2api_accounts_probe",
        })
        _clear_sub2api_accounts_cache()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _sub2api_maintain_lock.release()


@app.post("/api/sub2api/accounts/delete")
async def api_sub2api_accounts_delete(req: Sub2ApiAccountActionRequest) -> Dict[str, Any]:
    sm = _get_sub2api_maintainer()
    if not sm:
        raise HTTPException(status_code=400, detail="Sub2Api 未配置")
    if not req.account_ids:
        raise HTTPException(status_code=400, detail="请先选择至少一个账号")
    if not _sub2api_maintain_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Sub2Api 账号任务已在执行中")
    try:
        timeout = max(5, int(req.timeout))
        _state.broadcast({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "level": "info",
            "message": f"[Sub2Api] 开始批量删除：目标 {len(req.account_ids)}，超时 {timeout}s",
            "step": "sub2api_accounts_delete",
        })
        result = await run_in_threadpool(
            lambda: sm.delete_accounts_batch(req.account_ids, timeout=timeout)
        )
        _state.broadcast({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "level": "info",
            "message": (
                f"[Sub2Api] 批量删除: 请求 {result.get('requested', 0)}, "
                f"删除成功 {result.get('deleted_ok', 0)}, "
                f"删除失败 {result.get('deleted_fail', 0)}"
            ),
            "step": "sub2api_accounts_delete",
        })
        _clear_sub2api_accounts_cache()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _sub2api_maintain_lock.release()


@app.post("/api/sub2api/accounts/handle-exception")
async def api_sub2api_accounts_handle_exception(req: Sub2ApiExceptionHandleRequest) -> Dict[str, Any]:
    sm = _get_sub2api_maintainer()
    if not sm:
        raise HTTPException(status_code=400, detail="Sub2Api 未配置")
    if not _sub2api_maintain_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Sub2Api 账号任务已在执行中")
    try:
        timeout = max(5, int(req.timeout))
        target_count = len(req.account_ids or [])
        _state.broadcast({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "level": "info",
            "message": (
                f"[Sub2Api] 开始异常处理：目标 {target_count or '全部异常账号'}，"
                f"删除未恢复={bool(req.delete_unresolved)}，超时 {timeout}s"
            ),
            "step": "sub2api_accounts_exception",
        })
        result = await run_in_threadpool(
            lambda: sm.handle_exception_accounts(
                req.account_ids or None,
                timeout=timeout,
                delete_unresolved=bool(req.delete_unresolved),
            )
        )
        _state.broadcast({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "level": "info",
            "message": (
                f"[Sub2Api] 异常处理: 目标 {result.get('targeted', 0)}, "
                f"恢复 {result.get('recovered', 0)}, "
                f"删除 {result.get('deleted_ok', 0)}(失败 {result.get('deleted_fail', 0)})"
            ),
            "step": "sub2api_accounts_exception",
        })
        _clear_sub2api_accounts_cache()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _sub2api_maintain_lock.release()


@app.get("/api/sub2api/pool/status")
async def api_sub2api_pool_status() -> Dict[str, Any]:
    sm = _get_sub2api_maintainer()
    if not sm:
        return {"configured": False, "error": "Sub2Api 未配置"}
    status = await run_in_threadpool(sm.get_pool_status)
    status["configured"] = True
    return status


@app.post("/api/sub2api/pool/check")
async def api_sub2api_pool_check() -> Dict[str, Any]:
    sm = _get_sub2api_maintainer()
    if not sm:
        raise HTTPException(status_code=400, detail="Sub2Api 未配置")
    result = await run_in_threadpool(sm.test_connection)
    return result


@app.post("/api/sub2api/pool/maintain")
async def api_sub2api_pool_maintain() -> Dict[str, Any]:
    sm = _get_sub2api_maintainer()
    if not sm:
        raise HTTPException(status_code=400, detail="Sub2Api 未配置")
    if not _sub2api_maintain_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Sub2Api 维护任务已在执行中")
    try:
        cfg = _get_sync_config()
        actions = _get_sub2api_maintain_actions(cfg)
        def _broadcast_before_status(status: Dict[str, Any]) -> None:
            _state.broadcast({
                "ts": datetime.now().strftime("%H:%M:%S"),
                "level": "info",
                "message": (
                    f"[Sub2Api] 开始维护({_describe_sub2api_maintain_actions(actions)})："
                    f"{_format_sub2api_pool_status_summary(status)}"
                ),
                "step": "sub2api_maintain",
            })

        result = await run_in_threadpool(
            lambda: sm.probe_and_clean_sync(
                actions=actions,
                before_status_callback=_broadcast_before_status,
            )
        )
        after_status = result.get("after_status")
        if not isinstance(after_status, dict):
            after_status = await run_in_threadpool(sm.get_pool_status)
        _state.broadcast({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "level": "info",
            "message": (
                f"{_format_sub2api_maintain_result_message(result)}；"
                f"{_format_sub2api_pool_status_summary(after_status)}"
            ),
            "step": "sub2api_maintain",
        })
        _clear_sub2api_accounts_cache()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _sub2api_maintain_lock.release()


@app.post("/api/sub2api/pool/dedupe")
async def api_sub2api_pool_dedupe(req: Sub2ApiDedupeRequest) -> Dict[str, Any]:
    sm = _get_sub2api_maintainer()
    if not sm:
        raise HTTPException(status_code=400, detail="Sub2Api 未配置")
    if not _sub2api_maintain_lock.acquire(blocking=False):
        raise HTTPException(status_code=409, detail="Sub2Api 维护任务已在执行中")
    try:
        timeout = max(5, int(req.timeout))
        dry_run = bool(req.dry_run)
        _state.broadcast({
            "ts": datetime.now().strftime("%H:%M:%S"),
            "level": "info",
            "message": f"[Sub2Api] 开始重复账号{'预检' if dry_run else '清理'}：超时 {timeout}s",
            "step": "sub2api_dedupe",
        })
        result = await run_in_threadpool(
            lambda: sm.dedupe_duplicate_accounts(timeout=timeout, dry_run=dry_run)
        )
        if dry_run:
            _state.broadcast({
                "ts": datetime.now().strftime("%H:%M:%S"),
                "level": "info",
                "message": (
                    f"[Sub2Api] 重复预检完成: 重复组 {result.get('duplicate_groups', 0)}, "
                    f"可删 {result.get('to_delete', 0)}"
                ),
                "step": "sub2api_dedupe",
            })
        else:
            _state.broadcast({
                "ts": datetime.now().strftime("%H:%M:%S"),
                "level": "info",
                "message": (
                    f"[Sub2Api] 重复清理完成: 删除成功 {result.get('deleted_ok', 0)}, "
                    f"删除失败 {result.get('deleted_fail', 0)}"
                ),
                "step": "sub2api_dedupe",
            })
        _clear_sub2api_accounts_cache()
        return result
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))
    finally:
        _sub2api_maintain_lock.release()


def _start_sub2api_auto_maintain() -> None:
    global _sub2api_auto_maintain_thread, _sub2api_auto_maintain_stop
    cfg = _get_sync_config()
    interval = max(5, int(cfg.get("sub2api_maintain_interval_minutes", 30))) * 60
    with _sub2api_auto_maintain_ctl_lock:
        if _sub2api_auto_maintain_thread and _sub2api_auto_maintain_thread.is_alive():
            return
        stop_event = threading.Event()
        _sub2api_auto_maintain_stop = stop_event

    def _loop(local_stop: threading.Event) -> None:
        while not local_stop.is_set():
            sm = _get_sub2api_maintainer()
            if sm:
                current_cfg = _get_sync_config()
                if not _sub2api_maintain_lock.acquire(blocking=False):
                    _state.broadcast({
                        "ts": datetime.now().strftime("%H:%M:%S"),
                        "level": "warn",
                        "message": "[Sub2Api] 跳过自动维护：已有维护任务在执行",
                        "step": "sub2api_auto",
                    })
                else:
                    try:
                        actions = _get_sub2api_maintain_actions(current_cfg)
                        def _broadcast_before_status(status: Dict[str, Any]) -> None:
                            _state.broadcast({
                                "ts": datetime.now().strftime("%H:%M:%S"),
                                "level": "info",
                                "message": (
                                    f"[Sub2Api] 自动维护启动({_describe_sub2api_maintain_actions(actions)})："
                                    f"{_format_sub2api_pool_status_summary(status)}"
                                ),
                                "step": "sub2api_auto",
                            })

                        result = sm.probe_and_clean_sync(
                            actions=actions,
                            before_status_callback=_broadcast_before_status,
                        )
                        after_status = result.get("after_status")
                        if not isinstance(after_status, dict):
                            after_status = sm.get_pool_status()
                        _state.broadcast({
                            "ts": datetime.now().strftime("%H:%M:%S"),
                            "level": "info",
                            "message": (
                                f"{_format_sub2api_maintain_result_message(result, auto=True)}；"
                                f"{_format_sub2api_pool_status_summary(after_status)}"
                            ),
                            "step": "sub2api_auto",
                        })
                        _clear_sub2api_accounts_cache()
                    except Exception as e:
                        _state.broadcast({
                            "ts": datetime.now().strftime("%H:%M:%S"),
                            "level": "error",
                            "message": f"[Sub2Api] 自动维护异常: {e}",
                            "step": "sub2api_auto",
                        })
                    finally:
                        _sub2api_maintain_lock.release()
                    if _as_bool(current_cfg.get("auto_register", False), default=False):
                        _try_auto_register()
                    else:
                        _state.broadcast({
                            "ts": datetime.now().strftime("%H:%M:%S"),
                            "level": "info",
                            "message": "[AUTO] 自动维护完成，但池不足自动注册未开启，跳过补号",
                            "step": "auto_register",
                        })
            local_stop.wait(interval)

    thread = threading.Thread(target=_loop, args=(stop_event,), daemon=True)
    with _sub2api_auto_maintain_ctl_lock:
        _sub2api_auto_maintain_thread = thread
    thread.start()


def _stop_sub2api_auto_maintain() -> None:
    global _sub2api_auto_maintain_thread, _sub2api_auto_maintain_stop
    with _sub2api_auto_maintain_ctl_lock:
        stop_event = _sub2api_auto_maintain_stop
        thread = _sub2api_auto_maintain_thread
    if stop_event:
        stop_event.set()
    if thread and thread.is_alive():
        thread.join(timeout=5)
    with _sub2api_auto_maintain_ctl_lock:
        if _sub2api_auto_maintain_thread is thread and (thread is None or not thread.is_alive()):
            _sub2api_auto_maintain_thread = None
            _sub2api_auto_maintain_stop = None

# 挂载静态文件
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")





