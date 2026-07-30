# Durable HTTP profile: `tbm.durable-agent-wire.v1`

**English** | [简体中文](durable-http-v1.zh-CN.md)

The explicit `tbm-http --profile durable-v3` profile exposes the complete
durable Agent lifecycle over bounded HTTP. It uses the unified SQLite v3
authority graph or the isolated PostgreSQL v3 authorities and can continue a
session after the server and database runtime are reopened.

The default `tbm-http` behavior remains `compat-v2`. Durable v3 is never
selected implicitly.

## Security and identity boundary

- A bearer secret from `TBM_DURABLE_HTTP_TOKEN` is required for every route.
- Request JSON never supplies caller, provider, evaluator, tenant,
  environment, authorization-event, or authority identity.
- An operator-owned `DurableHTTPApplication` factory supplies
  `DurableRuntimeDependencies` and derives live trusted contexts after bearer
  authentication.
- Injection and replay bytes are hidden unless the corresponding explicit
  startup flag is set.
- IPv4 loopback is the intended local profile. A non-loopback IPv4 bind requires TLS;
  TLS 1.2 is the minimum. Client-certificate verification is available, but
  does not replace the bearer boundary. IPv6 is rejected by this profile.
- Headers, bodies, JSON depth/node count, canonical base64, response sizes,
  worker count, queue length, and connection/TLS-handshake time are bounded.

The local bearer proves access to this process only. It is not a tenant
identity and does not turn this profile into an untrusted multi-tenant
service.

## Trusted application factory

Create an importable module in the operator-controlled environment:

```python
from trace_backed_memory.durable_http_entry import DurableHTTPApplication

from my_service.tbm_dependencies import (
    durable_runtime_dependencies,
    trusted_contexts_for_http_request,
)


def create_application() -> DurableHTTPApplication:
    return DurableHTTPApplication(
        dependencies=durable_runtime_dependencies(),
        context_provider=trusted_contexts_for_http_request,
    )
```

The context provider receives bounded transport evidence only after the
bearer secret matches. It returns `DurableHTTPAuthenticatedContexts`.
Repository resolution, entity-registry lookup, provider registration, and
evaluator authentication remain server-owned.

## Start SQLite v3

Install the service dependencies and create a private secret of at least 32
characters:

```powershell
python -m pip install -e ".[service]"
$env:TBM_DURABLE_HTTP_TOKEN = "<random-secret-at-least-32-characters>"
$env:TBM_DURABLE_HTTP_APPLICATION_FACTORY = "tbm_local_app:create_application"
tbm-http --profile durable-v3 --sqlite .tbm\durable.sqlite3 --initialize
```

```bash
python -m pip install -e '.[service]'
export TBM_DURABLE_HTTP_TOKEN='<random-secret-at-least-32-characters>'
export TBM_DURABLE_HTTP_APPLICATION_FACTORY='tbm_local_app:create_application'
tbm-http --profile durable-v3 --sqlite .tbm/durable.sqlite3 --initialize
```

The database parent directory must already exist. Use `--initialize` only for
the first atomic bundle installation. Restart without that flag:

```text
tbm-http --profile durable-v3 --sqlite .tbm/durable.sqlite3
```

PostgreSQL uses `--postgres-env ENV_NAME`; the named environment variable
contains the connection string and the isolated v3 schema must already be
installed and verified.

## Routes and capability negotiation

`GET /durable/v1/capabilities` reports
`tbm.durable-agent-wire.v1`, durable sessions, the storage mode, and whether
content exposure is enabled. `GET /durable/v1/openapi` returns the canonical
OpenAPI 3.1 contract. The lifecycle routes are:

```text
prepare → decide → finalize → start/resume/abandon
        → complete/cancel → get-session → export-replay
```

Every state mutation carries the expected session version. Stale transitions
fail closed. Retries reuse durable session/run identities; the dispatcher
does not keep process-local lifecycle handles.

## Content and replay

Descriptor-only session and replay responses are the default. Exact rendered
injection bytes require `--expose-injection-content`. Replay bytes additionally
require `--expose-replay-content`, a current `artifact:read` authorization
decision, an explicit classification allowlist, an exact session version, and
the request byte limit.

See also [durable Agent wire v1](durable-agent-wire-v1.md),
[durable Agent v3](durable-agent-v3.md), and
[unified SQLite v3 bundle](sqlite-bundle-v3.md).
