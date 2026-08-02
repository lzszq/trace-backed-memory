from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import hashlib
import json
import re
from typing import Literal, NoReturn, Protocol, cast

from ._ingestion import parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .authorization_v3 import (
    AuthorizationDecision,
    AuthorizationPolicyBundle,
    AuthorizationRequest,
    parse_authorization_decision,
    parse_authorization_policy,
    verify_authorization_decision,
)
from .contracts_v3 import canonical_sha256
from .durable_finalization_v3 import (
    FINALIZATION_RENDERER_ID,
    FINALIZATION_RENDERER_VERSION,
)
from .event_registry_v1 import EventPayloadRegistration, EventTypeRegistry
from .event_v1 import CanonicalEvent, build_canonical_event, verify_event_parent
from .gate_evaluation_v3 import GATE_EVALUATION_MAX_DECISIONS
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_READ_PAGE,
    EventLedgerConflictError,
    EventLedgerPort,
    LedgerAccessContext,
    LedgerAppendReceipt,
    LedgerAppendRequest,
    LedgerIdempotency,
    LedgerTenantPartition,
    verify_ledger_append_receipt,
)
from .policy import (
    FULL_CASE_INJECTION_TEXT_MAX_CHARS,
    INJECTION_MAX_MEMORIES,
    INJECTION_SNIPPET_MAX_CHARS,
    INJECTION_TEXT_MAX_CHARS,
    LLM_GATE_MAX_CANDIDATES,
)
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    ReducerV1Error,
    execute_reducer_step,
    initial_reducer_state,
)
from .retrieval_policy_v3 import (
    RETRIEVAL_POLICY_CONTRACT_VERSION,
    RETRIEVAL_POLICY_MAX_PAYLOAD_BYTES,
    RETRIEVAL_RANKING_STAGES,
    RETRIEVAL_TASK_MODES,
    RetrievalPolicyBundle,
    TaskMode,
    loads_retrieval_policy,
)
from .retrieval_preparation_v3 import RETRIEVAL_PREPARATION_MAX_CANDIDATES


ACTIVE_POLICY_BUNDLE_CONTRACT_VERSION = "tbm.active-policy-bundle.v1"
ACTIVE_POLICY_EVENT_PROTOCOL_VERSION = "tbm.active-policy-event.v1"
ACTIVE_POLICY_EVENT_STREAM_TYPE = "active_policy"
ACTIVE_POLICY_EVENT_PROJECTION = "active_policy_current_v1"
ACTIVE_POLICY_EVENT_REDUCER_ID = "active-policy-current"
ACTIVE_POLICY_EVENT_MAX_BATCH = 16
ACTIVE_POLICY_EVENT_MAX_STREAM_EVENTS = 10_000
ACTIVE_POLICY_JSON_MAX_BYTES = 256 * 1024
ACTIVE_POLICY_JSON_MAX_DEPTH = 32
ACTIVE_POLICY_JSON_MAX_NODES = 20_000
ACTIVE_POLICY_BUNDLE_SCHEMA_ID = (
    "https://trace-backed-memory.dev/schemas/"
    "active_policy_bundle_v1.schema.json"
)
ACTIVE_POLICY_EVENT_PAYLOAD_SCHEMA_ID = (
    "https://trace-backed-memory.dev/schemas/"
    "active_policy_event_payload_registry_v1.schema.json"
)

POLICY_BUNDLE_REGISTERED = "tbm.policy.bundle_registered"
POLICY_BUNDLE_ACTIVATED = "tbm.policy.bundle_activated"
ACTIVE_POLICY_EVENT_TYPES = tuple(
    sorted((POLICY_BUNDLE_REGISTERED, POLICY_BUNDLE_ACTIVATED))
)
_PAYLOAD_SCHEMAS = {
    event_type: event_type + ".v1" for event_type in ACTIVE_POLICY_EVENT_TYPES
}

TrustTier = Literal["reviewed", "regression_verified", "causally_verified"]
TRUST_TIERS = (
    "reviewed",
    "regression_verified",
    "causally_verified",
)
_TRUST_TIER_ORDER = {value: index for index, value in enumerate(TRUST_TIERS)}
RendererMode = Literal["none", "short_summary", "full_case_summary"]
RENDERER_MODES = ("none", "short_summary", "full_case_summary")

_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_BUNDLE_ID_RE = re.compile(r"^active_policy_sha256_[0-9a-f]{64}$")
_RENDERER_POLICY_ID_RE = re.compile(
    r"^renderer_policy_sha256_[0-9a-f]{64}$"
)
_REGISTRATION_ID_RE = re.compile(
    r"^policy_registration_sha256_[0-9a-f]{64}$"
)
_ACTIVATION_ID_RE = re.compile(
    r"^policy_activation_sha256_[0-9a-f]{64}$"
)


class ActivePolicyEventV1Error(ReducerV1Error):
    """Stable active-policy contract, replay, and durable-ledger failure."""


def _fail(code: str, message: str) -> NoReturn:
    raise ActivePolicyEventV1Error(code, message)


def _record_invalid(message: str) -> NoReturn:
    _fail("TBM_ACTIVE_POLICY_RECORD_INVALID", message)


def _transition_invalid(message: str) -> NoReturn:
    _fail("TBM_ACTIVE_POLICY_TRANSITION_INVALID", message)


def _projection_invalid(message: str) -> NoReturn:
    _fail("TBM_ACTIVE_POLICY_PROJECTION_INVALID", message)


@dataclass(frozen=True)
class TrustTierPolicy:
    minimum_trust_tier: TrustTier
    require_active_revision: bool = True
    allow_legacy_unstructured: bool = False
    contract_version: str = "tbm.trust-tier-policy.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.trust-tier-policy.v1":
            _record_invalid("trust-tier policy version is unsupported")
        if (
            type(self.minimum_trust_tier) is not str
            or self.minimum_trust_tier not in TRUST_TIERS
        ):
            _record_invalid("minimum_trust_tier is unsupported")
        if self.require_active_revision is not True:
            _record_invalid("active policy must require an active revision")
        if self.allow_legacy_unstructured is not False:
            _record_invalid("legacy unstructured evidence cannot satisfy trust")

    @property
    def minimum_rank(self) -> int:
        return _TRUST_TIER_ORDER[self.minimum_trust_tier]

    def allows(self, trust_tier: TrustTier) -> bool:
        if type(trust_tier) is not str or trust_tier not in TRUST_TIERS:
            return False
        return _TRUST_TIER_ORDER[trust_tier] >= self.minimum_rank

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "minimum_trust_tier": self.minimum_trust_tier,
            "require_active_revision": self.require_active_revision,
            "allow_legacy_unstructured": self.allow_legacy_unstructured,
        }


@dataclass(frozen=True)
class CandidateBudget:
    discovery_max_candidates: int
    system_gate_max_candidates: int
    semantic_gate_max_candidates: int
    injection_max_memories: int
    payload_budget_bytes: int
    contract_version: str = "tbm.candidate-budget.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.candidate-budget.v1":
            _record_invalid("candidate budget version is unsupported")
        limits = (
            (
                "discovery_max_candidates",
                self.discovery_max_candidates,
                RETRIEVAL_PREPARATION_MAX_CANDIDATES,
            ),
            (
                "system_gate_max_candidates",
                self.system_gate_max_candidates,
                GATE_EVALUATION_MAX_DECISIONS,
            ),
            (
                "semantic_gate_max_candidates",
                self.semantic_gate_max_candidates,
                LLM_GATE_MAX_CANDIDATES,
            ),
            (
                "injection_max_memories",
                self.injection_max_memories,
                INJECTION_MAX_MEMORIES,
            ),
            (
                "payload_budget_bytes",
                self.payload_budget_bytes,
                RETRIEVAL_POLICY_MAX_PAYLOAD_BYTES,
            ),
        )
        for name, value, maximum in limits:
            if type(value) is not int or not 1 <= value <= maximum:
                _record_invalid(f"{name} is outside the supported bound")
        if not (
            self.injection_max_memories
            <= self.semantic_gate_max_candidates
            <= self.system_gate_max_candidates
            <= self.discovery_max_candidates
        ):
            _record_invalid("candidate budgets must narrow monotonically")

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "discovery_max_candidates": self.discovery_max_candidates,
            "system_gate_max_candidates": self.system_gate_max_candidates,
            "semantic_gate_max_candidates": self.semantic_gate_max_candidates,
            "injection_max_memories": self.injection_max_memories,
            "payload_budget_bytes": self.payload_budget_bytes,
        }


@dataclass(frozen=True)
class RendererPolicy:
    renderer_policy_id: str
    renderer_id: str
    renderer_version: str
    allowed_modes: tuple[RendererMode, ...]
    summary_item_max_chars: int
    full_item_max_chars: int
    max_memories: int
    snippet_max_chars: int
    snippet_max_utf8_bytes: int
    output_format: str
    media_type: str
    contract_version: str = "tbm.renderer-policy.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.renderer-policy.v1":
            _record_invalid("renderer policy version is unsupported")
        if (
            type(self.renderer_policy_id) is not str
            or _RENDERER_POLICY_ID_RE.fullmatch(self.renderer_policy_id) is None
        ):
            _record_invalid("renderer_policy_id is invalid")
        _identifier(self.renderer_id, "renderer_id")
        _identifier(self.renderer_version, "renderer_version")
        if (
            type(self.allowed_modes) is not tuple
            or self.allowed_modes != RENDERER_MODES
        ):
            _record_invalid("allowed_modes must use the complete canonical set")
        for name, value, maximum in (
            (
                "summary_item_max_chars",
                self.summary_item_max_chars,
                INJECTION_TEXT_MAX_CHARS,
            ),
            (
                "full_item_max_chars",
                self.full_item_max_chars,
                FULL_CASE_INJECTION_TEXT_MAX_CHARS,
            ),
            ("max_memories", self.max_memories, INJECTION_MAX_MEMORIES),
            (
                "snippet_max_chars",
                self.snippet_max_chars,
                INJECTION_SNIPPET_MAX_CHARS,
            ),
            (
                "snippet_max_utf8_bytes",
                self.snippet_max_utf8_bytes,
                INJECTION_SNIPPET_MAX_CHARS * 4,
            ),
        ):
            if type(value) is not int or not 1 <= value <= maximum:
                _record_invalid(f"{name} is outside the supported bound")
        if self.summary_item_max_chars > self.full_item_max_chars:
            _record_invalid("summary renderer limit cannot exceed full limit")
        if self.snippet_max_utf8_bytes < self.snippet_max_chars:
            _record_invalid("UTF-8 budget cannot be below the character budget")
        if self.output_format != "canonical-json-data-envelope":
            _record_invalid("renderer output format is unsupported")
        if self.media_type != "application/json":
            _record_invalid("renderer media type is unsupported")
        if self.renderer_policy_id != _content_id(
            "renderer_policy_sha256_", self._unsigned_dict()
        ):
            _fail(
                "TBM_ACTIVE_POLICY_HASH_MISMATCH",
                "renderer_policy_id does not match canonical content",
            )

    @property
    def renderer_sha256(self) -> str:
        return "sha256:" + self.renderer_policy_id.removeprefix(
            "renderer_policy_sha256_"
        )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "renderer_id": self.renderer_id,
            "renderer_version": self.renderer_version,
            "allowed_modes": list(self.allowed_modes),
            "summary_item_max_chars": self.summary_item_max_chars,
            "full_item_max_chars": self.full_item_max_chars,
            "max_memories": self.max_memories,
            "snippet_max_chars": self.snippet_max_chars,
            "snippet_max_utf8_bytes": self.snippet_max_utf8_bytes,
            "output_format": self.output_format,
            "media_type": self.media_type,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "renderer_policy_id": self.renderer_policy_id,
            **self._unsigned_dict(),
        }


