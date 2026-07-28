from __future__ import annotations

from contextlib import contextmanager
from pathlib import Path

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.postgres_memory_revision_v3 as pg_revision
from tests.postgres_support import PostgresCluster
from trace_backed_memory.postgres import _load_psycopg
from trace_backed_memory.postgres_memory_revision_v3 import (
    PostgresMemoryRevisionV3ConflictError,
    PostgresMemoryRevisionV3NotFoundError,
    PostgresMemoryRevisionV3Repository,
    PostgresMemoryRevisionV3SchemaError,
    _MEMORY_REVISION_CATALOG_SHA256_QUERY,
)


ROOT = Path(__file__).resolve().parents[1]
INSTALL = ROOT / "schemas" / "postgres-v3-memory-revision.sql"
ROLLBACK = ROOT / "schemas" / "postgres-v3-memory-revision-rollback.sql"
SCHEMA = "trace_backed_memory_v3_memory_revision"


class _FakeCursor:
    def __init__(
        self,
        *,
        fetchalls: list[list[object]] | None = None,
        fetchones: list[object] | None = None,
    ) -> None:
        self._fetchalls = [] if fetchalls is None else list(fetchalls)
        self._fetchones = [] if fetchones is None else list(fetchones)

    def execute(self, *_args: object, **_kwargs: object) -> None:
        return None

    def fetchall(self) -> list[object]:
        return self._fetchalls.pop(0)

    def fetchone(self) -> object:
        return None if not self._fetchones else self._fetchones.pop(0)


def _install(postgres_cluster: PostgresCluster) -> None:
    postgres_cluster.load_schema()
    result = postgres_cluster.run_script(INSTALL)
    assert result.returncode == 0, result.stderr


def _records() -> tuple[
    tbm.MemoryRevision,
    tbm.FixEvidence,
    tbm.StructuredRegressionEvidence,
]:
    fix = tbm.loads_fix_evidence(
        (ROOT / "examples" / "fix_evidence_v3.example.json").read_bytes()
    )
    regression = tbm.loads_structured_regression_evidence(
        (
            ROOT
            / "examples"
            / "structured_regression_evidence_v3.example.json"
        ).read_bytes()
    )
    source = tbm.loads_memory_revision(
        (ROOT / "examples" / "memory_revision_v3.example.json").read_bytes()
    )
    revision = tbm.build_memory_revision(
        **{
            **{
                key: value
                for key, value in source.__dict__.items()
                if key
                not in {
                    "revision_id",
                    "contract_version",
                    "fix_evidence_id",
                    "regression_evidence_ids",
                }
            },
            "fix_evidence_id": fix.evidence_id,
            "regression_evidence_ids": (regression.evidence_id,),
        }
    )
    return revision, fix, regression


def _repository(
    postgres_cluster: PostgresCluster,
) -> PostgresMemoryRevisionV3Repository:
    return PostgresMemoryRevisionV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    )


def test_postgres_memory_revision_catalog_fingerprint(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    psycopg, dict_row, _Jsonb = _load_psycopg()
    with psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                _MEMORY_REVISION_CATALOG_SHA256_QUERY,
                (SCHEMA,) * 7,
            )
            fingerprint = cursor.fetchone()["catalog_sha256"]
            cursor.execute(
                "SELECT class.relname FROM pg_catalog.pg_class AS class "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = class.relnamespace "
                "WHERE namespace.nspname = %s "
                "AND class.relkind IN ('r', 'i', 'p') "
                "ORDER BY class.relname",
                (SCHEMA,),
            )
            relations = tuple(row["relname"] for row in cursor.fetchall())
            cursor.execute(
                "SELECT procedure.proname FROM pg_catalog.pg_proc AS procedure "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = procedure.pronamespace "
                "WHERE namespace.nspname = %s ORDER BY procedure.proname",
                (SCHEMA,),
            )
            functions = tuple(row["proname"] for row in cursor.fetchall())
            cursor.execute(
                "SELECT trigger.tgname FROM pg_catalog.pg_trigger AS trigger "
                "JOIN pg_catalog.pg_class AS class "
                "ON class.oid = trigger.tgrelid "
                "JOIN pg_catalog.pg_namespace AS namespace "
                "ON namespace.oid = class.relnamespace "
                "WHERE namespace.nspname = %s AND NOT trigger.tgisinternal "
                "ORDER BY trigger.tgname",
                (SCHEMA,),
            )
            triggers = tuple(row["tgname"] for row in cursor.fetchall())
    assert fingerprint == (
        "3cb8d46c1a89e2c504096f42d282926c02e828c5d9a1355f694767da92d80a41"
    )
    assert relations == (
        "schema_metadata",
        "schema_metadata_pkey",
        "v3_fix_evidence",
        "v3_fix_evidence_pkey",
        "v3_memory_revision_proposals",
        "v3_memory_revision_proposals_memory_id_revision_number_key",
        "v3_memory_revision_proposals_pkey",
        "v3_memory_revision_regression_evidence",
        "v3_memory_revision_regression_evidence_pkey",
        "v3_memory_revision_regression_evidence_revision_id_ordinal_key",
        "v3_regression_evidence",
        "v3_regression_evidence_pkey",
    )
    assert functions == (
        "reject_immutable_change",
        "validate_revision_parent",
    )
    assert triggers == (
        "memory_revision_fix_immutable",
        "memory_revision_fix_no_truncate",
        "memory_revision_link_immutable",
        "memory_revision_link_no_truncate",
        "memory_revision_metadata_immutable",
        "memory_revision_metadata_no_truncate",
        "memory_revision_proposal_immutable",
        "memory_revision_proposal_no_truncate",
        "memory_revision_proposal_parent",
        "memory_revision_regression_immutable",
        "memory_revision_regression_no_truncate",
    )


