import json
import os
import subprocess
import sys
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]


def test_quickstart_executes_a_complete_sqlite_agent_run(tmp_path: Path):
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    pythonpath = str(ROOT / "src")
    if existing_pythonpath:
        pythonpath = f"{pythonpath}{os.pathsep}{existing_pythonpath}"
    environment["PYTHONPATH"] = pythonpath
    database = tmp_path / "quickstart.sqlite3"

    completed = subprocess.run(
        [
            sys.executable,
            "examples/quickstart.py",
            "--repo-path",
            str(ROOT),
            "--database",
            str(database),
        ],
        cwd=ROOT,
        env=environment,
        check=True,
        capture_output=True,
        text=True,
    )

    payload = json.loads(completed.stdout)
    assert payload["protocol_version"] == "tbm.agent.v1"
    assert payload["prepared"]["trace_id"] == payload["completed"]["trace_id"]
    assert payload["finalized"]["use_memory"] is False
    assert payload["completed"]["eval_result"] == "pass"
    assert database.is_file()
