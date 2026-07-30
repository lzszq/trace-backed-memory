from __future__ import annotations

from copy import deepcopy
import json
from typing import cast

from pydantic import BaseModel

from .durable_agent_wire_v1 import (
    DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
    DurableAbandonRequest,
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


DURABLE_AGENT_HTTP_OPENAPI_VERSION = "tbm.durable-agent-http.v1"
_REQUEST_MODELS: dict[str, type[BaseModel]] = {
    "Prepare": DurablePrepareRequest,
    "Decide": DurableDecideRequest,
    "Finalize": DurableFinalizeRequest,
    "Start": DurableStartRequest,
    "Resume": DurableResumeRequest,
    "Abandon": DurableAbandonRequest,
    "Complete": DurableCompleteRequest,
    "Cancel": DurableCancelRequest,
    "GetSession": DurableGetSessionRequest,
    "ExportReplay": DurableReplayRequest,
}
_ROUTES = (
    ("prepare", "Prepare", "/durable/v1/prepare"),
    ("decide", "Decide", "/durable/v1/decide"),
    ("finalize", "Finalize", "/durable/v1/finalize"),
    ("start", "Start", "/durable/v1/start"),
    ("resume", "Resume", "/durable/v1/resume"),
    ("abandon", "Abandon", "/durable/v1/abandon"),
    ("complete", "Complete", "/durable/v1/complete"),
    ("cancel", "Cancel", "/durable/v1/cancel"),
    ("get_session", "GetSession", "/durable/v1/get-session"),
    ("export_replay", "ExportReplay", "/durable/v1/export-replay"),
)
_RESULT_KEYS = {
    "prepare": (
        "authorization_event_id",
        "session",
        "retrieval_snapshot",
        "system_gate_evaluation",
        "retrieval_policy",
    ),
    "decide": (
        "session",
        "attempt",
        "prompt_artifact",
        "response_artifact",
        "replayed",
    ),
    "finalize": (
        "session",
        "usage_decision",
        "injection",
        "manifest",
        "snippet",
        "content_exposed",
        "replayed",
    ),
    "start": (
        "session",
        "usage_decision",
        "injection",
        "manifest",
        "transition_authorization_event_id",
        "snippet",
        "content_exposed",
        "execution_required",
        "replayed",
    ),
    "resume": (
        "session",
        "usage_decision",
        "injection",
        "manifest",
        "transition_authorization_event_id",
        "snippet",
        "content_exposed",
        "execution_required",
        "replayed",
    ),
    "abandon": (
        "session",
        "transition_authorization_event_id",
        "replayed",
    ),
    "complete": (
        "session",
        "outcome",
        "outbox_event",
        "outbox_delivery",
        "transition_authorization_event_id",
        "inserted",
        "event_inserted",
        "replayed",
    ),
    "cancel": (
        "session",
        "transition_authorization_event_id",
        "replayed",
    ),
    "get_session": ("session",),
    "export_replay": (
        "session",
        "bundle",
        "read_authorization_event_id",
        "retrieval_authorization_event_id",
        "content_exposed",
    ),
}


def durable_agent_http_openapi() -> dict[str, object]:
    """Return the deterministic OpenAPI 3.1 contract served by the adapter."""

    schemas: dict[str, object] = {
        f"Durable{name}Request": _request_schema(model)
        for name, model in _REQUEST_MODELS.items()
    }
    schemas.update(
        {
            "DurableCapabilities": _capabilities_schema(),
            "DurableHealth": _health_schema(),
            "DurableError": _error_schema(),
        }
    )
    for operation, _name, _path in _ROUTES:
        schemas[f"Durable{_pascal(operation)}Success"] = (
            _success_schema(operation)
        )

    paths: dict[str, object] = {
        "/durable/v1/openapi": {
            "get": _get_operation(
                "getDurableAgentOpenAPI",
                "Read this authenticated OpenAPI contract",
                {"type": "object"},
            )
        },
        "/durable/v1/capabilities": {
            "get": _get_operation(
                "getDurableAgentCapabilities",
                "Discover durable operations and fail-closed content profiles",
                {"$ref": "#/components/schemas/DurableCapabilities"},
            )
        },
        "/durable/v1/health": {
            "get": _get_operation(
                "getDurableAgentHealth",
                "Read non-sensitive durable adapter health",
                {"$ref": "#/components/schemas/DurableHealth"},
            )
        },
    }
    for operation, name, path in _ROUTES:
        paths[path] = {
            "post": _post_operation(
                operation,
                f"Durable{name}Request",
                f"Durable{_pascal(operation)}Success",
            )
        }

    return {
        "openapi": "3.1.0",
        "jsonSchemaDialect": (
            "https://json-schema.org/draft/2020-12/schema"
        ),
        "info": {
            "title": "Trace-backed Memory authenticated durable Agent HTTP API",
            "version": DURABLE_AGENT_HTTP_OPENAPI_VERSION,
            "description": (
                "Durable GateSession HTTP binding. The adapter authenticates "
                "each request and constructs service, provider, and evaluator "
                "contexts outside request JSON. Non-loopback deployment "
                "requires TLS."
            ),
        },
        "servers": [
            {
                "url": "http://127.0.0.1:8766",
                "description": "Authenticated loopback profile",
            },
            {
                "url": "https://tbm.example.invalid",
                "description": (
                    "Operator-provisioned TLS bind with the same required "
                    "bearer boundary"
                ),
            },
        ],
        "security": [{"bearerAuth": []}],
        "paths": paths,
        "components": {
            "securitySchemes": {
                "bearerAuth": {
                    "type": "http",
                    "scheme": "bearer",
                    "bearerFormat": "opaque local secret",
                },
            },
            "headers": {
                "ProtocolVersion": {
                    "required": True,
                    "schema": {
                        "type": "string",
                        "const": DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
                    },
                }
            },
            "schemas": schemas,
            "responses": {
                "BadRequest": _error_response("Invalid request"),
                "Unauthorized": _error_response(
                    "Transport authentication failed"
                ),
                "Forbidden": _error_response("Authorization denied"),
                "NotFound": _error_response("Resource not found"),
                "Conflict": _error_response("Durable state conflict"),
                "InternalError": _error_response("Internal failure"),
                "ServiceUnavailable": _error_response(
                    "Retryable provider, persistence, evaluator, or recovery "
                    "failure"
                ),
            },
        },
    }


def dumps_durable_agent_http_openapi() -> bytes:
    return (
        json.dumps(
            durable_agent_http_openapi(),
            allow_nan=False,
            ensure_ascii=False,
            indent=2,
            sort_keys=True,
        )
        + "\n"
    ).encode("utf-8")


def _request_schema(model: type[BaseModel]) -> dict[str, object]:
    schema = deepcopy(model.model_json_schema(mode="validation"))
    rewritten = cast(
        dict[str, object],
        _rewrite_refs(schema, prefix=f"#/components/schemas/{model.__name__}"),
    )
    _augment_request_schema(model, rewritten)
    return rewritten


def _augment_request_schema(
    model: type[BaseModel],
    schema: dict[str, object],
) -> None:
    properties = cast(dict[str, object], schema["properties"])
    if model is DurablePrepareRequest:
        schema["dependentRequired"] = {
            "evaluation_suite": ["evaluation_case_id"],
            "evaluation_case_id": ["evaluation_suite"],
        }
        _canonical_base64(properties["query_base64"])
    if model is DurableDecideRequest:
        _canonical_base64(properties["prompt_base64"])
        _canonical_base64(properties["response_base64"])
        for name in (
            "final_allowed_revision_ids",
            "final_blocked_revision_ids",
        ):
            field = cast(dict[str, object], properties[name])
            field["uniqueItems"] = True
            field["items"] = {
                "type": "string",
                "pattern": "^memory_revision_sha256_[0-9a-f]{64}$",
            }
            field["description"] = (
                "Canonical IDs in lexicographic order; allowed and blocked "
                "sets must be disjoint."
            )
    if model is DurableCompleteRequest:
        evidence = cast(
            dict[str, object],
            properties["evidence_artifact_sha256s"],
        )
        evidence["uniqueItems"] = True
        evidence["items"] = {
            "type": "string",
            "pattern": "^sha256:[0-9a-f]{64}$",
        }
        evidence["description"] = (
            "Canonical SHA-256 digests in lexicographic order."
        )
        schema["allOf"] = [
            {
                "anyOf": [
                    {"required": ["output_sha256"]},
                    {"required": ["tool_outputs_sha256"]},
                ]
            },
            {
                "if": {
                    "properties": {"result": {"const": "error"}},
                    "required": ["result"],
                },
                "then": {
                    "required": ["error_code"],
                    "properties": {
                        "error_code": {"type": "string"}
                    },
                },
                "else": {
                    "properties": {
                        "error_code": {"type": "null"}
                    }
                },
            },
        ]
    if model is DurableReplayRequest:
        classifications = cast(
            dict[str, object],
            properties["allowed_classifications"],
        )
        classifications["uniqueItems"] = True


def _canonical_base64(value: object) -> None:
    field = cast(dict[str, object], value)
    branches = field.get("anyOf")
    targets = (
        cast(list[object], branches)
        if isinstance(branches, list)
        else [field]
    )
    for target in targets:
        if (
            isinstance(target, dict)
            and target.get("type") == "string"
        ):
            target["pattern"] = (
                "^(?:[A-Za-z0-9+/]{4})*"
                "(?:[A-Za-z0-9+/]{2}==|[A-Za-z0-9+/]{3}=)?$"
            )
            target["contentEncoding"] = "base64"


def _rewrite_refs(value: object, *, prefix: str) -> object:
    if isinstance(value, dict):
        return {
            key: (
                prefix + item[1:]
                if key == "$ref"
                and type(item) is str
                and item.startswith("#/$defs/")
                else _rewrite_refs(item, prefix=prefix)
            )
            for key, item in value.items()
        }
    if isinstance(value, list):
        return [_rewrite_refs(item, prefix=prefix) for item in value]
    return value


def _success_schema(operation: str) -> dict[str, object]:
    properties: dict[str, object] = {
        key: {}
        for key in _RESULT_KEYS[operation]
    }
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["protocol_version", "operation", "result"],
        "properties": {
            "protocol_version": {
                "type": "string",
                "const": DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
            },
            "operation": {"type": "string", "const": operation},
            "result": {
                "type": "object",
                "additionalProperties": False,
                "required": list(_RESULT_KEYS[operation]),
                "properties": properties,
            },
        },
    }


