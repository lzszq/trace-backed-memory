from __future__ import annotations

import os
import shutil
import socket
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path

import pytest


ROOT = Path(__file__).resolve().parents[1]


@dataclass(frozen=True)
class AdvisoryLatch:
    key: int
    process: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_path: Path


@dataclass
class PostgresCluster:
    psql: str
    env: dict[str, str]
    root: Path
    clients: list[subprocess.Popen[bytes]] = field(default_factory=list, repr=False)

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

    def run_script(
        self,
        path: Path,
        *,
        caller_search_path: str | None = None,
        timeout: float = 20.0,
    ) -> subprocess.CompletedProcess[str]:
        child_env = dict(self.env)
        if caller_search_path is not None:
            child_env["PGOPTIONS"] = (
                f"{child_env.get('PGOPTIONS', '')} "
                f"-c search_path={caller_search_path}"
            ).strip()
        return subprocess.run(
            [
                self.psql,
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                str(path),
            ],
            env=child_env,
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def load_schema(self, *, caller_search_path: str | None = None) -> None:
        result = self.run_script(
            ROOT / "schemas" / "postgres.sql",
            caller_search_path=caller_search_path,
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
        self.clients.append(process)
        return process, stdout_path, stderr_path

    def acquire_latch(
        self,
        name: str,
        key: int,
        *,
        timeout: float = 5.0,
    ) -> AdvisoryLatch:
        stdout_path = self.root / f"{name}.stdout"
        stderr_path = self.root / f"{name}.stderr"
        child_env = {**self.env, "PGAPPNAME": name}
        with stdout_path.open("wb") as stdout_file, stderr_path.open("wb") as stderr_file:
            process = subprocess.Popen(
                [self.psql, "-X", "-v", "ON_ERROR_STOP=1", "-qAt"],
                env=child_env,
                stdin=subprocess.PIPE,
                stdout=stdout_file,
                stderr=stderr_file,
                close_fds=True,
            )
        self.clients.append(process)
        assert process.stdin is not None
        process.stdin.write(f"SELECT pg_advisory_lock({key});\n".encode("ascii"))
        process.stdin.flush()

        deadline = time.monotonic() + timeout
        escaped_name = name.replace("'", "''")
        while time.monotonic() < deadline:
            acquired = self.run(
                "SELECT count(*) FROM pg_locks AS locks "
                "JOIN pg_stat_activity AS activity USING (pid) "
                "WHERE locks.locktype = 'advisory' AND locks.granted "
                f"AND activity.application_name = '{escaped_name}'"
            )
            assert acquired.returncode == 0, acquired.stderr
            if acquired.stdout.strip() == "1":
                return AdvisoryLatch(key, process, stdout_path, stderr_path)
            if process.poll() is not None:
                break
            time.sleep(0.05)

        self.terminate_clients()
        pytest.fail(f"database latch {name!r} was not acquired")

    def release_latch(self, latch: AdvisoryLatch, *, timeout: float = 5.0) -> None:
        if latch.process.poll() is not None or latch.process.stdin is None:
            pytest.fail("database latch process exited before release")
        latch.process.stdin.write(
            (
                f"SELECT pg_advisory_unlock({latch.key});\n"
                "\\q\n"
            ).encode("ascii")
        )
        latch.process.stdin.flush()
        latch.process.stdin.close()
        returncode = latch.process.wait(timeout=timeout)
        stdout = latch.stdout_path.read_text(encoding="utf-8", errors="replace")
        stderr = latch.stderr_path.read_text(encoding="utf-8", errors="replace")
        assert returncode == 0, f"stdout:\n{stdout}\nstderr:\n{stderr}"

    def terminate_clients(self) -> None:
        cleanup_errors: list[Exception] = []

        def kill_and_wait(process: subprocess.Popen[bytes]) -> None:
            try:
                process.kill()
            except ProcessLookupError:
                pass
            except Exception as exc:
                cleanup_errors.append(exc)
            try:
                process.wait(timeout=3)
            except (ChildProcessError, ProcessLookupError):
                pass
            except Exception as exc:
                cleanup_errors.append(exc)

        for process in self.clients:
            if process.poll() is None and process.stdin is not None:
                try:
                    process.stdin.close()
                except (OSError, ValueError):
                    pass
            if process.poll() is None:
                try:
                    process.terminate()
                except ProcessLookupError:
                    pass
                except Exception as exc:
                    cleanup_errors.append(exc)

        for process in self.clients:
            try:
                process.wait(timeout=3)
            except subprocess.TimeoutExpired:
                kill_and_wait(process)
            except (ChildProcessError, ProcessLookupError):
                pass
            except Exception as exc:
                cleanup_errors.append(exc)
                if process.poll() is None:
                    kill_and_wait(process)

        if cleanup_errors:
            raise ExceptionGroup("PostgreSQL client cleanup failed", cleanup_errors)

    def unfinished_clients(self) -> list[subprocess.Popen[bytes]]:
        return [process for process in self.clients if process.poll() is None]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _cleanup_postgres_resources(
    *,
    cluster: PostgresCluster | None,
    data: Path,
    root: Path,
    tmp_path: Path,
    pg_ctl: str,
    env: dict[str, str],
) -> list[Exception]:
    cleanup_errors: list[Exception] = []

    try:
        if cluster is not None:
            cluster.terminate_clients()
    except Exception as exc:
        exc.add_note("PostgreSQL client cleanup stage")
        cleanup_errors.append(exc)

    try:
        if data.exists():
            stop = subprocess.run(
                [
                    pg_ctl,
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
                capture_output=True,
                text=True,
                encoding="utf-8",
                errors="replace",
                timeout=30,
                check=False,
            )
            if stop.returncode != 0 and (data / "postmaster.pid").exists():
                raise RuntimeError(
                    "pg_ctl stop failed:\n" + stop.stdout + "\n" + stop.stderr
                )
    except Exception as exc:
        exc.add_note("PostgreSQL server cleanup stage")
        cleanup_errors.append(exc)

    try:
        resolved_root = root.resolve()
        if not resolved_root.is_relative_to(tmp_path.resolve()):
            raise AssertionError("PostgreSQL test root escaped pytest tmp_path")
        if root.exists():
            shutil.rmtree(resolved_root, ignore_errors=False)
    except Exception as exc:
        exc.add_note("PostgreSQL directory cleanup stage")
        cleanup_errors.append(exc)

    return cleanup_errors


def _report_cleanup_errors(
    cleanup_errors: list[Exception], original_error: BaseException | None
) -> None:
    if not cleanup_errors:
        return
    if original_error is not None:
        summary = "; ".join(
            f"{type(error).__name__}: {error}" for error in cleanup_errors
        )
        original_error.add_note("PostgreSQL cleanup also failed: " + summary)
        return
    raise ExceptionGroup("PostgreSQL cleanup failed", cleanup_errors)


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
    cluster: PostgresCluster | None = None
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
        cluster = PostgresCluster(executables["psql"], env, root)
        yield cluster
    finally:
        original_error = sys.exc_info()[1]
        cleanup_errors = _cleanup_postgres_resources(
            cluster=cluster,
            data=data,
            root=root,
            tmp_path=tmp_path,
            pg_ctl=executables["pg_ctl"],
            env=env,
        )
        _report_cleanup_errors(cleanup_errors, original_error)


def _assert_sql_succeeds(cluster: PostgresCluster, sql: str) -> str:
    result = cluster.run(sql)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def _assert_sql_fails(cluster: PostgresCluster, sql: str, message: str) -> None:
    result = cluster.run(sql)
    assert result.returncode != 0, "SQL unexpectedly succeeded"
    assert message in result.stderr, result.stderr


def _assert_registry_parity(cluster: PostgresCluster) -> None:
    assert _assert_sql_succeeds(
        cluster,
        """
        WITH source_ids(memory_id, memory_kind) AS (
          SELECT case_id, 'failure_case' FROM failure_cases
          UNION ALL
          SELECT lesson_id, 'lesson' FROM lessons
          UNION ALL
          SELECT policy_id, 'project_policy' FROM project_policies
        ), differences AS (
          (SELECT memory_id, memory_kind FROM memory_ids
           EXCEPT
           SELECT memory_id, memory_kind FROM source_ids)
          UNION ALL
          (SELECT memory_id, memory_kind FROM source_ids
           EXCEPT
           SELECT memory_id, memory_kind FROM memory_ids)
        )
        SELECT count(*) FROM differences
        """,
    ) == "0"


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


def test_postgres_schema_install_is_atomic_and_public(
    postgres_cluster: PostgresCluster,
):
    cluster = postgres_cluster
    schema = (ROOT / "schemas" / "postgres.sql").read_text(encoding="utf-8")
    broken_schema = schema.replace(
        "CREATE TABLE lessons (",
        "SELECT release_closure_missing_function();\n\nCREATE TABLE lessons (",
        1,
    )
    broken_path = cluster.root / "broken-postgres.sql"
    broken_path.write_text(broken_schema, encoding="utf-8")

    failed_install = cluster.run_script(broken_path)
    assert failed_install.returncode != 0
    assert "release_closure_missing_function" in failed_install.stderr
    assert _assert_sql_succeeds(
        cluster,
        """
        SELECT
          (SELECT count(*) FROM pg_class
           WHERE relnamespace = 'public'::regnamespace
             AND relname IN (
               'traces', 'memory_ids', 'failure_cases', 'lessons',
               'project_policies', 'memory_usage_decisions'
             ))
          +
          (SELECT count(*) FROM pg_proc
           WHERE pronamespace = 'public'::regnamespace
             AND proname IN (
               'protect_memory_id_registry', 'valid_memory_scope_json',
               'register_runtime_memory_id'
             ))
        """,
    ) == "0"

    cluster.load_schema(caller_search_path="pg_catalog")
    assert _assert_sql_succeeds(
        cluster,
        """
        SELECT count(*) FROM pg_class
        WHERE relnamespace = 'public'::regnamespace
          AND relname IN (
            'traces', 'memory_ids', 'failure_cases', 'lessons',
            'project_policies', 'memory_usage_decisions'
          )
        """,
    ) == "6"
    assert _assert_sql_succeeds(
        cluster,
        "SELECT to_regclass('pg_catalog.traces') IS NULL",
    ) == "t"


def test_postgres_cluster_cleanup_terminates_unfinished_clients(
    postgres_cluster: PostgresCluster,
):
    cluster = postgres_cluster
    latch = cluster.acquire_latch("abandoned_release_latch", 92_001)

    cluster.terminate_clients()

    assert latch.process.poll() is not None
    assert cluster.unfinished_clients() == []
    assert _assert_sql_succeeds(cluster, "SELECT 1") == "1"


def test_client_termination_tolerates_process_exit_race(tmp_path: Path):
    class FakeInput:
        def __init__(self) -> None:
            self.closed = False

        def close(self) -> None:
            self.closed = True

    class RaceProcess:
        def __init__(self) -> None:
            self.stdin = FakeInput()
            self.waited = False
            self.kill_called = False

        def poll(self):
            return 0 if self.waited else None

        def terminate(self) -> None:
            raise ProcessLookupError("process exited before terminate")

        def wait(self, timeout: float):
            self.waited = True
            return 0

        def kill(self) -> None:
            self.kill_called = True

    process = RaceProcess()
    cluster = PostgresCluster("psql", {}, tmp_path, clients=[process])  # type: ignore[list-item]

    cluster.terminate_clients()

    assert process.stdin.closed is True
    assert process.waited is True
    assert process.kill_called is False


def test_client_cleanup_collects_errors_and_waits_every_tracked_process(
    tmp_path: Path,
):
    class TrackedProcess:
        stdin = None

        def __init__(self, terminate_error: Exception | None = None) -> None:
            self.terminate_error = terminate_error
            self.waited = False

        def poll(self):
            return 0 if self.waited else None

        def terminate(self) -> None:
            if self.terminate_error is not None:
                raise self.terminate_error

        def wait(self, timeout: float):
            self.waited = True
            return 0

        def kill(self) -> None:
            self.waited = True

    first = TrackedProcess(RuntimeError("terminate implementation failed"))
    second = TrackedProcess()
    cluster = PostgresCluster(
        "psql",
        {},
        tmp_path,
        clients=[first, second],  # type: ignore[list-item]
    )

    with pytest.raises(ExceptionGroup, match="PostgreSQL client cleanup failed"):
        cluster.terminate_clients()

    assert first.waited is True
    assert second.waited is True


def test_cleanup_stages_are_independent_and_preserve_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "postgres-cluster"
    data = root / "data"
    data.mkdir(parents=True)
    (data / "postmaster.pid").write_text("123", encoding="ascii")
    events: list[str] = []

    class FailingCluster:
        def terminate_clients(self) -> None:
            events.append("clients")
            raise RuntimeError("client cleanup failed")

    def fail_server_stop(*_args, **_kwargs):
        events.append("server")
        raise subprocess.TimeoutExpired("pg_ctl", 1)

    real_rmtree = shutil.rmtree

    def remove_directory(path: Path, *, ignore_errors: bool) -> None:
        events.append("directory")
        real_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(subprocess, "run", fail_server_stop)
    monkeypatch.setattr(shutil, "rmtree", remove_directory)

    cleanup_errors = _cleanup_postgres_resources(
        cluster=FailingCluster(),  # type: ignore[arg-type]
        data=data,
        root=root,
        tmp_path=tmp_path,
        pg_ctl="pg_ctl",
        env={},
    )

    assert events == ["clients", "server", "directory"]
    assert len(cleanup_errors) == 2
    assert root.exists() is False
    original_error = AssertionError("original test failure")
    _report_cleanup_errors(cleanup_errors, original_error)
    assert any("PostgreSQL cleanup also failed" in note for note in original_error.__notes__)

    with pytest.raises(ExceptionGroup, match="PostgreSQL cleanup failed"):
        _report_cleanup_errors(cleanup_errors, None)


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
        GRANT SELECT ON memory_ids TO memory_app;
        GRANT INSERT ON memory_usage_decisions TO memory_app;
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

    _assert_sql_fails(
        cluster,
        """
        CREATE SCHEMA memory_app_shadow AUTHORIZATION memory_app;
        SET ROLE memory_app;
        CREATE FUNCTION memory_app_shadow.jsonb_array_elements_text(jsonb)
        RETURNS SETOF text LANGUAGE SQL IMMUTABLE
        AS $shadow$ SELECT NULL::text WHERE false $shadow$;
        CREATE FUNCTION memory_app_shadow.jsonb_object_keys(jsonb)
        RETURNS SETOF text LANGUAGE SQL IMMUTABLE
        AS $shadow$ SELECT NULL::text WHERE false $shadow$;
        SET search_path = memory_app_shadow, public, pg_catalog;
        INSERT INTO memory_usage_decisions(
          decision_id, run_id, trace_id, mode, candidate_memory_ids,
          used_memory_ids, blocked_memory_ids, risk, reason,
          recommended_injection, context, candidate_memory_statuses,
          system_blocked_reasons
        ) VALUES (
          'decision_helper_shadow', 'run_app', 'trace_app', 'repair',
          '["ghost_helper_shadow"]', '[]', '["ghost_helper_shadow"]',
          'none', 'shadowed helpers must not bypass invariants', 'none',
          '{"mode":"repair","repo":"repo","commit_sha":"commit_app"}',
          '{"ghost_helper_shadow":"active"}',
          '{"ghost_helper_shadow":"not a concrete runtime memory"}'
        );
        RESET ROLE;
        """,
        "usage log references unknown memory IDs: ghost_helper_shadow",
    )

    for table_name in [
        "memory_ids",
        "failure_cases",
        "lessons",
        "project_policies",
    ]:
        truncate_sql = f"TRUNCATE TABLE {table_name}"
        if table_name == "failure_cases":
            truncate_sql += " CASCADE"
        _assert_sql_fails(
            cluster,
            truncate_sql,
            f"runtime memory table does not allow TRUNCATE: {table_name}",
        )
        _assert_registry_parity(cluster)
        _assert_sql_fails(
            cluster,
            """
            INSERT INTO project_policies(policy_id, policy_text, scope_json)
            VALUES ('case_app', 'policy', '{"repo":"repo"}')
            """,
            "duplicate runtime memory_id: case_app",
        )

    assert _assert_sql_succeeds(
        cluster,
        """
        SELECT string_agg(
          table_name || ':' || has_table_privilege(
            'memory_app', 'public.' || table_name, 'TRUNCATE'
          )::text,
          ',' ORDER BY table_name
        )
        FROM unnest(ARRAY[
          'memory_ids', 'failure_cases', 'lessons', 'project_policies'
        ]) AS tables(table_name)
        """,
    ) == (
        "failure_cases:false,lessons:false,memory_ids:false,"
        "project_policies:false"
    )

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
        INSERT INTO memory_usage_decisions(
          decision_id, run_id, trace_id, mode, candidate_memory_ids,
          used_memory_ids, blocked_memory_ids, risk, reason,
          recommended_injection, context, candidate_memory_statuses,
          system_blocked_reasons
        ) VALUES (
          'decision_valid', 'run_app', 'trace_app', 'repair', '["policy_app"]',
          '["policy_app"]', '[]', 'low', 'verified policy applies',
          'short_summary',
          '{"mode":"repair","repo":"repo","commit_sha":"commit_app"}',
          '{"policy_app":"active"}', '{}'
        )
        """,
    )
    assert _assert_sql_succeeds(
        cluster,
        "SELECT count(*) FROM memory_usage_decisions WHERE decision_id = 'decision_valid'",
    ) == "1"

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
    insert_latch = cluster.acquire_latch("lesson_insert_latch", 91_001)
    holder, holder_out, holder_err = cluster.spawn(
        "lesson_insert_holder",
        """
        BEGIN;
        INSERT INTO lessons(lesson_id, source_case_id, lesson_text, memory_type, scope_json)
        VALUES ('lesson_insert_first', 'case_insert_first', 'rule', 'procedural', '{"repo":"repo"}');
        SELECT pg_advisory_lock(91001);
        SELECT pg_advisory_unlock(91001);
        COMMIT;
        """,
    )
    _wait_for_activity(cluster, "lesson_insert_holder", "Lock", holder)
    obsoleter, obsoleter_out, obsoleter_err = cluster.spawn(
        "parent_obsoleter",
        """
        SET lock_timeout = '5s';
        UPDATE failure_cases SET status = 'obsolete' WHERE case_id = 'case_insert_first';
        """,
    )
    _wait_for_activity(cluster, "parent_obsoleter", "Lock", obsoleter)
    cluster.release_latch(insert_latch)
    _wait_process(holder, holder_out, holder_err, expected_returncode=0)
    _wait_process(obsoleter, obsoleter_out, obsoleter_err, expected_returncode=0)
    assert _assert_sql_succeeds(
        cluster,
        "SELECT status FROM lessons WHERE lesson_id = 'lesson_insert_first'",
    ) == "obsolete"

    _seed_verified_case(cluster, "parent_first")
    parent_latch = cluster.acquire_latch("parent_update_latch", 91_002)
    parent, parent_out, parent_err = cluster.spawn(
        "parent_update_holder",
        """
        BEGIN;
        UPDATE failure_cases SET status = 'obsolete' WHERE case_id = 'case_parent_first';
        SELECT pg_advisory_lock(91002);
        SELECT pg_advisory_unlock(91002);
        COMMIT;
        """,
    )
    _wait_for_activity(cluster, "parent_update_holder", "Lock", parent)
    lesson, lesson_out, lesson_err = cluster.spawn(
        "lesson_insert_waiter",
        """
        SET lock_timeout = '5s';
        INSERT INTO lessons(lesson_id, source_case_id, lesson_text, memory_type, scope_json)
        VALUES ('lesson_parent_first', 'case_parent_first', 'rule', 'procedural', '{"repo":"repo"}');
        """,
    )
    _wait_for_activity(cluster, "lesson_insert_waiter", "Lock", lesson)
    cluster.release_latch(parent_latch)
    _wait_process(parent, parent_out, parent_err, expected_returncode=0)
    lesson_stderr = _wait_process(
        lesson, lesson_out, lesson_err, expected_returncode=1
    )
    assert "verified regression-backed failure case" in lesson_stderr
    assert _assert_sql_succeeds(
        cluster,
        "SELECT count(*) FROM lessons WHERE lesson_id = 'lesson_parent_first' AND status = 'active'",
    ) == "0"
