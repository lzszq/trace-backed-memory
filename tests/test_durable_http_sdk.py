from __future__ import annotations

import asyncio
from contextlib import contextmanager
import http.client
import json
from pathlib import Path
import secrets
import socket
import ssl
from threading import Event, Lock, Thread
from typing import Iterator, cast

import pytest

from tests.test_durable_agent_v3 import _completion, _stack
from tests.test_durable_agent_wire_v1 import (
    _decide_request,
    _dispatcher,
    _prepare_request,
)
from tests.test_durable_execution_v3 import EVALUATOR_CONTEXT
from tests.test_durable_runtime_v3 import _Clock, _dependencies
from tests.test_durable_semantic_gate_v3 import _context as _provider_context
from trace_backed_memory.durable_agent_wire_v1 import (
    DurableAgentProtocolDispatcher,
    DurableAgentWireError,
    DurableCancelRequest,
    DurableCompleteRequest,
    DurableFinalizeRequest,
    DurableGetSessionRequest,
    DurableReplayRequest,
    DurableResumeRequest,
    DurableStartRequest,
)
from trace_backed_memory.durable_http_server import (
    DurableAgentHTTPServer,
    DurableHTTPAuthenticatedContexts,
    DurableHTTPAuthenticationRequest,
    DurableHTTPServerConfiguration,
)
from trace_backed_memory.durable_runtime_v3 import DurableRuntimeFactory
from trace_backed_memory.durable_sdk import (
    AsyncDurableAgentHTTPClient,
    DurableAgentHTTPClient,
    DurableAgentHTTPClientError,
    DurableAgentHTTPResponse,
)
import trace_backed_memory.durable_sdk as durable_sdk_module
import trace_backed_memory.durable_http_server as durable_http_server_module


TOKEN = "durable_http_test_token_" + "a" * 48
WRONG_TOKEN = "durable_http_wrong_token_" + "b" * 48


def _http_stack(**kwargs):
    return _stack(check_same_thread=False, **kwargs)


def _raw_http_json(
    server: DurableAgentHTTPServer,
    request: bytes,
) -> tuple[int, dict[str, object]]:
    connection = socket.create_connection(
        ("127.0.0.1", server.server_address[1]),
        timeout=5,
    )
    try:
        connection.sendall(request)
        response = http.client.HTTPResponse(connection)
        response.begin()
        payload = json.loads(response.read())
        return response.status, payload
    finally:
        connection.close()


class _Authenticator:
    def __init__(self, stack) -> None:
        self._stack = stack
        self.operations: list[str] = []

    def __call__(
        self,
        request: DurableHTTPAuthenticationRequest,
    ) -> DurableHTTPAuthenticatedContexts:
        self.operations.append(request.operation)
        if request.authorization is None or not secrets.compare_digest(
            request.authorization,
            f"Bearer {TOKEN}",
        ):
            raise ValueError("untrusted credential")
        return DurableHTTPAuthenticatedContexts(
            self._stack.context,
            provider=_provider_context(),
            evaluator=EVALUATOR_CONTEXT,
        )


@contextmanager
def _running_server(
    stack,
    *,
    expose_content: bool = True,
    authenticator=None,
) -> Iterator[
    tuple[
        DurableAgentHTTPServer,
        DurableAgentHTTPClient,
        _Authenticator,
    ]
]:
    dispatcher = _dispatcher(
        stack,
        expose_injection_content=expose_content,
        expose_replay_content=expose_content,
    )
    live_authenticator = authenticator or _Authenticator(stack)
    server = DurableAgentHTTPServer(
        DurableHTTPServerConfiguration(port=0),
        dispatcher,
        live_authenticator,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    client = DurableAgentHTTPClient(
        f"http://127.0.0.1:{server.server_address[1]}",
        TOKEN,
    )
    try:
        yield server, client, live_authenticator
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@contextmanager
def _running_runtime_server(
    runtime,
    context,
) -> Iterator[DurableAgentHTTPClient]:
    def authenticate(
        request: DurableHTTPAuthenticationRequest,
    ) -> DurableHTTPAuthenticatedContexts:
        if request.authorization != f"Bearer {TOKEN}":
            raise ValueError("untrusted credential")
        return DurableHTTPAuthenticatedContexts(
            context,
            provider=_provider_context(),
            evaluator=EVALUATOR_CONTEXT,
        )

    server = DurableAgentHTTPServer(
        DurableHTTPServerConfiguration(port=0),
        runtime.dispatcher,
        authenticate,
    )
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield DurableAgentHTTPClient(
            f"http://127.0.0.1:{server.server_address[1]}",
            TOKEN,
        )
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


def test_durable_http_sdk_runs_complete_persisted_lifecycle() -> None:
    stack = _http_stack(
        permissions=(
            "memory:retrieve",
            "gate_session:transition",
            "artifact:read",
        )
    )
    try:
        with _running_server(stack) as (_server, client, authenticator):
            capabilities = client.capabilities()
            assert capabilities["protocol_version"] == (
                "tbm.durable-agent-wire.v1"
            )
            assert capabilities["transport_profile"] == "durable-v3"
            assert capabilities["transport_authentication"] == "required"
            assert capabilities["process_local_records"] == []
            openapi = client.openapi()
            assert openapi["openapi"] == "3.1.0"
            assert openapi["security"] == [{"bearerAuth": []}]
            assert set(openapi["components"]["securitySchemes"]) == {
                "bearerAuth"
            }
            assert set(openapi["paths"]) == {
                "/durable/v1/openapi",
                "/durable/v1/capabilities",
                "/durable/v1/health",
                "/durable/v1/prepare",
                "/durable/v1/decide",
                "/durable/v1/finalize",
                "/durable/v1/start",
                "/durable/v1/resume",
                "/durable/v1/abandon",
                "/durable/v1/complete",
                "/durable/v1/cancel",
                "/durable/v1/get-session",
                "/durable/v1/export-replay",
            }
            assert client.health()["durable_sessions"] is True

            prepared_response = client.prepare(_prepare_request())
            prepared = stack.sessions.get(
                prepared_response.result["session"]["session_id"]
            )
            evaluation = stack.evidence.load_evaluation(
                prepared.system_gate_evaluation_id
            )

            decided_response = client.decide(
                _decide_request(prepared, evaluation)
            )
            decided = stack.sessions.get(prepared.session_id)
            assert decided_response.operation == "decide"
            assert decided_response.result["session"]["status"] == "decided"

            finalized_response = client.finalize(
                DurableFinalizeRequest(
                    session_id=decided.session_id,
                    expected_session_version=decided.version,
                )
            )
            finalized = stack.sessions.get(decided.session_id)
            assert finalized_response.result["content_exposed"] is True
            assert finalized_response.result["snippet"]

            started_response = client.start(
                DurableStartRequest(
                    session_id=finalized.session_id,
                    expected_session_version=finalized.version,
                )
            )
            executing = stack.sessions.get(finalized.session_id)
            assert started_response.result["session"]["status"] == "executing"

            resumed_response = client.resume(
                DurableResumeRequest(
                    session_id=executing.session_id,
                    expected_session_version=executing.version,
                    lease_seconds=2_700,
                )
            )
            executing = stack.sessions.get(executing.session_id)
            assert resumed_response.result["replayed"] is True

            completion = _completion(executing)
            completed_response = client.complete(
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
                )
            )
            completed = stack.sessions.get(executing.session_id)
            assert completed_response.result["session"]["status"] == "completed"
            assert completed_response.result["outcome"]["result"] == "pass"

            session_response = client.get_session(
                DurableGetSessionRequest(session_id=completed.session_id)
            )
            assert session_response.result["session"] == completed.to_dict()

            replay_response = client.export_replay(
                DurableReplayRequest(
                    session_id=completed.session_id,
                    expected_session_version=completed.version,
                    allowed_classifications=["internal"],
                )
            )
            assert replay_response.result["content_exposed"] is True
            assert replay_response.result["bundle"]["artifacts"]

            assert authenticator.operations == [
                "capabilities",
                "openapi",
                "health",
                "prepare",
                "decide",
                "finalize",
                "start",
                "resume",
                "complete",
                "get_session",
                "export_replay",
            ]
    finally:
        stack.close()


