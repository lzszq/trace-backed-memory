from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass
from datetime import datetime, timezone
import hashlib
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import tempfile
from typing import Literal, NoReturn

from .contracts_v3 import CommitRelationVerifier, canonical_sha256
from .locking import exclusive_file_lock
from .migration_v3 import (
    SnapshotV3MigrationBundle,
    verify_snapshot_v3_migration_bundle,
)
from .sqlite import SQLiteMemoryRepository
from .sqlite_bundle_v3 import (
    install_sqlite_v3_bundle,
    verify_sqlite_v3_bundle,
)
from .sqlite_v3 import SQLiteV3MigrationRepository
from .store import TraceBackedMemoryStore


SQLITE_V3_APPLY_MIGRATION_CONTRACT_VERSION = (
    "tbm.sqlite-v2-to-v3-apply.v1"
)
SQLITE_V3_APPLY_MIGRATION_LOCK_SUFFIX = ".tbm-v3-migration.lock"
SQLiteV3MigrationSourceKind = Literal["snapshot", "sqlite"]
SQLiteV3MigrationProfile = Literal["compat-v2", "durable-v3"]
_SOURCE_KINDS = {"snapshot", "sqlite"}
_PROFILE_EVENTS = (
    ("durable-v3", 1, "TBM_V3_MIGRATION_APPLIED"),
    ("compat-v2", 2, "TBM_V3_MIGRATION_ROLLED_BACK"),
)
_SHA256_PREFIX = "sha256:"


class SQLiteV3ApplyMigrationError(RuntimeError):
    """Stable fail-closed SQLite v2-to-v3 migration failure."""

    def __init__(self, code: str, message: str) -> None:
        self.code = code
        super().__init__(message)


@dataclass(frozen=True)
class _TemporaryFile:
    path: Path
    directory: Path
    device: int
    inode: int
    directory_device: int
    directory_inode: int


@dataclass(frozen=True)
class LegacyRecordDisposition:
    record_kind: str
    record_id: str
    source_status: str | None
    evidence_status: str
    target_status: str
    reason_code: str

    def to_dict(self) -> dict[str, object]:
        return {
            "record_kind": self.record_kind,
            "record_id": self.record_id,
            "source_status": self.source_status,
            "evidence_status": self.evidence_status,
            "target_status": self.target_status,
            "reason_code": self.reason_code,
        }


@dataclass(frozen=True)
class SQLiteV3MigrationInspection:
    application_id: str
    bundle_id: str
    source_kind: SQLiteV3MigrationSourceKind
    backup_path: str
    backup_sha256: str
    normalized_source_snapshot_sha256: str
    disposition_sha256: str
    record_count: int
    applied_at: str
    profile: SQLiteV3MigrationProfile
    compatibility_path: str | None
    compatibility_sha256: str | None
    component_count: int
    catalog_sha256: str
    dispositions: tuple[LegacyRecordDisposition, ...]

    def to_dict(self) -> dict[str, object]:
        evidence_counts: dict[str, int] = {}
        target_counts: dict[str, int] = {}
        for disposition in self.dispositions:
            evidence_counts[disposition.evidence_status] = (
                evidence_counts.get(disposition.evidence_status, 0) + 1
            )
            target_counts[disposition.target_status] = (
                target_counts.get(disposition.target_status, 0) + 1
            )
        return {
            "contract_version": (
                SQLITE_V3_APPLY_MIGRATION_CONTRACT_VERSION
            ),
            "application_id": self.application_id,
            "bundle_id": self.bundle_id,
            "source_kind": self.source_kind,
            "backup_path": self.backup_path,
            "backup_sha256": self.backup_sha256,
            "normalized_source_snapshot_sha256": (
                self.normalized_source_snapshot_sha256
            ),
            "disposition_sha256": self.disposition_sha256,
            "record_count": self.record_count,
            "applied_at": self.applied_at,
            "profile": self.profile,
            "compatibility_path": self.compatibility_path,
            "compatibility_sha256": self.compatibility_sha256,
            "component_count": self.component_count,
            "catalog_sha256": self.catalog_sha256,
            "legacy_evidence_counts": dict(sorted(evidence_counts.items())),
            "target_status_counts": dict(sorted(target_counts.items())),
        }


def _failed(code: str, message: str) -> NoReturn:
    raise SQLiteV3ApplyMigrationError(code, message)


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    try:
        with path.open("rb") as handle:
            while chunk := handle.read(1024 * 1024):
                digest.update(chunk)
    except OSError as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_FILE_READ_FAILED",
            "migration file could not be read",
        ) from error
    return _SHA256_PREFIX + digest.hexdigest()


def _validate_digest(value: object, label: str) -> str:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith(_SHA256_PREFIX)
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        _failed(
            "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
            f"{label} is not a canonical SHA-256 digest",
        )
    return value


def _validate_regular_file(path: Path, label: str) -> Path:
    try:
        file_stat = os.lstat(path)
    except OSError as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_PATH_INVALID",
            f"{label} is unavailable",
        ) from error
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(file_stat, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink != 1
        or bool(file_attributes & reparse_attribute)
    ):
        _failed(
            "TBM_SQLITE_V3_MIGRATION_PATH_INVALID",
            f"{label} must be a single-link regular file",
        )
    try:
        return path.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_PATH_INVALID",
            f"{label} could not be resolved",
        ) from error


def _canonical_output_path(path: str | Path, label: str) -> Path:
    candidate = Path(path).expanduser()
    if not candidate.name:
        _failed(
            "TBM_SQLITE_V3_MIGRATION_PATH_INVALID",
            f"{label} must name a file",
        )
    try:
        parent = candidate.parent.resolve(strict=True)
    except (OSError, RuntimeError) as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_PATH_INVALID",
            f"{label} parent directory is unavailable",
        ) from error
    output = parent / candidate.name
    if _path_lexists(output):
        return _validate_regular_file(output, label)
    return output


def _canonical_source_path(path: str | Path) -> Path:
    return _validate_regular_file(
        Path(path).expanduser(),
        "migration source",
    )


