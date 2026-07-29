from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import ipaddress
import json
import re
from typing import Literal, NoReturn, cast
from urllib.error import HTTPError, URLError
from urllib.parse import urlsplit
from urllib.request import (
    HTTPRedirectHandler,
    ProxyHandler,
    Request,
    build_opener,
)

from ._ingestion import (
    CLI_JSON_FILE_MAX_BYTES,
    CLI_JSON_MAX_DEPTH,
    CLI_JSON_MAX_NODES,
    decode_bounded_utf8,
    parse_bounded_json,
)
from .agent import (
    AGENT_PROTOCOL_VERSION,
    AgentCapabilities,
    AgentCompletedRun,
    AgentErrorCategory,
    AgentFinalizedMemory,
    AgentMemoryError,
    AgentOperation,
    AgentPreparedMemory,
)


SDK_RESPONSE_MAX_BYTES = CLI_JSON_FILE_MAX_BYTES
SDK_RESPONSE_MAX_NODES = CLI_JSON_MAX_NODES
SDK_RESPONSE_MAX_DEPTH = CLI_JSON_MAX_DEPTH
SDK_TOKEN_MIN_CHARS = 32
SDK_TOKEN_MAX_CHARS = 512
_IDENTIFIER_MAX_CHARS = 128
_DECISION_REASON_MAX_CHARS = 2_000
_ERROR_MESSAGE_MAX_CHARS = 2_048
_PREPARED_CANDIDATES_MAX_ITEMS = 1_000
_SYSTEM_ALLOWED_MAX_ITEMS = 50
_FINAL_ALLOWED_MAX_ITEMS = 20
_BLOCKED_MAX_ITEMS = 1_000
_PROMPT_MAX_CHARS = 32_000
_SNIPPET_MAX_CHARS = 12_000
_CAPABILITY_LIMIT_FIELDS = {
    "gate_candidates",
    "gate_prompt_chars",
    "gate_response_bytes",
    "gate_response_nodes",
    "gate_response_depth",
    "decision_reason_chars",
    "injection_memories",
    "injection_chars",
    "prepared_request_candidates",
    "pending_requests",
    "pending_candidate_references",
    "finalized_request_replays",
}
_STORAGE_MODES = {"memory", "sqlite", "postgres"}
_OPERATIONS = {
    "capture",
    "prepare",
    "finalize",
    "complete",
    "cancel",
    "run",
    "flush",
    "health",
}
_MODES = {
    "debug",
    "repair",
    "regression",
    "planning",
    "eval",
    "production",
}
_ERROR_CODE_PATTERN = re.compile(r"^TBM_[A-Z0-9_]+$")


@dataclass(frozen=True)
class AgentCanceledRun:
    protocol_version: str
    request_id: str
    canceled: bool

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": self.protocol_version,
            "request_id": self.request_id,
            "canceled": self.canceled,
        }


class _NoRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: object, **kwargs: object) -> None:
        return None


