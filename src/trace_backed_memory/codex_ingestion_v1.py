from __future__ import annotations

from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
import hashlib
import json
from typing import Literal, NoReturn, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from ._timestamps import canonical_rfc3339, parse_rfc3339
from .contracts_v3 import V3ContractError
from .event_v1 import CanonicalEvent, EventArtifactRef
from .ledger_port_v1 import EventLedgerPort, LedgerAppendReceipt
from .trace_event_v1 import (
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
    TraceEventDraft,
    TraceEventLineage,
    TracePermissionResult,
    TraceToolCorrelation,
    append_trace_event_batch,
    trace_event_stream_id,
    verify_trace_event,
)


CODEX_INGESTION_PROTOCOL_VERSION = "tbm.codex-ingestion.v1"
CODEX_INGESTION_MAX_FRAME_BYTES = 1024 * 1024
CODEX_INGESTION_MAX_FRAME_DEPTH = 64
CODEX_INGESTION_MAX_FRAME_NODES = 50_000
CODEX_INGESTION_MAX_SOURCE_CLOCK_SKEW_SECONDS = 300
CODEX_SOURCE_ARTIFACT_MEDIA_TYPE = (
    "application/vnd.trace-backed-memory.codex-source+json"
)
CODEX_SOURCE_ARTIFACT_RETENTION_POLICY_ID = "retention_codex_source_v1"

CODEX_SESSION_START = "SessionStart"
CODEX_USER_PROMPT_SUBMIT = "UserPromptSubmit"
CODEX_PRE_TOOL_USE = "PreToolUse"
CODEX_PERMISSION_REQUEST = "PermissionRequest"
CODEX_POST_TOOL_USE = "PostToolUse"
CODEX_SUBAGENT_START = "SubagentStart"
CODEX_SUBAGENT_STOP = "SubagentStop"
CODEX_PRE_COMPACT = "PreCompact"
CODEX_STOP = "Stop"
CODEX_SESSION_END = "SessionEnd"
CODEX_DIFF_UPDATE = "DiffUpdate"
CODEX_FINAL_RESPONSE = "FinalResponse"

CODEX_INGESTION_EVENT_TYPES = (
    CODEX_SESSION_START,
    CODEX_USER_PROMPT_SUBMIT,
    CODEX_PRE_TOOL_USE,
    CODEX_PERMISSION_REQUEST,
    CODEX_POST_TOOL_USE,
    CODEX_SUBAGENT_START,
    CODEX_SUBAGENT_STOP,
    CODEX_PRE_COMPACT,
    CODEX_STOP,
    CODEX_SESSION_END,
    CODEX_DIFF_UPDATE,
    CODEX_FINAL_RESPONSE,
)

CodexSourceProfile = Literal["codex_hook", "codex_app_server"]
CodexIngestionEventType = Literal[
    "SessionStart",
    "UserPromptSubmit",
    "PreToolUse",
    "PermissionRequest",
    "PostToolUse",
    "SubagentStart",
    "SubagentStop",
    "PreCompact",
    "Stop",
    "SessionEnd",
    "DiffUpdate",
    "FinalResponse",
]
CodexSourceArtifactWriter = Callable[[bytes], EventArtifactRef]

_TRACE_EVENT_BY_CODEX_EVENT = {
    CODEX_SESSION_START: TRACE_SESSION_STARTED,
    CODEX_USER_PROMPT_SUBMIT: TRACE_USER_PROMPT_SUBMITTED,
    CODEX_PRE_TOOL_USE: TRACE_TOOL_STARTED,
    CODEX_PERMISSION_REQUEST: TRACE_PERMISSION_RECORDED,
    CODEX_POST_TOOL_USE: TRACE_TOOL_COMPLETED,
    CODEX_SUBAGENT_START: TRACE_SUBAGENT_STARTED,
    CODEX_SUBAGENT_STOP: TRACE_SUBAGENT_STOPPED,
    CODEX_PRE_COMPACT: TRACE_PRE_COMPACT,
    CODEX_STOP: TRACE_STOPPED,
    CODEX_SESSION_END: TRACE_SESSION_ENDED,
    CODEX_DIFF_UPDATE: TRACE_DIFF_OBSERVED,
    CODEX_FINAL_RESPONSE: TRACE_FINAL_RESPONSE_RECORDED,
}
_HOOK_EVENT_TYPES = frozenset(CODEX_INGESTION_EVENT_TYPES[:10])
_APP_SERVER_NOTIFICATION_METHODS = frozenset(
    {
        "thread/started",
        "item/started",
        "item/completed",
        "turn/completed",
        "thread/closed",
        "turn/diff/updated",
    }
)
_APP_SERVER_PERMISSION_METHODS = {
    "item/commandExecution/requestApproval": "command_execution",
    "item/fileChange/requestApproval": "file_change",
    "item/permissions/requestApproval": "__active_tool__",
}
_APP_SERVER_ANY_TOOL_PERMISSION = "__active_tool__"
_SOURCE_METHODS_BY_EVENT = {
    "codex_hook": {
        event_type: frozenset({event_type})
        for event_type in _HOOK_EVENT_TYPES
    },
    "codex_app_server": {
        CODEX_SESSION_START: frozenset({"thread/started"}),
        CODEX_USER_PROMPT_SUBMIT: frozenset({"item/completed"}),
        CODEX_PRE_TOOL_USE: frozenset({"item/started"}),
        CODEX_PERMISSION_REQUEST: frozenset(_APP_SERVER_PERMISSION_METHODS),
        CODEX_POST_TOOL_USE: frozenset({"item/completed"}),
        CODEX_SUBAGENT_START: frozenset({"item/completed"}),
        CODEX_SUBAGENT_STOP: frozenset({"item/completed"}),
        CODEX_PRE_COMPACT: frozenset({"item/started"}),
        CODEX_STOP: frozenset({"turn/completed"}),
        CODEX_SESSION_END: frozenset({"thread/closed"}),
        CODEX_DIFF_UPDATE: frozenset({"turn/diff/updated"}),
        CODEX_FINAL_RESPONSE: frozenset({"item/completed"}),
    },
}
_APP_SERVER_TOOL_TYPES = frozenset(
    {
        "commandExecution",
        "fileChange",
        "mcpToolCall",
        "dynamicToolCall",
        "collabAgentToolCall",
        "webSearch",
        "imageGeneration",
    }
)
_TOOL_NAMES = {
    "commandExecution": "command_execution",
    "fileChange": "file_change",
    "webSearch": "web_search",
    "imageGeneration": "image_generation",
}
_VOLATILE_TOOL_FIELDS = frozenset(
    {
        "aggregatedOutput",
        "agentsStates",
        "contentItems",
        "durationMs",
        "error",
        "exitCode",
        "result",
        "results",
        "receiverThreadIds",
        "savedPath",
        "status",
        "success",
    }
)
_IDENTIFIER_MAX_CHARS = 128
_CODE_MAX_CHARS = 256
_MAX_SEQUENCE = 9_223_372_036_854_775_807


