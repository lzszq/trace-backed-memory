import json
import re
import tomllib
from dataclasses import MISSING, fields as dataclass_fields
from pathlib import Path
from typing import get_args, get_type_hints

from trace_backed_memory import (
    FailureCase,
    Lesson,
    MemoryContext,
    MemoryKind,
    MemoryObsolescenceRequest,
    MemoryUsageLog,
    ProjectPolicy,
    Trace,
    TraceBackedMemoryStore,
    parse_memory_context,
    memory_item_from_project_policy,
    parse_memory_decision,
)
from trace_backed_memory._timestamps import RFC3339_PATTERN
from trace_backed_memory.models import EvalResult, FailureCaseStatus, LessonStatus, MemoryType, Mode

ROOT = Path(__file__).resolve().parents[1]
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"
CURRENT_IMPLEMENTED_PHASE = "Phase 0-73"


def _markdown_heading_levels(document: str) -> tuple[int, ...]:
    levels: list[int] = []
    in_fence = False

    for line in document.splitlines():
        if line.startswith("```"):
            in_fence = not in_fence
            continue
        if in_fence:
            continue
        match = re.match(r"^(#{1,6})\s+", line)
        if match is not None:
            levels.append(len(match.group(1)))

    assert not in_fence
    return tuple(levels)


def test_postgres_adapter_dependencies_are_optional():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    extras = project["optional-dependencies"]

    assert project["dependencies"] == []
    assert extras["postgres"] == ["psycopg>=3.2,<4"]
    assert "psycopg[binary]>=3.2,<4" in extras["dev"]


def test_public_product_document_and_mit_metadata_stay_aligned():
    product = _doc("product.md")
    readme = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    pyproject = tomllib.loads(
        (ROOT / "pyproject.toml").read_text(encoding="utf-8")
    )
    project = pyproject["project"]
    license_text = (ROOT / "LICENSE").read_text(encoding="utf-8")
    gitignore = (ROOT / ".gitignore").read_text(encoding="utf-8")

    for contract in [
        "Trace -> Failure Case",
        "System Gate -> LLM Gate",
        "`prepare_memory()`",
        "`complete_memory_run()`",
        "`recover_ready_memory_runs()`",
        "`tbm`",
        "`python -m trace_backed_memory`",
        "`run_memory_execution()`",
        "`MemoryRunMeasurement`",
        "`MemoryObsolescenceRequest`",
        "`obsolete_memories()`",
        CURRENT_IMPLEMENTED_PHASE,
        "PostgreSQL 12+",
        "snapshot version 2",
        "PostgreSQL schema version",
        "Alpha",
        "MIT",
    ]:
        assert contract in product

    assert "[Product Overview and Current Capabilities](product.en.md)" in readme
    assert project["readme"] == "README.md"
    assert project["license"] == "MIT"
    assert project["license-files"] == ["LICENSE"]
    assert project["authors"] == [{"name": "lzszq"}]
    assert project["urls"] == {
        "Homepage": "https://github.com/lzszq/trace-backed-memory",
        "Repository": "https://github.com/lzszq/trace-backed-memory",
        "Issues": "https://github.com/lzszq/trace-backed-memory/issues",
    }
    assert "License :: OSI Approved :: MIT License" not in project["classifiers"]
    assert license_text.startswith("MIT License\n\nCopyright (c) 2026 lzszq")
    assert "Permission is hereby granted, free of charge" in license_text
    assert "repository is private" not in license_text.lower()
    for ignored_secret_pattern in [
        ".env.*",
        "*.pem",
        "*.key",
        "id_rsa",
        "id_ed25519",
        "credentials.json",
    ]:
        assert ignored_secret_pattern in gitignore


def test_current_docs_publish_postgres_v2_and_43_resource_contracts():
    current_english_documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.en.md": _doc("product.en.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    for name, document in current_english_documents.items():
        normalized = " ".join(document.split()).lower()
        assert (
            "postgresql schema version 2" in normalized
        ), f"{name} must publish the current PostgreSQL schema version"

    for name in ("README.md", "docs/architecture.md", "docs/usage-policy.md"):
        normalized = " ".join(
            current_english_documents[name].split()
        ).lower()
        assert "postgresql schema version 1" not in normalized

    bilingual_documents = {
        **{
            name: document
            for name, document in current_english_documents.items()
            if name != "docs/product-program.md"
        },
        "README.zh-CN.md": (ROOT / "docs" / "reference.zh-CN.md").read_text(
            encoding="utf-8"
        ),
        "docs/architecture.zh-CN.md": _doc("architecture.zh-CN.md"),
        "docs/usage-policy.zh-CN.md": _doc("usage-policy.zh-CN.md"),
    }
    for name, document in bilingual_documents.items():
        assert (
            "schemas/postgres-v2-lock-order-hotfix.sql" in document
        ), f"{name} must publish the version-2 hotfix resource"

    assert "contains 86 resources" in current_english_documents["README.md"]
    assert (
        "contains 86 resources"
        in current_english_documents["docs/architecture.md"]
    )
    assert (
        "86 installed resource copies"
        in current_english_documents["docs/usage-policy.md"]
    )
    assert (
        "Distribution resources | 86"
        in current_english_documents["docs/product.en.md"]
    )


def test_readme_language_versions_stay_linked_and_structurally_aligned():
    english_path = ROOT / "README.md"
    chinese_path = ROOT / "README.zh-CN.md"
    english = english_path.read_text(encoding="utf-8")
    chinese = chinese_path.read_text(encoding="utf-8")

    assert english.startswith(
        "# Trace-backed Memory\n\n"
        "**English** | [简体中文](README.zh-CN.md)\n"
    )
    assert chinese.startswith(
        "# Trace-backed Memory\n\n"
        "[English](README.md) | **简体中文**\n"
    )
    assert re.search(r"[\u4e00-\u9fff]", chinese) is not None
    assert _markdown_heading_levels(chinese) == _markdown_heading_levels(english)

    english_targets = [
        "docs/product.en.md",
        "docs/architecture.md",
        "docs/usage-policy.md",
        "docs/product-program.md",
        "docs/index.md",
        "docs/protocols/agent-v1.md",
        "docs/integrations/codex.md",
    ]
    chinese_targets = [
        "docs/product.md",
        "docs/architecture.zh-CN.md",
        "docs/usage-policy.zh-CN.md",
        "docs/product-program.zh-CN.md",
        "docs/index.zh-CN.md",
        "docs/protocols/agent-v1.zh-CN.md",
        "docs/integrations/codex.zh-CN.md",
    ]
    for document, targets in [
        (english, english_targets),
        (chinese, chinese_targets),
    ]:
        for target in targets:
            assert f"]({target})" in document
            assert (ROOT / target).is_file()


def test_readme_and_reference_local_links_resolve():
    documents = (
        ROOT / "README.md",
        ROOT / "README.zh-CN.md",
        ROOT / "docs" / "reference.md",
        ROOT / "docs" / "reference.zh-CN.md",
    )
    for document in documents:
        source = document.read_text(encoding="utf-8")
        for target in re.findall(r"\[[^\]]+\]\(([^)]+)\)", source):
            if (
                target.startswith(("https://", "http://", "mailto:", "#"))
                or " " in target
            ):
                continue
            path = target.split("#", 1)[0]
            assert (document.parent / path).resolve().exists(), (
                f"{document.relative_to(ROOT)} has a broken link: {target}"
            )

    english = (ROOT / "README.md").read_text(encoding="utf-8")
    chinese = (ROOT / "README.zh-CN.md").read_text(encoding="utf-8")
    codex = (ROOT / "docs" / "integrations" / "codex.md").read_text(
        encoding="utf-8"
    )
    codex_chinese = (
        ROOT / "docs" / "integrations" / "codex.zh-CN.md"
    ).read_text(encoding="utf-8")
    assert "](docs/reference.md)" in english
    assert "](docs/reference.zh-CN.md)" in chinese
    for document in (english, chinese):
        assert 'py -m pip install -e ".[mcp]"' in document
        assert "python3 -m pip install -e '.[mcp]'" in document
        assert "Windows PowerShell" in document
        assert "macOS" in document
        assert "Codex Desktop" in document
        assert "Codex CLI" in document
        assert "Claude Code" in document
        assert "Pi" in document
    for document in (codex, codex_chinese):
        assert "[mcp_servers.trace_backed_memory]" in document
        assert "enabled = true" in document
        assert 'command = "tbm-mcp"' in document
        assert '"--repo-path", "/absolute/path/to/repository"' in document
        assert "tbm_prepare_memory" in document
        assert "tbm_finalize_memory" in document


def test_product_and_reference_documents_are_localized_in_pairs():
    pairs = [
        ("docs/product.en.md", "docs/product.md"),
        ("docs/architecture.md", "docs/architecture.zh-CN.md"),
        ("docs/usage-policy.md", "docs/usage-policy.zh-CN.md"),
        ("docs/product-program.md", "docs/product-program.zh-CN.md"),
        ("docs/index.md", "docs/index.zh-CN.md"),
        ("docs/development.md", "docs/development.zh-CN.md"),
        (
            "docs/protocols/agent-v1.md",
            "docs/protocols/agent-v1.zh-CN.md",
        ),
        (
            "docs/protocols/authenticated-service-v3.md",
            "docs/protocols/authenticated-service-v3.zh-CN.md",
        ),
        (
            "docs/protocols/authenticated-gate-service-v3.md",
            "docs/protocols/authenticated-gate-service-v3.zh-CN.md",
        ),
        (
            "docs/protocols/gate-recovery-worker-v3.md",
            "docs/protocols/gate-recovery-worker-v3.zh-CN.md",
        ),
            (
                "docs/protocols/authorization-v3.md",
                "docs/protocols/authorization-v3.zh-CN.md",
            ),
            (
                "docs/protocols/audit-recovery-v3.md",
                "docs/protocols/audit-recovery-v3.zh-CN.md",
            ),
        (
            "docs/protocols/evidence-v3.md",
            "docs/protocols/evidence-v3.zh-CN.md",
        ),
        (
            "docs/protocols/gate-session-v3.md",
            "docs/protocols/gate-session-v3.zh-CN.md",
        ),
            (
                "docs/protocols/gate-evaluation-v3.md",
                "docs/protocols/gate-evaluation-v3.zh-CN.md",
            ),
            (
                "docs/protocols/outcome-v3.md",
                "docs/protocols/outcome-v3.zh-CN.md",
            ),
        (
            "docs/protocols/memory-revision-v3.md",
            "docs/protocols/memory-revision-v3.zh-CN.md",
        ),
        (
            "docs/protocols/retrieval-snapshot-v3.md",
            "docs/protocols/retrieval-snapshot-v3.zh-CN.md",
        ),
        (
            "docs/protocols/replay-v3.md",
            "docs/protocols/replay-v3.zh-CN.md",
        ),
        (
            "docs/migrations/snapshot-v3-preflight.md",
            "docs/migrations/snapshot-v3-preflight.zh-CN.md",
        ),
        (
            "docs/migrations/v3-staging-bundles.md",
            "docs/migrations/v3-staging-bundles.zh-CN.md",
        ),
        (
            "docs/integrations/codex.md",
            "docs/integrations/codex.zh-CN.md",
        ),
    ]

    for english_name, chinese_name in pairs:
        english_path = ROOT / english_name
        chinese_path = ROOT / chinese_name
        english = english_path.read_text(encoding="utf-8")
        chinese = chinese_path.read_text(encoding="utf-8")

        assert (
            f"**English** | [简体中文]({chinese_path.name})"
            in english.splitlines()[:4]
        )
        assert (
            f"[English]({english_path.name}) | **简体中文**"
            in chinese.splitlines()[:4]
        )
        assert re.search(r"[\u4e00-\u9fff]", chinese) is not None
        assert _markdown_heading_levels(chinese) == _markdown_heading_levels(english)


def test_public_batch_obsolescence_types_are_exact_and_canonical():
    assert get_args(MemoryKind) == (
        "failure_case",
        "lesson",
        "project_policy",
    )
    request = MemoryObsolescenceRequest("failure_case", "case_001")
    assert request.memory_kind == "failure_case"
    assert request.memory_id == "case_001"


def test_postgres_schema_publishes_adapter_version():
    schema = _postgres_schema()
    assert "CREATE TABLE public.trace_backed_memory_schema" in schema
    assert "schema_version INTEGER NOT NULL CHECK (schema_version > 0)" in schema
    assert (
        "INSERT INTO public.trace_backed_memory_schema(singleton, schema_version)"
        in schema
    )
    assert "VALUES (true, 2)" in schema
    assert "ON public.trace_backed_memory_schema FROM PUBLIC;" in schema


