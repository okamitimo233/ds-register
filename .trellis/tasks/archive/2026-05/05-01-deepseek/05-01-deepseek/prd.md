# 完成 DeepSeek 注册集成

## Goal

完成 DeepSeek 注册功能在 CLI 和 Web UI 中的集成，使用户能够通过现有架构（前端配置界面 + 后端 API）使用 DeepSeek 注册功能，并支持 ds2api 上传配置和失败账号重试。

## What I already know

**已完成的核心功能**：
- DeepSeek 注册核心逻辑（`core/deepseek_register.py`）：
  - PoW 算法实现（DeepSeekHashV1，支持 numba JIT 优化）
  - 注册流程实现（临时邮箱 → PoW 挑战 → 验证码 → 注册 → ds2api 上传）
  - 本地账号保存（`data/deepseek_accounts.json`）
  - 失败上传保存（`data/deepseek_failed_uploads.json`）
- 单元测试覆盖（`tests/test_deepseek_pow.py`）
- 示例脚本（`example_deepseek.py`）

**未完成的集成功能**：
1. **CLI 集成**：`main.py` 只支持 OpenAI 注册，缺少 `--target deepseek` 选项
2. **Web UI 集成**：
   - 前端无 DeepSeek 配置界面
   - 后端无 DeepSeek 注册路由（`/api/deepseek/start` 等）
   - 配置文件无 ds2api 上传相关字段
3. **失败账号重试接口**：虽然保存了上传失败的账号，但没有提供手动重试的 API 或前端按钮

**现有架构分析**：
- CLI 入口：`main.py` → `core/register.py:main()` → 仅支持 OpenAI
- Web UI 入口：`/api/start` → `TaskState.start_task()` → `_worker_loop()` → 调用 `run()`（OpenAI 注册）
- 配置管理：`data/sync_config.json` 通过 `_get_sync_config()` / `_save_sync_config()` 读写
- EventEmitter 日志系统用于 SSE 推送
- 前端使用原生 JS，静态文件在 `static/`

## Assumptions (confirmed)

- DeepSeek 注册核心逻辑已验证可用（单元测试通过）
- ds2api 上传接口已在 `core/deepseek_register.py` 中实现
- 现有代理池、邮箱提供商、EventEmitter 可直接复用
- DeepSeek 注册流程与 OpenAI 独立，无需共享状态
- 前端配置界面需要新增"注册目标"选择（OpenAI / DeepSeek）

## MVP Scope Decision

**用户选择**：
- ✅ 当前需求仅此而已（最小可行）：仅支持 CLI + Web UI 基础集成，ds2api 配置放在配置文件
- ✅ 添加配置验证：ds2api 配置保存前验证连接性和认证有效性
- ❌ 失败列表管理：留作后续优化，MVP 中不实现前端界面

**Open Questions 已决策**：

1. **CLI 参数设计** → 方案 B（复用配置文件）
   - DeepSeek 注册参数从 `data/sync_config.json` 读取
   - 新增 CLI 参数：`--target [openai|deepseek]`，默认 `openai`
   - 理由：避免命令行参数过多，配置文件更易管理

2. **Web UI 配置位置** → 方案 A（现有"同步配置"区域新增子区域）
   - 在"同步配置"面板底部新增"DeepSeek 上传配置"区域
   - 包含：启用开关、ds2api 地址、Admin Key、测试按钮
   - 理由：最小侵入性，保持界面简洁

3. **注册目标选择** → 方案 A（启动面板新增下拉框）
   - 在"启动"面板顶部新增"注册目标"下拉框（OpenAI / DeepSeek）
   - 选择 DeepSeek 时，显示 ds2api 配置状态提示
   - 理由：用户友好的切换方式，避免混淆

4. **失败重试范围** → 方案 A（仅 API，无前端）
   - 后端提供 `POST /api/deepseek/retry-failed` 批量重试 API
   - 前端不实现失败列表界面（后续优化）
   - 理由：MVP 优先核心功能，失败重试可手工调用 API

## Requirements (MVP)

### CLI 集成
- [ ] `main.py` 支持 `--target [openai|deepseek]` 参数，默认 `openai`
- [ ] CLI 模式下 `--target deepseek` 时从配置文件读取 ds2api 配置
- [ ] CLI 模式执行 DeepSeek 注册流程并输出结果
- [ ] 注册成功后保存账号信息到 `data/deepseek_accounts.json`
- [ ] 上传失败时保存到 `data/deepseek_failed_uploads.json`