def _validate_distinct_paths(*paths: Path) -> None:
    normalized = [os.path.normcase(os.fspath(path)) for path in paths]
    if len(normalized) != len(set(normalized)):
        _failed(
            "TBM_SQLITE_V3_MIGRATION_PATH_CONFLICT",
            "source, target, backup, and compatibility paths must differ",
        )


def _path_lexists(path: Path) -> bool:
    return os.path.lexists(os.fspath(path))


def _validate_private_directory(
    directory: Path,
    *,
    expected_device: int | None = None,
    expected_inode: int | None = None,
) -> os.stat_result:
    try:
        directory_stat = os.lstat(directory)
    except OSError as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_TEMP_INVALID",
            "migration private temporary directory is unavailable",
        ) from error
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(directory_stat, "st_file_attributes", 0)
    if (
        not stat.S_ISDIR(directory_stat.st_mode)
        or bool(file_attributes & reparse_attribute)
        or (
            os.name != "nt"
            and bool(stat.S_IMODE(directory_stat.st_mode) & 0o077)
        )
        or (
            expected_device is not None
            and directory_stat.st_dev != expected_device
        )
        or (
            expected_inode is not None
            and directory_stat.st_ino != expected_inode
        )
    ):
        _failed(
            "TBM_SQLITE_V3_MIGRATION_TEMP_INVALID",
            "migration private temporary directory changed identity",
        )
    return directory_stat


def _validate_temporary_identity(
    temporary: _TemporaryFile,
    *,
    require_single_link: bool = True,
) -> os.stat_result:
    _validate_private_directory(
        temporary.directory,
        expected_device=temporary.directory_device,
        expected_inode=temporary.directory_inode,
    )
    try:
        file_stat = os.lstat(temporary.path)
    except OSError as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_TEMP_INVALID",
            "migration temporary file is unavailable",
        ) from error
    reparse_attribute = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0)
    file_attributes = getattr(file_stat, "st_file_attributes", 0)
    if (
        not stat.S_ISREG(file_stat.st_mode)
        or file_stat.st_nlink < 1
        or (require_single_link and file_stat.st_nlink != 1)
        or bool(file_attributes & reparse_attribute)
        or file_stat.st_dev != temporary.device
        or file_stat.st_ino != temporary.inode
    ):
        _failed(
            "TBM_SQLITE_V3_MIGRATION_TEMP_INVALID",
            "migration temporary file changed identity",
        )
    return file_stat


def _create_temporary(
    destination: Path,
    purpose: str,
) -> _TemporaryFile:
    try:
        raw_directory = tempfile.mkdtemp(
            prefix=f".{destination.name}.{purpose}.",
            dir=destination.parent,
        )
    except OSError as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_TEMP_CREATE_FAILED",
            "migration private temporary directory could not be created",
        ) from error
    directory = Path(raw_directory)
    try:
        directory_stat = _validate_private_directory(directory)
        descriptor = os.open(
            directory / "payload",
            os.O_RDWR
            | os.O_CREAT
            | os.O_EXCL
            | getattr(os, "O_NOFOLLOW", 0),
            0o600,
        )
    except BaseException:
        try:
            directory.rmdir()
        except OSError:
            pass
        raise
    path = directory / "payload"
    try:
        file_stat = os.fstat(descriptor)
        if not stat.S_ISREG(file_stat.st_mode) or file_stat.st_nlink != 1:
            _failed(
                "TBM_SQLITE_V3_MIGRATION_TEMP_INVALID",
                "migration temporary file has an invalid identity",
            )
    except BaseException:
        os.close(descriptor)
        try:
            path.unlink()
            directory.rmdir()
        except OSError:
            pass
        raise
    try:
        os.close(descriptor)
    except OSError as error:
        try:
            path.unlink()
            directory.rmdir()
        except OSError:
            pass
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_TEMP_CREATE_FAILED",
            "migration temporary file could not be closed",
        ) from error
    temporary = _TemporaryFile(
        path=path,
        directory=directory,
        device=file_stat.st_dev,
        inode=file_stat.st_ino,
        directory_device=directory_stat.st_dev,
        directory_inode=directory_stat.st_ino,
    )
    _validate_temporary_identity(temporary)
    return temporary


def _remove_safe_temporary(temporary: _TemporaryFile) -> None:
    _validate_private_directory(
        temporary.directory,
        expected_device=temporary.directory_device,
        expected_inode=temporary.directory_inode,
    )
    try:
        os.lstat(temporary.path)
    except FileNotFoundError:
        pass
    except OSError as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_TEMP_CLEANUP_FAILED",
            "migration temporary file could not be inspected",
        ) from error
    else:
        _validate_temporary_identity(
            temporary,
            require_single_link=False,
        )
        try:
            temporary.path.unlink()
        except OSError as error:
            raise SQLiteV3ApplyMigrationError(
                "TBM_SQLITE_V3_MIGRATION_TEMP_CLEANUP_FAILED",
                "migration temporary file could not be removed",
            ) from error
    try:
        temporary.directory.rmdir()
    except OSError as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_TEMP_CLEANUP_FAILED",
            "migration private temporary directory could not be removed",
        ) from error


def _fsync_temporary(temporary: _TemporaryFile) -> None:
    flags = os.O_RDWR | getattr(os, "O_NOFOLLOW", 0)
    try:
        descriptor = os.open(temporary.path, flags)
    except OSError as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_SYNC_FAILED",
            "migration temporary file could not be opened for sync",
        ) from error
    try:
        file_stat = os.fstat(descriptor)
        if (
            not stat.S_ISREG(file_stat.st_mode)
            or file_stat.st_nlink != 1
            or file_stat.st_dev != temporary.device
            or file_stat.st_ino != temporary.inode
        ):
            _failed(
                "TBM_SQLITE_V3_MIGRATION_TEMP_INVALID",
                "migration temporary file changed identity before sync",
            )
        os.fsync(descriptor)
    except OSError as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_SYNC_FAILED",
            "migration temporary file could not be synchronized",
        ) from error
    finally:
        os.close(descriptor)
    _validate_temporary_identity(temporary)


