# REST API Endpoints Specification

> Executable contracts for the FastAPI backend REST API.

---

## Overview

This document specifies the REST API endpoints exposed by the FastAPI backend server. These contracts are critical for frontend-backend integration and external API consumers.

---

## API Endpoint Contracts

### Configuration Management

#### `GET /api/sync-config`

**Purpose**: Retrieve current synchronization configuration

**Response**:
```json
{
  "email": "string",
  "password": "string",
  "proxy": "string",
  "auto_register": "boolean",
  "multithread": "boolean",
  "thread_count": "integer",
  "listen_host": "string",
  "listen_port": "integer",
  "debug_logging": "boolean",
  "log_retention_days": "integer",
  "mail_providers": ["string"],
  "proxy_pool_enabled": "boolean"
}
```

**Status Codes**:
- `200 OK` - Configuration retrieved successfully

---

#### `POST /api/sync-config`

**Purpose**: Update synchronization configuration

**Request Body**: Partial configuration object (any field from GET response)

**Response**:
```json
{
  "status": "updated",
  "config": { /* updated config object */ }
}
```

**Status Codes**:
- `200 OK` - Configuration updated successfully
- `400 Bad Request` - Invalid configuration field

---

#### `GET /api/runtime-config`

**Purpose**: Retrieve runtime configuration (environment-derived settings)

**Response**:
```json
{
  "service_name": "string",
  "listen_host": "string",
  "listen_port": "integer",
  "debug_logging": "boolean",
  "anonymous_mode": "boolean",
  "reload_enabled": "boolean"
}
```

**Status Codes**:
- `200 OK` - Runtime configuration retrieved successfully

---

#### `POST /api/runtime-config`

**Purpose**: Update runtime configuration

**Request Body**: Partial runtime config object

**Response**:
```json
{
  "status": "updated"
}
```

**Status Codes**:
- `200 OK` - Runtime configuration updated successfully
- `400 Bad Request` - Invalid configuration field

---

### Registration Tasks

#### `POST /api/start`

**Purpose**: Start registration task

**Request Body**:
```json
{
  "proxy": "string (optional)",
  "worker_count": "integer (optional)",
  "target": "string (optional, 'openai' or 'deepseek')"
}
```

**Response**:
```json
{
  "status": "started",
  "worker_count": "integer"
}
```

**Status Codes**:
- `200 OK` - Task started successfully
- `409 Conflict` - Task already running

**Side Effects**:
- Creates background worker threads
- Emits SSE events to `/api/logs`
- Modifies application state

---

#### `POST /api/stop`

**Purpose**: Stop registration task

**Request Body**: Empty

**Response**:
```json
{
  "status": "stopped"
}
```

**Status Codes**:
- `200 OK` - Task stopped successfully
- `404 Not Found` - No task running

**Side Effects**:
- Signals worker threads to stop
- Waits for graceful shutdown
- Clears application state

---

#### `GET /api/status`

**Purpose**: Retrieve current service status

**Response**:
```json
{
  "running": "boolean",
  "worker_count": "integer",
  "registered_count": "integer",
  "failed_count": "integer",
  "uptime_seconds": "number"
}
```

**Status Codes**:
- `200 OK` - Status retrieved successfully

---

### Token Management

#### `GET /api/tokens`

**Purpose**: Retrieve paginated list of stored tokens

**Query Parameters**:
- `page` (integer, optional, default: 1) - Page number
- `page_size` (integer, optional, default: 50) - Items per page

**Response**:
```json
{
  "tokens": [
    {
      "filename": "string",
      "email": "string",
      "created_at": "string (ISO 8601)",
      "token_preview": "string (masked)"
    }
  ],
  "total": "integer",
  "page": "integer",
  "page_size": "integer"
}
```

**Status Codes**:
- `200 OK` - Token list retrieved successfully

---

#### `DELETE /api/tokens/{filename}`

**Purpose**: Delete a specific token file

**Path Parameters**:
- `filename` (string) - Token filename to delete

**Response**:
```json
{
  "status": "deleted",
  "filename": "string"
}
```

**Status Codes**:
- `200 OK` - Token deleted successfully
- `404 Not Found` - Token file not found

**Side Effects**:
- Deletes token file from `data/tokens/`
- Updates token inventory database

---

#### `POST /api/tokens/import`

**Purpose**: Import external tokens

**Request Body**:
```json
{
  "tokens": [
    {
      "email": "string",
      "password": "string",
      "token": "string"
    }
  ]
}
```

**Response**:
```json
{
  "status": "imported",
  "count": "integer"
}
```

**Status Codes**:
- `200 OK` - Tokens imported successfully
- `400 Bad Request` - Invalid token format

**Side Effects**:
- Creates token files in `data/tokens/`
- Updates token inventory database

---

### Logging

#### `GET /api/logs`

**Purpose**: Server-Sent Events (SSE) stream for real-time logs

**Response Type**: `text/event-stream`

**Event Format**:
```
data: {"ts": "HH:MM:SS", "level": "info|success|error|warn", "message": "string", "step": "string"}

```

**Status Codes**:
- `200 OK` - SSE stream started

**Connection Behavior**:
- Long-lived connection
- Client should reconnect on disconnect
- Server pushes events as they occur

---

## Error Response Format