class AgentHTTPClient:
    """Typed loopback client for the local tbm.agent.v1 HTTP profile."""

    def __init__(
        self,
        base_url: str,
        token: str,
        *,
        timeout_seconds: float = 30.0,
    ) -> None:
        if type(base_url) is not str:
            raise ValueError("base_url must be a string")
        parsed = urlsplit(base_url)
        try:
            address = ipaddress.ip_address(parsed.hostname or "")
            port = parsed.port
        except ValueError as error:
            raise ValueError(
                "base_url must identify a loopback HTTP service"
            ) from error
        if (
            parsed.scheme != "http"
            or not address.is_loopback
            or address.version != 4
            or port is None
            or parsed.username is not None
            or parsed.password is not None
            or parsed.query
            or parsed.fragment
            or parsed.path not in {"", "/"}
        ):
            raise ValueError(
                "base_url must identify a loopback HTTP service with a port"
            )
        if (
            type(token) is not str
            or not SDK_TOKEN_MIN_CHARS <= len(token) <= SDK_TOKEN_MAX_CHARS
            or not token.strip()
            or token.strip() != token
        ):
            raise ValueError("token must be a bounded nonblank bearer secret")
        if (
            type(timeout_seconds) not in {int, float}
            or isinstance(timeout_seconds, bool)
            or not 0 < float(timeout_seconds) <= 300
        ):
            raise ValueError("timeout_seconds must be between 0 and 300")
        self._base_url = f"http://{address.compressed}:{port}"
        self._token = token
        self._timeout_seconds = float(timeout_seconds)
        self._opener = build_opener(ProxyHandler({}), _NoRedirect())

    def capabilities(self) -> AgentCapabilities:
        return _parse_capabilities(
            self._request("GET", "/v1/capabilities", None, "health")
        )

    def health(self) -> dict[str, object]:
        return dict(
            _mapping(
                self._request("GET", "/v1/health", None, "health"),
                "health response",
            )
        )

    def prepare(
        self,
        request: Mapping[str, object],
    ) -> AgentPreparedMemory:
        return _parse_prepared(
            self._request("POST", "/v1/prepare", request, "prepare")
        )

    def finalize(
        self,
        request: Mapping[str, object],
    ) -> AgentFinalizedMemory:
        return _parse_finalized(
            self._request("POST", "/v1/finalize", request, "finalize")
        )

    def complete(
        self,
        request: Mapping[str, object],
    ) -> AgentCompletedRun:
        return _parse_completed(
            self._request("POST", "/v1/complete", request, "complete")
        )

    def cancel(
        self,
        request: Mapping[str, object],
    ) -> AgentCanceledRun:
        payload = _mapping(
            self._request("POST", "/v1/cancel", request, "cancel"),
            "cancel response",
        )
        _exact_keys(
            payload,
            {"protocol_version", "request_id", "canceled"},
            "cancel response",
        )
        _protocol(payload)
        return AgentCanceledRun(
            protocol_version=AGENT_PROTOCOL_VERSION,
            request_id=_string(
                payload["request_id"],
                "request_id",
                max_chars=_IDENTIFIER_MAX_CHARS,
            ),
            canceled=_true(payload["canceled"], "canceled"),
        )

    def _request(
        self,
        method: Literal["GET", "POST"],
        path: str,
        payload: Mapping[str, object] | None,
        operation: AgentOperation,
    ) -> object:
        body: bytes | None = None
        headers = {
            "Accept": "application/json",
            "Authorization": f"Bearer {self._token}",
        }
        if payload is not None:
            if not isinstance(payload, Mapping) or any(
                type(key) is not str for key in payload
            ):
                _sdk_input(operation, "request must be a string-keyed mapping")
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
                _sdk_input(operation, "request is not bounded JSON data")
            if len(body) > CLI_JSON_FILE_MAX_BYTES:
                _sdk_input(operation, "request exceeds the wire size limit")
            try:
                source = decode_bounded_utf8(
                    body,
                    max_bytes=CLI_JSON_FILE_MAX_BYTES,
                    description="SDK HTTP request",
                )
                parse_bounded_json(
                    source,
                    description="SDK HTTP request",
                    max_nodes=CLI_JSON_MAX_NODES,
                    max_depth=CLI_JSON_MAX_DEPTH,
                )
            except (TypeError, UnicodeError, ValueError, OverflowError):
                _sdk_input(operation, "request is not bounded JSON data")
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
            raise AgentMemoryError(
                "TBM_SDK_TRANSPORT_ERROR",
                "callback",
                operation,
                "local HTTP service could not be reached",
                retryable=True,
            ) from None
        with response:
            return self._read_response(response, operation)

    @staticmethod
    def _read_response(response: object, operation: AgentOperation) -> object:
        try:
            headers = response.headers
            protocols = headers.get_all("X-TBM-Protocol-Version", failobj=[])
            content_types = headers.get_all("Content-Type", failobj=[])
            lengths = headers.get_all("Content-Length", failobj=[])
            length_source = lengths[0] if len(lengths) == 1 else None
            if (
                protocols != [AGENT_PROTOCOL_VERSION]
                or len(content_types) != 1
                or content_types[0].split(";", 1)[0].strip().lower()
                != "application/json"
                or headers.get("Transfer-Encoding") is not None
                or type(length_source) is not str
                or not length_source.isascii()
                or not length_source.isdecimal()
            ):
                raise ValueError("response headers are invalid")
            normalized_length = length_source.lstrip("0") or "0"
            if len(normalized_length) > len(str(SDK_RESPONSE_MAX_BYTES)):
                raise ValueError("response length is invalid")
            length = int(normalized_length, 10)
            if not 0 <= length <= SDK_RESPONSE_MAX_BYTES:
                raise ValueError("response length is invalid")
            raw = response.read(SDK_RESPONSE_MAX_BYTES + 1)
            if len(raw) != length or len(raw) > SDK_RESPONSE_MAX_BYTES:
                raise ValueError("response body size is invalid")
            source = decode_bounded_utf8(
                raw,
                max_bytes=SDK_RESPONSE_MAX_BYTES,
                description="SDK HTTP response",
            )
            return parse_bounded_json(
                source,
                description="SDK HTTP response",
                max_nodes=SDK_RESPONSE_MAX_NODES,
                max_depth=SDK_RESPONSE_MAX_DEPTH,
            )
        except AgentMemoryError:
            raise
        except Exception:
            raise AgentMemoryError(
                "TBM_SDK_RESPONSE_INVALID",
                "callback",
                operation,
                "local HTTP service returned an invalid response",
            ) from None