def test_postgres_jsonpath_schema_publishes_supported_version_floor():
    schema = _postgres_schema()
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/product-program.md": _doc("product-program.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "design": _doc(
            "superpowers/specs/2026-07-12-postgres-runtime-adapter-design.md"
        ),
        "plan": _doc("superpowers/plans/2026-07-12-postgres-runtime-adapter.md"),
    }

    assert "jsonb_path_exists(" in schema
    for name, document in documents.items():
        assert "PostgreSQL 12+" in document, name
        assert "PostgreSQL 10+" not in document, name


def test_docs_publish_postgres_repository_operational_boundaries():
    readme = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    architecture = _doc("architecture.md")
    architecture_contract = " ".join(architecture.split())
    roadmap = _doc("product-program.md")
    usage_policy = _doc("usage-policy.md")

    for required_text in [
        "pip install 'trace-backed-memory[postgres]'",
        "PostgresMemoryRepository.connect",
        "repository.sync(store)",
        "repository.load()",
        "schema_version",
        "additive",
    ]:
        assert required_text in readme

    for required_text in [
        "schema version 1",
        "transaction",
        "FOR UPDATE",
        "FOR SHARE",
        "canonical",
        "borrowed",
        "owned",
        "migration",
        "pooling",
        "async",
    ]:
        assert required_text in architecture

    non_goals = architecture.split("## Non-goals", maxsplit=1)[1].lower()
    assert "postgresql runtime adapter" not in non_goals
    assert "migration" in non_goals
    assert "pooling" in non_goals
    assert "async" in non_goals
    assert "implemented" in roadmap.lower()
    assert "synchronous" in roadmap.lower()
    assert "postgres" in usage_policy.lower()

    assert (
        "Trace identity, provenance, input hash, retrieved context, tool calls, "
        "and creation time are immutable"
        in architecture_contract
    )
    assert (
        "A stored `unknown` Trace may complete once"
        in architecture_contract
    )
    assert (
        "Usage logs are immutable except for their separate forward outcome "
        "transition"
        in architecture_contract
    )
    assert (
        "diagnosis (`failure_type`, `symptom`, and `root_cause`)"
        in architecture_contract
    )
    assert (
        "review (`reviewed_by`, `review_notes`, and `reviewed_at`)"
        in architecture_contract
    )
    assert (
        "fix and regression (`fix`, `fix_commit_sha`, and `regression_passed`), "
        "and `status`"
        in architecture_contract
    )
    assert (
        "lessons and project policies may update only `status`"
        in architecture_contract
    )
    assert (
        "triggers still enforce forward-only status transitions"
        in architecture_contract
    )
    assert (
        "Failure cases may move from `draft` to `verified` or `obsolete`, "
        "and from `verified` to `obsolete`."
        in architecture_contract
    )


def test_docs_publish_pr_change_set_ephemeral_persistence_contract():
    readme = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    architecture = _doc("architecture.md")
    usage_policy = _doc("usage-policy.md")
    readme_contract = " ".join(readme.split())
    architecture_contract = " ".join(architecture.split())
    usage_policy_contract = " ".join(usage_policy.split())

    assert (
        "`PRChangeSet` values and endpoint provenance are ephemeral "
        "report-only values: they are not persisted"
        in readme_contract
    )
    assert (
        "Change sets and endpoint tags are ephemeral report-boundary values. "
        "They are not serialized or stored"
        in architecture_contract
    )
    assert (
        "Change sets and endpoint tags are ephemeral report inputs and outputs, "
        "not persisted records or schema extensions."
        in usage_policy_contract
    )
    assert "snapshot version remains 2" in architecture
    assert "PostgreSQL schema version remains 2" in architecture


def test_docs_publish_exact_postgres_transaction_ownership_contract():
    documents = [
        (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        _doc("architecture.md"),
        _doc("usage-policy.md"),
        _doc("superpowers/specs/2026-07-12-postgres-runtime-adapter-design.md"),
        _doc("superpowers/plans/2026-07-12-postgres-runtime-adapter.md"),
    ]
    required_contract = [
        "active caller transaction",
        "nested savepoint",
        "does not commit or roll back the outer transaction",
        "caller owns the final commit or rollback",
        "Without an outer transaction",
        "repository transaction commits normally",
    ]

    for document in documents:
        normalized = " ".join(document.split())
        for required_text in required_contract:
            assert required_text in normalized


def _json_example(name: str) -> dict[str, object]:
    return json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))


def _json_schema(name: str) -> dict[str, object]:
    schema_path = ROOT / "schemas" / name
    assert schema_path.exists(), f"{name} should exist"
    return json.loads(schema_path.read_text(encoding="utf-8"))


def _json_schema_accepts(schema: dict[str, object], instance: object) -> bool:
    schema_type = schema.get("type")
    if schema_type == "object" and not isinstance(instance, dict):
        return False
    if schema_type == "string" and not isinstance(instance, str):
        return False

    enum = schema.get("enum")
    if isinstance(enum, list) and instance not in enum:
        return False

    if isinstance(instance, str):
        min_length = schema.get("minLength")
        max_length = schema.get("maxLength")
        if isinstance(min_length, int) and len(instance) < min_length:
            return False
        if isinstance(max_length, int) and len(instance) > max_length:
            return False

    if isinstance(instance, dict):
        required = schema.get("required", [])
        if isinstance(required, list) and not set(required).issubset(instance):
            return False
        properties = schema.get("properties", {})
        if isinstance(properties, dict):
            for field_name, field_schema in properties.items():
                if field_name in instance and isinstance(field_schema, dict):
                    if not _json_schema_accepts(field_schema, instance[field_name]):
                        return False

    all_of = schema.get("allOf", [])
    if isinstance(all_of, list):
        for subschema in all_of:
            if isinstance(subschema, dict) and not _json_schema_accepts(
                subschema, instance
            ):
                return False

    condition = schema.get("if")
    if isinstance(condition, dict) and _json_schema_accepts(condition, instance):
        consequence = schema.get("then")
        if isinstance(consequence, dict) and not _json_schema_accepts(
            consequence, instance
        ):
            return False

    return True


def _postgres_schema() -> str:
    return (ROOT / "schemas" / "postgres.sql").read_text(encoding="utf-8")


def _doc(name: str) -> str:
    return (ROOT / "docs" / name).read_text(encoding="utf-8")


def _table_definition(schema: str, table_name: str) -> str:
    match = re.search(
        rf"CREATE TABLE {re.escape(table_name)} \((.*?)\n\);",
        schema,
        re.DOTALL,
    )
    assert match is not None, f"{table_name} table must exist"
    return match.group(1)


def _column_definition(table_sql: str, column_name: str) -> str:
    match = re.search(
        rf"^\s*{re.escape(column_name)}\s+.*?(?=,\n\s*\w|\n\s*\))",
        table_sql,
        re.DOTALL | re.MULTILINE,
    )
    assert match is not None, f"{column_name} column must exist"
    return match.group(0)


def _required_dataclass_fields(model_cls: type[object]) -> set[str]:
    return {
        field.name
        for field in dataclass_fields(model_cls)
        if field.default is MISSING and field.default_factory is MISSING
    }


def _schema_properties(schema: dict[str, object]) -> dict[str, object]:
    properties = schema.get("properties")
    assert isinstance(properties, dict), "schema should declare object properties"
    return properties


def _schema_enum_values(schema_fragment: object) -> set[object]:
    if not isinstance(schema_fragment, dict):
        return set()

    values = set()
    enum = schema_fragment.get("enum")
    if isinstance(enum, list):
        values.update(value for value in enum if value is not None)

    for combiner in ["anyOf", "oneOf", "allOf"]:
        nested = schema_fragment.get(combiner)
        if isinstance(nested, list):
            for item in nested:
                values.update(_schema_enum_values(item))

    return values


def test_stored_record_json_schemas_exist_are_draft_2020_12_and_cover_dataclass_contracts():
    usage_log_hints = get_type_hints(MemoryUsageLog)
    expectations = {
        "trace.schema.json": (
            Trace,
            {
                "eval_result": set(get_args(EvalResult)),
            },
        ),
        "failure_case.schema.json": (
            FailureCase,
            {
                "status": set(get_args(FailureCaseStatus)),
            },
        ),
        "lesson.schema.json": (
            Lesson,
            {
                "memory_type": set(get_args(MemoryType)),
                "status": set(get_args(LessonStatus)),
            },
        ),
        "project_policy.schema.json": (
            ProjectPolicy,
            {
                "status": set(get_args(LessonStatus)),
            },
        ),
        "memory_usage_log.schema.json": (
            MemoryUsageLog,
            {
                "mode": set(get_args(Mode)),
                "risk": set(get_args(usage_log_hints["risk"])),
                "recommended_injection": set(get_args(usage_log_hints["recommended_injection"])),
                "eval_result": set(get_args(EvalResult)),
            },
        ),
    }

    for schema_name, (model_cls, enum_expectations) in expectations.items():
        schema = _json_schema(schema_name)
        properties = _schema_properties(schema)

        assert schema.get("$schema") == DRAFT_2020_12
        assert schema.get("type") == "object"
        expected_required = _required_dataclass_fields(model_cls)
        if model_cls is MemoryUsageLog:
            expected_required |= {
                "trace_id",
                "context",
                "candidate_memory_statuses",
                "system_blocked_reasons",
            }
        assert set(schema.get("required", [])) == expected_required
        assert {field.name for field in dataclass_fields(model_cls)} <= set(properties)

        for field_name, expected_values in enum_expectations.items():
            assert _schema_enum_values(properties[field_name]) == expected_values


def test_persisted_timestamp_schemas_publish_shared_microsecond_boundary():
    timestamp_fields = {
        "trace.schema.json": ("created_at",),
        "failure_case.schema.json": ("created_at", "reviewed_at"),
        "lesson.schema.json": ("created_at",),
        "project_policy.schema.json": ("created_at",),
        "memory_usage_log.schema.json": ("created_at",),
    }

    for schema_name, field_names in timestamp_fields.items():
        properties = _schema_properties(_json_schema(schema_name))
        for field_name in field_names:
            assert properties[field_name]["format"] == "date-time"
            assert properties[field_name]["pattern"] == f"^{RFC3339_PATTERN}$"


def test_memory_store_snapshot_schema_requires_versioned_record_collections():
    schema = _json_schema("memory_store_snapshot.schema.json")
    properties = _schema_properties(schema)
    expected_arrays = {
        "traces": "trace.schema.json",
        "failure_cases": "failure_case.schema.json",
        "lessons": "lesson.schema.json",
        "project_policies": "project_policy.schema.json",
        "usage_logs": "memory_usage_log.schema.json",
    }

    assert schema.get("$schema") == DRAFT_2020_12
    assert schema.get("type") == "object"
    assert set(schema.get("required", [])) == {"snapshot_version", *expected_arrays}
    assert properties["snapshot_version"] == {"type": "integer", "const": 2}
    assert schema.get("additionalProperties") is False

    for field_name, schema_ref in expected_arrays.items():
        collection_schema = properties[field_name]
        assert isinstance(collection_schema, dict)
        assert collection_schema.get("type") == "array"
        assert collection_schema.get("items") == {"$ref": schema_ref}


def test_memory_store_snapshot_schema_matches_emitted_v2_envelope():
    schema = _json_schema("memory_store_snapshot.schema.json")
    snapshot = TraceBackedMemoryStore().to_snapshot()

    assert set(schema.get("required", [])) == set(snapshot)
    assert schema["properties"]["snapshot_version"] == {"type": "integer", "const": 2}
    assert snapshot["snapshot_version"] == 2
    assert schema.get("additionalProperties") is False


def test_memory_decision_schema_requires_non_empty_unique_memory_ids():
    schema = _json_schema("memory_decision.schema.json")
    properties = _schema_properties(schema)

    for field_name in ["allowed_memory_ids", "blocked_memory_ids"]:
        memory_ids = properties[field_name]
        assert isinstance(memory_ids, dict)
        assert memory_ids.get("type") == "array"
        assert memory_ids.get("maxItems") == 50
        assert memory_ids.get("uniqueItems") is True

        items = memory_ids.get("items")
        assert isinstance(items, dict)
        assert items.get("type") == "string"
        assert items.get("minLength") == 1


