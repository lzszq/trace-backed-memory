import argparse
import json
import os
import subprocess
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from pathlib import Path

import pytest

import trace_backed_memory.cli as cli
from trace_backed_memory import (
    CommitAncestryCaptureError,
    CommitAncestryEvidence,
    FailureCase,
    Lesson,
    MemoryContext,
    MemoryDecision,
    ProjectPolicy,
    Trace,
    TraceBackedMemoryStore,
)


def _pending_run(
    store: TraceBackedMemoryStore,
    suffix: str,
    *,
    output_hash: str | None = None,
    tool_outputs: list[dict[str, object]] | None = None,
    latency_ms: int | None = None,
    cost_usd: float | None = None,
    error: str | None = None,
    trace_uri: str | None = None,
) -> tuple[Trace, str]:
    trace = store.record_trace(
        Trace(
            trace_id=f"trace_cli_{suffix}",
            run_id=f"run_cli_{suffix}",
            commit_sha="commit_cli",
            repo="repo_cli",
            tenant="tenant_cli",
            eval_result="unknown",
            output_hash=output_hash,
            tool_outputs=[] if tool_outputs is None else tool_outputs,
            latency_ms=latency_ms,
            cost_usd=cost_usd,
            error=error,
            trace_uri=trace_uri,
        )
    )
    log = store.log_decision(
        trace.run_id,
        MemoryContext(
            mode="repair",
            repo="repo_cli",
            tenant="tenant_cli",
            commit_sha="commit_cli",
        ),
        [],
        MemoryDecision(
            use_memory=False,
            allowed_memory_ids=[],
            blocked_memory_ids=[],
            reason="no applicable memory",
            risk="none",
            recommended_injection="none",
        ),
    )
    return trace, log.decision_id


def _pending_memory_use_run(
    store: TraceBackedMemoryStore,
    suffix: str,
) -> tuple[Trace, str]:
    source = store.record_trace(
        Trace(
            trace_id=f"trace_cli_source_{suffix}",
            run_id=f"run_cli_source_{suffix}",
            commit_sha="commit_cli",
            repo="repo_cli",
            tenant="tenant_cli",
            eval_result="fail",
        )
    )
    case = store.add_failure_case(
        FailureCase(
            case_id=f"case_cli_{suffix}",
            source_trace_id=source.trace_id,
            commit_sha=source.commit_sha,
            failure_type="executor_failure",
            symptom="executor timed out",
            fix="bound executor runtime",
            fix_commit_sha="commit_cli_fix",
            regression_passed=True,
            root_cause="executor runtime was unbounded",
            reviewed_by="test-reviewer",
            reviewed_at="2026-07-22T00:00:00Z",
            status="verified",
        )
    )
    lesson = store.add_lesson(
        Lesson(
            lesson_id=f"lesson_cli_{suffix}",
            source_case_id=case.case_id,
            lesson_text="Bound executor runtime before retrying.",
            memory_type="procedural",
            scope={"repo": "repo_cli", "tenant": "tenant_cli"},
        )
    )
    trace = store.record_trace(
        Trace(
            trace_id=f"trace_cli_{suffix}",
            run_id=f"run_cli_{suffix}",
            commit_sha="commit_cli",
            repo="repo_cli",
            tenant="tenant_cli",
            eval_result="unknown",
        )
    )
    log = store.log_decision(
        trace.run_id,
        MemoryContext(
            mode="repair",
            repo="repo_cli",
            tenant="tenant_cli",
            commit_sha="commit_cli",
        ),
        [lesson.lesson_id],
        MemoryDecision(
            use_memory=True,
            allowed_memory_ids=[lesson.lesson_id],
            blocked_memory_ids=[],
            reason="directly relevant",
            risk="low",
            recommended_injection="short_summary",
        ),
    )
    return trace, log.decision_id


def _snapshot_with_states(
    tmp_path: Path,
    *states: str,
) -> tuple[Path, dict[str, str]]:
    tmp_path.mkdir(parents=True, exist_ok=True)
    store = TraceBackedMemoryStore()
    decision_ids: dict[str, str] = {}
    for index, state in enumerate(states, start=1):
        trace, decision_id = _pending_run(store, f"{index}_{state}")
        decision_ids[state] = decision_id
        if state == "trace_only_pass":
            store.complete_trace(trace.trace_id, eval_result="pass")
        elif state == "trace_only_fail":
            store.complete_trace(trace.trace_id, eval_result="fail")
        elif state == "decision_only_pass":
            store.record_decision_outcome(decision_id, "pass")
        elif state == "decision_only_error":
            store.record_decision_outcome(decision_id, "error")
        elif state == "complete":
            store.complete_memory_run(
                trace_id=trace.trace_id,
                decision_id=decision_id,
                eval_result="pass",
            )
        elif state == "conflict":
            store.complete_trace(trace.trace_id, eval_result="pass")
            store.record_decision_outcome(decision_id, "error")
        elif state != "pending":
            raise AssertionError(f"unknown CLI test state: {state}")

    path = tmp_path / "store.snapshot.json"
    store.save_json(path)
    return path, decision_ids


def _run(capsys, *args: str) -> tuple[int, object | None, object | None]:
    exit_code = cli.main(list(args))
    captured = capsys.readouterr()
    stdout = json.loads(captured.out) if captured.out else None
    stderr = json.loads(captured.err) if captured.err else None
    return exit_code, stdout, stderr


def _write_measurements(path: Path, measurements: object) -> Path:
    path.write_text(json.dumps(measurements), encoding="utf-8")
    return path


def _write_obsolescence_requests(path: Path, requests: object) -> Path:
    path.write_text(json.dumps(requests), encoding="utf-8")
    return path


def _pr_report_snapshot(tmp_path: Path) -> Path:
    store = TraceBackedMemoryStore()
    for endpoint in ("old", "new"):
        trace = store.record_trace(
            Trace(
                trace_id=f"trace_cli_pr_{endpoint}",
                run_id=f"run_cli_pr_{endpoint}",
                commit_sha=f"source-{endpoint}",
                repo="repo_cli",
                tenant="tenant_cli",
                model=f"model-{endpoint}",
                eval_result="fail",
                tool_calls=[{"name": "search_docs"}],
                trace_uri=f"trace://cli-pr-{endpoint}",
            )
        )
        store.add_failure_case(
            FailureCase(
                case_id=f"case_cli_{endpoint}",
                source_trace_id=trace.trace_id,
                commit_sha=trace.commit_sha,
                failure_type="invalid_tool_argument",
                symptom="search_docs rejected the request",
                fix="validate search_docs arguments",
                fix_commit_sha=f"fix-{endpoint}",
                regression_passed=True,
                root_cause="search_docs arguments were not validated",
                reviewed_by="test-reviewer",
                reviewed_at="2026-07-22T00:00:00Z",
                status="verified",
            )
        )
    path = tmp_path / "pr-report.snapshot.json"
    store.save_json(path)
    return path


def _pr_report_documents(
    tmp_path: Path,
    *,
    context: object | None = None,
    change_set: object | None = None,
) -> tuple[Path, Path]:
    context_payload = (
        {
            "mode": "regression",
            "repo": "repo_cli",
            "tenant": "tenant_cli",
            "commit_sha": "current-pr-head",
            "tool": "search_docs",
            "model": "model-new",
            "failure_type": "invalid_tool_argument",
        }
        if context is None
        else context
    )
    change_set_payload = (
        {
            "field_changes": [
                {
                    "field_name": "model",
                    "old_value": "model-old",
                    "new_value": "model-new",
                }
            ]
        }
        if change_set is None
        else change_set
    )
    context_path = _write_measurements(
        tmp_path / "pr-context.json",
        context_payload,
    )
    change_set_path = _write_measurements(
        tmp_path / "pr-change-set.json",
        change_set_payload,
    )
    return context_path, change_set_path


def _lesson_portability_store(
    *,
    include_lessons: bool,
) -> tuple[TraceBackedMemoryStore, tuple[Lesson, Lesson]]:
    store = TraceBackedMemoryStore()
    trace = store.record_trace(
        Trace(
            trace_id="trace_cli_lessons",
            run_id="run_cli_lessons",
            commit_sha="commit_cli_lessons",
            repo="repo_cli",
            tenant="tenant_cli",
            eval_result="fail",
        )
    )
    case = store.add_failure_case(
        FailureCase(
            case_id="case_cli_lessons",
            source_trace_id=trace.trace_id,
            commit_sha=trace.commit_sha,
            failure_type="executor_failure",
            symptom="executor timed out",
            fix="bound executor runtime",
            fix_commit_sha="commit_cli_lessons_fix",
            regression_passed=True,
            root_cause="executor runtime was unbounded",
            reviewed_by="test-reviewer",
            reviewed_at="2026-07-22T00:00:00Z",
            status="verified",
        )
    )
    active_lessons = (
        Lesson(
            lesson_id="lesson_cli_export_first",
            source_case_id=case.case_id,
            lesson_text="Bound executor runtime.\n\nRetry only after cleanup.",
            memory_type="procedural",
            scope={"repo": "repo_cli", "tenant": "tenant_cli"},
        ),
        Lesson(
            lesson_id="lesson_cli_export_second",
            source_case_id=case.case_id,
            lesson_text="Record timeout evidence before retrying.",
            memory_type="semantic",
            scope={"repo": "repo_cli", "tenant": "tenant_cli"},
            confidence=0.8,
        ),
    )
    if include_lessons:
        for lesson in active_lessons:
            store.add_lesson(lesson)
        store.add_lesson(
            Lesson(
                lesson_id="lesson_cli_export_obsolete",
                source_case_id=case.case_id,
                lesson_text="Retry immediately.",
                memory_type="procedural",
                scope={"repo": "repo_cli", "tenant": "tenant_cli"},
                status="obsolete",
            )
        )
    return store, active_lessons


def _obsolescence_store() -> tuple[
    TraceBackedMemoryStore,
    dict[str, object],
]:
    store, dependent_lessons = _lesson_portability_store(
        include_lessons=True
    )
    source_case_id = dependent_lessons[0].source_case_id
    other_trace = store.record_trace(
        Trace(
            trace_id="trace_cli_obsolete_other",
            run_id="run_cli_obsolete_other",
            commit_sha="commit_cli_obsolete_other",
            repo="repo_cli",
            tenant="tenant_cli",
            eval_result="fail",
        )
    )
    other_case = store.add_failure_case(
        FailureCase(
            case_id="case_cli_obsolete_other",
            source_trace_id=other_trace.trace_id,
            commit_sha=other_trace.commit_sha,
            failure_type="executor_failure",
            symptom="unrelated executor failure",
            fix="bound another executor",
            fix_commit_sha="commit_cli_obsolete_other_fix",
            regression_passed=True,
            root_cause="the other executor runtime was unbounded",
            reviewed_by="test-reviewer",
            reviewed_at="2026-07-22T00:00:00Z",
            status="verified",
        )
    )
    unrelated_lesson = store.add_lesson(
        Lesson(
            lesson_id="lesson_cli_obsolete_unrelated",
            source_case_id=other_case.case_id,
            lesson_text="Keep unrelated executor guidance active.",
            memory_type="procedural",
            scope={"repo": "repo_cli", "tenant": "tenant_cli"},
        )
    )
    active_policy = store.add_project_policy(
        ProjectPolicy(
            policy_id="policy_cli_obsolete_active",
            policy_text="Record executor timeout evidence.",
            scope={"repo": "repo_cli"},
        )
    )
    obsolete_policy = store.add_project_policy(
        ProjectPolicy(
            policy_id="policy_cli_obsolete_existing",
            policy_text="Retry every executor immediately.",
            scope={"repo": "repo_cli"},
            status="obsolete",
        )
    )
    draft_trace = store.record_trace(
        Trace(
            trace_id="trace_cli_obsolete_draft",
            run_id="run_cli_obsolete_draft",
            commit_sha="commit_cli_obsolete_draft",
            repo="repo_cli",
            tenant="tenant_cli",
            eval_result="fail",
        )
    )
    draft_case = store.add_failure_case(
        FailureCase(
            case_id="case_cli_obsolete_draft",
            source_trace_id=draft_trace.trace_id,
            commit_sha=draft_trace.commit_sha,
            failure_type="executor_failure",
            symptom="draft executor finding",
        )
    )
    return store, {
        "case_id": source_case_id,
        "dependent_lesson_ids": [
            lesson.lesson_id for lesson in dependent_lessons
        ],
        "existing_obsolete_lesson_id": "lesson_cli_export_obsolete",
        "unrelated_lesson_id": unrelated_lesson.lesson_id,
        "active_policy_id": active_policy.policy_id,
        "obsolete_policy_id": obsolete_policy.policy_id,
        "draft_case_id": draft_case.case_id,
    }


def test_cli_resource_commands_list_read_and_export(tmp_path, capsys):
    code, payload, error = _run(capsys, "resource", "list")

    assert code == 0
    assert error is None
    assert len(payload["resources"]) == 89
    names = [item["name"] for item in payload["resources"]]
    assert names == sorted(names)
    assert "schemas/postgres.sql" in names
    assert "schemas/sqlite.sql" in names

    code, payload, error = _run(
        capsys,
        "resource",
        "read",
        "memory/failure_taxonomy.yaml",
    )

    assert code == 0
    assert error is None
    assert payload["resource"]["kind"] == "memory"
    assert payload["text"] == (
        Path(__file__).resolve().parents[1]
        / "memory"
        / "failure_taxonomy.yaml"
    ).read_bytes().decode("utf-8")

    destination = tmp_path / "postgres.sql"
    code, payload, error = _run(
        capsys,
        "resource",
        "export",
        "schemas/postgres.sql",
        str(destination),
    )

    assert code == 0
    assert error is None
    assert payload["destination"] == str(destination)
    assert payload["overwrite"] is False
    assert payload["resource"]["name"] == "schemas/postgres.sql"
    assert destination.read_bytes() == (
        Path(__file__).resolve().parents[1] / "schemas" / "postgres.sql"
    ).read_bytes()

    destination.write_text("caller-owned", encoding="utf-8")
    code, payload, error = _run(
        capsys,
        "resource",
        "export",
        "schemas/postgres.sql",
        str(destination),
    )
    assert code == 4
    assert payload is None
    assert error["error"]["kind"] == "write"
    assert destination.read_text(encoding="utf-8") == "caller-owned"

    code, payload, error = _run(
        capsys,
        "resource",
        "export",
        "schemas/postgres.sql",
        str(destination),
        "--overwrite",
    )
    assert code == 0
    assert error is None
    assert payload["overwrite"] is True


def test_cli_lessons_export_is_active_only_and_protects_destination(
    tmp_path,
    capsys,
):
    store, active_lessons = _lesson_portability_store(include_lessons=True)
    snapshot_path = tmp_path / "lessons-export.snapshot.json"
    destination = tmp_path / "lessons.active.yaml"
    store.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()

    code, payload, error = _run(
        capsys,
        "lessons",
        "export",
        str(snapshot_path),
        str(destination),
    )

    assert code == 0
    assert error is None
    assert payload == {
        "destination": str(destination),
        "exported_count": 2,
        "exported_lesson_ids": [
            lesson.lesson_id for lesson in active_lessons
        ],
        "overwrite": False,
    }
    exported = destination.read_text(encoding="utf-8")
    assert "lesson_cli_export_first" in exported
    assert "lesson_cli_export_second" in exported
    assert "lesson_cli_export_obsolete" not in exported
    assert "    lesson_text: |\n" in exported
    assert snapshot_path.read_bytes() == original_snapshot

    destination.write_bytes(b"caller-owned lessons\n")
    code, payload, error = _run(
        capsys,
        "lessons",
        "export",
        str(snapshot_path),
        str(destination),
    )

    assert code == 4
    assert payload is None
    assert error["error"]["kind"] == "write"
    assert destination.read_bytes() == b"caller-owned lessons\n"
    assert snapshot_path.read_bytes() == original_snapshot

    code, payload, error = _run(
        capsys,
        "lessons",
        "export",
        str(snapshot_path),
        str(destination),
        "--overwrite",
    )

    assert code == 0
    assert error is None
    assert payload["overwrite"] is True
    assert "lesson_cli_export_first" in destination.read_text(encoding="utf-8")
    assert snapshot_path.read_bytes() == original_snapshot


