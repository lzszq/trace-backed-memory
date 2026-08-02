from __future__ import annotations

import json
from pathlib import Path
import shutil
import subprocess
import sys

import pytest

from tools.verify_full_persistence_progress import (
    DEFAULT_PROGRESS_PATH,
    FIXED_DENOMINATOR,
    PROGRESS_CONTRACT_VERSION,
    FullPersistenceProgressError,
    verify_full_persistence_progress,
)


ROOT = Path(__file__).resolve().parents[1]


def _copy_progress(tmp_path: Path) -> Path:
    destination = tmp_path / DEFAULT_PROGRESS_PATH
    destination.parent.mkdir(parents=True)
    shutil.copy2(ROOT / DEFAULT_PROGRESS_PATH, destination)
    return destination


def test_current_progress_contract_freezes_full_490_atom_denominator() -> None:
    document = verify_full_persistence_progress(ROOT)

    assert document["fixed_denominator"] == FIXED_DENOMINATOR
    assert document["formal_progress"] == {
        "state": "committed",
        "completed": 182,
        "remaining": 308,
        "percentage": "37.14",
        "note": (
            "The audited 162-atom baseline and the first 20-atom F2 "
            "event-first tranche are included in the current committed "
            "repository state."
        ),
    }
    candidate = document["candidate_progress"]
    assert type(candidate) is dict
    assert candidate["completed"] == 182
    assert candidate["percentage"] == "37.14"
    assert candidate["atom_ids"] == []
    assert candidate["evidence"] == {}
    assert len(candidate["last_promoted_atom_ids"]) == 20


def test_progress_cli_reports_formal_and_candidate_totals() -> None:
    result = subprocess.run(
        [sys.executable, "tools/verify_full_persistence_progress.py"],
        cwd=ROOT,
        check=True,
        capture_output=True,
        text=True,
    )

    assert json.loads(result.stdout) == {
        "candidate_completed": 182,
        "contract_version": PROGRESS_CONTRACT_VERSION,
        "fixed_denominator": 490,
        "formal_completed": 182,
    }
    assert result.stderr == ""


@pytest.mark.parametrize(
    ("mutator", "code"),
    [
        (
            lambda document: document.__setitem__("fixed_denominator", 345),
            "TBM_PROGRESS_DENOMINATOR_DRIFT",
        ),
        (
            lambda document: document["candidate_progress"].__setitem__(
                "percentage", "99.99"
            ),
            "TBM_PROGRESS_RECORD_INVALID",
        ),
        (
            lambda document: document["candidate_progress"]["atom_ids"].append(
                "F2-R12"
            ),
            "TBM_PROGRESS_ATOMS_INVALID",
        ),
    ],
)
def test_progress_drift_fails_closed(
    tmp_path: Path,
    mutator: object,
    code: str,
) -> None:
    path = _copy_progress(tmp_path)
    document = json.loads(path.read_text(encoding="utf-8"))
    assert callable(mutator)
    mutator(document)
    path.write_text(json.dumps(document), encoding="utf-8")

    with pytest.raises(FullPersistenceProgressError) as error:
        verify_full_persistence_progress(tmp_path)

    assert error.value.code == code