class CodexIngestionV1Error(V3ContractError):
    """Stable failure raised by Codex hook/App Server ingestion."""


@dataclass(frozen=True)
class CodexIngestionBinding:
    trace_id: str
    run_id: str
    lineage: TraceEventLineage
    hook_session_id: str | None = None
    app_server_thread_id: str | None = None

    def __post_init__(self) -> None:
        _identifier(self.trace_id, "trace_id")
        _identifier(self.run_id, "run_id")
        if type(self.lineage) is not TraceEventLineage:
            _fail(
                "TBM_CODEX_INGESTION_BINDING_INVALID",
                "lineage must be exactly TraceEventLineage",
            )
        _optional_identifier(self.hook_session_id, "hook_session_id")
        _optional_identifier(
            self.app_server_thread_id, "app_server_thread_id"
        )
        if self.hook_session_id is None and self.app_server_thread_id is None:
            _fail(
                "TBM_CODEX_INGESTION_BINDING_INVALID",
                "at least one trusted Codex source identity is required",
            )

    def expected_session_id(self, source: CodexSourceProfile) -> str:
        if source == "codex_hook" and self.hook_session_id is not None:
            return self.hook_session_id
        if (
            source == "codex_app_server"
            and self.app_server_thread_id is not None
        ):
            return self.app_server_thread_id
        _fail(
            "TBM_CODEX_INGESTION_SOURCE_DENIED",
            "Codex source is not enabled by the trusted binding",
        )


@dataclass(frozen=True)
class CodexToolFact:
    tool_call_id: str | None
    tool_name: str
    invocation_sha256: str | None
    parent_tool_call_id: str | None = None

    def __post_init__(self) -> None:
        _optional_identifier(self.tool_call_id, "tool_call_id")
        _code(self.tool_name, "tool_name")
        _optional_digest(self.invocation_sha256, "invocation_sha256")
        _optional_identifier(
            self.parent_tool_call_id, "parent_tool_call_id"
        )
        if (
            self.tool_call_id is not None
            and self.parent_tool_call_id == self.tool_call_id
        ):
            _fail(
                "TBM_CODEX_INGESTION_TOOL_INVALID",
                "tool correlation cannot name itself as parent",
            )


@dataclass(frozen=True)
class CodexSourceRecord:
    source: CodexSourceProfile
    source_event: CodexIngestionEventType
    source_method: str
    source_record_id: str
    source_session_id: str
    source_turn_id: str | None
    occurred_at: str
    source_artifact: EventArtifactRef
    tool: CodexToolFact | None = None
    permission_result: TracePermissionResult | None = None
    related_subagent_id: str | None = None

    def __post_init__(self) -> None:
        if self.source not in {"codex_hook", "codex_app_server"}:
            _fail(
                "TBM_CODEX_INGESTION_SOURCE_INVALID",
                "Codex source profile is invalid",
            )
        if self.source_event not in CODEX_INGESTION_EVENT_TYPES:
            _fail(
                "TBM_CODEX_INGESTION_EVENT_INVALID",
                "Codex source event is not mapped",
            )
        _code(self.source_method, "source_method")
        if self.source_method not in _SOURCE_METHODS_BY_EVENT[self.source].get(
            self.source_event, frozenset()
        ):
            _fail(
                "TBM_CODEX_INGESTION_SOURCE_INVALID",
                "source method does not map to the claimed source event",
            )
        _identifier(self.source_record_id, "source_record_id")
        _identifier(self.source_session_id, "source_session_id")
        _optional_identifier(self.source_turn_id, "source_turn_id")
        object.__setattr__(
            self,
            "occurred_at",
            _canonical_timestamp(self.occurred_at, "occurred_at"),
        )
        _verify_source_artifact(self.source_artifact)
        if self.source_record_id != _source_record_id(
            self.source,
            self.source_event,
            self.source_method,
            self.source_session_id,
            self.source_turn_id,
            self.source_artifact,
        ):
            _fail(
                "TBM_CODEX_INGESTION_SOURCE_INVALID",
                "source record identity does not bind its exact source Artifact",
            )
        if self.tool is not None and type(self.tool) is not CodexToolFact:
            _fail(
                "TBM_CODEX_INGESTION_TOOL_INVALID",
                "tool must be exactly CodexToolFact or null",
            )
        if (
            self.permission_result is not None
            and type(self.permission_result) is not TracePermissionResult
        ):
            _fail(
                "TBM_CODEX_INGESTION_PERMISSION_INVALID",
                "permission result must be exactly TracePermissionResult or null",
            )
        _optional_identifier(
            self.related_subagent_id, "related_subagent_id"
        )
        tool_event = self.source_event in {
            CODEX_PRE_TOOL_USE,
            CODEX_PERMISSION_REQUEST,
            CODEX_POST_TOOL_USE,
        }
        if tool_event != (self.tool is not None):
            _fail(
                "TBM_CODEX_INGESTION_TOOL_INVALID",
                "only tool lifecycle source events carry tool facts",
            )
        if self.tool is not None:
            missing_tool_call_id = self.tool.tool_call_id is None
            hook_permission = (
                self.source == "codex_hook"
                and self.source_event == CODEX_PERMISSION_REQUEST
            )
            if missing_tool_call_id != hook_permission:
                _fail(
                    "TBM_CODEX_INGESTION_TOOL_INVALID",
                    "only a Hook PermissionRequest omits tool_call_id",
                )
        if (self.source_event == CODEX_PERMISSION_REQUEST) != (
            self.permission_result is not None
        ):
            _fail(
                "TBM_CODEX_INGESTION_PERMISSION_INVALID",
                "only a completed permission mapping carries a permission result",
            )
        subagent_event = self.source_event in {
            CODEX_SUBAGENT_START,
            CODEX_SUBAGENT_STOP,
        }
        if subagent_event != (self.related_subagent_id is not None):
            _fail(
                "TBM_CODEX_INGESTION_SUBAGENT_INVALID",
                "only subagent lifecycle events carry a related subagent",
            )


@dataclass(frozen=True)
class CodexIngestionProjection:
    session_started: bool
    session_ended: bool
    seen_tool_call_ids: tuple[str, ...]
    active_tool_call_ids: tuple[str, ...]
    active_subagent_ids: tuple[str, ...]
    source_artifact_ids: tuple[str, ...]


@dataclass
class _MutableProjection:
    session_started: bool
    session_ended: bool
    seen_tools: set[str]
    active_tools: dict[str, TraceToolCorrelation]
    active_subagents: set[str]
    source_artifact_ids: set[str]