def test_schemas_and_docs_publish_aggregate_and_field_budgets():
    context_properties = _schema_properties(_json_schema("memory_context.schema.json"))
    for field_name, field_schema in context_properties.items():
        if field_name == "mode":
            continue
        assert field_schema["maxLength"] == 512

    trace_properties = _schema_properties(_json_schema("trace.schema.json"))
    assert trace_properties["trace_id"]["maxLength"] == 128
    failure_properties = _schema_properties(_json_schema("failure_case.schema.json"))
    assert failure_properties["case_id"]["maxLength"] == 128
    assert failure_properties["source_trace_id"]["maxLength"] == 128
    lesson_schema = _json_schema("lesson.schema.json")
    lesson_properties = _schema_properties(lesson_schema)
    assert lesson_properties["lesson_id"]["maxLength"] == 128
    assert lesson_properties["source_case_id"]["maxLength"] == 128
    assert lesson_schema["$defs"]["scope"]["additionalProperties"]["maxLength"] == 512
    policy_schema = _json_schema("project_policy.schema.json")
    assert _schema_properties(policy_schema)["policy_id"]["maxLength"] == 128
    assert policy_schema["$defs"]["scope"]["additionalProperties"]["maxLength"] == 512
    usage_schema = _json_schema("memory_usage_log.schema.json")
    assert usage_schema["$defs"]["memory_id_list"]["items"]["maxLength"] == 128
    decision_schema = _json_schema("memory_decision.schema.json")
    assert _schema_properties(decision_schema)["allowed_memory_ids"]["items"]["maxLength"] == 128

    expected_limits = {
        "MEMORY_ID_MAX_CHARS": "128",
        "METADATA_VALUE_MAX_CHARS": "512",
        "LLM_GATE_MAX_CANDIDATES": "50",
        "COMMIT_ANCESTRY_MAX_ANCHORS": "1,000",
        "LLM_GATE_PROMPT_MAX_CHARS": "32,000",
        "LLM_GATE_RESPONSE_MAX_BYTES": "65,536",
        "LLM_GATE_RESPONSE_MAX_NODES": "1,000",
        "LLM_GATE_RESPONSE_MAX_DEPTH": "20",
        "MEMORY_DECISION_REASON_MAX_CHARS": "2,000",
        "INJECTION_MAX_MEMORIES": "20",
        "INJECTION_SNIPPET_MAX_CHARS": "12,000",
    }
    for doc_name in ["usage-policy.md", "architecture.md"]:
        document = _doc(doc_name)
        for constant_name, value in expected_limits.items():
            assert constant_name in document
            assert value in document


def test_stored_schemas_match_postgres_nonblank_string_contracts():
    trace_properties = _schema_properties(_json_schema("trace.schema.json"))
    for field_name in ["trace_id", "run_id", "commit_sha"]:
        assert trace_properties[field_name]["pattern"] == r"\S"
    for field_name in ["repo", "tenant", "error", "trace_uri"]:
        assert "pattern" not in trace_properties[field_name]

    failure_schema = _json_schema("failure_case.schema.json")
    failure_properties = _schema_properties(failure_schema)
    for field_name in [
        "case_id",
        "source_trace_id",
        "commit_sha",
        "failure_type",
        "symptom",
        "fix",
        "fix_commit_sha",
    ]:
        assert failure_properties[field_name]["pattern"] == r"\S"
    for field_name in ["root_cause", "reviewed_by", "review_notes"]:
        assert failure_properties[field_name]["pattern"] == r"\S"
    verified_properties = failure_schema["allOf"][0]["then"]["properties"]
    assert verified_properties["fix"]["pattern"] == r"\S"
    assert verified_properties["fix_commit_sha"]["pattern"] == r"\S"

    lesson_schema = _json_schema("lesson.schema.json")
    lesson_properties = _schema_properties(lesson_schema)
    for field_name in ["lesson_id", "source_case_id", "lesson_text"]:
        assert lesson_properties[field_name]["pattern"] == r"\S"
    assert lesson_schema["$defs"]["scope"]["additionalProperties"]["pattern"] == r"\S"

    policy_schema = _json_schema("project_policy.schema.json")
    policy_properties = _schema_properties(policy_schema)
    for field_name in ["policy_id", "policy_text"]:
        assert policy_properties[field_name]["pattern"] == r"\S"
    assert policy_schema["$defs"]["scope"]["additionalProperties"]["pattern"] == r"\S"

    context_properties = _schema_properties(
        _json_schema("memory_context.schema.json")
    )
    for field_name, field_schema in context_properties.items():
        if field_name != "mode":
            assert field_schema["pattern"] == r"\S"

    usage_schema = _json_schema("memory_usage_log.schema.json")
    usage_properties = _schema_properties(usage_schema)
    for field_name in ["decision_id", "run_id", "reason", "trace_id"]:
        assert usage_properties[field_name]["pattern"] == r"\S"
    assert usage_properties["context"]["propertyNames"] == {"pattern": r"\S"}
    assert usage_properties["context"]["additionalProperties"]["pattern"] == r"\S"
    assert usage_properties["candidate_memory_statuses"]["propertyNames"] == {
        "pattern": r"\S",
        "maxLength": 128,
    }
    assert usage_properties["system_blocked_reasons"]["propertyNames"] == {
        "pattern": r"\S",
        "maxLength": 128,
    }
    assert usage_properties["system_blocked_reasons"]["additionalProperties"][
        "pattern"
    ] == r"\S"
    assert "pattern" not in usage_schema["$defs"]["memory_id_list"]["items"]

    changed_schema_names = [
        "trace.schema.json",
        "failure_case.schema.json",
        "lesson.schema.json",
        "project_policy.schema.json",
        "memory_context.schema.json",
        "memory_usage_log.schema.json",
    ]
    for schema_name in changed_schema_names:
        assert (
            ROOT / "src" / "trace_backed_memory" / "_resources" / "schemas" / schema_name
        ).read_bytes() == (ROOT / "schemas" / schema_name).read_bytes()


def test_memory_decision_schema_encodes_use_memory_consistency_rules():
    schema = _json_schema("memory_decision.schema.json")
    all_of = schema.get("allOf")

    assert isinstance(all_of, list)
    assert any("use_memory" in json.dumps(rule) and "allowed_memory_ids" in json.dumps(rule) for rule in all_of)
    assert any("use_memory" in json.dumps(rule) and "recommended_injection" in json.dumps(rule) for rule in all_of)


def test_decision_and_usage_schemas_require_nonblank_reasons():
    decision_reason = _schema_properties(
        _json_schema("memory_decision.schema.json")
    )["reason"]
    usage_reason = _schema_properties(
        _json_schema("memory_usage_log.schema.json")
    )["reason"]

    assert decision_reason["pattern"] == r"\S"
    assert usage_reason["pattern"] == r"\S"
    assert decision_reason["maxLength"] == 2_000
    assert usage_reason["maxLength"] == 2_000
    assert "CHECK (reason ~ '[^[:space:]]')" in _postgres_schema()
    assert "CHECK (char_length(reason) <= 2000)" in _postgres_schema()


def test_verified_failure_case_schema_requires_review_evidence():
    schema = _json_schema("failure_case.schema.json")
    verified = schema["allOf"][0]["then"]

    assert {"root_cause", "reviewed_by", "reviewed_at"} <= set(
        verified["required"]
    )
    for field_name in ("root_cause", "reviewed_by", "reviewed_at"):
        assert verified["properties"][field_name]["type"] == "string"


def test_memory_usage_log_schema_encodes_decision_consistency_rules():
    schema = _json_schema("memory_usage_log.schema.json")
    all_of = schema.get("allOf")

    assert isinstance(all_of, list)
    assert any("recommended_injection" in json.dumps(rule) and "used_memory_ids" in json.dumps(rule) for rule in all_of)
    assert any("memory_caused_failure" in json.dumps(rule) and "eval_result" in json.dumps(rule) for rule in all_of)


def test_usage_log_schema_requires_safe_workflow_audit_fields():
    schema = _json_schema("memory_usage_log.schema.json")
    required = set(schema["required"])
    assert {"trace_id", "context", "candidate_memory_statuses", "system_blocked_reasons"} <= required
    caused_failure_then = schema["allOf"][2]["then"]
    assert "eval_result" in caused_failure_then["required"]


def test_postgres_enforces_case_trace_commit_and_wrong_memory_evidence():
    sql = _postgres_schema()
    assert "UNIQUE (trace_id, commit_sha)" in sql
    assert "FOREIGN KEY (source_trace_id, commit_sha)" in sql
    assert "eval_result IS NOT NULL" in sql
    assert "candidate_memory_statuses JSONB NOT NULL" in sql


def test_json_examples_match_current_models_and_parsers():
    trace = Trace(**_json_example("trace.example.json"))
    case = FailureCase(**_json_example("failure_case.example.json"))
    lesson = Lesson(**_json_example("lesson.example.json"))
    project_policy = ProjectPolicy(**_json_example("project_policy.example.json"))
    decision = parse_memory_decision(_json_example("memory_decision.example.json"))
    usage_log = MemoryUsageLog(**_json_example("memory_usage_log.example.json"))

    store = TraceBackedMemoryStore()
    store.record_trace(trace)
    store.add_failure_case(case)
    store.add_lesson(lesson)
    restored_usage_log = MemoryUsageLog(
        **{**usage_log.__dict__, "run_id": trace.run_id}
    )
    restored = TraceBackedMemoryStore.from_snapshot(
        {
            "snapshot_version": 2,
            "traces": [store.to_snapshot()["traces"][0]],
            "failure_cases": [store.to_snapshot()["failure_cases"][0]],
            "lessons": [store.to_snapshot()["lessons"][0]],
            "project_policies": [],
            "usage_logs": [restored_usage_log.__dict__],
        }
    )

    context = MemoryContext(
        mode="repair",
        repo="agent-harness",
        tenant="tenant_a",
        commit_sha="abc123",
        prompt_family="planner",
        tool="search_docs",
        tool_schema_version="search_docs_v2",
    )
    candidates = store.candidate_memories(context)

    assert trace.repo == "agent-harness"
    assert trace.eval_suite == "tool_calling_regression"
    assert trace.input_hash == (
        "sha256:79e820f10f2b4f322f84307a68a09f62"
        "f8342d5c824b86bd1b7f3f6fbebf01f9"
    )
    assert case.source_trace_id == trace.trace_id
    assert lesson.source_case_id == case.case_id
    assert memory_item_from_project_policy(project_policy).source_policy_id == "project_policy_001"
    assert decision.allowed_memory_ids == ["lesson_001"]
    assert restored.usage_logs == [restored_usage_log]
    assert [memory.memory_id for memory in candidates] == ["lesson_001"]


def test_postgres_schema_mentions_current_model_fields():
    schema = (ROOT / "schemas" / "postgres.sql").read_text(encoding="utf-8")

    expected_columns = [
        "repo TEXT",
        "prompt_family TEXT",
        "retrieved_context JSONB",
        "tool_calls JSONB",
        "tool_outputs JSONB",
        "recommended_injection TEXT",
        "source_trace_id TEXT NOT NULL",
        "source_case_id TEXT NOT NULL REFERENCES failure_cases(case_id)",
        "CREATE TABLE project_policies",
        "policy_id TEXT PRIMARY KEY",
    ]

    for column in expected_columns:
        assert column in schema


def test_lesson_confidence_sql_default_matches_dataclass_default():
    lessons = _table_definition(_postgres_schema(), "lessons")
    confidence = _column_definition(lessons, "confidence")
    default = Lesson.__dataclass_fields__["confidence"].default

    assert f"DEFAULT {default}" in confidence


def test_postgres_status_defaults_match_dataclass_defaults():
    schema = _postgres_schema()

    expectations = {
        "failure_cases": ("status", FailureCase.__dataclass_fields__["status"].default),
        "lessons": ("status", Lesson.__dataclass_fields__["status"].default),
        "project_policies": ("status", ProjectPolicy.__dataclass_fields__["status"].default),
    }

    for table_name, (column_name, expected_default) in expectations.items():
        column_sql = _column_definition(_table_definition(schema, table_name), column_name)
        assert f"DEFAULT '{expected_default}'" in column_sql


def test_memory_usage_decision_contract_fields_are_required():
    decisions = _table_definition(_postgres_schema(), "memory_usage_decisions")

    for column_name in ["reason", "risk", "recommended_injection"]:
        assert "NOT NULL" in _column_definition(decisions, column_name)


def test_postgres_schema_preserves_shared_runtime_memory_id_namespace():
    schema = _postgres_schema()

    assert "CREATE TABLE memory_ids" in schema
    assert "memory_id TEXT PRIMARY KEY" in schema
    assert "memory_kind TEXT NOT NULL CHECK" in schema
    for table_name in ["failure_cases", "lessons", "project_policies"]:
        assert f"CREATE TRIGGER {table_name}_register_runtime_memory_id" in schema


def test_postgres_runtime_memory_records_are_append_only():
    schema = _postgres_schema()

    assert "CREATE FUNCTION protect_runtime_memory_identity()" in schema
    for table_name, identity_column in [
        ("failure_cases", "case_id"),
        ("lessons", "lesson_id"),
        ("project_policies", "policy_id"),
    ]:
        assert (
            f"CREATE TRIGGER {table_name}_protect_runtime_memory_identity"
            in schema
        )
        assert f"BEFORE UPDATE OF {identity_column} OR DELETE" in schema
        assert (
            f"EXECUTE FUNCTION protect_runtime_memory_identity('{identity_column}')"
            in schema
        )
    assert "runtime memory IDs are immutable" in schema
    assert "runtime memory records cannot be deleted" in schema


