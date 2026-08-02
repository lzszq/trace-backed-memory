from __future__ import annotations

from dataclasses import replace

import pytest

import trace_backed_memory as tbm
from tests.test_durable_finalization_v3 import (
    _event_access,
    _event_context,
    _request,
    _stack,
)


class _MismatchedDescriptorReader:
    def __init__(self, authority: tbm.SQLiteReplayV3Repository) -> None:
        self._authority = authority

    def load_artifact_descriptor(
        self,
        artifact_id: str,
    ) -> tbm.ContentAddressedArtifact:
        descriptor = self._authority.load_artifact_descriptor(artifact_id)
        return replace(descriptor, classification="public")

    def load_artifact(self, artifact_id: str) -> tbm.StoredReplayArtifact:
        return self._authority.load_artifact(artifact_id)


def test_ledger_replay_export_reconstructs_exact_projection_export() -> None:
    stack = _stack(event_first=True)
    trusted = _event_context(stack)
    try:
        with stack.sessions.bind_event_context(
            trusted
        ), stack.replay.bind_event_context(trusted):
            finalized = stack.finalizer().finalize(
                stack.context,
                stack.scope,
                _request(stack),
            )

        ledger = tbm.SQLiteEventLedgerV1(
            stack.connection,
            _event_access(stack),
        )
        try:
            reader = tbm.LedgerReplayExportReaderV1(
                ledger,
                stack.replay,
            )
            manifest = reader.load_manifest_for_session(
                finalized.session.session_id,
                finalized.usage_decision.decision_id,
                finalized.usage_decision.usage_decision_id,
                finalized.injection.artifact.artifact_id,
            )
            assert manifest == finalized.manifest
            assert reader.load_manifest(manifest.manifest_sha256) == manifest
            assert reader.load_injection(
                finalized.injection.artifact.artifact_id
            ) == (finalized.injection, finalized.snippet.encode())

            exported = tbm.verify_ledger_replay_export_parity(
                reader,
                stack.replay,
                manifest.manifest_sha256,
                allowed_classifications=frozenset({"internal"}),
            )
            assert exported.export_sha256 == tbm.export_replay_bundle(
                stack.replay,
                manifest.manifest_sha256,
                allowed_classifications=frozenset({"internal"}),
            ).export_sha256
        finally:
            ledger.close()
    finally:
        stack.close()


def test_ledger_replay_export_rejects_projection_descriptor_drift() -> None:
    stack = _stack(event_first=True)
    trusted = _event_context(stack)
    try:
        with stack.sessions.bind_event_context(
            trusted
        ), stack.replay.bind_event_context(trusted):
            finalized = stack.finalizer().finalize(
                stack.context,
                stack.scope,
                _request(stack),
            )
        ledger = tbm.SQLiteEventLedgerV1(
            stack.connection,
            _event_access(stack),
        )
        try:
            reader = tbm.LedgerReplayExportReaderV1(
                ledger,
                _MismatchedDescriptorReader(stack.replay),
            )
            reader.load_manifest(finalized.manifest.manifest_sha256)
            with pytest.raises(tbm.LedgerReplayExportV1Error) as captured:
                reader.load_artifact_descriptor(
                    finalized.injection.artifact.artifact_id
                )
            assert captured.value.code == "TBM_LEDGER_REPLAY_EXPORT_INVALID"
        finally:
            ledger.close()
    finally:
        stack.close()


def test_ledger_replay_export_session_scan_is_bounded() -> None:
    stack = _stack(event_first=True)
    trusted = _event_context(stack)
    try:
        with stack.sessions.bind_event_context(
            trusted
        ), stack.replay.bind_event_context(trusted):
            finalized = stack.finalizer().finalize(
                stack.context,
                stack.scope,
                _request(stack),
            )
        ledger = tbm.SQLiteEventLedgerV1(
            stack.connection,
            _event_access(stack),
        )
        try:
            reader = tbm.LedgerReplayExportReaderV1(
                ledger,
                stack.replay,
                max_scan_events=1,
            )
            assert reader.load_manifest(
                finalized.manifest.manifest_sha256
            ) == finalized.manifest
            with pytest.raises(tbm.LedgerReplayExportV1Error) as captured:
                reader.load_manifest_for_session(
                    finalized.session.session_id,
                    finalized.usage_decision.decision_id,
                    finalized.usage_decision.usage_decision_id,
                    finalized.injection.artifact.artifact_id,
                )
            assert captured.value.code == (
                "TBM_LEDGER_REPLAY_EXPORT_SCAN_LIMIT"
            )
        finally:
            ledger.close()
    finally:
        stack.close()
