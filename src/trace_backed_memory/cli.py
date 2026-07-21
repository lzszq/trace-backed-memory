from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict, fields
from pathlib import Path
from typing import Any, Sequence, TextIO

from ._ingestion import (
    CLI_JSON_FILE_MAX_BYTES,
    CLI_JSON_MAX_DEPTH,
    CLI_JSON_MAX_ITEMS,
    CLI_JSON_MAX_NODES,
    read_bounded_utf8,
)
from .capture import CommitAncestryCaptureError, capture_commit_ancestry
from .models import (
    Lesson,
    MemoryContext,
    MemoryObsolescenceRequest,
    MemoryRunCompletion,
    MemoryRunResult,
    PRChangeSet,
)
from .policy import validate_memory_context
from .resources import (
    PackagedResource,
    PackagedResourceError,
    export_packaged_resource,
    packaged_resources,
    read_packaged_resource,
)
from .store import TraceBackedMemoryStore


_ERROR_MESSAGE_MAX_CHARS = 2048
_MEMORY_CONTEXT_FIELDS = frozenset(
    field.name for field in fields(MemoryContext)
)
_MEMORY_CONTEXT_REQUIRED_FIELDS = ("mode", "repo", "commit_sha")


class CLIUsageError(ValueError):
    """Raised instead of terminating the process on invalid CLI arguments."""


class CLIInputError(ValueError):
    """Raised for command inputs that argparse cannot validate directly."""


class _JSONArgumentParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        raise CLIUsageError(message)


def _parse_boolean(value: str) -> bool:
    if value == "true":
        return True
    if value == "false":
        return False
    raise argparse.ArgumentTypeError("must be 'true' or 'false'")


def _parse_finite_float(value: str) -> float:
    try:
        parsed = float(value)
    except (ValueError, OverflowError) as error:
        raise argparse.ArgumentTypeError("must be a finite number") from error
    if not math.isfinite(parsed):
        raise argparse.ArgumentTypeError("must be a finite number")
    return parsed


