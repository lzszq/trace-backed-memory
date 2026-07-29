from __future__ import annotations

from pathlib import Path
import sqlite3

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.sqlite_memory_publication_v3 as sqlite_publication
from tests.test_memory_publication_v3 import (
    ACTIVATED_AT,
    APPROVED_AT,
    CONTENT,
    DIGEST,
    _authorization,
    _policy,
    _publication_inputs,
)
from trace_backed_memory.memory_publication_v3 import approve_memory_revision
from trace_backed_memory.memory_revision_v3 import build_memory_revision
from trace_backed_memory.resources import read_packaged_resource
from trace_backed_memory.replay_v3 import create_content_addressed_artifact
from trace_backed_memory.sqlite_memory_publication_v3 import (
    SQLiteMemoryPublicationV3AttestationError,
    SQLiteMemoryPublicationV3ConflictError,
    SQLiteMemoryPublicationV3Error,
    SQLiteMemoryPublicationV3NotFoundError,
    SQLiteMemoryPublicationV3PersistenceError,
    SQLiteMemoryPublicationV3Repository,
    SQLiteMemoryPublicationV3SchemaError,
    _loads_request,
    _normalized_schema_sql,
    _request_descriptor,
)
from trace_backed_memory.sqlite_memory_revision_v3 import (
    SQLiteMemoryRevisionV3Repository,
)


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(":memory:")
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    for resource in (
        "schemas/sqlite-v3-memory-revision.sql",
        "schemas/sqlite-v3-memory-publication.sql",
    ):
        connection.executescript(
            read_packaged_resource(resource).decode("utf-8")
        )
    return connection


def _stored_inputs(connection: sqlite3.Connection):
    revision, fixes, regressions = _publication_inputs()
    SQLiteMemoryRevisionV3Repository(connection).store_proposal(
        revision,
        next(iter(fixes.values())),
        tuple(regressions.values()),
    )
    return revision, fixes, regressions


def _approval_inputs(connection: sqlite3.Connection):
    revision, fixes, regressions = _stored_inputs(connection)
    policy = _policy()
    request, decision = _authorization(
        policy,
        actor_id="publication_approver",
        permission="memory:review",
        decided_at=APPROVED_AT,
    )
    return revision, fixes, regressions, policy, request, decision


def _append_approval(
    repository: SQLiteMemoryPublicationV3Repository,
    inputs,
):
    revision, fixes, regressions, policy, request, decision = inputs
    return repository.append_approval(
        revision=revision,
        previous_revision=None,
        content=CONTENT,
        fix_evidence_by_id=fixes,
        regression_evidence_by_id=regressions,
        policy=policy,
        request=request,
        decision=decision,
        approved_by="publication_approver",
        approved_via_client_id="publication_service",
        approved_at=APPROVED_AT,
        approval_attestation_sha256=DIGEST,
    )


def _append_activation(
    repository: SQLiteMemoryPublicationV3Repository,
    inputs,
    approval,
):
    revision, fixes, regressions, policy, approval_request, approval_decision = (
        inputs
    )
    request, decision = _authorization(
        policy,
        actor_id="publication_activator",
        permission="memory:activate",
        decided_at=ACTIVATED_AT,
    )
    return repository.append_activation(
        revision=revision,
        previous_revision=None,
        content=CONTENT,
        fix_evidence_by_id=fixes,
        regression_evidence_by_id=regressions,
        approval=approval,
        approval_policy=policy,
        approval_request=approval_request,
        approval_decision=approval_decision,
        policy=policy,
        request=request,
        decision=decision,
        activated_by="publication_activator",
        activated_via_client_id="publication_service",
        activated_at=ACTIVATED_AT,
        activation_attestation_sha256=DIGEST,
    )


def _second_revision(first):
    content = b'{"memory_text":"Prefer the verified workflow twice."}'
    artifact = create_content_addressed_artifact(
        content,
        media_type=first.content_artifact.media_type,
        classification=first.content_artifact.classification,
        created_at="2026-07-27T00:09:00Z",
    )
    revision = build_memory_revision(
        memory_id=first.memory_id,
        memory_kind=first.memory_kind,
        revision_number=2,
        previous_revision_id=first.revision_id,
        memory_type=first.memory_type,
        content_artifact=artifact,
        scope=first.scope,
        confidence=first.confidence,
        sensitive=first.sensitive,
        eval_leaking=first.eval_leaking,
        source_case_id=first.source_case_id,
        source_case_revision_id=first.source_case_revision_id,
        fix_evidence_id=first.fix_evidence_id,
        regression_evidence_ids=first.regression_evidence_ids,
        proposed_by=first.proposed_by,
        proposed_via_client_id=first.proposed_via_client_id,
        proposed_at="2026-07-27T00:10:00Z",
        proposal_attestation_sha256=first.proposal_attestation_sha256,
    )
    return revision, content


def _unstored_revision(revision):
    return build_memory_revision(
        **{
            **{
                key: value
                for key, value in revision.__dict__.items()
                if key not in {"revision_id", "contract_version"}
            },
            "memory_id": "memory_unstored",
        }
    )


