# Third-Party Platform Integration Guidelines

> How to integrate third-party AI platforms and services into this project.

---

## Overview

This document provides executable contracts for integrating third-party platforms (e.g., DeepSeek, OpenAI) into ds-register. It covers API signatures, data contracts, error handling, and testing requirements.

---

## Scenario: DeepSeek Registration Integration

### 1. Scope / Trigger

**Trigger**: Integrating a new AI platform registration workflow that requires:
- PoW (Proof of Work) challenge solving
- Email verification via temporary email providers
- Account registration and token retrieval
- Account upload to external account pool (ds2api)

**Why Code-Spec Depth**:
- Cross-layer data flow (DeepSeek API → email provider → ds2api → local storage)
- New API signatures and contracts
- Critical security considerations (token handling, sensitive data masking)

---

### 2. Signatures

**Core Function Signature**:

```python
async def run_deepseek(
    proxy: Optional[str] = None,
    email_provider: str = "mail.tm",
    stop_event: Optional[threading.Event] = None,
    ds2api_url: Optional[str] = None,
    ds2api_key: Optional[str] = None
) -> Dict[str, Any]:
    """
    Execute DeepSeek registration workflow.

    Args:
        proxy: HTTP proxy URL (e.g., "http://127.0.0.1:7890")
        email_provider: Email provider name ("mail.tm", "moemail", "duckmail", "cloudflare")
        stop_event: Threading event for graceful interruption
        ds2api_url: ds2api base URL for account upload (optional)
        ds2api_key: ds2api admin key for authentication (optional)

    Returns:
        {
            "email": str,           # Registered email
            "password": str,        # Account password
            "token": str,           # DeepSeek Bearer token
            "uploaded": bool        # Whether uploaded to ds2api
        }

    Raises:
        RuntimeError: Registration workflow failed
        ValueError: Invalid input parameters
        TimeoutError: PoW solving or email verification timeout
    """
```

**PoW Solver Signature**:

```python
def solve_pow_challenge(
    challenge_data: Dict[str, Any],
    stop_event: Optional[threading.Event] = None
) -> int:
    """
    Solve DeepSeek PoW challenge.

    Args:
        challenge_data: {
            "algorithm": str,      # "DeepSeekHashV1"
            "challenge": str,      # Target hash (hex)
            "salt": str,           # Salt string
            "expire_at": int,      # Expiration timestamp
            "difficulty": int,     # Max nonce value
            "signature": str,      # Challenge signature
            "target_path": str     # API endpoint path
        }
        stop_event: Threading event for interruption

    Returns:
        int: Nonce value that solves the challenge

    Raises:
        RuntimeError: No solution found within difficulty range
        ValueError: Invalid challenge data
    """
```

**ds2api Upload Signature**:

```python
async def upload_to_ds2api(
    account: Dict[str, str],
    ds2api_url: str,
    admin_key: str
) -> bool:
    """
    Upload registered account to ds2api account pool.

    Args:
        account: {"email": str, "password": str, "name": str, "remark": str}
        ds2api_url: ds2api base URL (e.g., "http://localhost:8080")
        admin_key: ds2api admin key for authentication

    Returns:
        bool: True if upload succeeded, False otherwise

    Side Effects:
        - Saves failed uploads to data/failed_uploads.json
        - Logs success/failure with masked sensitive data
    """
```

---

### 3. Contracts

#### DeepSeek API Contracts

**1. Get PoW Challenge**:
- **Endpoint**: `POST https://chat.deepseek.com/api/v0/users/create_guest_challenge`
- **Request**: `{"target_path": "/api/v0/users/create_email_verification_code"}`
- **Response**:
  ```json
  {
    "algorithm": "DeepSeekHashV1",
    "challenge": "abc123...",
    "salt": "xyz789",
    "expire_at": 1234567890,
    "difficulty": 144000,
    "signature": "sig456",
    "target_path": "/api/v0/users/create_email_verification_code"
  }
  ```
- **Headers**: `Content-Type: application/json`

