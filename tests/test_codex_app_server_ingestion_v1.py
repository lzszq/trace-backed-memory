from __future__ import annotations

from dataclasses import replace
import hashlib
import json
from pathlib import Path
from typing import cast

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.codex_app_server_ingestion_v1 import (
    CODEX_APP_SERVER_MAX_FRAME_BYTES,
    CODEX_APP_SERVER_MAX_FRAME_DEPTH,
    CODEX_APP_SERVER_MAX_FRAME_NODES,
    CODEX_APP_SERVER_SOURCE_SYSTEM,
    CodexAppServerIngestionV1Error,
    CodexAppServerTraceRecorder,
)
from trace_backed_memory.event_v1 import (
    CanonicalEvent,
    EventArtifactRef,
    EventTrustedContext,
)
from trace_backed_memory.ledger_port_v1 import (
    EventLedgerAtomicAppendPort,
    LedgerAccessContext,
    LedgerAppendCommit,
    LedgerClassificationFilter,
    LedgerIdempotency,
    LedgerTenantPartition,
)
from trace_backed_memory.postgres_event_ledger_v1 import PostgresEventLedgerV1
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1
from trace_backed_memory.trace_event_v1 import parse_trace_event
from tests.postgres_support import PostgresCluster


ROOT = Path(__file__).resolve().parents[1]
POSTGRES_INSTALL = ROOT / "schemas" / "postgres-v3-event-ledger.sql"
HOOK_EVENT_SEGMENTS = {
    "preToolUse": "pre_tool_use",
    "permissionRequest": "permission_request",
    "postToolUse": "post_tool_use",
    "preCompact": "pre_compact",
    "postCompact": "post_compact",
    "sessionStart": "session_start",
    "sessionEnd": "session_end",
    "userPromptSubmit": "user_prompt_submit",
    "subagentStart": "subagent_start",
    "subagentStop": "subagent_stop",
    "stop": "stop",
}
TOOL_HOOK_EVENTS = {"preToolUse", "permissionRequest", "postToolUse"}


class _PostCommitResponseLossLedger:
    def __init__(self, delegate: SQLiteEventLedgerV1) -> None:
        self.delegate = delegate
        self.failed = False

    @property
    def access_context(self) -> LedgerAccessContext:
        return self.delegate.access_context

    def append_once(
        self,
        stream_id: str,
        expected_version: int,
        events: tuple[CanonicalEvent, ...],
        idempotency: LedgerIdempotency,
    ) -> LedgerAppendCommit:
        commit = self.delegate.append_once(
            stream_id,
            expected_version,
            events,
            idempotency,
        )
        if not self.failed:
            self.failed = True
            raise RuntimeError("simulated response loss after commit")
        return commit


def _access(
    *,
    tenant_id: str = "tenant_001",
    classifications: tuple[str, ...] = (
        "public",
        "internal",
        "confidential",
        "restricted",
    ),
) -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id=tenant_id,
            repository_id="repository_001",
            environment_id="environment_local",
        ),
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="agent_client",
        actor_id="agent_client_001",
        authorization_decision_id="authorization_decision_001",
        classification_filter=LedgerClassificationFilter(
            classifications  # type: ignore[arg-type]
        ),
    )


def _recorder(
    ledger: EventLedgerAtomicAppendPort,
    **changes: object,
) -> CodexAppServerTraceRecorder:
    arguments: dict[str, object] = {
        "trace_id": "trace_codex_001",
        "run_id": "run_codex_001",
        "thread_id": "thread_codex_001",
        "trusted_context": _access().event_trusted_context(),
        "clock": lambda: "2026-08-03T02:00:00Z",
        "classification": "internal",
        "retention_policy_id": "retention_engineering_memory",
        "first_sequence": 1,
        "first_global_position": 1,
        "parent_event": None,
    }
    arguments.update(changes)
    return CodexAppServerTraceRecorder(ledger, **arguments)  # type: ignore[arg-type]