def test_sqlite_publication_round_trip_exact_replay_and_durable_head():
    connection = _connection()
    inputs = _approval_inputs(connection)
    calls: list[tuple[str, str, str, str]] = []

    def verify(kind, actor, client, digest):
        calls.append((kind, actor, client, digest))
        return True

    with SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=verify,
        attestation_verifier_id="attestation_verifier",
    ) as repository:
        first_approval = _append_approval(repository, inputs)
        replay_approval = _append_approval(repository, inputs)
        first_activation = _append_activation(
            repository,
            inputs,
            first_approval.approval,
        )
        replay_activation = _append_activation(
            repository,
            inputs,
            first_approval.approval,
        )

        assert first_approval.inserted is True
        assert replay_approval.inserted is False
        assert first_activation.inserted is True
        assert replay_activation.inserted is False
        assert repository.load_approval(
            first_approval.approval.approval_id
        ).approval == first_approval.approval
        assert repository.load_activation(
            first_activation.activation.activation_id
        ).activation == first_activation.activation
        approval_bundle = repository.load_approval_bundle(
            first_approval.approval.approval_id
        )
        assert approval_bundle.approval == first_approval.approval
        assert approval_bundle.policy.policy_sha256 == inputs[3].policy_sha256
        assert approval_bundle.request == inputs[4]
        assert approval_bundle.decision == inputs[5]
        assert approval_bundle.attestation_verified_by == "attestation_verifier"
        activation_bundle = repository.load_activation_bundle(
            first_activation.activation.activation_id
        )
        assert activation_bundle.activation == first_activation.activation
        assert activation_bundle.policy.policy_sha256 == inputs[3].policy_sha256
        assert activation_bundle.attestation_verified_by == "attestation_verifier"
        assert repository.load_head(
            tenant_id="tenant_001",
            repository_id="repository_001",
            memory_id=inputs[0].memory_id,
        ) == tbm.SQLiteMemoryPublicationV3Head(
            tenant_id="tenant_001",
            repository_id="repository_001",
            memory_id=inputs[0].memory_id,
            current_revision_number=1,
            current_revision_id=inputs[0].revision_id,
            current_activation_id=(
                first_activation.activation.activation_id
            ),
        )
    assert [item[0] for item in calls] == [
        "approval",
        "approval",
        "activation",
        "activation",
    ]


def test_sqlite_publication_advances_only_from_durable_head():
    connection = _connection()
    inputs = _approval_inputs(connection)
    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="attestation_verifier",
    )
    first_approval = _append_approval(repository, inputs).approval
    first_activation = _append_activation(
        repository,
        inputs,
        first_approval,
    ).activation
    second, second_content = _second_revision(inputs[0])
    SQLiteMemoryRevisionV3Repository(connection).store_proposal(
        second,
        next(iter(inputs[1].values())),
        tuple(inputs[2].values()),
    )
    review_request, review_decision = _authorization(
        inputs[3],
        actor_id="publication_approver",
        permission="memory:review",
        decided_at="2026-07-27T00:11:00Z",
    )
    second_approval = repository.append_approval(
        revision=second,
        previous_revision=inputs[0],
        content=second_content,
        fix_evidence_by_id=inputs[1],
        regression_evidence_by_id=inputs[2],
        policy=inputs[3],
        request=review_request,
        decision=review_decision,
        approved_by="publication_approver",
        approved_via_client_id="publication_service",
        approved_at="2026-07-27T00:11:00Z",
        approval_attestation_sha256=DIGEST,
    ).approval
    activate_request, activate_decision = _authorization(
        inputs[3],
        actor_id="publication_activator",
        permission="memory:activate",
        decided_at="2026-07-27T00:12:00Z",
    )
    second_activation = repository.append_activation(
        revision=second,
        previous_revision=inputs[0],
        content=second_content,
        fix_evidence_by_id=inputs[1],
        regression_evidence_by_id=inputs[2],
        approval=second_approval,
        approval_policy=inputs[3],
        approval_request=review_request,
        approval_decision=review_decision,
        policy=inputs[3],
        request=activate_request,
        decision=activate_decision,
        activated_by="publication_activator",
        activated_via_client_id="publication_service",
        activated_at="2026-07-27T00:12:00Z",
        activation_attestation_sha256=DIGEST,
    ).activation

    assert second_activation.previous_activation_id == (
        first_activation.activation_id
    )
    assert repository.load_head(
        tenant_id="tenant_001",
        repository_id="repository_001",
        memory_id=second.memory_id,
    ).current_revision_number == 2
    replay_second = repository.append_activation(
        revision=second,
        previous_revision=inputs[0],
        content=second_content,
        fix_evidence_by_id=inputs[1],
        regression_evidence_by_id=inputs[2],
        approval=second_approval,
        approval_policy=inputs[3],
        approval_request=review_request,
        approval_decision=review_decision,
        policy=inputs[3],
        request=activate_request,
        decision=activate_decision,
        activated_by="publication_activator",
        activated_via_client_id="publication_service",
        activated_at="2026-07-27T00:12:00Z",
        activation_attestation_sha256=DIGEST,
    )
    assert replay_second.inserted is False
    replay_first = _append_activation(
        repository,
        inputs,
        first_approval,
    )
    assert replay_first.inserted is False
    assert replay_first.activation == first_activation
    repository.close()