def _fsync_parent_directory(path: Path) -> None:
    if os.name == "nt":
        return
    flags = os.O_RDONLY | getattr(os, "O_DIRECTORY", 0)
    try:
        descriptor = os.open(path.parent, flags)
    except OSError as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_SYNC_FAILED",
            "migration parent directory could not be opened",
        ) from error
    try:
        os.fsync(descriptor)
    except OSError as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_SYNC_FAILED",
            "migration parent directory could not be synchronized",
        ) from error
    finally:
        os.close(descriptor)


def _publish_temporary(
    temporary: _TemporaryFile,
    destination: Path,
) -> None:
    _validate_temporary_identity(temporary)
    if _path_lexists(destination):
        _failed(
            "TBM_SQLITE_V3_MIGRATION_TARGET_EXISTS",
            "migration destination already exists",
        )
    _fsync_temporary(temporary)
    _validate_temporary_identity(temporary)
    try:
        os.link(temporary.path, destination)
        published_stat = os.lstat(destination)
        if (
            not stat.S_ISREG(published_stat.st_mode)
            or published_stat.st_dev != temporary.device
            or published_stat.st_ino != temporary.inode
        ):
            destination.unlink()
            _failed(
                "TBM_SQLITE_V3_MIGRATION_PUBLISH_FAILED",
                "published migration destination has the wrong identity",
            )
        _fsync_parent_directory(destination)
        temporary.path.unlink()
        _fsync_parent_directory(destination)
    except FileExistsError as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_TARGET_EXISTS",
            "migration destination appeared during publication",
        ) from error
    except OSError as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_PUBLISH_FAILED",
            "migration destination could not be published",
        ) from error


def _load_snapshot_source(path: Path) -> TraceBackedMemoryStore:
    try:
        return TraceBackedMemoryStore.load_json(path)
    except (
        OSError,
        UnicodeError,
        TypeError,
        ValueError,
        OverflowError,
    ) as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_SOURCE_INVALID",
            "source snapshot failed strict version-2 validation",
        ) from error


def _open_sqlite(path: Path, *, read_only: bool) -> sqlite3.Connection:
    try:
        if read_only:
            connection = sqlite3.connect(
                f"{path.as_uri()}?mode=ro",
                uri=True,
                timeout=5.0,
            )
        else:
            connection = sqlite3.connect(path, timeout=5.0)
        connection.execute("PRAGMA foreign_keys = ON")
        if not read_only:
            connection.execute("PRAGMA synchronous = FULL")
        return connection
    except (OSError, sqlite3.Error, ValueError) as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_DATABASE_OPEN_FAILED",
            "migration SQLite database could not be opened",
        ) from error


def _load_sqlite_source(path: Path) -> TraceBackedMemoryStore:
    connection = _open_sqlite(path, read_only=True)
    try:
        v3_row = connection.execute(
            "SELECT count(*) FROM sqlite_master "
            "WHERE type = 'table' "
            "AND name = 'trace_backed_memory_v3_bundle_schema'"
        ).fetchone()
        if v3_row != (0,):
            _failed(
                "TBM_SQLITE_V3_MIGRATION_SOURCE_ALREADY_V3",
                "source SQLite database already contains a v3 bundle",
            )
        return SQLiteMemoryRepository(connection).load()
    except SQLiteV3ApplyMigrationError:
        raise
    except Exception as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_SOURCE_INVALID",
            "source SQLite database failed strict schema-v1 validation",
        ) from error
    finally:
        connection.close()


def _load_source(
    path: Path,
    source_kind: SQLiteV3MigrationSourceKind,
) -> TraceBackedMemoryStore:
    if source_kind == "snapshot":
        return _load_snapshot_source(path)
    return _load_sqlite_source(path)


def _snapshot_digest(store: TraceBackedMemoryStore) -> str:
    return canonical_sha256(store.to_snapshot())


def _verify_source_matches_bundle(
    store: TraceBackedMemoryStore,
    bundle: SnapshotV3MigrationBundle,
) -> None:
    if _snapshot_digest(store) != bundle.normalized_source_snapshot_sha256:
        _failed(
            "TBM_SQLITE_V3_MIGRATION_SOURCE_MISMATCH",
            "source state does not match the migration bundle",
        )


def _backup_snapshot(source: Path, backup: Path) -> None:
    temporary = _create_temporary(backup, "backup")
    try:
        _validate_temporary_identity(temporary)
        shutil.copyfile(source, temporary.path)
        _validate_temporary_identity(temporary)
        _publish_temporary(temporary, backup)
    except SQLiteV3ApplyMigrationError:
        raise
    except OSError as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_BACKUP_FAILED",
            "source snapshot backup failed",
        ) from error
    finally:
        _remove_safe_temporary(temporary)


def _backup_sqlite(source: Path, backup: Path) -> None:
    temporary = _create_temporary(backup, "backup")
    source_connection = _open_sqlite(source, read_only=True)
    target_connection: sqlite3.Connection | None = None
    try:
        _validate_temporary_identity(temporary)
        target_connection = sqlite3.connect(temporary.path)
        target_connection.execute("PRAGMA synchronous = FULL")
        source_connection.backup(target_connection)
        target_connection.commit()
        target_connection.close()
        target_connection = None
        _validate_temporary_identity(temporary)
        _publish_temporary(temporary, backup)
    except SQLiteV3ApplyMigrationError:
        raise
    except (OSError, sqlite3.Error) as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_BACKUP_FAILED",
            "source SQLite backup failed",
        ) from error
    finally:
        source_connection.close()
        if target_connection is not None:
            target_connection.close()
        _remove_safe_temporary(temporary)


def _verify_backup(
    backup: Path,
    source_kind: SQLiteV3MigrationSourceKind,
    expected_snapshot_sha256: str,
) -> str:
    _validate_regular_file(backup, "migration backup")
    store = _load_source(backup, source_kind)
    if _snapshot_digest(store) != expected_snapshot_sha256:
        _failed(
            "TBM_SQLITE_V3_MIGRATION_BACKUP_MISMATCH",
            "migration backup does not reconstruct the source state",
        )
    return _sha256_file(backup)