def _notification(method: str, params: object) -> bytes:
    return json.dumps(
        {
            "method": method,
            "params": params,
            "emittedAtMs": 1_775_190_400_000,
        },
        ensure_ascii=False,
        separators=(",", ":"),
    ).encode("utf-8")


def _hook_frame(
    event_name: str,
    method: str,
    *,
    thread_id: str = "thread_codex_001",
) -> bytes:
    status = "running" if method == "hook/started" else "completed"
    run: dict[str, object] = {
        "displayOrder": 1,
        "entries": [
            {
                "kind": "context",
                "text": "secret-token-value\nmultiline\tcontext",
            }
        ],
        "eventName": event_name,
        "executionMode": "sync",
        "handlerType": "command",
        "id": f"hook_{event_name}_001",
        "scope": "turn",
        "sourcePath": "/workspace/private/.codex/hooks.json",
        "startedAt": 1_775_190_400_000,
        "status": status,
        "source": "project",
        "statusMessage": "status detail\nwith tab\tvalue",
    }
    if method == "hook/completed":
        run["completedAt"] = 1_775_190_400_100
        run["durationMs"] = 100
    return _notification(
        method,
        {
            "run": run,
            "threadId": thread_id,
            "turnId": "turn_codex_001",
        },
    )


def _diff_frame(diff: str = "diff --git a/secret b/secret\n+token=value\n") -> bytes:
    return _notification(
        "turn/diff/updated",
        {
            "diff": diff,
            "threadId": "thread_codex_001",
            "turnId": "turn_codex_001",
        },
    )


def _item_frame(item: dict[str, object]) -> bytes:
    return _notification(
        "item/completed",
        {
            "completedAtMs": 1_775_190_400_200,
            "item": item,
            "threadId": "thread_codex_001",
            "turnId": "turn_codex_001",
        },
    )


def _artifact(
    frame: bytes,
    *,
    classification: str = "internal",
    retention_policy_id: str = "retention_engineering_memory",
) -> EventArtifactRef:
    digest = hashlib.sha256(frame).hexdigest()
    return EventArtifactRef(
        artifact_id="artifact_sha256_" + digest,
        content_sha256="sha256:" + digest,
        media_type="application/json",
        size_bytes=len(frame),
        classification=classification,  # type: ignore[arg-type]
        retention_policy_id=retention_policy_id,
        encryption_key_id=(
            None
            if classification in {"public", "internal"}
            else "encryption_key_codex_001"
        ),
        availability="available",
    )


@pytest.mark.parametrize("event_name", tuple(HOOK_EVENT_SEGMENTS))
@pytest.mark.parametrize(
    ("method", "phase"),
    (("hook/started", "started"), ("hook/completed", "completed")),
)
def test_all_pinned_hook_events_append_artifact_only_trace_evidence(
    event_name: str,
    method: str,
    phase: str,
) -> None:
    frame = _hook_frame(event_name, method)
    artifact = _artifact(frame)
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        result = _recorder(ledger).ingest_notification(
            frame,
            artifact_refs=(artifact,),
        )

        assert result.status == "appended"
        assert result.method == method
        assert result.commit is not None
        assert result.commit.inserted is True
        assert result.artifact_ids == (artifact.artifact_id,)
        assert result.event is not None
        reference = parse_trace_event(result.event)
        assert reference.trace_event_type == (
            f"codex.hook.{HOOK_EVENT_SEGMENTS[event_name]}.{phase}"
        )
        assert reference.permission_result == (
            "pending"
            if event_name == "permissionRequest" and phase == "started"
            else "unknown"
            if event_name == "permissionRequest"
            else "not_applicable"
        )
        assert (reference.tool_correlation_id is not None) == (
            event_name in TOOL_HOOK_EVENTS
        )
        assert reference.source.source_system == CODEX_APP_SERVER_SOURCE_SYSTEM
        assert reference.source.source_record_id == (
            "codex_frame_" + hashlib.sha256(frame).hexdigest()
        )
        assert reference.artifact_refs == (artifact,)
        retained = ledger.read_global(limit=10).events
        assert retained == (result.event,)
        serialized = json.dumps(result.event.to_dict(), sort_keys=True)
        assert "secret-token-value" not in serialized
        assert "/workspace/private" not in serialized
        assert "status detail" not in serialized