def test_durable_http_sqlite_restarts_after_every_lifecycle_state(
    tmp_path: Path,
) -> None:
    database = tmp_path / "durable-http-restarts.sqlite3"
    dependencies, context = _dependencies(_Clock())
    factory = DurableRuntimeFactory(dependencies)

    runtime = factory.open_sqlite(
        database,
        initialize=True,
        expose_injection_content=True,
        expose_replay_content=True,
    )
    try:
        prepare_request = _prepare_request()
        with _running_runtime_server(runtime, context) as client:
            prepared_response = client.prepare(prepare_request)
            session_id = prepared_response.result["session"]["session_id"]
    finally:
        runtime.close()

    runtime = factory.open_sqlite(
        database,
        expose_injection_content=True,
        expose_replay_content=True,
    )
    try:
        prepared = runtime.sessions.get(session_id)
        evaluation = runtime.evidence_repository.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        decide_request = _decide_request(prepared, evaluation)
        with _running_runtime_server(runtime, context) as client:
            prepare_retry = client.prepare(prepare_request)
            assert prepare_retry.result == prepared_response.result
            decided_response = client.decide(decide_request)
    finally:
        runtime.close()

    runtime = factory.open_sqlite(
        database,
        expose_injection_content=True,
        expose_replay_content=True,
    )
    try:
        decided = runtime.sessions.get(session_id)
        finalize_request = DurableFinalizeRequest(
            session_id=session_id,
            expected_session_version=decided.version,
        )
        with _running_runtime_server(runtime, context) as client:
            decide_retry = client.decide(decide_request)
            assert decide_retry.result["replayed"] is True
            finalized_response = client.finalize(finalize_request)
    finally:
        runtime.close()

    runtime = factory.open_sqlite(
        database,
        expose_injection_content=True,
        expose_replay_content=True,
    )
    try:
        finalized = runtime.sessions.get(session_id)
        start_request = DurableStartRequest(
            session_id=session_id,
            expected_session_version=finalized.version,
        )
        with _running_runtime_server(runtime, context) as client:
            finalize_retry = client.finalize(finalize_request)
            assert finalize_retry.result["replayed"] is True
            started_response = client.start(start_request)
    finally:
        runtime.close()

    runtime = factory.open_sqlite(
        database,
        expose_injection_content=True,
        expose_replay_content=True,
    )
    try:
        executing = runtime.sessions.get(session_id)
        completion = _completion(executing)
        complete_request = DurableCompleteRequest(
            session_id=session_id,
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
        )
        with _running_runtime_server(runtime, context) as client:
            start_retry = client.start(start_request)
            assert start_retry.result["replayed"] is True
            completed_response = client.complete(complete_request)
    finally:
        runtime.close()

    runtime = factory.open_sqlite(
        database,
        expose_injection_content=True,
        expose_replay_content=True,
    )
    try:
        completed = runtime.sessions.get(session_id)
        with _running_runtime_server(runtime, context) as client:
            complete_retry = client.complete(complete_request)
            assert complete_retry.result["replayed"] is True
            assert complete_retry.result["outcome"] == (
                completed_response.result["outcome"]
            )
            loaded = client.get_session(
                DurableGetSessionRequest(session_id=session_id)
            )
            assert loaded.result["session"]["status"] == "completed"
            replay = client.export_replay(
                DurableReplayRequest(
                    session_id=session_id,
                    expected_session_version=completed.version,
                    allowed_classifications=["internal"],
                )
            )
            assert replay.result["content_exposed"] is True
        assert decided_response.result["session"]["status"] == "decided"
        assert finalized_response.result["session"]["status"] == "finalized"
        assert started_response.result["session"]["status"] == "executing"
    finally:
        runtime.close()


