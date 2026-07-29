from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.sqlite_gate_evidence_v3 import (
    SQLiteGateEvidenceV3Repository,
)


ROOT = Path(__file__).resolve().parents[1]
PROMPT = b"Review the exact bounded candidate set."
RESPONSE = b'{"decision":"allow","reason":"applicable"}'


def _evidence() -> tuple[tbm.RetrievalSnapshot, tbm.SystemGateEvaluation]:
    return (
        tbm.loads_retrieval_snapshot(
            (ROOT / "examples" / "retrieval_snapshot_v3.example.json").read_bytes()
        ),
        tbm.loads_system_gate_evaluation(
            (ROOT / "examples" / "system_gate_evaluation_v3.example.json").read_bytes()
        ),
    )


def _repository() -> tuple[
    tbm.SQLiteSemanticGateArtifactV3Repository,
    SQLiteGateEvidenceV3Repository,
    tbm.SystemGateEvaluation,
]:
    snapshot, evaluation = _evidence()
    authority = tbm.SQLiteSemanticGateArtifactV3Repository.connect(initialize=True)
    evidence = SQLiteGateEvidenceV3Repository(authority._connection)
    evidence.store_bundle(snapshot, evaluation)
    return authority, evidence, evaluation


def _context() -> tbm.AuthenticatedSemanticProviderContext:
    return tbm.AuthenticatedSemanticProviderContext(
        provider_id="provider_openai",
        authenticator_id="authenticator_oidc",
        credential_id="credential_prod_01",
    )


def _configuration() -> tbm.SemanticGateServiceConfiguration:
    return tbm.SemanticGateServiceConfiguration(
        prompt_template_id="semantic_gate_default",
        prompt_template_version="v1",
        generation_config_sha256="sha256:" + "3" * 64,
        response_media_type="application/json",
    )


def _service(
    authority: tbm.SQLiteSemanticGateArtifactV3Repository,
    evidence: SQLiteGateEvidenceV3Repository,
    times: Iterator[str],
) -> tbm.AuthenticatedSemanticGateService:
    return tbm.AuthenticatedSemanticGateService(
        provider=tbm.TrustedSemanticProvider(
            provider_id="provider_openai",
            authenticator_id="authenticator_oidc",
            credential_id="credential_prod_01",
            model_id="model_gate",
            model_version="2026-07-01",
            endpoint_id="endpoint_primary",
        ),
        configuration=_configuration(),
        evidence_reader=evidence,
        authority=authority,
        clock=lambda: next(times),
    )


def _provider_result(
    evaluation: tbm.SystemGateEvaluation,
    *,
    provider_request_id: str = "provider_request_01",
) -> tbm.SemanticProviderResult:
    allowed = tuple(
        sorted(
            decision.memory_revision_id
            for decision in evaluation.decisions
            if decision.outcome == "allowed"
        )
    )
    blocked = tuple(
        sorted(
            decision.memory_revision_id
            for decision in evaluation.decisions
            if decision.outcome == "blocked"
        )
    )
    return tbm.SemanticProviderResult(
        response=RESPONSE,
        provider_request_id=provider_request_id,
        decision_id="decision_allow_exact",
        final_allowed_revision_ids=allowed,
        final_blocked_revision_ids=blocked,
        reason="The candidate remains directly applicable.",
        risk="low",
        recommended_injection="summary",
        input_tokens=12,
        output_tokens=8,
    )


def test_authenticated_semantic_service_persists_trusted_success() -> None:
    authority, evidence, evaluation = _repository()
    with authority:
        service = _service(
            authority,
            evidence,
            iter(("2026-07-27T08:03:00Z", "2026-07-27T08:03:00.125Z")),
        )
        seen: list[tbm.SemanticProviderCall] = []

        result = service.invoke(
            _context(),
            tbm.SemanticGateInvocationRequest(
                system_gate_evaluation_id=evaluation.evaluation_id,
                prompt=PROMPT,
            ),
            lambda call: seen.append(call) or _provider_result(evaluation),
        )

        assert seen == [
            tbm.SemanticProviderCall(
                provider_id="provider_openai",
                model_id="model_gate",
                model_version="2026-07-01",
                endpoint_id="endpoint_primary",
                prompt=PROMPT,
            )
        ]
        assert result.attempt.sequence == 1
        assert result.attempt.previous_attempt_id is None
        assert result.attempt.started_at == "2026-07-27T08:03:00Z"
        assert result.attempt.finished_at == "2026-07-27T08:03:00.125000Z"
        assert result.attempt.latency_ms == 125
        assert result.attempt.provider_id == "provider_openai"
        assert authority.load_attempt_chain(evaluation.evaluation_id) == (
            result.attempt,
        )
        assert (
            authority.load_attempt_with_artifacts(result.attempt.attempt_id)
            == result.artifacts
        )


