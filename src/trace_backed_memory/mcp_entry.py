from __future__ import annotations

import sys
from typing import Sequence


def _selected_profile(argv: Sequence[str]) -> str:
    for index, argument in enumerate(argv):
        if argument == "--profile" and index + 1 < len(argv):
            return argv[index + 1]
        if argument.startswith("--profile="):
            return argument.partition("=")[2]
    return "compat-v2"


def main(argv: Sequence[str] | None = None) -> int:
    """Load the optional MCP runtime without affecting core package imports."""
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    try:
        if _selected_profile(arguments) == "durable-v3":
            from .durable_mcp_entry import main as mcp_main
        else:
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
    return mcp_main(arguments)


if __name__ == "__main__":
    raise SystemExit(main())
