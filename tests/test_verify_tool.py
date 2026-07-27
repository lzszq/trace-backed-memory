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
    assert [command[2] for command in payload["commands"]] == [
        "compileall",
        "ruff",
        "mypy",
        "pytest",
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
    assert payload["commands"][-2][-1] == "<temporary-dist>"
    assert payload["commands"][-1][-1] == "<temporary-dist>"