def _parse_capabilities(value: object) -> AgentCapabilities:
    payload = _mapping(value, "capabilities response")
    keys = {
        "protocol_version",
        "snapshot_version",
        "sqlite_schema_version",
        "postgres_schema_version",
        "storage_modes",
        "operations",
        "modes",
        "limits",
        "durable_records",
        "process_local_records",
    }
    _exact_keys(payload, keys, "capabilities response")
    _protocol(payload)
    limits = _mapping(payload["limits"], "capabilities limits")
    if set(limits) != _CAPABILITY_LIMIT_FIELDS or any(
        type(key) is not str or type(item) is not int or item < 0
        for key, item in limits.items()
    ):
        _invalid_response("capabilities limits are invalid")
    storage_modes = _string_tuple(
        payload["storage_modes"],
        "storage_modes",
        allowed=_STORAGE_MODES,
    )
    operations = _string_tuple(
        payload["operations"],
        "operations",
        allowed=_OPERATIONS,
    )
    modes = _string_tuple(
        payload["modes"],
        "modes",
        allowed=_MODES,
    )
    return AgentCapabilities(
        protocol_version=AGENT_PROTOCOL_VERSION,
        snapshot_version=_integer(
            payload["snapshot_version"],
            "snapshot_version",
            minimum=1,
        ),
        sqlite_schema_version=_integer(
            payload["sqlite_schema_version"],
            "sqlite_schema_version",
            minimum=1,
        ),
        postgres_schema_version=_integer(
            payload["postgres_schema_version"],
            "postgres_schema_version",
            minimum=1,
        ),
        storage_modes=storage_modes,
        operations=operations,
        modes=modes,
        limits=dict(limits),
        durable_records=_string_tuple(
            payload["durable_records"],
            "durable_records",
        ),
        process_local_records=_string_tuple(
            payload["process_local_records"],
            "process_local_records",
        ),
    )


def _parse_prepared(value: object) -> AgentPreparedMemory:
    payload = _mapping(value, "prepared response")
    _exact_keys(
        payload,
        {
            "protocol_version",
            "request_id",
            "trace_id",
            "run_id",
            "candidate_memory_ids",
            "system_allowed_memory_ids",
            "system_blocked",
            "prompt",
        },
        "prepared response",
    )
    _protocol(payload)
    blocked = _mapping(payload["system_blocked"], "system_blocked")
    if len(blocked) > _BLOCKED_MAX_ITEMS or any(
        type(key) is not str
        or not key
        or not key.strip()
        or len(key) > _IDENTIFIER_MAX_CHARS
        or type(item) is not str
        or not item
        or not item.strip()
        or len(item) > _DECISION_REASON_MAX_CHARS
        for key, item in blocked.items()
    ):
        _invalid_response("system_blocked is invalid")
    return AgentPreparedMemory(
        protocol_version=AGENT_PROTOCOL_VERSION,
        request_id=_identifier(payload["request_id"], "request_id"),
        trace_id=_identifier(payload["trace_id"], "trace_id"),
        run_id=_identifier(payload["run_id"], "run_id"),
        candidate_memory_ids=_string_tuple(
            payload["candidate_memory_ids"],
            "candidate_memory_ids",
            max_items=_PREPARED_CANDIDATES_MAX_ITEMS,
            max_chars=_IDENTIFIER_MAX_CHARS,
        ),
        system_allowed_memory_ids=_string_tuple(
            payload["system_allowed_memory_ids"],
            "system_allowed_memory_ids",
            max_items=_SYSTEM_ALLOWED_MAX_ITEMS,
            max_chars=_IDENTIFIER_MAX_CHARS,
        ),
        system_blocked=tuple(
            (cast(str, key), cast(str, item))
            for key, item in blocked.items()
        ),
        prompt=_string(
            payload["prompt"],
            "prompt",
            allow_empty=True,
            max_chars=_PROMPT_MAX_CHARS,
        ),
    )


