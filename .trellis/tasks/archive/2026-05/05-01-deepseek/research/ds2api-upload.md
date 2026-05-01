# Research: ds2api Account Upload/Import Mechanism

- **Query**: How does ds-register transfer successfully registered DeepSeek accounts (email + password) to the ds2api account pool?
- **Scope**: Internal (ds2api project codebase)
- **Date**: 2026-05-01

## Findings

### Overview

ds2api provides **multiple HTTP API endpoints** for uploading/importing accounts. The recommended approach for automated integration is using the batch import endpoint.

### API Endpoints for Account Upload

#### 1. POST /admin/import (Recommended for Batch Import)

**Endpoint**: `POST /admin/import`

**Authentication**: Admin JWT or admin_key
- Header: `Authorization: Bearer <jwt_token>` or `Authorization: Bearer <admin_key>`

**Request Body**:
```json
{
  "keys": ["api-key-1", "api-key-2"],
  "accounts": [
    {
      "email": "user1@example.com",
      "password": "password123",
      "name": "Account 1",
      "remark": "Production account"
    },
    {
      "email": "user2@example.com",
      "password": "password456"
    }
  ]
}
```

**Response**:
```json
{
  "success": true,
  "imported_keys": 2,
  "imported_accounts": 2
}
```

**Features**:
- Batch import both API keys and accounts in single request
- Automatically deduplicates accounts based on email/mobile
- Merges with existing accounts (does not delete existing ones)
- Lightweight and suitable for automation

**Implementation**: `E:/ds2api/internal/httpapi/admin/configmgmt/handler_config_write.go:169-227`

---

#### 2. POST /admin/accounts (Single Account)

**Endpoint**: `POST /admin/accounts`

**Authentication**: Admin JWT or admin_key

**Request Body**:
```json
{
  "email": "user@example.com",
  "password": "password123",
  "name": "Main Account",
  "remark": "Primary production account",
  "proxy_id": "proxy_xxx"
}
```

**Response**:
```json
{
  "success": true,
  "total_accounts": 5
}
```

**Features**:
- Add one account at a time
- Validates proxy_id if provided
- Checks for duplicate email/mobile
- Returns total account count after addition

**Implementation**: `E:/ds2api/internal/httpapi/admin/accounts/handler_accounts_crud.go:77-109`

---

#### 3. POST /admin/config/import (Full Config Import)

**Endpoint**: `POST /admin/config/import?mode=merge` or `?mode=replace`

**Authentication**: Admin JWT or admin_key

**Request Body** (direct config):
```json
{
  "accounts": [
    {"email": "user@example.com", "password": "pwd"}
  ],
  "api_keys": [
    {"key": "api-key-1", "name": "Main Key"}
  ],
  "model_aliases": {
    "gpt-4o": "deepseek-v4-flash"
  }
}
```

**Request Body** (wrapped format):
```json
{
  "config": {
    "accounts": [...],
    "api_keys": [...]
  },
  "mode": "merge"
}
```

**Response**:
```json
{
  "success": true,
  "mode": "merge",
  "imported_keys": 1,
  "imported_accounts": 1,
  "message": "config imported"
}
```

**Features**:
- Two modes: `merge` (default) or `replace`
- `merge`: Appends new accounts, merges keys, updates settings
- `replace`: Completely replaces configuration
- Can import accounts, keys, model_aliases, runtime settings, etc.
- Suitable for full configuration synchronization

**Implementation**: `E:/ds2api/internal/httpapi/admin/configmgmt/handler_config_import.go:11-145`

---

### Account Data Structure

**Definition**: `E:/ds2api/internal/config/config.go:30-38`

```go
type Account struct {
    Name     string `json:"name,omitempty"`      // Optional: Display name
    Remark   string `json:"remark,omitempty"`    // Optional: Notes
    Email    string `json:"email,omitempty"`     // Required (if no mobile)
    Mobile   string `json:"mobile,omitempty"`    // Required (if no email)
    Password string `json:"password,omitempty"`  // Required for login
    Token    string `json:"token,omitempty"`     // Optional: Pre-existing token
    ProxyID  string `json:"proxy_id,omitempty"`  // Optional: Proxy binding
}
```

**Account Identifier Logic**: `E:/ds2api/internal/config/account.go:5-13`
- Returns `email` if present (trimmed)
- Otherwise returns normalized `mobile`
- Empty string if neither exists

