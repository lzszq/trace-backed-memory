from __future__ import annotations

import sqlite3

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.contracts_v3 import (
    AuthorizationScope,
    CommitRelationEvidence,
)
from trace_backed_memory.evidence_v3 import build_structured_regression_evidence
from trace_backed_memory.fix_evidence_v3 import build_fix_evidence
from trace_backed_memory.memory_revision_v3 import build_memory_revision
from trace_backed_memory.replay_v3 import create_content_addressed_artifact
from trace_backed_memory.sqlite_memory_revision_v3 import (
    SQLiteMemoryRevisionV3ConflictError,
    SQLiteMemoryRevisionV3Error,
    SQLiteMemoryRevisionV3NotFoundError,
    SQLiteMemoryRevisionV3PersistenceError,
    SQLiteMemoryRevisionV3Repository,
    SQLiteMemoryRevisionV3SchemaError,
)


def _bundle(*, revision_number: int = 1, previous_revision_id: str | None = None):
    relation = CommitRelationEvidence(
        "abc123",
        "def456",
        "ancestor",
        "git_verifier",
        "2026-07-27T00:01:00Z",
    )
    fix = build_fix_evidence(
        case_id="case_001",
        source_trace_id="trace_source",
        source_commit_sha="abc123",
        fix_commit_sha="def456",
        source_to_fix=relation,
        artifact_hashes=("sha256:" + "a" * 64,),
        submitter_id="fix_submitter",
        submitted_at="2026-07-27T00:02:00Z",
        reviewer_id="fix_reviewer",
        reviewed_at="2026-07-27T00:03:00Z",
        attestation_sha256="sha256:" + "b" * 64,
    )
    regression = build_structured_regression_evidence(
        case_id="case_001",
        source_trace_id="trace_source",
        verification_trace_id=f"trace_verify_{revision_number}",
        verification_run_id=f"run_verify_{revision_number}",
        evaluator_id="evaluator",
        evaluator_version="1",
        evaluation_suite="regression",
        evaluation_case_id=f"case_fixed_{revision_number}",
        expected_outcome="failure absent",
        observed_outcome="passed",
        result="pass",
        environment={"python": "3.13"},
        source_commit_sha="abc123",
        fix_commit_sha="def456",
        verification_commit_sha="fedcba",
        source_to_fix=relation,
        fix_to_verification=CommitRelationEvidence(
            "def456",
            "fedcba",
            "ancestor",
            "git_verifier",
            "2026-07-27T00:02:30Z",
        ),
        artifact_hashes=("sha256:" + "c" * 64,),
        submitter_id="regression_submitter",
        submitted_at="2026-07-27T00:03:00Z",
        verifier_id="regression_verifier",
        verified_at="2026-07-27T00:04:00Z",
        attestation_sha256="sha256:" + "d" * 64,
    )
    artifact = create_content_addressed_artifact(
        f'{{"revision":{revision_number}}}'.encode(),
        media_type="application/vnd.trace-backed-memory.revision+json",
        classification="internal",
        created_at="2026-07-27T00:05:00Z",
    )
    revision = build_memory_revision(
        memory_id="memory_001",
        memory_kind="lesson",
        revision_number=revision_number,
        previous_revision_id=previous_revision_id,
        memory_type="procedural",
        content_artifact=artifact,
        scope=AuthorizationScope(
            "repository",
            "tenant_001",
            "repository_001",
            (),
        ),
        confidence=1,
        sensitive=False,
        eval_leaking=False,
        source_case_id="case_001",
        source_case_revision_id="case_revision_001",
        fix_evidence_id=fix.evidence_id,
        regression_evidence_ids=(regression.evidence_id,),
        proposed_by="revision_proposer",
        proposed_via_client_id="agent_client",
        proposed_at="2026-07-27T00:06:00Z",
        proposal_attestation_sha256="sha256:" + "e" * 64,
    )
    return revision, fix, regression


def test_sqlite_proposal_round_trip_and_exact_idempotency():
    revision, fix, regression = _bundle()
    with SQLiteMemoryRevisionV3Repository.connect(
        ":memory:", initialize=True
    ) as repository:
        first = repository.store_proposal(revision, fix, (regression,))
        replay = repository.store_proposal(revision, fix, (regression,))

        assert first.revision_inserted is True
        assert first.fix_evidence_inserted is True
        assert first.regression_evidence_inserted == 1
        assert replay.revision_inserted is False
        assert replay.fix_evidence_inserted is False
        assert replay.regression_evidence_inserted == 0
        assert repository.load_proposal(revision.revision_id) == (
            tbm.StoredMemoryRevisionProposal(revision, fix, (regression,))
        )


