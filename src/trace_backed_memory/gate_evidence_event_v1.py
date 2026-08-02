from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
import json
from typing import NoReturn, Protocol, cast

from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import V3ContractError
from .event_registry_v1 import EventPayloadRegistration, EventTypeRegistry
from .event_v1 import (
    CanonicalEvent,
    EventArtifactRef,
    build_canonical_event,
    verify_event_parent,
)
from .gate_evaluation_v3 import (
    SemanticGateAttempt,
    SystemGateEvaluation,
    dumps_semantic_gate_attempt,
    dumps_system_gate_evaluation,
    loads_semantic_gate_attempt,
    loads_system_gate_evaluation,
    verify_semantic_gate_attempt_chain,
    verify_system_gate_evaluation,
)
from .gate_session_v3 import GateSession
from .ledger_port_v1 import (
    EventLedgerConflictError,
    EventLedgerPort,
    LedgerAccessContext,
    LedgerIdempotency,
)
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    execute_reducer_step,
    initial_reducer_state,
)
from .replay_v3 import (
    DataClassification,
    ContentAddressedArtifact,
    DecisionReplayManifest,
    InjectionArtifact,
    StoredReplayArtifact,
    artifact_id_from_sha256,
    create_content_addressed_artifact,
    dumps_decision_replay_manifest,
    dumps_injection_artifact,
    loads_decision_replay_manifest,
    loads_injection_artifact,
)
from .replay_export_v3 import (
    REPLAY_EXPORT_MAX_CONTENT_BYTES,
    ReplayBundleExport,
    build_replay_bundle_export,
)
from .retrieval_v3 import (
    RetrievalSnapshot,
    dumps_retrieval_snapshot,
    loads_retrieval_snapshot,
)
from .semantic_gate_artifact_v3 import StoredSemanticGateAttemptArtifacts
from .usage_decision_v3 import (
    USAGE_DECISION_ARTIFACT_MEDIA_TYPE,
    UsageDecision,
    loads_usage_decision_artifact,
)


GATE_EVIDENCE_EVENT_PROTOCOL_VERSION = "tbm.gate-evidence-event.v1"
GATE_EVIDENCE_EVENT_STREAM_TYPE = "gate_evidence"
GATE_EVIDENCE_EVENT_MAX_APPEND_RETRIES = 8
GATE_EVIDENCE_EVENT_MAX_BATCH = 100
GATE_EVIDENCE_EVENT_PAYLOAD_SCHEMA_ID = (
    "https://trace-backed-memory.dev/schemas/"
    "gate_evidence_event_payload_registry_v1.schema.json"
)

RETRIEVAL_SNAPSHOT_RECORDED = "tbm.gate_evidence.retrieval_snapshot_recorded"
SYSTEM_GATE_EVALUATED = "tbm.gate_evidence.system_gate_evaluated"
SEMANTIC_GATE_ATTEMPT_RECORDED = (
    "tbm.gate_evidence.semantic_gate_attempt_recorded"
)
USAGE_DECISION_RECORDED = "tbm.gate_evidence.usage_decision_recorded"
INJECTION_ARTIFACT_RECORDED = (
    "tbm.gate_evidence.injection_artifact_recorded"
)

GATE_EVIDENCE_EVENT_TYPES = (
    RETRIEVAL_SNAPSHOT_RECORDED,
    SYSTEM_GATE_EVALUATED,
    SEMANTIC_GATE_ATTEMPT_RECORDED,
    USAGE_DECISION_RECORDED,
    INJECTION_ARTIFACT_RECORDED,
)

RETRIEVAL_CURRENT_REDUCER_ID = "retrieval-current"
SYSTEM_GATE_CURRENT_REDUCER_ID = "system-gate-current"
SEMANTIC_ATTEMPT_CHAIN_REDUCER_ID = "semantic-attempt-chain"
FINAL_DECISION_CURRENT_REDUCER_ID = "final-decision-current"
INJECTION_CURRENT_REDUCER_ID = "injection-current"

RETRIEVAL_CURRENT_PROJECTION = "retrieval_current_v1"
SYSTEM_GATE_CURRENT_PROJECTION = "system_gate_current_v1"
SEMANTIC_ATTEMPT_CHAIN_PROJECTION = "semantic_attempt_chain_v1"
FINAL_DECISION_CURRENT_PROJECTION = "final_decision_current_v1"
INJECTION_CURRENT_PROJECTION = "injection_current_v1"

RETRIEVAL_SNAPSHOT_MEDIA_TYPE = (
    "application/vnd.trace-backed-memory.retrieval-snapshot+json"
)
SYSTEM_GATE_EVALUATION_MEDIA_TYPE = (
    "application/vnd.trace-backed-memory.system-gate-evaluation+json"
)
SEMANTIC_GATE_ATTEMPT_MEDIA_TYPE = (
    "application/vnd.trace-backed-memory.semantic-gate-attempt+json"
)
INJECTION_DESCRIPTOR_MEDIA_TYPE = (
    "application/vnd.trace-backed-memory.injection-artifact+json"
)
REPLAY_MANIFEST_DESCRIPTOR_MEDIA_TYPE = (
    "application/vnd.trace-backed-memory.replay-manifest+json"
)

_PAYLOAD_SCHEMAS = {
    event_type: event_type + ".v1" for event_type in GATE_EVIDENCE_EVENT_TYPES
}
_EVENT_STAGES = {
    RETRIEVAL_SNAPSHOT_RECORDED: 1,
    SYSTEM_GATE_EVALUATED: 2,
    SEMANTIC_GATE_ATTEMPT_RECORDED: 3,
    USAGE_DECISION_RECORDED: 4,
    INJECTION_ARTIFACT_RECORDED: 5,
}


class GateEvidenceEventV1Error(V3ContractError):
    """Stable failure for event-first Gate evidence projections."""


class SemanticGateAttemptEventSink(Protocol):
    def append_semantic_attempt(
        self,
        bundle: StoredSemanticGateAttemptArtifacts,
    ) -> None: ...


class GateEvidenceReader(Protocol):
    def load_snapshot(self, snapshot_id: str) -> RetrievalSnapshot: ...

    def load_evaluation(
        self,
        evaluation_id: str,
    ) -> SystemGateEvaluation: ...


class SemanticGateEvidenceReader(Protocol):
    def load_attempt_with_artifacts(
        self,
        attempt_id: str,
    ) -> StoredSemanticGateAttemptArtifacts: ...

    def load_attempt_chain(
        self,
        evaluation_id: str,
    ) -> tuple[SemanticGateAttempt, ...]: ...


class GateEvidenceReplayArtifactRepository(Protocol):
    def store_artifact(
        self,
        artifact: ContentAddressedArtifact,
        content: bytes,
    ) -> bool: ...

    def load_artifact(self, artifact_id: str) -> StoredReplayArtifact: ...

    def load_injection(
        self,
        artifact_id: str,
    ) -> tuple[InjectionArtifact, bytes]: ...

    def load_manifest_for_session(
        self,
        session_id: str,
        decision_id: str,
        usage_decision_id: str,
        injection_artifact_id: str,
    ) -> DecisionReplayManifest: ...


class GateEvidenceSessionReader(Protocol):
    def get(self, session_id: str) -> GateSession: ...