def build_current_renderer_policy() -> RendererPolicy:
    values: dict[str, object] = {
        "contract_version": "tbm.renderer-policy.v1",
        "renderer_id": FINALIZATION_RENDERER_ID,
        "renderer_version": FINALIZATION_RENDERER_VERSION,
        "allowed_modes": list(RENDERER_MODES),
        "summary_item_max_chars": INJECTION_TEXT_MAX_CHARS,
        "full_item_max_chars": FULL_CASE_INJECTION_TEXT_MAX_CHARS,
        "max_memories": INJECTION_MAX_MEMORIES,
        "snippet_max_chars": INJECTION_SNIPPET_MAX_CHARS,
        "snippet_max_utf8_bytes": INJECTION_SNIPPET_MAX_CHARS * 4,
        "output_format": "canonical-json-data-envelope",
        "media_type": "application/json",
    }
    return RendererPolicy(
        renderer_policy_id=_content_id("renderer_policy_sha256_", values),
        renderer_id=FINALIZATION_RENDERER_ID,
        renderer_version=FINALIZATION_RENDERER_VERSION,
        allowed_modes=RENDERER_MODES,
        summary_item_max_chars=INJECTION_TEXT_MAX_CHARS,
        full_item_max_chars=FULL_CASE_INJECTION_TEXT_MAX_CHARS,
        max_memories=INJECTION_MAX_MEMORIES,
        snippet_max_chars=INJECTION_SNIPPET_MAX_CHARS,
        snippet_max_utf8_bytes=INJECTION_SNIPPET_MAX_CHARS * 4,
        output_format="canonical-json-data-envelope",
        media_type="application/json",
    )


@dataclass(frozen=True)
class ActivePolicyBundle:
    policy_bundle_id: str
    retrieval_policy: RetrievalPolicyBundle
    trust_tier_policy: TrustTierPolicy
    candidate_budget: CandidateBudget
    renderer_policy: RendererPolicy
    semantic_gate_required: bool
    contract_version: str = ACTIVE_POLICY_BUNDLE_CONTRACT_VERSION

    def __post_init__(self) -> None:
        if self.contract_version != ACTIVE_POLICY_BUNDLE_CONTRACT_VERSION:
            _record_invalid("active policy bundle version is unsupported")
        if (
            type(self.policy_bundle_id) is not str
            or _BUNDLE_ID_RE.fullmatch(self.policy_bundle_id) is None
        ):
            _record_invalid("policy_bundle_id is invalid")
        if type(self.retrieval_policy) is not RetrievalPolicyBundle:
            _record_invalid("retrieval_policy must be exact")
        if type(self.trust_tier_policy) is not TrustTierPolicy:
            _record_invalid("trust_tier_policy must be exact")
        if type(self.candidate_budget) is not CandidateBudget:
            _record_invalid("candidate_budget must be exact")
        if type(self.renderer_policy) is not RendererPolicy:
            _record_invalid("renderer_policy must be exact")
        if self.semantic_gate_required is not True:
            _record_invalid("Semantic Gate must remain required")
        if (
            self.candidate_budget.payload_budget_bytes
            != self.retrieval_policy.payload_budget_bytes
        ):
            _record_invalid(
                "candidate payload budget must match retrieval policy"
            )
        if self.candidate_budget.injection_max_memories != (
            self.renderer_policy.max_memories
        ):
            _record_invalid("candidate and renderer injection limits disagree")
        if self.policy_bundle_id != _content_id(
            "active_policy_sha256_", self._unsigned_dict()
        ):
            _fail(
                "TBM_ACTIVE_POLICY_HASH_MISMATCH",
                "policy_bundle_id does not match canonical content",
            )

    @property
    def policy_bundle_sha256(self) -> str:
        return "sha256:" + self.policy_bundle_id.removeprefix(
            "active_policy_sha256_"
        )

    @property
    def task_modes(self) -> tuple[TaskMode, ...]:
        return cast(tuple[TaskMode, ...], RETRIEVAL_TASK_MODES)

    @property
    def ancestry_mode(self) -> str:
        return self.retrieval_policy.ancestry_mode

    @property
    def allowed_classifications(self) -> tuple[str, ...]:
        return cast(tuple[str, ...], self.retrieval_policy.allowed_classifications)

    @property
    def block_eval_leaking(self) -> bool:
        return self.retrieval_policy.block_eval_leaking

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "retrieval_policy": self.retrieval_policy.to_dict(),
            "trust_tier_policy": self.trust_tier_policy.to_dict(),
            "candidate_budget": self.candidate_budget.to_dict(),
            "renderer_policy": self.renderer_policy.to_dict(),
            "semantic_gate_required": self.semantic_gate_required,
        }

    def to_dict(self) -> dict[str, object]:
        return {
            "policy_bundle_id": self.policy_bundle_id,
            **self._unsigned_dict(),
        }


def build_active_policy_bundle(
    *,
    retrieval_policy: RetrievalPolicyBundle,
    minimum_trust_tier: TrustTier,
    discovery_max_candidates: int = RETRIEVAL_PREPARATION_MAX_CANDIDATES,
    system_gate_max_candidates: int = GATE_EVALUATION_MAX_DECISIONS,
    semantic_gate_max_candidates: int = LLM_GATE_MAX_CANDIDATES,
    injection_max_memories: int = INJECTION_MAX_MEMORIES,
    renderer_policy: RendererPolicy | None = None,
) -> ActivePolicyBundle:
    if type(retrieval_policy) is not RetrievalPolicyBundle:
        _record_invalid("retrieval_policy must be exact")
    renderer = (
        build_current_renderer_policy()
        if renderer_policy is None
        else renderer_policy
    )
    trust_policy = TrustTierPolicy(minimum_trust_tier=minimum_trust_tier)
    budget = CandidateBudget(
        discovery_max_candidates=discovery_max_candidates,
        system_gate_max_candidates=system_gate_max_candidates,
        semantic_gate_max_candidates=semantic_gate_max_candidates,
        injection_max_memories=injection_max_memories,
        payload_budget_bytes=retrieval_policy.payload_budget_bytes,
    )
    values: dict[str, object] = {
        "contract_version": ACTIVE_POLICY_BUNDLE_CONTRACT_VERSION,
        "retrieval_policy": retrieval_policy.to_dict(),
        "trust_tier_policy": trust_policy.to_dict(),
        "candidate_budget": budget.to_dict(),
        "renderer_policy": renderer.to_dict(),
        "semantic_gate_required": True,
    }
    return ActivePolicyBundle(
        policy_bundle_id=_content_id("active_policy_sha256_", values),
        retrieval_policy=retrieval_policy,
        trust_tier_policy=trust_policy,
        candidate_budget=budget,
        renderer_policy=renderer,
        semantic_gate_required=True,
    )


@dataclass(frozen=True)
class ActivePolicyRegistration:
    registration_id: str
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    policy_bundle: ActivePolicyBundle
    registered_by: str
    registered_via_client_id: str
    authorization_event_id: str
    registered_at: str
    contract_version: str = "tbm.active-policy-registration.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.active-policy-registration.v1":
            _record_invalid("policy registration version is unsupported")
        if (
            type(self.registration_id) is not str
            or _REGISTRATION_ID_RE.fullmatch(self.registration_id) is None
        ):
            _record_invalid("registration_id is invalid")
        _target_partition(self)
        if type(self.policy_bundle) is not ActivePolicyBundle:
            _record_invalid("registration requires ActivePolicyBundle")
        _identifier(self.registered_by, "registered_by")
        _identifier(
            self.registered_via_client_id,
            "registered_via_client_id",
        )
        _identifier(self.authorization_event_id, "authorization_event_id")
        _timestamp(self.registered_at, "registered_at")
        if self.registration_id != _content_id(
            "policy_registration_sha256_", self._unsigned_dict()
        ):
            _fail(
                "TBM_ACTIVE_POLICY_HASH_MISMATCH",
                "registration_id does not match canonical content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "environment_id": self.environment_id,
            "policy_bundle": self.policy_bundle.to_dict(),
            "registered_by": self.registered_by,
            "registered_via_client_id": self.registered_via_client_id,
            "authorization_event_id": self.authorization_event_id,
            "registered_at": self.registered_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {"registration_id": self.registration_id, **self._unsigned_dict()}


@dataclass(frozen=True)
class ActivePolicyActivation:
    activation_id: str
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    policy_bundle_id: str
    registration_id: str
    previous_policy_bundle_id: str | None
    activated_by: str
    activated_via_client_id: str
    authorization_event_id: str
    activated_at: str
    contract_version: str = "tbm.active-policy-activation.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.active-policy-activation.v1":
            _record_invalid("policy activation version is unsupported")
        if (
            type(self.activation_id) is not str
            or _ACTIVATION_ID_RE.fullmatch(self.activation_id) is None
        ):
            _record_invalid("activation_id is invalid")
        _target_partition(self)
        if (
            type(self.policy_bundle_id) is not str
            or _BUNDLE_ID_RE.fullmatch(self.policy_bundle_id) is None
        ):
            _record_invalid("policy_bundle_id is invalid")
        if (
            type(self.registration_id) is not str
            or _REGISTRATION_ID_RE.fullmatch(self.registration_id) is None
        ):
            _record_invalid("registration_id is invalid")
        if self.previous_policy_bundle_id is not None and (
            type(self.previous_policy_bundle_id) is not str
            or _BUNDLE_ID_RE.fullmatch(self.previous_policy_bundle_id) is None
        ):
            _record_invalid("previous_policy_bundle_id is invalid")
        if self.previous_policy_bundle_id == self.policy_bundle_id:
            _record_invalid("policy activation cannot name itself as predecessor")
        _identifier(self.activated_by, "activated_by")
        _identifier(self.activated_via_client_id, "activated_via_client_id")
        _identifier(self.authorization_event_id, "authorization_event_id")
        _timestamp(self.activated_at, "activated_at")
        if self.activation_id != _content_id(
            "policy_activation_sha256_", self._unsigned_dict()
        ):
            _fail(
                "TBM_ACTIVE_POLICY_HASH_MISMATCH",
                "activation_id does not match canonical content",
            )

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "environment_id": self.environment_id,
            "policy_bundle_id": self.policy_bundle_id,
            "registration_id": self.registration_id,
            "previous_policy_bundle_id": self.previous_policy_bundle_id,
            "activated_by": self.activated_by,
            "activated_via_client_id": self.activated_via_client_id,
            "authorization_event_id": self.authorization_event_id,
            "activated_at": self.activated_at,
        }

    def to_dict(self) -> dict[str, object]:
        return {"activation_id": self.activation_id, **self._unsigned_dict()}


@dataclass(frozen=True)
class StoredActivePolicyRegistration:
    registration: ActivePolicyRegistration
    policy: AuthorizationPolicyBundle
    request: AuthorizationRequest
    decision: AuthorizationDecision
    attestation_verified_by: str

    def __post_init__(self) -> None:
        if type(self.registration) is not ActivePolicyRegistration:
            _record_invalid("stored registration is invalid")
        _stored_authorization_records(
            self.policy,
            self.request,
            self.decision,
            self.attestation_verified_by,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": "tbm.stored-active-policy-registration.v1",
            "registration": self.registration.to_dict(),
            "policy": self.policy.to_dict(),
            "request": self.request.to_dict(),
            "decision": self.decision.to_dict(),
            "attestation_verified_by": self.attestation_verified_by,
        }


