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
from .mcp_server import run_stdio_server


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
        if args.command == "init":
            return _run_init(args)
        if args.command == "local":
            return _run_local(args)
        if args.command == "doctor":
            return _run_doctor(args)
        if args.command == "health":
            return _run_health(args)
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