def capture_codex_hook_event(
    data: str | bytes | bytearray,
    *,
    received_at: str,
    artifact_writer: CodexSourceArtifactWriter,
    permission_result: TracePermissionResult | None = None,
) -> CodexSourceRecord | None:
    """Parse one structured Codex hook stdin frame and retain its exact bytes."""

    raw, value = _loads_frame(data, label="Codex hook")
    hook_event = value.get("hook_event_name")
    if hook_event is None:
        if any(
            key in value
            for key in ("transcript", "transcript_path", "messages")
        ):
            _fail(
                "TBM_CODEX_INGESTION_TRANSCRIPT_FORBIDDEN",
                "a transcript is not a structured Codex lifecycle event",
            )
        _fail(
            "TBM_CODEX_INGESTION_FRAME_INVALID",
            "Codex hook frame is missing hook_event_name",
        )
    if hook_event == "PostCompact":
        return None
    if type(hook_event) is not str or hook_event not in _HOOK_EVENT_TYPES:
        _fail(
            "TBM_CODEX_INGESTION_EVENT_INVALID",
            "Codex hook event is not mapped",
        )
    session_id = _mapping_identifier(value, "session_id")
    turn_id = _optional_mapping_identifier(value, "turn_id")
    occurred_at = _canonical_timestamp(received_at, "received_at")
    tool: CodexToolFact | None = None
    related_subagent_id: str | None = None
    if hook_event in {
        CODEX_PRE_TOOL_USE,
        CODEX_PERMISSION_REQUEST,
        CODEX_POST_TOOL_USE,
    }:
        source_tool_call_id = (
            None
            if hook_event == CODEX_PERMISSION_REQUEST
            else _mapping_identifier(value, "tool_use_id")
        )
        tool_call_id = (
            None
            if source_tool_call_id is None
            else _hook_tool_call_id(session_id, source_tool_call_id)
        )
        tool_name = _mapping_code(value, "tool_name")
        if "tool_input" not in value:
            _fail(
                "TBM_CODEX_INGESTION_TOOL_INVALID",
                "tool hook is missing tool_input",
            )
        tool = CodexToolFact(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            invocation_sha256=_hook_invocation_sha256(
                tool_name, value["tool_input"]
            ),
            parent_tool_call_id=(
                None
                if hook_event == CODEX_PERMISSION_REQUEST
                else _optional_hook_parent_tool_call_id(value, session_id)
            ),
        )
    if hook_event in {CODEX_SUBAGENT_START, CODEX_SUBAGENT_STOP}:
        related_subagent_id = _first_mapping_identifier(
            value, ("agent_id", "subagent_id")
        )
    if hook_event == CODEX_PERMISSION_REQUEST:
        if type(permission_result) is not TracePermissionResult:
            _fail(
                "TBM_CODEX_INGESTION_PERMISSION_INVALID",
                "PermissionRequest requires a trusted completed decision",
            )
        _verify_permission_request_digest(raw, permission_result)
        occurred_at = _permission_occurred_at(
            permission_result, received_at=received_at
        )
    elif permission_result is not None:
        _fail(
            "TBM_CODEX_INGESTION_PERMISSION_INVALID",
            "only PermissionRequest accepts a permission result",
        )
    artifact = _write_source_artifact(raw, artifact_writer)
    source_record_id = _source_record_id(
        "codex_hook",
        cast(CodexIngestionEventType, hook_event),
        hook_event,
        session_id,
        turn_id,
        artifact,
    )
    return CodexSourceRecord(
        source="codex_hook",
        source_event=cast(CodexIngestionEventType, hook_event),
        source_method=hook_event,
        source_record_id=source_record_id,
        source_session_id=session_id,
        source_turn_id=turn_id,
        occurred_at=occurred_at,
        source_artifact=artifact,
        tool=tool,
        permission_result=permission_result,
        related_subagent_id=related_subagent_id,
    )


def capture_codex_app_server_notification(
    data: str | bytes | bytearray,
    *,
    received_at: str,
    artifact_writer: CodexSourceArtifactWriter,
) -> CodexSourceRecord | None:
    """Map one stable App Server notification; ignore unrelated notifications."""

    raw, value = _loads_frame(data, label="Codex App Server notification")
    _notification_fields(value)
    method = _mapping_code(value, "method")
    if method not in _APP_SERVER_NOTIFICATION_METHODS:
        if "transcript" in method.lower():
            _fail(
                "TBM_CODEX_INGESTION_TRANSCRIPT_FORBIDDEN",
                "App Server transcript notifications are not lifecycle facts",
            )
        return None
    params = _mapping_object(value, "params")
    source_event: str
    session_id: str
    turn_id: str | None = None
    tool: CodexToolFact | None = None
    related_subagent_id: str | None = None
    if method == "thread/started":
        thread = _mapping_object(params, "thread")
        session_id = _mapping_identifier(thread, "id")
        source_event = CODEX_SESSION_START
    elif method == "thread/closed":
        session_id = _mapping_identifier(params, "threadId")
        source_event = CODEX_SESSION_END
    elif method == "turn/completed":
        session_id = _mapping_identifier(params, "threadId")
        turn = _mapping_object(params, "turn")
        turn_id = _mapping_identifier(turn, "id")
        source_event = CODEX_STOP
    elif method == "turn/diff/updated":
        session_id = _mapping_identifier(params, "threadId")
        turn_id = _mapping_identifier(params, "turnId")
        _mapping_string(params, "diff")
        source_event = CODEX_DIFF_UPDATE
    else:
        session_id = _mapping_identifier(params, "threadId")
        turn_id = _mapping_identifier(params, "turnId")
        item = _mapping_object(params, "item")
        _mapping_identifier(item, "id")
        item_type = _mapping_code(item, "type")
        if method == "item/started" and item_type in _APP_SERVER_TOOL_TYPES:
            source_event = CODEX_PRE_TOOL_USE
            tool = _app_server_tool_fact(
                item,
                source_session_id=session_id,
                source_turn_id=turn_id,
            )
        elif method == "item/completed" and item_type in _APP_SERVER_TOOL_TYPES:
            source_event = CODEX_POST_TOOL_USE
            tool = _app_server_tool_fact(
                item,
                source_session_id=session_id,
                source_turn_id=turn_id,
            )
        elif method == "item/completed" and item_type == "userMessage":
            source_event = CODEX_USER_PROMPT_SUBMIT
        elif method == "item/started" and item_type == "contextCompaction":
            source_event = CODEX_PRE_COMPACT
        elif method == "item/completed" and item_type == "subAgentActivity":
            activity_kind = _mapping_code(item, "kind")
            if activity_kind == "started":
                source_event = CODEX_SUBAGENT_START
            elif activity_kind == "interrupted":
                source_event = CODEX_SUBAGENT_STOP
            elif activity_kind == "interacted":
                return None
            else:
                _fail(
                    "TBM_CODEX_INGESTION_SUBAGENT_INVALID",
                    "App Server subagent activity kind is not supported",
                )
            related_subagent_id = _mapping_identifier(
                item, "agentThreadId"
            )
        elif method == "item/completed" and item_type == "agentMessage":
            if item.get("phase") != "final_answer":
                return None
            _mapping_string(item, "text")
            source_event = CODEX_FINAL_RESPONSE
        else:
            return None
    occurred_at = _app_server_occurred_at(
        value, params, method=method, received_at=received_at
    )
    artifact = _write_source_artifact(raw, artifact_writer)
    return CodexSourceRecord(
        source="codex_app_server",
        source_event=cast(CodexIngestionEventType, source_event),
        source_method=method,
        source_record_id=_source_record_id(
            "codex_app_server",
            cast(CodexIngestionEventType, source_event),
            method,
            session_id,
            turn_id,
            artifact,
        ),
        source_session_id=session_id,
        source_turn_id=turn_id,
        occurred_at=occurred_at,
        source_artifact=artifact,
        tool=tool,
        related_subagent_id=related_subagent_id,
    )


