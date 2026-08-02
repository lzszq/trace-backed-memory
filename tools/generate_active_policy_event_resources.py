from __future__ import annotations

import argparse
import json
from pathlib import Path

from trace_backed_memory.active_policy_event_v1 import (
    build_active_policy_bundle,
    build_active_policy_event_registry,
    dumps_active_policy_bundle_schema,
    dumps_active_policy_event_payload_dispatch_schema,
)
from trace_backed_memory.retrieval_policy_v3 import (
    ModeMemoryRule,
    build_retrieval_policy,
)


ROOT = Path(__file__).resolve().parents[1]


def _example() -> str:
    retrieval_policy = build_retrieval_policy(
        policy_version="retrieval_policy_001",
        allowed_classifications=("public", "internal"),
        mode_memory_rules=(
            ModeMemoryRule("planning", ("semantic", "policy")),
            ModeMemoryRule(
                "repair", ("procedural", "semantic", "policy")
            ),
            ModeMemoryRule(
                "debug", ("procedural", "episodic", "policy")
            ),
            ModeMemoryRule("eval", ("procedural", "semantic")),
            ModeMemoryRule("production", ("procedural", "policy")),
        ),
        ancestry_mode="required",
        ancestry_bypass_reason=None,
        stage_weights=(
            ("metadata", 0.1),
            ("lexical", 0.2),
            ("semantic", 0.4),
            ("evidence_graph", 0.3),
        ),
        minimum_fused_score=0.25,
        payload_budget_bytes=8192,
    )
    bundle = build_active_policy_bundle(
        retrieval_policy=retrieval_policy,
        minimum_trust_tier="regression_verified",
    )
    return json.dumps(
        bundle.to_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


TARGETS = {
    ROOT / "schemas" / "active_policy_bundle_v1.schema.json": (
        dumps_active_policy_bundle_schema
    ),
    ROOT / "schemas" / "active_policy_event_payload_registry_v1.schema.json": (
        dumps_active_policy_event_payload_dispatch_schema
    ),
    ROOT / "examples" / "active_policy_bundle_v1.example.json": _example,
    ROOT / "examples" / "active_policy_event_type_registry_v1.example.json": (
        lambda: json.dumps(
            build_active_policy_event_registry().catalog(),
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            indent=2,
        )
        + "\n"
    ),
}


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Generate deterministic active-policy event resources."
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