def _ensure_backup(
    source: Path,
    backup: Path,
    source_kind: SQLiteV3MigrationSourceKind,
    expected_snapshot_sha256: str,
) -> str:
    if _path_lexists(backup):
        return _verify_backup(
            backup,
            source_kind,
            expected_snapshot_sha256,
        )
    if source_kind == "snapshot":
        _backup_snapshot(source, backup)
    else:
        _backup_sqlite(source, backup)
    return _verify_backup(
        backup,
        source_kind,
        expected_snapshot_sha256,
    )


def _create_compatibility_database(
    store: TraceBackedMemoryStore,
    destination: Path,
) -> None:
    temporary = _create_temporary(destination, "compat")
    repository: SQLiteMemoryRepository | None = None
    try:
        _validate_temporary_identity(temporary)
        repository = SQLiteMemoryRepository.connect(
            temporary.path,
            initialize=True,
        )
        repository.sync(store)
        repository.close()
        repository = None
        _validate_temporary_identity(temporary)
        _publish_temporary(temporary, destination)
    except SQLiteV3ApplyMigrationError:
        raise
    except Exception as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_COMPAT_WRITE_FAILED",
            "compatibility SQLite database could not be written",
        ) from error
    finally:
        if repository is not None:
            repository.close()
        _remove_safe_temporary(temporary)


def _record_dispositions(
    bundle: SnapshotV3MigrationBundle,
) -> tuple[LegacyRecordDisposition, ...]:
    snapshot = bundle.source_snapshot
    evidence_case_ids = {
        evidence.case_id for evidence in bundle.mapping.regression_evidence
    }
    dispositions: list[LegacyRecordDisposition] = []
    for raw in snapshot["traces"]:
        trace_id = str(raw["trace_id"])
        dirty = bool(raw["dirty"])
        dispositions.append(
            LegacyRecordDisposition(
                record_kind="trace",
                record_id=trace_id,
                source_status="dirty" if dirty else "clean",
                evidence_status=(
                    "legacy_dirty_trace" if dirty else "legacy_trace"
                ),
                target_status="retained_legacy",
                reason_code=(
                    "TBM_V3_LEGACY_DIRTY_TRACE"
                    if dirty
                    else "TBM_V3_LEGACY_TRACE_RETAINED"
                ),
            )
        )
    case_evidence: dict[str, str] = {}
    for raw in snapshot["failure_cases"]:
        case_id = str(raw["case_id"])
        mapped = case_id in evidence_case_ids
        case_evidence[case_id] = (
            "mapped_regression_preflight"
            if mapped
            else "legacy_unverified"
        )
        dispositions.append(
            LegacyRecordDisposition(
                record_kind="failure_case",
                record_id=case_id,
                source_status=str(raw["status"]),
                evidence_status=case_evidence[case_id],
                target_status="retained_legacy",
                reason_code=(
                    "TBM_V3_MAPPED_REGRESSION_PREFLIGHT"
                    if mapped
                    else "TBM_V3_LEGACY_EVIDENCE_UNVERIFIED"
                ),
            )
        )
    for raw in snapshot["lessons"]:
        source_case_id = str(raw["source_case_id"])
        evidence_status = case_evidence.get(
            source_case_id,
            "legacy_unverified",
        )
        dispositions.append(
            LegacyRecordDisposition(
                record_kind="lesson",
                record_id=str(raw["lesson_id"]),
                source_status=str(raw["status"]),
                evidence_status=evidence_status,
                target_status="unpublished_v3",
                reason_code="TBM_V3_LEGACY_MEMORY_NOT_ACTIVATED",
            )
        )
    for raw in snapshot["project_policies"]:
        dispositions.append(
            LegacyRecordDisposition(
                record_kind="project_policy",
                record_id=str(raw["policy_id"]),
                source_status=str(raw["status"]),
                evidence_status="legacy_unverified",
                target_status="unpublished_v3",
                reason_code="TBM_V3_LEGACY_POLICY_NOT_ACTIVATED",
            )
        )
    for raw in snapshot["usage_logs"]:
        dispositions.append(
            LegacyRecordDisposition(
                record_kind="usage_log",
                record_id=str(raw["decision_id"]),
                source_status=(
                    None
                    if raw["eval_result"] is None
                    else str(raw["eval_result"])
                ),
                evidence_status="legacy_partial_replay",
                target_status="legacy_partial",
                reason_code="TBM_V3_LEGACY_REPLAY_PARTIAL",
            )
        )
    return tuple(
        sorted(
            dispositions,
            key=lambda item: (item.record_kind, item.record_id),
        )
    )


def _disposition_sha256(
    dispositions: tuple[LegacyRecordDisposition, ...],
) -> str:
    return canonical_sha256(
        [disposition.to_dict() for disposition in dispositions]
    )


def _timestamp(clock: Callable[[], datetime] | None) -> str:
    value = datetime.now(timezone.utc) if clock is None else clock()
    if (
        type(value) is not datetime
        or value.tzinfo is None
        or value.utcoffset() is None
    ):
        _failed(
            "TBM_SQLITE_V3_MIGRATION_CLOCK_INVALID",
            "migration clock must return an aware datetime",
        )
    return value.astimezone(timezone.utc).isoformat().replace("+00:00", "Z")


def _validate_timestamp(value: object, label: str) -> str:
    if type(value) is not str or not value.endswith("Z"):
        _failed(
            "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
            f"{label} must be a canonical UTC timestamp",
        )
    try:
        parsed = datetime.fromisoformat(value[:-1] + "+00:00")
    except ValueError as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
            f"{label} must be a canonical UTC timestamp",
        ) from error
    canonical = (
        parsed.astimezone(timezone.utc)
        .isoformat()
        .replace("+00:00", "Z")
    )
    if parsed.utcoffset() != timezone.utc.utcoffset(parsed) or canonical != value:
        _failed(
            "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
            f"{label} must be a canonical UTC timestamp",
        )
    return value


