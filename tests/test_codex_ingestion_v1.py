from __future__ import annotations

from dataclasses import replace
from datetime import datetime, timezone
import hashlib
import json
import sqlite3

import pytest

import trace_backed_memory as tbm
from trace_backed_memory.codex_ingestion_v1 import (
    CODEX_DIFF_UPDATE,
    CODEX_FINAL_RESPONSE,
    CODEX_INGESTION_EVENT_TYPES,
    CODEX_PERMISSION_REQUEST,
    CODEX_POST_TOOL_USE,
    CODEX_PRE_COMPACT,
    CODEX_PRE_TOOL_USE,
    CODEX_SESSION_END,
    CODEX_SESSION_START,
    CODEX_SOURCE_ARTIFACT_MEDIA_TYPE,
    CODEX_SOURCE_ARTIFACT_RETENTION_POLICY_ID,
    CODEX_STOP,
    CODEX_SUBAGENT_START,
    CODEX_SUBAGENT_STOP,
    CODEX_USER_PROMPT_SUBMIT,
    CodexIngestionBinding,
    CodexIngestionV1Error,
    append_codex_ingestion_batch,
    build_codex_ingestion_trace_drafts,
    capture_codex_app_server_notification,
    capture_codex_app_server_permission,
    capture_codex_hook_event,
    codex_ingestion_projection,
)
from trace_backed_memory.event_v1 import EventArtifactRef
from trace_backed_memory.ledger_port_v1 import (
    LedgerAccessContext,
    LedgerClassificationFilter,
    LedgerTenantPartition,
)
from trace_backed_memory.resources import read_packaged_resource
from trace_backed_memory.sqlite_event_ledger_v1 import (
    SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE,
    SQLiteEventLedgerV1,
)
from trace_backed_memory.trace_event_v1 import (
    TRACE_DIFF_OBSERVED,
    TRACE_FINAL_RESPONSE_RECORDED,
    TRACE_PERMISSION_RECORDED,
    TRACE_PRE_COMPACT,
    TRACE_SESSION_ENDED,
    TRACE_SESSION_STARTED,
    TRACE_STOPPED,
    TRACE_SUBAGENT_STARTED,
    TRACE_SUBAGENT_STOPPED,
    TRACE_TOOL_COMPLETED,
    TRACE_TOOL_STARTED,
    TRACE_USER_PROMPT_SUBMITTED,
    TraceEventLineage,
    TracePermissionResult,
    trace_event_stream_id,
)


DIGEST_A = "sha256:" + "a" * 64
DIGEST_B = "sha256:" + "b" * 64


def _access() -> LedgerAccessContext:
    return LedgerAccessContext(
        partition=LedgerTenantPartition(
            organization_id="organization_001",
            tenant_id="tenant_001",
            repository_id="repository_001",
            environment_id="environment_001",
        ),
        principal_id="principal_001",
        agent_client_id="agent_client_001",
        actor_type="service",
        actor_id="service_codex_ingestion",
        authorization_decision_id="authorization_codex_ingestion",
        classification_filter=LedgerClassificationFilter(
            ("public", "internal", "confidential", "restricted")
        ),
    )


def _connection() -> sqlite3.Connection:
    connection = sqlite3.connect(
        ":memory:", isolation_level=None, check_same_thread=False
    )
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA recursive_triggers = ON")
    connection.executescript(
        read_packaged_resource(SQLITE_EVENT_LEDGER_V1_SCHEMA_RESOURCE).decode(
            "utf-8"
        )
    )
    return connection


def _binding(*, hook_session_id: str = "session_001") -> CodexIngestionBinding:
    return CodexIngestionBinding(
        trace_id="trace_codex_001",
        run_id="run_codex_001",
        lineage=TraceEventLineage(
            role="root",
            subagent_id=None,
            parent_trace_id=None,
            parent_event_id=None,
        ),
        hook_session_id=hook_session_id,
        app_server_thread_id="session_001",
    )


