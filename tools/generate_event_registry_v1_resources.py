from __future__ import annotations

import argparse
import os
from pathlib import Path
import sys
import tempfile


ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from trace_backed_memory.event_registry_v1 import (  # noqa: E402
    DEFAULT_EVENT_TYPE_REGISTRY,
    dumps_event_payload_dispatch_schema,
    dumps_event_registry_catalog,
)


OUTPUTS = {
    ROOT / "examples" / "event_type_registry_v1.example.json": (
        dumps_event_registry_catalog(DEFAULT_EVENT_TYPE_REGISTRY)
    ),
    ROOT / "schemas" / "event_payload_registry_v1.schema.json": (
        dumps_event_payload_dispatch_schema(DEFAULT_EVENT_TYPE_REGISTRY)
    ),
}


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or verify exact event-registry version-1 resources."
    )
    parser.add_argument(
        "--write",
        action="store_true",
        help="atomically replace canonical generated resources",
    )
    return parser


def _write_atomic(path: Path, content: str) -> None:
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
    )
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8", newline="") as handle:
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary_name, path)
    except BaseException:
        try:
            os.unlink(temporary_name)
        except FileNotFoundError:
            pass
        raise


def main() -> int:
    arguments = _parser().parse_args()
    drift = tuple(
        path
        for path, content in OUTPUTS.items()
        if not path.is_file() or path.read_text(encoding="utf-8") != content
    )
    if not arguments.write:
        if drift:
            for path in drift:
                print(path.relative_to(ROOT).as_posix(), file=sys.stderr)
            return 1
        return 0
    for path in drift:
        _write_atomic(path, OUTPUTS[path])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