@dataclass(frozen=True)
class GateEvidenceEventDraft:
    event_type: str
    session_id: str
    record_id: str
    occurred_at: str
    payload: Mapping[str, object]
    artifact_refs: tuple[EventArtifactRef, ...]

    def __post_init__(self) -> None:
        if self.event_type not in GATE_EVIDENCE_EVENT_TYPES:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_TYPE_INVALID",
                "Gate evidence event type is invalid",
            )
        for value, name in (
            (self.session_id, "session_id"),
            (self.record_id, "record_id"),
        ):
            if type(value) is not str or not value or len(value) > 128:
                _fail(
                    "TBM_GATE_EVIDENCE_EVENT_DRAFT_INVALID",
                    f"{name} must be a bounded identifier",
                )
        try:
            canonical_rfc3339(self.occurred_at)
        except (TypeError, ValueError) as error:
            raise GateEvidenceEventV1Error(
                "TBM_GATE_EVIDENCE_EVENT_TIMESTAMP_INVALID",
                "Gate evidence event timestamp is invalid",
            ) from error
        if not isinstance(self.payload, Mapping):
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_DRAFT_INVALID",
                "Gate evidence event payload must be an object",
            )
        if self.payload.get("session_id") != self.session_id:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_DRAFT_INVALID",
                "Gate evidence event payload session does not match",
            )
        if type(self.artifact_refs) is not tuple or any(
            type(item) is not EventArtifactRef for item in self.artifact_refs
        ):
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_DRAFT_INVALID",
                "Gate evidence event artifact references are invalid",
            )

    def command_value(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "record_id": self.record_id,
            "occurred_at": canonical_rfc3339(self.occurred_at),
            "payload": _thaw_json(self.payload),
            "artifact_refs": [item.to_dict() for item in self.artifact_refs],
        }


@dataclass(frozen=True)
class GateEvidenceReducedViews:
    retrieval: Mapping[str, object] | None
    system_gate: Mapping[str, object] | None
    semantic_attempts: tuple[Mapping[str, object], ...]
    final_decision: Mapping[str, object] | None
    injection: Mapping[str, object] | None


@dataclass(frozen=True)
class GateEvidenceViews:
    retrieval: RetrievalSnapshot | None
    system_gate: SystemGateEvaluation | None
    semantic_attempts: tuple[SemanticGateAttempt, ...]
    final_decision: UsageDecision | None
    injection: InjectionArtifact | None
    replay_manifest: DecisionReplayManifest | None