@dataclass(frozen=True)
class StoredActivePolicyActivation:
    activation: ActivePolicyActivation
    policy: AuthorizationPolicyBundle
    request: AuthorizationRequest
    decision: AuthorizationDecision
    attestation_verified_by: str

    def __post_init__(self) -> None:
        if type(self.activation) is not ActivePolicyActivation:
            _record_invalid("stored activation is invalid")
        _stored_authorization_records(
            self.policy,
            self.request,
            self.decision,
            self.attestation_verified_by,
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "contract_version": "tbm.stored-active-policy-activation.v1",
            "activation": self.activation.to_dict(),
            "policy": self.policy.to_dict(),
            "request": self.request.to_dict(),
            "decision": self.decision.to_dict(),
            "attestation_verified_by": self.attestation_verified_by,
        }


ActivePolicyRecord = StoredActivePolicyRegistration | StoredActivePolicyActivation


def build_active_policy_registration(
    *,
    partition: LedgerTenantPartition,
    policy_bundle: ActivePolicyBundle,
    registered_by: str,
    registered_via_client_id: str,
    authorization_event_id: str,
    registered_at: str,
) -> ActivePolicyRegistration:
    if type(partition) is not LedgerTenantPartition:
        _record_invalid("partition must be LedgerTenantPartition")
    values: dict[str, object] = {
        "contract_version": "tbm.active-policy-registration.v1",
        "organization_id": partition.organization_id,
        "tenant_id": partition.tenant_id,
        "repository_id": partition.repository_id,
        "environment_id": partition.environment_id,
        "policy_bundle": policy_bundle.to_dict(),
        "registered_by": registered_by,
        "registered_via_client_id": registered_via_client_id,
        "authorization_event_id": authorization_event_id,
        "registered_at": _timestamp(registered_at, "registered_at"),
    }
    return ActivePolicyRegistration(
        registration_id=_content_id("policy_registration_sha256_", values),
        organization_id=partition.organization_id,
        tenant_id=partition.tenant_id,
        repository_id=partition.repository_id,
        environment_id=partition.environment_id,
        policy_bundle=policy_bundle,
        registered_by=registered_by,
        registered_via_client_id=registered_via_client_id,
        authorization_event_id=authorization_event_id,
        registered_at=cast(str, values["registered_at"]),
    )


def build_active_policy_activation(
    *,
    registration: ActivePolicyRegistration,
    previous_policy_bundle_id: str | None,
    activated_by: str,
    activated_via_client_id: str,
    authorization_event_id: str,
    activated_at: str,
) -> ActivePolicyActivation:
    if type(registration) is not ActivePolicyRegistration:
        _record_invalid("registration must be ActivePolicyRegistration")
    canonical_activated_at = _timestamp(activated_at, "activated_at")
    values: dict[str, object] = {
        "contract_version": "tbm.active-policy-activation.v1",
        "organization_id": registration.organization_id,
        "tenant_id": registration.tenant_id,
        "repository_id": registration.repository_id,
        "environment_id": registration.environment_id,
        "policy_bundle_id": registration.policy_bundle.policy_bundle_id,
        "registration_id": registration.registration_id,
        "previous_policy_bundle_id": previous_policy_bundle_id,
        "activated_by": activated_by,
        "activated_via_client_id": activated_via_client_id,
        "authorization_event_id": authorization_event_id,
        "activated_at": canonical_activated_at,
    }
    return ActivePolicyActivation(
        activation_id=_content_id("policy_activation_sha256_", values),
        organization_id=registration.organization_id,
        tenant_id=registration.tenant_id,
        repository_id=registration.repository_id,
        environment_id=registration.environment_id,
        policy_bundle_id=registration.policy_bundle.policy_bundle_id,
        registration_id=registration.registration_id,
        previous_policy_bundle_id=previous_policy_bundle_id,
        activated_by=activated_by,
        activated_via_client_id=activated_via_client_id,
        authorization_event_id=authorization_event_id,
        activated_at=canonical_activated_at,
    )


@dataclass(frozen=True)
class ActivePolicyHead:
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    policy_bundle_id: str
    retrieval_policy_id: str
    renderer_policy_id: str
    registration_id: str
    activation_id: str
    previous_policy_bundle_id: str | None
    registration_authorization_event_id: str
    activation_authorization_event_id: str
    registration_attestation_verified_by: str
    activation_attestation_verified_by: str
    activated_by: str
    activated_at: str
    source_event_sha256: str
    head_sha256: str
    contract_version: str = "tbm.active-policy-head.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.active-policy-head.v1":
            _projection_invalid("active policy head version is unsupported")
        _target_partition(self)
        for value, pattern, name in (
            (self.policy_bundle_id, _BUNDLE_ID_RE, "policy_bundle_id"),
            (self.renderer_policy_id, _RENDERER_POLICY_ID_RE, "renderer_policy_id"),
            (self.registration_id, _REGISTRATION_ID_RE, "registration_id"),
            (self.activation_id, _ACTIVATION_ID_RE, "activation_id"),
        ):
            if type(value) is not str or pattern.fullmatch(value) is None:
                _projection_invalid(f"{name} is invalid")
        _identifier(self.retrieval_policy_id, "retrieval_policy_id")
        if self.previous_policy_bundle_id is not None and (
            type(self.previous_policy_bundle_id) is not str
            or _BUNDLE_ID_RE.fullmatch(self.previous_policy_bundle_id) is None
        ):
            _projection_invalid("previous policy bundle is invalid")
        for name in (
            "registration_authorization_event_id",
            "activation_authorization_event_id",
            "registration_attestation_verified_by",
            "activation_attestation_verified_by",
            "activated_by",
        ):
            _identifier(getattr(self, name), name)
        _timestamp(self.activated_at, "activated_at")
        _digest(self.source_event_sha256, "source_event_sha256")
        _digest(self.head_sha256, "head_sha256")
        if self.head_sha256 != canonical_sha256(self._unsigned_dict()):
            _projection_invalid("active policy head digest does not match")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "organization_id": self.organization_id,
            "tenant_id": self.tenant_id,
            "repository_id": self.repository_id,
            "environment_id": self.environment_id,
            "policy_bundle_id": self.policy_bundle_id,
            "retrieval_policy_id": self.retrieval_policy_id,
            "renderer_policy_id": self.renderer_policy_id,
            "registration_id": self.registration_id,
            "activation_id": self.activation_id,
            "previous_policy_bundle_id": self.previous_policy_bundle_id,
            "registration_authorization_event_id": (
                self.registration_authorization_event_id
            ),
            "activation_authorization_event_id": (
                self.activation_authorization_event_id
            ),
            "registration_attestation_verified_by": (
                self.registration_attestation_verified_by
            ),
            "activation_attestation_verified_by": (
                self.activation_attestation_verified_by
            ),
            "activated_by": self.activated_by,
            "activated_at": self.activated_at,
            "source_event_sha256": self.source_event_sha256,
        }

    def to_dict(self) -> dict[str, object]:
        return {**self._unsigned_dict(), "head_sha256": self.head_sha256}


@dataclass(frozen=True)
class ActivePolicyProjection:
    organization_id: str
    tenant_id: str
    repository_id: str
    environment_id: str
    registrations: tuple[StoredActivePolicyRegistration, ...]
    activations: tuple[StoredActivePolicyActivation, ...]
    active_head: ActivePolicyHead | None
    last_event_sha256: str
    last_global_position: int

    def __post_init__(self) -> None:
        partition = _target_partition(self)
        if (
            type(self.registrations) is not tuple
            or not self.registrations
            or any(
                type(item) is not StoredActivePolicyRegistration
                for item in self.registrations
            )
        ):
            _projection_invalid("registrations are invalid")
        if (
            type(self.activations) is not tuple
            or any(
                type(item) is not StoredActivePolicyActivation
                for item in self.activations
            )
        ):
            _projection_invalid("activations are invalid")
        registration_ids = [
            item.registration.registration_id for item in self.registrations
        ]
        bundle_ids = [
            item.registration.policy_bundle.policy_bundle_id
            for item in self.registrations
        ]
        if (
            len(set(registration_ids)) != len(registration_ids)
            or len(set(bundle_ids)) != len(bundle_ids)
        ):
            _projection_invalid("registrations are duplicated")
        for item in self.registrations:
            if _target_partition(item.registration) != partition:
                _projection_invalid("registration crossed projection partition")
        for item in self.activations:
            if _target_partition(item.activation) != partition:
                _projection_invalid("activation crossed projection partition")
        if self.active_head is not None:
            if type(self.active_head) is not ActivePolicyHead:
                _projection_invalid("active_head is invalid")
            if _target_partition(self.active_head) != partition:
                _projection_invalid("active head crossed projection partition")
            registration = self._registration_by_bundle(
                self.active_head.policy_bundle_id
            )
            if (
                registration.registration.registration_id
                != self.active_head.registration_id
            ):
                _projection_invalid("active head registration does not match")
            matching = [
                item
                for item in self.activations
                if item.activation.activation_id == self.active_head.activation_id
            ]
            if len(matching) != 1:
                _projection_invalid("active head activation does not match")
            activation = matching[0]
            bundle = registration.registration.policy_bundle
            if (
                activation.activation.policy_bundle_id
                != self.active_head.policy_bundle_id
                or activation.activation.registration_id
                != self.active_head.registration_id
                or activation.activation.previous_policy_bundle_id
                != self.active_head.previous_policy_bundle_id
                or bundle.retrieval_policy.policy_id
                != self.active_head.retrieval_policy_id
                or bundle.renderer_policy.renderer_policy_id
                != self.active_head.renderer_policy_id
                or registration.registration.authorization_event_id
                != self.active_head.registration_authorization_event_id
                or activation.activation.authorization_event_id
                != self.active_head.activation_authorization_event_id
                or registration.attestation_verified_by
                != self.active_head.registration_attestation_verified_by
                or activation.attestation_verified_by
                != self.active_head.activation_attestation_verified_by
                or activation.activation.activated_by
                != self.active_head.activated_by
                or activation.activation.activated_at
                != self.active_head.activated_at
            ):
                _projection_invalid(
                    "active head fields do not match retained policy records"
                )
        _digest(self.last_event_sha256, "last_event_sha256")
        if type(self.last_global_position) is not int or self.last_global_position < 1:
            _projection_invalid("last_global_position is invalid")

    def _registration_by_bundle(
        self, policy_bundle_id: str
    ) -> StoredActivePolicyRegistration:
        for item in self.registrations:
            if item.registration.policy_bundle.policy_bundle_id == policy_bundle_id:
                return item
        _projection_invalid("active policy registration is missing")

    def load_active_policy(self) -> ActivePolicyBundle:
        if self.active_head is None:
            _fail(
                "TBM_ACTIVE_POLICY_HEAD_MISSING",
                "active policy projection has no activated head",
            )
        return self._registration_by_bundle(
            self.active_head.policy_bundle_id
        ).registration.policy_bundle

    def verify_head(self, head: ActivePolicyHead) -> None:
        if type(head) is not ActivePolicyHead or self.active_head != head:
            _fail(
                "TBM_ACTIVE_POLICY_HEAD_INVALID",
                "active policy head does not match event replay",
            )

    def __call__(self) -> RetrievalPolicyBundle:
        return self.load_active_policy().retrieval_policy


@dataclass(frozen=True)
class ActivePolicyAppendResult:
    receipt: LedgerAppendReceipt
    projection: ActivePolicyProjection

    def __post_init__(self) -> None:
        if type(self.receipt) is not LedgerAppendReceipt:
            _projection_invalid("append receipt is invalid")
        if type(self.projection) is not ActivePolicyProjection:
            _projection_invalid("append projection is invalid")


