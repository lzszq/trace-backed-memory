from __future__ import annotations

from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from email.message import Message
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
from pathlib import Path
import socket
from types import SimpleNamespace
from threading import Thread
from typing import Iterator

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.agent_wire_v1 as wire_module
import trace_backed_memory.http_entry as http_entry
import trace_backed_memory.http_server as http_server
import trace_backed_memory.sdk as sdk_module
from trace_backed_memory.agent_wire_v1 import (
    AgentProtocolConfiguration,
    AgentProtocolDispatcher,
    CancelRunRequest,
    CompleteRunRequest,
    FinalizeMemoryRequest,
    PrepareMemoryRequest,
)
from trace_backed_memory.http_server import (
    HTTP_REQUEST_MAX_BYTES,
    AgentHTTPServer,
    AgentHTTPServerConfiguration,
)


TOKEN = "test_http_bearer_token_" + "a" * 32


@contextmanager
def _running_server(
    runtime: tbm.LocalAgentMemory,
    *,
    token: str = TOKEN,
) -> Iterator[tuple[AgentHTTPServer, tbm.AgentHTTPClient]]:
    root = Path(__file__).resolve().parents[1]
    dispatcher = AgentProtocolDispatcher(
        AgentProtocolConfiguration(root),
        runtime,
    )
    server = AgentHTTPServer(
        AgentHTTPServerConfiguration(port=0, token=token),
        dispatcher,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    port = server.server_address[1]
    try:
        yield server, tbm.AgentHTTPClient(
            f"http://127.0.0.1:{port}",
            token,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def _static_response_client(
    payload: dict[str, object],
    *,
    status: int = 200,
) -> Iterator[tbm.AgentHTTPClient]:
    body = json.dumps(payload, separators=(",", ":")).encode("utf-8")

    class Handler(BaseHTTPRequestHandler):
        protocol_version = "HTTP/1.1"

        def do_GET(self) -> None:
            self._respond()

        def do_POST(self) -> None:
            length = int(self.headers.get("Content-Length", "0"))
            self.rfile.read(length)
            self._respond()

        def _respond(self) -> None:
            self.close_connection = True
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("X-TBM-Protocol-Version", "tbm.agent.v1")
            self.send_header("Connection", "close")
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, format: str, *args: object) -> None:
            return

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield tbm.AgentHTTPClient(
            f"http://127.0.0.1:{server.server_address[1]}",
            TOKEN,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def _decline(request_id: str) -> dict[str, object]:
    return {
        "request_id": request_id,
        "use_memory": False,
        "allowed_memory_ids": [],
        "blocked_memory_ids": [],
        "reason": "no applicable memory",
        "risk": "none",
        "recommended_injection": "none",
    }


def test_http_sdk_runs_typed_lifecycle_and_matches_direct_dispatcher() -> None:
    root = Path(__file__).resolve().parents[1]
    direct_runtime = tbm.LocalAgentMemory.in_memory()
    http_runtime = tbm.LocalAgentMemory.in_memory()
    direct = AgentProtocolDispatcher(
        AgentProtocolConfiguration(root),
        direct_runtime,
    )
    prepare_payload = {
        "task": "cross-adapter conformance",
        "mode": "planning",
        "trace_id": "trace_http_conformance",
        "run_id": "run_http_conformance",
    }
    try:
        with _running_server(http_runtime) as (_server, client):
            assert client.capabilities().to_dict() == direct.capabilities()
            prepared = client.prepare(prepare_payload)
            direct_prepared = direct.prepare(
                PrepareMemoryRequest.model_validate(prepare_payload)
            )
            direct_request_id = direct_prepared["request_id"]
            assert type(direct_request_id) is str
            direct_prepared.pop("request_id")
            prepared_payload = prepared.to_dict()
            prepared_payload.pop("request_id")
            assert prepared_payload == direct_prepared
            assert prepared.candidate_memory_ids == ()

            finalized = client.finalize(_decline(prepared.request_id))
            direct_finalized = direct.finalize(
                FinalizeMemoryRequest.model_validate(
                    _decline(direct_request_id)
                )
            )
            direct_finalized.pop("request_id")
            direct_finalized.pop("trace_id")
            direct_finalized.pop("decision_id")
            finalized_payload = finalized.to_dict()
            finalized_payload.pop("request_id")
            finalized_payload.pop("trace_id")
            finalized_payload.pop("decision_id")
            assert finalized_payload == direct_finalized
            assert finalized.snippet == ""

            completion = {
                "decision_id": finalized.decision_id,
                "eval_result": "pass",
            }
            completed = client.complete(completion)
            assert completed.eval_result == "pass"

            pending = client.prepare(
                {
                    "task": "cancel over HTTP",
                    "mode": "planning",
                }
            )
            canceled = client.cancel({"request_id": pending.request_id})
            assert canceled.protocol_version == "tbm.agent.v1"
            assert canceled.request_id == pending.request_id
            assert canceled.canceled is True
            health = client.health()
            assert health["pending_request_count"] == 0
            assert health["memory_run_metrics"]["complete_count"] == 1
    finally:
        direct_runtime.close()
        http_runtime.close()


def test_http_requires_exact_bearer_and_strict_request_fields() -> None:
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        with _running_server(runtime) as (server, client):
            wrong = tbm.AgentHTTPClient(
                f"http://127.0.0.1:{server.server_address[1]}",
                "wrong_http_bearer_token_" + "b" * 32,
            )
            with pytest.raises(tbm.AgentMemoryError) as unauthorized:
                wrong.capabilities()
            assert unauthorized.value.code == "TBM_HTTP_UNAUTHORIZED"

            with pytest.raises(tbm.AgentMemoryError) as invalid:
                client.prepare(
                    {
                        "task": "unknown field",
                        "mode": "planning",
                        "caller_repository": "forbidden",
                    }
                )
            assert invalid.value.code == "TBM_AGENT_INVALID_INPUT"
            assert runtime.snapshot()["traces"] == []
    finally:
        runtime.close()


def test_http_rejects_duplicate_bearer_and_hides_unknown_routes() -> None:
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        with _running_server(runtime) as (server, _client):
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_address[1],
                timeout=5,
            )
            connection.putrequest("GET", "/v1/capabilities")
            connection.putheader("Authorization", f"Bearer {TOKEN}")
            connection.putheader("Authorization", f"Bearer {TOKEN}")
            connection.endheaders()
            response = connection.getresponse()
            duplicate_payload = json.loads(response.read())
            connection.close()
            assert response.status == 401
            assert duplicate_payload["error"]["code"] == "TBM_HTTP_UNAUTHORIZED"

            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_address[1],
                timeout=5,
            )
            connection.request("GET", "/v1/not-present")
            response = connection.getresponse()
            hidden_payload = json.loads(response.read())
            connection.close()
            assert response.status == 401
            assert hidden_payload["error"]["code"] == "TBM_HTTP_UNAUTHORIZED"

            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_address[1],
                timeout=5,
            )
            connection.request(
                "GET",
                "/v1/not-present",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            response = connection.getresponse()
            visible_payload = json.loads(response.read())
            connection.close()
            assert response.status == 404
            assert visible_payload["error"]["code"] == "TBM_HTTP_NOT_FOUND"
    finally:
        runtime.close()


@pytest.mark.parametrize("method", ("DELETE", "PATCH", "PUT"))
def test_http_rejects_unsupported_methods_with_json(method: str) -> None:
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        with _running_server(runtime) as (server, _client):
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_address[1],
                timeout=5,
            )
            connection.request(
                method,
                "/v1/health",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()

            assert response.status == 405
            assert response.headers["Allow"] == "GET, POST"
            assert payload["error"]["code"] == "TBM_HTTP_METHOD_NOT_ALLOWED"
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "body",
    (
        b'{"task":"first","task":"second","mode":"planning"}',
        b'{"task":"nonfinite","mode":"planning","minimum_score":NaN}',
        b'["not","an","object"]',
    ),
)
def test_http_rejects_duplicate_nonfinite_and_nonobject_json(body: bytes) -> None:
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        with _running_server(runtime) as (server, _client):
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_address[1],
                timeout=5,
            )
            connection.request(
                "POST",
                "/v1/prepare",
                body=body,
                headers={
                    "Authorization": f"Bearer {TOKEN}",
                    "Content-Type": "application/json",
                },
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()

            assert response.status == 400
            assert payload["error"]["code"] == "TBM_HTTP_INVALID_REQUEST"
            assert runtime.snapshot()["traces"] == []
    finally:
        runtime.close()


def test_http_rejects_oversized_body_before_reading_it() -> None:
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        with _running_server(runtime) as (server, _client):
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_address[1],
                timeout=5,
            )
            connection.putrequest("POST", "/v1/prepare")
            connection.putheader("Authorization", f"Bearer {TOKEN}")
            connection.putheader("Content-Type", "application/json")
            connection.putheader(
                "Content-Length",
                str(HTTP_REQUEST_MAX_BYTES + 1),
            )
            connection.endheaders()
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()

            assert response.status == 400
            assert payload["error"]["code"] == "TBM_HTTP_INVALID_REQUEST"
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("headers", "body"),
    (
        ({"Content-Type": "text/plain"}, b"{}"),
        (
            {
                "Content-Type": "application/json",
                "Transfer-Encoding": "chunked",
            },
            b"",
        ),
        ({"Content-Type": "application/json"}, b""),
    ),
)
def test_http_rejects_invalid_body_framing(
    headers: dict[str, str],
    body: bytes,
) -> None:
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        with _running_server(runtime) as (server, _client):
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_address[1],
                timeout=5,
            )
            request_headers = {
                "Authorization": f"Bearer {TOKEN}",
                **headers,
            }
            connection.request(
                "POST",
                "/v1/prepare",
                body=body,
                headers=request_headers,
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            assert response.status == 400
            assert payload["error"]["code"] == "TBM_HTTP_INVALID_REQUEST"
    finally:
        runtime.close()


def test_http_maps_state_and_unexpected_dispatch_errors(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        with _running_server(runtime) as (_server, client):
            with pytest.raises(tbm.AgentMemoryError) as missing:
                client.finalize(_decline("missing_request"))
            assert missing.value.code == "TBM_AGENT_REQUEST_NOT_FOUND"

        def fail_health(self: AgentProtocolDispatcher) -> dict[str, object]:
            raise RuntimeError("private failure")

        monkeypatch.setattr(AgentProtocolDispatcher, "health", fail_health)
        with _running_server(runtime) as (_server, client):
            with pytest.raises(tbm.AgentMemoryError) as failed:
                client.health()
            assert failed.value.code == "TBM_HTTP_INTERNAL_ERROR"
            assert "private failure" not in str(failed.value)
    finally:
        runtime.close()


def test_http_sanitizes_malformed_protocol_request() -> None:
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        with _running_server(runtime) as (server, _client):
            connection = socket.create_connection(server.server_address, timeout=5)
            try:
                connection.sendall(b"BAD\r\n\r\n")
                payload = connection.recv(4096)
            finally:
                connection.close()
            assert b"TBM_HTTP_PROTOCOL_ERROR" in payload
            assert b"Python/" not in payload
            assert b"text/html" not in payload
    finally:
        runtime.close()


def test_http_rejects_unbounded_content_length_as_invalid_request() -> None:
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        with _running_server(runtime) as (server, _client):
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_address[1],
                timeout=5,
            )
            connection.putrequest("POST", "/v1/prepare")
            connection.putheader("Authorization", f"Bearer {TOKEN}")
            connection.putheader("Content-Type", "application/json")
            connection.putheader("Content-Length", "9" * 5_000)
            connection.endheaders()
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()

            assert response.status == 400
            assert payload["error"]["code"] == "TBM_HTTP_INVALID_REQUEST"
            assert response.headers["Server"] == "tbm"
    finally:
        runtime.close()


def test_http_concurrent_prepares_are_serialized_with_unique_handles() -> None:
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        with _running_server(runtime) as (server, _client):
            base_url = f"http://127.0.0.1:{server.server_address[1]}"

            def prepare(index: int) -> str:
                client = tbm.AgentHTTPClient(base_url, TOKEN)
                result = client.prepare(
                    {
                        "task": f"concurrent request {index}",
                        "mode": "planning",
                        "trace_id": f"trace_http_{index:03d}",
                        "run_id": f"run_http_{index:03d}",
                    }
                )
                return result.request_id

            with ThreadPoolExecutor(max_workers=4) as executor:
                request_ids = tuple(executor.map(prepare, range(8)))

            assert len(set(request_ids)) == 8
            assert runtime.health()["pending_request_count"] == 8
    finally:
        runtime.close()


def test_http_restart_does_not_reconstruct_process_local_pending_request(
    tmp_path: Path,
) -> None:
    database = tmp_path / "http-agent.sqlite3"
    first_runtime = tbm.LocalAgentMemory.open_sqlite(
        database,
        initialize=True,
        check_same_thread=False,
    )
    with _running_server(first_runtime) as (_server, first_client):
        prepared = first_client.prepare(
            {
                "task": "prepare before HTTP restart",
                "mode": "planning",
                "trace_id": "trace_http_restart",
                "run_id": "run_http_restart",
            }
        )
    first_runtime.close()

    second_runtime = tbm.LocalAgentMemory.open_sqlite(
        database,
        check_same_thread=False,
    )
    try:
        with _running_server(second_runtime) as (_server, second_client):
            with pytest.raises(tbm.AgentMemoryError) as stale:
                second_client.finalize(_decline(prepared.request_id))
            assert stale.value.code == "TBM_AGENT_REQUEST_NOT_FOUND"
    finally:
        second_runtime.close()


@pytest.mark.parametrize(
    ("base_url", "token"),
    (
        ("https://127.0.0.1:8080", TOKEN),
        ("http://192.0.2.1:8080", TOKEN),
        ("http://127.0.0.1:8080/path", TOKEN),
        ("http://127.0.0.1:8080", "short"),
    ),
)
def test_sdk_rejects_non_loopback_or_unsafe_configuration(
    base_url: str,
    token: str,
) -> None:
    with pytest.raises(ValueError):
        tbm.AgentHTTPClient(base_url, token)


def test_sdk_rejects_overdeep_request_before_transport() -> None:
    nested: dict[str, object] = {}
    current = nested
    for _ in range(102):
        child: dict[str, object] = {}
        current["nested"] = child
        current = child
    client = tbm.AgentHTTPClient("http://127.0.0.1:1", TOKEN)

    with pytest.raises(tbm.AgentMemoryError) as invalid:
        client.prepare(
            {
                "task": "overdeep SDK input",
                "mode": "planning",
                "tool_outputs": nested,
            }
        )

    assert invalid.value.code == "TBM_SDK_INVALID_INPUT"


def test_sdk_maps_transport_and_request_serialization_errors() -> None:
    client = tbm.AgentHTTPClient("http://127.0.0.1:1", TOKEN, timeout_seconds=0.1)
    with pytest.raises(tbm.AgentMemoryError) as transport:
        client.capabilities()
    assert transport.value.code == "TBM_SDK_TRANSPORT_ERROR"
    assert transport.value.retryable is True

    with pytest.raises(tbm.AgentMemoryError) as mapping:
        client.prepare([])  # type: ignore[arg-type]
    assert mapping.value.code == "TBM_SDK_INVALID_INPUT"

    with pytest.raises(tbm.AgentMemoryError) as serializing:
        client.prepare(
            {
                "task": "invalid object",
                "mode": "planning",
                "value": object(),
            }
        )
    assert serializing.value.code == "TBM_SDK_INVALID_INPUT"


@pytest.mark.parametrize(
    ("operation", "payload", "status"),
    (
        (
            "cancel",
            {
                "protocol_version": "tbm.agent.v1",
                "request_id": "request_001",
                "canceled": False,
            },
            200,
        ),
        (
            "prepare",
            {
                "protocol_version": "tbm.agent.v1",
                "request_id": "x" * 129,
                "trace_id": "trace_001",
                "run_id": "run_001",
                "candidate_memory_ids": [],
                "system_allowed_memory_ids": [],
                "system_blocked": {},
                "prompt": "",
            },
            200,
        ),
        (
            "capabilities",
            {
                **tbm.agent_capabilities().to_dict(),
                "limits": {"gate_candidates": 50},
            },
            200,
        ),
        (
            "capabilities",
            {
                "protocol_version": "tbm.agent.v1",
                "error": {
                    "code": "not-a-protocol-code",
                    "category": "input",
                    "message": "invalid",
                    "operation": "health",
                    "retryable": False,
                },
            },
            400,
        ),
    ),
)
def test_sdk_rejects_schema_invalid_service_responses(
    operation: str,
    payload: dict[str, object],
    status: int,
) -> None:
    with _static_response_client(payload, status=status) as client:
        with pytest.raises(tbm.AgentMemoryError) as invalid:
            if operation == "cancel":
                client.cancel({"request_id": "request_001"})
            elif operation == "prepare":
                client.prepare({"task": "invalid response", "mode": "planning"})
            else:
                client.capabilities()

    assert invalid.value.code == "TBM_SDK_RESPONSE_INVALID"


@pytest.mark.parametrize(
    ("parser", "payload"),
    (
        (
            sdk_module._parse_capabilities,
            {
                **tbm.agent_capabilities().to_dict(),
                "snapshot_version": 0,
            },
        ),
        (
            sdk_module._parse_capabilities,
            {
                **tbm.agent_capabilities().to_dict(),
                "storage_modes": ["remote"],
            },
        ),
        (
            sdk_module._parse_prepared,
            {
                "protocol_version": "wrong",
                "request_id": "request",
                "trace_id": "trace",
                "run_id": "run",
                "candidate_memory_ids": [],
                "system_allowed_memory_ids": [],
                "system_blocked": {},
                "prompt": "",
            },
        ),
        (
            sdk_module._parse_prepared,
            {
                "protocol_version": "tbm.agent.v1",
                "request_id": "request",
                "trace_id": "trace",
                "run_id": "run",
                "candidate_memory_ids": [],
                "system_allowed_memory_ids": [],
                "system_blocked": {"memory": " "},
                "prompt": "",
            },
        ),
        (
            sdk_module._parse_finalized,
            {
                "protocol_version": "tbm.agent.v1",
                "request_id": "request",
                "trace_id": "trace",
                "decision_id": "decision",
                "use_memory": False,
                "allowed_memory_ids": [],
                "blocked_memory_ids": [],
                "reason": "reason",
                "risk": "critical",
                "recommended_injection": "none",
                "snippet": "",
            },
        ),
        (
            sdk_module._parse_completed,
            {
                "protocol_version": "tbm.agent.v1",
                "request_id": None,
                "trace_id": "trace",
                "run_id": "run",
                "decision_id": "decision",
                "eval_result": "unknown",
                "memory_caused_failure": False,
            },
        ),
        (
            lambda value: sdk_module._parse_error(value, "health"),
            {
                "protocol_version": "tbm.agent.v1",
                "error": {
                    "code": "TBM_BAD",
                    "category": "unknown",
                    "message": "message",
                    "operation": "health",
                    "retryable": False,
                },
            },
        ),
        (
            lambda value: sdk_module._parse_error(value, "health"),
            {
                "protocol_version": "tbm.agent.v1",
                "error": {
                    "code": "TBM_BAD",
                    "category": "input",
                    "message": "message",
                    "operation": "health",
                    "retryable": "no",
                },
            },
        ),
    ),
)
def test_sdk_typed_parsers_reject_invalid_fields(
    parser: object,
    payload: dict[str, object],
) -> None:
    with pytest.raises(tbm.AgentMemoryError) as invalid:
        parser(payload)  # type: ignore[operator]
    assert invalid.value.code == "TBM_SDK_RESPONSE_INVALID"


class _FakeResponse:
    def __init__(
        self,
        body: bytes,
        *,
        headers: Message,
    ) -> None:
        self.headers = headers
        self._body = body

    def read(self, _limit: int) -> bytes:
        return self._body


@pytest.mark.parametrize(
    ("body", "header_values"),
    (
        (b"{}", {}),
        (
            b"{}",
            {
                "X-TBM-Protocol-Version": "wrong",
                "Content-Type": "application/json",
                "Content-Length": "2",
            },
        ),
        (
            b"not-json",
            {
                "X-TBM-Protocol-Version": "tbm.agent.v1",
                "Content-Type": "application/json",
                "Content-Length": "8",
            },
        ),
        (
            b"{}",
            {
                "X-TBM-Protocol-Version": "tbm.agent.v1",
                "Content-Type": "application/json",
                "Content-Length": "3",
            },
        ),
        (
            b"",
            {
                "X-TBM-Protocol-Version": "tbm.agent.v1",
                "Content-Type": "application/json",
                "Content-Length": "9" * 32,
            },
        ),
        (
            b"",
            {
                "X-TBM-Protocol-Version": "tbm.agent.v1",
                "Content-Type": "application/json",
                "Content-Length": str(sdk_module.SDK_RESPONSE_MAX_BYTES + 1),
            },
        ),
    ),
)
def test_sdk_rejects_invalid_response_headers_and_bodies(
    body: bytes,
    header_values: dict[str, str],
) -> None:
    headers = Message()
    for name, value in header_values.items():
        headers[name] = value
    response = _FakeResponse(body, headers=headers)
    with pytest.raises(tbm.AgentMemoryError) as invalid:
        tbm.AgentHTTPClient._read_response(response, "health")
    assert invalid.value.code == "TBM_SDK_RESPONSE_INVALID"


def test_http_server_configuration_rejects_non_loopback_bind() -> None:
    with pytest.raises(ValueError, match="loopback"):
        AgentHTTPServerConfiguration(
            host="0.0.0.0",
            token=TOKEN,
        )


@pytest.mark.parametrize(
    "configuration",
    (
        {"host": "localhost", "token": TOKEN},
        {"port": -1, "token": TOKEN},
        {"port": True, "token": TOKEN},
        {"token": "short"},
        {"token": " " + TOKEN},
    ),
)
def test_http_server_configuration_rejects_invalid_values(
    configuration: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        AgentHTTPServerConfiguration(**configuration)  # type: ignore[arg-type]


def test_dispatcher_guards_and_public_error_mapping() -> None:
    root = Path(__file__).resolve().parents[1]
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        with pytest.raises(ValueError):
            AgentProtocolConfiguration(Path("."))
        with pytest.raises(ValueError):
            AgentProtocolConfiguration(root, " ")
        with pytest.raises(TypeError):
            AgentProtocolDispatcher(
                AgentProtocolConfiguration(root),
                object(),  # type: ignore[arg-type]
            )
        dispatcher = AgentProtocolDispatcher(
            AgentProtocolConfiguration(root),
            runtime,
        )
        for callback in (
            dispatcher.prepare,
            dispatcher.finalize,
            dispatcher.complete,
            dispatcher.cancel,
        ):
            with pytest.raises(tbm.AgentMemoryError) as invalid:
                callback(object())  # type: ignore[arg-type]
            assert invalid.value.code == "TBM_AGENT_INVALID_INPUT"
        internal = wire_module.public_agent_error(RuntimeError("private"), "open")
        assert internal.code == "TBM_AGENT_INTERNAL_ERROR"
        assert "private" not in str(internal)
    finally:
        runtime.close()


@pytest.mark.parametrize(
    "payload",
    (
        {"task": " ", "mode": "planning"},
        {"task": "valid", "mode": "planning", "run_id": "\t"},
        {"task": "valid", "mode": "planning", "trace_id": "\n"},
    ),
)
def test_wire_prepare_request_rejects_blank_values(
    payload: dict[str, object],
) -> None:
    with pytest.raises(ValueError):
        PrepareMemoryRequest.model_validate(payload)


def test_dispatcher_rejects_nonexact_configuration_and_auth_runtime() -> None:
    root = Path(__file__).resolve().parents[1]
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        with pytest.raises(TypeError, match="configuration"):
            AgentProtocolDispatcher(  # type: ignore[arg-type]
                object(),
                runtime,
            )
        with pytest.raises(TypeError, match="authenticated_runtime"):
            AgentProtocolDispatcher(
                AgentProtocolConfiguration(root),
                runtime,
                authenticated_runtime=object(),  # type: ignore[arg-type]
            )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("error", "expected_code", "expected_category"),
    (
        (
            tbm.AgentMemoryError(
                "TBM_EXISTING",
                "state",
                "prepare",
                "existing",
            ),
            "TBM_EXISTING",
            "state",
        ),
        (TypeError("invalid"), "TBM_AGENT_INVALID_INPUT", "input"),
        (ValueError("invalid"), "TBM_AGENT_INVALID_INPUT", "input"),
        (OverflowError("invalid"), "TBM_AGENT_INVALID_INPUT", "input"),
        (RuntimeError("private"), "TBM_CUSTOM_INTERNAL", "internal"),
    ),
)
def test_public_agent_error_maps_supported_categories(
    error: Exception,
    expected_code: str,
    expected_category: str,
) -> None:
    public = wire_module.public_agent_error(
        error,
        "prepare",
        internal_code="TBM_CUSTOM_INTERNAL",
        internal_message="sanitized",
    )
    assert public.code == expected_code
    assert public.category == expected_category
    if type(error) is RuntimeError:
        assert str(public) == "sanitized"
        assert public.retryable is True


def test_dispatcher_sanitizes_health_complete_and_cancel_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    runtime = tbm.LocalAgentMemory.in_memory()
    dispatcher = AgentProtocolDispatcher(
        AgentProtocolConfiguration(root),
        runtime,
    )

    def fail(*_args: object, **_kwargs: object) -> object:
        raise RuntimeError("private")

    try:
        monkeypatch.setattr(tbm.LocalAgentMemory, "health", fail)
        with pytest.raises(tbm.AgentMemoryError) as health:
            dispatcher.health()
        assert health.value.code == "TBM_AGENT_INTERNAL_ERROR"

        monkeypatch.setattr(tbm.LocalAgentMemory, "complete", fail)
        with pytest.raises(tbm.AgentMemoryError) as complete:
            dispatcher.complete(
                CompleteRunRequest(
                    decision_id="decision_missing",
                    eval_result="pass",
                )
            )
        assert complete.value.code == "TBM_AGENT_INTERNAL_ERROR"

        monkeypatch.setattr(tbm.LocalAgentMemory, "cancel", fail)
        with pytest.raises(tbm.AgentMemoryError) as cancel:
            dispatcher.cancel(CancelRunRequest(request_id="request_missing"))
        assert cancel.value.code == "TBM_AGENT_INTERNAL_ERROR"
    finally:
        runtime.close()


def test_http_server_rejects_nonexact_constructor_inputs() -> None:
    root = Path(__file__).resolve().parents[1]
    runtime = tbm.LocalAgentMemory.in_memory()
    dispatcher = AgentProtocolDispatcher(
        AgentProtocolConfiguration(root),
        runtime,
    )
    try:
        with pytest.raises(TypeError, match="configuration"):
            AgentHTTPServer(object(), dispatcher)  # type: ignore[arg-type]
        with pytest.raises(TypeError, match="dispatcher"):
            AgentHTTPServer(
                AgentHTTPServerConfiguration(port=0, token=TOKEN),
                object(),  # type: ignore[arg-type]
            )
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("error", "status"),
    (
        (
            tbm.AgentMemoryError(
                "TBM_HTTP_NOT_FOUND",
                "input",
                "open",
                "missing",
            ),
            404,
        ),
        (
            tbm.AgentMemoryError(
                "TBM_HTTP_UNAUTHORIZED",
                "input",
                "open",
                "unauthorized",
            ),
            401,
        ),
        (
            tbm.AgentMemoryError("TBM_INPUT", "input", "open", "input"),
            400,
        ),
        (
            tbm.AgentMemoryError("TBM_STATE", "state", "open", "state"),
            409,
        ),
        (
            tbm.AgentMemoryError("TBM_CLOSED", "closed", "open", "closed"),
            503,
        ),
        (
            tbm.AgentMemoryError(
                "TBM_INTERNAL",
                "internal",
                "open",
                "internal",
            ),
            500,
        ),
    ),
)
def test_http_error_status_mapping(
    error: tbm.AgentMemoryError,
    status: int,
) -> None:
    assert http_server._status_for_error(error) == status