class GateEvidenceEventLedgerProjector:
    """Synchronize retained Gate evidence and rebuild its five current views."""

    def __init__(
        self,
        *,
        ledger_factory: Callable[[LedgerAccessContext], EventLedgerPort],
        access_resolver: Callable[[GateSession], LedgerAccessContext],
        session_reader: GateEvidenceSessionReader,
        evidence_reader: GateEvidenceReader,
        semantic_reader: SemanticGateEvidenceReader,
        artifact_repository: GateEvidenceReplayArtifactRepository,
    ) -> None:
        for callback in (ledger_factory, access_resolver):
            if not callable(callback):
                raise TypeError("Gate evidence event callbacks are invalid")
        for authority, methods in (
            (session_reader, ("get",)),
            (evidence_reader, ("load_snapshot", "load_evaluation")),
            (
                semantic_reader,
                ("load_attempt_with_artifacts", "load_attempt_chain"),
            ),
            (
                artifact_repository,
                (
                    "store_artifact",
                    "load_artifact",
                    "load_injection",
                    "load_manifest_for_session",
                ),
            ),
        ):
            if not all(callable(getattr(authority, name, None)) for name in methods):
                raise TypeError("Gate evidence event authority is invalid")
        self._ledger_factory = ledger_factory
        self._access_resolver = access_resolver
        self._session_reader = session_reader
        self._evidence_reader = evidence_reader
        self._semantic_reader = semantic_reader
        self._artifact_repository = artifact_repository
        self._event_registry = build_gate_evidence_event_registry()

    @property
    def event_registry(self) -> EventTypeRegistry:
        return self._event_registry

    def append_for_transition(
        self,
        current: GateSession | None,
        next_session: GateSession,
    ) -> None:
        """Synchronize every evidence view available on the next revision."""

        if current is not None and type(current) is not GateSession:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_SESSION_INVALID",
                "current session must be exactly GateSession or null",
            )
        if type(next_session) is not GateSession:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_SESSION_INVALID",
                "next session must be exactly GateSession",
            )
        self._synchronize(next_session, attempt_bundles=None)

    def append_semantic_attempt(
        self,
        bundle: StoredSemanticGateAttemptArtifacts,
    ) -> None:
        """Append one retained attempt before its authority transaction commits."""

        if type(bundle) is not StoredSemanticGateAttemptArtifacts:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_ATTEMPT_INVALID",
                "attempt bundle must be exactly StoredSemanticGateAttemptArtifacts",
            )
        session = self._session_reader.get(bundle.attempt.session_id)
        if type(session) is not GateSession:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_SESSION_INVALID",
                "session reader returned an invalid GateSession",
            )
        attempts = self._semantic_reader.load_attempt_chain(
            bundle.attempt.system_gate_evaluation_id
        )
        if not attempts or attempts[-1] != bundle.attempt:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_ATTEMPT_INVALID",
                "retained semantic attempt chain does not end at the stored attempt",
            )
        bundles = tuple(
            bundle
            if attempt.attempt_id == bundle.attempt.attempt_id
            else self._semantic_reader.load_attempt_with_artifacts(
                attempt.attempt_id
            )
            for attempt in attempts
        )
        self._synchronize(session, attempt_bundles=bundles)

    def read_events(self, session: GateSession) -> tuple[CanonicalEvent, ...]:
        if type(session) is not GateSession:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_SESSION_INVALID",
                "session must be exactly GateSession",
            )
        access = self._access_resolver(session)
        _verify_access(access, session)
        ledger = self._ledger_factory(access)
        _verify_ledger(ledger)
        try:
            return _read_stream(ledger, gate_evidence_stream_id(session.session_id))
        finally:
            close = getattr(ledger, "close", None)
            if callable(close):
                close()

    def rebuild_current(self, session: GateSession) -> GateEvidenceViews:
        events = self.read_events(session)
        reduced = reduce_gate_evidence_events(
            events,
            event_registry=self._event_registry,
        )
        return self._hydrate(session, events, reduced)

    def export_replay_bundle(
        self,
        session: GateSession,
        *,
        allowed_classifications: frozenset[DataClassification],
        max_content_bytes: int = REPLAY_EXPORT_MAX_CONTENT_BYTES,
    ) -> ReplayBundleExport:
        """Build the existing v3 replay export from events plus artifacts."""

        if (
            type(allowed_classifications) is not frozenset
            or not allowed_classifications
            or any(
                item
                not in {"public", "internal", "confidential", "restricted"}
                for item in allowed_classifications
            )
        ):
            _fail(
                "TBM_GATE_EVIDENCE_REPLAY_EXPORT_FORBIDDEN",
                "replay export classification allowlist is invalid",
            )
        if (
            type(max_content_bytes) is not int
            or not 1 <= max_content_bytes <= REPLAY_EXPORT_MAX_CONTENT_BYTES
        ):
            _fail(
                "TBM_GATE_EVIDENCE_REPLAY_EXPORT_INVALID",
                "replay export byte limit is invalid",
            )
        views = self.rebuild_current(session)
        manifest = views.replay_manifest
        injection = views.injection
        if manifest is None or injection is None:
            _fail(
                "TBM_GATE_EVIDENCE_REPLAY_EXPORT_NOT_FOUND",
                "Gate evidence stream has no finalized replay bundle",
            )
        artifacts: dict[str, StoredReplayArtifact] = {}
        total_content_bytes = 0
        for component_name, content_sha256 in manifest.components:
            if content_sha256 is None:
                continue
            stored = self._artifact_repository.load_artifact(
                artifact_id_from_sha256(content_sha256)
            )
            if stored.artifact.content_sha256 != content_sha256:
                _fail(
                    "TBM_GATE_EVIDENCE_REPLAY_EXPORT_MISMATCH",
                    "replay component differs from the event manifest",
                )
            if stored.artifact.classification not in allowed_classifications:
                _fail(
                    "TBM_GATE_EVIDENCE_REPLAY_EXPORT_FORBIDDEN",
                    "replay component classification is not allowed",
                )
            total_content_bytes += stored.artifact.size_bytes
            if total_content_bytes > max_content_bytes:
                _fail(
                    "TBM_GATE_EVIDENCE_REPLAY_EXPORT_TOO_LARGE",
                    "replay export exceeds the caller byte limit",
                )
            artifacts[component_name] = stored
        try:
            return build_replay_bundle_export(
                manifest=manifest,
                injection=injection,
                artifacts=artifacts,
            )
        except V3ContractError as error:
            raise GateEvidenceEventV1Error(
                "TBM_GATE_EVIDENCE_REPLAY_EXPORT_MISMATCH",
                "events and artifacts do not form an exact replay export",
            ) from error

    def _synchronize(
        self,
        session: GateSession,
        *,
        attempt_bundles: tuple[StoredSemanticGateAttemptArtifacts, ...] | None,
    ) -> None:
        access = self._access_resolver(session)
        _verify_access(access, session)
        drafts = self._desired_drafts(session, attempt_bundles=attempt_bundles)
        ledger = self._ledger_factory(access)
        _verify_ledger(ledger)
        try:
            stream_id = gate_evidence_stream_id(session.session_id)
            retained = _read_stream(ledger, stream_id)
            suffix = _missing_drafts(retained, drafts)
            if suffix:
                self._append_drafts(
                    ledger,
                    access=access,
                    retained=retained,
                    drafts=suffix,
                    session=session,
                )
            rebuilt_events = _read_stream(ledger, stream_id)
            reduced = reduce_gate_evidence_events(
                rebuilt_events,
                event_registry=self._event_registry,
            )
            views = self._hydrate(session, rebuilt_events, reduced)
            _verify_views_cover_drafts(views, drafts)
        finally:
            close = getattr(ledger, "close", None)
            if callable(close):
                close()

    def _append_drafts(
        self,
        ledger: EventLedgerPort,
        *,
        access: LedgerAccessContext,
        retained: tuple[CanonicalEvent, ...],
        drafts: tuple[GateEvidenceEventDraft, ...],
        session: GateSession,
    ) -> None:
        if not drafts:
            return
        expected_version = retained[-1].stream_version if retained else 0
        previous_event = retained[-1] if retained else None
        recorded_at = _batch_recorded_at(drafts, previous_event)
        for attempt in range(GATE_EVIDENCE_EVENT_MAX_APPEND_RETRIES):
            high_watermark = ledger.read_global(
                after_position=0,
                limit=1,
            ).high_watermark_global_position
            events, idempotency = build_gate_evidence_event_batch(
                drafts,
                access=access,
                expected_stream_version=expected_version,
                next_global_position=high_watermark + 1,
                previous_event=previous_event,
                recorded_at=recorded_at,
            )
            try:
                ledger.append(
                    gate_evidence_stream_id(session.session_id),
                    expected_version,
                    events,
                    idempotency,
                )
                return
            except EventLedgerConflictError as error:
                if (
                    error.code != "TBM_EVENT_LEDGER_GLOBAL_POSITION_CONFLICT"
                    or attempt + 1 >= GATE_EVIDENCE_EVENT_MAX_APPEND_RETRIES
                ):
                    raise
        raise AssertionError("Gate evidence event append loop did not terminate")

    def _desired_drafts(
        self,
        session: GateSession,
        *,
        attempt_bundles: tuple[StoredSemanticGateAttemptArtifacts, ...] | None,
    ) -> tuple[GateEvidenceEventDraft, ...]:
        drafts: list[GateEvidenceEventDraft] = []
        snapshot: RetrievalSnapshot | None = None
        evaluation: SystemGateEvaluation | None = None
        if session.retrieval_snapshot_id is not None:
            snapshot = self._evidence_reader.load_snapshot(
                session.retrieval_snapshot_id
            )
            if (
                snapshot.session_id != session.session_id
                or snapshot.snapshot_id != session.retrieval_snapshot_id
            ):
                _fail(
                    "TBM_GATE_EVIDENCE_EVENT_EVIDENCE_INVALID",
                    "retrieval snapshot does not match the GateSession",
                )
            record = self._store_record(
                dumps_retrieval_snapshot(snapshot).encode("utf-8"),
                media_type=RETRIEVAL_SNAPSHOT_MEDIA_TYPE,
                created_at=snapshot.created_at,
            )
            drafts.append(_retrieval_draft(snapshot, record))
        if session.system_gate_evaluation_id is not None:
            if snapshot is None:
                _fail(
                    "TBM_GATE_EVIDENCE_EVENT_EVIDENCE_INVALID",
                    "System Gate evidence requires retrieval evidence",
                )
            evaluation = self._evidence_reader.load_evaluation(
                session.system_gate_evaluation_id
            )
            verify_system_gate_evaluation(evaluation, snapshot)
            record = self._store_record(
                dumps_system_gate_evaluation(evaluation).encode("utf-8"),
                media_type=SYSTEM_GATE_EVALUATION_MEDIA_TYPE,
                created_at=evaluation.evaluated_at,
            )
            drafts.append(_system_gate_draft(evaluation, record))

        bundles = attempt_bundles
        if bundles is None and evaluation is not None:
            retained_attempts = self._semantic_reader.load_attempt_chain(
                evaluation.evaluation_id
            )
            retained_attempt_ids = tuple(
                attempt.attempt_id for attempt in retained_attempts
            )
            if (
                session.semantic_gate_attempt_ids
                and session.semantic_gate_attempt_ids != retained_attempt_ids
            ):
                _fail(
                    "TBM_GATE_EVIDENCE_EVENT_EVIDENCE_INVALID",
                    "GateSession semantic attempt IDs differ from retained chain",
                )
            bundles = tuple(
                self._semantic_reader.load_attempt_with_artifacts(attempt_id)
                for attempt_id in retained_attempt_ids
            )
        if bundles:
            if snapshot is None or evaluation is None:
                _fail(
                    "TBM_GATE_EVIDENCE_EVENT_EVIDENCE_INVALID",
                    "semantic attempts require retrieval and System Gate evidence",
                )
            attempts = tuple(bundle.attempt for bundle in bundles)
            verify_semantic_gate_attempt_chain(attempts, evaluation, snapshot)
            for bundle in bundles:
                record = self._store_record(
                    dumps_semantic_gate_attempt(bundle.attempt).encode("utf-8"),
                    media_type=SEMANTIC_GATE_ATTEMPT_MEDIA_TYPE,
                    created_at=bundle.attempt.finished_at,
                )
                self._store_existing_artifact(
                    bundle.prompt.binding.artifact,
                    bundle.prompt.content,
                )
                response_artifact: ContentAddressedArtifact | None = None
                if bundle.response is not None:
                    response_artifact = bundle.response.binding.artifact
                    self._store_existing_artifact(
                        response_artifact,
                        bundle.response.content,
                    )
                drafts.append(
                    _semantic_attempt_draft(
                        bundle.attempt,
                        record,
                        bundle.prompt.binding.artifact,
                        response_artifact,
                    )
                )

        if session.usage_decision_id is not None:
            if session.injection_artifact_id is None:
                _fail(
                    "TBM_GATE_EVIDENCE_EVENT_EVIDENCE_INVALID",
                    "final decision requires its injection artifact",
                )
            usage_artifact_id = artifact_id_from_sha256(
                "sha256:"
                + session.usage_decision_id.removeprefix(
                    "usage_decision_sha256_"
                )
            )
            usage_stored = self._artifact_repository.load_artifact(
                usage_artifact_id
            )
            usage = loads_usage_decision_artifact(usage_stored.content)
            injection, snippet = self._artifact_repository.load_injection(
                session.injection_artifact_id
            )
            manifest = self._artifact_repository.load_manifest_for_session(
                session.session_id,
                usage.decision_id,
                session.usage_decision_id,
                session.injection_artifact_id,
            )
            _verify_final_linkage(session, usage, injection, manifest)
            drafts.append(_usage_decision_draft(usage, usage_stored.artifact))
            injection_record = self._store_record(
                dumps_injection_artifact(injection).encode("utf-8"),
                media_type=INJECTION_DESCRIPTOR_MEDIA_TYPE,
                created_at=injection.rendered_at,
            )
            manifest_record = self._store_record(
                dumps_decision_replay_manifest(manifest).encode("utf-8"),
                media_type=REPLAY_MANIFEST_DESCRIPTOR_MEDIA_TYPE,
                created_at=manifest.created_at,
            )
            self._store_existing_artifact(injection.artifact, snippet)
            drafts.append(
                _injection_draft(
                    injection,
                    injection_record,
                    manifest,
                    manifest_record,
                )
            )
        if len(drafts) > GATE_EVIDENCE_EVENT_MAX_BATCH:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_BATCH_INVALID",
                "Gate evidence event batch exceeds its bound",
            )
        return tuple(drafts)

    def _store_record(
        self,
        content: bytes,
        *,
        media_type: str,
        created_at: str,
    ) -> ContentAddressedArtifact:
        artifact = create_content_addressed_artifact(
            content,
            media_type=media_type,
            classification="internal",
            created_at=created_at,
        )
        self._store_existing_artifact(artifact, content)
        return artifact

    def _store_existing_artifact(
        self,
        artifact: ContentAddressedArtifact,
        content: bytes,
    ) -> None:
        self._artifact_repository.store_artifact(artifact, content)
        retained = self._artifact_repository.load_artifact(artifact.artifact_id)
        if retained != StoredReplayArtifact(artifact, content):
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_ARTIFACT_MISMATCH",
                "Gate evidence artifact read-back does not match",
            )

    def _hydrate(
        self,
        session: GateSession,
        events: tuple[CanonicalEvent, ...],
        reduced: GateEvidenceReducedViews,
    ) -> GateEvidenceViews:
        for event in events:
            for artifact_ref in event.artifact_refs:
                retained = self._artifact_repository.load_artifact(
                    artifact_ref.artifact_id
                )
                if not _artifact_ref_matches(artifact_ref, retained.artifact):
                    _fail(
                        "TBM_GATE_EVIDENCE_EVENT_ARTIFACT_MISMATCH",
                        "event artifact reference does not match retained bytes",
                    )
        snapshot = cast(
            RetrievalSnapshot | None,
            self._load_record(
                reduced.retrieval,
                media_type=RETRIEVAL_SNAPSHOT_MEDIA_TYPE,
                loader=loads_retrieval_snapshot,
            ),
        )
        evaluation = cast(
            SystemGateEvaluation | None,
            self._load_record(
                reduced.system_gate,
                media_type=SYSTEM_GATE_EVALUATION_MEDIA_TYPE,
                loader=loads_system_gate_evaluation,
            ),
        )
        attempts = tuple(
            cast(
                SemanticGateAttempt,
                self._load_record(
                    payload,
                    media_type=SEMANTIC_GATE_ATTEMPT_MEDIA_TYPE,
                    loader=loads_semantic_gate_attempt,
                ),
            )
            for payload in reduced.semantic_attempts
        )
        usage = cast(
            UsageDecision | None,
            self._load_record(
                reduced.final_decision,
                media_type=USAGE_DECISION_ARTIFACT_MEDIA_TYPE,
                loader=loads_usage_decision_artifact,
            ),
        )
        injection = cast(
            InjectionArtifact | None,
            self._load_record(
                reduced.injection,
                media_type=INJECTION_DESCRIPTOR_MEDIA_TYPE,
                loader=loads_injection_artifact,
            ),
        )
        manifest: DecisionReplayManifest | None = None
        if reduced.injection is not None:
            manifest_payload = {
                "record_artifact_id": reduced.injection[
                    "manifest_artifact_id"
                ],
                "record_content_sha256": reduced.injection[
                    "manifest_content_sha256"
                ],
            }
            manifest = cast(
                DecisionReplayManifest,
                self._load_record(
                    manifest_payload,
                    media_type=REPLAY_MANIFEST_DESCRIPTOR_MEDIA_TYPE,
                    loader=loads_decision_replay_manifest,
                ),
            )
        _verify_hydrated_views(
            session,
            snapshot,
            evaluation,
            attempts,
            usage,
            injection,
            manifest,
        )
        return GateEvidenceViews(
            retrieval=snapshot,
            system_gate=evaluation,
            semantic_attempts=attempts,
            final_decision=usage,
            injection=injection,
            replay_manifest=manifest,
        )

    def _load_record(
        self,
        payload: Mapping[str, object] | None,
        *,
        media_type: str,
        loader: Callable[[str | bytes], object],
    ) -> object | None:
        if payload is None:
            return None
        artifact_id = payload.get("record_artifact_id")
        content_sha256 = payload.get("record_content_sha256")
        if type(artifact_id) is not str or type(content_sha256) is not str:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_PROJECTION_INVALID",
                "projection is missing its record artifact descriptor",
            )
        stored = self._artifact_repository.load_artifact(artifact_id)
        if (
            stored.artifact.content_sha256 != content_sha256
            or stored.artifact.media_type != media_type
        ):
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_ARTIFACT_MISMATCH",
                "projection record artifact does not match retained bytes",
            )
        try:
            return loader(stored.content)
        except (TypeError, ValueError) as error:
            raise GateEvidenceEventV1Error(
                "TBM_GATE_EVIDENCE_EVENT_ARTIFACT_INVALID",
                "Gate evidence record artifact is invalid",
            ) from error


