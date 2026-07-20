from __future__ import annotations

import argparse
import json
import math
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence, TextIO

from ._ingestion import (
    CLI_JSON_FILE_MAX_BYTES,
    CLI_JSON_MAX_DEPTH,
    CLI_JSON_MAX_ITEMS,
    CLI_JSON_MAX_NODES,
    read_bounded_utf8,
)
from .models import MemoryRunCompletion, MemoryRunResult
from .resources import (
    PackagedResource,
    PackagedResourceError,
    export_packaged_resource,
    packaged_resources,
    read_packaged_resource,
)
from .store import TraceBackedMemoryStore


_ERROR_MESSAGE_MAX_CHARS = 2048


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
        description="Operate trace-backed-memory snapshots and resources.",
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
    except ValueError as error:
        return _emit_error("state", error, 3)
    except Exception as error:
        return _emit_error("internal", error, 1)

    try:
        output = _json_text(payload)
    except Exception as error:
        return _emit_error("internal", error, 1)

    if getattr(args, "write", False):
        try:
            store.save_json(args.snapshot)
        except Exception as error:
            return _emit_error("write", error, 4)

    try:
        _write_text(sys.stdout, output)
    except Exception as error:
        if getattr(args, "write", False):
            return 0
        return _emit_error("internal", error, 1)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
