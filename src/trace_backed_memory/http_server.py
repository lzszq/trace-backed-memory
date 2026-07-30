from __future__ import annotations

import argparse
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import ipaddress
import json
import os
from pathlib import Path
import secrets
import socket
import sys
from threading import BoundedSemaphore
from typing import NoReturn

from pydantic import ValidationError

from ._ingestion import (
    CLI_JSON_FILE_MAX_BYTES,
    CLI_JSON_MAX_DEPTH,
    CLI_JSON_MAX_NODES,
    decode_bounded_utf8,
    parse_bounded_json,
)
from .agent import AGENT_PROTOCOL_VERSION, AgentMemoryError, LocalAgentMemory
from .agent_wire_v1 import (
    AgentProtocolConfiguration,
    AgentProtocolDispatcher,
    AgentWireOperation,
    CancelRunRequest,
    CompleteRunRequest,
    FinalizeMemoryRequest,
    PrepareMemoryRequest,
)
from .policy import METADATA_VALUE_MAX_CHARS


HTTP_REQUEST_MAX_BYTES = CLI_JSON_FILE_MAX_BYTES
HTTP_REQUEST_MAX_NODES = CLI_JSON_MAX_NODES
HTTP_REQUEST_MAX_DEPTH = CLI_JSON_MAX_DEPTH
HTTP_TOKEN_MIN_CHARS = 32
HTTP_TOKEN_MAX_CHARS = 512
HTTP_CONNECTION_TIMEOUT_SECONDS = 15.0
HTTP_MAX_WORKERS = 32
HTTP_REQUEST_QUEUE_SIZE = 32
_SocketRequest = socket.socket | tuple[bytes, socket.socket]

_GET_ROUTES: dict[
    str,
    tuple[AgentWireOperation, Callable[[AgentProtocolDispatcher], dict[str, object]]],
] = {
    "/v1/capabilities": ("health", lambda dispatcher: dispatcher.capabilities()),
    "/v1/health": ("health", lambda dispatcher: dispatcher.health()),
}
_POST_MODELS = {
    "/v1/prepare": ("prepare", PrepareMemoryRequest),
    "/v1/finalize": ("finalize", FinalizeMemoryRequest),
    "/v1/complete": ("complete", CompleteRunRequest),
    "/v1/cancel": ("cancel", CancelRunRequest),
}


@dataclass(frozen=True)
class AgentHTTPServerConfiguration:
    host: str = "127.0.0.1"
    port: int = 8765
    token: str = field(repr=False, default="")

    def __post_init__(self) -> None:
        try:
            address = ipaddress.ip_address(self.host)
        except ValueError as error:
            raise ValueError("HTTP host must be a loopback IP address") from error
        if not address.is_loopback or address.version != 4:
            raise ValueError("HTTP host must be a loopback IPv4 address")
        if type(self.port) is not int or not 0 <= self.port <= 65_535:
            raise ValueError("HTTP port must be between 0 and 65535")
        if (
            type(self.token) is not str
            or not HTTP_TOKEN_MIN_CHARS <= len(self.token) <= HTTP_TOKEN_MAX_CHARS
            or not self.token.strip()
            or self.token.strip() != self.token
        ):
            raise ValueError(
                "HTTP token must be a bounded nonblank secret of at least "
                f"{HTTP_TOKEN_MIN_CHARS} characters"
            )


class AgentHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = HTTP_REQUEST_QUEUE_SIZE

    def __init__(
        self,
        configuration: AgentHTTPServerConfiguration,
        dispatcher: AgentProtocolDispatcher,
    ) -> None:
        if type(configuration) is not AgentHTTPServerConfiguration:
            raise TypeError(
                "configuration must be exactly AgentHTTPServerConfiguration"
            )
        if type(dispatcher) is not AgentProtocolDispatcher:
            raise TypeError("dispatcher must be exactly AgentProtocolDispatcher")
        self.configuration = configuration
        self.dispatcher = dispatcher
        self._worker_slots = BoundedSemaphore(HTTP_MAX_WORKERS)
        super().__init__(
            (configuration.host, configuration.port),
            AgentHTTPRequestHandler,
        )

    def get_request(self) -> tuple[socket.socket, tuple[str, int]]:
        request, address = super().get_request()
        request.settimeout(HTTP_CONNECTION_TIMEOUT_SECONDS)
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
            super().process_request_thread(request, client_address)
        finally:
            self._worker_slots.release()

    def handle_error(
        self,
        request: _SocketRequest,
        client_address: tuple[str, int],
    ) -> None:
        return