def test_cli_lessons_import_is_dry_run_until_write(
    tmp_path,
    capsys,
):
    source, active_lessons = _lesson_portability_store(include_lessons=True)
    yaml_path = tmp_path / "lessons-to-import.yaml"
    source.save_lessons_yaml(yaml_path)
    target, _ = _lesson_portability_store(include_lessons=False)
    snapshot_path = tmp_path / "lessons-import.snapshot.json"
    target.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()

    code, payload, error = _run(
        capsys,
        "lessons",
        "import",
        str(snapshot_path),
        str(yaml_path),
    )

    expected_ids = [lesson.lesson_id for lesson in active_lessons]
    assert code == 0
    assert error is None
    assert payload == {
        "imported_count": 2,
        "imported_lesson_ids": expected_ids,
        "written": False,
    }
    assert snapshot_path.read_bytes() == original_snapshot
    assert TraceBackedMemoryStore.load_json(snapshot_path).lessons == {}

    code, payload, error = _run(
        capsys,
        "lessons",
        "import",
        str(snapshot_path),
        str(yaml_path),
        "--write",
    )

    assert code == 0
    assert error is None
    assert payload == {
        "imported_count": 2,
        "imported_lesson_ids": expected_ids,
        "written": True,
    }
    assert snapshot_path.read_bytes() != original_snapshot
    restored = TraceBackedMemoryStore.load_json(snapshot_path)
    assert list(restored.lessons) == expected_ids
    assert list(restored.lessons.values()) == list(active_lessons)