@dataclass(frozen=True)
class DurableActivePolicySnapshot:
    projection: ActivePolicyProjection
    partition_sha256: str
    reducer_descriptor_sha256: str
    reducer_configuration_sha256: str
    stream_version: int
    source_event_count: int
    snapshot_sha256: str
    contract_version: str = "tbm.durable-active-policy-snapshot.v1"

    def __post_init__(self) -> None:
        if self.contract_version != "tbm.durable-active-policy-snapshot.v1":
            _projection_invalid("durable snapshot version is unsupported")
        if type(self.projection) is not ActivePolicyProjection:
            _projection_invalid("durable snapshot projection is invalid")
        for name in (
            "partition_sha256",
            "reducer_descriptor_sha256",
            "reducer_configuration_sha256",
            "snapshot_sha256",
        ):
            _digest(getattr(self, name), name)
        if self.partition_sha256 != _target_partition(
            self.projection
        ).partition_sha256:
            _projection_invalid(
                "durable snapshot partition does not match its projection"
            )
        if (
            type(self.stream_version) is not int
            or self.stream_version < 1
            or type(self.source_event_count) is not int
            or self.source_event_count != self.stream_version
        ):
            _projection_invalid("durable snapshot event count is invalid")
        if self.snapshot_sha256 != canonical_sha256(self._unsigned_dict()):
            _projection_invalid("durable snapshot digest does not match")

    def _unsigned_dict(self) -> dict[str, object]:
        return {
            "contract_version": self.contract_version,
            "partition_sha256": self.partition_sha256,
            "reducer_descriptor_sha256": self.reducer_descriptor_sha256,
            "reducer_configuration_sha256": self.reducer_configuration_sha256,
            "stream_version": self.stream_version,
            "source_event_count": self.source_event_count,
            "projection": _projection_digest_value(self.projection),
        }

    def load_active_policy(self) -> ActivePolicyBundle:
        return self.projection.load_active_policy()

    def verify_head(self, head: ActivePolicyHead) -> None:
        self.projection.verify_head(head)

    def __call__(self) -> RetrievalPolicyBundle:
        return self.projection()


class ActivePolicyHeadReader(Protocol):
    def load_active_policy(self) -> ActivePolicyBundle: ...

    def verify_head(self, head: ActivePolicyHead) -> None: ...


def active_policy_stream_id(partition: LedgerTenantPartition) -> str:
    if type(partition) is not LedgerTenantPartition:
        _record_invalid("partition must be LedgerTenantPartition")
    return "active_policy_" + partition.partition_sha256.removeprefix("sha256:")


def build_active_policy_event_batch(
    access: LedgerAccessContext,
    records: tuple[ActivePolicyRecord, ...],
    *,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> tuple[tuple[CanonicalEvent, ...], LedgerIdempotency]:
    if type(access) is not LedgerAccessContext:
        _fail("TBM_ACTIVE_POLICY_ACCESS_INVALID", "access is invalid")
    if (
        type(records) is not tuple
        or not 1 <= len(records) <= ACTIVE_POLICY_EVENT_MAX_BATCH
    ):
        _fail(
            "TBM_ACTIVE_POLICY_BATCH_INVALID",
            "records must be a bounded non-empty tuple",
        )
    if type(expected_stream_version) is not int or expected_stream_version < 0:
        _fail(
            "TBM_ACTIVE_POLICY_BATCH_INVALID",
            "expected_stream_version is invalid",
        )
    if type(next_global_position) is not int or next_global_position < 1:
        _fail(
            "TBM_ACTIVE_POLICY_BATCH_INVALID",
            "next_global_position is invalid",
        )
    canonical_recorded_at = _timestamp(recorded_at, "recorded_at")
    descriptors = tuple(_record_descriptor(record) for record in records)
    stream_id = active_policy_stream_id(access.partition)
    if previous_event is None:
        if expected_stream_version != 0:
            _fail(
                "TBM_ACTIVE_POLICY_BATCH_INVALID",
                "nonzero stream version requires its parent",
            )
    elif (
        type(previous_event) is not CanonicalEvent
        or previous_event.stream_id != stream_id
        or previous_event.stream_version != expected_stream_version
    ):
        _fail(
            "TBM_ACTIVE_POLICY_BATCH_INVALID",
            "previous event does not match the active-policy head",
        )
    command_value = {
        "protocol_version": ACTIVE_POLICY_EVENT_PROTOCOL_VERSION,
        "partition_sha256": access.partition.partition_sha256,
        "stream_id": stream_id,
        "expected_stream_version": expected_stream_version,
        "records": [item[3] for item in descriptors],
    }
    command_sha256 = _domain_sha256(
        b"tbm.active-policy-event-command.v1\x00", command_value
    )
    idempotency = LedgerIdempotency(
        idempotency_key_sha256=_domain_sha256(
            b"tbm.active-policy-event-idempotency.v1\x00", command_value
        ),
        command_sha256=command_sha256,
    )
    trusted_context = access.event_trusted_context()
    parent = previous_event
    events: list[CanonicalEvent] = []
    for offset, descriptor in enumerate(descriptors):
        event_type, policy_bundle_id, occurred_at, record_dict, actor_id = (
            descriptor[:5]
        )
        client_id = descriptor[5]
        authorization_event_id = descriptor[6]
        partition = descriptor[7]
        _verify_record_access(
            access,
            partition=partition,
            actor_id=actor_id,
            client_id=client_id,
            authorization_event_id=authorization_event_id,
        )
        event_digest = hashlib.sha256(
            (command_sha256 + f"\x00{offset}").encode("utf-8")
        ).hexdigest()
        payload = {
            "policy_bundle_id": policy_bundle_id,
            "record_type": event_type,
            "record_sha256": canonical_sha256(record_dict),
            "record_json": _canonical_json(record_dict),
        }
        event = build_canonical_event(
            event_id="evt_ap_" + event_digest,
            event_type=event_type,
            event_version=1,
            event_kind="domain",
            origin="native",
            source=None,
            stream_id=stream_id,
            stream_type=ACTIVE_POLICY_EVENT_STREAM_TYPE,
            stream_version=expected_stream_version + offset + 1,
            global_position=next_global_position + offset,
            trusted_context=trusted_context,
            request_id="request_ap_" + event_digest[:32],
            idempotency_key_sha256=idempotency.idempotency_key_sha256,
            request_sha256=command_sha256,
            correlation_id="correlation_ap_" + stream_id[-32:],
            causation_id=None if parent is None else parent.event_id,
            occurred_at=occurred_at,
            recorded_at=canonical_recorded_at,
            producer="tbm_active_policy_runtime",
            producer_version="f4-v1",
            payload_schema=_PAYLOAD_SCHEMAS[event_type],
            previous_stream_event_sha256=(
                None if parent is None else parent.event_sha256
            ),
            classification="internal",
            retention_policy_id="retention_active_policy_events",
            artifact_refs=(),
            payload=payload,
        )
        events.append(event)
        parent = event
    return tuple(events), idempotency


def build_active_policy_event_registry() -> EventTypeRegistry:
    registry = EventTypeRegistry()
    schemas = _payload_json_schemas()
    for event_type in ACTIVE_POLICY_EVENT_TYPES:
        registry.register(
            EventPayloadRegistration(
                event_type=event_type,
                event_version=1,
                event_kind="domain",
                payload_schema=_PAYLOAD_SCHEMAS[event_type],
                schema=schemas[event_type],
            )
        )
    return registry.seal()


def active_policy_event_payload_dispatch_schema() -> dict[str, object]:
    schema = build_active_policy_event_registry().dispatch_schema()
    schema["$id"] = ACTIVE_POLICY_EVENT_PAYLOAD_SCHEMA_ID
    schema["title"] = "Trace-backed Memory active-policy event payloads v1"
    schema["$comment"] = (
        "Generated from the sealed active-policy event registry; exact stored "
        "authorization records remain authoritative during replay."
    )
    return schema


def dumps_active_policy_event_payload_dispatch_schema() -> str:
    return json.dumps(
        active_policy_event_payload_dispatch_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def build_active_policy_reducer(
    *, trusted_attestation_verifier_ids: tuple[str, ...]
) -> FunctionalReducer:
    trusted_verifiers = _trusted_verifier_set(
        trusted_attestation_verifier_ids
    )
    descriptor = ReducerDescriptor(
        reducer_id=ACTIVE_POLICY_EVENT_REDUCER_ID,
        reducer_version=1,
        input_event_types=ACTIVE_POLICY_EVENT_TYPES,
        output_projection=ACTIVE_POLICY_EVENT_PROJECTION,
        output_schema_version=1,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": "active-policy-current",
                "algorithm_version": 1,
                "event_types": list(ACTIVE_POLICY_EVENT_TYPES),
                "head_source": POLICY_BUNDLE_ACTIVATED,
            },
        ),
        configuration_sha256=_domain_sha256(
            b"tbm.reducer-configuration.v1\x00",
            {
                "configuration": "trusted-attestation-verifiers",
                "trusted_attestation_verifier_ids": sorted(trusted_verifiers),
                "version": 1,
            },
        ),
        target_event_versions={event_type: 1 for event_type in ACTIVE_POLICY_EVENT_TYPES},
    )

    def initial() -> Mapping[str, object]:
        return {
            "organization_id": None,
            "tenant_id": None,
            "repository_id": None,
            "environment_id": None,
            "registrations": {},
            "activations": [],
            "head": None,
            "last_event_sha256": None,
            "last_global_position": 0,
        }

    def transition(
        state: Mapping[str, object], reducer_event: ReducerEvent
    ) -> Mapping[str, object]:
        payload = _typed_payload(reducer_event)
        event = reducer_event.source_event
        partition = _event_partition(event)
        for name in (
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
        ):
            if state.get(name) not in {None, getattr(partition, name)}:
                _transition_invalid("active-policy stream crossed a partition")
        registrations = _state_mapping(state, "registrations")
        activations = _state_list(state, "activations")
        head = _optional_state_mapping(state.get("head"), "head")
        record = _load_record(event.event_type, cast(str, payload["record_json"]))
        _verify_loaded_record(payload, record, event)
        if event.event_type == POLICY_BUNDLE_REGISTERED:
            stored = cast(StoredActivePolicyRegistration, record)
            registration = stored.registration
            _verify_stored_authorization(
                stored.policy,
                stored.request,
                stored.decision,
                permission="policy:create_global",
                actor_id=registration.registered_by,
                client_id=registration.registered_via_client_id,
                authorization_event_id=registration.authorization_event_id,
                occurred_at=registration.registered_at,
                event=event,
            )
            if stored.attestation_verified_by not in trusted_verifiers:
                _transition_invalid("registration attestation verifier is not trusted")
            bundle_id = registration.policy_bundle.policy_bundle_id
            if bundle_id in registrations:
                _transition_invalid("policy bundle registration is duplicated")
            registrations[bundle_id] = _canonical_json(stored.to_dict())
        elif event.event_type == POLICY_BUNDLE_ACTIVATED:
            stored = cast(StoredActivePolicyActivation, record)
            activation = stored.activation
            _verify_stored_authorization(
                stored.policy,
                stored.request,
                stored.decision,
                permission="policy:approve_global",
                actor_id=activation.activated_by,
                client_id=activation.activated_via_client_id,
                authorization_event_id=activation.authorization_event_id,
                occurred_at=activation.activated_at,
                event=event,
            )
            if stored.attestation_verified_by not in trusted_verifiers:
                _transition_invalid("activation attestation verifier is not trusted")
            registration_json = registrations.get(activation.policy_bundle_id)
            if type(registration_json) is not str:
                _transition_invalid("activated policy bundle is not registered")
            registered = loads_active_policy_registration_publication(
                registration_json
            )
            registration = registered.registration
            if (
                activation.registration_id != registration.registration_id
                or activation.activated_by == registration.registered_by
                or parse_rfc3339(activation.activated_at)
                < parse_rfc3339(registration.registered_at)
            ):
                _transition_invalid("policy activation does not match registration")
            current_bundle_id = None if head is None else head.get("policy_bundle_id")
            if activation.previous_policy_bundle_id != current_bundle_id:
                _transition_invalid("policy activation predecessor is stale")
            if head is not None and parse_rfc3339(
                activation.activated_at
            ) <= parse_rfc3339(cast(str, head.get("activated_at"))):
                _transition_invalid("policy activation time is not forward-only")
            if any(
                loads_active_policy_activation_publication(value).activation.activation_id
                == activation.activation_id
                for value in activations
                if type(value) is str
            ):
                _transition_invalid("policy activation is duplicated")
            activations.append(_canonical_json(stored.to_dict()))
            head = _head_state(
                registration=registered,
                activation=stored,
                event=event,
            )
        else:  # pragma: no cover - sealed registry prevents this
            _transition_invalid("active-policy event type is unsupported")
        return {
            "organization_id": partition.organization_id,
            "tenant_id": partition.tenant_id,
            "repository_id": partition.repository_id,
            "environment_id": partition.environment_id,
            "registrations": registrations,
            "activations": activations,
            "head": head,
            "last_event_sha256": event.event_sha256,
            "last_global_position": event.global_position,
        }

    return FunctionalReducer(descriptor, initial, transition)