def _parse_finalized(value: object) -> AgentFinalizedMemory:
    payload = _mapping(value, "finalized response")
    _exact_keys(
        payload,
        {
            "protocol_version",
            "request_id",
            "trace_id",
            "decision_id",
            "use_memory",
            "allowed_memory_ids",
            "blocked_memory_ids",
            "reason",
            "risk",
            "recommended_injection",
            "snippet",
        },
        "finalized response",
    )
    _protocol(payload)
    risk = _string(payload["risk"], "risk")
    injection = _string(
        payload["recommended_injection"],
        "recommended_injection",
    )
    if risk not in {"none", "low", "medium", "high"} or injection not in {
        "none",
        "short_summary",
        "full_case_summary",
        "pointer_only",
    }:
        _invalid_response("finalized decision enums are invalid")
    return AgentFinalizedMemory(
        protocol_version=AGENT_PROTOCOL_VERSION,
        request_id=_identifier(payload["request_id"], "request_id"),
        trace_id=_identifier(payload["trace_id"], "trace_id"),
        decision_id=_identifier(payload["decision_id"], "decision_id"),
        use_memory=_boolean(payload["use_memory"], "use_memory"),
        allowed_memory_ids=_string_tuple(
            payload["allowed_memory_ids"],
            "allowed_memory_ids",
            max_items=_FINAL_ALLOWED_MAX_ITEMS,
            max_chars=_IDENTIFIER_MAX_CHARS,
        ),
        blocked_memory_ids=_string_tuple(
            payload["blocked_memory_ids"],
            "blocked_memory_ids",
            max_items=_BLOCKED_MAX_ITEMS,
            max_chars=_IDENTIFIER_MAX_CHARS,
        ),
        reason=_string(
            payload["reason"],
            "reason",
            max_chars=_DECISION_REASON_MAX_CHARS,
        ),
        risk=cast(Literal["none", "low", "medium", "high"], risk),
        recommended_injection=cast(
            Literal[
                "none",
                "short_summary",
                "full_case_summary",
                "pointer_only",
            ],
            injection,
        ),
        snippet=_string(
            payload["snippet"],
            "snippet",
            allow_empty=True,
            max_chars=_SNIPPET_MAX_CHARS,
        ),
    )


def _parse_completed(value: object) -> AgentCompletedRun:
    payload = _mapping(value, "completed response")
    _exact_keys(
        payload,
        {
            "protocol_version",
            "request_id",
            "trace_id",
            "run_id",
            "decision_id",
            "eval_result",
            "memory_caused_failure",
        },
        "completed response",
    )
    _protocol(payload)
    eval_result = _string(payload["eval_result"], "eval_result")
    if eval_result not in {"pass", "fail", "error"}:
        _invalid_response("eval_result is invalid")
    request_id_value = payload["request_id"]
    request_id = (
        None
        if request_id_value is None
        else _identifier(request_id_value, "request_id")
    )
    return AgentCompletedRun(
        protocol_version=AGENT_PROTOCOL_VERSION,
        request_id=request_id,
        trace_id=_identifier(payload["trace_id"], "trace_id"),
        run_id=_identifier(payload["run_id"], "run_id"),
        decision_id=_identifier(payload["decision_id"], "decision_id"),
        eval_result=cast(Literal["pass", "fail", "error"], eval_result),
        memory_caused_failure=_boolean(
            payload["memory_caused_failure"],
            "memory_caused_failure",
        ),
    )