def test_cli_lessons_import_rejects_obsolete_status_without_writing(
    tmp_path,
    capsys,
):
    source, _active_lessons = _lesson_portability_store(include_lessons=True)
    yaml_path = tmp_path / "obsolete-import.yaml"
    source.save_lessons_yaml(yaml_path)
    yaml_text = yaml_path.read_text(encoding="utf-8")
    prefix, marker, suffix = yaml_text.rpartition('    status: "active"')
    assert marker
    yaml_path.write_text(
        prefix + '    status: "obsolete"' + suffix,
        encoding="utf-8",
    )
    target, _ = _lesson_portability_store(include_lessons=False)
    snapshot_path = tmp_path / "obsolete-import.snapshot.json"
    target.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()

    code, payload, error = _run(
        capsys,
        "lessons",
        "import",
        str(snapshot_path),
        str(yaml_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "active lessons YAML requires status 'active'" in (
        error["error"]["message"]
    )
    assert snapshot_path.read_bytes() == original_snapshot
    assert TraceBackedMemoryStore.load_json(snapshot_path).lessons == {}


def test_cli_lessons_export_empty_store_and_structured_write_failure(
    tmp_path,
    capsys,
    monkeypatch,
):
    snapshot_path = tmp_path / "empty-lessons.snapshot.json"
    TraceBackedMemoryStore().save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()
    destination = tmp_path / "empty-lessons.yaml"

    code, payload, error = _run(
        capsys,
        "lessons",
        "export",
        str(snapshot_path),
        str(destination),
    )

    assert code == 0
    assert error is None
    assert payload == {
        "destination": str(destination),
        "exported_count": 0,
        "exported_lesson_ids": [],
        "overwrite": False,
    }
    assert destination.read_bytes() == b"lessons: []\n"

    def reject_export(_store, _destination, *, overwrite=True):
        raise OSError("injected lesson export failure")

    monkeypatch.setattr(
        TraceBackedMemoryStore,
        "save_lessons_yaml",
        reject_export,
    )
    failed_destination = tmp_path / "failed-lessons.yaml"
    code, payload, error = _run(
        capsys,
        "lessons",
        "export",
        str(snapshot_path),
        str(failed_destination),
    )

    assert code == 4
    assert payload is None
    assert error["error"] == {
        "kind": "write",
        "message": "injected lesson export failure",
        "type": "OSError",
    }
    assert not failed_destination.exists()
    assert snapshot_path.read_bytes() == original_snapshot


def test_cli_lessons_export_cannot_replace_snapshot_through_an_alias(
    tmp_path,
    capsys,
):
    store, _active_lessons = _lesson_portability_store(include_lessons=True)
    snapshot_path = tmp_path / "protected.snapshot.json"
    alias_path = tmp_path / "protected.snapshot.alias"
    store.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()
    os.link(snapshot_path, alias_path)

    for destination in (snapshot_path, alias_path):
        code, payload, error = _run(
            capsys,
            "lessons",
            "export",
            str(snapshot_path),
            str(destination),
            "--overwrite",
        )

        assert code == 2
        assert payload is None
        assert error["error"]["kind"] == "input"
        assert "destination must differ from snapshot" in (
            error["error"]["message"]
        )
        assert snapshot_path.read_bytes() == original_snapshot
        assert alias_path.read_bytes() == original_snapshot


def test_cli_lessons_import_empty_document_is_valid_no_op(tmp_path, capsys):
    target, _ = _lesson_portability_store(include_lessons=False)
    snapshot_path = tmp_path / "empty-import.snapshot.json"
    target.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()
    yaml_path = tmp_path / "empty-import.yaml"
    yaml_path.write_bytes(b"lessons: []\n")

    code, payload, error = _run(
        capsys,
        "lessons",
        "import",
        str(snapshot_path),
        str(yaml_path),
    )

    assert code == 0
    assert error is None
    assert payload == {
        "imported_count": 0,
        "imported_lesson_ids": [],
        "written": False,
    }
    assert snapshot_path.read_bytes() == original_snapshot


def test_cli_lessons_import_rejects_bad_documents_without_writing(
    tmp_path,
    capsys,
):
    target, _ = _lesson_portability_store(include_lessons=False)
    snapshot_path = tmp_path / "bad-import.snapshot.json"
    target.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()
    documents = (
        (
            "invalid-utf8.yaml",
            b"lessons:\n\xff",
            "active lessons YAML must be UTF-8",
        ),
        (
            "wrong-root.yaml",
            b"records: []\n",
            "lessons YAML must start with 'lessons:'",
        ),
        (
            "duplicate-key.yaml",
            (
                "lessons:\n"
                '  - lesson_id: "duplicate-first"\n'
                '    lesson_id: "duplicate-second"\n'
            ).encode("utf-8"),
            "duplicate lesson field: lesson_id",
        ),
    )

    for name, content, message in documents:
        yaml_path = tmp_path / name
        yaml_path.write_bytes(content)
        code, payload, error = _run(
            capsys,
            "lessons",
            "import",
            str(snapshot_path),
            str(yaml_path),
            "--write",
        )

        assert code == 2
        assert payload is None
        assert error["error"]["kind"] == "input"
        assert message in error["error"]["message"]
        assert snapshot_path.read_bytes() == original_snapshot

    missing_path = tmp_path / "missing-lessons.yaml"
    code, payload, error = _run(
        capsys,
        "lessons",
        "import",
        str(snapshot_path),
        str(missing_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "cannot read active lessons YAML file" in error["error"]["message"]
    assert snapshot_path.read_bytes() == original_snapshot


def test_cli_lessons_import_rejects_merge_and_provenance_failures(
    tmp_path,
    capsys,
):
    source, _active_lessons = _lesson_portability_store(include_lessons=True)
    yaml_path = tmp_path / "conflicting-lessons.yaml"
    source.save_lessons_yaml(yaml_path)

    conflicting_snapshot = tmp_path / "conflicting.snapshot.json"
    source.save_json(conflicting_snapshot)
    conflicting_original = conflicting_snapshot.read_bytes()
    code, payload, error = _run(
        capsys,
        "lessons",
        "import",
        str(conflicting_snapshot),
        str(yaml_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "duplicate lesson_id" in error["error"]["message"]
    assert conflicting_snapshot.read_bytes() == conflicting_original

    empty_snapshot = tmp_path / "missing-provenance.snapshot.json"
    TraceBackedMemoryStore().save_json(empty_snapshot)
    empty_original = empty_snapshot.read_bytes()
    code, payload, error = _run(
        capsys,
        "lessons",
        "import",
        str(empty_snapshot),
        str(yaml_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "source_case_id" in error["error"]["message"]
    assert empty_snapshot.read_bytes() == empty_original


def test_cli_lessons_import_enforces_fixed_byte_and_record_budgets(
    tmp_path,
    capsys,
):
    target, _ = _lesson_portability_store(include_lessons=False)
    snapshot_path = tmp_path / "bounded-import.snapshot.json"
    target.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()

    oversized_path = tmp_path / "oversized-lessons.yaml"
    oversized_path.write_bytes(b"lessons: []\n" + b" " * (8 * 1024 * 1024))
    code, payload, error = _run(
        capsys,
        "lessons",
        "import",
        str(snapshot_path),
        str(oversized_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "active lessons YAML file exceeds maximum size" in (
        error["error"]["message"]
    )
    assert snapshot_path.read_bytes() == original_snapshot

    too_many_path = tmp_path / "too-many-lessons.yaml"
    too_many_path.write_text(
        "lessons:\n"
        + "".join(
            f'  - lesson_id: "lesson_{index}"\n'
            for index in range(10_001)
        ),
        encoding="utf-8",
    )
    code, payload, error = _run(
        capsys,
        "lessons",
        "import",
        str(snapshot_path),
        str(too_many_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "more than 10000 records" in error["error"]["message"]
    assert snapshot_path.read_bytes() == original_snapshot


def test_cli_lessons_commands_reject_the_other_publication_flag(
    tmp_path,
    capsys,
):
    snapshot_path = tmp_path / "flag-shape.snapshot.json"
    TraceBackedMemoryStore().save_json(snapshot_path)
    yaml_path = tmp_path / "flag-shape.yaml"
    yaml_path.write_bytes(b"lessons: []\n")

    code, payload, error = _run(
        capsys,
        "lessons",
        "export",
        str(snapshot_path),
        str(tmp_path / "destination.yaml"),
        "--write",
    )
    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "--write" in error["error"]["message"]

    code, payload, error = _run(
        capsys,
        "lessons",
        "import",
        str(snapshot_path),
        str(yaml_path),
        "--overwrite",
    )
    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "--overwrite" in error["error"]["message"]


def test_cli_obsolete_failure_case_previews_and_writes_exact_cascade(
    tmp_path,
    capsys,
    monkeypatch,
):
    store, records = _obsolescence_store()
    snapshot_path = tmp_path / "obsolete-case.snapshot.json"
    store.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()
    calls = []
    original_obsolete = TraceBackedMemoryStore.obsolete_failure_case

    def tracked_obsolete(self, case_id):
        calls.append(case_id)
        return original_obsolete(self, case_id)

    monkeypatch.setattr(
        TraceBackedMemoryStore,
        "obsolete_failure_case",
        tracked_obsolete,
    )

    command = (
        "obsolete",
        str(snapshot_path),
        "failure-case",
        records["case_id"],
    )
    code, payload, error = _run(capsys, *command)

    expected_cascade = sorted(records["dependent_lesson_ids"])
    assert code == 0
    assert error is None
    expected_payload = {
        "cascaded_count": 2,
        "cascaded_lesson_ids": expected_cascade,
        "changed": True,
        "memory_id": records["case_id"],
        "memory_kind": "failure_case",
        "previous_status": "verified",
        "status": "obsolete",
        "written": False,
    }
    assert payload == expected_payload
    assert calls == [records["case_id"]]
    assert snapshot_path.read_bytes() == original_snapshot

    code, payload, error = _run(capsys, *command, "--write")

    assert code == 0
    assert error is None
    assert payload == {**expected_payload, "written": True}
    assert calls == [records["case_id"], records["case_id"]]
    assert snapshot_path.read_bytes() != original_snapshot
    restored = TraceBackedMemoryStore.load_json(snapshot_path)
    assert restored.failure_cases[records["case_id"]].status == "obsolete"
    assert all(
        restored.lessons[lesson_id].status == "obsolete"
        for lesson_id in records["dependent_lesson_ids"]
    )
    assert restored.lessons[records["unrelated_lesson_id"]].status == "active"
    assert (
        restored.lessons[records["existing_obsolete_lesson_id"]].status
        == "obsolete"
    )

    written_snapshot = snapshot_path.read_bytes()
    code, payload, error = _run(capsys, *command)

    assert code == 0
    assert error is None
    assert payload == {
        "cascaded_count": 0,
        "cascaded_lesson_ids": [],
        "changed": False,
        "memory_id": records["case_id"],
        "memory_kind": "failure_case",
        "previous_status": "obsolete",
        "status": "obsolete",
        "written": False,
    }
    assert snapshot_path.read_bytes() == written_snapshot


@pytest.mark.parametrize(
    ("memory_kind", "record_key", "collection_name"),
    [
        ("lesson", "dependent_lesson_ids", "lessons"),
        ("project-policy", "active_policy_id", "project_policies"),
    ],
)
def test_cli_obsolete_lesson_and_policy_are_independent_transitions(
    tmp_path,
    capsys,
    memory_kind,
    record_key,
    collection_name,
):
    store, records = _obsolescence_store()
    memory_id = records[record_key]
    if type(memory_id) is list:
        memory_id = memory_id[0]
    snapshot_path = tmp_path / f"obsolete-{memory_kind}.snapshot.json"
    store.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()

    command = (
        "obsolete",
        str(snapshot_path),
        memory_kind,
        memory_id,
    )
    code, payload, error = _run(capsys, *command)

    assert code == 0
    assert error is None
    assert payload == {
        "cascaded_count": 0,
        "cascaded_lesson_ids": [],
        "changed": True,
        "memory_id": memory_id,
        "memory_kind": memory_kind.replace("-", "_"),
        "previous_status": "active",
        "status": "obsolete",
        "written": False,
    }
    assert snapshot_path.read_bytes() == original_snapshot

    code, payload, error = _run(capsys, *command, "--write")

    assert code == 0
    assert error is None
    assert payload["written"] is True
    restored = TraceBackedMemoryStore.load_json(snapshot_path)
    assert getattr(restored, collection_name)[memory_id].status == "obsolete"


def test_cli_obsolete_draft_case_and_existing_records_are_idempotent(
    tmp_path,
    capsys,
):
    store, records = _obsolescence_store()
    snapshot_path = tmp_path / "obsolete-idempotent.snapshot.json"
    store.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()

    code, payload, error = _run(
        capsys,
        "obsolete",
        str(snapshot_path),
        "failure-case",
        records["draft_case_id"],
    )

    assert code == 0
    assert error is None
    assert payload["previous_status"] == "draft"
    assert payload["status"] == "obsolete"
    assert payload["changed"] is True
    assert payload["cascaded_lesson_ids"] == []
    assert snapshot_path.read_bytes() == original_snapshot

    for memory_kind, memory_id in (
        ("lesson", records["existing_obsolete_lesson_id"]),
        ("project-policy", records["obsolete_policy_id"]),
    ):
        code, payload, error = _run(
            capsys,
            "obsolete",
            str(snapshot_path),
            memory_kind,
            memory_id,
        )

        assert code == 0
        assert error is None
        assert payload["previous_status"] == "obsolete"
        assert payload["status"] == "obsolete"
        assert payload["changed"] is False
        assert payload["cascaded_count"] == 0
        assert payload["written"] is False
        assert snapshot_path.read_bytes() == original_snapshot


@pytest.mark.parametrize(
    ("memory_kind", "memory_id", "message"),
    [
        ("failure-case", "missing_case", "unknown failure case ID"),
        ("lesson", "missing_lesson", "unknown lesson ID"),
        ("project-policy", "missing_policy", "unknown project policy ID"),
        ("lesson", "", "lesson ID must be a non-empty string"),
        ("project-policy", "x" * 129, "ID must be at most 128 characters"),
    ],
)
def test_cli_obsolete_rejects_unknown_or_invalid_ids_without_writing(
    tmp_path,
    capsys,
    memory_kind,
    memory_id,
    message,
):
    store, _records = _obsolescence_store()
    snapshot_path = tmp_path / f"unknown-{memory_kind}.snapshot.json"
    store.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()

    code, payload, error = _run(
        capsys,
        "obsolete",
        str(snapshot_path),
        memory_kind,
        memory_id,
        "--write",
    )

    assert code == 3
    assert payload is None
    assert error["error"]["kind"] == "state"
    assert message in error["error"]["message"]
    assert snapshot_path.read_bytes() == original_snapshot


def test_cli_obsolete_rejects_existing_id_under_the_wrong_memory_kind(
    tmp_path,
    capsys,
):
    store, records = _obsolescence_store()
    case_id = records["case_id"]
    snapshot_path = tmp_path / "obsolete-wrong-kind.snapshot.json"
    store.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()

    code, payload, error = _run(
        capsys,
        "obsolete",
        str(snapshot_path),
        "lesson",
        case_id,
        "--write",
    )

    assert code == 3
    assert payload is None
    assert error["error"]["kind"] == "state"
    assert f"unknown lesson ID: {case_id}" in error["error"]["message"]
    assert snapshot_path.read_bytes() == original_snapshot


def test_cli_obsolete_escapes_control_characters_in_error_json(
    tmp_path,
    capsys,
):
    snapshot_path = tmp_path / "obsolete-control-id.snapshot.json"
    TraceBackedMemoryStore().save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()
    memory_id = "missing\n\u96ea"

    code = cli.main(
        [
            "obsolete",
            str(snapshot_path),
            "lesson",
            memory_id,
            "--write",
        ]
    )
    captured = capsys.readouterr()

    assert code == 3
    assert captured.out == ""
    assert captured.err.count("\n") == 1
    assert "\\n" in captured.err
    assert memory_id in json.loads(captured.err)["error"]["message"]
    assert snapshot_path.read_bytes() == original_snapshot


def test_cli_obsolete_rejects_unsupported_kind_as_input(tmp_path, capsys):
    snapshot_path = tmp_path / "invalid-kind.snapshot.json"
    TraceBackedMemoryStore().save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()

    code, payload, error = _run(
        capsys,
        "obsolete",
        str(snapshot_path),
        "trace",
        "trace_001",
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "invalid choice" in error["error"]["message"]
    assert snapshot_path.read_bytes() == original_snapshot


def test_cli_obsolete_batch_previews_and_writes_one_atomic_store_call(
    tmp_path,
    capsys,
    monkeypatch,
):
    store, records = _obsolescence_store()
    snapshot_path = tmp_path / "obsolete-batch.snapshot.json"
    store.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()
    requests = [
        {
            "memory_kind": "project_policy",
            "memory_id": records["active_policy_id"],
        },
        {"memory_kind": "failure_case", "memory_id": records["case_id"]},
        {
            "memory_kind": "lesson",
            "memory_id": records["dependent_lesson_ids"][0],
        },
        {
            "memory_kind": "project_policy",
            "memory_id": records["obsolete_policy_id"],
        },
    ]
    requests_path = _write_obsolescence_requests(
        tmp_path / "obsolete-requests.json",
        requests,
    )
    calls = []
    original_batch = TraceBackedMemoryStore.obsolete_memories

    def tracked_batch(current_store, current_requests):
        calls.append(current_requests)
        return original_batch(current_store, current_requests)

    monkeypatch.setattr(
        TraceBackedMemoryStore,
        "obsolete_memories",
        tracked_batch,
    )
    command = (
        "obsolete-batch",
        str(snapshot_path),
        str(requests_path),
    )

    code, payload, error = _run(capsys, *command)

    expected_cascade = sorted(records["dependent_lesson_ids"])
    expected_results = [
        {
            "changed": True,
            "memory_id": records["active_policy_id"],
            "memory_kind": "project_policy",
            "previous_status": "active",
            "status": "obsolete",
        },
        {
            "changed": True,
            "memory_id": records["case_id"],
            "memory_kind": "failure_case",
            "previous_status": "verified",
            "status": "obsolete",
        },
        {
            "changed": True,
            "memory_id": records["dependent_lesson_ids"][0],
            "memory_kind": "lesson",
            "previous_status": "active",
            "status": "obsolete",
        },
        {
            "changed": False,
            "memory_id": records["obsolete_policy_id"],
            "memory_kind": "project_policy",
            "previous_status": "obsolete",
            "status": "obsolete",
        },
    ]
    assert code == 0
    assert error is None
    assert payload == {
        "affected_count": 4,
        "cascaded_count": 2,
        "cascaded_lesson_ids": expected_cascade,
        "changed_count": 3,
        "requested_count": 4,
        "results": expected_results,
        "written": False,
    }
    assert len(calls) == 1
    assert [request.memory_id for request in calls[0]] == [
        item["memory_id"] for item in requests
    ]
    assert snapshot_path.read_bytes() == original_snapshot

    code, payload, error = _run(capsys, *command, "--write")

    assert code == 0
    assert error is None
    assert payload == {
        "affected_count": 4,
        "cascaded_count": 2,
        "cascaded_lesson_ids": expected_cascade,
        "changed_count": 3,
        "requested_count": 4,
        "results": expected_results,
        "written": True,
    }
    assert len(calls) == 2
    restored = TraceBackedMemoryStore.load_json(snapshot_path)
    assert restored.failure_cases[records["case_id"]].status == "obsolete"
    assert all(
        restored.lessons[lesson_id].status == "obsolete"
        for lesson_id in records["dependent_lesson_ids"]
    )
    assert restored.project_policies[records["active_policy_id"]].status == "obsolete"
    assert restored.lessons[records["unrelated_lesson_id"]].status == "active"

    written_snapshot = snapshot_path.read_bytes()
    code, payload, error = _run(capsys, *command)

    assert code == 0
    assert error is None
    assert payload["affected_count"] == 0
    assert payload["cascaded_lesson_ids"] == []
    assert payload["changed_count"] == 0
    assert all(result["changed"] is False for result in payload["results"])
    assert snapshot_path.read_bytes() == written_snapshot


@pytest.mark.parametrize(
    ("manifest", "message"),
    [
        ({}, "must be a non-empty array"),
        ([], "must be a non-empty array"),
        ([[]], "request 1 must be an object"),
        ([{}], "request 1 missing required field: memory_kind"),
        (
            [{"memory_kind": "lesson", "memory_id": "lesson_001", "extra": 1}],
            "request 1 has unknown field: extra",
        ),
        (
            [{"memory_kind": 1, "memory_id": "lesson_001"}],
            "request 1 memory_kind must be a string",
        ),
        (
            [{"memory_kind": "trace", "memory_id": "trace_001"}],
            "request 1 has unsupported memory_kind: trace",
        ),
        (
            [{"memory_kind": "lesson", "memory_id": 1}],
            "request 1 memory_id must be a string",
        ),
    ],
)
def test_cli_obsolete_batch_rejects_malformed_manifests_without_writing(
    tmp_path,
    capsys,
    manifest,
    message,
):
    snapshot_path = tmp_path / "obsolete-batch-malformed.snapshot.json"
    TraceBackedMemoryStore().save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()
    requests_path = _write_obsolescence_requests(
        tmp_path / "obsolete-batch-malformed.json",
        manifest,
    )

    code, payload, error = _run(
        capsys,
        "obsolete-batch",
        str(snapshot_path),
        str(requests_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert message in error["error"]["message"]
    assert snapshot_path.read_bytes() == original_snapshot


@pytest.mark.parametrize(
    ("requests", "message"),
    [
        (
            [
                {"memory_kind": "lesson", "memory_id": "lesson_missing"},
            ],
            "unknown lesson ID",
        ),
        (
            [
                {"memory_kind": "failure_case", "memory_id": "case_cli_lessons"},
                {"memory_kind": "lesson", "memory_id": "lesson_missing"},
            ],
            "unknown lesson ID",
        ),
        (
            [
                {
                    "memory_kind": "failure_case",
                    "memory_id": "case_cli_lessons",
                },
                {
                    "memory_kind": "failure_case",
                    "memory_id": "case_cli_lessons",
                },
            ],
            "unique memory_ids",
        ),
        (
            [{"memory_kind": "lesson", "memory_id": ""}],
            "memory_id",
        ),
        (
            [{"memory_kind": "project_policy", "memory_id": "x" * 129}],
            "at most 128 characters",
        ),
    ],
)
def test_cli_obsolete_batch_rejects_store_state_atomically(
    tmp_path,
    capsys,
    requests,
    message,
):
    store, _records = _obsolescence_store()
    snapshot_path = tmp_path / "obsolete-batch-state.snapshot.json"
    store.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()
    requests_path = _write_obsolescence_requests(
        tmp_path / "obsolete-batch-state.json",
        requests,
    )

    code, payload, error = _run(
        capsys,
        "obsolete-batch",
        str(snapshot_path),
        str(requests_path),
        "--write",
    )

    assert code == 3
    assert payload is None
    assert error["error"]["kind"] == "state"
    assert message in error["error"]["message"]
    assert snapshot_path.read_bytes() == original_snapshot


def test_cli_obsolete_batch_rejects_duplicate_keys_and_oversized_arrays(
    tmp_path,
    capsys,
):
    snapshot_path = tmp_path / "obsolete-batch-bounds.snapshot.json"
    TraceBackedMemoryStore().save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()
    requests_path = tmp_path / "obsolete-batch-bounds.json"
    requests_path.write_text(
        '[{"memory_kind":"lesson","memory_kind":"failure_case",'
        '"memory_id":"memory_001"}]',
        encoding="utf-8",
    )

    code, payload, error = _run(
        capsys,
        "obsolete-batch",
        str(snapshot_path),
        str(requests_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "duplicate object key: memory_kind" in error["error"]["message"]
    assert snapshot_path.read_bytes() == original_snapshot

    _write_obsolescence_requests(
        requests_path,
        [
            {"memory_kind": "lesson", "memory_id": f"lesson_{index}"}
            for index in range(10_001)
        ],
    )
    code, payload, error = _run(
        capsys,
        "obsolete-batch",
        str(snapshot_path),
        str(requests_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "more than 10000 items" in error["error"]["message"]
    assert snapshot_path.read_bytes() == original_snapshot


def test_cli_obsolete_batch_transition_serialization_and_write_failures(
    tmp_path,
    capsys,
    monkeypatch,
):
    store, records = _obsolescence_store()
    snapshot_path = tmp_path / "obsolete-batch-failures.snapshot.json"
    store.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()
    requests_path = _write_obsolescence_requests(
        tmp_path / "obsolete-batch-failures.json",
        [
            {
                "memory_kind": "project_policy",
                "memory_id": records["active_policy_id"],
            }
        ],
    )
    command = (
        "obsolete-batch",
        str(snapshot_path),
        str(requests_path),
        "--write",
    )

    def reject_transition(_store, _requests):
        raise ValueError("injected batch obsolescence rejection")

    monkeypatch.setattr(
        TraceBackedMemoryStore,
        "obsolete_memories",
        reject_transition,
    )
    code, payload, error = _run(capsys, *command)

    assert code == 3
    assert payload is None
    assert error["error"]["kind"] == "state"
    assert "injected batch obsolescence rejection" in error["error"]["message"]
    assert snapshot_path.read_bytes() == original_snapshot

    monkeypatch.undo()
    real_json_text = cli._json_text

    def reject_result(value):
        if type(value) is dict and "requested_count" in value:
            raise TypeError("injected batch obsolescence serialization failure")
        return real_json_text(value)

    monkeypatch.setattr(cli, "_json_text", reject_result)
    code, payload, error = _run(capsys, *command)

    assert code == 1
    assert payload is None
    assert error["error"]["kind"] == "internal"
    assert "serialization failure" in error["error"]["message"]
    assert snapshot_path.read_bytes() == original_snapshot

    monkeypatch.undo()

    def reject_write(_store, _path):
        raise OSError("injected batch obsolescence write failure")

    monkeypatch.setattr(TraceBackedMemoryStore, "save_json", reject_write)
    code, payload, error = _run(capsys, *command)

    assert code == 4
    assert payload is None
    assert error["error"]["kind"] == "write"
    assert "injected batch obsolescence write failure" in error["error"]["message"]
    assert snapshot_path.read_bytes() == original_snapshot


def test_cli_obsolete_transition_and_serialization_failures_do_not_write(
    tmp_path,
    capsys,
    monkeypatch,
):
    store, records = _obsolescence_store()
    snapshot_path = tmp_path / "obsolete-failure.snapshot.json"
    store.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()

    def reject_transition(_store, _lesson_id):
        raise ValueError("injected obsolescence rejection")

    monkeypatch.setattr(
        TraceBackedMemoryStore,
        "obsolete_lesson",
        reject_transition,
    )
    code, payload, error = _run(
        capsys,
        "obsolete",
        str(snapshot_path),
        "lesson",
        records["dependent_lesson_ids"][0],
        "--write",
    )

    assert code == 3
    assert payload is None
    assert error["error"]["kind"] == "state"
    assert "injected obsolescence rejection" in error["error"]["message"]
    assert snapshot_path.read_bytes() == original_snapshot

    monkeypatch.undo()
    real_json_text = cli._json_text

    def reject_obsolescence_result(value):
        if type(value) is dict and "memory_kind" in value:
            raise TypeError("injected obsolescence serialization failure")
        return real_json_text(value)

    monkeypatch.setattr(cli, "_json_text", reject_obsolescence_result)
    code, payload, error = _run(
        capsys,
        "obsolete",
        str(snapshot_path),
        "lesson",
        records["dependent_lesson_ids"][0],
        "--write",
    )

    assert code == 1
    assert payload is None
    assert error["error"]["kind"] == "internal"
    assert "injected obsolescence serialization failure" in (
        error["error"]["message"]
    )
    assert snapshot_path.read_bytes() == original_snapshot


def test_cli_obsolete_snapshot_write_failure_preserves_source(
    tmp_path,
    capsys,
    monkeypatch,
):
    store, records = _obsolescence_store()
    snapshot_path = tmp_path / "obsolete-write-failure.snapshot.json"
    store.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()

    def reject_write(_store, _path):
        raise OSError("injected obsolescence write failure")

    monkeypatch.setattr(TraceBackedMemoryStore, "save_json", reject_write)
    code, payload, error = _run(
        capsys,
        "obsolete",
        str(snapshot_path),
        "project-policy",
        records["active_policy_id"],
        "--write",
    )

    assert code == 4
    assert payload is None
    assert error["error"] == {
        "kind": "write",
        "message": "injected obsolescence write failure",
        "type": "OSError",
    }
    assert snapshot_path.read_bytes() == original_snapshot


def test_cli_resource_errors_preserve_input_read_and_write_classes(
    tmp_path,
    capsys,
    monkeypatch,
):
    code, payload, error = _run(
        capsys,
        "resource",
        "read",
        "../schemas/postgres.sql",
    )
    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert error["error"]["type"] == "PackagedResourceError"

    def reject_list():
        raise cli.PackagedResourceError(
            "read",
            name="schemas/postgres.sql",
            detail="injected missing package data",
        )

    monkeypatch.setattr(cli, "packaged_resources", reject_list)
    code, payload, error = _run(capsys, "resource", "list")
    assert code == 1
    assert payload is None
    assert error["error"]["kind"] == "internal"

    monkeypatch.undo()

    def reject_export(name, destination, *, overwrite=False):
        raise cli.PackagedResourceError(
            "export",
            name=name,
            destination=destination,
            detail="injected export failure",
        )

    monkeypatch.setattr(cli, "export_packaged_resource", reject_export)
    code, payload, error = _run(
        capsys,
        "resource",
        "export",
        "schemas/postgres.sql",
        str(tmp_path / "postgres.sql"),
    )
    assert code == 4
    assert payload is None
    assert error["error"]["kind"] == "write"
    assert "injected export failure" in error["error"]["message"]


def test_cli_read_commands_emit_canonical_json(tmp_path, capsys):
    path, decision_ids = _snapshot_with_states(
        tmp_path,
        "trace_only_pass",
        "decision_only_error",
        "complete",
    )

    code, payload, error = _run(
        capsys, "snapshot", "validate", str(path)
    )
    assert code == 0
    assert error is None
    assert payload == {
        "counts": {
            "failure_cases": 0,
            "lessons": 0,
            "project_policies": 0,
            "traces": 3,
            "usage_logs": 3,
        },
        "snapshot_version": 2,
        "valid": True,
    }

    code, payload, error = _run(capsys, "snapshot", "stats", str(path))
    assert code == 0
    assert error is None
    assert payload == {
        "counts": {
            "failure_cases": 0,
            "lessons": 0,
            "project_policies": 0,
            "traces": 3,
            "usage_logs": 3,
        },
        "snapshot_version": 2,
    }

    code, payload, error = _run(capsys, "audit", str(path))
    assert code == 0
    assert error is None
    assert [item["decision_id"] for item in payload] == sorted(
        decision_ids.values()
    )
    assert [item["status"] for item in payload] == [
        "trace_only",
        "decision_only",
        "complete",
    ]

    code, payload, error = _run(capsys, "remediation", str(path))
    assert code == 0
    assert error is None
    assert [item["action"] for item in payload] == [
        "recover",
        "recover",
        "none",
    ]
    assert payload[0]["resolved_eval_result"] == "pass"
    assert payload[1]["resolved_eval_result"] == "error"

    code, payload, error = _run(capsys, "metrics", str(path))
    assert code == 0
    assert error is None
    assert set(payload) == {"memory", "memory_outcomes", "memory_runs"}
    assert payload["memory"]["decision_count"] == 3
    assert payload["memory_runs"]["recoverable_count"] == 2
    assert payload["memory_runs"]["auto_recoverable_count"] == 2
    assert payload["memory_outcomes"] == []


def test_cli_reports_usage_file_and_snapshot_errors_as_json(tmp_path, capsys):
    code, payload, error = _run(capsys)
    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"

    code, payload, error = _run(capsys, "audit", str(tmp_path / "missing.json"))
    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert error["error"]["type"] == "FileNotFoundError"

    invalid_json = tmp_path / "invalid.json"
    invalid_json.write_text("{not-json", encoding="utf-8")
    code, payload, error = _run(capsys, "audit", str(invalid_json))
    assert code == 2
    assert payload is None
    assert error["error"]["type"] == "JSONDecodeError"

    invalid_snapshot = tmp_path / "invalid-snapshot.json"
    invalid_snapshot.write_text(
        json.dumps(
            {
                "snapshot_version": 3,
                "traces": [],
                "failure_cases": [],
                "lessons": [],
                "project_policies": [],
                "usage_logs": [],
            }
        ),
        encoding="utf-8",
    )
    code, payload, error = _run(
        capsys, "snapshot", "validate", str(invalid_snapshot)
    )
    assert code == 2
    assert payload is None
    assert error["error"]["type"] == "ValueError"
    assert "snapshot_version" in error["error"]["message"]


@pytest.mark.parametrize(
    "command",
    [
        ("snapshot", "validate"),
        ("snapshot", "stats"),
    ],
)
def test_cli_snapshot_reads_reject_whitespace_identity_without_rewriting_source(
    tmp_path, capsys, command: tuple[str, str]
):
    path, _decision_ids = _snapshot_with_states(tmp_path, "complete")
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot["usage_logs"][0]["decision_id"] = "   "
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    original = path.read_bytes()

    code, payload, error = _run(capsys, *command, str(path))

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert error["error"]["type"] == "ValueError"
    assert "decision_id" in error["error"]["message"]
    assert path.read_bytes() == original


def test_cli_outcome_is_a_private_dry_run_and_calls_store_once(
    tmp_path,
    capsys,
    monkeypatch,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    decision_id = decision_ids["pending"]
    original = path.read_bytes()
    calls: list[tuple[str, str, bool]] = []
    record_outcome = TraceBackedMemoryStore.record_decision_outcome

    def count_call(
        store,
        called_decision_id,
        eval_result,
        *,
        memory_caused_failure=False,
    ):
        calls.append(
            (called_decision_id, eval_result, memory_caused_failure)
        )
        return record_outcome(
            store,
            called_decision_id,
            eval_result,
            memory_caused_failure=memory_caused_failure,
        )

    monkeypatch.setattr(
        TraceBackedMemoryStore,
        "record_decision_outcome",
        count_call,
    )

    code, payload, error = _run(
        capsys,
        "outcome",
        str(path),
        decision_id,
        "--eval-result",
        "pass",
    )

    assert code == 0
    assert error is None
    assert calls == [(decision_id, "pass", False)]
    assert payload == {
        "changed": True,
        "decision_id": decision_id,
        "eval_result": "pass",
        "memory_caused_failure": False,
        "previous_eval_result": None,
        "previous_memory_caused_failure": False,
        "written": False,
    }
    assert path.read_bytes() == original
    restored = TraceBackedMemoryStore.load_json(path)
    assert restored.usage_logs[0].eval_result is None
    assert restored.traces[restored.usage_logs[0].trace_id].eval_result == "unknown"


def test_cli_outcome_writes_failure_attribution_and_replays_exactly(
    tmp_path,
    capsys,
):
    store = TraceBackedMemoryStore()
    trace, decision_id = _pending_memory_use_run(store, "outcome_write")
    path = tmp_path / "outcome-write.snapshot.json"
    store.save_json(path)
    command = (
        "outcome",
        str(path),
        decision_id,
        "--eval-result",
        "error",
        "--memory-caused-failure",
        "true",
        "--write",
    )

    code, payload, error = _run(capsys, *command)

    assert code == 0
    assert error is None
    assert payload == {
        "changed": True,
        "decision_id": decision_id,
        "eval_result": "error",
        "memory_caused_failure": True,
        "previous_eval_result": None,
        "previous_memory_caused_failure": False,
        "written": True,
    }
    restored = TraceBackedMemoryStore.load_json(path)
    assert restored.usage_logs[0].eval_result == "error"
    assert restored.usage_logs[0].memory_caused_failure is True
    assert restored.traces[trace.trace_id].eval_result == "unknown"
    written = path.read_bytes()

    replay_code, replay_payload, replay_error = _run(capsys, *command)

    assert replay_code == 0
    assert replay_error is None
    assert replay_payload == {
        **payload,
        "changed": False,
        "previous_eval_result": "error",
        "previous_memory_caused_failure": True,
    }
    assert path.read_bytes() == written


def test_cli_outcome_rejects_invalid_unknown_and_conflicting_state(
    tmp_path,
    capsys,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    decision_id = decision_ids["pending"]
    original = path.read_bytes()

    code, payload, error = _run(
        capsys,
        "outcome",
        str(path),
        decision_id,
        "--eval-result",
        "unknown",
        "--write",
    )
    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert path.read_bytes() == original

    for rejected_id in ("decision_missing", "", "x" * 129):
        code, payload, error = _run(
            capsys,
            "outcome",
            str(path),
            rejected_id,
            "--eval-result",
            "pass",
            "--write",
        )
        assert code == 3
        assert payload is None
        assert error["error"]["kind"] == "state"
        assert path.read_bytes() == original

    code, payload, error = _run(
        capsys,
        "outcome",
        str(path),
        decision_id,
        "--eval-result",
        "pass",
        "--memory-caused-failure",
        "true",
        "--write",
    )
    assert code == 3
    assert payload is None
    assert "requires eval_result fail or error" in error["error"]["message"]
    assert path.read_bytes() == original

    sealed_path, sealed_ids = _snapshot_with_states(
        tmp_path / "sealed",
        "decision_only_pass",
    )
    sealed_original = sealed_path.read_bytes()
    code, payload, error = _run(
        capsys,
        "outcome",
        str(sealed_path),
        sealed_ids["decision_only_pass"],
        "--eval-result",
        "error",
        "--write",
    )
    assert code == 3
    assert payload is None
    assert "already sealed" in error["error"]["message"]
    assert sealed_path.read_bytes() == sealed_original

    attributed_store = TraceBackedMemoryStore()
    _trace, attributed_id = _pending_memory_use_run(
        attributed_store,
        "attribution_conflict",
    )
    attributed_store.record_decision_outcome(
        attributed_id,
        "error",
        memory_caused_failure=True,
    )
    attributed_path = tmp_path / "attributed.snapshot.json"
    attributed_store.save_json(attributed_path)
    attributed_original = attributed_path.read_bytes()

    code, payload, error = _run(
        capsys,
        "outcome",
        str(attributed_path),
        attributed_id,
        "--eval-result",
        "error",
        "--memory-caused-failure",
        "false",
        "--write",
    )
    assert code == 3
    assert payload is None
    assert "already sealed" in error["error"]["message"]
    assert attributed_path.read_bytes() == attributed_original


def test_cli_outcome_rejects_wrong_memory_attribution_without_writing(
    tmp_path,
    capsys,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    original = path.read_bytes()

    code, payload, error = _run(
        capsys,
        "outcome",
        str(path),
        decision_ids["pending"],
        "--eval-result",
        "error",
        "--memory-caused-failure",
        "true",
        "--write",
    )

    assert code == 3
    assert payload is None
    assert "requires failed or errored memory use" in error["error"]["message"]
    assert path.read_bytes() == original


def test_cli_outcome_transition_serialization_and_write_failures_are_atomic(
    tmp_path,
    capsys,
    monkeypatch,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    decision_id = decision_ids["pending"]
    original = path.read_bytes()
    command = (
        "outcome",
        str(path),
        decision_id,
        "--eval-result",
        "pass",
        "--write",
    )

    def reject_transition(_store, _decision_id, _eval_result, **_kwargs):
        raise ValueError("injected outcome rejection")

    monkeypatch.setattr(
        TraceBackedMemoryStore,
        "record_decision_outcome",
        reject_transition,
    )
    code, payload, error = _run(capsys, *command)
    assert code == 3
    assert payload is None
    assert error["error"]["kind"] == "state"
    assert path.read_bytes() == original

    monkeypatch.undo()
    real_json_text = cli._json_text

    def reject_result(value):
        if type(value) is dict and "previous_eval_result" in value:
            raise TypeError("injected outcome serialization failure")
        return real_json_text(value)

    monkeypatch.setattr(cli, "_json_text", reject_result)
    code, payload, error = _run(capsys, *command)
    assert code == 1
    assert payload is None
    assert error["error"]["kind"] == "internal"
    assert path.read_bytes() == original

    monkeypatch.undo()

    def reject_write(_store, _path):
        raise OSError("injected outcome write failure")

    monkeypatch.setattr(TraceBackedMemoryStore, "save_json", reject_write)
    code, payload, error = _run(capsys, *command)
    assert code == 4
    assert payload is None
    assert error["error"]["kind"] == "write"
    assert path.read_bytes() == original


def test_cli_complete_is_dry_run_by_default(tmp_path, capsys):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    decision_id = decision_ids["pending"]
    trace_id = TraceBackedMemoryStore.load_json(path).memory_run_audits()[0].trace_id
    original = path.read_bytes()

    code, payload, error = _run(
        capsys,
        "complete",
        str(path),
        trace_id,
        decision_id,
        "--eval-result",
        "pass",
    )

    assert code == 0
    assert error is None
    assert payload["written"] is False
    assert payload["decision_ids"] == [decision_id]
    assert len(payload["completions"]) == 1
    completion = payload["completions"][0]
    assert completion["trace"]["trace_id"] == trace_id
    assert completion["trace"]["eval_result"] == "pass"
    assert completion["usage_log"]["eval_result"] == "pass"
    assert completion["usage_log"]["memory_caused_failure"] is False
    assert path.read_bytes() == original
    assert TraceBackedMemoryStore.load_json(path).memory_run_audits()[0].status == (
        "pending"
    )


def test_cli_complete_writes_full_evidence_and_replays_exactly(tmp_path, capsys):
    store = TraceBackedMemoryStore()
    trace, decision_id = _pending_memory_use_run(store, "full_evidence")
    trace_id = trace.trace_id
    path = tmp_path / "store.snapshot.json"
    store.save_json(path)
    tool_outputs_path = tmp_path / "tool-outputs.json"
    tool_outputs = [
        {"name": "search", "result": {"documents": 3}},
        {"name": "executor", "error": "timeout"},
    ]
    tool_outputs_path.write_text(
        json.dumps(tool_outputs),
        encoding="utf-8",
    )
    command = (
        "complete",
        str(path),
        trace_id,
        decision_id,
        "--eval-result",
        "error",
        "--memory-caused-failure",
        "true",
        "--output-hash",
        "sha256:cli-output",
        "--tool-outputs-file",
        str(tool_outputs_path),
        "--latency-ms",
        "2147483647",
        "--cost-usd",
        "0.0025",
        "--error",
        "executor failed",
        "--trace-uri",
        "trace://cli/completion",
        "--write",
    )

    code, payload, error = _run(capsys, *command)

    assert code == 0
    assert error is None
    assert payload["written"] is True
    assert payload["decision_ids"] == [decision_id]
    completed_trace = payload["completions"][0]["trace"]
    assert completed_trace["output_hash"] == "sha256:cli-output"
    assert completed_trace["tool_outputs"] == tool_outputs
    assert completed_trace["latency_ms"] == 2_147_483_647
    assert completed_trace["cost_usd"] == 0.0025
    assert completed_trace["error"] == "executor failed"
    assert completed_trace["trace_uri"] == "trace://cli/completion"

    restored = TraceBackedMemoryStore.load_json(path)
    assert restored.traces[trace_id].tool_outputs == tool_outputs
    audit = restored.memory_run_audits()[0]
    assert audit.status == "complete"
    assert audit.memory_caused_failure is True

    written = path.read_bytes()
    replay_code, replay_payload, replay_error = _run(capsys, *command)
    assert replay_code == 0
    assert replay_error is None
    assert replay_payload == payload
    assert path.read_bytes() == written


def test_cli_complete_preserves_omitted_evidence_and_accepts_empty_outputs(
    tmp_path,
    capsys,
):
    store = TraceBackedMemoryStore()
    trace, decision_id = _pending_run(
        store,
        "prefilled",
        output_hash="sha256:prefilled",
        tool_outputs=[{"status": "prefilled"}],
        latency_ms=80,
        cost_usd=0.03,
        error="prefilled error",
        trace_uri="trace://prefilled",
    )
    path = tmp_path / "prefilled.snapshot.json"
    store.save_json(path)
    original = path.read_bytes()

    code, payload, error = _run(
        capsys,
        "complete",
        str(path),
        trace.trace_id,
        decision_id,
        "--eval-result",
        "error",
    )

    assert code == 0
    assert error is None
    completed_trace = payload["completions"][0]["trace"]
    assert completed_trace["output_hash"] == "sha256:prefilled"
    assert completed_trace["tool_outputs"] == [{"status": "prefilled"}]
    assert completed_trace["latency_ms"] == 80
    assert completed_trace["cost_usd"] == 0.03
    assert completed_trace["error"] == "prefilled error"
    assert completed_trace["trace_uri"] == "trace://prefilled"
    assert path.read_bytes() == original

    empty_path, empty_ids = _snapshot_with_states(tmp_path / "empty", "pending")
    empty_decision_id = empty_ids["pending"]
    empty_trace_id = (
        TraceBackedMemoryStore.load_json(empty_path)
        .memory_run_audits()[0]
        .trace_id
    )
    outputs_path = tmp_path / "empty-tool-outputs.json"
    outputs_path.write_text("[]", encoding="utf-8")

    code, payload, error = _run(
        capsys,
        "complete",
        str(empty_path),
        empty_trace_id,
        empty_decision_id,
        "--eval-result",
        "pass",
        "--tool-outputs-file",
        str(outputs_path),
    )

    assert code == 0
    assert error is None
    assert payload["completions"][0]["trace"]["tool_outputs"] == []


@pytest.mark.parametrize(
    ("contents", "message_fragment"),
    [
        (b"{not-json", "JSON"),
        (b'[{"value":NaN}]', "non-finite"),
        (b"{}", "array"),
        (b"[1]", "objects"),
        (b"\xff", "UTF-8"),
    ],
)
def test_cli_complete_rejects_invalid_tool_output_files_without_writing(
    tmp_path,
    capsys,
    contents,
    message_fragment,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    decision_id = decision_ids["pending"]
    trace_id = TraceBackedMemoryStore.load_json(path).memory_run_audits()[0].trace_id
    original = path.read_bytes()
    outputs_path = tmp_path / "invalid-tool-outputs.json"
    outputs_path.write_bytes(contents)

    code, payload, error = _run(
        capsys,
        "complete",
        str(path),
        trace_id,
        decision_id,
        "--eval-result",
        "pass",
        "--tool-outputs-file",
        str(outputs_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert message_fragment in error["error"]["message"]
    assert path.read_bytes() == original


def test_cli_complete_rejects_missing_tool_output_file_without_writing(
    tmp_path,
    capsys,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    decision_id = decision_ids["pending"]
    trace_id = TraceBackedMemoryStore.load_json(path).memory_run_audits()[0].trace_id
    original = path.read_bytes()

    code, payload, error = _run(
        capsys,
        "complete",
        str(path),
        trace_id,
        decision_id,
        "--eval-result",
        "pass",
        "--tool-outputs-file",
        str(tmp_path / "missing-tool-outputs.json"),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "missing-tool-outputs.json" in error["error"]["message"]
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    "extra_args",
    [
        (),
        ("--eval-result", "unknown"),
        ("--eval-result", "pass", "--memory-caused-failure", "maybe"),
        ("--eval-result", "pass", "--latency-ms", "1.5"),
        ("--eval-result", "pass", "--cost-usd", "NaN"),
        ("--eval-result", "pass", "--cost-usd", "Infinity"),
    ],
)
def test_cli_complete_rejects_invalid_arguments_without_writing(
    tmp_path,
    capsys,
    extra_args,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    decision_id = decision_ids["pending"]
    trace_id = TraceBackedMemoryStore.load_json(path).memory_run_audits()[0].trace_id
    original = path.read_bytes()

    code, payload, error = _run(
        capsys,
        "complete",
        str(path),
        trace_id,
        decision_id,
        *extra_args,
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert path.read_bytes() == original


def test_cli_complete_reports_domain_rejections_without_writing(tmp_path, capsys):
    store = TraceBackedMemoryStore()
    first_trace, first_decision_id = _pending_run(store, "first")
    _second_trace, second_decision_id = _pending_run(store, "second")
    path = tmp_path / "store.snapshot.json"
    store.save_json(path)
    original = path.read_bytes()

    cases = (
        (
            first_trace.trace_id,
            second_decision_id,
            ("--eval-result", "pass"),
            "does not belong",
        ),
        (
            first_trace.trace_id,
            first_decision_id,
            (
                "--eval-result",
                "pass",
                "--memory-caused-failure",
                "true",
            ),
            "requires eval_result fail or error",
        ),
        (
            first_trace.trace_id,
            first_decision_id,
            ("--eval-result", "pass", "--output-hash", ""),
            "output_hash",
        ),
        (
            first_trace.trace_id,
            first_decision_id,
            ("--eval-result", "pass", "--latency-ms", "-1"),
            "latency_ms must be non-negative",
        ),
        (
            first_trace.trace_id,
            first_decision_id,
            ("--eval-result", "pass", "--latency-ms", "2147483648"),
            "latency_ms must be at most 2147483647",
        ),
    )
    for trace_id, decision_id, extra_args, message_fragment in cases:
        code, payload, error = _run(
            capsys,
            "complete",
            str(path),
            trace_id,
            decision_id,
            *extra_args,
            "--write",
        )

        assert code == 3
        assert payload is None
        assert error["error"]["kind"] == "state"
        assert message_fragment in error["error"]["message"]
        assert path.read_bytes() == original


def test_cli_complete_batch_is_ordered_and_dry_run_by_default(
    tmp_path,
    capsys,
    monkeypatch,
):
    store = TraceBackedMemoryStore()
    first_trace, first_decision_id = _pending_run(store, "batch_first")
    second_trace, second_decision_id = _pending_run(store, "batch_second")
    path = tmp_path / "store.snapshot.json"
    store.save_json(path)
    original = path.read_bytes()
    measurements_path = _write_measurements(
        tmp_path / "measurements.json",
        [
            {
                "decision_id": second_decision_id,
                "eval_result": "error",
                "error": "second failed",
            },
            {
                "decision_id": first_decision_id,
                "eval_result": "pass",
            },
        ],
    )
    calls = []
    complete_memory_runs = TraceBackedMemoryStore.complete_memory_runs

    def recording_complete_memory_runs(current_store, results):
        calls.append(results)
        return complete_memory_runs(current_store, results)

    monkeypatch.setattr(
        TraceBackedMemoryStore,
        "complete_memory_runs",
        recording_complete_memory_runs,
    )

    code, payload, error = _run(
        capsys,
        "complete-batch",
        str(path),
        str(measurements_path),
    )

    assert code == 0
    assert error is None
    assert payload["written"] is False
    assert payload["decision_ids"] == [second_decision_id, first_decision_id]
    assert len(calls) == 1
    assert tuple(result.decision_id for result in calls[0]) == (
        second_decision_id,
        first_decision_id,
    )
    assert [
        completion["trace"]["trace_id"]
        for completion in payload["completions"]
    ] == [second_trace.trace_id, first_trace.trace_id]
    assert path.read_bytes() == original
    restored_audits = TraceBackedMemoryStore.load_json(path).memory_run_audits()
    assert {audit.status for audit in restored_audits} == {"pending"}


def test_cli_complete_batch_writes_full_evidence_and_replays_exactly(
    tmp_path,
    capsys,
):
    store = TraceBackedMemoryStore()
    preserved_trace, preserved_decision_id = _pending_run(
        store,
        "batch_preserved",
        output_hash="sha256:preserved",
        tool_outputs=[{"status": "preserved"}],
    )
    measured_trace, measured_decision_id = _pending_memory_use_run(
        store,
        "batch_measured",
    )
    path = tmp_path / "store.snapshot.json"
    store.save_json(path)
    measurements_path = _write_measurements(
        tmp_path / "measurements.json",
        [
            {
                "decision_id": preserved_decision_id,
                "eval_result": "pass",
                "output_hash": None,
                "tool_outputs": None,
            },
            {
                "decision_id": measured_decision_id,
                "eval_result": "error",
                "memory_caused_failure": True,
                "output_hash": "sha256:batch-output",
                "tool_outputs": [],
                "latency_ms": 2_147_483_647,
                "cost_usd": 2,
                "error": "executor timeout",
                "trace_uri": "trace://cli/batch",
            },
        ],
    )
    command = (
        "complete-batch",
        str(path),
        str(measurements_path),
        "--write",
    )

    code, payload, error = _run(capsys, *command)

    assert code == 0
    assert error is None
    assert payload["written"] is True
    assert payload["decision_ids"] == [
        preserved_decision_id,
        measured_decision_id,
    ]
    assert payload["completions"][0]["trace"]["output_hash"] == (
        "sha256:preserved"
    )
    assert payload["completions"][0]["trace"]["tool_outputs"] == [
        {"status": "preserved"}
    ]
    completed_trace = payload["completions"][1]["trace"]
    assert completed_trace["trace_id"] == measured_trace.trace_id
    assert completed_trace["output_hash"] == "sha256:batch-output"
    assert completed_trace["tool_outputs"] == []
    assert completed_trace["latency_ms"] == 2_147_483_647
    assert completed_trace["cost_usd"] == 2
    assert completed_trace["error"] == "executor timeout"
    assert completed_trace["trace_uri"] == "trace://cli/batch"

    restored = TraceBackedMemoryStore.load_json(path)
    assert restored.traces[preserved_trace.trace_id].output_hash == (
        "sha256:preserved"
    )
    assert all(audit.status == "complete" for audit in restored.memory_run_audits())
    written = path.read_bytes()

    replay_code, replay_payload, replay_error = _run(capsys, *command)

    assert replay_code == 0
    assert replay_error is None
    assert replay_payload == payload
    assert path.read_bytes() == written


@pytest.mark.parametrize(
    ("contents", "message_fragment"),
    [
        (b"\xff", "UTF-8"),
        (b"{not-json", "JSON"),
        (b"{}", "non-empty array"),
        (b"[]", "non-empty array"),
        (b"[1]", "items must be objects"),
        (
            b'[{"decision_id":"decision_1"}]',
            "missing required field: eval_result",
        ),
        (
            b'[{"eval_result":"pass"}]',
            "missing required field: decision_id",
        ),
        (
            b'[{"decision_id":"decision_1","eval_result":"pass",'
            b'"trace_id":"trace_1"}]',
            "unknown field: trace_id",
        ),
        (
            b'[{"decision_id":"decision_1","decision_id":"decision_2",'
            b'"eval_result":"pass"}]',
            "duplicate object key: decision_id",
        ),
        (
            b'[{"decision_id":"decision_1","eval_result":"pass",'
            b'"tool_outputs":[{"value":1,"value":2}]}]',
            "duplicate object key: value",
        ),
        (
            b'[{"decision_id":"decision_1","eval_result":"pass",'
            b'"cost_usd":NaN}]',
            "non-finite number",
        ),
        (
            b'[{"decision_id":"decision_1","eval_result":"pass",'
            b'"cost_usd":1e400}]',
            "non-finite number",
        ),
    ],
)
def test_cli_complete_batch_rejects_invalid_manifest_without_writing(
    tmp_path,
    capsys,
    contents,
    message_fragment,
):
    path, _decision_ids = _snapshot_with_states(tmp_path, "pending")
    original = path.read_bytes()
    measurements_path = tmp_path / "invalid-measurements.json"
    measurements_path.write_bytes(contents)

    code, payload, error = _run(
        capsys,
        "complete-batch",
        str(path),
        str(measurements_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert message_fragment in error["error"]["message"]
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("field_name", "value", "message_fragment"),
    [
        ("decision_id", 1, "decision_id must be a string"),
        ("eval_result", True, "eval_result must be a string"),
        (
            "memory_caused_failure",
            1,
            "memory_caused_failure must be a boolean",
        ),
        ("output_hash", 1, "output_hash must be a string or null"),
        ("tool_outputs", {}, "tool_outputs must be an array of objects or null"),
        ("tool_outputs", [1], "tool_outputs must be an array of objects or null"),
        ("latency_ms", True, "latency_ms must be an integer or null"),
        ("latency_ms", 1.5, "latency_ms must be an integer or null"),
        ("cost_usd", True, "cost_usd must be a finite number or null"),
        ("cost_usd", "1", "cost_usd must be a finite number or null"),
        ("error", 1, "error must be a string or null"),
        ("trace_uri", 1, "trace_uri must be a string or null"),
    ],
)
def test_cli_complete_batch_rejects_wrong_field_types_without_writing(
    tmp_path,
    capsys,
    field_name,
    value,
    message_fragment,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    original = path.read_bytes()
    measurement = {
        "decision_id": decision_ids["pending"],
        "eval_result": "pass",
        field_name: value,
    }
    measurements_path = _write_measurements(
        tmp_path / "invalid-types.json",
        [measurement],
    )

    code, payload, error = _run(
        capsys,
        "complete-batch",
        str(path),
        str(measurements_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert message_fragment in error["error"]["message"]
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("measurements", "message_fragment"),
    [
        (
            [
                {"decision_id": "{decision_id}", "eval_result": "pass"},
                {"decision_id": "{decision_id}", "eval_result": "pass"},
            ],
            "unique decision_ids",
        ),
        (
            [
                {"decision_id": "{decision_id}", "eval_result": "pass"},
                {"decision_id": "missing_decision", "eval_result": "pass"},
            ],
            "unknown decision_id",
        ),
        (
            [{"decision_id": "{decision_id}", "eval_result": "unknown"}],
            "eval_result",
        ),
        (
            [
                {
                    "decision_id": "{decision_id}",
                    "eval_result": "pass",
                    "memory_caused_failure": True,
                }
            ],
            "requires eval_result fail or error",
        ),
        (
            [
                {
                    "decision_id": "{decision_id}",
                    "eval_result": "pass",
                    "latency_ms": -1,
                }
            ],
            "latency_ms must be non-negative",
        ),
        (
            [
                {
                    "decision_id": "{decision_id}",
                    "eval_result": "pass",
                    "latency_ms": 2_147_483_648,
                }
            ],
            "latency_ms must be at most 2147483647",
        ),
    ],
)
def test_cli_complete_batch_reports_store_rejections_without_writing(
    tmp_path,
    capsys,
    measurements,
    message_fragment,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    decision_id = decision_ids["pending"]
    original = path.read_bytes()
    resolved_measurements = [
        {
            key: decision_id if value == "{decision_id}" else value
            for key, value in measurement.items()
        }
        for measurement in measurements
    ]
    measurements_path = _write_measurements(
        tmp_path / "rejected-measurements.json",
        resolved_measurements,
    )

    code, payload, error = _run(
        capsys,
        "complete-batch",
        str(path),
        str(measurements_path),
        "--write",
    )

    assert code == 3
    assert payload is None
    assert error["error"]["kind"] == "state"
    assert message_fragment in error["error"]["message"]
    assert path.read_bytes() == original


def test_cli_complete_batch_rejects_missing_manifest_without_writing(
    tmp_path,
    capsys,
):
    path, _decision_ids = _snapshot_with_states(tmp_path, "pending")
    original = path.read_bytes()
    missing_path = tmp_path / "missing-measurements.json"

    code, payload, error = _run(
        capsys,
        "complete-batch",
        str(path),
        str(missing_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "missing-measurements.json" in error["error"]["message"]
    assert path.read_bytes() == original


def test_cli_complete_batch_rejects_deeply_nested_json_as_input(
    tmp_path,
    capsys,
):
    path, _decision_ids = _snapshot_with_states(tmp_path, "pending")
    original = path.read_bytes()
    measurements_path = tmp_path / "nested-measurements.json"
    measurements_path.write_bytes((b"[" * 2000) + (b"]" * 2000))

    code, payload, error = _run(
        capsys,
        "complete-batch",
        str(path),
        str(measurements_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "measurements JSON" in error["error"]["message"]
    assert path.read_bytes() == original


def test_cli_complete_batch_rejects_shared_trace_result_disagreement(
    tmp_path,
    capsys,
):
    store = TraceBackedMemoryStore()
    trace, first_decision_id = _pending_run(store, "batch_shared")
    second_log = store.log_decision(
        trace.run_id,
        MemoryContext(
            mode="repair",
            repo="repo_cli",
            tenant="tenant_cli",
            commit_sha="commit_cli",
        ),
        [],
        MemoryDecision(
            use_memory=False,
            allowed_memory_ids=[],
            blocked_memory_ids=[],
            reason="no applicable memory",
            risk="none",
            recommended_injection="none",
        ),
    )
    path = tmp_path / "shared.snapshot.json"
    store.save_json(path)
    original = path.read_bytes()
    measurements_path = _write_measurements(
        tmp_path / "shared-measurements.json",
        [
            {"decision_id": first_decision_id, "eval_result": "pass"},
            {"decision_id": second_log.decision_id, "eval_result": "error"},
        ],
    )

    code, payload, error = _run(
        capsys,
        "complete-batch",
        str(path),
        str(measurements_path),
        "--write",
    )

    assert code == 3
    assert payload is None
    assert error["error"]["kind"] == "state"
    assert "shared trace" in error["error"]["message"]
    assert path.read_bytes() == original


def test_cli_complete_batch_reports_write_failure_without_changing_snapshot(
    tmp_path,
    capsys,
    monkeypatch,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    original = path.read_bytes()
    measurements_path = _write_measurements(
        tmp_path / "write-failure-measurements.json",
        [
            {
                "decision_id": decision_ids["pending"],
                "eval_result": "pass",
            }
        ],
    )

    def reject_write(_store, _path):
        raise OSError("injected batch completion write failure")

    monkeypatch.setattr(TraceBackedMemoryStore, "save_json", reject_write)

    code, payload, error = _run(
        capsys,
        "complete-batch",
        str(path),
        str(measurements_path),
        "--write",
    )

    assert code == 4
    assert payload is None
    assert error["error"] == {
        "kind": "write",
        "message": "injected batch completion write failure",
        "type": "OSError",
    }
    assert path.read_bytes() == original


def test_cli_recover_ready_is_dry_run_by_default_and_writes_atomically(
    tmp_path,
    capsys,
):
    path, decision_ids = _snapshot_with_states(
        tmp_path,
        "trace_only_pass",
        "trace_only_fail",
    )
    original = path.read_bytes()

    code, payload, error = _run(capsys, "recover-ready", str(path))

    assert code == 0
    assert error is None
    assert payload["written"] is False
    assert payload["decision_ids"] == [decision_ids["trace_only_pass"]]
    assert len(payload["completions"]) == 1
    assert path.read_bytes() == original
    assert [
        item.action
        for item in TraceBackedMemoryStore.load_json(
            path
        ).memory_run_remediations()
    ] == ["recover", "recover_with_attribution"]

    code, payload, error = _run(
        capsys, "recover-ready", str(path), "--write"
    )
    assert code == 0
    assert error is None
    assert payload["written"] is True
    assert payload["decision_ids"] == [decision_ids["trace_only_pass"]]
    assert path.read_bytes() != original
    restored = TraceBackedMemoryStore.load_json(path)
    assert [item.action for item in restored.memory_run_remediations()] == [
        "none",
        "recover_with_attribution",
    ]

    written = path.read_bytes()
    code, payload, error = _run(
        capsys, "recover-ready", str(path), "--write"
    )
    assert code == 0
    assert error is None
    assert payload == {"completions": [], "decision_ids": [], "written": True}
    assert path.read_bytes() == written


def test_cli_single_recovery_requires_explicit_failed_trace_attribution(
    tmp_path,
    capsys,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "trace_only_fail")
    decision_id = decision_ids["trace_only_fail"]
    original = path.read_bytes()

    code, payload, error = _run(
        capsys, "recover", str(path), decision_id, "--write"
    )
    assert code == 3
    assert payload is None
    assert error["error"]["kind"] == "state"
    assert "memory_caused_failure" in error["error"]["message"]
    assert path.read_bytes() == original

    code, payload, error = _run(
        capsys,
        "recover",
        str(path),
        decision_id,
        "--memory-caused-failure",
        "maybe",
    )
    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert path.read_bytes() == original

    code, payload, error = _run(
        capsys,
        "recover",
        str(path),
        decision_id,
        "--memory-caused-failure",
        "false",
    )
    assert code == 0
    assert error is None
    assert payload["written"] is False
    assert payload["decision_ids"] == [decision_id]
    assert path.read_bytes() == original

    code, payload, error = _run(
        capsys,
        "recover",
        str(path),
        decision_id,
        "--memory-caused-failure",
        "false",
        "--write",
    )
    assert code == 0
    assert error is None
    assert payload["written"] is True
    assert TraceBackedMemoryStore.load_json(path).memory_run_audits()[0].status == (
        "complete"
    )


@pytest.mark.parametrize("state", ["pending", "conflict"])
def test_cli_single_recovery_rejects_nonrecoverable_state(
    tmp_path,
    capsys,
    state,
):
    path, decision_ids = _snapshot_with_states(tmp_path, state)
    original = path.read_bytes()

    code, payload, error = _run(
        capsys, "recover", str(path), decision_ids[state], "--write"
    )

    assert code == 3
    assert payload is None
    assert error["error"]["kind"] == "state"
    assert path.read_bytes() == original


def test_cli_batch_recovery_preserves_order_and_is_all_or_nothing(
    tmp_path,
    capsys,
):
    path, decision_ids = _snapshot_with_states(
        tmp_path,
        "decision_only_pass",
        "decision_only_error",
    )
    requested = [
        decision_ids["decision_only_error"],
        decision_ids["decision_only_pass"],
    ]
    original = path.read_bytes()

    code, payload, error = _run(
        capsys,
        "recover-batch",
        str(path),
        *requested,
    )
    assert code == 0
    assert error is None
    assert payload["written"] is False
    assert payload["decision_ids"] == requested
    assert path.read_bytes() == original

    code, payload, error = _run(
        capsys,
        "recover-batch",
        str(path),
        *requested,
        "--write",
    )
    assert code == 0
    assert error is None
    assert payload["written"] is True
    assert payload["decision_ids"] == requested
    assert all(
        audit.status == "complete"
        for audit in TraceBackedMemoryStore.load_json(path).memory_run_audits()
    )

    duplicate_path, duplicate_ids = _snapshot_with_states(
        tmp_path / "duplicate",
        "decision_only_pass",
    )
    duplicate_id = duplicate_ids["decision_only_pass"]
    duplicate_original = duplicate_path.read_bytes()
    code, payload, error = _run(
        capsys,
        "recover-batch",
        str(duplicate_path),
        duplicate_id,
        duplicate_id,
        "--write",
    )
    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert duplicate_path.read_bytes() == duplicate_original

    mixed_path, mixed_ids = _snapshot_with_states(
        tmp_path / "mixed",
        "decision_only_pass",
        "pending",
    )
    mixed_original = mixed_path.read_bytes()
    code, payload, error = _run(
        capsys,
        "recover-batch",
        str(mixed_path),
        mixed_ids["decision_only_pass"],
        mixed_ids["pending"],
        "--write",
    )
    assert code == 3
    assert payload is None
    assert error["error"]["kind"] == "state"
    assert mixed_path.read_bytes() == mixed_original


def test_cli_batch_recovery_default_argument_cardinality_limit_is_inclusive():
    decision_ids = [
        f"decision_{index}"
        for index in range(cli.CLI_RECOVER_BATCH_MAX_ITEMS)
    ]
    args = argparse.Namespace(
        command="recover-batch",
        decision_ids=decision_ids,
        attribution=[
            f"{decision_id}=true" for decision_id in decision_ids
        ],
    )

    cli._validate_recover_batch_cardinality(args)


def test_cli_batch_recovery_accepts_exact_configured_cardinality_limit(
    tmp_path,
    capsys,
    monkeypatch,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "trace_only_fail")
    decision_id = decision_ids["trace_only_fail"]
    monkeypatch.setattr(cli, "CLI_RECOVER_BATCH_MAX_ITEMS", 1)

    code, payload, error = _run(
        capsys,
        "recover-batch",
        str(path),
        decision_id,
        "--attribution",
        f"{decision_id}=false",
    )

    assert code == 0
    assert error is None
    assert payload["decision_ids"] == [decision_id]
    assert payload["written"] is False


@pytest.mark.parametrize(
    ("arguments", "message"),
    [
        (
            ("decision_one", "decision_one"),
            "recover-batch decision_ids contains more than 1 items",
        ),
        (
            (
                "decision_one",
                "--attribution",
                "decision_one=true",
                "--attribution",
                "decision_one=false",
            ),
            "recover-batch attributions contains more than 1 items",
        ),
    ],
)
def test_cli_batch_recovery_rejects_argument_cardinality_overflow_before_load(
    tmp_path,
    capsys,
    monkeypatch,
    arguments,
    message,
):
    snapshot_path = tmp_path / "must-not-be-read.snapshot.json"
    snapshot_path.write_bytes(b"sentinel snapshot bytes")
    original = snapshot_path.read_bytes()
    snapshot_loaded = False

    def reject_snapshot_load(*_args, **_kwargs):
        nonlocal snapshot_loaded
        snapshot_loaded = True
        raise AssertionError("recover-batch overflow must precede snapshot loading")

    monkeypatch.setattr(cli, "CLI_RECOVER_BATCH_MAX_ITEMS", 1)
    monkeypatch.setattr(
        TraceBackedMemoryStore,
        "load_json",
        reject_snapshot_load,
    )

    code, payload, error = _run(
        capsys,
        "recover-batch",
        str(snapshot_path),
        *arguments,
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"] == {
        "kind": "input",
        "message": message,
        "type": "CLIInputError",
    }
    assert snapshot_loaded is False
    assert snapshot_path.read_bytes() == original


def test_cli_batch_parses_attributions_strictly(tmp_path, capsys):
    path, decision_ids = _snapshot_with_states(tmp_path, "trace_only_fail")
    decision_id = decision_ids["trace_only_fail"]
    original = path.read_bytes()

    code, payload, error = _run(
        capsys,
        "recover-batch",
        str(path),
        decision_id,
        "--attribution",
        f"{decision_id}=false",
        "--write",
    )
    assert code == 0
    assert error is None
    assert payload["decision_ids"] == [decision_id]

    malformed_path, malformed_ids = _snapshot_with_states(
        tmp_path / "malformed",
        "trace_only_fail",
    )
    malformed_original = malformed_path.read_bytes()
    code, payload, error = _run(
        capsys,
        "recover-batch",
        str(malformed_path),
        malformed_ids["trace_only_fail"],
        "--attribution",
        "not-an-attribution",
        "--write",
    )
    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert malformed_path.read_bytes() == malformed_original

    code, payload, error = _run(
        capsys,
        "recover-batch",
        str(malformed_path),
        malformed_ids["trace_only_fail"],
        "--attribution",
        "decision_other=true",
    )
    assert code == 2
    assert payload is None
    assert "unrequested" in error["error"]["message"]


@pytest.mark.parametrize(
    "decision_id",
    [
        "decision=regional",
        "decision=regional=true",
    ],
)
def test_cli_batch_attribution_preserves_equals_in_decision_id(
    tmp_path,
    capsys,
    decision_id,
):
    path, _decision_ids = _snapshot_with_states(
        tmp_path,
        "trace_only_fail",
    )
    snapshot = json.loads(path.read_text(encoding="utf-8"))
    snapshot["usage_logs"][0]["decision_id"] = decision_id
    path.write_text(json.dumps(snapshot), encoding="utf-8")
    original = path.read_bytes()

    code, payload, error = _run(
        capsys,
        "recover-batch",
        str(path),
        decision_id,
        "--attribution",
        f"{decision_id}=false",
        "--write",
    )

    assert code == 0
    assert error is None
    assert payload["decision_ids"] == [decision_id]
    assert payload["written"] is True
    assert path.read_bytes() != original
    written = json.loads(path.read_text(encoding="utf-8"))
    assert written["usage_logs"][0]["decision_id"] == decision_id
    assert written["usage_logs"][0]["eval_result"] == "fail"


def test_cli_write_failure_uses_exit_four_without_replacing_snapshot(
    tmp_path,
    capsys,
    monkeypatch,
):
    path, _decision_ids = _snapshot_with_states(tmp_path, "trace_only_pass")
    original = path.read_bytes()

    def reject_write(_store, _path):
        raise OSError("injected CLI write failure")

    monkeypatch.setattr(TraceBackedMemoryStore, "save_json", reject_write)

    code, payload, error = _run(
        capsys, "recover-ready", str(path), "--write"
    )

    assert code == 4
    assert payload is None
    assert error["error"] == {
        "kind": "write",
        "message": "injected CLI write failure",
        "type": "OSError",
    }
    assert path.read_bytes() == original


def test_cli_write_lock_covers_load_to_save_and_releases_before_stdout(
    tmp_path,
    capsys,
    monkeypatch,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    events: list[str] = []
    original_load = TraceBackedMemoryStore.load_json
    original_save = TraceBackedMemoryStore.save_json
    original_write_text = cli._write_text

    @contextmanager
    def tracked_lock(snapshot_path):
        assert Path(snapshot_path) == path
        events.append("lock-enter")
        try:
            yield
        finally:
            events.append("lock-exit")

    def tracked_load(snapshot_path):
        events.append("load")
        return original_load(snapshot_path)

    def tracked_save(store, snapshot_path):
        events.append("save")
        return original_save(store, snapshot_path)

    def tracked_write_text(stream, text):
        events.append("stdout")
        return original_write_text(stream, text)

    monkeypatch.setattr(
        cli,
        "_snapshot_write_lock",
        tracked_lock,
        raising=False,
    )
    monkeypatch.setattr(TraceBackedMemoryStore, "load_json", tracked_load)
    monkeypatch.setattr(TraceBackedMemoryStore, "save_json", tracked_save)
    monkeypatch.setattr(cli, "_write_text", tracked_write_text)

    code, payload, error = _run(
        capsys,
        "outcome",
        str(path),
        decision_ids["pending"],
        "--eval-result",
        "pass",
        "--write",
    )

    assert code == 0
    assert error is None
    assert payload["written"] is True
    assert events == ["lock-enter", "load", "save", "lock-exit", "stdout"]


def test_cli_write_lock_failure_prevents_snapshot_loading(
    tmp_path,
    capsys,
    monkeypatch,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    original = path.read_bytes()
    events: list[str] = []

    @contextmanager
    def reject_lock(_snapshot_path):
        events.append("lock")
        raise OSError("injected snapshot lock failure")
        yield

    def reject_load(_snapshot_path):
        events.append("load")
        raise AssertionError("snapshot loading must follow lock acquisition")

    monkeypatch.setattr(
        cli,
        "_snapshot_write_lock",
        reject_lock,
        raising=False,
    )
    monkeypatch.setattr(TraceBackedMemoryStore, "load_json", reject_load)

    code, payload, error = _run(
        capsys,
        "outcome",
        str(path),
        decision_ids["pending"],
        "--eval-result",
        "pass",
        "--write",
    )

    assert code == 4
    assert payload is None
    assert error["error"] == {
        "kind": "write",
        "message": "injected snapshot lock failure",
        "type": "OSError",
    }
    assert events == ["lock"]
    assert path.read_bytes() == original


def test_cli_rejects_hard_link_lock_sidecar_before_snapshot_loading(
    tmp_path,
    capsys,
    monkeypatch,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    original = path.read_bytes()
    lock_path = cli._snapshot_lock_path(path)
    target_path = tmp_path / "unrelated-empty-file"
    target_path.write_bytes(b"")
    os.link(target_path, lock_path)

    def reject_load(_snapshot_path):
        raise AssertionError("unsafe sidecars must fail before snapshot load")

    monkeypatch.setattr(TraceBackedMemoryStore, "load_json", reject_load)

    code, payload, error = _run(
        capsys,
        "outcome",
        str(path),
        decision_ids["pending"],
        "--eval-result",
        "pass",
        "--write",
    )

    assert code == 4
    assert payload is None
    assert error["error"] == {
        "kind": "write",
        "message": (
            "snapshot lock sidecar must be a single-link regular file: "
            f"{lock_path}"
        ),
        "type": "OSError",
    }
    assert path.read_bytes() == original
    assert target_path.read_bytes() == b""


def test_cli_write_with_embedded_nul_path_returns_structured_error(capsys):
    code, payload, error = _run(
        capsys,
        "obsolete",
        "invalid\x00snapshot",
        "lesson",
        "lesson_001",
        "--write",
    )

    assert code == 4
    assert payload is None
    assert error["error"]["kind"] == "write"
    assert error["error"]["type"] == "ValueError"
    assert "embedded null" in error["error"]["message"]


def test_cli_snapshot_write_lock_delegates_to_shared_backend(
    tmp_path,
    monkeypatch,
):
    snapshot_path = tmp_path / "snapshot.json"
    calls = []

    @contextmanager
    def shared_lock(path, *, timeout_seconds):
        calls.append((Path(path), timeout_seconds))
        yield

    monkeypatch.setattr(cli, "_shared_snapshot_write_lock", shared_lock)
    monkeypatch.setattr(cli, "_SNAPSHOT_LOCK_TIMEOUT_SECONDS", 2.5)

    with cli._snapshot_write_lock(snapshot_path) as lock_value:
        assert lock_value is None

    assert calls == [(snapshot_path, 2.5)]


def test_snapshot_write_lock_serializes_contenders_and_releases_on_error(
    tmp_path,
):
    assert hasattr(cli, "_snapshot_write_lock")
    path, _decision_ids = _snapshot_with_states(tmp_path, "pending")
    first_acquired = threading.Event()
    release_first = threading.Event()
    second_attempting = threading.Event()
    second_acquired = threading.Event()

    def hold_lock():
        with cli._snapshot_write_lock(path):
            first_acquired.set()
            if not release_first.wait(timeout=5):
                raise AssertionError("first lock holder was not released")
            raise RuntimeError("injected locked operation failure")

    def wait_for_lock():
        second_attempting.set()
        with cli._snapshot_write_lock(path):
            second_acquired.set()

    with ThreadPoolExecutor(max_workers=2) as executor:
        first = executor.submit(hold_lock)
        assert first_acquired.wait(timeout=5)
        second = executor.submit(wait_for_lock)
        assert second_attempting.wait(timeout=5)
        assert not second_acquired.wait(timeout=0.25)
        release_first.set()
        with pytest.raises(
            RuntimeError,
            match="injected locked operation failure",
        ):
            first.result(timeout=5)
        second.result(timeout=5)

    assert second_acquired.is_set()
    lock_path = path.with_name(f"{path.name}.tbm.lock")
    assert lock_path.read_bytes()


def test_snapshot_write_lock_times_out_then_recovers(tmp_path, monkeypatch):
    path, _decision_ids = _snapshot_with_states(tmp_path, "pending")
    lock_path = path.with_name(f"{path.name}.tbm.lock")
    monkeypatch.setattr(cli, "_SNAPSHOT_LOCK_TIMEOUT_SECONDS", 0.0)

    with cli._snapshot_write_lock(path):
        with pytest.raises(
            TimeoutError,
            match=r"timed out waiting for snapshot write lock",
        ):
            with cli._snapshot_write_lock(path):
                raise AssertionError("a contending writer must not acquire the lock")

    with cli._snapshot_write_lock(path):
        pass
    assert lock_path.read_bytes()


def test_cli_snapshot_write_lock_timeout_uses_write_exit_code(
    tmp_path,
    capsys,
    monkeypatch,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    original = path.read_bytes()
    lock_path = cli._snapshot_lock_path(path)
    monkeypatch.setattr(cli, "_SNAPSHOT_LOCK_TIMEOUT_SECONDS", 0.0)

    with cli._snapshot_write_lock(path):
        code, payload, error = _run(
            capsys,
            "outcome",
            str(path),
            decision_ids["pending"],
            "--eval-result",
            "pass",
            "--write",
        )

    assert code == 4
    assert payload is None
    assert error["error"] == {
        "kind": "write",
        "message": f"timed out waiting for snapshot write lock: {lock_path}",
        "type": "TimeoutError",
    }
    assert path.read_bytes() == original


def test_cli_snapshot_writes_serialize_across_processes(tmp_path):
    path = tmp_path / "concurrent.snapshot.json"
    store = TraceBackedMemoryStore()
    decision_ids = [
        _pending_run(store, f"concurrent_{index}")[1]
        for index in (1, 2)
    ]
    store.save_json(path)
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (str(root / "src"), existing_pythonpath)
        if item
    )
    commands = [
        [
            sys.executable,
            "-m",
            "trace_backed_memory",
            "outcome",
            str(path),
            decision_id,
            "--eval-result",
            eval_result,
            "--write",
        ]
        for decision_id, eval_result in zip(
            decision_ids,
            ("pass", "error"),
            strict=True,
        )
    ]
    processes: list[subprocess.Popen[str]] = []

    try:
        with cli._snapshot_write_lock(path):
            processes = [
                subprocess.Popen(
                    command,
                    cwd=root,
                    env=environment,
                    stdout=subprocess.PIPE,
                    stderr=subprocess.PIPE,
                    text=True,
                )
                for command in commands
            ]
            time.sleep(0.25)
            assert all(process.poll() is None for process in processes)

        for process in processes:
            stdout, stderr = process.communicate(timeout=10)
            assert process.returncode == 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"
    finally:
        for process in processes:
            if process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    restored = TraceBackedMemoryStore.load_json(path)
    outcomes = {
        log.decision_id: log.eval_result for log in restored.usage_logs
    }
    assert outcomes == {
        decision_ids[0]: "pass",
        decision_ids[1]: "error",
    }


def test_cli_dry_run_and_read_only_commands_do_not_create_write_lock(
    tmp_path,
    capsys,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    lock_path = path.with_name(f"{path.name}.tbm.lock")

    dry_run_code, dry_run_payload, dry_run_error = _run(
        capsys,
        "outcome",
        str(path),
        decision_ids["pending"],
        "--eval-result",
        "pass",
    )
    read_code, read_payload, read_error = _run(
        capsys,
        "snapshot",
        "stats",
        str(path),
    )

    assert dry_run_code == 0
    assert dry_run_error is None
    assert dry_run_payload["written"] is False
    assert read_code == 0
    assert read_error is None
    assert read_payload["snapshot_version"] == 2
    assert not lock_path.exists()


def test_module_entry_point_emits_one_json_value(tmp_path):
    path, _decision_ids = _snapshot_with_states(tmp_path, "complete")
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (str(root / "src"), existing_pythonpath)
        if item
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trace_backed_memory",
            "snapshot",
            "stats",
            str(path),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.endswith("\n")
    assert result.stdout.count("\n") == 1
    assert json.loads(result.stdout) == {
        "counts": {
            "failure_cases": 0,
            "lessons": 0,
            "project_policies": 0,
            "traces": 1,
            "usage_logs": 1,
        },
        "snapshot_version": 2,
    }


def test_module_entry_point_seals_outcome_as_dry_run(tmp_path):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    decision_id = decision_ids["pending"]
    original = path.read_bytes()
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (str(root / "src"), existing_pythonpath)
        if item
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trace_backed_memory",
            "outcome",
            str(path),
            decision_id,
            "--eval-result",
            "pass",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.count("\n") == 1
    assert json.loads(result.stdout) == {
        "changed": True,
        "decision_id": decision_id,
        "eval_result": "pass",
        "memory_caused_failure": False,
        "previous_eval_result": None,
        "previous_memory_caused_failure": False,
        "written": False,
    }
    assert path.read_bytes() == original


def test_module_entry_point_completes_batch_as_dry_run(tmp_path):
    store = TraceBackedMemoryStore()
    _first_trace, first_decision_id = _pending_run(store, "module_batch_first")
    _second_trace, second_decision_id = _pending_run(store, "module_batch_second")
    path = tmp_path / "module-batch.snapshot.json"
    store.save_json(path)
    original = path.read_bytes()
    measurements_path = _write_measurements(
        tmp_path / "module-measurements.json",
        [
            {"decision_id": second_decision_id, "eval_result": "error"},
            {"decision_id": first_decision_id, "eval_result": "pass"},
        ],
    )
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (str(root / "src"), existing_pythonpath)
        if item
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trace_backed_memory",
            "complete-batch",
            str(path),
            str(measurements_path),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.count("\n") == 1
    assert json.loads(result.stdout)["decision_ids"] == [
        second_decision_id,
        first_decision_id,
    ]
    assert path.read_bytes() == original


def test_cli_broken_stdout_returns_internal_error(tmp_path, capsys, monkeypatch):
    path, _decision_ids = _snapshot_with_states(tmp_path, "complete")

    class BrokenStream:
        def write(self, _value):
            raise BrokenPipeError("injected closed stdout")

    monkeypatch.setattr(cli.sys, "stdout", BrokenStream())

    code = cli.main(["snapshot", "stats", str(path)])
    captured = capsys.readouterr()

    assert code == 1
    assert json.loads(captured.err) == {
        "error": {
            "kind": "internal",
            "message": "injected closed stdout",
            "type": "BrokenPipeError",
        }
    }


def test_cli_broken_stdout_after_write_does_not_report_false_failure(
    tmp_path,
    capsys,
    monkeypatch,
):
    path, _decision_ids = _snapshot_with_states(tmp_path, "trace_only_pass")
    original = path.read_bytes()

    class BrokenStream:
        def write(self, _value):
            raise BrokenPipeError("injected closed stdout")

    monkeypatch.setattr(cli.sys, "stdout", BrokenStream())

    code = cli.main(["recover-ready", str(path), "--write"])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    assert path.read_bytes() != original
    assert TraceBackedMemoryStore.load_json(path).memory_run_audits()[0].status == (
        "complete"
    )


def test_cli_outcome_broken_stdout_after_write_remains_successful(
    tmp_path,
    capsys,
    monkeypatch,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    decision_id = decision_ids["pending"]
    original = path.read_bytes()

    class BrokenStream:
        def write(self, _value):
            raise BrokenPipeError("injected closed outcome stdout")

    monkeypatch.setattr(cli.sys, "stdout", BrokenStream())

    code = cli.main(
        [
            "outcome",
            str(path),
            decision_id,
            "--eval-result",
            "pass",
            "--write",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    assert path.read_bytes() != original
    restored = TraceBackedMemoryStore.load_json(path)
    assert restored.usage_logs[0].eval_result == "pass"
    assert restored.traces[restored.usage_logs[0].trace_id].eval_result == "unknown"


def test_cli_outcome_broken_stdout_dry_run_is_internal_and_does_not_write(
    tmp_path,
    capsys,
    monkeypatch,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    original = path.read_bytes()

    class BrokenStream:
        def write(self, _value):
            raise BrokenPipeError("injected closed outcome stdout")

    monkeypatch.setattr(cli.sys, "stdout", BrokenStream())

    code = cli.main(
        [
            "outcome",
            str(path),
            decision_ids["pending"],
            "--eval-result",
            "pass",
        ]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert json.loads(captured.err) == {
        "error": {
            "kind": "internal",
            "message": "injected closed outcome stdout",
            "type": "BrokenPipeError",
        }
    }
    assert path.read_bytes() == original


def test_cli_complete_batch_broken_stdout_after_write_remains_successful(
    tmp_path,
    capsys,
    monkeypatch,
):
    store = TraceBackedMemoryStore()
    _first_trace, first_decision_id = _pending_run(store, "pipe_batch_first")
    _second_trace, second_decision_id = _pending_run(store, "pipe_batch_second")
    path = tmp_path / "pipe-batch.snapshot.json"
    store.save_json(path)
    original = path.read_bytes()
    measurements_path = _write_measurements(
        tmp_path / "pipe-measurements.json",
        [
            {"decision_id": first_decision_id, "eval_result": "pass"},
            {"decision_id": second_decision_id, "eval_result": "error"},
        ],
    )

    class BrokenStream:
        def write(self, _value):
            raise BrokenPipeError("injected closed stdout")

    monkeypatch.setattr(cli.sys, "stdout", BrokenStream())

    code = cli.main(
        [
            "complete-batch",
            str(path),
            str(measurements_path),
            "--write",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    assert path.read_bytes() != original
    restored = TraceBackedMemoryStore.load_json(path)
    assert all(audit.status == "complete" for audit in restored.memory_run_audits())


def test_cli_broken_stdout_after_resource_export_does_not_invite_retry(
    tmp_path,
    capsys,
    monkeypatch,
):
    destination = tmp_path / "postgres.sql"

    class BrokenStream:
        def write(self, _value):
            raise BrokenPipeError("injected closed resource stdout")

    monkeypatch.setattr(cli.sys, "stdout", BrokenStream())

    code = cli.main(
        [
            "resource",
            "export",
            "schemas/postgres.sql",
            str(destination),
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    assert destination.read_bytes() == (
        Path(__file__).resolve().parents[1] / "schemas" / "postgres.sql"
    ).read_bytes()


def test_cli_broken_stdout_after_lessons_export_does_not_invite_retry(
    tmp_path,
    capsys,
    monkeypatch,
):
    store, _active_lessons = _lesson_portability_store(include_lessons=True)
    snapshot_path = tmp_path / "pipe-export.snapshot.json"
    destination = tmp_path / "pipe-export.lessons.yaml"
    store.save_json(snapshot_path)

    class BrokenStream:
        def write(self, _value):
            raise BrokenPipeError("injected closed lessons export stdout")

    monkeypatch.setattr(cli.sys, "stdout", BrokenStream())

    code = cli.main(
        [
            "lessons",
            "export",
            str(snapshot_path),
            str(destination),
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    assert b"lesson_cli_export_first" in destination.read_bytes()


def test_cli_lessons_import_broken_stdout_tracks_snapshot_publication(
    tmp_path,
    capsys,
    monkeypatch,
):
    source, active_lessons = _lesson_portability_store(include_lessons=True)
    yaml_path = tmp_path / "pipe-import.lessons.yaml"
    source.save_lessons_yaml(yaml_path)
    target, _ = _lesson_portability_store(include_lessons=False)
    snapshot_path = tmp_path / "pipe-import.snapshot.json"
    target.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()

    class BrokenStream:
        def write(self, _value):
            raise BrokenPipeError("injected closed lessons import stdout")

    monkeypatch.setattr(cli.sys, "stdout", BrokenStream())

    code = cli.main(
        [
            "lessons",
            "import",
            str(snapshot_path),
            str(yaml_path),
        ]
    )
    captured = capsys.readouterr()

    assert code == 1
    assert json.loads(captured.err)["error"]["kind"] == "internal"
    assert snapshot_path.read_bytes() == original_snapshot

    code = cli.main(
        [
            "lessons",
            "import",
            str(snapshot_path),
            str(yaml_path),
            "--write",
        ]
    )
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    restored = TraceBackedMemoryStore.load_json(snapshot_path)
    assert list(restored.lessons) == [
        lesson.lesson_id for lesson in active_lessons
    ]


def test_cli_obsolete_broken_stdout_tracks_snapshot_publication(
    tmp_path,
    capsys,
    monkeypatch,
):
    store, records = _obsolescence_store()
    snapshot_path = tmp_path / "pipe-obsolete.snapshot.json"
    store.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()

    class BrokenStream:
        def write(self, _value):
            raise BrokenPipeError("injected closed obsolete stdout")

    monkeypatch.setattr(cli.sys, "stdout", BrokenStream())
    command = [
        "obsolete",
        str(snapshot_path),
        "project-policy",
        records["active_policy_id"],
    ]

    code = cli.main(command)
    captured = capsys.readouterr()

    assert code == 1
    assert json.loads(captured.err)["error"]["kind"] == "internal"
    assert snapshot_path.read_bytes() == original_snapshot

    code = cli.main([*command, "--write"])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    restored = TraceBackedMemoryStore.load_json(snapshot_path)
    assert (
        restored.project_policies[records["active_policy_id"]].status
        == "obsolete"
    )


def test_cli_obsolete_batch_broken_stdout_tracks_snapshot_publication(
    tmp_path,
    capsys,
    monkeypatch,
):
    store, records = _obsolescence_store()
    snapshot_path = tmp_path / "pipe-obsolete-batch.snapshot.json"
    store.save_json(snapshot_path)
    original_snapshot = snapshot_path.read_bytes()
    requests_path = _write_obsolescence_requests(
        tmp_path / "pipe-obsolete-batch.json",
        [
            {
                "memory_kind": "project_policy",
                "memory_id": records["active_policy_id"],
            }
        ],
    )

    class BrokenStream:
        def write(self, _value):
            raise BrokenPipeError("injected closed obsolete batch stdout")

    monkeypatch.setattr(cli.sys, "stdout", BrokenStream())
    command = [
        "obsolete-batch",
        str(snapshot_path),
        str(requests_path),
    ]

    code = cli.main(command)
    captured = capsys.readouterr()

    assert code == 1
    assert json.loads(captured.err)["error"]["kind"] == "internal"
    assert snapshot_path.read_bytes() == original_snapshot

    code = cli.main([*command, "--write"])
    captured = capsys.readouterr()

    assert code == 0
    assert captured.err == ""
    restored = TraceBackedMemoryStore.load_json(snapshot_path)
    assert (
        restored.project_policies[records["active_policy_id"]].status
        == "obsolete"
    )


def test_cli_bounds_error_messages(capsys):
    code = cli._emit_error("internal", RuntimeError("x" * 3000), 1)
    captured = capsys.readouterr()
    error = json.loads(captured.err)["error"]

    assert code == 1
    assert error["kind"] == "internal"
    assert error["type"] == "RuntimeError"
    assert len(error["message"]) == 2048
    assert error["message"].endswith("...")


def test_cli_unexpected_load_failure_is_structured_internal_error(
    capsys,
    monkeypatch,
):
    def reject_load(_path):
        raise RuntimeError("injected loader defect")

    monkeypatch.setattr(
        TraceBackedMemoryStore,
        "load_json",
        staticmethod(reject_load),
    )

    code, payload, error = _run(capsys, "audit", "snapshot.json")

    assert code == 1
    assert payload is None
    assert error == {
        "error": {
            "kind": "internal",
            "message": "injected loader defect",
            "type": "RuntimeError",
        }
    }


def test_cli_complete_batch_rejects_over_byte_budget_without_writing(
    tmp_path,
    capsys,
    monkeypatch,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    original = path.read_bytes()
    measurements_path = _write_measurements(
        tmp_path / "bounded-measurements.json",
        [
            {
                "decision_id": decision_ids["pending"],
                "eval_result": "pass",
            }
        ],
    )
    monkeypatch.setattr(
        cli,
        "CLI_JSON_FILE_MAX_BYTES",
        len(measurements_path.read_bytes()) - 1,
    )

    code, payload, error = _run(
        capsys,
        "complete-batch",
        str(path),
        str(measurements_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "measurements file exceeds maximum size" in error["error"]["message"]
    assert path.read_bytes() == original


@pytest.mark.parametrize(
    ("limit_name", "limit", "message_fragment"),
    [
        ("CLI_JSON_MAX_ITEMS", 1, "more than 1 items"),
        ("CLI_JSON_MAX_NODES", 3, "more than 3 nodes"),
        ("CLI_JSON_MAX_DEPTH", 1, "maximum depth of 1"),
    ],
)
def test_cli_complete_batch_rejects_json_budget_overages_without_writing(
    tmp_path,
    capsys,
    monkeypatch,
    limit_name,
    limit,
    message_fragment,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    decision_id = decision_ids["pending"]
    original = path.read_bytes()
    measurements = [
        {
            "decision_id": decision_id,
            "eval_result": "pass",
            "tool_outputs": [{"nested": {"value": 1}}],
        }
    ]
    if limit_name == "CLI_JSON_MAX_ITEMS":
        measurements.append(
            {"decision_id": decision_id, "eval_result": "pass"}
        )
    measurements_path = _write_measurements(
        tmp_path / "budgeted-measurements.json",
        measurements,
    )
    monkeypatch.setattr(cli, limit_name, limit)

    code, payload, error = _run(
        capsys,
        "complete-batch",
        str(path),
        str(measurements_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert message_fragment in error["error"]["message"]
    assert path.read_bytes() == original


def test_cli_complete_rejects_tool_output_item_budget_without_writing(
    tmp_path,
    capsys,
    monkeypatch,
):
    path, decision_ids = _snapshot_with_states(tmp_path, "pending")
    decision_id = decision_ids["pending"]
    trace_id = TraceBackedMemoryStore.load_json(path).memory_run_audits()[0].trace_id
    original = path.read_bytes()
    outputs_path = _write_measurements(
        tmp_path / "bounded-tool-outputs.json",
        [{"value": 1}, {"value": 2}],
    )
    monkeypatch.setattr(cli, "CLI_JSON_MAX_ITEMS", 1)

    code, payload, error = _run(
        capsys,
        "complete",
        str(path),
        trace_id,
        decision_id,
        "--eval-result",
        "pass",
        "--tool-outputs-file",
        str(outputs_path),
        "--write",
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "tool outputs JSON contains more than 1 items" in error["error"]["message"]
    assert path.read_bytes() == original


def test_cli_pr_report_captures_ancestry_and_reuses_change_set(
    tmp_path,
    capsys,
    monkeypatch,
):
    snapshot_path = _pr_report_snapshot(tmp_path)
    context_path, change_set_path = _pr_report_documents(tmp_path)
    original_snapshot = snapshot_path.read_bytes()
    repo_path = tmp_path / "repo"
    seen_change_sets = []
    original_anchor_method = TraceBackedMemoryStore.pr_report_commit_anchors
    original_report_method = TraceBackedMemoryStore.pr_memory_report

    def tracked_anchors(self, context, *, change_set=None):
        seen_change_sets.append(change_set)
        return original_anchor_method(self, context, change_set=change_set)

    def tracked_report(
        self,
        context,
        *,
        changed_fields=None,
        change_set=None,
        commit_ancestry=None,
    ):
        seen_change_sets.append(change_set)
        return original_report_method(
            self,
            context,
            changed_fields=changed_fields,
            change_set=change_set,
            commit_ancestry=commit_ancestry,
        )

    def capture(current_commit_sha, anchors, repo_path=None):
        assert current_commit_sha == "current-pr-head"
        assert anchors == ("source-new", "source-old")
        assert repo_path == str(tmp_path / "repo")
        return CommitAncestryEvidence(
            current_commit_sha=current_commit_sha,
            commit_relations=(
                ("source-new", False),
                ("source-old", True),
            ),
        )

    monkeypatch.setattr(
        TraceBackedMemoryStore,
        "pr_report_commit_anchors",
        tracked_anchors,
    )
    monkeypatch.setattr(
        TraceBackedMemoryStore,
        "pr_memory_report",
        tracked_report,
    )
    monkeypatch.setattr(cli, "capture_commit_ancestry", capture)

    code, payload, error = _run(
        capsys,
        "pr-report",
        str(snapshot_path),
        str(context_path),
        str(change_set_path),
        "--repo-path",
        str(repo_path),
    )

    assert code == 0
    assert error is None
    assert len(seen_change_sets) == 2
    assert seen_change_sets[0] is seen_change_sets[1]
    assert payload == {
        "commit_ancestry": {
            "commit_relations": [
                ["source-new", False],
                ["source-old", True],
            ],
            "current_commit_sha": "current-pr-head",
        },
        "report": {
            "related_case_ids": ["case_cli_old"],
            "related_case_provenance": [
                {
                    "case_id": "case_cli_old",
                    "commit_sha": "source-old",
                    "failure_type": "invalid_tool_argument",
                    "fix_commit_sha": "fix-old",
                    "matched_change_endpoint": "old",
                    "source_trace_id": "trace_cli_pr_old",
                    "trace_uri": "trace://cli-pr-old",
                }
            ],
            "suggested_regression_tests": [
                "Run invalid_tool_argument regression for tool "
                "search_docs before merging."
            ],
            "warnings": [
                "model change touches known failure case "
                "case_cli_old for search_docs."
            ],
        },
    }
    assert snapshot_path.read_bytes() == original_snapshot


@pytest.mark.parametrize(
    ("target", "document", "message_fragment"),
    [
        ("context", [], "PR context JSON must be an object"),
        (
            "context",
            {
                "mode": "regression",
                "repo": "repo_cli",
                "commit_sha": "current-pr-head",
                "unknown": "value",
            },
            "PR context has unknown field: unknown",
        ),
        (
            "context",
            {"mode": "regression", "repo": "repo_cli"},
            "PR context missing required field: commit_sha",
        ),
        (
            "context",
            {
                "mode": "regression",
                "repo": "repo_cli",
                "commit_sha": "current-pr-head",
                "model": 1,
            },
            "context model must be a non-empty string",
        ),
        ("change_set", [], "PR change set JSON must be an object"),
        (
            "change_set",
            {},
            "PR change set missing required field: field_changes",
        ),
        (
            "change_set",
            {"field_changes": []},
            "field_changes must be a non-empty array",
        ),
        (
            "change_set",
            {
                "field_changes": [
                    {"field_name": "model", "old_value": "model-old"}
                ]
            },
            "change 1 missing required field: new_value",
        ),
        (
            "change_set",
            {
                "field_changes": [
                    {
                        "field_name": "model",
                        "old_value": "model-old",
                        "new_value": "model-new",
                        "unknown": "value",
                    }
                ]
            },
            "change 1 has unknown field: unknown",
        ),
        (
            "change_set",
            {
                "field_changes": [
                    {
                        "field_name": "model",
                        "old_value": 1,
                        "new_value": "model-new",
                    }
                ]
            },
            "change 1 old_value must be a string or null",
        ),
    ],
)
def test_cli_pr_report_rejects_malformed_documents_as_input(
    tmp_path,
    capsys,
    monkeypatch,
    target,
    document,
    message_fragment,
):
    snapshot_path = _pr_report_snapshot(tmp_path)
    if target == "context":
        context_path, change_set_path = _pr_report_documents(
            tmp_path,
            context=document,
        )
    else:
        context_path, change_set_path = _pr_report_documents(
            tmp_path,
            change_set=document,
        )
    monkeypatch.setattr(
        cli,
        "capture_commit_ancestry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Git capture must not run for invalid input")
        ),
    )

    code, payload, error = _run(
        capsys,
        "pr-report",
        str(snapshot_path),
        str(context_path),
        str(change_set_path),
        "--repo-path",
        str(tmp_path),
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert message_fragment in error["error"]["message"]


def test_cli_pr_report_rejects_duplicate_context_key_before_git(
    tmp_path,
    capsys,
    monkeypatch,
):
    snapshot_path = _pr_report_snapshot(tmp_path)
    context_path, change_set_path = _pr_report_documents(tmp_path)
    context_path.write_text(
        """
        {
          "mode": "regression",
          "mode": "production",
          "repo": "repo_cli",
          "commit_sha": "current-pr-head"
        }
        """,
        encoding="utf-8",
    )
    monkeypatch.setattr(
        cli,
        "capture_commit_ancestry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Git capture must not run for duplicate JSON keys")
        ),
    )

    code, payload, error = _run(
        capsys,
        "pr-report",
        str(snapshot_path),
        str(context_path),
        str(change_set_path),
        "--repo-path",
        str(tmp_path),
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert (
        "PR context JSON contains duplicate object key: mode"
        in error["error"]["message"]
    )


def test_cli_pr_report_accepts_null_change_endpoint(tmp_path, capsys):
    snapshot_path = tmp_path / "empty-null-endpoint.snapshot.json"
    TraceBackedMemoryStore().save_json(snapshot_path)
    context_path, change_set_path = _pr_report_documents(
        tmp_path,
        context={
            "mode": "regression",
            "repo": "repo_cli",
            "commit_sha": "current-pr-head",
            "model": None,
        },
        change_set={
            "field_changes": [
                {
                    "field_name": "model",
                    "old_value": "model-old",
                    "new_value": None,
                }
            ]
        },
    )

    code, payload, error = _run(
        capsys,
        "pr-report",
        str(snapshot_path),
        str(context_path),
        str(change_set_path),
        "--repo-path",
        str(tmp_path / "unused-for-empty-anchors"),
    )

    assert code == 0
    assert error is None
    assert payload["commit_ancestry"] == {
        "commit_relations": [],
        "current_commit_sha": "current-pr-head",
    }
    assert payload["report"]["related_case_ids"] == []


@pytest.mark.parametrize(
    ("change_set", "message_fragment"),
    [
        (
            {
                "field_changes": [
                    {
                        "field_name": "model_family",
                        "old_value": "old",
                        "new_value": "new",
                    }
                ]
            },
            "unsupported change_set fields: model_family",
        ),
        (
            {
                "field_changes": [
                    {
                        "field_name": "model",
                        "old_value": "model-old",
                        "new_value": "model-new",
                    },
                    {
                        "field_name": "model",
                        "old_value": "other-old",
                        "new_value": "model-new",
                    },
                ]
            },
            "duplicate change_set fields: model",
        ),
        (
            {
                "field_changes": [
                    {
                        "field_name": "model",
                        "old_value": "model-old",
                        "new_value": "other-new",
                    }
                ]
            },
            "change_set model new value must match context",
        ),
        (
            {
                "field_changes": [
                    {
                        "field_name": "model",
                        "old_value": f"model-old-{index}",
                        "new_value": "model-new",
                    }
                    for index in range(7)
                ]
            },
            "change_set.field_changes accepts at most 6 entries",
        ),
    ],
)
def test_cli_pr_report_maps_change_set_domain_errors_to_input(
    tmp_path,
    capsys,
    monkeypatch,
    change_set,
    message_fragment,
):
    snapshot_path = _pr_report_snapshot(tmp_path)
    context_path, change_set_path = _pr_report_documents(
        tmp_path,
        change_set=change_set,
    )
    monkeypatch.setattr(
        cli,
        "capture_commit_ancestry",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(
            AssertionError("Git capture must not run for invalid change sets")
        ),
    )

    code, payload, error = _run(
        capsys,
        "pr-report",
        str(snapshot_path),
        str(context_path),
        str(change_set_path),
        "--repo-path",
        str(tmp_path),
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert message_fragment in error["error"]["message"]


def test_cli_pr_report_enforces_document_and_item_budgets(
    tmp_path,
    capsys,
    monkeypatch,
):
    snapshot_path = _pr_report_snapshot(tmp_path)
    context_path, change_set_path = _pr_report_documents(tmp_path)
    monkeypatch.setattr(
        cli,
        "CLI_JSON_FILE_MAX_BYTES",
        len(context_path.read_bytes()) - 1,
    )

    code, payload, error = _run(
        capsys,
        "pr-report",
        str(snapshot_path),
        str(context_path),
        str(change_set_path),
        "--repo-path",
        str(tmp_path),
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "PR context file exceeds maximum size" in error["error"]["message"]

    monkeypatch.setattr(cli, "CLI_JSON_FILE_MAX_BYTES", 8 * 1024 * 1024)
    monkeypatch.setattr(cli, "CLI_JSON_MAX_ITEMS", 1)
    context_path, change_set_path = _pr_report_documents(
        tmp_path,
        change_set={
            "field_changes": [
                {
                    "field_name": "model",
                    "old_value": "model-old",
                    "new_value": "model-new",
                },
                {
                    "field_name": "eval_suite",
                    "old_value": "eval-old",
                    "new_value": None,
                },
            ]
        },
    )

    code, payload, error = _run(
        capsys,
        "pr-report",
        str(snapshot_path),
        str(context_path),
        str(change_set_path),
        "--repo-path",
        str(tmp_path),
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "field_changes contains more than 1 items" in error["error"]["message"]


def test_cli_pr_report_maps_git_capture_failure_to_state_without_writing(
    tmp_path,
    capsys,
    monkeypatch,
):
    snapshot_path = _pr_report_snapshot(tmp_path)
    context_path, change_set_path = _pr_report_documents(tmp_path)
    original = snapshot_path.read_bytes()

    def reject_capture(*_args, **_kwargs):
        raise CommitAncestryCaptureError("missing PR commit object")

    monkeypatch.setattr(cli, "capture_commit_ancestry", reject_capture)

    code, payload, error = _run(
        capsys,
        "pr-report",
        str(snapshot_path),
        str(context_path),
        str(change_set_path),
        "--repo-path",
        str(tmp_path),
    )

    assert code == 3
    assert payload is None
    assert error["error"] == {
        "kind": "state",
        "message": "missing PR commit object",
        "type": "CommitAncestryCaptureError",
    }
    assert snapshot_path.read_bytes() == original


def test_cli_pr_report_maps_report_rejection_to_state_without_writing(
    tmp_path,
    capsys,
    monkeypatch,
):
    snapshot_path = _pr_report_snapshot(tmp_path)
    context_path, change_set_path = _pr_report_documents(tmp_path)
    original = snapshot_path.read_bytes()

    def capture(current_commit_sha, anchors, repo_path=None):
        return CommitAncestryEvidence(
            current_commit_sha,
            tuple((anchor, True) for anchor in anchors),
        )

    def reject_report(_store, _context, **_kwargs):
        raise ValueError("injected PR report state failure")

    monkeypatch.setattr(cli, "capture_commit_ancestry", capture)
    monkeypatch.setattr(
        TraceBackedMemoryStore,
        "pr_memory_report",
        reject_report,
    )

    code, payload, error = _run(
        capsys,
        "pr-report",
        str(snapshot_path),
        str(context_path),
        str(change_set_path),
        "--repo-path",
        str(tmp_path),
    )

    assert code == 3
    assert payload is None
    assert error["error"] == {
        "kind": "state",
        "message": "injected PR report state failure",
        "type": "ValueError",
    }
    assert snapshot_path.read_bytes() == original


def test_cli_pr_report_requires_explicit_repo_path(tmp_path, capsys):
    snapshot_path = _pr_report_snapshot(tmp_path)
    context_path, change_set_path = _pr_report_documents(tmp_path)

    code, payload, error = _run(
        capsys,
        "pr-report",
        str(snapshot_path),
        str(context_path),
        str(change_set_path),
    )

    assert code == 2
    assert payload is None
    assert error["error"]["kind"] == "input"
    assert "--repo-path" in error["error"]["message"]


def test_module_entry_point_emits_read_only_pr_report(tmp_path):
    snapshot_path = tmp_path / "empty-pr-report.snapshot.json"
    TraceBackedMemoryStore().save_json(snapshot_path)
    original = snapshot_path.read_bytes()
    context_path, change_set_path = _pr_report_documents(tmp_path)
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (str(root / "src"), existing_pythonpath)
        if item
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trace_backed_memory",
            "pr-report",
            str(snapshot_path),
            str(context_path),
            str(change_set_path),
            "--repo-path",
            str(tmp_path / "not-needed-for-empty-anchors"),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.count("\n") == 1
    assert json.loads(result.stdout) == {
        "commit_ancestry": {
            "commit_relations": [],
            "current_commit_sha": "current-pr-head",
        },
        "report": {
            "related_case_ids": [],
            "related_case_provenance": [],
            "suggested_regression_tests": [],
            "warnings": [],
        },
    }
    assert snapshot_path.read_bytes() == original


def test_module_entry_point_exports_and_imports_lessons(tmp_path):
    source, active_lessons = _lesson_portability_store(include_lessons=True)
    source_snapshot = tmp_path / "module-export.snapshot.json"
    destination = tmp_path / "module-export.lessons.yaml"
    source.save_json(source_snapshot)
    target, _ = _lesson_portability_store(include_lessons=False)
    target_snapshot = tmp_path / "module-import.snapshot.json"
    target.save_json(target_snapshot)
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (str(root / "src"), existing_pythonpath)
        if item
    )

    export_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trace_backed_memory",
            "lessons",
            "export",
            str(source_snapshot),
            str(destination),
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert export_result.returncode == 0, export_result.stderr
    assert export_result.stderr == ""
    assert export_result.stdout.count("\n") == 1
    assert json.loads(export_result.stdout)["exported_lesson_ids"] == [
        lesson.lesson_id for lesson in active_lessons
    ]

    import_result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trace_backed_memory",
            "lessons",
            "import",
            str(target_snapshot),
            str(destination),
            "--write",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert import_result.returncode == 0, import_result.stderr
    assert import_result.stderr == ""
    assert import_result.stdout.count("\n") == 1
    assert json.loads(import_result.stdout) == {
        "imported_count": 2,
        "imported_lesson_ids": [
            lesson.lesson_id for lesson in active_lessons
        ],
        "written": True,
    }
    restored = TraceBackedMemoryStore.load_json(target_snapshot)
    assert list(restored.lessons) == [
        lesson.lesson_id for lesson in active_lessons
    ]


def test_module_entry_point_obsoletes_failure_case_with_cascade(tmp_path):
    store, records = _obsolescence_store()
    snapshot_path = tmp_path / "module-obsolete.snapshot.json"
    store.save_json(snapshot_path)
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (str(root / "src"), existing_pythonpath)
        if item
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trace_backed_memory",
            "obsolete",
            str(snapshot_path),
            "failure-case",
            records["case_id"],
            "--write",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.count("\n") == 1
    payload = json.loads(result.stdout)
    assert payload["cascaded_lesson_ids"] == sorted(
        records["dependent_lesson_ids"]
    )
    assert payload["written"] is True
    restored = TraceBackedMemoryStore.load_json(snapshot_path)
    assert restored.failure_cases[records["case_id"]].status == "obsolete"
    assert all(
        restored.lessons[lesson_id].status == "obsolete"
        for lesson_id in records["dependent_lesson_ids"]
    )


def test_module_entry_point_obsoletes_an_atomic_memory_batch(tmp_path):
    store, records = _obsolescence_store()
    snapshot_path = tmp_path / "module-obsolete-batch.snapshot.json"
    store.save_json(snapshot_path)
    requests_path = _write_obsolescence_requests(
        tmp_path / "module-obsolete-batch.json",
        [
            {"memory_kind": "failure_case", "memory_id": records["case_id"]},
            {
                "memory_kind": "project_policy",
                "memory_id": records["active_policy_id"],
            },
        ],
    )
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (str(root / "src"), existing_pythonpath)
        if item
    )

    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "trace_backed_memory",
            "obsolete-batch",
            str(snapshot_path),
            str(requests_path),
            "--write",
        ],
        cwd=root,
        env=environment,
        capture_output=True,
        text=True,
        check=False,
    )

    assert result.returncode == 0, result.stderr
    assert result.stderr == ""
    assert result.stdout.count("\n") == 1
    payload = json.loads(result.stdout)
    assert payload["affected_count"] == 4
    assert payload["changed_count"] == 2
    assert payload["cascaded_lesson_ids"] == sorted(
        records["dependent_lesson_ids"]
    )
    assert payload["written"] is True
    restored = TraceBackedMemoryStore.load_json(snapshot_path)
    assert restored.failure_cases[records["case_id"]].status == "obsolete"
    assert (
        restored.project_policies[records["active_policy_id"]].status
        == "obsolete"
    )