def test_diff_and_final_response_append_without_raw_content_in_event_payload() -> None:
    diff = "diff --git a/private b/private\n+api_key=secret\tvalue\n"
    final_text = "final secret response\nwith multiline\tcontent"
    diff_frame = _diff_frame(diff)
    final_frame = _item_frame(
        {
            "id": "message_final_001",
            "text": final_text,
            "type": "agentMessage",
            "phase": "final_answer",
            "memoryCitation": None,
        }
    )
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        recorder = _recorder(ledger)
        diff_result = recorder.ingest_notification(
            diff_frame,
            artifact_refs=(_artifact(diff_frame),),
        )
        final_result = recorder.ingest_notification(
            final_frame,
            artifact_refs=(_artifact(final_frame),),
        )

        assert diff_result.event is not None
        assert final_result.event is not None
        assert parse_trace_event(diff_result.event).trace_event_type == (
            "codex.turn.diff.updated"
        )
        assert parse_trace_event(final_result.event).trace_event_type == (
            "codex.final_response"
        )
        assert recorder.next_sequence == 3
        assert recorder.next_global_position == 3
        retained = ledger.read_global(limit=10).events
        assert retained == (diff_result.event, final_result.event)
        serialized = json.dumps(
            [event.to_dict() for event in retained],
            sort_keys=True,
        )
        assert diff not in serialized
        assert final_text not in serialized
        assert "api_key" not in serialized


@pytest.mark.parametrize(
    ("frame", "reason"),
    (
        (
            _notification(
                "item/agentMessage/delta",
                {
                    "delta": "unstable transcript fragment",
                    "itemId": "message_stream_001",
                    "threadId": "thread_codex_001",
                    "turnId": "turn_codex_001",
                },
            ),
            "known_unmapped",
        ),
        (
            _item_frame(
                {
                    "id": "message_commentary_001",
                    "text": "commentary",
                    "type": "agentMessage",
                    "phase": "commentary",
                }
            ),
            "not_final_response",
        ),
        (
            _item_frame(
                {
                    "command": "print secret",
                    "commandActions": [],
                    "cwd": "/workspace",
                    "id": "command_001",
                    "status": "completed",
                    "type": "commandExecution",
                }
            ),
            "not_final_response",
        ),
    ),
)
def test_non_authoritative_notifications_are_skipped_without_artifacts(
    frame: bytes,
    reason: str,
) -> None:
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        result = _recorder(ledger).ingest_notification(frame)

        assert result.status == "skipped"
        assert result.skipped_reason == reason
        assert result.event is None
        assert result.commit is None
        assert result.artifact_ids == ()
        assert ledger.read_global(limit=10).events == ()


