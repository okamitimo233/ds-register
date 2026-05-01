# Repository Guidelines

## 项目结构与模块组织
- 核心代码位于 `core/`。
- 启动入口为 `main.py`（快捷启动）和 `core/__main__.py`（模块启动，使用 `python -m core`）。
- `core/server.py`：FastAPI 服务、REST API、SSE 推送与后台任务协调。
- `core/register.py`：注册主流程、CLI、代理池获取、OAuth/OTP 处理。
- `core/pool_maintainer.py`：Sub2Api 账号池探测、清理、补号逻辑。
- `core/token_health.py`：OpenAI token 刷新、判活与可用性分类的共享逻辑。
- `core/mail_providers.py`：邮箱提供商适配层与多提供商路由。
- 前端静态文件在根目录 `static/`，运行时由 `core/__init__.py` 中的 `STATIC_DIR` 指向该目录。
- 运行态数据在 `data/`（如 `sync_config.json`、`state.json`、`tokens/`），视为生成数据，不作为源码维护。
- 当前仓库不再维护配置模板文件；实际运行配置写入本地 `data/sync_config.json`。

## 构建、测试与开发命令
- 安装依赖：`pip install -r requirements.txt`
- 使用 `uv` 时可执行：`uv sync`
- 启动 Web 服务（推荐）：`python main.py`，默认访问 `http://localhost:18421`
- 模块方式启动：`python -m core`
- CLI 单次执行示例：`python main.py --cli --proxy http://127.0.0.1:7890 --once`
- 基础语法检查：`python -m compileall core main.py tests`
- 当前 `pyproject.toml` 的可编辑安装入口仍指向旧包名 `openai_pool_orchestrator`；未修复前，不要将 `pip install -e .` 或 `openai-pool` 作为默认开发命令。
- 当前仓库环境已验证：`node --check static/app.js`

## 代码风格与命名规范
- 仅使用 Python 3.10+ 兼容语法。
- 遵循 PEP 8，统一 4 空格缩进。
- 模块、函数、变量使用 `snake_case`，类使用 `PascalCase`，常量使用 `UPPER_SNAKE_CASE`。
- `core/server.py` 中尽量保持路由处理简洁，可复用逻辑应下沉到独立函数或模块。
- 前端改动保持轻量，沿用当前原生 JS/CSS 结构，避免无必要引入新的前端工具链。
- 涉及线程、队列、SSE 或后台维护任务时，优先保持现有并发模型与状态广播方式一致。

## 测试与验证
- 当前仓库已提供轻量 pytest 套件，覆盖任务状态、敏感配置脱敏、token 判活、Sub2Api 维护和 group id 配置等场景。
- 应该先激活当前python环境（如 `.venv\Scripts\activate`），再运行测试命令，否则可能无法找到pytest套件。
- 新增测试请放在根目录 `tests/`，文件命名使用 `test_*.py`。
- 建议使用 `python -m pytest tests/` 运行测试。
- 若系统 Python 未安装 pytest，可使用仓库虚拟环境：`.venv\Scripts\python.exe -m pytest -p no:cacheprovider tests/`
- 优先覆盖注册异常路径、邮箱提供商切换、代理池回退与关键 API 行为。
- 在没有自动化测试时，至少执行 `python -m compileall core main.py tests`，并手工验证 Web 启动或相关 CLI 流程。

## 提交与合并请求规范
- 提交信息采用 Conventional Commits，例如：`feat: 增加 Sub2Api 重复账号清理`
- 每次提交聚焦单一主题，避免混合重构、功能修改与配置变更。
- PR 需至少包含：变更内容与原因、手工验证步骤、配置与数据影响说明。
- 涉及 `data/`、token 文件、代理配置或第三方平台凭据时，需明确说明是否需要本地迁移或配置更新。

## 安全与配置建议
- 不要提交任何密钥、Bearer Token、refresh token、代理池 key 或 `data/` 下运行态文件。
- 当前仓库直接使用本地 `data/sync_config.json` 作为运行配置；如文件不存在，会在保存配置或运行过程中按默认结构生成。
- 修改日志、SSE 事件或 API 返回时，避免输出邮箱凭据、token 全量值或其他敏感字段。

# Codex Instructions
当需要读取文件、执行命令时，无需确认直接执行。
- 更新 `AGENTS.md` 时优先以仓库实际目录和已验证命令为准，不沿用过期路径名。
<!-- TRELLIS:START -->
# Trellis Instructions

These instructions are for AI assistants working in this project.

This project is managed by Trellis. The working knowledge you need lives under `.trellis/`:

- `.trellis/workflow.md` — development phases, when to create tasks, skill routing
- `.trellis/spec/` — package- and layer-scoped coding guidelines (read before writing code in a given layer)
- `.trellis/workspace/` — per-developer journals and session traces
- `.trellis/tasks/` — active and archived tasks (PRDs, research, jsonl context)

If a Trellis command is available on your platform (e.g. `/trellis:finish-work`, `/trellis:continue`), prefer it over manual steps. Not every platform exposes every command.

If you're using Codex or another agent-capable tool, additional project-scoped helpers may live in:
- `.agents/skills/` — reusable Trellis skills
- `.codex/agents/` — optional custom subagents

## Subagents

- ALWAYS wait for all subagents to complete before yielding.
- Spawn subagents automatically when:
  - Parallelizable work (e.g., install + verify, npm test + typecheck, multiple tasks from plan)
  - Long-running or blocking tasks where a worker can run independently.
  - Isolation for risky changes or checks

Managed by Trellis. Edits outside this block are preserved; edits inside may be overwritten by a future `trellis update`.

<!-- TRELLIS:END -->