def test_postgres_runtime_memory_tables_reject_truncate():
    schema = _postgres_schema()

    assert "CREATE FUNCTION reject_runtime_memory_truncate()" in schema
    for table_name in [
        "memory_ids",
        "traces",
        "failure_cases",
        "lessons",
        "project_policies",
        "memory_usage_decisions",
    ]:
        assert f"CREATE TRIGGER {table_name}_reject_truncate" in schema
        assert f"BEFORE TRUNCATE ON {table_name}" in schema
    assert (
        "REVOKE TRUNCATE ON memory_ids, traces, failure_cases, lessons, "
        "project_policies FROM PUBLIC"
        in schema
    )
    assert (
        "REVOKE DELETE, TRUNCATE ON memory_usage_decisions FROM PUBLIC"
        in schema
    )


def test_postgres_protects_trace_and_usage_audit_records():
    schema = _postgres_schema()

    assert "CREATE FUNCTION protect_trace_record()" in schema
    assert "CREATE TRIGGER traces_protect_record" in schema
    assert "BEFORE UPDATE OR DELETE ON traces" in schema
    assert "trace completion must move forward exactly once" in schema
    assert "REVOKE DELETE ON traces FROM PUBLIC" in schema
    assert "CREATE FUNCTION protect_usage_decision_record()" in schema
    assert "CREATE TRIGGER memory_usage_decisions_protect_record" in schema
    assert "BEFORE UPDATE OR DELETE ON memory_usage_decisions" in schema
    assert "usage decision outcome must move forward exactly once" in schema
    assert "AND run_id = NEW.run_id\n  FOR SHARE;" in schema
    assert "pg_catalog.btrim(request_id)" in schema
    assert "pg_catalog.char_length(request_id)" in schema


def test_postgres_fresh_install_is_atomic_and_explicitly_targets_public():
    schema = _postgres_schema()
    statements = schema.strip().splitlines()

    assert statements[2] == "BEGIN;"
    assert statements[3] == "SET LOCAL search_path = public, pg_catalog;"
    assert statements[-1] == "COMMIT;"


def test_postgres_schema_enforces_verified_case_and_lesson_source_lifecycle():
    failure_cases = _table_definition(_postgres_schema(), "failure_cases")
    schema = _postgres_schema()

    assert "status != 'verified'" in failure_cases
    assert "fix IS NOT NULL" in failure_cases
    assert "fix_commit_sha IS NOT NULL" in failure_cases
    assert "regression_passed" in failure_cases
    assert "root_cause IS NOT NULL" in failure_cases
    assert "reviewed_by IS NOT NULL" in failure_cases
    assert "reviewed_at IS NOT NULL" in failure_cases
    assert "CREATE FUNCTION require_failure_case_source_trace()" in schema
    assert "eval_result IN ('fail', 'error')" in schema
    assert "CREATE TRIGGER failure_cases_require_failed_source_trace" in schema
    assert "CREATE FUNCTION protect_failure_case_source()" in schema
    assert "CREATE TRIGGER failure_cases_protect_source_provenance" in schema
    assert (
        "BEFORE UPDATE OF source_trace_id, commit_sha ON failure_cases"
        in schema
    )
    assert "CREATE FUNCTION require_verified_lesson_source_case()" in schema
    assert "CREATE TRIGGER lessons_require_verified_source_case" in schema
    assert "CREATE FUNCTION protect_lesson_source()" in schema
    assert "CREATE TRIGGER lessons_protect_source_provenance" in schema
    assert "BEFORE UPDATE OF source_case_id ON lessons" in schema
    assert "IF NEW.status = 'active'" in schema
    assert "BEFORE INSERT OR UPDATE OF source_case_id, status ON lessons" in schema
    assert "status = 'verified'" in schema
    assert "regression_passed" in schema
    assert "NOT source_trace.dirty" in schema
    assert "CREATE FUNCTION enforce_failure_case_lesson_lifecycle()" in schema
    assert "CREATE TRIGGER failure_cases_enforce_lesson_lifecycle" in schema
    assert "BEFORE UPDATE OF status, regression_passed ON failure_cases" in schema
    assert "UPDATE public.lessons" in schema
    assert "SET status = 'obsolete'" in schema
    assert "active lessons require a verified regression-backed source case" in schema


def test_trace_eval_result_defaults_to_unknown_and_is_required():
    traces = _table_definition(_postgres_schema(), "traces")
    eval_result = _column_definition(traces, "eval_result")

    assert "NOT NULL" in eval_result
    assert "DEFAULT 'unknown'" in eval_result


def test_postgres_trace_cost_rejects_non_finite_numeric_values():
    traces = _table_definition(_postgres_schema(), "traces")
    cost_usd = _column_definition(traces, "cost_usd")

    for non_finite in ["NaN", "Infinity", "-Infinity"]:
        assert f"'{non_finite}'::numeric" in cost_usd
    assert "NOT IN" in cost_usd


def test_trace_latency_matches_postgres_integer_range_in_portable_schema():
    trace_latency = _schema_properties(
        _json_schema("trace.schema.json")
    )["latency_ms"]
    traces = _table_definition(_postgres_schema(), "traces")
    postgres_latency = _column_definition(traces, "latency_ms")

    assert trace_latency == {
        "type": ["integer", "null"],
        "minimum": 0,
        "maximum": 2_147_483_647,
    }
    assert "traces_latency_ms_non_negative" in postgres_latency
    assert "CHECK (latency_ms >= 0)" in postgres_latency


def test_large_integer_cost_and_confidence_schemas_match_runtime_contracts():
    large_integer = 10**1000
    trace_cost = _schema_properties(_json_schema("trace.schema.json"))["cost_usd"]
    lesson_confidence = _schema_properties(
        _json_schema("lesson.schema.json")
    )["confidence"]
    policy_confidence = _schema_properties(
        _json_schema("project_policy.schema.json")
    )["confidence"]
    traces = _table_definition(_postgres_schema(), "traces")
    lessons = _table_definition(_postgres_schema(), "lessons")

    assert json.loads(json.dumps(large_integer)) == large_integer
    assert trace_cost == {"type": ["number", "null"]}
    for confidence in [lesson_confidence, policy_confidence]:
        assert confidence["type"] == "number"
        assert confidence["minimum"] == 0.0
        assert confidence["maximum"] == 1.0
    assert "cost_usd NUMERIC" in traces
    assert "confidence NUMERIC" in lessons


def test_jsonb_columns_constrain_object_and_array_shapes():
    schema = _postgres_schema()

    for table_name in ["lessons", "project_policies"]:
        table_sql = _table_definition(schema, table_name)
        scope_json = _column_definition(table_sql, "scope_json")
        assert "CHECK (valid_memory_scope_json(scope_json))" in scope_json

    decisions = _table_definition(schema, "memory_usage_decisions")
    for column_name in ["candidate_memory_ids", "used_memory_ids", "blocked_memory_ids"]:
        column_sql = _column_definition(decisions, column_name)
        assert f"jsonb_typeof({column_name}) = 'array'" in column_sql


def test_postgres_scope_json_matches_runtime_scope_contract():
    schema = _postgres_schema()

    assert "CREATE FUNCTION valid_memory_scope_json(value JSONB) RETURNS BOOLEAN" in schema
    assert "value != '{}'::jsonb" in schema
    for scope_field in [
        "repo",
        "tenant",
        "branch",
        "prompt_version",
        "prompt_family",
        "tool",
        "tool_schema_version",
        "model",
        "model_family",
        "eval_suite",
        "task_type",
        "failure_type",
    ]:
        assert f"'{scope_field}'" in schema
    assert "jsonb_typeof(entry.scope_value) != 'string'" in schema
    assert "btrim(entry.scope_value #>> '{}') = ''" in schema


def test_postgres_jsonb_arrays_constrain_element_shapes():
    schema = _postgres_schema()

    traces = _table_definition(schema, "traces")
    for column_name in ["retrieved_context", "tool_calls", "tool_outputs"]:
        column_sql = _column_definition(traces, column_name)
        assert f"jsonb_path_exists({column_name}" in column_sql
        assert '@.type() != "object"' in column_sql

    decisions = _table_definition(schema, "memory_usage_decisions")
    for column_name in ["candidate_memory_ids", "used_memory_ids", "blocked_memory_ids"]:
        column_sql = _column_definition(decisions, column_name)
        assert f"jsonb_path_exists({column_name}" in column_sql
        assert '@.type() != "string"' in column_sql


def test_postgres_usage_log_memory_id_arrays_match_store_contract():
    schema = _postgres_schema()
    decisions = _table_definition(schema, "memory_usage_decisions")

    assert "CREATE FUNCTION jsonb_text_array_has_duplicates" in schema
    for column_name in ["candidate_memory_ids", "used_memory_ids", "blocked_memory_ids"]:
        column_sql = _column_definition(decisions, column_name)
        assert f"jsonb_path_exists({column_name}" in column_sql
        assert '@ == ""' in column_sql
        assert f"NOT jsonb_text_array_has_duplicates({column_name})" in column_sql


def test_postgres_usage_logs_reference_known_runtime_memory_ids():
    schema = _postgres_schema()

    assert "CREATE FUNCTION require_known_usage_memory_ids()" in schema
    assert "CREATE TRIGGER memory_usage_decisions_require_known_memory_ids" in schema
    assert "FROM public.memory_ids" in schema
    assert "used memory ids must be present in candidates" in schema
    assert "blocked memory ids must be present in candidates" in schema


def test_postgres_usage_logs_bind_trace_and_run_with_composite_foreign_key():
    schema = _postgres_schema()
    traces = _table_definition(schema, "traces")
    decisions = _table_definition(schema, "memory_usage_decisions")

    assert "UNIQUE (trace_id, run_id)" in traces
    assert (
        "FOREIGN KEY (trace_id, run_id) REFERENCES traces(trace_id, run_id)"
        in decisions
    )
    assert "trace_id TEXT NOT NULL REFERENCES traces(trace_id)" not in decisions


def test_postgres_usage_logs_require_matching_context_and_tenant_evidence():
    schema = _postgres_schema()

    assert "CREATE FUNCTION require_usage_trace_context()" in schema
    for required_key in ["mode", "repo", "commit_sha"]:
        assert f"NEW.context ? '{required_key}'" in schema
        assert f"NEW.context ->> '{required_key}'" in schema
    assert "trace_record.repo" in schema
    assert "trace_record.commit_sha" in schema
    assert "trace_record.tenant" in schema
    assert "usage context tenant conflicts with trace" in schema

    trigger_match = re.search(
        r"CREATE TRIGGER memory_usage_decisions_require_trace_context\s+"
        r"BEFORE INSERT OR UPDATE OF (.*?)\s+ON memory_usage_decisions",
        schema,
        re.DOTALL,
    )
    assert trigger_match is not None
    trigger_columns = trigger_match.group(1)
    for column_name in ["trace_id", "run_id", "mode", "context"]:
        assert column_name in trigger_columns


def test_postgres_system_block_reasons_must_reference_candidates():
    schema = _postgres_schema()

    assert "jsonb_object_keys(NEW.system_blocked_reasons)" in schema
    assert "system block reason must reference a candidate" in schema
    trigger_match = re.search(
        r"CREATE TRIGGER memory_usage_decisions_require_known_memory_ids\s+"
        r"BEFORE INSERT OR UPDATE OF (.*?)\s+ON memory_usage_decisions",
        schema,
        re.DOTALL,
    )
    assert trigger_match is not None
    assert "system_blocked_reasons" in trigger_match.group(1)


def test_postgres_usage_logs_require_complete_candidate_status_evidence():
    schema = _postgres_schema()

    assert "jsonb_object_keys(NEW.candidate_memory_statuses)" in schema
    assert "candidate status evidence must include every candidate" in schema
    assert "candidate status evidence must not include non-candidates" in schema
    trigger_match = re.search(
        r"BEFORE INSERT OR UPDATE OF (.*?)\nON memory_usage_decisions",
        schema,
        re.DOTALL,
    )
    assert trigger_match is not None
    assert "candidate_memory_statuses" in trigger_match.group(1)


def test_postgres_usage_logs_enforce_decision_consistency_rules():
    decisions = _table_definition(_postgres_schema(), "memory_usage_decisions")

    assert "jsonb_array_length(used_memory_ids) = 0 AND recommended_injection = 'none'" in decisions
    assert "jsonb_array_length(used_memory_ids) > 0 AND recommended_injection != 'none'" in decisions
    assert "NOT memory_caused_failure" in decisions
    assert "eval_result IN ('fail', 'error')" in decisions