def gate_evidence_stream_id(session_id: str) -> str:
    if type(session_id) is not str or not session_id or len(session_id) > 128:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_SESSION_INVALID",
            "session_id must be a bounded identifier",
        )
    return "stream_ge_" + hashlib.sha256(
        ("tbm.gate-evidence-stream.v1\x00" + session_id).encode("utf-8")
    ).hexdigest()


def build_gate_evidence_event_batch(
    drafts: tuple[GateEvidenceEventDraft, ...],
    *,
    access: LedgerAccessContext,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> tuple[tuple[CanonicalEvent, ...], LedgerIdempotency]:
    if (
        type(drafts) is not tuple
        or not 1 <= len(drafts) <= GATE_EVIDENCE_EVENT_MAX_BATCH
        or any(type(item) is not GateEvidenceEventDraft for item in drafts)
    ):
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_BATCH_INVALID",
            "event drafts must be a bounded non-empty tuple",
        )
    if type(access) is not LedgerAccessContext:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_ACCESS_INVALID",
            "ledger access must be exactly LedgerAccessContext",
        )
    if type(expected_stream_version) is not int or expected_stream_version < 0:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_BATCH_INVALID",
            "expected stream version is invalid",
        )
    if type(next_global_position) is not int or next_global_position < 1:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_BATCH_INVALID",
            "next global position is invalid",
        )
    session_id = drafts[0].session_id
    if any(draft.session_id != session_id for draft in drafts):
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_BATCH_INVALID",
            "event drafts must belong to one GateSession",
        )
    stream_id = gate_evidence_stream_id(session_id)
    if previous_event is None:
        if expected_stream_version != 0:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_BATCH_INVALID",
                "nonzero stream version requires its parent event",
            )
    elif (
        type(previous_event) is not CanonicalEvent
        or previous_event.stream_id != stream_id
        or previous_event.stream_version != expected_stream_version
    ):
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_BATCH_INVALID",
            "previous event does not match the expected stream head",
        )
    command_value = {
        "protocol_version": GATE_EVIDENCE_EVENT_PROTOCOL_VERSION,
        "partition_sha256": access.partition.partition_sha256,
        "stream_id": stream_id,
        "expected_stream_version": expected_stream_version,
        "drafts": [draft.command_value() for draft in drafts],
    }
    command_sha256 = _domain_sha256(
        b"tbm.gate-evidence-event-command.v1\x00",
        command_value,
    )
    idempotency_key_sha256 = _domain_sha256(
        b"tbm.gate-evidence-event-idempotency.v1\x00",
        command_value,
    )
    idempotency = LedgerIdempotency(
        idempotency_key_sha256=idempotency_key_sha256,
        command_sha256=command_sha256,
    )
    trusted_context = access.event_trusted_context()
    correlation_digest = hashlib.sha256(
        (access.partition.partition_sha256 + "\x00" + session_id).encode("utf-8")
    ).hexdigest()
    parent = previous_event
    events: list[CanonicalEvent] = []
    for offset, draft in enumerate(drafts):
        event_digest = hashlib.sha256(
            (command_sha256 + f"\x00{offset}").encode("utf-8")
        ).hexdigest()
        event = build_canonical_event(
            event_id="evt_ge_" + event_digest,
            event_type=draft.event_type,
            event_version=1,
            event_kind="domain",
            origin="native",
            source=None,
            stream_id=stream_id,
            stream_type=GATE_EVIDENCE_EVENT_STREAM_TYPE,
            stream_version=expected_stream_version + offset + 1,
            global_position=next_global_position + offset,
            trusted_context=trusted_context,
            request_id="request_ge_" + event_digest[:32],
            idempotency_key_sha256=idempotency_key_sha256,
            request_sha256=command_sha256,
            correlation_id="correlation_ge_" + correlation_digest[:32],
            causation_id=None if parent is None else parent.event_id,
            occurred_at=draft.occurred_at,
            recorded_at=recorded_at,
            producer="tbm_durable_gate_runtime",
            producer_version="f2-v1",
            payload_schema=_PAYLOAD_SCHEMAS[draft.event_type],
            previous_stream_event_sha256=(
                None if parent is None else parent.event_sha256
            ),
            classification="internal",
            retention_policy_id="retention_gate_evidence_events",
            artifact_refs=draft.artifact_refs,
            payload=draft.payload,
        )
        events.append(event)
        parent = event
    return tuple(events), idempotency


