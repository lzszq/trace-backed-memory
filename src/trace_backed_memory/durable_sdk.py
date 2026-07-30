from __future__ import annotations

import asyncio
from collections.abc import Mapping
from dataclasses import dataclass
import ipaddress
import json
import re
import ssl
from typing import Literal, NoReturn, Self, TypeAlias, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    HTTPSHandler,
    ProxyHandler,
    Request,
    build_opener,
)

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
from .durable_http_server import (
    DURABLE_HTTP_AUTHORIZATION_MAX_CHARS,
    DURABLE_HTTP_RESPONSE_MAX_BYTES,
    DURABLE_HTTP_TOKEN_MIN_CHARS,
)


DURABLE_SDK_RESPONSE_MAX_BYTES = DURABLE_HTTP_RESPONSE_MAX_BYTES
DURABLE_SDK_RESPONSE_MAX_NODES = CLI_JSON_MAX_NODES
DURABLE_SDK_RESPONSE_MAX_DEPTH = CLI_JSON_MAX_DEPTH
DURABLE_SDK_TOKEN_MIN_CHARS = DURABLE_HTTP_TOKEN_MIN_CHARS
DURABLE_SDK_TOKEN_MAX_CHARS = DURABLE_HTTP_AUTHORIZATION_MAX_CHARS - 7
DURABLE_SDK_ERROR_MESSAGE_MAX_CHARS = 2_000

DurableSDKOperation: TypeAlias = (
    DurableAgentWireOperation
    | Literal["capabilities", "health", "open", "close", "openapi"]
)
_ERROR_CODE_RE = re.compile(r"^TBM_[A-Z0-9_]{1,120}$")
_ERROR_CATEGORIES = {
    "input",
    "authentication",
    "authorization",
    "state",
    "not_found",
    "persistence",
    "provider",
    "evaluator",
    "recovery",
    "internal",
}
_SERVER_OPERATIONS = {
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
}
_OPERATION_PATHS: dict[DurableAgentWireOperation, str] = {
    "prepare": "/durable/v1/prepare",
    "decide": "/durable/v1/decide",
    "finalize": "/durable/v1/finalize",
    "start": "/durable/v1/start",
    "resume": "/durable/v1/resume",
    "abandon": "/durable/v1/abandon",
    "complete": "/durable/v1/complete",
    "cancel": "/durable/v1/cancel",
    "get_session": "/durable/v1/get-session",
    "export_replay": "/durable/v1/export-replay",
}
_OPERATION_MODELS: dict[
    DurableAgentWireOperation,
    type[BaseModel],
] = {
    "prepare": DurablePrepareRequest,
    "decide": DurableDecideRequest,
    "finalize": DurableFinalizeRequest,
    "start": DurableStartRequest,
    "resume": DurableResumeRequest,
    "abandon": DurableAbandonRequest,
    "complete": DurableCompleteRequest,
    "cancel": DurableCancelRequest,
    "get_session": DurableGetSessionRequest,
    "export_replay": DurableReplayRequest,
}
_RESULT_KEYS: dict[DurableAgentWireOperation, set[str]] = {
    "prepare": {
        "authorization_event_id",
        "session",
        "retrieval_snapshot",
        "system_gate_evaluation",
        "retrieval_policy",
    },
    "decide": {
        "session",
        "attempt",
        "prompt_artifact",
        "response_artifact",
        "replayed",
    },
    "finalize": {
        "session",
        "usage_decision",
        "injection",
        "manifest",
        "snippet",
        "content_exposed",
        "replayed",
    },
    "start": {
        "session",
        "usage_decision",
        "injection",
        "manifest",
        "transition_authorization_event_id",
        "snippet",
        "content_exposed",
        "execution_required",
        "replayed",
    },
    "resume": {
        "session",
        "usage_decision",
        "injection",
        "manifest",
        "transition_authorization_event_id",
        "snippet",
        "content_exposed",
        "execution_required",
        "replayed",
    },
    "abandon": {
        "session",
        "transition_authorization_event_id",
        "replayed",
    },
    "complete": {
        "session",
        "outcome",
        "outbox_event",
        "outbox_delivery",
        "transition_authorization_event_id",
        "inserted",
        "event_inserted",
        "replayed",
    },
    "cancel": {
        "session",
        "transition_authorization_event_id",
        "replayed",
    },
    "get_session": {"session"},
    "export_replay": {
        "session",
        "bundle",
        "read_authorization_event_id",
        "retrieval_authorization_event_id",
        "content_exposed",
    },
}