### Web UI 集成 - 配置管理
- [ ] 配置文件新增字段：
  - `deepseek_ds2api_enabled`: bool（是否启用 ds2api 上传）
  - `deepseek_ds2api_url`: string（ds2api 服务地址）
  - `deepseek_ds2api_admin_key`: string（ds2api 管理员密钥）
- [ ] 前端"同步配置"面板新增"DeepSeek 上传配置"区域
- [ ] 前端提供 ds2api 配置表单（启用开关、URL、Admin Key）
- [ ] 后端 API `POST /api/deepseek/config` 保存 ds2api 配置
- [ ] 后端保存前验证 ds2api 连接性（调用 `/admin/import` 测试）
- [ ] 前端显示配置验证结果（成功/失败原因）

### Web UI 集成 - 注册任务
- [ ] 前端"启动"面板新增"注册目标"下拉框（OpenAI / DeepSeek）
- [ ] 选择 DeepSeek 时显示 ds2api 配置状态提示
- [ ] 后端 API `POST /api/deepseek/start` 启动 DeepSeek 注册任务
- [ ] 后端 API `POST /api/deepseek/stop` 停止 DeepSeek 注册任务
- [ ] 复用现有 SSE 事件流推送 DeepSeek 注册日志
- [ ] 复用现有任务状态展示（进度、成功/失败计数）

### 失败账号重试（API only）
- [ ] 后端 API `POST /api/deepseek/retry-failed` 批量重试失败上传
- [ ] 读取 `data/deepseek_failed_uploads.json` 并重新上传
- [ ] 返回重试结果（成功数/失败数）
- [ ] 前端不实现失败列表界面（后续优化）

### 数据存储
- [ ] `data/sync_config.json` 新增 DeepSeek 配置字段
- [ ] 注册成功的账号保存到 `data/deepseek_accounts.json`
- [ ] 上传失败的账号保存到 `data/deepseek_failed_uploads.json`

## Acceptance Criteria (MVP)

### CLI 集成
- [ ] `python main.py --cli --target deepseek` 可以启动 DeepSeek 注册
- [ ] CLI 模式正确读取 `data/sync_config.json` 中的 ds2api 配置
- [ ] CLI 模式注册成功后输出账号信息（邮箱、密码、token 前 10 位）
- [ ] CLI 模式注册失败时输出明确错误原因

### Web UI 配置
- [ ] 前端"同步配置"面板显示"DeepSeek 上传配置"区域
- [ ] 可以输入 ds2api URL 和 Admin Key 并保存
- [ ] 保存时自动验证连接性，显示验证结果（成功/失败原因）
- [ ] 配置保存到 `data/sync_config.json` 并持久化

### Web UI 注册
- [ ] 前端"启动"面板显示"注册目标"下拉框（OpenAI / DeepSeek）
- [ ] 选择 DeepSeek 时，显示 ds2api 配置状态（已配置/未配置）
- [ ] 点击"启动"后正确调用 `/api/deepseek/start`
- [ ] SSE 日志流正确显示 DeepSeek 注册步骤
- [ ] 任务状态正确显示（运行中/成功/失败）
- [ ] 可以停止正在运行的 DeepSeek 注册任务

### 失败重试
- [ ] `POST /api/deepseek/retry-failed` 可以重新上传失败账号
- [ ] API 返回重试统计（成功数/失败数）

### 质量标准
- [ ] 代码通过 `python -m compileall core main.py tests` 语法检查
- [ ] 代理池和日志系统正常工作
- [ ] 错误处理完善（PoW 求解失败、验证码超时、ds2api 上传失败）
- [ ] 手工测试验证完整注册流程（至少 1 次成功注册）

## Definition of Done (team quality bar)

- 代码通过 `python -m compileall core main.py tests` 语法检查
- 日志输出清晰，便于调试
- 手工测试验证完整注册流程（至少 1 次成功注册）
- 前端配置界面可用且友好
- 错误处理完善，用户可以看到明确的错误提示

## Out of Scope (explicit)

- DeepSeek token 的自动刷新和判活（由 ds2api 负责）
- DeepSeek 聊天会话创建和对话功能（由 ds2api 负责）
- DeepSeek 账号池的生命周期管理（由 ds2api 负责）
- 通用的"AI 平台注册抽象层"（本项目只专注于 OpenAI 和 DeepSeek）
- 自动重试失败上传（仅提供手动 API 重试）
- 失败账号前端列表管理（后续优化）
- 并行运行 OpenAI 和 DeepSeek 注册（后续优化）

## Technical Approach

### 架构设计

