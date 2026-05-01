from __future__ import annotations

import logging
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

from loguru import logger as _base_logger

app_logger = None
_LOGGING_CONFIGURED = False
_RUNTIME_LOGGING_CONFIG: dict[str, Any] = {
    "debug_logging": None,
    "anonymous_mode": None,
    "log_level": None,
    "file_log_level": None,
    "log_dir": None,
    "log_rotation": None,
    "log_retention_days": None,
}

_EMAIL_RE = re.compile(r"\b([A-Za-z0-9._%+-]{1,64})@([A-Za-z0-9.-]+\.[A-Za-z]{2,})\b")
_SECRET_RE = re.compile(
    r"(?i)\b("
    r"access_token|refresh_token|id_token|bearer_token|token|api_key|password|jwt"
    r")(\s*[:=]\s*)([^\s,;]+)"
)
_QUERY_SECRET_RE = re.compile(r"(?i)([?&](?:token|api_key|jwt)=)([^&\s]+)")
_JWT_RE = re.compile(r"\beyJ[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\.[A-Za-z0-9_-]+\b")
_EVENT_LEVEL_MAP = {
    "debug": "DEBUG",
    "info": "INFO",
    "success": "SUCCESS",
    "warn": "WARNING",
    "warning": "WARNING",
    "error": "ERROR",
    "critical": "CRITICAL",
}
_VALID_LOG_LEVELS = {"TRACE", "DEBUG", "INFO", "SUCCESS", "WARNING", "ERROR", "CRITICAL"}


def _as_bool_env(name: str, default: bool = False) -> bool:
    value = os.getenv(name)
    if value is None:
        return default
    return str(value).strip().lower() in {"1", "true", "yes", "on"}


def _normalize_log_level(value: Any, default: str) -> str:
    text = str(value or "").strip().upper() or default
    return text if text in _VALID_LOG_LEVELS else default


def _normalize_retention_days(value: Any, default: int) -> int:
    try:
        return max(1, int(value))
    except (TypeError, ValueError):
        return default


def set_runtime_logging_flags(
    *,
    debug_logging: Optional[bool] = None,
    anonymous_mode: Optional[bool] = None,
) -> None:
    set_runtime_logging_config(
        debug_logging=debug_logging,
        anonymous_mode=anonymous_mode,
    )


def set_runtime_logging_config(
    *,
    debug_logging: Optional[bool] = None,
    anonymous_mode: Optional[bool] = None,
    log_level: Optional[str] = None,
    file_log_level: Optional[str] = None,
    log_dir: Optional[str | Path] = None,
    log_rotation: Optional[str] = None,
    log_retention_days: Optional[int] = None,
) -> None:
    if debug_logging is not None:
        _RUNTIME_LOGGING_CONFIG["debug_logging"] = bool(debug_logging)
    if anonymous_mode is not None:
        _RUNTIME_LOGGING_CONFIG["anonymous_mode"] = bool(anonymous_mode)
    if log_level is not None:
        _RUNTIME_LOGGING_CONFIG["log_level"] = _normalize_log_level(log_level, "INFO")
    if file_log_level is not None:
        _RUNTIME_LOGGING_CONFIG["file_log_level"] = _normalize_log_level(file_log_level, "DEBUG")
    if log_dir is not None:
        _RUNTIME_LOGGING_CONFIG["log_dir"] = str(log_dir).strip() or None
    if log_rotation is not None:
        _RUNTIME_LOGGING_CONFIG["log_rotation"] = str(log_rotation).strip() or None
    if log_retention_days is not None:
        _RUNTIME_LOGGING_CONFIG["log_retention_days"] = _normalize_retention_days(
            log_retention_days,
            7,
        )


def _is_debug_logging_enabled() -> bool:
    value = _RUNTIME_LOGGING_CONFIG.get("debug_logging")
    if value is not None:
        return bool(value)
    return _as_bool_env("OPENAI_POOL_DEBUG_LOGGING", default=False)


def _is_anonymous_mode_enabled() -> bool:
    value = _RUNTIME_LOGGING_CONFIG.get("anonymous_mode")
    if value is not None:
        return bool(value)
    return _as_bool_env("OPENAI_POOL_ANONYMOUS_MODE", default=False)


def _resolve_log_level(debug_mode: bool, override: Optional[str] = None) -> str:
    if override:
        return _normalize_log_level(override, "DEBUG" if debug_mode else "INFO")
    runtime_value = _RUNTIME_LOGGING_CONFIG.get("log_level")
    if runtime_value:
        return _normalize_log_level(runtime_value, "DEBUG" if debug_mode else "INFO")
    return _normalize_log_level(
        os.getenv("OPENAI_POOL_LOG_LEVEL", "DEBUG" if debug_mode else "INFO"),
        "DEBUG" if debug_mode else "INFO",
    )