def build_gate_evidence_event_registry() -> EventTypeRegistry:
    registry = EventTypeRegistry()
    schemas = _payload_json_schemas()
    for event_type in GATE_EVIDENCE_EVENT_TYPES:
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


def gate_evidence_event_payload_dispatch_schema() -> dict[str, object]:
    schema = build_gate_evidence_event_registry().dispatch_schema()
    schema["$id"] = GATE_EVIDENCE_EVENT_PAYLOAD_SCHEMA_ID
    schema["title"] = "Trace-backed Memory Gate evidence event payloads v1"
    schema["$comment"] = (
        "Generated from the sealed Gate evidence tbm.event-registry.v1 catalog. "
        "The Gate evidence event runtime remains the authoritative validator."
    )
    return schema


def dumps_gate_evidence_event_payload_dispatch_schema() -> str:
    return json.dumps(
        gate_evidence_event_payload_dispatch_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def build_retrieval_current_reducer() -> FunctionalReducer:
    return _build_singleton_reducer(
        reducer_id=RETRIEVAL_CURRENT_REDUCER_ID,
        projection=RETRIEVAL_CURRENT_PROJECTION,
        event_type=RETRIEVAL_SNAPSHOT_RECORDED,
    )


def build_system_gate_current_reducer() -> FunctionalReducer:
    return _build_singleton_reducer(
        reducer_id=SYSTEM_GATE_CURRENT_REDUCER_ID,
        projection=SYSTEM_GATE_CURRENT_PROJECTION,
        event_type=SYSTEM_GATE_EVALUATED,
    )


def build_final_decision_current_reducer() -> FunctionalReducer:
    return _build_singleton_reducer(
        reducer_id=FINAL_DECISION_CURRENT_REDUCER_ID,
        projection=FINAL_DECISION_CURRENT_PROJECTION,
        event_type=USAGE_DECISION_RECORDED,
    )


def build_injection_current_reducer() -> FunctionalReducer:
    return _build_singleton_reducer(
        reducer_id=INJECTION_CURRENT_REDUCER_ID,
        projection=INJECTION_CURRENT_PROJECTION,
        event_type=INJECTION_ARTIFACT_RECORDED,
    )


def build_semantic_attempt_chain_reducer() -> FunctionalReducer:
    event_type = SEMANTIC_GATE_ATTEMPT_RECORDED
    descriptor = _reducer_descriptor(
        reducer_id=SEMANTIC_ATTEMPT_CHAIN_REDUCER_ID,
        projection=SEMANTIC_ATTEMPT_CHAIN_PROJECTION,
        event_type=event_type,
        algorithm="semantic-attempt-chain",
    )

    def initial() -> Mapping[str, object]:
        return {"attempts": []}

    def transition(
        state: Mapping[str, object],
        reducer_event: ReducerEvent,
    ) -> Mapping[str, object]:
        payload = _typed_payload(reducer_event, event_type)
        raw_attempts = _thaw_json(state.get("attempts"))
        if type(raw_attempts) is not list or any(
            type(item) is not dict for item in raw_attempts
        ):
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_STATE_INVALID",
                "semantic attempt reducer state is invalid",
            )
        attempts = cast(list[dict[str, object]], raw_attempts)
        expected_sequence = len(attempts) + 1
        previous_attempt_id = (
            None if not attempts else attempts[-1].get("attempt_id")
        )
        if (
            payload.get("sequence") != expected_sequence
            or payload.get("previous_attempt_id") != previous_attempt_id
        ):
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_TRANSITION_INVALID",
                "semantic attempt event does not extend the exact chain",
            )
        if attempts:
            for field in (
                "session_id",
                "retrieval_snapshot_id",
                "system_gate_evaluation_id",
            ):
                if payload.get(field) != attempts[-1].get(field):
                    _fail(
                        "TBM_GATE_EVIDENCE_EVENT_TRANSITION_INVALID",
                        "semantic attempt event belongs to another chain",
                    )
        return {"attempts": [*attempts, payload]}

    return FunctionalReducer(descriptor, initial, transition)


def reduce_gate_evidence_events(
    events: tuple[CanonicalEvent, ...],
    *,
    event_registry: EventTypeRegistry | None = None,
) -> GateEvidenceReducedViews:
    if type(events) is not tuple or any(
        type(event) is not CanonicalEvent for event in events
    ):
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_SEQUENCE_INVALID",
            "events must be a tuple of CanonicalEvent values",
        )
    registry = (
        build_gate_evidence_event_registry()
        if event_registry is None
        else event_registry
    )
    if type(registry) is not EventTypeRegistry or not registry.sealed:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_REGISTRY_INVALID",
            "event registry must be a sealed EventTypeRegistry",
        )
    reducers = {
        RETRIEVAL_SNAPSHOT_RECORDED: build_retrieval_current_reducer(),
        SYSTEM_GATE_EVALUATED: build_system_gate_current_reducer(),
        SEMANTIC_GATE_ATTEMPT_RECORDED: build_semantic_attempt_chain_reducer(),
        USAGE_DECISION_RECORDED: build_final_decision_current_reducer(),
        INJECTION_ARTIFACT_RECORDED: build_injection_current_reducer(),
    }
    states = {
        event_type: initial_reducer_state(reducer)
        for event_type, reducer in reducers.items()
    }
    parent: CanonicalEvent | None = None
    previous_stage = 0
    stream_id: str | None = None
    for event in events:
        if parent is None:
            if (
                event.stream_version != 1
                or event.previous_stream_event_sha256 is not None
            ):
                _fail(
                    "TBM_GATE_EVIDENCE_EVENT_SEQUENCE_INVALID",
                    "Gate evidence stream must begin at version one",
                )
            stream_id = event.stream_id
        else:
            verify_event_parent(event, parent)
        if (
            event.stream_type != GATE_EVIDENCE_EVENT_STREAM_TYPE
            or event.stream_id != stream_id
        ):
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_SEQUENCE_INVALID",
                "Gate evidence event stream identity is invalid",
            )
        stage = _EVENT_STAGES.get(event.event_type)
        if stage is None or stage < previous_stage:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_SEQUENCE_INVALID",
                "Gate evidence event stages are out of order",
            )
        typed = registry.consume(event, target_version=1)
        _verify_event_artifact_refs(event)
        reducer = reducers[event.event_type]
        states[event.event_type] = execute_reducer_step(
            reducer,
            states[event.event_type].state,
            ReducerEvent(event, typed),
        )
        previous_stage = stage
        parent = event
    retrieval = _current_payload(states[RETRIEVAL_SNAPSHOT_RECORDED].state)
    system_gate = _current_payload(states[SYSTEM_GATE_EVALUATED].state)
    semantic_attempts = _attempt_payloads(
        states[SEMANTIC_GATE_ATTEMPT_RECORDED].state
    )
    final_decision = _current_payload(states[USAGE_DECISION_RECORDED].state)
    injection = _current_payload(states[INJECTION_ARTIFACT_RECORDED].state)
    if system_gate is not None and retrieval is None:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_SEQUENCE_INVALID",
            "System Gate view requires a retrieval view",
        )
    if semantic_attempts and system_gate is None:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_SEQUENCE_INVALID",
            "semantic attempt view requires a System Gate view",
        )
    if final_decision is not None and not semantic_attempts:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_SEQUENCE_INVALID",
            "final decision view requires semantic attempts",
        )
    if injection is not None and final_decision is None:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_SEQUENCE_INVALID",
            "injection view requires a final decision view",
        )
    return GateEvidenceReducedViews(
        retrieval=retrieval,
        system_gate=system_gate,
        semantic_attempts=semantic_attempts,
        final_decision=final_decision,
        injection=injection,
    )


