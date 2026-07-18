from __future__ import annotations

import argparse
import json
import sys
from dataclasses import asdict
from pathlib import Path
from typing import Any, Sequence, TextIO

from .models import MemoryRunCompletion
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


def _build_parser() -> argparse.ArgumentParser:
    parser = _JSONArgumentParser(
        prog="tbm",
        description="Inspect and recover trace-backed-memory snapshots.",
    )
    commands = parser.add_subparsers(dest="command", required=True)

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


def _recovery_payload(
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

    if args.command == "recover-ready":
        return _recovery_payload(
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
        return _recovery_payload((completion,), written=args.write)

    decision_ids = tuple(args.decision_ids)
    if len(set(decision_ids)) != len(decision_ids):
        raise CLIInputError("recover-batch decision_ids must be unique")
    attributions = _parse_attributions(args.attribution, decision_ids)
    return _recovery_payload(
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