def _capabilities_schema() -> dict[str, object]:
    required = [
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
    ]
    return {
        "type": "object",
        "additionalProperties": False,
        "required": required,
        "properties": {
            "protocol_version": {
                "type": "string",
                "const": DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
            },
            "transport_profile": {
                "type": "string",
                "const": "durable-v3",
            },
            "durable_agent_contract_version": {"type": "string"},
            "storage_mode": {"enum": ["sqlite", "postgres"]},
            "operations": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string"},
            },
            "gate_session_statuses": {
                "type": "array",
                "uniqueItems": True,
                "items": {"type": "string"},
            },
            "identity_source": {
                "type": "string",
                "const": "trusted_adapter",
            },
            "transport_authentication": {
                "type": "string",
                "const": "required",
            },
            "caller_identity_fields": {"type": "boolean", "const": False},
            "durable_sessions": {"type": "boolean", "const": True},
            "process_local_records": {
                "type": "array",
                "maxItems": 0,
            },
            "injection_content_exposed": {"type": "boolean"},
            "replay_content_exposed": {"type": "boolean"},
            "limits": {"type": "object"},
        },
    }


def _health_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": [
            "protocol_version",
            "status",
            "storage_mode",
            "durable_sessions",
            "process_local_records",
        ],
        "properties": {
            "protocol_version": {
                "type": "string",
                "const": DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
            },
            "status": {"type": "string", "const": "ok"},
            "storage_mode": {"enum": ["sqlite", "postgres"]},
            "durable_sessions": {"type": "boolean", "const": True},
            "process_local_records": {
                "type": "array",
                "maxItems": 0,
            },
        },
    }