def _build_singleton_reducer(
    *,
    reducer_id: str,
    projection: str,
    event_type: str,
) -> FunctionalReducer:
    descriptor = _reducer_descriptor(
        reducer_id=reducer_id,
        projection=projection,
        event_type=event_type,
        algorithm="singleton-current",
    )

    def initial() -> Mapping[str, object]:
        return {"current": None}

    def transition(
        state: Mapping[str, object],
        reducer_event: ReducerEvent,
    ) -> Mapping[str, object]:
        if _thaw_json(state.get("current")) is not None:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_TRANSITION_INVALID",
                "singleton Gate evidence view cannot be replaced",
            )
        return {"current": _typed_payload(reducer_event, event_type)}

    return FunctionalReducer(descriptor, initial, transition)


def _reducer_descriptor(
    *,
    reducer_id: str,
    projection: str,
    event_type: str,
    algorithm: str,
) -> ReducerDescriptor:
    return ReducerDescriptor(
        reducer_id=reducer_id,
        reducer_version=1,
        input_event_types=(event_type,),
        output_projection=projection,
        output_schema_version=1,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": algorithm,
                "algorithm_version": 1,
                "event_type": event_type,
                "projection": projection,
            },
        ),
        configuration_sha256=_domain_sha256(
            b"tbm.reducer-configuration.v1\x00",
            {"configuration": "none", "version": 1},
        ),
        target_event_versions={event_type: 1},
    )


def _typed_payload(
    reducer_event: ReducerEvent,
    expected_event_type: str,
) -> dict[str, object]:
    typed = reducer_event.typed_event
    if typed is None or reducer_event.source_event.event_type != expected_event_type:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_TYPED_INPUT_REQUIRED",
            "Gate evidence reducer requires its exact typed event",
        )
    payload = _thaw_json(typed.payload)
    if type(payload) is not dict:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_PAYLOAD_INVALID",
            "Gate evidence event payload must be an object",
        )
    if reducer_event.source_event.stream_id != gate_evidence_stream_id(
        cast(str, payload.get("session_id"))
    ):
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_STREAM_INVALID",
            "Gate evidence event stream does not match its session",
        )
    return cast(dict[str, object], payload)


def _retrieval_draft(
    snapshot: RetrievalSnapshot,
    record: ContentAddressedArtifact,
) -> GateEvidenceEventDraft:
    payload = {
        "session_id": snapshot.session_id,
        "retrieval_snapshot_id": snapshot.snapshot_id,
        "authorization_event_id": snapshot.authorization_event_id,
        **_record_fields(record),
    }
    return _draft(
        RETRIEVAL_SNAPSHOT_RECORDED,
        snapshot.session_id,
        snapshot.snapshot_id,
        snapshot.created_at,
        payload,
        (record,),
    )


def _system_gate_draft(
    evaluation: SystemGateEvaluation,
    record: ContentAddressedArtifact,
) -> GateEvidenceEventDraft:
    payload = {
        "session_id": evaluation.session_id,
        "system_gate_evaluation_id": evaluation.evaluation_id,
        "retrieval_snapshot_id": evaluation.retrieval_snapshot_id,
        "authorization_event_id": evaluation.authorization_event_id,
        **_record_fields(record),
    }
    return _draft(
        SYSTEM_GATE_EVALUATED,
        evaluation.session_id,
        evaluation.evaluation_id,
        evaluation.evaluated_at,
        payload,
        (record,),
    )


def _semantic_attempt_draft(
    attempt: SemanticGateAttempt,
    record: ContentAddressedArtifact,
    prompt: ContentAddressedArtifact,
    response: ContentAddressedArtifact | None,
) -> GateEvidenceEventDraft:
    payload = {
        "session_id": attempt.session_id,
        "attempt_id": attempt.attempt_id,
        "retrieval_snapshot_id": attempt.retrieval_snapshot_id,
        "system_gate_evaluation_id": attempt.system_gate_evaluation_id,
        "sequence": attempt.sequence,
        "previous_attempt_id": attempt.previous_attempt_id,
        "status": attempt.status,
        "decision_id": attempt.decision_id,
        "prompt_artifact_id": prompt.artifact_id,
        "prompt_content_sha256": prompt.content_sha256,
        "response_artifact_id": (
            None if response is None else response.artifact_id
        ),
        "response_content_sha256": (
            None if response is None else response.content_sha256
        ),
        **_record_fields(record),
    }
    artifacts = (record, prompt) if response is None else (record, prompt, response)
    return _draft(
        SEMANTIC_GATE_ATTEMPT_RECORDED,
        attempt.session_id,
        attempt.attempt_id,
        attempt.finished_at,
        payload,
        artifacts,
    )


def _usage_decision_draft(
    usage: UsageDecision,
    record: ContentAddressedArtifact,
) -> GateEvidenceEventDraft:
    payload = {
        "session_id": usage.session_id,
        "usage_decision_id": usage.usage_decision_id,
        "decision_id": usage.decision_id,
        "retrieval_snapshot_id": usage.retrieval_snapshot_id,
        "system_gate_evaluation_id": usage.system_gate_evaluation_id,
        "semantic_gate_attempt_id": usage.semantic_gate_attempt_id,
        "injection_artifact_id": usage.injection_artifact_id,
        **_record_fields(record),
    }
    return _draft(
        USAGE_DECISION_RECORDED,
        usage.session_id,
        usage.usage_decision_id,
        usage.created_at,
        payload,
        (record,),
    )


def _injection_draft(
    injection: InjectionArtifact,
    record: ContentAddressedArtifact,
    manifest: DecisionReplayManifest,
    manifest_record: ContentAddressedArtifact,
) -> GateEvidenceEventDraft:
    payload = {
        "session_id": injection.session_id,
        "injection_artifact_id": injection.artifact.artifact_id,
        "injection_content_sha256": injection.artifact.content_sha256,
        "usage_decision_id": injection.usage_decision_id,
        "decision_id": injection.decision_id,
        "manifest_sha256": manifest.manifest_sha256,
        "manifest_artifact_id": manifest_record.artifact_id,
        "manifest_content_sha256": manifest_record.content_sha256,
        **_record_fields(record),
    }
    return _draft(
        INJECTION_ARTIFACT_RECORDED,
        injection.session_id,
        injection.artifact.artifact_id,
        injection.rendered_at,
        payload,
        (record, injection.artifact, manifest_record),
    )


