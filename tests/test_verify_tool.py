import json
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_verify_tool_lists_fast_cross_platform_commands_without_running():
    result = subprocess.run(
        [sys.executable, "tools/verify.py", "--fast", "--list"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["mode"] == "fast"
    assert payload["postgres_required"] is False
    assert payload["node_required"] is False
    assert payload["commands"] == [
        [
            sys.executable,
            "-m",
            "compileall",
            "-q",
            "src",
            "tests",
            "tools",
            "examples",
        ],
        [sys.executable, "-m", "ruff", "check", "src", "tools", "examples"],
        [sys.executable, "-m", "mypy", "src/trace_backed_memory"],
        [sys.executable, "tools/verify_projection_determinism.py"],
        [sys.executable, "-m", "pytest"],
    ]


def test_verify_tool_lists_isolated_distribution_checks_for_full_mode():
    result = subprocess.run(
        [
            sys.executable,
            "tools/verify.py",
            "--full",
            "--postgres",
            "--list",
        ],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["mode"] == "full"
    assert payload["postgres_required"] is True
    assert payload["node_required"] is False
    assert "--no-isolation" in payload["commands"][-2]
    assert payload["commands"][-2][-1] == "<temporary-dist>"
    assert payload["commands"][-1][-1] == "<temporary-dist>"


def test_verify_tool_lists_the_complete_offline_repository_gate() -> None:
    result = subprocess.run(
        [sys.executable, "tools/verify.py", "--all", "--list"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(result.stdout)
    assert payload["mode"] == "all"
    assert payload["postgres_required"] is True
    assert payload["node_required"] is True
    commands = payload["commands"]
    assert commands[0][-2:] == [
        "tools/generate_sqlite_v3_bundle.py",
        "--check",
    ]
    assert commands[1][-1] == "tools/verify_authority_registry.py"
    assert commands[2][-2:] == [
        "tools/generate_resources.py",
        "--check",
    ]
    assert [command[-1] for command in commands if "<npm>" in command] == [
        "check",
        "test",
        "pack:check",
    ]
    assert "--no-isolation" in commands[-2]
    assert commands[-2][-1] == "<temporary-dist>"
    assert commands[-1][-1] == "<temporary-dist>"
