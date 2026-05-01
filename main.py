#!/usr/bin/env python3
"""
项目主入口。

用法:
    python main.py              # 启动 Web 服务
    python main.py --cli        # CLI 模式（单次注册）
    python main.py --cli --proxy http://127.0.0.1:7890
"""

from __future__ import annotations

import argparse
import asyncio
import os
import signal
import sys
from typing import Optional, Sequence

GRACEFUL_SHUTDOWN_TIMEOUT = 5


def _shutdown_signal_values() -> list[int]:
    signals = [signal.SIGINT, signal.SIGTERM]
    if hasattr(signal, "SIGBREAK"):
        signals.append(signal.SIGBREAK)
    return signals


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(description="OpenAI Pool Orchestrator 入口")
    parser.add_argument("--cli", action="store_true", help="运行 CLI 注册模式")
    parser.add_argument("--target", choices=["openai", "deepseek"], default="openai", help="注册目标平台 (openai 或 deepseek)")
    parser.add_argument("--debug", dest="debug_logging", action="store_true", help="开启 DEBUG 日志")
    parser.add_argument("--no-debug", dest="debug_logging", action="store_false", help="关闭 DEBUG 日志")
    parser.add_argument("--reload", dest="reload_enabled", action="store_true", help="开启 Granian 热重载")
    parser.add_argument("--no-reload", dest="reload_enabled", action="store_false", help="关闭 Granian 热重载")
    parser.add_argument("--anonymous", dest="anonymous_mode", action="store_true", help="开启匿名日志脱敏")
    parser.add_argument("--no-anonymous", dest="anonymous_mode", action="store_false", help="关闭匿名日志脱敏")
    parser.add_argument("--host", dest="listen_host", default=None, help="监听地址")
    parser.add_argument("--port", dest="listen_port", type=int, default=None, help="监听端口")
    parser.add_argument("--service-name", dest="service_name", default=None, help="服务显示名称")
    parser.set_defaults(debug_logging=None, reload_enabled=None, anonymous_mode=None)
    return parser


def _apply_runtime_overrides(args: argparse.Namespace) -> None:
    if args.debug_logging is not None:
        os.environ["OPENAI_POOL_DEBUG_LOGGING"] = "1" if args.debug_logging else "0"
    if args.reload_enabled is not None:
        os.environ["OPENAI_POOL_RELOAD"] = "1" if args.reload_enabled else "0"
    if args.anonymous_mode is not None:
        os.environ["OPENAI_POOL_ANONYMOUS_MODE"] = "1" if args.anonymous_mode else "0"
    if args.listen_host:
        os.environ["OPENAI_POOL_LISTEN_HOST"] = str(args.listen_host).strip()
    if args.listen_port is not None:
        os.environ["OPENAI_POOL_LISTEN_PORT"] = str(args.listen_port)
    if args.service_name:
        os.environ["OPENAI_POOL_SERVICE_NAME"] = str(args.service_name).strip()


def run_cli(argv: Optional[Sequence[str]] = None) -> None:
    cli_args = list(argv or [])
    sys.argv = [sys.argv[0], *cli_args]

    from core.logger import get_logger, set_runtime_logging_config, setup_logger
    from core.register import main as cli_main
    from core.runtime_settings import load_runtime_settings

    settings = load_runtime_settings()
    set_runtime_logging_config(
        debug_logging=settings.debug_logging,
        anonymous_mode=settings.anonymous_mode,
        log_level=settings.log_level,
        file_log_level=settings.file_log_level,
        log_dir=settings.log_dir,
        log_rotation=settings.log_rotation,
        log_retention_days=settings.log_retention_days,
    )
    setup_logger(
        settings.log_dir,
        debug_mode=settings.debug_logging,
        log_level=settings.log_level,
        file_log_level=settings.file_log_level,
        log_rotation=settings.log_rotation,
        log_retention_days=settings.log_retention_days,
        force=True,
    )
    parser = _build_parser()
    args, remaining = parser.parse_known_args(cli_args)

    # Pass target to register module
    if hasattr(args, 'target'):
        import os
        os.environ['DS_REGISTER_TARGET'] = args.target

    get_logger(__name__).debug("切换到 CLI 模式: argv={}", cli_args)
    cli_main()