def _application_id(
    *,
    bundle: SnapshotV3MigrationBundle,
    source_kind: SQLiteV3MigrationSourceKind,
    backup_path: Path,
    backup_sha256: str,
    disposition_sha256: str,
    record_count: int,
    applied_at: str,
) -> str:
    return canonical_sha256(
        {
            "contract_version": (
                SQLITE_V3_APPLY_MIGRATION_CONTRACT_VERSION
            ),
            "bundle_id": bundle.bundle_id,
            "source_kind": source_kind,
            "backup_path": os.fspath(backup_path),
            "backup_sha256": backup_sha256,
            "normalized_source_snapshot_sha256": (
                bundle.normalized_source_snapshot_sha256
            ),
            "disposition_sha256": disposition_sha256,
            "record_count": record_count,
            "applied_at": applied_at,
        }
    )


def _insert_application(
    connection: sqlite3.Connection,
    *,
    application_id: str,
    bundle: SnapshotV3MigrationBundle,
    source_kind: SQLiteV3MigrationSourceKind,
    backup_path: Path,
    backup_sha256: str,
    dispositions: tuple[LegacyRecordDisposition, ...],
    applied_at: str,
) -> None:
    disposition_sha256 = _disposition_sha256(dispositions)
    try:
        connection.execute("BEGIN IMMEDIATE")
        connection.execute(
            "INSERT INTO v3_migration_applications ("
            "application_id, bundle_id, source_kind, backup_path, "
            "backup_sha256, normalized_source_snapshot_sha256, "
            "disposition_sha256, record_count, applied_at"
            ") VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                application_id,
                bundle.bundle_id,
                source_kind,
                os.fspath(backup_path),
                backup_sha256,
                bundle.normalized_source_snapshot_sha256,
                disposition_sha256,
                len(dispositions),
                applied_at,
            ),
        )
        connection.executemany(
            "INSERT INTO v3_migration_record_dispositions ("
            "application_id, record_kind, record_id, source_status, "
            "evidence_status, target_status, reason_code"
            ") VALUES (?, ?, ?, ?, ?, ?, ?)",
            (
                (
                    application_id,
                    disposition.record_kind,
                    disposition.record_id,
                    disposition.source_status,
                    disposition.evidence_status,
                    disposition.target_status,
                    disposition.reason_code,
                )
                for disposition in dispositions
            ),
        )
        connection.execute(
            "INSERT INTO v3_migration_profile_events ("
            "application_id, sequence, profile, compatibility_path, "
            "compatibility_sha256, occurred_at, reason_code"
            ") VALUES (?, 1, 'durable-v3', NULL, NULL, ?, ?)",
            (
                application_id,
                applied_at,
                "TBM_V3_MIGRATION_APPLIED",
            ),
        )
        connection.commit()
    except sqlite3.Error as error:
        if connection.in_transaction:
            connection.rollback()
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_STATE_WRITE_FAILED",
            "migration application state could not be committed",
        ) from error


def _build_target(
    *,
    store: TraceBackedMemoryStore,
    bundle: SnapshotV3MigrationBundle,
    target: Path,
    backup_path: Path,
    backup_sha256: str,
    source_kind: SQLiteV3MigrationSourceKind,
    commit_relation_verifier: CommitRelationVerifier | None,
    clock: Callable[[], datetime] | None,
) -> None:
    temporary = _create_temporary(target, "apply")
    repository: SQLiteMemoryRepository | None = None
    connection: sqlite3.Connection | None = None
    try:
        _validate_temporary_identity(temporary)
        repository = SQLiteMemoryRepository.connect(
            temporary.path,
            initialize=True,
        )
        repository.sync(store)
        repository.close()
        repository = None
        connection = _open_sqlite(temporary.path, read_only=False)
        connection.execute("PRAGMA recursive_triggers = ON")
        install_sqlite_v3_bundle(connection)
        SQLiteV3MigrationRepository(connection).stage(
            bundle,
            commit_relation_verifier=commit_relation_verifier,
        )
        dispositions = _record_dispositions(bundle)
        applied_at = _timestamp(clock)
        disposition_sha256 = _disposition_sha256(dispositions)
        application_id = _application_id(
            bundle=bundle,
            source_kind=source_kind,
            backup_path=backup_path,
            backup_sha256=backup_sha256,
            disposition_sha256=disposition_sha256,
            record_count=len(dispositions),
            applied_at=applied_at,
        )
        _insert_application(
            connection,
            application_id=application_id,
            bundle=bundle,
            source_kind=source_kind,
            backup_path=backup_path,
            backup_sha256=backup_sha256,
            dispositions=dispositions,
            applied_at=applied_at,
        )
        connection.close()
        connection = None
        _validate_temporary_identity(temporary)
        _publish_temporary(temporary, target)
    except SQLiteV3ApplyMigrationError:
        raise
    except Exception as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_APPLY_FAILED",
            "SQLite v3 migration target could not be constructed",
        ) from error
    finally:
        if repository is not None:
            repository.close()
        if connection is not None:
            connection.close()
        _remove_safe_temporary(temporary)


def _application_row(
    connection: sqlite3.Connection,
) -> tuple[object, ...]:
    try:
        rows = connection.execute(
            "SELECT application_id, bundle_id, source_kind, backup_path, "
            "backup_sha256, normalized_source_snapshot_sha256, "
            "disposition_sha256, record_count, applied_at "
            "FROM v3_migration_applications ORDER BY application_id"
        ).fetchall()
    except sqlite3.Error as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
            "migration application state is unavailable",
        ) from error
    if len(rows) != 1 or len(rows[0]) != 9:
        _failed(
            "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
            "migration target must contain exactly one application",
        )
    return rows[0]


