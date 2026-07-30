# Trace-backed Memory

[English](README.md) | **简体中文**

面向 LLM 与 Agent harness 工程、由 provenance 支撑的记忆层。

Trace-backed Memory 把 Agent Trace、评测结果与 Git 证据转化为经过审查、带作用域且
可审计的工程记忆：

```text
Trace -> Failure Case -> Verified Lesson -> Gated Runtime Memory
```

[文档索引](docs/index.zh-CN.md) ·
[当前状态](docs/status/current-capability-matrix.zh-CN.md) ·
[详细参考](docs/reference.zh-CN.md) ·
[产品与当前能力](docs/product.md) ·
[架构](docs/architecture.zh-CN.md) ·
[使用策略](docs/usage-policy.zh-CN.md) ·
[交付计划](docs/product-program.zh-CN.md)

## 为什么需要它

这不是通用聊天记忆，也不是原始 transcript 仓库。它遵守五项核心保证：

- Raw Trace 始终是证据，默认不会成为 prompt memory；
- 模型不能验证或激活自己提出的 Lesson；
- System Gate 的 block 不能被 LLM 重新打开；
- runtime renderer 只使用最终允许的 memory 集合；
- 每次注入都关联 Trace 与可审计 decision。

完整能力矩阵见[产品契约](docs/product.md)，完整系统模型见
[架构文档](docs/architecture.zh-CN.md)。

## 快速开始

需要 Python 3.11 或更高版本。

```powershell
python -m pip install -e .
```

```python
from trace_backed_memory import (
    LocalAgentMemory,
    MemoryContext,
    MemoryRunMeasurement,
    capture_local_trace,
)

trace = capture_local_trace(".", tenant="acme")
context = MemoryContext(
    mode="repair",
    repo=trace.repo,
    tenant=trace.tenant,
    commit_sha=trace.commit_sha,
)

with LocalAgentMemory.open_sqlite("tbm-memory.sqlite3") as memory:
    prepared = memory.prepare(trace, context, task="repair the failed run")
    finalized = memory.finalize(
        prepared.request_id,
        {
            "use_memory": False,
            "allowed_memory_ids": [],
            "blocked_memory_ids": [],
            "reason": "No prepared lesson is needed.",
            "risk": "none",
            "recommended_injection": "none",
        },
    )
    memory.complete(
        finalized.decision_id,
        MemoryRunMeasurement(eval_result="pass"),
    )
```

协议细节与生命周期约束见
[`tbm.agent.v1` 指南](docs/protocols/agent-v1.zh-CN.md)。

## 两分钟连接 MCP + Codex（也支持 Claude Code 与 Pi）

安装 MCP 可选依赖并创建项目本地状态目录：

Windows PowerShell：

```powershell
py -m pip install -e ".[mcp]"
New-Item -ItemType Directory -Force .tbm,.codex
```

macOS 或 Linux：

```bash
python3 -m pip install -e '.[mcp]'
mkdir -p .tbm .codex
```

Codex Desktop、Codex CLI 与 IDE 扩展共享 MCP 配置。把以下项目级文件保存到仓库：

```toml
# .codex/config.toml
[mcp_servers.trace_backed_memory]
command = "tbm-mcp"
args = ["--repo-path", ".", "--sqlite", ".tbm/memory.sqlite3"]
```

重新打开受信任仓库，重启当前 Codex 端，并让 Codex 调用 `tbm_capabilities`。
[Codex 多端指南](docs/integrations/codex.zh-CN.md)覆盖 Desktop、CLI、IDE 扩展、
故障排查与可跨重启的 durable profile。其他客户端：

- **Claude Code：**执行
  [单命令配置](docs/integrations/claude-code.zh-CN.md#连接-claude-code)，用
  `claude mcp get trace-backed-memory` 验证，再打开 `/mcp`。
- **Pi + `pi-mcp-adapter`：**把该 adapter 安装为 Pi 的 MCP 客户端，再按
  [Pi 客户端教程](docs/integrations/pi.zh-CN.md#连接-pi)完成配置；授予项目信任前
  应先审查这个可执行 adapter。

使用默认兼容 profile 的客户端必须让 `tbm-mcp` 在完整的
`prepare -> finalize -> complete` 生命周期内保持存活；若不再执行，则应在 finalize
前调用 `cancel`。该服务只暴露 runtime operation，不能 review、verify 或 activate memory。

如需可跨重启续接的 GateSession state，请使用进阶的显式
[durable MCP profile](docs/protocols/durable-mcp-v1.zh-CN.md)。

## 接口

- Python：`TraceBackedMemoryStore` 与 `LocalAgentMemory`
- CLI：`tbm capabilities`、snapshot 操作、migration preflight 与资源发现
- 本地 MCP：安装可选 `mcp` 依赖后使用 `tbm-mcp`
- 本地 HTTP SDK：兼容 profile 提供同步/异步 Python 与 Node.js TypeScript，
  显式 durable-v3 profile 也提供 Python/TypeScript client；详见
  [兼容指南](docs/protocols/agent-http-v1.zh-CN.md)与
  [durable 指南](docs/protocols/durable-http-v1.zh-CN.md)
- 可选认证本地 MCP：可信启动配置选择 version-3 identity/environment；详见
  [参考文档](docs/reference.zh-CN.md#长驻本地-mcp)
- 可跨重启的本地 MCP：显式 `--profile durable-v3`；详见
  [durable MCP 指南](docs/protocols/durable-mcp-v1.zh-CN.md)
- 持久化：内存、SQLite 与 PostgreSQL adapter
- Version-3 准备能力：认证 retrieval 前边界、GateSession、授权、实体注册表、
  replay、audit/recovery、结构化 evidence、不可变 revision、retrieval snapshot、
  gate evaluation 与 outcome

每个协议、migration、integration 与运维指南都可从
[文档索引](docs/index.zh-CN.md)进入。Canonical Schema 与示例可通过
`tbm resource list` 或 Python resource API 获取。

## 当前边界

active 兼容边界仍是 snapshot version 2、SQLite schema version 1、PostgreSQL
schema version 2 与 `tbm.agent.v1`。Version-3 契约及其隔离、opt-in repository
不会暗中改变默认 Store、Agent 或 MCP 生命周期。显式 durable HTTP 与可信本地
MCP profile 会选择 version-3 authority graph。

默认兼容 profile 的 pending gate request 仍是进程内状态；显式 durable profile
会持久化 GateSession state。Scope matching 不是 tenant authorization。不要把当前
Alpha 作为不受信任的共享多租户服务部署。

精确交付行为见[产品与当前能力](docs/product.md)，operator 规则见
[记忆使用策略](docs/usage-policy.zh-CN.md)。

## 开发

```powershell
python -m pip install -e ".[dev]"
python tools/verify.py --fast
python tools/verify.py --full
python tools/verify.py --full --postgres
python tools/verify.py --all
```

涉及 PostgreSQL 行为的变更必须执行 PostgreSQL 验证。runtime、测试与验证工具不得
增加隐式网络访问。环境与发行验证细节见
[开发与验证](docs/development.zh-CN.md)。

## 许可证

[MIT](LICENSE)
