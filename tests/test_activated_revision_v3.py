from __future__ import annotations

from dataclasses import dataclass, replace
import sqlite3
from types import SimpleNamespace

import pytest

import trace_backed_memory as tbm
from tests.test_artifact_service_v3 import _Provider, _context, _registry
from tests.test_memory_publication_v3 import (
    CONTENT,
    DIGEST,
    _authorization,
    _policy,
    _publication_inputs,
)
from tests.test_sqlite_memory_publication_v3 import (
    _append_activation,
    _append_approval,
    _connection,
)


NOW = "2026-07-29T00:00:00Z"


@dataclass
class _PublishedEnvironment:
    source: tbm.ActivatedRevisionSource
    registry: tbm.EntityRegistrySnapshot
    revision: tbm.MemoryRevision
    approval: tbm.MemoryRevisionApproval
    activation: tbm.MemoryRevisionActivation
    publication: tbm.SQLiteMemoryPublicationV3Repository
    authorization_repository: tbm.SQLiteAuthorizationV3Repository
    artifact_repository: tbm.SQLiteArtifactV3Repository
    connection: sqlite3.Connection
    authorization: tbm.AuthenticatedRetrievalService
    proposals: tbm.SQLiteMemoryRevisionV3Repository
    artifacts: tbm.AuthenticatedArtifactService
    fixes: dict[str, tbm.FixEvidence]
    regressions: dict[str, tbm.StructuredRegressionEvidence]
    publication_policy: tbm.AuthorizationPolicyBundle

    def close(self) -> None:
        self.publication.close()
        self.authorization_repository.close()
        self.artifact_repository.close()
        self.connection.close()


