from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass
import hashlib
from pathlib import PurePosixPath, PureWindowsPath
from threading import Lock
from typing import Literal, NoReturn, cast

from ._ingestion import decode_bounded_utf8, parse_bounded_json
from .contracts_v3 import V3ContractError
from .event_v1 import (
    EVENT_MAX_VERSION,
    CanonicalEvent,
    EventArtifactRef,
    EventClassification,
    EventSource,
    EventTrustedContext,
    verify_event_trusted_context,
)
from .ledger_port_v1 import (
    EventLedgerAtomicAppendPort,
    EventLedgerClassificationDeniedError,
    EventLedgerConflictError,
    EventLedgerIdempotencyConflictError,
    EventLedgerInvalidRequestError,
    EventLedgerNotFoundError,
    EventLedgerScopeDeniedError,
    EventLedgerUnsupportedError,
    LedgerAccessContext,
    LedgerAppendCommit,
)
from .trace_event_v1 import (
    TRACE_EVENT_MAX_SEQUENCE,
    TraceEventRecordRef,
    TraceEventV1Error,
    TracePermissionResult,
    append_trace_event_batch,
    build_trace_event_batch,
    parse_trace_event,
    verify_trace_event_batch,
)


CODEX_APP_SERVER_INGESTION_CONTRACT_VERSION = "tbm.codex-app-server-ingestion.v1"
CODEX_APP_SERVER_WIRE_VERSION = "v2"
CODEX_APP_SERVER_CLI_VERSION = "0.146.0"
CODEX_APP_SERVER_SOURCE_SYSTEM = "codex_app_server_0.146.0"
CODEX_APP_SERVER_MAX_FRAME_BYTES = 8 * 1024 * 1024
CODEX_APP_SERVER_MAX_FRAME_NODES = 100_000
CODEX_APP_SERVER_MAX_FRAME_DEPTH = 100
CODEX_APP_SERVER_MAX_HOOK_ENTRIES = 100
CODEX_APP_SERVER_MAX_HOOK_TEXT_CHARS = 8_192
CODEX_APP_SERVER_MAX_PATH_CHARS = 4_096
CODEX_APP_SERVER_MAX_PATCH_CHANGES = 10_000

CodexNotificationStatus = Literal["appended", "skipped"]
CodexSkipReason = Literal["known_unmapped", "not_final_response"]

