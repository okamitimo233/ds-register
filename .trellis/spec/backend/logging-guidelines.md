# Logging Guidelines

> How logging is done in this project.

---

## Overview

This project uses **loguru** as the logging library, providing structured logging with automatic sensitive data sanitization. The logging system is configured to protect sensitive information (emails, tokens, API keys) while providing detailed debugging information.

Key features:
- **loguru** library for enhanced logging
- Automatic sensitive data masking (emails, tokens, secrets)
- Runtime-configurable log levels and destinations
- Console and file logging with rotation
- Anonymous mode for privacy-compliant logs

---

## Log Levels

Use these log levels appropriately:

| Level | When to use | Example |
|-------|------------|---------|
| **TRACE** | Very detailed debugging (rarely needed) | Function entry/exit tracing |
| **DEBUG** | Development debugging, detailed flow info | "Trying proxy pool: http://..." |
| **INFO** | Normal operation events | "Server started on port 18421" |
| **SUCCESS** | Operation completed successfully | "Registration completed for user@example.com" |
| **WARNING** | Non-critical issues, recoverable errors | "Token refresh failed, retrying..." |
| **ERROR** | Errors that affect operation | "Failed to connect to proxy pool" |
| **CRITICAL** | System-level failures | "Configuration file corrupted" |

### Environment Variables

Configure log levels via environment variables or runtime config:

```bash
OPENAI_POOL_LOG_LEVEL=INFO           # Console log level
OPENAI_POOL_FILE_LOG_LEVEL=DEBUG     # File log level
OPENAI_POOL_DEBUG_LOGGING=false      # Enable debug mode
OPENAI_POOL_ANONYMOUS_MODE=true      # Mask sensitive data
```

---

## Structured Logging

### Log Format

The project uses loguru's default format with extra fields:

```python
logger.info("Operation completed", extra={
    "component": "register",  # Module/component name
    "user_id": "abc123"       # Context-specific fields
})
```

### Log Rotation

Default rotation and retention:

- **Rotation**: 10 MB per file
- **Retention**: 7 days
- **Compression**: Automatic for rotated files

Configure via environment variables:

```bash
OPENAI_POOL_LOG_DIR=./logs
OPENAI_POOL_LOG_ROTATION="10 MB"
OPENAI_POOL_LOG_RETENTION_DAYS=7
```

---

## What to Log

### Do Log

- **Application lifecycle**: Startup, shutdown, configuration loaded
- **API operations**: Request received, response sent (with status)
- **Business events**: Registration started/completed, token refreshed
- **Background tasks**: Pool maintenance started, accounts cleaned
- **Error conditions**: Exceptions, retries, fallbacks
- **Performance metrics**: Task duration, queue size

Example:

```python
from core.logger import app_logger

logger = app_logger.bind(component="register")

logger.info("Starting registration", extra={
    "email": email,  # Will be sanitized automatically
    "proxy": proxy
})

logger.success("Registration completed", extra={
    "user_id": user_id
})
```

---

## What NOT to Log

### Never Log These (Even Before Sanitization)

The sanitization system automatically masks these, but avoid logging them explicitly:

- **Full tokens**: `Bearer eyJ...` → logged as `Be***er`
- **API keys**: `sk-...` → logged as `sk***`
- **Passwords**: Any password fields
- **Full email addresses**: `user@example.com` → logged as `us***r@example.com`
- **JWT tokens**: Full JWT strings → logged as `eyJ***`
- **Secret query parameters**: `?token=abc123` → logged as `?token=ab***23`

### Sensitive Data Sanitization

The logger automatically sanitizes:

1. **Email addresses**: `user@example.com` → `us***r@example.com`
2. **Token/secret patterns**: `token: abc123xyz` → `token: ab***yz`
3. **JWT tokens**: `eyJhbGci...` → `eyJ***`
4. **Query string secrets**: `?api_key=secret` → `?api_key=se***et`

**Important**: The sanitization applies to log *messages* only. Do not log sensitive data in structured fields unless necessary.

### Anonymous Mode

Enable anonymous mode for maximum privacy:

```python
from core.logger import set_runtime_logging_flags

set_runtime_logging_flags(anonymous_mode=True)
```

In anonymous mode, additional masking rules apply to ensure no personally identifiable information leaks.

---

## Common Patterns

### Using the Logger

```python
from core.logger import app_logger

# Bind component name at module level
logger = app_logger.bind(component="pool_maintainer")

def maintain_pool():
    logger.info("Starting pool maintenance")
    try:
        # ... maintenance logic
        logger.success("Pool maintenance completed", extra={
            "active_accounts": count
        })
    except Exception as e:
        logger.error("Pool maintenance failed", extra={
            "error": str(e)
        })
        raise
```

### HTTP Request Logging

```python
logger.debug("Sending request", extra={
    "url": url,
    "method": "POST",
    "has_auth": bool(api_key)
})

response = await client.post(url, ...)

logger.debug("Response received", extra={
    "status_code": response.status_code,
    "response_time_ms": elapsed_ms
})
```

### Background Task Logging

```python
logger.info("Background task started", extra={
    "task_id": task_id,
    "task_type": "token_refresh"
})

# ... task execution

logger.success("Background task completed", extra={
    "task_id": task_id,
    "duration_seconds": duration
})
```