def test_postgres_memory_revision_exports_and_resources() -> None:
    assert (
        tbm.PostgresMemoryRevisionV3Repository
        is PostgresMemoryRevisionV3Repository
    )
    assert tbm.POSTGRES_MEMORY_REVISION_V3_SCHEMA_VERSION == 1
    assert (
        tbm.POSTGRES_MEMORY_REVISION_V3_CONTRACT_VERSION
        == "tbm.memory-revision.v3"
    )
    assert tbm.read_packaged_resource(
        "schemas/postgres-v3-memory-revision.sql"
    ) == INSTALL.read_bytes()
    assert tbm.read_packaged_resource(
        "schemas/postgres-v3-memory-revision-rollback.sql"
    ) == ROLLBACK.read_bytes()


def test_postgres_memory_revision_rollback_removes_only_isolated_schema(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    result = postgres_cluster.run_script(ROLLBACK)
    assert result.returncode == 0, result.stderr
    psycopg, dict_row, _Jsonb = _load_psycopg()
    with psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    ) as connection:
        with connection.cursor() as cursor:
            cursor.execute(
                "SELECT to_regclass('public.traces') AS active, "
                "to_regclass(%s) AS revision",
                (f"{SCHEMA}.schema_metadata",),
            )
            assert cursor.fetchone() == {
                "active": "traces",
                "revision": None,
            }


