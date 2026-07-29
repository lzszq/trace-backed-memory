from __future__ import annotations

from contextlib import contextmanager
import json
import os
from pathlib import Path
import sys
from threading import Thread
from typing import Iterator

import anyio
from jsonschema import Draft202012Validator
import pytest

import trace_backed_memory as tbm
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
    HTTP_REQUEST_MAX_DEPTH,
    HTTP_REQUEST_MAX_NODES,
    AgentHTTPServer,
    AgentHTTPServerConfiguration,
)
import trace_backed_memory.sdk as sdk_module


ROOT = Path(__file__).resolve().parents[1]
TOKEN = "agent_protocol_contract_" + "a" * 32
REQUEST_CONTRACTS = {
    "agent_prepare_request": PrepareMemoryRequest,
    "agent_finalize_request": FinalizeMemoryRequest,
    "agent_complete_request": CompleteRunRequest,
    "agent_cancel_request": CancelRunRequest,
}
SUCCESS_SCHEMA_REFS = {
    "AgentCapabilities": "./agent_capabilities.schema.json",
    "AgentHealth": "./agent_health.schema.json",
    "AgentPrepareRequest": "./agent_prepare_request.schema.json",
    "AgentPrepared": "./agent_prepared.schema.json",
    "AgentFinalizeRequest": "./agent_finalize_request.schema.json",
    "AgentFinalized": "./agent_finalized.schema.json",
    "AgentCompleteRequest": "./agent_complete_request.schema.json",
    "AgentCompleted": "./agent_completed.schema.json",
    "AgentCancelRequest": "./agent_cancel_request.schema.json",
    "AgentCanceled": "./agent_canceled.schema.json",
    "AgentError": "./agent_error.schema.json",
}


def _load_json(path: Path) -> object:
    return json.loads(path.read_text(encoding="utf-8"))


def test_agent_request_and_health_contracts_validate_examples_and_runtime() -> None:
    for name, model in REQUEST_CONTRACTS.items():
        schema = _load_json(ROOT / "schemas" / f"{name}.schema.json")
        example = _load_json(ROOT / "examples" / f"{name}.example.json")
        assert isinstance(schema, dict)
        Draft202012Validator.check_schema(schema)
        Draft202012Validator(schema).validate(example)
        model.model_validate(example)

    health_schema = _load_json(ROOT / "schemas" / "agent_health.schema.json")
    health_example = _load_json(ROOT / "examples" / "agent_health.example.json")
    assert isinstance(health_schema, dict)
    Draft202012Validator.check_schema(health_schema)
    Draft202012Validator(health_schema).validate(health_example)
    runtime = tbm.LocalAgentMemory.in_memory()
    try:
        Draft202012Validator(health_schema).validate(runtime.health())
    finally:
        runtime.close()


