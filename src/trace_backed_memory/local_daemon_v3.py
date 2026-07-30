from __future__ import annotations

from contextlib import contextmanager
from dataclasses import dataclass
import math
import os
from pathlib import Path
import stat
from threading import Event, Lock, Thread
from typing import Iterator

from .durable_runtime_v3 import (
    DurableRuntimeV3Error,
    DurableSQLiteRuntime,
)
from .locking import exclusive_file_lock


LOCAL_DAEMON_CONTRACT_VERSION = "tbm.local-daemon.v1"
LOCAL_DAEMON_DATABASE_NAME = "durable.sqlite3"
LOCAL_DAEMON_LOCK_NAME = "tbmd.lock"
LOCAL_DAEMON_STATE_DIRECTORY_MODE = 0o700
LOCAL_DAEMON_DATABASE_MODE = 0o600


class LocalDaemonV3Error(RuntimeError):
    """Stable local-daemon construction or lifecycle failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


def _state_failed(code: str, message: str) -> None:
    raise LocalDaemonV3Error(code, message)


def _is_reparse(file_stat: os.stat_result) -> bool:
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(file_stat, "st_file_attributes", 0)
    return bool(file_attributes & reparse_attribute)


def _validate_state_directory(path: Path) -> None:
    try:
        file_stat = os.lstat(path)
    except OSError as error:
        raise LocalDaemonV3Error(
            "TBM_LOCAL_DAEMON_STATE_UNAVAILABLE",
            "local daemon state directory is unavailable",
        ) from error
    if not stat.S_ISDIR(file_stat.st_mode) or _is_reparse(file_stat):
        _state_failed(
            "TBM_LOCAL_DAEMON_STATE_UNSAFE",
            "local daemon state directory must be a real directory",
        )
    if os.name != "nt":
        if (
            file_stat.st_uid != os.geteuid()
            or stat.S_IMODE(file_stat.st_mode) & 0o077
        ):
            _state_failed(
                "TBM_LOCAL_DAEMON_STATE_PERMISSIONS",
                "local daemon state directory must be owner-only",
            )
    if not os.access(path, os.R_OK | os.W_OK | os.X_OK):
        _state_failed(
            "TBM_LOCAL_DAEMON_STATE_PERMISSIONS",
            "local daemon state directory is not accessible",
        )


def _validate_state_ancestry(path: Path) -> None:
    """Reject aliases and ancestors that another local account can replace."""
    current = path
    while True:
        try:
            file_stat = os.lstat(current)
        except OSError as error:
            raise LocalDaemonV3Error(
                "TBM_LOCAL_DAEMON_STATE_UNAVAILABLE",
                "local daemon state directory ancestry is unavailable",
            ) from error
        if not stat.S_ISDIR(file_stat.st_mode) or _is_reparse(file_stat):
            _state_failed(
                "TBM_LOCAL_DAEMON_STATE_UNSAFE",
                "local daemon state directory ancestry is unsafe",
            )
        if os.name != "nt":
            mode = stat.S_IMODE(file_stat.st_mode)
            trusted_owner = file_stat.st_uid in {0, os.geteuid()}
            replaceable = bool(mode & 0o022) and not bool(mode & stat.S_ISVTX)
            if not trusted_owner or replaceable:
                _state_failed(
                    "TBM_LOCAL_DAEMON_STATE_PERMISSIONS",
                    "local daemon state directory ancestry is unsafe",
                )
        parent = current.parent
        if parent == current:
            return
        current = parent


def _validate_owner_only_file(
    path: Path,
    file_stat: os.stat_result,
    *,
    unsafe_code: str,
    permissions_code: str,
    label: str,
) -> None:
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
        or _is_reparse(file_stat)
    ):
        _state_failed(
            unsafe_code,
            f"{label} must be a single-link regular file",
        )
    if os.name != "nt" and (
        file_stat.st_uid != os.geteuid()
        or stat.S_IMODE(file_stat.st_mode) & 0o077
    ):
        _state_failed(
            permissions_code,
            f"{label} must be owner-only",
        )


def prepare_local_state_directory(
    state_directory: str | Path,
    *,
    create: bool = False,
) -> Path:
    """Create or verify one owner-controlled local daemon state directory."""
    if type(create) is not bool:
        raise TypeError("create must be a boolean")
    candidate = Path(state_directory).expanduser()
    if candidate.name in {"", ".", ".."}:
        _state_failed(
            "TBM_LOCAL_DAEMON_STATE_INVALID",
            "local daemon state directory is invalid",
        )
    try:
        candidate = candidate.absolute()
    except (OSError, RuntimeError) as error:
        raise LocalDaemonV3Error(
            "TBM_LOCAL_DAEMON_STATE_INVALID",
            "local daemon state directory is invalid",
        ) from error
    try:
        os.lstat(candidate)
    except FileNotFoundError:
        if not create:
            _state_failed(
                "TBM_LOCAL_DAEMON_STATE_MISSING",
                "local daemon state directory does not exist",
            )
        parent = candidate.parent
        try:
            parent_stat = os.lstat(parent)
        except OSError as error:
            raise LocalDaemonV3Error(
                "TBM_LOCAL_DAEMON_STATE_MISSING",
                "local daemon state directory parent does not exist",
            ) from error
        if not stat.S_ISDIR(parent_stat.st_mode) or _is_reparse(parent_stat):
            _state_failed(
                "TBM_LOCAL_DAEMON_STATE_UNSAFE",
                "local daemon state directory parent is unsafe",
            )
        _validate_state_ancestry(parent)
        try:
            candidate.mkdir(mode=LOCAL_DAEMON_STATE_DIRECTORY_MODE)
        except OSError as error:
            raise LocalDaemonV3Error(
                "TBM_LOCAL_DAEMON_STATE_UNAVAILABLE",
                "local daemon state directory could not be created",
            ) from error
    _validate_state_directory(candidate)
    _validate_state_ancestry(candidate)
    try:
        canonical = candidate.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise LocalDaemonV3Error(
            "TBM_LOCAL_DAEMON_STATE_UNAVAILABLE",
            "local daemon state directory could not be resolved",
        ) from error
    if os.path.normcase(os.fspath(canonical)) != os.path.normcase(
        os.fspath(candidate)
    ):
        _state_failed(
            "TBM_LOCAL_DAEMON_STATE_UNSAFE",
            "local daemon state directory must not use aliases",
        )
    return canonical


def verify_local_database_target(
    database: Path,
    *,
    expected_stat: os.stat_result | None = None,
) -> os.stat_result:
    """Verify one fixed database inode and optionally its prior identity."""
    if not isinstance(database, Path):
        raise TypeError("database must be a Path")
    try:
        file_stat = os.lstat(database)
    except OSError as error:
        raise LocalDaemonV3Error(
            "TBM_LOCAL_DAEMON_DATABASE_UNAVAILABLE",
            "local daemon database is unavailable",
        ) from error
    _validate_owner_only_file(
        database,
        file_stat,
        unsafe_code="TBM_LOCAL_DAEMON_DATABASE_UNSAFE",
        permissions_code="TBM_LOCAL_DAEMON_DATABASE_PERMISSIONS",
        label="local daemon database",
    )
    if expected_stat is not None and not os.path.samestat(
        expected_stat,
        file_stat,
    ):
        _state_failed(
            "TBM_LOCAL_DAEMON_DATABASE_UNSAFE",
            "local daemon database identity changed",
        )
    if not os.access(database, os.R_OK | os.W_OK):
        _state_failed(
            "TBM_LOCAL_DAEMON_DATABASE_PERMISSIONS",
            "local daemon database is not readable and writable",
        )
    return file_stat


def prepare_local_database(
    state_directory: Path,
    *,
    initialize: bool,
) -> Path:
    """Create or verify the fixed single-link SQLite database target."""
    if not isinstance(state_directory, Path):
        raise TypeError("state_directory must be a Path")
    if type(initialize) is not bool:
        raise TypeError("initialize must be a boolean")
    database = state_directory / LOCAL_DAEMON_DATABASE_NAME
    try:
        file_stat = os.lstat(database)
    except FileNotFoundError:
        if not initialize:
            _state_failed(
                "TBM_LOCAL_DAEMON_DATABASE_MISSING",
                "local daemon database does not exist",
            )
        flags = os.O_RDWR | os.O_CREAT | os.O_EXCL
        flags |= getattr(os, "O_BINARY", 0)
        flags |= getattr(os, "O_NOINHERIT", 0)
        try:
            descriptor = os.open(
                database,
                flags,
                LOCAL_DAEMON_DATABASE_MODE,
            )
        except OSError as error:
            raise LocalDaemonV3Error(
                "TBM_LOCAL_DAEMON_DATABASE_UNAVAILABLE",
                "local daemon database could not be created",
            ) from error
        os.close(descriptor)
        file_stat = os.lstat(database)
    except OSError as error:
        raise LocalDaemonV3Error(
            "TBM_LOCAL_DAEMON_DATABASE_UNAVAILABLE",
            "local daemon database is unavailable",
        ) from error
    verify_local_database_target(database, expected_stat=file_stat)
    return database


@contextmanager
def local_daemon_lock(
    state_directory: Path,
    *,
    timeout_seconds: int | float = 0,
) -> Iterator[None]:
    """Hold the single-instance lock for one local daemon state directory."""
    if not isinstance(state_directory, Path):
        raise TypeError("state_directory must be a Path")
    _validate_state_directory(state_directory)
    _validate_state_ancestry(state_directory)
    try:
        state_stat = os.lstat(state_directory)
    except OSError as error:
        raise LocalDaemonV3Error(
            "TBM_LOCAL_DAEMON_STATE_UNAVAILABLE",
            "local daemon state directory is unavailable",
        ) from error
    lock_path = state_directory / LOCAL_DAEMON_LOCK_NAME
    try:
        held_lock = exclusive_file_lock(
            lock_path,
            timeout_seconds=timeout_seconds,
        )
        held_lock.__enter__()
    except TimeoutError as error:
        raise LocalDaemonV3Error(
            "TBM_LOCAL_DAEMON_ALREADY_RUNNING",
            "another local daemon already owns this state directory",
        ) from error
    except OSError as error:
        raise LocalDaemonV3Error(
            "TBM_LOCAL_DAEMON_LOCK_UNSAFE",
            "local daemon lock could not be acquired safely",
        ) from error
    try:
        _validate_state_directory(state_directory)
        _validate_state_ancestry(state_directory)
        if not os.path.samestat(
            state_stat,
            os.lstat(state_directory),
        ):
            _state_failed(
                "TBM_LOCAL_DAEMON_STATE_UNSAFE",
                "local daemon state directory identity changed",
            )
        lock_stat = os.lstat(lock_path)
        _validate_owner_only_file(
            lock_path,
            lock_stat,
            unsafe_code="TBM_LOCAL_DAEMON_LOCK_UNSAFE",
            permissions_code="TBM_LOCAL_DAEMON_LOCK_PERMISSIONS",
            label="local daemon lock",
        )
        yield
    finally:
        held_lock.__exit__(None, None, None)


@dataclass(frozen=True)
class LocalDaemonWorkerConfiguration:
    interval_seconds: float = 1.0
    recovery_limit: int = 100
    outbox_lease_seconds: int = 60
    outbox_limit: int = 100
    outbox_retry_delay_seconds: int = 60
    outbox_max_attempts: int = 5

    def __post_init__(self) -> None:
        if (
            type(self.interval_seconds) is not float
            or not math.isfinite(self.interval_seconds)
            or not 0.05 <= self.interval_seconds <= 3_600
        ):
            raise ValueError(
                "worker interval_seconds must be between 0.05 and 3600"
            )
        for name, value, maximum in (
            ("recovery_limit", self.recovery_limit, 10_000),
            ("outbox_lease_seconds", self.outbox_lease_seconds, 86_400),
            ("outbox_limit", self.outbox_limit, 1_000),
            (
                "outbox_retry_delay_seconds",
                self.outbox_retry_delay_seconds,
                604_800,
            ),
            ("outbox_max_attempts", self.outbox_max_attempts, 1_000),
        ):
            if (
                type(value) is not int
                or value < 1
                or value > maximum
            ):
                raise ValueError(
                    f"worker {name} must be between 1 and {maximum}"
                )


@dataclass(frozen=True)
class LocalDaemonWorkerStatus:
    contract_version: str
    running: bool
    tick_count: int
    recovered_session_count: int
    recovery_required_session_count: int
    superseded_session_count: int
    delivered_event_count: int
    retry_wait_event_count: int
    dead_letter_event_count: int
    recovery_required_event_count: int
    superseded_event_count: int
    last_error_code: str | None

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "running": self.running,
            "tick_count": self.tick_count,
            "recovered_session_count": self.recovered_session_count,
            "recovery_required_session_count": (
                self.recovery_required_session_count
            ),
            "superseded_session_count": self.superseded_session_count,
            "delivered_event_count": self.delivered_event_count,
            "retry_wait_event_count": self.retry_wait_event_count,
            "dead_letter_event_count": self.dead_letter_event_count,
            "recovery_required_event_count": (
                self.recovery_required_event_count
            ),
            "superseded_event_count": self.superseded_event_count,
            "last_error_code": self.last_error_code,
        }


class DurableLocalWorkerLoop:
    """Run bounded recovery and outbox pages over one shared SQLite runtime."""

    def __init__(
        self,
        runtime: DurableSQLiteRuntime,
        *,
        worker_id: str,
        configuration: LocalDaemonWorkerConfiguration,
    ) -> None:
        if type(runtime) is not DurableSQLiteRuntime:
            raise TypeError("runtime must be exactly DurableSQLiteRuntime")
        if (
            type(worker_id) is not str
            or not worker_id
            or worker_id.strip() != worker_id
            or len(worker_id) > 128
            or not worker_id.isprintable()
        ):
            raise ValueError("worker_id must be a bounded printable string")
        if type(configuration) is not LocalDaemonWorkerConfiguration:
            raise TypeError(
                "configuration must be LocalDaemonWorkerConfiguration"
            )
        self._runtime = runtime
        self._worker_id = worker_id
        self._configuration = configuration
        self._stop_event = Event()
        self._status_lock = Lock()
        self._thread: Thread | None = None
        self._tick_count = 0
        self._recovered_session_count = 0
        self._recovery_required_session_count = 0
        self._superseded_session_count = 0
        self._delivered_event_count = 0
        self._retry_wait_event_count = 0
        self._dead_letter_event_count = 0
        self._recovery_required_event_count = 0
        self._superseded_event_count = 0
        self._last_error_code: str | None = None

    def _record(
        self,
        *,
        recovered: int,
        recovery_required_sessions: int,
        superseded_sessions: int,
        delivered: int,
        retry_wait_events: int,
        dead_letter_events: int,
        recovery_required_events: int,
        superseded_events: int,
        error_code: str | None,
    ) -> None:
        with self._status_lock:
            self._tick_count += 1
            self._recovered_session_count += recovered
            self._recovery_required_session_count += (
                recovery_required_sessions
            )
            self._superseded_session_count += superseded_sessions
            self._delivered_event_count += delivered
            self._retry_wait_event_count += retry_wait_events
            self._dead_letter_event_count += dead_letter_events
            self._recovery_required_event_count += recovery_required_events
            self._superseded_event_count += superseded_events
            self._last_error_code = error_code

    def run_once(self) -> LocalDaemonWorkerStatus:
        recovered = 0
        recovery_required_sessions = 0
        superseded_sessions = 0
        delivered = 0
        retry_wait_events = 0
        dead_letter_events = 0
        recovery_required_events = 0
        superseded_events = 0
        error_code: str | None = None
        try:
            recovery = self._runtime.recover_due(
                limit=self._configuration.recovery_limit,
            )
            recovered = sum(
                result.outcome == "expired" for result in recovery
            )
            recovery_required_sessions = sum(
                result.outcome == "recovery_required"
                for result in recovery
            )
            superseded_sessions = sum(
                result.outcome == "superseded" for result in recovery
            )
        except DurableRuntimeV3Error as error:
            error_code = error.code
        except Exception:
            error_code = "TBM_LOCAL_DAEMON_RECOVERY_FAILED"
        try:
            outbox = self._runtime.deliver_outbox(
                worker_id=self._worker_id,
                lease_seconds=self._configuration.outbox_lease_seconds,
                limit=self._configuration.outbox_limit,
                retry_delay_seconds=(
                    self._configuration.outbox_retry_delay_seconds
                ),
                max_attempts=self._configuration.outbox_max_attempts,
            )
            delivered = sum(
                result.outcome == "delivered" for result in outbox
            )
            retry_wait_events = sum(
                result.outcome == "retry_wait" for result in outbox
            )
            dead_letter_events = sum(
                result.outcome == "dead_letter" for result in outbox
            )
            recovery_required_events = sum(
                result.outcome == "recovery_required"
                for result in outbox
            )
            superseded_events = sum(
                result.outcome == "superseded" for result in outbox
            )
        except DurableRuntimeV3Error as error:
            error_code = error.code
        except Exception:
            error_code = "TBM_LOCAL_DAEMON_OUTBOX_FAILED"
        self._record(
            recovered=recovered,
            recovery_required_sessions=recovery_required_sessions,
            superseded_sessions=superseded_sessions,
            delivered=delivered,
            retry_wait_events=retry_wait_events,
            dead_letter_events=dead_letter_events,
            recovery_required_events=recovery_required_events,
            superseded_events=superseded_events,
            error_code=error_code,
        )
        return self.status()

    def _run(self) -> None:
        while not self._stop_event.is_set():
            self.run_once()
            self._stop_event.wait(
                self._configuration.interval_seconds
            )

    def start(self) -> None:
        if self._thread is not None:
            raise LocalDaemonV3Error(
                "TBM_LOCAL_DAEMON_WORKER_ALREADY_STARTED",
                "local daemon worker is already started",
            )
        self._thread = Thread(
            target=self._run,
            name="tbmd-durable-workers",
            daemon=False,
        )
        self._thread.start()

    def stop(self, *, timeout_seconds: float = 10.0) -> None:
        if (
            type(timeout_seconds) is not float
            or not math.isfinite(timeout_seconds)
            or timeout_seconds <= 0
        ):
            raise ValueError("worker stop timeout must be positive and finite")
        self._stop_event.set()
        thread = self._thread
        if thread is None:
            return
        thread.join(timeout_seconds)
        if thread.is_alive():
            raise LocalDaemonV3Error(
                "TBM_LOCAL_DAEMON_WORKER_STOP_TIMEOUT",
                "local daemon worker did not stop in time",
            )

    def status(self) -> LocalDaemonWorkerStatus:
        with self._status_lock:
            thread = self._thread
            return LocalDaemonWorkerStatus(
                contract_version=LOCAL_DAEMON_CONTRACT_VERSION,
                running=thread is not None and thread.is_alive(),
                tick_count=self._tick_count,
                recovered_session_count=self._recovered_session_count,
                recovery_required_session_count=(
                    self._recovery_required_session_count
                ),
                superseded_session_count=self._superseded_session_count,
                delivered_event_count=self._delivered_event_count,
                retry_wait_event_count=self._retry_wait_event_count,
                dead_letter_event_count=self._dead_letter_event_count,
                recovery_required_event_count=(
                    self._recovery_required_event_count
                ),
                superseded_event_count=self._superseded_event_count,
                last_error_code=self._last_error_code,
            )


__all__ = [
    "LOCAL_DAEMON_CONTRACT_VERSION",
    "LOCAL_DAEMON_DATABASE_MODE",
    "LOCAL_DAEMON_DATABASE_NAME",
    "LOCAL_DAEMON_LOCK_NAME",
    "LOCAL_DAEMON_STATE_DIRECTORY_MODE",
    "DurableLocalWorkerLoop",
    "LocalDaemonV3Error",
    "LocalDaemonWorkerConfiguration",
    "LocalDaemonWorkerStatus",
    "local_daemon_lock",
    "prepare_local_database",
    "prepare_local_state_directory",
    "verify_local_database_target",
]