@pytest.mark.parametrize(
    ("method", "params"),
    (
        *(
            (
                method,
                {
                    "delta": "fragment\nwith tab\t",
                    "itemId": "item_stream_001",
                    "threadId": "thread_codex_001",
                    "turnId": "turn_codex_001",
                },
            )
            for method in (
                "item/agentMessage/delta",
                "item/plan/delta",
                "item/commandExecution/outputDelta",
                "item/fileChange/outputDelta",
            )
        ),
        (
            "item/fileChange/patchUpdated",
            {
                "changes": [
                    {
                        "diff": "@@ -1 +1 @@\n-old\n+new\n",
                        "kind": {"type": "update", "move_path": "moved.py"},
                        "path": "source.py",
                    }
                ],
                "itemId": "item_patch_001",
                "threadId": "thread_codex_001",
                "turnId": "turn_codex_001",
            },
        ),
        (
            "item/reasoning/summaryTextDelta",
            {
                "delta": "summary",
                "itemId": "item_reasoning_001",
                "summaryIndex": 0,
                "threadId": "thread_codex_001",
                "turnId": "turn_codex_001",
            },
        ),
        (
            "item/reasoning/summaryPartAdded",
            {
                "itemId": "item_reasoning_001",
                "summaryIndex": 1,
                "threadId": "thread_codex_001",
                "turnId": "turn_codex_001",
            },
        ),
        (
            "item/reasoning/textDelta",
            {
                "contentIndex": 0,
                "delta": "reasoning",
                "itemId": "item_reasoning_001",
                "threadId": "thread_codex_001",
                "turnId": "turn_codex_001",
            },
        ),
        (
            "thread/compacted",
            {
                "threadId": "thread_codex_001",
                "turnId": "turn_codex_001",
            },
        ),
    ),
)
def test_all_pinned_unstable_notifications_are_validated_then_skipped(
    method: str,
    params: dict[str, object],
) -> None:
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        result = _recorder(ledger).ingest_notification(_notification(method, params))

        assert result.status == "skipped"
        assert result.skipped_reason == "known_unmapped"
        assert ledger.read_global(limit=10).events == ()


def test_skipped_notification_rejects_artifact_claims() -> None:
    frame = _notification(
        "item/agentMessage/delta",
        {
            "delta": "fragment",
            "itemId": "message_stream_001",
            "threadId": "thread_codex_001",
            "turnId": "turn_codex_001",
        },
    )
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        with pytest.raises(CodexAppServerIngestionV1Error) as captured:
            _recorder(ledger).ingest_notification(
                frame,
                artifact_refs=(_artifact(frame),),
            )

    assert captured.value.code == "TBM_CODEX_APP_SERVER_ARTIFACT_INVALID"


def test_non_agent_completed_item_requires_common_identity_fields() -> None:
    frame = _item_frame({"type": "commandExecution"})
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        with pytest.raises(CodexAppServerIngestionV1Error) as captured:
            _recorder(ledger).ingest_notification(frame)

    assert captured.value.code == "TBM_CODEX_APP_SERVER_FRAME_INVALID"


def test_unknown_completed_item_type_fails_closed() -> None:
    frame = _item_frame(
        {"id": "future_item_001", "type": "futureItem", "secret": "value"}
    )
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        with pytest.raises(CodexAppServerIngestionV1Error) as captured:
            _recorder(ledger).ingest_notification(frame)

    assert captured.value.code == "TBM_CODEX_APP_SERVER_ITEM_UNSUPPORTED"


@pytest.mark.parametrize(
    "mutate",
    (
        lambda frame, artifact: _artifact(frame + b" "),
        lambda frame, artifact: replace(artifact, media_type="text/plain"),
        lambda frame, artifact: replace(artifact, size_bytes=len(frame) + 1),
        lambda frame, artifact: replace(
            artifact,
            classification="confidential",
            encryption_key_id="encryption_key_codex_001",
        ),
        lambda frame, artifact: replace(
            artifact,
            retention_policy_id="retention_other",
        ),
        lambda frame, artifact: replace(artifact, availability="unavailable"),
    ),
)
def test_frame_artifact_descriptor_must_match_exact_bytes_and_policy(
    mutate: object,
) -> None:
    frame = _diff_frame()
    artifact = _artifact(frame)
    invalid_artifact = mutate(frame, artifact)  # type: ignore[operator]
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        with pytest.raises(CodexAppServerIngestionV1Error) as captured:
            _recorder(ledger).ingest_notification(
                frame,
                artifact_refs=(invalid_artifact,),
            )

    assert captured.value.code == "TBM_CODEX_APP_SERVER_ARTIFACT_INVALID"


@pytest.mark.parametrize("artifact_count", (0, 2))
def test_mapped_notification_requires_exactly_one_artifact(
    artifact_count: int,
) -> None:
    frame = _diff_frame()
    artifacts = tuple(_artifact(frame) for _ in range(artifact_count))
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        with pytest.raises(CodexAppServerIngestionV1Error) as captured:
            _recorder(ledger).ingest_notification(
                frame,
                artifact_refs=artifacts,
            )

    assert captured.value.code == "TBM_CODEX_APP_SERVER_ARTIFACT_INVALID"