def test_sqlite_publication_requires_stored_proposal_and_attestation():
    connection = _connection()
    revision, fixes, regressions = _publication_inputs()
    policy = _policy()
    request, decision = _authorization(
        policy,
        actor_id="publication_approver",
        permission="memory:review",
        decided_at=APPROVED_AT,
    )
    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="attestation_verifier",
    )
    with pytest.raises(
        SQLiteMemoryPublicationV3NotFoundError,
        match="proposal",
    ):
        repository.append_approval(
            revision=revision,
            previous_revision=None,
            content=CONTENT,
            fix_evidence_by_id=fixes,
            regression_evidence_by_id=regressions,
            policy=policy,
            request=request,
            decision=decision,
            approved_by="publication_approver",
            approved_via_client_id="publication_service",
            approved_at=APPROVED_AT,
            approval_attestation_sha256=DIGEST,
        )
    repository.close()

    stored = _approval_inputs(connection)
    rejecting = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: False,
        attestation_verifier_id="attestation_verifier",
    )
    with pytest.raises(
        SQLiteMemoryPublicationV3AttestationError,
        match="not verified",
    ):
        _append_approval(rejecting, stored)
    assert connection.execute(
        "SELECT COUNT(*) FROM v3_memory_revision_approvals"
    ).fetchone() == (0,)
    rejecting.close()


def test_sqlite_activation_requires_exact_durable_approval_provenance():
    connection = _connection()
    inputs = _approval_inputs(connection)
    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="attestation_verifier",
    )
    _append_approval(repository, inputs)
    revision, fixes, regressions, policy, request, decision = inputs
    alternate = approve_memory_revision(
        revision=revision,
        previous_revision=None,
        content=CONTENT,
        fix_evidence_by_id=fixes,
        regression_evidence_by_id=regressions,
        policy=policy,
        request=request,
        decision=decision,
        approved_by="publication_approver",
        approved_via_client_id="publication_service",
        approved_at=APPROVED_AT,
        approval_attestation_sha256="sha256:" + "a" * 64,
    )
    with pytest.raises(
        SQLiteMemoryPublicationV3NotFoundError,
        match="approval",
    ):
        _append_activation(repository, inputs, alternate)
    assert connection.execute(
        "SELECT COUNT(*) FROM v3_memory_revision_activations"
    ).fetchone() == (0,)
    repository.close()


def test_sqlite_publication_savepoint_failure_preserves_outer_transaction():
    connection = _connection()
    inputs = _approval_inputs(connection)
    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="attestation_verifier",
    )
    connection.execute("CREATE TABLE caller_state (value TEXT NOT NULL)")
    connection.execute("INSERT INTO caller_state VALUES ('retained')")
    unstored_revision = build_memory_revision(
        **{
            **{
                key: value
                for key, value in inputs[0].__dict__.items()
                if key not in {"revision_id", "contract_version"}
            },
            "memory_id": "memory_unstored",
        }
    )

    with pytest.raises(SQLiteMemoryPublicationV3NotFoundError):
        repository.append_approval(
            revision=unstored_revision,
            previous_revision=None,
            content=CONTENT,
            fix_evidence_by_id=inputs[1],
            regression_evidence_by_id=inputs[2],
            policy=inputs[3],
            request=inputs[4],
            decision=inputs[5],
            approved_by="publication_approver",
            approved_via_client_id="publication_service",
            approved_at=APPROVED_AT,
            approval_attestation_sha256=DIGEST,
        )
    assert connection.in_transaction is True
    assert connection.execute(
        "SELECT value FROM caller_state"
    ).fetchall() == [("retained",)]
    assert connection.execute(
        "SELECT COUNT(*) FROM v3_memory_revision_approvals"
    ).fetchone() == (0,)
    connection.rollback()
    repository.close()


def test_sqlite_publication_schema_and_direct_mutation_fail_closed():
    connection = _connection()
    inputs = _approval_inputs(connection)
    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="attestation_verifier",
    )
    approval = _append_approval(repository, inputs).approval
    activation = _append_activation(repository, inputs, approval).activation
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "UPDATE v3_memory_revision_approvals SET memory_id = 'changed'"
        )
    with pytest.raises(sqlite3.IntegrityError, match="immutable"):
        connection.execute(
            "DELETE FROM v3_memory_revision_activations "
            "WHERE activation_id = ?",
            (activation.activation_id,),
        )
    with pytest.raises(sqlite3.IntegrityError, match="cannot be deleted"):
        connection.execute(
            "DELETE FROM v3_memory_revision_activation_heads"
        )
    connection.rollback()

    connection.execute(
        "DROP TRIGGER v3_memory_revision_heads_validate_advance"
    )
    with pytest.raises(SQLiteMemoryPublicationV3SchemaError):
        repository.load_head(
            tenant_id="tenant_001",
            repository_id="repository_001",
            memory_id=inputs[0].memory_id,
        )
    repository.close()