def reduce_active_policy_events(
    events: tuple[CanonicalEvent, ...],
    *,
    trusted_attestation_verifier_ids: tuple[str, ...],
    event_registry: EventTypeRegistry | None = None,
) -> ActivePolicyProjection:
    if (
        type(events) is not tuple
        or not events
        or len(events) > ACTIVE_POLICY_EVENT_MAX_STREAM_EVENTS
        or any(type(event) is not CanonicalEvent for event in events)
    ):
        _fail(
            "TBM_ACTIVE_POLICY_EVENT_SEQUENCE_INVALID",
            "events must be a bounded non-empty CanonicalEvent tuple",
        )
    registry = (
        build_active_policy_event_registry()
        if event_registry is None
        else event_registry
    )
    if type(registry) is not EventTypeRegistry or not registry.sealed:
        _fail(
            "TBM_ACTIVE_POLICY_EVENT_REGISTRY_INVALID",
            "event registry must be sealed",
        )
    reducer = build_active_policy_reducer(
        trusted_attestation_verifier_ids=trusted_attestation_verifier_ids
    )
    state = initial_reducer_state(reducer)
    parent: CanonicalEvent | None = None
    stream_id = events[0].stream_id
    for event in events:
        try:
            verify_event_parent(event, parent)
        except ValueError as error:
            raise ActivePolicyEventV1Error(
                "TBM_ACTIVE_POLICY_EVENT_SEQUENCE_INVALID",
                "active-policy event chain is invalid",
            ) from error
        if (
            event.stream_type != ACTIVE_POLICY_EVENT_STREAM_TYPE
            or event.stream_id != stream_id
            or event.event_type not in ACTIVE_POLICY_EVENT_TYPES
            or event.classification != "internal"
            or event.producer != "tbm_active_policy_runtime"
            or event.producer_version != "f4-v1"
            or event.retention_policy_id != "retention_active_policy_events"
        ):
            _fail(
                "TBM_ACTIVE_POLICY_EVENT_SEQUENCE_INVALID",
                "active-policy event envelope is invalid",
            )
        if event.stream_id != active_policy_stream_id(_event_partition(event)):
            _fail(
                "TBM_ACTIVE_POLICY_EVENT_SEQUENCE_INVALID",
                "active-policy stream does not match its partition",
            )
        typed = registry.consume(event, target_version=1)
        state = execute_reducer_step(
            reducer,
            state.state,
            ReducerEvent(event, typed),
        )
        parent = event
    return _hydrate_projection(state.state)


def append_active_policy_records(
    ledger: EventLedgerPort,
    records: tuple[ActivePolicyRecord, ...],
    *,
    recorded_at: str,
    trusted_attestation_verifier_ids: tuple[str, ...],
) -> ActivePolicyAppendResult:
    access = _require_ledger(ledger)
    if type(records) is not tuple or not records:
        _fail(
            "TBM_ACTIVE_POLICY_BATCH_INVALID",
            "records must be a non-empty tuple",
        )
    stream_id = active_policy_stream_id(access.partition)
    retained = _read_active_policy_stream(ledger, stream_id, allow_empty=True)
    if retained:
        _verify_retained_stream(ledger, stream_id, retained)
    expected_version = len(retained)
    parent = None if not retained else retained[-1]
    events: tuple[CanonicalEvent, ...] | None = None
    idempotency: LedgerIdempotency | None = None
    predicted: ActivePolicyProjection | None = None
    for attempt in range(8):
        high_watermark = ledger.read_global(
            after_position=0, limit=1
        ).high_watermark_global_position
        events, idempotency = build_active_policy_event_batch(
            access,
            records,
            expected_stream_version=expected_version,
            next_global_position=high_watermark + 1,
            previous_event=parent,
            recorded_at=recorded_at,
        )
        predicted = reduce_active_policy_events(
            (*retained, *events),
            trusted_attestation_verifier_ids=trusted_attestation_verifier_ids,
        )
        try:
            receipt = ledger.append(
                stream_id,
                expected_version,
                events,
                idempotency,
            )
            break
        except EventLedgerConflictError as error:
            if (
                error.code != "TBM_EVENT_LEDGER_GLOBAL_POSITION_CONFLICT"
                or attempt == 7
            ):
                raise
    else:  # pragma: no cover
        raise AssertionError("active-policy append retry did not terminate")
    request = LedgerAppendRequest(
        access=access,
        stream_id=stream_id,
        expected_stream_version=expected_version,
        events=cast(tuple[CanonicalEvent, ...], events),
        idempotency=cast(LedgerIdempotency, idempotency),
    )
    verify_ledger_append_receipt(request, receipt)
    durable_events = _read_active_policy_stream(ledger, stream_id)
    _verify_retained_stream(ledger, stream_id, durable_events)
    rebuilt = reduce_active_policy_events(
        durable_events,
        trusted_attestation_verifier_ids=trusted_attestation_verifier_ids,
    )
    if rebuilt != predicted:
        _fail(
            "TBM_ACTIVE_POLICY_PROJECTION_MISMATCH",
            "durable policy replay differs from pre-append projection",
        )
    return ActivePolicyAppendResult(receipt=receipt, projection=rebuilt)


def rebuild_active_policy_from_ledger(
    ledger: EventLedgerPort,
    *,
    trusted_attestation_verifier_ids: tuple[str, ...],
) -> DurableActivePolicySnapshot:
    access = _require_ledger(ledger)
    stream_id = active_policy_stream_id(access.partition)
    events = _read_active_policy_stream(ledger, stream_id)
    _verify_retained_stream(ledger, stream_id, events)
    projection = reduce_active_policy_events(
        events,
        trusted_attestation_verifier_ids=trusted_attestation_verifier_ids,
    )
    repeated = _read_active_policy_stream(ledger, stream_id)
    if repeated != events:
        _fail(
            "TBM_ACTIVE_POLICY_REBUILD_SUPERSEDED",
            "active-policy stream changed during rebuild",
        )
    _verify_retained_stream(ledger, stream_id, repeated)
    descriptor = build_active_policy_reducer(
        trusted_attestation_verifier_ids=trusted_attestation_verifier_ids
    ).descriptor
    values = {
        "contract_version": "tbm.durable-active-policy-snapshot.v1",
        "partition_sha256": access.partition.partition_sha256,
        "reducer_descriptor_sha256": descriptor.descriptor_sha256,
        "reducer_configuration_sha256": descriptor.configuration_sha256,
        "stream_version": len(events),
        "source_event_count": len(events),
        "projection": _projection_digest_value(projection),
    }
    return DurableActivePolicySnapshot(
        projection=projection,
        partition_sha256=access.partition.partition_sha256,
        reducer_descriptor_sha256=descriptor.descriptor_sha256,
        reducer_configuration_sha256=descriptor.configuration_sha256,
        stream_version=len(events),
        source_event_count=len(events),
        snapshot_sha256=canonical_sha256(values),
    )


def dumps_active_policy_bundle(bundle: ActivePolicyBundle) -> str:
    if type(bundle) is not ActivePolicyBundle:
        _record_invalid("bundle must be ActivePolicyBundle")
    return _canonical_json(bundle.to_dict())


def loads_active_policy_bundle(document: str | bytes) -> ActivePolicyBundle:
    item = _loads_record(document, "active policy bundle")
    _require_fields(
        item,
        {
            "contract_version",
            "policy_bundle_id",
            "retrieval_policy",
            "trust_tier_policy",
            "candidate_budget",
            "renderer_policy",
            "semantic_gate_required",
        },
        "active policy bundle",
    )
    retrieval = _mapping(item, "retrieval_policy")
    trust = _mapping(item, "trust_tier_policy")
    budget = _mapping(item, "candidate_budget")
    renderer = _mapping(item, "renderer_policy")
    return ActivePolicyBundle(
        contract_version=_string(item, "contract_version"),
        policy_bundle_id=_string(item, "policy_bundle_id"),
        retrieval_policy=loads_retrieval_policy(_canonical_json(retrieval)),
        trust_tier_policy=_parse_trust_tier_policy(trust),
        candidate_budget=_parse_candidate_budget(budget),
        renderer_policy=_parse_renderer_policy(renderer),
        semantic_gate_required=_boolean(item, "semantic_gate_required"),
    )


def dumps_active_policy_registration_publication(
    record: StoredActivePolicyRegistration,
) -> str:
    if type(record) is not StoredActivePolicyRegistration:
        _record_invalid("record must be StoredActivePolicyRegistration")
    return _canonical_json(record.to_dict())


def loads_active_policy_registration_publication(
    document: str | bytes,
) -> StoredActivePolicyRegistration:
    item = _loads_record(document, "stored active policy registration")
    _require_fields(
        item,
        {
            "contract_version",
            "registration",
            "policy",
            "request",
            "decision",
            "attestation_verified_by",
        },
        "stored active policy registration",
    )
    if item.get("contract_version") != "tbm.stored-active-policy-registration.v1":
        _record_invalid("stored registration version is unsupported")
    registration = _parse_registration(_mapping(item, "registration"))
    return StoredActivePolicyRegistration(
        registration=registration,
        policy=parse_authorization_policy(_mapping(item, "policy")),
        request=_parse_authorization_request(_mapping(item, "request")),
        decision=parse_authorization_decision(_mapping(item, "decision")),
        attestation_verified_by=_string(item, "attestation_verified_by"),
    )


def dumps_active_policy_activation_publication(
    record: StoredActivePolicyActivation,
) -> str:
    if type(record) is not StoredActivePolicyActivation:
        _record_invalid("record must be StoredActivePolicyActivation")
    return _canonical_json(record.to_dict())


def loads_active_policy_activation_publication(
    document: str | bytes,
) -> StoredActivePolicyActivation:
    item = _loads_record(document, "stored active policy activation")
    _require_fields(
        item,
        {
            "contract_version",
            "activation",
            "policy",
            "request",
            "decision",
            "attestation_verified_by",
        },
        "stored active policy activation",
    )
    if item.get("contract_version") != "tbm.stored-active-policy-activation.v1":
        _record_invalid("stored activation version is unsupported")
    return StoredActivePolicyActivation(
        activation=_parse_activation(_mapping(item, "activation")),
        policy=parse_authorization_policy(_mapping(item, "policy")),
        request=_parse_authorization_request(_mapping(item, "request")),
        decision=parse_authorization_decision(_mapping(item, "decision")),
        attestation_verified_by=_string(item, "attestation_verified_by"),
    )