@dataclass(frozen=True)
class DurableAgentHTTPResponse:
    protocol_version: str
    operation: DurableAgentWireOperation
    result: dict[str, object]

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "operation": self.operation,
            "result": dict(self.result),
        }


class DurableAgentHTTPClientError(RuntimeError):
    """Stable client/transport or server error for the durable HTTP SDK."""

    def __init__(
        self,
        code: str,
        category: DurableAgentWireErrorCategory,
        operation: DurableSDKOperation,
        message: str,
        *,
        retryable: bool = False,
    ) -> None:
        self.code = code
        self.category = category
        self.operation = operation
        self.retryable = retryable
        super().__init__(message[:DURABLE_SDK_ERROR_MESSAGE_MAX_CHARS])

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
            "error": {
                "code": self.code,
                "category": self.category,
                "message": str(self),
                "operation": self.operation,
                "retryable": self.retryable,
            },
        }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class DurableAgentHTTPClient:
    """Typed client for the authenticated durable HTTP profile."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 30.0,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        if type(base_url) is not str:
            raise ValueError("base_url must be a string")
        parsed = urlsplit(base_url)
        try:
            port = parsed.port
        except ValueError as error:
            raise ValueError("base_url port is invalid") from error
        if (
            parsed.scheme not in {"http", "https"}
            or not parsed.hostname
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "base_url must identify an HTTP(S) service with an explicit port"
            )
        if parsed.scheme == "http":
            try:
                address = ipaddress.ip_address(parsed.hostname)
            except ValueError as error:
                raise ValueError(
                    "plaintext durable HTTP must use a loopback IP"
                ) from error
            if address.version != 4 or not address.is_loopback:
                raise ValueError(
                    "plaintext durable HTTP must use a loopback IPv4 address"
                )
            host = address.compressed
        else:
            host = parsed.hostname
            try:
                address = ipaddress.ip_address(host)
            except ValueError:
                pass
            else:
                if address.version != 4:
                    raise ValueError(
                        "durable HTTPS does not support IPv6 addresses"
                    )
        if tls_context is not None and not isinstance(tls_context, ssl.SSLContext):
            raise ValueError("tls_context must be an SSLContext")
        if tls_context is not None and (
            tls_context.protocol != ssl.PROTOCOL_TLS_CLIENT
            or tls_context.minimum_version < ssl.TLSVersion.TLSv1_2
            or tls_context.verify_mode != ssl.CERT_REQUIRED
            or not tls_context.check_hostname
        ):
            raise ValueError(
                "tls_context must verify hostnames and certificates with "
                "TLS 1.2+"
            )
        if (
            type(token) is not str
            or not DURABLE_SDK_TOKEN_MIN_CHARS
            <= len(token)
            <= DURABLE_SDK_TOKEN_MAX_CHARS
            or token.strip() != token
            or any(ord(char) < 32 or ord(char) == 127 for char in token)
        ):
            raise ValueError("token must be a bounded nonblank bearer secret")
        if (
            type(timeout_seconds) not in {int, float}
            or isinstance(timeout_seconds, bool)
            or not 0 < float(timeout_seconds) <= 300
        ):
            raise ValueError("timeout_seconds must be between 0 and 300")
        self._base_url = f"{parsed.scheme}://{host}:{port}"
        self._token = token
        self._timeout_seconds = float(timeout_seconds)
        handlers: list[object] = [ProxyHandler({}), _NoRedirect()]
        if parsed.scheme == "https" and tls_context is not None:
            handlers.append(HTTPSHandler(context=tls_context))
        self._opener = build_opener(*handlers)

    def capabilities(self) -> dict[str, object]:
        value = self._request(
            "GET",
            "/durable/v1/capabilities",
            None,
            "capabilities",
        )
        payload = _mapping(value, "capabilities response")
        expected = {
            "protocol_version",
            "transport_profile",
            "durable_agent_contract_version",
            "storage_mode",
            "operations",
            "gate_session_statuses",
            "identity_source",
            "transport_authentication",
            "caller_identity_fields",
            "durable_sessions",
            "process_local_records",
            "injection_content_exposed",
            "replay_content_exposed",
            "limits",
        }
        _exact_keys(payload, expected, "capabilities response")
        _protocol(payload)
        if (
            payload["identity_source"] != "trusted_adapter"
            or payload["transport_profile"] != "durable-v3"
            or payload["transport_authentication"] != "required"
            or payload["caller_identity_fields"] is not False
            or payload["durable_sessions"] is not True
            or payload["process_local_records"] != []
            or type(payload["operations"]) is not list
            or type(payload["gate_session_statuses"]) is not list
            or not isinstance(payload["limits"], dict)
        ):
            _invalid_response("durable capabilities are invalid")
        return dict(payload)

    def openapi(self) -> dict[str, object]:
        payload = _mapping(
            self._request(
                "GET",
                "/durable/v1/openapi",
                None,
                "openapi",
            ),
            "OpenAPI response",
        )
        if (
            payload.get("openapi") != "3.1.0"
            or not isinstance(payload.get("paths"), dict)
            or not isinstance(payload.get("components"), dict)
        ):
            _invalid_response("durable OpenAPI response is invalid")
        return dict(payload)

    def health(self) -> dict[str, object]:
        payload = _mapping(
            self._request("GET", "/durable/v1/health", None, "health"),
            "health response",
        )
        _exact_keys(
            payload,
            {
                "protocol_version",
                "status",
                "storage_mode",
                "durable_sessions",
                "process_local_records",
            },
            "health response",
        )
        _protocol(payload)
        if (
            payload["status"] != "ok"
            or payload["durable_sessions"] is not True
            or payload["process_local_records"] != []
        ):
            _invalid_response("durable health response is invalid")
        return dict(payload)

    def prepare(
        self,
        request: DurablePrepareRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return self._operation("prepare", request)

    def decide(
        self,
        request: DurableDecideRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return self._operation("decide", request)

    def finalize(
        self,
        request: DurableFinalizeRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return self._operation("finalize", request)

    def start(
        self,
        request: DurableStartRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return self._operation("start", request)

    def resume(
        self,
        request: DurableResumeRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return self._operation("resume", request)

    def abandon(
        self,
        request: DurableAbandonRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return self._operation("abandon", request)

    def complete(
        self,
        request: DurableCompleteRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return self._operation("complete", request)

    def cancel(
        self,
        request: DurableCancelRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return self._operation("cancel", request)

    def get_session(
        self,
        request: DurableGetSessionRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return self._operation("get_session", request)

    def export_replay(
        self,
        request: DurableReplayRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return self._operation("export_replay", request)

    def _operation(
        self,
        operation: DurableAgentWireOperation,
        request: BaseModel | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        model = _OPERATION_MODELS[operation]
        try:
            validated = (
                request
                if type(request) is model
                else model.model_validate(request)
            )
        except (TypeError, ValidationError, ValueError):
            _invalid_input(operation, "durable SDK request failed validation")
        payload = cast(BaseModel, validated).model_dump(mode="json")
        value = self._request(
            "POST",
            _OPERATION_PATHS[operation],
            payload,
            operation,
        )
        return _parse_operation_response(value, operation)

    def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        payload: Mapping[str, object] | None,
        operation: DurableSDKOperation,
    ) -> object:
        body: bytes | None = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        if payload is not None:
            try:
                body = json.dumps(
                    dict(payload),
                    allow_nan=False,
                    ensure_ascii=False,
                    separators=(",", ":"),
                    sort_keys=True,
                ).encode("utf-8")
            except (
                RecursionError,
                TypeError,
                UnicodeError,
                ValueError,
                OverflowError,
            ):
                _invalid_input(operation, "request is not bounded JSON data")
            if len(body) > CLI_JSON_FILE_MAX_BYTES:
                _invalid_input(operation, "request exceeds the wire size limit")
            try:
                source = decode_bounded_utf8(
                    body,
                    max_bytes=CLI_JSON_FILE_MAX_BYTES,
                    description="durable SDK HTTP request",
                )
                parse_bounded_json(
                    source,
                    description="durable SDK HTTP request",
                    max_nodes=CLI_JSON_MAX_NODES,
                    max_depth=CLI_JSON_MAX_DEPTH,
                )
            except (TypeError, UnicodeError, ValueError, OverflowError):
                _invalid_input(operation, "request is not bounded JSON data")
            headers["Content-Type"] = "application/json"
        request = Request(
            self._base_url + path,
            data=body,
            headers=headers,
            method=method,
        )
        try:
            response = self._opener.open(
                request,
                timeout=self._timeout_seconds,
            )
        except HTTPError as error:
            parsed = self._read_response(error, operation)
            raise _parse_error(parsed, operation) from None
        except (OSError, URLError, TimeoutError):
            raise DurableAgentHTTPClientError(
                "TBM_DURABLE_SDK_TRANSPORT_ERROR",
                "internal",
                operation,
                "durable HTTP service could not be reached",
                retryable=True,
            ) from None
        with response:
            return self._read_response(response, operation)

    @staticmethod
    def _read_response(response: object, operation: DurableSDKOperation) -> object:
        try:
            headers = response.headers
            protocols = headers.get_all("X-TBM-Protocol-Version", failobj=[])
            content_types = headers.get_all("Content-Type", failobj=[])
            lengths = headers.get_all("Content-Length", failobj=[])
            source_length = lengths[0] if len(lengths) == 1 else None
            if (
                protocols != [DURABLE_AGENT_WIRE_PROTOCOL_VERSION]
                or len(content_types) != 1
                or content_types[0].split(";", 1)[0].strip().lower()
                != "application/json"
                or headers.get("Transfer-Encoding") is not None
                or type(source_length) is not str
                or not source_length.isascii()
                or not source_length.isdecimal()
            ):
                raise ValueError("response headers are invalid")
            normalized = source_length.lstrip("0") or "0"
            if len(normalized) > len(str(DURABLE_SDK_RESPONSE_MAX_BYTES)):
                raise ValueError("response length is invalid")
            length = int(normalized, 10)
            if not 0 <= length <= DURABLE_SDK_RESPONSE_MAX_BYTES:
                raise ValueError("response length is invalid")
            raw = response.read(DURABLE_SDK_RESPONSE_MAX_BYTES + 1)
            if len(raw) != length or len(raw) > DURABLE_SDK_RESPONSE_MAX_BYTES:
                raise ValueError("response body size is invalid")
            source = decode_bounded_utf8(
                raw,
                max_bytes=DURABLE_SDK_RESPONSE_MAX_BYTES,
                description="durable SDK HTTP response",
            )
            return parse_bounded_json(
                source,
                description="durable SDK HTTP response",
                max_nodes=DURABLE_SDK_RESPONSE_MAX_NODES,
                max_depth=DURABLE_SDK_RESPONSE_MAX_DEPTH,
            )
        except DurableAgentHTTPClientError:
            raise
        except Exception:
            raise DurableAgentHTTPClientError(
                "TBM_DURABLE_SDK_RESPONSE_INVALID",
                "internal",
                operation,
                "durable HTTP service returned an invalid response",
            ) from None


class AsyncDurableAgentHTTPClient:
    """Async wrapper; urllib calls execute in worker threads."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 30.0,
        tls_context: ssl.SSLContext | None = None,
    ) -> None:
        self._client = DurableAgentHTTPClient(
            base_url,
            token,
            timeout_seconds=timeout_seconds,
            tls_context=tls_context,
        )
        self._closed = False

    async def __aenter__(self) -> Self:
        self._ensure_open("open")
        return self

    async def __aexit__(
        self,
        exc_type: object,
        exc_value: object,
        traceback: object,
    ) -> None:
        await self.aclose()

    async def aclose(self) -> None:
        self._closed = True

    async def capabilities(self) -> dict[str, object]:
        self._ensure_open("capabilities")
        return await asyncio.to_thread(self._client.capabilities)

    async def openapi(self) -> dict[str, object]:
        self._ensure_open("openapi")
        return await asyncio.to_thread(self._client.openapi)

    async def health(self) -> dict[str, object]:
        self._ensure_open("health")
        return await asyncio.to_thread(self._client.health)

    async def prepare(
        self,
        request: DurablePrepareRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return await self._call("prepare", request)

    async def decide(
        self,
        request: DurableDecideRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return await self._call("decide", request)

    async def finalize(
        self,
        request: DurableFinalizeRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return await self._call("finalize", request)

    async def start(
        self,
        request: DurableStartRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return await self._call("start", request)

    async def resume(
        self,
        request: DurableResumeRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return await self._call("resume", request)

    async def abandon(
        self,
        request: DurableAbandonRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return await self._call("abandon", request)

    async def complete(
        self,
        request: DurableCompleteRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return await self._call("complete", request)

    async def cancel(
        self,
        request: DurableCancelRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return await self._call("cancel", request)

    async def get_session(
        self,
        request: DurableGetSessionRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return await self._call("get_session", request)

    async def export_replay(
        self,
        request: DurableReplayRequest | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        return await self._call("export_replay", request)

    async def _call(
        self,
        operation: DurableAgentWireOperation,
        request: BaseModel | Mapping[str, object],
    ) -> DurableAgentHTTPResponse:
        self._ensure_open(operation)
        callback = getattr(self._client, operation)
        return await asyncio.to_thread(callback, request)

    def _ensure_open(self, operation: DurableSDKOperation) -> None:
        if self._closed:
            raise DurableAgentHTTPClientError(
                "TBM_DURABLE_SDK_CLOSED",
                "state",
                operation,
                "async durable HTTP client is closed",
            )


def _parse_operation_response(
    value: object,
    operation: DurableAgentWireOperation,
) -> DurableAgentHTTPResponse:
    payload = _mapping(value, "durable operation response")
    _exact_keys(
        payload,
        {"protocol_version", "operation", "result"},
        "durable operation response",
    )
    _protocol(payload)
    if payload["operation"] != operation:
        _invalid_response("durable response operation is invalid")
    result = _mapping(payload["result"], "durable operation result")
    _exact_keys(result, _RESULT_KEYS[operation], "durable operation result")
    return DurableAgentHTTPResponse(
        DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
        operation,
        dict(result),
    )


def _parse_error(
    value: object,
    requested_operation: DurableSDKOperation,
) -> DurableAgentHTTPClientError:
    payload = _mapping(value, "durable error response")
    _exact_keys(
        payload,
        {"protocol_version", "error"},
        "durable error response",
    )
    _protocol(payload)
    detail = _mapping(payload["error"], "durable error detail")
    _exact_keys(
        detail,
        {"code", "category", "message", "operation", "retryable"},
        "durable error detail",
    )
    code = detail["code"]
    category = detail["category"]
    operation = detail["operation"]
    message = detail["message"]
    retryable = detail["retryable"]
    if (
        type(code) is not str
        or _ERROR_CODE_RE.fullmatch(code) is None
        or type(category) is not str
        or category not in _ERROR_CATEGORIES
        or type(operation) is not str
        or operation not in _SERVER_OPERATIONS
        or operation != requested_operation
        or type(message) is not str
        or not message
        or len(message) > DURABLE_SDK_ERROR_MESSAGE_MAX_CHARS
        or type(retryable) is not bool
    ):
        _invalid_response("durable error fields are invalid")
    return DurableAgentHTTPClientError(
        code,
        cast(DurableAgentWireErrorCategory, category),
        cast(DurableSDKOperation, operation),
        message,
        retryable=retryable,
    )


def _mapping(value: object, name: str) -> Mapping[str, object]:
    if not isinstance(value, dict) or any(type(key) is not str for key in value):
        _invalid_response(f"{name} must be an object")
    return value


def _exact_keys(
    value: Mapping[str, object],
    expected: set[str],
    name: str,
) -> None:
    if set(value) != expected:
        _invalid_response(f"{name} fields are invalid")


def _protocol(value: Mapping[str, object]) -> None:
    if value.get("protocol_version") != DURABLE_AGENT_WIRE_PROTOCOL_VERSION:
        _invalid_response("durable protocol_version is invalid")


def _invalid_input(
    operation: DurableSDKOperation,
    message: str,
) -> NoReturn:
    raise DurableAgentHTTPClientError(
        "TBM_DURABLE_SDK_INVALID_INPUT",
        "input",
        operation,
        message,
    )


def _invalid_response(message: str) -> NoReturn:
    raise DurableAgentHTTPClientError(
        "TBM_DURABLE_SDK_RESPONSE_INVALID",
        "internal",
        "open",
        message,
    )


__all__ = [
    "DURABLE_SDK_ERROR_MESSAGE_MAX_CHARS",
    "DURABLE_SDK_RESPONSE_MAX_BYTES",
    "DURABLE_SDK_RESPONSE_MAX_DEPTH",
    "DURABLE_SDK_RESPONSE_MAX_NODES",
    "DURABLE_SDK_TOKEN_MAX_CHARS",
    "DURABLE_SDK_TOKEN_MIN_CHARS",
    "AsyncDurableAgentHTTPClient",
    "DurableAgentHTTPClient",
    "DurableAgentHTTPClientError",
    "DurableAgentHTTPResponse",
    "DurableSDKOperation",
]