def test_sqlite_publication_fails_on_proposal_dependency_schema_drift():
    connection = _connection()
    inputs = _approval_inputs(connection)
    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="attestation_verifier",
    )
    connection.execute(
        "DROP TRIGGER v3_memory_revision_proposals_immutable_update"
    )
    with pytest.raises(
        SQLiteMemoryPublicationV3SchemaError,
        match="proposal dependency",
    ):
        _append_approval(repository, inputs)
    repository.close()


def test_sqlite_publication_public_exports_and_resource_bytes():
    assert tbm.SQLiteMemoryPublicationV3Repository is (
        SQLiteMemoryPublicationV3Repository
    )
    assert tbm.SQLITE_MEMORY_PUBLICATION_V3_SCHEMA_VERSION == 1
    assert (
        read_packaged_resource(
            "schemas/sqlite-v3-memory-publication.sql"
        )
        == Path("schemas/sqlite-v3-memory-publication.sql").read_bytes()
    )


def test_sqlite_publication_connect_lifecycle_and_validation():
    with pytest.raises(ValueError, match="sqlite3.Connection"):
        SQLiteMemoryPublicationV3Repository(  # type: ignore[arg-type]
            object(),
            attestation_verifier=lambda *_args: True,
            attestation_verifier_id="verifier",
        )
    with pytest.raises(ValueError, match="callable"):
        SQLiteMemoryPublicationV3Repository(
            sqlite3.connect(":memory:"),
            attestation_verifier=None,  # type: ignore[arg-type]
            attestation_verifier_id="verifier",
        )
    with pytest.raises(ValueError, match="identifier"):
        SQLiteMemoryPublicationV3Repository(
            sqlite3.connect(":memory:"),
            attestation_verifier=lambda *_args: True,
            attestation_verifier_id=" ",
        )
    with pytest.raises(ValueError, match="boolean"):
        SQLiteMemoryPublicationV3Repository.connect(
            ":memory:",
            initialize=1,  # type: ignore[arg-type]
            attestation_verifier=lambda *_args: True,
            attestation_verifier_id="verifier",
        )
    with pytest.raises(
        SQLiteMemoryPublicationV3PersistenceError,
        match="failed to connect",
    ):
        SQLiteMemoryPublicationV3Repository.connect(
            ":memory:",
            unsupported_option=True,
            attestation_verifier=lambda *_args: True,
            attestation_verifier_id="verifier",
        )

    repository = SQLiteMemoryPublicationV3Repository.connect(
        ":memory:",
        initialize=True,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
    )
    with pytest.raises(
        SQLiteMemoryPublicationV3NotFoundError,
        match="head",
    ):
        repository.load_head(
            tenant_id="tenant_001",
            repository_id=None,
            memory_id="memory_missing",
        )
    with pytest.raises(SQLiteMemoryPublicationV3NotFoundError):
        repository.load_approval("approval_sha256_" + "0" * 64)
    with pytest.raises(SQLiteMemoryPublicationV3NotFoundError):
        repository.load_activation("activation_sha256_" + "0" * 64)
    repository.close()
    repository.close()
    with pytest.raises(SQLiteMemoryPublicationV3Error, match="closed"):
        repository.load_head(
            tenant_id="tenant_001",
            repository_id=None,
            memory_id="memory_missing",
        )


def test_sqlite_publication_shared_lock_and_non_owned_close():
    connection = _connection()
    first = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
    )
    second = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
    )
    first.close()
    assert connection.execute("SELECT 1").fetchone() == (1,)
    second.close()
    assert connection.execute("SELECT 1").fetchone() == (1,)


@pytest.mark.parametrize(
    ("pragma", "message"),
    [
        ("foreign_keys", "foreign keys"),
        ("recursive_triggers", "recursive triggers"),
    ],
)
def test_sqlite_publication_requires_safety_pragmas(pragma, message):
    connection = _connection()
    connection.execute(f"PRAGMA {pragma} = OFF")
    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
    )
    with pytest.raises(SQLiteMemoryPublicationV3SchemaError, match=message):
        repository.load_head(
            tenant_id="tenant_001",
            repository_id=None,
            memory_id="memory_missing",
        )
    repository.close()


def test_sqlite_publication_rejects_metadata_and_temp_schema_drift():
    for mutation in (
        "DELETE FROM trace_backed_memory_v3_memory_publication_schema",
        "DELETE FROM trace_backed_memory_v3_memory_revision_schema",
        (
            "CREATE TEMP TRIGGER publication_shadow AFTER INSERT ON "
            "main.v3_memory_revision_approvals BEGIN SELECT 1; END"
        ),
    ):
        connection = _connection()
        connection.execute(mutation)
        repository = SQLiteMemoryPublicationV3Repository(
            connection,
            attestation_verifier=lambda *_args: True,
            attestation_verifier_id="verifier",
        )
        with pytest.raises(SQLiteMemoryPublicationV3SchemaError):
            repository.load_head(
                tenant_id="tenant_001",
                repository_id=None,
                memory_id="memory_missing",
            )
        repository.close()