def test_sqlite_proposal_enforces_linear_parent_continuity():
    first, fix, regression = _bundle()
    second, second_fix, second_regression = _bundle(
        revision_number=2,
        previous_revision_id=first.revision_id,
    )
    with SQLiteMemoryRevisionV3Repository.connect(
        ":memory:", initialize=True
    ) as repository:
        with pytest.raises(SQLiteMemoryRevisionV3ConflictError):
            repository.store_proposal(
                second,
                second_fix,
                (second_regression,),
            )
        repository.store_proposal(first, fix, (regression,))
        repository.store_proposal(second, second_fix, (second_regression,))
        assert repository.load_proposal(second.revision_id).revision == second


def test_sqlite_proposal_rejects_competing_revision_slot():
    revision, fix, regression = _bundle()
    artifact = create_content_addressed_artifact(
        b'{"competing":true}',
        media_type=revision.content_artifact.media_type,
        classification="internal",
        created_at=revision.content_artifact.created_at,
    )
    competing = build_memory_revision(
        **{
            **{
                key: value
                for key, value in revision.__dict__.items()
                if key not in {"revision_id", "contract_version"}
            },
            "content_artifact": artifact,
        }
    )
    with SQLiteMemoryRevisionV3Repository.connect(
        ":memory:", initialize=True
    ) as repository:
        repository.store_proposal(revision, fix, (regression,))
        with pytest.raises(SQLiteMemoryRevisionV3ConflictError):
            repository.store_proposal(competing, fix, (regression,))


def test_sqlite_project_policy_proposal_requires_no_case_evidence():
    lesson, _, _ = _bundle()
    policy = build_memory_revision(
        **{
            **{
                key: value
                for key, value in lesson.__dict__.items()
                if key not in {"revision_id", "contract_version"}
            },
            "memory_id": "policy_001",
            "memory_kind": "project_policy",
            "memory_type": "policy",
            "source_case_id": None,
            "source_case_revision_id": None,
            "fix_evidence_id": None,
            "regression_evidence_ids": (),
        }
    )
    with SQLiteMemoryRevisionV3Repository.connect(
        ":memory:", initialize=True
    ) as repository:
        repository.store_proposal(policy, None, ())
        stored = repository.load_proposal(policy.revision_id)
        assert stored == tbm.StoredMemoryRevisionProposal(policy, None, ())


def test_sqlite_proposal_fails_closed_on_evidence_mismatch():
    revision, fix, regression = _bundle()
    with SQLiteMemoryRevisionV3Repository.connect(
        ":memory:", initialize=True
    ) as repository:
        with pytest.raises(ValueError, match="exactly match"):
            repository.store_proposal(revision, fix, ())
        with pytest.raises(ValueError, match="missing fix"):
            repository.store_proposal(revision, None, (regression,))


def test_sqlite_proposal_rejects_invalid_exact_input_types_and_duplicates():
    revision, fix, regression = _bundle()
    repository = SQLiteMemoryRevisionV3Repository.connect(":memory:")

    with pytest.raises(ValueError, match="exactly MemoryRevision"):
        repository.store_proposal(object(), fix, (regression,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exactly FixEvidence"):
        repository.store_proposal(revision, object(), (regression,))  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="exact tuple"):
        repository.store_proposal(revision, fix, [regression])  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="duplicates"):
        repository.store_proposal(revision, fix, (regression, regression))


def test_sqlite_proposal_tables_are_immutable():
    revision, fix, regression = _bundle()
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    repository = SQLiteMemoryRevisionV3Repository(connection)
    connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-memory-revision.sql"
        ).decode()
    )
    repository.store_proposal(revision, fix, (regression,))

    for statement in (
        "UPDATE v3_fix_evidence SET case_id = 'changed'",
        "DELETE FROM v3_regression_evidence",
        "UPDATE v3_memory_revision_proposals SET memory_id = 'changed'",
        "DELETE FROM v3_memory_revision_regression_evidence",
    ):
        with pytest.raises(sqlite3.IntegrityError):
            connection.execute(statement)


def test_sqlite_proposal_preserves_caller_transaction():
    revision, fix, regression = _bundle()
    connection = sqlite3.connect(":memory:")
    repository = SQLiteMemoryRevisionV3Repository(connection)
    connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-memory-revision.sql"
        ).decode()
    )
    connection.execute("BEGIN")
    repository.store_proposal(revision, fix, (regression,))
    connection.rollback()

    with pytest.raises(SQLiteMemoryRevisionV3NotFoundError):
        repository.load_proposal(revision.revision_id)