def _error_schema() -> dict[str, object]:
    return {
        "type": "object",
        "additionalProperties": False,
        "required": ["protocol_version", "error"],
        "properties": {
            "protocol_version": {
                "type": "string",
                "const": DURABLE_AGENT_WIRE_PROTOCOL_VERSION,
            },
            "error": {
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "code",
                    "category",
                    "message",
                    "operation",
                    "retryable",
                ],
                "properties": {
                    "code": {
                        "type": "string",
                        "pattern": "^TBM_[A-Z0-9_]{1,120}$",
                    },
                    "category": {
                        "enum": [
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
                        ]
                    },
                    "message": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 2000,
                    },
                    "operation": {"type": "string"},
                    "retryable": {"type": "boolean"},
                },
            },
        },
    }


def _get_operation(
    operation_id: str,
    summary: str,
    response_schema: dict[str, object],
) -> dict[str, object]:
    return {
        "operationId": operation_id,
        "summary": summary,
        "responses": {
            "200": _success_response(response_schema),
            "401": {"$ref": "#/components/responses/Unauthorized"},
            "404": {"$ref": "#/components/responses/NotFound"},
            "500": {"$ref": "#/components/responses/InternalError"},
            "503": {"$ref": "#/components/responses/ServiceUnavailable"},
        },
    }