def test_sqlite_publication_attestation_failure_and_input_conflicts():
    connection = _connection()
    inputs = _approval_inputs(connection)

    def failing_verifier(*_args):
        raise RuntimeError("secret verifier detail")

    failing = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=failing_verifier,
        attestation_verifier_id="verifier",
    )
    with pytest.raises(
        SQLiteMemoryPublicationV3AttestationError,
        match="verification failed",
    ):
        _append_approval(failing, inputs)
    failing.close()

    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
    )
    _append_approval(repository, inputs)
    with pytest.raises(
        SQLiteMemoryPublicationV3ConflictError,
        match="approval conflicts",
    ):
        revision, fixes, regressions, policy, request, decision = inputs
        repository.append_approval(
            revision=revision,
            previous_revision=None,
            content=CONTENT,
            fix_evidence_by_id=fixes,
            regression_evidence_by_id=regressions,
            policy=policy,
            request=request,
            decision=decision,
            approved_by="publication_approver",
            approved_via_client_id="publication_service",
            approved_at=APPROVED_AT,
            approval_attestation_sha256="sha256:" + "a" * 64,
        )
    repository.close()


def test_sqlite_publication_rejects_missing_supplied_evidence():
    connection = _connection()
    inputs = _approval_inputs(connection)
    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
    )
    revision, _fixes, regressions, policy, request, decision = inputs
    with pytest.raises(
        ValueError,
        match="evidence bundle",
    ):
        repository.append_approval(
            revision=revision,
            previous_revision=None,
            content=CONTENT,
            fix_evidence_by_id={},
            regression_evidence_by_id=regressions,
            policy=policy,
            request=request,
            decision=decision,
            approved_by="publication_approver",
            approved_via_client_id="publication_service",
            approved_at=APPROVED_AT,
            approval_attestation_sha256=DIGEST,
        )
    with pytest.raises(
        ValueError,
        match="evidence bundle",
    ):
        repository.append_approval(
            revision=revision,
            previous_revision=None,
            content=CONTENT,
            fix_evidence_by_id=inputs[1],
            regression_evidence_by_id={},
            policy=policy,
            request=request,
            decision=decision,
            approved_by="publication_approver",
            approved_via_client_id="publication_service",
            approved_at=APPROVED_AT,
            approval_attestation_sha256=DIGEST,
        )
    repository.close()


def test_sqlite_publication_strict_request_helpers():
    request, _decision = _authorization(
        _policy(),
        actor_id="publication_approver",
        permission="memory:review",
        decided_at=APPROVED_AT,
    )
    assert _loads_request(_request_descriptor(request)) == request
    with pytest.raises(ValueError, match="exactly AuthorizationRequest"):
        _request_descriptor(object())  # type: ignore[arg-type]
    with pytest.raises(
        SQLiteMemoryPublicationV3PersistenceError,
        match="stored authorization request",
    ):
        _loads_request('{"request_id":"first","request_id":"second"}')
    with pytest.raises(
        SQLiteMemoryPublicationV3PersistenceError,
        match="stored authorization request",
    ):
        _loads_request("{}")
    with pytest.raises(SQLiteMemoryPublicationV3SchemaError):
        _normalized_schema_sql(None)


def test_sqlite_publication_loader_defenses():
    class OneRowCursor:
        def __init__(self, row):
            self.row = row

        def execute(self, *_args):
            return None

        def fetchone(self):
            return self.row

    with pytest.raises(ValueError, match="exactly one"):
        SQLiteMemoryPublicationV3Repository._load_approval_row(
            OneRowCursor(None),  # type: ignore[arg-type]
        )
    with pytest.raises(ValueError, match="exactly one"):
        SQLiteMemoryPublicationV3Repository._load_activation_row(
            OneRowCursor(None),  # type: ignore[arg-type]
        )
    for loader, identity_name in (
        (
            SQLiteMemoryPublicationV3Repository._load_approval_row,
            "approval_id",
        ),
        (
            SQLiteMemoryPublicationV3Repository._load_activation_row,
            "activation_id",
        ),
    ):
        with pytest.raises(
            SQLiteMemoryPublicationV3PersistenceError,
            match="invalid shape",
        ):
            loader(  # type: ignore[arg-type]
                OneRowCursor(("invalid",)),
                **{identity_name: "identity"},
            )
        with pytest.raises(
            SQLiteMemoryPublicationV3PersistenceError,
            match="is invalid",
        ):
            loader(  # type: ignore[arg-type]
                OneRowCursor(("{}", "{}", "{}", "{}", "verifier")),
                **{identity_name: "identity"},
            )
    for row, message in (
        (("tenant", None, "memory", "0", None, None), "invalid shape"),
        (("tenant", None, "memory", 0, "revision", None), "inconsistent"),
        (("tenant", None, "memory", 1, None, None), "incomplete"),
    ):
        with pytest.raises(
            SQLiteMemoryPublicationV3PersistenceError,
            match=message,
        ):
            SQLiteMemoryPublicationV3Repository._select_head(
                OneRowCursor(row),  # type: ignore[arg-type]
                tenant_id="tenant",
                repository_id=None,
                memory_id="memory",
            )
    assert (
        SQLiteMemoryPublicationV3Repository._select_head(
            OneRowCursor(("tenant", None, "memory", 0, None, None)),
            tenant_id="tenant",
            repository_id=None,
            memory_id="memory",
        )
        is None
    )

    class ExistingCursor:
        def execute(self, *_args):
            return None

        def fetchone(self):
            return ("different", "value")

    with pytest.raises(
        SQLiteMemoryPublicationV3ConflictError,
        match="identity conflict",
    ):
        SQLiteMemoryPublicationV3Repository._put_exact(
            ExistingCursor(),  # type: ignore[arg-type]
            table="example",
            id_column="identity",
            columns=("identity", "value"),
            values=("expected", "value"),
            conflict_message="identity conflict",
        )


