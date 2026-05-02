# OpenAI 账号池编排器

自动化 OpenAI / DeepSeek 账号注册、Token 管理与账号池维护工具，支持 Web UI 和 CLI 两种运行模式。

## 功能特性

- **多平台注册支持** - 支持 OpenAI 和 DeepSeek 账号自动注册
- **Token 健康管理** - 自动刷新、判活、可用性分类
- **Sub2Api 账号池维护** - 探测、清理、补号自动化
- **多邮箱提供商** - 支持 Mail.tm、MoeMail、DuckMail 及自定义 API
- **代理池集成** - 支持代理池 API、TCP 检测、稳定代理优先
- **双模式运行** - Web UI 可视化管理 + CLI 命令行批处理
- **实时日志推送** - SSE 实时日志流，便于监控和调试

## 快速开始

### 系统要求

- Python >= 3.10
- Windows / Linux / macOS

### 安装

**方式一：使用 pip**

```bash
# 克隆仓库
git clone <repository-url>
cd ds-register

# 安装依赖
pip install -r requirements.txt
```

**方式二：使用 uv（推荐）**

```bash
# 克隆仓库
git clone <repository-url>
cd ds-register

# 使用 uv 同步依赖
uv sync
```

### 启动 Web 服务

```bash
# 基本启动（默认监听 http://0.0.0.0:18421）
python main.py

# 自定义监听地址和端口
python main.py --host 127.0.0.1 --port 8080

# 开启调试日志
python main.py --debug

# 开启热重载（开发环境）
python main.py --reload
```

启动后访问 http://localhost:18421 进入 Web 管理界面。

### 使用 CLI 模式

```bash
# 单次注册（OpenAI）
python main.py --cli --proxy http://127.0.0.1:7890 --once

# 单次注册（DeepSeek）
python main.py --cli --target deepseek --proxy http://127.0.0.1:7890 --once
```

## 配置说明

配置文件位于 `data/sync_config.json`，首次运行会自动生成。以下为典型配置场景：

### 场景一：最小配置（仅必填项）

```json
{
  "email": "your-email@example.com",
  "password": "your-password",
  "proxy": "http://127.0.0.1:7890"
}
```

**说明**：仅配置邮箱、密码和代理，适用于快速测试。

### 场景二：完整配置（包含可选优化）

```json
{
  "email": "your-email@example.com",
  "password": "your-password",
  "proxy": "http://127.0.0.1:7890",
  "auto_register": true,
  "multithread": true,
  "thread_count": 3,
  "debug_logging": false,
  "log_retention_days": 7,
  "listen_port": 18421
}
```

**说明**：开启自动注册、多线程、日志轮转等功能。

### 场景三：多邮箱提供商配置

```json
{
  "mail_providers": ["mailtm", "moemail"],
  "mail_provider_configs": {
    "mailtm": {
      "api_base": "https://api.mail.tm"
    },
    "moemail": {
      "api_base": "https://api.moemail.app"
    }
  },
  "mail_strategy": "round_robin"
}
```

**说明**：配置多个邮箱提供商，使用轮询策略。

### 场景四：代理池集成配置

```json
{
  "proxy_pool_enabled": true,
  "proxy_pool_api_url": "https://api.example.com/proxies",
  "proxy_pool_api_key": "your-api-key",
  "proxy_pool_country": "US",
  "proxy_pool_tcp_check_enabled": true,
  "proxy_pool_prefer_stable_proxy": true,
  "proxy_pool_count": 3
}
```

**说明**：启用代理池 API、TCP 检测、稳定代理优先。

### 配置项说明

| 配置项 | 类型 | 默认值 | 说明 |
|--------|------|--------|------|
| `email` | string | "" | 邮箱账号（必填） |
| `password` | string | "" | 邮箱密码（必填） |
| `proxy` | string | "" | 代理地址（必填，格式：http://host:port） |
| `auto_register` | bool | false | 是否开启自动注册 |
| `multithread` | bool | false | 是否开启多线程注册 |
| `thread_count` | int | 3 | 线程数量 |
| `listen_host` | string | "0.0.0.0" | Web 服务监听地址 |
| `listen_port` | int | 18421 | Web 服务监听端口 |
| `debug_logging` | bool | false | 是否开启 DEBUG 日志 |
| `log_retention_days` | int | 7 | 日志保留天数 |

