# 开发与验证

[English](development.md) | **简体中文**

## 环境

使用 Python 3.11 或更高版本：

```text
python -m pip install -e ".[dev]"
```

核心包没有强制第三方运行依赖。PostgreSQL 开发还需要 PostgreSQL 12+ server
tools 和 `postgres` 可选依赖。本地 STDIO MCP profile 使用 `mcp` 可选依赖；
`dev` extra 会安装两种 adapter 以便验证。
独立 TypeScript SDK 的开发需要 Node.js 20 或更高版本，且没有 runtime package
dependency。

## 统一验证入口

```text
python tools/verify.py --fast
python tools/verify.py --full
python tools/verify.py --full --postgres
```

fast 模式执行源码编译、Ruff、mypy 和 pytest。full 模式使用分支覆盖率，
随后在全新临时目录构建 wheel/sdist，并逐字节验证发行内容。`--postgres`
会设置 `TBM_REQUIRE_POSTGRES=1`，使缺少数据库前提成为失败。

## 变更检查

- 先阅读 `AGENTS.md` 与即将修改的确切实现。
- 策略留在内核；CLI、Agent 和持久化代码保持为适配器。
- 每个状态转换都增加拒绝路径与精确重放测试。
- 存储契约变更同步更新 domain、Schema、持久化、示例、资源和文档。
- 保持规范资源与安装副本字节一致。
- 没有显式 migration 与 verifier 时不得提升 schema version。

## 聚焦命令

```text
python -m pytest tests/test_agent.py -q
python -m pytest tests/test_mcp_server.py -q
python -m pytest tests/test_sqlite_repository.py -q
python -m pytest tests/test_postgres_repository.py -q
python -m ruff check src tools examples
python -m mypy src/trace_backed_memory
```

普通本地环境缺少 server tools 时，PostgreSQL 测试可以跳过；release 与 CI
qualification 必须强制执行。

TypeScript SDK 使用固定 toolchain；执行 `npm ci` 后运行独立、无网络的验证步骤：

```text
cd packages/typescript-sdk
npm ci --ignore-scripts
npm run check
npm test
npm run pack:check
```

`npm run check` 核验规范 OpenAPI binding，`npm test` 包含真实 Python HTTP
lifecycle，`pack:check` 检查可发布 package。专用 CI job 会运行全部四条命令；
Python `tools/verify.py` 保持不依赖 Node，也不会隐式安装 npm package。
