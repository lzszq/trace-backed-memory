# Durable MCP profile: `tbm.durable-agent-wire.v1`

**English** | [简体中文](durable-mcp-v1.zh-CN.md)

The explicit `tbm-mcp --profile durable-v3` profile maps the complete durable
Agent lifecycle to runtime-only MCP tools. It uses the same
`DurableRuntimeFactory` and unified SQLite or isolated PostgreSQL v3 authority
graph as the durable HTTP profile. The default `tbm-mcp` profile remains the
version-2 compatibility lifecycle.

## Trust boundary

- The MCP client launches a local STDIO child process. There is no independent
  peer-authentication handshake on that inherited pipe.
- An operator-owned application factory supplies
  `DurableRuntimeDependencies` and fixed trusted service, Semantic Gate
  provider, and outcome-evaluator contexts outside tool JSON.
- Tool request schemas reject caller, tenant, repository, environment,
  provider-authentication, evaluator-authentication, and authority identities.
- Current registry and authorization state is still rechecked by the durable
  services on each operation.
- Injection and replay content is hidden unless explicitly enabled at startup.
- Input frames use the same bounded, duplicate-key-rejecting JSONL STDIO
  transport as the compatibility MCP profile.

This is a trusted local-process profile. Startup configuration proves which
identities the process acts for; it does not authenticate an arbitrary peer
and must not be presented as a shared or untrusted multi-tenant service.

## Trusted application factory

Create an importable module in the operator-controlled environment:

```python
from trace_backed_memory.durable_mcp_entry import DurableMCPApplication
from trace_backed_memory.durable_mcp_server import DurableMCPTrustedContexts

from my_service.tbm_dependencies import (
    durable_runtime_dependencies,
    trusted_evaluator_context,
    trusted_provider_context,
    trusted_service_context,
)


def create_application() -> DurableMCPApplication:
    return DurableMCPApplication(
        dependencies=durable_runtime_dependencies(),
        contexts=DurableMCPTrustedContexts(
            service=trusted_service_context(),
            provider=trusted_provider_context(),
            evaluator=trusted_evaluator_context(),
        ),
    )
```

Create the database parent first (`mkdir -p .tbm` on macOS/Linux, or
`New-Item -ItemType Directory -Force .tbm` in PowerShell), then start a new
unified SQLite v3 database:

```bash
tbm-mcp \
  --profile durable-v3 \
  --application-factory my_service.tbm_mcp:create_application \
  --sqlite .tbm/durable.sqlite3 \
  --initialize
```

On later starts, omit `--initialize`. The factory path may instead come from
`TBM_DURABLE_MCP_APPLICATION_FACTORY`. PostgreSQL uses
`--postgres-env ENV_NAME`.

Content remains hidden by default. `--expose-injection-content` enables the
exact runtime snippet. `--expose-replay-content` additionally enables retained
replay bytes and therefore requires injection exposure.

## Runtime-only tools

The profile exposes only:

- `tbm_durable_capabilities`;
- `tbm_durable_prepare`;
- `tbm_durable_decide`;
- `tbm_durable_finalize`;
- `tbm_durable_start`;
- `tbm_durable_resume`;
- `tbm_durable_abandon`;
- `tbm_durable_complete`;
- `tbm_durable_cancel`;
- `tbm_durable_get_session`;
- `tbm_durable_export_replay`.

It exposes no extraction, review, verification, publication, activation, or
other curation tools. Tool annotations identify read-only operations and
idempotent durable transitions.

Capabilities report `transport_profile=durable-v3`,
`transport_security=trusted-local-stdio`, and
`peer_authentication=false`. Clients continue with a durable `session_id` and
the exact current GateSession version, never with a process-local handle.

## Restart and replay

SQLite and PostgreSQL session state belongs to the authority graph, not the MCP
process. After a restart, call `tbm_durable_get_session`, inspect the current
status and version, then continue the legal transition. Prepare, finalization,
start, cancellation, abandonment, and completion retries preserve the durable
services' exact replay rules. Replay export remains exact-version,
classification-bounded, freshly authorized, and disabled unless explicitly
enabled.

The real-process tests launch the SDK's STDIO client against three consecutive
MCP child processes: prepare in the first, continue and complete in the second,
then retry completion and export replay in the third.
