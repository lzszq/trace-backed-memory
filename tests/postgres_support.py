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


def _cleanup_postgres_resources(
    *,
    cluster: PostgresCluster | None,
    data: Path,
    root: Path,
    tmp_path: Path,
    pg_ctl: str,
    env: dict[str, str],
) -> list[BaseException]:
    cleanup_errors: list[BaseException] = []

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


@pytest.fixture
def postgres_cluster(tmp_path: Path) -> Iterator[PostgresCluster]:
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


def assert_sql_succeeds(cluster: PostgresCluster, sql: str) -> str:
    result = cluster.run(sql)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip()


def assert_sql_fails(cluster: PostgresCluster, sql: str, message: str) -> None:
    result = cluster.run(sql)
    assert result.returncode != 0, "SQL unexpectedly succeeded"
    assert message in result.stderr, result.stderr