@pytest.mark.parametrize(
    ("frame", "expected_code"),
    (
        (
            b'{"method":"turn/diff/updated","method":"hook/started","params":{}}',
            "TBM_CODEX_APP_SERVER_FRAME_INVALID",
        ),
        (
            b'{"method":"unknown","params":{"value":NaN}}',
            "TBM_CODEX_APP_SERVER_FRAME_INVALID",
        ),
        (
            b'{"method":"unknown","params":{"value":Infinity}}',
            "TBM_CODEX_APP_SERVER_FRAME_INVALID",
        ),
        (b"\xff", "TBM_CODEX_APP_SERVER_FRAME_INVALID"),
        (
            b'{"method":"unknown","params":{},"extra":true}',
            "TBM_CODEX_APP_SERVER_FRAME_INVALID",
        ),
        (
            b'{"id":1,"method":"hook/started","params":{}}',
            "TBM_CODEX_APP_SERVER_NOTIFICATION_REQUIRED",
        ),
        (
            b'{"id":1,"result":{}}',
            "TBM_CODEX_APP_SERVER_NOTIFICATION_REQUIRED",
        ),
        (
            b'{"jsonrpc":"2.0","method":"hook/started","params":{}}',
            "TBM_CODEX_APP_SERVER_NOTIFICATION_REQUIRED",
        ),
        (
            b'{"method":"future/method","params":{},"emittedAtMs":null}',
            "TBM_CODEX_APP_SERVER_FRAME_INVALID",
        ),
        (
            b'{"hook_event_name":"SessionStart","session_id":"guess"}',
            "TBM_CODEX_APP_SERVER_FRAME_INVALID",
        ),
        (
            b'{"method":"future/method","params":{}}',
            "TBM_CODEX_APP_SERVER_METHOD_UNSUPPORTED",
        ),
        (
            b'{"method":"thread/realtime/transcript/updated","params":{}}',
            "TBM_CODEX_APP_SERVER_TRANSCRIPT_UNSUPPORTED",
        ),
    ),
)
def test_untrusted_or_unsupported_wire_frames_fail_closed(
    frame: bytes,
    expected_code: str,
) -> None:
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        with pytest.raises(CodexAppServerIngestionV1Error) as captured:
            _recorder(ledger).ingest_notification(frame)

        assert ledger.read_global(limit=10).events == ()
    assert captured.value.code == expected_code


def test_oversized_deep_and_wide_frames_fail_before_mapping() -> None:
    oversized = b" " * (CODEX_APP_SERVER_MAX_FRAME_BYTES + 1)
    deep_value = "0"
    for _ in range(CODEX_APP_SERVER_MAX_FRAME_DEPTH + 1):
        deep_value = "[" + deep_value + "]"
    deep = ('{"method":"future/method","params":{"value":' + deep_value + "}}").encode()
    wide = (
        b'{"method":"future/method","params":{"value":['
        + b"0," * CODEX_APP_SERVER_MAX_FRAME_NODES
        + b"0]}}"
    )
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        recorder = _recorder(ledger)
        for frame in (oversized, deep, wide):
            with pytest.raises(CodexAppServerIngestionV1Error) as captured:
                recorder.ingest_notification(frame)
            assert captured.value.code == "TBM_CODEX_APP_SERVER_FRAME_INVALID"
        assert ledger.read_global(limit=10).events == ()