def _run_windows_embedded_server(settings, logger) -> None:
    from granian.server.embed import Server as EmbeddedGranian

    from core.server import app, request_service_shutdown

    if settings.reload_enabled:
        logger.warning("Windows 下嵌入式 Granian 不支持 reload，已忽略该配置")

    server = EmbeddedGranian(
        app,
        interface="asgi",
        address=settings.listen_host,
        port=settings.listen_port,
        log_level=settings.granian_log_level,
    )
    previous_handlers: dict[int, object] = {}
    stopping = {"value": False}

    def _signal_handler(signum, frame) -> None:  # noqa: ARG001
        if stopping["value"]:
            raise KeyboardInterrupt
        stopping["value"] = True
        logger.info("🛑 收到中断信号，正在关闭服务...")
        try:
            request_service_shutdown(wait_for_idle=False)
        except Exception:
            pass
        try:
            server.stop()
        except Exception:
            pass

    try:
        for sig in _shutdown_signal_values():
            previous_handlers[int(sig)] = signal.getsignal(sig)
            signal.signal(sig, _signal_handler)
        asyncio.run(server.serve())
    finally:
        for sig, handler in previous_handlers.items():
            try:
                signal.signal(sig, handler)
            except (OSError, ValueError):
                continue
        request_service_shutdown()


def _run_granian_mp_server(settings) -> None:
    from granian import Granian

    from core.granian_reload import RELOAD_CONFIG

    Granian(
        settings.app_target,
        interface="asgi",
        address=settings.listen_host,
        port=settings.listen_port,
        reload=settings.reload_enabled,
        log_level=settings.granian_log_level,
        process_name=settings.process_name,
        workers_kill_timeout=GRACEFUL_SHUTDOWN_TIMEOUT,
        **RELOAD_CONFIG,
    ).serve()


def run_server() -> None:
    from core.logger import get_logger, set_runtime_logging_config, setup_logger
    from core.runtime_settings import load_runtime_settings

    settings = load_runtime_settings()
    set_runtime_logging_config(
        debug_logging=settings.debug_logging,
        anonymous_mode=settings.anonymous_mode,
        log_level=settings.log_level,
        file_log_level=settings.file_log_level,
        log_dir=settings.log_dir,
        log_rotation=settings.log_rotation,
        log_retention_days=settings.log_retention_days,
    )
    setup_logger(
        settings.log_dir,
        debug_mode=settings.debug_logging,
        log_level=settings.log_level,
        file_log_level=settings.file_log_level,
        log_rotation=settings.log_rotation,
        log_retention_days=settings.log_retention_days,
        force=True,
    )
    logger = get_logger(__name__)
    service_name = settings.service_name

    logger.info(f"🚀 启动 {service_name} 服务...")
    logger.info(f"📡 监听地址: {settings.listen_host}:{settings.listen_port}")
    logger.info(f"🔧 调试模式: {'开启' if settings.debug_logging else '关闭'}")
    logger.info(f"🔐 匿名模式: {'开启' if settings.anonymous_mode else '关闭'}")
    logger.info(
        f"📝 日志配置: 控制台={settings.log_level}, 文件={settings.file_log_level}, "
        f"目录={settings.log_dir}, 轮转={settings.log_rotation}, 保留={settings.log_retention_days}天"
    )

    try:
        if sys.platform == "win32":
            _run_windows_embedded_server(settings, logger)
        else:
            _run_granian_mp_server(settings)
    except KeyboardInterrupt:
        logger.info("🛑 收到中断信号，正在关闭服务...")
    except Exception as exc:
        logger.error(f"服务启动失败: {exc}")
        sys.exit(1)


def main(argv: Optional[Sequence[str]] = None) -> None:
    parser = _build_parser()
    args, remaining = parser.parse_known_args(list(argv) if argv is not None else sys.argv[1:])
    _apply_runtime_overrides(args)

    if args.cli:
        run_cli(remaining)
        return

    if remaining:
        parser.error(f"未知参数: {' '.join(remaining)}")
    run_server()


if __name__ == "__main__":
    main()
