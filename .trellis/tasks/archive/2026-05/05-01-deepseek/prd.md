# 集成 DeepSeek 注册能力

## Goal

参考已有的 OpenAI 注册方法，为项目集成 DeepSeek 的注册能力，使用户能够通过相同的架构（临时邮箱 + 代理池 + 事件日志）自动注册 DeepSeek 账号并获取 Bearer Token。

## What I already know

**从分析报告中了解：**
- DeepSeek 注册流程：工作量证明挑战 → 发送邮箱验证码 → 提交注册 → 直接获取 Bearer Token
- 核心端点：
  - `/api/v0/users/create_guest_challenge` - 获取 PoW 挑战
  - `/api/v0/users/create_email_verification_code` - 发送验证码
  - `/api/v0/users/register` - 提交注册
- 鉴权机制：注册成功后在响应体中直接返回 `user.token`，后续请求使用 `Authorization: Bearer <token>`
- 工作量证明算法：`DeepSeekHashV1`（具体实现需研究）
- 邮箱验证码格式：6位数字

**从现有代码了解：**
- 项目已有完善的邮箱提供商抽象层（`core/mail_providers.py`）
- 已有代理池支持（固定代理 + 动态代理池 relay）
- 已有 EventEmitter 日志系统用于 SSE 推送
- 已有 Token 存储和验证机制（`core/local_tokens.py`, `core/token_health.py`）
- OpenAI 注册流程使用 OAuth + Sentinel Token（工作量证明）

## Assumptions (confirmed)

- DeepSeek PoW 算法已由 ds2api 项目用 Go 实现（`E:\ds2api\pow/`），需要移植到 Python
- DeepSeek 注册流程比 OpenAI 简单（无 OAuth，注册成功直接返回 token）
- 可以复用现有的邮箱提供商（Mail.tm / MoeMail / DuckMail / Cloudflare）
- DeepSeek 账号池由外部项目 ds2api 维护，本项目只负责注册和上传
- 不需要实现 DeepSeek token 的自动刷新和判活（由 ds2api 负责）

## Dependencies

- **前置研究任务**: [`05-01-research-ds2api-upload`](../05-01-research-ds2api-upload) - 研究 ds2api 账号上传接口
  - 状态：planning
  - 目标：确定 ds2api 的账号上传机制（API 或文件导入）
  - 输出：`research/ds2api-upload.md`

## Open Questions

1. ~~**代码组织**：在 `core/register.py` 中添加 `run_deepseek()` 函数，还是创建独立的 `core/deepseek_register.py` 模块？~~
   - **已决策**：创建独立的 `core/deepseek_register.py` 模块（方案 B）
2. ~~**ds2api 上传接口**：具体的 API 端点和认证方式是什么？~~
   - **已决策**：使用 `POST /admin/import` 批量导入接口，认证方式为 `Authorization: Bearer <admin_key>`（详见 `research/ds2api-upload.md`）
3. ~~**前端配置**：账号上传行为需要配置哪些参数（ds2api 地址、认证密钥等）？~~
   - **已决策**：提供基础配置（ds2api 地址、认证 Token）
4. ~~**错误处理**：如果 ds2api 上传失败，是否需要本地缓存账号信息并重试？~~
   - **已决策**：本地缓存 + 手动重试（方案 B）
5. ~~**PoW 性能优化**：使用什么方案优化 PoW 算法性能？~~
   - **已决策**：使用 numba JIT 编译（方案 B）
6. ~~**测试策略**：如何测试 DeepSeek 注册功能？~~
   - **已决策**：单元测试覆盖 PoW 算法，其他部分手工测试（方案 2）

## Requirements (evolving)

### 核心功能
- 实现 DeepSeek 注册流程的核心函数（`run_deepseek()`）
- 实现 DeepSeekHashV1 PoW 算法（Python 移植）
- 复用现有的邮箱提供商抽象层
- 复用现有的代理池机制
- 复用 EventEmitter 日志系统
- 实现注册成功后调用 ds2api 上传接口

### Web UI 配置
- 前端支持配置 ds2api 上传接口地址和认证 Token
- 前端支持选择注册目标（OpenAI / DeepSeek）
- 显示注册进度和上传状态
- 提供失败账号的手动重试按钮

### 数据存储
- 本地保存注册成功的账号信息（邮箱、密码）作为备份
- 记录 ds2api 上传状态和失败原因
- 失败账号保存到本地 JSON 文件，支持手动重试上传

## Acceptance Criteria (evolving)

- [ ] DeepSeek 注册流程可以成功执行（邮箱创建 → PoW 求解 → 验证码获取 → 注册 → ds2api 上传）
- [ ] 支持通过 CLI 运行 DeepSeek 注册
- [ ] 支持 Web UI 配置和触发 DeepSeek 注册
- [ ] PoW 算法性能满足要求（difficulty=144000 时能在合理时间内求解）
- [ ] PoW 算法正确性通过单元测试验证（至少 3 个测试用例）
- [ ] 注册成功的账号信息保存到本地（作为备份）
- [ ] ds2api 上传接口调用成功，账号进入 ds2api 账号池
- [ ] 代理池和日志系统正常工作
- [ ] 错误处理完善（PoW 求解失败、验证码超时、ds2api 上传失败）
- [ ] 上传失败的账号保存到本地 JSON，支持手动重试

## Definition of Done (team quality bar)