**2. Send Verification Code**:
- **Endpoint**: `POST https://chat.deepseek.com/api/v0/users/create_email_verification_code`
- **Headers**:
  - `Content-Type: application/json`
  - `x-ds-pow-response`: Base64-encoded JSON with PoW solution
- **Request**: `{"email": "user@example.com", "scenario": "register"}`
- **Response**: `{"success": true}` or error message
- **Error Codes**:
  - 400: Invalid email or PoW solution
  - 429: Rate limited

**3. Submit Registration**:
- **Endpoint**: `POST https://chat.deepseek.com/api/v0/users/register`
- **Headers**:
  - `Content-Type: application/json`
  - `x-ds-pow-response`: Base64-encoded JSON with registration PoW
- **Request**:
  ```json
  {
    "email": "user@example.com",
    "email_verification_code": "123456",
    "password": "securepassword"
  }
  ```
- **Response**:
  ```json
  {
    "user": {
      "id": "user_xxx",
      "token": "sk-abc123...",
      "email": "user@example.com"
    }
  }
  ```

#### ds2api Upload Contract

**Endpoint**: `POST {ds2api_url}/admin/import`

**Headers**:
- `Content-Type: application/json`
- `Authorization: Bearer {admin_key}`

**Request**:
```json
{
  "accounts": [
    {
      "email": "user@example.com",
      "password": "securepassword",
      "name": "Auto-registered from ds-register",
      "remark": "Created at 2026-05-01 20:30"
    }
  ]
}
```

**Response**:
```json
{
  "success": true,
  "imported_accounts": 1
}
```

**Error Responses**:
- 400: Invalid request body
- 401: Invalid admin key
- 200 with `imported_accounts: 0`: Duplicate account

#### Local Storage Contract

**File**: `data/registered_accounts.json`

**Format**:
```json
[
  {
    "email": "user@example.com",
    "password": "securepassword",
    "token": "sk-abc123...",
    "registered_at": "2026-05-01T20:30:00",
    "uploaded_to_ds2api": true
  }
]
```

**File**: `data/failed_uploads.json`

**Format**:
```json
[
  {
    "email": "user@example.com",
    "password": "securepassword",
    "failed_at": "2026-05-01T20:35:00",
    "reason": "ds2api connection failed"
  }
]
```

---

### 4. Validation & Error Matrix

| Condition | Error Type | HTTP Status | Action |
|-----------|------------|-------------|--------|
| Invalid email format | `ValueError` | - | Validate before API call |
| PoW solving timeout | `TimeoutError` | - | Stop event triggered, clean exit |
| PoW no solution found | `RuntimeError` | - | Log difficulty, raise error |
| DeepSeek API 429 | `RuntimeError` | 429 | Log rate limit, suggest retry |
| DeepSeek API 400 | `RuntimeError` | 400 | Log error detail, check input |
| Email verification timeout | `TimeoutError` | - | Poll timeout (5 min), clean exit |
| Invalid OTP code | `RuntimeError` | - | Retry or raise after max attempts |
| ds2api upload 401 | `RuntimeError` | 401 | Log auth error, skip upload |
| ds2api upload 400 | `RuntimeError` | 400 | Log error, save to failed_uploads.json |
| Proxy connection failed | `RuntimeError` | - | Fallback to no proxy, log warning |
| Token missing in response | `RuntimeError` | - | Log response, raise error |

**Validation Rules**:

```python
# Email format validation
import re
EMAIL_PATTERN = re.compile(r'^[a-zA-Z0-9_.+-]+@[a-zA-Z0-9-]+\.[a-zA-Z0-9-.]+$')

def validate_email(email: str) -> None:
    if not email or not EMAIL_PATTERN.match(email):
        raise ValueError(f"Invalid email format: {email}")

# Password strength validation
def validate_password(password: str) -> None:
    if len(password) < 8:
        raise ValueError("Password must be at least 8 characters")

# ds2api URL validation
def validate_ds2api_url(url: str) -> None:
    if not url.startswith(("http://", "https://")):
        raise ValueError(f"Invalid ds2api URL: {url}")
```

---

### 5. Good/Base/Bad Cases

#### Good Case: Successful Registration