def capture_codex_app_server_permission(
    data: str | bytes | bytearray,
    *,
    received_at: str,
    artifact_writer: CodexSourceArtifactWriter,
    permission_result: TracePermissionResult,
) -> CodexSourceRecord:
    """Map an intercepted App Server approval request after its exact decision."""

    if type(permission_result) is not TracePermissionResult:
        _fail(
            "TBM_CODEX_INGESTION_PERMISSION_INVALID",
            "App Server permission mapping requires a trusted decision",
        )
    raw, value = _loads_frame(data, label="Codex App Server request")
    _exact_fields(value, frozenset({"id", "method", "params"}), "request")
    method = _mapping_code(value, "method")
    tool_name = _APP_SERVER_PERMISSION_METHODS.get(method)
    if tool_name is None:
        _fail(
            "TBM_CODEX_INGESTION_PERMISSION_INVALID",
            "App Server request is not a mapped approval request",
        )
    params = _mapping_object(value, "params")
    session_id = _mapping_identifier(params, "threadId")
    turn_id = _mapping_identifier(params, "turnId")
    source_item_id = _mapping_identifier(params, "itemId")
    if method == "item/permissions/requestApproval":
        _mapping_string(params, "cwd")
        _mapping_object(params, "permissions")
    tool_call_id = _app_server_tool_call_id(
        session_id, turn_id, source_item_id
    )
    started_at = _unix_milliseconds_timestamp(
        params.get("startedAtMs"), "startedAtMs"
    )
    occurred_at = _permission_occurred_at(
        permission_result,
        received_at=received_at,
        started_at=started_at,
    )
    _verify_permission_request_digest(raw, permission_result)
    artifact = _write_source_artifact(raw, artifact_writer)
    return CodexSourceRecord(
        source="codex_app_server",
        source_event=CODEX_PERMISSION_REQUEST,
        source_method=method,
        source_record_id=_source_record_id(
            "codex_app_server",
            CODEX_PERMISSION_REQUEST,
            method,
            session_id,
            turn_id,
            artifact,
        ),
        source_session_id=session_id,
        source_turn_id=turn_id,
        occurred_at=occurred_at,
        source_artifact=artifact,
        tool=CodexToolFact(
            tool_call_id=tool_call_id,
            tool_name=tool_name,
            invocation_sha256=None,
        ),
        permission_result=permission_result,
    )


def build_codex_ingestion_trace_drafts(
    binding: CodexIngestionBinding,
    records: tuple[CodexSourceRecord, ...],
    *,
    prior_events: tuple[CanonicalEvent, ...] = (),
) -> tuple[TraceEventDraft, ...]:
    """Validate source lifecycle state and map records to TraceEvent drafts."""

    if type(binding) is not CodexIngestionBinding:
        _fail(
            "TBM_CODEX_INGESTION_BINDING_INVALID",
            "binding must be exactly CodexIngestionBinding",
        )
    if (
        type(records) is not tuple
        or not 1 <= len(records) <= 100
        or any(type(item) is not CodexSourceRecord for item in records)
    ):
        _fail(
            "TBM_CODEX_INGESTION_BATCH_INVALID",
            "source records must be a bounded non-empty tuple",
        )
    state = _projection_from_prior_events(binding, prior_events)
    drafts: list[TraceEventDraft] = []
    for offset, record in enumerate(records, start=1):
        expected_session_id = binding.expected_session_id(record.source)
        if record.source_session_id != expected_session_id:
            _fail(
                "TBM_CODEX_INGESTION_SCOPE_MISMATCH",
                "source session does not match the trusted binding",
            )
        if record.source_artifact.artifact_id in state.source_artifact_ids:
            _fail(
                "TBM_CODEX_INGESTION_DUPLICATE_SOURCE",
                "source Artifact was already mapped in this Trace stream",
            )
        state.source_artifact_ids.add(record.source_artifact.artifact_id)
        tool = _apply_source_record(state, record)
        draft = TraceEventDraft(
            event_type=_TRACE_EVENT_BY_CODEX_EVENT[record.source_event],
            trace_id=binding.trace_id,
            run_id=binding.run_id,
            sequence=len(prior_events) + offset,
            occurred_at=record.occurred_at,
            artifact_refs=(record.source_artifact,),
            tool=tool,
            permission_result=record.permission_result,
            lineage=binding.lineage,
            related_subagent_id=record.related_subagent_id,
            classification=record.source_artifact.classification,
            retention_policy_id=record.source_artifact.retention_policy_id,
        )
        drafts.append(draft)
    return tuple(drafts)


def codex_ingestion_projection(
    binding: CodexIngestionBinding,
    events: tuple[CanonicalEvent, ...],
) -> CodexIngestionProjection:
    state = _projection_from_prior_events(binding, events)
    return CodexIngestionProjection(
        session_started=state.session_started,
        session_ended=state.session_ended,
        seen_tool_call_ids=tuple(sorted(state.seen_tools)),
        active_tool_call_ids=tuple(sorted(state.active_tools)),
        active_subagent_ids=tuple(sorted(state.active_subagents)),
        source_artifact_ids=tuple(sorted(state.source_artifact_ids)),
    )


def append_codex_ingestion_batch(
    ledger: EventLedgerPort,
    binding: CodexIngestionBinding,
    records: tuple[CodexSourceRecord, ...],
    *,
    prior_events: tuple[CanonicalEvent, ...],
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    recorded_at: str,
) -> LedgerAppendReceipt:
    """Map and atomically append one bounded Codex source batch."""

    if type(expected_stream_version) is not int or not (
        0 <= expected_stream_version <= _MAX_SEQUENCE
    ):
        _fail(
            "TBM_CODEX_INGESTION_BATCH_INVALID",
            "expected stream version is invalid",
        )
    if len(prior_events) != expected_stream_version:
        _fail(
            "TBM_CODEX_INGESTION_HISTORY_INVALID",
            "complete prior Trace history must match the expected head",
        )
    if expected_stream_version == 0:
        if previous_event is not None:
            _fail(
                "TBM_CODEX_INGESTION_HISTORY_INVALID",
                "new Codex Trace stream cannot have a previous event",
            )
    elif not prior_events or previous_event != prior_events[-1]:
        _fail(
            "TBM_CODEX_INGESTION_HISTORY_INVALID",
            "previous event must be the exact complete-history head",
        )
    drafts = build_codex_ingestion_trace_drafts(
        binding,
        records,
        prior_events=prior_events,
    )
    return append_trace_event_batch(
        ledger,
        drafts,
        expected_stream_version=expected_stream_version,
        next_global_position=next_global_position,
        previous_event=previous_event,
        recorded_at=recorded_at,
    )


