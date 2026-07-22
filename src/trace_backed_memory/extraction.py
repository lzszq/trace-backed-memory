from __future__ import annotations

from collections.abc import Iterator, Mapping
from pathlib import Path

from ._ingestion import (
    FAILURE_TAXONOMY_FILE_MAX_BYTES,
    FAILURE_TAXONOMY_MAX_RECORDS,
    decode_bounded_utf8,
    read_bounded_utf8,
    validate_non_negative_limit,
)
from .lifecycle import draft_failure_case
from .models import FailureCase, Trace
from .resources import read_packaged_resource

FailureTaxonomy = dict[str, str]
_REQUIRED_TOOL_ARGUMENT_MARKERS = (
    "required argument",
    "required parameter",
    "required field",
    "required property",
)


def load_failure_taxonomy(
    path: str | Path | None = None,
    *,
    max_bytes: int | None = FAILURE_TAXONOMY_FILE_MAX_BYTES,
    max_failure_types: int | None = FAILURE_TAXONOMY_MAX_RECORDS,
) -> FailureTaxonomy:
    validate_non_negative_limit(max_failure_types, "max_failure_types")
    if path is None:
        text = decode_bounded_utf8(
            read_packaged_resource("memory/failure_taxonomy.yaml"),
            max_bytes=max_bytes,
            description="failure taxonomy YAML",
        )
    else:
        text = read_bounded_utf8(
            path,
            max_bytes=max_bytes,
            description="failure taxonomy YAML",
        )
    return _failure_taxonomy_from_yaml(
        text,
        max_failure_types=max_failure_types,
    )


def classify_failure_type(trace: Trace, *, taxonomy: Mapping[str, str] | None = None) -> str:
    text = _trace_text(trace)
    lower_text = text.lower()
    lower_tool_error_texts = tuple(
        error_text.lower() for error_text in _tool_error_texts(trace)
    )

    if "without retrieving" in lower_text or "required context" in lower_text:
        return _taxonomy_checked("missing_required_context", taxonomy)
    if "invalid argument" in lower_text or any(
        marker in lower_tool_error_text
        for lower_tool_error_text in lower_tool_error_texts
        for marker in _REQUIRED_TOOL_ARGUMENT_MARKERS
    ):
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
    parts.extend(_tool_error_texts(trace))
    return " ".join(parts)


def _tool_error_texts(trace: Trace) -> Iterator[str]:
    for record in _tool_records(trace):
        value = record.get("error")
        if value is not None:
            yield str(value)


def _symptom(trace: Trace, failure_type: str) -> str:
    tool_names = [
        str(record["name"])
        for record in trace.tool_calls
        if record.get("name") and record.get("error")
    ]
    if not tool_names:
        tool_names = [
            str(record["name"])
            for record in trace.tool_outputs
            if record.get("name") and record.get("error")
        ]
    if tool_names:
        return f"{failure_type}: tool call failed for {', '.join(tool_names)}"
    if trace.error:
        return f"{failure_type}: {trace.error}"
    return f"{failure_type}: trace {trace.trace_id} failed"


def _root_cause(trace: Trace) -> str | None:
    if trace.error:
        return trace.error
    for record in _tool_records(trace):
        if record.get("error"):
            return str(record["error"])
    return None


def _tool_records(trace: Trace) -> Iterator[dict[str, object]]:
    yield from trace.tool_calls
    yield from trace.tool_outputs


def _taxonomy_checked(failure_type: str, taxonomy: Mapping[str, str] | None) -> str:
    if taxonomy is None or failure_type == "unknown":
        return failure_type
    if failure_type not in taxonomy:
        raise ValueError(f"failure type {failure_type!r} is not present in taxonomy")
    return failure_type


def _failure_taxonomy_from_yaml(
    text: str,
    *,
    max_failure_types: int | None = FAILURE_TAXONOMY_MAX_RECORDS,
) -> FailureTaxonomy:
    failure_type_limit = validate_non_negative_limit(
        max_failure_types,
        "max_failure_types",
    )
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    if not lines:
        raise ValueError("failure taxonomy YAML must not be empty")
    if lines[0] != "failure_types:":
        raise ValueError("failure taxonomy YAML must start with 'failure_types:'")

    taxonomy: FailureTaxonomy = {}
    current_id: str | None = None
    described_ids: set[str] = set()
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
            if (
                failure_type_limit is not None
                and len(taxonomy) >= failure_type_limit
            ):
                raise ValueError(
                    "failure taxonomy YAML contains more than "
                    f"{failure_type_limit} failure types"
                )
            taxonomy[current_id] = ""
            continue

        if current_id is None:
            raise ValueError("failure taxonomy description must follow an id")
        key, value = _yaml_key_value(stripped)
        if key != "description":
            raise ValueError(f"unsupported failure taxonomy field: {key}")
        if current_id in described_ids:
            raise ValueError(
                f"duplicate failure taxonomy description: {current_id}"
            )
        if not value:
            raise ValueError(f"failure taxonomy description must be non-empty: {current_id}")
        taxonomy[current_id] = value
        described_ids.add(current_id)

    missing_description = [failure_type for failure_type, description in taxonomy.items() if not description]
    if missing_description:
        raise ValueError(f"failure taxonomy entries missing descriptions: {', '.join(missing_description)}")
    return taxonomy


def _yaml_key_value(text: str) -> tuple[str, str]:
    if ":" not in text:
        raise ValueError(f"expected YAML key/value line: {text}")
    key, value = text.split(":", 1)
    return key.strip(), value.strip()