def test_http_post_unknown_validation_and_unexpected_paths(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        with _running_server(runtime) as (server, _client):
            for path, body, expected_status, expected_code in (
                ("/v1/not-present", b"{}", 404, "TBM_HTTP_NOT_FOUND"),
                (
                    "/v1/prepare",
                    b'{"task":"missing mode"}',
                    400,
                    "TBM_AGENT_INVALID_INPUT",
                ),
            ):
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    server.server_address[1],
                    timeout=5,
                )
                connection.request(
                    "POST",
                    path,
                    body=body,
                    headers={
                        "Authorization": f"Bearer {TOKEN}",
                        "Content-Type": "application/json",
                    },
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                assert response.status == expected_status
                assert payload["error"]["code"] == expected_code

        def fail_prepare(
            self: AgentProtocolDispatcher,
            request: PrepareMemoryRequest,
        ) -> dict[str, object]:
            raise RuntimeError("private")

        monkeypatch.setattr(AgentProtocolDispatcher, "prepare", fail_prepare)
        with _running_server(runtime) as (_server, client):
            with pytest.raises(tbm.AgentMemoryError) as failed:
                client.prepare({"task": "failure", "mode": "planning"})
            assert failed.value.code == "TBM_HTTP_INTERNAL_ERROR"
    finally:
        runtime.close()


def test_http_replaces_unserializable_runtime_response(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    runtime = tbm.LocalAgentMemory.in_memory()

    def invalid_health(
        self: AgentProtocolDispatcher,
    ) -> dict[str, object]:
        return {"invalid": float("nan")}

    monkeypatch.setattr(AgentProtocolDispatcher, "health", invalid_health)
    try:
        with _running_server(runtime) as (_server, client):
            with pytest.raises(tbm.AgentMemoryError) as failed:
                client.health()
            assert failed.value.code == "TBM_HTTP_RESPONSE_INVALID"
    finally:
        runtime.close()


def test_http_cli_reads_token_from_environment_and_closes_cleanly(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    served = False

    def serve_once(
        self: AgentHTTPServer,
        poll_interval: float = 0.5,
    ) -> None:
        nonlocal served
        assert poll_interval == 0.25
        served = True

    monkeypatch.setenv("TEST_TBM_HTTP_TOKEN", TOKEN)
    monkeypatch.setattr(AgentHTTPServer, "serve_forever", serve_once)

    assert (
        http_server.main(
            [
                "--repo-path",
                str(root),
                "--port",
                "0",
                "--token-env",
                "TEST_TBM_HTTP_TOKEN",
                "--memory",
            ]
        )
        == 0
    )
    assert served is True


def test_http_cli_handles_keyboard_interrupt(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]

    def interrupt(
        self: AgentHTTPServer,
        poll_interval: float = 0.5,
    ) -> None:
        raise KeyboardInterrupt

    monkeypatch.setenv("TEST_TBM_HTTP_TOKEN", TOKEN)
    monkeypatch.setattr(AgentHTTPServer, "serve_forever", interrupt)
    assert (
        http_server.main(
            [
                "--repo-path",
                str(root),
                "--port",
                "0",
                "--token-env",
                "TEST_TBM_HTTP_TOKEN",
                "--memory",
            ]
        )
        == 0
    )


@pytest.mark.parametrize(
    "extra_arguments",
    (
        ("--tenant", " "),
        ("--token-env", "BAD=NAME"),
    ),
)
def test_http_cli_rejects_invalid_declared_values(
    extra_arguments: tuple[str, str],
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.setenv("TEST_TBM_HTTP_TOKEN", TOKEN)
    arguments = [
        "--repo-path",
        str(root),
        "--port",
        "0",
        "--token-env",
        "TEST_TBM_HTTP_TOKEN",
        "--memory",
        *extra_arguments,
    ]
    assert http_server.main(arguments) == 2
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["code"] == "TBM_HTTP_STARTUP_FAILED"
    assert payload["error"]["category"] == "input"


def test_http_cli_sanitizes_os_startup_failure(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = Path(__file__).resolve().parents[1]

    def fail_open(
        args: object,
        repo_path: Path,
    ) -> tbm.LocalAgentMemory:
        raise OSError(f"private failure at {repo_path}")

    monkeypatch.setenv("TEST_TBM_HTTP_TOKEN", TOKEN)
    monkeypatch.setattr(http_server, "_open_runtime", fail_open)
    assert (
        http_server.main(
            [
                "--repo-path",
                str(root),
                "--port",
                "0",
                "--token-env",
                "TEST_TBM_HTTP_TOKEN",
                "--memory",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["category"] == "internal"
    assert payload["error"]["retryable"] is True
    assert str(root) not in payload["error"]["message"]


def test_http_cli_reports_missing_token_without_secret_or_path(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    root = Path(__file__).resolve().parents[1]
    monkeypatch.delenv("TEST_TBM_HTTP_TOKEN", raising=False)

    assert (
        http_server.main(
            [
                "--repo-path",
                str(root),
                "--port",
                "0",
                "--token-env",
                "TEST_TBM_HTTP_TOKEN",
                "--memory",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["code"] == "TBM_HTTP_STARTUP_FAILED"
    assert str(root) not in payload["error"]["message"]


def test_http_cli_rejects_repo_path_that_is_not_a_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    repo_file = tmp_path / "not-a-directory"
    repo_file.write_text("fixture", encoding="utf-8")
    monkeypatch.setenv("TEST_TBM_HTTP_TOKEN", TOKEN)
    assert (
        http_server.main(
            [
                "--repo-path",
                str(repo_file),
                "--port",
                "0",
                "--token-env",
                "TEST_TBM_HTTP_TOKEN",
                "--memory",
            ]
        )
        == 2
    )
    payload = json.loads(capsys.readouterr().err)
    assert payload["error"]["category"] == "input"


def test_http_open_runtime_storage_profiles(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    root = Path(__file__).resolve().parents[1]
    sqlite_runtime = http_server._open_runtime(
        SimpleNamespace(
            memory=False,
            sqlite=Path("agent.sqlite3"),
            postgres_env=None,
        ),
        tmp_path,
    )
    try:
        assert sqlite_runtime.health()["protocol_version"] == "tbm.agent.v1"
        assert (tmp_path / "agent.sqlite3").is_file()
    finally:
        sqlite_runtime.close()

    with pytest.raises(ValueError, match="parent"):
        http_server._open_runtime(
            SimpleNamespace(
                memory=False,
                sqlite=Path("missing") / "agent.sqlite3",
                postgres_env=None,
            ),
            tmp_path,
        )
    with pytest.raises(ValueError, match="environment variable name"):
        http_server._open_runtime(
            SimpleNamespace(
                memory=False,
                sqlite=None,
                postgres_env="BAD=NAME",
            ),
            root,
        )
    monkeypatch.delenv("MISSING_POSTGRES_DSN", raising=False)
    with pytest.raises(ValueError, match="missing"):
        http_server._open_runtime(
            SimpleNamespace(
                memory=False,
                sqlite=None,
                postgres_env="MISSING_POSTGRES_DSN",
            ),
            root,
        )

    captured: list[str] = []

    def open_postgres(
        cls: type[tbm.LocalAgentMemory],
        conninfo: str,
    ) -> tbm.LocalAgentMemory:
        captured.append(conninfo)
        return cls.in_memory()

    monkeypatch.setenv("TEST_POSTGRES_DSN", "postgresql://local/test")
    monkeypatch.setattr(
        tbm.LocalAgentMemory,
        "open_postgres",
        classmethod(open_postgres),
    )
    postgres_runtime = http_server._open_runtime(
        SimpleNamespace(
            memory=False,
            sqlite=None,
            postgres_env="TEST_POSTGRES_DSN",
        ),
        root,
    )
    postgres_runtime.close()
    assert captured == ["postgresql://local/test"]


@pytest.mark.parametrize(
    ("base_url", "token", "timeout"),
    (
        (object(), TOKEN, 30),
        ("http://localhost:8765", TOKEN, 30),
        ("http://127.0.0.1:not-a-port", TOKEN, 30),
        ("http://127.0.0.1:8765?query=1", TOKEN, 30),
        ("http://user@127.0.0.1:8765", TOKEN, 30),
        ("http://127.0.0.1:8765", object(), 30),
        ("http://127.0.0.1:8765", TOKEN, True),
        ("http://127.0.0.1:8765", TOKEN, 0),
        ("http://127.0.0.1:8765", TOKEN, 301),
        ("http://127.0.0.1:8765", TOKEN, "30"),
    ),
)
def test_sdk_rejects_additional_invalid_configuration(
    base_url: object,
    token: object,
    timeout: object,
) -> None:
    with pytest.raises(ValueError):
        tbm.AgentHTTPClient(  # type: ignore[arg-type]
            base_url,
            token,
            timeout_seconds=timeout,
        )


def test_sdk_canceled_serialization_and_redirect_policy() -> None:
    canceled = sdk_module.AgentCanceledRun(
        protocol_version="tbm.agent.v1",
        request_id="request_001",
        canceled=True,
    )
    assert canceled.to_dict() == {
        "protocol_version": "tbm.agent.v1",
        "request_id": "request_001",
        "canceled": True,
    }
    assert sdk_module._NoRedirect().redirect_request() is None


def test_sdk_rejects_request_over_wire_limit(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = tbm.AgentHTTPClient("http://127.0.0.1:1", TOKEN)
    monkeypatch.setattr(sdk_module, "CLI_JSON_FILE_MAX_BYTES", 8)
    with pytest.raises(tbm.AgentMemoryError) as invalid:
        client.prepare({"task": "too large", "mode": "planning"})
    assert invalid.value.code == "TBM_SDK_INVALID_INPUT"


@pytest.mark.parametrize(
    "payload",
    (
        [],
        {"protocol_version": "tbm.agent.v1"},
        {
            "protocol_version": "tbm.agent.v1",
            "error": {"code": "TBM_BAD"},
        },
    ),
)
def test_sdk_error_parser_rejects_invalid_shapes(payload: object) -> None:
    with pytest.raises(tbm.AgentMemoryError) as invalid:
        sdk_module._parse_error(payload, "health")
    assert invalid.value.code == "TBM_SDK_RESPONSE_INVALID"


def test_http_entry_lazy_dependency_paths(
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    monkeypatch.setattr(http_server, "main", lambda argv: 17)
    assert http_entry.main(["--help"]) == 17

    import builtins

    original_import = builtins.__import__

    def missing_dependency(
        name: str,
        globals: dict[str, object] | None = None,
        locals: dict[str, object] | None = None,
        fromlist: tuple[str, ...] = (),
        level: int = 0,
    ) -> object:
        if name == "trace_backed_memory.http_server" or (
            name == "http_server" and level == 1
        ):
            raise ModuleNotFoundError(
                "No module named 'pydantic'",
                name="pydantic",
            )
        return original_import(name, globals, locals, fromlist, level)

    monkeypatch.setattr(builtins, "__import__", missing_dependency)
    assert http_entry.main([]) == 2
    assert "[service]" in capsys.readouterr().err


def test_http_server_applies_connection_timeout_and_worker_bound() -> None:
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        root = Path(__file__).resolve().parents[1]
        server = AgentHTTPServer(
            AgentHTTPServerConfiguration(port=0, token=TOKEN),
            AgentProtocolDispatcher(
                AgentProtocolConfiguration(root),
                runtime,
            ),
        )
        client_socket = socket.create_connection(server.server_address, timeout=5)
        try:
            accepted, _address = server.get_request()
            try:
                assert (
                    accepted.gettimeout()
                    == http_server.HTTP_CONNECTION_TIMEOUT_SECONDS
                )
                assert http_server.HTTP_MAX_WORKERS > 0
                assert server.request_queue_size > 0
            finally:
                accepted.close()
        finally:
            client_socket.close()
            server.server_close()
    finally:
        runtime.close()