def _projection_from_prior_events(
    binding: CodexIngestionBinding,
    events: tuple[CanonicalEvent, ...],
) -> _MutableProjection:
    if type(events) is not tuple or any(
        type(item) is not CanonicalEvent for item in events
    ):
        _fail(
            "TBM_CODEX_INGESTION_HISTORY_INVALID",
            "prior events must be a tuple of CanonicalEvent values",
        )
    state = _MutableProjection(False, False, set(), {}, set(), set())
    previous: CanonicalEvent | None = None
    expected_stream_id = trace_event_stream_id(binding.trace_id)
    for sequence, event in enumerate(events, start=1):
        verify_trace_event(event)
        if (
            event.stream_id != expected_stream_id
            or event.stream_version != sequence
            or event.payload["trace_id"] != binding.trace_id
            or event.payload["run_id"] != binding.run_id
            or event.payload["lineage"] != binding.lineage.to_dict()
            or (
                previous is not None
                and event.previous_stream_event_sha256
                != previous.event_sha256
            )
        ):
            _fail(
                "TBM_CODEX_INGESTION_HISTORY_INVALID",
                "prior Trace history is incomplete or belongs to another binding",
            )
        source_refs = tuple(
            item
            for item in event.artifact_refs
            if item.media_type == CODEX_SOURCE_ARTIFACT_MEDIA_TYPE
        )
        if len(source_refs) != 1:
            _fail(
                "TBM_CODEX_INGESTION_HISTORY_INVALID",
                "Codex ingestion cannot mix with unqualified Trace history",
            )
        source_ref = source_refs[0]
        _verify_source_artifact(source_ref)
        if source_ref.artifact_id in state.source_artifact_ids:
            _fail(
                "TBM_CODEX_INGESTION_DUPLICATE_SOURCE",
                "source Artifact is mapped more than once",
            )
        state.source_artifact_ids.add(source_ref.artifact_id)
        _apply_trace_event(state, event)
        previous = event
    return state


def _apply_source_record(
    state: _MutableProjection,
    record: CodexSourceRecord,
) -> TraceToolCorrelation | None:
    event_type = record.source_event
    if state.session_ended:
        _fail(
            "TBM_CODEX_INGESTION_LIFECYCLE_INVALID",
            "no event may follow SessionEnd",
        )
    if event_type == CODEX_SESSION_START:
        if state.session_started:
            _fail(
                "TBM_CODEX_INGESTION_LIFECYCLE_INVALID",
                "SessionStart must be the first and only session start",
            )
        state.session_started = True
        return None
    if not state.session_started:
        _fail(
            "TBM_CODEX_INGESTION_LIFECYCLE_INVALID",
            "SessionStart must precede all other source events",
        )
    if event_type == CODEX_SESSION_END:
        if state.active_tools or state.active_subagents:
            _fail(
                "TBM_CODEX_INGESTION_LIFECYCLE_INVALID",
                "SessionEnd requires no active tools or subagents",
            )
        state.session_ended = True
        return None
    if event_type == CODEX_PRE_TOOL_USE:
        fact = cast(CodexToolFact, record.tool)
        tool_call_id = cast(str, fact.tool_call_id)
        if fact.invocation_sha256 is None:
            _fail(
                "TBM_CODEX_INGESTION_TOOL_INVALID",
                "PreToolUse requires an exact invocation digest",
            )
        if tool_call_id in state.seen_tools:
            _fail(
                "TBM_CODEX_INGESTION_TOOL_INVALID",
                "tool call identity cannot be reused",
            )
        correlation = _trace_tool(fact, "request", fact.invocation_sha256)
        state.seen_tools.add(tool_call_id)
        state.active_tools[tool_call_id] = correlation
        return correlation
    if event_type in {CODEX_PERMISSION_REQUEST, CODEX_POST_TOOL_USE}:
        fact = cast(CodexToolFact, record.tool)
        tool_call_id, active = _resolve_active_tool(state, fact)
        if active is None:
            _fail(
                "TBM_CODEX_INGESTION_TOOL_INVALID",
                "tool transition requires an active PreToolUse",
            )
        tool_name_matches = fact.tool_name == active.tool_name or (
            event_type == CODEX_PERMISSION_REQUEST
            and fact.tool_name == _APP_SERVER_ANY_TOOL_PERMISSION
        )
        if (
            not tool_name_matches
            or (
                fact.parent_tool_call_id is not None
                and fact.parent_tool_call_id != active.parent_tool_call_id
            )
            or (
                fact.invocation_sha256 is not None
                and fact.invocation_sha256 != active.invocation_sha256
            )
        ):
            _fail(
                "TBM_CODEX_INGESTION_TOOL_INVALID",
                "tool transition does not match its exact invocation",
            )
        if event_type == CODEX_PERMISSION_REQUEST:
            return TraceToolCorrelation(
                tool_call_id=tool_call_id,
                tool_name=active.tool_name,
                phase="permission",
                invocation_sha256=active.invocation_sha256,
                parent_tool_call_id=active.parent_tool_call_id,
            )
        del state.active_tools[tool_call_id]
        return _trace_tool(fact, "result", active.invocation_sha256)
    if event_type == CODEX_SUBAGENT_START:
        subagent_id = cast(str, record.related_subagent_id)
        if subagent_id in state.active_subagents:
            _fail(
                "TBM_CODEX_INGESTION_SUBAGENT_INVALID",
                "subagent is already active",
            )
        state.active_subagents.add(subagent_id)
    elif event_type == CODEX_SUBAGENT_STOP:
        subagent_id = cast(str, record.related_subagent_id)
        if subagent_id not in state.active_subagents:
            _fail(
                "TBM_CODEX_INGESTION_SUBAGENT_INVALID",
                "SubagentStop requires a matching SubagentStart",
            )
        state.active_subagents.remove(subagent_id)
    return None


