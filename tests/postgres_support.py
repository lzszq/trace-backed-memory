from __future__ import annotations

import json
import os
import re
import shutil
import socket
import subprocess
import sys
import time
import uuid
from collections.abc import Iterator, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from types import MappingProxyType

import pytest


ROOT = Path(__file__).resolve().parents[1]
_TEST_DATABASE_RE = re.compile(r"tbm_test_[0-9a-f]{32}\Z")
_POSTGRES_CLEANUP_CALLBACK_ATTR = "_postgres_cleanup_callback"
_POSTGRES_START_ATTEMPTS = 3
_POSTGRES_STOP_ATTEMPTS = 2
_ADDRESS_IN_USE_MARKERS = (
    "address already in use",
    "only one usage of each socket address",
    "wsaeaddrinuse",
    "eaddrinuse",
)


@dataclass(frozen=True)
class AdvisoryLatch:
    key: int
    process: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_path: Path


@dataclass
class TrackedClient:
    process: subprocess.Popen[bytes]
    stdout_path: Path
    stderr_path: Path


@dataclass(frozen=True)
class PostgresServer:
    psql: str
    pg_ctl: str
    env: Mapping[str, str]
    root: Path
    data: Path
    baseline_roles: frozenset[str]

    def __post_init__(self) -> None:
        object.__setattr__(self, "env", MappingProxyType(dict(self.env)))


def _new_test_database_name() -> str:
    return f"tbm_test_{uuid.uuid4().hex}"


def _require_test_database_name(database_name: str) -> str:
    if _TEST_DATABASE_RE.fullmatch(database_name) is None:
        raise ValueError("invalid PostgreSQL test database name")
    return database_name


def _quote_identifier(identifier: str) -> str:
    if not isinstance(identifier, str) or "\x00" in identifier:
        raise ValueError("PostgreSQL identifier must be a string without NUL")
    return '"' + identifier.replace('"', '""') + '"'


def _run_psql(
    psql: str,
    env: Mapping[str, str],
    sql: str,
    timeout: float = 15.0,
) -> subprocess.CompletedProcess[str]:
    return subprocess.run(
        [psql, "-X", "-v", "ON_ERROR_STOP=1", "-Atqc", sql],
        env=dict(env),
        stdin=subprocess.DEVNULL,
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        timeout=timeout,
        check=False,
    )


def _read_role_names(psql: str, env: Mapping[str, str]) -> frozenset[str]:
    result = _run_psql(
        psql,
        env,
        "SELECT COALESCE(json_agg(rolname ORDER BY rolname), '[]'::json)::text "
        "FROM pg_catalog.pg_roles",
    )
    if result.returncode != 0:
        raise RuntimeError("PostgreSQL role discovery failed:\n" + result.stderr)
    try:
        roles = json.loads(result.stdout)
    except (json.JSONDecodeError, TypeError):
        raise RuntimeError(
            "PostgreSQL role discovery returned invalid JSON"
        ) from None
    if not isinstance(roles, list) or not all(isinstance(role, str) for role in roles):
        raise RuntimeError("PostgreSQL role discovery returned invalid JSON")
    return frozenset(roles)


def _create_test_database(server: PostgresServer, database_name: str) -> None:
    database_name = _require_test_database_name(database_name)
    result = _run_psql(
        server.psql,
        server.env,
        f"CREATE DATABASE {_quote_identifier(database_name)} TEMPLATE template0",
    )
    if result.returncode != 0:
        raise RuntimeError("PostgreSQL test database creation failed:\n" + result.stderr)


def _terminate_database_sessions(server: PostgresServer, database_name: str) -> None:
    database_name = _require_test_database_name(database_name)
    result = _run_psql(
        server.psql,
        server.env,
        "SELECT pg_terminate_backend(pid) "
        "FROM pg_catalog.pg_stat_activity "
        f"WHERE datname = '{database_name}' AND pid <> pg_backend_pid()",
    )
    if result.returncode != 0:
        raise RuntimeError("PostgreSQL test session termination failed:\n" + result.stderr)


