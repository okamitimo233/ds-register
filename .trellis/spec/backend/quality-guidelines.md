# Quality Guidelines

> Code quality standards for backend development.

---

## Overview

This project maintains code quality through a combination of manual testing, syntax checking, and adherence to Python coding standards. The codebase prioritizes simplicity, readability, and maintainability over complex abstractions.

Key principles:
- **Python 3.10+ syntax only** - Use modern Python features
- **PEP 8 compliant** - Follow Python style guide
- **Type hints encouraged** - Add type annotations for clarity
- **Test manually when automation is minimal** - Validate critical paths
- **Keep it simple** - Avoid premature abstraction

---

## Forbidden Patterns

### ❌ Python 2 Legacy Syntax

```python
# Don't use old-style syntax
print "Hello"  # Python 2 syntax
x, y = y, x  # Old tuple unpacking
```

```python
# Use Python 3.10+ syntax
print("Hello")
x, y = y, x  # Still works, but prefer explicit when complex
```

---

### ❌ Deep Nesting and Complex Conditionals

```python
# Too nested
def process(data):
    if data:
        if data.items:
            for item in data.items:
                if item.active:
                    if item.value > 0:
                        # Process...
```

```python
# Flatten with early returns/guards
def process(data):
    if not data or not data.items:
        return
    
    for item in data.items:
        if not item.active or item.value <= 0:
            continue
        # Process...
```

---

### ❌ God Functions

```python
# Function doing too many things
def handle_registration():
    # 200 lines of validation, API calls, email sending, logging...
```

```python
# Split into focused functions
def handle_registration():
    validate_input(data)
    account = create_account(data)
    send_welcome_email(account)
    logger.success("Registration completed")
```

---

### ❌ Mutable Default Arguments

```python
# Dangerous: mutable default
def append_item(item, items=[]):
    items.append(item)
    return items
```

```python
# Safe: use None default
def append_item(item, items=None):
    if items is None:
        items = []
    items.append(item)
    return items
```

---

### ❌ Bare Except Clauses

```python
# Bad: catches everything including KeyboardInterrupt
try:
    risky_operation()
except:
    pass
```

```python
# Good: catch specific exceptions
try:
    risky_operation()
except (ValueError, ConnectionError) as e:
    logger.error("Operation failed", extra={"error": str(e)})
```

---

## Required Patterns

### ✅ Use Context Managers

```python
# Always use context managers for resources
with open("file.json") as f:
    data = json.load(f)

async with httpx.AsyncClient() as client:
    response = await client.get(url)
```

---

### ✅ Use f-strings for String Formatting

```python
# Modern and readable
message = f"User {user_id} registered at {timestamp}"

# Avoid old-style formatting
message = "User %s registered at %s" % (user_id, timestamp)
message = "User {} registered at {}".format(user_id, timestamp)
```

---

### ✅ Use Type Hints for Function Signatures

```python
from typing import Dict, List, Optional

def get_tokens(status: str = "all") -> List[Dict[str, Any]]:
    """Retrieve tokens filtered by status."""
    # Implementation

async def refresh_token(token_id: str) -> Optional[str]:
    """Refresh token and return new value, or None if failed."""
    # Implementation
```

---

### ✅ Use Pydantic for Request/Response Models

```python
from pydantic import BaseModel, Field

class StartRequest(BaseModel):
    proxy: Optional[str] = None
    worker_count: int = Field(default=1, ge=1, le=10)

class TokenResponse(BaseModel):
    token_id: str
    email: str
    status: str
    created_at: datetime
```

---

## Testing Requirements

### Current State

The project has a **lightweight pytest suite** covering critical scenarios:

- Task state management
- Sensitive configuration sanitization
- Token health validation
- Sub2Api maintenance logic
- Group ID configuration

### Test Commands

```bash
# Activate virtual environment first
.venv\Scripts\activate

# Run tests with pytest
python -m pytest tests/

# Alternative: use venv Python directly
.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/
```

### Minimum Validation

When automated tests don't cover changes:

1. **Syntax check**:
   ```bash
   python -m compileall core main.py tests
   ```

2. **Manual verification**:
   - Web UI: `python main.py` → visit `http://localhost:18421`
   - CLI: `python main.py --cli --proxy http://127.0.0.1:7890 --once`
   - Check logs for errors

### Test File Organization

```
tests/
├── test_task_state.py       # Task state management
├── test_config_sanitization.py  # Sensitive data masking
├── test_token_health.py     # Token validation
├── test_pool_maintainer.py  # Pool maintenance logic
└── test_group_config.py     # Group ID configuration
```

---

## Code Review Checklist

When reviewing backend code changes, verify:

### ✅ Functionality

