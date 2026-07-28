# Trace-backed Memory

**English** | [简体中文](README.zh-CN.md)

A provenance-backed memory layer for LLM and agent harness engineering.

Trace-backed Memory turns agent traces, evaluation results, and Git evidence
into reviewed, scoped, auditable memory:

```text
Trace -> Failure Case -> Verified Lesson -> Gated Runtime Memory
```

[Documentation](docs/index.md) ·
[Detailed reference](docs/reference.md) ·
[Product and capabilities](docs/product.en.md) ·
[Architecture](docs/architecture.md) ·
[Usage policy](docs/usage-policy.md) ·
[Delivery program](docs/product-program.md)

## Why it exists

This is not generic chatbot memory or a raw transcript store. It is an
engineering memory system with five core guarantees:

- raw traces remain evidence and are not prompt memory by default;
- a model cannot verify or activate its own lesson;
- System Gate blocks cannot be reopened by an LLM;
- runtime rendering uses only the final allowed memory set;
- every injection is linked to a Trace and an auditable decision.

See the [product contract](docs/product.en.md) for the complete capability
matrix and [architecture](docs/architecture.md) for the system model.

## Quick start

Python 3.11 or newer is required.

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

For protocol details and lifecycle constraints, read the
[`tbm.agent.v1` guide](docs/protocols/agent-v1.md).

## MCP clients in 2 minutes

Install the MCP profile and create project-local state:

Windows PowerShell:

```powershell
py -m pip install -e ".[mcp]"
New-Item -ItemType Directory -Force .tbm
```

macOS or Linux:

```bash
python3 -m pip install -e '.[mcp]'
mkdir -p .tbm
```

Then connect the client you use:

- **Codex Desktop and Codex CLI:** add the project-level
  [Codex configuration](docs/integrations/codex.md), then reopen
  the trusted repository.
- **Claude Code:** run the
  [one-command setup](docs/integrations/claude-code.md#connect-claude-code),
  verify it with `claude mcp get trace-backed-memory`, and open `/mcp`.
- **Pi + `pi-mcp-adapter`:** install the adapter as Pi's MCP client, then follow
  the [Pi client tutorial](docs/integrations/pi.md#connect-pi). Review the
  executable adapter before granting project trust.

All clients must keep `tbm-mcp` alive for the complete
`prepare -> finalize -> complete` lifecycle, or call `cancel` before
finalization. The server exposes runtime operations only; it cannot review,
verify, or activate memory.

## Interfaces

- Python: `TraceBackedMemoryStore` and `LocalAgentMemory`
- CLI: `tbm capabilities`, snapshot operations, migration preflight, and
  resource discovery
- Local MCP: `tbm-mcp` with the optional `mcp` dependency
- Opt-in authenticated local MCP: trusted startup selects version-3 identity
  and environment; see the [reference](docs/reference.md#long-running-local-mcp)
- Persistence: in-memory, SQLite, and PostgreSQL adapters
- Version-3 preparation: authenticated pre-retrieval boundary, GateSession,
  authorization, entity registry, replay, audit/recovery, structured evidence,
  immutable revisions, retrieval snapshots, gate evaluations, and outcomes

Use the [documentation index](docs/index.md) to reach each protocol, migration,
integration, and operations guide. Canonical Schemas and examples are
available through `tbm resource list` or the Python resource API.

## Current boundary

The active compatibility boundary remains snapshot version 2, SQLite schema
version 1, PostgreSQL schema version 2, and `tbm.agent.v1`. Version-3
contracts and their isolated opt-in repositories do not silently change the
active Store, Agent, or MCP lifecycle.

Pending gate requests remain process-local until a versioned durable
gate-session migration and authenticated service integration are complete.
Scope matching is not tenant authorization. Do not deploy the current Alpha as
an untrusted shared multi-tenant service.

Read [Product and current capabilities](docs/product.en.md) for exact delivered
behavior and [Memory usage policy](docs/usage-policy.md) for operator rules.

## Development

```powershell
python -m pip install -e ".[dev]"
python tools/verify.py --fast
python tools/verify.py --full
python tools/verify.py --full --postgres
```

PostgreSQL verification is required for changes to PostgreSQL behavior.
Runtime, tests, and verification tools must not add implicit network access.
See [Development and verification](docs/development.md) for environment and
distribution details.

## License

[MIT](LICENSE)