class AgentHTTPRequestHandler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"
    server_version = "tbm"
    sys_version = ""
    server: AgentHTTPServer

    def do_GET(self) -> None:
        if not self._authorized("health"):
            return
        route = _GET_ROUTES.get(self.path)
        if route is None:
            self._write_error(_not_found(), 404)
            return
        operation, callback = route
        try:
            self._write_json(callback(self.server.dispatcher), 200)
        except Exception as error:
            self._write_public_error(error, operation)

    def do_POST(self) -> None:
        route = _POST_MODELS.get(self.path)
        operation: AgentWireOperation = "open" if route is None else route[0]
        if not self._authorized(operation):
            return
        if route is None:
            self._write_error(_not_found(), 404)
            return
        operation, model = route
        try:
            payload = self._read_payload(operation)
            request = model.model_validate(payload)
            callback = getattr(self.server.dispatcher, operation)
            self._write_json(callback(request), 200)
        except ValidationError:
            self._write_error(
                AgentMemoryError(
                    "TBM_AGENT_INVALID_INPUT",
                    "input",
                    operation,
                    "HTTP request payload failed strict validation",
                ),
                400,
            )
        except AgentMemoryError as error:
            self._write_error(error, _status_for_error(error))
        except Exception:
            self._write_error(
                AgentMemoryError(
                    "TBM_HTTP_INTERNAL_ERROR",
                    "internal",
                    operation,
                    "HTTP runtime operation failed",
                    retryable=True,
                ),
                500,
            )

    def do_DELETE(self) -> None:
        self._method_not_allowed()

    def do_PATCH(self) -> None:
        self._method_not_allowed()

    def do_PUT(self) -> None:
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
        status = code if 400 <= code <= 599 else 400
        self._write_error(
            AgentMemoryError(
                "TBM_HTTP_PROTOCOL_ERROR",
                "input",
                "open",
                "HTTP request could not be processed",
            ),
            status,
        )

    def _method_not_allowed(self) -> None:
        error = AgentMemoryError(
            "TBM_HTTP_METHOD_NOT_ALLOWED",
            "input",
            "open",
            "HTTP method is not allowed",
        )
        self._write_error(error, 405, extra_headers={"Allow": "GET, POST"})

    def _authorized(self, operation: AgentWireOperation) -> bool:
        supplied_values = self.headers.get_all("Authorization", failobj=[])
        supplied = supplied_values[0] if len(supplied_values) == 1 else None
        expected = f"Bearer {self.server.configuration.token}"
        if (
            type(supplied) is str
            and len(supplied) <= HTTP_TOKEN_MAX_CHARS + 7
            and secrets.compare_digest(supplied, expected)
        ):
            return True
        self._write_error(
            AgentMemoryError(
                "TBM_HTTP_UNAUTHORIZED",
                "input",
                operation,
                "HTTP bearer authentication failed",
            ),
            401,
            extra_headers={"WWW-Authenticate": "Bearer"},
        )
        return False

    def _read_payload(
        self,
        operation: AgentWireOperation,
    ) -> Mapping[str, object]:
        if self.headers.get("Transfer-Encoding") is not None:
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
        length_source = lengths[0]
        if (
            type(length_source) is not str
            or not length_source.isascii()
            or not length_source.isdecimal()
        ):
            _invalid_http(operation, "Content-Length is invalid")
        normalized_length = length_source.lstrip("0") or "0"
        if len(normalized_length) > len(str(HTTP_REQUEST_MAX_BYTES)):
            _invalid_http(operation, "HTTP request body exceeds the limit")
        try:
            length = int(normalized_length, 10)
        except (ValueError, OverflowError):
            _invalid_http(operation, "Content-Length is invalid")
        if not 0 < length <= HTTP_REQUEST_MAX_BYTES:
            _invalid_http(operation, "HTTP request body exceeds the limit")
        body = self.rfile.read(length)
        if len(body) != length:
            _invalid_http(operation, "HTTP request body is incomplete")
        try:
            source = decode_bounded_utf8(
                body,
                max_bytes=HTTP_REQUEST_MAX_BYTES,
                description="HTTP request body",
            )
            payload = parse_bounded_json(
                source,
                description="HTTP request body",
                max_nodes=HTTP_REQUEST_MAX_NODES,
                max_depth=HTTP_REQUEST_MAX_DEPTH,
            )
        except (TypeError, ValueError, OverflowError, UnicodeError):
            _invalid_http(operation, "HTTP request body is invalid")
        if not isinstance(payload, dict) or any(
            type(key) is not str for key in payload
        ):
            _invalid_http(operation, "HTTP request body must be a JSON object")
        return payload

    def _write_public_error(
        self,
        error: Exception,
        operation: AgentWireOperation,
    ) -> None:
        if isinstance(error, AgentMemoryError):
            public = error
        else:
            public = AgentMemoryError(
                "TBM_HTTP_INTERNAL_ERROR",
                "internal",
                operation,
                "HTTP runtime operation failed",
                retryable=True,
            )
        self._write_error(public, _status_for_error(public))

    def _write_error(
        self,
        error: AgentMemoryError,
        status: int,
        *,
        extra_headers: Mapping[str, str] | None = None,
    ) -> None:
        self._write_json(
            error.to_dict(),
            status,
            extra_headers=extra_headers,
        )

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
        except (TypeError, ValueError, OverflowError):
            fallback = AgentMemoryError(
                "TBM_HTTP_RESPONSE_INVALID",
                "internal",
                "open",
                "HTTP response could not be serialized",
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
        self.send_header("X-TBM-Protocol-Version", AGENT_PROTOCOL_VERSION)
        if extra_headers is not None:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        self.wfile.write(body)


def _invalid_http(
    operation: AgentWireOperation,
    message: str,
) -> NoReturn:
    raise AgentMemoryError(
        "TBM_HTTP_INVALID_REQUEST",
        "input",
        operation,
        message,
    )


def _not_found() -> AgentMemoryError:
    return AgentMemoryError(
        "TBM_HTTP_NOT_FOUND",
        "input",
        "open",
        "HTTP endpoint was not found",
    )


def _status_for_error(error: AgentMemoryError) -> int:
    if error.code == "TBM_HTTP_NOT_FOUND":
        return 404
    if error.code == "TBM_HTTP_UNAUTHORIZED":
        return 401
    if error.category == "input":
        return 400
    if error.category == "state":
        return 409
    if error.category == "closed":
        return 503
    return 500


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="tbm-http",
        description=(
            "Run a Trace-backed Memory HTTP service. The default compat-v2 "
            "profile preserves the process-local agent.v1 contract."
        ),
    )
    parser.add_argument(
        "--profile",
        choices=("compat-v2", "durable-v3"),
        default="compat-v2",
        help=(
            "HTTP profile. durable-v3 is selected by the tbm-http entry point "
            "and never enabled implicitly."
        ),
    )
    parser.add_argument(
        "--repo-path",
        type=Path,
        required=True,
        help="Explicit Git checkout root used to derive provenance.",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8765)
    parser.add_argument(
        "--token-env",
        default="TBM_HTTP_TOKEN",
        help="Environment variable containing the HTTP bearer secret.",
    )
    parser.add_argument(
        "--tenant",
        help=(
            "Optional fixed declared tenant applicability value. "
            "This is not authorization in schema version 2."
        ),
    )
    storage = parser.add_mutually_exclusive_group(required=True)
    storage.add_argument("--memory", action="store_true")
    storage.add_argument("--sqlite", type=Path)
    storage.add_argument("--postgres-env", metavar="ENV_NAME")
    return parser


