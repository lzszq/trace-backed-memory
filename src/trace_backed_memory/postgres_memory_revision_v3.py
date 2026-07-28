from __future__ import annotations

from collections.abc import Callable, Mapping
from contextlib import contextmanager
from dataclasses import dataclass
from functools import wraps
import re
from threading import RLock
from typing import Iterator, NoReturn, ParamSpec, TypeVar, cast

from .evidence_v3 import (
    StructuredRegressionEvidence,
    dumps_structured_regression_evidence,
    loads_structured_regression_evidence,
)
from .fix_evidence_v3 import FixEvidence, dumps_fix_evidence, loads_fix_evidence
from .memory_revision_v3 import (
    MemoryRevision,
    dumps_memory_revision,
    loads_memory_revision,
    verify_memory_revision_evidence_bundle,
)
from .postgres import _load_psycopg
from .postgres_authorization_v3 import _CATALOG_SHA256_QUERY


POSTGRES_MEMORY_REVISION_V3_SCHEMA_VERSION = 1
POSTGRES_MEMORY_REVISION_V3_CONTRACT_VERSION = "tbm.memory-revision.v3"
_SCHEMA = "trace_backed_memory_v3_memory_revision"
_MISSING_SCHEMA_MESSAGE = (
    "PostgreSQL memory revision v3 schema is missing or incomplete"
)
_EXPECTED_CATALOG_SHA256 = (
    "3cb8d46c1a89e2c504096f42d282926c02e828c5d9a1355f694767da92d80a41"
)
_MEMORY_REVISION_CATALOG_SHA256_QUERY = _CATALOG_SHA256_QUERY.replace(
    "trace_backed_memory_v3_authorization.",
    "trace_backed_memory_v3_memory_revision.",
).replace(" || '|' ||\n           attribute.attcompression::text", "")
_UNDEFINED_OBJECT_SQLSTATES = frozenset({"3F000", "42P01", "42703"})
_REVISION_ID_RE = re.compile(r"memory_revision_sha256_[0-9a-f]{64}")
_MAX_LINEAGE_DEPTH = 10_000
_P = ParamSpec("_P")
_R = TypeVar("_R")


class PostgresMemoryRevisionV3Error(RuntimeError):
    pass


class PostgresMemoryRevisionV3SchemaError(PostgresMemoryRevisionV3Error):
    pass


class PostgresMemoryRevisionV3ConflictError(PostgresMemoryRevisionV3Error):
    pass


class PostgresMemoryRevisionV3NotFoundError(PostgresMemoryRevisionV3Error):
    pass


class PostgresMemoryRevisionV3PersistenceError(PostgresMemoryRevisionV3Error):
    pass


@dataclass(frozen=True)
class PostgresMemoryRevisionV3StoreResult:
    revision_id: str
    revision_inserted: bool
    fix_evidence_inserted: bool
    regression_evidence_inserted: int


@dataclass(frozen=True)
class StoredPostgresMemoryRevisionProposal:
    revision: MemoryRevision
    fix_evidence: FixEvidence | None
    regression_evidence: tuple[StructuredRegressionEvidence, ...]


def _synchronized(method: Callable[_P, _R]) -> Callable[_P, _R]:
    @wraps(method)
    def wrapped(*args: _P.args, **kwargs: _P.kwargs) -> _R:
        repository = args[0]
        with repository._lock:
            return method(*args, **kwargs)

    return wrapped