def _apply_trace_event(
    state: _MutableProjection, event: CanonicalEvent
) -> None:
    event_type = event.event_type
    if state.session_ended:
        _fail(
            "TBM_CODEX_INGESTION_LIFECYCLE_INVALID",
            "no event may follow SessionEnd",
        )
    if event_type == TRACE_SESSION_STARTED:
        if state.session_started:
            _fail(
                "TBM_CODEX_INGESTION_LIFECYCLE_INVALID",
                "SessionStart must be the first and only session start",
            )
        state.session_started = True
        return
    if not state.session_started:
        _fail(
            "TBM_CODEX_INGESTION_LIFECYCLE_INVALID",
            "SessionStart must precede all other Trace events",
        )
    if event_type == TRACE_SESSION_ENDED:
        if state.active_tools or state.active_subagents:
            _fail(
                "TBM_CODEX_INGESTION_LIFECYCLE_INVALID",
                "SessionEnd requires no active tools or subagents",
            )
        state.session_ended = True
        return
    tool_value = event.payload["tool"]
    if event_type == TRACE_TOOL_STARTED:
        tool = _tool_from_payload(tool_value)
        if tool.tool_call_id in state.seen_tools:
            _fail(
                "TBM_CODEX_INGESTION_TOOL_INVALID",
                "tool call identity cannot be reused",
            )
        state.seen_tools.add(tool.tool_call_id)
        state.active_tools[tool.tool_call_id] = tool
    elif event_type in {TRACE_PERMISSION_RECORDED, TRACE_TOOL_COMPLETED}:
        tool = _tool_from_payload(tool_value)
        active = state.active_tools.get(tool.tool_call_id)
        if active is None or (
            tool.tool_name,
            tool.invocation_sha256,
            tool.parent_tool_call_id,
        ) != (
            active.tool_name,
            active.invocation_sha256,
            active.parent_tool_call_id,
        ):
            _fail(
                "TBM_CODEX_INGESTION_TOOL_INVALID",
                "Trace tool transition does not match its active invocation",
            )
        if event_type == TRACE_TOOL_COMPLETED:
            del state.active_tools[tool.tool_call_id]
    elif event_type == TRACE_SUBAGENT_STARTED:
        subagent_id = cast(str, event.payload["related_subagent_id"])
        if subagent_id in state.active_subagents:
            _fail(
                "TBM_CODEX_INGESTION_SUBAGENT_INVALID",
                "subagent is already active",
            )
        state.active_subagents.add(subagent_id)
    elif event_type == TRACE_SUBAGENT_STOPPED:
        subagent_id = cast(str, event.payload["related_subagent_id"])
        if subagent_id not in state.active_subagents:
            _fail(
                "TBM_CODEX_INGESTION_SUBAGENT_INVALID",
                "SubagentStop requires a matching SubagentStart",
            )
        state.active_subagents.remove(subagent_id)


def _app_server_tool_fact(
    item: Mapping[str, object],
    *,
    source_session_id: str,
    source_turn_id: str,
) -> CodexToolFact:
    item_type = _mapping_code(item, "type")
    source_item_id = _mapping_identifier(item, "id")
    if item_type == "mcpToolCall":
        tool_name = "mcp:" + _mapping_code(item, "server") + "/" + _mapping_code(
            item, "tool"
        )
    elif item_type == "dynamicToolCall":
        namespace = item.get("namespace")
        prefix = "dynamic" if namespace is None else _code_value(
            namespace, "namespace"
        )
        tool_name = prefix + ":" + _mapping_code(item, "tool")
    elif item_type == "collabAgentToolCall":
        tool_name = "collab:" + _mapping_code(item, "tool")
    else:
        tool_name = _TOOL_NAMES.get(item_type, item_type)
    _code(tool_name, "tool_name")
    invocation = {
        key: child
        for key, child in item.items()
        if key not in _VOLATILE_TOOL_FIELDS
    }
    return CodexToolFact(
        tool_call_id=_app_server_tool_call_id(
            source_session_id, source_turn_id, source_item_id
        ),
        tool_name=tool_name,
        invocation_sha256=_invocation_sha256(invocation),
    )


def _resolve_active_tool(
    state: _MutableProjection, fact: CodexToolFact
) -> tuple[str, TraceToolCorrelation | None]:
    if fact.tool_call_id is not None:
        return fact.tool_call_id, state.active_tools.get(fact.tool_call_id)
    candidates = tuple(
        (tool_call_id, active)
        for tool_call_id, active in state.active_tools.items()
        if active.tool_name == fact.tool_name
        and fact.invocation_sha256 == active.invocation_sha256
    )
    if len(candidates) != 1:
        _fail(
            "TBM_CODEX_INGESTION_TOOL_INVALID",
            "Hook PermissionRequest must match exactly one active PreToolUse",
        )
    return candidates[0]


def _trace_tool(
    fact: CodexToolFact,
    phase: Literal["request", "permission", "result"],
    invocation_sha256: str,
) -> TraceToolCorrelation:
    return TraceToolCorrelation(
        tool_call_id=cast(str, fact.tool_call_id),
        tool_name=fact.tool_name,
        phase=phase,
        invocation_sha256=invocation_sha256,
        parent_tool_call_id=fact.parent_tool_call_id,
    )


def _tool_from_payload(value: object) -> TraceToolCorrelation:
    if not isinstance(value, Mapping):
        _fail(
            "TBM_CODEX_INGESTION_HISTORY_INVALID",
            "Trace tool payload is missing",
        )
    return TraceToolCorrelation(
        tool_call_id=_mapping_identifier(value, "tool_call_id"),
        tool_name=_mapping_code(value, "tool_name"),
        phase=cast(
            Literal["request", "permission", "result"],
            _mapping_code(value, "phase"),
        ),
        invocation_sha256=_mapping_digest(value, "invocation_sha256"),
        parent_tool_call_id=_optional_mapping_identifier(
            value, "parent_tool_call_id"
        ),
    )


def _loads_frame(
    data: str | bytes | bytearray, *, label: str
) -> tuple[bytes, Mapping[str, object]]:
    try:
        if isinstance(data, (bytes, bytearray)):
            raw = bytes(data)
            text = decode_bounded_utf8(
                raw,
                max_bytes=CODEX_INGESTION_MAX_FRAME_BYTES,
                description=label,
            )
        elif type(data) is str:
            raw = data.encode("utf-8")
            if len(raw) > CODEX_INGESTION_MAX_FRAME_BYTES:
                raise ValueError(f"{label} exceeds byte limit")
            text = data
        else:
            raise TypeError(f"{label} must be str, bytes, or bytearray")
        value = parse_bounded_json(
            text,
            description=label,
            max_depth=CODEX_INGESTION_MAX_FRAME_DEPTH,
            max_nodes=CODEX_INGESTION_MAX_FRAME_NODES,
        )
    except (TypeError, UnicodeError, ValueError) as error:
        _fail("TBM_CODEX_INGESTION_INVALID_JSON", str(error))
    if not isinstance(value, Mapping):
        _fail(
            "TBM_CODEX_INGESTION_FRAME_INVALID",
            f"{label} must contain one JSON object",
        )
    return raw, cast(Mapping[str, object], value)


def _write_source_artifact(
    raw: bytes, artifact_writer: CodexSourceArtifactWriter
) -> EventArtifactRef:
    if not callable(artifact_writer):
        _fail(
            "TBM_CODEX_INGESTION_ARTIFACT_INVALID",
            "artifact_writer must be callable",
        )
    artifact = artifact_writer(raw)
    _verify_source_artifact(artifact)
    digest = "sha256:" + hashlib.sha256(raw).hexdigest()
    if artifact.content_sha256 != digest or artifact.size_bytes != len(raw):
        _fail(
            "TBM_CODEX_INGESTION_ARTIFACT_INVALID",
            "source Artifact does not bind the exact input frame bytes",
        )
    return artifact


