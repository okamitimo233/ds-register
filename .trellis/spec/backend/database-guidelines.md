# Database Guidelines

> Data storage patterns and conventions for this project.

---

## Overview

This project does **not use a traditional database**. Instead, it uses **file-based storage** for configuration and runtime data:

- **Configuration**: `data/sync_config.json`
- **Application state**: `data/state.json`
- **Token storage**: `data/tokens/` directory
- **Runtime data**: Various JSON files in `data/`

This approach prioritizes simplicity and portability over scalability. The data directory is treated as runtime-generated state and is **not committed to version control**.

---

## Data Storage Patterns

### JSON Configuration Files

The project uses JSON files for configuration and state:

```python
import json
from pathlib import Path

DATA_DIR = Path("data")
CONFIG_FILE = DATA_DIR / "sync_config.json"

def load_config() -> dict:
    """Load configuration from JSON file."""
    if not CONFIG_FILE.exists():
        return get_default_config()
    
    with open(CONFIG_FILE, encoding="utf-8") as f:
        return json.load(f)

def save_config(config: dict) -> None:
    """Save configuration to JSON file."""
    DATA_DIR.mkdir(exist_ok=True)
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
```

---

### Default Configuration Pattern

Always provide sensible defaults when configuration is missing:

```python
def get_default_config() -> dict:
    """Return default configuration structure."""
    return {
        "proxy": "",
        "worker_count": 1,
        "sub2api_url": "",
        "sub2api_key": "",
        "email_provider": "auto",
        "tokens": []
    }

def load_config() -> dict:
    """Load configuration with defaults."""
    if not CONFIG_FILE.exists():
        default = get_default_config()
        save_config(default)
        return default
    
    with open(CONFIG_FILE, encoding="utf-8") as f:
        config = json.load(f)
    
    # Merge with defaults for missing keys
    default = get_default_config()
    return {**default, **config}
```

---

### Token File Storage

Tokens are stored in individual files under `data/tokens/`:

```python
TOKENS_DIR = Path("data/tokens")

def save_token(token_id: str, token_data: dict) -> None:
    """Save token to individual file."""
    TOKENS_DIR.mkdir(exist_ok=True)
    token_file = TOKENS_DIR / f"{token_id}.json"
    
    with open(token_file, "w", encoding="utf-8") as f:
        json.dump(token_data, f, indent=2, ensure_ascii=False)

def load_token(token_id: str) -> Optional[dict]:
    """Load token from file."""
    token_file = TOKENS_DIR / f"{token_id}.json"
    
    if not token_file.exists():
        return None
    
    with open(token_file, encoding="utf-8") as f:
        return json.load(f)

def list_tokens() -> list[dict]:
    """List all tokens."""
    if not TOKENS_DIR.exists():
        return []
    
    tokens = []
    for token_file in TOKENS_DIR.glob("*.json"):
        with open(token_file, encoding="utf-8") as f:
            tokens.append(json.load(f))
    
    return tokens
```

---

## File Naming Conventions

### Data Files

| File | Purpose | Format |
|------|---------|--------|
| `sync_config.json` | Main configuration file | JSON object |
| `state.json` | Application runtime state | JSON object |
| `tokens/*.json` | Individual token records | JSON object per file |
| `logs/*.log` | Log files | Text (loguru format) |

### Naming Rules

- **Configuration files**: `*_config.json` or `config.json`
- **State files**: Descriptive names like `state.json`, `cache.json`
- **Token files**: `{token_id}.json` (token ID as filename)
- **Log files**: `{date}.log` or `{component}.log`

---

## Concurrency Considerations

### File Locking

The project does not implement file locking. When concurrent access is possible:

1. **Read-heavy workloads**: Safe to read concurrently
2. **Write operations**: Coordinate at application level (single writer)
3. **State mutations**: Use in-memory state, persist periodically

Example pattern:

```python
import threading

class StateManager:
    def __init__(self):
        self._lock = threading.Lock()
        self._state = self._load_state()
    
    def update_state(self, key: str, value: Any) -> None:
        with self._lock:
            self._state[key] = value
            self._save_state()
    
    def _load_state(self) -> dict:
        # Load from file
        pass
    
    def _save_state(self) -> None:
        # Save to file
        pass
```

---

## Data Migration

### Schema Evolution

When configuration structure changes:

