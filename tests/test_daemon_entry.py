from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from dataclasses import replace
from datetime import datetime, timedelta, timezone
import json
import os
from pathlib import Path
import secrets
import signal
import socket
import subprocess
import sys
import time
from types import SimpleNamespace
from typing import cast

import anyio
from mcp import ClientSession
from mcp.client.stdio import StdioServerParameters, stdio_client
import pytest

import trace_backed_memory as tbm
from tests.test_durable_agent_v3 import _completion
from tests.test_durable_agent_wire_v1 import (
    _decide_request,
    _prepare_request,
)
from tests.test_durable_execution_v3 import EVALUATOR_CONTEXT
from tests.test_durable_runtime_v3 import _Clock, _dependencies
from tests.test_durable_semantic_gate_v3 import (
    _context as _provider_context,
)
from tests.test_sqlite_event_ledger_v1 import (
    _access as _ledger_access,
    _append as _ledger_append,
    _batch as _ledger_batch,
)
from trace_backed_memory import daemon_entry
from trace_backed_memory._timestamps import utc_timestamp
from trace_backed_memory.daemon_entry import DurableLocalApplication
from trace_backed_memory.durable_agent_wire_v1 import (
    DurableCancelRequest,
    DurableCompleteRequest,
    DurableFinalizeRequest,
    DurableStartRequest,
)
from trace_backed_memory.durable_http_server import (
    DurableHTTPAuthenticatedContexts,
)
from trace_backed_memory.durable_mcp_server import (
    DURABLE_MCP_SERVER_INSTRUCTIONS,
    DurableMCPTrustedContexts,
)
from trace_backed_memory.durable_runtime_v3 import (
    DurableRuntimeFactory,
    DurableRuntimeV3Error,
)
from trace_backed_memory.durable_sdk import (
    DurableAgentHTTPClient,
    DurableAgentHTTPClientError,
)
from trace_backed_memory.local_daemon_v3 import (
    LOCAL_DAEMON_CONTRACT_VERSION,
    local_daemon_lock,
    prepare_local_database,
    prepare_local_state_directory,
)
from trace_backed_memory.sqlite_event_ledger_v1 import SQLiteEventLedgerV1


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "local_daemon_test_token_" + "a" * 48
APPLICATION_FACTORY = "tests.test_daemon_entry:create_test_application"


def _daemon_test_clock() -> str:
    offset_file = os.environ.get("TBM_TEST_DAEMON_CLOCK_OFFSET_FILE")
    offset = 0
    if offset_file is not None:
        offset = int(Path(offset_file).read_text(encoding="utf-8"))
    value = datetime.now(timezone.utc) + timedelta(seconds=offset)
    return value.isoformat().replace("+00:00", "Z")


def create_test_application() -> DurableLocalApplication:
    """Return trusted real-clock dependencies for daemon process tests."""
    dependencies, context = _dependencies(_Clock())

    def consume(
        event: tbm.CompletionOutboxEvent,
    ) -> tbm.CompletionOutboxConsumerReceipt:
        delivery_file = os.environ.get("TBM_TEST_DAEMON_DELIVERY_FILE")
        if delivery_file is not None:
            with open(delivery_file, "a", encoding="utf-8") as stream:
                stream.write(event.event_id + "\n")
        return tbm.CompletionOutboxConsumerReceipt(
            response_sha256="sha256:" + "f" * 64
        )

    dependencies = replace(
        dependencies,
        clock=(
            _daemon_test_clock
            if os.environ.get("TBM_TEST_DAEMON_CLOCK_OFFSET_FILE")
            else utc_timestamp
        ),
        authorization_request_id_factory=lambda: (
            f"authorization_daemon_{secrets.token_hex(16)}"
        ),
        session_id_factory=lambda: (
            f"gate_session_daemon_{secrets.token_hex(16)}"
        ),
        completion_consumer=consume,
    )
    mcp_contexts = DurableMCPTrustedContexts(
        context,
        _provider_context(),
        EVALUATOR_CONTEXT,
    )
    return DurableLocalApplication(
        dependencies,
        mcp_contexts,
        lambda _request: DurableHTTPAuthenticatedContexts(
            context,
            provider=_provider_context(),
            evaluator=EVALUATOR_CONTEXT,
        ),
    )


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return cast(int, listener.getsockname()[1])