def test_sqlite_proposal_rolls_back_after_commit_failure():
    class FailingCommitConnection(sqlite3.Connection):
        fail_commit = False

        def commit(self) -> None:
            if self.fail_commit:
                raise sqlite3.OperationalError("simulated commit failure")
            super().commit()

    revision, fix, regression = _bundle()
    connection = sqlite3.connect(
        ":memory:",
        factory=FailingCommitConnection,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-memory-revision.sql"
        ).decode()
    )
    repository = SQLiteMemoryRevisionV3Repository(connection)
    connection.fail_commit = True

    with pytest.raises(
        SQLiteMemoryRevisionV3PersistenceError,
        match="failed to store",
    ):
        repository.store_proposal(revision, fix, (regression,))

    assert connection.in_transaction is False
    assert connection.execute(
        "SELECT COUNT(*) FROM v3_memory_revision_proposals"
    ).fetchone() == (0,)


def test_sqlite_proposal_detects_missing_and_drifted_schema():
    missing = SQLiteMemoryRevisionV3Repository.connect(":memory:")
    with pytest.raises(SQLiteMemoryRevisionV3SchemaError):
        missing.load_proposal("memory_revision_sha256_" + "0" * 64)

    revision, fix, regression = _bundle()
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-memory-revision.sql"
        ).decode()
    )
    connection.execute("DROP TRIGGER v3_fix_evidence_immutable_update")
    repository = SQLiteMemoryRevisionV3Repository(connection)
    with pytest.raises(SQLiteMemoryRevisionV3SchemaError):
        repository.store_proposal(revision, fix, (regression,))


@pytest.mark.parametrize(
    ("pragma", "expected"),
    (
        ("PRAGMA foreign_keys = OFF", "requires foreign keys"),
        ("PRAGMA recursive_triggers = OFF", "requires recursive triggers"),
    ),
)
def test_sqlite_proposal_requires_safety_pragmas(pragma: str, expected: str):
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-memory-revision.sql"
        ).decode()
    )
    connection.execute(pragma)
    repository = SQLiteMemoryRevisionV3Repository(connection)

    with pytest.raises(SQLiteMemoryRevisionV3SchemaError, match=expected):
        repository.load_proposal("memory_revision_sha256_" + "0" * 64)


def test_sqlite_proposal_rejects_schema_metadata_drift():
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-memory-revision.sql"
        ).decode()
    )
    connection.execute(
        "DELETE FROM trace_backed_memory_v3_memory_revision_schema"
    )
    repository = SQLiteMemoryRevisionV3Repository(connection)

    with pytest.raises(SQLiteMemoryRevisionV3SchemaError, match="metadata"):
        repository.load_proposal("memory_revision_sha256_" + "0" * 64)


def test_sqlite_proposal_rejects_unexpected_main_and_temp_triggers():
    revision, fix, regression = _bundle()
    for statement in (
        "CREATE TRIGGER unexpected_revision_trigger "
        "BEFORE INSERT ON v3_fix_evidence BEGIN SELECT 1; END",
        "CREATE TEMP TRIGGER unexpected_temp_revision_trigger "
        "BEFORE INSERT ON main.v3_fix_evidence BEGIN SELECT 1; END",
    ):
        connection = sqlite3.connect(":memory:")
        connection.execute("PRAGMA foreign_keys = ON")
        connection.execute("PRAGMA recursive_triggers = ON")
        connection.executescript(
            tbm.read_packaged_resource(
                "schemas/sqlite-v3-memory-revision.sql"
            ).decode()
        )
        connection.execute(statement)
        repository = SQLiteMemoryRevisionV3Repository(connection)

        with pytest.raises(SQLiteMemoryRevisionV3SchemaError):
            repository.store_proposal(revision, fix, (regression,))


def test_sqlite_proposal_preflights_invalid_bundle_before_database_io():
    revision, fix, _ = _bundle()
    connection = sqlite3.connect(":memory:")
    repository = SQLiteMemoryRevisionV3Repository(connection)
    statements: list[str] = []
    connection.set_trace_callback(statements.append)

    with pytest.raises(ValueError, match="exactly match"):
        repository.store_proposal(revision, fix, ())

    assert statements == []


