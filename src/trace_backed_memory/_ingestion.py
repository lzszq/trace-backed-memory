from __future__ import annotations

import errno
import json
import math
import os
from pathlib import Path
import stat
from typing import Any


SNAPSHOT_FILE_MAX_BYTES = 64 * 1024 * 1024
SNAPSHOT_MAX_RECORDS_PER_COLLECTION = 100_000
SNAPSHOT_MAX_TOTAL_RECORDS = 250_000
LESSONS_YAML_FILE_MAX_BYTES = 8 * 1024 * 1024
LESSONS_YAML_MAX_RECORDS = 10_000
FAILURE_TAXONOMY_FILE_MAX_BYTES = 1024 * 1024
FAILURE_TAXONOMY_MAX_RECORDS = 1_000
CLI_JSON_FILE_MAX_BYTES = 8 * 1024 * 1024
CLI_JSON_MAX_ITEMS = 10_000
CLI_JSON_MAX_NODES = 100_000
CLI_JSON_MAX_DEPTH = 100
CLI_RECOVER_BATCH_MAX_ITEMS = 10_000


def unique_json_object_pairs(
    pairs: list[tuple[str, Any]],
    *,
    description: str,
) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(
                f"{description} JSON contains duplicate object key: {key}"
            )
        result[key] = value
    return result


def parse_bounded_json(
    source_text: str,
    *,
    description: str,
    max_nodes: int,
    max_depth: int,
    source: str | Path | None = None,
) -> Any:
    """Parse one duplicate-rejecting, finite, bounded JSON document."""
    if type(source_text) is not str:
        raise ValueError("source_text must be a string")
    _validate_positive_limit(max_nodes, "max_nodes")
    validate_non_negative_limit(max_depth, "max_depth")
    if not isinstance(description, str) or not description.strip():
        raise ValueError("description must be a nonblank string")

    def reject_non_finite(value: str) -> Any:
        raise ValueError(
            f"{description} JSON contains non-finite number: {value}"
        )

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        return unique_json_object_pairs(
            pairs,
            description=description,
        )

    try:
        payload: Any = json.loads(
            source_text,
            parse_constant=reject_non_finite,
            object_pairs_hook=unique_object,
        )
    except (json.JSONDecodeError, RecursionError) as error:
        location = f" in {source}" if source is not None else ""
        raise ValueError(
            f"invalid {description} JSON{location}: {error}"
        ) from error

    node_count = 0
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > max_depth:
            raise ValueError(
                f"{description} JSON exceeds maximum depth of {max_depth}"
            )
        node_count += 1
        if node_count > max_nodes:
            raise ValueError(
                f"{description} JSON contains more than {max_nodes} nodes"
            )
        if type(value) is float and not math.isfinite(value):
            raise ValueError(
                f"{description} JSON contains non-finite number"
            )
        if type(value) is str:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as error:
                raise ValueError(
                    f"{description} JSON contains an invalid Unicode string"
                ) from error
            continue
        if type(value) is list:
            if len(value) > max_nodes - node_count:
                raise ValueError(
                    f"{description} JSON contains more than "
                    f"{max_nodes} nodes"
                )
            pending.extend((item, depth + 1) for item in value)
            continue
        if type(value) is dict:
            if len(value) > max_nodes - node_count:
                raise ValueError(
                    f"{description} JSON contains more than "
                    f"{max_nodes} nodes"
                )
            for key in value:
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError as error:
                    raise ValueError(
                        f"{description} JSON contains an invalid Unicode key"
                    ) from error
            pending.extend(
                (item, depth + 1) for item in value.values()
            )
    return payload


def read_bounded_utf8(
    path: str | Path,
    *,
    max_bytes: int | None,
    description: str,
) -> str:
    _validate_positive_limit(max_bytes, "max_bytes")
    target = Path(path)
    flags = (
        os.O_RDONLY
        | getattr(os, "O_BINARY", 0)
        | getattr(os, "O_NOINHERIT", 0)
        | getattr(os, "O_NOFOLLOW", 0)
    )
    if os.name == "posix":
        flags |= os.O_NONBLOCK
    descriptor = os.open(target, flags)
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = os.stat(target, follow_symlinks=False)
        if (
            not stat.S_ISREG(descriptor_stat.st_mode)
            or not stat.S_ISREG(path_stat.st_mode)
            or not os.path.samestat(descriptor_stat, path_stat)
        ):
            raise OSError(
                errno.EINVAL,
                f"{description} path must reference one regular file",
                str(target),
            )
        with os.fdopen(descriptor, "rb") as source:
            descriptor = -1
            data = (
                source.read()
                if max_bytes is None
                else source.read(max_bytes + 1)
            )
    finally:
        if descriptor >= 0:
            os.close(descriptor)
    return decode_bounded_utf8(
        data,
        max_bytes=max_bytes,
        description=description,
        source=target,
    )


def decode_bounded_utf8(
    data: bytes,
    *,
    max_bytes: int | None,
    description: str,
    source: str | Path | None = None,
) -> str:
    _validate_positive_limit(max_bytes, "max_bytes")
    if max_bytes is not None and len(data) > max_bytes:
        label = f"{description} file" if source is not None else description
        location = f": {source}" if source is not None else ""
        raise ValueError(
            f"{label} exceeds maximum size of {max_bytes} bytes{location}"
        )
    return data.decode("utf-8")


def validate_non_negative_limit(value: int | None, name: str) -> int | None:
    if value is not None and (type(value) is not int or value < 0):
        raise ValueError(f"{name} must be a non-negative integer or None")
    return value


def validate_snapshot_record_count(
    collection_name: str,
    record_count: object,
    *,
    max_records_per_collection: int | None,
) -> int:
    limit = validate_non_negative_limit(
        max_records_per_collection,
        "max_records_per_collection",
    )
    if type(record_count) is not int or record_count < 0:
        raise ValueError(
            f"snapshot field {collection_name!r} record count must be a "
            "non-negative integer"
        )
    if limit is not None and record_count > limit:
        raise ValueError(
            f"snapshot field {collection_name!r} contains {record_count} records; "
            f"maximum is {limit}"
        )
    return record_count


def validate_snapshot_total_record_count(
    total_records: object,
    *,
    max_total_records: int | None,
) -> int:
    limit = validate_non_negative_limit(
        max_total_records,
        "max_total_records",
    )
    if type(total_records) is not int or total_records < 0:
        raise ValueError(
            "snapshot total record count must be a non-negative integer"
        )
    if limit is not None and total_records > limit:
        raise ValueError(
            f"snapshot contains {total_records} records; maximum is {limit}"
        )
    return total_records


def _validate_positive_limit(value: int | None, name: str) -> int | None:
    if value is not None and (type(value) is not int or value <= 0):
        raise ValueError(f"{name} must be a positive integer or None")
    return value
