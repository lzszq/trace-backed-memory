from __future__ import annotations

import argparse
import json
from collections.abc import Sequence
from pathlib import Path

from trace_backed_memory import (
    LocalAgentMemory,
    MemoryContext,
    MemoryRunMeasurement,
    capture_local_trace,
)


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run one complete local trace-backed-memory lifecycle."
    )
    parser.add_argument(
        "--repo-path",
        type=Path,
        default=Path.cwd(),
        help="Git checkout whose provenance will be captured",
    )
    parser.add_argument(
        "--database",
        default="tbm-memory.sqlite3",
        help="SQLite database path or :memory:",
    )
    parser.add_argument(
        "--task",
        default="inspect the repository with verified memory when applicable",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    trace = capture_local_trace(args.repo_path)
    context = MemoryContext(
        mode="planning",
        repo=trace.repo,
        commit_sha=trace.commit_sha,
    )
    with LocalAgentMemory.open_sqlite(args.database) as memory:
        result = memory.run(
            trace,
            context,
            task=args.task,
            decide=lambda _prepared: {
                "use_memory": False,
                "allowed_memory_ids": [],
                "blocked_memory_ids": [],
                "reason": "No prepared verified memory is required.",
                "risk": "none",
                "recommended_injection": "none",
            },
            execute=lambda _finalized: MemoryRunMeasurement(
                eval_result="pass"
            ),
        )
    print(
        json.dumps(
            result.to_dict(),
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
