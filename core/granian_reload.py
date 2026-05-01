"""
Granian 热重载配置。
"""

from __future__ import annotations


RELOAD_IGNORE_DIRS = [
    ".git",
    ".github",
    ".idea",
    ".pytest_cache",
    ".ruff_cache",
    ".venv",
    "__pycache__",
    "build",
    "config",
    "data",
    "dist",
    "htmlcov",
    "node_modules",
    "examples",
    "logs",
    "static",
    "templates",
]

RELOAD_IGNORE_PATTERNS = [
    r".*\.coverage$",
    r".*\.db$",
    r".*\.egg-info.*",
    r".*\.git.*",
    r".*\.lock$",
    r".*\.log$",
    r".*\.pid$",
    r".*\.pyc$",
    r".*\.pyd$",
    r".*\.pyo$",
    r".*\.tmp$",
]

RELOAD_WATCH_PATHS = [
    "core",
    "main.py",
    "static",
]

RELOAD_CONFIG = {
    "reload_ignore_dirs": RELOAD_IGNORE_DIRS,
    "reload_ignore_patterns": RELOAD_IGNORE_PATTERNS,
    "reload_paths": RELOAD_WATCH_PATHS,
    "reload_tick": 500,
}
