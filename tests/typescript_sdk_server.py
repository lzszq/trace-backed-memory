from __future__ import annotations

import json
from pathlib import Path
import sys
from threading import Thread

from trace_backed_memory import LocalAgentMemory
from trace_backed_memory.agent_wire_v1 import (
    AgentProtocolConfiguration,
    AgentProtocolDispatcher,
)
from trace_backed_memory.http_server import (
    AgentHTTPServer,
    AgentHTTPServerConfiguration,
)


def main() -> int:
    token = "typescript_sdk_conformance_" + "a" * 32
    root = Path(__file__).resolve().parents[1]
    runtime = LocalAgentMemory.in_memory()
    server = AgentHTTPServer(
        AgentHTTPServerConfiguration(port=0, token=token),
        AgentProtocolDispatcher(AgentProtocolConfiguration(root), runtime),
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
                    "token": token,
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
        runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
