from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import http.client
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import secrets
import socket
import ssl
from threading import BoundedSemaphore
from typing import BinaryIO, Literal, NoReturn, Protocol, TypeAlias, cast

from pydantic import BaseModel, ValidationError

from ._ingestion import (
    CLI_JSON_FILE_MAX_BYTES,
    CLI_JSON_MAX_DEPTH,
    CLI_JSON_MAX_NODES,
    decode_bounded_utf8,
    parse_bounded_json,
)
from .durable_agent_wire_v1 import (
    DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
    DurableAbandonRequest,
    DurableAgentProtocolDispatcher,
    DurableAgentWireError,
    DurableAgentWireErrorCategory,
    DurableAgentWireOperation,
    DurableCancelRequest,
    DurableCompleteRequest,
    DurableDecideRequest,
    DurableFinalizeRequest,
    DurableGetSessionRequest,
    DurablePrepareRequest,
    DurableReplayRequest,
    DurableResumeRequest,
    DurableStartRequest,
)
from .durable_execution_v3 import AuthenticatedOutcomeEvaluatorContext
from .durable_http_contract import durable_agent_http_openapi
from .semantic_gate_service_v3 import AuthenticatedSemanticProviderContext
from .service_v3 import AuthenticatedServiceContext


DURABLE_HTTP_REQUEST_MAX_BYTES = CLI_JSON_FILE_MAX_BYTES
DURABLE_HTTP_REQUEST_MAX_NODES = CLI_JSON_MAX_NODES
DURABLE_HTTP_REQUEST_MAX_DEPTH = CLI_JSON_MAX_DEPTH
DURABLE_HTTP_RESPONSE_MAX_BYTES = 16 * 1024 * 1024
DURABLE_HTTP_AUTHORIZATION_MAX_CHARS = 8_192
DURABLE_HTTP_TOKEN_MIN_CHARS = 32
DURABLE_HTTP_TOKEN_MAX_CHARS = (
    DURABLE_HTTP_AUTHORIZATION_MAX_CHARS - len("Bearer ")
)
DURABLE_HTTP_HEADER_MAX_COUNT = 64
DURABLE_HTTP_HEADER_MAX_BYTES = 32 * 1024
DURABLE_HTTP_CONNECTION_TIMEOUT_SECONDS = 15.0
DURABLE_HTTP_MAX_WORKERS = 32
DURABLE_HTTP_REQUEST_QUEUE_SIZE = 32

DurableHTTPAuthenticationOperation: TypeAlias = (
    DurableAgentWireOperation
    | Literal["capabilities", "health", "open", "openapi"]
)
_SocketRequest = socket.socket | tuple[bytes, socket.socket]


class _BoundedHeaderReader:
    """Bound raw header reads before the standard library parses them."""

    def __init__(self, source: BinaryIO, maximum_bytes: int) -> None:
        self._source = source
        self._maximum_bytes = maximum_bytes
        self._consumed = 0

    def readline(self, size: int = -1) -> bytes:
        remaining = self._maximum_bytes - self._consumed
        read_size = remaining + 1
        if size >= 0:
            read_size = min(size, read_size)
        line = self._source.readline(read_size)
        if len(line) > remaining:
            raise http.client.LineTooLong("durable HTTP header block")
        self._consumed += len(line)
        return line