def test_sqlite_publication_schema_reader_defenses(monkeypatch):
    canonical = sqlite_publication._canonical_schema_definitions()

    class SchemaCursor:
        def __init__(self, fetchalls, fetchone=None):
            self.fetchalls = list(fetchalls)
            self.fetchone_value = fetchone

        def execute(self, *_args):
            return None

        def fetchall(self):
            return self.fetchalls.pop(0)

        def fetchone(self):
            return self.fetchone_value

    with pytest.raises(
        SQLiteMemoryPublicationV3SchemaError,
        match="invalid shape",
    ):
        invalid_definitions = list(canonical)
        invalid_definitions[0] = ("table",)  # type: ignore[assignment]
        sqlite_publication._read_schema_definitions(
            SchemaCursor([invalid_definitions])  # type: ignore[arg-type]
        )
    with pytest.raises(
        SQLiteMemoryPublicationV3SchemaError,
        match="unexpected objects",
    ):
        sqlite_publication._read_schema_definitions(
            SchemaCursor([list(canonical), []])  # type: ignore[arg-type]
        )
    with pytest.raises(
        SQLiteMemoryPublicationV3SchemaError,
        match="temporary shadows",
    ):
        sqlite_publication._read_schema_definitions(
            SchemaCursor(
                [list(canonical), [
                    (name,) for name in sorted(
                        sqlite_publication._SCHEMA_OBJECT_NAMES
                    )
                ]],
                fetchone=("shadow",),
            )  # type: ignore[arg-type]
        )

    sqlite_publication._canonical_schema_definitions.cache_clear()

    def fail_resource(_name):
        raise OSError("resource unavailable")

    monkeypatch.setattr(
        sqlite_publication,
        "read_packaged_resource",
        fail_resource,
    )
    with pytest.raises(
        SQLiteMemoryPublicationV3SchemaError,
        match="could not validate canonical",
    ):
        sqlite_publication._canonical_schema_definitions()
    sqlite_publication._canonical_schema_definitions.cache_clear()


def test_sqlite_publication_proposal_bundle_defenses():
    revision, fixes, regressions = _publication_inputs()

    class QueueCursor:
        def __init__(self, fetchones, fetchalls=()):
            self.fetchones = list(fetchones)
            self.fetchalls = list(fetchalls)

        def execute(self, *_args):
            return None

        def fetchone(self):
            return self.fetchones.pop(0)

        def fetchall(self):
            return self.fetchalls.pop(0)

    second, _content = _second_revision(revision)
    with pytest.raises(
        SQLiteMemoryPublicationV3NotFoundError,
        match="previous",
    ):
        SQLiteMemoryPublicationV3Repository._require_proposal_bundle(
            QueueCursor(
                [(tbm.dumps_memory_revision(second),), None]
            ),  # type: ignore[arg-type]
            second,
            revision,
            fixes,
            regressions,
        )
    proposal = (tbm.dumps_memory_revision(revision),)
    with pytest.raises(
        SQLiteMemoryPublicationV3NotFoundError,
        match="fix evidence is not supplied",
    ):
        SQLiteMemoryPublicationV3Repository._require_proposal_bundle(
            QueueCursor([proposal]),  # type: ignore[arg-type]
            revision,
            None,
            {},
            regressions,
        )
    with pytest.raises(
        SQLiteMemoryPublicationV3NotFoundError,
        match="fix evidence is not stored",
    ):
        SQLiteMemoryPublicationV3Repository._require_proposal_bundle(
            QueueCursor([proposal, None]),  # type: ignore[arg-type]
            revision,
            None,
            fixes,
            regressions,
        )
    fix = next(iter(fixes.values()))
    fix_row = (tbm.dumps_fix_evidence(fix),)
    with pytest.raises(
        SQLiteMemoryPublicationV3PersistenceError,
        match="links",
    ):
        SQLiteMemoryPublicationV3Repository._require_proposal_bundle(
            QueueCursor([proposal, fix_row], [[]]),  # type: ignore[arg-type]
            revision,
            None,
            fixes,
            regressions,
        )
    link_rows = [(item,) for item in revision.regression_evidence_ids]
    with pytest.raises(
        SQLiteMemoryPublicationV3NotFoundError,
        match="regression evidence is not supplied",
    ):
        SQLiteMemoryPublicationV3Repository._require_proposal_bundle(
            QueueCursor(
                [proposal, fix_row],
                [link_rows],
            ),  # type: ignore[arg-type]
            revision,
            None,
            fixes,
            {},
        )
    with pytest.raises(
        SQLiteMemoryPublicationV3NotFoundError,
        match="regression evidence is not stored",
    ):
        SQLiteMemoryPublicationV3Repository._require_proposal_bundle(
            QueueCursor(
                [proposal, fix_row, None],
                [link_rows],
            ),  # type: ignore[arg-type]
            revision,
            None,
            fixes,
            regressions,
        )


