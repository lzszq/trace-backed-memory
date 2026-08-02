from __future__ import annotations

from decimal import Decimal, ROUND_HALF_UP
import json
from pathlib import Path


ROOT = Path(__file__).resolve().parents[1]
PROGRESS_PATH = ROOT / "docs" / "status" / "full-persistence-progress.json"
PLAN_SHA256 = "248d9a4e95d3cb955fc6f9c29ebc0f8c44f8902a1fb3482d5e749ea1e00ec164"
EXPECTED_PACKAGE_POINTS = {
    "F0-01": 12,
    "F0-02": 15,
    "F0-03": 9,
    "F0-04": 12,
    "F0-05": 3,
    "F1-01": 14,
    "F1-02": 9,
    "F1-03": 7,
    "F1-04": 14,
    "F1-05": 7,
    "F1-06": 6,
    "F2-01": 10,
    "F2-02": 8,
    "F2-03": 5,
    "F2-04": 2,
    "F2-05": 6,
    "F2-06": 3,
    "F2-07": 6,
    "F2-08": 15,
    "F3-01": 8,
    "F3-02": 10,
    "F3-03": 6,
    "F3-04": 8,
    "F3-05": 12,
    "F3-06": 8,
    "F4-01": 4,
    "F4-02": 2,
    "F4-03": 8,
    "F4-04": 2,
    "F4-05": 8,
    "F4-06": 6,
    "F4-07": 5,
    "F5-01": 5,
    "F5-02": 8,
    "F5-03": 1,
    "F5-04": 4,
    "F5-05": 3,
    "F5-06": 4,
    "F6-01": 8,
    "F6-02": 6,
    "F6-03": 7,
    "F6-04": 8,
    "F6-05": 5,
    "F6-06": 8,
    "F6-07": 9,
    "F6-08": 10,
    "F6-09": 9,
}


def _reject_duplicates(pairs: list[tuple[str, object]]) -> dict[str, object]:
    result: dict[str, object] = {}
    for key, value in pairs:
        assert key not in result, key
        result[key] = value
    return result


def _load() -> dict[str, object]:
    return json.loads(
        PROGRESS_PATH.read_text(encoding="utf-8"),
        object_pairs_hook=_reject_duplicates,
    )


def test_full_persistence_progress_uses_the_fixed_plan_denominator() -> None:
    payload = _load()
    source = payload["source_plan"]
    assert isinstance(source, dict)
    assert source["sha256"] == PLAN_SHA256
    assert source["release_train"] == "F0-F6"
    assert source["work_package_count"] == len(EXPECTED_PACKAGE_POINTS) == 47
    assert source["fixed_total_points"] == sum(EXPECTED_PACKAGE_POINTS.values()) == 345

    packages: dict[str, int] = {}
    stages = payload["stages"]
    assert isinstance(stages, list)
    for stage in stages:
        assert isinstance(stage, dict)
        work_packages = stage["work_packages"]
        assert isinstance(work_packages, list)
        assert stage["total_points"] == sum(
            package["total_points"] for package in work_packages
        )
        for package in work_packages:
            package_id = package["id"]
            assert package_id not in packages
            packages[package_id] = package["total_points"]

    assert packages == EXPECTED_PACKAGE_POINTS


def test_full_persistence_progress_current_total_is_derived_from_every_stage() -> None:
    payload = _load()
    current = payload["current"]
    assert isinstance(current, dict)
    stages = payload["stages"]
    assert isinstance(stages, list)

    completed = 0
    total = 0
    for stage in stages:
        assert isinstance(stage, dict)
        work_packages = stage["work_packages"]
        assert isinstance(work_packages, list)
        stage_completed = 0
        for package in work_packages:
            status = package["status"]
            package_completed = package["completed_points"]
            package_total = package["total_points"]
            assert status in {"not_started", "partial", "done"}
            assert 0 <= package_completed <= package_total
            if status == "not_started":
                assert package_completed == 0
            elif status == "partial":
                assert 0 < package_completed < package_total
            else:
                assert package_completed == package_total
            stage_completed += package_completed
        assert stage["completed_points"] == stage_completed
        completed += stage_completed
        total += stage["total_points"]

    percent = (Decimal(completed) * 100 / Decimal(total)).quantize(
        Decimal("0.01"),
        rounding=ROUND_HALF_UP,
    )
    assert current == {
        "completed_points": completed,
        "total_points": total,
        "completed_percent": format(percent, ".2f"),
    }