All error responses follow this structure:

```json
{
  "detail": "Human-readable error message"
}
```

**Common Error Codes**:
- `400 Bad Request` - Invalid request body or parameters
- `404 Not Found` - Resource not found
- `409 Conflict` - Resource state conflict (e.g., task already running)
- `500 Internal Server Error` - Server-side error

---

## Validation Rules

### Email Validation

```python
import re

EMAIL_PATTERN = re.compile(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$')

def validate_email(email: str) -> bool:
    """Validate email format."""
    return bool(EMAIL_PATTERN.match(email))
```

### Proxy URL Validation

```python
def validate_proxy_url(url: str) -> bool:
    """Validate proxy URL format."""
    return url.startswith(("http://", "https://", "socks5://"))
```

### Port Range Validation

```python
def validate_port(port: int) -> bool:
    """Validate port number is in valid range."""
    return 1 <= port <= 65535
```

---

## Good/Base/Bad Cases

### Good Case: Start Task with Valid Config

```python
# Request
POST /api/start
{
  "proxy": "http://127.0.0.1:7890",
  "worker_count": 3
}

# Response
200 OK
{
  "status": "started",
  "worker_count": 3
}

# Side effects:
# - Background workers started
# - SSE events begin streaming
# - Status endpoint reflects running state
```

---

### Base Case: Get Tokens with Default Pagination

```python
# Request
GET /api/tokens

# Response
200 OK
{
  "tokens": [...],  # Up to 50 tokens
  "total": 120,
  "page": 1,
  "page_size": 50
}
```

---

### Bad Case: Start Task When Already Running

```python
# Request
POST /api/start
{}

# Response
409 Conflict
{
  "detail": "Task already running"
}

# No side effects
```

---

## Tests Required

**Unit Tests** (`tests/test_api_endpoints.py`):

```python
import pytest
from fastapi.testclient import TestClient
from core.server import app

client = TestClient(app)

def test_get_sync_config():
    """Test GET /api/sync-config returns valid config."""
    response = client.get("/api/sync-config")
    assert response.status_code == 200
    assert "email" in response.json()
    assert "listen_port" in response.json()

def test_update_sync_config():
    """Test POST /api/sync-config updates config."""
    response = client.post("/api/sync-config", json={
        "debug_logging": True
    })
    assert response.status_code == 200
    assert response.json()["config"]["debug_logging"] == True

def test_start_stop_task():
    """Test task lifecycle."""
    # Start task
    start_response = client.post("/api/start", json={"worker_count": 1})
    assert start_response.status_code == 200
    assert start_response.json()["status"] == "started"

    # Check status
    status_response = client.get("/api/status")
    assert status_response.status_code == 200
    assert status_response.json()["running"] == True

    # Stop task
    stop_response = client.post("/api/stop")
    assert stop_response.status_code == 200
    assert stop_response.json()["status"] == "stopped"

def test_start_task_when_already_running():
    """Test starting task when already running returns 409."""
    client.post("/api/start", json={"worker_count": 1})
    response = client.post("/api/start", json={"worker_count": 1})
    assert response.status_code == 409
    client.post("/api/stop")  # Cleanup

def test_token_pagination():
    """Test token list pagination."""
    response = client.get("/api/tokens?page=1&page_size=10")
    assert response.status_code == 200
    assert response.json()["page"] == 1
    assert response.json()["page_size"] == 10
```

---

## Wrong vs Correct

### Wrong: Inconsistent API Naming

```python
# ❌ Wrong: Mixed naming conventions
@app.get("/api/getConfig")  # camelCase
@app.post("/api/update_config")  # snake_case

# ✅ Correct: Consistent kebab-case
@app.get("/api/sync-config")
@app.post("/api/sync-config")
```

---

### Wrong: Missing Error Handling

```python
# ❌ Wrong: No error handling
@app.post("/api/start")
async def start_task():
    start_background_task()  # May raise exception
    return {"status": "started"}

# ✅ Correct: Explicit error handling
@app.post("/api/start")
async def start_task():
    if is_task_running():
        raise HTTPException(409, "Task already running")
    try:
        start_background_task()
        return {"status": "started"}
    except Exception as e:
        logger.error("Failed to start task", exc_info=e)
        raise HTTPException(500, f"Failed to start task: {e}")
```

---

### Wrong: Synchronous Blocking in Async Handler

```python
# ❌ Wrong: Blocking call in async function
@app.get("/api/tokens")
async def get_tokens():
    time.sleep(5)  # Blocks entire event loop!
    return load_tokens()

# ✅ Correct: Use async I/O
@app.get("/api/tokens")
async def get_tokens():
    await asyncio.sleep(0)  # Yield to event loop
    return await run_in_threadpool(load_tokens)
```

---

## Documentation Checklist

When adding/modifying API endpoints:

- [ ] Document endpoint in this spec file
- [ ] Include request/response schemas
- [ ] List all status codes
- [ ] Document side effects (if any)
- [ ] Add unit tests
- [ ] Update README.md API section
- [ ] Verify consistency with actual implementation

---

## Related Documentation

- [Integration Guidelines](./integration-guidelines.md) - External API integration patterns
- [Error Handling](./error-handling.md) - Error handling patterns
- [Quality Guidelines](./quality-guidelines.md) - Code quality standards
