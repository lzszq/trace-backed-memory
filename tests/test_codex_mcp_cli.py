from __future__ import annotations

import json
import os
from pathlib import Path
import shutil
import subprocess

import pytest


def _run_codex(
    executable: str,
    *arguments: str,
    cwd: Path,
    env: dict[str, str],
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [executable, *arguments],
        cwd=cwd,
        env=env,
        check=False,
        capture_output=True,
        text=True,
        encoding="utf-8",
        timeout=30,
    )


def test_codex_cli_round_trips_durable_stdio_profile(tmp_path: Path) -> None:
    executable = shutil.which("codex")
    if executable is None:
        pytest.skip("Codex CLI is not installed")

    root = Path(__file__).resolve().parents[1]
    probe = _run_codex(
        executable,
        "--version",
        cwd=root,
        env=dict(os.environ),
    )
    if probe.returncode in {126, 127}:
        pytest.skip("Codex CLI runtime is not installed")
    assert probe.returncode == 0, probe.stderr

    codex_home = tmp_path / "codex-home"
    codex_home.mkdir()
    environment = dict(os.environ)
    environment["CODEX_HOME"] = str(codex_home)

    server_name = "trace_backed_memory_durable"
    expected_args = [
        "--profile",
        "durable-v3",
        "--application-factory",
        "operator.tbm:create_application",
        "--sqlite",
        ".tbm/durable.sqlite3",
    ]
    added = _run_codex(
        executable,
        "mcp",
        "add",
        server_name,
        "--",
        "tbm-mcp",
        *expected_args,
        cwd=root,
        env=environment,
    )
    assert added.returncode == 0, added.stderr

    retrieved = _run_codex(
        executable,
        "mcp",
        "get",
        server_name,
        "--json",
        cwd=root,
        env=environment,
    )
    assert retrieved.returncode == 0, retrieved.stderr
    configuration = json.loads(retrieved.stdout)
    assert configuration["name"] == server_name
    assert configuration["enabled"] is True
    assert configuration["transport"] == {
        "type": "stdio",
        "command": "tbm-mcp",
        "args": expected_args,
        "env": None,
        "env_vars": [],
        "cwd": None,
    }

    listed = _run_codex(
        executable,
        "mcp",
        "list",
        cwd=root,
        env=environment,
    )
    assert listed.returncode == 0, listed.stderr
    assert server_name in listed.stdout

    removed = _run_codex(
        executable,
        "mcp",
        "remove",
        server_name,
        cwd=root,
        env=environment,
    )
    assert removed.returncode == 0, removed.stderr