def test_durable_http_continues_session_across_server_restart() -> None:
    stack = _http_stack()
    try:
        with _running_server(stack) as (_server, client, _authenticator):
            prepared_response = client.prepare(_prepare_request())
            session_id = prepared_response.result["session"]["session_id"]

        prepared = stack.sessions.get(session_id)
        evaluation = stack.evidence.load_evaluation(
            prepared.system_gate_evaluation_id
        )
        with _running_server(stack) as (_server, client, _authenticator):
            decided = client.decide(_decide_request(prepared, evaluation))
            assert decided.result["session"]["status"] == "decided"
            loaded = client.get_session(
                DurableGetSessionRequest(session_id=session_id)
            )
            assert loaded.result["session"]["version"] > prepared.version
    finally:
        stack.close()


def test_durable_http_sdk_cancel_and_abandon_paths() -> None:
    cancel_stack = _http_stack()
    try:
        with _running_server(cancel_stack) as (
            _server,
            client,
            _authenticator,
        ):
            prepared_response = client.prepare(_prepare_request())
            prepared = cancel_stack.sessions.get(
                prepared_response.result["session"]["session_id"]
            )
            canceled = client.cancel(
                DurableCancelRequest(
                    session_id=prepared.session_id,
                    expected_session_version=prepared.version,
                    reason="cancel through authenticated HTTP",
                )
            )
            replayed = client.cancel(
                DurableCancelRequest(
                    session_id=prepared.session_id,
                    expected_session_version=prepared.version,
                    reason="cancel through authenticated HTTP",
                )
            )
            assert canceled.result["session"]["status"] == "canceled"
            assert canceled.result["replayed"] is False
            assert replayed.result["replayed"] is True
    finally:
        cancel_stack.close()

    abandon_stack = _http_stack()
    try:
        with _running_server(abandon_stack) as (
            _server,
            client,
            _authenticator,
        ):
            prepared_response = client.prepare(_prepare_request())
            prepared = abandon_stack.sessions.get(
                prepared_response.result["session"]["session_id"]
            )
            evaluation = abandon_stack.evidence.load_evaluation(
                prepared.system_gate_evaluation_id
            )
            client.decide(_decide_request(prepared, evaluation))
            decided = abandon_stack.sessions.get(prepared.session_id)
            client.finalize(
                DurableFinalizeRequest(
                    session_id=decided.session_id,
                    expected_session_version=decided.version,
                )
            )
            finalized = abandon_stack.sessions.get(decided.session_id)
            client.start(
                DurableStartRequest(
                    session_id=finalized.session_id,
                    expected_session_version=finalized.version,
                )
            )
            executing = abandon_stack.sessions.get(finalized.session_id)
            abandoned = client.abandon(
                {
                    "session_id": executing.session_id,
                    "expected_session_version": executing.version,
                    "reason": "abandon through authenticated HTTP",
                }
            )
            replayed = client.abandon(
                {
                    "session_id": executing.session_id,
                    "expected_session_version": executing.version,
                    "reason": "abandon through authenticated HTTP",
                }
            )
            assert abandoned.result["session"]["status"] == "abandoned"
            assert abandoned.result["replayed"] is False
            assert replayed.result["replayed"] is True
    finally:
        abandon_stack.close()


