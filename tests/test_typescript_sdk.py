from __future__ import annotations

import json
from pathlib import Path

import trace_backed_memory as tbm
import trace_backed_memory.sdk as sdk_module


ROOT = Path(__file__).resolve().parents[1]
SDK_ROOT = ROOT / "packages" / "typescript-sdk"


def _json(path: Path) -> dict[str, object]:
    value = json.loads(path.read_text(encoding="utf-8"))
    assert isinstance(value, dict)
    return value


def test_python_async_sdk_is_an_intentional_package_root_export() -> None:
    assert tbm.AsyncAgentHTTPClient is sdk_module.AsyncAgentHTTPClient
    assert "AsyncAgentHTTPClient" in tbm.__all__
    assert "AsyncAgentHTTPClient" in sdk_module.__all__


def test_typescript_sdk_metadata_is_dependency_free_and_locked() -> None:
    package = _json(SDK_ROOT / "package.json")
    lock = _json(SDK_ROOT / "package-lock.json")
    assert package["name"] == "@trace-backed-memory/agent-http"
    assert package["version"] == "0.2.0"
    assert package["type"] == "module"
    assert package["license"] == "MIT"
    assert package["engines"] == {"node": ">=20"}
    assert "dependencies" not in package
    assert package["devDependencies"] == {
        "@types/node": "20.19.43",
        "typescript": "5.9.3",
    }
    assert lock["lockfileVersion"] == 3
    packages = lock["packages"]
    assert isinstance(packages, dict)
    root_package = packages[""]
    assert root_package["devDependencies"] == package["devDependencies"]
    scripts = package["scripts"]
    assert scripts["prepack"] == "npm run build"
    assert scripts["pack:check"] == "node scripts/check-package.mjs"
    assert (SDK_ROOT / "LICENSE").read_bytes() == (ROOT / "LICENSE").read_bytes()


def test_typescript_sdk_uses_direct_transport_and_canonical_contract() -> None:
    client = (SDK_ROOT / "src" / "client.ts").read_text(encoding="utf-8")
    strict_json = (SDK_ROOT / "src" / "strict-json.ts").read_text(
        encoding="utf-8"
    )
    contract_check = (SDK_ROOT / "scripts" / "check-contract.mjs").read_text(
        encoding="utf-8"
    )
    package_check = (SDK_ROOT / "scripts" / "check-package.mjs").read_text(
        encoding="utf-8"
    )
    assert 'from "node:http"' in client
    assert "fetch(" not in client
    assert "agent: false" in client
    assert "JSON object keys must be unique" in strict_json
    assert "schemas/agent-http-v1.openapi.json" in contract_check
    assert "pendingRequestsAreProcessLocal" in contract_check
    assert "dist/index.js" in package_check
    assert '"--dry-run", "--json"' in package_check


def test_typescript_sdk_publishes_the_explicit_durable_profile() -> None:
    durable_client = (
        SDK_ROOT / "src" / "durable-client.ts"
    ).read_text(encoding="utf-8")
    durable_validation = (
        SDK_ROOT / "src" / "durable-validation.ts"
    ).read_text(encoding="utf-8")
    exports = (SDK_ROOT / "src" / "index.ts").read_text(encoding="utf-8")
    fixture = _json(
        ROOT / "tests" / "fixtures" / "durable_client_lifecycle.json"
    )

    assert "DurableAgentHTTPClient" in exports
    assert "DurableAgentHTTPError" in exports
    assert "durableSessionReference" in exports
    assert "DurableDecideResult" in exports
    assert "DurableCompleteResult" in exports
    assert "tbm.durable-agent-wire.v1" in (
        SDK_ROOT / "src" / "durable-types.ts"
    ).read_text(encoding="utf-8")
    assert '"/durable/v1/capabilities"' in durable_client
    assert "heartbeat(" in durable_client
    assert "maxAttempts" in durable_client
    assert "identity fields are never accepted" in durable_validation
    assert "tenant_id" not in json.dumps(fixture, sort_keys=True)


def test_typescript_sdk_ci_and_bilingual_docs_stay_published() -> None:
    ci = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert "typescript-sdk:" in ci
    assert "packages/typescript-sdk/package-lock.json" in ci
    assert "npm ci --ignore-scripts" in ci
    assert "npm run pack:check" in ci
    assert "tools/verify.py --all" in ci
    assert ci.count("resources/manifest.json") >= 2
    assert "len(packaged_resources()) == 149" not in ci

    documents = {
        "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
        "README.zh-CN.md": (ROOT / "README.zh-CN.md").read_text(
            encoding="utf-8"
        ),
        "agent-http-v1.md": (
            ROOT / "docs" / "protocols" / "agent-http-v1.md"
        ).read_text(encoding="utf-8"),
        "agent-http-v1.zh-CN.md": (
            ROOT / "docs" / "protocols" / "agent-http-v1.zh-CN.md"
        ).read_text(encoding="utf-8"),
    }
    for name, document in documents.items():
        assert "TypeScript" in document, name
        assert "Python" in document, name
    assert "AsyncAgentHTTPClient" in documents["agent-http-v1.md"]
    assert "AsyncAgentHTTPClient" in documents["agent-http-v1.zh-CN.md"]
    assert "pi-mcp-adapter" in documents["README.md"]
    assert "pi-mcp-adapter" in documents["README.zh-CN.md"]