def _draft(
    event_type: str,
    session_id: str,
    record_id: str,
    occurred_at: str,
    payload: Mapping[str, object],
    artifacts: tuple[ContentAddressedArtifact, ...],
) -> GateEvidenceEventDraft:
    refs = tuple(
        sorted(
            {
                artifact.artifact_id: _event_artifact_ref(artifact)
                for artifact in artifacts
            }.values(),
            key=lambda item: item.artifact_id,
        )
    )
    return GateEvidenceEventDraft(
        event_type=event_type,
        session_id=session_id,
        record_id=record_id,
        occurred_at=canonical_rfc3339(occurred_at),
        payload=payload,
        artifact_refs=refs,
    )


def _record_fields(artifact: ContentAddressedArtifact) -> dict[str, object]:
    return {
        "record_artifact_id": artifact.artifact_id,
        "record_content_sha256": artifact.content_sha256,
    }


def _event_artifact_ref(
    artifact: ContentAddressedArtifact,
) -> EventArtifactRef:
    return EventArtifactRef(
        artifact_id=artifact.artifact_id,
        content_sha256=artifact.content_sha256,
        media_type=artifact.media_type,
        size_bytes=artifact.size_bytes,
        classification=artifact.classification,
        retention_policy_id="retention_gate_evidence_artifacts",
        encryption_key_id=artifact.encryption_key_id,
        availability="available",
    )


def _missing_drafts(
    retained: tuple[CanonicalEvent, ...],
    desired: tuple[GateEvidenceEventDraft, ...],
) -> tuple[GateEvidenceEventDraft, ...]:
    if len(retained) > len(desired):
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_PROJECTION_DRIFT",
            "retained Gate evidence stream exceeds the desired projection",
        )
    for event, draft in zip(retained, desired, strict=False):
        if (
            event.event_type != draft.event_type
            or _thaw_json(event.payload) != _thaw_json(draft.payload)
            or event.artifact_refs != draft.artifact_refs
            or event.occurred_at != canonical_rfc3339(draft.occurred_at)
        ):
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_PROJECTION_DRIFT",
                "retained Gate evidence event differs from authority evidence",
            )
    return desired[len(retained) :]


def _verify_event_artifact_refs(event: CanonicalEvent) -> None:
    payload = _thaw_json(event.payload)
    if type(payload) is not dict:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_PAYLOAD_INVALID",
            "Gate evidence event payload must be an object",
        )
    fields = {
        RETRIEVAL_SNAPSHOT_RECORDED: ("record_artifact_id",),
        SYSTEM_GATE_EVALUATED: ("record_artifact_id",),
        SEMANTIC_GATE_ATTEMPT_RECORDED: (
            "record_artifact_id",
            "prompt_artifact_id",
            "response_artifact_id",
        ),
        USAGE_DECISION_RECORDED: ("record_artifact_id",),
        INJECTION_ARTIFACT_RECORDED: (
            "record_artifact_id",
            "injection_artifact_id",
            "manifest_artifact_id",
        ),
    }[event.event_type]
    expected = {
        value
        for field in fields
        if (value := payload.get(field)) is not None
    }
    actual = {item.artifact_id for item in event.artifact_refs}
    if expected != actual:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_ARTIFACT_MISMATCH",
            "Gate evidence event artifact references are incomplete",
        )


def _artifact_ref_matches(
    reference: EventArtifactRef,
    artifact: ContentAddressedArtifact,
) -> bool:
    return (
        reference.artifact_id == artifact.artifact_id
        and reference.content_sha256 == artifact.content_sha256
        and reference.media_type == artifact.media_type
        and reference.size_bytes == artifact.size_bytes
        and reference.classification == artifact.classification
        and reference.encryption_key_id == artifact.encryption_key_id
        and reference.availability == "available"
    )


def _verify_final_linkage(
    session: GateSession,
    usage: UsageDecision,
    injection: InjectionArtifact,
    manifest: DecisionReplayManifest,
) -> None:
    if (
        usage.session_id != session.session_id
        or usage.usage_decision_id != session.usage_decision_id
        or usage.decision_id != session.decision_id
        or usage.injection_artifact_id != session.injection_artifact_id
        or injection.session_id != session.session_id
        or injection.usage_decision_id != usage.usage_decision_id
        or injection.decision_id != usage.decision_id
        or injection.artifact.artifact_id != usage.injection_artifact_id
        or manifest.session_id != session.session_id
        or manifest.usage_decision_id != usage.usage_decision_id
        or manifest.decision_id != usage.decision_id
        or manifest.injection_artifact_id != injection.artifact.artifact_id
    ):
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_EVIDENCE_INVALID",
            "final decision, injection, and replay manifest linkage differs",
        )


def _verify_hydrated_views(
    session: GateSession,
    snapshot: RetrievalSnapshot | None,
    evaluation: SystemGateEvaluation | None,
    attempts: tuple[SemanticGateAttempt, ...],
    usage: UsageDecision | None,
    injection: InjectionArtifact | None,
    manifest: DecisionReplayManifest | None,
) -> None:
    if snapshot is not None:
        if snapshot.session_id != session.session_id:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_PROJECTION_MISMATCH",
                "retrieval projection belongs to another session",
            )
    if evaluation is not None:
        if snapshot is None:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_PROJECTION_MISMATCH",
                "System Gate projection lacks retrieval evidence",
            )
        verify_system_gate_evaluation(evaluation, snapshot)
    if attempts:
        if snapshot is None or evaluation is None:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_PROJECTION_MISMATCH",
                "semantic projection lacks prior Gate evidence",
            )
        verify_semantic_gate_attempt_chain(attempts, evaluation, snapshot)
    if usage is not None or injection is not None or manifest is not None:
        if usage is None or injection is None or manifest is None:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_PROJECTION_MISMATCH",
                "final Gate evidence projection is incomplete",
            )
        _verify_final_linkage(session, usage, injection, manifest)
        if not attempts or usage.semantic_gate_attempt_id != attempts[-1].attempt_id:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_PROJECTION_MISMATCH",
                "final decision does not reference the retained semantic head",
            )
        if snapshot is None or evaluation is None or (
            usage.retrieval_snapshot_id != snapshot.snapshot_id
            or usage.system_gate_evaluation_id != evaluation.evaluation_id
        ):
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_PROJECTION_MISMATCH",
                "final decision does not reference retained Gate evidence",
            )


def _verify_views_cover_drafts(
    views: GateEvidenceViews,
    drafts: tuple[GateEvidenceEventDraft, ...],
) -> None:
    expected_types = tuple(draft.event_type for draft in drafts)
    actual_types: list[str] = []
    if views.retrieval is not None:
        actual_types.append(RETRIEVAL_SNAPSHOT_RECORDED)
    if views.system_gate is not None:
        actual_types.append(SYSTEM_GATE_EVALUATED)
    actual_types.extend(
        SEMANTIC_GATE_ATTEMPT_RECORDED for _attempt in views.semantic_attempts
    )
    if views.final_decision is not None:
        actual_types.append(USAGE_DECISION_RECORDED)
    if views.injection is not None:
        actual_types.append(INJECTION_ARTIFACT_RECORDED)
    if tuple(actual_types) != expected_types:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_PROJECTION_MISMATCH",
            "Gate evidence reducer did not reproduce desired evidence views",
        )


def _current_payload(
    state: Mapping[str, object],
) -> Mapping[str, object] | None:
    current = _thaw_json(state.get("current"))
    if current is None:
        return None
    if type(current) is not dict:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_STATE_INVALID",
            "Gate evidence current reducer state is invalid",
        )
    return cast(dict[str, object], current)


def _attempt_payloads(
    state: Mapping[str, object],
) -> tuple[Mapping[str, object], ...]:
    attempts = _thaw_json(state.get("attempts"))
    if type(attempts) is not list or any(type(item) is not dict for item in attempts):
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_STATE_INVALID",
            "semantic attempt reducer state is invalid",
        )
    return tuple(cast(list[dict[str, object]], attempts))