- [ ] Code achieves the stated goal
- [ ] Edge cases are handled (empty input, None values, errors)
- [ ] Business logic is correct
- [ ] No regressions in existing features

### ✅ Code Quality

- [ ] Python 3.10+ syntax used (no legacy patterns)
- [ ] PEP 8 compliant (4 spaces, snake_case, PascalCase for classes)
- [ ] Type hints added for function signatures
- [ ] No deep nesting (max 3 levels)
- [ ] Functions are focused and not too long (< 50 lines ideal)

### ✅ Error Handling

- [ ] Exceptions are specific (not bare `except:`)
- [ ] Errors are logged with context before raising
- [ ] HTTP exceptions have meaningful messages
- [ ] No sensitive data in error responses

### ✅ Security

- [ ] No secrets/tokens in logs (use logger sanitization)
- [ ] No secrets committed to VCS
- [ ] User input validated
- [ ] File paths sanitized

### ✅ Testing

- [ ] Syntax check passed: `python -m compileall core main.py tests`
- [ ] Existing tests still pass: `python -m pytest tests/`
- [ ] New critical paths have test coverage (when applicable)
- [ ] Manual testing completed for UI changes

### ✅ Documentation

- [ ] Complex logic has brief comments explaining "why"
- [ ] CLAUDE.md updated if project structure/commands change
- [ ] Commit message follows Conventional Commits: `feat:`, `fix:`, `chore:`

---

## Linting and Type Checking

### Current Tools

The project uses minimal tooling:

- **Syntax validation**: `python -m compileall` (built-in)
- **Type checking**: Not enforced, but type hints encouraged
- **Linting**: No automated linter configured

### Manual Checks

Run these before committing:

```bash
# Syntax check
python -m compileall core main.py tests

# Frontend check (if modifying static files)
node --check static/app.js

# Test suite
python -m pytest tests/
```

---

## Sensitive Data Handling

### Token and Secret Masking

Always mask sensitive data in logs and console output:

```python
# ✅ Good: Mask sensitive tokens
logger.info(f"Token obtained: {token[:10]}...")

# ❌ Bad: Expose full token
logger.info(f"Token obtained: {token}")
```

**Pattern for Token Display**:
- Show first 10 characters only
- Never log passwords, API keys, or full tokens
- Use structured logging with sanitization when possible

**Example**:
```python
def mask_token(token: str, show_chars: int = 10) -> str:
    """Mask token for safe display."""
    if not token or len(token) <= show_chars:
        return "***"
    return f"{token[:show_chars]}..."

# Usage
logger.info(f"DeepSeek token: {mask_token(result['token'])}")
# Output: "DeepSeek token: sk-abc123xyz..."
```

---

## Performance Optimization

### numba JIT for Performance-Sensitive Code

For computationally intensive operations (e.g., cryptography, hashing), use numba JIT compilation:

**When to Use**:
- Computationally expensive loops
- Cryptographic operations
- Mathematical algorithms with significant iteration

**Pattern**:
```python
from numba import jit
import numpy as np

# JIT-compiled function for performance
@jit(nopython=True)
def compute_hash(data: np.ndarray) -> np.ndarray:
    """JIT-accelerated hash computation."""
    # Implementation
    return result

# Graceful fallback
try:
    from numba import jit
    NUMBA_AVAILABLE = True
except ImportError:
    NUMBA_AVAILABLE = False
    def jit(*args, **kwargs):
        """Fallback: identity decorator."""
        def decorator(func):
            return func
        return decorator
```

**Dependencies**:
```txt
# requirements.txt
numba>=0.59.0  # Optional: JIT acceleration
```

**Performance Impact**:
- Pure Python: ~30-60 seconds
- With numba JIT: ~10-20 seconds (2-3x faster)

---

## Common Mistakes to Avoid

### 1. Over-Engineering

```python
# Don't: Create abstractions for single use cases
class TokenValidatorFactory:
    def create_validator(self, type: str) -> TokenValidator:
        # ... complex factory pattern for 2 validators
```

```python
# Do: Keep it simple until you have multiple use cases
def validate_token(token: dict) -> bool:
    return token.get("status") == "active"
```

---

### 2. Premature Optimization

```python
# Don't: Optimize without profiling
def get_token(token_id: str):
    # Complex caching, lazy loading, connection pooling...
```

```python
# Do: Write clear code, optimize when needed
def get_token(token_id: str):
    with open("data/tokens.json") as f:
        tokens = json.load(f)
    return tokens.get(token_id)
```

---

### 3. Ignoring Existing Patterns

```python
# Don't: Introduce new patterns when existing ones work
# Using requests library when project uses httpx
import requests
response = requests.get(url)
```

```python
# Do: Follow existing patterns
import httpx
async with httpx.AsyncClient() as client:
    response = await client.get(url)
```