完整配置项请查看运行时生成的 `data/sync_config.json`。

## 架构概览

### 目录结构

```
ds-register/
├── core/              # 核心代码
│   ├── __init__.py    # 包初始化、目录定义
│   ├── server.py      # FastAPI 服务、REST API、SSE
│   ├── register.py    # 注册主流程、CLI、OAuth/OTP
│   ├── pool_maintainer.py  # Sub2Api 账号池维护
│   ├── token_health.py     # Token 刷新与判活
│   ├── mail_providers.py   # 邮箱提供商适配层
│   └── deepseek_register.py  # DeepSeek 注册逻辑
├── static/            # 前端静态文件
│   ├── index.html     # Web UI 入口
│   ├── app.js         # 前端逻辑
│   └── style.css      # 样式表
├── data/              # 运行时数据（不纳入版本控制）
│   ├── sync_config.json  # 运行配置
│   ├── state.json        # 服务状态
│   ├── tokens/           # Token 存储
│   └── logs/             # 日志目录
├── tests/             # 测试套件
├── main.py            # 主入口
└── pyproject.toml     # 项目元数据
```

### 核心模块

**server.py** - FastAPI 后端服务
- REST API 端点：配置管理、注册任务、Token 查询
- SSE 实时日志推送
- 后台任务协调

**register.py** - 注册主流程
- CLI 参数解析
- 代理池获取
- OAuth/OTP 处理
- 注册流程编排

**pool_maintainer.py** - Sub2Api 账号池维护
- 探测账号可用性
- 清理异常账号
- 去重重复账号
- 自动补号

**token_health.py** - Token 健康管理
- Access Token 刷新
- Refresh Token 判活
- 可用性分类

**mail_providers.py** - 邮箱提供商适配
- 统一抽象接口
- 支持 Mail.tm、MoeMail、DuckMail
- 自定义 API 集成

## 高级用法

### CLI 参数

```bash
python main.py [选项]

选项：
  --cli                运行 CLI 注册模式
  --target {openai,deepseek}  注册目标平台（默认：openai）
  --debug              开启 DEBUG 日志
  --no-debug           关闭 DEBUG 日志
  --reload             开启 Granian 热重载
  --no-reload          关闭 Granian 热重载
  --anonymous          开启匿名日志脱敏
  --no-anonymous       关闭匿名日志脱敏
  --host HOST          监听地址
  --port PORT          监听端口
  --service-name NAME  服务显示名称
```

### 环境变量

支持通过环境变量覆盖配置：

```bash
# 日志配置
export OPENAI_POOL_DEBUG_LOGGING=1
export OPENAI_POOL_ANONYMOUS_MODE=1

# 服务配置
export OPENAI_POOL_LISTEN_HOST=127.0.0.1
export OPENAI_POOL_LISTEN_PORT=8080
export OPENAI_POOL_SERVICE_NAME="My Service"
export OPENAI_POOL_RELOAD=1
```

### Docker 部署

项目提供 `Dockerfile` 和 `docker-compose.yml`：

```bash
# 构建镜像
docker build -t ds-register .

# 运行容器
docker run -d -p 18421:18421 -v $(pwd)/data:/app/data ds-register

# 或使用 docker-compose
docker-compose up -d
```

### API 端点

**配置管理**
- `GET /api/sync-config` - 获取同步配置
- `POST /api/sync-config` - 更新同步配置
- `GET /api/runtime-config` - 获取运行时配置
- `POST /api/runtime-config` - 更新运行时配置

**注册任务**
- `POST /api/start` - 启动注册任务
- `POST /api/stop` - 停止注册任务
- `GET /api/status` - 获取服务状态

**Token 管理**
- `GET /api/tokens` - 获取 Token 列表（支持分页）
- `DELETE /api/tokens/{filename}` - 删除 Token
- `POST /api/tokens/import` - 导入 Token

