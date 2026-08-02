from __future__ import annotations

import argparse
import json
from pathlib import Path

from trace_backed_memory.ledger_port_v1 import LedgerTenantPartition
from trace_backed_memory.managed_index_v3 import (
    ManagedIndexBuildInput,
    build_managed_index_bundle,
)
from trace_backed_memory.retrieval_index_event_v1 import (
    build_retrieval_index_event_registry,
    build_retrieval_index_manifest,
    dumps_retrieval_index_event_payload_dispatch_schema,
    dumps_retrieval_index_manifest_schema,
)


ROOT = Path(__file__).resolve().parents[1]


def _example() -> str:
    bundle = build_managed_index_bundle(
        ManagedIndexBuildInput(
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
            retriever_id="reference_retriever",
            retriever_version="v1",
            sources=(),
            semantic_provider_id="reference_embeddings",
            semantic_provider_version="v1",
        )
    )
    manifest = build_retrieval_index_manifest(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
        ),
        bundle=bundle,
        source_event_watermark=42,
        source_event_sha256="sha256:" + "a" * 64,
    )
    return json.dumps(
        manifest.to_dict(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


TARGETS = {
    ROOT / "schemas" / "retrieval_index_manifest_v1.schema.json": (
        dumps_retrieval_index_manifest_schema
    ),
    ROOT
    / "schemas"
    / "retrieval_index_event_payload_registry_v1.schema.json": (
        dumps_retrieval_index_event_payload_dispatch_schema
    ),
    ROOT / "examples" / "retrieval_index_manifest_v1.example.json": _example,
    ROOT
    / "examples"
    / "retrieval_index_event_type_registry_v1.example.json": (
        lambda: json.dumps(
            build_retrieval_index_event_registry().catalog(),
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
        description="Generate deterministic retrieval-index event resources."
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