```python
def migrate_config(config: dict) -> dict:
    """Migrate configuration to latest schema."""
    version = config.get("version", 1)
    
    if version < 2:
        # Add new field with default
        config["new_field"] = "default_value"
        config["version"] = 2
    
    if version < 3:
        # Rename field
        if "old_name" in config:
            config["new_name"] = config.pop("old_name")
        config["version"] = 3
    
    return config

def load_config() -> dict:
    """Load and migrate configuration."""
    config = _load_raw_config()
    config = migrate_config(config)
    save_config(config)  # Persist migrated version
    return config
```

---

## Common Mistakes

### ❌ Don't: Assume Data Directory Exists

```python
with open("data/sync_config.json") as f:
    data = json.load(f)
# Error if data/ doesn't exist
```

### ✅ Do: Create Directory if Missing

```python
from pathlib import Path

DATA_DIR = Path("data")
DATA_DIR.mkdir(exist_ok=True)

with open(DATA_DIR / "sync_config.json") as f:
    data = json.load(f)
```

---

### ❌ Don't: Hardcode File Paths

```python
with open("data/tokens/token123.json") as f:
    token = json.load(f)
```

### ✅ Do: Use Path Objects

```python
from pathlib import Path

TOKENS_DIR = Path("data") / "tokens"
token_file = TOKENS_DIR / f"{token_id}.json"

if token_file.exists():
    with open(token_file) as f:
        token = json.load(f)
```

---

### ❌ Don't: Store Sensitive Data in Plain Text

```python
config = {
    "api_key": "sk-abc123xyz",  # Stored in plain text
    "password": "secret123"     # Stored in plain text
}
save_config(config)
```

### ✅ Do: Mask or Encrypt Sensitive Data

```python
from core.logger import sanitize_log_text

# Option 1: Don't persist sensitive data if possible
config = {
    "api_key": "",  # Load from environment variable at runtime
}

# Option 2: Mask in logs (not storage)
# The logger automatically sanitizes sensitive data
logger.info("Config loaded", extra={"config": config})  # Will be masked

# Option 3: Encrypt before storing (if persistence is required)
# (Not currently implemented in this project)
```

---

### ❌ Don't: Write to Files Without Error Handling

```python
with open(config_file, "w") as f:
    json.dump(config, f)
# Error if disk is full, permissions denied, etc.
```

### ✅ Do: Handle File Write Errors

```python
from core.logger import app_logger

logger = app_logger.bind(component="config")

def save_config(config: dict) -> None:
    try:
        DATA_DIR.mkdir(exist_ok=True)
        with open(CONFIG_FILE, "w", encoding="utf-8") as f:
            json.dump(config, f, indent=2, ensure_ascii=False)
        logger.info("Configuration saved")
    except (IOError, OSError) as e:
        logger.error("Failed to save configuration", extra={"error": str(e)})
        raise RuntimeError(f"Cannot save configuration: {e}")
```

---

## Backup and Recovery

### Manual Backups

Since `data/` is not in VCS, manual backups are recommended:

```bash
# Backup data directory
cp -r data/ data_backup_$(date +%Y%m%d)/

# Backup specific files
cp data/sync_config.json sync_config_backup.json
```

### Recovery

If data files are corrupted:

1. **Configuration**: Delete `data/sync_config.json` → defaults will be recreated
2. **Tokens**: Restore from backup or re-register
3. **State**: Delete `data/state.json` → fresh state will be initialized

---

## Performance Considerations

### Large Token Datasets

When token count grows large (1000+):

1. **Use pagination**: Load tokens in batches, not all at once
2. **Index in memory**: Build lookup dict for frequent access
3. **Lazy loading**: Load token details on demand

Example:

```python
def list_tokens_paginated(page: int = 1, page_size: int = 20) -> list[dict]:
    """List tokens with pagination."""
    token_files = sorted(TOKENS_DIR.glob("*.json"))
    start = (page - 1) * page_size
    end = start + page_size
    
    tokens = []
    for token_file in token_files[start:end]:
        with open(token_file, encoding="utf-8") as f:
            tokens.append(json.load(f))
    
    return tokens
```

---

## Data Integrity

### Validation

Validate data before saving:

```python
def validate_config(config: dict) -> bool:
    """Validate configuration structure."""
    required_fields = ["proxy", "worker_count"]
    return all(field in config for field in required_fields)

def save_config(config: dict) -> None:
    """Save configuration with validation."""
    if not validate_config(config):
        raise ValueError("Invalid configuration structure")
    
    # Save configuration
    with open(CONFIG_FILE, "w", encoding="utf-8") as f:
        json.dump(config, f, indent=2, ensure_ascii=False)
```
