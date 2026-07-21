from __future__ import annotations

from pathlib import Path


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


def read_bounded_utf8(
    path: str | Path,
    *,
    max_bytes: int | None,
    description: str,
) -> str:
    _validate_positive_limit(max_bytes, "max_bytes")
    target = Path(path)
    with target.open("rb") as source:
        data = source.read() if max_bytes is None else source.read(max_bytes + 1)
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