def test_durable_http_authenticates_before_route_or_payload_dispatch() -> None:
    stack = _http_stack()
    try:
        with _running_server(stack) as (server, client, _authenticator):
            wrong = DurableAgentHTTPClient(
                f"http://127.0.0.1:{server.server_address[1]}",
                WRONG_TOKEN,
            )
            with pytest.raises(DurableAgentHTTPClientError) as unauthorized:
                wrong.capabilities()
            assert unauthorized.value.code == "TBM_DURABLE_HTTP_UNAUTHORIZED"
            assert unauthorized.value.category == "authentication"

            with pytest.raises(DurableAgentHTTPClientError) as invalid:
                client.prepare(
                    {
                        **_prepare_request().model_dump(),
                        "tenant_id": "forged_tenant",
                    }
                )
            assert invalid.value.code == "TBM_DURABLE_SDK_INVALID_INPUT"
            assert stack.sessions.list_due(limit=100) == ()

            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_address[1],
                timeout=5,
            )
            connection.request(
                "GET",
                "/durable/v1/not-present",
                headers={"Authorization": f"Bearer {WRONG_TOKEN}"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            assert response.status == 401
            assert payload["error"]["code"] == (
                "TBM_DURABLE_HTTP_UNAUTHORIZED"
            )

            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_address[1],
                timeout=5,
            )
            connection.request(
                "GET",
                "/durable/v1/not-present",
                headers={"Authorization": f"Bearer {TOKEN}"},
            )
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            assert response.status == 404
            assert payload["error"]["code"] == "TBM_DURABLE_HTTP_NOT_FOUND"

            raw = socket.create_connection(server.server_address, timeout=5)
            try:
                raw.sendall(b"NOT-A-VALID-HTTP-REQUEST\r\n\r\n")
                raw.shutdown(socket.SHUT_WR)
                response_bytes = raw.recv(4096)
                assert (
                    b"TBM_DURABLE_HTTP_PROTOCOL_ERROR"
                    not in response_bytes
                )
            finally:
                raw.close()
    finally:
        stack.close()


def test_durable_http_rejects_duplicate_auth_and_invalid_json() -> None:
    stack = _http_stack()
    try:
        with _running_server(stack) as (server, _client, _authenticator):
            connection = http.client.HTTPConnection(
                "127.0.0.1",
                server.server_address[1],
                timeout=5,
            )
            connection.putrequest("GET", "/durable/v1/capabilities")
            connection.putheader("Authorization", f"Bearer {TOKEN}")
            connection.putheader("Authorization", f"Bearer {TOKEN}")
            connection.endheaders()
            response = connection.getresponse()
            payload = json.loads(response.read())
            connection.close()
            assert response.status == 401
            assert payload["error"]["code"] == (
                "TBM_DURABLE_HTTP_UNAUTHORIZED"
            )

            for body in (
                b'{"session_id":"one","session_id":"two"}',
                b'{"cost_usd":NaN}',
                b'["not","an","object"]',
            ):
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    server.server_address[1],
                    timeout=5,
                )
                connection.request(
                    "POST",
                    "/durable/v1/get-session",
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
                assert payload["error"]["code"] == (
                    "TBM_DURABLE_HTTP_INVALID_REQUEST"
                )
    finally:
        stack.close()


def test_durable_http_rejects_ambiguous_framing_and_unbounded_headers() -> None:
    stack = _http_stack()
    try:
        with _running_server(stack) as (server, _client, _authenticator):
            base_headers = (
                b"POST /durable/v1/get-session HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                + f"Authorization: Bearer {TOKEN}\r\n".encode("ascii")
                + b"Content-Type: application/json\r\n"
            )
            requests = (
                base_headers
                + b"Content-Length: 2\r\n"
                + b"Content-Length: 2\r\n\r\n{}",
                base_headers
                + b"Transfer-Encoding: chunked\r\n\r\n"
                + b"2\r\n{}\r\n0\r\n\r\n",
                base_headers
                + b"Content-Type: application/json\r\n"
                + b"Content-Length: 2\r\n\r\n{}",
            )
            for request in requests:
                status, payload = _raw_http_json(server, request)
                assert status == 400
                assert payload["error"]["code"] == (
                    "TBM_DURABLE_HTTP_INVALID_REQUEST"
                )

            many_headers = (
                b"GET /durable/v1/capabilities HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                + f"Authorization: Bearer {TOKEN}\r\n".encode("ascii")
                + b"".join(
                    f"X-Test-{index}: value\r\n".encode("ascii")
                    for index in range(63)
                )
                + b"\r\n"
            )
            status, payload = _raw_http_json(server, many_headers)
            assert status == 431
            assert payload["error"]["code"] == (
                "TBM_DURABLE_HTTP_HEADERS_INVALID"
            )
    finally:
        stack.close()


def test_durable_http_bounds_raw_header_bytes_before_parsing() -> None:
    stack = _http_stack()
    try:
        with _running_server(stack) as (server, client, _authenticator):
            request = (
                b"GET /durable/v1/capabilities HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                + f"Authorization: Bearer {TOKEN}\r\n".encode("ascii")
                + b"X-Oversized: "
                + b"a"
                * (durable_http_server_module.DURABLE_HTTP_HEADER_MAX_BYTES + 1)
                + b"\r\n\r\n"
            )
            connection = socket.create_connection(
                ("127.0.0.1", server.server_address[1]),
                timeout=5,
            )
            try:
                connection.sendall(request)
                try:
                    response = connection.recv(4096)
                except ConnectionResetError:
                    response = b""
                assert response == b""
            finally:
                connection.close()

            assert client.capabilities()["transport_profile"] == "durable-v3"
    finally:
        stack.close()


def test_durable_http_raw_requests_reject_identity_and_malformed_base64() -> None:
    stack = _http_stack()
    try:
        with _running_server(stack) as (server, client, _authenticator):
            forged = {
                **_prepare_request().model_dump(mode="json"),
                "tenant_id": "forged_tenant",
            }
            body = json.dumps(forged).encode("utf-8")
            request = (
                b"POST /durable/v1/prepare HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                + f"Authorization: Bearer {TOKEN}\r\n".encode("ascii")
                + b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
            status, payload = _raw_http_json(server, request)
            assert status == 400
            assert payload["error"]["code"] == (
                "TBM_DURABLE_HTTP_INVALID_INPUT"
            )

            prepared_response = client.prepare(_prepare_request())
            prepared = stack.sessions.get(
                prepared_response.result["session"]["session_id"]
            )
            evaluation = stack.evidence.load_evaluation(
                prepared.system_gate_evaluation_id
            )
            malformed = _decide_request(
                prepared,
                evaluation,
            ).model_dump(mode="json")
            malformed["prompt_base64"] = "not-canonical-base64"
            body = json.dumps(malformed).encode("utf-8")
            request = (
                b"POST /durable/v1/decide HTTP/1.1\r\n"
                b"Host: 127.0.0.1\r\n"
                + f"Authorization: Bearer {TOKEN}\r\n".encode("ascii")
                + b"Content-Type: application/json\r\n"
                + f"Content-Length: {len(body)}\r\n\r\n".encode("ascii")
                + body
            )
            status, payload = _raw_http_json(server, request)
            assert status == 400
            assert payload["error"]["code"] == (
                "TBM_DURABLE_HTTP_INVALID_INPUT"
            )
            assert stack.sessions.get(prepared.session_id).status == (
                "prepared"
            )
    finally:
        stack.close()


def test_durable_http_content_profiles_and_stale_state_fail_closed() -> None:
    stack = _http_stack()
    try:
        with _running_server(stack, expose_content=False) as (
            _server,
            client,
            _authenticator,
        ):
            capabilities = client.capabilities()
            assert "export_replay" not in capabilities["operations"]
            prepared_response = client.prepare(_prepare_request())
            prepared = stack.sessions.get(
                prepared_response.result["session"]["session_id"]
            )
            evaluation = stack.evidence.load_evaluation(
                prepared.system_gate_evaluation_id
            )
            client.decide(_decide_request(prepared, evaluation))
            decided = stack.sessions.get(prepared.session_id)

            with pytest.raises(DurableAgentHTTPClientError) as stale:
                client.finalize(
                    DurableFinalizeRequest(
                        session_id=decided.session_id,
                        expected_session_version=decided.version - 1,
                    )
                )
            assert stale.value.category == "state"

            finalized = client.finalize(
                DurableFinalizeRequest(
                    session_id=decided.session_id,
                    expected_session_version=decided.version,
                )
            )
            assert finalized.result["snippet"] is None
            assert finalized.result["content_exposed"] is False

            current = stack.sessions.get(decided.session_id)
            with pytest.raises(DurableAgentHTTPClientError) as disabled:
                client.export_replay(
                    DurableReplayRequest(
                        session_id=current.session_id,
                        expected_session_version=current.version,
                        allowed_classifications=["internal"],
                    )
                )
            assert disabled.value.code == (
                "TBM_DURABLE_WIRE_REPLAY_CONTENT_DISABLED"
            )
            assert disabled.value.category == "authorization"
    finally:
        stack.close()


def test_async_durable_http_sdk_uses_worker_threads_and_closes() -> None:
    stack = _http_stack()
    try:
        with _running_server(stack) as (server, _client, _authenticator):
            base_url = f"http://127.0.0.1:{server.server_address[1]}"

            async def scenario() -> None:
                async with AsyncDurableAgentHTTPClient(
                    base_url,
                    TOKEN,
                ) as client:
                    capabilities = await client.capabilities()
                    assert capabilities["durable_sessions"] is True
                    prepared = await client.prepare(_prepare_request())
                    session_id = prepared.result["session"]["session_id"]
                    loaded = await client.get_session(
                        DurableGetSessionRequest(session_id=session_id)
                    )
                    assert loaded.result["session"]["session_id"] == session_id
                with pytest.raises(DurableAgentHTTPClientError) as closed:
                    await client.health()
                assert closed.value.code == "TBM_DURABLE_SDK_CLOSED"

            asyncio.run(scenario())
    finally:
        stack.close()


def test_durable_http_configuration_requires_tls_off_loopback() -> None:
    with pytest.raises(ValueError, match="requires TLS"):
        DurableHTTPServerConfiguration(host="0.0.0.0")
    with pytest.raises(ValueError, match="IP address"):
        DurableHTTPServerConfiguration(host="localhost")
    with pytest.raises(ValueError, match="IPv4"):
        DurableHTTPServerConfiguration(host="::1")
    with pytest.raises(ValueError):
        DurableHTTPServerConfiguration(port=True)

    client_context = ssl.create_default_context()
    with pytest.raises(ValueError, match="server context"):
        DurableHTTPServerConfiguration(
            host="0.0.0.0",
            tls_context=client_context,
        )

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    configuration = DurableHTTPServerConfiguration(
        host="0.0.0.0",
        tls_context=server_context,
    )
    assert configuration.host == "0.0.0.0"


def test_durable_http_transport_record_validation() -> None:
    with pytest.raises(ValueError, match="operation"):
        DurableHTTPAuthenticationRequest(
            operation="invalid",
            client_ip="127.0.0.1",
            authorization=f"Bearer {TOKEN}",
        )
    with pytest.raises(ValueError, match="client IP"):
        DurableHTTPAuthenticationRequest(
            operation="health",
            client_ip="localhost",
            authorization=f"Bearer {TOKEN}",
        )
    for authorization in ("", " bearer", "x" * 8193, "bad\nvalue"):
        with pytest.raises(ValueError, match="authorization evidence"):
            DurableHTTPAuthenticationRequest(
                operation="health",
                client_ip="127.0.0.1",
                authorization=authorization,
            )
    for digest in ("sha256:abc", "sha256:" + "g" * 64):
        with pytest.raises(ValueError, match="certificate digest"):
            DurableHTTPAuthenticationRequest(
                operation="health",
                client_ip="127.0.0.1",
                peer_certificate_sha256=digest,
            )
    with pytest.raises(ValueError, match="evidence is missing"):
        DurableHTTPAuthenticationRequest(
            operation="health",
            client_ip="127.0.0.1",
        )

    stack = _http_stack()
    try:
        with pytest.raises(TypeError, match="service context"):
            DurableHTTPAuthenticatedContexts(cast(object, object()))
        with pytest.raises(TypeError, match="provider context"):
            DurableHTTPAuthenticatedContexts(
                stack.context,
                provider=cast(object, object()),
            )
        with pytest.raises(TypeError, match="evaluator context"):
            DurableHTTPAuthenticatedContexts(
                stack.context,
                evaluator=cast(object, object()),
            )
    finally:
        stack.close()


def test_durable_bearer_authenticator_validation() -> None:
    stack = _http_stack()
    try:
        contexts = DurableHTTPAuthenticatedContexts(stack.context)
        for token in ("x" * 31, "x" * 8186, " " + "x" * 32, "x\n" + "y" * 32):
            with pytest.raises(ValueError, match="bounded secret"):
                durable_http_server_module.DurableBearerAuthenticator(
                    token,
                    lambda _request: contexts,
                )
        with pytest.raises(TypeError, match="context provider"):
            durable_http_server_module.DurableBearerAuthenticator(
                TOKEN,
                cast(object, None),
            )
        authenticator = durable_http_server_module.DurableBearerAuthenticator(
            TOKEN,
            lambda _request: cast(
                DurableHTTPAuthenticatedContexts,
                object(),
            ),
        )
        with pytest.raises(TypeError, match="request is invalid"):
            authenticator(cast(DurableHTTPAuthenticationRequest, object()))
        request = DurableHTTPAuthenticationRequest(
            operation="health",
            client_ip="127.0.0.1",
            authorization=f"Bearer {TOKEN}",
        )
        with pytest.raises(TypeError, match="invalid data"):
            authenticator(request)
    finally:
        stack.close()


def test_durable_http_server_constructor_and_status_mapping() -> None:
    stack = _http_stack()
    dispatcher = _dispatcher(stack)
    try:
        with pytest.raises(TypeError, match="configuration"):
            DurableAgentHTTPServer(
                cast(DurableHTTPServerConfiguration, object()),
                dispatcher,
                _Authenticator(stack),
            )
        with pytest.raises(TypeError, match="dispatcher"):
            DurableAgentHTTPServer(
                DurableHTTPServerConfiguration(port=0),
                cast(DurableAgentProtocolDispatcher, object()),
                _Authenticator(stack),
            )
        with pytest.raises(TypeError, match="authenticator"):
            DurableAgentHTTPServer(
                DurableHTTPServerConfiguration(port=0),
                dispatcher,
                cast(object, None),
            )

        for category, expected in (
            ("authentication", 401),
            ("authorization", 403),
            ("input", 400),
            ("not_found", 404),
            ("state", 409),
            ("persistence", 503),
            ("provider", 503),
            ("evaluator", 503),
            ("recovery", 503),
            ("internal", 500),
        ):
            wire_error = DurableAgentWireError(
                "TBM_TEST",
                cast(object, category),
                "prepare",
                "test",
            )
            assert (
                durable_http_server_module._status_for_wire_error(wire_error)
                == expected
            )
            http_error = durable_http_server_module.DurableHTTPError(
                "TBM_TEST",
                cast(object, category),
                "prepare",
                "test",
                retryable=category == "persistence",
            )
            http_expected = (
                expected
                if category
                in {
                    "authentication",
                    "authorization",
                    "input",
                    "not_found",
                    "state",
                    "persistence",
                }
                else 500
            )
            assert (
                durable_http_server_module._status_for_http_error(http_error)
                == http_expected
            )
        assert (
            durable_http_server_module._status_for_wire_error(
                DurableAgentWireError(
                    "TBM_TEST",
                    "internal",
                    "prepare",
                    "test",
                    retryable=True,
                )
            )
            == 503
        )
    finally:
        stack.close()


def test_durable_http_method_internal_and_serialization_failures(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _http_stack()
    try:
        with _running_server(stack) as (server, _client, _authenticator):
            for method in ("DELETE", "PATCH", "PUT"):
                connection = http.client.HTTPConnection(
                    "127.0.0.1",
                    server.server_address[1],
                    timeout=5,
                )
                connection.request(
                    method,
                    "/durable/v1/health",
                    headers={"Authorization": f"Bearer {TOKEN}"},
                )
                response = connection.getresponse()
                payload = json.loads(response.read())
                connection.close()
                assert response.status == 405
                assert payload["error"]["code"] == (
                    "TBM_DURABLE_HTTP_METHOD_NOT_ALLOWED"
                )

            monkeypatch.setattr(
                DurableAgentProtocolDispatcher,
                "capabilities",
                lambda _self: {"unserializable": object()},
            )
            status, payload = _raw_http_json(
                server,
                (
                    b"GET /durable/v1/capabilities HTTP/1.1\r\n"
                    b"Host: 127.0.0.1\r\n"
                    + f"Authorization: Bearer {TOKEN}\r\n".encode()
                    + b"\r\n"
                ),
            )
            assert status == 500
            assert payload["error"]["code"] == (
                "TBM_DURABLE_HTTP_RESPONSE_INVALID"
            )
    finally:
        stack.close()

    stack = _http_stack()
    try:
        with _running_server(stack) as (server, client, _authenticator):
            monkeypatch.setattr(
                DurableAgentProtocolDispatcher,
                "prepare",
                lambda *_args, **_kwargs: (_ for _ in ()).throw(
                    RuntimeError("private dispatcher failure")
                ),
            )
            with pytest.raises(DurableAgentHTTPClientError) as failed:
                client.prepare(_prepare_request())
            assert failed.value.code == "TBM_DURABLE_HTTP_INTERNAL_ERROR"
    finally:
        stack.close()


def test_durable_sdk_aligns_address_token_and_tls_security_contracts() -> None:
    with pytest.raises(ValueError, match="loopback IPv4"):
        DurableAgentHTTPClient("http://[::1]:8766", TOKEN)
    with pytest.raises(ValueError, match="does not support IPv6"):
        DurableAgentHTTPClient("https://[::1]:8766", TOKEN)
    with pytest.raises(ValueError, match="bounded"):
        DurableAgentHTTPClient("http://127.0.0.1:8766", "x" * 31)

    unverified = ssl._create_unverified_context()
    with pytest.raises(ValueError, match="verify hostnames"):
        DurableAgentHTTPClient(
            "https://127.0.0.1:8766",
            TOKEN,
            tls_context=unverified,
        )

    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.minimum_version = ssl.TLSVersion.TLSv1_2
    with pytest.raises(ValueError, match="verify hostnames"):
        DurableAgentHTTPClient(
            "https://127.0.0.1:8766",
            TOKEN,
            tls_context=server_context,
        )

    verified = ssl.create_default_context()
    DurableAgentHTTPClient(
        "https://127.0.0.1:8766",
        TOKEN,
        tls_context=verified,
    )


@pytest.mark.parametrize(
    ("base_url", "token", "timeout", "message"),
    (
        (cast(str, 7), TOKEN, 30, "must be a string"),
        ("ftp://127.0.0.1:8766", TOKEN, 30, "HTTP"),
        ("http://127.0.0.1", TOKEN, 30, "explicit port"),
        ("http://user@127.0.0.1:8766", TOKEN, 30, "HTTP"),
        ("http://127.0.0.1:bad", TOKEN, 30, "port is invalid"),
        ("http://localhost:8766", TOKEN, 30, "loopback IP"),
        ("http://192.0.2.1:8766", TOKEN, 30, "loopback IPv4"),
        ("http://127.0.0.1:8766/path", TOKEN, 30, "HTTP"),
        ("http://127.0.0.1:8766", "x" * 8186, 30, "bounded"),
        ("http://127.0.0.1:8766", "x\n" + "y" * 32, 30, "bounded"),
        ("http://127.0.0.1:8766", TOKEN, True, "between 0 and 300"),
        ("http://127.0.0.1:8766", TOKEN, 0, "between 0 and 300"),
        ("http://127.0.0.1:8766", TOKEN, 301, "between 0 and 300"),
    ),
)
def test_durable_sdk_constructor_rejects_ambiguous_configuration(
    base_url: str,
    token: str,
    timeout: object,
    message: str,
) -> None:
    with pytest.raises(ValueError, match=message):
        DurableAgentHTTPClient(
            base_url,
            token,
            timeout_seconds=cast(float, timeout),
        )
    with pytest.raises(ValueError, match="SSLContext"):
        DurableAgentHTTPClient(
            "https://127.0.0.1:8766",
            TOKEN,
            tls_context=cast(ssl.SSLContext, object()),
        )


def test_durable_sdk_records_and_response_shape_validation(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    response = DurableAgentHTTPResponse(
        "tbm.durable-agent-wire.v1",
        "prepare",
        {"session": {"session_id": "session_001"}},
    )
    assert response.to_dict()["operation"] == "prepare"
    error = DurableAgentHTTPClientError(
        "TBM_TEST",
        "input",
        "prepare",
        "test failure",
        retryable=True,
    )
    assert error.to_dict()["error"]["retryable"] is True
    assert durable_sdk_module._NoRedirect().redirect_request() is None

    client = DurableAgentHTTPClient("http://127.0.0.1:8766", TOKEN)
    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args: {
            "protocol_version": "tbm.durable-agent-wire.v1",
            "transport_profile": "wrong",
            "durable_agent_contract_version": "v3",
            "storage_mode": "sqlite",
            "operations": [],
            "gate_session_statuses": [],
            "identity_source": "trusted_adapter",
            "transport_authentication": "required",
            "caller_identity_fields": False,
            "durable_sessions": True,
            "process_local_records": [],
            "injection_content_exposed": False,
            "replay_content_exposed": False,
            "limits": {},
        },
    )
    with pytest.raises(DurableAgentHTTPClientError):
        client.capabilities()

    monkeypatch.setattr(client, "_request", lambda *_args: {"openapi": "3.0.0"})
    with pytest.raises(DurableAgentHTTPClientError):
        client.openapi()

    monkeypatch.setattr(
        client,
        "_request",
        lambda *_args: {
            "protocol_version": "tbm.durable-agent-wire.v1",
            "status": "failed",
            "storage_mode": "sqlite",
            "durable_sessions": True,
            "process_local_records": [],
        },
    )
    with pytest.raises(DurableAgentHTTPClientError):
        client.health()

    with pytest.raises(DurableAgentHTTPClientError):
        durable_sdk_module._parse_operation_response(
            {
                "protocol_version": "tbm.durable-agent-wire.v1",
                "operation": "finalize",
                "result": {},
            },
            "prepare",
        )
    for invalid in ([], {1: "not-string-key"}):
        with pytest.raises(DurableAgentHTTPClientError):
            durable_sdk_module._mapping(invalid, "test")
    with pytest.raises(DurableAgentHTTPClientError):
        durable_sdk_module._exact_keys({"a": 1}, {"b"}, "test")
    with pytest.raises(DurableAgentHTTPClientError):
        durable_sdk_module._protocol({"protocol_version": "wrong"})


def test_async_durable_sdk_method_parity(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    client = AsyncDurableAgentHTTPClient("http://127.0.0.1:8766", TOKEN)
    calls: list[str] = []

    def operation(name: str):
        def invoke(_request=None):
            calls.append(name)
            if name in {"openapi", "health"}:
                return {"name": name}
            return DurableAgentHTTPResponse(
                "tbm.durable-agent-wire.v1",
                cast(object, name),
                {},
            )

        return invoke

    for name in (
        "openapi",
        "health",
        "decide",
        "finalize",
        "start",
        "resume",
        "abandon",
        "complete",
        "cancel",
        "export_replay",
    ):
        monkeypatch.setattr(client._client, name, operation(name))

    async def scenario() -> None:
        assert (await client.openapi())["name"] == "openapi"
        assert (await client.health())["name"] == "health"
        await client.decide({})
        await client.finalize({})
        await client.start({})
        await client.resume({})
        await client.abandon({})
        await client.complete({})
        await client.cancel({})
        await client.export_replay({})

    asyncio.run(scenario())
    assert calls == [
        "openapi",
        "health",
        "decide",
        "finalize",
        "start",
        "resume",
        "abandon",
        "complete",
        "cancel",
        "export_replay",
    ]


def test_durable_sdk_strict_response_reader() -> None:
    class _Headers:
        def __init__(
            self,
            *,
            protocol: list[str] | None = None,
            content_type: list[str] | None = None,
            lengths: list[str] | None = None,
            transfer_encoding: str | None = None,
        ) -> None:
            self.values = {
                "X-TBM-Protocol-Version": (
                    ["tbm.durable-agent-wire.v1"]
                    if protocol is None
                    else protocol
                ),
                "Content-Type": (
                    ["application/json"]
                    if content_type is None
                    else content_type
                ),
                "Content-Length": ["2"] if lengths is None else lengths,
            }
            self.transfer_encoding = transfer_encoding

        def get_all(self, name: str, *, failobj: list[str]) -> list[str]:
            return self.values.get(name, failobj)

        def get(self, name: str) -> str | None:
            if name == "Transfer-Encoding":
                return self.transfer_encoding
            return None

    class _Response:
        def __init__(self, headers: _Headers, body: bytes = b"{}") -> None:
            self.headers = headers
            self.body = body

        def read(self, _limit: int) -> bytes:
            return self.body

    assert (
        DurableAgentHTTPClient._read_response(
            _Response(_Headers()),
            "health",
        )
        == {}
    )
    invalid = (
        _Response(_Headers(protocol=[])),
        _Response(_Headers(content_type=["text/plain"])),
        _Response(_Headers(lengths=[])),
        _Response(_Headers(lengths=["not-a-number"])),
        _Response(_Headers(lengths=["9" * 20])),
        _Response(_Headers(lengths=["99999999"])),
        _Response(_Headers(lengths=["3"])),
        _Response(_Headers(transfer_encoding="chunked")),
        _Response(_Headers(), b"\xff\xff"),
    )
    for response_value in invalid:
        with pytest.raises(DurableAgentHTTPClientError) as raised:
            DurableAgentHTTPClient._read_response(
                response_value,
                "health",
            )
        assert raised.value.code == "TBM_DURABLE_SDK_RESPONSE_INVALID"


def test_durable_tls_handshakes_are_bounded_workers_not_listener_work(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    stack = _http_stack()
    entered = Event()
    two_entered = Event()
    release = Event()
    lock = Lock()
    calls = 0

    def blocked_wrap_socket(
        self: ssl.SSLContext,
        sock: socket.socket,
        *,
        server_side: bool,
        do_handshake_on_connect: bool,
        **_kwargs: object,
    ) -> ssl.SSLSocket:
        nonlocal calls
        assert server_side is True
        assert do_handshake_on_connect is False
        with lock:
            calls += 1
            entered.set()
            if calls == 2:
                two_entered.set()
        release.wait(timeout=5)
        raise ssl.SSLError("test handshake stopped")

    monkeypatch.setattr(
        ssl.SSLContext,
        "wrap_socket",
        blocked_wrap_socket,
    )
    context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    context.minimum_version = ssl.TLSVersion.TLSv1_2
    server = DurableAgentHTTPServer(
        DurableHTTPServerConfiguration(port=0, tls_context=context),
        _dispatcher(stack),
        _Authenticator(stack),
    )
    assert calls == 0
    thread = Thread(target=server.serve_forever, daemon=True)
    thread.start()
    first = socket.create_connection(server.server_address, timeout=5)
    second: socket.socket | None = None
    try:
        assert entered.wait(timeout=2)
        second = socket.create_connection(server.server_address, timeout=5)
        assert two_entered.wait(timeout=2)
    finally:
        release.set()
        first.close()
        if second is not None:
            second.close()
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
        stack.close()


def test_durable_sdk_rejects_error_for_a_different_operation() -> None:
    payload = {
        "protocol_version": "tbm.durable-agent-wire.v1",
        "error": {
            "code": "TBM_DURABLE_HTTP_INVALID_INPUT",
            "category": "input",
            "message": "invalid request",
            "operation": "finalize",
            "retryable": False,
        },
    }

    with pytest.raises(DurableAgentHTTPClientError) as raised:
        durable_sdk_module._parse_error(payload, "prepare")

    assert raised.value.code == "TBM_DURABLE_SDK_RESPONSE_INVALID"


def test_durable_http_missing_provider_or_evaluator_context_is_unauthorized() -> None:
    stack = _http_stack()

    def incomplete_authenticator(
        request: DurableHTTPAuthenticationRequest,
    ) -> DurableHTTPAuthenticatedContexts:
        if request.authorization != f"Bearer {TOKEN}":
            raise ValueError("bad credential")
        return DurableHTTPAuthenticatedContexts(stack.context)

    try:
        with _running_server(
            stack,
            authenticator=incomplete_authenticator,
        ) as (_server, client, _authenticator):
            prepared_response = client.prepare(_prepare_request())
            prepared = stack.sessions.get(
                prepared_response.result["session"]["session_id"]
            )
            evaluation = stack.evidence.load_evaluation(
                prepared.system_gate_evaluation_id
            )
            with pytest.raises(DurableAgentHTTPClientError) as provider:
                client.decide(_decide_request(prepared, evaluation))
            assert provider.value.code == "TBM_DURABLE_HTTP_UNAUTHORIZED"
            assert stack.sessions.get(prepared.session_id).status == "prepared"

            with pytest.raises(DurableAgentHTTPClientError) as evaluator:
                client.complete(
                    DurableCompleteRequest(
                        session_id=prepared.session_id,
                        expected_session_version=prepared.version,
                        result="pass",
                        evidence_artifact_sha256s=["sha256:" + "a" * 64],
                        output_sha256="sha256:" + "b" * 64,
                    )
                )
            assert evaluator.value.code == "TBM_DURABLE_HTTP_UNAUTHORIZED"
    finally:
        stack.close()
