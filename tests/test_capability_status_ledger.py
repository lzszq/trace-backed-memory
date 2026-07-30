from __future__ import annotations

import json
import re
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
STATUS_VALUES = {"active", "opt-in", "contract-only", "planned"}
ROW = re.compile(
    r"^\| `(?P<id>[^`]+)` \| .* \| `(?P<status>active|opt-in|contract-only|planned)` \|"
)


def _matrix_rows(path: Path) -> dict[str, str]:
    rows: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        match = ROW.match(line)
        if match is None:
            continue
        capability_id = match.group("id")
        assert capability_id not in rows
        rows[capability_id] = match.group("status")
    return rows


def test_machine_capability_ledger_matches_bilingual_matrices() -> None:
    status_dir = ROOT / "docs" / "status"
    payload = json.loads(
        (status_dir / "current-capabilities.json").read_text(encoding="utf-8")
    )

    assert payload["ledger_version"] == "tbm.capability-status.v1"
    assert set(payload["status_values"]) == STATUS_VALUES
    entries = payload["capabilities"]
    machine_rows = {entry["id"]: entry["status"] for entry in entries}
    assert len(machine_rows) == len(entries)
    assert set(machine_rows.values()) <= STATUS_VALUES
    assert machine_rows == _matrix_rows(
        status_dir / "current-capability-matrix.md"
    )
    assert machine_rows == _matrix_rows(
        status_dir / "current-capability-matrix.zh-CN.md"
    )

    for entry in entries:
        assert entry["evidence"]
        for relative_path in entry["evidence"]:
            assert (ROOT / relative_path).exists(), relative_path


def test_capability_ledger_records_current_compatibility_boundary() -> None:
    payload = json.loads(
        (
            ROOT / "docs" / "status" / "current-capabilities.json"
        ).read_text(encoding="utf-8")
    )
    assert payload["compatibility_boundary"] == {
        "snapshot_version": 2,
        "sqlite_schema_version": 1,
        "postgresql_schema_version": 2,
        "agent_protocol": "tbm.agent.v1",
        "pending_gate_requests": "process-local",
    }


def test_accepted_adrs_have_bilingual_pairs() -> None:
    adr_dir = ROOT / "docs" / "adr"
    english = sorted(
        path for path in adr_dir.glob("*.md") if not path.name.endswith(".zh-CN.md")
    )
    assert [path.name[:4] for path in english] == [
        "0001",
        "0002",
        "0003",
        "0004",
        "0005",
    ]
    for path in english:
        chinese = path.with_name(f"{path.stem}.zh-CN.md")
        assert chinese.is_file()
        assert chinese.name in path.read_text(encoding="utf-8")
        assert path.name in chinese.read_text(encoding="utf-8")