```python
# Input: Valid proxy, email provider, ds2api config
result = await run_deepseek(
    proxy="http://127.0.0.1:7890",
    email_provider="mail.tm",
    ds2api_url="http://localhost:8080",
    ds2api_key="admin-key-123"
)

# Output:
{
    "email": "abc123@mail.tm",
    "password": "generatedPassword123",
    "token": "sk-abc123xyz...",
    "uploaded": True
}

# Side effects:
# - Account saved to data/registered_accounts.json
# - Account uploaded to ds2api
# - Logs: "DeepSeek registration completed" with masked token
```

#### Base Case: Registration without ds2api Upload

```python
# Input: No ds2api config
result = await run_deepseek(
    proxy="http://127.0.0.1:7890",
    email_provider="mail.tm"
)

# Output:
{
    "email": "xyz789@mail.tm",
    "password": "generatedPassword456",
    "token": "sk-xyz789...",
    "uploaded": False
}

# Side effects:
# - Account saved to data/registered_accounts.json
# - No ds2api upload attempted
# - Logs: "DeepSeek registration completed (no ds2api upload)"
```

#### Bad Case: PoW Solving Failure

```python
# Input: Invalid proxy, high difficulty
try:
    result = await run_deepseek(
        proxy="http://invalid-proxy:9999",
        email_provider="mail.tm"
    )
except RuntimeError as e:
    # Error: "PoW solving failed: no solution found"
    # Logs: "PoW solving timeout" with difficulty and duration
    # No account saved
```

#### Bad Case: ds2api Upload Failure

```python
# Input: Invalid ds2api key
result = await run_deepseek(
    proxy="http://127.0.0.1:7890",
    email_provider="mail.tm",
    ds2api_url="http://localhost:8080",
    ds2api_key="invalid-key"
)

# Output:
{
    "email": "user@mail.tm",
    "password": "password123",
    "token": "sk-abc...",
    "uploaded": False
}

# Side effects:
# - Account saved to data/registered_accounts.json
# - Failed upload saved to data/failed_uploads.json
# - Logs: "ds2api upload failed: 401 Unauthorized"
```

---

### 6. Tests Required

**Unit Tests** (`tests/test_deepseek_pow.py`):

```python
import pytest
from core.deepseek_register import (
    DeepSeekHashV1,
    solve_pow_challenge,
    build_pow_header
)

def test_hash_algorithm_correctness():
    """Test DeepSeekHashV1 produces correct output."""
    # Known test vector
    input_data = b"test_input_123"
    expected_hash = "abc123..."  # Pre-computed expected value

    result = DeepSeekHashV1(input_data)
    assert result.hex() == expected_hash

def test_pow_solving_finds_valid_nonce():
    """Test PoW solver finds a valid nonce."""
    challenge_data = {
        "algorithm": "DeepSeekHashV1",
        "challenge": "0000abc123...",
        "salt": "test_salt",
        "expire_at": 9999999999,
        "difficulty": 1000,  # Low difficulty for fast test
        "signature": "test_sig",
        "target_path": "/api/test"
    }

    nonce = solve_pow_challenge(challenge_data)
    assert 0 <= nonce < challenge_data["difficulty"]

    # Verify solution
    prefix = f"{challenge_data['salt']}_{challenge_data['expire_at']}_"
    hash_result = DeepSeekHashV1((prefix + str(nonce)).encode())
    assert hash_result.hex().startswith(challenge_data["challenge"][:8])

def test_pow_solving_timeout():
    """Test PoW solver respects stop_event."""
    import threading
    import time

    challenge_data = {
        "algorithm": "DeepSeekHashV1",
        "challenge": "ffffffff...",  # Impossible challenge
        "salt": "test_salt",
        "expire_at": 9999999999,
        "difficulty": 10000000,  # High difficulty
        "signature": "test_sig",
        "target_path": "/api/test"
    }

    stop_event = threading.Event()
    stop_event.set()  # Immediately stop

    with pytest.raises(RuntimeError, match="PoW solving interrupted"):
        solve_pow_challenge(challenge_data, stop_event)

def test_pow_header_building():
    """Test x-ds-pow-response header format."""
    challenge_data = {
        "algorithm": "DeepSeekHashV1",
        "challenge": "abc123",
        "salt": "xyz789",
        "expire_at": 1234567890,
        "difficulty": 144000,
        "signature": "sig456",
        "target_path": "/api/test"
    }
    nonce = 12345

    header = build_pow_header(challenge_data, nonce)

    # Verify base64 encoding
    import base64
    import json
    decoded = json.loads(base64.b64decode(header))

    assert decoded["algorithm"] == "DeepSeekHashV1"
    assert decoded["answer"] == nonce
    assert decoded["challenge"] == "abc123"

def test_hash_determinism():
    """Test hash function produces consistent results."""
    input_data = b"test_data"

    hash1 = DeepSeekHashV1(input_data)
    hash2 = DeepSeekHashV1(input_data)

    assert hash1 == hash2

def test_hash_boundary_conditions():
    """Test hash function with edge cases."""
    # Empty input
    assert len(DeepSeekHashV1(b"")) == 32

    # Large input
    large_input = b"x" * 10000
    assert len(DeepSeekHashV1(large_input)) == 32
```

