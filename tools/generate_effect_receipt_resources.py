from __future__ import annotations

import argparse
import json
from pathlib import Path

from trace_backed_memory.effect_receipt_v1 import (
    build_effect_receipt_registry,
    dumps_effect_receipt_payload_dispatch_schema,
)


ROOT = Path(__file__).resolve().parents[1]
TARGETS = {
    ROOT / "schemas" / "effect_receipt_payload_registry_v1.schema.json": (
        dumps_effect_receipt_payload_dispatch_schema
    ),
    ROOT / "examples" / "effect_receipt_type_registry_v1.example.json": (
        lambda: json.dumps(
            build_effect_receipt_registry().catalog(),
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
        description="Generate deterministic effect-receipt schema/catalog resources."
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