def _open_runtime(
    args: argparse.Namespace,
    repo_path: Path,
) -> LocalAgentMemory:
    if args.memory:
        return LocalAgentMemory.in_memory()
    if args.sqlite is not None:
        sqlite_path = args.sqlite
        if not sqlite_path.is_absolute():
            sqlite_path = repo_path / sqlite_path
        sqlite_path = sqlite_path.resolve(strict=False)
        if not sqlite_path.parent.is_dir():
            raise ValueError("SQLite database parent directory must exist")
        return LocalAgentMemory.open_sqlite(
            sqlite_path,
            initialize=True,
            check_same_thread=False,
        )
    postgres_env = args.postgres_env
    if (
        type(postgres_env) is not str
        or not postgres_env
        or "=" in postgres_env
    ):
        raise ValueError(
            "postgres environment variable name must be nonblank "
            "and must not contain '='"
        )
    conninfo = os.environ.get(postgres_env)
    if conninfo is None or not conninfo.strip():
        raise ValueError(
            "configured PostgreSQL environment variable is missing"
        )
    return LocalAgentMemory.open_postgres(conninfo)


def main(argv: Sequence[str] | None = None) -> int:
    parser = _build_parser()
    args = parser.parse_args(argv)
    runtime: LocalAgentMemory | None = None
    server: AgentHTTPServer | None = None
    try:
        if args.profile != "compat-v2":
            raise ValueError(
                "durable-v3 must be selected through the tbm-http entry point"
            )
        repo_path = args.repo_path.resolve(strict=True)
        if not repo_path.is_dir():
            raise ValueError("repo_path must be a directory")
        tenant = args.tenant
        if tenant is not None and (
            type(tenant) is not str
            or not tenant.strip()
            or tenant.strip() != tenant
            or len(tenant) > METADATA_VALUE_MAX_CHARS
        ):
            raise ValueError("tenant must be a nonblank bounded string")
        token_env = args.token_env
        if (
            type(token_env) is not str
            or not token_env
            or "=" in token_env
        ):
            raise ValueError("token environment variable name is invalid")
        token = os.environ.get(token_env)
        if token is None:
            raise ValueError("configured HTTP token environment variable is missing")
        runtime = _open_runtime(args, repo_path)
        dispatcher = AgentProtocolDispatcher(
            AgentProtocolConfiguration(repo_path, tenant),
            runtime,
        )
        server = AgentHTTPServer(
            AgentHTTPServerConfiguration(
                host=args.host,
                port=args.port,
                token=token,
            ),
            dispatcher,
        )
    except (AgentMemoryError, OSError, TypeError, ValueError) as error:
        if server is not None:
            server.server_close()
        if runtime is not None:
            runtime.close()
        if isinstance(error, AgentMemoryError):
            message = error
        elif isinstance(error, (TypeError, ValueError)):
            message = AgentMemoryError(
                "TBM_HTTP_STARTUP_FAILED",
                "input",
                "open",
                str(error),
            )
        else:
            message = AgentMemoryError(
                "TBM_HTTP_STARTUP_FAILED",
                "internal",
                "open",
                "HTTP service could not be started",
                retryable=True,
            )
        sys.stderr.write(
            json.dumps(
                message.to_dict(),
                allow_nan=False,
                ensure_ascii=False,
                separators=(",", ":"),
                sort_keys=True,
            )
            + "\n"
        )
        return 2
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        runtime.close()
    return 0


__all__ = [
    "HTTP_REQUEST_MAX_BYTES",
    "HTTP_REQUEST_MAX_DEPTH",
    "HTTP_REQUEST_MAX_NODES",
    "HTTP_CONNECTION_TIMEOUT_SECONDS",
    "HTTP_MAX_WORKERS",
    "HTTP_REQUEST_QUEUE_SIZE",
    "HTTP_TOKEN_MAX_CHARS",
    "HTTP_TOKEN_MIN_CHARS",
    "AgentHTTPRequestHandler",
    "AgentHTTPServer",
    "AgentHTTPServerConfiguration",
    "main",
]


if __name__ == "__main__":
    raise SystemExit(main())
