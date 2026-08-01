from __future__ import annotations

import json
from pathlib import Path
import sys
from typing import NoReturn

from trace_backed_memory.event_v1 import loads_canonical_event
from trace_backed_memory.reducer import (
    ReducerEvent,
    build_event_inventory_reducer,
    canonical_projection_state,
    execute_reducer_step,
    initial_reducer_state,
)


ROOT = Path(__file__).resolve().parents[1]
EVENT_FIXTURE = ROOT / "examples" / "event_v1.example.json"
GOLDEN_FIXTURE = ROOT / "tests" / "fixtures" / "event_projection_v1.golden.json"


def _reject_constant(value: str) -> NoReturn:
    raise ValueError(f"non-finite JSON constant is forbidden: {value}")


def _strict_object(pairs: list[tuple[str, object]]) -> dict[str, object]:
    value: dict[str, object] = {}
    for key, item in pairs:
        if key in value:
            raise ValueError("duplicate JSON object key is forbidden")
        value[key] = item
    return value


def _load_golden() -> dict[str, object]:
    value = json.loads(
        GOLDEN_FIXTURE.read_text(encoding="utf-8"),
        object_pairs_hook=_strict_object,
        parse_constant=_reject_constant,
    )
    if type(value) is not dict:
        raise ValueError("projection determinism fixture must be an object")
    return value


def verify_projection_determinism() -> dict[str, object]:
    event = loads_canonical_event(EVENT_FIXTURE.read_text(encoding="utf-8"))
    golden = _load_golden()
    reducer = build_event_inventory_reducer()
    initial = initial_reducer_state(reducer)
    first = execute_reducer_step(
        reducer,
        initial.state,
        ReducerEvent(event, None),
    )
    second = execute_reducer_step(
        reducer,
        initial.state,
        ReducerEvent(event, None),
    )
    actual = {
        "descriptor_sha256": reducer.descriptor.descriptor_sha256,
        "fixture_version": "tbm.reducer-determinism-fixture.v1",
        "initial_state_sha256": initial.state_sha256,
        "input_event_sha256": event.event_sha256,
        "projection": canonical_projection_state(first.state),
        "projection_sha256": first.state_sha256,
        "reducer_id": reducer.descriptor.reducer_id,
        "reducer_version": reducer.descriptor.reducer_version,
    }
    if first != second:
        raise RuntimeError("repeated reducer execution changed canonical output")
    if actual != golden:
        raise RuntimeError("projection determinism fixture does not match golden bytes")
    return {
        "fixture_version": actual["fixture_version"],
        "projection_sha256": actual["projection_sha256"],
        "status": "ok",
    }


def main() -> int:
    try:
        result = verify_projection_determinism()
    except Exception:
        sys.stderr.write(
            json.dumps(
                {
                    "error": "projection determinism verification failed",
                    "status": "error",
                },
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        return 1
    sys.stdout.write(
        json.dumps(
            result,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
