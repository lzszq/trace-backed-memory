import os
from pathlib import Path

import pytest

from trace_backed_memory._ingestion import (
    decode_bounded_utf8,
    parse_bounded_json,
    read_bounded_utf8,
    validate_snapshot_record_count,
    validate_snapshot_total_record_count,
)


def test_bounded_utf8_reader_counts_bytes_and_accepts_exact_limit(tmp_path: Path):
    path = tmp_path / "multibyte.txt"
    path.write_bytes("\u00e9".encode("utf-8"))

    assert read_bounded_utf8(
        path,
        max_bytes=2,
        description="fixture",
    ) == "\u00e9"

    with pytest.raises(
        ValueError,
        match="fixture file exceeds maximum size of 1 bytes",
    ):
        read_bounded_utf8(path, max_bytes=1, description="fixture")


def test_bounded_utf8_reader_allows_explicit_trusted_opt_out(tmp_path: Path):
    path = tmp_path / "trusted.txt"
    path.write_text("trusted migration input", encoding="utf-8")

    assert read_bounded_utf8(
        path,
        max_bytes=None,
        description="fixture",
    ) == "trusted migration input"


@pytest.mark.parametrize("max_bytes", [True, 0, -1, 1.5, "10"])
def test_bounded_utf8_reader_rejects_invalid_limits(tmp_path: Path, max_bytes):
    path = tmp_path / "fixture.txt"
    path.write_text("fixture", encoding="utf-8")

    with pytest.raises(
        ValueError,
        match="max_bytes must be a positive integer or None",
    ):
        read_bounded_utf8(
            path,
            max_bytes=max_bytes,
            description="fixture",
        )


def test_bounded_utf8_decoder_applies_the_same_budget_and_strict_decoding():
    assert decode_bounded_utf8(
        b"exact",
        max_bytes=5,
        description="fixture",
    ) == "exact"

    with pytest.raises(ValueError, match="fixture exceeds maximum size"):
        decode_bounded_utf8(
            b"one-over",
            max_bytes=7,
            description="fixture",
        )
    with pytest.raises(UnicodeDecodeError):
        decode_bounded_utf8(
            b"\xff",
            max_bytes=1,
            description="fixture",
        )


@pytest.mark.parametrize(
    ("source", "kwargs", "match"),
    (
        (
            object(),
            {"description": "fixture", "max_nodes": 2, "max_depth": 1},
            "source_text must be a string",
        ),
        (
            "{}",
            {"description": " ", "max_nodes": 2, "max_depth": 1},
            "description must be a nonblank",
        ),
        (
            "[0]",
            {"description": "fixture", "max_nodes": 1, "max_depth": 1},
            "more than 1 nodes",
        ),
        (
            '{"key":0}',
            {"description": "fixture", "max_nodes": 1, "max_depth": 1},
            "more than 1 nodes",
        ),
        (
            '{"\\ud800":0}',
            {"description": "fixture", "max_nodes": 2, "max_depth": 1},
            "invalid Unicode key",
        ),
    ),
)
def test_bounded_json_rejects_invalid_metadata_and_budgets(
    source: object,
    kwargs: dict[str, object],
    match: str,
) -> None:
    with pytest.raises(ValueError, match=match):
        parse_bounded_json(source, **kwargs)  # type: ignore[arg-type]


def test_bounded_json_reports_source_and_depth() -> None:
    with pytest.raises(ValueError, match="in fixture.json"):
        parse_bounded_json(
            "{",
            description="fixture",
            max_nodes=2,
            max_depth=1,
            source="fixture.json",
        )
    with pytest.raises(ValueError, match="maximum depth"):
        parse_bounded_json(
            "[[0]]",
            description="fixture",
            max_nodes=3,
            max_depth=1,
        )


@pytest.mark.parametrize("record_count", (True, -1, 1.5, "1"))
def test_snapshot_record_count_rejects_invalid_values(
    record_count: object,
) -> None:
    with pytest.raises(ValueError, match="non-negative integer"):
        validate_snapshot_record_count(
            "traces",
            record_count,
            max_records_per_collection=10,
        )
    with pytest.raises(ValueError, match="non-negative integer"):
        validate_snapshot_total_record_count(
            record_count,
            max_total_records=10,
        )


def test_snapshot_record_count_enforces_limits() -> None:
    with pytest.raises(ValueError, match="maximum is 1"):
        validate_snapshot_record_count(
            "traces",
            2,
            max_records_per_collection=1,
        )
    with pytest.raises(ValueError, match="maximum is 1"):
        validate_snapshot_total_record_count(2, max_total_records=1)


@pytest.mark.skipif(not hasattr(os, "mkfifo"), reason="requires POSIX FIFO support")
def test_bounded_utf8_reader_rejects_fifo_without_blocking(tmp_path: Path):
    fifo = tmp_path / "fixture.fifo"
    os.mkfifo(fifo)

    with pytest.raises(OSError, match="must reference one regular file"):
        read_bounded_utf8(
            fifo,
            max_bytes=10,
            description="fixture",
        )