def _artifact_writer(raw: bytes) -> EventArtifactRef:
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    return EventArtifactRef(
        artifact_id="artifact_sha256_" + digest.removeprefix("sha256:"),
        content_sha256=digest,
        media_type=CODEX_SOURCE_ARTIFACT_MEDIA_TYPE,
        size_bytes=len(raw),
        classification="confidential",
        retention_policy_id=CODEX_SOURCE_ARTIFACT_RETENTION_POLICY_ID,
        encryption_key_id="codex_source_key_001",
        availability="available",
    )


def _permission(
    data: str | bytes | None = None,
    *,
    decided_at: str = "2026-08-02T00:00:04Z",
) -> TracePermissionResult:
    if data is None:
        request_sha256 = DIGEST_A
    else:
        raw = data.encode("utf-8") if type(data) is str else data
        request_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
    return TracePermissionResult(
        decision_id="permission_decision_001",
        permission="tool:execute",
        status="allowed",
        reason_code="allowed",
        decided_at=decided_at,
        request_sha256=request_sha256,
        policy_sha256=DIGEST_B,
    )


def _hook(
    event: str,
    second: int,
    **extra: object,
):
    frame: dict[str, object] = {
        "hook_event_name": event,
        "session_id": "session_001",
        "turn_id": "turn_001",
        "cwd": "C:/repo",
        "model": "gpt-5.6",
        "permission_mode": "never",
        "transcript_path": "C:/protected/codex-rollout.jsonl",
    }
    frame.update(extra)
    raw = json.dumps(frame, separators=(",", ":"))
    return capture_codex_hook_event(
        raw,
        received_at=f"2026-08-02T00:00:{second:02d}Z",
        artifact_writer=_artifact_writer,
        permission_result=(
            _permission(raw) if event == CODEX_PERMISSION_REQUEST else None
        ),
    )


def _notification(
    method: str,
    params: dict[str, object],
    second: int,
):
    emitted_at_ms = int(
        datetime(
            2026, 8, 2, 0, 0, second, tzinfo=timezone.utc
        ).timestamp()
        * 1000
    )
    if method == "item/started":
        params.setdefault("startedAtMs", emitted_at_ms)
    elif method == "item/completed":
        params.setdefault("completedAtMs", emitted_at_ms)
    return capture_codex_app_server_notification(
        json.dumps(
            {
                "emittedAtMs": emitted_at_ms,
                "method": method,
                "params": params,
            },
            separators=(",", ":"),
        ),
        received_at=f"2026-08-02T00:00:{second:02d}Z",
        artifact_writer=_artifact_writer,
    )


def _hybrid_records():
    tool = {
        "tool_use_id": "tool_call_001",
        "tool_name": "shell_command",
        "tool_input": {"command": "python -m pytest focused"},
    }
    records = (
        _hook(CODEX_SESSION_START, 1, source="startup"),
        _hook(CODEX_USER_PROMPT_SUBMIT, 2, prompt="sensitive prompt"),
        _hook(CODEX_PRE_TOOL_USE, 3, **tool),
        _hook(
            CODEX_PERMISSION_REQUEST,
            4,
            tool_name=tool["tool_name"],
            tool_input=tool["tool_input"],
        ),
        _hook(
            CODEX_POST_TOOL_USE,
            5,
            **tool,
            tool_response={"output": "sensitive output"},
        ),
        _hook(CODEX_SUBAGENT_START, 6, agent_id="subagent_001"),
        _hook(CODEX_SUBAGENT_STOP, 7, agent_id="subagent_001"),
        _hook(CODEX_PRE_COMPACT, 8),
        _notification(
            "turn/diff/updated",
            {
                "threadId": "session_001",
                "turnId": "turn_001",
                "diff": "diff --git a/secret b/secret",
            },
            9,
        ),
        _notification(
            "item/completed",
            {
                "threadId": "session_001",
                "turnId": "turn_001",
                "item": {
                    "id": "message_001",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "sensitive final response",
                },
            },
            10,
        ),
        _hook(CODEX_STOP, 11),
        _hook(CODEX_SESSION_END, 12),
    )
    assert all(record is not None for record in records)
    return records


