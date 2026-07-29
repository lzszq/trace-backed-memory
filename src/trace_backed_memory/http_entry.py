from __future__ import annotations

import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Load optional local HTTP service dependencies on demand."""
    try:
        from .http_server import main as http_main
    except ModuleNotFoundError as error:
        if (
            error.name is None
            or error.name == "trace_backed_memory"
            or error.name.startswith("trace_backed_memory.")
        ):
            raise
        sys.stderr.write(
            "trace-backed-memory HTTP support is not installed; "
            "install trace-backed-memory[service]\n"
        )
        return 2
    return http_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