**Required Fields**:
- At minimum: `email` OR `mobile` (at least one identifier)
- For ds-register use case: `email` + `password` (since registration uses email)

**Optional Fields**:
- `name`: Display name for the account
- `remark`: Notes or description
- `token`: Pre-existing DeepSeek token (will be managed by ds2api if not provided)
- `proxy_id`: ID of proxy configuration to bind

---

### Authentication Requirements

All admin endpoints require authentication:

**Method 1: Admin JWT Token**
```
Authorization: Bearer <jwt_token>
```
- Obtain JWT via `POST /admin/login` with admin password
- JWT validity period configurable via `admin.jwt_expire_hours` (default: 24h)

**Method 2: Admin Key (Direct)**
```
Authorization: Bearer <admin_key>
```
- Admin key configured via `DS2API_ADMIN_KEY` environment variable
- Simpler for automation (no JWT expiration)

---

### Integration Workflow

**Recommended integration flow from ds-register to ds2api**:

1. **ds-register** successfully registers a DeepSeek account (email + password)
2. **ds-register** calls `POST /admin/import` on ds2api with:
   ```json
   {
     "accounts": [
       {
         "email": "newly-registered@example.com",
         "password": "registered-password",
         "name": "Auto-registered from ds-register",
         "remark": "Created at 2026-05-01 20:30"
       }
     ]
   }
   ```
3. **ds2api** validates and adds the account to its pool
4. **ds2api** can now use this account for API requests

**Error Handling**:
- Duplicate account (email/mobile already exists): Request succeeds, `imported_accounts: 0`
- Invalid data: Returns `400 Bad Request` with `{"detail": "error message"}`
- Authentication failure: Returns `401 Unauthorized`

---

### Code References

| File Path | Description |
|---|---|
| `E:/ds2api/internal/httpapi/admin/configmgmt/handler_config_write.go` | Batch import handler (`POST /admin/import`) |
| `E:/ds2api/internal/httpapi/admin/configmgmt/handler_config_import.go` | Full config import handler (`POST /admin/config/import`) |
| `E:/ds2api/internal/httpapi/admin/accounts/handler_accounts_crud.go` | Single account CRUD handlers |
| `E:/ds2api/internal/config/config.go` | Account data structure definition |
| `E:/ds2api/internal/config/account.go` | Account identifier logic |
| `E:/ds2api/internal/httpapi/admin/configmgmt/routes.go` | Route registration |
| `E:/ds2api/API.md:978-1001` | API documentation for batch import |

---

### Configuration Example

**Example config.json** (from `E:/ds2api/config.example.json`):

```json
{
  "accounts": [
    {
      "name": "Main Account",
      "remark": "Primary production account",
      "email": "example1@example.com",
      "password": "your-password-1"
    },
    {
      "name": "Backup Account",
      "email": "example2@example.com",
      "password": "your-password-2"
    },
    {
      "mobile": "12345678901",
      "password": "your-password-3"
    }
  ]
}
```

---

## Recommendations

### For ds-register Integration

**Use `POST /admin/import`** because:
1. Simple request format
2. Batch support (can import multiple accounts at once)
3. Automatic deduplication
4. Returns clear import statistics
5. Suitable for automated workflows

**Example integration code** (pseudo-code):
```python
def push_account_to_ds2api(email, password, ds2api_base_url, admin_key):
    response = requests.post(
        f"{ds2api_base_url}/admin/import",
        headers={"Authorization": f"Bearer {admin_key}"},
        json={
            "accounts": [{
                "email": email,
                "password": password,
                "name": f"Auto-registered {email}",
                "remark": f"Created by ds-register at {datetime.now()}"
            }]
        }
    )
    return response.json()
```

---

## Caveats / Not Found

- No file-based import mechanism found (all import via HTTP API)
- No WebSocket or push notification mechanism for account status changes
- Token management is automatic (ds2api handles token refresh internally)
- Accounts without valid email/mobile identifier will be rejected by admin APIs
- Import endpoints clear `token` field for security (will be managed by ds2api)

---

## Related Documentation

- `E:/ds2api/API.md` - Complete API documentation
- `E:/ds2api/README.MD` - Project overview
- `E:/ds2api/config.example.json` - Configuration template
