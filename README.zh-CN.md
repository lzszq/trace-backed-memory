# Trace-backed Memory

[English](README.md) | **简体中文**

面向 LLM 与 Agent harness 工程、由 provenance 支撑的记忆层。

Trace-backed Memory 把 Agent Trace、评测结果与 Git 证据转化为经过审查、带作用域且
可审计的工程记忆：

```text
Trace -> Failure Case -> Verified Lesson -> Gated Runtime Memory
```

[文档索引](docs/index.zh-CN.md) ·
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

## 两分钟启用 MCP + Codex

安装 MCP 可选依赖并创建项目本地状态目录。

Windows PowerShell：

```powershell
py -m pip install -e ".[mcp]"
New-Item -ItemType Directory -Force .tbm, .codex
```

macOS 或 Linux：

```bash
python3 -m pip install -e '.[mcp]'
mkdir -p .tbm .codex
```

Codex Desktop 与 Codex CLI 共用以下项目级 `.codex/config.toml`：

```toml
[mcp_servers.trace_backed_memory]
enabled = true
command = "tbm-mcp"
args = ["--repo-path", ".", "--sqlite", ".tbm/memory.sqlite3"]
```

在 Codex 中打开或信任该仓库，然后重启 Codex Desktop，或从仓库根目录启动新的
Codex CLI session。之后 Codex 可发现服务，并按以下顺序使用 runtime lifecycle：

```text
capabilities -> prepare -> finalize -> complete
                           `-> cancel
```

`tbm-mcp` 是长驻本地 STDIO server。从 `prepare` 到 `finalize` 或 `cancel` 必须保持
同一 server process 存活；当前 schema 的 pending request 是进程内状态。该服务只
暴露 runtime operation，不能 review、verify 或 activate memory。

完整 tool 顺序、存储选择、故障排查与安全边界见
[Codex 集成指南](docs/integrations/codex.zh-CN.md)。

## 接口

- Python：`TraceBackedMemoryStore` 与 `LocalAgentMemory`
- CLI：`tbm capabilities`、snapshot 操作、migration preflight 与资源发现
- 本地 MCP：安装可选 `mcp` 依赖后使用 `tbm-mcp`
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
不会暗中改变 active Store、Agent 或 MCP 生命周期。

在完成带版本的 durable GateSession migration 与 authenticated service integration
之前，pending gate request 仍是进程内状态。Scope matching 不是 tenant
authorization。不要把当前 Alpha 作为不受信任的共享多租户服务部署。

精确交付行为见[产品与当前能力](docs/product.md)，operator 规则见
[记忆使用策略](docs/usage-policy.zh-CN.md)。

## 开发

```powershell
python -m pip install -e ".[dev]"
python tools/verify.py --fast
python tools/verify.py --full
python tools/verify.py --full --postgres
```

涉及 PostgreSQL 行为的变更必须执行 PostgreSQL 验证。runtime、测试与验证工具不得
增加隐式网络访问。环境与发行验证细节见
[开发与验证](docs/development.zh-CN.md)。

## 许可证

[MIT](LICENSE)