def test_hook_overlay_rejects_thread_mismatch_unknown_fields_and_bad_status() -> None:
    mismatched = _hook_frame(
        "sessionStart",
        "hook/started",
        thread_id="thread_other",
    )
    extra_payload = json.loads(_hook_frame("sessionStart", "hook/started"))
    extra_payload["params"]["run"]["secret"] = "value"
    extra = json.dumps(extra_payload, separators=(",", ":")).encode()
    bad_status_payload = json.loads(_hook_frame("sessionStart", "hook/started"))
    bad_status_payload["params"]["run"]["status"] = "completed"
    bad_status = json.dumps(bad_status_payload, separators=(",", ":")).encode()
    relative_path_payload = json.loads(_hook_frame("sessionStart", "hook/started"))
    relative_path_payload["params"]["run"]["sourcePath"] = "relative/hooks.json"
    relative_path = json.dumps(
        relative_path_payload,
        separators=(",", ":"),
    ).encode()
    dotted_path_payload = json.loads(_hook_frame("sessionStart", "hook/started"))
    dotted_path_payload["params"]["run"]["sourcePath"] = "/workspace/./hooks.json"
    dotted_path = json.dumps(
        dotted_path_payload,
        separators=(",", ":"),
    ).encode()
    repeated_path_payload = json.loads(_hook_frame("sessionStart", "hook/started"))
    repeated_path_payload["params"]["run"]["sourcePath"] = "/workspace//hooks.json"
    repeated_path = json.dumps(
        repeated_path_payload,
        separators=(",", ":"),
    ).encode()
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        recorder = _recorder(ledger)
        with pytest.raises(CodexAppServerIngestionV1Error) as thread_error:
            recorder.ingest_notification(mismatched)
        assert thread_error.value.code == "TBM_CODEX_APP_SERVER_THREAD_MISMATCH"
        for frame in (
            extra,
            bad_status,
            relative_path,
            dotted_path,
            repeated_path,
        ):
            with pytest.raises(CodexAppServerIngestionV1Error) as captured:
                recorder.ingest_notification(frame)
            assert captured.value.code == "TBM_CODEX_APP_SERVER_FRAME_INVALID"


def test_known_skipped_delta_validates_exact_fields_and_thread_binding() -> None:
    missing_item = _notification(
        "item/agentMessage/delta",
        {
            "delta": "fragment",
            "threadId": "thread_codex_001",
            "turnId": "turn_codex_001",
        },
    )
    other_thread = _notification(
        "item/agentMessage/delta",
        {
            "delta": "fragment",
            "itemId": "message_stream_001",
            "threadId": "thread_other",
            "turnId": "turn_codex_001",
        },
    )
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        recorder = _recorder(ledger)
        with pytest.raises(CodexAppServerIngestionV1Error) as malformed:
            recorder.ingest_notification(missing_item)
        assert malformed.value.code == "TBM_CODEX_APP_SERVER_FRAME_INVALID"
        with pytest.raises(CodexAppServerIngestionV1Error) as mismatch:
            recorder.ingest_notification(other_thread)
        assert mismatch.value.code == "TBM_CODEX_APP_SERVER_THREAD_MISMATCH"


def test_hook_started_and_completed_share_stable_run_correlation() -> None:
    started = _hook_frame("preToolUse", "hook/started")
    completed = _hook_frame("preToolUse", "hook/completed")
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        recorder = _recorder(ledger)
        started_result = recorder.ingest_notification(
            started,
            artifact_refs=(_artifact(started),),
        )
        completed_result = recorder.ingest_notification(
            completed,
            artifact_refs=(_artifact(completed),),
        )

    assert started_result.event is not None
    assert completed_result.event is not None
    started_reference = parse_trace_event(started_result.event)
    completed_reference = parse_trace_event(completed_result.event)
    assert started_reference.tool_correlation_id is not None
    assert (
        completed_reference.tool_correlation_id == started_reference.tool_correlation_id
    )


def test_final_response_rejects_memory_citation_and_unknown_phase() -> None:
    citation = _item_frame(
        {
            "id": "message_001",
            "text": "answer",
            "type": "agentMessage",
            "phase": "final_answer",
            "memoryCitation": {"memoryId": "unsafe"},
        }
    )
    unknown_phase = _item_frame(
        {
            "id": "message_002",
            "text": "answer",
            "type": "agentMessage",
            "phase": "future_phase",
        }
    )
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        recorder = _recorder(ledger)
        for frame in (citation, unknown_phase):
            with pytest.raises(CodexAppServerIngestionV1Error) as captured:
                recorder.ingest_notification(frame)
            assert captured.value.code == "TBM_CODEX_APP_SERVER_FRAME_INVALID"