def _environment(
    *,
    delivery_file: Path | None = None,
    clock_file: Path | None = None,
) -> dict[str, str]:
    environment = dict(os.environ)
    source_path = str(ROOT / "src")
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not prior_pythonpath
        else source_path + os.pathsep + prior_pythonpath
    )
    environment[daemon_entry.DURABLE_LOCAL_TOKEN_ENV] = TOKEN
    environment[
        daemon_entry.DURABLE_LOCAL_APPLICATION_FACTORY_ENV
    ] = APPLICATION_FACTORY
    if delivery_file is not None:
        environment["TBM_TEST_DAEMON_DELIVERY_FILE"] = str(delivery_file)
    if clock_file is not None:
        environment["TBM_TEST_DAEMON_CLOCK_OFFSET_FILE"] = str(clock_file)
    return environment


def _arguments(
    state_directory: Path,
    port: int,
    *,
    initialize: bool,
    no_mcp: bool,
) -> list[str]:
    arguments = [
        "-m",
        "trace_backed_memory.daemon_entry",
        "local",
        "--state-dir",
        str(state_directory),
        "--port",
        str(port),
        "--worker-interval",
        "0.05",
    ]
    if initialize:
        arguments.append("--initialize")
    if no_mcp:
        arguments.append("--no-mcp")
    return arguments


