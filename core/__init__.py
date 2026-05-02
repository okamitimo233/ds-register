"""
OpenAI Pool Orchestrator
"""

__version__ = "2.0.0"
__author__ = "OpenAI Pool Orchestrator Contributors"

from pathlib import Path

PACKAGE_DIR = Path(__file__).parent
PROJECT_ROOT = PACKAGE_DIR.parent

DATA_DIR = PROJECT_ROOT / "data"
DATA_DIR.mkdir(exist_ok=True)

TOKENS_DIR = DATA_DIR / "tokens"
TOKENS_DIR.mkdir(exist_ok=True)

LOGS_DIR = DATA_DIR / "logs"
LOGS_DIR.mkdir(exist_ok=True)

CONFIG_FILE = DATA_DIR / "sync_config.json"
STATE_FILE = DATA_DIR / "state.json"

STATIC_DIR = PROJECT_ROOT / "static"

from .logger import setup_logger

setup_logger(LOGS_DIR)