def test_hybrid_hook_and_app_server_sources_map_all_twelve_acceptance_points():
    records = _hybrid_records()
    expected_points = (
        CODEX_SESSION_START,
        CODEX_USER_PROMPT_SUBMIT,
        CODEX_PRE_TOOL_USE,
        CODEX_PERMISSION_REQUEST,
        CODEX_POST_TOOL_USE,
        CODEX_SUBAGENT_START,
        CODEX_SUBAGENT_STOP,
        CODEX_PRE_COMPACT,
        CODEX_DIFF_UPDATE,
        CODEX_FINAL_RESPONSE,
        CODEX_STOP,
        CODEX_SESSION_END,
    )
    assert len(records) == len(expected_points) == len(CODEX_INGESTION_EVENT_TYPES)
    assert set(record.source_event for record in records) == set(
        CODEX_INGESTION_EVENT_TYPES
    ) == set(expected_points)

    drafts = build_codex_ingestion_trace_drafts(_binding(), records)
    assert tuple(draft.event_type for draft in drafts) == (
        TRACE_SESSION_STARTED,
        TRACE_USER_PROMPT_SUBMITTED,
        TRACE_TOOL_STARTED,
        TRACE_PERMISSION_RECORDED,
        TRACE_TOOL_COMPLETED,
        TRACE_SUBAGENT_STARTED,
        TRACE_SUBAGENT_STOPPED,
        TRACE_PRE_COMPACT,
        TRACE_DIFF_OBSERVED,
        TRACE_FINAL_RESPONSE_RECORDED,
        TRACE_STOPPED,
        TRACE_SESSION_ENDED,
    )
    assert drafts[2].tool is not None
    assert drafts[3].tool is not None
    assert drafts[4].tool is not None
    assert (
        drafts[2].tool.invocation_sha256
        == drafts[3].tool.invocation_sha256
        == drafts[4].tool.invocation_sha256
    )
    assert drafts[3].permission_result == records[3].permission_result
    assert records[3].tool is not None
    assert records[3].tool.tool_call_id is None
    assert drafts[0].occurred_at == "2026-08-02T00:00:01Z"
    assert drafts[8].occurred_at == "2026-08-02T00:00:09Z"
    assert all(len(draft.artifact_refs) == 1 for draft in drafts)
    metadata = json.dumps([draft.payload() for draft in drafts])
    assert "sensitive prompt" not in metadata
    assert "sensitive output" not in metadata
    assert "diff --git" not in metadata
    assert "sensitive final response" not in metadata