@dataclass(frozen=True)
class DurableHTTPAuthenticationRequest:
    """Bounded transport evidence presented to a trusted authenticator."""

    operation: DurableHTTPAuthenticationOperation
    client_ip: str
    authorization: str | None = field(default=None, repr=False)
    peer_certificate_sha256: str | None = None

    def __post_init__(self) -> None:
        if self.operation not in {
            "prepare",
            "decide",
            "finalize",
            "start",
            "resume",
            "abandon",
            "complete",
            "cancel",
            "get_session",
            "export_replay",
            "capabilities",
            "health",
            "open",
            "openapi",
        }:
            raise ValueError("durable HTTP operation is invalid")
        try:
            ipaddress.ip_address(self.client_ip)
        except ValueError as error:
            raise ValueError("durable HTTP client IP is invalid") from error
        if self.authorization is not None and (
            type(self.authorization) is not str
            or not self.authorization
            or self.authorization.strip() != self.authorization
            or len(self.authorization) > DURABLE_HTTP_AUTHORIZATION_MAX_CHARS
            or any(ord(char) < 32 or ord(char) == 127 for char in self.authorization)
        ):
            raise ValueError("durable HTTP authorization evidence is invalid")
        if self.peer_certificate_sha256 is not None and (
            type(self.peer_certificate_sha256) is not str
            or len(self.peer_certificate_sha256) != 71
            or not self.peer_certificate_sha256.startswith("sha256:")
            or any(
                char not in "0123456789abcdef"
                for char in self.peer_certificate_sha256[7:]
            )
        ):
            raise ValueError("durable HTTP peer certificate digest is invalid")
        if self.authorization is None and self.peer_certificate_sha256 is None:
            raise ValueError("durable HTTP authentication evidence is missing")


@dataclass(frozen=True)
class DurableHTTPAuthenticatedContexts:
    """Server-owned contexts established by live transport authentication."""

    service: AuthenticatedServiceContext
    provider: AuthenticatedSemanticProviderContext | None = None
    evaluator: AuthenticatedOutcomeEvaluatorContext | None = None

    def __post_init__(self) -> None:
        if type(self.service) is not AuthenticatedServiceContext:
            raise TypeError("service context is invalid")
        if self.provider is not None and (
            type(self.provider) is not AuthenticatedSemanticProviderContext
        ):
            raise TypeError("provider context is invalid")
        if self.evaluator is not None and (
            type(self.evaluator) is not AuthenticatedOutcomeEvaluatorContext
        ):
            raise TypeError("evaluator context is invalid")


class DurableHTTPAuthenticator(Protocol):
    """Authenticate live transport evidence and derive trusted contexts."""

    def __call__(
        self,
        request: DurableHTTPAuthenticationRequest,
    ) -> DurableHTTPAuthenticatedContexts: ...


@dataclass(frozen=True)
class DurableBearerAuthenticator:
    """Verify one operator-owned bearer secret before deriving contexts."""

    token: str = field(repr=False)
    context_provider: Callable[
        [DurableHTTPAuthenticationRequest],
        DurableHTTPAuthenticatedContexts,
    ] = field(repr=False)

    def __post_init__(self) -> None:
        if (
            type(self.token) is not str
            or len(self.token) < DURABLE_HTTP_TOKEN_MIN_CHARS
            or len(self.token) > DURABLE_HTTP_TOKEN_MAX_CHARS
            or self.token.strip() != self.token
            or any(ord(char) < 32 or ord(char) == 127 for char in self.token)
        ):
            raise ValueError(
                "durable HTTP bearer token must be a bounded secret"
            )
        if not callable(self.context_provider):
            raise TypeError("durable HTTP context provider must be callable")

    def __call__(
        self,
        request: DurableHTTPAuthenticationRequest,
    ) -> DurableHTTPAuthenticatedContexts:
        if type(request) is not DurableHTTPAuthenticationRequest:
            raise TypeError("durable HTTP authentication request is invalid")
        authorization = request.authorization
        if authorization is None or not secrets.compare_digest(
            authorization,
            f"Bearer {self.token}",
        ):
            raise ValueError("durable HTTP bearer authentication failed")
        contexts = self.context_provider(request)
        if type(contexts) is not DurableHTTPAuthenticatedContexts:
            raise TypeError("durable HTTP context provider returned invalid data")
        return contexts