def test_authentication_and_expected_parent_fail_before_provider_call() -> None:
    authority, evidence, evaluation = _repository()
    called = False

    def provider(_call: tbm.SemanticProviderCall) -> tbm.SemanticProviderResult:
        nonlocal called
        called = True
        return _provider_result(evaluation)

    with authority:
        service = _service(
            authority,
            evidence,
            iter(("2026-07-27T08:03:00Z", "2026-07-27T08:03:01Z")),
        )
        wrong = tbm.AuthenticatedSemanticProviderContext(
            provider_id="provider_openai",
            authenticator_id="authenticator_oidc",
            credential_id="credential_wrong",
        )
        with pytest.raises(tbm.SemanticGateServiceV3Error) as auth_error:
            service.invoke(
                wrong,
                tbm.SemanticGateInvocationRequest(
                    evaluation.evaluation_id,
                    PROMPT,
                ),
                provider,
            )
        assert auth_error.value.code == ("TBM_SEMANTIC_SERVICE_AUTHENTICATION_FAILED")

        with pytest.raises(tbm.SemanticGateServiceV3Error) as parent_error:
            service.invoke(
                _context(),
                tbm.SemanticGateInvocationRequest(
                    evaluation.evaluation_id,
                    PROMPT,
                    expected_previous_attempt_id=(
                        "semantic_attempt_sha256_" + "0" * 64
                    ),
                ),
                provider,
            )
        assert parent_error.value.code == "TBM_SEMANTIC_SERVICE_CHAIN_CHANGED"
        assert called is False
        assert authority.load_attempt_chain(evaluation.evaluation_id) == ()


@pytest.mark.parametrize("raw_failure", [False, True])
def test_provider_failure_is_sanitized_and_durably_recorded(
    raw_failure: bool,
) -> None:
    authority, evidence, evaluation = _repository()
    with authority:
        service = _service(
            authority,
            evidence,
            iter(("2026-07-27T08:03:00Z", "2026-07-27T08:03:01Z")),
        )

        def fail(_call: tbm.SemanticProviderCall) -> tbm.SemanticProviderResult:
            if raw_failure:
                raise RuntimeError("secret provider response")
            raise tbm.SemanticProviderCallError(
                "provider_timeout",
                provider_request_id="provider_request_timeout",
                input_tokens=12,
            )

        with pytest.raises(tbm.SemanticProviderInvocationFailedError) as captured:
            service.invoke(
                _context(),
                tbm.SemanticGateInvocationRequest(
                    evaluation.evaluation_id,
                    PROMPT,
                ),
                fail,
            )

        attempt = captured.value.result.attempt
        assert captured.value.code == "TBM_SEMANTIC_SERVICE_PROVIDER_FAILED"
        assert "secret" not in str(captured.value)
        assert captured.value.__cause__ is None
        assert attempt.status == "failed"
        assert attempt.endpoint_id == "endpoint_primary"
        assert attempt.error_code == (
            "provider_error" if raw_failure else "provider_timeout"
        )
        assert captured.value.result.artifacts.response is None
        assert authority.load_attempt_chain(evaluation.evaluation_id) == (attempt,)


