from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from pathlib import Path
from threading import Barrier
from types import SimpleNamespace

import pytest

import trace_backed_memory as tbm
from tests.test_memory_publication_v3 import (
    ACTIVATED_AT,
    APPROVED_AT,
    CONTENT,
    DIGEST,
    _authorization,
    _policy,
    _publication_inputs,
)
from tests.test_artifact_service_v3 import _Provider, _context, _registry
from tests.postgres_support import PostgresCluster
from trace_backed_memory.postgres import _load_psycopg
from trace_backed_memory.postgres_memory_publication_v3 import (
    PostgresMemoryPublicationV3AttestationError,
    PostgresMemoryPublicationV3ConflictError,
    PostgresMemoryPublicationV3Error,
    PostgresMemoryPublicationV3NotFoundError,
    PostgresMemoryPublicationV3PersistenceError,
    PostgresMemoryPublicationV3SchemaError,
    PostgresMemoryPublicationV3Repository,
    _EXPECTED_CATALOG_SHA256,
    _PUBLICATION_CATALOG_SHA256_QUERY,
    _loads_request,
    _request_descriptor,
)
from trace_backed_memory.postgres_memory_revision_v3 import (
    PostgresMemoryRevisionV3Repository,
)
from trace_backed_memory.memory_revision_v3 import build_memory_revision
from trace_backed_memory.replay_v3 import create_content_addressed_artifact


ROOT = Path(__file__).resolve().parents[1]
REVISION_INSTALL = ROOT / "schemas" / "postgres-v3-memory-revision.sql"
INSTALL = ROOT / "schemas" / "postgres-v3-memory-publication.sql"
AUTHORIZATION_INSTALL = ROOT / "schemas" / "postgres-v3-authorization.sql"
ARTIFACT_INSTALL = ROOT / "schemas" / "postgres-v3-artifact-authority.sql"
ROLLBACK = ROOT / "schemas" / "postgres-v3-memory-publication-rollback.sql"
SCHEMA = "trace_backed_memory_v3_memory_publication"


def _install(postgres_cluster: PostgresCluster) -> None:
    postgres_cluster.load_schema()
    result = postgres_cluster.run_script(REVISION_INSTALL)
    assert result.returncode == 0, result.stderr
    result = postgres_cluster.run_script(INSTALL)
    assert result.returncode == 0, result.stderr


def _inputs(postgres_cluster: PostgresCluster):
    revision, fixes, regressions = _publication_inputs()
    with PostgresMemoryRevisionV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    ) as proposals:
        proposals.store_proposal(
            revision,
            next(iter(fixes.values())),
            tuple(regressions.values()),
        )
    policy = _policy()
    request, decision = _authorization(
        policy,
        actor_id="publication_approver",
        permission="memory:review",
        decided_at=APPROVED_AT,
    )
    return revision, fixes, regressions, policy, request, decision


def _append_approval(repository, inputs):
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


def _append_activation(repository, inputs, approval):
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


def test_postgres_memory_publication_catalog_fingerprint(
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
                _PUBLICATION_CATALOG_SHA256_QUERY,
                (SCHEMA,) * 7,
            )
            fingerprint = cursor.fetchone()["catalog_sha256"]
    assert fingerprint == _EXPECTED_CATALOG_SHA256


def test_postgres_memory_publication_resources_are_packaged() -> None:
    for name in (
        "schemas/postgres-v3-memory-publication.sql",
        "schemas/postgres-v3-memory-publication-rollback.sql",
    ):
        assert tbm.read_packaged_resource(name) == (ROOT / name).read_bytes()