def test_docs_describe_shared_runtime_memory_id_namespace_and_usage_id_arrays_precisely():
    architecture = _doc("architecture.md")
    roadmap = _doc("product-program.md")

    assert "shared runtime memory ID namespace" in architecture
    assert "failure cases, lessons, and project policies" in architecture
    assert "duplicate, empty-string, or non-string memory IDs" in architecture
    assert "duplicate, empty-string, or non-string memory ID lists" in roadmap
    assert "empty memory ID lists" not in architecture
    assert "empty memory ID lists" not in roadmap


def test_docs_describe_memory_decision_consistency_contract():
    readme = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    architecture = _doc("architecture.md")
    usage_policy = _doc("usage-policy.md")

    for document in [readme, architecture, usage_policy]:
        assert "use_memory" in document
        assert "recommended_injection" in document
        assert "consistent" in document


def test_memory_context_example_matches_parser_contract():
    example_path = ROOT / "examples" / "memory_context.example.json"
    assert example_path.exists(), "memory_context.example.json should document parse_memory_context input"

    payload = json.loads(example_path.read_text(encoding="utf-8"))
    context = parse_memory_context(payload)

    assert context.mode == payload["mode"]
    assert context.repo == payload["repo"]
    assert context.commit_sha == payload["commit_sha"]


def test_memory_context_schema_requires_complete_input_hash_identity_pair():
    schema = _json_schema("memory_context.schema.json")
    properties = _schema_properties(schema)

    assert properties["input_hash"] == {
        "type": "string",
        "pattern": r"\S",
        "minLength": 1,
        "maxLength": 512,
    }
    assert schema["allOf"] == [
        {
            "if": {"required": ["input_hash"]},
            "then": {"required": ["eval_suite"]},
        }
    ]

    eval_suite_only_context = {
        "mode": "repair",
        "repo": "repo",
        "commit_sha": "abc",
        "eval_suite": "suite",
    }
    complete_context = {
        **eval_suite_only_context,
        "input_hash": "sha256:example",
    }
    input_hash_only_context = {
        "mode": "repair",
        "repo": "repo",
        "commit_sha": "abc",
        "input_hash": "sha256:example",
    }

    assert _json_schema_accepts(schema, complete_context)
    assert _json_schema_accepts(schema, eval_suite_only_context)
    assert not _json_schema_accepts(schema, input_hash_only_context)


def test_docs_publish_benchmark_leakage_contract_and_persistence_boundaries():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`(eval_suite, input_hash)`",
        "canonicalize",
        "collision-resistant",
        "Each trace carries the hash of its own example",
        "current `MemoryContext` must match the current trace",
        "same hash only when they represent the same canonical example",
        "Incomplete identities never trigger a guessed match",
        "every mode",
        "Static `sensitive` and `eval_leaking` checks retain precedence",
        "ephemeral `source_eval_suite` and `source_input_hash`",
        "Candidate `source_eval_suite` and `source_input_hash` fields are not serialized into prompts or snippets",
        "builders do not render structured `input_hash` fields",
        "`eval_suite` remains ordinary prompt context",
        "context/trace binding",
        "automatic block reason",
        "`input_hash` is identity evidence, not memory scope",
        "snapshot version 2",
        "PostgreSQL schema version 2",
        "no new persisted memory fields",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert (
        "Phase 11: Benchmark example leakage classification (implemented)"
        in documents["docs/product-program.md"]
    )

    context_schema = _json_schema("memory_context.schema.json")
    assert "input_hash" in _schema_properties(context_schema)
    assert context_schema["allOf"] == [
        {
            "if": {"required": ["input_hash"]},
            "then": {"required": ["eval_suite"]},
        }
    ]

    postgres_schema = _postgres_schema()
    assert postgres_schema.count("input_hash TEXT") == 1
    assert "source_eval_suite" not in postgres_schema
    assert "source_input_hash" not in postgres_schema


def test_docs_publish_outcome_aware_metrics_and_ephemeral_boundary():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`pass`, `fail`, and `error` are evaluated outcomes",
        "`error` is an evaluated non-pass",
        "`unknown` and `None` are unevaluated",
        "`evaluated_with_memory_count`",
        "`evaluated_without_memory_count`",
        "`unevaluated_decision_count`",
        "decision counts, not per-memory causal attribution",
        "Metrics remain derived and are not persisted",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert (
        "Phase 12: Outcome-aware metrics (implemented)"
        in documents["docs/product-program.md"]
    )

    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert "metrics" not in _schema_properties(snapshot_schema)
    postgres_schema = _postgres_schema()
    for field_name in (
        "evaluated_with_memory_count",
        "evaluated_without_memory_count",
        "unevaluated_decision_count",
    ):
        assert field_name not in postgres_schema


def test_docs_publish_per_memory_outcome_metrics_and_noncausal_boundary():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`memory_outcome_metrics()`",
        "every stored failure case, lesson, and project policy",
        "`candidate_count`",
        "`used_count`",
        "`blocked_count`",
        "both deterministic and LLM-narrowing blocks",
        "`evaluated_use_count`",
        "`unevaluated_use_count`",
        "`observed_pass_rate`",
        "observed associations, not causal effectiveness estimates",
        "does not derive per-memory wrong-memory attribution",
        "Metrics remain derived and are not persisted",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert (
        "Phase 13: Per-memory outcome metrics (implemented)"
        in documents["docs/product-program.md"]
    )

    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert "memory_outcome_metrics" not in _schema_properties(snapshot_schema)
    postgres_schema = _postgres_schema()
    for field_name in (
        "candidate_count",
        "used_count",
        "blocked_count",
        "evaluated_use_count",
        "unevaluated_use_count",
        "observed_pass_rate",
    ):
        assert field_name not in postgres_schema


def test_docs_publish_declared_trace_provenance_binding_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`repo`, `commit_sha`, and `tenant` always match",
        "`branch`, `prompt_version`, `prompt_family`, `tool_schema_version`, `model`, and `eval_suite`",
        "only when the context declares them",
        "exact plain-string Trace tool call",
        "Omitted optional provenance remains broad",
        "`model_family`, `task_type`, and `failure_type` remain unbound",
        "before pending request consumption or usage-log append",
        "Imported version-2 and supplied legacy context evidence",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert (
        "Phase 14: Declared Trace provenance binding (implemented)"
        in documents["docs/product-program.md"]
    )

    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema


def test_docs_publish_deferred_outcome_sealing_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`record_decision_outcome()`",
        "`None` or `unknown`",
        "`pass`",
        "`fail`",
        "`error`",
        "`memory_caused_failure`",
        "exact replay",
        "every other usage",
        "immutable",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        normalized_lower = normalized.lower()
        for contract in required_contracts:
            assert contract.lower() in normalized_lower, (
                f"{name} should publish: {contract}"
            )

    assert (
        "Phase 15: Deferred decision outcome sealing (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    usage_schema = _json_schema("memory_usage_log.schema.json")
    assert "eval_result" in usage_schema["properties"]
    assert "memory_caused_failure" in usage_schema["properties"]
    assert "outcome_sealed" not in usage_schema["properties"]
    assert "VALUES (true, 2)" in _postgres_schema()


def test_docs_publish_deferred_trace_completion_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`complete_trace()`",
        "`output_hash`",
        "`tool_outputs`",
        "`eval_result`",
        "`latency_ms`",
        "`cost_usd`",
        "`error`",
        "`trace_uri`",
        "`unknown`",
        "exact replay",
        "`record_decision_outcome()`",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        normalized_lower = normalized.lower()
        for contract in required_contracts:
            assert contract.lower() in normalized_lower, (
                f"{name} should publish: {contract}"
            )

    assert (
        "Phase 16: Deferred Trace completion (implemented)"
        in documents["docs/product-program.md"]
    )
    trace_schema = _json_schema("trace.schema.json")
    for field_name in (
        "output_hash",
        "tool_outputs",
        "eval_result",
        "latency_ms",
        "cost_usd",
        "error",
        "trace_uri",
    ):
        assert field_name in trace_schema["properties"]
    assert "execution_completed" not in trace_schema["properties"]
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "VALUES (true, 2)" in _postgres_schema()


def test_docs_publish_atomic_memory_run_completion_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`complete_memory_run()`",
        "`MemoryRunCompletion`",
        "`trace_id`",
        "`decision_id`",
        "`complete_trace()`",
        "`record_decision_outcome()`",
        "partial recovery",
        "atomic",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        normalized_lower = normalized.lower()
        for contract in required_contracts:
            assert contract.lower() in normalized_lower, (
                f"{name} should publish: {contract}"
            )

    assert (
        "Phase 17: Atomic memory-run completion (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "memory_run_completions" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "memory_run_completions" not in postgres_schema


def test_docs_publish_memory_run_audits_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`memory_run_audits()`",
        "`MemoryRunAudit`",
        "`trace_id`",
        "`decision_id`",
        "`pending`",
        "`trace_only`",
        "`decision_only`",
        "`complete`",
        "`conflict`",
        "one record for every usage decision",
        "partial recovery",
        "never auto",
        "derived",
        "not persisted",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        normalized_lower = normalized.lower()
        for contract in required_contracts:
            assert contract.lower() in normalized_lower, (
                f"{name} should publish: {contract}"
            )

    assert (
        "Phase 18: Memory-run audit view (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "memory_run_audits" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "memory_run_audits" not in postgres_schema


def test_docs_publish_memory_run_recovery_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`recover_memory_run()`",
        "`decision_id`",
        "does not accept `trace_id` or `eval_result`",
        "`pending`",
        "`trace_only`",
        "`decision_only`",
        "`complete`",
        "`conflict`",
        "`memory_caused_failure`",
        "never guesses",
        "`MemoryRunCompletion`",
        "`complete_memory_run()`",
        "atomic",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        normalized_lower = normalized.lower()
        for contract in required_contracts:
            assert contract.lower() in normalized_lower, (
                f"{name} should publish: {contract}"
            )

    assert (
        "Phase 19: Safe memory-run recovery (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "memory_run_recoveries" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "memory_run_recoveries" not in postgres_schema


def test_docs_publish_memory_run_metrics_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`memory_run_metrics()`",
        "`MemoryRunMetrics`",
        "`decision_count`",
        "`pending_count`",
        "`trace_only_count`",
        "`decision_only_count`",
        "`complete_count`",
        "`conflict_count`",
        "`recoverable_count`",
        "one usage decision",
        "`recoverable_count` is the sum",
        "derived",
        "not persisted",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        normalized_lower = normalized.lower()
        for contract in required_contracts:
            assert contract.lower() in normalized_lower, (
                f"{name} should publish: {contract}"
            )

    assert (
        "Phase 20: Memory-run health metrics (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "memory_run_metrics" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "memory_run_metrics" not in postgres_schema


def test_docs_publish_atomic_batch_memory_run_recovery_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`recover_memory_runs()`",
        "non-empty tuple",
        "unique",
        "`memory_caused_failures`",
        "`trace_only`",
        "`decision_only`",
        "`complete`",
        "`pending`",
        "`conflict`",
        "preserves request order",
        "all-or-nothing",
        "shared Trace",
        "does not accept `trace_id` or `eval_result`",
        "completion evidence",
        "`recover_memory_run()`",
        "not persisted",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        normalized_lower = normalized.lower()
        for contract in required_contracts:
            assert contract.lower() in normalized_lower, (
                f"{name} should publish: {contract}"
            )

    assert (
        "Phase 21: Atomic batch memory-run recovery (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "memory_run_recovery_batches" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "memory_run_recovery_batches" not in postgres_schema


def test_docs_publish_atomic_batch_memory_run_completion_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`complete_memory_runs()`",
        "`MemoryRunResult`",
        "`MeasuredEvalResult`",
        "non-empty tuple",
        "unique",
        "derives `trace_id`",
        "preserves request order",
        "shared Trace",
        "merge",
        "all-or-nothing",
        "`tool_outputs`",
        "`None` means omitted",
        "`complete_memory_run()`",
        "`recover_memory_runs()`",
        "not persisted",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        normalized_lower = normalized.lower()
        for contract in required_contracts:
            assert contract.lower() in normalized_lower, (
                f"{name} should publish: {contract}"
            )

    assert (
        "Phase 22: Atomic batch memory-run completion (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "memory_run_results" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "memory_run_results" not in postgres_schema


def test_docs_publish_memory_run_remediation_plan_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`memory_run_remediations()`",
        "`MemoryRunRemediation`",
        "`MemoryRunRemediationAction`",
        "`measure`",
        "`recover`",
        "`recover_with_attribution`",
        "`investigate`",
        "`none`",
        "`resolved_eval_result`",
        "`resolved_memory_caused_failure`",
        "`auto_recoverable_count`",
        "`attribution_required_count`",
        "`recoverable_count`",
        "stale",
        "`complete_memory_runs()`",
        "`recover_memory_runs()`",
        "derived and not persisted",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        normalized_lower = normalized.lower()
        for contract in required_contracts:
            assert contract.lower() in normalized_lower, (
                f"{name} should publish: {contract}"
            )

    assert (
        "Phase 23: Memory-run remediation plan (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "memory_run_remediations" not in snapshot_schema["properties"]
    assert "memory_run_metrics" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "memory_run_remediations" not in postgres_schema


def test_docs_publish_ready_memory_run_recovery_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`recover_ready_memory_runs()`",
        "`recover_memory_run()`",
        "`recover_memory_runs()`",
        "`recover_with_attribution`",
        "`decision_id`",
        "reentrant lock",
        "empty tuple",
        "skip",
        "shared Trace",
        "all-or-nothing",
        "concurrent",
        "explicit",
        "selection is not persisted",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        normalized_lower = normalized.lower()
        for contract in required_contracts:
            assert contract.lower() in normalized_lower, (
                f"{name} should publish: {contract}"
            )

    assert (
        "Phase 24: Atomic ready memory-run recovery (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "ready_memory_run_recoveries" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "ready_memory_run_recoveries" not in postgres_schema


def test_docs_publish_snapshot_operations_cli_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`tbm`",
        "`python -m trace_backed_memory`",
        "snapshot validate",
        "snapshot stats",
        "audit",
        "metrics",
        "remediation",
        "recover-ready",
        "recover-batch",
        "dry-run",
        "`--write`",
        "structured JSON",
        "exit codes",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        normalized_lower = normalized.lower()
        for contract in required_contracts:
            assert contract.lower() in normalized_lower, (
                f"{name} should publish: {contract}"
            )

    assert (
        "Phase 25: Snapshot Operations CLI (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert set(snapshot_schema["properties"]) == {
        "snapshot_version",
        "traces",
        "failure_cases",
        "lessons",
        "project_policies",
        "usage_logs",
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    for ephemeral_name in [
        "snapshot_operations",
        "memory_run_audits",
        "memory_run_metrics",
        "memory_run_remediations",
    ]:
        assert ephemeral_name not in postgres_schema


def test_docs_publish_memory_run_execution_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`run_memory_execution()`",
        "`MemoryDecisionCallback`",
        "`MemoryExecutionCallback`",
        "`MemoryRunMeasurement`",
        "`MemoryRunExecutionError`",
        "`MemoryGateRequest`",
        "`GatedMemoryResult`",
        "`complete_memory_run()`",
        "decision_id",
        "callback",
        "advanced",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        normalized_lower = normalized.lower()
        for contract in required_contracts:
            assert contract.lower() in normalized_lower, (
                f"{name} should publish: {contract}"
            )

    assert (
        "Phase 26: Synchronous memory-run execution (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "memory_run_measurements" not in snapshot_schema["properties"]
    assert "memory_run_callback_errors" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "memory_run_measurements" not in postgres_schema
    assert "memory_run_callback_errors" not in postgres_schema


def test_docs_publish_packaged_resources_and_persistence_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`packaged_resources()`",
        "`read_packaged_resource()`",
        "`export_packaged_resource()`",
        "allowlist",
        "20",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        for contract in required_contracts:
            assert contract.lower() in normalized.lower(), (
                f"{name} should publish: {contract}"
            )

    assert "`py.typed`" in documents["README.md"]
    assert "byte-identical" in documents["README.md"]
    assert "`PackagedResourceError`" in documents["docs/architecture.md"]
    assert (
        "Phase 27: Packaged distribution resources (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "packaged_resources" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "packaged_resources" not in postgres_schema


def test_docs_publish_evidence_ingestion_integrity_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "`tool_outputs`",
        "top-level",
        "duplicate",
        "all-or-nothing",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        for contract in required_contracts:
            assert contract.lower() in normalized.lower(), (
                f"{name} should publish: {contract}"
            )

    assert (
        "Phase 28: Evidence ingestion integrity (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "evidence_ingestion" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "evidence_ingestion" not in postgres_schema


def test_docs_publish_conservative_failure_extraction_accuracy():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "truthy top-level `error`",
        "`required argument`",
        "`required parameter`",
        "`required field`",
        "`required property`",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 50: Conservative failure extraction accuracy (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_linear_snapshot_usage_log_validation():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "average o(n)",
        "`decision_id`",
        "`run_id`",
        "candidate/used/blocked",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 51: Linear snapshot usage-log validation (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_indexed_usage_log_operations():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "average o(1)",
        "`decision_id`",
        "numeric suffix",
        "derived index",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 52: Indexed usage-log operations (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_indexed_run_to_trace_lookup():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "average o(1)",
        "`run_id`",
        "`trace_id`",
        "derived index",
        "ambiguous",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 53: Indexed run-to-Trace lookup (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_referenced_live_memory_id_validation():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "average o(r)",
        "referenced ids",
        "`known_memory_ids`",
        "no new derived index",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 54: Referenced live memory-ID validation (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_single_pass_store_metrics():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "one usage-log pass",
        "o(1) accumulator space",
        "`metrics()`",
        "`memory_outcome_metrics()`",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 55: Single-pass Store metrics (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_single_pass_memory_run_metrics():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "`memory_run_metrics()`",
        "one usage-log pass",
        "without sorting",
        "o(1) accumulator space",
        "`memory_run_audits()`",
        "decision-id order",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 56: Single-pass memory-run metrics (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_serialized_snapshot_cli_writes():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "`--write`",
        "read-modify-write",
        "advisory lock",
        "`.tbm.lock`",
        "before snapshot load",
        "before stdout",
        "30 seconds",
        "exit code 4",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 57: Serialized snapshot CLI writes (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_active_only_lesson_imports():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "active-only",
        "`load_lessons_yaml()`",
        "status",
        "active",
        "obsolete",
        "all-or-nothing",
        "input error",
        "exit code 2",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 58: Active-only lesson imports (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_bounded_pr_change_sets():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "prchangeset",
        "at most 6 entries",
        "before entry",
        "one pass",
        "exit code 2",
        "without git",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 59: Bounded PR change sets (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_linear_legacy_pr_warnings():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "legacy pr warning",
        "one pass",
        "first occurrence",
        "at most 7",
        "duplicate",
        "unknown",
        "o(w + c)",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 60: Linear legacy PR warnings (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_bounded_git_capture():
    from trace_backed_memory import packaged_resources
    from trace_backed_memory.capture import (
        GIT_CAPTURE_OUTPUT_MAX_BYTES,
        GIT_CAPTURE_TIMEOUT_SECONDS,
    )

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "git capture",
        "devnull",
        "30 seconds",
        "64 kib",
        "utf-8",
        "first byte",
        "injected runner",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert GIT_CAPTURE_TIMEOUT_SECONDS == 30.0
    assert GIT_CAPTURE_OUTPUT_MAX_BYTES == 64 * 1024
    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 61: Bounded Git capture (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_durable_atomic_publish():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "atomic publish",
        "parent directory",
        "posix",
        "post-publication",
        "indeterminate durability",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 62: Durable atomic publish (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_bounded_semantic_top_k():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "bounded semantic top-k",
        "membership view",
        "full sort",
        "score-descending",
        "memory-id-ascending",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 63: Bounded semantic top-k (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_public_snapshot_write_lock():
    from trace_backed_memory import packaged_resources, snapshot_write_lock

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "snapshot_write_lock",
        "timeout_seconds",
        "read-modify-write",
        ".tbm.lock",
        "advisory",
        "non-reentrant",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert callable(snapshot_write_lock)
    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 64: Public snapshot write lock (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_bounded_runtime_trace_json():
    import trace_backed_memory.store as store_module
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = (
        "retrieved_context",
        "tool_calls",
        "tool_outputs",
        "100,000",
        "8 mib",
        "utf-8",
        "depth 100",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = (
            " ".join(document.split())
            .lower()
            .replace("depth-100", "depth 100")
        )
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert store_module.TRACE_JSON_MAX_DEPTH == 100
    assert store_module.TRACE_JSON_MAX_NODES == 100_000
    assert store_module.TRACE_JSON_MAX_TEXT_BYTES == 8 * 1024 * 1024
    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 65: Bounded runtime Trace JSON (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_postgres_loaded_row_payloads():
    from trace_backed_memory import packaged_resources, postgres

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
        "Phase 48 design": _doc(
            "superpowers/specs/"
            "2026-07-22-postgres-load-payload-budget-design.md"
        ),
    }
    required_contracts = (
        "loaded-row projection",
        "updated_at",
        "compact",
        "64 mib",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower().replace(
            "loaded row",
            "loaded-row",
        )
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    payload_sql = " ".join(
        postgres._MEASURE_SNAPSHOT_PAYLOAD_BYTES.split()
    )
    projection = "OPERATOR(pg_catalog.-) 'updated_at'"
    assert [
        projection in branch for branch in payload_sql.split("UNION ALL")
    ] == [False, True, True, True, False]
    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 66: PostgreSQL loaded-row payloads (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert postgres_schema.count("updated_at TIMESTAMPTZ DEFAULT now()") == 3
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_snapshot_lock_sidecar_safety():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
        "Phase 57 design": _doc(
            "superpowers/specs/"
            "2026-07-22-serialized-snapshot-cli-writes-design.md"
        ),
        "Phase 64 design": _doc(
            "superpowers/specs/"
            "2026-07-22-public-snapshot-write-lock-design.md"
        ),
        "Phase 67 design": _doc(
            "superpowers/specs/"
            "2026-07-22-snapshot-lock-sidecar-safety-design.md"
        ),
    }
    required_contracts = (
        "single-link regular file",
        "symbolic link",
        "hard link",
        "reparse point",
        "placeholder",
        "exit code 4",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 67: Snapshot lock sidecar safety (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_git_metadata_output_validation():
    import trace_backed_memory.capture as capture_module
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
        "Phase 61 design": _doc(
            "superpowers/specs/2026-07-22-bounded-git-capture-design.md"
        ),
        "Phase 68 design": _doc(
            "superpowers/specs/"
            "2026-07-22-git-metadata-output-validation-design.md"
        ),
    }
    required_contracts = (
        "blank commit sha",
        "blank repository root",
        "non-string output",
        "512 characters",
        "detached head",
        "tracemetadatacaptureerror",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower().replace(
            "512-character",
            "512 characters",
        )
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert capture_module.METADATA_VALUE_MAX_CHARS == 512
    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 68: Git metadata output validation (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_explicit_failure_text_classification():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
        "Phase 28 design": _doc(
            "superpowers/specs/"
            "2026-07-20-evidence-ingestion-integrity-design.md"
        ),
        "Phase 69 design": _doc(
            "superpowers/specs/"
            "2026-07-22-explicit-failure-text-classification-design.md"
        ),
    }
    required_contracts = (
        "trace.error",
        "top-level",
        "error",
        "symptom",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"
        for keyword in ("tool", "name", "taxonomy"):
            assert keyword in normalized, f"{name} should publish: {keyword}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 69: Explicit failure text classification (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_measured_completion_cli_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "complete",
        "fresh measured result",
        "--eval-result",
        "--tool-outputs-file",
        "array of objects",
        "dry-run",
        "--write",
        "does not infer",
        "snapshot version 2",
        "PostgreSQL schema version 2",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract.lower() in normalized, (
                f"{name} should publish: {contract}"
            )

    assert (
        "Phase 29: Measured memory-run completion CLI (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "measured_completion_commands" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "measured_completion_commands" not in postgres_schema


def test_docs_publish_lesson_yaml_persistence_integrity_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "save_lessons_yaml",
        "sibling temporary",
        "fsync",
        "os.replace",
        "lesson_text: |",
        "blank lines",
        "canonical lf",
        "snapshot version 2",
        "postgresql schema version",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert (
        "Phase 30: Lesson YAML persistence integrity (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "lesson_yaml_writes" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "lesson_yaml_writes" not in postgres_schema


def test_docs_publish_batch_measured_completion_cli_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "complete-batch",
        "measurements_json",
        "strict utf-8 json",
        "non-empty array",
        "memoryrunresult",
        "complete_memory_runs",
        "manifest order",
        "duplicate object keys",
        "all-or-nothing",
        "dry-run",
        "--write",
        "snapshot version 2",
        "postgresql schema version",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert (
        "Phase 31: Batch measured memory-run completion CLI (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "batch_completion_commands" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "batch_completion_commands" not in postgres_schema


def test_docs_publish_bounded_local_document_ingestion_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/product.md": _doc("product.md"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "bounded local document ingestion",
        "64 mib",
        "8 mib",
        "1 mib",
        "max_bytes",
        "none",
        "snapshot version 2",
        "postgresql schema version",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert (
        "Phase 32: Bounded local document ingestion (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "ingestion_limits" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "ingestion_limits" not in postgres_schema


def test_docs_publish_pr_report_cli_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/product.md": _doc("product.md"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "pr-report",
        "context_json",
        "change_set_json",
        "--repo-path",
        "pr_report_commit_anchors",
        "capture_commit_ancestry",
        "pr_memory_report",
        "read-only",
        "snapshot version 2",
        "postgresql schema version",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert (
        "Phase 33: PR report CLI (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "pr_report_commands" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "pr_report_commands" not in postgres_schema

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert ".package-smoke/bin/tbm pr-report" in workflow
    assert (
        ".sdist-smoke/bin/python -m trace_backed_memory pr-report"
        in workflow
    )


def test_docs_publish_active_lessons_cli_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/product.md": _doc("product.md"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "lessons export",
        "lessons import",
        "--overwrite",
        "--write",
        "8 mib",
        "10,000",
        "dry-run",
        "save_lessons_yaml",
        "load_lessons_yaml",
        "all-or-nothing",
        "snapshot version 2",
        "postgresql schema version",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert (
        "Phase 34: Active lessons portability CLI (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "lesson_cli_commands" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "lesson_cli_commands" not in postgres_schema

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert ".package-smoke/bin/tbm lessons export" in workflow
    assert ".package-smoke/bin/tbm lessons import" in workflow
    assert (
        ".sdist-smoke/bin/python -m trace_backed_memory lessons export"
        in workflow
    )
    assert (
        ".sdist-smoke/bin/python -m trace_backed_memory lessons import"
        in workflow
    )


def test_docs_publish_atomic_batch_obsolescence_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/product.md": _doc("product.md"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    required_contracts = [
        "obsolete",
        "obsolete-batch",
        "memoryobsolescencerequest",
        "obsolete_memories()",
        "failure-case",
        "project-policy",
        "forward-only",
        "cascade",
        "dry-run",
        "--write",
        "all-or-nothing",
        "snapshot version 2",
        "postgresql schema version",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 35: Memory obsolescence CLI (implemented)"
        in documents["docs/product-program.md"]
    )
    assert (
        "Phase 36: Atomic batch memory obsolescence (implemented)"
        in documents["docs/product-program.md"]
    )
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "obsolescence_commands" not in snapshot_schema["properties"]
    assert "memory_obsolescence_batches" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "obsolescence_commands" not in postgres_schema
    assert "memory_obsolescence_batches" not in postgres_schema

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert ".package-smoke/bin/tbm obsolete" in workflow
    assert ".package-smoke/bin/tbm obsolete-batch" in workflow
    assert (
        ".sdist-smoke/bin/python -m trace_backed_memory obsolete"
        in workflow
    )
    assert (
        ".sdist-smoke/bin/python -m trace_backed_memory obsolete-batch"
        in workflow
    )


def test_docs_publish_required_postgres_and_windows_ci_coverage():
    readme = (ROOT / "docs" / "reference.md").read_text(encoding="utf-8")
    product = _doc("product.md")
    architecture = _doc("architecture.md")
    usage_policy = _doc("usage-policy.md")
    roadmap = _doc("product-program.md")
    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )

    assert CURRENT_IMPLEMENTED_PHASE in product
    assert (
        "Phase 37: Required PostgreSQL and Windows CI coverage (implemented)"
        in roadmap
    )
    for document in (readme, architecture, usage_policy, roadmap):
        assert "TBM_REQUIRE_POSTGRES" in document
        assert "Windows" in document or "windows-latest" in document
        assert "initdb" in document
        assert "pg_ctl" in document
        assert "psql" in document

    for contract in (
        "windows:",
        "runs-on: windows-latest",
        "postgres:",
        'TBM_REQUIRE_POSTGRES: "1"',
        "sudo apt-get install --yes postgresql postgresql-client",
        "initdb --version",
        "pg_ctl --version",
        "psql --version",
        'python -c "import psycopg; print(psycopg.__version__)"',
        "tests/test_postgres_integration.py",
        "tests/test_postgres_repository.py",
    ):
        assert contract in workflow


def test_docs_publish_deferred_outcome_cli_and_compatibility():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    product = _doc("product.md")
    required_contracts = [
        "outcome",
        "record_decision_outcome()",
        "decision_id",
        "pass",
        "fail",
        "error",
        "memory_caused_failure",
        "dry-run",
        "changed",
        "written",
        "snapshot version 2",
        "postgresql schema version",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in product
    assert "decision-only `outcome` CLI" in product
    assert (
        "Phase 38: Deferred decision outcome CLI (implemented)"
        in documents["docs/product-program.md"]
    )

    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "outcome_commands" not in snapshot_schema["properties"]
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "outcome_commands" not in postgres_schema

    workflow = (ROOT / ".github" / "workflows" / "ci.yml").read_text(
        encoding="utf-8"
    )
    assert ".package-smoke/bin/tbm outcome" in workflow
    assert (
        ".sdist-smoke/bin/python -m trace_backed_memory outcome"
        in workflow
    )


def test_docs_publish_postgres_consistency_hardening_without_schema_change():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in (
            "share",
            "for update",
            "external",
            "snapshot version 2",
            "postgresql schema version",
        ):
            assert contract in normalized, f"{name} should publish: {contract}"

    for name in (
        "README.md",
        "docs/architecture.md",
        "docs/usage-policy.md",
    ):
        normalized = " ".join(documents[name].split()).lower()
        assert (
            "table-level `update`, `delete`, or `truncate` privilege"
            in normalized
        )
        assert "outer" in normalized
        assert "commit or rollback" in normalized

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 39: PostgreSQL consistent snapshots and lifecycle row locks "
        "(implemented)"
        in documents["docs/product-program.md"]
    )

    runtime = (
        ROOT / "src" / "trace_backed_memory" / "postgres.py"
    ).read_text(encoding="utf-8")
    assert "_LOCK_SNAPSHOT_TABLES_FOR_SHARE" in runtime
    assert "LOCK TABLE public.traces" in runtime
    assert "public.memory_usage_decisions\nIN SHARE MODE" in runtime

    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "snapshot_locks" not in postgres_schema


def test_docs_publish_postgres_bounded_load_before_materialization():
    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in (
            "count(*)",
            "count preflight",
            "100,000",
            "250,000",
            "snapshot version 2",
            "postgresql schema version",
        ):
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 40: PostgreSQL bounded load materialization (implemented)"
        in documents["docs/product-program.md"]
    )

    runtime = (
        ROOT / "src" / "trace_backed_memory" / "postgres.py"
    ).read_text(encoding="utf-8")
    load_runtime = runtime[runtime.index("    def load(self)") :]
    assert "_COUNT_SNAPSHOT_RECORDS" in runtime
    assert load_runtime.index("_snapshot_record_counts") < load_runtime.index(
        "_load_traces"
    )

    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "snapshot_record_counts" not in postgres_schema


def test_docs_publish_runtime_cardinality_limits_and_schema_change():
    from trace_backed_memory import (
        COMMIT_ANCESTRY_MAX_ANCHORS,
        packaged_resources,
    )

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in (
            "allowed_memory_ids",
            "blocked_memory_ids",
            "50",
            "commit_ancestry_max_anchors",
            "1,000",
            "snapshot version 2",
            "postgresql schema version",
        ):
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 41: Runtime collection cardinality limits (implemented)"
        in documents["docs/product-program.md"]
    )
    assert COMMIT_ANCESTRY_MAX_ANCHORS == 1_000

    decision_schema = _json_schema("memory_decision.schema.json")
    for field_name in ("allowed_memory_ids", "blocked_memory_ids"):
        assert decision_schema["properties"][field_name]["maxItems"] == 50
    canonical_schema = (ROOT / "schemas" / "memory_decision.schema.json").read_bytes()
    packaged_schema = (
        ROOT
        / "src"
        / "trace_backed_memory"
        / "_resources"
        / "schemas"
        / "memory_decision.schema.json"
    ).read_bytes()
    assert packaged_schema == canonical_schema
    assert len(packaged_resources()) == 89

    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "commit_ancestry_max_anchors" not in postgres_schema


def test_docs_publish_postgres_concurrent_insert_revalidation():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in (
            "same-primary-key",
            "savepoint",
            "23505",
            "p0001",
            "for update",
            "unchanged",
            "updated",
            "postgresconflicterror",
            "postgrespersistenceerror",
            "snapshot version 2",
            "postgresql schema version",
        ):
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 42: PostgreSQL concurrent insert revalidation (implemented)"
        in documents["docs/product-program.md"]
    )

    runtime = (
        ROOT / "src" / "trace_backed_memory" / "postgres.py"
    ).read_text(encoding="utf-8")
    for contract in (
        '_UNIQUE_VIOLATION_SQLSTATE = "23505"',
        '_RAISE_EXCEPTION_SQLSTATE = "P0001"',
        "def _is_recoverable_insert_collision(",
        "def _insert_or_reselect_concurrent_row(",
        "register_runtime_memory_id()",
    ):
        assert contract in runtime

    canonical_postgres = (ROOT / "schemas" / "postgres.sql").read_bytes()
    packaged_postgres = (
        ROOT
        / "src"
        / "trace_backed_memory"
        / "_resources"
        / "schemas"
        / "postgres.sql"
    ).read_bytes()
    assert packaged_postgres == canonical_postgres
    assert len(packaged_resources()) == 89

    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "VALUES (true, 2)" in _postgres_schema()


def test_docs_publish_strict_json_object_key_uniqueness():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in (
            "duplicate object key",
            "last-key-wins",
            "tracebackedmemorystore.load_json()",
            "parse_memory_context()",
            "parse_memory_decision()",
            "snapshot version 2",
            "postgresql schema version",
        ):
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 43: Strict JSON object key uniqueness (implemented)"
        in documents["docs/product-program.md"]
    )

    runtime_files = {
        name: (ROOT / "src" / "trace_backed_memory" / name).read_text(
            encoding="utf-8"
        )
        for name in ("_ingestion.py", "cli.py", "policy.py", "store.py")
    }
    assert "def unique_json_object_pairs(" in runtime_files["_ingestion.py"]
    assert "def parse_bounded_json(" in runtime_files["_ingestion.py"]
    assert "parse_bounded_json" in runtime_files["cli.py"]
    for name in ("policy.py", "store.py"):
        assert "unique_json_object_pairs" in runtime_files[name]
        assert "object_pairs_hook" in runtime_files[name]

    assert len(packaged_resources()) == 89
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "VALUES (true, 2)" in _postgres_schema()


def test_docs_publish_recover_batch_argument_cardinality():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in (
            "recover-batch",
            "10,000",
            "snapshot version 2",
            "postgresql schema version",
        ):
            assert contract in normalized, f"{name} should publish: {contract}"

    published_contract = " ".join(
        documents[name]
        for name in (
            "README.md",
            "docs/architecture.md",
            "docs/usage-policy.md",
            "docs/product.md",
        )
    ).lower()
    for contract in (
        "10,000 decision ids",
        "10,000 attribution",
        "before snapshot loading",
    ):
        assert contract in published_contract

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 44: Bounded recover-batch arguments (implemented)"
        in documents["docs/product-program.md"]
    )

    ingestion_source = (
        ROOT / "src" / "trace_backed_memory" / "_ingestion.py"
    ).read_text(encoding="utf-8")
    assert "CLI_RECOVER_BATCH_MAX_ITEMS = 10_000" in ingestion_source

    cli_source = (
        ROOT / "src" / "trace_backed_memory" / "cli.py"
    ).read_text(encoding="utf-8")
    main_source = cli_source.split("def main(", maxsplit=1)[1]
    assert "def _validate_recover_batch_cardinality(" in cli_source
    assert main_source.index(
        "_validate_recover_batch_cardinality(args)"
    ) < main_source.index("TraceBackedMemoryStore.load_json(args.snapshot)")

    assert len(packaged_resources()) == 89
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "VALUES (true, 2)" in _postgres_schema()


def test_docs_publish_recover_attribution_final_delimiter():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
        "Phase 25 design": _doc(
            "superpowers/specs/2026-07-18-snapshot-operations-cli-design.md"
        ),
        "Phase 44 design": _doc(
            "superpowers/specs/"
            "2026-07-22-recover-batch-cardinality-design.md"
        ),
        "Phase 70 design": _doc(
            "superpowers/specs/"
            "2026-07-22-recover-attribution-delimiter-design.md"
        ),
    }
    required_contracts = (
        "recover-batch",
        "decision_id=true|false",
        "final `=`",
        "exit code 2",
        "snapshot version 2",
        "postgresql schema version",
    )
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 70: Recover attribution final delimiter (implemented)"
        in documents["docs/product-program.md"]
    )
    usage_schema = _json_schema("memory_usage_log.schema.json")
    assert usage_schema["properties"]["decision_id"] == {
        "type": "string",
        "pattern": "\\S",
        "minLength": 1,
        "maxLength": 128,
    }
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "VALUES (true, 2)" in _postgres_schema()
    assert len(packaged_resources()) == 89


def test_docs_publish_non_negative_trace_latency_contract():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in (
            "latency_ms",
            "minimum",
            "snapshot version 2",
            "postgresql schema version",
        ):
            assert contract in normalized, f"{name} should publish: {contract}"

    published_contract = " ".join(documents.values()).lower()
    for contract in (
        "state",
        "exit code 3",
        "fresh-install",
        "18",
    ):
        assert contract in published_contract

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 45: Non-negative trace latency (implemented)"
        in documents["docs/product-program.md"]
    )

    store_source = (
        ROOT / "src" / "trace_backed_memory" / "store.py"
    ).read_text(encoding="utf-8")
    assert "if trace.latency_ms < 0:" in store_source
    assert 'raise ValueError("latency_ms must be non-negative")' in store_source

    trace_schema = _json_schema("trace.schema.json")
    assert trace_schema["properties"]["latency_ms"] == {
        "type": ["integer", "null"],
        "minimum": 0,
        "maximum": 2_147_483_647,
    }
    assert trace_schema["properties"]["cost_usd"] == {
        "type": ["number", "null"]
    }

    canonical_trace_schema = (
        ROOT / "schemas" / "trace.schema.json"
    ).read_bytes()
    packaged_trace_schema = (
        ROOT
        / "src"
        / "trace_backed_memory"
        / "_resources"
        / "schemas"
        / "trace.schema.json"
    ).read_bytes()
    assert packaged_trace_schema == canonical_trace_schema

    canonical_postgres = (ROOT / "schemas" / "postgres.sql").read_bytes()
    packaged_postgres = (
        ROOT
        / "src"
        / "trace_backed_memory"
        / "_resources"
        / "schemas"
        / "postgres.sql"
    ).read_bytes()
    assert packaged_postgres == canonical_postgres
    assert b"traces_latency_ms_non_negative" in canonical_postgres
    assert len(packaged_resources()) == 89

    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "VALUES (true, 2)" in _postgres_schema()


def test_docs_publish_public_project_policy_obsolescence_export():
    import trace_backed_memory as tbm
    from trace_backed_memory import lifecycle, packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    for name, document in documents.items():
        assert "obsolete_project_policy" in document, (
            f"{name} should publish the project-policy helper"
        )

    assert "package root" in documents["README.md"]
    assert "package root" in documents["docs/architecture.md"]
    assert "根包" in documents["docs/product.md"]
    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 46: Public project-policy obsolescence export (implemented)"
        in documents["docs/product-program.md"]
    )

    assert tbm.obsolete_project_policy is lifecycle.obsolete_project_policy
    assert "obsolete_project_policy" in tbm.__all__
    assert len(packaged_resources()) == 89

    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "VALUES (true, 2)" in _postgres_schema()


def test_docs_publish_postgres_compatible_trace_latency_range():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in (
            "latency_ms",
            "2,147,483,647",
            "snapshot version 2",
            "postgresql schema version",
        ):
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 47: PostgreSQL-compatible trace latency range (implemented)"
        in documents["docs/product-program.md"]
    )

    store_source = (
        ROOT / "src" / "trace_backed_memory" / "store.py"
    ).read_text(encoding="utf-8")
    assert "TRACE_LATENCY_MAX_MS = 2_147_483_647" in store_source
    assert "if trace.latency_ms > TRACE_LATENCY_MAX_MS:" in store_source
    assert "latency_ms must be at most {TRACE_LATENCY_MAX_MS}" in store_source

    trace_schema = _json_schema("trace.schema.json")
    assert trace_schema["properties"]["latency_ms"] == {
        "type": ["integer", "null"],
        "minimum": 0,
        "maximum": 2_147_483_647,
    }
    assert trace_schema["properties"]["cost_usd"] == {
        "type": ["number", "null"]
    }

    canonical_trace_schema = (
        ROOT / "schemas" / "trace.schema.json"
    ).read_bytes()
    packaged_trace_schema = (
        ROOT
        / "src"
        / "trace_backed_memory"
        / "_resources"
        / "schemas"
        / "trace.schema.json"
    ).read_bytes()
    assert packaged_trace_schema == canonical_trace_schema

    canonical_postgres = (ROOT / "schemas" / "postgres.sql").read_bytes()
    packaged_postgres = (
        ROOT
        / "src"
        / "trace_backed_memory"
        / "_resources"
        / "schemas"
        / "postgres.sql"
    ).read_bytes()
    assert packaged_postgres == canonical_postgres
    assert b"latency_ms INTEGER" in canonical_postgres
    assert b"traces_latency_ms_non_negative" in canonical_postgres
    assert len(packaged_resources()) == 89

    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert "VALUES (true, 2)" in _postgres_schema()


def test_docs_publish_postgres_bounded_load_payloads():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in (
            "64 mib",
            "utf-8",
            "snapshot version 2",
            "postgresql schema version",
        ):
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 48: PostgreSQL bounded load payloads (implemented)"
        in documents["docs/product-program.md"]
    )
    assert "max_record_bytes" in documents["docs/architecture.md"]
    assert "total_bytes" in documents["docs/architecture.md"]

    runtime = (
        ROOT / "src" / "trace_backed_memory" / "postgres.py"
    ).read_text(encoding="utf-8")
    load_runtime = runtime[runtime.index("    def load(self)") :]
    assert "_MEASURE_SNAPSHOT_PAYLOAD_BYTES" in runtime
    assert "pg_catalog.to_jsonb(snapshot_row)" in runtime
    assert "pg_catalog.convert_to(" in runtime
    assert "_POSTGRES_LOAD_MAX_RECORD_BYTES = SNAPSHOT_FILE_MAX_BYTES" in runtime
    assert (
        "_POSTGRES_LOAD_MAX_TOTAL_PAYLOAD_BYTES = SNAPSHOT_FILE_MAX_BYTES"
        in runtime
    )
    assert load_runtime.index("_snapshot_record_counts") < load_runtime.index(
        "_snapshot_payload_sizes"
    )
    assert load_runtime.index("_snapshot_payload_sizes") < load_runtime.index(
        "_load_traces"
    )

    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    postgres_schema = _postgres_schema()
    assert "VALUES (true, 2)" in postgres_schema
    assert "snapshot_payload_bytes" not in postgres_schema
    assert len(packaged_resources()) == 89


def test_docs_publish_portable_nonblank_persisted_strings():
    from trace_backed_memory import packaged_resources

    documents = {
        "README.md": (ROOT / "docs" / "reference.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/product.md": _doc("product.md"),
        "docs/product-program.md": _doc("product-program.md"),
    }
    for name, document in documents.items():
        normalized = " ".join(document.split()).lower()
        for contract in (
            "non-whitespace",
            "snapshot version 2",
            "postgresql schema version",
        ):
            assert contract in normalized, f"{name} should publish: {contract}"

    assert CURRENT_IMPLEMENTED_PHASE in documents["docs/product.md"]
    assert (
        "Phase 49: Portable nonblank persisted strings (implemented)"
        in documents["docs/product-program.md"]
    )
    assert '`pattern: "\\\\S"`' in documents["README.md"]
    assert "default `btrim(text)`" in documents["docs/architecture.md"]
    assert "Direct SQL" in documents["docs/usage-policy.md"]

    store_source = (
        ROOT / "src" / "trace_backed_memory" / "store.py"
    ).read_text(encoding="utf-8")
    policy_source = (
        ROOT / "src" / "trace_backed_memory" / "policy.py"
    ).read_text(encoding="utf-8")
    lifecycle_source = (
        ROOT / "src" / "trace_backed_memory" / "lifecycle.py"
    ).read_text(encoding="utf-8")
    assert "not value.strip()" in store_source
    assert "not memory_id.strip()" in store_source
    assert "not value.strip()" in policy_source
    assert "not value.strip()" in lifecycle_source

    postgres_schema = _postgres_schema()
    assert "CHECK (btrim(trace_id) <> '')" in postgres_schema
    assert "VALUES (true, 2)" in postgres_schema
    snapshot_schema = _json_schema("memory_store_snapshot.schema.json")
    assert snapshot_schema["properties"]["snapshot_version"] == {
        "type": "integer",
        "const": 2,
    }
    assert len(packaged_resources()) == 89


def test_postgres_memory_id_registry_rejects_direct_dml():
    schema = _postgres_schema()

    assert "CREATE FUNCTION protect_memory_id_registry()" in schema
    assert "CREATE TRIGGER memory_ids_reject_direct_dml" in schema
    assert "BEFORE INSERT OR UPDATE OR DELETE ON memory_ids" in schema
    assert "FOR EACH STATEMENT EXECUTE FUNCTION protect_memory_id_registry()" in schema
    assert "TG_OP != 'INSERT' OR pg_trigger_depth() < 2" in schema
    assert "pg_trigger_depth()" in schema
    assert "memory_ids registry does not allow direct" in schema
    assert "REVOKE INSERT, UPDATE, DELETE ON memory_ids FROM PUBLIC" in schema


def test_postgres_runtime_registration_is_narrow_security_definer():
    schema = _postgres_schema()

    assert "SECURITY DEFINER" in schema
    assert "SET search_path = pg_catalog" in schema
    assert "public.memory_ids" in schema
    assert "TG_RELID = 'public.failure_cases'::regclass" in schema
    assert "REVOKE ALL ON FUNCTION register_runtime_memory_id() FROM PUBLIC" in schema


def test_every_postgres_invariant_function_pins_pg_catalog_search_path():
    schema = _postgres_schema()
    expected_functions = {
        "protect_memory_id_registry",
        "protect_trace_record",
        "valid_memory_scope_json",
        "reject_runtime_memory_truncate",
        "register_runtime_memory_id",
        "protect_runtime_memory_identity",
        "protect_failure_case_source",
        "protect_lesson_source",
        "require_failure_case_source_trace",
        "require_verified_lesson_source_case",
        "enforce_failure_case_status_transition",
        "enforce_active_obsolete_status_transition",
        "enforce_failure_case_lesson_lifecycle",
        "jsonb_text_array_has_duplicates",
        "valid_non_empty_text_object",
        "valid_candidate_memory_statuses",
        "require_usage_trace_context",
        "protect_usage_decision_record",
        "require_known_usage_memory_ids",
    }
    function_blocks = {
        name: block
        for name, block in re.findall(
            r"CREATE FUNCTION\s+(\w+)\b(.*?)(?=\nCREATE FUNCTION|\Z)",
            schema,
            re.DOTALL,
        )
    }

    assert expected_functions == set(function_blocks)
    for function_name in sorted(expected_functions):
        block = function_blocks[function_name]
        assert re.search(
            r"\$\$\s+LANGUAGE\s+(?:SQL|plpgsql)(?:\s+IMMUTABLE)?\s+"
            r"(?:SECURITY DEFINER\s+)?"
            r"SET search_path = pg_catalog;",
            block,
        ), function_name


def test_pinned_postgres_functions_schema_qualify_application_tables():
    schema = _postgres_schema()
    application_table = re.compile(
        r"(?<!public\.)\b(?:traces|memory_ids|failure_cases|lessons|"
        r"project_policies|memory_usage_decisions)\b"
    )

    for function_name, block in re.findall(
        r"CREATE FUNCTION\s+(\w+)\b(.*?)(?=\nCREATE FUNCTION|\Z)",
        schema,
        re.DOTALL,
    ):
        body = block.split("$$", 2)[1]
        body_without_literals = re.sub(r"'(?:''|[^'])*'", "''", body)
        assert application_table.search(body_without_literals) is None, function_name


def test_postgres_usage_registry_checks_concrete_runtime_rows():
    schema = _postgres_schema()

    assert "public.memory_ids.memory_kind = 'failure_case'" in schema
    assert "FROM public.failure_cases" in schema
    assert "public.memory_ids.memory_kind = 'lesson'" in schema
    assert "FROM public.lessons" in schema
    assert "public.memory_ids.memory_kind = 'project_policy'" in schema
    assert "FROM public.project_policies" in schema


def test_postgres_status_updates_are_forward_only():
    schema = _postgres_schema()

    assert "CREATE FUNCTION enforce_failure_case_status_transition()" in schema
    assert "CREATE FUNCTION enforce_active_obsolete_status_transition()" in schema
    assert "failure case status transition is not allowed" in schema
    assert "runtime memory status transition is not allowed" in schema
    assert "OLD.status = 'verified' AND NEW.status = 'draft'" in schema
    assert "OLD.status = 'obsolete' AND NEW.status != 'obsolete'" in schema
    for trigger_name in [
        "failure_cases_enforce_forward_status",
        "lessons_enforce_forward_status",
        "project_policies_enforce_forward_status",
    ]:
        assert f"CREATE TRIGGER {trigger_name}" in schema


def test_postgres_active_lesson_parent_check_uses_share_lock():
    schema = _postgres_schema()

    assert "FROM public.failure_cases" in schema
    assert "FOR SHARE" in schema
    assert "FOR KEY SHARE" not in schema