def _build_parser() -> argparse.ArgumentParser:
    parser = _JSONArgumentParser(
        prog="tbm",
        description="Operate trace-backed-memory snapshots, reports, and resources.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

    resource = commands.add_parser(
        "resource",
        help="Inspect and export installed canonical resources.",
    )
    resource_commands = resource.add_subparsers(
        dest="resource_command",
        required=True,
    )
    resource_commands.add_parser(
        "list",
        help="List installed canonical resources.",
    )
    resource_read = resource_commands.add_parser(
        "read",
        help="Read one installed UTF-8 resource.",
    )
    resource_read.add_argument("name")
    resource_export = resource_commands.add_parser(
        "export",
        help="Export one installed resource to a local file.",
    )
    resource_export.add_argument("name")
    resource_export.add_argument("destination", type=Path)
    resource_export.add_argument(
        "--overwrite",
        action="store_true",
        help="Atomically replace an existing destination.",
    )

    snapshot = commands.add_parser("snapshot", help="Inspect snapshot files.")
    snapshot_commands = snapshot.add_subparsers(
        dest="snapshot_command",
        required=True,
    )
    for command, help_text in (
        ("validate", "Validate a snapshot and print its summary."),
        ("stats", "Print snapshot counts."),
    ):
        subcommand = snapshot_commands.add_parser(command, help=help_text)
        subcommand.add_argument("snapshot", type=Path)

    for command, help_text in (
        ("audit", "Audit memory-run completion state."),
        ("metrics", "Print memory and recovery metrics."),
        ("remediation", "List safe memory-run remediation actions."),
    ):
        subcommand = commands.add_parser(command, help=help_text)
        subcommand.add_argument("snapshot", type=Path)

    lessons = commands.add_parser(
        "lessons",
        help="Import and export active lessons YAML.",
    )
    lesson_commands = lessons.add_subparsers(
        dest="lessons_command",
        required=True,
    )
    lesson_export = lesson_commands.add_parser(
        "export",
        help="Export active lessons from a snapshot.",
    )
    lesson_export.add_argument("snapshot", type=Path)
    lesson_export.add_argument("destination", type=Path)
    lesson_export.add_argument(
        "--overwrite",
        action="store_true",
        help="Atomically replace an existing destination.",
    )
    lesson_import = lesson_commands.add_parser(
        "import",
        help="Validate and import active lessons into a snapshot.",
    )
    lesson_import.add_argument("snapshot", type=Path)
    lesson_import.add_argument(
        "source_yaml",
        type=Path,
        metavar="SOURCE_YAML",
    )
    lesson_import.add_argument(
        "--write",
        action="store_true",
        help="Atomically replace the snapshot after a successful import.",
    )

    obsolete = commands.add_parser(
        "obsolete",
        help="Make one failure case, lesson, or project policy obsolete.",
    )
    obsolete.add_argument("snapshot", type=Path)
    obsolete.add_argument(
        "memory_kind",
        choices=("failure-case", "lesson", "project-policy"),
        metavar="{failure-case,lesson,project-policy}",
    )
    obsolete.add_argument("memory_id", metavar="MEMORY_ID")
    obsolete.add_argument(
        "--write",
        action="store_true",
        help="Atomically replace the snapshot after successful obsolescence.",
    )

    obsolete_batch = commands.add_parser(
        "obsolete-batch",
        help="Atomically make a manifest of memories obsolete.",
    )
    obsolete_batch.add_argument("snapshot", type=Path)
    obsolete_batch.add_argument(
        "requests_json",
        type=Path,
        metavar="REQUESTS_JSON",
    )
    obsolete_batch.add_argument(
        "--write",
        action="store_true",
        help="Atomically replace the snapshot after successful obsolescence.",
    )

    pr_report = commands.add_parser(
        "pr-report",
        help="Generate an endpoint-aware, ancestry-filtered PR report.",
    )
    pr_report.add_argument("snapshot", type=Path)
    pr_report.add_argument(
        "context_json",
        type=Path,
        metavar="CONTEXT_JSON",
    )
    pr_report.add_argument(
        "change_set_json",
        type=Path,
        metavar="CHANGE_SET_JSON",
    )
    pr_report.add_argument(
        "--repo-path",
        type=Path,
        required=True,
        metavar="REPO_PATH",
        help="Git repository containing the current and source commits.",
    )

    complete = commands.add_parser(
        "complete",
        help="Complete one memory run with a measured result.",
    )
    complete.add_argument("snapshot", type=Path)
    complete.add_argument("trace_id")
    complete.add_argument("decision_id")
    complete.add_argument(
        "--eval-result",
        choices=("pass", "fail", "error"),
        required=True,
    )
    complete.add_argument(
        "--memory-caused-failure",
        type=_parse_boolean,
        default=False,
        metavar="true|false",
    )
    complete.add_argument("--output-hash")
    complete.add_argument("--tool-outputs-file", type=Path)
    complete.add_argument("--latency-ms", type=int)
    complete.add_argument("--cost-usd", type=_parse_finite_float)
    complete.add_argument("--error")
    complete.add_argument("--trace-uri")
    complete.add_argument(
        "--write",
        action="store_true",
        help="Atomically replace the snapshot after successful completion.",
    )

    complete_batch = commands.add_parser(
        "complete-batch",
        help="Atomically complete an ordered batch of measured memory runs.",
    )
    complete_batch.add_argument("snapshot", type=Path)
    complete_batch.add_argument(
        "measurements_json",
        type=Path,
        metavar="MEASUREMENTS_JSON",
    )
    complete_batch.add_argument(
        "--write",
        action="store_true",
        help="Atomically replace the snapshot after successful completion.",
    )

    recover_ready = commands.add_parser(
        "recover-ready",
        help="Recover every run that needs no attribution input.",
    )
    recover_ready.add_argument("snapshot", type=Path)
    recover_ready.add_argument(
        "--write",
        action="store_true",
        help="Atomically replace the snapshot after successful recovery.",
    )

    recover = commands.add_parser(
        "recover",
        help="Recover one eligible memory run.",
    )
    recover.add_argument("snapshot", type=Path)
    recover.add_argument("decision_id")
    recover.add_argument(
        "--memory-caused-failure",
        type=_parse_boolean,
        default=None,
        metavar="true|false",
    )
    recover.add_argument(
        "--write",
        action="store_true",
        help="Atomically replace the snapshot after successful recovery.",
    )

    recover_batch = commands.add_parser(
        "recover-batch",
        help="Atomically recover an ordered batch of eligible memory runs.",
    )
    recover_batch.add_argument("snapshot", type=Path)
    recover_batch.add_argument("decision_ids", nargs="+")
    recover_batch.add_argument(
        "--attribution",
        action="append",
        default=[],
        metavar="DECISION_ID=true|false",
    )
    recover_batch.add_argument(
        "--write",
        action="store_true",
        help="Atomically replace the snapshot after successful recovery.",
    )
    return parser


def _json_text(payload: object) -> str:
    return json.dumps(
        payload,
        allow_nan=False,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
    ) + "\n"


def _write_text(stream: TextIO, value: str) -> None:
    stream.write(value)
    stream.flush()


def _write_json(stream: TextIO, payload: object) -> None:
    _write_text(stream, _json_text(payload))


def _error_message(error: BaseException) -> str:
    message = str(error)
    if len(message) <= _ERROR_MESSAGE_MAX_CHARS:
        return message
    return message[: _ERROR_MESSAGE_MAX_CHARS - 3] + "..."


def _emit_error(kind: str, error: BaseException, exit_code: int) -> int:
    try:
        _write_json(
            sys.stderr,
            {
                "error": {
                    "kind": kind,
                    "message": _error_message(error),
                    "type": type(error).__name__,
                }
            },
        )
    except Exception:
        pass
    return exit_code


def _snapshot_summary(store: TraceBackedMemoryStore) -> dict[str, object]:
    snapshot = store.to_snapshot()
    collection_names = (
        "failure_cases",
        "lessons",
        "project_policies",
        "traces",
        "usage_logs",
    )
    return {
        "counts": {name: len(snapshot[name]) for name in collection_names},
        "snapshot_version": snapshot["snapshot_version"],
    }


def _parse_attributions(
    values: Sequence[str],
    requested_ids: Sequence[str],
) -> dict[str, bool]:
    requested = set(requested_ids)
    attributions: dict[str, bool] = {}
    for value in values:
        decision_id, separator, raw_attribution = value.partition("=")
        if not separator or not decision_id or not raw_attribution:
            raise CLIInputError(
                "attribution must use DECISION_ID=true|false"
            )
        if decision_id not in requested:
            raise CLIInputError(
                f"attribution provided for unrequested decision_id: {decision_id}"
            )
        if decision_id in attributions:
            raise CLIInputError(
                f"duplicate attribution for decision_id: {decision_id}"
            )
        try:
            attributions[decision_id] = _parse_boolean(raw_attribution)
        except argparse.ArgumentTypeError as error:
            raise CLIInputError(
                f"invalid attribution for decision_id {decision_id}: {error}"
            ) from error
    return attributions


def _load_json_file(path: Path, description: str) -> Any:
    try:
        source = read_bounded_utf8(
            path,
            max_bytes=CLI_JSON_FILE_MAX_BYTES,
            description=description,
        )
    except UnicodeDecodeError as error:
        raise CLIInputError(
            f"{description} file must be UTF-8: {path}"
        ) from error
    except OSError as error:
        raise CLIInputError(
            f"cannot read {description} file {path}: {error}"
        ) from error
    except ValueError as error:
        raise CLIInputError(str(error)) from error

    def reject_non_finite(value: str) -> Any:
        raise CLIInputError(
            f"{description} JSON contains non-finite number: {value}"
        )

    def unique_object(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
        result: dict[str, Any] = {}
        for key, value in pairs:
            if key in result:
                raise CLIInputError(
                    f"{description} JSON contains duplicate object key: {key}"
                )
            result[key] = value
        return result

    try:
        payload: Any = json.loads(
            source,
            parse_constant=reject_non_finite,
            object_pairs_hook=unique_object,
        )
    except (json.JSONDecodeError, RecursionError) as error:
        raise CLIInputError(
            f"invalid {description} JSON in {path}: {error}"
        ) from error

    node_count = 0
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > CLI_JSON_MAX_DEPTH:
            raise CLIInputError(
                f"{description} JSON exceeds maximum depth of "
                f"{CLI_JSON_MAX_DEPTH}"
            )
        node_count += 1
        if node_count > CLI_JSON_MAX_NODES:
            raise CLIInputError(
                f"{description} JSON contains more than "
                f"{CLI_JSON_MAX_NODES} nodes"
            )
        if type(value) is float and not math.isfinite(value):
            raise CLIInputError(
                f"{description} JSON contains non-finite number"
            )
        if type(value) is list:
            pending.extend((item, depth + 1) for item in value)
        elif type(value) is dict:
            pending.extend((item, depth + 1) for item in value.values())
    return payload


def _load_pr_context(path: Path) -> MemoryContext:
    payload = _load_json_file(path, "PR context")
    if type(payload) is not dict:
        raise CLIInputError("PR context JSON must be an object")

    unknown_fields = sorted(set(payload) - _MEMORY_CONTEXT_FIELDS)
    if unknown_fields:
        raise CLIInputError(
            f"PR context has unknown field: {unknown_fields[0]}"
        )
    for field_name in _MEMORY_CONTEXT_REQUIRED_FIELDS:
        if field_name not in payload:
            raise CLIInputError(
                f"PR context missing required field: {field_name}"
            )

    context = MemoryContext(**payload)
    try:
        validate_memory_context(context)
    except ValueError as error:
        raise CLIInputError(str(error)) from error
    return context


def _load_lessons_yaml(
    store: TraceBackedMemoryStore,
    path: Path,
) -> list[Lesson]:
    try:
        return store.load_lessons_yaml(path)
    except UnicodeDecodeError as error:
        raise CLIInputError(
            f"active lessons YAML must be UTF-8: {path}"
        ) from error
    except OSError as error:
        raise CLIInputError(
            f"cannot read active lessons YAML file {path}: {error}"
        ) from error
    except (ValueError, TypeError, OverflowError) as error:
        raise CLIInputError(str(error)) from error


def _load_obsolescence_requests(
    path: Path,
) -> tuple[MemoryObsolescenceRequest, ...]:
    payload = _load_json_file(path, "obsolescence requests")
    if type(payload) is not list or not payload:
        raise CLIInputError(
            "obsolescence requests JSON must be a non-empty array"
        )
    if len(payload) > CLI_JSON_MAX_ITEMS:
        raise CLIInputError(
            "obsolescence requests contains more than "
            f"{CLI_JSON_MAX_ITEMS} items"
        )

    required_fields = ("memory_kind", "memory_id")
    allowed_kinds = {"failure_case", "lesson", "project_policy"}
    requests: list[MemoryObsolescenceRequest] = []
    for index, item in enumerate(payload, start=1):
        if type(item) is not dict:
            raise CLIInputError(f"request {index} must be an object")
        unknown_fields = sorted(set(item) - set(required_fields))
        if unknown_fields:
            raise CLIInputError(
                f"request {index} has unknown field: {unknown_fields[0]}"
            )
        for field_name in required_fields:
            if field_name not in item:
                raise CLIInputError(
                    f"request {index} missing required field: {field_name}"
                )

        memory_kind = item["memory_kind"]
        memory_id = item["memory_id"]
        if type(memory_kind) is not str:
            raise CLIInputError(
                f"request {index} memory_kind must be a string"
            )
        if memory_kind not in allowed_kinds:
            raise CLIInputError(
                f"request {index} has unsupported memory_kind: {memory_kind}"
            )
        if type(memory_id) is not str:
            raise CLIInputError(
                f"request {index} memory_id must be a string"
            )
        requests.append(
            MemoryObsolescenceRequest(
                memory_kind=memory_kind,
                memory_id=memory_id,
            )
        )
    return tuple(requests)


def _load_pr_change_set(path: Path) -> PRChangeSet:
    payload = _load_json_file(path, "PR change set")
    if type(payload) is not dict:
        raise CLIInputError("PR change set JSON must be an object")

    unknown_fields = sorted(set(payload) - {"field_changes"})
    if unknown_fields:
        raise CLIInputError(
            f"PR change set has unknown field: {unknown_fields[0]}"
        )
    if "field_changes" not in payload:
        raise CLIInputError(
            "PR change set missing required field: field_changes"
        )
    field_changes = payload["field_changes"]
    if type(field_changes) is not list or not field_changes:
        raise CLIInputError("field_changes must be a non-empty array")
    if len(field_changes) > CLI_JSON_MAX_ITEMS:
        raise CLIInputError(
            "field_changes contains more than "
            f"{CLI_JSON_MAX_ITEMS} items"
        )

    required_fields = ("field_name", "old_value", "new_value")
    changes: list[tuple[str, str | None, str | None]] = []
    for index, item in enumerate(field_changes, start=1):
        if type(item) is not dict:
            raise CLIInputError(f"change {index} must be an object")
        unknown_item_fields = sorted(set(item) - set(required_fields))
        if unknown_item_fields:
            raise CLIInputError(
                f"change {index} has unknown field: {unknown_item_fields[0]}"
            )
        for field_name in required_fields:
            if field_name not in item:
                raise CLIInputError(
                    f"change {index} missing required field: {field_name}"
                )

        field_name = item["field_name"]
        if type(field_name) is not str:
            raise CLIInputError(
                f"change {index} field_name must be a string"
            )
        old_value = item["old_value"]
        new_value = item["new_value"]
        for value_name, value in (
            ("old_value", old_value),
            ("new_value", new_value),
        ):
            if value is not None and type(value) is not str:
                raise CLIInputError(
                    f"change {index} {value_name} must be a string or null"
                )
        changes.append((field_name, old_value, new_value))
    return PRChangeSet(tuple(changes))


def _load_tool_outputs(path: Path) -> list[dict[str, object]]:
    payload = _load_json_file(path, "tool outputs")

    if type(payload) is not list:
        raise CLIInputError("tool outputs JSON must be an array of objects")
    if len(payload) > CLI_JSON_MAX_ITEMS:
        raise CLIInputError(
            "tool outputs JSON contains more than "
            f"{CLI_JSON_MAX_ITEMS} items"
        )
    if any(type(item) is not dict for item in payload):
        raise CLIInputError("tool outputs JSON array items must be objects")
    return payload


def _load_memory_run_results(path: Path) -> tuple[MemoryRunResult, ...]:
    payload = _load_json_file(path, "measurements")
    if type(payload) is not list or not payload:
        raise CLIInputError(
            "measurements JSON must be a non-empty array of objects"
        )
    if len(payload) > CLI_JSON_MAX_ITEMS:
        raise CLIInputError(
            "measurements JSON contains more than "
            f"{CLI_JSON_MAX_ITEMS} items"
        )
    if any(type(item) is not dict for item in payload):
        raise CLIInputError("measurements JSON array items must be objects")

    required_fields = ("decision_id", "eval_result")
    optional_string_fields = (
        "output_hash",
        "error",
        "trace_uri",
    )
    allowed_fields = {
        *required_fields,
        "memory_caused_failure",
        *optional_string_fields,
        "tool_outputs",
        "latency_ms",
        "cost_usd",
    }
    results: list[MemoryRunResult] = []
    for index, item in enumerate(payload, start=1):
        unknown_fields = sorted(set(item) - allowed_fields)
        if unknown_fields:
            raise CLIInputError(
                f"measurement {index} has unknown field: {unknown_fields[0]}"
            )
        for field_name in required_fields:
            if field_name not in item:
                raise CLIInputError(
                    f"measurement {index} missing required field: {field_name}"
                )

        if type(item["decision_id"]) is not str:
            raise CLIInputError(
                f"measurement {index} decision_id must be a string"
            )
        if type(item["eval_result"]) is not str:
            raise CLIInputError(
                f"measurement {index} eval_result must be a string"
            )
        if (
            "memory_caused_failure" in item
            and type(item["memory_caused_failure"]) is not bool
        ):
            raise CLIInputError(
                f"measurement {index} memory_caused_failure must be a boolean"
            )
        for field_name in optional_string_fields:
            value = item.get(field_name)
            if value is not None and type(value) is not str:
                raise CLIInputError(
                    f"measurement {index} {field_name} must be a string or null"
                )

        tool_outputs = item.get("tool_outputs")
        if tool_outputs is not None and (
            type(tool_outputs) is not list
            or any(type(output) is not dict for output in tool_outputs)
        ):
            raise CLIInputError(
                f"measurement {index} tool_outputs must be an array "
                "of objects or null"
            )
        latency_ms = item.get("latency_ms")
        if latency_ms is not None and type(latency_ms) is not int:
            raise CLIInputError(
                f"measurement {index} latency_ms must be an integer or null"
            )
        cost_usd = item.get("cost_usd")
        if cost_usd is not None and (
            type(cost_usd) not in (int, float)
            or (type(cost_usd) is float and not math.isfinite(cost_usd))
        ):
            raise CLIInputError(
                f"measurement {index} cost_usd must be a finite number or null"
            )

        values = dict(item)
        if tool_outputs is not None:
            values["tool_outputs"] = tuple(tool_outputs)
        results.append(MemoryRunResult(**values))
    return tuple(results)


def _completion_evidence(args: argparse.Namespace) -> dict[str, object]:
    evidence = {
        field_name: getattr(args, field_name)
        for field_name in (
            "output_hash",
            "latency_ms",
            "cost_usd",
            "error",
            "trace_uri",
        )
        if getattr(args, field_name) is not None
    }
    if args.tool_outputs_file is not None:
        evidence["tool_outputs"] = _load_tool_outputs(args.tool_outputs_file)
    return evidence


def _completion_payload(
    completions: Sequence[MemoryRunCompletion],
    *,
    written: bool,
) -> dict[str, object]:
    serialized = [asdict(completion) for completion in completions]
    return {
        "completions": serialized,
        "decision_ids": [
            completion["usage_log"]["decision_id"]
            for completion in serialized
        ],
        "written": written,
    }


def _obsolescence_payload(
    args: argparse.Namespace,
    store: TraceBackedMemoryStore,
) -> dict[str, object]:
    cascaded_lesson_ids: list[str] = []
    if args.memory_kind == "failure-case":
        before = store.failure_cases.get(args.memory_id)
        active_dependents = {
            lesson_id
            for lesson_id, lesson in store.lessons.items()
            if lesson.source_case_id == args.memory_id
            and lesson.status == "active"
        }
        obsolete = store.obsolete_failure_case(args.memory_id)
        after_lessons = store.lessons
        cascaded_lesson_ids = sorted(
            lesson_id
            for lesson_id in active_dependents
            if after_lessons[lesson_id].status == "obsolete"
        )
    elif args.memory_kind == "lesson":
        before = store.lessons.get(args.memory_id)
        obsolete = store.obsolete_lesson(args.memory_id)
    else:
        before = store.project_policies.get(args.memory_id)
        obsolete = store.obsolete_project_policy(args.memory_id)

    if before is None:
        raise RuntimeError("Store obsolescence returned an unknown record")
    return {
        "cascaded_count": len(cascaded_lesson_ids),
        "cascaded_lesson_ids": cascaded_lesson_ids,
        "changed": before.status != obsolete.status,
        "memory_id": args.memory_id,
        "memory_kind": args.memory_kind.replace("-", "_"),
        "previous_status": before.status,
        "status": obsolete.status,
        "written": args.write,
    }


def _batch_obsolescence_payload(
    args: argparse.Namespace,
    store: TraceBackedMemoryStore,
) -> dict[str, object]:
    requests = _load_obsolescence_requests(args.requests_json)
    collections = {
        "failure_case": store.failure_cases,
        "lesson": store.lessons,
        "project_policy": store.project_policies,
    }
    before_records = tuple(
        collections[request.memory_kind].get(request.memory_id)
        for request in requests
    )
    cascading_case_ids = {
        request.memory_id
        for request, before in zip(requests, before_records)
        if request.memory_kind == "failure_case"
        and before is not None
        and before.status != "obsolete"
    }
    entry_active_dependents = {
        lesson_id
        for lesson_id, lesson in collections["lesson"].items()
        if lesson.source_case_id in cascading_case_ids
        and lesson.status == "active"
    }

    obsolete_records = store.obsolete_memories(requests)
    if any(before is None for before in before_records):
        raise RuntimeError("Store batch obsolescence returned an unknown record")

    results: list[dict[str, object]] = []
    changed_ids: set[str] = set()
    for request, before, obsolete in zip(
        requests,
        before_records,
        obsolete_records,
    ):
        if before is None:
            raise RuntimeError(
                "Store batch obsolescence returned an unknown record"
            )
        changed = before.status != obsolete.status
        if changed:
            changed_ids.add(request.memory_id)
        results.append(
            {
                "changed": changed,
                "memory_id": request.memory_id,
                "memory_kind": request.memory_kind,
                "previous_status": before.status,
                "status": obsolete.status,
            }
        )

    after_lessons = store.lessons
    cascaded_lesson_ids = sorted(
        lesson_id
        for lesson_id in entry_active_dependents
        if after_lessons[lesson_id].status == "obsolete"
    )
    return {
        "affected_count": len(changed_ids | set(cascaded_lesson_ids)),
        "cascaded_count": len(cascaded_lesson_ids),
        "cascaded_lesson_ids": cascaded_lesson_ids,
        "changed_count": len(changed_ids),
        "requested_count": len(requests),
        "results": results,
        "written": args.write,
    }


def _packaged_resource(name: str) -> PackagedResource:
    read_packaged_resource(name)
    for resource in packaged_resources():
        if resource.name == name:
            return resource
    raise PackagedResourceError("lookup", name=name)


def _run_resource_command(args: argparse.Namespace) -> int:
    wrote_resource = False
    try:
        if args.resource_command == "list":
            payload: object = {
                "resources": [
                    asdict(resource) for resource in packaged_resources()
                ]
            }
        else:
            resource = _packaged_resource(args.name)
            if args.resource_command == "read":
                payload = {
                    "resource": asdict(resource),
                    "text": read_packaged_resource(args.name).decode("utf-8"),
                }
            else:
                payload = {
                    "destination": str(args.destination),
                    "overwrite": args.overwrite,
                    "resource": asdict(resource),
                }
        output = _json_text(payload)
        if args.resource_command == "export":
            export_packaged_resource(
                args.name,
                args.destination,
                overwrite=args.overwrite,
            )
            wrote_resource = True
    except PackagedResourceError as error:
        if error.operation == "lookup":
            return _emit_error("input", error, 2)
        if error.operation == "export":
            return _emit_error("write", error, 4)
        return _emit_error("internal", error, 1)
    except Exception as error:
        return _emit_error("internal", error, 1)

    try:
        _write_text(sys.stdout, output)
    except Exception as error:
        if wrote_resource:
            return 0
        return _emit_error("internal", error, 1)
    return 0


def _execute(
    args: argparse.Namespace,
    store: TraceBackedMemoryStore,
) -> dict[str, Any] | list[dict[str, Any]]:
    if args.command == "snapshot":
        summary = _snapshot_summary(store)
        if args.snapshot_command == "validate":
            return {"valid": True, **summary}
        return summary

    if args.command == "audit":
        return [asdict(audit) for audit in store.memory_run_audits()]

    if args.command == "metrics":
        return {
            "memory": asdict(store.metrics()),
            "memory_outcomes": [
                asdict(metrics) for metrics in store.memory_outcome_metrics()
            ],
            "memory_runs": asdict(store.memory_run_metrics()),
        }

    if args.command == "remediation":
        return [
            asdict(remediation)
            for remediation in store.memory_run_remediations()
        ]

    if args.command == "lessons":
        if args.lessons_command == "export":
            try:
                destination_is_snapshot = args.snapshot.samefile(
                    args.destination
                )
            except FileNotFoundError:
                destination_is_snapshot = False
            except OSError as error:
                raise CLIInputError(
                    "cannot compare lesson export destination with snapshot: "
                    f"{error}"
                ) from error
            if destination_is_snapshot:
                raise CLIInputError(
                    "lesson export destination must differ from snapshot"
                )
            exported_ids = [
                lesson.lesson_id
                for lesson in store.lessons.values()
                if lesson.status == "active"
            ]
            return {
                "destination": str(args.destination),
                "exported_count": len(exported_ids),
                "exported_lesson_ids": exported_ids,
                "overwrite": args.overwrite,
            }
        imported_lessons = _load_lessons_yaml(store, args.source_yaml)
        return {
            "imported_count": len(imported_lessons),
            "imported_lesson_ids": [
                lesson.lesson_id for lesson in imported_lessons
            ],
            "written": args.write,
        }

    if args.command == "obsolete":
        return _obsolescence_payload(args, store)

    if args.command == "obsolete-batch":
        return _batch_obsolescence_payload(args, store)

    if args.command == "pr-report":
        context = _load_pr_context(args.context_json)
        change_set = _load_pr_change_set(args.change_set_json)
        try:
            anchors = store.pr_report_commit_anchors(
                context,
                change_set=change_set,
            )
        except ValueError as error:
            raise CLIInputError(str(error)) from error
        commit_ancestry = capture_commit_ancestry(
            context.commit_sha,
            anchors,
            repo_path=str(args.repo_path),
        )
        report = store.pr_memory_report(
            context,
            change_set=change_set,
            commit_ancestry=commit_ancestry,
        )
        return {
            "commit_ancestry": asdict(commit_ancestry),
            "report": asdict(report),
        }

    if args.command == "complete":
        completion = store.complete_memory_run(
            trace_id=args.trace_id,
            decision_id=args.decision_id,
            eval_result=args.eval_result,
            memory_caused_failure=args.memory_caused_failure,
            **_completion_evidence(args),
        )
        return _completion_payload((completion,), written=args.write)

    if args.command == "complete-batch":
        return _completion_payload(
            store.complete_memory_runs(
                _load_memory_run_results(args.measurements_json)
            ),
            written=args.write,
        )

    if args.command == "recover-ready":
        return _completion_payload(
            store.recover_ready_memory_runs(),
            written=args.write,
        )

    if args.command == "recover":
        if args.memory_caused_failure is None:
            completion = store.recover_memory_run(args.decision_id)
        else:
            completion = store.recover_memory_run(
                args.decision_id,
                memory_caused_failure=args.memory_caused_failure,
            )
        return _completion_payload((completion,), written=args.write)

    decision_ids = tuple(args.decision_ids)
    if len(set(decision_ids)) != len(decision_ids):
        raise CLIInputError("recover-batch decision_ids must be unique")
    attributions = _parse_attributions(args.attribution, decision_ids)
    return _completion_payload(
        store.recover_memory_runs(
            decision_ids,
            memory_caused_failures=attributions or None,
        ),
        written=args.write,
    )


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    try:
        args = parser.parse_args(argv)
    except CLIUsageError as error:
        return _emit_error("input", error, 2)

    if args.command == "resource":
        return _run_resource_command(args)

    try:
        store = TraceBackedMemoryStore.load_json(args.snapshot)
    except (OSError, UnicodeError, ValueError, TypeError, OverflowError) as error:
        return _emit_error("input", error, 2)
    except Exception as error:
        return _emit_error("internal", error, 1)

    try:
        payload = _execute(args, store)
    except CLIInputError as error:
        return _emit_error("input", error, 2)
    except CommitAncestryCaptureError as error:
        return _emit_error("state", error, 3)
    except ValueError as error:
        return _emit_error("state", error, 3)
    except Exception as error:
        return _emit_error("internal", error, 1)

    try:
        output = _json_text(payload)
    except Exception as error:
        return _emit_error("internal", error, 1)

    published = False
    if args.command == "lessons" and args.lessons_command == "export":
        try:
            store.save_lessons_yaml(
                args.destination,
                overwrite=args.overwrite,
            )
        except Exception as error:
            return _emit_error("write", error, 4)
        published = True
    elif getattr(args, "write", False):
        try:
            store.save_json(args.snapshot)
        except Exception as error:
            return _emit_error("write", error, 4)
        published = True

    try:
        _write_text(sys.stdout, output)
    except Exception as error:
        if published:
            return 0
        return _emit_error("internal", error, 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