def active_policy_bundle_schema() -> dict[str, object]:
    digest_id = {"type": "string", "pattern": "^active_policy_sha256_[0-9a-f]{64}$"}
    identifier = {"type": "string", "minLength": 1, "maxLength": 128}
    return {
        "$schema": "https://json-schema.org/draft/2020-12/schema",
        "$id": ACTIVE_POLICY_BUNDLE_SCHEMA_ID,
        "title": "Trace-backed Memory active policy bundle v1",
        "$defs": {"retrieval_policy": _retrieval_policy_schema()},
        "type": "object",
        "additionalProperties": False,
        "required": [
            "contract_version",
            "policy_bundle_id",
            "retrieval_policy",
            "trust_tier_policy",
            "candidate_budget",
            "renderer_policy",
            "semantic_gate_required",
        ],
        "properties": {
            "contract_version": {"const": ACTIVE_POLICY_BUNDLE_CONTRACT_VERSION},
            "policy_bundle_id": digest_id,
            "retrieval_policy": {"$ref": "#/$defs/retrieval_policy"},
            "trust_tier_policy": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "contract_version",
                    "minimum_trust_tier",
                    "require_active_revision",
                    "allow_legacy_unstructured",
                ],
                "properties": {
                    "contract_version": {"const": "tbm.trust-tier-policy.v1"},
                    "minimum_trust_tier": {"enum": list(TRUST_TIERS)},
                    "require_active_revision": {"const": True},
                    "allow_legacy_unstructured": {"const": False},
                },
            },
            "candidate_budget": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "contract_version",
                    "discovery_max_candidates",
                    "system_gate_max_candidates",
                    "semantic_gate_max_candidates",
                    "injection_max_memories",
                    "payload_budget_bytes",
                ],
                "properties": {
                    "contract_version": {"const": "tbm.candidate-budget.v1"},
                    "discovery_max_candidates": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": RETRIEVAL_PREPARATION_MAX_CANDIDATES,
                    },
                    "system_gate_max_candidates": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": GATE_EVALUATION_MAX_DECISIONS,
                    },
                    "semantic_gate_max_candidates": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": LLM_GATE_MAX_CANDIDATES,
                    },
                    "injection_max_memories": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": INJECTION_MAX_MEMORIES,
                    },
                    "payload_budget_bytes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": RETRIEVAL_POLICY_MAX_PAYLOAD_BYTES,
                    },
                },
            },
            "renderer_policy": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "renderer_policy_id",
                    "contract_version",
                    "renderer_id",
                    "renderer_version",
                    "allowed_modes",
                    "summary_item_max_chars",
                    "full_item_max_chars",
                    "max_memories",
                    "snippet_max_chars",
                    "snippet_max_utf8_bytes",
                    "output_format",
                    "media_type",
                ],
                "properties": {
                    "renderer_policy_id": {
                        "type": "string",
                        "pattern": "^renderer_policy_sha256_[0-9a-f]{64}$",
                    },
                    "contract_version": {"const": "tbm.renderer-policy.v1"},
                    "renderer_id": identifier,
                    "renderer_version": identifier,
                    "allowed_modes": {
                        "const": list(RENDERER_MODES),
                    },
                    "summary_item_max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": INJECTION_TEXT_MAX_CHARS,
                    },
                    "full_item_max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": FULL_CASE_INJECTION_TEXT_MAX_CHARS,
                    },
                    "max_memories": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": INJECTION_MAX_MEMORIES,
                    },
                    "snippet_max_chars": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": INJECTION_SNIPPET_MAX_CHARS,
                    },
                    "snippet_max_utf8_bytes": {
                        "type": "integer",
                        "minimum": 1,
                        "maximum": INJECTION_SNIPPET_MAX_CHARS * 4,
                    },
                    "output_format": {"const": "canonical-json-data-envelope"},
                    "media_type": {"const": "application/json"},
                },
            },
            "semantic_gate_required": {"const": True},
        },
    }


def _retrieval_policy_schema() -> dict[str, object]:
    memory_types = ("procedural", "semantic", "episodic", "policy")
    classifications = (
        "public",
        "internal",
        "confidential",
        "restricted",
    )
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "contract_version",
            "policy_id",
            "policy_version",
            "allowed_classifications",
            "mode_memory_rules",
            "ancestry_mode",
            "ancestry_bypass_reason",
            "stage_weights",
            "minimum_fused_score",
            "payload_budget_bytes",
            "block_eval_leaking",
        ],
        "properties": {
            "contract_version": {"const": RETRIEVAL_POLICY_CONTRACT_VERSION},
            "policy_id": {
                "type": "string",
                "pattern": "^retrieval_policy_sha256_[0-9a-f]{64}$",
            },
            "policy_version": {
                "type": "string",
                "minLength": 1,
                "maxLength": 128,
            },
            "allowed_classifications": {
                "type": "array",
                "minItems": 1,
                "maxItems": len(classifications),
                "uniqueItems": True,
                "items": {"enum": list(classifications)},
            },
            "mode_memory_rules": {
                "type": "array",
                "minItems": len(RETRIEVAL_TASK_MODES),
                "maxItems": len(RETRIEVAL_TASK_MODES),
                "uniqueItems": True,
                "items": {
                    "type": "object",
                    "additionalProperties": False,
                    "required": ["task_mode", "allowed_memory_types"],
                    "properties": {
                        "task_mode": {"enum": list(RETRIEVAL_TASK_MODES)},
                        "allowed_memory_types": {
                            "type": "array",
                            "minItems": 1,
                            "maxItems": len(memory_types),
                            "uniqueItems": True,
                            "items": {"enum": list(memory_types)},
                        },
                    },
                },
                "allOf": [
                    {
                        "contains": {
                            "type": "object",
                            "required": ["task_mode"],
                            "properties": {
                                "task_mode": {"const": task_mode}
                            },
                        },
                        "minContains": 1,
                        "maxContains": 1,
                    }
                    for task_mode in RETRIEVAL_TASK_MODES
                ],
            },
            "ancestry_mode": {"enum": ["required", "disabled"]},
            "ancestry_bypass_reason": {
                "type": ["string", "null"],
                "minLength": 1,
                "maxLength": 128,
                "pattern": "\\S",
            },
            "stage_weights": {
                "type": "object",
                "additionalProperties": False,
                "required": list(RETRIEVAL_RANKING_STAGES),
                "properties": {
                    stage: {
                        "type": "number",
                        "exclusiveMinimum": 0,
                        "maximum": 1,
                    }
                    for stage in RETRIEVAL_RANKING_STAGES
                },
            },
            "minimum_fused_score": {
                "type": "number",
                "minimum": 0,
                "maximum": 1,
            },
            "payload_budget_bytes": {
                "type": "integer",
                "minimum": 1,
                "maximum": RETRIEVAL_POLICY_MAX_PAYLOAD_BYTES,
            },
            "block_eval_leaking": {"const": True},
        },
        "allOf": [
            {
                "if": {
                    "properties": {"ancestry_mode": {"const": "required"}}
                },
                "then": {
                    "properties": {
                        "ancestry_bypass_reason": {"type": "null"}
                    }
                },
                "else": {
                    "properties": {
                        "ancestry_bypass_reason": {"type": "string"}
                    }
                },
            }
        ],
    }