def _load_dispositions(
    connection: sqlite3.Connection,
    application_id: str,
) -> tuple[LegacyRecordDisposition, ...]:
    try:
        rows = connection.execute(
            "SELECT record_kind, record_id, source_status, "
            "evidence_status, target_status, reason_code "
            "FROM v3_migration_record_dispositions "
            "WHERE application_id = ? ORDER BY record_kind, record_id",
            (application_id,),
        ).fetchall()
    except sqlite3.Error as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
            "migration record dispositions are unavailable",
        ) from error
    dispositions: list[LegacyRecordDisposition] = []
    for row in rows:
        if (
            len(row) != 6
            or type(row[0]) is not str
            or type(row[1]) is not str
            or (row[2] is not None and type(row[2]) is not str)
            or type(row[3]) is not str
            or type(row[4]) is not str
            or type(row[5]) is not str
        ):
            _failed(
                "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
                "migration record disposition is invalid",
            )
        dispositions.append(
            LegacyRecordDisposition(
                record_kind=row[0],
                record_id=row[1],
                source_status=row[2],
                evidence_status=row[3],
                target_status=row[4],
                reason_code=row[5],
            )
        )
    return tuple(dispositions)


def _profile_state(
    connection: sqlite3.Connection,
    application_id: str,
    *,
    applied_at: str,
) -> tuple[SQLiteV3MigrationProfile, str | None, str | None]:
    try:
        rows = connection.execute(
            "SELECT sequence, profile, compatibility_path, "
            "compatibility_sha256, occurred_at, reason_code "
            "FROM v3_migration_profile_events "
            "WHERE application_id = ? ORDER BY sequence",
            (application_id,),
        ).fetchall()
    except sqlite3.Error as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
            "migration profile events are unavailable",
        ) from error
    if len(rows) not in {1, 2}:
        _failed(
            "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
            "migration profile event chain is incomplete",
        )
    for index, row in enumerate(rows):
        expected_profile, expected_sequence, expected_reason = (
            _PROFILE_EVENTS[index]
        )
        if (
            len(row) != 6
            or row[0] != expected_sequence
            or row[1] != expected_profile
            or row[5] != expected_reason
        ):
            _failed(
                "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
                "migration profile event chain is invalid",
            )
        occurred_at = _validate_timestamp(
            row[4],
            "profile event occurred_at",
        )
        if index == 0 and occurred_at != applied_at:
            _failed(
                "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
                "initial profile event timestamp must match the application",
            )
    latest = rows[-1]
    profile = latest[1]
    compatibility_path = latest[2]
    compatibility_sha256 = latest[3]
    if profile == "durable-v3":
        if compatibility_path is not None or compatibility_sha256 is not None:
            _failed(
                "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
                "durable migration profile must not name compatibility data",
            )
    else:
        if (
            type(compatibility_path) is not str
            or type(compatibility_sha256) is not str
        ):
            _failed(
                "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
                "compatibility migration profile is incomplete",
            )
        _validate_digest(
            compatibility_sha256,
            "compatibility_sha256",
        )
    return profile, compatibility_path, compatibility_sha256


def _verify_compatibility_output(
    path_value: str,
    expected_sha256: str,
    expected_snapshot_sha256: str,
) -> None:
    path = _validate_regular_file(
        Path(path_value),
        "compatibility SQLite database",
    )
    if _sha256_file(path) != expected_sha256:
        _failed(
            "TBM_SQLITE_V3_MIGRATION_COMPAT_MISMATCH",
            "compatibility SQLite digest does not match rollback state",
        )
    store = _load_sqlite_source(path)
    if _snapshot_digest(store) != expected_snapshot_sha256:
        _failed(
            "TBM_SQLITE_V3_MIGRATION_COMPAT_MISMATCH",
            "compatibility SQLite state does not match the source",
        )


def _verify_integrity(connection: sqlite3.Connection) -> None:
    try:
        rows = connection.execute("PRAGMA integrity_check").fetchall()
    except sqlite3.Error as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_INTEGRITY_FAILED",
            "SQLite integrity verification failed",
        ) from error
    if rows != [("ok",)]:
        _failed(
            "TBM_SQLITE_V3_MIGRATION_INTEGRITY_FAILED",
            "SQLite integrity verification did not return ok",
        )