@pytest.mark.parametrize(
    ("schema_name", "payload"),
    (
        ("agent_prepare_request", {"task": " ", "mode": "planning"}),
        (
            "agent_prepare_request",
            {"task": "task", "mode": "planning", "unexpected": True},
        ),
        (
            "agent_prepare_request",
            {
                "task": "task",
                "mode": "planning",
                "query": "query",
                "semantic_scores": {"memory": 0.9},
                "max_candidates": 10,
            },
        ),
        (
            "agent_prepare_request",
            {
                "task": "task",
                "mode": "planning",
                "semantic_scores": {"memory": 0.9},
            },
        ),
        (
            "agent_prepare_request",
            {
                "task": "task",
                "mode": "planning",
                "max_candidates": 10,
            },
        ),
        (
            "agent_prepare_request",
            {
                "task": "task",
                "mode": "planning",
                "minimum_score": 0.5,
            },
        ),
        (
            "agent_finalize_request",
            {
                "request_id": "request",
                "use_memory": False,
                "allowed_memory_ids": ["memory", "memory"],
                "blocked_memory_ids": [],
                "reason": "reason",
                "risk": "none",
                "recommended_injection": "none",
            },
        ),
        (
            "agent_finalize_request",
            {
                "request_id": "request",
                "use_memory": True,
                "allowed_memory_ids": [],
                "blocked_memory_ids": [],
                "reason": "reason",
                "risk": "none",
                "recommended_injection": "none",
            },
        ),
        (
            "agent_finalize_request",
            {
                "request_id": "request",
                "use_memory": False,
                "allowed_memory_ids": ["memory"],
                "blocked_memory_ids": [],
                "reason": "reason",
                "risk": "none",
                "recommended_injection": "short_summary",
            },
        ),
        (
            "agent_complete_request",
            {
                "decision_id": "decision",
                "eval_result": "pass",
                "memory_caused_failure": True,
            },
        ),
        ("agent_cancel_request", {"request_id": ""}),
    ),
)
def test_agent_request_schemas_reject_invalid_contract_shapes(
    schema_name: str,
    payload: dict[str, object],
) -> None:
    schema = _load_json(ROOT / "schemas" / f"{schema_name}.schema.json")
    assert isinstance(schema, dict)
    assert tuple(Draft202012Validator(schema).iter_errors(payload))


def test_openapi_contract_has_exact_routes_refs_security_and_bounds() -> None:
    document = _load_json(ROOT / "schemas" / "agent-http-v1.openapi.json")
    assert isinstance(document, dict)
    assert document["openapi"] == "3.1.0"
    assert document["jsonSchemaDialect"] == (
        "https://json-schema.org/draft/2020-12/schema"
    )
    assert document["security"] == [{"bearerAuth": []}]

    paths = document["paths"]
    assert isinstance(paths, dict)
    assert {
        path: set(item)
        for path, item in paths.items()
    } == {
        "/v1/capabilities": {"get"},
        "/v1/health": {"get"},
        "/v1/prepare": {"post"},
        "/v1/finalize": {"post"},
        "/v1/complete": {"post"},
        "/v1/cancel": {"post"},
    }
    operation_ids = [
        operation["operationId"]
        for path_item in paths.values()
        for operation in path_item.values()
    ]
    assert len(operation_ids) == len(set(operation_ids)) == 6

    components = document["components"]
    assert isinstance(components, dict)
    assert components["securitySchemes"]["bearerAuth"] == {
        "type": "http",
        "scheme": "bearer",
        "bearerFormat": "opaque local secret",
        "description": (
            "An operator-provisioned 32-512 character secret. It protects "
            "the loopback transport but does not establish a user, tenant, "
            "or workload identity."
        ),
    }
    schemas = components["schemas"]
    assert {
        name: item["$ref"]
        for name, item in schemas.items()
    } == SUCCESS_SCHEMA_REFS
    for item in schemas.values():
        target = ROOT / "schemas" / item["$ref"].removeprefix("./")
        assert target.is_file()
        if target.name.endswith(".schema.json"):
            schema = _load_json(target)
            assert isinstance(schema, dict)
            Draft202012Validator.check_schema(schema)

    transport = document["x-tbm-transport"]
    assert transport == {
        "scope": "single-user, single-host, IPv4 loopback only",
        "requestBodyMaxBytes": HTTP_REQUEST_MAX_BYTES,
        "requestJsonMaxNodes": HTTP_REQUEST_MAX_NODES,
        "requestJsonMaxDepth": HTTP_REQUEST_MAX_DEPTH,
        "responseBodyMaxBytes": sdk_module.SDK_RESPONSE_MAX_BYTES,
        "responseJsonMaxNodes": sdk_module.SDK_RESPONSE_MAX_NODES,
        "responseJsonMaxDepth": sdk_module.SDK_RESPONSE_MAX_DEPTH,
        "pendingRequestsAreProcessLocal": True,
        "redirects": False,
        "proxies": False,
    }