def dumps_active_policy_bundle_schema() -> str:
    return json.dumps(
        active_policy_bundle_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def _record_descriptor(
    record: ActivePolicyRecord,
) -> tuple[
    str,
    str,
    str,
    dict[str, object],
    str,
    str,
    str,
    LedgerTenantPartition,
]:
    if type(record) is StoredActivePolicyRegistration:
        registration = record.registration
        return (
            POLICY_BUNDLE_REGISTERED,
            registration.policy_bundle.policy_bundle_id,
            registration.registered_at,
            record.to_dict(),
            registration.registered_by,
            registration.registered_via_client_id,
            registration.authorization_event_id,
            _target_partition(registration),
        )
    if type(record) is StoredActivePolicyActivation:
        activation = record.activation
        return (
            POLICY_BUNDLE_ACTIVATED,
            activation.policy_bundle_id,
            activation.activated_at,
            record.to_dict(),
            activation.activated_by,
            activation.activated_via_client_id,
            activation.authorization_event_id,
            _target_partition(activation),
        )
    _record_invalid("active policy record type is unsupported")


def _verify_record_access(
    access: LedgerAccessContext,
    *,
    partition: LedgerTenantPartition,
    actor_id: str,
    client_id: str,
    authorization_event_id: str,
) -> None:
    if access.partition != partition:
        _fail(
            "TBM_ACTIVE_POLICY_SCOPE_DENIED",
            "record target is outside the ledger partition",
        )
    if (
        access.actor_id != actor_id
        or access.actor_type != "principal"
        or access.principal_id != actor_id
        or access.agent_client_id != client_id
        or access.authorization_decision_id != authorization_event_id
    ):
        _fail(
            "TBM_ACTIVE_POLICY_ACTOR_MISMATCH",
            "record provenance does not match trusted ledger access",
        )
    if not access.classification_filter.allows("internal"):
        _fail(
            "TBM_ACTIVE_POLICY_CLASSIFICATION_DENIED",
            "ledger access must include internal events",
        )


def _typed_payload(reducer_event: ReducerEvent) -> dict[str, object]:
    typed = reducer_event.typed_event
    if typed is None:
        _fail(
            "TBM_ACTIVE_POLICY_TYPED_INPUT_REQUIRED",
            "active-policy reducer requires typed input",
        )
    payload = _thaw_json(typed.payload)
    if type(payload) is not dict:
        _transition_invalid("active-policy payload must be an object")
    if payload.get("record_type") != reducer_event.source_event.event_type:
        _transition_invalid("active-policy payload type does not match event")
    return cast(dict[str, object], payload)


def _load_record(event_type: str, record_json: str) -> ActivePolicyRecord:
    try:
        if event_type == POLICY_BUNDLE_REGISTERED:
            return loads_active_policy_registration_publication(record_json)
        if event_type == POLICY_BUNDLE_ACTIVATED:
            return loads_active_policy_activation_publication(record_json)
    except ValueError as error:
        raise ActivePolicyEventV1Error(
            "TBM_ACTIVE_POLICY_EVENT_RECORD_INVALID",
            "active-policy event contains an invalid exact record",
        ) from error
    _transition_invalid("active-policy record type is unsupported")


def _verify_loaded_record(
    payload: Mapping[str, object],
    record: ActivePolicyRecord,
    event: CanonicalEvent,
) -> None:
    descriptor = _record_descriptor(record)
    partition = descriptor[7]
    if (
        payload.get("record_type") != descriptor[0]
        or payload.get("policy_bundle_id") != descriptor[1]
        or payload.get("record_sha256") != canonical_sha256(descriptor[3])
        or payload.get("record_json") != _canonical_json(descriptor[3])
        or event.occurred_at != descriptor[2]
        or event.actor_id != descriptor[4]
        or event.actor_type != "principal"
        or event.principal_id != descriptor[4]
        or event.agent_client_id != descriptor[5]
        or event.authorization_decision_id != descriptor[6]
        or _event_partition(event) != partition
    ):
        _transition_invalid("active-policy record binding does not match event")


def _verify_stored_authorization(
    policy: AuthorizationPolicyBundle,
    request: AuthorizationRequest,
    decision: AuthorizationDecision,
    *,
    permission: Literal["policy:create_global", "policy:approve_global"],
    actor_id: str,
    client_id: str,
    authorization_event_id: str,
    occurred_at: str,
    event: CanonicalEvent,
) -> None:
    try:
        verify_authorization_decision(policy, request, decision)
    except ValueError as error:
        raise ActivePolicyEventV1Error(
            "TBM_ACTIVE_POLICY_AUTHORIZATION_INVALID",
            "stored policy authorization is invalid",
        ) from error
    if (
        not decision.allowed
        or request.permission != permission
        or decision.permission != permission
        or request.tenant_id is not None
        or request.repository_reference is not None
        or decision.tenant_id is not None
        or decision.repository_id is not None
        or request.principal_id != actor_id
        or request.agent_client_id != client_id
        or decision.principal_id != actor_id
        or decision.agent_client_id != client_id
        or decision.authorization_event_id != authorization_event_id
        or event.authorization_decision_id != authorization_event_id
        or parse_rfc3339(decision.decided_at) > parse_rfc3339(occurred_at)
    ):
        _transition_invalid("stored authorization does not match policy event")


def _head_state(
    *,
    registration: StoredActivePolicyRegistration,
    activation: StoredActivePolicyActivation,
    event: CanonicalEvent,
) -> dict[str, object]:
    registered = registration.registration
    activated = activation.activation
    bundle = registered.policy_bundle
    values: dict[str, object] = {
        "contract_version": "tbm.active-policy-head.v1",
        "organization_id": activated.organization_id,
        "tenant_id": activated.tenant_id,
        "repository_id": activated.repository_id,
        "environment_id": activated.environment_id,
        "policy_bundle_id": activated.policy_bundle_id,
        "retrieval_policy_id": bundle.retrieval_policy.policy_id,
        "renderer_policy_id": bundle.renderer_policy.renderer_policy_id,
        "registration_id": activated.registration_id,
        "activation_id": activated.activation_id,
        "previous_policy_bundle_id": activated.previous_policy_bundle_id,
        "registration_authorization_event_id": (
            registered.authorization_event_id
        ),
        "activation_authorization_event_id": activated.authorization_event_id,
        "registration_attestation_verified_by": (
            registration.attestation_verified_by
        ),
        "activation_attestation_verified_by": (
            activation.attestation_verified_by
        ),
        "activated_by": activated.activated_by,
        "activated_at": activated.activated_at,
        "source_event_sha256": event.event_sha256,
    }
    return {**values, "head_sha256": canonical_sha256(values)}


def _hydrate_projection(state: Mapping[str, object]) -> ActivePolicyProjection:
    registrations_state = _state_mapping(state, "registrations")
    registrations = tuple(
        loads_active_policy_registration_publication(value)
        for _, value in sorted(registrations_state.items())
        if type(value) is str
    )
    activations = tuple(
        loads_active_policy_activation_publication(cast(str, value))
        for value in _state_list(state, "activations")
    )
    head_state = _optional_state_mapping(state.get("head"), "head")
    head = None if head_state is None else _parse_head(head_state)
    return ActivePolicyProjection(
        organization_id=cast(str, state.get("organization_id")),
        tenant_id=cast(str, state.get("tenant_id")),
        repository_id=cast(str, state.get("repository_id")),
        environment_id=cast(str, state.get("environment_id")),
        registrations=registrations,
        activations=activations,
        active_head=head,
        last_event_sha256=cast(str, state.get("last_event_sha256")),
        last_global_position=cast(int, state.get("last_global_position")),
    )


def _read_active_policy_stream(
    ledger: EventLedgerPort,
    stream_id: str,
    *,
    allow_empty: bool = False,
) -> tuple[CanonicalEvent, ...]:
    events: list[CanonicalEvent] = []
    from_version = 1
    while True:
        page = ledger.read_stream(
            stream_id,
            from_version=from_version,
            limit=EVENT_LEDGER_MAX_READ_PAGE,
        )
        events.extend(page.events)
        if len(events) > ACTIVE_POLICY_EVENT_MAX_STREAM_EVENTS:
            _fail(
                "TBM_ACTIVE_POLICY_EVENT_SEQUENCE_INVALID",
                "active-policy stream exceeds the event limit",
            )
        if not page.has_more:
            break
        if page.next_stream_version is None:
            _fail(
                "TBM_ACTIVE_POLICY_LEDGER_READ_FAILED",
                "active-policy page lacks its next cursor",
            )
        from_version = page.next_stream_version
    if not events and not allow_empty:
        _fail(
            "TBM_ACTIVE_POLICY_HEAD_MISSING",
            "active-policy stream is empty",
        )
    return tuple(events)


def _verify_retained_stream(
    ledger: EventLedgerPort,
    stream_id: str,
    events: tuple[CanonicalEvent, ...],
) -> None:
    verification = ledger.verify_stream(stream_id)
    if (
        not verification.valid
        or verification.verified_stream_version != len(events)
        or verification.head_event_sha256 != events[-1].event_sha256
    ):
        _fail(
            "TBM_ACTIVE_POLICY_LEDGER_VERIFICATION_FAILED",
            "retained active-policy stream failed verification",
        )


def _require_ledger(ledger: EventLedgerPort) -> LedgerAccessContext:
    access = getattr(ledger, "access_context", None)
    if type(access) is not LedgerAccessContext or not all(
        callable(getattr(ledger, name, None))
        for name in ("append", "read_stream", "read_global", "verify_stream")
    ):
        _fail(
            "TBM_ACTIVE_POLICY_LEDGER_INVALID",
            "operation requires an access-bound EventLedgerPort",
        )
    if not access.classification_filter.allows("internal"):
        _fail(
            "TBM_ACTIVE_POLICY_CLASSIFICATION_DENIED",
            "active-policy ledger access must include internal events",
        )
    return access


def _parse_registration(item: Mapping[str, object]) -> ActivePolicyRegistration:
    _require_fields(
        item,
        {
            "contract_version",
            "registration_id",
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
            "policy_bundle",
            "registered_by",
            "registered_via_client_id",
            "authorization_event_id",
            "registered_at",
        },
        "active policy registration",
    )
    return ActivePolicyRegistration(
        contract_version=_string(item, "contract_version"),
        registration_id=_string(item, "registration_id"),
        organization_id=_string(item, "organization_id"),
        tenant_id=_string(item, "tenant_id"),
        repository_id=_string(item, "repository_id"),
        environment_id=_string(item, "environment_id"),
        policy_bundle=loads_active_policy_bundle(
            _canonical_json(_mapping(item, "policy_bundle"))
        ),
        registered_by=_string(item, "registered_by"),
        registered_via_client_id=_string(item, "registered_via_client_id"),
        authorization_event_id=_string(item, "authorization_event_id"),
        registered_at=_string(item, "registered_at"),
    )


def _parse_activation(item: Mapping[str, object]) -> ActivePolicyActivation:
    _require_fields(
        item,
        {
            "contract_version",
            "activation_id",
            "organization_id",
            "tenant_id",
            "repository_id",
            "environment_id",
            "policy_bundle_id",
            "registration_id",
            "previous_policy_bundle_id",
            "activated_by",
            "activated_via_client_id",
            "authorization_event_id",
            "activated_at",
        },
        "active policy activation",
    )
    previous = item.get("previous_policy_bundle_id")
    if previous is not None and type(previous) is not str:
        _record_invalid("previous_policy_bundle_id must be a string or null")
    return ActivePolicyActivation(
        contract_version=_string(item, "contract_version"),
        activation_id=_string(item, "activation_id"),
        organization_id=_string(item, "organization_id"),
        tenant_id=_string(item, "tenant_id"),
        repository_id=_string(item, "repository_id"),
        environment_id=_string(item, "environment_id"),
        policy_bundle_id=_string(item, "policy_bundle_id"),
        registration_id=_string(item, "registration_id"),
        previous_policy_bundle_id=cast(str | None, previous),
        activated_by=_string(item, "activated_by"),
        activated_via_client_id=_string(item, "activated_via_client_id"),
        authorization_event_id=_string(item, "authorization_event_id"),
        activated_at=_string(item, "activated_at"),
    )


def _parse_head(item: Mapping[str, object]) -> ActivePolicyHead:
    return ActivePolicyHead(
        contract_version=_string(item, "contract_version"),
        organization_id=_string(item, "organization_id"),
        tenant_id=_string(item, "tenant_id"),
        repository_id=_string(item, "repository_id"),
        environment_id=_string(item, "environment_id"),
        policy_bundle_id=_string(item, "policy_bundle_id"),
        retrieval_policy_id=_string(item, "retrieval_policy_id"),
        renderer_policy_id=_string(item, "renderer_policy_id"),
        registration_id=_string(item, "registration_id"),
        activation_id=_string(item, "activation_id"),
        previous_policy_bundle_id=cast(
            str | None, item.get("previous_policy_bundle_id")
        ),
        registration_authorization_event_id=_string(
            item, "registration_authorization_event_id"
        ),
        activation_authorization_event_id=_string(
            item, "activation_authorization_event_id"
        ),
        registration_attestation_verified_by=_string(
            item, "registration_attestation_verified_by"
        ),
        activation_attestation_verified_by=_string(
            item, "activation_attestation_verified_by"
        ),
        activated_by=_string(item, "activated_by"),
        activated_at=_string(item, "activated_at"),
        source_event_sha256=_string(item, "source_event_sha256"),
        head_sha256=_string(item, "head_sha256"),
    )


def _parse_trust_tier_policy(item: Mapping[str, object]) -> TrustTierPolicy:
    _require_fields(
        item,
        {
            "contract_version",
            "minimum_trust_tier",
            "require_active_revision",
            "allow_legacy_unstructured",
        },
        "trust tier policy",
    )
    return TrustTierPolicy(
        contract_version=_string(item, "contract_version"),
        minimum_trust_tier=cast(
            TrustTier, _string(item, "minimum_trust_tier")
        ),
        require_active_revision=_boolean(item, "require_active_revision"),
        allow_legacy_unstructured=_boolean(
            item, "allow_legacy_unstructured"
        ),
    )


def _parse_candidate_budget(item: Mapping[str, object]) -> CandidateBudget:
    _require_fields(
        item,
        {
            "contract_version",
            "discovery_max_candidates",
            "system_gate_max_candidates",
            "semantic_gate_max_candidates",
            "injection_max_memories",
            "payload_budget_bytes",
        },
        "candidate budget",
    )
    return CandidateBudget(
        contract_version=_string(item, "contract_version"),
        discovery_max_candidates=_integer(item, "discovery_max_candidates"),
        system_gate_max_candidates=_integer(
            item, "system_gate_max_candidates"
        ),
        semantic_gate_max_candidates=_integer(
            item, "semantic_gate_max_candidates"
        ),
        injection_max_memories=_integer(item, "injection_max_memories"),
        payload_budget_bytes=_integer(item, "payload_budget_bytes"),
    )


def _parse_renderer_policy(item: Mapping[str, object]) -> RendererPolicy:
    _require_fields(
        item,
        {
            "renderer_policy_id",
            "contract_version",
            "renderer_id",
            "renderer_version",
            "allowed_modes",
            "summary_item_max_chars",
            "full_item_max_chars",
            "max_memories",
            "snippet_max_chars",
            "snippet_max_utf8_bytes",
            "output_format",
            "media_type",
        },
        "renderer policy",
    )
    modes = item.get("allowed_modes")
    if type(modes) is not list or any(type(value) is not str for value in modes):
        _record_invalid("allowed_modes must be an array of strings")
    return RendererPolicy(
        renderer_policy_id=_string(item, "renderer_policy_id"),
        contract_version=_string(item, "contract_version"),
        renderer_id=_string(item, "renderer_id"),
        renderer_version=_string(item, "renderer_version"),
        allowed_modes=cast(tuple[RendererMode, ...], tuple(modes)),
        summary_item_max_chars=_integer(item, "summary_item_max_chars"),
        full_item_max_chars=_integer(item, "full_item_max_chars"),
        max_memories=_integer(item, "max_memories"),
        snippet_max_chars=_integer(item, "snippet_max_chars"),
        snippet_max_utf8_bytes=_integer(item, "snippet_max_utf8_bytes"),
        output_format=_string(item, "output_format"),
        media_type=_string(item, "media_type"),
    )


def _parse_authorization_request(
    item: Mapping[str, object],
) -> AuthorizationRequest:
    _require_fields(
        item,
        {
            "request_id",
            "principal_id",
            "agent_client_id",
            "tenant_id",
            "repository_reference",
            "permission",
            "requested_at",
        },
        "AuthorizationRequest",
    )
    tenant_id = item.get("tenant_id")
    repository_reference = item.get("repository_reference")
    if tenant_id is not None and type(tenant_id) is not str:
        _record_invalid("AuthorizationRequest tenant_id is invalid")
    if repository_reference is not None and type(repository_reference) is not str:
        _record_invalid("AuthorizationRequest repository_reference is invalid")
    return AuthorizationRequest(
        request_id=_string(item, "request_id"),
        principal_id=_string(item, "principal_id"),
        agent_client_id=_string(item, "agent_client_id"),
        tenant_id=cast(str | None, tenant_id),
        repository_reference=cast(str | None, repository_reference),
        permission=cast(
            Literal[
                "memory:retrieve",
                "memory:inject",
                "memory:create",
                "memory:review",
                "memory:verify",
                "memory:activate",
                "gate_session:create",
                "gate_session:transition",
                "artifact:read",
                "artifact:write",
                "tenant:audit_read",
                "policy:create_global",
                "policy:approve_global",
                "platform:audit_read",
                "platform:admin",
            ],
            _string(item, "permission"),
        ),
        requested_at=_string(item, "requested_at"),
    )


def _stored_authorization_records(
    policy: AuthorizationPolicyBundle,
    request: AuthorizationRequest,
    decision: AuthorizationDecision,
    attestation_verified_by: str,
) -> None:
    if (
        type(policy) is not AuthorizationPolicyBundle
        or type(request) is not AuthorizationRequest
        or type(decision) is not AuthorizationDecision
    ):
        _record_invalid("stored authorization records are invalid")
    _identifier(attestation_verified_by, "attestation_verified_by")


def _trusted_verifier_set(values: tuple[str, ...]) -> frozenset[str]:
    if (
        type(values) is not tuple
        or not values
        or len(values) > 64
    ):
        _record_invalid(
            "trusted_attestation_verifier_ids must be a bounded unique tuple"
        )
    if any(type(value) is not str for value in values):
        _record_invalid("trusted attestation verifier IDs must be strings")
    if len(set(values)) != len(values):
        _record_invalid("trusted attestation verifier IDs must be unique")
    for value in values:
        _identifier(value, "trusted_attestation_verifier_id")
    return frozenset(values)


def _target_partition(value: object) -> LedgerTenantPartition:
    try:
        return LedgerTenantPartition(
            organization_id=cast(str, getattr(value, "organization_id")),
            tenant_id=cast(str, getattr(value, "tenant_id")),
            repository_id=cast(str, getattr(value, "repository_id")),
            environment_id=cast(str, getattr(value, "environment_id")),
        )
    except (AttributeError, TypeError, ValueError) as error:
        raise ActivePolicyEventV1Error(
            "TBM_ACTIVE_POLICY_RECORD_INVALID",
            "active policy target partition is invalid",
        ) from error


def _event_partition(event: CanonicalEvent) -> LedgerTenantPartition:
    return LedgerTenantPartition(
        organization_id=event.organization_id,
        tenant_id=event.tenant_id,
        repository_id=event.repository_id,
        environment_id=event.environment_id,
    )


def _payload_json_schemas() -> dict[str, Mapping[str, object]]:
    digest = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
    result: dict[str, Mapping[str, object]] = {}
    for event_type in ACTIVE_POLICY_EVENT_TYPES:
        properties = {
            "policy_bundle_id": {
                "type": "string",
                "pattern": r"^active_policy_sha256_[0-9a-f]{64}$",
            },
            "record_type": {"const": event_type},
            "record_sha256": digest,
            "record_json": {
                "type": "string",
                "minLength": 2,
                "maxLength": ACTIVE_POLICY_JSON_MAX_BYTES,
            },
        }
        result[event_type] = {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": properties,
        }
    return result


def _loads_record(document: str | bytes, description: str) -> dict[str, object]:
    if type(document) is bytes:
        try:
            source = document.decode("utf-8", errors="strict")
        except UnicodeError as error:
            raise ActivePolicyEventV1Error(
                "TBM_ACTIVE_POLICY_JSON_INVALID",
                f"{description} must be strict UTF-8 JSON",
            ) from error
    elif type(document) is str:
        source = document
    else:
        _record_invalid(f"{description} must be JSON text")
    try:
        size = len(source.encode("utf-8"))
    except UnicodeError as error:
        raise ActivePolicyEventV1Error(
            "TBM_ACTIVE_POLICY_JSON_INVALID",
            f"{description} must be strict UTF-8 JSON",
        ) from error
    if size > ACTIVE_POLICY_JSON_MAX_BYTES:
        _record_invalid(f"{description} exceeds the byte limit")
    try:
        value = parse_bounded_json(
            source,
            max_depth=ACTIVE_POLICY_JSON_MAX_DEPTH,
            max_nodes=ACTIVE_POLICY_JSON_MAX_NODES,
            description=description,
        )
    except (TypeError, ValueError) as error:
        raise ActivePolicyEventV1Error(
            "TBM_ACTIVE_POLICY_JSON_INVALID",
            f"{description} is invalid",
        ) from error
    if type(value) is not dict:
        _record_invalid(f"{description} must be an object")
    return cast(dict[str, object], value)


def _projection_digest_value(
    projection: ActivePolicyProjection,
) -> dict[str, object]:
    return {
        "organization_id": projection.organization_id,
        "tenant_id": projection.tenant_id,
        "repository_id": projection.repository_id,
        "environment_id": projection.environment_id,
        "registrations": [item.to_dict() for item in projection.registrations],
        "activations": [item.to_dict() for item in projection.activations],
        "active_head": (
            None
            if projection.active_head is None
            else projection.active_head.to_dict()
        ),
        "last_event_sha256": projection.last_event_sha256,
        "last_global_position": projection.last_global_position,
    }


def _content_id(prefix: str, value: Mapping[str, object]) -> str:
    return prefix + canonical_sha256(value).removeprefix("sha256:")


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(
        domain + _canonical_json(value).encode("utf-8")
    ).hexdigest()


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        )
    except (TypeError, ValueError, UnicodeError) as error:
        raise ActivePolicyEventV1Error(
            "TBM_ACTIVE_POLICY_RECORD_INVALID",
            "active policy record is not canonical JSON",
        ) from error