def test_app_server_stable_notifications_and_approval_request_map_all_points():
    command = {
        "id": "tool_call_001",
        "type": "commandExecution",
        "command": "python -m pytest focused",
        "commandActions": [],
        "cwd": "C:/repo",
    }
    pre_tool_record = _notification(
        "item/started",
        {
            "threadId": "session_001",
            "turnId": "turn_001",
            "item": {**command, "status": "inProgress"},
        },
        3,
    )
    assert pre_tool_record is not None and pre_tool_record.tool is not None
    records = (
        _notification(
            "thread/started", {"thread": {"id": "session_001"}}, 1
        ),
        _notification(
            "item/completed",
            {
                "threadId": "session_001",
                "turnId": "turn_001",
                "item": {
                    "id": "user_001",
                    "type": "userMessage",
                    "content": [{"type": "text", "text": "secret"}],
                },
            },
            2,
        ),
        pre_tool_record,
        capture_codex_app_server_permission(
            approval_raw := json.dumps(
                {
                    "id": 7,
                    "method": "item/commandExecution/requestApproval",
                    "params": {
                        "threadId": "session_001",
                        "turnId": "turn_001",
                        "itemId": "tool_call_001",
                        "startedAtMs": int(
                            datetime(
                                2026, 8, 2, 0, 0, 2, tzinfo=timezone.utc
                            ).timestamp()
                            * 1000
                        ),
                        "command": "python -m pytest focused",
                    },
                },
                separators=(",", ":"),
            ),
            received_at="2026-08-02T00:00:04Z",
            artifact_writer=_artifact_writer,
            permission_result=_permission(approval_raw),
        ),
        _notification(
            "item/completed",
            {
                "threadId": "session_001",
                "turnId": "turn_001",
                "item": {
                    **command,
                    "status": "completed",
                    "aggregatedOutput": "secret output",
                    "exitCode": 0,
                },
            },
            5,
        ),
        _notification(
            "item/completed",
            {
                "threadId": "session_001",
                "turnId": "turn_001",
                "item": {
                    "id": "subactivity_001",
                    "type": "subAgentActivity",
                    "agentThreadId": "subagent_001",
                    "agentPath": "subagent_001",
                    "kind": "started",
                },
            },
            6,
        ),
        _notification(
            "item/completed",
            {
                "threadId": "session_001",
                "turnId": "turn_001",
                "item": {
                    "id": "subactivity_001",
                    "type": "subAgentActivity",
                    "agentThreadId": "subagent_001",
                    "agentPath": "subagent_001",
                    "kind": "interrupted",
                },
            },
            7,
        ),
        _notification(
            "item/started",
            {
                "threadId": "session_001",
                "turnId": "turn_001",
                "item": {"id": "compact_001", "type": "contextCompaction"},
            },
            8,
        ),
        _notification(
            "turn/diff/updated",
            {
                "threadId": "session_001",
                "turnId": "turn_001",
                "diff": "diff --git a/a b/a",
            },
            9,
        ),
        _notification(
            "item/completed",
            {
                "threadId": "session_001",
                "turnId": "turn_001",
                "item": {
                    "id": "assistant_001",
                    "type": "agentMessage",
                    "phase": "final_answer",
                    "text": "done",
                },
            },
            10,
        ),
        _notification(
            "turn/completed",
            {
                "threadId": "session_001",
                "turn": {"id": "turn_001", "status": "completed"},
            },
            11,
        ),
        _notification("thread/closed", {"threadId": "session_001"}, 12),
    )
    assert all(record is not None for record in records)
    drafts = build_codex_ingestion_trace_drafts(_binding(), records)
    assert len(drafts) == 12
    assert drafts[2].tool is not None and drafts[4].tool is not None
    assert drafts[2].tool.invocation_sha256 == drafts[4].tool.invocation_sha256
    assert drafts[3].occurred_at == "2026-08-02T00:00:04Z"


def test_current_app_server_envelope_times_and_collab_items_are_supported():
    started = _notification(
        "item/started",
        {
            "threadId": "session_001",
            "turnId": "turn_001",
            "item": {
                "agentsStates": {},
                "id": "collab_001",
                "receiverThreadIds": [],
                "senderThreadId": "session_001",
                "status": "inProgress",
                "tool": "spawnAgent",
                "type": "collabAgentToolCall",
                "prompt": "protected subagent prompt",
            },
        },
        2,
    )
    completed = _notification(
        "item/completed",
        {
            "threadId": "session_001",
            "turnId": "turn_001",
            "item": {
                "agentsStates": {
                    "subagent_001": {"status": "completed"}
                },
                "id": "collab_001",
                "receiverThreadIds": ["subagent_001"],
                "senderThreadId": "session_001",
                "status": "completed",
                "tool": "spawnAgent",
                "type": "collabAgentToolCall",
                "prompt": "protected subagent prompt",
            },
        },
        3,
    )
    start = _hook(CODEX_SESSION_START, 1)
    assert start is not None and started is not None and completed is not None
    drafts = build_codex_ingestion_trace_drafts(
        _binding(), (start, started, completed)
    )
    assert drafts[1].event_type == TRACE_TOOL_STARTED
    assert drafts[2].event_type == TRACE_TOOL_COMPLETED
    assert drafts[1].tool is not None and drafts[2].tool is not None
    assert drafts[1].tool.tool_name == "collab:spawnAgent"
    assert drafts[1].tool.invocation_sha256 == drafts[2].tool.invocation_sha256
    assert drafts[1].occurred_at == "2026-08-02T00:00:02Z"

    interacted = _notification(
        "item/completed",
        {
            "threadId": "session_001",
            "turnId": "turn_001",
            "item": {
                "id": "activity_001",
                "type": "subAgentActivity",
                "agentThreadId": "subagent_001",
                "agentPath": "subagent_001",
                "kind": "interacted",
            },
        },
        4,
    )
    assert interacted is None