def _inspect_connection(
    connection: sqlite3.Connection,
    *,
    commit_relation_verifier: CommitRelationVerifier | None,
    replay_bundle: bool = True,
) -> SQLiteV3MigrationInspection:
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    manifest = verify_sqlite_v3_bundle(connection)
    _verify_integrity(connection)
    row = _application_row(connection)
    (
        application_id,
        bundle_id,
        source_kind,
        backup_path,
        backup_sha256,
        normalized_source_snapshot_sha256,
        disposition_sha256,
        record_count,
        applied_at,
    ) = row
    if (
        type(application_id) is not str
        or type(bundle_id) is not str
        or source_kind not in _SOURCE_KINDS
        or type(backup_path) is not str
        or type(record_count) is not int
        or type(applied_at) is not str
    ):
        _failed(
            "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
            "migration application row is invalid",
        )
    _validate_digest(application_id, "application_id")
    _validate_digest(backup_sha256, "backup_sha256")
    _validate_digest(
        normalized_source_snapshot_sha256,
        "normalized_source_snapshot_sha256",
    )
    _validate_digest(disposition_sha256, "disposition_sha256")
    applied_at = _validate_timestamp(applied_at, "application applied_at")
    repository = SQLiteV3MigrationRepository(connection)
    try:
        bundle = repository.load(bundle_id)
    except Exception as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
            "migration bundle could not be loaded from the target",
        ) from error
    if not bundle.ready:
        _failed(
            "TBM_SQLITE_V3_MIGRATION_BUNDLE_BLOCKED",
            "migration target references a blocked bundle",
        )
    if replay_bundle:
        verify_snapshot_v3_migration_bundle(
            bundle,
            commit_relation_verifier=commit_relation_verifier,
        )
    if (
        bundle.normalized_source_snapshot_sha256
        != normalized_source_snapshot_sha256
    ):
        _failed(
            "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
            "migration application does not match its bundle",
        )
    store = SQLiteMemoryRepository(connection).load()
    if _snapshot_digest(store) != normalized_source_snapshot_sha256:
        _failed(
            "TBM_SQLITE_V3_MIGRATION_TARGET_MISMATCH",
            "target compatibility state does not match the source bundle",
        )
    backup = _validate_regular_file(
        Path(backup_path),
        "migration backup",
    )
    if (
        os.path.normcase(os.fspath(backup))
        != os.path.normcase(backup_path)
        or _sha256_file(backup) != backup_sha256
    ):
        _failed(
            "TBM_SQLITE_V3_MIGRATION_BACKUP_MISMATCH",
            "migration backup digest does not match the application",
        )
    backup_store = _load_source(backup, source_kind)
    if _snapshot_digest(backup_store) != normalized_source_snapshot_sha256:
        _failed(
            "TBM_SQLITE_V3_MIGRATION_BACKUP_MISMATCH",
            "migration backup state does not match the source bundle",
        )
    dispositions = _load_dispositions(connection, application_id)
    expected_dispositions = _record_dispositions(bundle)
    if (
        dispositions != expected_dispositions
        or len(dispositions) != record_count
        or _disposition_sha256(dispositions) != disposition_sha256
    ):
        _failed(
            "TBM_SQLITE_V3_MIGRATION_DISPOSITION_MISMATCH",
            "legacy record dispositions do not exactly replay",
        )
    expected_application_id = _application_id(
        bundle=bundle,
        source_kind=source_kind,
        backup_path=Path(backup_path),
        backup_sha256=backup_sha256,
        disposition_sha256=disposition_sha256,
        record_count=record_count,
        applied_at=applied_at,
    )
    if application_id != expected_application_id:
        _failed(
            "TBM_SQLITE_V3_MIGRATION_APPLICATION_MISMATCH",
            "migration application ID does not match its immutable state",
        )
    profile, compatibility_path, compatibility_sha256 = _profile_state(
        connection,
        application_id,
        applied_at=applied_at,
    )
    if (
        profile == "compat-v2"
        and compatibility_path is not None
        and compatibility_sha256 is not None
    ):
        _verify_compatibility_output(
            compatibility_path,
            compatibility_sha256,
            normalized_source_snapshot_sha256,
        )
    return SQLiteV3MigrationInspection(
        application_id=application_id,
        bundle_id=bundle_id,
        source_kind=source_kind,
        backup_path=backup_path,
        backup_sha256=backup_sha256,
        normalized_source_snapshot_sha256=(
            normalized_source_snapshot_sha256
        ),
        disposition_sha256=disposition_sha256,
        record_count=record_count,
        applied_at=applied_at,
        profile=profile,
        compatibility_path=compatibility_path,
        compatibility_sha256=compatibility_sha256,
        component_count=len(manifest.components),
        catalog_sha256=manifest.catalog_sha256,
        dispositions=dispositions,
    )


def verify_sqlite_v3_migration(
    target_database: str | Path,
    *,
    commit_relation_verifier: CommitRelationVerifier | None = None,
) -> SQLiteV3MigrationInspection:
    """Verify the schema, bundle, legacy data, backup, and profile chain."""

    target = _validate_regular_file(
        Path(target_database).expanduser(),
        "migration target",
    )
    connection = _open_sqlite(target, read_only=False)
    try:
        return _inspect_connection(
            connection,
            commit_relation_verifier=commit_relation_verifier,
        )
    except SQLiteV3ApplyMigrationError:
        raise
    except Exception as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_VERIFY_FAILED",
            "SQLite v3 migration verification failed",
        ) from error
    finally:
        connection.close()


def require_durable_sqlite_migration_profile(
    connection: sqlite3.Connection,
) -> None:
    """Reject runtime use after an operator has rolled back to compat-v2."""

    if not isinstance(connection, sqlite3.Connection):
        raise TypeError("connection must be sqlite3.Connection")
    try:
        rows = connection.execute(
            "SELECT application_id FROM v3_migration_applications"
        ).fetchall()
    except sqlite3.Error as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
            "migration profile state is unavailable",
        ) from error
    if not rows:
        return
    if len(rows) != 1 or len(rows[0]) != 1 or type(rows[0][0]) is not str:
        _failed(
            "TBM_SQLITE_V3_MIGRATION_STATE_INVALID",
            "migration target has ambiguous application state",
        )
    inspection = _inspect_connection(
        connection,
        commit_relation_verifier=None,
        replay_bundle=False,
    )
    if inspection.profile != "durable-v3":
        _failed(
            "TBM_SQLITE_V3_MIGRATION_PROFILE_ROLLED_BACK",
            "migration target is rolled back to the compat-v2 profile",
        )


def apply_sqlite_v3_migration(
    bundle: SnapshotV3MigrationBundle,
    *,
    source: str | Path,
    source_kind: SQLiteV3MigrationSourceKind,
    target_database: str | Path,
    backup: str | Path,
    commit_relation_verifier: CommitRelationVerifier | None = None,
    clock: Callable[[], datetime] | None = None,
) -> SQLiteV3MigrationInspection:
    """Create one restart-safe side-by-side durable SQLite target."""

    if type(bundle) is not SnapshotV3MigrationBundle:
        raise TypeError("bundle must be exactly SnapshotV3MigrationBundle")
    if type(source_kind) is not str or source_kind not in _SOURCE_KINDS:
        raise ValueError("source_kind must be snapshot or sqlite")
    verify_snapshot_v3_migration_bundle(
        bundle,
        commit_relation_verifier=commit_relation_verifier,
    )
    if not bundle.ready:
        _failed(
            "TBM_SQLITE_V3_MIGRATION_BUNDLE_BLOCKED",
            "blocked migration bundle cannot be applied",
        )
    source_path = _canonical_source_path(source)
    target = _canonical_output_path(target_database, "migration target")
    backup_path = _canonical_output_path(backup, "migration backup")
    _validate_distinct_paths(source_path, target, backup_path)
    store = _load_source(source_path, source_kind)
    _verify_source_matches_bundle(store, bundle)
    lock_path = target.with_name(
        f"{target.name}{SQLITE_V3_APPLY_MIGRATION_LOCK_SUFFIX}"
    )
    with exclusive_file_lock(lock_path):
        if _path_lexists(target):
            inspection = verify_sqlite_v3_migration(
                target,
                commit_relation_verifier=commit_relation_verifier,
            )
            if (
                inspection.bundle_id != bundle.bundle_id
                or inspection.source_kind != source_kind
                or os.path.normcase(inspection.backup_path)
                != os.path.normcase(os.fspath(backup_path))
            ):
                _failed(
                    "TBM_SQLITE_V3_MIGRATION_TARGET_CONFLICT",
                    "existing migration target has different immutable state",
                )
            if inspection.profile != "durable-v3":
                _failed(
                    "TBM_SQLITE_V3_MIGRATION_ALREADY_ROLLED_BACK",
                    "rolled-back migration cannot be silently reapplied",
                )
            return inspection
        backup_sha256 = _ensure_backup(
            source_path,
            backup_path,
            source_kind,
            bundle.normalized_source_snapshot_sha256,
        )
        _build_target(
            store=store,
            bundle=bundle,
            target=target,
            backup_path=backup_path,
            backup_sha256=backup_sha256,
            source_kind=source_kind,
            commit_relation_verifier=commit_relation_verifier,
            clock=clock,
        )
        return verify_sqlite_v3_migration(
            target,
            commit_relation_verifier=commit_relation_verifier,
        )


