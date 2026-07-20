import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

import trace_backed_memory.cli as cli
from trace_backed_memory import (
    MemoryContext,
    MemoryDecision,
    Trace,
    TraceBackedMemoryStore,
)


def _pending_run(
    store: TraceBackedMemoryStore,
    suffix: str,
) -> tuple[Trace, str]:
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


def test_cli_resource_commands_list_read_and_export(tmp_path, capsys):
    code, payload, error = _run(capsys, "resource", "list")

    assert code == 0
    assert error is None
    assert len(payload["resources"]) == 18
    names = [item["name"] for item in payload["resources"]]
    assert names == sorted(names)
    assert "schemas/postgres.sql" in names

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
