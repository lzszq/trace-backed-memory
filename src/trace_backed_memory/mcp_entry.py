from __future__ import annotations

import sys
from typing import Sequence


def main(argv: Sequence[str] | None = None) -> int:
    """Load the optional MCP runtime without affecting core package imports."""
    try:
        from .mcp_server import main as mcp_main
    except ModuleNotFoundError as error:
        if (
            error.name is None
            or error.name == "trace_backed_memory"
            or error.name.startswith("trace_backed_memory.")
        ):
            raise
        sys.stderr.write(
            "trace-backed-memory MCP support is not installed; "
            "install trace-backed-memory[mcp]\n"
        )
        return 2
    return mcp_main(argv)


if __name__ == "__main__":
    raise SystemExit(main())