def _encrypted_revision():
    base, fixes, regressions = _publication_inputs()
    artifact = tbm.create_content_addressed_artifact(
        CONTENT,
        media_type="application/json",
        classification="restricted",
        created_at=base.content_artifact.created_at,
        encryption_key_id="key_001",
    )
    revision = tbm.build_memory_revision(
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
    return revision, fixes, regressions


def _runtime_services(
    authorization_repository: tbm.SQLiteAuthorizationV3Repository,
    artifact_repository: tbm.SQLiteArtifactV3Repository,
):
    registry = _registry(
        permissions=("memory:retrieve", "artifact:read", "artifact:write")
    )
    request_number = iter(range(1, 100))
    authorization = tbm.AuthenticatedRetrievalService(
        registry_provider=lambda: registry,
        decision_writer=authorization_repository,
        clock=lambda: NOW,
        request_id_factory=lambda: f"activated_request_{next(request_number):03d}",
    )
    artifacts = tbm.AuthenticatedArtifactService(
        authorization_service=authorization,
        authority=artifact_repository,
        encryption_provider=_Provider(),
        clock=lambda: NOW,
    )
    return registry, authorization, artifacts


def _published_source(*, trusted_verifier: str = "attestation_verifier"):
    connection = _connection()
    revision, fixes, regressions = _encrypted_revision()
    proposals = tbm.SQLiteMemoryRevisionV3Repository(connection)
    proposals.store_proposal(
        revision,
        next(iter(fixes.values())),
        tuple(regressions.values()),
    )
    policy = _policy()
    review_request, review_decision = _authorization(
        policy,
        actor_id="publication_approver",
        permission="memory:review",
        decided_at="2026-07-27T00:07:00Z",
    )
    inputs = (
        revision,
        fixes,
        regressions,
        policy,
        review_request,
        review_decision,
    )
    publication = tbm.SQLiteMemoryPublicationV3Repository(
        connection,
        attestation_verifier=lambda *_args: True,
        attestation_verifier_id="attestation_verifier",
    )
    approval = _append_approval(publication, inputs).approval
    activation = _append_activation(
        publication, inputs, approval
    ).activation
    authorization_repository = tbm.SQLiteAuthorizationV3Repository.connect(
        initialize=True
    )
    artifact_repository = tbm.SQLiteArtifactV3Repository.connect(
        initialize=True
    )
    registry, authorization, artifacts = _runtime_services(
        authorization_repository, artifact_repository
    )
    artifacts.put(_context(registry), revision.content_artifact, CONTENT)
    source = tbm.ActivatedRevisionSource(
        authorization_service=authorization,
        proposal_reader=proposals,
        publication_reader=publication,
        artifact_reader=artifacts,
        trusted_attestation_verifier_ids=(trusted_verifier,),
    )
    return _PublishedEnvironment(
        source=source,
        registry=registry,
        revision=revision,
        approval=approval,
        activation=activation,
        publication=publication,
        authorization_repository=authorization_repository,
        artifact_repository=artifact_repository,
        connection=connection,
        authorization=authorization,
        proposals=proposals,
        artifacts=artifacts,
        fixes=fixes,
        regressions=regressions,
        publication_policy=policy,
    )


def test_activated_revision_source_resolves_current_verified_candidate():
    environment = _published_source()
    try:
        candidate = environment.source.load_current(
            _context(environment.registry),
            memory_id=environment.revision.memory_id,
            repository_id="repository_001",
        )
        assert candidate.revision == environment.revision
        assert candidate.approval == environment.approval
        assert candidate.activation == environment.activation
        assert candidate.content == CONTENT
        assert candidate.candidate_sha256 == tbm.activated_revision_candidate_sha256(
            environment.revision,
            environment.approval,
            environment.activation,
            approval_attestation_verified_by="attestation_verifier",
            activation_attestation_verified_by="attestation_verifier",
        )
        assert candidate.retrieval_authorization_event_id.startswith(
            "authz_sha256_"
        )
        assert candidate.artifact_authorization_event_id.startswith(
            "authz_sha256_"
        )
        decisions = environment.authorization_repository.list_decisions(
            environment.registry.authorization_policy.policy_sha256
        )
        permissions = [item.permission for item in decisions]
        assert permissions.count("artifact:write") == 1
        assert permissions.count("memory:retrieve") == 1
        assert permissions.count("artifact:read") == 1
    finally:
        environment.close()


def test_activated_revision_source_rejects_untrusted_attestation_verifier():
    environment = _published_source(trusted_verifier="different_verifier")
    try:
        with pytest.raises(tbm.ActivatedRevisionV3Error) as caught:
            environment.source.load_current(
                _context(environment.registry),
                memory_id=environment.revision.memory_id,
                repository_id="repository_001",
            )
        assert caught.value.code == "TBM_ACTIVATED_REVISION_ATTESTATION_UNTRUSTED"
    finally:
        environment.close()


def test_activated_revision_source_rejects_invalid_input_before_authorization():
    environment = _published_source()
    try:
        with pytest.raises(tbm.ActivatedRevisionV3Error) as caught:
            environment.source.load_current(
                _context(environment.registry),
                memory_id="\ud800",
                repository_id="repository_001",
            )
        assert caught.value.code == "TBM_ACTIVATED_REVISION_INPUT_INVALID"
        decisions = environment.authorization_repository.list_decisions(
            environment.registry.authorization_policy.policy_sha256
        )
        assert [item.permission for item in decisions] == ["artifact:write"]
    finally:
        environment.close()


def test_activated_revision_source_rejects_repository_outside_authorized_scope():
    environment = _published_source()
    try:
        with pytest.raises(tbm.ActivatedRevisionV3Error) as caught:
            environment.source.load_current(
                _context(environment.registry),
                memory_id=environment.revision.memory_id,
                repository_id="repository_other",
            )
        assert caught.value.code == "TBM_ACTIVATED_REVISION_SCOPE_REJECTED"
        decisions = environment.authorization_repository.list_decisions(
            environment.registry.authorization_policy.policy_sha256
        )
        assert [item.permission for item in decisions].count("artifact:read") == 0
    finally:
        environment.close()


def test_activated_revision_source_rejects_invalid_trusted_verifier_id():
    environment = _published_source()
    try:
        with pytest.raises(ValueError):
            tbm.ActivatedRevisionSource(
                authorization_service=environment.authorization,
                proposal_reader=environment.proposals,
                publication_reader=environment.publication,
                artifact_reader=environment.artifacts,
                trusted_attestation_verifier_ids=("\ud800",),
            )
    finally:
        environment.close()


def test_activated_revision_source_sanitizes_malformed_reader_failure():
    environment = _published_source()
    try:
        class MalformedProposal:
            @property
            def revision(self):
                raise RuntimeError("backend detail")

        class MalformedProposalReader:
            def load_proposal(self, _revision_id: str):
                return MalformedProposal()

        source = tbm.ActivatedRevisionSource(
            authorization_service=environment.authorization,
            proposal_reader=MalformedProposalReader(),
            publication_reader=environment.publication,
            artifact_reader=environment.artifacts,
            trusted_attestation_verifier_ids=("attestation_verifier",),
        )
        with pytest.raises(tbm.ActivatedRevisionV3Error) as caught:
            source.load_current(
                _context(environment.registry),
                memory_id=environment.revision.memory_id,
                repository_id="repository_001",
            )
        assert caught.value.code == "TBM_ACTIVATED_REVISION_READ_FAILED"
        assert "backend detail" not in str(caught.value)
    finally:
        environment.close()


def test_activated_revision_source_resolves_linear_successor_head():
    environment = _published_source()
    try:
        content = b'{"memory_text":"Use the second verified workflow."}'
        artifact = tbm.create_content_addressed_artifact(
            content,
            media_type="application/json",
            classification="restricted",
            created_at="2026-07-27T00:09:00Z",
            encryption_key_id="key_002",
        )
        first = environment.revision
        second = tbm.build_memory_revision(
            memory_id=first.memory_id,
            memory_kind=first.memory_kind,
            revision_number=2,
            previous_revision_id=first.revision_id,
            memory_type=first.memory_type,
            content_artifact=artifact,
            scope=first.scope,
            confidence=first.confidence,
            sensitive=True,
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
        environment.proposals.store_proposal(
            second,
            next(iter(environment.fixes.values())),
            tuple(environment.regressions.values()),
        )
        review_request, review_decision = _authorization(
            environment.publication_policy,
            actor_id="publication_approver",
            permission="memory:review",
            decided_at="2026-07-27T00:11:00Z",
        )
        approval = environment.publication.append_approval(
            revision=second,
            previous_revision=first,
            content=content,
            fix_evidence_by_id=environment.fixes,
            regression_evidence_by_id=environment.regressions,
            policy=environment.publication_policy,
            request=review_request,
            decision=review_decision,
            approved_by="publication_approver",
            approved_via_client_id="publication_service",
            approved_at="2026-07-27T00:11:00Z",
            approval_attestation_sha256=DIGEST,
        ).approval
        activate_request, activate_decision = _authorization(
            environment.publication_policy,
            actor_id="publication_activator",
            permission="memory:activate",
            decided_at="2026-07-27T00:12:00Z",
        )
        activation = environment.publication.append_activation(
            revision=second,
            previous_revision=first,
            content=content,
            fix_evidence_by_id=environment.fixes,
            regression_evidence_by_id=environment.regressions,
            approval=approval,
            approval_policy=environment.publication_policy,
            approval_request=review_request,
            approval_decision=review_decision,
            policy=environment.publication_policy,
            request=activate_request,
            decision=activate_decision,
            activated_by="publication_activator",
            activated_via_client_id="publication_service",
            activated_at="2026-07-27T00:12:00Z",
            activation_attestation_sha256=DIGEST,
        ).activation
        environment.artifacts.put(
            _context(environment.registry), artifact, content
        )
        candidate = environment.source.load_current(
            _context(environment.registry),
            memory_id=second.memory_id,
            repository_id="repository_001",
        )
        assert candidate.revision == second
        assert candidate.activation == activation
        assert candidate.content == content
    finally:
        environment.close()


def test_activated_revision_source_rejects_head_change_during_read():
    environment = _published_source()
    try:
        class ChangingPublication:
            def __init__(self) -> None:
                self.head_reads = 0

            def load_head(self, **kwargs):
                self.head_reads += 1
                head = environment.publication.load_head(**kwargs)
                if self.head_reads == 2:
                    return replace(head, current_revision_number=2)
                return head

            def load_approval_bundle(self, approval_id: str):
                return environment.publication.load_approval_bundle(approval_id)

            def load_activation_bundle(self, activation_id: str):
                return environment.publication.load_activation_bundle(activation_id)

        changed = tbm.ActivatedRevisionSource(
            authorization_service=environment.authorization,
            proposal_reader=environment.proposals,
            publication_reader=ChangingPublication(),
            artifact_reader=environment.artifacts,
            trusted_attestation_verifier_ids=("attestation_verifier",),
        )
        with pytest.raises(tbm.ActivatedRevisionV3Error) as caught:
            changed.load_current(
                _context(environment.registry),
                memory_id=environment.revision.memory_id,
                repository_id="repository_001",
            )
        assert caught.value.code == "TBM_ACTIVATED_REVISION_STALE"
    finally:
        environment.close()


def test_activated_revision_candidate_rejects_changed_identity():
    environment = _published_source()
    try:
        candidate = environment.source.load_current(
            _context(environment.registry),
            memory_id=environment.revision.memory_id,
            repository_id="repository_001",
        )
        with pytest.raises(tbm.ActivatedRevisionV3Error):
            replace(candidate, candidate_sha256=DIGEST)
    finally:
        environment.close()


def test_activated_revision_candidate_rejects_cross_scope_approval():
    environment = _published_source()
    try:
        candidate = environment.source.load_current(
            _context(environment.registry),
            memory_id=environment.revision.memory_id,
            repository_id="repository_001",
        )
        values = environment.approval.to_dict()
        values.pop("approval_id")
        values["repository_id"] = "repository_other"
        approval_id = tbm.memory_revision_approval_id(values)
        mismatched = tbm.MemoryRevisionApproval(
            approval_id=approval_id,
            **values,
        )
        with pytest.raises(tbm.ActivatedRevisionV3Error):
            replace(candidate, approval=mismatched, candidate_sha256=DIGEST)
    finally:
        environment.close()


@pytest.mark.parametrize(
    "invalid_field",
    (
        "approval_attestation_verified_by",
        "activation_attestation_verified_by",
    ),
)
def test_activated_revision_candidate_hash_rejects_invalid_verifier_id(
    invalid_field,
):
    environment = _published_source()
    try:
        verifier_ids = {
            "approval_attestation_verified_by": "attestation_verifier",
            "activation_attestation_verified_by": "attestation_verifier",
        }
        verifier_ids[invalid_field] = "\ud800"
        with pytest.raises(tbm.ActivatedRevisionV3Error):
            tbm.activated_revision_candidate_sha256(
                environment.revision,
                environment.approval,
                environment.activation,
                **verifier_ids,
            )
    finally:
        environment.close()


@pytest.mark.parametrize(
    ("field_name", "value"),
    (
        ("contract_version", "unsupported"),
        ("revision", object()),
        ("approval", object()),
        ("activation", object()),
        ("content", b"wrong"),
        ("retrieval_authorization_event_id", "invalid"),
        ("artifact_authorization_event_id", "invalid"),
        ("approval_attestation_verified_by", ""),
        ("activation_attestation_verified_by", ""),
    ),
)
def test_activated_revision_candidate_validates_every_boundary(
    field_name,
    value,
):
    environment = _published_source()
    try:
        candidate = environment.source.load_current(
            _context(environment.registry),
            memory_id=environment.revision.memory_id,
            repository_id="repository_001",
        )
        with pytest.raises(tbm.ActivatedRevisionV3Error) as caught:
            replace(candidate, **{field_name: value})
        assert caught.value.code == "TBM_ACTIVATED_REVISION_INVALID"
    finally:
        environment.close()


@pytest.mark.parametrize("field_name", ("revision", "approval", "activation"))
def test_activated_revision_candidate_hash_validates_record_types(field_name):
    environment = _published_source()
    try:
        values = {
            "revision": environment.revision,
            "approval": environment.approval,
            "activation": environment.activation,
        }
        values[field_name] = object()
        with pytest.raises(tbm.ActivatedRevisionV3Error) as caught:
            tbm.activated_revision_candidate_sha256(
                values["revision"],
                values["approval"],
                values["activation"],
                approval_attestation_verified_by="attestation_verifier",
                activation_attestation_verified_by="attestation_verifier",
            )
        assert caught.value.code == "TBM_ACTIVATED_REVISION_INVALID"
    finally:
        environment.close()


def test_activated_revision_source_constructor_and_repository_guards():
    environment = _published_source()
    try:
        with pytest.raises(TypeError):
            tbm.ActivatedRevisionSource(
                authorization_service=object(),
                proposal_reader=environment.proposals,
                publication_reader=environment.publication,
                artifact_reader=environment.artifacts,
                trusted_attestation_verifier_ids=("attestation_verifier",),
            )
        with pytest.raises(TypeError):
            tbm.ActivatedRevisionSource(
                authorization_service=environment.authorization,
                proposal_reader=object(),
                publication_reader=environment.publication,
                artifact_reader=environment.artifacts,
                trusted_attestation_verifier_ids=("attestation_verifier",),
            )
        with pytest.raises(tbm.ActivatedRevisionV3Error) as caught:
            environment.source.load_current(
                _context(environment.registry),
                memory_id=environment.revision.memory_id,
                repository_id="",
            )
        assert caught.value.code == "TBM_ACTIVATED_REVISION_INPUT_INVALID"
    finally:
        environment.close()


@pytest.mark.parametrize(
    ("mode", "expected_code"),
    (
        ("head_raise", "TBM_ACTIVATED_REVISION_PUBLICATION_READ_FAILED"),
        ("activation_raise", "TBM_ACTIVATED_REVISION_PUBLICATION_READ_FAILED"),
        ("activation_invalid", "TBM_ACTIVATED_REVISION_PUBLICATION_INVALID"),
        ("approval_raise", "TBM_ACTIVATED_REVISION_PUBLICATION_READ_FAILED"),
        ("approval_invalid", "TBM_ACTIVATED_REVISION_PUBLICATION_INVALID"),
        ("proposal_raise", "TBM_ACTIVATED_REVISION_PROPOSAL_READ_FAILED"),
        ("proposal_invalid", "TBM_ACTIVATED_REVISION_PROPOSAL_INVALID"),
        ("artifact_raise", "TBM_ACTIVATED_REVISION_ARTIFACT_READ_FAILED"),
        ("artifact_invalid", "TBM_ACTIVATED_REVISION_ARTIFACT_INVALID"),
    ),
)
def test_activated_revision_source_reader_failures_are_stable(
    mode,
    expected_code,
):
    environment = _published_source()
    try:
        class Publication:
            def load_head(self, **kwargs):
                if mode == "head_raise":
                    raise RuntimeError("publication secret")
                return environment.publication.load_head(**kwargs)

            def load_approval_bundle(self, approval_id):
                if mode == "approval_raise":
                    raise RuntimeError("publication secret")
                if mode == "approval_invalid":
                    return object()
                return environment.publication.load_approval_bundle(approval_id)

            def load_activation_bundle(self, activation_id):
                if mode == "activation_raise":
                    raise RuntimeError("publication secret")
                if mode == "activation_invalid":
                    return object()
                return environment.publication.load_activation_bundle(
                    activation_id
                )

        class Proposals:
            def load_proposal(self, revision_id):
                if mode == "proposal_raise":
                    raise RuntimeError("proposal secret")
                proposal = environment.proposals.load_proposal(revision_id)
                if mode == "proposal_invalid":
                    return SimpleNamespace(
                        revision=proposal.revision,
                        fix_evidence=proposal.fix_evidence,
                        regression_evidence=list(proposal.regression_evidence),
                    )
                return proposal

        class Artifacts:
            def get_with_receipt(self, context, artifact_id):
                if mode == "artifact_raise":
                    raise RuntimeError("artifact secret")
                if mode == "artifact_invalid":
                    return object()
                return environment.artifacts.get_with_receipt(
                    context,
                    artifact_id,
                )

        source = tbm.ActivatedRevisionSource(
            authorization_service=environment.authorization,
            proposal_reader=Proposals(),
            publication_reader=Publication(),
            artifact_reader=Artifacts(),
            trusted_attestation_verifier_ids=("attestation_verifier",),
        )
        with pytest.raises(tbm.ActivatedRevisionV3Error) as caught:
            source.load_current(
                _context(environment.registry),
                memory_id=environment.revision.memory_id,
                repository_id="repository_001",
            )
        assert caught.value.code == expected_code
        assert "secret" not in str(caught.value)
    finally:
        environment.close()


def test_activated_revision_source_rejects_head_and_evidence_mismatch():
    environment = _published_source()
    try:
        class HeadMismatchPublication:
            def load_head(self, **kwargs):
                return replace(
                    environment.publication.load_head(**kwargs),
                    memory_id="memory_other",
                )

            def load_approval_bundle(self, approval_id):
                return environment.publication.load_approval_bundle(approval_id)

            def load_activation_bundle(self, activation_id):
                return environment.publication.load_activation_bundle(
                    activation_id
                )

        mismatch = tbm.ActivatedRevisionSource(
            authorization_service=environment.authorization,
            proposal_reader=environment.proposals,
            publication_reader=HeadMismatchPublication(),
            artifact_reader=environment.artifacts,
            trusted_attestation_verifier_ids=("attestation_verifier",),
        )
        with pytest.raises(tbm.ActivatedRevisionV3Error) as caught:
            mismatch.load_current(
                _context(environment.registry),
                memory_id=environment.revision.memory_id,
                repository_id="repository_001",
            )
        assert caught.value.code == "TBM_ACTIVATED_REVISION_HEAD_MISMATCH"

        class MissingEvidenceProposals:
            def load_proposal(self, revision_id):
                proposal = environment.proposals.load_proposal(revision_id)
                return SimpleNamespace(
                    revision=proposal.revision,
                    fix_evidence=None,
                    regression_evidence=proposal.regression_evidence,
                )

        missing_evidence = tbm.ActivatedRevisionSource(
            authorization_service=environment.authorization,
            proposal_reader=MissingEvidenceProposals(),
            publication_reader=environment.publication,
            artifact_reader=environment.artifacts,
            trusted_attestation_verifier_ids=("attestation_verifier",),
        )
        with pytest.raises(tbm.ActivatedRevisionV3Error) as caught:
            missing_evidence.load_current(
                _context(environment.registry),
                memory_id=environment.revision.memory_id,
                repository_id="repository_001",
            )
        assert caught.value.code == "TBM_ACTIVATED_REVISION_VERIFICATION_FAILED"
    finally:
        environment.close()


def test_activated_revision_source_maps_authorization_failure():
    environment = _published_source()
    try:
        registry = _registry(
            permissions=("artifact:read", "artifact:write")
        )
        with tbm.SQLiteAuthorizationV3Repository.connect(
            initialize=True
        ) as authorization_repository:
            authorization = tbm.AuthenticatedRetrievalService(
                registry_provider=lambda: registry,
                decision_writer=authorization_repository,
                clock=lambda: NOW,
                request_id_factory=lambda: "denied_request",
            )
            source = tbm.ActivatedRevisionSource(
                authorization_service=authorization,
                proposal_reader=environment.proposals,
                publication_reader=environment.publication,
                artifact_reader=environment.artifacts,
                trusted_attestation_verifier_ids=("attestation_verifier",),
            )
            with pytest.raises(tbm.ActivatedRevisionV3Error) as caught:
                source.load_current(
                    _context(registry),
                    memory_id=environment.revision.memory_id,
                    repository_id="repository_001",
                )
            assert caught.value.code == "TBM_SERVICE_AUTHORIZATION_DENIED"
            assert caught.value.__cause__ is None
    finally:
        environment.close()


def test_activated_revision_source_rejects_authorization_receipt_mismatch(
    monkeypatch,
):
    environment = _published_source()
    try:
        candidate = environment.source.load_current(
            _context(environment.registry),
            memory_id=environment.revision.memory_id,
            repository_id="repository_001",
        )
        mismatched = replace(
            candidate,
            retrieval_authorization_event_id=(
                "authz_sha256_" + "0" * 64
            ),
        )
        monkeypatch.setattr(
            environment.source,
            "_resolve_authorized",
            lambda *_args, **_kwargs: mismatched,
        )
        with pytest.raises(tbm.ActivatedRevisionV3Error) as caught:
            environment.source.load_current(
                _context(environment.registry),
                memory_id=environment.revision.memory_id,
                repository_id="repository_001",
            )
        assert (
            caught.value.code
            == "TBM_ACTIVATED_REVISION_AUTHORIZATION_MISMATCH"
        )
    finally:
        environment.close()