@dataclass(frozen=True)
class DurableHTTPServerConfiguration:
    host: str = "127.0.0.1"
    port: int = 8766
    tls_context: ssl.SSLContext | None = field(default=None, repr=False)

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError as error:
            raise ValueError("durable HTTP host must be an IP address") from error
        if address.version != 4:
            raise ValueError("durable HTTP host must be an IPv4 address")
        if type(self.port) is not int or not 0 <= self.port <= 65_535:
            raise ValueError("durable HTTP port must be between 0 and 65535")
        if self.tls_context is not None and (
            not isinstance(self.tls_context, ssl.SSLContext)
            or self.tls_context.protocol != ssl.PROTOCOL_TLS_SERVER
            or self.tls_context.minimum_version < ssl.TLSVersion.TLSv1_2
        ):
            raise ValueError(
                "durable HTTP TLS must use a server context with TLS 1.2+"
            )
        if not address.is_loopback and self.tls_context is None:
            raise ValueError("non-loopback durable HTTP requires TLS")


@dataclass(frozen=True)
class DurableHTTPError(RuntimeError):
    code: str
    category: DurableAgentWireErrorCategory
    operation: DurableHTTPAuthenticationOperation
    message: str
    retryable: bool = False

    def __post_init__(self) -> None:
        RuntimeError.__init__(self, self.message)

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
            "error": {
                "code": self.code,
                "category": self.category,
                "message": self.message,
                "operation": self.operation,
                "retryable": self.retryable,
            },
        }


_POST_MODELS: dict[
    str,
    tuple[DurableAgentWireOperation, type[BaseModel]],
] = {
    "/durable/v1/prepare": ("prepare", DurablePrepareRequest),
    "/durable/v1/decide": ("decide", DurableDecideRequest),
    "/durable/v1/finalize": ("finalize", DurableFinalizeRequest),
    "/durable/v1/start": ("start", DurableStartRequest),
    "/durable/v1/resume": ("resume", DurableResumeRequest),
    "/durable/v1/abandon": ("abandon", DurableAbandonRequest),
    "/durable/v1/complete": ("complete", DurableCompleteRequest),
    "/durable/v1/cancel": ("cancel", DurableCancelRequest),
    "/durable/v1/get-session": ("get_session", DurableGetSessionRequest),
    "/durable/v1/export-replay": ("export_replay", DurableReplayRequest),
}
_POST_OPERATIONS = {
    path: operation for path, (operation, _model) in _POST_MODELS.items()
}
_GET_OPERATIONS: dict[str, DurableHTTPAuthenticationOperation] = {
    "/durable/v1/openapi": "openapi",
    "/durable/v1/capabilities": "capabilities",
    "/durable/v1/health": "health",
}


class DurableAgentHTTPServer(ThreadingHTTPServer):
    """Bounded durable HTTP binding with adapter-owned live authentication."""

    daemon_threads = True
    request_queue_size = DURABLE_HTTP_REQUEST_QUEUE_SIZE

    def __init__(
        self,
        configuration: DurableHTTPServerConfiguration,
        dispatcher: DurableAgentProtocolDispatcher,
        authenticator: DurableHTTPAuthenticator,
    ) -> None:
        if type(configuration) is not DurableHTTPServerConfiguration:
            raise TypeError(
                "configuration must be exactly DurableHTTPServerConfiguration"
            )
        if type(dispatcher) is not DurableAgentProtocolDispatcher:
            raise TypeError(
                "dispatcher must be exactly DurableAgentProtocolDispatcher"
            )
        if not callable(authenticator):
            raise TypeError("authenticator must be callable")
        self.configuration = configuration
        self.dispatcher = dispatcher
        self.authenticator = authenticator
        self._worker_slots = BoundedSemaphore(DURABLE_HTTP_MAX_WORKERS)
        super().__init__(
            (configuration.host, configuration.port),
            DurableAgentHTTPRequestHandler,
        )

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        request, address = super().get_request()
        request.settimeout(DURABLE_HTTP_CONNECTION_TIMEOUT_SECONDS)
        return request, address

    def process_request(
        self,
        request: _SocketRequest,
        client_address: tuple[str, int],
    ) -> None:
        if not self._worker_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:
            self._worker_slots.release()
            raise

    def process_request_thread(
        self,
        request: _SocketRequest,
        client_address: tuple[str, int],
    ) -> None:
        try:
            secured = self._complete_tls_handshake(request)
            if secured is not None:
                super().process_request_thread(secured, client_address)
        finally:
            self._worker_slots.release()

    def _complete_tls_handshake(
        self,
        request: _SocketRequest,
    ) -> _SocketRequest | None:
        context = self.configuration.tls_context
        if context is None:
            return request
        if not isinstance(request, socket.socket):
            self.shutdown_request(request)
            return None
        wrapped: ssl.SSLSocket | None = None
        try:
            wrapped = context.wrap_socket(
                request,
                server_side=True,
                do_handshake_on_connect=False,
            )
            wrapped.settimeout(DURABLE_HTTP_CONNECTION_TIMEOUT_SECONDS)
            wrapped.do_handshake()
            return wrapped
        except (OSError, ValueError, ssl.SSLError):
            self.shutdown_request(request if wrapped is None else wrapped)
            return None

    def handle_error(
        self,
        request: _SocketRequest,
        client_address: tuple[str, int],
    ) -> None:
        return


class DurableAgentHTTPRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tbm"
    sys_version = ""
    server: DurableAgentHTTPServer

    def parse_request(self) -> bool:
        self._tbm_headers_parsed = False
        original_reader = self.rfile
        # The additional CRLF terminates the header block and is not part of
        # the aggregate field budget checked by _headers_are_bounded().
        bounded_reader = _BoundedHeaderReader(
            original_reader,
            DURABLE_HTTP_HEADER_MAX_BYTES + 2,
        )
        self.rfile = cast(BinaryIO, bounded_reader)
        try:
            parsed = super().parse_request()
        finally:
            self.rfile = original_reader
        if parsed:
            self._tbm_headers_parsed = True
        return parsed

    def do_GET(self) -> None:
        operation = _GET_OPERATIONS.get(self.path, "open")
        if self._authenticate(operation) is None:
            return
        try:
            if operation == "openapi":
                self._write_json(durable_agent_http_openapi(), 200)
                return
            if operation == "capabilities":
                capabilities = dict(self.server.dispatcher.capabilities())
                capabilities["transport_profile"] = "durable-v3"
                self._write_json(capabilities, 200)
                return
            if operation == "health":
                capabilities = self.server.dispatcher.capabilities()
                self._write_json(
                    {
                        "protocol_version": DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
                        "status": "ok",
                        "storage_mode": capabilities["storage_mode"],
                        "durable_sessions": True,
                        "process_local_records": [],
                    },
                    200,
                )
                return
            self._write_error(_not_found(operation), 404)
        except DurableHTTPError as error:
            self._write_error(error, _status_for_http_error(error))
        except Exception:
            self._write_error(
                DurableHTTPError(
                    "TBM_DURABLE_HTTP_INTERNAL_ERROR",
                    "internal",
                    operation,
                    "durable HTTP operation failed",
                    retryable=True,
                ),
                500,
            )

    def do_POST(self) -> None:
        operation: DurableHTTPAuthenticationOperation = _POST_OPERATIONS.get(
            self.path,
            "open",
        )
        contexts = self._authenticate(operation)
        if contexts is None:
            return
        route = _POST_MODELS.get(self.path)
        if route is None:
            self._write_error(_not_found(operation), 404)
            return
        wire_operation, model = route
        try:
            payload = self._read_payload(wire_operation)
            request = model.model_validate(payload)
            result = self._dispatch(
                contexts,
                wire_operation,
                cast(BaseModel, request),
            )
            self._write_json(result, 200)
        except ValidationError:
            self._write_error(
                DurableHTTPError(
                    "TBM_DURABLE_HTTP_INVALID_INPUT",
                    "input",
                    operation,
                    "durable HTTP request payload failed strict validation",
                ),
                400,
            )
        except DurableAgentWireError as error:
            self._write_json(error.to_dict(), _status_for_wire_error(error))
        except DurableHTTPError as error:
            self._write_error(error, _status_for_http_error(error))
        except Exception:
            self._write_error(
                DurableHTTPError(
                    "TBM_DURABLE_HTTP_INTERNAL_ERROR",
                    "internal",
                    operation,
                    "durable HTTP operation failed",
                    retryable=True,
                ),
                500,
            )

    def do_DELETE(self) -> None:
        if self._authenticate("open") is not None:
            self._method_not_allowed()

    def do_PATCH(self) -> None:
        if self._authenticate("open") is not None:
            self._method_not_allowed()

    def do_PUT(self) -> None:
        if self._authenticate("open") is not None:
            self._method_not_allowed()

    def log_message(self, format: str, *args: object) -> None:
        return

    def version_string(self) -> str:
        return self.server_version

    def send_error(
        self,
        code: int,
        message: str | None = None,
        explain: str | None = None,
    ) -> None:
        if not getattr(self, "_tbm_headers_parsed", False):
            self.close_connection = True
            return
        if self._authenticate("open") is None:
            return
        status = code if 400 <= code <= 599 else 400
        self._write_error(
            DurableHTTPError(
                "TBM_DURABLE_HTTP_PROTOCOL_ERROR",
                "input",
                "open",
                "durable HTTP request could not be processed",
            ),
            status,
        )

    def _method_not_allowed(self) -> None:
        self._write_error(
            DurableHTTPError(
                "TBM_DURABLE_HTTP_METHOD_NOT_ALLOWED",
                "input",
                "open",
                "durable HTTP method is not allowed",
            ),
            405,
            extra_headers={"Allow": "GET, POST"},
        )

    def _authenticate(
        self,
        operation: DurableHTTPAuthenticationOperation,
    ) -> DurableHTTPAuthenticatedContexts | None:
        if not self._headers_are_bounded():
            self._write_error(
                DurableHTTPError(
                    "TBM_DURABLE_HTTP_HEADERS_INVALID",
                    "input",
                    operation,
                    "durable HTTP request headers exceed the limit",
                ),
                431,
            )
            return None
        values = self.headers.get_all("Authorization", failobj=[])
        authorization = values[0] if len(values) == 1 else None
        peer_digest = self._peer_certificate_digest()
        if len(values) > 1:
            authorization = None
            peer_digest = None
        try:
            request = DurableHTTPAuthenticationRequest(
                operation=operation,
                client_ip=self.client_address[0],
                authorization=authorization,
                peer_certificate_sha256=peer_digest,
            )
            contexts = self.server.authenticator(request)
            if type(contexts) is not DurableHTTPAuthenticatedContexts:
                raise TypeError("authenticator returned invalid contexts")
            if operation == "decide" and contexts.provider is None:
                raise ValueError("provider context is missing")
            if operation == "complete" and contexts.evaluator is None:
                raise ValueError("evaluator context is missing")
            return contexts
        except Exception:
            self._write_error(
                DurableHTTPError(
                    "TBM_DURABLE_HTTP_UNAUTHORIZED",
                    "authentication",
                    operation,
                    "durable HTTP transport authentication failed",
                ),
                401,
                extra_headers={"WWW-Authenticate": "Bearer"},
            )
            return None

    def _headers_are_bounded(self) -> bool:
        items = list(self.headers.raw_items())
        if len(items) > DURABLE_HTTP_HEADER_MAX_COUNT:
            return False
        total = 0
        for name, value in items:
            try:
                encoded_name = name.encode("ascii", errors="strict")
                encoded_value = value.encode("latin-1", errors="strict")
            except UnicodeError:
                return False
            total += len(encoded_name) + len(encoded_value) + 4
            if total > DURABLE_HTTP_HEADER_MAX_BYTES:
                return False
        return True

    def _peer_certificate_digest(self) -> str | None:
        connection = self.connection
        if not isinstance(connection, ssl.SSLSocket):
            return None
        try:
            certificate = connection.getpeercert(binary_form=True)
        except (OSError, ValueError):
            return None
        if not certificate:
            return None
        return "sha256:" + hashlib.sha256(certificate).hexdigest()

    def _dispatch(
        self,
        contexts: DurableHTTPAuthenticatedContexts,
        operation: DurableAgentWireOperation,
        request: BaseModel,
    ) -> dict[str, object]:
        dispatcher = self.server.dispatcher
        if operation == "decide":
            provider = contexts.provider
            if provider is None:
                raise RuntimeError("authenticated provider context is missing")
            return dispatcher.decide(
                contexts.service,
                provider,
                cast(DurableDecideRequest, request),
            )
        if operation == "complete":
            evaluator = contexts.evaluator
            if evaluator is None:
                raise RuntimeError("authenticated evaluator context is missing")
            return dispatcher.complete(
                contexts.service,
                evaluator,
                cast(DurableCompleteRequest, request),
            )
        callbacks: dict[
            DurableAgentWireOperation,
            Callable[[AuthenticatedServiceContext, object], dict[str, object]],
        ] = {
            "prepare": cast(Callable[..., dict[str, object]], dispatcher.prepare),
            "finalize": cast(Callable[..., dict[str, object]], dispatcher.finalize),
            "start": cast(Callable[..., dict[str, object]], dispatcher.start),
            "resume": cast(Callable[..., dict[str, object]], dispatcher.resume),
            "abandon": cast(Callable[..., dict[str, object]], dispatcher.abandon),
            "cancel": cast(Callable[..., dict[str, object]], dispatcher.cancel),
            "get_session": cast(
                Callable[..., dict[str, object]],
                dispatcher.get_session,
            ),
            "export_replay": cast(
                Callable[..., dict[str, object]],
                dispatcher.export_replay,
            ),
        }
        callback = callbacks.get(operation)
        if callback is None:
            raise RuntimeError("durable HTTP operation is not dispatchable")
        return callback(contexts.service, request)

    def _read_payload(
        self,
        operation: DurableAgentWireOperation,
    ) -> Mapping[str, object]:
        transfer_encodings = self.headers.get_all(
            "Transfer-Encoding",
            failobj=[],
        )
        if transfer_encodings:
            _invalid_http(operation, "chunked request bodies are not supported")
        content_types = self.headers.get_all("Content-Type", failobj=[])
        if (
            len(content_types) != 1
            or content_types[0].split(";", 1)[0].strip().lower()
            != "application/json"
        ):
            _invalid_http(operation, "Content-Type must be application/json")
        lengths = self.headers.get_all("Content-Length", failobj=[])
        if len(lengths) != 1:
            _invalid_http(operation, "Content-Length must appear exactly once")
        source_length = lengths[0]
        if (
            type(source_length) is not str
            or not source_length.isascii()
            or not source_length.isdecimal()
        ):
            _invalid_http(operation, "Content-Length is invalid")
        normalized = source_length.lstrip("0") or "0"
        if len(normalized) > len(str(DURABLE_HTTP_REQUEST_MAX_BYTES)):
            _invalid_http(operation, "durable HTTP request exceeds the limit")
        try:
            length = int(normalized, 10)
        except (ValueError, OverflowError):
            _invalid_http(operation, "Content-Length is invalid")
        if not 0 < length <= DURABLE_HTTP_REQUEST_MAX_BYTES:
            _invalid_http(operation, "durable HTTP request exceeds the limit")
        body = self.rfile.read(length)
        if len(body) != length:
            _invalid_http(operation, "durable HTTP request body is incomplete")
        try:
            source = decode_bounded_utf8(
                body,
                max_bytes=DURABLE_HTTP_REQUEST_MAX_BYTES,
                description="durable HTTP request body",
            )
            payload = parse_bounded_json(
                source,
                description="durable HTTP request body",
                max_nodes=DURABLE_HTTP_REQUEST_MAX_NODES,
                max_depth=DURABLE_HTTP_REQUEST_MAX_DEPTH,
            )
        except (TypeError, ValueError, OverflowError, UnicodeError):
            _invalid_http(operation, "durable HTTP request body is invalid")
        if not isinstance(payload, dict) or any(
            type(key) is not str for key in payload
        ):
            _invalid_http(operation, "durable HTTP request must be an object")
        return payload

    def _write_error(
        self,
        error: DurableHTTPError,
        status: int,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._write_json(error.to_dict(), status, extra_headers=extra_headers)

    def _write_json(
        self,
        payload: Mapping[str, object],
        status: int,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        try:
            body = json.dumps(
                payload,
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            if len(body) > DURABLE_HTTP_RESPONSE_MAX_BYTES:
                raise ValueError("durable HTTP response exceeds the limit")
        except (RecursionError, TypeError, UnicodeError, ValueError, OverflowError):
            fallback = DurableHTTPError(
                "TBM_DURABLE_HTTP_RESPONSE_INVALID",
                "internal",
                "open",
                "durable HTTP response could not be serialized",
            )
            body = json.dumps(
                fallback.to_dict(),
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            ).encode("utf-8")
            status = 500
        self.close_connection = True
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("Connection", "close")
        self.send_header(
            "X-TBM-Protocol-Version",
            DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
        )
        if extra_headers is not None:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def _invalid_http(
    operation: DurableAgentWireOperation,
    message: str,
) -> NoReturn:
    raise DurableHTTPError(
        "TBM_DURABLE_HTTP_INVALID_REQUEST",
        "input",
        operation,
        message,
    )


def _not_found(
    operation: DurableHTTPAuthenticationOperation,
) -> DurableHTTPError:
    return DurableHTTPError(
        "TBM_DURABLE_HTTP_NOT_FOUND",
        "not_found",
        operation,
        "durable HTTP endpoint was not found",
    )


def _status_for_wire_error(error: DurableAgentWireError) -> int:
    if error.category == "authentication":
        return 401
    if error.category == "authorization":
        return 403
    if error.category == "input":
        return 400
    if error.category == "not_found":
        return 404
    if error.category == "state":
        return 409
    if error.retryable or error.category in {
        "persistence",
        "provider",
        "evaluator",
        "recovery",
    }:
        return 503
    return 500


def _status_for_http_error(error: DurableHTTPError) -> int:
    if error.category == "authentication":
        return 401
    if error.category == "authorization":
        return 403
    if error.category == "input":
        return 400
    if error.category == "not_found":
        return 404
    if error.category == "state":
        return 409
    if error.retryable:
        return 503
    return 500


__all__ = [
    "DURABLE_HTTP_AUTHORIZATION_MAX_CHARS",
    "DURABLE_HTTP_CONNECTION_TIMEOUT_SECONDS",
    "DURABLE_HTTP_HEADER_MAX_BYTES",
    "DURABLE_HTTP_HEADER_MAX_COUNT",
    "DURABLE_HTTP_MAX_WORKERS",
    "DURABLE_HTTP_REQUEST_MAX_BYTES",
    "DURABLE_HTTP_REQUEST_MAX_DEPTH",
    "DURABLE_HTTP_REQUEST_MAX_NODES",
    "DURABLE_HTTP_REQUEST_QUEUE_SIZE",
    "DURABLE_HTTP_RESPONSE_MAX_BYTES",
    "DURABLE_HTTP_TOKEN_MAX_CHARS",
    "DURABLE_HTTP_TOKEN_MIN_CHARS",
    "DurableAgentHTTPRequestHandler",
    "DurableAgentHTTPServer",
    "DurableBearerAuthenticator",
    "DurableHTTPAuthenticatedContexts",
    "DurableHTTPAuthenticationOperation",
    "DurableHTTPAuthenticationRequest",
    "DurableHTTPAuthenticator",
    "DurableHTTPError",
    "DurableHTTPServerConfiguration",
]