def _start_daemon(
    state_directory: Path,
    port: int,
    *,
    initialize: bool,
    delivery_file: Path | None = None,
    clock_file: Path | None = None,
) -> subprocess.Popen[str]:
    process = subprocess.Popen(
        [
            sys.executable,
            *_arguments(
                state_directory,
                port,
                initialize=initialize,
                no_mcp=True,
            ),
        ],
        cwd=ROOT,
        env=_environment(
            delivery_file=delivery_file,
            clock_file=clock_file,
        ),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    return process


def _wait_for_client(
    process: subprocess.Popen[str],
    port: int,
) -> DurableAgentHTTPClient:
    client = DurableAgentHTTPClient(
        f"http://127.0.0.1:{port}",
        TOKEN,
        timeout_seconds=1.0,
    )
    deadline = time.monotonic() + 15
    last_error: Exception | None = None
    while time.monotonic() < deadline:
        if process.poll() is not None:
            stderr = process.stderr.read() if process.stderr else ""
            raise AssertionError(
                f"daemon exited with {process.returncode}: {stderr}"
            )
        try:
            client.health()
            return client
        except DurableAgentHTTPClientError as error:
            last_error = error
            time.sleep(0.05)
    raise AssertionError(f"daemon did not become healthy: {last_error}")


def _kill(process: subprocess.Popen[str]) -> None:
    if process.poll() is None:
        process.kill()
    process.wait(timeout=10)


def _request_payload(request: object) -> dict[str, object]:
    return cast(
        dict[str, object],
        request.model_dump(mode="json"),
    )


def test_daemon_parser_application_and_factory_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    parser = daemon_entry._build_parser()
    local = parser.parse_args(["local", "--initialize"])
    assert local.state_dir == Path(".tbm")
    assert local.port == 8766
    assert local.worker_interval == 1.0
    health = parser.parse_args(["health"])
    assert health.base_url == "http://127.0.0.1:8766"
    ledger = parser.parse_args(["ledger", "verify"])
    assert ledger.database == Path(".tbm") / "event-ledger.sqlite3"
    projection = parser.parse_args(
        ["projection", "rebuild", "--generation", "1"]
    )
    assert projection.reducer_id == "canonical-event-inventory"
    assert projection.version == 1

    application = create_test_application()
    dependencies, _context = _dependencies(_Clock())
    with pytest.raises(ValueError, match="outbox consumer"):
        DurableLocalApplication(
            dependencies,
            application.mcp_contexts,
            application.http_context_provider,
        )
    for path in (
        "",
        "missing-colon",
        "too:many:colons",
        "bad-module!:create",
    ):
        with pytest.raises(ValueError, match="MODULE:CALLABLE"):
            daemon_entry._load_application(path)
    monkeypatch.setattr(
        daemon_entry.importlib,
        "import_module",
        lambda _name: SimpleNamespace(
            nested=SimpleNamespace(create=lambda: application)
        ),
    )
    assert (
        daemon_entry._load_application(
            "trusted.application:nested.create"
        )
        is application
    )


def test_daemon_rejects_invalid_trusted_startup_inputs(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    application = create_test_application()
    original_import_module = daemon_entry.importlib.import_module
    with pytest.raises(TypeError, match="dependencies"):
        DurableLocalApplication(  # type: ignore[arg-type]
            object(),
            application.mcp_contexts,
            application.http_context_provider,
        )
    with pytest.raises(TypeError, match="MCP contexts"):
        DurableLocalApplication(  # type: ignore[arg-type]
            application.dependencies,
            object(),
            application.http_context_provider,
        )
    with pytest.raises(TypeError, match="context provider"):
        DurableLocalApplication(  # type: ignore[arg-type]
            application.dependencies,
            application.mcp_contexts,
            None,
        )

    for value in ("", " bad", "BAD=NAME", "BAD\x00NAME", 1):
        with pytest.raises(ValueError, match="environment variable name"):
            daemon_entry._validate_environment_name(value, "test")

    monkeypatch.delenv(
        daemon_entry.DURABLE_LOCAL_APPLICATION_FACTORY_ENV,
        raising=False,
    )
    with pytest.raises(ValueError, match="not configured"):
        daemon_entry._factory_path(
            SimpleNamespace(application_factory=None)
        )

    def import_failure(_name: str) -> object:
        raise ImportError("private import failure")

    monkeypatch.setattr(
        daemon_entry.importlib,
        "import_module",
        import_failure,
    )
    with pytest.raises(RuntimeError, match="could not be loaded"):
        daemon_entry._load_application("trusted.application:create")

    monkeypatch.setattr(
        daemon_entry.importlib,
        "import_module",
        lambda _name: SimpleNamespace(create=object()),
    )
    with pytest.raises(ValueError, match="not callable"):
        daemon_entry._load_application("trusted.application:create")

    def factory_failure() -> object:
        raise RuntimeError("private factory failure")

    monkeypatch.setattr(
        daemon_entry.importlib,
        "import_module",
        lambda _name: SimpleNamespace(create=factory_failure),
    )
    with pytest.raises(RuntimeError, match="factory failed"):
        daemon_entry._load_application("trusted.application:create")

    monkeypatch.setattr(
        daemon_entry.importlib,
        "import_module",
        lambda _name: SimpleNamespace(create=lambda: object()),
    )
    with pytest.raises(ValueError, match="returned invalid data"):
        daemon_entry._load_application("trusted.application:create")
    monkeypatch.setattr(
        daemon_entry.importlib,
        "import_module",
        original_import_module,
    )

    monkeypatch.delenv(daemon_entry.DURABLE_LOCAL_TOKEN_ENV, raising=False)
    with pytest.raises(ValueError, match="is missing"):
        daemon_entry._load_token(
            SimpleNamespace(
                token_env=daemon_entry.DURABLE_LOCAL_TOKEN_ENV
            )
        )
    with pytest.raises(ValueError, match="worker interval"):
        daemon_entry._worker_configuration(
            SimpleNamespace(
                worker_interval=0.0,
                worker_page_size=100,
                outbox_lease_seconds=60,
                outbox_retry_delay_seconds=60,
                outbox_max_attempts=5,
            )
        )

    parser = daemon_entry._build_parser()
    replay_only = parser.parse_args(
        ["local", "--expose-replay-content"]
    )
    with pytest.raises(ValueError, match="requires"):
        daemon_entry._run_local(replay_only)
    invalid_lock = parser.parse_args(
        ["local", "--lock-timeout", "nan"]
    )
    with pytest.raises(ValueError, match="non-negative"):
        daemon_entry._run_local(invalid_lock)

    with DurableRuntimeFactory(application.dependencies).open_sqlite(
        initialize=True,
    ) as runtime:
        with pytest.raises(ValueError, match="host"):
            daemon_entry._http_server(
                SimpleNamespace(host="not-an-ip", port=8766),
                application,
                runtime,
                TOKEN,
            )


def test_daemon_public_errors_are_stable_and_sanitized() -> None:
    running = daemon_entry._public_error(
        daemon_entry.LocalDaemonV3Error(
            "TBM_LOCAL_DAEMON_ALREADY_RUNNING",
            "another local daemon already owns this state directory",
        ),
        "doctor",
    )
    assert running["error"]["category"] == "state"
    assert running["error"]["retryable"] is True

    state = daemon_entry._public_error(
        daemon_entry.LocalDaemonV3Error(
            "TBM_LOCAL_DAEMON_STATE_UNSAFE",
            "local daemon state directory is unsafe",
        ),
        "doctor",
    )
    assert state["error"]["category"] == "input"
    assert state["error"]["retryable"] is False

    http = daemon_entry._public_error(
        DurableAgentHTTPClientError(
            "TBM_TEST_HTTP",
            "authentication",
            "health",
            "test HTTP failure",
            retryable=False,
        ),
        "health",
    )
    assert http["error"]["code"] == "TBM_TEST_HTTP"
    assert http["error"]["category"] == "authentication"

    persistence = daemon_entry._public_error(
        DurableRuntimeV3Error(
            "TBM_TEST_PERSISTENCE",
            "private persistence failure",
        ),
        "local",
    )
    assert persistence["error"]["category"] == "persistence"
    assert persistence["error"]["retryable"] is True

    ledger_conflict = daemon_entry._public_error(
        daemon_entry.EventLedgerPortError(
            "TBM_EVENT_LEDGER_CONFLICT",
            "event ledger head changed",
        ),
        "ledger verify",
    )
    ledger_input = daemon_entry._public_error(
        daemon_entry.EventLedgerPortError(
            "TBM_EVENT_LEDGER_INVALID_REQUEST",
            "event ledger request is invalid",
        ),
        "ledger verify",
    )
    assert ledger_conflict["error"]["category"] == "state"
    assert ledger_conflict["error"]["retryable"] is True
    assert ledger_input["error"]["category"] == "input"
    assert ledger_input["error"]["retryable"] is False

    projection_not_found = daemon_entry._public_error(
        daemon_entry.ProjectionCheckpointError(
            "TBM_PROJECTION_NOT_FOUND",
            "projection is not retained",
        ),
        "projection list",
    )
    projection_conflict = daemon_entry._public_error(
        daemon_entry.ProjectionRuntimeError(
            "TBM_PROJECTION_HEAD_CONFLICT",
            "projection head changed",
        ),
        "projection activate",
    )
    reducer_input = daemon_entry._public_error(
        daemon_entry.ReducerRegistryError(
            "TBM_REDUCER_NOT_FOUND",
            "reducer is not registered",
        ),
        "projection rebuild",
    )
    reducer_persistence = daemon_entry._public_error(
        daemon_entry.ReducerV1Error(
            "TBM_REDUCER_PERSISTENCE",
            "reducer persistence failed",
        ),
        "projection rebuild",
    )
    assert projection_not_found["error"]["category"] == "state"
    assert projection_not_found["error"]["retryable"] is False
    assert projection_conflict["error"]["category"] == "state"
    assert projection_conflict["error"]["retryable"] is True
    assert reducer_input["error"]["category"] == "state"
    assert reducer_input["error"]["retryable"] is False
    assert reducer_persistence["error"]["category"] == "input"
    assert reducer_persistence["error"]["retryable"] is True

    internal = daemon_entry._public_error(
        RuntimeError("private secret"),
        "local",
    )
    assert internal["error"]["message"] == "local daemon operation failed"
    assert "private secret" not in json.dumps(internal)


def test_daemon_termination_context_restores_signal_handler(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    completed: list[bool] = []

    def use_from_worker() -> None:
        with daemon_entry._termination_interrupt():
            completed.append(True)

    with ThreadPoolExecutor(max_workers=1) as pool:
        pool.submit(use_from_worker).result(timeout=5)
    assert completed == [True]

    termination = getattr(signal, "SIGTERM", None)
    if termination is not None:
        previous = signal.getsignal(termination)
        with pytest.raises(KeyboardInterrupt):
            with daemon_entry._termination_interrupt():
                handler = signal.getsignal(termination)
                assert callable(handler)
                handler(termination, None)
        assert signal.getsignal(termination) == previous

    monkeypatch.delattr(daemon_entry.signal, "SIGTERM", raising=False)
    with daemon_entry._termination_interrupt():
        pass


def test_daemon_shutdown_preserves_first_failure() -> None:
    calls: list[str] = []

    class Server:
        def shutdown(self) -> None:
            calls.append("shutdown")
            raise RuntimeError("shutdown failed")

        def server_close(self) -> None:
            calls.append("server_close")
            raise RuntimeError("server close failed")

    class HTTPThread:
        def join(self, _timeout: float) -> None:
            calls.append("http_join")

        def is_alive(self) -> bool:
            return True

    class Workers:
        def stop(self, *, timeout_seconds: float) -> None:
            assert timeout_seconds == 10.0
            calls.append("worker_stop")
            raise RuntimeError("worker stop failed")

    class Runtime:
        def close(self) -> None:
            calls.append("runtime_close")
            raise RuntimeError("runtime close failed")

    with pytest.raises(RuntimeError, match="shutdown failed"):
        daemon_entry._shutdown_services(  # type: ignore[arg-type]
            Server(),
            HTTPThread(),
            Workers(),
            Runtime(),
            http_started=True,
            workers_started=True,
        )
    assert calls == [
        "shutdown",
        "http_join",
        "worker_stop",
        "server_close",
        "runtime_close",
    ]

    class CloseFailureServer:
        def server_close(self) -> None:
            raise RuntimeError("only close failed")

    with pytest.raises(RuntimeError, match="only close failed"):
        daemon_entry._shutdown_services(  # type: ignore[arg-type]
            CloseFailureServer(),
            HTTPThread(),
            Workers(),
            Runtime(),
            http_started=False,
            workers_started=False,
        )


def test_daemon_rejects_malformed_cli_as_deterministic_json(
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert daemon_entry.main(["local", "--port", "not-a-port"]) == 2
    captured = capsys.readouterr()
    assert captured.out == ""
    assert "usage:" not in captured.err
    assert json.loads(captured.err) == {
        "contract_version": LOCAL_DAEMON_CONTRACT_VERSION,
        "error": {
            "category": "input",
            "code": "TBM_LOCAL_DAEMON_INPUT_INVALID",
            "message": "local daemon command line is invalid",
            "operation": "parse",
            "retryable": False,
        },
    }


def test_daemon_ledger_and_projection_commands_are_deterministic_json(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    database = tmp_path / "event-ledger.sqlite3"
    access = _ledger_access()
    with SQLiteEventLedgerV1.connect(
        database,
        access,
        initialize=True,
    ) as writer:
        _ledger_append(writer, _ledger_batch(access, count=1))
    target = ["--database", str(database)]

    assert daemon_entry.main(["ledger", "stats", *target]) == 0
    statistics = json.loads(capsys.readouterr().out)
    assert statistics["operation"] == "ledger.stats"
    assert statistics["statistics"]["events"] == 1

    assert daemon_entry.main(["ledger", "verify", *target]) == 0
    verified = json.loads(capsys.readouterr().out)
    assert verified["operation"] == "ledger.verify"
    assert verified["valid"] is True

    assert (
        daemon_entry.main(
            [
                "projection",
                "rebuild",
                *target,
                "--generation",
                "1",
            ]
        )
        == 0
    )
    rebuilt = json.loads(capsys.readouterr().out)
    build_id = rebuilt["checkpoint"]["build_id"]
    projection_name = rebuilt["checkpoint"]["projection_name"]
    assert rebuilt["operation"] == "projection.rebuild"
    assert rebuilt["processed_events"] == 1

    assert (
        daemon_entry.main(
            [
                "projection",
                "activate",
                *target,
                build_id,
                "--approve",
            ]
        )
        == 0
    )
    first_activation = json.loads(capsys.readouterr().out)
    assert first_activation["activation"]["head_version"] == 1

    assert (
        daemon_entry.main(
            [
                "projection",
                "activate",
                *target,
                build_id,
                "--approve",
                "--expected-head-version",
                "1",
                "--expected-current-build-id",
                build_id,
            ]
        )
        == 0
    )
    second_activation = json.loads(capsys.readouterr().out)
    assert second_activation["activation"]["head_version"] == 2
    assert second_activation["comparison_sha256"].startswith("sha256:")

    assert (
        daemon_entry.main(
            [
                "projection",
                "rollback",
                *target,
                projection_name,
                "--expected-head-version",
                "2",
                "--expected-current-build-id",
                build_id,
            ]
        )
        == 0
    )
    rollback = json.loads(capsys.readouterr().out)
    assert rollback["activation"]["head_version"] == 3
    assert rollback["activation"]["operation"] == "rollback"

    assert daemon_entry.main(["projection", "list", *target]) == 0
    listed = json.loads(capsys.readouterr().out)
    assert listed["checkpoints"][0]["active"] is True

    assert (
        daemon_entry.main(
            ["projection", "compare", *target, build_id, build_id]
        )
        == 0
    )
    compared = json.loads(capsys.readouterr().out)
    assert compared["comparison"]["equivalent"] is True


def test_daemon_ledger_missing_database_uses_public_error_contract(
    tmp_path: Path,
    capsys: pytest.CaptureFixture[str],
) -> None:
    assert (
        daemon_entry.main(
            [
                "ledger",
                "verify",
                "--database",
                str(tmp_path / "missing.sqlite3"),
            ]
        )
        == 2
    )
    captured = capsys.readouterr()
    assert captured.out == ""
    error = json.loads(captured.err)["error"]
    assert error["code"] == "TBM_EVENT_LEDGER_NOT_FOUND"
    assert error["operation"] == "ledger.verify"
    assert error["category"] == "state"


def test_daemon_doctor_is_deterministic_and_respects_the_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_directory = prepare_local_state_directory(
        tmp_path / ".tbm",
        create=True,
    )
    monkeypatch.setenv(daemon_entry.DURABLE_LOCAL_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(
        daemon_entry.DURABLE_LOCAL_APPLICATION_FACTORY_ENV,
        APPLICATION_FACTORY,
    )

    assert (
        daemon_entry.main(
            ["init", "--state-dir", str(state_directory)]
        )
        == 0
    )
    initialized = json.loads(capsys.readouterr().out)
    assert initialized["status"] == "initialized"

    assert (
        daemon_entry.main(
            ["doctor", "--state-dir", str(state_directory)]
        )
        == 0
    )
    output = json.loads(capsys.readouterr().out)
    assert output == {
        "application": "valid",
        "contract_version": LOCAL_DAEMON_CONTRACT_VERSION,
        "database_schema": "valid",
        "outbox_consumer": True,
        "state_directory": "ready",
        "status": "ok",
        "storage_mode": "sqlite",
    }

    with local_daemon_lock(state_directory):
        assert (
            daemon_entry.main(
                ["doctor", "--state-dir", str(state_directory)]
            )
            == 2
        )
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == "TBM_LOCAL_DAEMON_ALREADY_RUNNING"


def test_daemon_worker_start_failure_releases_runtime_and_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_directory = tmp_path / ".tbm"
    monkeypatch.setenv(daemon_entry.DURABLE_LOCAL_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(
        daemon_entry.DURABLE_LOCAL_APPLICATION_FACTORY_ENV,
        APPLICATION_FACTORY,
    )

    def fail_start(_self: object) -> None:
        raise RuntimeError("private worker start failure")

    monkeypatch.setattr(
        daemon_entry.DurableLocalWorkerLoop,
        "start",
        fail_start,
    )
    assert (
        daemon_entry.main(
            [
                "local",
                "--state-dir",
                str(state_directory),
                "--initialize",
                "--port",
                "0",
                "--no-mcp",
            ]
        )
        == 2
    )
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == "TBM_LOCAL_DAEMON_INTERNAL_ERROR"

    assert (
        daemon_entry.main(
            ["doctor", "--state-dir", str(state_directory)]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_daemon_http_bind_failure_releases_runtime_and_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    state_directory = tmp_path / ".tbm"
    monkeypatch.setenv(daemon_entry.DURABLE_LOCAL_TOKEN_ENV, TOKEN)
    monkeypatch.setenv(
        daemon_entry.DURABLE_LOCAL_APPLICATION_FACTORY_ENV,
        APPLICATION_FACTORY,
    )
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        listener.listen()
        port = cast(int, listener.getsockname()[1])
        assert (
            daemon_entry.main(
                [
                    "local",
                    "--state-dir",
                    str(state_directory),
                    "--initialize",
                    "--port",
                    str(port),
                    "--no-mcp",
                ]
            )
            == 2
        )
    error = json.loads(capsys.readouterr().err)
    assert error["error"]["code"] == "TBM_LOCAL_DAEMON_INTERNAL_ERROR"

    assert (
        daemon_entry.main(
            ["doctor", "--state-dir", str(state_directory)]
        )
        == 0
    )
    assert json.loads(capsys.readouterr().out)["status"] == "ok"


def test_daemon_default_process_serves_mcp_and_http(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / ".tbm"
    port = _free_port()
    parameters = StdioServerParameters(
        command=sys.executable,
        args=_arguments(
            state_directory,
            port,
            initialize=True,
            no_mcp=False,
        ),
        cwd=str(ROOT),
        env=_environment(),
    )

    async def exercise() -> None:
        async with stdio_client(parameters) as (read, write):
            async with ClientSession(read, write) as session:
                initialized = await session.initialize()
                assert initialized.instructions == (
                    DURABLE_MCP_SERVER_INSTRUCTIONS
                )
                tools = await session.list_tools()
                names = {tool.name for tool in tools.tools}
                assert "tbm_durable_prepare" in names
                capabilities = await session.call_tool(
                    "tbm_durable_capabilities",
                    {},
                )
                assert capabilities.isError is False
                source = _prepare_request().model_copy(
                    update={
                        "request_id": "daemon_cross_transport_prepare",
                        "trace_id": "trace_daemon_cross_transport",
                        "run_id": "run_daemon_cross_transport",
                        "idempotency_key": "daemon_cross_transport",
                    }
                )
                prepared = await session.call_tool(
                    "tbm_durable_prepare",
                    {"request": _request_payload(source)},
                )
                assert prepared.isError is False
                assert isinstance(prepared.structuredContent, dict)
                session_id = cast(
                    str,
                    prepared.structuredContent["result"]["session"][
                        "session_id"
                    ],
                )
                client = DurableAgentHTTPClient(
                    f"http://127.0.0.1:{port}",
                    TOKEN,
                )
                assert client.health()["status"] == "ok"
                loaded = client.get_session({"session_id": session_id})
                assert loaded.result["session"]["status"] == "prepared"

    anyio.run(exercise)


def test_daemon_crash_restart_lock_concurrency_and_expiry(
    tmp_path: Path,
) -> None:
    state_directory = tmp_path / ".tbm"
    clock_file = tmp_path / "clock-offset.txt"
    clock_file.write_text("0", encoding="utf-8")
    port = _free_port()
    first = _start_daemon(
        state_directory,
        port,
        initialize=True,
        clock_file=clock_file,
    )
    try:
        client = _wait_for_client(first, port)
        prepared = client.prepare(
            _request_payload(
                _prepare_request().model_copy(
                    update={"expires_in_seconds": 300}
                )
            )
        )
        session_id = cast(
            str,
            prepared.result["session"]["session_id"],
        )

        with ThreadPoolExecutor(max_workers=8) as pool:
            health = list(
                pool.map(
                    lambda _index: client.health()["status"],
                    range(16),
                )
            )
        assert health == ["ok"] * 16

        cancel_source = _prepare_request().model_copy(
            update={
                "request_id": "daemon_concurrent_cancel_prepare",
                "trace_id": "trace_daemon_concurrent_cancel",
                "run_id": "run_daemon_concurrent_cancel",
                "idempotency_key": "daemon_concurrent_cancel",
            }
        )
        cancel_prepared = client.prepare(
            _request_payload(cancel_source)
        )
        cancel_session = cancel_prepared.result["session"]
        cancel_request = DurableCancelRequest(
            session_id=cast(str, cancel_session["session_id"]),
            expected_session_version=cast(int, cancel_session["version"]),
            reason="concurrent local daemon cancellation",
        )

        def cancel_once(_index: int) -> bool:
            concurrent_client = DurableAgentHTTPClient(
                f"http://127.0.0.1:{port}",
                TOKEN,
            )
            return cast(
                bool,
                concurrent_client.cancel(cancel_request).result["replayed"],
            )

        with ThreadPoolExecutor(max_workers=2) as pool:
            replayed = list(pool.map(cancel_once, range(2)))
        assert sorted(replayed) == [False, True]

        contender = subprocess.run(
            [
                sys.executable,
                *_arguments(
                    state_directory,
                    port,
                    initialize=False,
                    no_mcp=True,
                ),
            ],
            cwd=ROOT,
                env=_environment(clock_file=clock_file),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert contender.returncode == 2
        error = json.loads(contender.stderr)
        assert (
            error["error"]["code"]
            == "TBM_LOCAL_DAEMON_ALREADY_RUNNING"
        )
    finally:
        _kill(first)

    second = _start_daemon(
        state_directory,
        port,
        initialize=False,
        clock_file=clock_file,
    )
    try:
        client = _wait_for_client(second, port)
        current = client.get_session({"session_id": session_id})
        assert current.result["session"]["status"] == "prepared"

        clock_file.write_text("600", encoding="utf-8")
        deadline = time.monotonic() + 10
        status = "prepared"
        while time.monotonic() < deadline:
            status = cast(
                str,
                client.get_session(
                    {"session_id": session_id}
                ).result["session"]["status"],
            )
            if status == "expired":
                break
            time.sleep(0.1)
        assert status == "expired"

        health = subprocess.run(
            [
                sys.executable,
                "-m",
                "trace_backed_memory.daemon_entry",
                "health",
                "--base-url",
                f"http://127.0.0.1:{port}",
            ],
            cwd=ROOT,
            env=_environment(clock_file=clock_file),
            capture_output=True,
            text=True,
            timeout=10,
            check=False,
        )
        assert health.returncode == 0
        assert json.loads(health.stdout)["status"] == "ok"
    finally:
        _kill(second)


def test_daemon_reclaims_an_expired_outbox_lease(
    tmp_path: Path,
) -> None:
    state_directory = prepare_local_state_directory(
        tmp_path / ".tbm",
        create=True,
    )
    database = prepare_local_database(
        state_directory,
        initialize=True,
    )
    application = create_test_application()
    runtime = DurableRuntimeFactory(application.dependencies).open_sqlite(
        database,
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
    )
    try:
        context = application.mcp_contexts.service
        prepared_response = runtime.dispatcher.prepare(
            context,
            _prepare_request(),
        )
        prepared = runtime.sessions.get(
            prepared_response["result"]["session"]["session_id"]
        )
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        runtime.dispatcher.decide(
            context,
            application.mcp_contexts.provider,
            _decide_request(prepared, evaluation),
        )
        decided = runtime.sessions.get(prepared.session_id)
        runtime.dispatcher.finalize(
            context,
            DurableFinalizeRequest(
                session_id=decided.session_id,
                expected_session_version=decided.version,
            ),
        )
        finalized = runtime.sessions.get(decided.session_id)
        runtime.dispatcher.start(
            context,
            DurableStartRequest(
                session_id=finalized.session_id,
                expected_session_version=finalized.version,
            ),
        )
        executing = runtime.sessions.get(finalized.session_id)
        completion = _completion(executing)
        completed = runtime.dispatcher.complete(
            context,
            application.mcp_contexts.evaluator,
            DurableCompleteRequest(
                session_id=completion.session_id,
                expected_session_version=completion.expected_version,
                result=completion.result,
                evidence_artifact_sha256s=list(
                    completion.evidence_artifact_sha256s
                ),
                output_sha256=completion.output_sha256,
                latency_ms=completion.latency_ms,
                cost_usd=completion.cost_usd,
            ),
        )
        event_id = cast(
            str,
            completed["result"]["outbox_event"]["event_id"],
        )
        claimed = runtime.outbox_repository.claim_due(
            worker_id="worker_crashed_before_ack",
            lease_seconds=1,
            limit=1,
        )
        assert len(claimed) == 1
        assert claimed[0].event.event_id == event_id
    finally:
        runtime.close()

    clock_file = tmp_path / "clock-offset.txt"
    clock_file.write_text("120", encoding="utf-8")
    delivery_file = tmp_path / "deliveries.txt"
    port = _free_port()
    process = _start_daemon(
        state_directory,
        port,
        initialize=False,
        delivery_file=delivery_file,
        clock_file=clock_file,
    )
    try:
        _wait_for_client(process, port)
        deadline = time.monotonic() + 10
        while (
            not delivery_file.exists()
            and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert delivery_file.exists()
        assert delivery_file.read_text(encoding="utf-8").splitlines() == [
            event_id
        ]
    finally:
        _kill(process)