def test_current_item_notifications_require_exact_source_lifecycle_time():
    emitted_at_ms = int(
        datetime(2026, 8, 2, tzinfo=timezone.utc).timestamp() * 1000
    )
    with pytest.raises(CodexIngestionV1Error, match="startedAtMs"):
        capture_codex_app_server_notification(
            json.dumps(
                {
                    "emittedAtMs": emitted_at_ms,
                    "method": "item/started",
                    "params": {
                        "threadId": "session_001",
                        "turnId": "turn_001",
                        "item": {
                            "id": "tool_001",
                            "type": "commandExecution",
                            "command": "dir",
                            "commandActions": [],
                            "cwd": "C:/repo",
                            "status": "inProgress",
                        },
                    },
                }
            ),
            received_at="2026-08-02T00:00:01Z",
            artifact_writer=_artifact_writer,
        )


def test_source_times_and_permission_decisions_fail_before_artifact_write():
    written: list[bytes] = []

    def recording_writer(raw: bytes) -> EventArtifactRef:
        written.append(raw)
        return _artifact_writer(raw)

    future_ms = int(
        datetime(2100, 1, 1, tzinfo=timezone.utc).timestamp() * 1000
    )
    with pytest.raises(CodexIngestionV1Error, match="skew bound"):
        capture_codex_app_server_notification(
            json.dumps(
                {
                    "emittedAtMs": future_ms,
                    "method": "thread/closed",
                    "params": {"threadId": "session_001"},
                },
                separators=(",", ":"),
            ),
            received_at="2026-08-02T00:00:01Z",
            artifact_writer=recording_writer,
        )
    assert written == []

    started_at_ms = int(
        datetime(2026, 8, 2, 0, 0, 2, tzinfo=timezone.utc).timestamp()
        * 1000
    )
    raw = json.dumps(
        {
            "id": 8,
            "method": "item/fileChange/requestApproval",
            "params": {
                "threadId": "session_001",
                "turnId": "turn_001",
                "itemId": "tool_call_001",
                "startedAtMs": started_at_ms,
            },
        },
        separators=(",", ":"),
    )
    with pytest.raises(CodexIngestionV1Error, match="exact source request"):
        capture_codex_app_server_permission(
            raw,
            received_at="2026-08-02T00:00:02Z",
            artifact_writer=recording_writer,
            permission_result=_permission(
                decided_at="2026-08-02T00:00:02Z"
            ),
        )
    assert written == []

    incomplete_generic_raw = json.dumps(
        {
            "id": 9,
            "method": "item/permissions/requestApproval",
            "params": {
                "threadId": "session_001",
                "turnId": "turn_001",
                "itemId": "tool_call_001",
                "startedAtMs": started_at_ms,
            },
        },
        separators=(",", ":"),
    )
    with pytest.raises(CodexIngestionV1Error, match="cwd"):
        capture_codex_app_server_permission(
            incomplete_generic_raw,
            received_at="2026-08-02T00:00:02Z",
            artifact_writer=recording_writer,
            permission_result=_permission(
                incomplete_generic_raw,
                decided_at="2026-08-02T00:00:02Z",
            ),
        )
    assert written == []

    with pytest.raises(CodexIngestionV1Error, match="turn_id"):
        capture_codex_hook_event(
            json.dumps(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "session_001",
                    "turn_id": 7,
                },
                separators=(",", ":"),
            ),
            received_at="2026-08-02T00:00:02Z",
            artifact_writer=recording_writer,
        )
    assert written == []