def test_post_commit_response_loss_blocks_new_input_until_exact_resume() -> None:
    frame = _diff_frame()
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        response_loss = _PostCommitResponseLossLedger(ledger)
        recorder = _recorder(
            cast(EventLedgerAtomicAppendPort, response_loss),
        )

        with pytest.raises(CodexAppServerIngestionV1Error) as uncertain:
            recorder.ingest_notification(
                frame,
                artifact_refs=(_artifact(frame),),
            )

        assert uncertain.value.code == "TBM_CODEX_APP_SERVER_APPEND_UNCERTAIN"
        assert recorder.has_pending is True
        assert recorder.next_sequence == 1
        assert recorder.next_global_position == 1
        assert len(ledger.read_global(limit=10).events) == 1
        with pytest.raises(CodexAppServerIngestionV1Error) as blocked:
            recorder.ingest_notification(_notification("thread/started", {}))
        assert blocked.value.code == ("TBM_CODEX_APP_SERVER_PENDING_RESUME_REQUIRED")

        result = recorder.resume_pending()

        assert result is not None
        assert result.commit is not None
        assert result.commit.replayed is True
        assert recorder.has_pending is False
        assert recorder.next_sequence == 2
        assert recorder.next_global_position == 2
        assert len(ledger.read_global(limit=10).events) == 1
        assert recorder.resume_pending() is None


def test_deterministic_ledger_conflict_clears_pending_for_reconstruction() -> None:
    first_frame = _diff_frame("first")
    conflict_frame = _diff_frame("conflict")
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        first = _recorder(ledger).ingest_notification(
            first_frame,
            artifact_refs=(_artifact(first_frame),),
        )
        assert first.status == "appended"
        stale = _recorder(ledger, first_global_position=2)

        with pytest.raises(CodexAppServerIngestionV1Error) as captured:
            stale.ingest_notification(
                conflict_frame,
                artifact_refs=(_artifact(conflict_frame),),
            )

        assert captured.value.code == "TBM_CODEX_APP_SERVER_APPEND_REJECTED"
        assert stale.has_pending is False
        assert stale.next_sequence == 1
        assert stale.next_global_position == 2
        assert len(ledger.read_global(limit=10).events) == 1


def test_new_recorder_continues_exact_retained_parent() -> None:
    first_frame = _diff_frame()
    second_frame = _hook_frame("sessionEnd", "hook/completed")
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        first = _recorder(ledger).ingest_notification(
            first_frame,
            artifact_refs=(_artifact(first_frame),),
        )
        assert first.event is not None
        continued = _recorder(
            ledger,
            first_sequence=2,
            first_global_position=2,
            parent_event=first.event,
        )
        second = continued.ingest_notification(
            second_frame,
            artifact_refs=(_artifact(second_frame),),
        )

        assert second.event is not None
        assert second.event.stream_version == 2
        assert second.event.previous_stream_event_sha256 == first.event.event_sha256
        assert ledger.read_global(limit=10).events == (first.event, second.event)