def test_postgres_memory_publication_round_trip_replay_and_head(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    inputs = _inputs(postgres_cluster)
    with PostgresMemoryPublicationV3Repository.connect(
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="attestation_verifier",
        **postgres_cluster.connection_kwargs(),
    ) as repository:
        approval = _append_approval(repository, inputs)
        replay_approval = _append_approval(repository, inputs)
        activation = _append_activation(
            repository,
            inputs,
            approval.approval,
        )
        replay_activation = _append_activation(
            repository,
            inputs,
            approval.approval,
        )

        assert approval.inserted is True
        assert replay_approval.inserted is False
        assert activation.inserted is True
        assert replay_activation.inserted is False
        assert repository.load_approval(
            approval.approval.approval_id
        ).approval == approval.approval
        assert repository.load_activation(
            activation.activation.activation_id
        ).activation == activation.activation
        approval_bundle = repository.load_approval_bundle(
            approval.approval.approval_id
        )
        assert approval_bundle.approval == approval.approval
        assert approval_bundle.policy.policy_sha256 == inputs[3].policy_sha256
        assert approval_bundle.request == inputs[4]
        assert approval_bundle.decision == inputs[5]
        assert approval_bundle.attestation_verified_by == "attestation_verifier"
        activation_bundle = repository.load_activation_bundle(
            activation.activation.activation_id
        )
        assert activation_bundle.activation == activation.activation
        assert activation_bundle.policy.policy_sha256 == inputs[3].policy_sha256
        assert activation_bundle.attestation_verified_by == "attestation_verifier"
        assert repository.load_head(
            tenant_id="tenant_001",
            repository_id="repository_001",
            memory_id=inputs[0].memory_id,
        ) == tbm.PostgresMemoryPublicationV3Head(
            tenant_id="tenant_001",
            repository_id="repository_001",
            memory_id=inputs[0].memory_id,
            current_revision_number=1,
            current_revision_id=inputs[0].revision_id,
            current_activation_id=activation.activation.activation_id,
        )
        with pytest.raises(
            PostgresMemoryPublicationV3ConflictError,
            match="approval identity conflict",
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


def test_activated_revision_source_reads_postgres_publication_head(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    for script in (AUTHORIZATION_INSTALL, ARTIFACT_INSTALL):
        result = postgres_cluster.run_script(script)
        assert result.returncode == 0, result.stderr
    base, fixes, regressions = _publication_inputs()
    artifact = create_content_addressed_artifact(
        CONTENT,
        media_type="application/json",
        classification="restricted",
        created_at=base.content_artifact.created_at,
        encryption_key_id="key_001",
    )
    revision = build_memory_revision(
        memory_id=base.memory_id,
        memory_kind=base.memory_kind,
        revision_number=base.revision_number,
        previous_revision_id=base.previous_revision_id,
        memory_type=base.memory_type,
        content_artifact=artifact,
        scope=base.scope,
        confidence=base.confidence,
        sensitive=True,
        eval_leaking=base.eval_leaking,
        source_case_id=base.source_case_id,
        source_case_revision_id=base.source_case_revision_id,
        fix_evidence_id=base.fix_evidence_id,
        regression_evidence_ids=base.regression_evidence_ids,
        proposed_by=base.proposed_by,
        proposed_via_client_id=base.proposed_via_client_id,
        proposed_at=base.proposed_at,
        proposal_attestation_sha256=base.proposal_attestation_sha256,
    )
    policy = _policy()
    review_request, review_decision = _authorization(
        policy,
        actor_id="publication_approver",
        permission="memory:review",
        decided_at=APPROVED_AT,
    )
    inputs = (
        revision,
        fixes,
        regressions,
        policy,
        review_request,
        review_decision,
    )
    runtime_registry = _registry(
        permissions=("memory:retrieve", "artifact:read", "artifact:write")
    )
    request_number = iter(range(1, 100))
    with PostgresMemoryRevisionV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    ) as proposals, PostgresMemoryPublicationV3Repository.connect(
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="attestation_verifier",
        **postgres_cluster.connection_kwargs(),
    ) as publication, tbm.PostgresAuthorizationV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    ) as authorizations, tbm.PostgresArtifactV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    ) as artifact_repository:
        proposals.store_proposal(
            revision,
            next(iter(fixes.values())),
            tuple(regressions.values()),
        )
        approval = _append_approval(publication, inputs).approval
        activation = _append_activation(
            publication, inputs, approval
        ).activation
        authorization = tbm.AuthenticatedRetrievalService(
            registry_provider=lambda: runtime_registry,
            decision_writer=authorizations,
            clock=lambda: "2026-07-29T00:00:00Z",
            request_id_factory=lambda: (
                f"postgres_activated_request_{next(request_number):03d}"
            ),
        )
        artifact_service = tbm.AuthenticatedArtifactService(
            authorization_service=authorization,
            authority=artifact_repository,
            encryption_provider=_Provider(),
            clock=lambda: "2026-07-29T00:00:00Z",
        )
        artifact_service.put(
            _context(runtime_registry), revision.content_artifact, CONTENT
        )
        source = tbm.ActivatedRevisionSource(
            authorization_service=authorization,
            proposal_reader=proposals,
            publication_reader=publication,
            artifact_reader=artifact_service,
            trusted_attestation_verifier_ids=("attestation_verifier",),
        )
        candidate = source.load_current(
            _context(runtime_registry),
            memory_id=revision.memory_id,
            repository_id="repository_001",
        )
        assert candidate.revision == revision
        assert candidate.activation == activation
        assert candidate.content == CONTENT


