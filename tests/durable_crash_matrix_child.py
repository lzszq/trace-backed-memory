from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
import os
from pathlib import Path

from tests.durable_event_first_support import open_event_first_runtime
from tests.test_durable_agent_v3 import _completion
from tests.test_durable_agent_wire_v1 import (
    _decide_request,
    _prepare_request,
)
from tests.test_durable_execution_v3 import EVALUATOR_CONTEXT
from tests.test_durable_semantic_gate_v3 import _context as _provider_context
from trace_backed_memory.durable_agent_wire_v1 import (
    DurableCompleteRequest,
    DurableFinalizeRequest,
    DurableStartRequest,
)
from trace_backed_memory.durable_runtime_v3 import DurableSQLiteRuntime


CRASH_EXIT_CODE = 86
STAGES = (
    "auth",
    "created",
    "retrieval_evidence",
    "prepared",
    "provider_call",
    "decided",
    "replay_retention",
    "finalized",
    "executing",
    "outcome",
    "outbox",
)


class _ExitAfterCommitGuard:
    def __init__(self, guard: object) -> None:
        self._guard = guard

    def __enter__(self) -> object:
        return self._guard.__enter__()

    def __exit__(self, *args: object) -> object:
        result = self._guard.__exit__(*args)
        if args[0] is None:
            os._exit(CRASH_EXIT_CODE)
        return result


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser()
    parser.add_argument("--database", type=Path, required=True)
    parser.add_argument("--stage", choices=STAGES, required=True)
    parser.add_argument(
        "--mode",
        choices=("precommit", "response_lost"),
        required=True,
    )
    parser.add_argument("--session-id")
    return parser


def _exit_after(value: object) -> object:
    del value
    os._exit(CRASH_EXIT_CODE)


def _wrap_after(
    target: object,
    name: str,
    predicate: Callable[..., bool] | None = None,
) -> None:
    original = getattr(target, name)

    def wrapped(*args: object, **kwargs: object) -> object:
        value = original(*args, **kwargs)
        if predicate is None or predicate(*args, **kwargs):
            return _exit_after(value)
        return value

    setattr(target, name, wrapped)


def _install_precommit_fault(
    runtime: DurableSQLiteRuntime,
    stage: str,
) -> None:
    if stage == "auth":
        _wrap_after(runtime.authorization_repository, "append_decision")
        return
    if stage in {"created", "prepared", "decided", "finalized", "executing"}:
        status = stage

        def matches_status(*args: object, **_kwargs: object) -> bool:
            return getattr(args[1], "status", None) == status

        _wrap_after(
            runtime.gate_session_event_projector,
            "append_and_reduce",
            matches_status,
        )
        return
    if stage == "retrieval_evidence":
        _wrap_after(runtime.evidence_repository, "store_bundle")
        return
    if stage == "provider_call":
        semantic_session = runtime.service_bundle.semantic_service
        semantic_service = semantic_session._semantic_gate_service  # noqa: SLF001
        original = semantic_service.invoke

        def invoke(
            context: object,
            request: object,
            call_provider: Callable[[object], object],
        ) -> object:
            def call_then_exit(call: object) -> object:
                return _exit_after(call_provider(call))

            return original(context, request, call_then_exit)

        semantic_service.invoke = invoke
        return
    if stage == "replay_retention":
        _wrap_after(runtime.replay_repository, "store_complete_bundle")
        return
    if stage == "outcome":
        projector = runtime.outcome_effect_event_projector
        if projector is None:
            raise AssertionError("Outcome/Effect projector is missing")
        _wrap_after(projector, "append_completion")
        return
    if stage == "outbox":
        _wrap_after(runtime.outbox_repository, "_insert_bundle")
        return
    raise AssertionError(f"unhandled crash stage: {stage}")


def _invoke(
    runtime: DurableSQLiteRuntime,
    context: object,
    stage: str,
    session_id: str | None,
) -> None:
    if stage in {"auth", "created", "retrieval_evidence", "prepared"}:
        runtime.dispatcher.prepare(context, _prepare_request())
        return
    if session_id is None:
        raise ValueError("session_id is required after prepare")
    session = runtime.sessions.get(session_id)
    if stage in {"provider_call", "decided"}:
        evaluation = runtime.evidence_repository.load_evaluation(
            session.system_gate_evaluation_id
        )
        runtime.dispatcher.decide(
            context,
            _provider_context(),
            _decide_request(session, evaluation),
        )
        return
    if stage in {"replay_retention", "finalized"}:
        runtime.dispatcher.finalize(
            context,
            DurableFinalizeRequest(
                session_id=session_id,
                expected_session_version=session.version,
            ),
        )
        return
    if stage == "executing":
        runtime.dispatcher.start(
            context,
            DurableStartRequest(
                session_id=session_id,
                expected_session_version=session.version,
            ),
        )
        return
    if stage in {"outcome", "outbox"}:
        completion = _completion(session)
        runtime.dispatcher.complete(
            context,
            EVALUATOR_CONTEXT,
            DurableCompleteRequest(
                session_id=completion.session_id,
                expected_session_version=completion.expected_version,
                result=completion.result,
                evidence_artifact_sha256s=list(
                    completion.evidence_artifact_sha256s
                ),
                output_sha256=completion.output_sha256,
                tool_outputs_sha256=completion.tool_outputs_sha256,
                latency_ms=completion.latency_ms,
                cost_usd=completion.cost_usd,
                error_code=completion.error_code,
            ),
        )
        return
    raise AssertionError(f"unhandled crash stage: {stage}")


def main(argv: Sequence[str] | None = None) -> int:
    args = _build_parser().parse_args(argv)
    prefix = f"crash_{args.stage}_{args.mode}"
    runtime, context = open_event_first_runtime(
        args.database,
        initialize=False,
        identifier_prefix=prefix,
        clock_advance_seconds=10,
    )
    try:
        if args.mode == "precommit":
            _install_precommit_fault(runtime, args.stage)
        else:
            guard = runtime.dispatcher._operation_lock  # noqa: SLF001
            runtime.dispatcher._operation_lock = (  # noqa: SLF001
                _ExitAfterCommitGuard(guard)
            )
        _invoke(runtime, context, args.stage, args.session_id)
    finally:
        runtime.close()
    return 3


if __name__ == "__main__":
    raise SystemExit(main())