def _verify_permission_request_digest(
    raw: bytes, permission_result: TracePermissionResult
) -> None:
    request_sha256 = "sha256:" + hashlib.sha256(raw).hexdigest()
    if permission_result.request_sha256 != request_sha256:
        _fail(
            "TBM_CODEX_INGESTION_PERMISSION_INVALID",
            "permission decision does not bind the exact source request bytes",
        )


def _verify_source_artifact(value: object) -> None:
    if type(value) is not EventArtifactRef:
        _fail(
            "TBM_CODEX_INGESTION_ARTIFACT_INVALID",
            "source Artifact must be exactly EventArtifactRef",
        )
    artifact = cast(EventArtifactRef, value)
    if (
        artifact.media_type != CODEX_SOURCE_ARTIFACT_MEDIA_TYPE
        or artifact.retention_policy_id
        != CODEX_SOURCE_ARTIFACT_RETENTION_POLICY_ID
        or artifact.classification not in {"confidential", "restricted"}
        or artifact.encryption_key_id is None
        or artifact.availability != "available"
    ):
        _fail(
            "TBM_CODEX_INGESTION_ARTIFACT_INVALID",
            "source frames require an available protected Codex source Artifact",
        )


def _source_record_id(
    source: CodexSourceProfile,
    source_event: CodexIngestionEventType,
    method: str,
    session_id: str,
    turn_id: str | None,
    artifact: EventArtifactRef,
) -> str:
    digest = hashlib.sha256(
        b"tbm.codex-source-record.v1\x00"
        + source.encode("utf-8")
        + b"\x00"
        + source_event.encode("utf-8")
        + b"\x00"
        + method.encode("utf-8")
        + b"\x00"
        + session_id.encode("utf-8")
        + b"\x00"
        + (b"" if turn_id is None else turn_id.encode("utf-8"))
        + b"\x00"
        + artifact.content_sha256.encode("ascii")
    ).hexdigest()
    return "codex_source_" + digest


def _hook_invocation_sha256(tool_name: str, tool_input: object) -> str:
    return _invocation_sha256(
        {
            "source": "codex_hook",
            "tool_name": tool_name,
            "tool_input": tool_input,
        }
    )


def _hook_tool_call_id(session_id: str, source_tool_call_id: str) -> str:
    return _derived_tool_call_id(
        "codex_hook", session_id, None, source_tool_call_id
    )


def _optional_hook_parent_tool_call_id(
    value: Mapping[str, object], session_id: str
) -> str | None:
    source_parent_id = _optional_mapping_identifier(
        value, "parent_tool_use_id"
    )
    if source_parent_id is None:
        return None
    return _hook_tool_call_id(session_id, source_parent_id)


def _app_server_tool_call_id(
    session_id: str, turn_id: str, source_item_id: str
) -> str:
    return _derived_tool_call_id(
        "codex_app_server", session_id, turn_id, source_item_id
    )


def _derived_tool_call_id(
    source: CodexSourceProfile,
    session_id: str,
    turn_id: str | None,
    source_tool_call_id: str,
) -> str:
    digest = hashlib.sha256(
        b"tbm.codex-tool-correlation.v1\x00"
        + source.encode("ascii")
        + b"\x00"
        + session_id.encode("utf-8")
        + b"\x00"
        + (b"" if turn_id is None else turn_id.encode("utf-8"))
        + b"\x00"
        + source_tool_call_id.encode("utf-8")
    ).hexdigest()
    return "codex_tool_" + digest


def _invocation_sha256(value: object) -> str:
    return "sha256:" + hashlib.sha256(
        b"tbm.codex-tool-invocation.v1\x00" + _canonical_json_bytes(value)
    ).hexdigest()


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError) as error:
        _fail(
            "TBM_CODEX_INGESTION_FRAME_INVALID",
            f"source value is not canonical JSON: {error}",
        )


def _exact_fields(
    value: Mapping[str, object], fields: frozenset[str], label: str
) -> None:
    if set(value) != fields:
        _fail(
            "TBM_CODEX_INGESTION_FRAME_INVALID",
            f"App Server {label} fields do not match the supported contract",
        )


def _notification_fields(value: Mapping[str, object]) -> None:
    fields = set(value)
    if not {"method", "params"}.issubset(fields) or not fields.issubset(
        {"method", "params", "emittedAtMs"}
    ):
        _fail(
            "TBM_CODEX_INGESTION_FRAME_INVALID",
            "App Server notification fields do not match the supported contract",
        )
    if "emittedAtMs" in value:
        _unix_milliseconds_timestamp(value["emittedAtMs"], "emittedAtMs")


def _app_server_occurred_at(
    envelope: Mapping[str, object],
    params: Mapping[str, object],
    *,
    method: str,
    received_at: str,
) -> str:
    received = _canonical_timestamp(received_at, "received_at")
    if method == "item/started":
        return _bounded_source_timestamp(
            _unix_milliseconds_timestamp(
                params.get("startedAtMs"), "startedAtMs"
            ),
            received,
        )
    if method == "item/completed":
        return _bounded_source_timestamp(
            _unix_milliseconds_timestamp(
                params.get("completedAtMs"), "completedAtMs"
            ),
            received,
        )
    emitted_at_ms = envelope.get("emittedAtMs")
    if emitted_at_ms is not None:
        return _bounded_source_timestamp(
            _unix_milliseconds_timestamp(emitted_at_ms, "emittedAtMs"),
            received,
        )
    return received


def _bounded_source_timestamp(source_at: str, received_at: str) -> str:
    received = _canonical_timestamp(received_at, "received_at")
    try:
        skew = abs(
            (
                parse_rfc3339(source_at) - parse_rfc3339(received)
            ).total_seconds()
        )
    except ValueError:
        _fail(
            "TBM_CODEX_INGESTION_TIMESTAMP_INVALID",
            "source timestamp is not a valid RFC 3339 timestamp",
        )
    if skew > CODEX_INGESTION_MAX_SOURCE_CLOCK_SKEW_SECONDS:
        _fail(
            "TBM_CODEX_INGESTION_TIMESTAMP_INVALID",
            "source timestamp exceeds the trusted receive-time skew bound",
        )
    return source_at


def _permission_occurred_at(
    permission_result: TracePermissionResult,
    *,
    received_at: str,
    started_at: str | None = None,
) -> str:
    received = _canonical_timestamp(received_at, "received_at")
    decided = _canonical_timestamp(
        permission_result.decided_at, "permission decided_at"
    )
    received_time = parse_rfc3339(received)
    decided_time = parse_rfc3339(decided)
    if (
        abs((received_time - decided_time).total_seconds())
        > CODEX_INGESTION_MAX_SOURCE_CLOCK_SKEW_SECONDS
    ):
        _fail(
            "TBM_CODEX_INGESTION_TIMESTAMP_INVALID",
            "permission decision exceeds the trusted receive-time skew bound",
        )
    if started_at is not None and parse_rfc3339(started_at) > decided_time:
        _fail(
            "TBM_CODEX_INGESTION_TIMESTAMP_INVALID",
            "permission request cannot start after its completed decision",
        )
    return decided