def _drop_test_database(server: PostgresServer, database_name: str) -> None:
    database_name = _require_test_database_name(database_name)
    result = _run_psql(
        server.psql,
        server.env,
        f"DROP DATABASE IF EXISTS {_quote_identifier(database_name)}",
    )
    if result.returncode != 0:
        raise RuntimeError("PostgreSQL test database removal failed:\n" + result.stderr)


@dataclass
class PostgresCluster:
    psql: str
    env: dict[str, str]
    root: Path
    clients: list[TrackedClient] = field(default_factory=list, repr=False)

    def connection_kwargs(self) -> dict[str, str]:
        return {
            "host": self.env["PGHOST"],
            "port": self.env["PGPORT"],
            "user": self.env["PGUSER"],
            "dbname": self.env["PGDATABASE"],
            "connect_timeout": self.env["PGCONNECT_TIMEOUT"],
        }

    def run(
        self,
        sql: str,
        *,
        timeout: float = 15.0,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        return _run_psql(
            self.psql,
            {**self.env, **(env or {})},
            sql,
            timeout=timeout,
        )

    def run_script(
        self,
        path: Path,
        *,
        env: dict[str, str] | None = None,
        timeout: float = 20.0,
    ) -> subprocess.CompletedProcess[str]:
        return subprocess.run(
            [
                self.psql,
                "-X",
                "-v",
                "ON_ERROR_STOP=1",
                "-f",
                str(path),
            ],
            env={**self.env, **(env or {})},
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=timeout,
            check=False,
        )

    def load_schema(
        self,
        *,
        sql: str | None = None,
        env: dict[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        schema = (
            sql
            if sql is not None
            else (ROOT / "schemas" / "postgres.sql").read_text(encoding="utf-8")
        )
        result = self.run(schema, timeout=20.0, env=env)
        assert result.returncode == 0, result.stderr
        return result

    def spawn(self, name: str, sql: str) -> TrackedClient:
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
        client = TrackedClient(process, stdout_path, stderr_path)
        self.clients.append(client)
        return client

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
        self.clients.append(TrackedClient(process, stdout_path, stderr_path))
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

    def terminate_clients(self) -> list[BaseException]:
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

        for client in self.clients:
            process = client.process
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

        for client in self.clients:
            process = client.process
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
        return cleanup_errors

    def unfinished_clients(self) -> list[TrackedClient]:
        return [client for client in self.clients if client.process.poll() is None]


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.bind(("127.0.0.1", 0))
        return int(listener.getsockname()[1])


def _start_postgres_server(
    *,
    pg_ctl: str,
    data: Path,
    log: Path,
    env: Mapping[str, str],
) -> dict[str, str]:
    for attempt in range(1, _POSTGRES_START_ATTEMPTS + 1):
        if log.exists():
            log.unlink()
        attempt_env = {**env, "PGPORT": str(_free_port())}
        start = subprocess.run(
            [
                pg_ctl,
                "-D",
                str(data),
                "-o",
                f"-F -p {attempt_env['PGPORT']} -h 127.0.0.1",
                "-l",
                str(log),
                "-w",
                "-t",
                "20",
                "start",
            ],
            env=attempt_env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            timeout=30,
            check=False,
        )
        if start.returncode == 0:
            return attempt_env

        details = (
            log.read_text(encoding="utf-8", errors="replace")
            if log.exists()
            else ""
        )
        address_in_use = any(
            marker in details.lower() for marker in _ADDRESS_IN_USE_MARKERS
        )
        server_may_be_running = (data / "postmaster.pid").exists()
        if address_in_use and not server_may_be_running:
            if attempt < _POSTGRES_START_ATTEMPTS:
                continue
            raise RuntimeError(
                "pg_ctl start failed after "
                f"{_POSTGRES_START_ATTEMPTS} address-in-use attempts:\n{details}"
            )
        raise RuntimeError("pg_ctl start failed:\n" + details)

    raise AssertionError("unreachable PostgreSQL startup retry state")


def _remove_postgres_test_root(root: Path, pytest_parent: Path) -> None:
    resolved_root = root.resolve()
    resolved_parent = pytest_parent.resolve()
    if resolved_root == resolved_parent or not resolved_root.is_relative_to(
        resolved_parent
    ):
        raise AssertionError(
            "PostgreSQL cleanup root escaped pytest-owned parent"
        )
    if resolved_root.exists():
        shutil.rmtree(resolved_root, ignore_errors=False)


def _cleanup_postgres_database_resources(
    *,
    server: PostgresServer,
    cluster: PostgresCluster | None,
    database_name: str,
    root: Path,
    tmp_path: Path,
) -> list[BaseException]:
    cleanup_errors: list[BaseException] = []

    try:
        if cluster is not None:
            cluster.terminate_clients()
    except Exception as exc:
        exc.add_note("PostgreSQL client cleanup stage")
        cleanup_errors.append(exc)

    try:
        _terminate_database_sessions(server, database_name)
    except Exception as exc:
        exc.add_note("PostgreSQL database session cleanup stage")
        cleanup_errors.append(exc)

    try:
        _drop_test_database(server, database_name)
    except Exception as exc:
        exc.add_note("PostgreSQL database cleanup stage")
        cleanup_errors.append(exc)

    try:
        extra_roles = (
            _read_role_names(server.psql, server.env) - server.baseline_roles
        )
    except Exception as exc:
        exc.add_note("PostgreSQL role cleanup stage")
        cleanup_errors.append(exc)
    else:
        for role_name in sorted(extra_roles):
            try:
                result = _run_psql(
                    server.psql,
                    server.env,
                    f"DROP ROLE {_quote_identifier(role_name)}",
                )
                if result.returncode != 0:
                    raise RuntimeError(
                        "PostgreSQL test role removal failed:\n" + result.stderr
                    )
            except Exception as exc:
                exc.add_note("PostgreSQL role cleanup stage")
                cleanup_errors.append(exc)

    try:
        _remove_postgres_test_root(root, tmp_path)
    except Exception as exc:
        exc.add_note("PostgreSQL directory cleanup stage")
        cleanup_errors.append(exc)

    return cleanup_errors


def _cleanup_postgres_server_resources(
    *, server: PostgresServer, pytest_parent: Path
) -> list[BaseException]:
    cleanup_errors: list[BaseException] = []
    pid_file = server.data / "postmaster.pid"
    shutdown_confirmed = not server.data.exists() or not pid_file.exists()
    last_stop_error: Exception | None = None

    if not shutdown_confirmed:
        for _attempt in range(_POSTGRES_STOP_ATTEMPTS):
            try:
                stop = subprocess.run(
                    [
                        server.pg_ctl,
                        "-D",
                        str(server.data),
                        "-m",
                        "immediate",
                        "-w",
                        "-t",
                        "20",
                        "stop",
                    ],
                    env=dict(server.env),
                    stdin=subprocess.DEVNULL,
                    capture_output=True,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    timeout=30,
                    check=False,
                )
                last_stop_error = None
                if stop.returncode != 0:
                    last_stop_error = RuntimeError(
                        "pg_ctl stop failed:\n" + stop.stdout + "\n" + stop.stderr
                    )
            except Exception as exc:
                last_stop_error = exc

            if not pid_file.exists():
                shutdown_confirmed = True
                last_stop_error = None
                break

        if not shutdown_confirmed:
            if last_stop_error is None:
                last_stop_error = RuntimeError(
                    "PostgreSQL shutdown could not be confirmed: postmaster.pid "
                    f"remains after {_POSTGRES_STOP_ATTEMPTS} stop attempts"
                )
            last_stop_error.add_note("PostgreSQL server cleanup stage")
            cleanup_errors.append(last_stop_error)

    if shutdown_confirmed:
        try:
            _remove_postgres_test_root(server.root, pytest_parent)
        except Exception as exc:
            exc.add_note("PostgreSQL directory cleanup stage")
            cleanup_errors.append(exc)
    else:
        skipped_cleanup = RuntimeError(
            "PostgreSQL directory cleanup skipped because server shutdown "
            "could not be confirmed"
        )
        skipped_cleanup.add_note("PostgreSQL directory cleanup stage")
        cleanup_errors.append(skipped_cleanup)

    return cleanup_errors


def _report_cleanup_errors(
    cleanup_errors: list[BaseException], original_error: BaseException | None
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


@pytest.fixture(scope="session")
def _postgres_server(
    tmp_path_factory: pytest.TempPathFactory,
) -> Iterator[PostgresServer]:
    executables = {
        name: shutil.which(name) for name in ("initdb", "pg_ctl", "psql")
    }
    missing = sorted(name for name, path in executables.items() if path is None)
    if missing:
        pytest.skip("PostgreSQL executables unavailable: " + ", ".join(missing))

    root = tmp_path_factory.mktemp("postgres-server")
    data = root / "data"
    log = root / "postgres.log"
    env = {
        **os.environ,
        "PGHOST": "127.0.0.1",
        "PGUSER": "postgres",
        "PGDATABASE": "postgres",
        "PGCONNECT_TIMEOUT": "5",
        "PGOPTIONS": "-c statement_timeout=10000",
    }
    original_error: BaseException | None = None
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

        try:
            env = _start_postgres_server(
                pg_ctl=executables["pg_ctl"],
                data=data,
                log=log,
                env=env,
            )
        except RuntimeError as exc:
            pytest.fail(str(exc))
        yield PostgresServer(
            psql=executables["psql"],
            pg_ctl=executables["pg_ctl"],
            env=env,
            root=root,
            data=data,
            baseline_roles=_read_role_names(executables["psql"], env),
        )
    except BaseException as exc:
        original_error = exc
        raise
    finally:
        _report_cleanup_errors(
            _cleanup_postgres_server_resources(
                server=PostgresServer(
                    psql=executables["psql"],
                    pg_ctl=executables["pg_ctl"],
                    env=env,
                    root=root,
                    data=data,
                    baseline_roles=frozenset(),
                ),
                pytest_parent=tmp_path_factory.getbasetemp(),
            ),
            original_error,
        )


@pytest.fixture
def postgres_cluster(
    _postgres_server: PostgresServer,
    tmp_path: Path,
    request: pytest.FixtureRequest,
) -> Iterator[PostgresCluster]:
    database_name = _new_test_database_name()
    root = tmp_path / "postgres-cluster"
    root.mkdir()
    cluster: PostgresCluster | None = None
    cleanup_started = False

    def cleanup(original_error: BaseException | None = None) -> None:
        nonlocal cleanup_started
        if cleanup_started:
            return
        cleanup_started = True
        _report_cleanup_errors(
            _cleanup_postgres_database_resources(
                server=_postgres_server,
                cluster=cluster,
                database_name=database_name,
                root=root,
                tmp_path=tmp_path,
            ),
            original_error,
        )

    setattr(request.node, _POSTGRES_CLEANUP_CALLBACK_ATTR, cleanup)
    request.addfinalizer(cleanup)
    _create_test_database(_postgres_server, database_name)
    cluster = PostgresCluster(
        _postgres_server.psql,
        {**_postgres_server.env, "PGDATABASE": database_name},
        root,
    )
    yield cluster


def assert_sql_succeeds(cluster: PostgresCluster, sql: str) -> str:
    result = cluster.run(sql)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def assert_sql_fails(cluster: PostgresCluster, sql: str, message: str) -> None:
    result = cluster.run(sql)
    assert result.returncode != 0, "SQL unexpectedly succeeded"
    assert message in result.stderr, result.stderr