def test_configuration_and_clock_failures_use_stable_ingestion_errors() -> None:
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        with pytest.raises(CodexAppServerIngestionV1Error) as identity:
            _recorder(ledger, trace_id="bad trace id")
        assert identity.value.code == "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID"
        with pytest.raises(CodexAppServerIngestionV1Error) as public:
            _recorder(ledger, classification="public")
        assert public.value.code == "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID"
        with pytest.raises(CodexAppServerIngestionV1Error) as wire:
            _recorder(ledger, wire_version="v3")
        assert wire.value.code == "TBM_CODEX_APP_SERVER_WIRE_VERSION_UNSUPPORTED"
        with pytest.raises(CodexAppServerIngestionV1Error) as version:
            _recorder(ledger, codex_cli_version="0.147.0")
        assert version.value.code == "TBM_CODEX_APP_SERVER_CLI_VERSION_UNSUPPORTED"

        def failed_clock() -> str:
            raise RuntimeError("clock secret")

        frame = _diff_frame()
        recorder = _recorder(ledger, clock=failed_clock)
        with pytest.raises(CodexAppServerIngestionV1Error) as clock:
            recorder.ingest_notification(
                frame,
                artifact_refs=(_artifact(frame),),
            )
        assert clock.value.code == "TBM_CODEX_APP_SERVER_CLOCK_FAILED"
        assert "secret" not in str(clock.value)
        invalid_clock = _recorder(ledger, clock=lambda: "not-a-timestamp")
        with pytest.raises(CodexAppServerIngestionV1Error) as invalid_time:
            invalid_clock.ingest_notification(
                frame,
                artifact_refs=(_artifact(frame),),
            )
        assert invalid_time.value.code == "TBM_CODEX_APP_SERVER_TRACE_INVALID"


def test_trusted_context_must_match_ledger_owned_identity() -> None:
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        other_context: EventTrustedContext = _access(
            tenant_id="tenant_other"
        ).event_trusted_context()
        with pytest.raises(CodexAppServerIngestionV1Error) as captured:
            _recorder(ledger, trusted_context=other_context)

    assert captured.value.code == "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID"


def test_recorder_classification_must_be_allowed_before_any_append() -> None:
    public_only = _access(classifications=("public",))
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        public_only,
        initialize=True,
    ) as ledger:
        with pytest.raises(CodexAppServerIngestionV1Error) as captured:
            _recorder(
                ledger,
                trusted_context=public_only.event_trusted_context(),
                classification="internal",
            )

    assert captured.value.code == "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID"


def test_retained_parent_must_match_complete_trusted_context() -> None:
    frame = _diff_frame()
    other_access = _access(tenant_id="tenant_other")
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        other_access,
        initialize=True,
    ) as other_ledger:
        other_parent = (
            _recorder(
                other_ledger,
                trusted_context=other_access.event_trusted_context(),
            )
            .ingest_notification(
                frame,
                artifact_refs=(_artifact(frame),),
            )
            .event
        )
    assert other_parent is not None
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as ledger:
        with pytest.raises(CodexAppServerIngestionV1Error) as captured:
            _recorder(
                ledger,
                first_sequence=2,
                first_global_position=2,
                parent_event=other_parent,
            )

    assert captured.value.code == "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID"


def test_sqlite_and_postgres_emit_identical_codex_trace_event(
    postgres_cluster: PostgresCluster,
) -> None:
    postgres_cluster.load_schema()
    installed = postgres_cluster.run_script(POSTGRES_INSTALL)
    assert installed.returncode == 0, installed.stderr
    frame = _hook_frame("userPromptSubmit", "hook/completed")
    artifact = _artifact(frame)
    with SQLiteEventLedgerV1.connect(
        ":memory:",
        _access(),
        initialize=True,
    ) as sqlite_ledger:
        sqlite_result = _recorder(sqlite_ledger).ingest_notification(
            frame,
            artifact_refs=(artifact,),
        )
    with PostgresEventLedgerV1.connect(
        _access(),
        **postgres_cluster.connection_kwargs(),
    ) as postgres_ledger:
        postgres_result = _recorder(postgres_ledger).ingest_notification(
            frame,
            artifact_refs=(artifact,),
        )

    assert postgres_result == sqlite_result


def test_package_root_exports_codex_ingestion_contract() -> None:
    assert tbm.CODEX_APP_SERVER_INGESTION_CONTRACT_VERSION == (
        "tbm.codex-app-server-ingestion.v1"
    )
    assert tbm.CodexAppServerTraceRecorder is CodexAppServerTraceRecorder