def _unix_milliseconds_timestamp(value: object, name: str) -> str:
    if type(value) is not int or not 0 <= value <= _MAX_SEQUENCE:
        _fail(
            "TBM_CODEX_INGESTION_TIMESTAMP_INVALID",
            f"{name} must be a bounded Unix millisecond timestamp",
        )
    try:
        seconds, milliseconds = divmod(value, 1000)
        parsed = datetime.fromtimestamp(seconds, timezone.utc) + timedelta(
            milliseconds=milliseconds
        )
    except (OverflowError, OSError, ValueError):
        _fail(
            "TBM_CODEX_INGESTION_TIMESTAMP_INVALID",
            f"{name} is outside the supported timestamp range",
        )
    return parsed.isoformat().replace("+00:00", "Z")


def _mapping_object(
    value: Mapping[str, object], name: str
) -> Mapping[str, object]:
    child = value.get(name)
    if not isinstance(child, Mapping):
        _fail(
            "TBM_CODEX_INGESTION_FRAME_INVALID",
            f"{name} must be a JSON object",
        )
    return cast(Mapping[str, object], child)


def _mapping_string(value: Mapping[str, object], name: str) -> str:
    child = value.get(name)
    if type(child) is not str:
        _fail(
            "TBM_CODEX_INGESTION_FRAME_INVALID",
            f"{name} must be a string",
        )
    _utf8(cast(str, child), name)
    return cast(str, child)


def _mapping_identifier(value: Mapping[str, object], name: str) -> str:
    child = _mapping_string(value, name)
    _identifier(child, name)
    return child


def _optional_mapping_identifier(
    value: Mapping[str, object], name: str
) -> str | None:
    child = value.get(name)
    if child is None:
        return None
    if type(child) is not str:
        _fail(
            "TBM_CODEX_INGESTION_FRAME_INVALID",
            f"{name} must be a string or null",
        )
    _identifier(child, name)
    return cast(str, child)


def _first_mapping_identifier(
    value: Mapping[str, object], names: Sequence[str]
) -> str:
    for name in names:
        child = value.get(name)
        if child is not None:
            if type(child) is not str:
                _fail(
                    "TBM_CODEX_INGESTION_FRAME_INVALID",
                    f"{name} must be a string",
                )
            _identifier(child, name)
            return cast(str, child)
    _fail(
        "TBM_CODEX_INGESTION_FRAME_INVALID",
        "subagent event is missing its stable subagent identity",
    )


def _mapping_code(value: Mapping[str, object], name: str) -> str:
    child = _mapping_string(value, name)
    _code(child, name)
    return child


def _code_value(value: object, name: str) -> str:
    if type(value) is not str:
        _fail(
            "TBM_CODEX_INGESTION_FRAME_INVALID",
            f"{name} must be a string",
        )
    _code(value, name)
    return cast(str, value)


def _mapping_digest(value: Mapping[str, object], name: str) -> str:
    child = _mapping_string(value, name)
    _digest(child, name)
    return child


def _canonical_timestamp(value: object, name: str) -> str:
    try:
        canonical = canonical_rfc3339(value)
    except ValueError:
        _fail(
            "TBM_CODEX_INGESTION_TIMESTAMP_INVALID",
            f"{name} must be a canonical UTC RFC 3339 timestamp",
        )
    if value != canonical:
        _fail(
            "TBM_CODEX_INGESTION_TIMESTAMP_INVALID",
            f"{name} must be a canonical UTC RFC 3339 timestamp",
        )
    return canonical


def _identifier(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _IDENTIFIER_MAX_CHARS
        or any(ord(character) < 0x20 for character in value)
    ):
        _fail(
            "TBM_CODEX_INGESTION_IDENTIFIER_INVALID",
            f"{name} must be a bounded non-empty identifier",
        )
    _utf8(cast(str, value), name)


def _optional_identifier(value: object, name: str) -> None:
    if value is not None:
        _identifier(value, name)


def _code(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > _CODE_MAX_CHARS
        or any(ord(character) < 0x20 for character in value)
    ):
        _fail(
            "TBM_CODEX_INGESTION_CODE_INVALID",
            f"{name} must be a bounded non-empty code",
        )
    _utf8(cast(str, value), name)


def _optional_digest(value: object, name: str) -> None:
    if value is not None:
        _digest(value, name)


def _digest(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value.startswith("sha256:")
        or len(value) != 71
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        _fail(
            "TBM_CODEX_INGESTION_DIGEST_INVALID",
            f"{name} must be a lowercase sha256 digest",
        )


def _utf8(value: str, name: str) -> None:
    try:
        value.encode("utf-8")
    except UnicodeEncodeError:
        _fail(
            "TBM_CODEX_INGESTION_STRING_INVALID",
            f"{name} must be valid UTF-8",
        )


def _fail(code: str, message: str) -> NoReturn:
    raise CodexIngestionV1Error(code, message)


__all__ = [
    "CODEX_DIFF_UPDATE",
    "CODEX_FINAL_RESPONSE",
    "CODEX_INGESTION_EVENT_TYPES",
    "CODEX_INGESTION_MAX_FRAME_BYTES",
    "CODEX_INGESTION_MAX_FRAME_DEPTH",
    "CODEX_INGESTION_MAX_FRAME_NODES",
    "CODEX_INGESTION_MAX_SOURCE_CLOCK_SKEW_SECONDS",
    "CODEX_INGESTION_PROTOCOL_VERSION",
    "CODEX_PERMISSION_REQUEST",
    "CODEX_POST_TOOL_USE",
    "CODEX_PRE_COMPACT",
    "CODEX_PRE_TOOL_USE",
    "CODEX_SESSION_END",
    "CODEX_SESSION_START",
    "CODEX_SOURCE_ARTIFACT_MEDIA_TYPE",
    "CODEX_SOURCE_ARTIFACT_RETENTION_POLICY_ID",
    "CODEX_STOP",
    "CODEX_SUBAGENT_START",
    "CODEX_SUBAGENT_STOP",
    "CODEX_USER_PROMPT_SUBMIT",
    "CodexIngestionBinding",
    "CodexIngestionEventType",
    "CodexIngestionProjection",
    "CodexIngestionV1Error",
    "CodexSourceArtifactWriter",
    "CodexSourceProfile",
    "CodexSourceRecord",
    "CodexToolFact",
    "append_codex_ingestion_batch",
    "build_codex_ingestion_trace_drafts",
    "capture_codex_app_server_notification",
    "capture_codex_app_server_permission",
    "capture_codex_hook_event",
    "codex_ingestion_projection",
]