**Integration Tests** (Manual):

```python
# Test complete registration flow
async def test_full_registration_flow():
    """Test end-to-end DeepSeek registration."""
    result = await run_deepseek(
        email_provider="mail.tm",
        ds2api_url="http://localhost:8080",
        ds2api_key="test-admin-key"
    )

    assert "email" in result
    assert "password" in result
    assert "token" in result
    assert result["token"].startswith("sk-")

    # Verify token works
    # TODO: Make test API call to DeepSeek

# Test ds2api upload
async def test_ds2api_upload():
    """Test account upload to ds2api."""
    account = {
        "email": "test@example.com",
        "password": "test_password",
        "name": "Test Account",
        "remark": "Integration test"
    }

    success = await upload_to_ds2api(
        account,
        "http://localhost:8080",
        "test-admin-key"
    )

    assert success == True

# Test failed upload caching
async def test_failed_upload_caching():
    """Test that failed uploads are cached."""
    account = {
        "email": "test@example.com",
        "password": "test_password"
    }

    # Attempt upload with invalid URL
    success = await upload_to_ds2api(
        account,
        "http://invalid-url:9999",
        "invalid-key"
    )

    assert success == False

    # Verify cache file created
    import json
    from pathlib import Path
    cache_file = Path("data/failed_uploads.json")
    assert cache_file.exists()

    with open(cache_file) as f:
        failed = json.load(f)
    assert any(a["email"] == account["email"] for a in failed)
```

---

### 7. Wrong vs Correct

#### Wrong: Bare Exception Handling

```python
# ❌ Wrong: Catch all exceptions silently
try:
    result = await run_deepseek()
except Exception:
    pass  # Error swallowed
```

#### Correct: Specific Exception Handling with Logging

```python
# ✅ Correct: Handle specific exceptions, log with context
try:
    result = await run_deepseek(
        proxy=proxy,
        email_provider=email_provider
    )
    logger.success("Registration completed", extra={
        "email": result["email"],
        "token": mask_token(result["token"])
    })
except ValueError as e:
    logger.error("Invalid input", extra={"error": str(e)})
    raise
except TimeoutError as e:
    logger.error("Registration timeout", extra={"error": str(e)})
    raise RuntimeError("Registration timeout - please retry") from e
except RuntimeError as e:
    logger.error("Registration failed", extra={"error": str(e)})
    raise
```

---

#### Wrong: Exposing Full Token in Logs

```python
# ❌ Wrong: Log full token
token = result["user"]["token"]
logger.info(f"Obtained token: {token}")
# Output: "Obtained token: sk-abc123xyz789verylongtoken..."
```

#### Correct: Mask Sensitive Data

```python
# ✅ Correct: Mask token before logging
token = result["user"]["token"]
logger.info(f"Obtained token: {token[:10]}...")
# Output: "Obtained token: sk-abc123xy..."
```

---

#### Wrong: Synchronous Blocking in Async Function

