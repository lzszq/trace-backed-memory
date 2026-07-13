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
    MemoryUsageLog,
    ProjectPolicy,
    Trace,
    TraceBackedMemoryStore,
    parse_memory_context,
    memory_item_from_project_policy,
    parse_memory_decision,
)
from trace_backed_memory.models import EvalResult, FailureCaseStatus, LessonStatus, MemoryType, Mode

ROOT = Path(__file__).resolve().parents[1]
DRAFT_2020_12 = "https://json-schema.org/draft/2020-12/schema"


def test_postgres_adapter_dependencies_are_optional():
    pyproject = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    project = pyproject["project"]
    extras = project["optional-dependencies"]

    assert project["dependencies"] == []
    assert extras["postgres"] == ["psycopg>=3.2,<4"]
    assert "psycopg[binary]>=3.2,<4" in extras["dev"]


def test_postgres_schema_publishes_adapter_version():
    schema = _postgres_schema()
    assert "CREATE TABLE public.trace_backed_memory_schema" in schema
    assert "schema_version INTEGER NOT NULL CHECK (schema_version > 0)" in schema
    assert (
        "INSERT INTO public.trace_backed_memory_schema(singleton, schema_version)"
        in schema
    )
    assert "VALUES (true, 1)" in schema
    assert "ON public.trace_backed_memory_schema FROM PUBLIC;" in schema


def test_postgres_jsonpath_schema_publishes_supported_version_floor():
    schema = _postgres_schema()
    documents = {
        "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/mvp-roadmap.md": _doc("mvp-roadmap.md"),
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
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    architecture = _doc("architecture.md")
    architecture_contract = " ".join(architecture.split())
    roadmap = _doc("mvp-roadmap.md")
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

    assert "treats traces and usage logs as immutable" in architecture_contract
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
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
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
    assert "PostgreSQL schema version remains 1" in architecture


def test_docs_publish_exact_postgres_transaction_ownership_contract():
    documents = [
        (ROOT / "README.md").read_text(encoding="utf-8"),
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
        "LLM_GATE_PROMPT_MAX_CHARS": "32,000",
        "INJECTION_MAX_MEMORIES": "20",
        "INJECTION_SNIPPET_MAX_CHARS": "12,000",
    }
    for doc_name in ["usage-policy.md", "architecture.md"]:
        document = _doc(doc_name)
        for constant_name, value in expected_limits.items():
            assert constant_name in document
            assert value in document


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
    assert "CHECK (reason ~ '[^[:space:]]')" in _postgres_schema()


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
        "failure_cases",
        "lessons",
        "project_policies",
    ]:
        assert f"CREATE TRIGGER {table_name}_reject_truncate" in schema
        assert f"BEFORE TRUNCATE ON {table_name}" in schema
    assert (
        "REVOKE TRUNCATE ON memory_ids, failure_cases, lessons, project_policies "
        "FROM PUBLIC"
        in schema
    )


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
    assert "CREATE FUNCTION require_verified_lesson_source_case()" in schema
    assert "CREATE TRIGGER lessons_require_verified_source_case" in schema
    assert "IF NEW.status = 'active'" in schema
    assert "BEFORE INSERT OR UPDATE OF source_case_id, status ON lessons" in schema
    assert "status = 'verified'" in schema
    assert "regression_passed" in schema
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
    roadmap = _doc("mvp-roadmap.md")

    assert "shared runtime memory ID namespace" in architecture
    assert "failure cases, lessons, and project policies" in architecture
    assert "duplicate, empty-string, or non-string memory IDs" in architecture
    assert "duplicate, empty-string, or non-string memory ID lists" in roadmap
    assert "empty memory ID lists" not in architecture
    assert "empty memory ID lists" not in roadmap


def test_docs_describe_memory_decision_consistency_contract():
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
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
        "README.md": (ROOT / "README.md").read_text(encoding="utf-8"),
        "docs/architecture.md": _doc("architecture.md"),
        "docs/usage-policy.md": _doc("usage-policy.md"),
        "docs/mvp-roadmap.md": _doc("mvp-roadmap.md"),
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
        "PostgreSQL schema version 1",
        "no new persisted memory fields",
    ]
    for name, document in documents.items():
        normalized = " ".join(document.split())
        for contract in required_contracts:
            assert contract in normalized, f"{name} should publish: {contract}"

    assert (
        "Phase 11: Benchmark example leakage classification (implemented)"
        in documents["docs/mvp-roadmap.md"]
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
        "valid_memory_scope_json",
        "reject_runtime_memory_truncate",
        "register_runtime_memory_id",
        "protect_runtime_memory_identity",
        "require_verified_lesson_source_case",
        "enforce_failure_case_status_transition",
        "enforce_active_obsolete_status_transition",
        "enforce_failure_case_lesson_lifecycle",
        "jsonb_text_array_has_duplicates",
        "valid_non_empty_text_object",
        "valid_candidate_memory_statuses",
        "require_usage_trace_context",
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