def test_agent_protocol_resources_are_exact_packaged_bytes() -> None:
    names = {
        "schemas/agent-http-v1.openapi.json",
        "schemas/agent_prepare_request.schema.json",
        "schemas/agent_finalize_request.schema.json",
        "schemas/agent_complete_request.schema.json",
        "schemas/agent_cancel_request.schema.json",
        "schemas/agent_health.schema.json",
        "examples/agent_prepare_request.example.json",
        "examples/agent_finalize_request.example.json",
        "examples/agent_complete_request.example.json",
        "examples/agent_cancel_request.example.json",
        "examples/agent_health.example.json",
    }
    descriptions = {
        item.name: item for item in tbm.packaged_resources()
    }
    assert names.issubset(descriptions)
    assert descriptions["schemas/agent-http-v1.openapi.json"].media_type == (
        "application/vnd.oai.openapi+json;version=3.1"
    )
    for name in names:
        assert tbm.read_packaged_resource(name) == (ROOT / name).read_bytes()


@contextmanager
def _running_http(
    runtime: tbm.LocalAgentMemory,
) -> Iterator[tbm.AgentHTTPClient]:
    dispatcher = AgentProtocolDispatcher(
        AgentProtocolConfiguration(ROOT),
        runtime,
    )
    server = AgentHTTPServer(
        AgentHTTPServerConfiguration(port=0, token=TOKEN),
        dispatcher,
    )
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


def _normalize(value: object) -> object:
    if isinstance(value, dict):
        normalized: dict[str, object] = {}
        for key, item in value.items():
            if key == "request_id":
                normalized[key] = (
                    None if item is None else "<request_id>"
                )
            elif key == "decision_id":
                normalized[key] = "<decision_id>"
            else:
                normalized[key] = _normalize(item)
        return normalized
    if isinstance(value, list):
        return [_normalize(item) for item in value]
    return value


def _direct_lifecycle(
    runtime: tbm.LocalAgentMemory,
    prepare_payload: dict[str, object],
) -> dict[str, object]:
    dispatcher = AgentProtocolDispatcher(
        AgentProtocolConfiguration(ROOT),
        runtime,
    )
    prepared = dispatcher.prepare(
        PrepareMemoryRequest.model_validate(prepare_payload)
    )
    request_id = prepared["request_id"]
    assert isinstance(request_id, str)
    finalized = dispatcher.finalize(
        FinalizeMemoryRequest.model_validate(_decline(request_id))
    )
    decision_id = finalized["decision_id"]
    assert isinstance(decision_id, str)
    completed = dispatcher.complete(
        CompleteRunRequest(
            decision_id=decision_id,
            eval_result="pass",
            latency_ms=1,
        )
    )
    cancel_prepared = dispatcher.prepare(
        PrepareMemoryRequest(
            task="cancel conformance request",
            mode="planning",
            trace_id="trace_agent_conformance_cancel",
            run_id="run_agent_conformance_cancel",
        )
    )
    cancel_request_id = cancel_prepared["request_id"]
    assert isinstance(cancel_request_id, str)
    canceled = dispatcher.cancel(
        CancelRunRequest(request_id=cancel_request_id)
    )
    return {
        "capabilities": dispatcher.capabilities(),
        "prepared": prepared,
        "finalized": finalized,
        "completed": completed,
        "canceled": canceled,
        "health": dispatcher.health(),
    }


def _http_lifecycle(
    runtime: tbm.LocalAgentMemory,
    prepare_payload: dict[str, object],
) -> dict[str, object]:
    with _running_http(runtime) as client:
        capabilities = client.capabilities().to_dict()
        prepared = client.prepare(prepare_payload)
        finalized = client.finalize(_decline(prepared.request_id))
        completed = client.complete(
            {
                "decision_id": finalized.decision_id,
                "eval_result": "pass",
                "latency_ms": 1,
            }
        )
        cancel_prepared = client.prepare(
            {
                "task": "cancel conformance request",
                "mode": "planning",
                "trace_id": "trace_agent_conformance_cancel",
                "run_id": "run_agent_conformance_cancel",
            }
        )
        canceled = client.cancel(
            {"request_id": cancel_prepared.request_id}
        )
        return {
            "capabilities": capabilities,
            "prepared": prepared.to_dict(),
            "finalized": finalized.to_dict(),
            "completed": completed.to_dict(),
            "canceled": canceled.to_dict(),
            "health": client.health(),
        }