def _resolve_file_log_level(console_level: str, override: Optional[str] = None) -> str:
    if override:
        return _normalize_log_level(override, console_level)
    runtime_value = _RUNTIME_LOGGING_CONFIG.get("file_log_level")
    if runtime_value:
        return _normalize_log_level(runtime_value, console_level)
    return _normalize_log_level(os.getenv("OPENAI_POOL_FILE_LOG_LEVEL", console_level), console_level)


def _resolve_log_dir(default: str | Path) -> Path:
    runtime_value = _RUNTIME_LOGGING_CONFIG.get("log_dir")
    raw = runtime_value or os.getenv("OPENAI_POOL_LOG_DIR") or default
    return Path(raw).resolve()


def _resolve_log_rotation(default: str) -> str:
    runtime_value = _RUNTIME_LOGGING_CONFIG.get("log_rotation")
    return str(runtime_value or os.getenv("OPENAI_POOL_LOG_ROTATION") or default).strip() or default


def _resolve_log_retention_days(default: int) -> int:
    runtime_value = _RUNTIME_LOGGING_CONFIG.get("log_retention_days")
    if runtime_value is not None:
        return _normalize_retention_days(runtime_value, default)
    env_value = os.getenv("OPENAI_POOL_LOG_RETENTION_DAYS")
    if env_value is not None:
        return _normalize_retention_days(env_value, default)
    return default


def _mask_secret(value: str) -> str:
    text = str(value or "").strip()
    if not text:
        return "***"
    if len(text) <= 6:
        return "***"
    return text[:2] + "***" + text[-2:]


def _mask_email(match: re.Match[str]) -> str:
    local = match.group(1)
    domain = match.group(2)
    if len(local) <= 2:
        masked_local = local[:1] + "***"
    else:
        masked_local = local[:2] + "***" + local[-1:]
    return f"{masked_local}@{domain}"


def _mask_secret_pair(match: re.Match[str]) -> str:
    return f"{match.group(1)}{match.group(2)}{_mask_secret(match.group(3))}"


def _mask_query_secret(match: re.Match[str]) -> str:
    return f"{match.group(1)}{_mask_secret(match.group(2))}"


def sanitize_log_text(message: str) -> str:
    text = str(message or "")
    if not text:
        return text
    text = _EMAIL_RE.sub(_mask_email, text)
    text = _SECRET_RE.sub(_mask_secret_pair, text)
    text = _QUERY_SECRET_RE.sub(_mask_query_secret, text)
    text = _JWT_RE.sub(lambda match: _mask_secret(match.group(0)), text)
    return text


def _patch_record(record: dict[str, Any]) -> None:
    extra = record["extra"]
    component = str(extra.get("component") or record["name"] or "app").strip()
    request_id = str(extra.get("request_id") or "").strip()
    run_id = str(extra.get("run_id") or "").strip()
    step = str(extra.get("step") or "").strip()
    worker_label = str(extra.get("worker_label") or "").strip()
    worker_id = extra.get("worker_id")
    provider_name = str(
        extra.get("provider_name") or extra.get("provider") or extra.get("mail_provider") or ""
    ).strip()
    method = str(extra.get("method") or "").strip().upper()
    path = str(extra.get("path") or "").strip()
    client_ip = str(extra.get("client_ip") or "").strip()
    attempt = extra.get("attempt")

    context_parts = []
    if request_id:
        context_parts.append(f"req={request_id}")
    if run_id:
        context_parts.append(f"run={run_id}")
    if method and path:
        context_parts.append(f"{method} {path}")
    elif path:
        context_parts.append(path)
    if client_ip:
        context_parts.append(f"ip={client_ip}")
    if step:
        context_parts.append(f"step={step}")
    if worker_label:
        context_parts.append(f"worker={worker_label}")
    elif worker_id not in (None, ""):
        context_parts.append(f"worker={worker_id}")
    if provider_name:
        context_parts.append(f"provider={provider_name}")
    if attempt not in (None, ""):
        context_parts.append(f"attempt={attempt}")

    extra["component"] = component
    extra["context"] = f" [{' '.join(context_parts)}]" if context_parts else ""
    if _is_anonymous_mode_enabled():
        record["message"] = sanitize_log_text(str(record["message"]))


logger = _base_logger.patch(_patch_record)


class InterceptHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            level: str | int = logger.level(record.levelname).name
        except ValueError:
            level = record.levelno

        frame = logging.currentframe()
        depth = 2
        logging_file = getattr(logging, "__file__", None)
        while frame is not None and logging_file and frame.f_code.co_filename == logging_file:
            frame = frame.f_back
            depth += 1

        logger.bind(component=record.name).opt(depth=depth, exception=record.exc_info).log(
            level,
            record.getMessage(),
        )


def _console_format(debug_mode: bool) -> str:
    if debug_mode:
        return (
            "<green>{time:YYYY-MM-DD HH:mm:ss}</green> | "
            "<level>{level: <8}</level> | "
            "<cyan>{name}</cyan>:<cyan>{function}</cyan>:<cyan>{line}</cyan> | "
            "<cyan>{extra[component]}</cyan><cyan>{extra[context]}</cyan> | "
            "<level>{message}</level>{exception}"
        )
    return (
        "<green>{time:HH:mm:ss}</green> | "
        "<level>{level: <8}</level> | "
        "<cyan>{extra[component]}</cyan><cyan>{extra[context]}</cyan> | "
        "<level>{message}</level>{exception}"
    )


def _file_format() -> str:
    return (
        "{time:YYYY-MM-DD HH:mm:ss.SSS} | {level: <8} | "
        "{name}:{function}:{line} | {extra[component]}{extra[context]} | {message}{exception}"
    )


def setup_logger(
    log_dir: str | Path,
    log_retention_days: int = 7,
    log_rotation: str = "1 day",
    debug_mode: Optional[bool] = None,
    log_level: Optional[str] = None,
    file_log_level: Optional[str] = None,
    force: bool = False,
) -> Any:
    global app_logger, _LOGGING_CONFIGURED

    if _LOGGING_CONFIGURED and not force and app_logger is not None:
        return app_logger

    enabled_debug_mode = _is_debug_logging_enabled() if debug_mode is None else bool(debug_mode)
    resolved_log_level = _resolve_log_level(enabled_debug_mode, log_level)
    resolved_log_dir = _resolve_log_dir(log_dir)
    resolved_rotation = _resolve_log_rotation(log_rotation)
    resolved_retention_days = _resolve_log_retention_days(log_retention_days)

    _base_logger.remove()
    _base_logger.add(
        sys.stderr,
        level=resolved_log_level,
        format=_console_format(enabled_debug_mode),
        colorize=getattr(sys.stderr, "isatty", lambda: False)(),
        enqueue=True,
        catch=True,
        backtrace=False,
        diagnose=False,
    )

    if enabled_debug_mode:
        try:
            resolved_log_dir.mkdir(parents=True, exist_ok=True)
            resolved_file_log_level = _resolve_file_log_level(resolved_log_level, file_log_level)
            log_file = resolved_log_dir / "{time:YYYY-MM-DD}.log"
            _base_logger.add(
                str(log_file),
                level=resolved_file_log_level,
                format=_file_format(),
                rotation=resolved_rotation,
                retention=os.getenv("OPENAI_POOL_LOG_RETENTION", f"{resolved_retention_days} days"),
                encoding="utf-8",
                compression="zip",
                enqueue=True,
                catch=True,
                backtrace=False,
                diagnose=False,
            )
        except (PermissionError, OSError) as exc:
            logger.warning("无法创建日志文件({})，将仅使用控制台输出", exc)

    logging.basicConfig(handlers=[InterceptHandler()], level=0, force=True)
    logging.captureWarnings(True)

    for logger_name in (
        "asyncio",
        "fastapi",
        "granian",
        "granian.access",
        "granian.error",
    ):
        std_logger = logging.getLogger(logger_name)
        std_logger.handlers = [InterceptHandler()]
        std_logger.propagate = False

    app_logger = logger
    _LOGGING_CONFIGURED = True
    return app_logger


def setup_logging(log_dir: str | Path, level: Optional[str] = None, force: bool = False) -> None:
    debug_mode = None
    if level is not None:
        debug_mode = str(level).strip().upper() == "DEBUG"
    setup_logger(log_dir=log_dir, debug_mode=debug_mode, force=force)


def get_logger(component: Optional[str] = None, **extra: Any) -> Any:
    global app_logger

    if app_logger is None:
        setup_logger(log_dir=Path("logs"))

    bound_extra = dict(extra)
    if component:
        bound_extra["component"] = component
    return app_logger.bind(**bound_extra) if bound_extra else app_logger


def log_event(level: str, message: str, *, component: Optional[str] = None, **extra: Any) -> None:
    level_name = _EVENT_LEVEL_MAP.get(str(level or "").strip().lower(), "INFO")
    get_logger(component, **extra).log(level_name, message)
