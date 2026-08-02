from __future__ import annotations

import argparse
import json
from pathlib import Path

from trace_backed_memory.outcome_harm_event_v1 import (
    build_outcome_evaluation_context,
    build_outcome_harm_event_registry,
    dumps_outcome_harm_event_payload_dispatch_schema,
    outcome_evaluation_context_schema,
)


ROOT = Path(__file__).resolve().parents[1]


def _json(value: object) -> str:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def _context_example() -> str:
    context = build_outcome_evaluation_context(
        organization_id="organization_001",
        tenant_id="tenant_001",
        repository_id="repository_001",
        environment_id="environment_001",
        run_outcome_id="run_outcome_sha256_" + "1" * 64,
        session_id="gate_session_001",
        trace_id="trace_001",
        run_id="run_001",
        usage_decision_id="usage_decision_001",
        usage_decision_sha256="sha256:" + "2" * 64,
        replay_manifest_sha256="sha256:" + "3" * 64,
        retrieval_snapshot_sha256="sha256:" + "4" * 64,
        injection_artifact_id="injection_artifact_001",
        memory_revision_ids=("memory_revision_sha256_" + "5" * 64,),
        evaluation_suite="regression_suite_001",
        evaluation_case="regression_case_001",
        experiment_id="experiment_001",
        cohort_id="cohort_with_memory_001",
        cohort_arm="with_memory",
        assignment_method="randomized",
        assignment_evidence_sha256="sha256:" + "6" * 64,
        bound_by="outcome_reviewer",
        bound_via_client_id="outcome_review_service",
        authorization_event_id="authz_sha256_" + "7" * 64,
        bound_at="2026-07-29T00:10:32Z",
    )
    return _json(context.to_dict())


TARGETS = {
    ROOT / "schemas" / "outcome_evaluation_context_v1.schema.json": (
        lambda: _json(outcome_evaluation_context_schema())
    ),
    ROOT
    / "schemas"
    / "outcome_harm_event_payload_registry_v1.schema.json": (
        dumps_outcome_harm_event_payload_dispatch_schema
    ),
    ROOT / "examples" / "outcome_evaluation_context_v1.example.json": (
        _context_example
    ),
    ROOT
    / "examples"
    / "outcome_harm_event_type_registry_v1.example.json": (
        lambda: _json(build_outcome_harm_event_registry().catalog())
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate deterministic outcome-harm event resources."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="write generated resources instead of checking them",
    )
    args = parser.parse_args()
    stale: list[Path] = []
    for path, render in TARGETS.items():
        expected = render()
        if args.write:
            path.write_text(expected, encoding="utf-8", newline="\n")
        elif not path.exists() or path.read_text(encoding="utf-8") != expected:
            stale.append(path)
    if stale:
        for path in stale:
            print(f"stale: {path.relative_to(ROOT)}")
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