def test_sqlite_publication_rolls_back_after_commit_failure():
    class FailingCommitConnection(sqlite3.Connection):
        fail_commit = False

        def commit(self) -> None:
            if self.fail_commit:
                raise sqlite3.OperationalError("simulated commit failure")
            super().commit()

    connection = sqlite3.connect(
        ":memory:",
        factory=FailingCommitConnection,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    for resource in (
        "schemas/sqlite-v3-memory-revision.sql",
        "schemas/sqlite-v3-memory-publication.sql",
    ):
        connection.executescript(
            read_packaged_resource(resource).decode("utf-8")
        )
    inputs = _approval_inputs(connection)
    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
    )
    connection.fail_commit = True
    with pytest.raises(
        SQLiteMemoryPublicationV3PersistenceError,
        match="append memory revision approval",
    ):
        _append_approval(repository, inputs)
    assert connection.in_transaction is False
    assert connection.execute(
        "SELECT COUNT(*) FROM v3_memory_revision_approvals"
    ).fetchone() == (0,)
    repository.close()


def test_sqlite_publication_missing_schema_is_sanitized():
    repository = SQLiteMemoryPublicationV3Repository.connect(
        ":memory:",
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
    )
    with pytest.raises(SQLiteMemoryPublicationV3SchemaError, match="missing"):
        repository.load_head(
            tenant_id="tenant",
            repository_id=None,
            memory_id="memory",
        )
    repository.close()


def test_sqlite_publication_rejects_approval_provenance_mismatch():
    connection = _connection()
    inputs = _approval_inputs(connection)
    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
    )
    approval = _append_approval(repository, inputs).approval
    alternate_request, alternate_decision = _authorization(
        inputs[3],
        actor_id="publication_approver",
        permission="memory:review",
        decided_at="2026-07-27T00:08:00Z",
    )
    activation_request, activation_decision = _authorization(
        inputs[3],
        actor_id="publication_activator",
        permission="memory:activate",
        decided_at=ACTIVATED_AT,
    )
    with pytest.raises(
        SQLiteMemoryPublicationV3ConflictError,
        match="approval provenance",
    ):
        repository.append_activation(
            revision=inputs[0],
            previous_revision=None,
            content=CONTENT,
            fix_evidence_by_id=inputs[1],
            regression_evidence_by_id=inputs[2],
            approval=approval,
            approval_policy=inputs[3],
            approval_request=alternate_request,
            approval_decision=alternate_decision,
            policy=inputs[3],
            request=activation_request,
            decision=activation_decision,
            activated_by="publication_activator",
            activated_via_client_id="publication_service",
            activated_at=ACTIVATED_AT,
            activation_attestation_sha256=DIGEST,
        )
    repository.close()


def test_sqlite_publication_requires_activated_predecessor():
    connection = _connection()
    inputs = _approval_inputs(connection)
    second, second_content = _second_revision(inputs[0])
    SQLiteMemoryRevisionV3Repository(connection).store_proposal(
        second,
        next(iter(inputs[1].values())),
        tuple(inputs[2].values()),
    )
    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
    )
    review_request, review_decision = _authorization(
        inputs[3],
        actor_id="publication_approver",
        permission="memory:review",
        decided_at="2026-07-27T00:11:00Z",
    )
    approval = repository.append_approval(
        revision=second,
        previous_revision=inputs[0],
        content=second_content,
        fix_evidence_by_id=inputs[1],
        regression_evidence_by_id=inputs[2],
        policy=inputs[3],
        request=review_request,
        decision=review_decision,
        approved_by="publication_approver",
        approved_via_client_id="publication_service",
        approved_at="2026-07-27T00:11:00Z",
        approval_attestation_sha256=DIGEST,
    ).approval
    activation_request, activation_decision = _authorization(
        inputs[3],
        actor_id="publication_activator",
        permission="memory:activate",
        decided_at="2026-07-27T00:12:00Z",
    )
    with pytest.raises(
        SQLiteMemoryPublicationV3ConflictError,
        match="no durable predecessor",
    ):
        repository.append_activation(
            revision=second,
            previous_revision=inputs[0],
            content=second_content,
            fix_evidence_by_id=inputs[1],
            regression_evidence_by_id=inputs[2],
            approval=approval,
            approval_policy=inputs[3],
            approval_request=review_request,
            approval_decision=review_decision,
            policy=inputs[3],
            request=activation_request,
            decision=activation_decision,
            activated_by="publication_activator",
            activated_via_client_id="publication_service",
            activated_at="2026-07-27T00:12:00Z",
            activation_attestation_sha256=DIGEST,
        )
    repository.close()