def test_sqlite_proposal_rejects_extra_direct_sql_evidence_link():
    revision, fix, regression = _bundle()
    _, _, extra_regression = _bundle(
        revision_number=2,
        previous_revision_id=revision.revision_id,
    )
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    repository = SQLiteMemoryRevisionV3Repository(connection)
    connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-memory-revision.sql"
        ).decode()
    )
    repository.store_proposal(revision, fix, (regression,))
    connection.execute(
        "INSERT INTO v3_regression_evidence "
        "(evidence_id, case_id, source_trace_id, source_commit_sha, "
        "fix_commit_sha, descriptor) VALUES (?, ?, ?, ?, ?, ?)",
        (
            extra_regression.evidence_id,
            extra_regression.case_id,
            extra_regression.source_trace_id,
            extra_regression.source_commit_sha,
            extra_regression.fix_commit_sha,
            tbm.dumps_structured_regression_evidence(extra_regression),
        ),
    )
    connection.execute(
        "INSERT INTO v3_memory_revision_regression_evidence "
        "(revision_id, ordinal, evidence_id) VALUES (?, ?, ?)",
        (revision.revision_id, 1, extra_regression.evidence_id),
    )

    with pytest.raises(
        SQLiteMemoryRevisionV3PersistenceError,
        match="do not match revision",
    ):
        repository.load_proposal(revision.revision_id)


def test_sqlite_proposal_replay_never_repairs_tampered_evidence_links():
    revision, fix, regression = _bundle()
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    repository = SQLiteMemoryRevisionV3Repository(connection)
    schema = tbm.read_packaged_resource(
        "schemas/sqlite-v3-memory-revision.sql"
    ).decode()
    connection.executescript(schema)
    repository.store_proposal(revision, fix, (regression,))
    connection.execute("DROP TRIGGER v3_memory_revision_links_immutable_delete")
    connection.execute(
        "DELETE FROM v3_memory_revision_regression_evidence "
        "WHERE revision_id = ?",
        (revision.revision_id,),
    )
    connection.executescript(schema)

    with pytest.raises(
        SQLiteMemoryRevisionV3PersistenceError,
        match="do not match revision",
    ):
        repository.store_proposal(revision, fix, (regression,))

    assert connection.execute(
        "SELECT evidence_id FROM v3_memory_revision_regression_evidence "
        "WHERE revision_id = ?",
        (revision.revision_id,),
    ).fetchall() == []


def test_sqlite_proposal_rejects_non_contiguous_evidence_ordinals():
    revision, fix, regression = _bundle()
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    repository = SQLiteMemoryRevisionV3Repository(connection)
    schema = tbm.read_packaged_resource(
        "schemas/sqlite-v3-memory-revision.sql"
    ).decode()
    connection.executescript(schema)
    repository.store_proposal(revision, fix, (regression,))
    connection.execute("DROP TRIGGER v3_memory_revision_links_immutable_update")
    connection.execute(
        "UPDATE v3_memory_revision_regression_evidence SET ordinal = 1 "
        "WHERE revision_id = ?",
        (revision.revision_id,),
    )
    connection.executescript(schema)

    with pytest.raises(
        SQLiteMemoryRevisionV3PersistenceError,
        match="ordinals are not contiguous",
    ):
        repository.load_proposal(revision.revision_id)


def test_sqlite_proposal_reports_missing_linked_evidence_as_corruption():
    revision, _, _ = _bundle()
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-memory-revision.sql"
        ).decode()
    )
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute(
        "INSERT INTO v3_memory_revision_proposals "
        "(revision_id, memory_id, revision_number, previous_revision_id, "
        "fix_evidence_id, descriptor) VALUES (?, ?, ?, ?, ?, ?)",
        (
            revision.revision_id,
            revision.memory_id,
            revision.revision_number,
            revision.previous_revision_id,
            revision.fix_evidence_id,
            tbm.dumps_memory_revision(revision),
        ),
    )
    connection.commit()
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    repository = SQLiteMemoryRevisionV3Repository(connection)

    with pytest.raises(
        SQLiteMemoryRevisionV3PersistenceError,
        match="fix evidence reference is missing",
    ):
        repository.load_proposal(revision.revision_id)