def test_permission_item_and_source_record_provenance_fail_closed():
    started_at_ms = int(
        datetime(2026, 8, 2, 0, 0, 3, tzinfo=timezone.utc).timestamp()
        * 1000
    )
    wrong_item_raw = json.dumps(
        {
            "id": 9,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "session_001",
                "turnId": "turn_001",
                "itemId": "tool_call_other",
                "startedAtMs": started_at_ms,
                "command": "dir",
            },
        },
        separators=(",", ":"),
    )
    start = _notification(
        "thread/started", {"thread": {"id": "session_001"}}, 1
    )
    pre = _notification(
        "item/started",
        {
            "threadId": "session_001",
            "turnId": "turn_001",
            "item": {
                "id": "tool_call_001",
                "type": "commandExecution",
                "command": "dir",
                "commandActions": [],
                "cwd": "C:/repo",
                "status": "inProgress",
            },
        },
        2,
    )
    wrong_permission = capture_codex_app_server_permission(
        wrong_item_raw,
        received_at="2026-08-02T00:00:03Z",
        artifact_writer=_artifact_writer,
        permission_result=_permission(
            wrong_item_raw, decided_at="2026-08-02T00:00:03Z"
        ),
    )
    assert start is not None and pre is not None
    with pytest.raises(CodexIngestionV1Error, match="active PreToolUse"):
        build_codex_ingestion_trace_drafts(
            _binding(), (start, pre, wrong_permission)
        )

    cross_turn_raw = json.dumps(
        {
            "id": 10,
            "method": "item/commandExecution/requestApproval",
            "params": {
                "threadId": "session_001",
                "turnId": "turn_002",
                "itemId": "tool_call_001",
                "startedAtMs": started_at_ms,
                "command": "dir",
            },
        },
        separators=(",", ":"),
    )
    cross_turn_permission = capture_codex_app_server_permission(
        cross_turn_raw,
        received_at="2026-08-02T00:00:03Z",
        artifact_writer=_artifact_writer,
        permission_result=_permission(
            cross_turn_raw, decided_at="2026-08-02T00:00:03Z"
        ),
    )
    with pytest.raises(CodexIngestionV1Error, match="active PreToolUse"):
        build_codex_ingestion_trace_drafts(
            _binding(), (start, pre, cross_turn_permission)
        )

    generic_raw = json.dumps(
        {
            "id": 11,
            "method": "item/permissions/requestApproval",
            "params": {
                "threadId": "session_001",
                "turnId": "turn_001",
                "itemId": "tool_call_001",
                "startedAtMs": started_at_ms,
                "cwd": "C:/repo",
                "permissions": {"network": {"enabled": True}},
            },
        },
        separators=(",", ":"),
    )
    generic_permission = capture_codex_app_server_permission(
        generic_raw,
        received_at="2026-08-02T00:00:03Z",
        artifact_writer=_artifact_writer,
        permission_result=_permission(
            generic_raw, decided_at="2026-08-02T00:00:03Z"
        ),
    )
    generic_drafts = build_codex_ingestion_trace_drafts(
        _binding(), (start, pre, generic_permission)
    )
    assert generic_drafts[2].tool is not None
    assert generic_drafts[2].tool.tool_name == "command_execution"

    approval_records = []
    for request_id, approval_id in ((12, "approval_a"), (13, "approval_b")):
        approval_raw = json.dumps(
            {
                "id": request_id,
                "method": "item/commandExecution/requestApproval",
                "params": {
                    "threadId": "session_001",
                    "turnId": "turn_001",
                    "itemId": "tool_call_001",
                    "startedAtMs": started_at_ms,
                    "approvalId": approval_id,
                    "command": "dir",
                },
            },
            separators=(",", ":"),
        )
        approval_records.append(
            capture_codex_app_server_permission(
                approval_raw,
                received_at="2026-08-02T00:00:03Z",
                artifact_writer=_artifact_writer,
                permission_result=replace(
                    _permission(
                        approval_raw,
                        decided_at="2026-08-02T00:00:03Z",
                    ),
                    decision_id="permission_" + approval_id,
                ),
            )
        )
    multiple_approval_drafts = build_codex_ingestion_trace_drafts(
        _binding(), (start, pre, *approval_records)
    )
    assert tuple(
        draft.event_type for draft in multiple_approval_drafts[2:]
    ) == (TRACE_PERMISSION_RECORDED, TRACE_PERMISSION_RECORDED)
    assert (
        multiple_approval_drafts[2].tool
        == multiple_approval_drafts[3].tool
    )
    assert (
        multiple_approval_drafts[2].permission_result.request_sha256
        != multiple_approval_drafts[3].permission_result.request_sha256
    )

    hook_start = _hook(CODEX_SESSION_START, 1)
    other_start = _hook(CODEX_SESSION_START, 2, source="other")
    assert hook_start is not None and other_start is not None
    with pytest.raises(CodexIngestionV1Error, match="exact source Artifact"):
        replace(hook_start, source_artifact=other_start.source_artifact)
    with pytest.raises(CodexIngestionV1Error, match="does not map"):
        replace(hook_start, source_method="forged")