```python
# ❌ Wrong: Blocking call in async function
async def run_deepseek():
    # Blocking for up to 60 seconds!
    nonce = solve_pow_challenge(challenge_data)  # Synchronous
    await send_verification_code()
```

#### Correct: Run Blocking Code in Thread Pool

```python
# ✅ Correct: Offload blocking work to thread pool
import asyncio
from concurrent.futures import ThreadPoolExecutor

executor = ThreadPoolExecutor(max_workers=4)

async def run_deepseek():
    # Run PoW solving in thread pool
    loop = asyncio.get_event_loop()
    nonce = await loop.run_in_executor(
        executor,
        solve_pow_challenge,
        challenge_data
    )
    await send_verification_code()
```

---

#### Wrong: Hardcoded API URLs and Keys

```python
# ❌ Wrong: Hardcoded values
DEEPSEEK_URL = "https://chat.deepseek.com"
DS2API_KEY = "admin123"

async def upload(account):
    await requests.post(f"{DEEPSEEK_URL}/admin/import", ...)
```

#### Correct: Configurable with Environment Variables

```python
# ✅ Correct: Read from config/environment
import os

DEEPSEEK_BASE_URL = os.getenv("DEEPSEEK_BASE_URL", "https://chat.deepseek.com")
DS2API_URL = os.getenv("DS2API_URL")
DS2API_KEY = os.getenv("DS2API_KEY")

async def upload(account, ds2api_url: str, admin_key: str):
    if not ds2api_url or not admin_key:
        logger.warning("ds2api config missing, skipping upload")
        return False

    await requests.post(f"{ds2api_url}/admin/import", ...)
```

---

#### Wrong: No Retry on Transient Failures

```python
# ❌ Wrong: Fail immediately on first error
response = requests.post(url, json=data)
if response.status_code != 200:
    raise RuntimeError("Request failed")
```

#### Correct: Retry with Exponential Backoff

```python
# ✅ Correct: Retry transient failures
import time

async def call_with_retry(url: str, data: dict, max_retries: int = 3):
    for attempt in range(max_retries):
        try:
            response = requests.post(url, json=data, timeout=30)
            if response.status_code == 200:
                return response.json()
            elif response.status_code >= 500:
                # Server error - retry
                logger.warning(f"Server error, retrying ({attempt+1}/{max_retries})")
                time.sleep(2 ** attempt)  # Exponential backoff
                continue
            else:
                # Client error - don't retry
                raise RuntimeError(f"Request failed: {response.status_code}")
        except requests.exceptions.Timeout:
            if attempt < max_retries - 1:
                logger.warning(f"Timeout, retrying ({attempt+1}/{max_retries})")
                time.sleep(2 ** attempt)
                continue
            raise

    raise RuntimeError("Max retries exceeded")
```

---

## Integration Checklist

When adding a new third-party platform integration, verify:

### ✅ Contracts
- [ ] API endpoints documented with full URLs
- [ ] Request/response schemas defined
- [ ] Authentication headers specified
- [ ] Error codes enumerated

### ✅ Validation
- [ ] Input validation before API calls
- [ ] Response validation after API calls
- [ ] Sensitive data masking in logs

### ✅ Error Handling
- [ ] Specific exception types used
- [ ] Errors logged with context
- [ ] Graceful degradation when service unavailable
- [ ] Failed operations cached for retry

### ✅ Testing
- [ ] Unit tests for core algorithms (PoW, crypto)
- [ ] Integration tests for API interactions (manual)
- [ ] Error path tests (timeout, network failure)

### ✅ Security
- [ ] No secrets in logs or error messages
- [ ] Tokens masked before display
- [ ] Credentials loaded from environment/config
- [ ] HTTPS used for all external calls

### ✅ Performance
- [ ] Blocking operations offloaded to thread pool
- [ ] Long-running operations support interruption
- [ ] Optional JIT compilation for compute-heavy code

---

## Related Documentation

- [Error Handling](./error-handling.md) - Error handling patterns
- [Quality Guidelines](./quality-guidelines.md) - Code quality standards
- [Logging Guidelines](./logging-guidelines.md) - Structured logging