def _parse_error(value: object, operation: AgentOperation) -> AgentMemoryError:
    payload = _mapping(value, "error response")
    _exact_keys(payload, {"protocol_version", "error"}, "error response")
    _protocol(payload)
    detail = _mapping(payload["error"], "error detail")
    required = {
        "code",
        "category",
        "message",
        "operation",
        "retryable",
    }
    if not required.issubset(detail) or not set(detail).issubset(
        required | {"request_id", "decision_id"}
    ):
        _invalid_response("error detail fields are invalid")
    category = _string(detail["category"], "category")
    server_operation = _string(detail["operation"], "operation")
    if category not in {
        "input",
        "state",
        "persistence",
        "callback",
        "closed",
        "internal",
    } or server_operation not in {
        "open",
        "capture",
        "prepare",
        "finalize",
        "complete",
        "cancel",
        "flush",
        "health",
        "run",
        "close",
    }:
        _invalid_response("error category or operation is invalid")
    code = _string(detail["code"], "code")
    if _ERROR_CODE_PATTERN.fullmatch(code) is None:
        _invalid_response("error code is invalid")
    return AgentMemoryError(
        code,
        cast(AgentErrorCategory, category),
        cast(AgentOperation, server_operation),
        _string(
            detail["message"],
            "message",
            max_chars=_ERROR_MESSAGE_MAX_CHARS,
        ),
        retryable=_boolean(detail["retryable"], "retryable"),
        request_id=(
            None
            if "request_id" not in detail
            else _identifier(detail["request_id"], "request_id")
        ),
        decision_id=(
            None
            if "decision_id" not in detail
            else _identifier(detail["decision_id"], "decision_id")
        ),
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
    if value.get("protocol_version") != AGENT_PROTOCOL_VERSION:
        _invalid_response("protocol_version is invalid")


def _string(
    value: object,
    name: str,
    *,
    allow_empty: bool = False,
    max_chars: int = SDK_RESPONSE_MAX_BYTES,
) -> str:
    if (
        type(value) is not str
        or len(value) > max_chars
        or (not allow_empty and (not value or not value.strip()))
    ):
        _invalid_response(f"{name} is invalid")
    return value


def _identifier(value: object, name: str) -> str:
    return _string(value, name, max_chars=_IDENTIFIER_MAX_CHARS)


def _string_tuple(
    value: object,
    name: str,
    *,
    max_items: int = SDK_RESPONSE_MAX_NODES,
    max_chars: int = SDK_RESPONSE_MAX_BYTES,
    allowed: set[str] | None = None,
) -> tuple[str, ...]:
    if (
        type(value) is not list
        or len(value) > max_items
        or any(
            type(item) is not str
            or not item
            or not item.strip()
            or len(item) > max_chars
            or (allowed is not None and item not in allowed)
            for item in value
        )
        or len(set(value)) != len(value)
    ):
        _invalid_response(f"{name} is invalid")
    return tuple(value)


def _integer(value: object, name: str, *, minimum: int = 0) -> int:
    if type(value) is not int or value < minimum:
        _invalid_response(f"{name} is invalid")
    return value


def _boolean(value: object, name: str) -> bool:
    if type(value) is not bool:
        _invalid_response(f"{name} is invalid")
    return value


def _true(value: object, name: str) -> bool:
    if value is not True:
        _invalid_response(f"{name} is invalid")
    return True


def _sdk_input(operation: AgentOperation, message: str) -> NoReturn:
    raise AgentMemoryError(
        "TBM_SDK_INVALID_INPUT",
        "input",
        operation,
        message,
    )


def _invalid_response(message: str) -> NoReturn:
    raise AgentMemoryError(
        "TBM_SDK_RESPONSE_INVALID",
        "callback",
        "open",
        message,
    )


__all__ = [
    "SDK_RESPONSE_MAX_BYTES",
    "SDK_RESPONSE_MAX_DEPTH",
    "SDK_RESPONSE_MAX_NODES",
    "SDK_TOKEN_MAX_CHARS",
    "SDK_TOKEN_MIN_CHARS",
    "AgentCanceledRun",
    "AgentHTTPClient",
]