def _post_operation(
    operation: str,
    request_schema: str,
    success_schema: str,
) -> dict[str, object]:
    return {
        "operationId": f"durable{_pascal(operation)}",
        "requestBody": {
            "required": True,
            "content": {
                "application/json": {
                    "schema": {
                        "$ref": f"#/components/schemas/{request_schema}"
                    }
                }
            },
        },
        "responses": {
            "200": _success_response(
                {"$ref": f"#/components/schemas/{success_schema}"}
            ),
            "400": {"$ref": "#/components/responses/BadRequest"},
            "401": {"$ref": "#/components/responses/Unauthorized"},
            "403": {"$ref": "#/components/responses/Forbidden"},
            "404": {"$ref": "#/components/responses/NotFound"},
            "409": {"$ref": "#/components/responses/Conflict"},
            "500": {"$ref": "#/components/responses/InternalError"},
            "503": {"$ref": "#/components/responses/ServiceUnavailable"},
        },
    }


def _success_response(schema: dict[str, object]) -> dict[str, object]:
    return {
        "description": "Authenticated durable response",
        "headers": {
            "X-TBM-Protocol-Version": {
                "$ref": "#/components/headers/ProtocolVersion"
            }
        },
        "content": {"application/json": {"schema": schema}},
    }


def _error_response(description: str) -> dict[str, object]:
    return {
        "description": description,
        "headers": {
            "X-TBM-Protocol-Version": {
                "$ref": "#/components/headers/ProtocolVersion"
            }
        },
        "content": {
            "application/json": {
                "schema": {"$ref": "#/components/schemas/DurableError"}
            }
        },
    }


def _pascal(value: str) -> str:
    return "".join(part.capitalize() for part in value.split("_"))


__all__ = [
    "DURABLE_AGENT_HTTP_OPENAPI_VERSION",
    "dumps_durable_agent_http_openapi",
    "durable_agent_http_openapi",
]
