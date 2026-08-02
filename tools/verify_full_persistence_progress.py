from __future__ import annotations

import argparse
from collections.abc import Mapping, Sequence
from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path
import re
import sys
from typing import NoReturn, cast


ROOT = Path(__file__).resolve().parents[1]
DEFAULT_PROGRESS_PATH = Path("docs/status/full-persistence-progress.json")
PROGRESS_CONTRACT_VERSION = "tbm.full-persistence-progress.v1"
FIXED_DENOMINATOR = 490
PHASES = ("F0", "F1", "F2", "F3", "F4", "F5", "F6")
SOURCE_COUNTS = {
    "release-train": 312,
    "test-matrix": 74,
    "definition-of-done": 67,
    "retention": 33,
    "global-gates": 4,
}
PHASE_COUNTS = {
    "F0": 48,
    "F1": 90,
    "F2": 62,
    "F3": 117,
    "F4": 38,
    "F5": 48,
    "F6": 87,
}
_ATOM_ID_RE = re.compile(r"^F[0-6]-(?:[A-Z][0-9]{2}|G[0-9]{2})$")
_HEX_SHA256_RE = re.compile(r"^sha256:[0-9a-f]{64}$")


class FullPersistenceProgressError(RuntimeError):
    """Stable failure for fixed-denominator progress drift."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def verify_full_persistence_progress(
    root: Path = ROOT,
    progress_path: Path | None = None,
) -> Mapping[str, object]:
    relative = DEFAULT_PROGRESS_PATH if progress_path is None else progress_path
    path = relative if relative.is_absolute() else root / relative
    document = _load_document(path)
    expected_fields = {
        "contract_version",
        "plan",
        "fixed_denominator",
        "atom_sources",
        "global_gates",
        "phase_denominators",
        "formal_progress",
        "candidate_progress",
    }
    if set(document) != expected_fields:
        _fail("TBM_PROGRESS_FIELDS_INVALID", "progress root fields are invalid")
    if document["contract_version"] != PROGRESS_CONTRACT_VERSION:
        _fail("TBM_PROGRESS_VERSION_UNSUPPORTED", "progress version is unsupported")
    if document["fixed_denominator"] != FIXED_DENOMINATOR:
        _fail("TBM_PROGRESS_DENOMINATOR_DRIFT", "fixed denominator must remain 490")
    _verify_plan(document["plan"])
    _verify_sources(document["atom_sources"])
    _verify_global_gates(document["global_gates"])
    _verify_phases(document["phase_denominators"])
    formal = _verify_progress(document["formal_progress"], state="committed")
    candidate = _verify_progress(
        document["candidate_progress"],
        state="uncommitted",
        root=root,
    )
    base_completed = candidate.get("base_completed")
    if base_completed != formal["completed"]:
        _fail("TBM_PROGRESS_BASE_DRIFT", "candidate base must equal formal progress")
    atom_ids = candidate.get("atom_ids")
    if type(atom_ids) is not list:
        _fail("TBM_PROGRESS_ATOMS_INVALID", "candidate atom IDs are invalid")
    candidate_completed = cast(int, candidate["completed"])
    formal_completed = cast(int, formal["completed"])
    if candidate_completed - formal_completed != len(atom_ids):
        _fail("TBM_PROGRESS_ATOMS_INVALID", "candidate atom count is inconsistent")
    return document


def _load_document(path: Path) -> dict[str, object]:
    if path.is_symlink() or not path.is_file():
        _fail("TBM_PROGRESS_READ_FAILED", "progress contract is missing or unsafe")
    try:
        data = path.read_bytes()
    except OSError as error:
        raise FullPersistenceProgressError(
            "TBM_PROGRESS_READ_FAILED",
            "progress contract could not be read",
        ) from error
    if not 1 <= len(data) <= 256 * 1024:
        _fail("TBM_PROGRESS_INVALID", "progress contract byte size is invalid")
    try:
        value = json.loads(
            data.decode("utf-8"),
            object_pairs_hook=_reject_duplicate_keys,
            parse_constant=_reject_non_finite,
        )
    except FullPersistenceProgressError:
        raise
    except (UnicodeError, json.JSONDecodeError) as error:
        raise FullPersistenceProgressError(
            "TBM_PROGRESS_INVALID",
            "progress contract is not strict UTF-8 JSON",
        ) from error
    if type(value) is not dict:
        _fail("TBM_PROGRESS_INVALID", "progress contract root must be an object")
    return cast(dict[str, object], value)


def _verify_plan(value: object) -> None:
    if type(value) is not dict or set(value) != {
        "source_name",
        "sha256",
        "line_count",
        "mapping_basis",
    }:
        _fail("TBM_PROGRESS_PLAN_INVALID", "plan descriptor is invalid")
    plan = cast(dict[str, object], value)
    if (
        plan["source_name"]
        != "trace-backed-memory-final-full-persistence-execution-plan-2026-07-31.md"
        or type(plan["sha256"]) is not str
        or _HEX_SHA256_RE.fullmatch(cast(str, plan["sha256"])) is None
        or plan["line_count"] != 2768
        or plan["mapping_basis"] != "audited-fixed-atomization-v1"
    ):
        _fail("TBM_PROGRESS_PLAN_INVALID", "plan descriptor drifted")


def _verify_sources(value: object) -> None:
    if type(value) is not list:
        _fail("TBM_PROGRESS_SOURCES_INVALID", "atom sources are invalid")
    counts: dict[str, int] = {}
    for item in value:
        if type(item) is not dict or not {"source_id", "count", "plan_lines"} <= set(item):
            _fail("TBM_PROGRESS_SOURCES_INVALID", "atom source is invalid")
        source_id = item["source_id"]
        count = item["count"]
        if type(source_id) is not str or type(count) is not int or source_id in counts:
            _fail("TBM_PROGRESS_SOURCES_INVALID", "atom source identity is invalid")
        counts[source_id] = count
    if counts != SOURCE_COUNTS or sum(counts.values()) != FIXED_DENOMINATOR:
        _fail("TBM_PROGRESS_DENOMINATOR_DRIFT", "atom source counts drifted")


def _verify_global_gates(value: object) -> None:
    if type(value) is not list or len(value) != 4:
        _fail("TBM_PROGRESS_GATES_INVALID", "global gates are invalid")
    atom_ids: list[str] = []
    for item in value:
        if type(item) is not dict or set(item) != {"atom_id", "name", "plan_lines"}:
            _fail("TBM_PROGRESS_GATES_INVALID", "global gate is invalid")
        atom_id = item["atom_id"]
        if type(atom_id) is not str:
            _fail("TBM_PROGRESS_GATES_INVALID", "global gate ID is invalid")
        atom_ids.append(atom_id)
    if atom_ids != ["F0-G01", "F0-G02", "F0-G03", "F0-G04"]:
        _fail("TBM_PROGRESS_GATES_INVALID", "global gate IDs drifted")


def _verify_phases(value: object) -> None:
    if type(value) is not list or len(value) != len(PHASES):
        _fail("TBM_PROGRESS_PHASES_INVALID", "phase denominators are invalid")
    next_start = 1
    aggregate_sources = {source_id: 0 for source_id in SOURCE_COUNTS}
    for expected_phase, item in zip(PHASES, value, strict=True):
        if type(item) is not dict or set(item) != {
            "phase",
            "start",
            "end",
            "count",
            "components",
        }:
            _fail("TBM_PROGRESS_PHASES_INVALID", "phase descriptor is invalid")
        phase = item["phase"]
        start = item["start"]
        end = item["end"]
        count = item["count"]
        components = item["components"]
        if (
            phase != expected_phase
            or type(start) is not int
            or type(end) is not int
            or type(count) is not int
            or type(components) is not dict
            or start != next_start
            or end - start + 1 != count
            or count != PHASE_COUNTS[expected_phase]
        ):
            _fail("TBM_PROGRESS_PHASES_INVALID", "phase range or count drifted")
        component_total = 0
        for source_id, component_count in components.items():
            if source_id not in aggregate_sources or type(component_count) is not int:
                _fail("TBM_PROGRESS_PHASES_INVALID", "phase component is invalid")
            aggregate_sources[source_id] += component_count
            component_total += component_count
        if component_total != count:
            _fail("TBM_PROGRESS_PHASES_INVALID", "phase components do not sum")
        next_start = end + 1
    if next_start != FIXED_DENOMINATOR + 1 or aggregate_sources != SOURCE_COUNTS:
        _fail("TBM_PROGRESS_DENOMINATOR_DRIFT", "phase/source totals drifted")


def _verify_progress(
    value: object,
    *,
    state: str,
    root: Path | None = None,
) -> dict[str, object]:
    if type(value) is not dict:
        _fail("TBM_PROGRESS_RECORD_INVALID", "progress record is invalid")
    progress = cast(dict[str, object], value)
    required = {"state", "completed", "remaining", "percentage"}
    if not required <= set(progress) or progress["state"] != state:
        _fail("TBM_PROGRESS_RECORD_INVALID", "progress record fields are invalid")
    completed = progress["completed"]
    remaining = progress["remaining"]
    percentage = progress["percentage"]
    if (
        type(completed) is not int
        or not 0 <= completed <= FIXED_DENOMINATOR
        or remaining != FIXED_DENOMINATOR - completed
        or percentage != _percentage(completed)
    ):
        _fail("TBM_PROGRESS_RECORD_INVALID", "progress arithmetic is invalid")
    if state == "uncommitted":
        _verify_candidate_evidence(progress, root)
    return progress


def _verify_candidate_evidence(
    progress: Mapping[str, object],
    root: Path | None,
) -> None:
    atom_ids = progress.get("atom_ids")
    evidence = progress.get("evidence")
    if (
        type(atom_ids) is not list
        or atom_ids != sorted(atom_ids)
        or len(atom_ids) != len(set(atom_ids))
        or any(type(item) is not str or _ATOM_ID_RE.fullmatch(item) is None for item in atom_ids)
        or type(evidence) is not dict
        or set(evidence) != set(atom_ids)
    ):
        _fail("TBM_PROGRESS_ATOMS_INVALID", "candidate atom evidence is invalid")
    if not atom_ids:
        return
    if root is None:
        _fail("TBM_PROGRESS_EVIDENCE_INVALID", "repository root is unavailable")
    for paths in evidence.values():
        if type(paths) is not list or not paths:
            _fail("TBM_PROGRESS_EVIDENCE_INVALID", "candidate evidence is empty")
        for relative in paths:
            if type(relative) is not str or relative.startswith(("/", ".")):
                _fail("TBM_PROGRESS_EVIDENCE_INVALID", "candidate evidence path is invalid")
            path = root / relative
            if path.is_symlink() or not path.is_file():
                _fail("TBM_PROGRESS_EVIDENCE_INVALID", "candidate evidence path is missing")


def _percentage(completed: int) -> str:
    value = (Decimal(completed) * Decimal(100) / Decimal(FIXED_DENOMINATOR)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    return format(value, ".2f")


def _reject_duplicate_keys(pairs: Sequence[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        if key in result:
            _fail("TBM_PROGRESS_DUPLICATE_KEY", "progress JSON contains duplicate keys")
        result[key] = value
    return result


def _reject_non_finite(value: str) -> NoReturn:
    _fail("TBM_PROGRESS_INVALID", f"non-finite number is not allowed: {value}")


def _fail(code: str, message: str) -> NoReturn:
    raise FullPersistenceProgressError(code, message)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        description="Verify the fixed 490-atom Full Persistence progress contract."
    )
    parser.add_argument("--path", type=Path, default=DEFAULT_PROGRESS_PATH)
    args = parser.parse_args(argv)
    try:
        document = verify_full_persistence_progress(progress_path=args.path)
    except FullPersistenceProgressError as error:
        print(json.dumps({"code": error.code, "message": str(error)}, sort_keys=True))
        return 1
    formal = cast(dict[str, object], document["formal_progress"])
    candidate = cast(dict[str, object], document["candidate_progress"])
    print(
        json.dumps(
            {
                "candidate_completed": candidate["completed"],
                "contract_version": PROGRESS_CONTRACT_VERSION,
                "fixed_denominator": FIXED_DENOMINATOR,
                "formal_completed": formal["completed"],
            },
            sort_keys=True,
        )
    )
    return 0


if __name__ == "__main__":
    sys.exit(main())
