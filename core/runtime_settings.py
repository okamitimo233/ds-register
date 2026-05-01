from __future__ import annotations

import json
import os
from dataclasses import dataclass
from pathlib import Path

RUNTIME_CONFIG_FILE = Path(__file__).resolve().parent.parent / "data" / "sync_config.json"


def _as_int_env(name: str, default: int) -> int:
    value = os.getenv(name)
    if value is None:
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _coerce_bool(value: object, default: bool) -> bool:
    if isinstance(value, bool):
        return value
    if value is None:
        return default
    if isinstance(value, (int, float)):
        return bool(value)
    text = str(value).strip().lower()
    if text in {"1", "true", "yes", "on"}:
        return True
    if text in {"0", "false", "no", "off", ""}:
        return False
    return default


def _load_runtime_config() -> dict:
    if RUNTIME_CONFIG_FILE.exists():
        try:
            return json.loads(RUNTIME_CONFIG_FILE.read_text(encoding="utf-8"))
        except Exception:
            return {}
    return {}


def _get_str_setting(env_name: str, config: dict, config_key: str, default: str) -> str:
    env_value = os.getenv(env_name)
    if env_value is not None:
        return str(env_value).strip() or default
    return str(config.get(config_key, default) or "").strip() or default


def _get_bool_setting(env_name: str, config: dict, config_key: str, default: bool) -> bool:
    env_value = os.getenv(env_name)
    if env_value is not None:
        return _coerce_bool(env_value, default)
    return _coerce_bool(config.get(config_key, default), default)


def _get_int_setting(
    env_name: str,
    config: dict,
    config_key: str,
    default: int,
    *,
    min_value: int,
    max_value: int,
) -> int:
    env_value = os.getenv(env_name)
    if env_value is not None:
        return max(min_value, min(_as_int_env(env_name, default), max_value))
    try:
        value = int(config.get(config_key, default) or default)
    except (TypeError, ValueError):
        value = default
    return max(min_value, min(value, max_value))


@dataclass(frozen=True)
class RuntimeSettings:
    service_name: str
    process_name: str
    listen_host: str
    listen_port: int
    reload_enabled: bool
    debug_logging: bool
    anonymous_mode: bool
    log_dir: str
    log_level: str
    file_log_level: str
    log_rotation: str
    log_retention_days: int
    app_target: str

    @property
    def listen_address(self) -> str:
        return f"{self.listen_host}:{self.listen_port}"

    @property
    def granian_log_level(self) -> str:
        return "info" if self.debug_logging else "warning"


def load_runtime_settings() -> RuntimeSettings:
    config = _load_runtime_config()
    debug_logging = _get_bool_setting("OPENAI_POOL_DEBUG_LOGGING", config, "debug_logging", False)
    anonymous_mode = _get_bool_setting("OPENAI_POOL_ANONYMOUS_MODE", config, "anonymous_mode", False)
    service_name = _get_str_setting(
        "OPENAI_POOL_SERVICE_NAME",
        config,
        "service_name",
        "OpenAI Pool Orchestrator",
    )
    process_name = _get_str_setting(
        "OPENAI_POOL_PROCESS_NAME",
        config,
        "process_name",
        "openai-pool",
    )
    listen_host = _get_str_setting(
        "OPENAI_POOL_LISTEN_HOST",
        config,
        "listen_host",
        "0.0.0.0",
    )
    listen_port = _get_int_setting(
        "OPENAI_POOL_LISTEN_PORT",
        config,
        "listen_port",
        18421,
        min_value=1,
        max_value=65535,
    )
    log_dir = _get_str_setting(
        "OPENAI_POOL_LOG_DIR",
        config,
        "log_dir",
        str(RUNTIME_CONFIG_FILE.parent / "logs"),
    )
    log_level = _get_str_setting(
        "OPENAI_POOL_LOG_LEVEL",
        config,
        "log_level",
        "DEBUG" if debug_logging else "INFO",
    ).upper()
    file_log_level = _get_str_setting(
        "OPENAI_POOL_FILE_LOG_LEVEL",
        config,
        "file_log_level",
        "DEBUG",
    ).upper()
    log_rotation = _get_str_setting(
        "OPENAI_POOL_LOG_ROTATION",
        config,
        "log_rotation",
        "1 day",
    )
    log_retention_days = _get_int_setting(
        "OPENAI_POOL_LOG_RETENTION_DAYS",
        config,
        "log_retention_days",
        7,
        min_value=1,
        max_value=3650,
    )

    return RuntimeSettings(
        service_name=service_name,
        process_name=process_name,
        listen_host=listen_host,
        listen_port=listen_port,
        reload_enabled=_get_bool_setting("OPENAI_POOL_RELOAD", config, "reload_enabled", False),
        debug_logging=debug_logging,
        anonymous_mode=anonymous_mode,
        log_dir=log_dir,
        log_level=log_level,
        file_log_level=file_log_level,
        log_rotation=log_rotation,
        log_retention_days=log_retention_days,
        app_target=_get_str_setting("OPENAI_POOL_APP_TARGET", config, "app_target", "core.server:app"),
    )
