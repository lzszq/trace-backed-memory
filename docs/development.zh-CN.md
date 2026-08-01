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
python tools/verify.py --all
```

fast 模式执行源码编译、Ruff、mypy 和 pytest。full 模式使用分支覆盖率，
随后在全新临时目录构建 wheel/sdist，并逐字节验证发行内容。`--postgres`
会设置 `TBM_REQUIRE_POSTGRES=1`，使缺少数据库前提成为失败。
`--all` 是离线全仓门禁：还会核验持久化 authority registry 与规范 resource
manifest、强制 PostgreSQL、运行 `pip check`，并调用已经安装的 TypeScript
toolchain。它不会运行 `npm ci` 或下载工具。

## 变更检查

- 先阅读 `AGENTS.md` 与即将修改的确切实现。
- 策略留在内核；CLI、Agent 和持久化代码保持为适配器。
- 每个状态转换都增加拒绝路径与精确重放测试。
- 存储契约变更同步更新 domain、Schema、持久化、示例、资源和文档。
- 保持规范资源与安装副本字节一致。
- 运行 `python tools/verify_authority_registry.py`；每个新的
  `sqlite_*_v3.py` 或 `postgres_*_v3.py` 模块都必须声明 ledger、projection、
  migration 或 coordinator 角色及 event/projection 影响。
- 运行 `python tools/generate_sqlite_v3_bundle.py --check`；明确修改清单内的
  SQLite v3 component 后，先运行其 `--refresh`，再运行 resource generator。
- 运行 `python tools/generate_resources.py --check`；只有在修改 manifest 已列出的
  规范字节时，才显式使用 `--refresh` 与 `--write`。
- 没有显式 migration 与 verifier 时不得提升 schema version。

## 聚焦命令

```text
python -m pytest tests/test_agent.py -q
python -m pytest tests/test_mcp_server.py -q
python -m pytest tests/test_sqlite_repository.py -q
python -m pytest tests/test_postgres_repository.py -q
python tools/verify_projection_determinism.py
python -m ruff check src tools examples
python -m mypy src/trace_backed_memory
```

projection 确定性 verifier 会重放已提交的 canonical event fixture，并将精确的
projection digest 与已提交 golden 值比较。CI 会在 Ubuntu 和 Windows 上分别使用
Python 3.11、3.12 与 3.13 重复该检查；任何 digest 漂移都会使 matrix 失败。

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
