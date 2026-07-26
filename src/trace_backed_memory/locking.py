from __future__ import annotations

import errno
import math
import os
import stat
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


def _unsafe_lock_sidecar_error(lock_path: Path) -> OSError:
    return OSError(
        "snapshot lock sidecar must be a single-link regular file: "
        f"{lock_path}"
    )


def _unsafe_snapshot_target_error(snapshot_path: Path) -> OSError:
    return OSError(
        "snapshot write target must be a single-link regular file: "
        f"{snapshot_path}"
    )


def _validate_snapshot_write_target(snapshot_path: str | Path) -> None:
    target_path = Path(snapshot_path)
    try:
        target_stat = os.lstat(target_path)
    except FileNotFoundError:
        return
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(target_stat, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(target_stat.st_mode)
        or target_stat.st_nlink != 1
        or bool(file_attributes & reparse_attribute)
    ):
        raise _unsafe_snapshot_target_error(target_path)


def _validate_lock_sidecar_stat(
    lock_path: Path,
    file_stat: os.stat_result,
) -> None:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(file_stat, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
        or bool(file_attributes & reparse_attribute)
    ):
        raise _unsafe_lock_sidecar_error(lock_path)


def _wrap_lock_descriptor(descriptor: int) -> BinaryIO:
    try:
        return os.fdopen(descriptor, "r+b")
    except BaseException:
        os.close(descriptor)
        raise


def _verified_lock_file(
    descriptor: int,
    lock_path: Path,
    *,
    expected_stat: os.stat_result | None = None,
) -> BinaryIO:
    try:
        descriptor_stat = os.fstat(descriptor)
        path_stat = os.lstat(lock_path)
        _validate_lock_sidecar_stat(lock_path, descriptor_stat)
        _validate_lock_sidecar_stat(lock_path, path_stat)
        if not os.path.samestat(descriptor_stat, path_stat):
            raise _unsafe_lock_sidecar_error(lock_path)
        if expected_stat is not None and not os.path.samestat(
            expected_stat,
            descriptor_stat,
        ):
            raise _unsafe_lock_sidecar_error(lock_path)
    except BaseException:
        os.close(descriptor)
        raise
    return _wrap_lock_descriptor(descriptor)


def _open_lock_file(lock_path: Path) -> BinaryIO:
    open_flags = os.O_RDWR
    open_flags |= getattr(os, "O_BINARY", 0)
    open_flags |= getattr(os, "O_NOINHERIT", 0)
    try:
        descriptor = os.open(
            lock_path,
            open_flags | os.O_CREAT | os.O_EXCL,
            0o666,
        )
    except FileExistsError:
        expected_stat = os.lstat(lock_path)
        _validate_lock_sidecar_stat(lock_path, expected_stat)
        existing_flags = open_flags | getattr(os, "O_NOFOLLOW", 0)
        try:
            descriptor = os.open(lock_path, existing_flags)
        except OSError as error:
            if error.errno == errno.ELOOP:
                raise _unsafe_lock_sidecar_error(lock_path) from error
            raise
        return _verified_lock_file(
            descriptor,
            lock_path,
            expected_stat=expected_stat,
        )
    return _verified_lock_file(descriptor, lock_path)


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


def _validate_acquired_lock_identity(
    lock_file: BinaryIO,
    lock_path: Path,
) -> None:
    descriptor_stat = os.fstat(lock_file.fileno())
    path_stat = os.lstat(lock_path)
    _validate_lock_sidecar_stat(lock_path, descriptor_stat)
    _validate_lock_sidecar_stat(lock_path, path_stat)
    if not os.path.samestat(descriptor_stat, path_stat):
        raise _unsafe_lock_sidecar_error(lock_path)


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
    _validate_snapshot_write_target(snapshot_path)
    lock_path = _snapshot_lock_path(snapshot_path)
    with _open_lock_file(lock_path) as lock_file:
        _initialize_lock_file(lock_file)
        _acquire_file_lock(lock_file, lock_path, validated_timeout)
        try:
            _validate_acquired_lock_identity(lock_file, lock_path)
            _validate_snapshot_write_target(snapshot_path)
            yield
        finally:
            _release_file_lock(lock_file)
