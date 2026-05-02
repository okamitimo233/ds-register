# Directory Structure

> How backend code is organized in this project.

---

## Overview

This project follows a simple, flat module structure with clear separation of concerns. The core business logic lives in the `core/` package, with a single entry point for web service and CLI modes.

Key principles:
- Flat module structure (no deep nesting)
- Single responsibility per module
- Configuration and runtime data separated from source code
- Static frontend assets served directly

---

## Directory Layout

```
ds-register/
├── core/                    # Core backend package
│   ├── __init__.py          # Package init, static dir config
│   ├── __main__.py          # Module entry point (python -m core)
│   ├── server.py            # FastAPI app, REST API, SSE, background tasks
│   ├── register.py          # Registration flow, CLI, proxy pool, OAuth/OTP
│   ├── pool_maintainer.py   # Sub2Api pool maintenance (probe, clean, refill)
│   ├── token_health.py      # OpenAI token refresh, health check logic
│   ├── mail_providers.py    # Email provider abstraction layer
│   ├── logger.py            # Logging setup with sanitization
│   ├── local_tokens.py      # Local token management utilities
│   ├── runtime_settings.py  # Runtime configuration management
│   └── granian_reload.py    # Development server reload helper
├── static/                  # Frontend static files (HTML/JS/CSS)
├── data/                    # Runtime data directory (generated, not in VCS)
│   ├── sync_config.json     # Runtime configuration
│   ├── state.json           # Application state
│   └── tokens/              # Token storage directory
├── main.py                  # Quick start entry point
├── requirements.txt         # Python dependencies
├── pyproject.toml           # Project metadata and build config
└── .venv/                   # Virtual environment (local, not in VCS)
```

---

## Module Organization

### Core Modules

Each module in `core/` has a distinct responsibility:

| Module | Responsibility | When to modify |
|--------|---------------|----------------|
| `server.py` | FastAPI routes, SSE streaming, background task coordination | Adding/modifying API endpoints, SSE events |
| `register.py` | Registration flow, CLI interface, OAuth/OTP handling | Modifying registration logic, adding CLI flags |
| `pool_maintainer.py` | Sub2Api account pool maintenance | Changing pool management strategies |
| `token_health.py` | Token refresh, health classification | Modifying token validation logic |
| `mail_providers.py` | Email provider abstraction, multi-provider routing | Adding new email providers |
| `logger.py` | Logging setup, sensitive data sanitization | Changing log format, adding sanitization rules |

### Adding New Modules

When adding new functionality:

1. **Keep modules flat** - Avoid creating `core/submodule/` unless absolutely necessary
2. **Single responsibility** - One module = one domain concern
3. **Import from `core.*`** - Use absolute imports: `from core.logger import app_logger`
4. **Minimize cross-dependencies** - Keep module interfaces clean and minimal

---

## Naming Conventions

### Files and Directories

- **Python modules**: `snake_case` (e.g., `pool_maintainer.py`, `token_health.py`)
- **Directories**: `snake_case` (e.g., `core/`, `static/`, `data/`)
- **Test files**: `test_*.py` in `tests/` directory at project root (when tests exist)

### Runtime Data

- **Configuration files**: `*_config.json` (e.g., `sync_config.json`)
- **State files**: Descriptive names like `state.json`
- **Data directories**: Plural nouns (e.g., `tokens/`)

---

## Version Management

### Version Consistency Convention

**Rule**: The version number MUST be kept consistent between these two files:

1. `pyproject.toml` - Project metadata version
2. `core/__init__.py` - Runtime version constant (`__version__`)

**Why**:
- Ensures consistency between build metadata and runtime version
- Prevents confusion when debugging version-specific issues
- Maintains trust in version reporting

**Example**:

```toml
# pyproject.toml
[project]
name = "openai-pool-orchestrator"
version = "2.0.0"
```

```python
# core/__init__.py
__version__ = "2.0.0"  # Must match pyproject.toml
```

**When to update**:
- Before release: update both files simultaneously
- After merge: verify consistency if version was changed
- On version mismatch: treat as a bug, fix immediately

**Common mistake**:

```python
# ❌ Wrong: Only update pyproject.toml
# pyproject.toml: version = "2.1.0"
# core/__init__.py: __version__ = "2.0.0"  # Inconsistent!

# ✅ Correct: Update both
# pyproject.toml: version = "2.1.0"
# core/__init__.py: __version__ = "2.1.0"  # Consistent
```

**Verification**:

Add a test to verify version consistency:

```python
# tests/test_version.py
import tomli
from pathlib import Path

def test_version_consistency():
    """Verify version numbers match between pyproject.toml and core/__init__.py."""
    # Read pyproject.toml
    pyproject_path = Path(__file__).parent.parent / "pyproject.toml"
    with open(pyproject_path, "rb") as f:
        pyproject_data = tomli.load(f)
    pyproject_version = pyproject_data["project"]["version"]
    
    # Read core/__init__.py
    from core import __version__
    
    # Assert consistency
    assert __version__ == pyproject_version, (
        f"Version mismatch: pyproject.toml={pyproject_version}, "
        f"core/__init__.py={__version__}"
    )
```

---

## Examples

### Well-organized modules

**`core/server.py`** - Clear separation between:
- FastAPI route definitions
- SSE streaming endpoints
- Background task management
- Request/response models (Pydantic)

**`core/logger.py`** - Single responsibility:
- Logging configuration
- Sensitive data sanitization
- Runtime flag management

**`core/mail_providers.py`** - Abstraction layer:
- Provider interface definition
- Multi-provider routing logic
- Provider-specific implementations