def test_sqlite_append_is_atomic_exactly_idempotent_and_replayable():
    connection = _connection()
    ledger = SQLiteEventLedgerV1(connection, _access())
    binding = _binding()
    records = _hybrid_records()

    connection.execute("BEGIN IMMEDIATE")
    rolled_back = append_codex_ingestion_batch(
        ledger,
        binding,
        records,
        prior_events=(),
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at="2026-08-02T00:01:00Z",
    )
    assert rolled_back.current_stream_version == 12
    connection.rollback()
    assert ledger.read_stream(trace_event_stream_id(binding.trace_id)).events == ()

    committed = append_codex_ingestion_batch(
        ledger,
        binding,
        records,
        prior_events=(),
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at="2026-08-02T00:01:00Z",
    )
    replayed = append_codex_ingestion_batch(
        ledger,
        binding,
        records,
        prior_events=(),
        expected_stream_version=0,
        next_global_position=1,
        previous_event=None,
        recorded_at="2026-08-02T00:01:00Z",
    )
    assert replayed == committed
    projection = codex_ingestion_projection(binding, committed.events)
    assert projection.session_started
    assert projection.session_ended
    assert projection.active_tool_call_ids == ()
    assert projection.active_subagent_ids == ()
    assert len(projection.source_artifact_ids) == 12


def test_transcript_and_unstable_or_unmapped_messages_are_never_fact_sources():
    with pytest.raises(CodexIngestionV1Error, match="transcript"):
        capture_codex_hook_event(
            json.dumps(
                {
                    "session_id": "session_001",
                    "transcript_path": "C:/rollout.jsonl",
                    "messages": [],
                }
            ),
            received_at="2026-08-02T00:00:01Z",
            artifact_writer=_artifact_writer,
        )
    with pytest.raises(CodexIngestionV1Error, match="transcript"):
        _notification(
            "thread/realtime/transcript/done",
            {"threadId": "session_001", "text": "not stable evidence"},
            1,
        )
    assert (
        _notification(
            "item/agentMessage/delta",
            {"threadId": "session_001", "delta": "partial"},
            1,
        )
        is None
    )
    assert (
        capture_codex_hook_event(
            json.dumps(
                {
                    "hook_event_name": "PostCompact",
                    "session_id": "session_001",
                    "timestamp": "2026-08-02T00:00:01Z",
                }
            ),
            received_at="2026-08-02T00:00:01Z",
            artifact_writer=_artifact_writer,
        )
        is None
    )