**日志流**
- `GET /api/logs` - SSE 实时日志流

## 故障排查

### 常见问题

**Q: 启动时提示端口被占用？**

A: 修改监听端口：
```bash
python main.py --port 8080
```

或在配置文件中修改 `listen_port`。

**Q: 注册失败，提示代理错误？**

A: 检查代理配置：
1. 确认代理地址格式正确（http://host:port）
2. 测试代理可用性
3. 尝试使用其他代理

**Q: Web UI 无法访问？**

A: 检查防火墙和监听地址：
1. 确认服务已启动（查看日志）
2. 检查防火墙是否放行端口
3. 尝试使用 `--host 127.0.0.1` 绑定本地地址

**Q: 邮箱提供商连接失败？**

A: 检查邮箱配置：
1. 确认邮箱 API 地址正确
2. 检查网络连接
3. 尝试更换其他邮箱提供商

**Q: Token 刷新失败？**

A: 检查 Token 状态：
1. 确认 Refresh Token 有效
2. 检查网络和代理配置
3. 查看日志获取详细错误信息

### 日志查看

日志文件位于 `data/logs/` 目录：

```bash
# 查看最新日志
tail -f data/logs/app.log

# 搜索错误日志
grep ERROR data/logs/app.log
```

### 开启调试模式

```bash
# 方式一：命令行参数
python main.py --debug

# 方式二：环境变量
export OPENAI_POOL_DEBUG_LOGGING=1
python main.py
```

## 贡献指南

### 开发环境搭建

```bash
# 克隆仓库
git clone <repository-url>
cd ds-register

# 安装依赖
pip install -r requirements.txt

# 激活虚拟环境（Windows）
.venv\Scripts\activate

# 激活虚拟环境（Linux/macOS）
source .venv/bin/activate
```

### 运行测试

```bash
# 运行所有测试
python -m pytest tests/

# 运行特定测试
python -m pytest tests/test_config.py

# 查看测试覆盖率
python -m pytest --cov=core tests/
```

### 代码规范

- 遵循 PEP 8 代码风格
- 使用 4 空格缩进
- 函数和变量使用 `snake_case`
- 类使用 `PascalCase`
- 常量使用 `UPPER_SNAKE_CASE`

### 提交规范

采用 Conventional Commits 规范：

```
feat: 添加新功能
fix: 修复 Bug
docs: 文档更新
style: 代码格式调整
refactor: 重构代码
test: 测试相关
chore: 构建/工具链更新
```

### 安全建议

- 不要提交 `data/` 目录下的运行态文件
- 不要提交密钥、Token、代理池 Key 等敏感信息
- 使用 `.gitignore` 排除敏感文件
- 日志中避免输出完整的 Token 和凭据

## 许可证

MIT License

Copyright (c) 2026 OpenAI Pool Orchestrator Contributors

Permission is hereby granted, free of charge, to any person obtaining a copy
of this software and associated documentation files (the "Software"), to deal
in the Software without restriction, including without limitation the rights
to use, copy, modify, merge, publish, distribute, sublicense, and/or sell
copies of the Software, and to permit persons to whom the Software is
furnished to do so, subject to the following conditions:

The above copyright notice and this permission notice shall be included in all
copies or substantial portions of the Software.

THE SOFTWARE IS PROVIDED "AS IS", WITHOUT WARRANTY OF ANY KIND, EXPRESS OR
IMPLIED, INCLUDING BUT NOT LIMITED TO THE WARRANTIES OF MERCHANTABILITY,
FITNESS FOR A PARTICULAR PURPOSE AND NONINFRINGEMENT. IN NO EVENT SHALL THE
AUTHORS OR COPYRIGHT HOLDERS BE LIABLE FOR ANY CLAIM, DAMAGES OR OTHER
LIABILITY, WHETHER IN AN ACTION OF CONTRACT, TORT OR OTHERWISE, ARISING FROM,
OUT OF OR IN CONNECTION WITH THE SOFTWARE OR THE USE OR OTHER DEALINGS IN THE
SOFTWARE.
