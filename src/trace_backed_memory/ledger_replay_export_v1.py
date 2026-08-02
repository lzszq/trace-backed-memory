from __future__ import annotations

from collections.abc import Callable, Iterator
from contextlib import contextmanager
from contextvars import ContextVar
import hmac
from typing import NoReturn, Protocol

from .contracts_v3 import V3ContractError
from .event_v1 import CanonicalEvent, EventArtifactRef, EventTrustedContext
from .finalization_event_v1 import (
    INJECTION_RENDERED_EVENT,
    FinalizationEventRef,
    FinalizationEventV1Error,
    finalization_event_stream_id,
    parse_injection_rendered_event,
)
from .ledger_port_v1 import (
    EVENT_LEDGER_MAX_READ_PAGE,
    EventLedgerPort,
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from .replay_export_v3 import (
    REPLAY_EXPORT_MAX_CONTENT_BYTES,
    ReplayBundleExport,
    ReplayExportReader,
    export_replay_bundle,
)
from .replay_v3 import (
    ContentAddressedArtifact,
    DataClassification,
    DecisionReplayManifest,
    InjectionArtifact,
    StoredReplayArtifact,
    build_decision_replay_manifest,
    verify_artifact_content,
)


LEDGER_REPLAY_EXPORT_CONTRACT_VERSION = "tbm.ledger-replay-export.v1"
LEDGER_REPLAY_EXPORT_MAX_SCAN_EVENTS = 100_000


class LedgerReplayExportV1Error(V3ContractError):
    """Stable failure while reconstructing replay metadata from events."""


class LedgerReplayArtifactReader(Protocol):
    def load_artifact_descriptor(
        self,
        artifact_id: str,
    ) -> ContentAddressedArtifact: ...

    def load_artifact(self, artifact_id: str) -> StoredReplayArtifact: ...


class LedgerReplayExportReaderV1:
    """Reconstruct replay metadata from finalization events and exact bytes."""

    def __init__(
        self,
        ledger: EventLedgerPort,
        artifact_reader: LedgerReplayArtifactReader,
        *,
        max_scan_events: int = LEDGER_REPLAY_EXPORT_MAX_SCAN_EVENTS,
    ) -> None:
        if not all(
            callable(getattr(ledger, name, None))
            for name in ("read_stream", "read_global")
        ):
            raise TypeError("ledger must satisfy EventLedgerPort reads")
        if not all(
            callable(getattr(artifact_reader, name, None))
            for name in ("load_artifact_descriptor", "load_artifact")
        ):
            raise TypeError("artifact_reader is invalid")
        if (
            type(max_scan_events) is not int
            or not 1 <= max_scan_events <= LEDGER_REPLAY_EXPORT_MAX_SCAN_EVENTS
        ):
            raise ValueError("max_scan_events is invalid")
        self._ledger = ledger
        self._artifact_reader = artifact_reader
        self._max_scan_events = max_scan_events
        self._refs_by_manifest: dict[str, FinalizationEventRef] = {}
        self._refs_by_injection: dict[str, FinalizationEventRef] = {}
        self._artifact_refs: dict[str, EventArtifactRef] = {}

    def load_manifest(
        self,
        manifest_sha256: str,
    ) -> DecisionReplayManifest:
        ref = self._refs_by_manifest.get(manifest_sha256)
        if ref is None:
            stream_id = finalization_event_stream_id(manifest_sha256)
            page = self._ledger.read_stream(stream_id, 1, 2)
            if not page.events:
                raise KeyError(manifest_sha256)
            if len(page.events) != 1 or page.has_more:
                _invalid("finalization event stream is not singular")
            ref = self._cache_event(page.events[0])
        manifest = _manifest_from_ref(ref)
        if not hmac.compare_digest(
            manifest.manifest_sha256,
            manifest_sha256,
        ):
            _invalid("finalization event differs from requested manifest")
        return manifest

    def load_manifest_for_session(
        self,
        session_id: str,
        decision_id: str,
        usage_decision_id: str,
        injection_artifact_id: str,
    ) -> DecisionReplayManifest:
        matches = tuple(
            ref
            for ref in self._all_finalization_refs()
            if (
                ref.usage_decision.session_id == session_id
                and ref.usage_decision.decision_id == decision_id
                and ref.usage_decision.usage_decision_id == usage_decision_id
                and ref.injection.artifact.artifact_id
                == injection_artifact_id
            )
        )
        if not matches:
            raise KeyError(session_id)
        if len(matches) != 1:
            _invalid("session finalization event linkage is ambiguous")
        return _manifest_from_ref(matches[0])

    def load_injection(
        self,
        artifact_id: str,
    ) -> tuple[InjectionArtifact, bytes]:
        ref = self._refs_by_injection.get(artifact_id)
        if ref is None:
            matches = tuple(
                candidate
                for candidate in self._all_finalization_refs()
                if candidate.injection.artifact.artifact_id == artifact_id
            )
            if not matches:
                raise KeyError(artifact_id)
            if len(matches) != 1:
                _invalid("injection finalization event linkage is ambiguous")
            ref = matches[0]
        stored = self.load_artifact(artifact_id)
        if stored.artifact != ref.injection.artifact:
            _invalid("injection Artifact differs from finalization event")
        return ref.injection, stored.content

    def load_artifact_descriptor(
        self,
        artifact_id: str,
    ) -> ContentAddressedArtifact:
        event_ref = self._artifact_refs.get(artifact_id)
        if event_ref is None:
            self._all_finalization_refs()
            event_ref = self._artifact_refs.get(artifact_id)
        if event_ref is None:
            raise KeyError(artifact_id)
        descriptor = self._artifact_reader.load_artifact_descriptor(
            artifact_id
        )
        if type(descriptor) is not ContentAddressedArtifact:
            _invalid("artifact reader returned an invalid descriptor")
        if not _descriptor_matches_event_ref(descriptor, event_ref):
            _invalid("Artifact descriptor differs from finalization event")
        return descriptor

    def load_artifact(self, artifact_id: str) -> StoredReplayArtifact:
        descriptor = self.load_artifact_descriptor(artifact_id)
        stored = self._artifact_reader.load_artifact(artifact_id)
        if (
            type(stored) is not StoredReplayArtifact
            or stored.artifact != descriptor
            or not verify_artifact_content(stored.artifact, stored.content)
        ):
            _invalid("Artifact bytes differ from finalization evidence")
        return stored

    def _all_finalization_refs(self) -> tuple[FinalizationEventRef, ...]:
        scanned = 0
        after_position = 0
        while True:
            page = self._ledger.read_global(
                after_position,
                EVENT_LEDGER_MAX_READ_PAGE,
            )
            scanned += len(page.events)
            if scanned > self._max_scan_events:
                raise LedgerReplayExportV1Error(
                    "TBM_LEDGER_REPLAY_EXPORT_SCAN_LIMIT",
                    "ledger replay export scan exceeds its configured bound",
                )
            for event in page.events:
                if event.event_type == INJECTION_RENDERED_EVENT:
                    self._cache_event(event)
            if not page.has_more:
                break
            next_position = page.next_global_position
            if (
                type(next_position) is not int
                or next_position <= after_position
            ):
                _invalid("ledger global replay cursor did not advance")
            after_position = next_position
        return tuple(
            self._refs_by_manifest[key]
            for key in sorted(self._refs_by_manifest)
        )

    def _cache_event(self, event: object) -> FinalizationEventRef:
        if type(event) is not CanonicalEvent:
            _invalid("ledger returned an invalid event")
        try:
            ref = parse_injection_rendered_event(event)
        except (FinalizationEventV1Error, TypeError) as error:
            raise LedgerReplayExportV1Error(
                "TBM_LEDGER_REPLAY_EXPORT_EVENT_INVALID",
                "finalization event cannot reconstruct replay metadata",
            ) from error
        retained = self._refs_by_manifest.get(ref.replay_manifest_sha256)
        if retained is not None and retained != ref:
            _invalid("manifest has conflicting finalization events")
        self._refs_by_manifest[ref.replay_manifest_sha256] = ref
        injection_id = ref.injection.artifact.artifact_id
        retained = self._refs_by_injection.get(injection_id)
        if retained is not None and retained != ref:
            _invalid("injection has conflicting finalization events")
        self._refs_by_injection[injection_id] = ref
        for artifact_ref in ref.artifact_refs:
            retained_artifact = self._artifact_refs.get(
                artifact_ref.artifact_id
            )
            if (
                retained_artifact is not None
                and retained_artifact != artifact_ref
            ):
                _invalid("Artifact has conflicting finalization references")
            self._artifact_refs[artifact_ref.artifact_id] = artifact_ref
        return ref


class ContextualLedgerReplayExportReaderV1:
    """Bind one ledger-backed reader to a trusted adapter context."""

    def __init__(
        self,
        ledger_factory: Callable[[LedgerAccessContext], EventLedgerPort],
        artifact_reader: LedgerReplayArtifactReader,
        *,
        max_scan_events: int = LEDGER_REPLAY_EXPORT_MAX_SCAN_EVENTS,
    ) -> None:
        if not callable(ledger_factory):
            raise TypeError("ledger_factory must be callable")
        self._ledger_factory = ledger_factory
        self._artifact_reader = artifact_reader
        self._max_scan_events = max_scan_events
        self._current: ContextVar[LedgerReplayExportReaderV1 | None] = (
            ContextVar(
                f"tbm_contextual_ledger_replay_export_{id(self)}",
                default=None,
            )
        )

    @contextmanager
    def bind_event_context(
        self,
        trusted_context: EventTrustedContext,
    ) -> Iterator[None]:
        if type(trusted_context) is not EventTrustedContext:
            raise ValueError("trusted_context must be exactly EventTrustedContext")
        access = _ledger_access(trusted_context)
        ledger = self._ledger_factory(access)
        reader = LedgerReplayExportReaderV1(
            ledger,
            self._artifact_reader,
            max_scan_events=self._max_scan_events,
        )
        token = self._current.set(reader)
        try:
            yield
        finally:
            self._current.reset(token)
            close = getattr(ledger, "close", None)
            if callable(close):
                close()

    def load_manifest(
        self,
        manifest_sha256: str,
    ) -> DecisionReplayManifest:
        return self._reader().load_manifest(manifest_sha256)

    def load_manifest_for_session(
        self,
        session_id: str,
        decision_id: str,
        usage_decision_id: str,
        injection_artifact_id: str,
    ) -> DecisionReplayManifest:
        return self._reader().load_manifest_for_session(
            session_id,
            decision_id,
            usage_decision_id,
            injection_artifact_id,
        )

    def load_injection(
        self,
        artifact_id: str,
    ) -> tuple[InjectionArtifact, bytes]:
        return self._reader().load_injection(artifact_id)

    def load_artifact_descriptor(
        self,
        artifact_id: str,
    ) -> ContentAddressedArtifact:
        return self._reader().load_artifact_descriptor(artifact_id)

    def load_artifact(self, artifact_id: str) -> StoredReplayArtifact:
        return self._reader().load_artifact(artifact_id)

    def _reader(self) -> LedgerReplayExportReaderV1:
        reader = self._current.get()
        if reader is None:
            raise LedgerReplayExportV1Error(
                "TBM_LEDGER_REPLAY_EXPORT_CONTEXT_REQUIRED",
                "ledger replay export requires trusted adapter context",
            )
        return reader


def verify_ledger_replay_export_parity(
    ledger_reader: ReplayExportReader,
    projection_reader: ReplayExportReader,
    manifest_sha256: str,
    *,
    allowed_classifications: frozenset[DataClassification],
    max_content_bytes: int = REPLAY_EXPORT_MAX_CONTENT_BYTES,
) -> ReplayBundleExport:
    ledger_export = export_replay_bundle(
        ledger_reader,
        manifest_sha256,
        allowed_classifications=allowed_classifications,
        max_content_bytes=max_content_bytes,
    )
    projection_export = export_replay_bundle(
        projection_reader,
        manifest_sha256,
        allowed_classifications=allowed_classifications,
        max_content_bytes=max_content_bytes,
    )
    if (
        ledger_export != projection_export
        or not hmac.compare_digest(
            ledger_export.export_sha256,
            projection_export.export_sha256,
        )
    ):
        raise LedgerReplayExportV1Error(
            "TBM_LEDGER_REPLAY_EXPORT_PARITY_MISMATCH",
            "ledger and replay projection exports differ",
        )
    return ledger_export


def _manifest_from_ref(ref: FinalizationEventRef) -> DecisionReplayManifest:
    manifest = build_decision_replay_manifest(
        session_id=ref.usage_decision.session_id,
        decision_id=ref.usage_decision.decision_id,
        usage_decision_id=ref.usage_decision.usage_decision_id,
        component_hashes=dict(ref.usage_decision.replay_components),
        injection_artifact_id=ref.usage_decision.injection_artifact_id,
        completeness="complete",
        created_at=ref.usage_decision.created_at,
    )
    if not hmac.compare_digest(
        manifest.manifest_sha256,
        ref.replay_manifest_sha256,
    ):
        _invalid("reconstructed manifest differs from finalization event")
    return manifest


def _descriptor_matches_event_ref(
    descriptor: ContentAddressedArtifact,
    event_ref: EventArtifactRef,
) -> bool:
    return (
        descriptor.artifact_id == event_ref.artifact_id
        and descriptor.content_sha256 == event_ref.content_sha256
        and descriptor.media_type == event_ref.media_type
        and descriptor.size_bytes == event_ref.size_bytes
        and descriptor.classification == event_ref.classification
        and descriptor.encryption_key_id == event_ref.encryption_key_id
        and event_ref.availability == "available"
    )


def _ledger_access(
    trusted_context: EventTrustedContext,
) -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            trusted_context.organization_id,
            trusted_context.tenant_id,
            trusted_context.repository_id,
            trusted_context.environment_id,
        ),
        principal_id=trusted_context.principal_id,
        agent_client_id=trusted_context.agent_client_id,
        actor_type=trusted_context.actor_type,
        actor_id=trusted_context.actor_id,
        authorization_decision_id=(
            trusted_context.authorization_decision_id
        ),
        classification_filter=LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _invalid(message: str) -> NoReturn:
    raise LedgerReplayExportV1Error(
        "TBM_LEDGER_REPLAY_EXPORT_INVALID",
        message,
    )


__all__ = [
    "LEDGER_REPLAY_EXPORT_CONTRACT_VERSION",
    "LEDGER_REPLAY_EXPORT_MAX_SCAN_EVENTS",
    "ContextualLedgerReplayExportReaderV1",
    "LedgerReplayArtifactReader",
    "LedgerReplayExportReaderV1",
    "LedgerReplayExportV1Error",
    "verify_ledger_replay_export_parity",
]