def _payload_json_schemas() -> dict[str, Mapping[str, object]]:
    identifier = {"type": "string", "minLength": 1, "maxLength": 128}
    artifact_id = {
        "type": "string",
        "pattern": r"^artifact_sha256_[0-9a-f]{64}$",
    }
    digest = {"type": "string", "pattern": r"^sha256:[0-9a-f]{64}$"}
    nullable_identifier = {"oneOf": [{"type": "null"}, identifier]}
    nullable_artifact = {"oneOf": [{"type": "null"}, artifact_id]}
    nullable_digest = {"oneOf": [{"type": "null"}, digest]}

    def schema(
        properties: Mapping[str, object],
    ) -> Mapping[str, object]:
        return {
            "type": "object",
            "additionalProperties": False,
            "required": list(properties),
            "properties": dict(properties),
        }

    common = {
        "session_id": identifier,
        "record_artifact_id": artifact_id,
        "record_content_sha256": digest,
    }
    return {
        RETRIEVAL_SNAPSHOT_RECORDED: schema(
            {
                "session_id": identifier,
                "retrieval_snapshot_id": identifier,
                "authorization_event_id": identifier,
                **{key: common[key] for key in common if key != "session_id"},
            }
        ),
        SYSTEM_GATE_EVALUATED: schema(
            {
                "session_id": identifier,
                "system_gate_evaluation_id": identifier,
                "retrieval_snapshot_id": identifier,
                "authorization_event_id": identifier,
                **{key: common[key] for key in common if key != "session_id"},
            }
        ),
        SEMANTIC_GATE_ATTEMPT_RECORDED: schema(
            {
                "session_id": identifier,
                "attempt_id": identifier,
                "retrieval_snapshot_id": identifier,
                "system_gate_evaluation_id": identifier,
                "sequence": {"type": "integer", "minimum": 1, "maximum": 100},
                "previous_attempt_id": nullable_identifier,
                "status": {"enum": ["succeeded", "failed"]},
                "decision_id": nullable_identifier,
                "prompt_artifact_id": artifact_id,
                "prompt_content_sha256": digest,
                "response_artifact_id": nullable_artifact,
                "response_content_sha256": nullable_digest,
                **{key: common[key] for key in common if key != "session_id"},
            }
        ),
        USAGE_DECISION_RECORDED: schema(
            {
                "session_id": identifier,
                "usage_decision_id": identifier,
                "decision_id": identifier,
                "retrieval_snapshot_id": identifier,
                "system_gate_evaluation_id": identifier,
                "semantic_gate_attempt_id": identifier,
                "injection_artifact_id": artifact_id,
                **{key: common[key] for key in common if key != "session_id"},
            }
        ),
        INJECTION_ARTIFACT_RECORDED: schema(
            {
                "session_id": identifier,
                "injection_artifact_id": artifact_id,
                "injection_content_sha256": digest,
                "usage_decision_id": identifier,
                "decision_id": identifier,
                "manifest_sha256": digest,
                "manifest_artifact_id": artifact_id,
                "manifest_content_sha256": digest,
                **{key: common[key] for key in common if key != "session_id"},
            }
        ),
    }


def _read_stream(
    ledger: EventLedgerPort,
    stream_id: str,
) -> tuple[CanonicalEvent, ...]:
    events: list[CanonicalEvent] = []
    from_version = 1
    while True:
        page = ledger.read_stream(stream_id, from_version=from_version, limit=100)
        events.extend(page.events)
        if not page.has_more:
            break
        if page.next_stream_version is None:
            _fail(
                "TBM_GATE_EVIDENCE_EVENT_SEQUENCE_INVALID",
                "stream page omitted its continuation version",
            )
        from_version = page.next_stream_version
    return tuple(events)


def _verify_access(access: object, session: GateSession) -> None:
    if type(access) is not LedgerAccessContext:
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_ACCESS_INVALID",
            "access resolver must return exactly LedgerAccessContext",
        )
    if (
        access.partition.tenant_id != session.tenant_id
        or access.partition.repository_id != session.repository_id
        or access.principal_id != session.principal_id
        or access.agent_client_id != session.agent_client_id
        or not access.classification_filter.allows("internal")
    ):
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_ACCESS_INVALID",
            "ledger access does not match the GateSession scope",
        )


def _verify_ledger(ledger: object) -> None:
    if not all(
        callable(getattr(ledger, name, None))
        for name in ("append", "read_stream", "read_global")
    ):
        _fail(
            "TBM_GATE_EVIDENCE_EVENT_LEDGER_INVALID",
            "ledger factory returned an invalid event ledger",
        )


def _batch_recorded_at(
    drafts: tuple[GateEvidenceEventDraft, ...],
    parent: CanonicalEvent | None,
) -> str:
    try:
        values = [parse_rfc3339(draft.occurred_at) for draft in drafts]
        if parent is not None:
            values.append(parse_rfc3339(parent.recorded_at))
        return canonical_rfc3339(max(values).isoformat())
    except (TypeError, ValueError) as error:
        raise GateEvidenceEventV1Error(
            "TBM_GATE_EVIDENCE_EVENT_TIMESTAMP_INVALID",
            "Gate evidence event timestamp is invalid",
        ) from error


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [
            _thaw_json(item)
            for item in cast(list[object] | tuple[object, ...], value)
        ]
    return value


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError) as error:
        raise GateEvidenceEventV1Error(
            "TBM_GATE_EVIDENCE_EVENT_CANONICALIZATION_FAILED",
            "Gate evidence event value is not canonical JSON",
        ) from error


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_json_bytes(value)).hexdigest()


def _fail(code: str, message: str) -> NoReturn:
    raise GateEvidenceEventV1Error(code, message)


__all__ = [
    "FINAL_DECISION_CURRENT_PROJECTION",
    "FINAL_DECISION_CURRENT_REDUCER_ID",
    "GATE_EVIDENCE_EVENT_MAX_APPEND_RETRIES",
    "GATE_EVIDENCE_EVENT_MAX_BATCH",
    "GATE_EVIDENCE_EVENT_PAYLOAD_SCHEMA_ID",
    "GATE_EVIDENCE_EVENT_PROTOCOL_VERSION",
    "GATE_EVIDENCE_EVENT_STREAM_TYPE",
    "GATE_EVIDENCE_EVENT_TYPES",
    "INJECTION_ARTIFACT_RECORDED",
    "INJECTION_CURRENT_PROJECTION",
    "INJECTION_CURRENT_REDUCER_ID",
    "INJECTION_DESCRIPTOR_MEDIA_TYPE",
    "REPLAY_MANIFEST_DESCRIPTOR_MEDIA_TYPE",
    "RETRIEVAL_CURRENT_PROJECTION",
    "RETRIEVAL_CURRENT_REDUCER_ID",
    "RETRIEVAL_SNAPSHOT_MEDIA_TYPE",
    "RETRIEVAL_SNAPSHOT_RECORDED",
    "SEMANTIC_ATTEMPT_CHAIN_PROJECTION",
    "SEMANTIC_ATTEMPT_CHAIN_REDUCER_ID",
    "SEMANTIC_GATE_ATTEMPT_MEDIA_TYPE",
    "SEMANTIC_GATE_ATTEMPT_RECORDED",
    "SYSTEM_GATE_CURRENT_PROJECTION",
    "SYSTEM_GATE_CURRENT_REDUCER_ID",
    "SYSTEM_GATE_EVALUATED",
    "SYSTEM_GATE_EVALUATION_MEDIA_TYPE",
    "USAGE_DECISION_RECORDED",
    "GateEvidenceEventDraft",
    "GateEvidenceEventLedgerProjector",
    "GateEvidenceEventV1Error",
    "GateEvidenceReducedViews",
    "GateEvidenceViews",
    "SemanticGateAttemptEventSink",
    "build_final_decision_current_reducer",
    "build_gate_evidence_event_batch",
    "build_gate_evidence_event_registry",
    "build_injection_current_reducer",
    "build_retrieval_current_reducer",
    "build_semantic_attempt_chain_reducer",
    "build_system_gate_current_reducer",
    "dumps_gate_evidence_event_payload_dispatch_schema",
    "gate_evidence_event_payload_dispatch_schema",
    "gate_evidence_stream_id",
    "reduce_gate_evidence_events",
]