def test_external_frames_cannot_select_trusted_scope_or_relax_strict_json():
    start = _hook(CODEX_SESSION_START, 1)
    assert start is not None
    with pytest.raises(CodexIngestionV1Error, match="trusted binding"):
        build_codex_ingestion_trace_drafts(
            _binding(hook_session_id="session_other"), (start,)
        )
    with pytest.raises(CodexIngestionV1Error, match="fields do not match"):
        capture_codex_app_server_notification(
            json.dumps(
                {
                    "method": "thread/closed",
                    "params": {"threadId": "session_001"},
                    "tenant_id": "tenant_forged",
                }
            ),
            received_at="2026-08-02T00:00:01Z",
            artifact_writer=_artifact_writer,
        )
    with pytest.raises(CodexIngestionV1Error, match="duplicate object key"):
        capture_codex_hook_event(
            '{"hook_event_name":"SessionStart",'
            '"hook_event_name":"SessionEnd",'
            '"session_id":"session_001",'
            '"timestamp":"2026-08-02T00:00:01Z"}',
            received_at="2026-08-02T00:00:01Z",
            artifact_writer=_artifact_writer,
        )
    with pytest.raises(CodexIngestionV1Error, match="non-finite"):
        capture_codex_hook_event(
            '{"hook_event_name":"SessionStart",'
            '"session_id":"session_001",'
            '"timestamp":"2026-08-02T00:00:01Z","bad":NaN}',
            received_at="2026-08-02T00:00:01Z",
            artifact_writer=_artifact_writer,
        )


def test_lifecycle_tool_permission_subagent_and_source_rejections_fail_closed():
    prompt = _hook(CODEX_USER_PROMPT_SUBMIT, 1, prompt="secret")
    assert prompt is not None
    with pytest.raises(CodexIngestionV1Error, match="SessionStart"):
        build_codex_ingestion_trace_drafts(_binding(), (prompt,))

    start = _hook(CODEX_SESSION_START, 1)
    permission = _hook(
        CODEX_PERMISSION_REQUEST,
        2,
        tool_name="shell_command",
        tool_input={"command": "dir"},
    )
    assert start is not None and permission is not None
    with pytest.raises(CodexIngestionV1Error, match="active PreToolUse"):
        build_codex_ingestion_trace_drafts(_binding(), (start, permission))

    subagent_stop = _hook(CODEX_SUBAGENT_STOP, 2, agent_id="subagent_001")
    assert subagent_stop is not None
    with pytest.raises(CodexIngestionV1Error, match="SubagentStart"):
        build_codex_ingestion_trace_drafts(_binding(), (start, subagent_stop))

    with pytest.raises(CodexIngestionV1Error, match="already mapped"):
        build_codex_ingestion_trace_drafts(_binding(), (start, start))

    pre_tool = _hook(
        CODEX_PRE_TOOL_USE,
        2,
        tool_use_id="tool_call_001",
        tool_name="shell_command",
        tool_input={"command": "dir"},
    )
    end = _hook(CODEX_SESSION_END, 3)
    assert pre_tool is not None and end is not None
    with pytest.raises(CodexIngestionV1Error, match="active tools"):
        build_codex_ingestion_trace_drafts(
            _binding(), (start, pre_tool, end)
        )


def test_source_artifact_must_bind_exact_protected_bytes():
    def wrong_digest(raw: bytes) -> EventArtifactRef:
        return replace(
            _artifact_writer(raw),
            content_sha256="sha256:" + "f" * 64,
            artifact_id="artifact_sha256_" + "f" * 64,
        )

    with pytest.raises(CodexIngestionV1Error, match="exact input frame"):
        capture_codex_hook_event(
            json.dumps(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "session_001",
                    "timestamp": "2026-08-02T00:00:01Z",
                }
            ),
            received_at="2026-08-02T00:00:01Z",
            artifact_writer=wrong_digest,
        )

    def public_artifact(raw: bytes) -> EventArtifactRef:
        return replace(
            _artifact_writer(raw),
            classification="public",
            encryption_key_id=None,
        )

    with pytest.raises(CodexIngestionV1Error, match="protected"):
        capture_codex_hook_event(
            json.dumps(
                {
                    "hook_event_name": "SessionStart",
                    "session_id": "session_001",
                    "timestamp": "2026-08-02T00:00:01Z",
                }
            ),
            received_at="2026-08-02T00:00:01Z",
            artifact_writer=public_artifact,
        )


def test_root_exports_are_intentional():
    assert tbm.CODEX_INGESTION_EVENT_TYPES == CODEX_INGESTION_EVENT_TYPES
    assert tbm.CodexIngestionBinding is CodexIngestionBinding
    assert tbm.capture_codex_hook_event is capture_codex_hook_event