**复用现有架构**：
- EventEmitter 日志系统（SSE 推送）
- 代理池机制（proxy_pool_config）
- 邮箱提供商路由（MultiMailRouter）
- 配置管理（_get_sync_config / _save_sync_config）
- TaskState 状态管理

**新增模块**：
- DeepSeek 任务状态管理（独立于 OpenAI 任务）
- DeepSeek 配置验证逻辑
- DeepSeek 失败重试 API

### 实现方案

#### 1. CLI 集成

**文件修改**：
- `main.py`：新增 `--target` 参数解析
- `core/register.py:main()`：根据 target 调用不同注册函数

**实现逻辑**：
```python
# main.py
parser.add_argument("--target", choices=["openai", "deepseek"], default="openai")

# core/register.py
if args.target == "deepseek":
    from .deepseek_register import run_deepseek
    result = run_deepseek(
        proxy=proxy,
        emitter=emitter,
        ds2api_config={
            "enabled": cfg.get("deepseek_ds2api_enabled", False),
            "url": cfg.get("deepseek_ds2api_url", ""),
            "admin_key": cfg.get("deepseek_ds2api_admin_key", ""),
        }
    )
else:
    token_json = run(proxy=proxy, emitter=emitter)
```

#### 2. Web UI 配置管理

**前端新增元素**（static/index.html）：
```html
<!-- 在"同步配置"面板底部 -->
<div class="config-section">
  <h3>DeepSeek 上传配置</h3>
  <label>启用 ds2api 上传</label>
  <input type="checkbox" id="deepseek-ds2api-enabled">
  <label>ds2api 地址</label>
  <input type="text" id="deepseek-ds2api-url" placeholder="http://localhost:8080">
  <label>Admin Key</label>
  <input type="password" id="deepseek-ds2api-admin-key">
  <button onclick="testDeepSeekDs2apiConfig()">测试配置</button>
  <button onclick="saveDeepSeekDs2apiConfig()">保存配置</button>
</div>
```

**后端 API**（core/server.py）：
```python
class DeepSeekConfigRequest(BaseModel):
    deepseek_ds2api_enabled: bool = False
    deepseek_ds2api_url: str = ""
    deepseek_ds2api_admin_key: str = ""

@app.post("/api/deepseek/config")
async def api_set_deepseek_config(req: DeepSeekConfigRequest):
    # 1. 验证配置（如果启用）
    if req.deepseek_ds2api_enabled:
        test_result = await test_ds2api_connection(
            req.deepseek_ds2api_url,
            req.deepseek_ds2api_admin_key
        )
        if not test_result["ok"]:
            raise HTTPException(400, f"ds2api 配置验证失败: {test_result['error']}")
    
    # 2. 保存配置
    cfg = _get_sync_config()
    cfg.update({
        "deepseek_ds2api_enabled": req.deepseek_ds2api_enabled,
        "deepseek_ds2api_url": req.deepseek_ds2api_url,
        "deepseek_ds2api_admin_key": req.deepseek_ds2api_admin_key,
    })
    _save_sync_config(cfg)
    return {"status": "saved"}
```

**配置验证逻辑**：
```python
async def test_ds2api_connection(url: str, admin_key: str) -> Dict[str, Any]:
    try:
        resp = await httpx.post(
            f"{url}/admin/import",
            headers={"Authorization": f"Bearer {admin_key}"},
            json={"accounts": []},  # 空列表测试连接
            timeout=10.0
        )
        if resp.status_code == 200:
            return {"ok": True}
        else:
            return {"ok": False, "error": f"HTTP {resp.status_code}"}
    except Exception as e:
        return {"ok": False, "error": str(e)}
```

#### 3. Web UI 注册任务

**前端修改**（static/app.js）：
```javascript
// 启动面板新增下拉框
<select id="register-target">
  <option value="openai">OpenAI</option>
  <option value="deepseek">DeepSeek</option>
</select>

// 启动逻辑
async function startRegistration() {
  const target = document.getElementById('register-target').value;
  if (target === 'deepseek') {
    const resp = await fetch('/api/deepseek/start', {
      method: 'POST',
      body: JSON.stringify({proxy: currentProxy})
    });
    // ...
  } else {
    // 现有 OpenAI 逻辑
  }
}
```