def test_retry_rejects_invalid_gate_partition_before_authority(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    authority, evidence, evaluation = _repository()
    times = iter(
        (
            "2026-07-27T08:03:00Z",
            "2026-07-27T08:03:01Z",
            "2026-07-27T08:03:02Z",
            "2026-07-27T08:03:03Z",
        )
    )
    with authority:
        service = _service(authority, evidence, times)
        first = service.invoke(
            _context(),
            tbm.SemanticGateInvocationRequest(
                evaluation.evaluation_id,
                PROMPT,
            ),
            lambda _call: _provider_result(evaluation),
        )
        invalid = tbm.SemanticProviderResult(
            response=RESPONSE,
            provider_request_id="provider_request_02",
            decision_id="decision_reopen",
            final_allowed_revision_ids=(),
            final_blocked_revision_ids=(),
            reason="Attempted reopen.",
            risk="high",
            recommended_injection="full",
        )
        authority_called = False

        def unexpected_store(*_args: object) -> object:
            nonlocal authority_called
            authority_called = True
            return object()

        monkeypatch.setattr(
            authority,
            "store_attempt_with_artifacts",
            unexpected_store,
        )

        with pytest.raises(tbm.SemanticGateServiceV3Error) as captured:
            service.invoke(
                _context(),
                tbm.SemanticGateInvocationRequest(
                    evaluation.evaluation_id,
                    PROMPT,
                    expected_previous_attempt_id=first.attempt.attempt_id,
                ),
                lambda _call: invalid,
            )

        assert captured.value.code == ("TBM_SEMANTIC_SERVICE_PROVIDER_RESULT_INVALID")
        assert authority_called is False
        assert authority.load_attempt_chain(evaluation.evaluation_id) == (
            first.attempt,
        )


def test_clock_rollback_and_sensitive_configuration_fail_closed() -> None:
    with pytest.raises(tbm.SemanticGateServiceV3Error) as config_error:
        tbm.SemanticGateServiceConfiguration(
            prompt_template_id="semantic_gate_default",
            prompt_template_version="v1",
            generation_config_sha256="sha256:" + "3" * 64,
            response_media_type="application/json",
            classification="restricted",
        )
    assert config_error.value.code == "TBM_SEMANTIC_SERVICE_ENCRYPTION_REQUIRED"

    authority, evidence, evaluation = _repository()
    with authority:
        service = _service(
            authority,
            evidence,
            iter(("2026-07-27T08:03:01Z", "2026-07-27T08:03:00Z")),
        )
        with pytest.raises(tbm.SemanticGateServiceV3Error) as clock_error:
            service.invoke(
                _context(),
                tbm.SemanticGateInvocationRequest(
                    evaluation.evaluation_id,
                    PROMPT,
                ),
                lambda _call: _provider_result(evaluation),
            )
        assert clock_error.value.code == "TBM_SEMANTIC_SERVICE_CLOCK_INVALID"
        assert authority.load_attempt_chain(evaluation.evaluation_id) == ()


def test_external_values_are_canonical_bounded_and_use_stable_errors() -> None:
    with pytest.raises(ValueError, match="not supported"):
        tbm.SemanticProviderCallError("secret password")

    with pytest.raises(tbm.SemanticGateServiceV3Error) as context_error:
        tbm.AuthenticatedSemanticProviderContext(
            provider_id="",
            authenticator_id="authenticator_oidc",
            credential_id="credential_prod_01",
        )
    assert context_error.value.code == ("TBM_SEMANTIC_SERVICE_AUTHENTICATION_FAILED")

    with pytest.raises(tbm.SemanticGateServiceV3Error) as request_error:
        tbm.SemanticGateInvocationRequest("system_gate_" + "x" * 1_000_000, PROMPT)
    assert request_error.value.code == "TBM_SEMANTIC_SERVICE_REQUEST_INVALID"

    with pytest.raises(tbm.SemanticGateServiceV3Error) as result_error:
        tbm.SemanticProviderResult(
            response=RESPONSE,
            provider_request_id="provider_request_01",
            decision_id="decision_invalid",
            final_allowed_revision_ids=(
                "memory_revision_sha256_" + "f" * 64,
                "memory_revision_sha256_" + "f" * 64,
            ),
            final_blocked_revision_ids=(),
            reason="x" * 4_097,
            risk="low",
            recommended_injection="summary",
            input_tokens=2_147_483_648,
        )
    assert result_error.value.code == ("TBM_SEMANTIC_SERVICE_PROVIDER_RESULT_INVALID")
