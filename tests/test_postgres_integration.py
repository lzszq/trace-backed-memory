from __future__ import annotations

import os
import shutil
import socket
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class PostgresCluster:
    psql: str
    env: dict[str, str]
    root: Path

    def run(self, sql: str, *, timeout: float = 15.0) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [self.psql, "-X", "-v", "ON_ERROR_STOP=1", "-Atqc", sql],
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def load_schema(self) -> None:
        result = subprocess.run(
            [
                self.psql,
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                str(ROOT / "schemas" / "postgres.sql"),
            ],
            env=self.env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=20,
            check=False,
        )
        assert result.returncode == 0, result.stderr

    def spawn(self, name: str, sql: str) -> tuple[subprocess.Popen[bytes], Path, Path]:
        stdout_path = self.root / f"{name}.stdout"
        stderr_path = self.root / f"{name}.stderr"
        child_env = {**self.env, "PGAPPNAME": name}
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            process = subprocess.Popen(
                [self.psql, "-X", "-v", "ON_ERROR_STOP=1", "-q", "-c", sql],
                env=child_env,
                stdin=subprocess.DEVNULL,
                stdout=stdout_file,
                stderr=stderr_file,
                close_fds=True,
            )
        return process, stdout_path, stderr_path


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


@pytest.fixture
def postgres_cluster(tmp_path: Path):
    executables = {
        name: shutil.which(name) for name in ("initdb", "pg_ctl", "psql")
    }
    missing = sorted(name for name, path in executables.items() if path is None)
    if missing:
        pytest.skip("PostgreSQL executables unavailable: " + ", ".join(missing))

    root = tmp_path / "postgres-cluster"
    data = root / "data"
    log = root / "postgres.log"
    root.mkdir()
    started = False
    env = {
        **os.environ,
        "PGHOST": "127.0.0.1",
        "PGPORT": str(_free_port()),
        "PGUSER": "postgres",
        "PGDATABASE": "postgres",
        "PGCONNECT_TIMEOUT": "5",
        "PGOPTIONS": "-c statement_timeout=10000",
    }
    try:
        init = subprocess.run(
            [
                executables["initdb"],
                "-D",
                str(data),
                "-A",
                "trust",
                "-U",
                "postgres",
                "--no-locale",
                "--encoding=UTF8",
            ],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=30,
            check=False,
        )
        if init.returncode != 0:
            error = f"{init.stdout}\n{init.stderr}".lower()
            if "cannot be run as root" in error or "must not be run as root" in error:
                pytest.skip("initdb cannot legally run as the current user")
            pytest.fail(f"initdb failed:\n{init.stdout}\n{init.stderr}")

        start = subprocess.run(
            [
                executables["pg_ctl"],
                "-D",
                str(data),
                "-o",
                f"-F -p {env['PGPORT']} -h 127.0.0.1",
                "-l",
                str(log),
                "-w",
                "-t",
                "20",
                "start",
            ],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if start.returncode != 0:
            details = log.read_text(encoding="utf-8", errors="replace") if log.exists() else ""
            pytest.fail(f"pg_ctl start failed:\n{details}")
        started = True
        yield PostgresCluster(executables["psql"], env, root)
    finally:
        try:
            if data.exists():
                subprocess.run(
                    [
                        executables["pg_ctl"],
                        "-D",
                        str(data),
                        "-m",
                        "immediate",
                        "-w",
                        "-t",
                        "20",
                        "stop",
                    ],
                    env=env,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=30,
                    check=False,
                )
        finally:
            resolved_root = root.resolve()
            assert resolved_root.is_relative_to(tmp_path.resolve())
            if started or root.exists():
                shutil.rmtree(resolved_root, ignore_errors=False)


def _assert_sql_succeeds(cluster: PostgresCluster, sql: str) -> str:
    result = cluster.run(sql)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _assert_sql_fails(cluster: PostgresCluster, sql: str, message: str) -> None:
    result = cluster.run(sql)
    assert result.returncode != 0, "SQL unexpectedly succeeded"
    assert message in result.stderr, result.stderr


def _wait_for_activity(
    cluster: PostgresCluster,
    application_name: str,
    wait_event_type: str,
    process: subprocess.Popen[bytes],
    *,
    timeout: float = 5.0,
) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        activity = cluster.run(
            "SELECT coalesce(wait_event_type, '') "
            "FROM pg_stat_activity "
            f"WHERE application_name = '{application_name}'"
        )
        assert activity.returncode == 0, activity.stderr
        if wait_event_type in activity.stdout.splitlines():
            return
        if process.poll() is not None:
            break
        time.sleep(0.05)
    pytest.fail(
        f"session {application_name!r} did not enter {wait_event_type!r} wait"
    )


def _wait_process(
    process: subprocess.Popen[bytes],
    stdout_path: Path,
    stderr_path: Path,
    *,
    expected_returncode: int,
    timeout: float = 10.0,
) -> str:
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        pytest.fail("PostgreSQL test session exceeded its bounded timeout")
    stdout = stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr = stderr_path.read_text(encoding="utf-8", errors="replace")
    assert returncode == expected_returncode, f"stdout:\n{stdout}\nstderr:\n{stderr}"
    return stderr


def _seed_verified_case(cluster: PostgresCluster, suffix: str) -> None:
    _assert_sql_succeeds(
        cluster,
        f"""
        INSERT INTO traces(trace_id, run_id, commit_sha, repo)
        VALUES ('trace_{suffix}', 'run_{suffix}', 'commit_{suffix}', 'repo');
        INSERT INTO failure_cases(
          case_id, source_trace_id, commit_sha, failure_type, symptom,
          fix, fix_commit_sha, regression_passed, status
        ) VALUES (
          'case_{suffix}', 'trace_{suffix}', 'commit_{suffix}', 'tool_error',
          'symptom', 'fix', 'fix_commit_{suffix}', true, 'verified'
        );
        """,
    )


def test_postgres_registry_lifecycle_and_two_session_serialization(
    postgres_cluster: PostgresCluster,
):
    cluster = postgres_cluster
    cluster.load_schema()

    _assert_sql_succeeds(
        cluster,
        """
        CREATE ROLE memory_app;
        GRANT USAGE ON SCHEMA public TO memory_app;
        GRANT SELECT, INSERT, UPDATE ON traces, failure_cases, lessons, project_policies TO memory_app;
        SET ROLE memory_app;
        INSERT INTO traces(trace_id, run_id, commit_sha, repo)
        VALUES ('trace_app', 'run_app', 'commit_app', 'repo');
        INSERT INTO failure_cases(
          case_id, source_trace_id, commit_sha, failure_type, symptom
        ) VALUES ('case_app', 'trace_app', 'commit_app', 'tool_error', 'symptom');
        INSERT INTO lessons(
          lesson_id, source_case_id, lesson_text, memory_type, scope_json, status
        ) VALUES (
          'lesson_app', 'case_app', 'rule', 'procedural',
          '{"repo":"repo"}', 'obsolete'
        );
        INSERT INTO project_policies(policy_id, policy_text, scope_json)
        VALUES ('policy_app', 'policy', '{"repo":"repo"}');
        RESET ROLE;
        """,
    )
    assert _assert_sql_succeeds(
        cluster,
        """
        SELECT string_agg(memory_id || ':' || memory_kind, ',' ORDER BY memory_id)
        FROM memory_ids
        WHERE memory_id IN ('case_app', 'lesson_app', 'policy_app')
        """,
    ) == "case_app:failure_case,lesson_app:lesson,policy_app:project_policy"

    _assert_sql_succeeds(
        cluster,
        """
        INSERT INTO traces(trace_id, run_id, commit_sha, repo)
        VALUES ('trace_restore', 'run_restore', 'commit_restore', 'repo');
        INSERT INTO failure_cases(
          case_id, source_trace_id, commit_sha, failure_type, symptom, status
        ) VALUES (
          'case_restore_obsolete', 'trace_restore', 'commit_restore',
          'tool_error', 'symptom', 'obsolete'
        );
        INSERT INTO lessons(
          lesson_id, source_case_id, lesson_text, memory_type, scope_json, status
        ) VALUES (
          'lesson_restore_obsolete', 'case_restore_obsolete', 'rule',
          'procedural', '{"repo":"repo"}', 'obsolete'
        );
        INSERT INTO project_policies(
          policy_id, policy_text, scope_json, status
        ) VALUES (
          'policy_restore_obsolete', 'policy', '{"repo":"repo"}', 'obsolete'
        );
        """,
    )
    assert _assert_sql_succeeds(
        cluster,
        """
        SELECT count(*) FROM memory_ids
        WHERE memory_id IN (
          'case_restore_obsolete', 'lesson_restore_obsolete',
          'policy_restore_obsolete'
        )
        """,
    ) == "3"

    _assert_sql_fails(
        cluster,
        "INSERT INTO memory_ids(memory_id, memory_kind) VALUES ('forged', 'lesson')",
        "memory_ids registry does not allow direct INSERT",
    )
    _assert_sql_fails(
        cluster,
        "UPDATE memory_ids SET memory_kind = 'lesson' WHERE memory_id = 'case_app'",
        "memory_ids registry does not allow direct UPDATE",
    )
    _assert_sql_fails(
        cluster,
        "DELETE FROM memory_ids WHERE memory_id = 'case_app'",
        "memory_ids registry does not allow direct DELETE",
    )
    _assert_sql_fails(
        cluster,
        """
        INSERT INTO project_policies(policy_id, policy_text, scope_json)
        VALUES ('case_app', 'policy', '{"repo":"repo"}')
        """,
        "duplicate runtime memory_id: case_app",
    )

    _assert_sql_succeeds(
        cluster,
        """
        SET session_replication_role = replica;
        INSERT INTO memory_ids(memory_id, memory_kind) VALUES ('ghost_memory', 'lesson');
        SET session_replication_role = origin;
        """,
    )
    _assert_sql_fails(
        cluster,
        """
        INSERT INTO memory_usage_decisions(
          decision_id, run_id, trace_id, mode, candidate_memory_ids,
          used_memory_ids, blocked_memory_ids, risk, reason,
          recommended_injection, context, candidate_memory_statuses,
          system_blocked_reasons
        ) VALUES (
          'decision_ghost', 'run_app', 'trace_app', 'repair', '["ghost_memory"]',
          '[]', '["ghost_memory"]', 'none', 'ghost must fail', 'none',
          '{"mode":"repair","repo":"repo","commit_sha":"commit_app"}',
          '{"ghost_memory":"active"}', '{"ghost_memory":"not concrete"}'
        )
        """,
        "usage log references unknown memory IDs: ghost_memory",
    )
    _assert_sql_fails(
        cluster,
        """
        CREATE TEMP TABLE memory_ids(memory_id text, memory_kind text);
        CREATE TEMP TABLE lessons(lesson_id text);
        INSERT INTO memory_ids VALUES ('ghost_shadow', 'lesson');
        INSERT INTO lessons VALUES ('ghost_shadow');
        INSERT INTO public.memory_usage_decisions(
          decision_id, run_id, trace_id, mode, candidate_memory_ids,
          used_memory_ids, blocked_memory_ids, risk, reason,
          recommended_injection, context, candidate_memory_statuses,
          system_blocked_reasons
        ) VALUES (
          'decision_shadow', 'run_app', 'trace_app', 'repair', '["ghost_shadow"]',
          '[]', '["ghost_shadow"]', 'none', 'shadow ghost must fail', 'none',
          '{"mode":"repair","repo":"repo","commit_sha":"commit_app"}',
          '{"ghost_shadow":"active"}', '{"ghost_shadow":"not concrete"}'
        )
        """,
        "usage log references unknown memory IDs: ghost_shadow",
    )
    _assert_sql_fails(
        cluster,
        """
        CREATE TEMP TABLE failure_cases(
          case_id text, status text, regression_passed boolean
        );
        INSERT INTO failure_cases VALUES ('case_app', 'verified', true);
        INSERT INTO public.lessons(
          lesson_id, source_case_id, lesson_text, memory_type, scope_json
        ) VALUES (
          'lesson_shadow_parent', 'case_app', 'rule', 'procedural',
          '{"repo":"repo"}'
        )
        """,
        "lesson source_case_id must reference a verified regression-backed failure case: case_app",
    )

    _seed_verified_case(cluster, "forward")
    _assert_sql_fails(
        cluster,
        "UPDATE failure_cases SET status = 'draft' WHERE case_id = 'case_forward'",
        "failure case status transition is not allowed: verified -> draft",
    )
    _assert_sql_succeeds(
        cluster,
        "UPDATE failure_cases SET status = 'obsolete' WHERE case_id = 'case_forward'",
    )
    _assert_sql_fails(
        cluster,
        "UPDATE failure_cases SET status = 'verified' WHERE case_id = 'case_forward'",
        "failure case status transition is not allowed: obsolete -> verified",
    )

    _seed_verified_case(cluster, "child_forward")
    _assert_sql_succeeds(
        cluster,
        """
        INSERT INTO lessons(lesson_id, source_case_id, lesson_text, memory_type, scope_json)
        VALUES ('lesson_forward', 'case_child_forward', 'rule', 'procedural', '{"repo":"repo"}');
        UPDATE lessons SET status = 'obsolete' WHERE lesson_id = 'lesson_forward';
        INSERT INTO project_policies(policy_id, policy_text, scope_json)
        VALUES ('policy_forward', 'policy', '{"repo":"repo"}');
        UPDATE project_policies SET status = 'obsolete' WHERE policy_id = 'policy_forward';
        """,
    )
    _assert_sql_fails(
        cluster,
        "UPDATE lessons SET status = 'active' WHERE lesson_id = 'lesson_forward'",
        "runtime memory status transition is not allowed: obsolete -> active",
    )
    _assert_sql_fails(
        cluster,
        "UPDATE project_policies SET status = 'active' WHERE policy_id = 'policy_forward'",
        "runtime memory status transition is not allowed: obsolete -> active",
    )

    _seed_verified_case(cluster, "insert_first")
    holder, holder_out, holder_err = cluster.spawn(
        "lesson_insert_holder",
        """
        BEGIN;
        INSERT INTO lessons(lesson_id, source_case_id, lesson_text, memory_type, scope_json)
        VALUES ('lesson_insert_first', 'case_insert_first', 'rule', 'procedural', '{"repo":"repo"}');
        SELECT pg_sleep(2);
        COMMIT;
        """,
    )
    _wait_for_activity(cluster, "lesson_insert_holder", "Timeout", holder)
    obsoleter, obsoleter_out, obsoleter_err = cluster.spawn(
        "parent_obsoleter",
        """
        SET lock_timeout = '5s';
        UPDATE failure_cases SET status = 'obsolete' WHERE case_id = 'case_insert_first';
        """,
    )
    _wait_for_activity(cluster, "parent_obsoleter", "Lock", obsoleter)
    _wait_process(holder, holder_out, holder_err, expected_returncode=0)
    _wait_process(obsoleter, obsoleter_out, obsoleter_err, expected_returncode=0)
    assert _assert_sql_succeeds(
        cluster,
        "SELECT status FROM lessons WHERE lesson_id = 'lesson_insert_first'",
    ) == "obsolete"

    _seed_verified_case(cluster, "parent_first")
    parent, parent_out, parent_err = cluster.spawn(
        "parent_update_holder",
        """
        BEGIN;
        UPDATE failure_cases SET status = 'obsolete' WHERE case_id = 'case_parent_first';
        SELECT pg_sleep(2);
        COMMIT;
        """,
    )
    _wait_for_activity(cluster, "parent_update_holder", "Timeout", parent)
    lesson, lesson_out, lesson_err = cluster.spawn(
        "lesson_insert_waiter",
        """
        SET lock_timeout = '5s';
        INSERT INTO lessons(lesson_id, source_case_id, lesson_text, memory_type, scope_json)
        VALUES ('lesson_parent_first', 'case_parent_first', 'rule', 'procedural', '{"repo":"repo"}');
        """,
    )
    _wait_for_activity(cluster, "lesson_insert_waiter", "Lock", lesson)
    _wait_process(parent, parent_out, parent_err, expected_returncode=0)
    lesson_stderr = _wait_process(
        lesson, lesson_out, lesson_err, expected_returncode=1
    )
    assert "verified regression-backed failure case" in lesson_stderr
    assert _assert_sql_succeeds(
        cluster,
        "SELECT count(*) FROM lessons WHERE lesson_id = 'lesson_parent_first' AND status = 'active'",
    ) == "0"
