from __future__ import annotations

from collections.abc import Mapping
from pathlib import Path

from .lifecycle import draft_failure_case
from .models import FailureCase, Trace
from .resources import read_packaged_resource

FailureTaxonomy = dict[str, str]


def load_failure_taxonomy(path: str | Path | None = None) -> FailureTaxonomy:
    if path is None:
        text = read_packaged_resource(
            "memory/failure_taxonomy.yaml"
        ).decode("utf-8")
    else:
        text = Path(path).read_text(encoding="utf-8")
    return _failure_taxonomy_from_yaml(text)


def classify_failure_type(trace: Trace, *, taxonomy: Mapping[str, str] | None = None) -> str:
    text = _trace_text(trace)
    lower_text = text.lower()
    lower_tool_error_text = _tool_error_text(trace).lower()

    if "without retrieving" in lower_text or "required context" in lower_text:
        return _taxonomy_checked("missing_required_context", taxonomy)
    if "invalid argument" in lower_text or "required" in lower_tool_error_text:
        return _taxonomy_checked("invalid_tool_argument", taxonomy)
    if (
        "stale" in lower_text
        or "outdated" in lower_text
        or "old retrieved context" in lower_text
        or "previous commit" in lower_text and "context" in lower_text
    ):
        return _taxonomy_checked("stale_context", taxonomy)
    if "enum" in lower_text or "not allowed" in lower_text or "schema options" in lower_text:
        return _taxonomy_checked("hallucinated_enum_value", taxonomy)
    if "evaluator" in lower_text or "format mismatch" in lower_text or "rubric mismatch" in lower_text:
        return _taxonomy_checked("evaluator_mismatch", taxonomy)
    if (
        "prompt contract" in lower_text
        or "output contract" in lower_text
        or "format" in lower_text
        or "rubric" in lower_text
    ):
        return _taxonomy_checked("prompt_contract_violation", taxonomy)
    if trace.eval_result == "fail":
        return _taxonomy_checked("evaluator_mismatch", taxonomy)
    return _taxonomy_checked("unknown", taxonomy)


def draft_failure_case_from_trace(
    trace: Trace,
    *,
    case_id: str,
    taxonomy: Mapping[str, str] | None = None,
) -> FailureCase:
    failure_type = classify_failure_type(trace, taxonomy=taxonomy)
    return draft_failure_case(
        trace,
        case_id=case_id,
        failure_type=failure_type,
        symptom=_symptom(trace, failure_type),
        root_cause=_root_cause(trace),
    )


def _trace_text(trace: Trace) -> str:
    parts: list[str] = []
    if trace.error:
        parts.append(trace.error)
    for call in trace.tool_calls:
        for key in ("name", "error"):
            value = call.get(key)
            if value is not None:
                parts.append(str(value))
    return " ".join(parts)


def _tool_error_text(trace: Trace) -> str:
    parts: list[str] = []
    for call in trace.tool_calls:
        value = call.get("error")
        if value is not None:
            parts.append(str(value))
    return " ".join(parts)


def _symptom(trace: Trace, failure_type: str) -> str:
    if trace.tool_calls:
        tool_names = [str(call.get("name")) for call in trace.tool_calls if call.get("name")]
        if tool_names:
            return f"{failure_type}: tool call failed for {', '.join(tool_names)}"
    if trace.error:
        return f"{failure_type}: {trace.error}"
    return f"{failure_type}: trace {trace.trace_id} failed"


def _root_cause(trace: Trace) -> str | None:
    if trace.error:
        return trace.error
    for call in trace.tool_calls:
        if call.get("error"):
            return str(call["error"])
    return None


def _taxonomy_checked(failure_type: str, taxonomy: Mapping[str, str] | None) -> str:
    if taxonomy is None or failure_type == "unknown":
        return failure_type
    if failure_type not in taxonomy:
        raise ValueError(f"failure type {failure_type!r} is not present in taxonomy")
    return failure_type


def _failure_taxonomy_from_yaml(text: str) -> FailureTaxonomy:
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("failure taxonomy YAML must not be empty")
    if lines[0] != "failure_types:":
        raise ValueError("failure taxonomy YAML must start with 'failure_types:'")

    taxonomy: FailureTaxonomy = {}
    current_id: str | None = None
    for raw_line in lines[1:]:
        stripped = raw_line.strip()
        if stripped.startswith("- "):
            key, value = _yaml_key_value(stripped[2:])
            if key != "id":
                raise ValueError("failure taxonomy entries must start with id")
            current_id = value
            if not current_id:
                raise ValueError("failure taxonomy id must be non-empty")
            if current_id in taxonomy:
                raise ValueError(f"duplicate failure taxonomy id: {current_id}")
            taxonomy[current_id] = ""
            continue

        if current_id is None:
            raise ValueError("failure taxonomy description must follow an id")
        key, value = _yaml_key_value(stripped)
        if key != "description":
            raise ValueError(f"unsupported failure taxonomy field: {key}")
        if not value:
            raise ValueError(f"failure taxonomy description must be non-empty: {current_id}")
        taxonomy[current_id] = value

    missing_description = [failure_type for failure_type, description in taxonomy.items() if not description]
    if missing_description:
        raise ValueError(f"failure taxonomy entries missing descriptions: {', '.join(missing_description)}")
    return taxonomy


def _yaml_key_value(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise ValueError(f"expected YAML key/value line: {text}")
    key, value = text.split(":", 1)
    return key.strip(), value.strip()
