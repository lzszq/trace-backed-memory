from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from collections.abc import Sequence
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def _base_commands(*, fast: bool) -> list[list[str]]:
    python = sys.executable
    commands = [
        [
            python,
            "-m",
            "compileall",
            "-q",
            "src",
            "tests",
            "tools",
            "examples",
        ],
        [python, "-m", "ruff", "check", "src", "tools", "examples"],
        [python, "-m", "mypy", "src/trace_backed_memory"],
    ]
    if fast:
        commands.append([python, "-m", "pytest"])
    else:
        commands.extend(
            [
                [python, "-m", "coverage", "run", "-m", "pytest"],
                [python, "-m", "coverage", "report"],
            ]
        )
    return commands


def _all_only_commands(*, npm: str) -> list[list[str]]:
    return [
        [
            sys.executable,
            "tools/generate_sqlite_v3_bundle.py",
            "--check",
        ],
        [sys.executable, "tools/generate_resources.py", "--check"],
        [sys.executable, "-m", "pip", "check"],
        [
            npm,
            "--prefix",
            "packages/typescript-sdk",
            "run",
            "check",
        ],
        [
            npm,
            "--prefix",
            "packages/typescript-sdk",
            "test",
        ],
        [
            npm,
            "--prefix",
            "packages/typescript-sdk",
            "run",
            "pack:check",
        ],
    ]


def _display_commands(
    *,
    fast: bool,
    all_mode: bool,
) -> list[list[str]]:
    commands = []
    if all_mode:
        commands.extend(
            _all_only_commands(npm="<npm>")[:2]
        )
    commands.extend(_base_commands(fast=fast))
    if all_mode:
        commands.extend(_all_only_commands(npm="<npm>")[2:])
    if not fast:
        commands.extend(
            [
                [
                    sys.executable,
                    "-m",
                    "build",
                    "--no-isolation",
                    "--outdir",
                    "<temporary-dist>",
                ],
                [
                    sys.executable,
                    "tests/verify_distribution.py",
                    "<temporary-dist>",
                ],
            ]
        )
    return commands


def _run(
    command: Sequence[str],
    *,
    env: dict[str, str],
) -> None:
    rendered = subprocess.list2cmdline(command)
    print(f"+ {rendered}", flush=True)
    subprocess.run(
        list(command),
        check=True,
        cwd=ROOT,
        env=env,
    )


def _verify_distribution(*, env: dict[str, str]) -> None:
    with tempfile.TemporaryDirectory(prefix="tbm-dist-") as directory:
        destination = Path(directory)
        _run(
            [
                sys.executable,
                "-m",
                "build",
                "--no-isolation",
                "--outdir",
                str(destination),
            ],
            env=env,
        )
        _run(
            [
                sys.executable,
                "tests/verify_distribution.py",
                str(destination),
            ],
            env=env,
        )


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Run the canonical trace-backed-memory verification suite."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--fast",
        action="store_true",
        help="run compile, lint, type, and pytest checks",
    )
    mode.add_argument(
        "--full",
        action="store_true",
        help="run the default coverage and distribution verification",
    )
    mode.add_argument(
        "--all",
        action="store_true",
        dest="all_mode",
        help=(
            "require PostgreSQL and run Python, resources, TypeScript, "
            "dependency-integrity, and distribution verification"
        ),
    )
    parser.add_argument(
        "--postgres",
        action="store_true",
        help="require PostgreSQL integration prerequisites and tests",
    )
    parser.add_argument(
        "--list",
        action="store_true",
        dest="list_only",
        help="print the planned commands as JSON without executing them",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    all_mode = bool(args.all_mode)
    fast = bool(args.fast) and not all_mode
    postgres_required = bool(args.postgres) or all_mode
    if args.list_only:
        print(
            json.dumps(
                {
                    "mode": (
                        "all"
                        if all_mode
                        else "fast"
                        if fast
                        else "full"
                    ),
                    "postgres_required": postgres_required,
                    "node_required": all_mode,
                    "commands": _display_commands(
                        fast=fast,
                        all_mode=all_mode,
                    ),
                },
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
        )
        return 0

    env = os.environ.copy()
    if postgres_required:
        env["TBM_REQUIRE_POSTGRES"] = "1"
    if all_mode:
        for command in _all_only_commands(npm="<npm>")[:2]:
            _run(command, env=env)
    for command in _base_commands(fast=fast):
        _run(command, env=env)
    if all_mode:
        npm = shutil.which("npm")
        if npm is None:
            print(
                "full repository verification requires Node.js 20+ and npm",
                file=sys.stderr,
            )
            return 2
        for command in _all_only_commands(npm=npm)[2:]:
            _run(command, env=env)
    if not fast:
        _verify_distribution(env=env)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
