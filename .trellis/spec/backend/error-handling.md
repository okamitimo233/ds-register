# Error Handling

> How errors are handled in this project.

---

## Overview

This project uses FastAPI's exception handling for API routes, with clear separation between business logic errors and system errors. Errors are propagated using Python exceptions and converted to appropriate HTTP responses at the API layer.

Key principles:
- Use specific exception types for different error categories
- Convert exceptions to HTTP responses at API boundaries
- Always include meaningful error messages
- Log errors with context before raising
- Never expose internal implementation details in error responses

---

## Error Types

### Built-in Exceptions

The project primarily uses Python's built-in exceptions:

| Exception | When to use | HTTP Status |
|-----------|------------|-------------|
| `RuntimeError` | Invalid state, operation cannot proceed | 409 Conflict |
| `ValueError` | Invalid input, malformed data | 400 Bad Request |
| `KeyError` | Missing required data | 400 Bad Request |
| `FileNotFoundError` | Required file missing | 404 Not Found |
| `PermissionError` | Insufficient permissions | 403 Forbidden |

### HTTP Exceptions

For API routes, use FastAPI's `HTTPException`:

```python
from fastapi import HTTPException

# Client errors (4xx)
raise HTTPException(status_code=400, detail="Invalid email format")
raise HTTPException(status_code=404, detail="Token not found")
raise HTTPException(status_code=409, detail="Task already running")

# Never expose server errors (5xx) directly to clients
```

---

## Error Handling Patterns

### API Route Pattern

Catch business logic exceptions and convert to HTTP responses:

```python
from fastapi import HTTPException

@app.post("/api/start")
async def api_start(req: StartRequest) -> Dict[str, Any]:
    try:
        _state.start_task(req.proxy, req.worker_count)
    except RuntimeError as e:
        # Business logic error - client actionable
        raise HTTPException(status_code=409, detail=str(e))
    except ValueError as e:
        # Invalid input - client actionable
        raise HTTPException(status_code=400, detail=str(e))
    
    # Success response
    return {"run_id": snapshot["task"].get("run_id")}
```

### Background Task Pattern

Log errors with context, then raise or handle:

```python
from core.logger import app_logger

logger = app_logger.bind(component="pool_maintainer")

async def maintain_pool():
    try:
        accounts = await fetch_accounts()
        for account in accounts:
            try:
                await validate_account(account)
            except Exception as e:
                # Log individual failures, continue processing
                logger.error("Account validation failed", extra={
                    "account_id": account.id,
                    "error": str(e)
                })
                continue
    except Exception as e:
        # Log critical error, stop task
        logger.error("Pool maintenance failed", extra={
            "error": str(e)
        })
        raise
```

### CLI Pattern

For CLI commands, catch exceptions and exit with appropriate code:

```python
import sys

def cli_register():
    try:
        result = perform_registration()
        print(f"Success: {result}")
    except ValueError as e:
        print(f"Error: {e}", file=sys.stderr)
        sys.exit(1)
    except Exception as e:
        print(f"Unexpected error: {e}", file=sys.stderr)
        sys.exit(2)
```

---

## API Error Responses

### Standard Error Format

FastAPI automatically formats HTTP exceptions as JSON:

```json
{
  "detail": "Task already running"
}
```

### Error Response Guidelines

1. **Be specific**: `"Token not found"` not `"Error"`
2. **Be actionable**: `"Email format invalid"` not `"Invalid input"`
3. **Don't expose internals**: Never include stack traces, file paths, or internal state
4. **Use appropriate status codes**:
   - 400: Client input error
   - 404: Resource not found
   - 409: Conflict (e.g., already exists, invalid state)
   - 500: Server error (avoid exposing details)

### Example Error Handling

```python
@app.post("/api/tokens/{token_id}/refresh")
async def refresh_token(token_id: str):
    try:
        token = get_token(token_id)
        if not token:
            raise HTTPException(status_code=404, detail="Token not found")
        
        refreshed = await refresh_token_logic(token)
        return {"status": "refreshed", "token_id": token_id}
    
    except HTTPException:
        # Re-raise HTTP exceptions as-is
        raise
    except Exception as e:
        # Log server error, return generic message
        logger.error("Token refresh failed", extra={
            "token_id": token_id,
            "error": str(e)
        })
        raise HTTPException(status_code=500, detail="Internal server error")
```

---

## Common Mistakes

### ❌ Don't: Catch and Silence Exceptions

```python
try:
    risky_operation()
except Exception:
    pass  # Bad: error is swallowed
```

### ✅ Do: Log and Re-raise or Handle

```python
try:
    risky_operation()
except Exception as e:
    logger.error("Operation failed", extra={"error": str(e)})
    raise  # Re-raise or handle appropriately
```

---

### ❌ Don't: Expose Internal Errors to Clients

```python
@app.post("/api/data")
async def process_data():
    try:
        result = internal_processing()
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))  # Exposes internals
```

### ✅ Do: Log Internally, Return Generic Message

```python
@app.post("/api/data")
async def process_data():
    try:
        result = internal_processing()
    except Exception as e:
        logger.error("Processing failed", extra={"error": str(e)})
        raise HTTPException(status_code=500, detail="Processing failed")
```

---

### ❌ Don't: Use Bare Except

```python
try:
    operation()
except:  # Catches KeyboardInterrupt, SystemExit, etc.
    handle_error()
```

### ✅ Do: Catch Specific Exceptions

```python
try:
    operation()
except (ValueError, RuntimeError) as e:
    handle_error(e)
```

---

### ❌ Don't: Raise Generic Exceptions

```python
if not token:
    raise Exception("No token")  # Too generic
```

### ✅ Do: Use Specific Exception Types

```python
if not token:
    raise ValueError("Token is required")
```

---

## Error Context and Logging

Always log errors with context before raising or handling:

```python
from core.logger import app_logger

logger = app_logger.bind(component="register")

def register_user(email: str, password: str):
    if not email:
        logger.error("Registration failed: missing email")
        raise ValueError("Email is required")
    
    try:
        result = create_account(email, password)
        logger.success("Registration completed", extra={"email": email})
        return result
    except ConnectionError as e:
        logger.error("Registration failed: network error", extra={
            "email": email,
            "error": str(e)
        })
        raise RuntimeError("Network error during registration") from e
```