def _timestamp(value: object, name: str) -> str:
    try:
        if type(value) is not str:
            raise ValueError("timestamp must be a string")
        return canonical_rfc3339(value)
    except (TypeError, ValueError) as error:
        raise ActivePolicyEventV1Error(
            "TBM_ACTIVE_POLICY_RECORD_INVALID",
            f"{name} must be canonical RFC3339",
        ) from error


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _record_invalid(f"{name} must be a bounded identifier")


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _record_invalid(f"{name} must be sha256:<64 lowercase hex>")


def _require_fields(
    value: Mapping[str, object], expected: set[str], name: str
) -> None:
    if set(value) != expected:
        _record_invalid(f"{name} fields do not match the contract")


def _mapping(value: Mapping[str, object], name: str) -> dict[str, object]:
    item = value.get(name)
    if type(item) is not dict:
        _record_invalid(f"{name} must be an object")
    return cast(dict[str, object], item)


def _string(value: Mapping[str, object], name: str) -> str:
    item = value.get(name)
    if type(item) is not str:
        _record_invalid(f"{name} must be a string")
    return item


def _integer(value: Mapping[str, object], name: str) -> int:
    item = value.get(name)
    if type(item) is not int:
        _record_invalid(f"{name} must be an integer")
    return item


def _boolean(value: Mapping[str, object], name: str) -> bool:
    item = value.get(name)
    if type(item) is not bool:
        _record_invalid(f"{name} must be a boolean")
    return item


def _state_mapping(
    state: Mapping[str, object], name: str
) -> dict[str, object]:
    value = _thaw_json(state.get(name))
    if type(value) is not dict:
        _projection_invalid(f"{name} reducer state is invalid")
    return cast(dict[str, object], value)


def _optional_state_mapping(
    value: object, name: str
) -> dict[str, object] | None:
    thawed = _thaw_json(value)
    if thawed is None:
        return None
    if type(thawed) is not dict:
        _projection_invalid(f"{name} reducer state is invalid")
    return cast(dict[str, object], thawed)


def _state_list(state: Mapping[str, object], name: str) -> list[object]:
    value = _thaw_json(state.get(name))
    if type(value) is not list:
        _projection_invalid(f"{name} reducer state is invalid")
    return cast(list[object], value)


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
        return [_thaw_json(item) for item in value]
    return value


__all__ = [
    "ACTIVE_POLICY_BUNDLE_CONTRACT_VERSION",
    "ACTIVE_POLICY_BUNDLE_SCHEMA_ID",
    "ACTIVE_POLICY_EVENT_MAX_BATCH",
    "ACTIVE_POLICY_EVENT_MAX_STREAM_EVENTS",
    "ACTIVE_POLICY_EVENT_PAYLOAD_SCHEMA_ID",
    "ACTIVE_POLICY_EVENT_PROJECTION",
    "ACTIVE_POLICY_EVENT_PROTOCOL_VERSION",
    "ACTIVE_POLICY_EVENT_REDUCER_ID",
    "ACTIVE_POLICY_EVENT_STREAM_TYPE",
    "ACTIVE_POLICY_EVENT_TYPES",
    "ACTIVE_POLICY_JSON_MAX_BYTES",
    "ACTIVE_POLICY_JSON_MAX_DEPTH",
    "ACTIVE_POLICY_JSON_MAX_NODES",
    "POLICY_BUNDLE_ACTIVATED",
    "POLICY_BUNDLE_REGISTERED",
    "RENDERER_MODES",
    "TRUST_TIERS",
    "ActivePolicyActivation",
    "ActivePolicyAppendResult",
    "ActivePolicyBundle",
    "ActivePolicyEventV1Error",
    "ActivePolicyHead",
    "ActivePolicyHeadReader",
    "ActivePolicyProjection",
    "ActivePolicyRecord",
    "ActivePolicyRegistration",
    "CandidateBudget",
    "DurableActivePolicySnapshot",
    "RendererMode",
    "RendererPolicy",
    "StoredActivePolicyActivation",
    "StoredActivePolicyRegistration",
    "TrustTier",
    "TrustTierPolicy",
    "active_policy_bundle_schema",
    "active_policy_event_payload_dispatch_schema",
    "active_policy_stream_id",
    "append_active_policy_records",
    "build_active_policy_activation",
    "build_active_policy_bundle",
    "build_active_policy_event_batch",
    "build_active_policy_event_registry",
    "build_active_policy_reducer",
    "build_active_policy_registration",
    "build_current_renderer_policy",
    "dumps_active_policy_activation_publication",
    "dumps_active_policy_bundle",
    "dumps_active_policy_bundle_schema",
    "dumps_active_policy_event_payload_dispatch_schema",
    "dumps_active_policy_registration_publication",
    "loads_active_policy_activation_publication",
    "loads_active_policy_bundle",
    "loads_active_policy_registration_publication",
    "rebuild_active_policy_from_ledger",
    "reduce_active_policy_events",
]
