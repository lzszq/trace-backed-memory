from __future__ import annotations

import errno
import math
import os
import time
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import BinaryIO, Iterator


_SNAPSHOT_LOCK_SUFFIX = ".tbm.lock"
_SNAPSHOT_LOCK_RETRY_SECONDS = 0.05
_SNAPSHOT_LOCK_TIMEOUT_SECONDS = 30.0


def _validated_timeout_seconds(timeout_seconds: int | float) -> float:
    if type(timeout_seconds) not in {int, float}:
        raise ValueError(
            "timeout_seconds must be a non-negative finite number"
        )
    try:
        normalized = float(timeout_seconds)
    except (OverflowError, ValueError) as error:
        raise ValueError(
            "timeout_seconds must be a non-negative finite number"
        ) from error
    if not math.isfinite(normalized) or normalized < 0:
        raise ValueError(
            "timeout_seconds must be a non-negative finite number"
        )
    return normalized


def _snapshot_lock_path(snapshot_path: str | Path) -> Path:
    try:
        canonical = Path(snapshot_path).expanduser().resolve(strict=False)
    except (OSError, RuntimeError) as error:
        raise OSError(f"cannot resolve snapshot lock path: {error}") from error
    normalized = Path(os.path.normcase(os.fspath(canonical)))
    return normalized.with_name(f"{normalized.name}{_SNAPSHOT_LOCK_SUFFIX}")


def _initialize_lock_file(lock_file: BinaryIO) -> None:
    lock_file.seek(0, os.SEEK_END)
    if lock_file.tell() == 0:
        lock_file.write(b"0")
        lock_file.flush()
    lock_file.seek(0)


def _acquire_file_lock(
    lock_file: BinaryIO,
    lock_path: Path,
    timeout_seconds: float,
) -> None:
    is_windows = os.name == "nt"
    if is_windows:
        import msvcrt

        contention_errors = {errno.EACCES, errno.EAGAIN, errno.EDEADLK}
    else:
        import fcntl

        contention_errors = {errno.EACCES, errno.EAGAIN}

    started_at = time.monotonic()
    while True:
        lock_file.seek(0)
        try:
            if is_windows:
                msvcrt.locking(lock_file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
            return
        except OSError as error:
            if error.errno not in contention_errors:
                raise
            remaining = timeout_seconds - (time.monotonic() - started_at)
            if remaining <= 0:
                raise TimeoutError(
                    f"timed out waiting for snapshot write lock: {lock_path}"
                ) from error
            time.sleep(min(_SNAPSHOT_LOCK_RETRY_SECONDS, remaining))


def _release_file_lock(lock_file: BinaryIO) -> None:
    with suppress(OSError):
        lock_file.seek(0)
        if os.name == "nt":
            import msvcrt

            msvcrt.locking(lock_file.fileno(), msvcrt.LK_UNLCK, 1)
        else:
            import fcntl

            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


@contextmanager
def snapshot_write_lock(
    snapshot_path: str | Path,
    *,
    timeout_seconds: int | float = _SNAPSHOT_LOCK_TIMEOUT_SECONDS,
) -> Iterator[None]:
    """Serialize a cooperating snapshot read-modify-write transaction.

    Callers must hold this context across load, mutation, and ``save_json()``.
    The lock is advisory and independent acquisitions are non-reentrant.
    """
    validated_timeout = _validated_timeout_seconds(timeout_seconds)
    lock_path = _snapshot_lock_path(snapshot_path)
    with lock_path.open("a+b") as lock_file:
        _initialize_lock_file(lock_file)
        _acquire_file_lock(lock_file, lock_path, validated_timeout)
        try:
            yield
        finally:
            _release_file_lock(lock_file)
