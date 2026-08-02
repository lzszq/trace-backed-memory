from __future__ import annotations

import argparse
from collections.abc import Callable, Sequence
from contextlib import contextmanager
from dataclasses import dataclass, field
import importlib
import json
import math
import os
from pathlib import Path
import secrets
import signal
import sys
from threading import Event, Thread, current_thread, main_thread
from types import FrameType
from typing import Iterator, NoReturn

from .durable_http_server import (
    DurableAgentHTTPServer,
    DurableBearerAuthenticator,
    DurableHTTPAuthenticatedContexts,
    DurableHTTPAuthenticationRequest,
    DurableHTTPServerConfiguration,
)
from .durable_mcp_server import (
    DurableMCPTrustedContexts,
    create_durable_mcp_server,
)
from .durable_runtime_v3 import (
    DurableRuntimeDependencies,
    DurableRuntimeFactory,
    DurableRuntimeV3Error,
    DurableSQLiteRuntime,
)
from .durable_sdk import (
    DurableAgentHTTPClient,
    DurableAgentHTTPClientError,
)
from .event_registry_v1 import DEFAULT_EVENT_TYPE_REGISTRY
from .local_daemon_v3 import (
    LOCAL_DAEMON_CONTRACT_VERSION,
    DurableLocalWorkerLoop,
    LocalDaemonV3Error,
    LocalDaemonWorkerConfiguration,
    local_daemon_lock,
    prepare_local_database,
    prepare_local_state_directory,
    verify_local_database_target,
)
from .ledger_port_v1 import EventLedgerPortError
from .mcp_server import run_stdio_server
from .projection import ProjectionRuntime, ProjectionRuntimeError
from .projection_checkpoint import (
    ProjectionCheckpoint,
    ProjectionCheckpointError,
)
from .reducer import ReducerV1Error
from .reducer_registry import (
    ReducerRegistryError,
    build_default_reducer_registry,
)
from .sqlite_event_ledger_v1 import SQLiteEventLedgerV1


DURABLE_LOCAL_APPLICATION_FACTORY_ENV = (
    "TBM_DURABLE_DAEMON_APPLICATION_FACTORY"
)
DURABLE_LOCAL_TOKEN_ENV = "TBM_DURABLE_HTTP_TOKEN"


class _StartupInputError(ValueError):
    """Bounded local-daemon operator input failure."""


class _JSONArgumentParser(argparse.ArgumentParser):
    """Route malformed command lines through the public JSON error contract."""

    def error(self, _message: str) -> NoReturn:
        raise _StartupInputError("local daemon command line is invalid")


@dataclass(frozen=True)
class DurableLocalApplication:
    """Trusted dependencies and identities shared by local MCP and HTTP."""

    dependencies: DurableRuntimeDependencies
    mcp_contexts: DurableMCPTrustedContexts
    http_context_provider: Callable[
        [DurableHTTPAuthenticationRequest],
        DurableHTTPAuthenticatedContexts,
    ] = field(repr=False)

    def __post_init__(self) -> None:
        if type(self.dependencies) is not DurableRuntimeDependencies:
            raise TypeError(
                "local daemon application dependencies are invalid"
            )
        if type(self.mcp_contexts) is not DurableMCPTrustedContexts:
            raise TypeError(
                "local daemon MCP contexts are invalid"
            )
        if not callable(self.http_context_provider):
            raise TypeError(
                "local daemon HTTP context provider is invalid"
            )
        if self.dependencies.completion_consumer is None:
            raise ValueError(
                "local daemon application requires an outbox consumer"
            )


def _add_factory_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--application-factory",
        help=(
            "Trusted MODULE:CALLABLE returning DurableLocalApplication. "
            f"Defaults to {DURABLE_LOCAL_APPLICATION_FACTORY_ENV}."
        ),
    )


def _add_token_argument(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--token-env",
        default=DURABLE_LOCAL_TOKEN_ENV,
        help="Environment variable containing the durable bearer secret.",
    )