def test_postgres_memory_publication_advances_from_locked_durable_head(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    inputs = _inputs(postgres_cluster)
    with PostgresMemoryPublicationV3Repository.connect(
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="attestation_verifier",
        **postgres_cluster.connection_kwargs(),
    ) as repository:
        first_approval = _append_approval(repository, inputs).approval
        first_activation = _append_activation(
            repository,
            inputs,
            first_approval,
        ).activation
        second, second_content = _second_revision(inputs[0])
        with PostgresMemoryRevisionV3Repository.connect(
            **postgres_cluster.connection_kwargs()
        ) as proposals:
            proposals.store_proposal(
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


def test_postgres_memory_publication_concurrent_exact_replay_is_idempotent(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    inputs = _inputs(postgres_cluster)
    approval_barrier = Barrier(2)

    def append_approval_once():
        def verify(*_args):
            approval_barrier.wait(timeout=10)
            return True

        with PostgresMemoryPublicationV3Repository.connect(
            attestation_verifier=verify,
            attestation_verifier_id="attestation_verifier",
            **postgres_cluster.connection_kwargs(),
        ) as repository:
            return _append_approval(repository, inputs)

    with ThreadPoolExecutor(max_workers=2) as executor:
        approval_results = tuple(
            executor.map(lambda _index: append_approval_once(), range(2))
        )
    assert sorted(result.inserted for result in approval_results) == [
        False,
        True,
    ]
    approval = approval_results[0].approval
    activation_barrier = Barrier(2)

    def append_activation_once():
        def verify(*_args):
            activation_barrier.wait(timeout=10)
            return True

        with PostgresMemoryPublicationV3Repository.connect(
            attestation_verifier=verify,
            attestation_verifier_id="attestation_verifier",
            **postgres_cluster.connection_kwargs(),
        ) as repository:
            return _append_activation(repository, inputs, approval)

    with ThreadPoolExecutor(max_workers=2) as executor:
        activation_results = tuple(
            executor.map(lambda _index: append_activation_once(), range(2))
        )
    assert sorted(result.inserted for result in activation_results) == [
        False,
        True,
    ]
    assert (
        activation_results[0].activation
        == activation_results[1].activation
    )

    second, second_content = _second_revision(inputs[0])
    with PostgresMemoryRevisionV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    ) as proposals:
        proposals.store_proposal(
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
    with PostgresMemoryPublicationV3Repository.connect(
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="attestation_verifier",
        **postgres_cluster.connection_kwargs(),
    ) as repository:
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
    second_barrier = Barrier(2)

    def append_second_activation_once():
        def verify(*_args):
            second_barrier.wait(timeout=10)
            return True

        with PostgresMemoryPublicationV3Repository.connect(
            attestation_verifier=verify,
            attestation_verifier_id="attestation_verifier",
            **postgres_cluster.connection_kwargs(),
        ) as repository:
            return repository.append_activation(
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

    with ThreadPoolExecutor(max_workers=2) as executor:
        second_results = tuple(
            executor.map(
                lambda _index: append_second_activation_once(),
                range(2),
            )
        )
    assert sorted(result.inserted for result in second_results) == [
        False,
        True,
    ]
    assert second_results[0].activation == second_results[1].activation


def test_postgres_memory_publication_attestation_rejects_without_write(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    inputs = _inputs(postgres_cluster)
    with PostgresMemoryPublicationV3Repository.connect(
        attestation_verifier=lambda *_args: False,
        attestation_verifier_id="attestation_verifier",
        **postgres_cluster.connection_kwargs(),
    ) as repository:
        with pytest.raises(
            PostgresMemoryPublicationV3AttestationError,
            match="not verified",
        ):
            _append_approval(repository, inputs)
    assert postgres_cluster.run(
        "SELECT count(*) FROM "
        "trace_backed_memory_v3_memory_publication."
        "v3_memory_revision_approvals"
    ).stdout.strip() == "0"


def test_postgres_memory_publication_direct_mutation_and_drift_fail_closed(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    inputs = _inputs(postgres_cluster)
    with PostgresMemoryPublicationV3Repository.connect(
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="attestation_verifier",
        **postgres_cluster.connection_kwargs(),
    ) as repository:
        approval = _append_approval(repository, inputs)
        activation = _append_activation(
            repository,
            inputs,
            approval.approval,
        )
        mutation = postgres_cluster.run(
            "UPDATE trace_backed_memory_v3_memory_publication."
            "v3_memory_revision_activations SET memory_id = 'changed'"
        )
        assert mutation.returncode != 0
        assert "immutable" in mutation.stderr
        drift = postgres_cluster.run(
            "ALTER FUNCTION "
            "trace_backed_memory_v3_memory_publication."
            "validate_head_advance() RENAME TO validate_head_advance_drift"
        )
        assert drift.returncode == 0, drift.stderr
        with pytest.raises(PostgresMemoryPublicationV3SchemaError):
            repository.load_activation(
                activation.activation.activation_id
            )


def test_postgres_memory_publication_rollback_restricts_external_dependency(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    result = postgres_cluster.run(
        "CREATE TABLE public.publication_external_ref ("
        "approval_id text PRIMARY KEY REFERENCES "
        "trace_backed_memory_v3_memory_publication."
        "v3_memory_revision_approvals(approval_id))"
    )
    assert result.returncode == 0, result.stderr
    rollback = postgres_cluster.run_script(ROLLBACK)
    assert rollback.returncode != 0
    assert "depend" in rollback.stderr.lower()
    assert postgres_cluster.run(
        "SELECT to_regnamespace("
        "'trace_backed_memory_v3_memory_publication') IS NOT NULL"
    ).stdout.strip() == "t"
    assert postgres_cluster.run(
        "DROP TABLE public.publication_external_ref"
    ).returncode == 0
    retry = postgres_cluster.run_script(ROLLBACK)
    assert retry.returncode == 0, retry.stderr


def test_postgres_memory_publication_rollback_is_dependency_safe(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    result = postgres_cluster.run_script(ROLLBACK)
    assert result.returncode == 0, result.stderr
    assert postgres_cluster.run(
        "SELECT to_regnamespace("
        "'trace_backed_memory_v3_memory_publication') IS NULL"
    ).stdout.strip() == "t"


def test_postgres_memory_publication_constructor_connect_and_lifecycle(
    postgres_cluster: PostgresCluster,
) -> None:
    with pytest.raises(ValueError, match="connection"):
        PostgresMemoryPublicationV3Repository(
            None,
            attestation_verifier=lambda *_args: True,
            attestation_verifier_id="verifier",
        )
    with pytest.raises(ValueError, match="callable"):
        PostgresMemoryPublicationV3Repository(
            object(),
            attestation_verifier=None,  # type: ignore[arg-type]
            attestation_verifier_id="verifier",
        )
    with pytest.raises(ValueError, match="identifier"):
        PostgresMemoryPublicationV3Repository(
            object(),
            attestation_verifier=lambda *_args: True,
            attestation_verifier_id=" ",
        )
    with pytest.raises(
        PostgresMemoryPublicationV3PersistenceError,
        match="failed to connect",
    ):
        PostgresMemoryPublicationV3Repository.connect(
            "postgresql://invalid.invalid.invalid:1/missing",
            connect_timeout=1,
            attestation_verifier=lambda *_args: True,
            attestation_verifier_id="verifier",
        )

    _install(postgres_cluster)
    repository = PostgresMemoryPublicationV3Repository.connect(
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
        **postgres_cluster.connection_kwargs(),
    )
    with pytest.raises(PostgresMemoryPublicationV3NotFoundError):
        repository.load_approval("approval_sha256_" + "0" * 64)
    with pytest.raises(PostgresMemoryPublicationV3NotFoundError):
        repository.load_activation("activation_sha256_" + "0" * 64)
    with pytest.raises(PostgresMemoryPublicationV3NotFoundError):
        repository.load_head(
            tenant_id="tenant_001",
            repository_id=None,
            memory_id="memory_missing",
        )
    repository.close()
    repository.close()
    with pytest.raises(PostgresMemoryPublicationV3Error, match="closed"):
        repository.load_head(
            tenant_id="tenant_001",
            repository_id=None,
            memory_id="memory_missing",
        )


def test_postgres_memory_publication_attestation_callback_failure(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    inputs = _inputs(postgres_cluster)

    def failing_verifier(*_args):
        raise RuntimeError("secret verifier detail")

    with PostgresMemoryPublicationV3Repository.connect(
        attestation_verifier=failing_verifier,
        attestation_verifier_id="verifier",
        **postgres_cluster.connection_kwargs(),
    ) as repository:
        with pytest.raises(
            PostgresMemoryPublicationV3AttestationError,
            match="verification failed",
        ):
            _append_approval(repository, inputs)


@pytest.mark.parametrize(
    "mutation",
    [
        (
            "ALTER TABLE trace_backed_memory_v3_memory_publication."
            "schema_metadata DISABLE TRIGGER "
            "memory_publication_metadata_immutable; "
            "DELETE FROM trace_backed_memory_v3_memory_publication."
            "schema_metadata"
        ),
        (
            "DROP TRIGGER memory_revision_proposal_immutable ON "
            "trace_backed_memory_v3_memory_revision."
            "v3_memory_revision_proposals"
        ),
        (
            "DELETE FROM public.trace_backed_memory_schema"
        ),
        (
            "ALTER TABLE trace_backed_memory_v3_memory_revision."
            "schema_metadata DISABLE TRIGGER "
            "memory_revision_metadata_immutable; "
            "DELETE FROM trace_backed_memory_v3_memory_revision."
            "schema_metadata"
        ),
        (
            "CREATE VIEW trace_backed_memory_v3_memory_publication."
            "unsupported_view AS SELECT 1 AS value"
        ),
    ],
)
def test_postgres_memory_publication_dependency_and_metadata_drift(
    postgres_cluster: PostgresCluster,
    mutation: str,
) -> None:
    _install(postgres_cluster)
    assert postgres_cluster.run(mutation).returncode == 0
    with PostgresMemoryPublicationV3Repository.connect(
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
        **postgres_cluster.connection_kwargs(),
    ) as repository:
        with pytest.raises(PostgresMemoryPublicationV3SchemaError):
            repository.load_head(
                tenant_id="tenant_001",
                repository_id=None,
                memory_id="memory_missing",
            )


def test_postgres_memory_publication_rejects_approval_provenance_mismatch(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    inputs = _inputs(postgres_cluster)
    with PostgresMemoryPublicationV3Repository.connect(
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
        **postgres_cluster.connection_kwargs(),
    ) as repository:
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
            PostgresMemoryPublicationV3ConflictError,
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


def test_postgres_memory_publication_requires_activated_predecessor(
    postgres_cluster: PostgresCluster,
) -> None:
    _install(postgres_cluster)
    inputs = _inputs(postgres_cluster)
    second, second_content = _second_revision(inputs[0])
    with PostgresMemoryRevisionV3Repository.connect(
        **postgres_cluster.connection_kwargs()
    ) as proposals:
        proposals.store_proposal(
            second,
            next(iter(inputs[1].values())),
            tuple(inputs[2].values()),
        )
    with PostgresMemoryPublicationV3Repository.connect(
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
        **postgres_cluster.connection_kwargs(),
    ) as repository:
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
            PostgresMemoryPublicationV3ConflictError,
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


def test_postgres_memory_publication_strict_helpers_and_error_mapping():
    request, _decision = _authorization(
        _policy(),
        actor_id="publication_approver",
        permission="memory:review",
        decided_at=APPROVED_AT,
    )
    assert _loads_request(_request_descriptor(request)) == request
    with pytest.raises(ValueError, match="exactly AuthorizationRequest"):
        _request_descriptor(object())  # type: ignore[arg-type]
    with pytest.raises(PostgresMemoryPublicationV3PersistenceError):
        _loads_request('{"request_id":"first","request_id":"second"}')
    with pytest.raises(PostgresMemoryPublicationV3PersistenceError):
        _loads_request("{}")

    class StorageError(Exception):
        def __init__(self, sqlstate):
            super().__init__("secret database detail")
            self.sqlstate = sqlstate

    with pytest.raises(PostgresMemoryPublicationV3SchemaError):
        PostgresMemoryPublicationV3Repository._raise_storage_error(
            StorageError("42P01"),
            "load publication",
        )
    with pytest.raises(PostgresMemoryPublicationV3ConflictError):
        PostgresMemoryPublicationV3Repository._raise_storage_error(
            StorageError("23505"),
            "append publication",
        )
    with pytest.raises(PostgresMemoryPublicationV3PersistenceError):
        PostgresMemoryPublicationV3Repository._raise_storage_error(
            StorageError(None),
            "load publication",
        )


def test_postgres_memory_publication_loader_and_put_defenses(monkeypatch):
    class RowsCursor:
        def __init__(self, *rows):
            self.rows = list(rows)

        def execute(self, *_args):
            return None

        def fetchall(self):
            return self.rows.pop(0)

    with pytest.raises(ValueError, match="exactly one"):
        PostgresMemoryPublicationV3Repository._load_approval_row(
            RowsCursor([]),
        )
    with pytest.raises(ValueError, match="exactly one"):
        PostgresMemoryPublicationV3Repository._load_activation_row(
            RowsCursor([]),
        )
    for loader, identity_name in (
        (
            PostgresMemoryPublicationV3Repository._load_approval_row,
            "approval_id",
        ),
        (
            PostgresMemoryPublicationV3Repository._load_activation_row,
            "activation_id",
        ),
    ):
        with pytest.raises(PostgresMemoryPublicationV3NotFoundError):
            loader(  # type: ignore[arg-type]
                RowsCursor([]),
                **{identity_name: "identity"},
            )
        with pytest.raises(
            PostgresMemoryPublicationV3PersistenceError,
            match="invalid shape",
        ):
            loader(  # type: ignore[arg-type]
                RowsCursor([{"descriptor": None}]),
                **{identity_name: "identity"},
            )
        with pytest.raises(
            PostgresMemoryPublicationV3PersistenceError,
            match="is invalid",
        ):
            loader(  # type: ignore[arg-type]
                RowsCursor([{
                    "descriptor": "{}",
                    "authorization_policy_descriptor": "{}",
                    "authorization_request_descriptor": "{}",
                    "authorization_decision_descriptor": "{}",
                    "attestation_verified_by": "verifier",
                }]),
                **{identity_name: "identity"},
            )
    with pytest.raises(
        PostgresMemoryPublicationV3PersistenceError,
        match="invalid shape",
    ):
        PostgresMemoryPublicationV3Repository._put_exact(
            RowsCursor([], [{"identity": "wrong"}]),  # type: ignore[arg-type]
            table="example",
            id_column="identity",
            columns=("identity", "value"),
            values=("expected", "value"),
            conflict_message="conflict",
        )
    with pytest.raises(PostgresMemoryPublicationV3ConflictError):
        PostgresMemoryPublicationV3Repository._put_exact(
            RowsCursor([], [], []),  # type: ignore[arg-type]
            table="example",
            id_column="identity",
            columns=("identity", "value"),
            values=("expected", "value"),
            conflict_message="conflict",
        )
    for rows, message in (
        ([{}, {}], "not unique"),
        ([{
            "current_revision_number": 0,
            "current_revision_id": "revision",
            "current_activation_id": None,
        }], "inconsistent"),
        ([{"current_revision_number": 1}], "invalid shape"),
    ):
        with pytest.raises(
            PostgresMemoryPublicationV3PersistenceError,
            match=message,
        ):
            PostgresMemoryPublicationV3Repository._select_head(
                RowsCursor(rows),  # type: ignore[arg-type]
                tenant_id="tenant",
                repository_id=None,
                memory_id="memory",
                for_update=False,
            )
    assert (
        PostgresMemoryPublicationV3Repository._select_head(
            RowsCursor([{
                "tenant_id": "tenant",
                "repository_id": None,
                "memory_id": "memory",
                "current_revision_number": 0,
                "current_revision_id": None,
                "current_activation_id": None,
            }]),  # type: ignore[arg-type]
            tenant_id="tenant",
            repository_id=None,
            memory_id="memory",
            for_update=True,
        )
        is None
    )
    head_row = {
        "tenant_id": "tenant",
        "repository_id": None,
        "memory_id": "memory",
        "current_revision_number": 1,
        "current_revision_id": "revision",
        "current_activation_id": "activation",
    }
    monkeypatch.setattr(
        PostgresMemoryPublicationV3Repository,
        "_load_activation_row",
        staticmethod(
            lambda *_args, **_kwargs: (
                SimpleNamespace(
                    revision_id="different",
                    revision_number=1,
                    tenant_id="tenant",
                    repository_id=None,
                    memory_id="memory",
                ),
                None,
                None,
                None,
                "verifier",
            )
        ),
    )
    with pytest.raises(
        PostgresMemoryPublicationV3PersistenceError,
        match="does not match",
    ):
        PostgresMemoryPublicationV3Repository._select_head(
            RowsCursor([head_row]),  # type: ignore[arg-type]
            tenant_id="tenant",
            repository_id=None,
            memory_id="memory",
            for_update=False,
        )

    repository = PostgresMemoryPublicationV3Repository(
        object(),
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="verifier",
    )
    with pytest.raises(
        PostgresMemoryPublicationV3SchemaError,
        match="search_path",
    ):
        repository._lock_schema(
            RowsCursor([]),  # type: ignore[arg-type]
            for_write=False,
        )


def test_postgres_memory_publication_proposal_bundle_defenses():
    revision, fixes, regressions = _publication_inputs()

    class RowsCursor:
        def __init__(self, *rows):
            self.rows = list(rows)

        def execute(self, *_args):
            return None

        def fetchall(self):
            return self.rows.pop(0)

    with pytest.raises(
        PostgresMemoryPublicationV3NotFoundError,
        match="proposal",
    ):
        PostgresMemoryPublicationV3Repository._require_proposal_bundle(
            RowsCursor([]),  # type: ignore[arg-type]
            revision,
            None,
            fixes,
            regressions,
        )
    proposal_row = [{"descriptor": tbm.dumps_memory_revision(revision)}]
    second, _content = _second_revision(revision)
    with pytest.raises(
        PostgresMemoryPublicationV3NotFoundError,
        match="previous",
    ):
        PostgresMemoryPublicationV3Repository._require_proposal_bundle(
            RowsCursor(
                [{"descriptor": tbm.dumps_memory_revision(second)}],
                [],
            ),  # type: ignore[arg-type]
            second,
            revision,
            fixes,
            regressions,
        )
    with pytest.raises(
        PostgresMemoryPublicationV3NotFoundError,
        match="fix evidence",
    ):
        PostgresMemoryPublicationV3Repository._require_proposal_bundle(
            RowsCursor(proposal_row),  # type: ignore[arg-type]
            revision,
            None,
            {},
            regressions,
        )
    with pytest.raises(
        PostgresMemoryPublicationV3NotFoundError,
        match="fix evidence is not stored",
    ):
        PostgresMemoryPublicationV3Repository._require_proposal_bundle(
            RowsCursor(proposal_row, []),  # type: ignore[arg-type]
            revision,
            None,
            fixes,
            regressions,
        )
    fix_row = [{
        "descriptor": tbm.dumps_fix_evidence(next(iter(fixes.values())))
    }]
    with pytest.raises(
        PostgresMemoryPublicationV3PersistenceError,
        match="links",
    ):
        PostgresMemoryPublicationV3Repository._require_proposal_bundle(
            RowsCursor(proposal_row, fix_row, []),  # type: ignore[arg-type]
            revision,
            None,
            fixes,
            regressions,
        )
    link_rows = [
        {"evidence_id": evidence_id}
        for evidence_id in revision.regression_evidence_ids
    ]
    with pytest.raises(
        PostgresMemoryPublicationV3NotFoundError,
        match="regression evidence is not supplied",
    ):
        PostgresMemoryPublicationV3Repository._require_proposal_bundle(
            RowsCursor(proposal_row, fix_row, link_rows),  # type: ignore[arg-type]
            revision,
            None,
            fixes,
            {},
        )
    with pytest.raises(
        PostgresMemoryPublicationV3NotFoundError,
        match="regression evidence is not stored",
    ):
        PostgresMemoryPublicationV3Repository._require_proposal_bundle(
            RowsCursor(
                proposal_row,
                fix_row,
                link_rows,
                [],
            ),  # type: ignore[arg-type]
            revision,
            None,
            fixes,
            regressions,
        )
