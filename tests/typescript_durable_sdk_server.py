from __future__ import annotations

import json
import sys
from threading import Thread

from tests.test_durable_agent_wire_v1 import _dispatcher
from tests.test_durable_http_sdk import TOKEN, _Authenticator, _http_stack
from trace_backed_memory.durable_http_server import (
    DurableAgentHTTPServer,
    DurableHTTPServerConfiguration,
)


def main() -> int:
    stack = _http_stack(
        permissions=(
            "memory:retrieve",
            "gate_session:transition",
            "artifact:read",
        )
    )
    server = DurableAgentHTTPServer(
        DurableHTTPServerConfiguration(port=0),
        _dispatcher(
            stack,
            expose_injection_content=True,
            expose_replay_content=True,
        ),
        _Authenticator(stack),
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        sys.stdout.write(
            json.dumps(
                {
                    "base_url": (
                        f"http://127.0.0.1:{server.server_address[1]}"
                    ),
                    "token": TOKEN,
                },
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        sys.stdout.flush()
        sys.stdin.buffer.read(1)
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        stack.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