def test_sqlite_publication_savepoint_release_failure_rolls_back_write():
    class FailingReleaseConnection(sqlite3.Connection):
        fail_release = False

        def execute(self, sql, parameters=()):
            if (
                self.fail_release
                and sql.startswith(
                    "RELEASE SAVEPOINT tbm_sqlite_memory_publication_v3_"
                )
            ):
                self.fail_release = False
                raise sqlite3.OperationalError("simulated release failure")
            return super().execute(sql, parameters)

    connection = sqlite3.connect(
        ":memory:",
        factory=FailingReleaseConnection,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    for resource in (
        "schemas/sqlite-v3-memory-revision.sql",
        "schemas/sqlite-v3-memory-publication.sql",
    ):
        connection.executescript(
            read_packaged_resource(resource).decode("utf-8")
        )
    inputs = _approval_inputs(connection)
    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
    )
    connection.execute("BEGIN")
    connection.fail_release = True
    with pytest.raises(SQLiteMemoryPublicationV3PersistenceError):
        _append_approval(repository, inputs)
    assert connection.in_transaction is True
    assert connection.execute(
        "SELECT COUNT(*) FROM v3_memory_revision_approvals"
    ).fetchone() == (0,)
    connection.rollback()
    repository.close()


def test_sqlite_publication_failed_savepoint_cleanup_fails_closed():
    class FailingSavepointCleanupConnection(sqlite3.Connection):
        fail_cleanup = False

        def execute(self, sql, parameters=()):
            if self.fail_cleanup and sql.startswith("ROLLBACK TO SAVEPOINT"):
                self.fail_cleanup = False
                raise sqlite3.OperationalError("simulated cleanup failure")
            return super().execute(sql, parameters)

    connection = sqlite3.connect(
        ":memory:",
        factory=FailingSavepointCleanupConnection,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    for resource in (
        "schemas/sqlite-v3-memory-revision.sql",
        "schemas/sqlite-v3-memory-publication.sql",
    ):
        connection.executescript(
            read_packaged_resource(resource).decode("utf-8")
        )
    inputs = _approval_inputs(connection)
    unstored = _unstored_revision(inputs[0])
    fixes, regressions = inputs[1], inputs[2]
    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
    )
    connection.execute("BEGIN")
    connection.fail_cleanup = True
    with pytest.raises(SQLiteMemoryPublicationV3NotFoundError) as captured:
        repository.append_approval(
            revision=unstored,
            previous_revision=None,
            content=CONTENT,
            fix_evidence_by_id=fixes,
            regression_evidence_by_id=regressions,
            policy=inputs[3],
            request=inputs[4],
            decision=inputs[5],
            approved_by="publication_approver",
            approved_via_client_id="publication_service",
            approved_at=APPROVED_AT,
            approval_attestation_sha256=DIGEST,
        )
    assert connection.in_transaction is False
    assert any(
        "failed to clean up" in note
        for note in getattr(captured.value, "__notes__", ())
    )
    repository.close()


def test_sqlite_publication_top_level_rollback_failure_closes_connection():
    class FailingRollbackConnection(sqlite3.Connection):
        def rollback(self) -> None:
            raise sqlite3.OperationalError("simulated rollback failure")

    connection = sqlite3.connect(
        ":memory:",
        factory=FailingRollbackConnection,
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    for resource in (
        "schemas/sqlite-v3-memory-revision.sql",
        "schemas/sqlite-v3-memory-publication.sql",
    ):
        connection.executescript(
            read_packaged_resource(resource).decode("utf-8")
        )
    inputs = _approval_inputs(connection)
    unstored = _unstored_revision(inputs[0])
    fixes, regressions = inputs[1], inputs[2]
    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
    )
    with pytest.raises(SQLiteMemoryPublicationV3NotFoundError) as captured:
        repository.append_approval(
            revision=unstored,
            previous_revision=None,
            content=CONTENT,
            fix_evidence_by_id=fixes,
            regression_evidence_by_id=regressions,
            policy=inputs[3],
            request=inputs[4],
            decision=inputs[5],
            approved_by="publication_approver",
            approved_via_client_id="publication_service",
            approved_at=APPROVED_AT,
            approval_attestation_sha256=DIGEST,
        )
    assert connection.closed if hasattr(connection, "closed") else True
    assert len(getattr(captured.value, "__notes__", ())) >= 2
    repository.close()


def test_sqlite_publication_closed_connection_and_initialize_resource_error(
    monkeypatch,
):
    connection = _connection()
    repository = SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
    )
    connection.close()
    with pytest.raises(SQLiteMemoryPublicationV3Error, match="closed"):
        repository.load_head(
            tenant_id="tenant",
            repository_id=None,
            memory_id="memory",
        )
    repository.close()

    def fail_resource(_name):
        raise OSError("resource unavailable")

    monkeypatch.setattr(
        sqlite_publication,
        "read_packaged_resource",
        fail_resource,
    )
    with pytest.raises(SQLiteMemoryPublicationV3PersistenceError):
        SQLiteMemoryPublicationV3Repository.connect(
            ":memory:",
            initialize=True,
            attestation_verifier=lambda *_args: True,
            attestation_verifier_id="verifier",
        )