def _add_ledger_target_arguments(parser: argparse.ArgumentParser) -> None:
    parser.add_argument(
        "--database",
        type=Path,
        default=Path(".tbm") / "event-ledger.sqlite3",
        help="Existing owner-controlled SQLite canonical event ledger.",
    )
    parser.add_argument(
        "--partition-sha256",
        help="Required only when the ledger retains multiple tenant partitions.",
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = _JSONArgumentParser(
        prog="tbmd",
        description=(
            "Run and diagnose the local durable Trace-backed Memory daemon."
        ),
    )
    commands = parser.add_subparsers(dest="command", required=True)

    initialize = commands.add_parser(
        "init",
        help="Create and verify one owner-controlled local SQLite v3 state.",
    )
    initialize.add_argument(
        "--state-dir",
        type=Path,
        default=Path(".tbm"),
    )
    _add_factory_argument(initialize)

    local = commands.add_parser(
        "local",
        help="Run one SQLite v3 daemon with MCP, HTTP, and workers.",
    )
    local.add_argument("--state-dir", type=Path, default=Path(".tbm"))
    local.add_argument("--initialize", action="store_true")
    local.add_argument("--host", default="127.0.0.1")
    local.add_argument("--port", type=int, default=8766)
    local.add_argument(
        "--no-mcp",
        action="store_true",
        help="Run HTTP and workers without attaching MCP to STDIO.",
    )
    local.add_argument(
        "--expose-injection-content",
        action="store_true",
    )
    local.add_argument(
        "--expose-replay-content",
        action="store_true",
    )
    local.add_argument(
        "--worker-interval",
        type=float,
        default=1.0,
    )
    local.add_argument(
        "--worker-page-size",
        type=int,
        default=100,
    )
    local.add_argument(
        "--outbox-lease-seconds",
        type=int,
        default=60,
    )
    local.add_argument(
        "--outbox-retry-delay-seconds",
        type=int,
        default=60,
    )
    local.add_argument(
        "--outbox-max-attempts",
        type=int,
        default=5,
    )
    local.add_argument(
        "--lock-timeout",
        type=float,
        default=0.0,
    )
    _add_factory_argument(local)
    _add_token_argument(local)

    doctor = commands.add_parser(
        "doctor",
        help="Verify local state, schema, application, token, and lock.",
    )
    doctor.add_argument("--state-dir", type=Path, default=Path(".tbm"))
    _add_factory_argument(doctor)
    _add_token_argument(doctor)

    health = commands.add_parser(
        "health",
        help="Query the authenticated durable HTTP health route.",
    )
    health.add_argument(
        "--base-url",
        default="http://127.0.0.1:8766",
    )
    health.add_argument(
        "--timeout",
        type=float,
        default=5.0,
    )
    _add_token_argument(health)

    ledger = commands.add_parser(
        "ledger",
        help="Verify or inspect an explicit canonical event ledger.",
    )
    ledger_commands = ledger.add_subparsers(
        dest="ledger_command",
        required=True,
    )
    ledger_verify = ledger_commands.add_parser(
        "verify",
        help="Verify schema, event chains, checkpoints, and projection heads.",
    )
    _add_ledger_target_arguments(ledger_verify)
    ledger_stats = ledger_commands.add_parser(
        "stats",
        help="Return metadata-only ledger and projection counts.",
    )
    _add_ledger_target_arguments(ledger_stats)

    projection = commands.add_parser(
        "projection",
        help="Rebuild and govern replaceable event-ledger projections.",
    )
    projection_commands = projection.add_subparsers(
        dest="projection_command",
        required=True,
    )
    projection_list = projection_commands.add_parser(
        "list",
        help="List retained projection checkpoints and active heads.",
    )
    _add_ledger_target_arguments(projection_list)
    projection_list.add_argument("--projection-name")

    projection_rebuild = projection_commands.add_parser(
        "rebuild",
        help="Build or resume one deterministic shadow projection.",
    )
    _add_ledger_target_arguments(projection_rebuild)
    projection_rebuild.add_argument(
        "reducer_id",
        nargs="?",
        default="canonical-event-inventory",
    )
    projection_rebuild.add_argument("--version", type=int, default=1)
    projection_rebuild.add_argument("--owner", default="tbmd_projection_operator")
    projection_rebuild.add_argument("--generation", type=int, required=True)
    projection_rebuild.add_argument("--page-size", type=int, default=100)
    projection_rebuild.add_argument(
        "--checkpoint-interval",
        type=int,
        default=100,
    )
    projection_rebuild.add_argument("--resume", action="store_true")

    projection_compare = projection_commands.add_parser(
        "compare",
        help="Compare active and shadow projection digests with bounded diffs.",
    )
    _add_ledger_target_arguments(projection_compare)
    projection_compare.add_argument("active_build_id")
    projection_compare.add_argument("shadow_build_id")

    projection_activate = projection_commands.add_parser(
        "activate",
        help="Atomically switch a projection head after explicit approval.",
    )
    _add_ledger_target_arguments(projection_activate)
    projection_activate.add_argument("shadow_build_id")
    projection_activate.add_argument("--owner", default="tbmd_projection_operator")
    projection_activate.add_argument("--approve", action="store_true")
    projection_activate.add_argument("--expected-head-version", type=int)
    projection_activate.add_argument("--expected-current-build-id")

    projection_rollback = projection_commands.add_parser(
        "rollback",
        help="Atomically select a previously active projection build.",
    )
    _add_ledger_target_arguments(projection_rollback)
    projection_rollback.add_argument("projection_name")
    projection_rollback.add_argument("--owner", default="tbmd_projection_operator")
    projection_rollback.add_argument("--expected-head-version", type=int)
    projection_rollback.add_argument("--expected-current-build-id")
    projection_rollback.add_argument("--target-build-id")
    return parser


def _validate_environment_name(value: object, label: str) -> str:
    if (
        type(value) is not str
        or not value
        or value.strip() != value
        or "=" in value
        or "\x00" in value
    ):
        raise _StartupInputError(
            f"{label} environment variable name is invalid"
        )
    return value


def _factory_path(args: argparse.Namespace) -> str:
    path = args.application_factory
    if path is None:
        path = os.environ.get(DURABLE_LOCAL_APPLICATION_FACTORY_ENV)
    if path is None:
        raise _StartupInputError(
            "local daemon application factory is not configured"
        )
    return path


def _load_application(path: str) -> DurableLocalApplication:
    if (
        type(path) is not str
        or not path
        or path.strip() != path
        or path.count(":") != 1
    ):
        raise _StartupInputError(
            "local daemon application factory must be MODULE:CALLABLE"
        )
    module_name, separator, attribute_name = path.partition(":")
    if (
        not separator
        or not module_name
        or not attribute_name
        or any(not part.isidentifier() for part in module_name.split("."))
        or any(
            not part.isidentifier()
            for part in attribute_name.split(".")
        )
    ):
        raise _StartupInputError(
            "local daemon application factory must be MODULE:CALLABLE"
        )
    try:
        target: object = importlib.import_module(module_name)
        for part in attribute_name.split("."):
            target = getattr(target, part)
    except Exception as error:
        raise RuntimeError(
            "local daemon application factory could not be loaded"
        ) from error
    if not callable(target):
        raise _StartupInputError(
            "local daemon application factory is not callable"
        )
    try:
        application = target()
    except Exception as error:
        raise RuntimeError(
            "local daemon application factory failed"
        ) from error
    if type(application) is not DurableLocalApplication:
        raise _StartupInputError(
            "local daemon application factory returned invalid data"
        )
    return application


def _load_token(args: argparse.Namespace) -> str:
    token_env = _validate_environment_name(args.token_env, "token")
    token = os.environ.get(token_env)
    if token is None:
        raise _StartupInputError(
            "configured local daemon token environment variable is missing"
        )
    return token


def _worker_configuration(
    args: argparse.Namespace,
) -> LocalDaemonWorkerConfiguration:
    try:
        return LocalDaemonWorkerConfiguration(
            interval_seconds=args.worker_interval,
            recovery_limit=args.worker_page_size,
            outbox_lease_seconds=args.outbox_lease_seconds,
            outbox_limit=args.worker_page_size,
            outbox_retry_delay_seconds=(
                args.outbox_retry_delay_seconds
            ),
            outbox_max_attempts=args.outbox_max_attempts,
        )
    except (TypeError, ValueError) as error:
        raise _StartupInputError(str(error)) from error


def _http_server(
    args: argparse.Namespace,
    application: DurableLocalApplication,
    runtime: DurableSQLiteRuntime,
    token: str,
) -> DurableAgentHTTPServer:
    try:
        configuration = DurableHTTPServerConfiguration(
            host=args.host,
            port=args.port,
        )
        authenticator = DurableBearerAuthenticator(
            token,
            application.http_context_provider,
        )
    except (TypeError, ValueError) as error:
        raise _StartupInputError(str(error)) from error
    return DurableAgentHTTPServer(
        configuration,
        runtime.dispatcher,
        authenticator,
    )


@contextmanager
def _termination_interrupt() -> Iterator[None]:
    if current_thread() is not main_thread():
        yield
        return
    termination = getattr(signal, "SIGTERM", None)
    if termination is None:
        yield
        return
    previous = signal.getsignal(termination)

    def interrupt(_signum: int, _frame: FrameType | None) -> None:
        raise KeyboardInterrupt

    signal.signal(termination, interrupt)
    try:
        yield
    finally:
        signal.signal(termination, previous)


def _wait_without_mcp() -> None:
    wait = Event()
    while True:
        wait.wait(3_600)


def _shutdown_services(
    server: DurableAgentHTTPServer,
    http_thread: Thread,
    workers: DurableLocalWorkerLoop,
    runtime: DurableSQLiteRuntime,
    *,
    http_started: bool,
    workers_started: bool,
) -> None:
    failure: Exception | None = None
    if http_started:
        try:
            server.shutdown()
        except Exception as error:
            failure = error
        try:
            http_thread.join(10.0)
            if http_thread.is_alive() and failure is None:
                failure = LocalDaemonV3Error(
                    "TBM_LOCAL_DAEMON_HTTP_STOP_TIMEOUT",
                    "local daemon HTTP service did not stop in time",
                )
        except Exception as error:
            if failure is None:
                failure = error
    if workers_started:
        try:
            workers.stop(timeout_seconds=10.0)
        except Exception as error:
            if failure is None:
                failure = error
    try:
        server.server_close()
    except Exception as error:
        if failure is None:
            failure = error
    finally:
        try:
            runtime.close()
        except Exception as error:
            if failure is None:
                failure = error
    if failure is not None:
        raise failure


def _run_local(args: argparse.Namespace) -> int:
    if (
        args.expose_replay_content
        and not args.expose_injection_content
    ):
        raise _StartupInputError(
            "--expose-replay-content requires "
            "--expose-injection-content"
        )
    if (
        type(args.lock_timeout) is not float
        or not math.isfinite(args.lock_timeout)
        or args.lock_timeout < 0
    ):
        raise _StartupInputError(
            "--lock-timeout must be non-negative"
        )
    application = _load_application(_factory_path(args))
    token = _load_token(args)
    state_directory = prepare_local_state_directory(
        args.state_dir,
        create=args.initialize,
    )
    configuration = _worker_configuration(args)
    with local_daemon_lock(
        state_directory,
        timeout_seconds=args.lock_timeout,
    ):
        database = prepare_local_database(
            state_directory,
            initialize=args.initialize,
        )
        database_stat = verify_local_database_target(database)
        runtime = DurableRuntimeFactory(
            application.dependencies
        ).open_sqlite(
            database,
            initialize=args.initialize,
            expose_injection_content=args.expose_injection_content,
            expose_replay_content=args.expose_replay_content,
            check_same_thread=False,
        )
        try:
            verify_local_database_target(
                database,
                expected_stat=database_stat,
            )
            server = _http_server(
                args,
                application,
                runtime,
                token,
            )
            mcp_server = (
                None
                if args.no_mcp
                else create_durable_mcp_server(
                    runtime.dispatcher,
                    application.mcp_contexts,
                )
            )
            workers = DurableLocalWorkerLoop(
                runtime,
                worker_id=(
                    f"tbmd_{os.getpid()}_{secrets.token_hex(8)}"
                ),
                configuration=configuration,
            )
        except Exception:
            runtime.close()
            raise
        http_thread = Thread(
            target=server.serve_forever,
            kwargs={"poll_interval": 0.25},
            name="tbmd-durable-http",
            daemon=False,
        )
        http_started = False
        workers_started = False
        try:
            http_thread.start()
            http_started = True
            if not http_thread.is_alive():
                raise LocalDaemonV3Error(
                    "TBM_LOCAL_DAEMON_HTTP_START_FAILED",
                    "local daemon HTTP service did not start",
                )
            workers.start()
            workers_started = True
            with _termination_interrupt():
                try:
                    if mcp_server is None:
                        _wait_without_mcp()
                    else:
                        run_stdio_server(mcp_server)
                except KeyboardInterrupt:
                    pass
        finally:
            _shutdown_services(
                server,
                http_thread,
                workers,
                runtime,
                http_started=http_started,
                workers_started=workers_started,
            )
    return 0


def _run_init(args: argparse.Namespace) -> int:
    application = _load_application(_factory_path(args))
    state_directory = prepare_local_state_directory(
        args.state_dir,
        create=True,
    )
    with local_daemon_lock(state_directory, timeout_seconds=0):
        database = prepare_local_database(
            state_directory,
            initialize=True,
        )
        database_stat = verify_local_database_target(database)
        runtime = DurableRuntimeFactory(
            application.dependencies
        ).open_sqlite(
            database,
            initialize=True,
            check_same_thread=False,
        )
        try:
            verify_local_database_target(
                database,
                expected_stat=database_stat,
            )
            capabilities = runtime.dispatcher.capabilities()
        finally:
            runtime.close()
    _write_stdout(
        {
            "contract_version": LOCAL_DAEMON_CONTRACT_VERSION,
            "status": "initialized",
            "state_directory": "ready",
            "database_schema": "valid",
            "storage_mode": capabilities["storage_mode"],
        }
    )
    return 0


def _run_doctor(args: argparse.Namespace) -> int:
    application = _load_application(_factory_path(args))
    token = _load_token(args)
    state_directory = prepare_local_state_directory(
        args.state_dir,
        create=False,
    )
    DurableBearerAuthenticator(
        token,
        application.http_context_provider,
    )
    with local_daemon_lock(state_directory, timeout_seconds=0):
        database = prepare_local_database(
            state_directory,
            initialize=False,
        )
        database_stat = verify_local_database_target(database)
        runtime = DurableRuntimeFactory(
            application.dependencies
        ).open_sqlite(
            database,
            initialize=False,
            check_same_thread=False,
        )
        try:
            verify_local_database_target(
                database,
                expected_stat=database_stat,
            )
            capabilities = runtime.dispatcher.capabilities()
        finally:
            runtime.close()
    _write_stdout(
        {
            "contract_version": LOCAL_DAEMON_CONTRACT_VERSION,
            "status": "ok",
            "state_directory": "ready",
            "database_schema": "valid",
            "application": "valid",
            "outbox_consumer": True,
            "storage_mode": capabilities["storage_mode"],
        }
    )
    return 0


def _run_health(args: argparse.Namespace) -> int:
    token = _load_token(args)
    try:
        client = DurableAgentHTTPClient(
            args.base_url,
            token,
            timeout_seconds=args.timeout,
        )
        health = client.health()
    except (TypeError, ValueError) as error:
        raise _StartupInputError(str(error)) from error
    _write_stdout(
        {
            "contract_version": LOCAL_DAEMON_CONTRACT_VERSION,
            "status": "ok",
            "durable_http": health,
        }
    )
    return 0


def _open_operator_ledger(args: argparse.Namespace) -> SQLiteEventLedgerV1:
    return SQLiteEventLedgerV1.connect_operator(
        args.database,
        partition_sha256=args.partition_sha256,
    )


def _run_ledger(args: argparse.Namespace) -> int:
    with _open_operator_ledger(args) as ledger:
        if args.ledger_command == "stats":
            statistics = ledger.operator_statistics()
            _write_stdout(
                {
                    "contract_version": LOCAL_DAEMON_CONTRACT_VERSION,
                    "operation": "ledger.stats",
                    "status": "ok",
                    "statistics": statistics,
                }
            )
            return 0
        if args.ledger_command == "verify":
            verifications = ledger.verify_integrity()
            _write_stdout(
                {
                    "contract_version": LOCAL_DAEMON_CONTRACT_VERSION,
                    "operation": "ledger.verify",
                    "status": "ok",
                    "valid": all(item.valid for item in verifications),
                    "statistics": ledger.operator_statistics(),
                    "streams": [
                        {
                            "stream_id": item.stream_id,
                            "partition_sha256": item.partition_sha256,
                            "verified_stream_version": (
                                item.verified_stream_version
                            ),
                            "verified_event_count": item.verified_event_count,
                            "head_event_sha256": item.head_event_sha256,
                            "valid": item.valid,
                            "issue_codes": list(item.issue_codes),
                        }
                        for item in verifications
                    ],
                }
            )
            return 0
    raise AssertionError("unreachable ledger command")


def _checkpoint_summary(
    checkpoint: ProjectionCheckpoint,
    *,
    active_build_id: str | None,
) -> dict[str, object]:
    return {
        "build_id": checkpoint.build_id,
        "checkpoint_sha256": checkpoint.checkpoint_sha256,
        "projection_name": checkpoint.projection_name,
        "reducer_id": checkpoint.reducer_id,
        "reducer_version": checkpoint.reducer_version,
        "output_schema_version": (
            checkpoint.reducer_descriptor.output_schema_version
        ),
        "partition_sha256": checkpoint.partition_sha256,
        "global_position": checkpoint.global_position,
        "event_high_watermark": checkpoint.event_high_watermark,
        "state_sha256": checkpoint.state_sha256,
        "code_sha256": checkpoint.reducer_descriptor.code_sha256,
        "configuration_sha256": (
            checkpoint.reducer_descriptor.configuration_sha256
        ),
        "rebuild_generation": checkpoint.rebuild_generation,
        "active": checkpoint.build_id == active_build_id,
    }


def _projection_runtime(ledger: SQLiteEventLedgerV1) -> ProjectionRuntime:
    return ProjectionRuntime(
        ledger,
        build_default_reducer_registry(),
        ledger,
        event_registry=DEFAULT_EVENT_TYPE_REGISTRY,
    )


def _observed_head(
    ledger: SQLiteEventLedgerV1,
    projection_name: str,
    partition_sha256: str,
) -> tuple[int, str | None]:
    current = ledger.current_activation(projection_name, partition_sha256)
    return (
        0 if current is None else current.head_version,
        None if current is None else current.target_build_id,
    )


def _require_expected_head(
    args: argparse.Namespace,
    observed_version: int,
    observed_build_id: str | None,
) -> None:
    if (
        args.expected_head_version is not None
        and args.expected_head_version != observed_version
    ) or (
        args.expected_current_build_id is not None
        and args.expected_current_build_id != observed_build_id
    ):
        raise ProjectionRuntimeError(
            "TBM_PROJECTION_HEAD_CONFLICT",
            "projection head does not match the operator precondition",
        )


def _run_projection(args: argparse.Namespace) -> int:
    with _open_operator_ledger(args) as ledger:
        runtime = _projection_runtime(ledger)
        partition_sha256 = ledger.access_context.partition.partition_sha256
        if args.projection_command == "list":
            checkpoints = ledger.list_checkpoints(
                args.projection_name,
                partition_sha256,
            )
            active_by_projection: dict[str, str] = {}
            for projection_name in sorted(
                {checkpoint.projection_name for checkpoint in checkpoints}
            ):
                current = ledger.current_activation(
                    projection_name,
                    partition_sha256,
                )
                if current is not None:
                    active_by_projection[projection_name] = current.target_build_id
            _write_stdout(
                {
                    "contract_version": LOCAL_DAEMON_CONTRACT_VERSION,
                    "operation": "projection.list",
                    "status": "ok",
                    "partition_sha256": partition_sha256,
                    "checkpoints": [
                        _checkpoint_summary(
                            checkpoint,
                            active_build_id=active_by_projection.get(
                                checkpoint.projection_name
                            ),
                        )
                        for checkpoint in checkpoints
                    ],
                }
            )
            return 0
        if args.projection_command == "rebuild":
            result = runtime.rebuild(
                args.reducer_id,
                args.version,
                partition_sha256=partition_sha256,
                owner=args.owner,
                rebuild_generation=args.generation,
                page_size=args.page_size,
                checkpoint_interval=args.checkpoint_interval,
                resume=args.resume,
            )
            _write_stdout(
                {
                    "contract_version": LOCAL_DAEMON_CONTRACT_VERSION,
                    "operation": "projection.rebuild",
                    "status": result.status,
                    "checkpoint": _checkpoint_summary(
                        result.checkpoint,
                        active_build_id=None,
                    ),
                    "blocked": (
                        None
                        if result.blocked is None
                        else result.blocked.to_dict()
                    ),
                    "resumed_from_build_id": result.resumed_from_build_id,
                    "processed_events": result.processed_events,
                }
            )
            return 0 if result.status == "completed" else 2
        if args.projection_command == "compare":
            comparison = runtime.compare(
                args.active_build_id,
                args.shadow_build_id,
            )
            _write_stdout(
                {
                    "contract_version": LOCAL_DAEMON_CONTRACT_VERSION,
                    "operation": "projection.compare",
                    "status": "ok",
                    "comparison": comparison.to_dict(),
                }
            )
            return 0
        if args.projection_command == "activate":
            shadow = ledger.load_checkpoint(args.shadow_build_id)
            observed_version, observed_build = _observed_head(
                ledger,
                shadow.projection_name,
                partition_sha256,
            )
            _require_expected_head(args, observed_version, observed_build)
            comparison = (
                None
                if observed_build is None
                else runtime.compare(observed_build, shadow.build_id)
            )
            activation = runtime.activate(
                shadow.build_id,
                owner=args.owner,
                approved=args.approve,
                expected_head_version=observed_version,
                expected_current_build_id=observed_build,
                comparison=comparison,
            )
            _write_stdout(
                {
                    "contract_version": LOCAL_DAEMON_CONTRACT_VERSION,
                    "operation": "projection.activate",
                    "status": "ok",
                    "comparison_sha256": (
                        None
                        if comparison is None
                        else comparison.comparison_sha256
                    ),
                    "activation": activation.to_dict(),
                }
            )
            return 0
        if args.projection_command == "rollback":
            observed_version, observed_build = _observed_head(
                ledger,
                args.projection_name,
                partition_sha256,
            )
            _require_expected_head(args, observed_version, observed_build)
            if observed_build is None:
                raise ProjectionRuntimeError(
                    "TBM_PROJECTION_ROLLBACK_UNAVAILABLE",
                    "projection does not have an active head",
                )
            activation = runtime.rollback(
                args.projection_name,
                partition_sha256,
                owner=args.owner,
                expected_head_version=observed_version,
                expected_current_build_id=observed_build,
                target_build_id=args.target_build_id,
            )
            _write_stdout(
                {
                    "contract_version": LOCAL_DAEMON_CONTRACT_VERSION,
                    "operation": "projection.rollback",
                    "status": "ok",
                    "activation": activation.to_dict(),
                }
            )
            return 0
    raise AssertionError("unreachable projection command")


def _write_stdout(value: dict[str, object]) -> None:
    sys.stdout.write(
        json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
        + "\n"
    )


def _public_error(
    error: Exception,
    operation: str,
) -> dict[str, object]:
    if isinstance(error, LocalDaemonV3Error):
        code = error.code
        category = (
            "state"
            if code == "TBM_LOCAL_DAEMON_ALREADY_RUNNING"
            else "input"
        )
        message = str(error)
        retryable = code == "TBM_LOCAL_DAEMON_ALREADY_RUNNING"
    elif isinstance(error, _StartupInputError):
        code = "TBM_LOCAL_DAEMON_INPUT_INVALID"
        category = "input"
        message = str(error)
        retryable = False
    elif isinstance(error, DurableAgentHTTPClientError):
        code = error.code
        category = error.category
        message = str(error)
        retryable = error.retryable
    elif isinstance(error, DurableRuntimeV3Error):
        code = error.code
        category = "persistence"
        message = str(error)
        retryable = True
    elif isinstance(error, EventLedgerPortError):
        code = error.code
        category = (
            "persistence"
            if "SQLITE" in code or "POSTGRES" in code
            else "state"
            if "CONFLICT" in code or "NOT_FOUND" in code
            else "input"
        )
        message = str(error)
        retryable = "CONFLICT" in code or "PERSISTENCE" in code
    elif isinstance(
        error,
        (
            ProjectionCheckpointError,
            ProjectionRuntimeError,
            ReducerRegistryError,
            ReducerV1Error,
        ),
    ):
        code = error.code
        category = (
            "persistence"
            if "SQLITE" in code or "POSTGRES" in code
            else "state"
            if any(
                marker in code
                for marker in ("CONFLICT", "NOT_FOUND", "BLOCKED")
            )
            else "input"
        )
        message = str(error)
        retryable = "CONFLICT" in code or "PERSISTENCE" in code
    else:
        code = "TBM_LOCAL_DAEMON_INTERNAL_ERROR"
        category = "internal"
        message = "local daemon operation failed"
        retryable = True
    return {
        "contract_version": LOCAL_DAEMON_CONTRACT_VERSION,
        "error": {
            "code": code,
            "category": category,
            "message": message,
            "operation": operation,
            "retryable": retryable,
        },
    }


def main(argv: Sequence[str] | None = None) -> int:
    operation = "parse"
    try:
        args = _build_parser().parse_args(argv)
        operation = args.command
        if args.command == "ledger":
            operation = f"ledger.{args.ledger_command}"
        elif args.command == "projection":
            operation = f"projection.{args.projection_command}"
        if args.command == "init":
            return _run_init(args)
        if args.command == "local":
            return _run_local(args)
        if args.command == "doctor":
            return _run_doctor(args)
        if args.command == "health":
            return _run_health(args)
        if args.command == "ledger":
            return _run_ledger(args)
        if args.command == "projection":
            return _run_projection(args)
        raise AssertionError("unreachable local daemon command")
    except Exception as error:
        sys.stderr.write(
            json.dumps(
                _public_error(error, operation),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        return 2


__all__ = [
    "DURABLE_LOCAL_APPLICATION_FACTORY_ENV",
    "DURABLE_LOCAL_TOKEN_ENV",
    "DurableLocalApplication",
    "main",
]


if __name__ == "__main__":
    from .daemon_entry import main as _canonical_main

    raise SystemExit(_canonical_main())
