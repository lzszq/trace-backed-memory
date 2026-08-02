from __future__ import annotations

import json
from pathlib import Path
import sys
from tempfile import NamedTemporaryFile, TemporaryDirectory
from threading import Thread

from tests.durable_event_first_support import (
    EXPECTED_SESSION_ID,
    event_first_report,
    open_event_first_runtime,
)
from tests.test_durable_execution_v3 import EVALUATOR_CONTEXT
from tests.test_durable_http_sdk import TOKEN
from tests.test_durable_semantic_gate_v3 import _context as _provider_context
from trace_backed_memory.durable_http_server import (
    DurableAgentHTTPServer,
    DurableHTTPAuthenticatedContexts,
    DurableHTTPAuthenticationRequest,
    DurableHTTPServerConfiguration,
)


def main() -> int:
    with NamedTemporaryFile(
        prefix="tbm-durable-event-first-",
        suffix=".json",
        delete=False,
    ) as report_file:
        report_path = Path(report_file.name)
    report_path.unlink(missing_ok=True)
    with TemporaryDirectory(prefix="tbm-durable-ts-") as directory:
        runtime, context = open_event_first_runtime(
            Path(directory) / "durable.sqlite3"
        )

        def authenticate(
            request: DurableHTTPAuthenticationRequest,
        ) -> DurableHTTPAuthenticatedContexts:
            if request.authorization != f"Bearer {TOKEN}":
                raise ValueError("untrusted credential")
            return DurableHTTPAuthenticatedContexts(
                context,
                provider=_provider_context(),
                evaluator=EVALUATOR_CONTEXT,
            )

        server = DurableAgentHTTPServer(
            DurableHTTPServerConfiguration(port=0),
            runtime.dispatcher,
            authenticate,
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
                        "report_path": str(report_path),
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
            report_path.write_text(
                json.dumps(
                    event_first_report(runtime, EXPECTED_SESSION_ID),
                    allow_nan=False,
                    ensure_ascii=False,
                    indent=2,
                    sort_keys=True,
                )
                + "\n",
                encoding="utf-8",
            )
            runtime.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
