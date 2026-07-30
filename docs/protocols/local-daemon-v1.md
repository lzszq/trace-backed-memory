# Local durable daemon v1

**English** | [简体中文](local-daemon-v1.zh-CN.md)

`tbmd` is the explicit local SQLite v3 process owner. One `tbmd local`
process holds the unified authority graph, exposes the same
`tbm.durable-agent-wire.v1` dispatcher through bounded STDIO MCP and
bearer-authenticated loopback HTTP, and runs bounded GateSession recovery and
completion-outbox delivery pages.

This is a trusted single-host profile. It is not the remote, peer-authenticated
multi-tenant service.

## Install and provide trusted dependencies

Install both local transport extras:

```powershell
python -m pip install -e ".[mcp,service]"
```

The daemon does not infer repository, principal, provider, evaluator, or
consumer identity from request JSON. Create an importable operator-controlled
module such as `tbm_local_app.py`:

```python
from trace_backed_memory.daemon_entry import DurableLocalApplication
from trace_backed_memory.durable_http_server import (
    DurableHTTPAuthenticatedContexts,
)
from trace_backed_memory.durable_mcp_server import DurableMCPTrustedContexts

from my_service.tbm_dependencies import (
    completion_consumer,
    durable_runtime_dependencies,
    evaluator_context,
    provider_context,
    service_context,
)


def create_application() -> DurableLocalApplication:
    dependencies = durable_runtime_dependencies(
        completion_consumer=completion_consumer,
    )
    mcp_contexts = DurableMCPTrustedContexts(
        service_context(),
        provider_context(),
        evaluator_context(),
    )
    return DurableLocalApplication(
        dependencies=dependencies,
        mcp_contexts=mcp_contexts,
        http_context_provider=lambda _request: (
            DurableHTTPAuthenticatedContexts(
                service_context(),
                provider=provider_context(),
                evaluator=evaluator_context(),
            )
        ),
    )
```

`DurableRuntimeDependencies.completion_consumer` is mandatory for this
profile. Consumers must deduplicate by immutable outbox `event_id`; delivery is
at least once.

Set the factory and a private bearer secret of 32 or more characters:

```powershell
$env:TBM_DURABLE_DAEMON_APPLICATION_FACTORY = "tbm_local_app:create_application"
$env:TBM_DURABLE_HTTP_TOKEN = "<random-secret-at-least-32-characters>"
```

```bash
export TBM_DURABLE_DAEMON_APPLICATION_FACTORY='tbm_local_app:create_application'
export TBM_DURABLE_HTTP_TOKEN='<random-secret-at-least-32-characters>'
```

## Initialize, diagnose, and run

Initialize once, then verify the offline state:

```text
tbmd init --state-dir .tbm
tbmd doctor --state-dir .tbm
```

`init` atomically installs the generated 15-component SQLite v3 bundle in
`.tbm/durable.sqlite3`. `doctor` acquires the single-instance lock and verifies
the state directory, fixed database target, complete schema fingerprint,
trusted application factory, bearer format, and configured outbox consumer.
It intentionally fails while another daemon owns the state directory.

Run the complete local profile:

```text
tbmd local --state-dir .tbm
```

The default HTTP endpoint is `http://127.0.0.1:8766`. Check it without exposing
the token on the command line:

```text
tbmd health --base-url http://127.0.0.1:8766
```

For an SDK-only background process, explicitly use `--no-mcp`. The default
command reserves standard input/output for bounded MCP JSONL and starts HTTP
and workers in companion threads. Closing MCP STDIO, pressing Ctrl+C, or a
supported termination signal stops HTTP acceptance, joins workers, closes the
shared runtime, and releases the lock.

## MCP clients

After `tbmd init`, use the daemon itself as the project MCP command. Codex
project configuration is:

```toml
[mcp_servers.trace_backed_memory]
command = "tbmd"
args = ["local", "--state-dir", ".tbm"]
```

Claude Code and Pi use the same command and arguments in their respective MCP
configuration. Pi still requires the external
[`pi-mcp-adapter`](https://pi.dev/packages/pi-mcp-adapter?name=mcp) as its MCP
client; the adapter is not part of Trace-backed Memory.

Call `tbm_durable_capabilities` first. Persist the returned `session_id` and
exact session version after every mutation. A later daemon process reopens the
same SQLite graph and continues through `get_session`; it does not restore a
process-local handle.

## Recovery and delivery semantics

Each worker tick processes bounded pages:

- PREPARED or AWAITING_DECISION sessions whose session TTL expired transition
  to terminal `EXPIRED` through exact-version CAS.
- Lease-only expiry and due DECIDED, FINALIZED, or EXECUTING sessions remain
  `recovery_required`; the daemon does not invent an abandonment decision.
- Pending/retry-wait outbox deliveries and expired leases are claimed with a
  new worker lease. A crash after consumer execution but before acknowledgement
  may deliver the same immutable event again.
- Recovery and outbox operations share the same runtime RLock and SQLite
  connection as HTTP and MCP; separate dispatchers are not constructed.

Worker interval, page size, outbox lease, retry delay, and maximum attempts are
bounded CLI options. Unexpected worker failures are reduced to stable error
codes and retried on a later tick; request identities and secrets are never
written to the in-process worker status.

## State and security boundary

The state directory is canonical, non-aliased, readable/writable, and not a
Windows reparse point. Its ancestor chain must not be replaceable by another
local account. On POSIX the state directory must be owned by the current user
with no group or other permission bits; the database and persistent lock
placeholder must be owner-only single-link regular files. On Windows the
daemon rejects reparse/hard-link aliases and requires local
read/write/traverse access; operators remain responsible for applying an
owner-only ACL.

One held OS advisory lock on `.tbm/tbmd.lock`, not a PID file, is the ownership
authority. A second process fails with
`TBM_LOCAL_DAEMON_ALREADY_RUNNING`. Crash release comes from the operating
system; the persistent placeholder is identity-checked again on the next open.

Loopback bearer authentication proves access to this local process only. It is
not tenant identity, shared-service authorization, or a reason to bind the
profile to an untrusted network.

## Verification

Repository tests cover:

- one real process serving MCP and HTTP over one dispatcher;
- an MCP mutation read back through HTTP, concurrent exact-version HTTP
  mutation/replay, and a rejected second daemon;
- hard process termination, reopen, and session continuation;
- expired-session recovery;
- outbox lease reclaim after a simulated pre-ack crash;
- state-directory, database, symlink/reparse/hard-link, and lock checks;
- deterministic `init`, `doctor`, `health`, startup errors, and package entry
  metadata.
