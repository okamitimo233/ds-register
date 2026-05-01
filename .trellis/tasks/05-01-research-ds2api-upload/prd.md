# 研究 ds2api 账号上传接口

## Goal

研究 ds2api 项目的账号上传/导入机制，确定 ds-register 项目如何将注册成功的 DeepSeek 账号（邮箱+密码）传递给 ds2api 账号池。

## Context

**主任务**: [`05-01-deepseek`](../05-01-deepseek) - 集成 DeepSeek 注册能力

**背景**:
- ds-register 项目负责自动注册 DeepSeek 账号
- ds2api 项目负责维护 DeepSeek 账号池（token 刷新、判活、清理等）
- 需要在注册成功后将账号信息传递给 ds2api

**项目路径**: `E:\ds2api`

## Research Questions

1. **优先方案：HTTP API 上传**
   - ds2api 是否提供了 HTTP API 端点用于上传账号？
   - 端点 URL、HTTP 方法、认证方式是什么？
   - 请求体格式（JSON 字段）是什么？
   - 响应格式和错误处理方式？

2. **回退方案：JSON 文件导入**
   - ds2api 是否支持从 JSON 文件批量导入账号？
   - 导入文件的路径、格式、字段要求是什么？
   - 导入触发方式（启动时自动加载？定时扫描？手动触发？）

## What to Look For

**项目结构**:
- `E:\ds2api\README.md` 或 `E:\ds2api\docs/` - 项目文档
- `E:\ds2api\internal/` - 内部实现（路由、handler、服务层）
- `E:\ds2api\api/` - API 定义（如果有）
- `E:\ds2api\cmd/` - 命令行入口
- `E:\ds2api\config/` - 配置文件

**搜索关键词**:
- account, accounts
- upload, import, batch
- admin, management
- route, router, handler
- json, file

**期望输出**:
- 如果找到 API 端点：记录完整的 API 规范（URL、Method、Headers、Body、Response）
- 如果使用文件导入：记录文件格式、路径、触发方式
- 提供代码示例（如果有现成的使用示例）
- 列出相关代码文件路径供参考

## Constraints

- 只做研究，不修改任何代码
- 结果持久化到 `research/ds2api-upload.md`
- 提供可操作的接入方案（明确步骤和参数）

## Acceptance Criteria

- [ ] 明确了接入方案（API 或文件导入）
- [ ] 提供了具体的接口规范或文件格式
- [ ] 包含代码示例或使用步骤
- [ ] 列出了相关代码文件路径

## Definition of Done

- 研究结果写入 `research/ds2api-upload.md`
- 主任务 PRD 已更新，包含研究结果链接
- 研究结果足够清晰，可以直接指导实现

## Technical Notes

**已知的 ds2api 技术栈**（从 pow 模块推断）:
- Go 语言
- 可能有 `internal/` 目录存放内部实现
- 可能使用标准库 `net/http` 或框架（gin、echo 等）

**相关的已知文件**:
- `E:\ds2api\pow\` - PoW 算法实现（已研究）
- `E:\ds2api\internal\deepseek\pow.go` - 服务侧 PoW 适配层