**后端 API**（core/server.py）：
```python
# DeepSeek 任务状态（独立于 OpenAI）
_deepseek_state = TaskState()  # 复用现有 TaskState 类

@app.post("/api/deepseek/start")
async def api_deepseek_start(req: StartRequest):
    _deepseek_state.start_task(
        proxy=req.proxy,
        worker_count=req.worker_count,
        target="deepseek",  # 新增参数
    )
    return _deepseek_state.get_status_snapshot()

@app.post("/api/deepseek/stop")
async def api_deepseek_stop():
    _deepseek_state.stop_task()
    return _deepseek_state.get_status_snapshot()
```

**TaskState 修改**（core/server.py）：
```python
def start_task(self, proxy: str, worker_count: int = 1, target: str = "openai"):
    # ... 现有逻辑 ...
    
    def _worker_loop(worker_id: int):
        if target == "deepseek":
            result = run_deepseek(
                proxy=proxy,
                emitter=attempt_emitter,
                stop_event=self.stop_event,
                mail_provider=provider,
                proxy_pool_config={...},
                ds2api_config={
                    "enabled": config_snapshot.get("deepseek_ds2api_enabled", False),
                    "url": config_snapshot.get("deepseek_ds2api_url", ""),
                    "admin_key": config_snapshot.get("deepseek_ds2api_admin_key", ""),
                }
            )
            # 处理 result ...
        else:
            # 现有 OpenAI 逻辑
            token_json = run(...)
```

#### 4. 失败重试 API

```python
@app.post("/api/deepseek/retry-failed")
async def api_deepseek_retry_failed():
    """批量重试失败的 ds2api 上传"""
    failed_file = DATA_DIR / "deepseek_failed_uploads.json"
    if not failed_file.exists():
        return {"total": 0, "success": 0, "failed": 0}
    
    with open(failed_file, "r", encoding="utf-8") as f:
        failed_accounts = json.load(f)
    
    cfg = _get_sync_config()
    ds2api_url = cfg.get("deepseek_ds2api_url", "")
    admin_key = cfg.get("deepseek_ds2api_admin_key", "")
    
    success_count = 0
    failed_count = 0
    for account in failed_accounts:
        try:
            resp = requests.post(
                f"{ds2api_url}/admin/import",
                headers={"Authorization": f"Bearer {admin_key}"},
                json={"accounts": [{
                    "email": account["email"],
                    "password": account["password"],
                    "name": f"Retry {account['email']}",
                }]}
            )
            if resp.status_code == 200:
                success_count += 1
            else:
                failed_count += 1
        except Exception:
            failed_count += 1
    
    # 清空失败列表（或仅移除成功的）
    if success_count > 0:
        with open(failed_file, "w", encoding="utf-8") as f:
            json.dump([acc for acc in failed_accounts if ...], f)
    
    return {"total": len(failed_accounts), "success": success_count, "failed": failed_count}
```

### 关键设计决策

1. **独立任务状态**：DeepSeek 使用独立的 `TaskState` 实例，避免与 OpenAI 任务状态冲突
2. **复用 Worker 框架**：在 `_worker_loop` 中根据 `target` 参数调用不同的注册函数
3. **配置验证前置**：保存 ds2api 配置时立即验证，避免运行时失败
4. **最小前端改动**：仅在启动面板新增下拉框，复用现有日志展示和状态管理

## Technical Notes

### 关键文件
- `main.py` - CLI 入口，需要支持 `--target deepseek` 参数
- `core/register.py:main()` - OpenAI 注册 CLI 逻辑，需要复制类似逻辑用于 DeepSeek
- `core/deepseek_register.py` - DeepSeek 注册核心函数，已实现完整流程
- `core/server.py` - FastAPI 后端，需要新增 DeepSeek 注册路由
- `static/app.js` - 前端逻辑，需要新增 DeepSeek 配置和启动界面
- `static/index.html` - 前端 HTML，需要新增配置表单元素
- `data/sync_config.json` - 运行时配置，需要新增 DeepSeek/ds2api 字段

### DeepSeek 注册函数签名
```python
def run_deepseek(
    proxy: Optional[str],
    emitter,
    stop_event: Optional[threading.Event] = None,
    mail_provider=None,
    proxy_pool_config: Optional[Dict[str, Any]] = None,
    ds2api_config: Optional[Dict[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """
    Returns:
        成功时返回 {"email": str, "password": str, "token": str, "uploaded": bool}
        失败时返回 None
    """
```

### ds2api 配置格式
```json
{
  "enabled": true,
  "url": "http://your-ds2api-server:port",
  "admin_key": "your-admin-key"
}
```

### 配置文件字段（新增）
```json
{
  "deepseek_ds2api_enabled": false,
  "deepseek_ds2api_url": "",
  "deepseek_ds2api_admin_key": ""
}
```