_HOOK_EVENT_NAMES = (
    "preToolUse",
    "permissionRequest",
    "postToolUse",
    "preCompact",
    "postCompact",
    "sessionStart",
    "sessionEnd",
    "userPromptSubmit",
    "subagentStart",
    "subagentStop",
    "stop",
)
_HOOK_EVENT_SEGMENTS = {
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
_HOOK_EXECUTION_MODES = frozenset({"sync", "async"})
_HOOK_HANDLER_TYPES = frozenset({"command", "prompt", "agent"})
_HOOK_SCOPES = frozenset({"thread", "turn"})
_HOOK_SOURCES = frozenset(
    {
        "system",
        "user",
        "project",
        "mdm",
        "sessionFlags",
        "plugin",
        "cloudRequirements",
        "cloudManagedConfig",
        "legacyManagedConfigFile",
        "legacyManagedConfigMdm",
        "unknown",
    }
)
_HOOK_STATUSES = frozenset({"running", "completed", "failed", "blocked", "stopped"})
_HOOK_ENTRY_KINDS = frozenset({"warning", "stop", "feedback", "context", "error"})
_TOOL_HOOK_EVENTS = frozenset({"preToolUse", "permissionRequest", "postToolUse"})
_MAPPED_METHODS = frozenset(
    {"hook/started", "hook/completed", "turn/diff/updated", "item/completed"}
)
_TEXT_DELTA_SKIPPED_METHODS = frozenset(
    {
        "item/agentMessage/delta",
        "item/plan/delta",
        "item/commandExecution/outputDelta",
        "item/fileChange/outputDelta",
    }
)
_KNOWN_SKIPPED_METHODS = _TEXT_DELTA_SKIPPED_METHODS | frozenset(
    {
        "item/fileChange/patchUpdated",
        "item/reasoning/summaryTextDelta",
        "item/reasoning/summaryPartAdded",
        "item/reasoning/textDelta",
        "thread/compacted",
    }
)
_PATCH_CHANGE_TYPES = frozenset({"add", "delete", "update"})
_SKIPPED_ITEM_FIELDS = {
    "collabAgentToolCall": (
        frozenset(
            {
                "agentsStates",
                "id",
                "receiverThreadIds",
                "senderThreadId",
                "status",
                "tool",
                "type",
            }
        ),
        frozenset(
            {
                "agentsStates",
                "id",
                "model",
                "prompt",
                "reasoningEffort",
                "receiverThreadIds",
                "senderThreadId",
                "status",
                "tool",
                "type",
            }
        ),
    ),
    "commandExecution": (
        frozenset({"command", "commandActions", "cwd", "id", "status", "type"}),
        frozenset(
            {
                "aggregatedOutput",
                "command",
                "commandActions",
                "cwd",
                "durationMs",
                "exitCode",
                "id",
                "pluginId",
                "processId",
                "scriptPath",
                "source",
                "status",
                "type",
            }
        ),
    ),
    "contextCompaction": (
        frozenset({"id", "type"}),
        frozenset({"id", "type"}),
    ),
    "dynamicToolCall": (
        frozenset({"arguments", "id", "status", "tool", "type"}),
        frozenset(
            {
                "arguments",
                "contentItems",
                "durationMs",
                "id",
                "namespace",
                "status",
                "success",
                "tool",
                "type",
            }
        ),
    ),
    "enteredReviewMode": (
        frozenset({"id", "review", "type"}),
        frozenset({"id", "review", "type"}),
    ),
    "exitedReviewMode": (
        frozenset({"id", "review", "type"}),
        frozenset({"id", "review", "type"}),
    ),
    "fileChange": (
        frozenset({"changes", "id", "status", "type"}),
        frozenset({"changes", "id", "status", "type"}),
    ),
    "hookPrompt": (
        frozenset({"fragments", "id", "type"}),
        frozenset({"fragments", "id", "type"}),
    ),
    "imageGeneration": (
        frozenset({"id", "result", "status", "type"}),
        frozenset({"id", "result", "revisedPrompt", "savedPath", "status", "type"}),
    ),
    "imageView": (
        frozenset({"id", "path", "type"}),
        frozenset({"id", "path", "type"}),
    ),
    "mcpToolCall": (
        frozenset({"arguments", "id", "server", "status", "tool", "type"}),
        frozenset(
            {
                "appContext",
                "arguments",
                "durationMs",
                "error",
                "id",
                "mcpAppResourceUri",
                "pluginId",
                "result",
                "server",
                "status",
                "tool",
                "type",
            }
        ),
    ),
    "plan": (
        frozenset({"id", "text", "type"}),
        frozenset({"id", "text", "type"}),
    ),
    "reasoning": (
        frozenset({"id", "type"}),
        frozenset({"content", "id", "summary", "type"}),
    ),
    "sleep": (
        frozenset({"durationMs", "id", "type"}),
        frozenset({"durationMs", "id", "type"}),
    ),
    "subAgentActivity": (
        frozenset({"agentPath", "agentThreadId", "id", "kind", "type"}),
        frozenset({"agentPath", "agentThreadId", "id", "kind", "type"}),
    ),
    "userMessage": (
        frozenset({"content", "id", "type"}),
        frozenset({"clientId", "content", "id", "type"}),
    ),
    "webSearch": (
        frozenset({"id", "query", "type"}),
        frozenset({"action", "id", "query", "results", "type"}),
    ),
}


class CodexAppServerIngestionV1Error(V3ContractError):
    """Stable failure for bounded Codex App Server Trace ingestion."""


@dataclass(frozen=True)
class CodexAppServerNotificationResult:
    status: CodexNotificationStatus
    method: str
    event: CanonicalEvent | None
    commit: LedgerAppendCommit | None
    artifact_ids: tuple[str, ...]
    skipped_reason: CodexSkipReason | None

    def __post_init__(self) -> None:
        if self.status not in {"appended", "skipped"}:
            _invalid("TBM_CODEX_APP_SERVER_RESULT_INVALID", "result status is invalid")
        _bounded_string(self.method, "result method", max_chars=128)
        if type(self.artifact_ids) is not tuple or any(
            type(item) is not str for item in self.artifact_ids
        ):
            _invalid(
                "TBM_CODEX_APP_SERVER_RESULT_INVALID",
                "result artifact identifiers are invalid",
            )
        if self.status == "appended":
            if (
                type(self.event) is not CanonicalEvent
                or type(self.commit) is not LedgerAppendCommit
                or self.skipped_reason is not None
                or self.artifact_ids
                != tuple(item.artifact_id for item in self.event.artifact_refs)
            ):
                _invalid(
                    "TBM_CODEX_APP_SERVER_RESULT_INVALID",
                    "appended result is invalid",
                )
        elif (
            self.event is not None
            or self.commit is not None
            or self.artifact_ids
            or self.skipped_reason not in {"known_unmapped", "not_final_response"}
        ):
            _invalid(
                "TBM_CODEX_APP_SERVER_RESULT_INVALID",
                "skipped result is invalid",
            )


@dataclass(frozen=True)
class _MappedNotification:
    method: str
    trace_event_type: str
    tool_correlation_id: str | None
    permission_result: TracePermissionResult


@dataclass(frozen=True)
class _PendingNotification:
    method: str
    events: tuple[CanonicalEvent, ...]
    parent_event: CanonicalEvent | None


class CodexAppServerTraceRecorder:
    def __init__(
        self,
        ledger: EventLedgerAtomicAppendPort,
        *,
        trace_id: str,
        run_id: str,
        thread_id: str,
        trusted_context: EventTrustedContext,
        clock: Callable[[], str],
        classification: EventClassification,
        retention_policy_id: str,
        first_sequence: int,
        first_global_position: int,
        parent_event: CanonicalEvent | None,
        wire_version: str = CODEX_APP_SERVER_WIRE_VERSION,
        codex_cli_version: str = CODEX_APP_SERVER_CLI_VERSION,
    ) -> None:
        if type(trusted_context) is not EventTrustedContext:
            _invalid(
                "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID",
                "trusted_context must be exactly EventTrustedContext",
            )
        try:
            access_context = ledger.access_context
        except Exception as error:
            raise CodexAppServerIngestionV1Error(
                "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID",
                "ledger must expose trusted atomic append",
            ) from error
        if (
            type(access_context) is not LedgerAccessContext
            or access_context.event_trusted_context() != trusted_context
        ):
            _invalid(
                "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID",
                "ledger trusted context does not match recorder context",
            )
        if not access_context.classification_filter.allows(classification):
            _invalid(
                "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID",
                "ledger classification filter does not allow recorder events",
            )
        if not callable(clock):
            _invalid(
                "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID",
                "clock must be callable",
            )
        if wire_version != CODEX_APP_SERVER_WIRE_VERSION:
            _invalid(
                "TBM_CODEX_APP_SERVER_WIRE_VERSION_UNSUPPORTED",
                "Codex App Server wire version is unsupported",
            )
        if codex_cli_version != CODEX_APP_SERVER_CLI_VERSION:
            _invalid(
                "TBM_CODEX_APP_SERVER_CLI_VERSION_UNSUPPORTED",
                "Codex CLI version is unsupported",
            )
        if classification == "public":
            _invalid(
                "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID",
                "Codex App Server frames cannot use public classification",
            )
        self._ledger = ledger
        self._trace_id = trace_id
        self._run_id = run_id
        self._thread_id = _bounded_string(
            thread_id,
            "thread_id",
            max_chars=128,
        )
        self._trusted_context = trusted_context
        self._clock = clock
        self._classification = classification
        self._retention_policy_id = retention_policy_id
        self._next_sequence = first_sequence
        self._next_global_position = first_global_position
        self._parent_event = parent_event
        self._pending: _PendingNotification | None = None
        self._lock = Lock()
        self._preflight()

    @property
    def parent_event(self) -> CanonicalEvent | None:
        with self._lock:
            return self._parent_event

    @property
    def next_sequence(self) -> int:
        with self._lock:
            return self._next_sequence

    @property
    def next_global_position(self) -> int:
        with self._lock:
            return self._next_global_position

    @property
    def has_pending(self) -> bool:
        with self._lock:
            return self._pending is not None

    def ingest_notification(
        self,
        frame: bytes,
        *,
        artifact_refs: tuple[EventArtifactRef, ...] = (),
    ) -> CodexAppServerNotificationResult:
        with self._lock:
            if self._pending is not None:
                _invalid(
                    "TBM_CODEX_APP_SERVER_PENDING_RESUME_REQUIRED",
                    "pending Codex App Server append must be resumed first",
                )
            payload = _parse_notification_frame(frame)
            method = cast(str, payload["method"])
            if method.startswith("thread/realtime/transcript/"):
                _invalid(
                    "TBM_CODEX_APP_SERVER_TRANSCRIPT_UNSUPPORTED",
                    "realtime transcript notifications are unsupported",
                )
            if method in _KNOWN_SKIPPED_METHODS:
                params = _mapping(payload.get("params"), "notification params")
                self._validate_skipped_notification(method, params)
                _require_no_artifacts(artifact_refs)
                return _skipped(method, "known_unmapped")
            if method not in _MAPPED_METHODS:
                _invalid(
                    "TBM_CODEX_APP_SERVER_METHOD_UNSUPPORTED",
                    "Codex App Server notification method is unsupported",
                )
            params = _mapping(payload.get("params"), "notification params")
            mapped = self._map_notification(method, params)
            if mapped is None:
                _require_no_artifacts(artifact_refs)
                return _skipped(method, "not_final_response")
            artifacts = self._validate_artifact(frame, artifact_refs)
            pending = self._build_pending(mapped, artifacts)
            self._pending = pending
            return self._drain_pending()

    def resume_pending(self) -> CodexAppServerNotificationResult | None:
        with self._lock:
            if self._pending is None:
                return None
            return self._drain_pending()

    def _preflight(self) -> None:
        if (
            type(self._next_sequence) is not int
            or not 1 <= self._next_sequence <= TRACE_EVENT_MAX_SEQUENCE
        ):
            _invalid(
                "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID",
                "first_sequence is invalid",
            )
        if (
            type(self._next_global_position) is not int
            or not 1 <= self._next_global_position <= EVENT_MAX_VERSION
        ):
            _invalid(
                "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID",
                "first_global_position is invalid",
            )
        if self._parent_event is None:
            if self._next_sequence != 1:
                _invalid(
                    "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID",
                    "new Codex Trace stream must begin at sequence one",
                )
        else:
            try:
                parent = parse_trace_event(self._parent_event)
            except TraceEventV1Error as error:
                raise CodexAppServerIngestionV1Error(
                    "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID",
                    "parent_event is not a retained Trace event",
                ) from error
            if (
                parent.trace_id != self._trace_id
                or parent.run_id != self._run_id
                or self._next_sequence != parent.sequence + 1
                or self._next_global_position <= self._parent_event.global_position
            ):
                _invalid(
                    "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID",
                    "Codex Trace cursor does not continue parent_event",
                )
            try:
                verify_event_trusted_context(
                    self._parent_event,
                    self._trusted_context,
                )
            except V3ContractError as error:
                raise CodexAppServerIngestionV1Error(
                    "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID",
                    "parent_event trusted context does not match recorder",
                ) from error
        try:
            TraceEventRecordRef(
                trace_id=self._trace_id,
                run_id=self._run_id,
                sequence=self._next_sequence,
                trace_event_type="codex.preflight",
                occurred_at="1970-01-01T00:00:00Z",
                authorization_event_id=(
                    self._trusted_context.authorization_decision_id
                ),
                source=EventSource(
                    source_system=CODEX_APP_SERVER_SOURCE_SYSTEM,
                    source_record_id="codex_preflight",
                    evidence_quality="exact",
                    observed_at="1970-01-01T00:00:00Z",
                ),
                artifact_refs=(),
                classification=self._classification,
                retention_policy_id=self._retention_policy_id,
            )
        except (V3ContractError, ValueError) as error:
            raise CodexAppServerIngestionV1Error(
                "TBM_CODEX_APP_SERVER_CONFIGURATION_INVALID",
                "Codex App Server recorder configuration is invalid",
            ) from error

    def _map_notification(
        self,
        method: str,
        params: dict[str, object],
    ) -> _MappedNotification | None:
        if method in {"hook/started", "hook/completed"}:
            return self._map_hook(method, params)
        if method == "turn/diff/updated":
            self._validate_diff(params)
            return _MappedNotification(
                method,
                "codex.turn.diff.updated",
                None,
                "not_applicable",
            )
        return self._map_final_response(params)

    def _map_hook(
        self,
        method: str,
        params: dict[str, object],
    ) -> _MappedNotification:
        if set(params) not in (
            {"run", "threadId"},
            {"run", "threadId", "turnId"},
        ):
            _frame_invalid("hook notification fields are invalid")
        self._validate_thread(params)
        turn_id = params.get("turnId")
        if turn_id is not None:
            _bounded_string(turn_id, "turnId", max_chars=128)
        run = _mapping(params.get("run"), "hook run")
        event_name = self._validate_hook_run(method, run)
        phase = "started" if method == "hook/started" else "completed"
        permission: TracePermissionResult = "not_applicable"
        if event_name == "permissionRequest":
            permission = "pending" if phase == "started" else "unknown"
        correlation = None
        if event_name in _TOOL_HOOK_EVENTS:
            correlation = _correlation_identifier(
                b"tbm.codex-hook.v1\x00",
                self._trace_id,
                self._run_id,
                cast(str, run["id"]),
            )
        return _MappedNotification(
            method,
            f"codex.hook.{_HOOK_EVENT_SEGMENTS[event_name]}.{phase}",
            correlation,
            permission,
        )

    def _validate_hook_run(
        self,
        method: str,
        run: dict[str, object],
    ) -> str:
        required = {
            "displayOrder",
            "entries",
            "eventName",
            "executionMode",
            "handlerType",
            "id",
            "scope",
            "sourcePath",
            "startedAt",
            "status",
        }
        optional = {"completedAt", "durationMs", "source", "statusMessage"}
        if not required <= set(run) or not set(run) <= required | optional:
            _frame_invalid("hook run fields are invalid")
        _non_negative_int64(run.get("displayOrder"), "displayOrder")
        _non_negative_int64(run.get("startedAt"), "startedAt")
        for field in ("completedAt", "durationMs"):
            value = run.get(field)
            if value is not None:
                _non_negative_int64(value, field)
        entries = run.get("entries")
        if (
            type(entries) is not list
            or len(entries) > CODEX_APP_SERVER_MAX_HOOK_ENTRIES
        ):
            _frame_invalid("hook entries are invalid")
        for value in entries:
            entry = _mapping(value, "hook entry")
            if set(entry) != {"kind", "text"}:
                _frame_invalid("hook entry fields are invalid")
            if entry.get("kind") not in _HOOK_ENTRY_KINDS:
                _frame_invalid("hook entry kind is invalid")
            _bounded_text(
                entry.get("text"),
                "hook entry text",
                max_chars=CODEX_APP_SERVER_MAX_HOOK_TEXT_CHARS,
                allow_empty=True,
            )
        event_name = run.get("eventName")
        if event_name not in _HOOK_EVENT_NAMES:
            _frame_invalid("hook eventName is invalid")
        if run.get("executionMode") not in _HOOK_EXECUTION_MODES:
            _frame_invalid("hook executionMode is invalid")
        if run.get("handlerType") not in _HOOK_HANDLER_TYPES:
            _frame_invalid("hook handlerType is invalid")
        _bounded_string(run.get("id"), "hook id", max_chars=128)
        if run.get("scope") not in _HOOK_SCOPES:
            _frame_invalid("hook scope is invalid")
        source_path = _bounded_string(
            run.get("sourcePath"),
            "hook sourcePath",
            max_chars=CODEX_APP_SERVER_MAX_PATH_CHARS,
        )
        if not _is_absolute_normalized_path(source_path):
            _frame_invalid("hook sourcePath is invalid")
        source = run.get("source", "unknown")
        if source not in _HOOK_SOURCES:
            _frame_invalid("hook source is invalid")
        status = run.get("status")
        if status not in _HOOK_STATUSES:
            _frame_invalid("hook status is invalid")
        if method == "hook/started" and status != "running":
            _frame_invalid("started hook status is invalid")
        if method == "hook/completed" and status == "running":
            _frame_invalid("completed hook status is invalid")
        status_message = run.get("statusMessage")
        if status_message is not None:
            _bounded_text(
                status_message,
                "hook statusMessage",
                max_chars=CODEX_APP_SERVER_MAX_HOOK_TEXT_CHARS,
                allow_empty=True,
            )
        return cast(str, event_name)

    def _validate_diff(self, params: dict[str, object]) -> None:
        if set(params) != {"diff", "threadId", "turnId"}:
            _frame_invalid("turn diff fields are invalid")
        self._validate_thread(params)
        _bounded_string(params.get("turnId"), "turnId", max_chars=128)
        _bounded_text(
            params.get("diff"),
            "turn diff",
            max_chars=CODEX_APP_SERVER_MAX_FRAME_BYTES,
            allow_empty=True,
        )

    def _map_final_response(
        self,
        params: dict[str, object],
    ) -> _MappedNotification | None:
        if set(params) != {"completedAtMs", "item", "threadId", "turnId"}:
            _frame_invalid("item completed fields are invalid")
        self._validate_thread(params)
        _bounded_string(params.get("turnId"), "turnId", max_chars=128)
        _non_negative_int64(params.get("completedAtMs"), "completedAtMs")
        item = _mapping(params.get("item"), "completed item")
        if not {"id", "type"} <= set(item):
            _frame_invalid("completed item fields are invalid")
        item_type = item.get("type")
        if type(item_type) is not str:
            _frame_invalid("completed item type is invalid")
        _bounded_string(item.get("id"), "completed item id", max_chars=128)
        if item_type != "agentMessage":
            contract = _SKIPPED_ITEM_FIELDS.get(item_type)
            if contract is None:
                _invalid(
                    "TBM_CODEX_APP_SERVER_ITEM_UNSUPPORTED",
                    "completed Codex item type is unsupported",
                )
            required, allowed = contract
            if not required <= set(item) or not set(item) <= allowed:
                _frame_invalid("completed item fields are invalid")
            return None
        required = {"id", "text", "type"}
        optional = {"phase", "memoryCitation"}
        if not required <= set(item) or not set(item) <= required | optional:
            _frame_invalid("agent message fields are invalid")
        _bounded_text(
            item.get("text"),
            "agent message text",
            max_chars=CODEX_APP_SERVER_MAX_FRAME_BYTES,
            allow_empty=True,
        )
        phase = item.get("phase")
        if phase not in {None, "commentary", "final_answer"}:
            _frame_invalid("agent message phase is invalid")
        if item.get("memoryCitation") is not None:
            _frame_invalid("agent message memoryCitation is unsupported")
        if phase != "final_answer":
            return None
        return _MappedNotification(
            "item/completed",
            "codex.final_response",
            None,
            "not_applicable",
        )

    def _validate_skipped_notification(
        self,
        method: str,
        params: dict[str, object],
    ) -> None:
        if method in _TEXT_DELTA_SKIPPED_METHODS:
            required = {"delta", "itemId", "threadId", "turnId"}
            if set(params) != required:
                _frame_invalid("delta notification fields are invalid")
            self._validate_item_turn(params)
            _bounded_text(
                params.get("delta"),
                "delta",
                max_chars=CODEX_APP_SERVER_MAX_FRAME_BYTES,
                allow_empty=True,
            )
            return
        if method == "thread/compacted":
            if set(params) != {"threadId", "turnId"}:
                _frame_invalid("compacted notification fields are invalid")
            self._validate_thread(params)
            _bounded_string(params.get("turnId"), "turnId", max_chars=128)
            return
        if method == "item/fileChange/patchUpdated":
            if set(params) != {"changes", "itemId", "threadId", "turnId"}:
                _frame_invalid("patch notification fields are invalid")
            self._validate_item_turn(params)
            changes = params.get("changes")
            if (
                type(changes) is not list
                or len(changes) > CODEX_APP_SERVER_MAX_PATCH_CHANGES
            ):
                _frame_invalid("patch changes are invalid")
            for value in changes:
                change = _mapping(value, "patch change")
                if set(change) != {"diff", "kind", "path"}:
                    _frame_invalid("patch change fields are invalid")
                _bounded_text(
                    change.get("diff"),
                    "patch diff",
                    max_chars=CODEX_APP_SERVER_MAX_FRAME_BYTES,
                    allow_empty=True,
                )
                _bounded_text(
                    change.get("path"),
                    "patch path",
                    max_chars=CODEX_APP_SERVER_MAX_PATH_CHARS,
                )
                kind = _mapping(change.get("kind"), "patch kind")
                kind_type = kind.get("type")
                if kind_type not in _PATCH_CHANGE_TYPES:
                    _frame_invalid("patch kind is invalid")
                allowed_kind_fields = (
                    {"type", "move_path"} if kind_type == "update" else {"type"}
                )
                if not {"type"} <= set(kind) or not set(kind) <= allowed_kind_fields:
                    _frame_invalid("patch kind fields are invalid")
                move_path = kind.get("move_path")
                if move_path is not None:
                    _bounded_text(
                        move_path,
                        "patch move_path",
                        max_chars=CODEX_APP_SERVER_MAX_PATH_CHARS,
                    )
            return
        required = {"itemId", "summaryIndex", "threadId", "turnId"}
        if method == "item/reasoning/summaryTextDelta":
            required.add("delta")
        elif method == "item/reasoning/textDelta":
            required = {"contentIndex", "delta", "itemId", "threadId", "turnId"}
        if set(params) != required:
            _frame_invalid("reasoning notification fields are invalid")
        self._validate_item_turn(params)
        for field in ("summaryIndex", "contentIndex"):
            if field in params:
                _non_negative_int64(params.get(field), field)
        if "delta" in params:
            _bounded_text(
                params.get("delta"),
                "reasoning delta",
                max_chars=CODEX_APP_SERVER_MAX_FRAME_BYTES,
                allow_empty=True,
            )

    def _validate_item_turn(self, params: Mapping[str, object]) -> None:
        self._validate_thread(params)
        _bounded_string(params.get("turnId"), "turnId", max_chars=128)
        _bounded_string(params.get("itemId"), "itemId", max_chars=128)

    def _validate_thread(self, params: Mapping[str, object]) -> None:
        thread_id = _bounded_string(
            params.get("threadId"),
            "threadId",
            max_chars=128,
        )
        if thread_id != self._thread_id:
            _invalid(
                "TBM_CODEX_APP_SERVER_THREAD_MISMATCH",
                "Codex App Server thread does not match trusted binding",
            )

    def _validate_artifact(
        self,
        frame: bytes,
        artifact_refs: tuple[EventArtifactRef, ...],
    ) -> tuple[EventArtifactRef, ...]:
        if (
            type(artifact_refs) is not tuple
            or len(artifact_refs) != 1
            or type(artifact_refs[0]) is not EventArtifactRef
        ):
            _invalid(
                "TBM_CODEX_APP_SERVER_ARTIFACT_INVALID",
                "mapped notification requires one exact frame Artifact",
            )
        artifact = artifact_refs[0]
        digest = "sha256:" + hashlib.sha256(frame).hexdigest()
        if (
            artifact.content_sha256 != digest
            or artifact.artifact_id
            != "artifact_sha256_" + digest.removeprefix("sha256:")
            or artifact.media_type != "application/json"
            or artifact.size_bytes != len(frame)
            or artifact.classification != self._classification
            or artifact.retention_policy_id != self._retention_policy_id
            or artifact.availability != "available"
        ):
            _invalid(
                "TBM_CODEX_APP_SERVER_ARTIFACT_INVALID",
                "frame Artifact does not match the notification bytes",
            )
        return artifact_refs

    def _build_pending(
        self,
        mapped: _MappedNotification,
        artifact_refs: tuple[EventArtifactRef, ...],
    ) -> _PendingNotification:
        try:
            now = self._clock()
        except Exception as error:
            raise CodexAppServerIngestionV1Error(
                "TBM_CODEX_APP_SERVER_CLOCK_FAILED",
                "trusted Codex ingestion clock failed",
            ) from error
        source_record_id = artifact_refs[0].artifact_id.replace(
            "artifact_sha256_",
            "codex_frame_",
        )
        try:
            reference = TraceEventRecordRef(
                trace_id=self._trace_id,
                run_id=self._run_id,
                sequence=self._next_sequence,
                trace_event_type=mapped.trace_event_type,
                occurred_at=now,
                authorization_event_id=(
                    self._trusted_context.authorization_decision_id
                ),
                source=EventSource(
                    source_system=CODEX_APP_SERVER_SOURCE_SYSTEM,
                    source_record_id=source_record_id,
                    evidence_quality="exact",
                    observed_at=now,
                ),
                artifact_refs=artifact_refs,
                classification=self._classification,
                retention_policy_id=self._retention_policy_id,
                tool_correlation_id=mapped.tool_correlation_id,
                permission_result=mapped.permission_result,
            )
            events = build_trace_event_batch(
                (reference,),
                parent_event=self._parent_event,
                first_global_position=self._next_global_position,
                trusted_context=self._trusted_context,
                recorded_at=now,
            )
        except (V3ContractError, ValueError) as error:
            raise CodexAppServerIngestionV1Error(
                "TBM_CODEX_APP_SERVER_TRACE_INVALID",
                "Codex App Server notification cannot build Trace evidence",
            ) from error
        return _PendingNotification(mapped.method, events, self._parent_event)

    def _drain_pending(self) -> CodexAppServerNotificationResult:
        pending = self._pending
        if pending is None:
            raise AssertionError("pending notification is absent")
        try:
            verify_trace_event_batch(
                pending.events,
                parent_event=pending.parent_event,
            )
            commit = append_trace_event_batch(
                self._ledger,
                pending.events,
                parent_event=pending.parent_event,
            )
        except (
            TraceEventV1Error,
            EventLedgerInvalidRequestError,
            EventLedgerConflictError,
            EventLedgerIdempotencyConflictError,
            EventLedgerScopeDeniedError,
            EventLedgerClassificationDeniedError,
            EventLedgerNotFoundError,
            EventLedgerUnsupportedError,
        ) as error:
            self._pending = None
            raise CodexAppServerIngestionV1Error(
                "TBM_CODEX_APP_SERVER_APPEND_REJECTED",
                "Codex App Server Trace append was rejected before commit",
            ) from error
        except Exception as error:
            raise CodexAppServerIngestionV1Error(
                "TBM_CODEX_APP_SERVER_APPEND_UNCERTAIN",
                "Codex App Server Trace append requires exact resume",
            ) from error
        event = pending.events[-1]
        self._parent_event = event
        self._next_sequence += 1
        self._next_global_position += 1
        self._pending = None
        return CodexAppServerNotificationResult(
            "appended",
            pending.method,
            event,
            commit,
            tuple(item.artifact_id for item in event.artifact_refs),
            None,
        )


def _parse_notification_frame(frame: bytes) -> dict[str, object]:
    if type(frame) is not bytes:
        _frame_invalid("frame must be bytes")
    try:
        source_text = decode_bounded_utf8(
            frame,
            max_bytes=CODEX_APP_SERVER_MAX_FRAME_BYTES,
            description="Codex App Server notification",
        )
        payload = parse_bounded_json(
            source_text,
            description="Codex App Server notification",
            max_nodes=CODEX_APP_SERVER_MAX_FRAME_NODES,
            max_depth=CODEX_APP_SERVER_MAX_FRAME_DEPTH,
        )
    except (UnicodeDecodeError, ValueError) as error:
        raise CodexAppServerIngestionV1Error(
            "TBM_CODEX_APP_SERVER_FRAME_INVALID",
            "Codex App Server notification frame is invalid",
        ) from error
    if type(payload) is not dict:
        _frame_invalid("notification frame must be an object")
    notification = cast(dict[str, object], payload)
    if "id" in notification or "jsonrpc" in notification:
        _invalid(
            "TBM_CODEX_APP_SERVER_NOTIFICATION_REQUIRED",
            "Codex App Server requests and responses are unsupported",
        )
    if set(notification) not in (
        {"method", "params"},
        {"method", "params", "emittedAtMs"},
    ):
        _frame_invalid("notification envelope fields are invalid")
    _bounded_string(notification.get("method"), "method", max_chars=128)
    if not isinstance(notification.get("params"), Mapping):
        _frame_invalid("notification params must be an object")
    if "emittedAtMs" in notification:
        _non_negative_int64(notification.get("emittedAtMs"), "emittedAtMs")
    return notification


def _mapping(value: object, name: str) -> dict[str, object]:
    if type(value) is not dict:
        _frame_invalid(f"{name} must be an object")
    return cast(dict[str, object], value)


def _bounded_string(
    value: object,
    name: str,
    *,
    max_chars: int,
    allow_empty: bool = False,
) -> str:
    if (
        type(value) is not str
        or (not allow_empty and not value)
        or len(value) > max_chars
        or any(ord(character) < 32 or ord(character) == 127 for character in value)
    ):
        _frame_invalid(f"{name} is invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise CodexAppServerIngestionV1Error(
            "TBM_CODEX_APP_SERVER_FRAME_INVALID",
            "Codex App Server notification string is invalid",
        ) from error
    return value


def _non_negative_int64(value: object, name: str) -> int:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        _frame_invalid(f"{name} is invalid")
    return value


def _bounded_text(
    value: object,
    name: str,
    *,
    max_chars: int,
    allow_empty: bool = False,
) -> str:
    if (
        type(value) is not str
        or (not allow_empty and not value)
        or len(value) > max_chars
    ):
        _frame_invalid(f"{name} is invalid")
    try:
        value.encode("utf-8")
    except UnicodeEncodeError as error:
        raise CodexAppServerIngestionV1Error(
            "TBM_CODEX_APP_SERVER_FRAME_INVALID",
            "Codex App Server notification text is invalid",
        ) from error
    return value


def _correlation_identifier(domain: bytes, *parts: str) -> str:
    digest = hashlib.sha256(domain)
    for part in parts:
        encoded = part.encode("utf-8")
        digest.update(len(encoded).to_bytes(4, "big"))
        digest.update(encoded)
    return "codex_hook_" + digest.hexdigest()[:40]


def _is_absolute_normalized_path(value: str) -> bool:
    posix = PurePosixPath(value)
    if posix.is_absolute():
        return value == str(posix)
    windows = PureWindowsPath(value)
    if not windows.is_absolute():
        return False
    canonical = str(windows)
    return value in {canonical, canonical.replace("\\", "/")}


def _require_no_artifacts(
    artifact_refs: tuple[EventArtifactRef, ...],
) -> None:
    if type(artifact_refs) is not tuple or artifact_refs:
        _invalid(
            "TBM_CODEX_APP_SERVER_ARTIFACT_INVALID",
            "skipped notification cannot retain Artifact references",
        )


def _skipped(
    method: str,
    reason: CodexSkipReason,
) -> CodexAppServerNotificationResult:
    return CodexAppServerNotificationResult(
        "skipped",
        method,
        None,
        None,
        (),
        reason,
    )


def _frame_invalid(message: str) -> NoReturn:
    _invalid("TBM_CODEX_APP_SERVER_FRAME_INVALID", message)


def _invalid(code: str, message: str) -> NoReturn:
    raise CodexAppServerIngestionV1Error(code, message)


__all__ = [
    "CODEX_APP_SERVER_CLI_VERSION",
    "CODEX_APP_SERVER_INGESTION_CONTRACT_VERSION",
    "CODEX_APP_SERVER_MAX_FRAME_BYTES",
    "CODEX_APP_SERVER_MAX_FRAME_DEPTH",
    "CODEX_APP_SERVER_MAX_FRAME_NODES",
    "CODEX_APP_SERVER_MAX_PATCH_CHANGES",
    "CODEX_APP_SERVER_SOURCE_SYSTEM",
    "CODEX_APP_SERVER_WIRE_VERSION",
    "CodexAppServerIngestionV1Error",
    "CodexAppServerNotificationResult",
    "CodexAppServerTraceRecorder",
    "CodexNotificationStatus",
    "CodexSkipReason",
]