async def _mcp_stdio_lifecycle(
    database: Path,
    prepare_payload: dict[str, object],
) -> dict[str, object]:
    from mcp import ClientSession
    from mcp.client.stdio import StdioServerParameters, stdio_client

    environment = dict(os.environ)
    source_path = str(ROOT / "src")
    prior_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = (
        source_path
        if not prior_pythonpath
        else source_path + os.pathsep + prior_pythonpath
    )
    parameters = StdioServerParameters(
        command=sys.executable,
        args=[
            "-m",
            "trace_backed_memory.mcp_entry",
            "--repo-path",
            str(ROOT),
            "--sqlite",
            str(database),
        ],
        cwd=str(ROOT),
        env=environment,
    )

    async with stdio_client(parameters) as (read, write):
        async with ClientSession(read, write) as session:
            await session.initialize()

            async def call(
                name: str,
                arguments: dict[str, object],
            ) -> dict[str, object]:
                result = await session.call_tool(name, arguments)
                assert result.isError is False
                assert isinstance(result.structuredContent, dict)
                return dict(result.structuredContent)

            capabilities = await call("tbm_capabilities", {})
            prepared = await call(
                "tbm_prepare_memory",
                {"request": prepare_payload},
            )
            request_id = prepared["request_id"]
            assert isinstance(request_id, str)
            finalized = await call(
                "tbm_finalize_memory",
                {"request": _decline(request_id)},
            )
            decision_id = finalized["decision_id"]
            assert isinstance(decision_id, str)
            completed = await call(
                "tbm_complete_run",
                {
                    "request": {
                        "decision_id": decision_id,
                        "eval_result": "pass",
                        "latency_ms": 1,
                    }
                },
            )
            cancel_prepared = await call(
                "tbm_prepare_memory",
                {
                    "request": {
                        "task": "cancel conformance request",
                        "mode": "planning",
                        "trace_id": "trace_agent_conformance_cancel",
                        "run_id": "run_agent_conformance_cancel",
                    }
                },
            )
            cancel_id = cancel_prepared["request_id"]
            assert isinstance(cancel_id, str)
            canceled = await call(
                "tbm_cancel_run",
                {"request": {"request_id": cancel_id}},
            )
            health = await call("tbm_health", {})
            return {
                "capabilities": capabilities,
                "prepared": prepared,
                "finalized": finalized,
                "completed": completed,
                "canceled": canceled,
                "health": health,
            }


def test_dispatcher_http_and_mcp_stdio_share_one_lifecycle_contract(
    tmp_path: Path,
) -> None:
    pytest.importorskip("mcp")
    prepare_payload: dict[str, object] = {
        "task": "cross-adapter protocol conformance",
        "mode": "planning",
        "trace_id": "trace_agent_conformance",
        "run_id": "run_agent_conformance",
    }
    direct_runtime = tbm.LocalAgentMemory.in_memory()
    http_runtime = tbm.LocalAgentMemory.in_memory()
    try:
        direct = _direct_lifecycle(direct_runtime, prepare_payload)
        http = _http_lifecycle(http_runtime, prepare_payload)
        mcp = anyio.run(
            _mcp_stdio_lifecycle,
            tmp_path / "agent-conformance.sqlite3",
            prepare_payload,
        )
    finally:
        direct_runtime.close()
        http_runtime.close()

    assert _normalize(http) == _normalize(direct)
    assert _normalize(mcp) == _normalize(direct)
