import json
import re
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


def _json_example(name: str) -> dict[str, object]:
    return json.loads((ROOT / "examples" / name).read_text(encoding="utf-8"))


def _json_schema(name: str) -> dict[str, object]:
    schema_path = ROOT / "schemas" / name
    assert schema_path.exists(), f"{name} should exist"
    return json.loads(schema_path.read_text(encoding="utf-8"))


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
        assert set(schema.get("required", [])) == _required_dataclass_fields(model_cls)
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


def test_memory_decision_schema_encodes_use_memory_consistency_rules():
    schema = _json_schema("memory_decision.schema.json")
    all_of = schema.get("allOf")

    assert isinstance(all_of, list)
    assert any("use_memory" in json.dumps(rule) and "allowed_memory_ids" in json.dumps(rule) for rule in all_of)
    assert any("use_memory" in json.dumps(rule) and "recommended_injection" in json.dumps(rule) for rule in all_of)


def test_memory_usage_log_schema_encodes_decision_consistency_rules():
    schema = _json_schema("memory_usage_log.schema.json")
    all_of = schema.get("allOf")

    assert isinstance(all_of, list)
    assert any("recommended_injection" in json.dumps(rule) and "used_memory_ids" in json.dumps(rule) for rule in all_of)
    assert any("memory_caused_failure" in json.dumps(rule) and "eval_result" in json.dumps(rule) for rule in all_of)


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
    restored = TraceBackedMemoryStore.from_snapshot(
        {
            "traces": [store.to_snapshot()["traces"][0]],
            "failure_cases": [store.to_snapshot()["failure_cases"][0]],
            "lessons": [store.to_snapshot()["lessons"][0]],
            "project_policies": [],
            "usage_logs": [usage_log.__dict__],
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
    assert case.source_trace_id == trace.trace_id
    assert lesson.source_case_id == case.case_id
    assert memory_item_from_project_policy(project_policy).source_policy_id == "project_policy_001"
    assert decision.allowed_memory_ids == ["lesson_001"]
    assert restored.usage_logs == [usage_log]
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
        "source_trace_id TEXT NOT NULL REFERENCES traces(trace_id)",
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


def test_postgres_schema_enforces_verified_case_and_lesson_source_lifecycle():
    failure_cases = _table_definition(_postgres_schema(), "failure_cases")
    schema = _postgres_schema()

    assert "status != 'verified'" in failure_cases
    assert "fix IS NOT NULL" in failure_cases
    assert "fix_commit_sha IS NOT NULL" in failure_cases
    assert "regression_passed" in failure_cases
    assert "CREATE FUNCTION require_verified_lesson_source_case()" in schema
    assert "CREATE TRIGGER lessons_require_verified_source_case" in schema
    assert "status = 'verified'" in schema
    assert "regression_passed" in schema


def test_trace_eval_result_defaults_to_unknown_and_is_required():
    traces = _table_definition(_postgres_schema(), "traces")
    eval_result = _column_definition(traces, "eval_result")

    assert "NOT NULL" in eval_result
    assert "DEFAULT 'unknown'" in eval_result


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
    assert "entry.scope_value #>> '{}' = ''" in schema


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
    assert "FROM memory_ids" in schema
    assert "used memory ids must be present in candidates" in schema
    assert "blocked memory ids must be present in candidates" in schema


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