- 代码通过 `python -m compileall core` 语法检查
- PoW 算法有单元测试覆盖，测试通过
- 日志输出清晰，便于调试
- 手工测试验证完整注册流程（至少 3 次成功注册）
- 前端配置界面可用且友好
- 依赖项已更新（requirements.txt 包含 numba）

## Out of Scope (explicit)

- DeepSeek token 的自动刷新和判活（由 ds2api 负责）
- DeepSeek 聊天会话创建和对话功能（由 ds2api 负责）
- DeepSeek 账号池的生命周期管理（由 ds2api 负责）
- 通用的"AI 平台注册抽象层"（本项目只专注于 OpenAI 和 DeepSeek）
- 为未来其他 AI 平台预留扩展点

## Technical Notes

### 关键文件
- `core/register.py` - OpenAI 注册主流程，2483 行，包含 EventEmitter、代理池、OAuth 逻辑
- `core/mail_providers.py` - 邮箱提供商抽象层，支持 4 种提供商
- `core/token_health.py` - OpenAI token 验证逻辑（DeepSeek 不需要）
- `core/local_tokens.py` - 本地 token 存储

### DeepSeek API 端点（从分析报告）
- **Base URL**: `https://chat.deepseek.com`
- **获取 PoW 挑战**: `POST /api/v0/users/create_guest_challenge`
  - Request: `{"target_path": "/api/v0/users/create_email_verification_code"}`
  - Response: `{algorithm, challenge, salt, expire_at, difficulty, signature, target_path}`
- **发送验证码**: `POST /api/v0/users/create_email_verification_code`
  - Headers: `x-ds-pow-response: base64(json({algorithm, challenge, salt, answer, signature, target_path}))`
  - Request: `{"email": "xxx@example.com", "scenario": "register"}`
- **提交注册**: `POST /api/v0/users/register`
  - Headers: `x-ds-pow-response: <new_pow_header>`
  - Request: `{"email": "xxx@example.com", "email_verification_code": "123456", "password": "xxx"}`
  - Response: `{user: {id, token, ...}}`

### DeepSeek PoW 算法（从 ds2api 移植）

**核心算法**：
- DeepSeekHashV1 = SHA3-256 但跳过 Keccak-f[1600] round 0（只做 rounds 1..23）
- rate=136, padding=0x06+0x80, output=32 字节

**求解流程**：
1. 从 API 获取 challenge: `{algorithm, challenge, salt, expire_at, difficulty, signature, target_path}`
2. 构建 prefix = `salt + "_" + str(expire_at) + "_"`
3. 遍历 nonce ∈ [0, difficulty)，计算 `DeepSeekHashV1(prefix + str(nonce))`
4. 如果 hash 结果的前 32 字节匹配 challenge，返回该 nonce
5. 构建 `x-ds-pow-response` header = base64(json({algorithm, challenge, salt, answer, signature, target_path}))

**Go 实现参考**：
- `E:\ds2api\pow\deepseek_hash.go` - DeepSeekHashV1 和 Keccak-f[1600] rounds 1..23
- `E:\ds2api\pow\deepseek_pow.go` - SolvePow、BuildPowHeader、SolveAndBuildHeader

**Python 移植要点**：
- 使用 `struct.pack` 处理小端序 uint64
- 需要实现 `keccak_f23()` 函数（rounds 1..23）
- **性能优化**：使用 `numba.jit(nopython=True)` 加速核心哈希函数
  - 优点：接近原生速度，代码仍然是 Python
  - 缺点：需要安装 `numba` 依赖
  - 实现：对 `keccak_f23()` 和 `DeepSeekHashV1()` 使用 `@numba.jit` 装饰器

### 技术约束
- 使用 `curl_cffi.requests` 进行 HTTP 请求（绕过 TLS 指纹检测）
- 需要 `impersonate="chrome"` 参数
- 需要 `http_version="v2"` 或 `http_version="v1"`（根据代理支持情况）

### ds2api 集成（已确认）
- **上传账号**: `POST /admin/import` (推荐)
  - Headers: `Authorization: Bearer <admin_key>`
  - Request: `{"accounts": [{"email": "xxx", "password": "xxx", "name": "xxx", "remark": "xxx"}]}`
  - Response: `{"success": true, "imported_accounts": 1}`
  - 特性：自动去重、批量导入、轻量级
- **认证方式**：支持两种方式
  1. JWT Token（通过 `/admin/login` 获取，有效期 24h）
  2. Admin Key（环境变量 `DS2API_ADMIN_KEY`，更适合自动化）
- **数据结构**：
  ```json
  {
    "email": "user@example.com",  // 必填（或 mobile）
    "password": "password123",     // 必填
    "name": "Display Name",        // 可选
    "remark": "Notes",             // 可选
    "token": "",                   // 可选（由 ds2api 管理）
    "proxy_id": "proxy_xxx"        // 可选
  }
  ```
- **错误处理**：
  - 重复账号：请求成功，`imported_accounts: 0`
  - 数据无效：`400 Bad Request` + `{"detail": "error message"}`
  - 认证失败：`401 Unauthorized`
- **详细文档**：`research/ds2api-upload.md`

### 实现复杂度评估
- **PoW 算法移植**：中等难度（需要仔细移植位运算，可能需要性能优化）
- **注册流程实现**：简单（比 OpenAI OAuth 简单，无重定向链）
- **邮箱验证码**：简单（复用现有提供商）
- **代理池集成**：简单（复用现有机制）
- **ds2api 上传**：简单（HTTP POST 请求）
- **前端配置**：中等难度（需要新增配置项和状态展示）