def test_sqlite_proposal_reports_missing_regression_evidence_as_corruption():
    revision, fix, regression = _bundle()
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    repository = SQLiteMemoryRevisionV3Repository(connection)
    schema = tbm.read_packaged_resource(
        "schemas/sqlite-v3-memory-revision.sql"
    ).decode()
    connection.executescript(schema)
    repository.store_proposal(revision, fix, (regression,))
    connection.execute("PRAGMA foreign_keys = OFF")
    connection.execute("DROP TRIGGER v3_regression_evidence_immutable_delete")
    connection.execute(
        "DELETE FROM v3_regression_evidence WHERE evidence_id = ?",
        (regression.evidence_id,),
    )
    connection.executescript(schema)
    connection.execute("PRAGMA foreign_keys = ON")

    with pytest.raises(
        SQLiteMemoryRevisionV3PersistenceError,
        match="regression evidence reference is missing",
    ):
        repository.load_proposal(revision.revision_id)


@pytest.mark.parametrize(
    ("trigger", "statement", "expected"),
    (
        (
            "v3_memory_revision_proposals_immutable_update",
            "UPDATE v3_memory_revision_proposals SET descriptor = '{}'",
            "revision descriptor failed validation",
        ),
        (
            "v3_fix_evidence_immutable_update",
            "UPDATE v3_fix_evidence SET descriptor = '{}'",
            "fix evidence failed validation",
        ),
        (
            "v3_regression_evidence_immutable_update",
            "UPDATE v3_regression_evidence SET descriptor = '{}'",
            "regression evidence failed validation",
        ),
        (
            "v3_fix_evidence_immutable_update",
            "UPDATE v3_fix_evidence SET case_id = 'wrong_case'",
            "fix evidence columns do not match descriptor",
        ),
        (
            "v3_regression_evidence_immutable_update",
            "UPDATE v3_regression_evidence SET case_id = 'wrong_case'",
            "regression columns do not match descriptor",
        ),
    ),
)
def test_sqlite_proposal_rejects_corrupt_evidence_descriptors_and_columns(
    trigger: str,
    statement: str,
    expected: str,
):
    revision, fix, regression = _bundle()
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    repository = SQLiteMemoryRevisionV3Repository(connection)
    schema = tbm.read_packaged_resource(
        "schemas/sqlite-v3-memory-revision.sql"
    ).decode()
    connection.executescript(schema)
    repository.store_proposal(revision, fix, (regression,))
    connection.execute(f"DROP TRIGGER {trigger}")
    connection.execute(statement)
    connection.executescript(schema)

    with pytest.raises(SQLiteMemoryRevisionV3PersistenceError, match=expected):
        repository.load_proposal(revision.revision_id)


def test_sqlite_proposal_rejects_corrupt_direct_sql_rows():
    revision, _, _ = _bundle()
    connection = sqlite3.connect(":memory:")
    connection.executescript(
        tbm.read_packaged_resource(
            "schemas/sqlite-v3-memory-revision.sql"
        ).decode()
    )
    connection.execute(
        "INSERT INTO v3_memory_revision_proposals "
        "(revision_id, memory_id, revision_number, previous_revision_id, "
        "fix_evidence_id, descriptor) VALUES (?, ?, ?, ?, ?, ?)",
        (
            revision.revision_id,
            "wrong_memory",
            1,
            None,
            None,
            tbm.dumps_memory_revision(revision),
        ),
    )
    repository = SQLiteMemoryRevisionV3Repository(connection)

    with pytest.raises(tbm.SQLiteMemoryRevisionV3PersistenceError):
        repository.load_proposal(revision.revision_id)


def test_sqlite_proposal_closed_repository_fails():
    repository = SQLiteMemoryRevisionV3Repository.connect(
        ":memory:", initialize=True
    )
    repository.close()
    repository.close()

    with pytest.raises(SQLiteMemoryRevisionV3Error, match="closed"):
        repository.load_proposal("memory_revision_sha256_" + "0" * 64)


def test_sqlite_proposal_rejects_invalid_constructor_and_initialize_types():
    with pytest.raises(ValueError, match="sqlite3.Connection"):
        SQLiteMemoryRevisionV3Repository(object())  # type: ignore[arg-type]
    with pytest.raises(ValueError, match="boolean"):
        SQLiteMemoryRevisionV3Repository.connect(
            ":memory:",
            initialize=1,  # type: ignore[arg-type]
        )


def test_sqlite_proposal_wraps_connection_configuration_errors():
    with pytest.raises(
        SQLiteMemoryRevisionV3PersistenceError,
        match="failed to connect",
    ):
        SQLiteMemoryRevisionV3Repository.connect(
            ":memory:",
            unsupported_option=True,
        )


def test_sqlite_proposal_close_preserves_non_owned_connection():
    connection = sqlite3.connect(":memory:")
    repository = SQLiteMemoryRevisionV3Repository(connection)

    repository.close()

    assert connection.execute("SELECT 1").fetchone() == (1,)