class PostgresMemoryRevisionV3Repository:
    """Immutable proposal-only PostgreSQL MemoryRevision v3 ledger."""

    def __init__(self, connection: object, *, owns_connection: bool = False):
        if connection is None:
            raise ValueError("connection is required")
        self._connection = connection
        self._owns_connection = owns_connection
        self._lock = RLock()
        self._closed = False

    @classmethod
    def connect(
        cls,
        conninfo: str = "",
        **kwargs: object,
    ) -> PostgresMemoryRevisionV3Repository:
        try:
            psycopg, dict_row, _Jsonb = _load_psycopg()
            connection = psycopg.connect(
                conninfo,
                row_factory=dict_row,
                **kwargs,
            )
        except Exception as error:
            raise PostgresMemoryRevisionV3PersistenceError(
                "failed to connect to PostgreSQL memory revision v3 storage"
            ) from error
        return cls(connection, owns_connection=True)

    def _require_open(self) -> None:
        if self._closed or getattr(self._connection, "closed", False):
            raise PostgresMemoryRevisionV3Error(
                "PostgreSQL memory revision v3 repository is closed"
            )

    def _cursor(self) -> object:
        _psycopg, dict_row, _Jsonb = _load_psycopg()
        return self._connection.cursor(row_factory=dict_row)

    def _lock_schema(self, cursor: object, *, for_write: bool) -> str:
        cursor.execute(
            "SELECT pg_catalog.current_setting('search_path') "
            "AS search_path"
        )
        search_path_rows = cursor.fetchall()
        if (
            len(search_path_rows) != 1
            or type(search_path_rows[0].get("search_path")) is not str
        ):
            raise PostgresMemoryRevisionV3SchemaError(
                "PostgreSQL search_path has invalid shape"
            )
        original_search_path = cast(str, search_path_rows[0]["search_path"])
        cursor.execute(
            "SELECT pg_catalog.set_config("
            "'search_path', 'pg_catalog', true)"
        )
        cursor.execute(
            "SELECT schema_version AS active_version "
            "FROM public.trace_backed_memory_schema "
            "WHERE singleton FOR SHARE"
        )
        if cursor.fetchall() != [{"active_version": 2}]:
            raise PostgresMemoryRevisionV3SchemaError(
                "PostgreSQL active schema metadata mismatch"
            )
        cursor.execute(
            "SELECT schema_version AS revision_version, "
            "contract_version FROM "
            "trace_backed_memory_v3_memory_revision.schema_metadata "
            "WHERE singleton = 1 FOR SHARE"
        )
        if cursor.fetchall() != [{
            "revision_version": POSTGRES_MEMORY_REVISION_V3_SCHEMA_VERSION,
            "contract_version": POSTGRES_MEMORY_REVISION_V3_CONTRACT_VERSION,
        }]:
            raise PostgresMemoryRevisionV3SchemaError(
                "PostgreSQL memory revision v3 metadata mismatch"
            )
        cursor.execute(
            "LOCK TABLE "
            "trace_backed_memory_v3_memory_revision.schema_metadata, "
            "trace_backed_memory_v3_memory_revision.v3_fix_evidence, "
            "trace_backed_memory_v3_memory_revision.v3_regression_evidence, "
            "trace_backed_memory_v3_memory_revision."
            "v3_memory_revision_proposals, "
            "trace_backed_memory_v3_memory_revision."
            "v3_memory_revision_regression_evidence "
            f"IN {'ROW EXCLUSIVE' if for_write else 'ACCESS SHARE'} MODE"
        )
        self._verify_schema_catalog(cursor)
        return original_search_path

    def _verify_schema_catalog(self, cursor: object) -> None:
        cursor.execute(
            "SELECT "
            "(SELECT count(*) FROM pg_catalog.pg_policy AS policy "
            "JOIN pg_catalog.pg_class AS class "
            "ON class.oid = policy.polrelid "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = class.relnamespace "
            "WHERE namespace.nspname = %s) AS policy_count, "
            "(SELECT count(*) FROM pg_catalog.pg_rewrite AS rule "
            "JOIN pg_catalog.pg_class AS class "
            "ON class.oid = rule.ev_class "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = class.relnamespace "
            "WHERE namespace.nspname = %s "
            "AND rule.rulename <> '_RETURN') AS rule_count, "
            "(SELECT count(*) FROM pg_catalog.pg_class AS class "
            "JOIN pg_catalog.pg_namespace AS namespace "
            "ON namespace.oid = class.relnamespace "
            "WHERE namespace.nspname = %s "
            "AND class.relkind NOT IN ('r', 'i', 'p')) "
            "AS unsupported_relation_count",
            (_SCHEMA, _SCHEMA, _SCHEMA),
        )
        if cursor.fetchall() != [
            {
                "policy_count": 0,
                "rule_count": 0,
                "unsupported_relation_count": 0,
            }
        ]:
            raise PostgresMemoryRevisionV3SchemaError(
                "PostgreSQL memory revision v3 contains unsupported "
                "policies, rules, or relation kinds"
            )
        cursor.execute(
            _MEMORY_REVISION_CATALOG_SHA256_QUERY,
            (_SCHEMA,) * 7,
        )
        rows = cursor.fetchall()
        if (
            len(rows) != 1
            or rows[0].get("catalog_sha256") != _EXPECTED_CATALOG_SHA256
        ):
            raise PostgresMemoryRevisionV3SchemaError(
                "PostgreSQL memory revision v3 catalog does not match"
            )

    @contextmanager
    def _secured_cursor(self, *, for_write: bool) -> Iterator[object]:
        with self._cursor() as cursor:
            original_search_path = self._lock_schema(
                cursor,
                for_write=for_write,
            )
            try:
                yield cursor
            except Exception:
                raise
            else:
                self._verify_schema_catalog(cursor)
                cursor.execute(
                    "SELECT pg_catalog.set_config("
                    "'search_path', %s, true)",
                    (original_search_path,),
                )

    @staticmethod
    def _fix_values(evidence: FixEvidence) -> tuple[object, ...]:
        return (
            evidence.evidence_id,
            evidence.case_id,
            evidence.source_trace_id,
            evidence.source_commit_sha,
            evidence.fix_commit_sha,
            dumps_fix_evidence(evidence),
        )

    @staticmethod
    def _regression_values(
        evidence: StructuredRegressionEvidence,
    ) -> tuple[object, ...]:
        return (
            evidence.evidence_id,
            evidence.case_id,
            evidence.source_trace_id,
            evidence.source_commit_sha,
            evidence.fix_commit_sha,
            dumps_structured_regression_evidence(evidence),
        )

    @staticmethod
    def _revision_values(revision: MemoryRevision) -> tuple[object, ...]:
        return (
            revision.revision_id,
            revision.memory_id,
            revision.revision_number,
            revision.previous_revision_id,
            revision.fix_evidence_id,
            dumps_memory_revision(revision),
        )

    @staticmethod
    def _persistence(message: str) -> NoReturn:
        raise PostgresMemoryRevisionV3PersistenceError(message)

    @staticmethod
    def _row_values(
        row: Mapping[str, object],
        columns: tuple[str, ...],
    ) -> tuple[object, ...]:
        return tuple(row.get(column) for column in columns)

    def _put_exact(
        self,
        cursor: object,
        *,
        table: str,
        id_column: str,
        columns: tuple[str, ...],
        values: tuple[object, ...],
        conflict_message: str,
    ) -> bool:
        column_list = ", ".join(columns)
        placeholders = ", ".join("%s" for _ in values)
        cursor.execute(
            f"INSERT INTO {_SCHEMA}.{table} ({column_list}) "
            f"VALUES ({placeholders}) ON CONFLICT DO NOTHING "
            f"RETURNING {id_column}",
            values,
        )
        inserted = cursor.fetchone() is not None
        cursor.execute(
            f"SELECT {column_list} FROM {_SCHEMA}.{table} "
            f"WHERE {id_column} = %s FOR SHARE",
            (values[0],),
        )
        rows = cursor.fetchall()
        if not rows:
            raise PostgresMemoryRevisionV3ConflictError(conflict_message)
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            self._persistence("immutable proposal lookup is ambiguous")
        if self._row_values(rows[0], columns) != values:
            raise PostgresMemoryRevisionV3ConflictError(conflict_message)
        return inserted

    def _load_fix(self, cursor: object, evidence_id: str) -> FixEvidence:
        columns = (
            "evidence_id",
            "case_id",
            "source_trace_id",
            "source_commit_sha",
            "fix_commit_sha",
            "descriptor",
        )
        cursor.execute(
            f"SELECT {', '.join(columns)} FROM {_SCHEMA}.v3_fix_evidence "
            "WHERE evidence_id = %s FOR SHARE",
            (evidence_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            self._persistence("stored fix evidence reference is missing")
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            self._persistence("stored fix evidence lookup is ambiguous")
        values = self._row_values(rows[0], columns)
        if type(values[5]) is not str:
            self._persistence("stored fix evidence row has invalid shape")
        try:
            evidence = loads_fix_evidence(cast(str, values[5]))
        except ValueError as error:
            raise PostgresMemoryRevisionV3PersistenceError(
                "stored fix evidence failed validation"
            ) from error
        if values != self._fix_values(evidence):
            self._persistence(
                "stored fix evidence columns do not match descriptor"
            )
        return evidence

    def _load_regression(
        self,
        cursor: object,
        evidence_id: str,
    ) -> StructuredRegressionEvidence:
        columns = (
            "evidence_id",
            "case_id",
            "source_trace_id",
            "source_commit_sha",
            "fix_commit_sha",
            "descriptor",
        )
        cursor.execute(
            f"SELECT {', '.join(columns)} FROM {_SCHEMA}."
            "v3_regression_evidence WHERE evidence_id = %s FOR SHARE",
            (evidence_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            self._persistence(
                "stored regression evidence reference is missing"
            )
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            self._persistence(
                "stored regression evidence lookup is ambiguous"
            )
        values = self._row_values(rows[0], columns)
        if type(values[5]) is not str:
            self._persistence("stored regression evidence row has invalid shape")
        try:
            evidence = loads_structured_regression_evidence(
                cast(str, values[5])
            )
        except ValueError as error:
            raise PostgresMemoryRevisionV3PersistenceError(
                "stored regression evidence failed validation"
            ) from error
        if values != self._regression_values(evidence):
            self._persistence(
                "stored regression columns do not match descriptor"
            )
        return evidence

    def _load_bundle(
        self,
        cursor: object,
        revision_id: str,
        *,
        missing_is_not_found: bool,
    ) -> StoredPostgresMemoryRevisionProposal:
        columns = (
            "revision_id",
            "memory_id",
            "revision_number",
            "previous_revision_id",
            "fix_evidence_id",
            "descriptor",
        )
        cursor.execute(
            f"SELECT {', '.join(columns)} FROM {_SCHEMA}."
            "v3_memory_revision_proposals WHERE revision_id = %s FOR SHARE",
            (revision_id,),
        )
        rows = cursor.fetchall()
        if not rows:
            if missing_is_not_found:
                raise PostgresMemoryRevisionV3NotFoundError(
                    "memory revision proposal was not found"
                )
            self._persistence("stored memory revision proposal is missing")
        if len(rows) != 1 or not isinstance(rows[0], Mapping):
            self._persistence("stored proposal lookup is ambiguous")
        values = self._row_values(rows[0], columns)
        if type(values[5]) is not str:
            self._persistence("stored revision row has invalid shape")
        try:
            revision = loads_memory_revision(cast(str, values[5]))
        except ValueError as error:
            raise PostgresMemoryRevisionV3PersistenceError(
                "stored revision descriptor failed validation"
            ) from error
        if values != self._revision_values(revision):
            self._persistence(
                "stored revision columns do not match descriptor"
            )
        self._verify_parent_lineage(cursor, revision)
        fix = (
            None
            if revision.fix_evidence_id is None
            else self._load_fix(cursor, revision.fix_evidence_id)
        )
        cursor.execute(
            f"SELECT ordinal, evidence_id FROM {_SCHEMA}."
            "v3_memory_revision_regression_evidence "
            "WHERE revision_id = %s ORDER BY ordinal FOR SHARE",
            (revision_id,),
        )
        link_rows = cursor.fetchall()
        if any(
            not isinstance(row, Mapping)
            or type(row.get("ordinal")) is not int
            or type(row.get("evidence_id")) is not str
            for row in link_rows
        ):
            self._persistence(
                "stored regression evidence links have invalid shape"
            )
        ordinals = tuple(row["ordinal"] for row in link_rows)
        if ordinals != tuple(range(len(link_rows))):
            self._persistence(
                "stored regression evidence link ordinals are not contiguous"
            )
        evidence_ids = tuple(
            cast(str, row["evidence_id"]) for row in link_rows
        )
        if evidence_ids != revision.regression_evidence_ids:
            self._persistence(
                "stored regression evidence links do not match revision"
            )
        regression = tuple(
            self._load_regression(cursor, evidence_id)
            for evidence_id in evidence_ids
        )
        try:
            verify_memory_revision_evidence_bundle(
                revision,
                {} if fix is None else {fix.evidence_id: fix},
                {evidence.evidence_id: evidence for evidence in regression},
            )
        except ValueError as error:
            raise PostgresMemoryRevisionV3PersistenceError(
                "stored proposal bundle failed validation"
            ) from error
        return StoredPostgresMemoryRevisionProposal(revision, fix, regression)

    def _verify_parent_lineage(
        self,
        cursor: object,
        revision: MemoryRevision,
    ) -> None:
        if revision.revision_number > _MAX_LINEAGE_DEPTH:
            self._persistence(
                "stored revision lineage exceeds the verification bound"
            )
        child = revision
        seen = {child.revision_id}
        while child.previous_revision_id is not None:
            cursor.execute(
                f"SELECT memory_id, revision_number, descriptor "
                f"FROM {_SCHEMA}.v3_memory_revision_proposals "
                "WHERE revision_id = %s FOR SHARE",
                (child.previous_revision_id,),
            )
            parent_rows = cursor.fetchall()
            if (
                len(parent_rows) != 1
                or not isinstance(parent_rows[0], Mapping)
                or type(parent_rows[0].get("descriptor")) is not str
            ):
                self._persistence(
                    "stored revision parent is missing or ambiguous"
                )
            try:
                parent = loads_memory_revision(
                    cast(str, parent_rows[0]["descriptor"])
                )
            except ValueError as error:
                raise PostgresMemoryRevisionV3PersistenceError(
                    "stored revision parent descriptor failed validation"
                ) from error
            if (
                parent_rows[0]
                != {
                    "memory_id": parent.memory_id,
                    "revision_number": parent.revision_number,
                    "descriptor": dumps_memory_revision(parent),
                }
                or parent.revision_id != child.previous_revision_id
                or parent.revision_id in seen
                or parent.memory_id != child.memory_id
                or parent.revision_number != child.revision_number - 1
            ):
                self._persistence(
                    "stored revision parent continuity does not match"
                )
            seen.add(parent.revision_id)
            child = parent

    @_synchronized
    def store_proposal(
        self,
        revision: MemoryRevision,
        fix_evidence: FixEvidence | None,
        regression_evidence: tuple[StructuredRegressionEvidence, ...],
    ) -> PostgresMemoryRevisionV3StoreResult:
        if type(revision) is not MemoryRevision:
            raise ValueError("revision must be exactly MemoryRevision")
        if revision.revision_number > _MAX_LINEAGE_DEPTH:
            raise ValueError(
                "revision_number exceeds the PostgreSQL lineage bound"
            )
        if fix_evidence is not None and type(fix_evidence) is not FixEvidence:
            raise ValueError("fix_evidence must be exactly FixEvidence or None")
        if type(regression_evidence) is not tuple or any(
            type(item) is not StructuredRegressionEvidence
            for item in regression_evidence
        ):
            raise ValueError("regression_evidence must be an exact tuple")
        regression_by_id = {
            evidence.evidence_id: evidence for evidence in regression_evidence
        }
        if len(regression_by_id) != len(regression_evidence):
            raise ValueError("regression_evidence must not contain duplicates")
        if tuple(sorted(regression_by_id)) != revision.regression_evidence_ids:
            raise ValueError("regression_evidence must exactly match revision")
        ordered_regression = tuple(
            regression_by_id[evidence_id]
            for evidence_id in revision.regression_evidence_ids
        )
        verify_memory_revision_evidence_bundle(
            revision,
            (
                {}
                if fix_evidence is None
                else {fix_evidence.evidence_id: fix_evidence}
            ),
            regression_by_id,
        )
        self._require_open()
        try:
            with self._connection.transaction():
                with self._secured_cursor(for_write=True) as cursor:
                    cursor.execute(
                        f"SELECT revision_id FROM {_SCHEMA}."
                        "v3_memory_revision_proposals "
                        "WHERE revision_id = %s FOR SHARE",
                        (revision.revision_id,),
                    )
                    existing = cursor.fetchall()
                    expected = StoredPostgresMemoryRevisionProposal(
                        revision,
                        fix_evidence,
                        ordered_regression,
                    )
                    if existing:
                        if existing != [{"revision_id": revision.revision_id}]:
                            self._persistence(
                                "stored proposal identity has invalid shape"
                            )
                        if self._load_bundle(
                            cursor,
                            revision.revision_id,
                            missing_is_not_found=False,
                        ) != expected:
                            raise PostgresMemoryRevisionV3ConflictError(
                                "stored proposal bundle does not match input"
                            )
                        return PostgresMemoryRevisionV3StoreResult(
                            revision.revision_id,
                            False,
                            False,
                            0,
                        )
                    fix_inserted = (
                        False
                        if fix_evidence is None
                        else self._put_exact(
                            cursor,
                            table="v3_fix_evidence",
                            id_column="evidence_id",
                            columns=(
                                "evidence_id",
                                "case_id",
                                "source_trace_id",
                                "source_commit_sha",
                                "fix_commit_sha",
                                "descriptor",
                            ),
                            values=self._fix_values(fix_evidence),
                            conflict_message=(
                                "fix evidence ID has conflicting content"
                            ),
                        )
                    )
                    regression_inserted = sum(
                        self._put_exact(
                            cursor,
                            table="v3_regression_evidence",
                            id_column="evidence_id",
                            columns=(
                                "evidence_id",
                                "case_id",
                                "source_trace_id",
                                "source_commit_sha",
                                "fix_commit_sha",
                                "descriptor",
                            ),
                            values=self._regression_values(evidence),
                            conflict_message=(
                                "regression evidence ID has conflicting content"
                            ),
                        )
                        for evidence in ordered_regression
                    )
                    revision_inserted = self._put_exact(
                        cursor,
                        table="v3_memory_revision_proposals",
                        id_column="revision_id",
                        columns=(
                            "revision_id",
                            "memory_id",
                            "revision_number",
                            "previous_revision_id",
                            "fix_evidence_id",
                            "descriptor",
                        ),
                        values=self._revision_values(revision),
                        conflict_message=(
                            "memory revision ID has conflicting content"
                        ),
                    )
                    for ordinal, evidence_id in enumerate(
                        revision.regression_evidence_ids
                    ):
                        cursor.execute(
                            f"INSERT INTO {_SCHEMA}."
                            "v3_memory_revision_regression_evidence "
                            "(revision_id, evidence_id, ordinal) "
                            "VALUES (%s, %s, %s) ON CONFLICT DO NOTHING",
                            (revision.revision_id, evidence_id, ordinal),
                        )
                    if self._load_bundle(
                        cursor,
                        revision.revision_id,
                        missing_is_not_found=False,
                    ) != expected:
                        raise PostgresMemoryRevisionV3ConflictError(
                            "stored proposal bundle does not match input"
                        )
            return PostgresMemoryRevisionV3StoreResult(
                revision.revision_id,
                revision_inserted,
                fix_inserted,
                regression_inserted,
            )
        except (
            PostgresMemoryRevisionV3Error,
            ValueError,
        ):
            raise
        except Exception as error:
            self._raise_database(error)

    @_synchronized
    def load_proposal(
        self,
        revision_id: str,
    ) -> StoredPostgresMemoryRevisionProposal:
        self._require_open()
        if (
            type(revision_id) is not str
            or _REVISION_ID_RE.fullmatch(revision_id) is None
        ):
            raise ValueError("revision_id must be a v3 memory revision ID")
        try:
            with self._connection.transaction():
                with self._secured_cursor(for_write=False) as cursor:
                    return self._load_bundle(
                        cursor,
                        revision_id,
                        missing_is_not_found=True,
                    )
        except PostgresMemoryRevisionV3Error:
            raise
        except Exception as error:
            self._raise_database(error)

    def _raise_database(self, error: Exception) -> NoReturn:
        sqlstate = getattr(error, "sqlstate", None)
        if sqlstate in _UNDEFINED_OBJECT_SQLSTATES:
            raise PostgresMemoryRevisionV3SchemaError(
                _MISSING_SCHEMA_MESSAGE
            ) from error
        if (
            type(sqlstate) is str
            and (sqlstate.startswith("23") or sqlstate == "P0001")
        ):
            raise PostgresMemoryRevisionV3ConflictError(
                "proposal conflicts with immutable PostgreSQL storage"
            ) from error
        raise PostgresMemoryRevisionV3PersistenceError(
            "PostgreSQL memory revision v3 operation failed"
        ) from error

    @_synchronized
    def close(self) -> None:
        if self._closed:
            return
        self._closed = True
        if self._owns_connection:
            self._connection.close()

    def __enter__(self) -> PostgresMemoryRevisionV3Repository:
        self._require_open()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.close()


__all__ = [
    "POSTGRES_MEMORY_REVISION_V3_CONTRACT_VERSION",
    "POSTGRES_MEMORY_REVISION_V3_SCHEMA_VERSION",
    "PostgresMemoryRevisionV3ConflictError",
    "PostgresMemoryRevisionV3Error",
    "PostgresMemoryRevisionV3NotFoundError",
    "PostgresMemoryRevisionV3PersistenceError",
    "PostgresMemoryRevisionV3Repository",
    "PostgresMemoryRevisionV3SchemaError",
    "PostgresMemoryRevisionV3StoreResult",
    "StoredPostgresMemoryRevisionProposal",
]