def _append_rollback_event(
    connection: sqlite3.Connection,
    *,
    application_id: str,
    compatibility_path: Path,
    compatibility_sha256: str,
    occurred_at: str,
) -> None:
    try:
        connection.execute(
            "INSERT INTO v3_migration_profile_events ("
            "application_id, sequence, profile, compatibility_path, "
            "compatibility_sha256, occurred_at, reason_code"
            ") VALUES (?, 2, 'compat-v2', ?, ?, ?, ?)",
            (
                application_id,
                os.fspath(compatibility_path),
                compatibility_sha256,
                occurred_at,
                "TBM_V3_MIGRATION_ROLLED_BACK",
            ),
        )
    except sqlite3.Error as error:
        raise SQLiteV3ApplyMigrationError(
            "TBM_SQLITE_V3_MIGRATION_ROLLBACK_STATE_FAILED",
            "migration rollback event could not be committed",
        ) from error


def rollback_sqlite_v3_migration(
    target_database: str | Path,
    *,
    compatibility_database: str | Path,
    commit_relation_verifier: CommitRelationVerifier | None = None,
    clock: Callable[[], datetime] | None = None,
) -> SQLiteV3MigrationInspection:
    """Materialize compat-v2 data and append an immutable rollback event."""

    target = _validate_regular_file(
        Path(target_database).expanduser(),
        "migration target",
    )
    compatibility = _canonical_output_path(
        compatibility_database,
        "compatibility database",
    )
    _validate_distinct_paths(target, compatibility)
    lock_path = target.with_name(
        f"{target.name}{SQLITE_V3_APPLY_MIGRATION_LOCK_SUFFIX}"
    )
    with exclusive_file_lock(lock_path):
        connection = _open_sqlite(target, read_only=False)
        try:
            connection.execute("PRAGMA recursive_triggers = ON")
            connection.execute("BEGIN IMMEDIATE")
            inspection = _inspect_connection(
                connection,
                commit_relation_verifier=commit_relation_verifier,
            )
            if inspection.profile == "compat-v2":
                if (
                    os.path.normcase(inspection.compatibility_path or "")
                    != os.path.normcase(os.fspath(compatibility))
                ):
                    _failed(
                        "TBM_SQLITE_V3_MIGRATION_ROLLBACK_CONFLICT",
                        "migration is already rolled back to a different path",
                    )
                connection.rollback()
                return inspection
            if os.path.normcase(inspection.backup_path) == os.path.normcase(
                os.fspath(compatibility)
            ):
                _failed(
                    "TBM_SQLITE_V3_MIGRATION_PATH_CONFLICT",
                    "compatibility database must differ from the source backup",
                )
            if _path_lexists(compatibility):
                compatibility_path = _validate_regular_file(
                    compatibility,
                    "compatibility database",
                )
                compatibility_store = _load_sqlite_source(
                    compatibility_path
                )
                if (
                    _snapshot_digest(compatibility_store)
                    != inspection.normalized_source_snapshot_sha256
                ):
                    _failed(
                        "TBM_SQLITE_V3_MIGRATION_ROLLBACK_CONFLICT",
                        "existing compatibility database has different state",
                    )
            else:
                application = _application_row(connection)
                bundle = SQLiteV3MigrationRepository(
                    connection
                ).load(str(application[1]))
                store = TraceBackedMemoryStore.from_snapshot(
                    bundle.source_snapshot
                )
                _create_compatibility_database(store, compatibility)
            compatibility_path = _validate_regular_file(
                compatibility,
                "compatibility database",
            )
            compatibility_sha256 = _sha256_file(compatibility_path)
            _append_rollback_event(
                connection,
                application_id=inspection.application_id,
                compatibility_path=compatibility,
                compatibility_sha256=compatibility_sha256,
                occurred_at=_timestamp(clock),
            )
            completed = _inspect_connection(
                connection,
                commit_relation_verifier=commit_relation_verifier,
            )
            if completed.profile != "compat-v2":
                _failed(
                    "TBM_SQLITE_V3_MIGRATION_ROLLBACK_STATE_FAILED",
                    "migration rollback profile did not commit",
                )
            connection.commit()
        except BaseException:
            if connection.in_transaction:
                connection.rollback()
            raise
        finally:
            connection.close()
        return verify_sqlite_v3_migration(
            target,
            commit_relation_verifier=commit_relation_verifier,
        )


__all__ = [
    "SQLITE_V3_APPLY_MIGRATION_CONTRACT_VERSION",
    "LegacyRecordDisposition",
    "SQLiteV3ApplyMigrationError",
    "SQLiteV3MigrationInspection",
    "SQLiteV3MigrationProfile",
    "SQLiteV3MigrationSourceKind",
    "apply_sqlite_v3_migration",
    "require_durable_sqlite_migration_profile",
    "rollback_sqlite_v3_migration",
    "verify_sqlite_v3_migration",
]
