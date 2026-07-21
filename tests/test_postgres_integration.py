from __future__ import annotations

import re
import shutil
import subprocess
import time
from pathlib import Path

import pytest

from tests import postgres_support
from tests.postgres_support import (
    PostgresCluster,
    PostgresServer,
    TrackedClient,
    _cleanup_postgres_database_resources,
    _cleanup_postgres_server_resources,
    _new_test_database_name,
    _quote_identifier,
    _read_role_names,
    _report_cleanup_errors,
    assert_sql_fails,
    assert_sql_succeeds,
)


ROOT = Path(__file__).resolve().parents[1]


@pytest.mark.parametrize("value", [None, "", "0", "true"])
def test_unavailable_postgres_runtime_skips_unless_explicitly_required(
    monkeypatch: pytest.MonkeyPatch,
    value: str | None,
):
    if value is None:
        monkeypatch.delenv("TBM_REQUIRE_POSTGRES", raising=False)
    else:
        monkeypatch.setenv("TBM_REQUIRE_POSTGRES", value)

    with pytest.raises(
        pytest.skip.Exception,
        match="PostgreSQL executables unavailable: initdb",
    ):
        postgres_support._unavailable_postgres_runtime(
            "PostgreSQL executables unavailable: initdb"
        )


def test_unavailable_postgres_runtime_fails_in_required_mode(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("TBM_REQUIRE_POSTGRES", "1")

    with pytest.raises(
        pytest.fail.Exception,
        match="initdb cannot legally run as the current user",
    ):
        postgres_support._unavailable_postgres_runtime(
            "initdb cannot legally run as the current user"
        )


def test_postgres_server_missing_executables_obeys_required_mode(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("TBM_REQUIRE_POSTGRES", "1")
    monkeypatch.setattr(postgres_support.shutil, "which", lambda _name: None)
    fixture = postgres_support._postgres_server.__wrapped__(None)

    with pytest.raises(
        pytest.fail.Exception,
        match="PostgreSQL executables unavailable: initdb, pg_ctl, psql",
    ):
        next(fixture)


def test_postgres_server_illegal_user_obeys_required_mode_and_cleans_root(
    monkeypatch: pytest.MonkeyPatch,
    tmp_path: Path,
):
    root = tmp_path / "postgres-server"

    class TmpPathFactory:
        def mktemp(self, _name: str) -> Path:
            root.mkdir()
            return root

        def getbasetemp(self) -> Path:
            return tmp_path

    monkeypatch.setenv("TBM_REQUIRE_POSTGRES", "1")
    monkeypatch.setattr(postgres_support.shutil, "which", lambda name: name)
    monkeypatch.setattr(
        postgres_support.subprocess,
        "run",
        lambda args, **_kwargs: subprocess.CompletedProcess(
            args,
            1,
            "",
            "initdb cannot be run as root",
        ),
    )
    fixture = postgres_support._postgres_server.__wrapped__(TmpPathFactory())

    with pytest.raises(
        pytest.fail.Exception,
        match="initdb cannot legally run as the current user",
    ):
        next(fixture)

    assert root.exists() is False


def test_test_database_names_are_unique_safe_identifiers():
    names = {_new_test_database_name() for _ in range(100)}
    assert len(names) == 100
    assert all(re.fullmatch(r"tbm_test_[0-9a-f]{32}", name) for name in names)


def test_postgres_cluster_targets_an_isolated_test_database(
    postgres_cluster: PostgresCluster,
):
    database_name = assert_sql_succeeds(postgres_cluster, "SELECT current_database()")
    assert database_name == postgres_cluster.env["PGDATABASE"]
    assert re.fullmatch(r"tbm_test_[0-9a-f]{32}", database_name)


def test_postgres_server_sequential_databases_are_isolated(
    _postgres_server: postgres_support.PostgresServer,
    tmp_path: Path,
):
    server = _postgres_server
    first_database = _new_test_database_name()
    postgres_support._create_test_database(server, first_database)
    try:
        first_cluster = PostgresCluster(
            server.psql,
            {**server.env, "PGDATABASE": first_database},
            tmp_path / "first-database",
        )
        assert_sql_succeeds(
            first_cluster,
            f"""
            CREATE TABLE first_database_only (value text);
            ALTER DATABASE {_quote_identifier(first_database)}
            SET lock_timeout TO '1234ms';
            """,
        )
    finally:
        postgres_support._terminate_database_sessions(server, first_database)
        postgres_support._drop_test_database(server, first_database)

    second_database = _new_test_database_name()
    postgres_support._create_test_database(server, second_database)
    try:
        second_cluster = PostgresCluster(
            server.psql,
            {**server.env, "PGDATABASE": second_database},
            tmp_path / "second-database",
        )
        assert (
            assert_sql_succeeds(
                second_cluster,
                "SELECT to_regclass('public.first_database_only') IS NULL",
            )
            == "t"
        )
        assert assert_sql_succeeds(second_cluster, "SHOW lock_timeout") == "0"
    finally:
        postgres_support._terminate_database_sessions(server, second_database)
        postgres_support._drop_test_database(server, second_database)


def test_postgres_identifier_quoting_escapes_embedded_quotes():
    assert _quote_identifier('role "owner"') == '"role ""owner"""'


def test_read_role_names_decodes_structured_output(monkeypatch):
    result = subprocess.CompletedProcess(
        ["psql"], 0, '["postgres", "role with newline\\ninside"]\n', ""
    )
    monkeypatch.setattr(
        postgres_support,
        "_run_psql",
        lambda *_args, **_kwargs: result,
    )

    assert _read_role_names("psql", {}) == frozenset(
        {"postgres", "role with newline\ninside"}
    )


@pytest.mark.parametrize("stdout", ["not-json", "{}", '["postgres", 1]'])
def test_read_role_names_rejects_invalid_json_shapes(
    monkeypatch: pytest.MonkeyPatch, stdout: str
):
    result = subprocess.CompletedProcess(["psql"], 0, stdout, "")
    monkeypatch.setattr(
        postgres_support,
        "_run_psql",
        lambda *_args, **_kwargs: result,
    )

    with pytest.raises(
        RuntimeError, match="^PostgreSQL role discovery returned invalid JSON$"
    ):
        _read_role_names("psql", {})


def _assert_registry_parity(cluster: PostgresCluster) -> None:
    assert assert_sql_succeeds(
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
    client: TrackedClient,
    *,
    expected_returncode: int,
    timeout: float = 10.0,
) -> str:
    process = client.process
    try:
        returncode = process.wait(timeout=timeout)
    except subprocess.TimeoutExpired:
        process.kill()
        process.wait(timeout=5)
        pytest.fail("PostgreSQL test session exceeded its bounded timeout")
    stdout = client.stdout_path.read_text(encoding="utf-8", errors="replace")
    stderr = client.stderr_path.read_text(encoding="utf-8", errors="replace")
    assert returncode == expected_returncode, f"stdout:\n{stdout}\nstderr:\n{stderr}"
    return stderr


def _seed_verified_case(cluster: PostgresCluster, suffix: str) -> None:
    assert_sql_succeeds(
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


def test_postgres_cluster_meets_documented_version_floor(
    postgres_cluster: PostgresCluster,
):
    readme = (ROOT / "README.md").read_text(encoding="utf-8")
    documented_floor = re.search(r"PostgreSQL (\d+)\+", readme)

    assert documented_floor is not None
    assert int(documented_floor.group(1)) == 12
    server_version = int(
        assert_sql_succeeds(postgres_cluster, "SHOW server_version_num")
    )
    assert server_version >= 120000


def test_postgres_trace_latency_accepts_null_and_zero_but_rejects_negative(
    postgres_cluster: PostgresCluster,
):
    cluster = postgres_cluster
    cluster.load_schema()

    assert_sql_succeeds(
        cluster,
        """
        INSERT INTO traces(trace_id, run_id, commit_sha, latency_ms)
        VALUES
          ('trace_latency_null', 'run_latency_null', 'commit_latency', NULL),
          ('trace_latency_zero', 'run_latency_zero', 'commit_latency', 0);
        """,
    )
    assert_sql_fails(
        cluster,
        """
        INSERT INTO traces(trace_id, run_id, commit_sha, latency_ms)
        VALUES ('trace_latency_negative', 'run_latency_negative', 'commit_latency', -1)
        """,
        "traces_latency_ms_non_negative",
    )
    assert assert_sql_succeeds(
        cluster,
        "SELECT count(*) FROM traces WHERE latency_ms IS NULL OR latency_ms = 0",
    ) == "2"


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
    assert assert_sql_succeeds(
        cluster,
        """
        SELECT
          (SELECT count(*) FROM pg_class
           WHERE relnamespace = 'public'::regnamespace
             AND relname IN (
               'trace_backed_memory_schema', 'traces', 'memory_ids', 'failure_cases', 'lessons',
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

    cluster.load_schema(
        env={"PGOPTIONS": f"{cluster.env['PGOPTIONS']} -c search_path=pg_catalog"}
    )
    assert assert_sql_succeeds(
        cluster,
        """
        SELECT count(*) FROM pg_class
        WHERE relnamespace = 'public'::regnamespace
          AND relname IN (
            'trace_backed_memory_schema', 'traces', 'memory_ids', 'failure_cases', 'lessons',
            'project_policies', 'memory_usage_decisions'
          )
        """,
    ) == "7"
    assert assert_sql_succeeds(
        cluster,
        "SELECT count(*) FROM public.trace_backed_memory_schema WHERE schema_version = 1",
    ) == "1"
    assert assert_sql_succeeds(
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
    assert assert_sql_succeeds(cluster, "SELECT 1") == "1"


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
    cluster = PostgresCluster(
        "psql",
        {},
        tmp_path,
        clients=[
            TrackedClient(
                process, tmp_path / "race.stdout", tmp_path / "race.stderr"
            )  # type: ignore[arg-type]
        ],
    )

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
        clients=[
            TrackedClient(
                first, tmp_path / "first.stdout", tmp_path / "first.stderr"
            ),  # type: ignore[arg-type]
            TrackedClient(
                second, tmp_path / "second.stdout", tmp_path / "second.stderr"
            ),  # type: ignore[arg-type]
        ],
    )

    with pytest.raises(ExceptionGroup, match="PostgreSQL client cleanup failed"):
        cluster.terminate_clients()

    assert first.waited is True
    assert second.waited is True


def test_session_root_cleanup_rejects_root_outside_pytest_base(tmp_path: Path):
    pytest_base = tmp_path / "pytest-base"
    escaped_root = tmp_path / "escaped-session-root"
    pytest_base.mkdir()
    escaped_root.mkdir()
    marker = escaped_root / "must-remain"
    marker.write_text("protected", encoding="ascii")

    with pytest.raises(
        AssertionError,
        match="PostgreSQL cleanup root escaped pytest-owned parent",
    ):
        postgres_support._remove_postgres_test_root(escaped_root, pytest_base)

    assert marker.read_text(encoding="ascii") == "protected"


def test_per_test_root_cleanup_rejects_root_outside_tmp_path(tmp_path: Path):
    per_test_parent = tmp_path / "test-case"
    escaped_root = tmp_path / "escaped-cluster-root"
    per_test_parent.mkdir()
    escaped_root.mkdir()
    marker = escaped_root / "must-remain"
    marker.write_text("protected", encoding="ascii")

    with pytest.raises(
        AssertionError,
        match="PostgreSQL cleanup root escaped pytest-owned parent",
    ):
        postgres_support._remove_postgres_test_root(escaped_root, per_test_parent)

    assert marker.read_text(encoding="ascii") == "protected"


def test_database_cleanup_stages_are_independent_and_preserve_original_error(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "postgres-cluster"
    root.mkdir()
    events: list[str] = []
    server = PostgresServer(
        psql="psql",
        pg_ctl="pg_ctl",
        env={},
        root=tmp_path / "postgres-server",
        data=tmp_path / "postgres-server" / "data",
        baseline_roles=frozenset({"postgres"}),
    )
    database_name = _new_test_database_name()

    class FailingCluster:
        def terminate_clients(self) -> None:
            events.append("clients")
            raise RuntimeError("client cleanup failed")

    def fail_session_cleanup(*_args, **_kwargs) -> None:
        events.append("sessions")
        raise RuntimeError("session cleanup failed")

    def fail_database_cleanup(*_args, **_kwargs) -> None:
        events.append("database")
        raise RuntimeError("database cleanup failed")

    def extra_role(*_args, **_kwargs) -> frozenset[str]:
        events.append("roles")
        return frozenset({"postgres", "memory_cleanup_role"})

    def fail_role_cleanup(*_args, **_kwargs) -> subprocess.CompletedProcess[str]:
        return subprocess.CompletedProcess(["psql"], 1, "", "role cleanup failed")

    def fail_directory_cleanup(*_args, **_kwargs) -> None:
        events.append("directory")
        raise RuntimeError("directory cleanup failed")

    monkeypatch.setattr(
        postgres_support, "_terminate_database_sessions", fail_session_cleanup
    )
    monkeypatch.setattr(postgres_support, "_drop_test_database", fail_database_cleanup)
    monkeypatch.setattr(postgres_support, "_read_role_names", extra_role)
    monkeypatch.setattr(postgres_support, "_run_psql", fail_role_cleanup)
    monkeypatch.setattr(
        postgres_support, "_remove_postgres_test_root", fail_directory_cleanup
    )

    cleanup_errors = _cleanup_postgres_database_resources(
        server=server,
        cluster=FailingCluster(),  # type: ignore[arg-type]
        database_name=database_name,
        root=root,
        tmp_path=tmp_path,
    )

    assert events == ["clients", "sessions", "database", "roles", "directory"]
    assert [str(error) for error in cleanup_errors] == [
        "client cleanup failed",
        "session cleanup failed",
        "database cleanup failed",
        "PostgreSQL test role removal failed:\nrole cleanup failed",
        "directory cleanup failed",
    ]
    assert [error.__notes__ for error in cleanup_errors] == [
        ["PostgreSQL client cleanup stage"],
        ["PostgreSQL database session cleanup stage"],
        ["PostgreSQL database cleanup stage"],
        ["PostgreSQL role cleanup stage"],
        ["PostgreSQL directory cleanup stage"],
    ]
    original_error = AssertionError("original test failure")
    _report_cleanup_errors(cleanup_errors, original_error)
    assert any(
        "PostgreSQL cleanup also failed" in note
        for note in original_error.__notes__
    )

    with pytest.raises(ExceptionGroup, match="PostgreSQL cleanup failed"):
        _report_cleanup_errors(cleanup_errors, None)


def test_pytest_call_phase_cleanup_preserves_failure_and_runs_once(
    pytester: pytest.Pytester,
):
    pytester.makeconftest(
        f"""
        from pathlib import Path
        import sys

        import pytest

        sys.path.insert(0, {str(ROOT)!r})

        from tests import postgres_support
        from tests.conftest import pytest_runtest_call
        from tests.postgres_support import PostgresServer, postgres_cluster

        cleanup_log = Path({str(pytester.path / "cleanup.log")!r})

        postgres_support._create_test_database = lambda *_args, **_kwargs: None

        def fail_cleanup(*_args, **_kwargs):
            with cleanup_log.open("a", encoding="ascii") as stream:
                stream.write("cleanup\\n")
            return [RuntimeError("forced cleanup failure")]

        postgres_support._cleanup_postgres_database_resources = fail_cleanup

        @pytest.fixture(scope="session")
        def _postgres_server(tmp_path_factory):
            root = tmp_path_factory.mktemp("fake-postgres-server")
            return PostgresServer(
                psql="psql",
                pg_ctl="pg_ctl",
                env={{"PGDATABASE": "postgres"}},
                root=root,
                data=root / "data",
                baseline_roles=frozenset({{"postgres"}}),
            )
        """
    )
    test_file = pytester.makepyfile(
        """
        import pytest

        @pytest.fixture
        def failed_setup(postgres_cluster):
            raise RuntimeError("later fixture setup failure")

        def test_call_and_cleanup_fail(postgres_cluster):
            raise AssertionError("original call failure")

        def test_dynamic_call_and_cleanup_fail(request):
            request.getfixturevalue("postgres_cluster")
            raise AssertionError("original dynamic call failure")

        def test_only_cleanup_fails(postgres_cluster):
            pass

        def test_setup_fallback(failed_setup):
            pass
        """
    )

    scenarios = (
        ("test_call_and_cleanup_fail", {"failed": 1}, "original call failure"),
        (
            "test_dynamic_call_and_cleanup_fail",
            {"failed": 1},
            "original dynamic call failure",
        ),
        ("test_only_cleanup_fails", {"failed": 1}, None),
        ("test_setup_fallback", {"errors": 2}, "later fixture setup failure"),
    )
    cleanup_note = (
        "PostgreSQL cleanup also failed: RuntimeError: forced cleanup failure"
    )

    for node_name, expected_outcomes, original_failure in scenarios:
        cleanup_log = pytester.path / "cleanup.log"
        cleanup_log.unlink(missing_ok=True)
        result = pytester.runpytest_subprocess(
            "-q", f"{test_file.name}::{node_name}"
        )
        result.assert_outcomes(**expected_outcomes)
        output = result.stdout.str()
        assert cleanup_log.read_text(encoding="ascii").splitlines() == ["cleanup"]

        if original_failure is not None and node_name != "test_setup_fallback":
            output_lines = output.splitlines()
            original_failure_line = next(
                index
                for index, line in enumerate(output_lines)
                if f"AssertionError: {original_failure}" in line
            )
            assert cleanup_note in output_lines[original_failure_line + 1]
        else:
            assert "ExceptionGroup: PostgreSQL cleanup failed" in output

        if node_name == "test_setup_fallback":
            assert "later fixture setup failure" in output


def test_server_cleanup_retains_root_until_shutdown_is_confirmed(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "postgres-server"
    data = root / "data"
    data.mkdir(parents=True)
    (data / "postmaster.pid").write_text("123", encoding="ascii")
    server = PostgresServer(
        psql="psql",
        pg_ctl="pg_ctl",
        env={},
        root=root,
        data=data,
        baseline_roles=frozenset({"postgres"}),
    )
    events: list[str] = []
    real_rmtree = shutil.rmtree

    def fail_server_stop(*_args, **_kwargs) -> None:
        events.append("server")
        raise subprocess.TimeoutExpired("pg_ctl", 1)

    def fail_if_directory_removed(*_args, **_kwargs) -> None:
        pytest.fail("server root was removed before shutdown was confirmed")

    monkeypatch.setattr(subprocess, "run", fail_server_stop)
    monkeypatch.setattr(shutil, "rmtree", fail_if_directory_removed)

    cleanup_errors = _cleanup_postgres_server_resources(
        server=server, pytest_parent=tmp_path
    )

    assert events == ["server", "server"]
    assert [str(error) for error in cleanup_errors] == [
        "Command 'pg_ctl' timed out after 1 seconds",
        "PostgreSQL directory cleanup skipped because server shutdown "
        "could not be confirmed",
    ]
    assert [error.__notes__ for error in cleanup_errors] == [
        ["PostgreSQL server cleanup stage"],
        ["PostgreSQL directory cleanup stage"],
    ]
    assert root.exists() is True
    real_rmtree(root)


def test_server_cleanup_removes_root_when_shutdown_wins_command_race(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    root = tmp_path / "postgres-server"
    data = root / "data"
    data.mkdir(parents=True)
    pid_file = data / "postmaster.pid"
    pid_file.write_text("123", encoding="ascii")
    server = PostgresServer(
        psql="psql",
        pg_ctl="pg_ctl",
        env={},
        root=root,
        data=data,
        baseline_roles=frozenset({"postgres"}),
    )
    events: list[str] = []
    real_rmtree = shutil.rmtree

    def raced_server_stop(*args, **_kwargs):
        events.append("server")
        pid_file.unlink()
        return subprocess.CompletedProcess(args[0], 1, "", "server stopped")

    def remove_directory(path: Path, *, ignore_errors: bool) -> None:
        events.append("directory")
        real_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(subprocess, "run", raced_server_stop)
    monkeypatch.setattr(shutil, "rmtree", remove_directory)

    cleanup_errors = _cleanup_postgres_server_resources(
        server=server, pytest_parent=tmp_path
    )

    assert cleanup_errors == []
    assert events == ["server", "directory"]
    assert root.exists() is False


def test_postgres_start_retries_address_in_use_and_retains_successful_port(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    data = tmp_path / "data"
    data.mkdir()
    log = tmp_path / "postgres.log"
    ports = iter([41001, 41002])
    attempted_ports: list[str] = []

    monkeypatch.setattr(postgres_support, "_free_port", lambda: next(ports))

    def start(*args, env, **_kwargs):
        attempted_ports.append(env["PGPORT"])
        if len(attempted_ports) == 1:
            log.write_text(
                'could not bind IPv4 address "127.0.0.1": Address already in use',
                encoding="utf-8",
            )
            return subprocess.CompletedProcess(args[0], 1)
        return subprocess.CompletedProcess(args[0], 0)

    monkeypatch.setattr(subprocess, "run", start)

    started_env = postgres_support._start_postgres_server(
        pg_ctl="pg_ctl",
        data=data,
        log=log,
        env={"PGDATABASE": "postgres"},
    )

    assert attempted_ports == ["41001", "41002"]
    assert started_env["PGPORT"] == "41002"


@pytest.mark.parametrize("has_unix_sockets", [False, True])
def test_postgres_start_options_use_only_a_private_unix_socket_directory(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    has_unix_sockets: bool,
):
    data = tmp_path / "data directory"
    monkeypatch.setattr(
        postgres_support,
        "_HAS_UNIX_DOMAIN_SOCKETS",
        has_unix_sockets,
    )

    options = postgres_support._postgres_start_options(data, "41003")

    assert options.startswith("-F -p 41003 -h 127.0.0.1")
    assert (" -k " in options) is has_unix_sockets
    assert "/var/run/postgresql" not in options
    if has_unix_sockets:
        assert str(data) in options


def test_postgres_start_bounds_address_in_use_retries(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    data = tmp_path / "data"
    data.mkdir()
    log = tmp_path / "postgres.log"
    attempted_ports: list[str] = []
    ports = iter([42001, 42002, 42003, 42004])

    monkeypatch.setattr(postgres_support, "_free_port", lambda: next(ports))

    def address_in_use(*args, env, **_kwargs):
        attempted_ports.append(env["PGPORT"])
        log.write_text("Address already in use", encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 1)

    monkeypatch.setattr(subprocess, "run", address_in_use)

    with pytest.raises(
        RuntimeError,
        match="pg_ctl start failed after 3 address-in-use attempts",
    ):
        postgres_support._start_postgres_server(
            pg_ctl="pg_ctl",
            data=data,
            log=log,
            env={"PGDATABASE": "postgres"},
        )

    assert attempted_ports == ["42001", "42002", "42003"]


def test_postgres_start_does_not_retry_unrelated_failure(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    data = tmp_path / "data"
    data.mkdir()
    log = tmp_path / "postgres.log"
    attempts = 0

    monkeypatch.setattr(postgres_support, "_free_port", lambda: 43001)

    def configuration_failure(*args, **_kwargs):
        nonlocal attempts
        attempts += 1
        log.write_text("invalid value for parameter", encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 1)

    monkeypatch.setattr(subprocess, "run", configuration_failure)

    with pytest.raises(RuntimeError, match="pg_ctl start failed"):
        postgres_support._start_postgres_server(
            pg_ctl="pg_ctl",
            data=data,
            log=log,
            env={"PGDATABASE": "postgres"},
        )

    assert attempts == 1


def test_postgres_start_does_not_retry_when_pid_file_remains(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
):
    data = tmp_path / "data"
    data.mkdir()
    log = tmp_path / "postgres.log"
    attempts = 0

    monkeypatch.setattr(postgres_support, "_free_port", lambda: 44001)

    def ambiguous_start(*args, **_kwargs):
        nonlocal attempts
        attempts += 1
        (data / "postmaster.pid").write_text("123", encoding="ascii")
        log.write_text("Address already in use", encoding="utf-8")
        return subprocess.CompletedProcess(args[0], 1)

    monkeypatch.setattr(subprocess, "run", ambiguous_start)

    with pytest.raises(RuntimeError, match="pg_ctl start failed"):
        postgres_support._start_postgres_server(
            pg_ctl="pg_ctl",
            data=data,
            log=log,
            env={"PGDATABASE": "postgres"},
        )

    assert attempts == 1
    (data / "postmaster.pid").unlink()


def test_database_cleanup_removes_untracked_sessions_and_new_roles(
    _postgres_server: PostgresServer,
    tmp_path: Path,
):
    server = _postgres_server
    database_name = _new_test_database_name()
    root = tmp_path / "postgres-cluster"
    root.mkdir()
    untracked: subprocess.Popen[bytes] | None = None
    database_created = False
    try:
        postgres_support._create_test_database(server, database_name)
        database_created = True
        cluster = PostgresCluster(
            server.psql, {**server.env, "PGDATABASE": database_name}, root
        )
        untracked = subprocess.Popen(
            [
                server.psql,
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-q",
                "-c",
                "SELECT pg_sleep(60)",
            ],
            env={**cluster.env, "PGAPPNAME": "untracked_cleanup_client"},
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            close_fds=True,
        )
        _wait_for_activity(
            cluster,
            "untracked_cleanup_client",
            "Timeout",
            untracked,
        )
        assert_sql_succeeds(cluster, "CREATE ROLE memory_cleanup_role")

        cleanup_errors = _cleanup_postgres_database_resources(
            server=server,
            cluster=cluster,
            database_name=database_name,
            root=root,
            tmp_path=tmp_path,
        )

        assert cleanup_errors == []
        assert untracked.wait(timeout=5) is not None
        assert assert_sql_succeeds(
            PostgresCluster(server.psql, dict(server.env), tmp_path),
            "SELECT count(*) FROM pg_database "
            f"WHERE datname = '{database_name}'",
        ) == "0"
        assert assert_sql_succeeds(
            PostgresCluster(server.psql, dict(server.env), tmp_path),
            "SELECT count(*) FROM pg_roles WHERE rolname = 'memory_cleanup_role'",
        ) == "0"
    finally:
        cleanup_steps = [
            lambda: (
                untracked.terminate()
                if untracked is not None and untracked.poll() is None
                else None
            ),
            lambda: postgres_support._terminate_database_sessions(
                server, database_name
            )
            if database_created
            else None,
            lambda: postgres_support._drop_test_database(server, database_name)
            if database_created
            else None,
            lambda: postgres_support._run_psql(
                server.psql,
                server.env,
                "DROP ROLE IF EXISTS \"memory_cleanup_role\"",
            ),
            lambda: postgres_support._remove_postgres_test_root(root, tmp_path),
        ]
        for cleanup_step in cleanup_steps:
            try:
                cleanup_step()
            except Exception:
                pass


def test_postgres_registry_lifecycle_and_two_session_serialization(
    postgres_cluster: PostgresCluster,
):
    cluster = postgres_cluster
    cluster.load_schema()

    assert_sql_succeeds(
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
    assert assert_sql_succeeds(
        cluster,
        """
        SELECT string_agg(memory_id || ':' || memory_kind, ',' ORDER BY memory_id)
        FROM memory_ids
        WHERE memory_id IN ('case_app', 'lesson_app', 'policy_app')
        """,
    ) == "case_app:failure_case,lesson_app:lesson,policy_app:project_policy"

    assert_sql_fails(
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
        assert_sql_fails(
            cluster,
            truncate_sql,
            f"runtime memory table does not allow TRUNCATE: {table_name}",
        )
        _assert_registry_parity(cluster)
        assert_sql_fails(
            cluster,
            """
            INSERT INTO project_policies(policy_id, policy_text, scope_json)
            VALUES ('case_app', 'policy', '{"repo":"repo"}')
            """,
            "duplicate runtime memory_id: case_app",
        )

    assert assert_sql_succeeds(
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

    assert_sql_succeeds(
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
    assert assert_sql_succeeds(
        cluster,
        """
        SELECT count(*) FROM memory_ids
        WHERE memory_id IN (
          'case_restore_obsolete', 'lesson_restore_obsolete',
          'policy_restore_obsolete'
        )
        """,
    ) == "3"

    assert_sql_fails(
        cluster,
        "INSERT INTO memory_ids(memory_id, memory_kind) VALUES ('forged', 'lesson')",
        "memory_ids registry does not allow direct INSERT",
    )
    assert_sql_fails(
        cluster,
        "UPDATE memory_ids SET memory_kind = 'lesson' WHERE memory_id = 'case_app'",
        "memory_ids registry does not allow direct UPDATE",
    )
    assert_sql_fails(
        cluster,
        "DELETE FROM memory_ids WHERE memory_id = 'case_app'",
        "memory_ids registry does not allow direct DELETE",
    )
    assert_sql_fails(
        cluster,
        """
        INSERT INTO project_policies(policy_id, policy_text, scope_json)
        VALUES ('case_app', 'policy', '{"repo":"repo"}')
        """,
        "duplicate runtime memory_id: case_app",
    )

    assert_sql_succeeds(
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
    assert assert_sql_succeeds(
        cluster,
        "SELECT count(*) FROM memory_usage_decisions WHERE decision_id = 'decision_valid'",
    ) == "1"

    assert_sql_succeeds(
        cluster,
        """
        SET session_replication_role = replica;
        INSERT INTO memory_ids(memory_id, memory_kind) VALUES ('ghost_memory', 'lesson');
        SET session_replication_role = origin;
        """,
    )
    assert_sql_fails(
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
    assert_sql_fails(
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
    assert_sql_fails(
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
    assert_sql_fails(
        cluster,
        "UPDATE failure_cases SET status = 'draft' WHERE case_id = 'case_forward'",
        "failure case status transition is not allowed: verified -> draft",
    )
    assert_sql_succeeds(
        cluster,
        "UPDATE failure_cases SET status = 'obsolete' WHERE case_id = 'case_forward'",
    )
    assert_sql_fails(
        cluster,
        "UPDATE failure_cases SET status = 'verified' WHERE case_id = 'case_forward'",
        "failure case status transition is not allowed: obsolete -> verified",
    )

    _seed_verified_case(cluster, "child_forward")
    assert_sql_succeeds(
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
    assert_sql_fails(
        cluster,
        "UPDATE lessons SET status = 'active' WHERE lesson_id = 'lesson_forward'",
        "runtime memory status transition is not allowed: obsolete -> active",
    )
    assert_sql_fails(
        cluster,
        "UPDATE project_policies SET status = 'active' WHERE policy_id = 'policy_forward'",
        "runtime memory status transition is not allowed: obsolete -> active",
    )

    _seed_verified_case(cluster, "insert_first")
    insert_latch = cluster.acquire_latch("lesson_insert_latch", 91_001)
    holder = cluster.spawn(
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
    _wait_for_activity(cluster, "lesson_insert_holder", "Lock", holder.process)
    obsoleter = cluster.spawn(
        "parent_obsoleter",
        """
        SET lock_timeout = '5s';
        UPDATE failure_cases SET status = 'obsolete' WHERE case_id = 'case_insert_first';
        """,
    )
    _wait_for_activity(cluster, "parent_obsoleter", "Lock", obsoleter.process)
    cluster.release_latch(insert_latch)
    _wait_process(holder, expected_returncode=0)
    _wait_process(obsoleter, expected_returncode=0)
    assert assert_sql_succeeds(
        cluster,
        "SELECT status FROM lessons WHERE lesson_id = 'lesson_insert_first'",
    ) == "obsolete"

    _seed_verified_case(cluster, "parent_first")
    parent_latch = cluster.acquire_latch("parent_update_latch", 91_002)
    parent = cluster.spawn(
        "parent_update_holder",
        """
        BEGIN;
        UPDATE failure_cases SET status = 'obsolete' WHERE case_id = 'case_parent_first';
        SELECT pg_advisory_lock(91002);
        SELECT pg_advisory_unlock(91002);
        COMMIT;
        """,
    )
    _wait_for_activity(cluster, "parent_update_holder", "Lock", parent.process)
    lesson = cluster.spawn(
        "lesson_insert_waiter",
        """
        SET lock_timeout = '5s';
        INSERT INTO lessons(lesson_id, source_case_id, lesson_text, memory_type, scope_json)
        VALUES ('lesson_parent_first', 'case_parent_first', 'rule', 'procedural', '{"repo":"repo"}');
        """,
    )
    _wait_for_activity(cluster, "lesson_insert_waiter", "Lock", lesson.process)
    cluster.release_latch(parent_latch)
    _wait_process(parent, expected_returncode=0)
    lesson_stderr = _wait_process(
        lesson, expected_returncode=1
    )
    assert "verified regression-backed failure case" in lesson_stderr
    assert assert_sql_succeeds(
        cluster,
        "SELECT count(*) FROM lessons WHERE lesson_id = 'lesson_parent_first' AND status = 'active'",
    ) == "0"
