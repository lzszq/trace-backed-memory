from pathlib import Path

import pytest

from trace_backed_memory._ingestion import (
    decode_bounded_utf8,
    read_bounded_utf8,
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