def test_postgres_memory_revision_round_trip_and_exact_replay(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    revision, fix, regression = _records()
    with _repository(postgres_cluster) as repository:
        first = repository.store_proposal(revision, fix, (regression,))
        second = repository.store_proposal(revision, fix, (regression,))
        assert first.revision_inserted is True
        assert first.fix_evidence_inserted is True
        assert first.regression_evidence_inserted == 1
        assert second.revision_inserted is False
        assert second.fix_evidence_inserted is False
        assert second.regression_evidence_inserted == 0
        stored = repository.load_proposal(revision.revision_id)
        assert stored.revision == revision
        assert stored.fix_evidence == fix
        assert stored.regression_evidence == (regression,)


def test_postgres_memory_revision_parent_continuity_and_immutability(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    revision, fix, regression = _records()
    with _repository(postgres_cluster) as repository:
        repository.store_proposal(revision, fix, (regression,))
    psycopg, _dict_row, _Jsonb = _load_psycopg()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        for statement in (
            f"UPDATE {SCHEMA}.v3_fix_evidence SET case_id = 'changed'",
            f"DELETE FROM {SCHEMA}.v3_regression_evidence",
            f"TRUNCATE {SCHEMA}.v3_memory_revision_proposals",
            f"DELETE FROM {SCHEMA}.v3_memory_revision_regression_evidence",
        ):
            with pytest.raises(psycopg.Error):
                with connection.transaction():
                    connection.execute(statement)


def test_postgres_memory_revision_missing_and_schema_drift(
    postgres_cluster: PostgresCluster,
) -> None:
    postgres_cluster.load_schema()
    repository = _repository(postgres_cluster)
    try:
        with pytest.raises(PostgresMemoryRevisionV3SchemaError):
            repository.load_proposal("memory_revision_sha256_" + "0" * 64)
    finally:
        repository.close()

    result = postgres_cluster.run_script(INSTALL)
    assert result.returncode == 0, result.stderr
    revision, fix, regression = _records()
    psycopg, _dict_row, _Jsonb = _load_psycopg()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        connection.execute(
            f"ALTER TABLE {SCHEMA}.v3_fix_evidence "
            "DISABLE TRIGGER memory_revision_fix_immutable"
        )
    with _repository(postgres_cluster) as repository:
        with pytest.raises(PostgresMemoryRevisionV3SchemaError):
            repository.store_proposal(revision, fix, (regression,))


def test_postgres_memory_revision_input_and_not_found(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    revision, fix, regression = _records()
    with _repository(postgres_cluster) as repository:
        with pytest.raises(ValueError, match="exactly match"):
            repository.store_proposal(revision, fix, ())
        with pytest.raises(PostgresMemoryRevisionV3NotFoundError):
            repository.load_proposal("memory_revision_sha256_" + "0" * 64)
        repository.close()
        with pytest.raises(Exception, match="closed"):
            repository.load_proposal(revision.revision_id)


def test_postgres_memory_revision_competing_revision_slot_is_atomic(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    revision, fix, regression = _records()
    competing = tbm.build_memory_revision(
        **{
            **{
                key: value
                for key, value in revision.__dict__.items()
                if key not in {"revision_id", "contract_version"}
            },
            "proposed_by": "competing_proposer",
        }
    )
    with _repository(postgres_cluster) as repository:
        repository.store_proposal(revision, fix, (regression,))
        with pytest.raises(PostgresMemoryRevisionV3ConflictError):
            repository.store_proposal(competing, fix, (regression,))
        assert repository.load_proposal(revision.revision_id).revision == revision


def test_postgres_memory_revision_preserves_caller_transaction(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    revision, fix, regression = _records()
    psycopg, dict_row, _Jsonb = _load_psycopg()
    connection = psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    )
    repository = PostgresMemoryRevisionV3Repository(connection)
    try:
        with connection.transaction():
            connection.execute(
                "SET LOCAL search_path = public, pg_catalog"
            )
            original_search_path = connection.execute(
                "SHOW search_path"
            ).fetchone()["search_path"]
            connection.execute("CREATE TEMP TABLE caller_work (value text)")
            connection.execute("INSERT INTO caller_work VALUES ('before')")
            repository.store_proposal(revision, fix, (regression,))
            assert connection.execute(
                "SELECT value FROM caller_work"
            ).fetchall() == [{"value": "before"}]
            assert (
                connection.execute("SHOW search_path").fetchone()["search_path"]
                == original_search_path
            )
        assert repository.load_proposal(revision.revision_id).revision == revision
    finally:
        repository.close()
        connection.close()


def test_postgres_memory_revision_rejects_extra_direct_sql_link(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    revision, fix, regression = _records()
    extra = tbm.build_structured_regression_evidence(
        **{
            **{
                key: value
                for key, value in regression.__dict__.items()
                if key not in {"evidence_id", "contract_version"}
            },
            "verification_trace_id": "trace_verification_extra",
            "verification_run_id": "run_verification_extra",
            "evaluation_case_id": "case_fixed_extra",
            "environment": dict(regression.environment),
        }
    )
    with _repository(postgres_cluster) as repository:
        repository.store_proposal(revision, fix, (regression,))
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        with psycopg.connect(
            **postgres_cluster.connection_kwargs()
        ) as connection:
            connection.execute(
                f"INSERT INTO {SCHEMA}.v3_regression_evidence "
                "(evidence_id, case_id, source_trace_id, source_commit_sha, "
                "fix_commit_sha, descriptor) VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    extra.evidence_id,
                    extra.case_id,
                    extra.source_trace_id,
                    extra.source_commit_sha,
                    extra.fix_commit_sha,
                    tbm.dumps_structured_regression_evidence(extra),
                ),
            )
            connection.execute(
                f"INSERT INTO {SCHEMA}."
                "v3_memory_revision_regression_evidence "
                "(revision_id, evidence_id, ordinal) VALUES (%s, %s, 1)",
                (revision.revision_id, extra.evidence_id),
            )
        with pytest.raises(
            tbm.PostgresMemoryRevisionV3PersistenceError,
            match="do not match revision",
        ):
            repository.load_proposal(revision.revision_id)


def test_postgres_memory_revision_replay_never_repairs_missing_link(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    revision, fix, regression = _records()
    repository = _repository(postgres_cluster)
    try:
        repository.store_proposal(revision, fix, (regression,))
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        with psycopg.connect(
            **postgres_cluster.connection_kwargs()
        ) as connection:
            connection.execute(
                f"ALTER TABLE {SCHEMA}."
                "v3_memory_revision_regression_evidence "
                "DISABLE TRIGGER memory_revision_link_immutable"
            )
            connection.execute(
                f"DELETE FROM {SCHEMA}."
                "v3_memory_revision_regression_evidence "
                "WHERE revision_id = %s",
                (revision.revision_id,),
            )
            connection.execute(
                f"ALTER TABLE {SCHEMA}."
                "v3_memory_revision_regression_evidence "
                "ENABLE TRIGGER memory_revision_link_immutable"
            )
        with pytest.raises(
            tbm.PostgresMemoryRevisionV3PersistenceError,
            match="do not match revision",
        ):
            repository.store_proposal(revision, fix, (regression,))
        psycopg, dict_row, _Jsonb = _load_psycopg()
        with psycopg.connect(
            **postgres_cluster.connection_kwargs(),
            row_factory=dict_row,
        ) as connection:
            assert connection.execute(
                f"SELECT evidence_id FROM {SCHEMA}."
                "v3_memory_revision_regression_evidence"
            ).fetchall() == []
    finally:
        repository.close()


def test_postgres_memory_revision_load_revalidates_parent_continuity(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    parent, fix, regression = _records()
    child = tbm.build_memory_revision(
        **{
            **{
                key: value
                for key, value in parent.__dict__.items()
                if key
                not in {
                    "revision_id",
                    "contract_version",
                    "memory_id",
                    "revision_number",
                    "previous_revision_id",
                }
            },
            "memory_id": "different_memory",
            "revision_number": 2,
            "previous_revision_id": parent.revision_id,
        }
    )
    with _repository(postgres_cluster) as repository:
        repository.store_proposal(parent, fix, (regression,))
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        with psycopg.connect(
            **postgres_cluster.connection_kwargs()
        ) as connection:
            connection.execute(
                f"ALTER TABLE {SCHEMA}.v3_memory_revision_proposals "
                "DISABLE TRIGGER memory_revision_proposal_parent"
            )
            connection.execute(
                f"INSERT INTO {SCHEMA}.v3_memory_revision_proposals "
                "(revision_id, memory_id, revision_number, "
                "previous_revision_id, fix_evidence_id, descriptor) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    child.revision_id,
                    child.memory_id,
                    child.revision_number,
                    child.previous_revision_id,
                    child.fix_evidence_id,
                    tbm.dumps_memory_revision(child),
                ),
            )
            connection.execute(
                f"ALTER TABLE {SCHEMA}.v3_memory_revision_proposals "
                "ENABLE TRIGGER memory_revision_proposal_parent"
            )
        with pytest.raises(
            tbm.PostgresMemoryRevisionV3PersistenceError,
            match="parent continuity",
        ):
            repository.load_proposal(child.revision_id)


def test_postgres_memory_revision_load_revalidates_complete_ancestry(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    first, fix, regression = _records()
    foreign_first = tbm.build_memory_revision(
        **{
            **{
                key: value
                for key, value in first.__dict__.items()
                if key
                not in {
                    "revision_id",
                    "contract_version",
                    "memory_id",
                }
            },
            "memory_id": "foreign_memory",
        }
    )
    second = tbm.build_memory_revision(
        **{
            **{
                key: value
                for key, value in first.__dict__.items()
                if key
                not in {
                    "revision_id",
                    "contract_version",
                    "revision_number",
                    "previous_revision_id",
                }
            },
            "revision_number": 2,
            "previous_revision_id": foreign_first.revision_id,
        }
    )
    third = tbm.build_memory_revision(
        **{
            **{
                key: value
                for key, value in second.__dict__.items()
                if key
                not in {
                    "revision_id",
                    "contract_version",
                    "revision_number",
                    "previous_revision_id",
                }
            },
            "revision_number": 3,
            "previous_revision_id": second.revision_id,
        }
    )
    with _repository(postgres_cluster) as repository:
        repository.store_proposal(first, fix, (regression,))
        repository.store_proposal(foreign_first, fix, (regression,))
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        with psycopg.connect(
            **postgres_cluster.connection_kwargs()
        ) as connection:
            connection.execute(
                f"ALTER TABLE {SCHEMA}.v3_memory_revision_proposals "
                "DISABLE TRIGGER memory_revision_proposal_parent"
            )
            for revision in (second, third):
                connection.execute(
                    f"INSERT INTO {SCHEMA}.v3_memory_revision_proposals "
                    "(revision_id, memory_id, revision_number, "
                    "previous_revision_id, fix_evidence_id, descriptor) "
                    "VALUES (%s, %s, %s, %s, %s, %s)",
                    (
                        revision.revision_id,
                        revision.memory_id,
                        revision.revision_number,
                        revision.previous_revision_id,
                        revision.fix_evidence_id,
                        tbm.dumps_memory_revision(revision),
                    ),
                )
                if revision is second:
                    connection.execute(
                        f"ALTER TABLE {SCHEMA}.v3_memory_revision_proposals "
                        "ENABLE TRIGGER memory_revision_proposal_parent"
                    )
        with pytest.raises(
            tbm.PostgresMemoryRevisionV3PersistenceError,
            match="parent continuity",
        ):
            repository.load_proposal(third.revision_id)


def test_postgres_memory_revision_lineage_bound_fails_closed(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    first, fix, regression = _records()
    oversized = tbm.build_memory_revision(
        **{
            **{
                key: value
                for key, value in first.__dict__.items()
                if key
                not in {
                    "revision_id",
                    "contract_version",
                    "revision_number",
                    "previous_revision_id",
                }
            },
            "revision_number": 10_001,
            "previous_revision_id": first.revision_id,
        }
    )
    with _repository(postgres_cluster) as repository:
        repository.store_proposal(first, fix, (regression,))
        with pytest.raises(ValueError, match="lineage bound"):
            repository.store_proposal(oversized, fix, (regression,))
        psycopg, _dict_row, _Jsonb = _load_psycopg()
        with psycopg.connect(
            **postgres_cluster.connection_kwargs()
        ) as connection:
            connection.execute(
                f"ALTER TABLE {SCHEMA}.v3_memory_revision_proposals "
                "DISABLE TRIGGER memory_revision_proposal_parent"
            )
            connection.execute(
                f"INSERT INTO {SCHEMA}.v3_memory_revision_proposals "
                "(revision_id, memory_id, revision_number, "
                "previous_revision_id, fix_evidence_id, descriptor) "
                "VALUES (%s, %s, %s, %s, %s, %s)",
                (
                    oversized.revision_id,
                    oversized.memory_id,
                    oversized.revision_number,
                    oversized.previous_revision_id,
                    oversized.fix_evidence_id,
                    tbm.dumps_memory_revision(oversized),
                ),
            )
            connection.execute(
                f"ALTER TABLE {SCHEMA}.v3_memory_revision_proposals "
                "ENABLE TRIGGER memory_revision_proposal_parent"
            )
        with pytest.raises(
            tbm.PostgresMemoryRevisionV3PersistenceError,
            match="verification bound",
        ):
            repository.load_proposal(oversized.revision_id)


def test_postgres_memory_revision_schema_lock_blocks_trigger_ddl(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    psycopg, dict_row, _Jsonb = _load_psycopg()
    with (
        psycopg.connect(
            **postgres_cluster.connection_kwargs(),
            row_factory=dict_row,
        ) as locked_connection,
        psycopg.connect(
            **postgres_cluster.connection_kwargs()
        ) as ddl_connection,
    ):
        repository = PostgresMemoryRevisionV3Repository(locked_connection)
        with locked_connection.transaction():
            with repository._cursor() as cursor:
                repository._lock_schema(cursor, for_write=True)
                ddl_connection.execute("SET lock_timeout = '100ms'")
                with pytest.raises(psycopg.errors.LockNotAvailable):
                    ddl_connection.execute(
                        f"ALTER TABLE {SCHEMA}."
                        "v3_memory_revision_proposals "
                        "DISABLE TRIGGER memory_revision_proposal_parent"
                    )


def test_postgres_memory_revision_rechecks_catalog_before_commit(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    psycopg, dict_row, _Jsonb = _load_psycopg()
    with psycopg.connect(
        **postgres_cluster.connection_kwargs(),
        row_factory=dict_row,
    ) as connection:
        repository = PostgresMemoryRevisionV3Repository(connection)
        with pytest.raises(
            PostgresMemoryRevisionV3SchemaError,
            match="catalog does not match",
        ):
            with connection.transaction():
                with repository._secured_cursor(for_write=True) as cursor:
                    cursor.execute(
                        f"CREATE OR REPLACE FUNCTION {SCHEMA}."
                        "validate_revision_parent() RETURNS trigger "
                        "LANGUAGE plpgsql SET search_path = pg_catalog "
                        "AS $$ BEGIN RETURN NEW; END $$"
                    )


def test_postgres_memory_revision_rollback_rejects_catalog_drift(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    psycopg, _dict_row, _Jsonb = _load_psycopg()
    with psycopg.connect(**postgres_cluster.connection_kwargs()) as connection:
        connection.execute(f"CREATE TABLE {SCHEMA}.unexpected (value integer)")
    result = postgres_cluster.run_script(ROLLBACK)
    assert result.returncode != 0
    assert "catalog" in result.stderr


def test_postgres_memory_revision_constructor_and_connect_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    with pytest.raises(ValueError, match="connection is required"):
        PostgresMemoryRevisionV3Repository(None)

    class _FailingPsycopg:
        @staticmethod
        def connect(*_args: object, **_kwargs: object) -> object:
            raise RuntimeError("unavailable")

    monkeypatch.setattr(
        pg_revision,
        "_load_psycopg",
        lambda: (_FailingPsycopg, object(), object()),
    )
    with pytest.raises(
        tbm.PostgresMemoryRevisionV3PersistenceError,
        match="failed to connect",
    ):
        PostgresMemoryRevisionV3Repository.connect()


def test_postgres_memory_revision_schema_validation_error_shapes() -> None:
    repository = PostgresMemoryRevisionV3Repository(object())
    with pytest.raises(PostgresMemoryRevisionV3SchemaError, match="search_path"):
        repository._lock_schema(
            _FakeCursor(fetchalls=[[]]),
            for_write=False,
        )
    with pytest.raises(
        PostgresMemoryRevisionV3SchemaError,
        match="active schema metadata",
    ):
        repository._lock_schema(
            _FakeCursor(
                fetchalls=[[{"search_path": "public"}], []],
            ),
            for_write=False,
        )
    with pytest.raises(
        PostgresMemoryRevisionV3SchemaError,
        match="memory revision v3 metadata",
    ):
        repository._lock_schema(
            _FakeCursor(
                fetchalls=[
                    [{"search_path": "public"}],
                    [{"active_version": 2}],
                    [],
                ],
            ),
            for_write=False,
        )
    with pytest.raises(
        PostgresMemoryRevisionV3SchemaError,
        match="unsupported",
    ):
        repository._verify_schema_catalog(
            _FakeCursor(
                fetchalls=[
                    [{
                        "policy_count": 1,
                        "rule_count": 0,
                        "unsupported_relation_count": 0,
                    }],
                ],
            )
        )


@pytest.mark.parametrize(
    ("rows", "expected"),
    [
        ([], PostgresMemoryRevisionV3ConflictError),
        ([{}, {}], tbm.PostgresMemoryRevisionV3PersistenceError),
        ([{"item_id": "wrong"}], PostgresMemoryRevisionV3ConflictError),
    ],
)
def test_postgres_memory_revision_put_exact_rejects_corruption(
    rows: list[object],
    expected: type[Exception],
) -> None:
    repository = PostgresMemoryRevisionV3Repository(object())
    with pytest.raises(expected):
        repository._put_exact(
            _FakeCursor(fetchalls=[rows]),
            table="items",
            id_column="item_id",
            columns=("item_id",),
            values=("expected",),
            conflict_message="conflict",
        )


@pytest.mark.parametrize("kind", ["fix", "regression"])
@pytest.mark.parametrize(
    "corruption",
    ["missing", "ambiguous", "shape", "json", "columns"],
)
def test_postgres_memory_revision_evidence_load_rejects_corruption(
    kind: str,
    corruption: str,
) -> None:
    revision, fix, regression = _records()
    del revision
    repository = PostgresMemoryRevisionV3Repository(object())
    evidence = fix if kind == "fix" else regression
    values = (
        repository._fix_values(fix)
        if kind == "fix"
        else repository._regression_values(regression)
    )
    columns = (
        "evidence_id",
        "case_id",
        "source_trace_id",
        "source_commit_sha",
        "fix_commit_sha",
        "descriptor",
    )
    valid_row = dict(zip(columns, values, strict=True))
    rows: list[object]
    if corruption == "missing":
        rows = []
    elif corruption == "ambiguous":
        rows = [valid_row, valid_row]
    elif corruption == "shape":
        rows = [{**valid_row, "descriptor": None}]
    elif corruption == "json":
        rows = [{**valid_row, "descriptor": "{"}]
    else:
        rows = [{**valid_row, "case_id": "mismatch"}]
    loader = repository._load_fix if kind == "fix" else repository._load_regression
    with pytest.raises(tbm.PostgresMemoryRevisionV3PersistenceError):
        loader(_FakeCursor(fetchalls=[rows]), evidence.evidence_id)


def _revision_fixture() -> tbm.MemoryRevision:
    revision, _fix, _regression = _records()
    return revision


@pytest.mark.parametrize(
    "corruption",
    ["missing", "ambiguous", "shape", "json", "columns"],
)
def test_postgres_memory_revision_bundle_load_rejects_corruption(
    corruption: str,
) -> None:
    revision = _revision_fixture()
    repository = PostgresMemoryRevisionV3Repository(object())
    columns = (
        "revision_id",
        "memory_id",
        "revision_number",
        "previous_revision_id",
        "fix_evidence_id",
        "descriptor",
    )
    valid_row = dict(
        zip(columns, repository._revision_values(revision), strict=True)
    )
    if corruption == "missing":
        rows: list[object] = []
    elif corruption == "ambiguous":
        rows = [valid_row, valid_row]
    elif corruption == "shape":
        rows = [{**valid_row, "descriptor": None}]
    elif corruption == "json":
        rows = [{**valid_row, "descriptor": "{"}]
    else:
        rows = [{**valid_row, "memory_id": "mismatch"}]
    with pytest.raises(tbm.PostgresMemoryRevisionV3PersistenceError):
        repository._load_bundle(
            _FakeCursor(fetchalls=[rows]),
            revision.revision_id,
            missing_is_not_found=False,
        )


@pytest.mark.parametrize(
    "link_rows",
    [
        [{"ordinal": "0", "evidence_id": "invalid"}],
        [{"ordinal": 1, "evidence_id": "regression_sha256_" + "0" * 64}],
    ],
)
def test_postgres_memory_revision_bundle_load_rejects_bad_links(
    link_rows: list[object],
) -> None:
    revision, fix, _regression = _records()
    repository = PostgresMemoryRevisionV3Repository(object())
    columns = (
        "revision_id",
        "memory_id",
        "revision_number",
        "previous_revision_id",
        "fix_evidence_id",
        "descriptor",
    )
    row = dict(zip(columns, repository._revision_values(revision), strict=True))
    evidence_columns = (
        "evidence_id",
        "case_id",
        "source_trace_id",
        "source_commit_sha",
        "fix_commit_sha",
        "descriptor",
    )
    fix_row = dict(
        zip(
            evidence_columns,
            repository._fix_values(fix),
            strict=True,
        )
    )
    with pytest.raises(tbm.PostgresMemoryRevisionV3PersistenceError):
        repository._load_bundle(
            _FakeCursor(fetchalls=[[row], [fix_row], link_rows]),
            revision.revision_id,
            missing_is_not_found=False,
        )


def test_postgres_memory_revision_parent_and_input_errors() -> None:
    revision, fix, regression = _records()
    child = tbm.build_memory_revision(
        **{
            **{
                key: value
                for key, value in revision.__dict__.items()
                if key
                not in {
                    "revision_id",
                    "contract_version",
                    "revision_number",
                    "previous_revision_id",
                }
            },
            "revision_number": 2,
            "previous_revision_id": revision.revision_id,
        }
    )
    repository = PostgresMemoryRevisionV3Repository(object())
    with pytest.raises(
        tbm.PostgresMemoryRevisionV3PersistenceError,
        match="parent is missing",
    ):
        repository._verify_parent_lineage(
            _FakeCursor(fetchalls=[[]]),
            child,
        )
    with pytest.raises(
        tbm.PostgresMemoryRevisionV3PersistenceError,
        match="descriptor failed",
    ):
        repository._verify_parent_lineage(
            _FakeCursor(
                fetchalls=[[
                    {
                        "memory_id": revision.memory_id,
                        "revision_number": 1,
                        "descriptor": "{",
                    }
                ]],
            ),
            child,
        )
    with pytest.raises(ValueError, match="revision must"):
        repository.store_proposal(object(), None, ())
    with pytest.raises(ValueError, match="fix_evidence"):
        repository.store_proposal(revision, object(), (regression,))
    with pytest.raises(ValueError, match="exact tuple"):
        repository.store_proposal(revision, fix, [])
    with pytest.raises(ValueError, match="duplicates"):
        repository.store_proposal(revision, fix, (regression, regression))
    with pytest.raises(ValueError, match="revision_id"):
        repository.load_proposal("invalid")


@pytest.mark.parametrize(
    ("sqlstate", "expected"),
    [
        ("42P01", PostgresMemoryRevisionV3SchemaError),
        ("23505", PostgresMemoryRevisionV3ConflictError),
        ("P0001", PostgresMemoryRevisionV3ConflictError),
        (None, tbm.PostgresMemoryRevisionV3PersistenceError),
    ],
)
def test_postgres_memory_revision_maps_database_errors(
    sqlstate: str | None,
    expected: type[Exception],
) -> None:
    error = RuntimeError("database")
    error.sqlstate = sqlstate  # type: ignore[attr-defined]
    repository = PostgresMemoryRevisionV3Repository(object())
    with pytest.raises(expected):
        repository._raise_database(error)


class _FakeTransaction:
    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error

    def __enter__(self) -> None:
        if self._error is not None:
            raise self._error

    def __exit__(self, *_args: object) -> None:
        return None


class _FakeConnection:
    closed = False

    def __init__(self, *, error: Exception | None = None) -> None:
        self._error = error

    def transaction(self) -> _FakeTransaction:
        return _FakeTransaction(error=self._error)


def test_postgres_memory_revision_store_rejects_corrupt_replay_identity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision, fix, regression = _records()
    repository = PostgresMemoryRevisionV3Repository(_FakeConnection())

    @contextmanager
    def secured_cursor(*, for_write: bool):
        assert for_write is True
        yield _FakeCursor(
            fetchalls=[[{"revision_id": "wrong"}]],
        )

    monkeypatch.setattr(repository, "_secured_cursor", secured_cursor)
    with pytest.raises(
        tbm.PostgresMemoryRevisionV3PersistenceError,
        match="identity",
    ):
        repository.store_proposal(revision, fix, (regression,))


def test_postgres_memory_revision_store_rejects_replay_bundle_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision, fix, regression = _records()
    repository = PostgresMemoryRevisionV3Repository(_FakeConnection())

    @contextmanager
    def secured_cursor(*, for_write: bool):
        assert for_write is True
        yield _FakeCursor(
            fetchalls=[[{"revision_id": revision.revision_id}]],
        )

    monkeypatch.setattr(repository, "_secured_cursor", secured_cursor)
    monkeypatch.setattr(repository, "_load_bundle", lambda *_args, **_kwargs: None)
    with pytest.raises(
        PostgresMemoryRevisionV3ConflictError,
        match="does not match input",
    ):
        repository.store_proposal(revision, fix, (regression,))


def test_postgres_memory_revision_store_rejects_readback_mismatch(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    revision, fix, regression = _records()
    repository = PostgresMemoryRevisionV3Repository(_FakeConnection())

    @contextmanager
    def secured_cursor(*, for_write: bool):
        assert for_write is True
        yield _FakeCursor(fetchalls=[[]])

    monkeypatch.setattr(repository, "_secured_cursor", secured_cursor)
    monkeypatch.setattr(repository, "_put_exact", lambda *_args, **_kwargs: True)
    monkeypatch.setattr(repository, "_load_bundle", lambda *_args, **_kwargs: None)
    with pytest.raises(
        PostgresMemoryRevisionV3ConflictError,
        match="does not match input",
    ):
        repository.store_proposal(revision, fix, (regression,))


def test_postgres_memory_revision_store_sanitizes_database_errors() -> None:
    revision, fix, regression = _records()
    repository = PostgresMemoryRevisionV3Repository(
        _FakeConnection(error=RuntimeError("secret")),
    )
    with pytest.raises(
        tbm.PostgresMemoryRevisionV3PersistenceError,
        match="operation failed",
    ):
        repository.store_proposal(revision, fix, (regression,))
