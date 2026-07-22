import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import trace_backed_memory as tbm
from trace_backed_memory import TraceBackedMemoryStore


class FloatLike(float):
    pass


def test_snapshot_write_lock_is_exported_from_the_package_root():
    assert "snapshot_write_lock" in tbm.__all__
    assert callable(tbm.snapshot_write_lock)


def test_snapshot_write_lock_has_documented_default_timeout():
    timeout = inspect.signature(tbm.snapshot_write_lock).parameters[
        "timeout_seconds"
    ]

    assert timeout.kind is inspect.Parameter.KEYWORD_ONLY
    assert timeout.default == 30.0


def test_snapshot_write_lock_zero_timeout_succeeds_without_contention(tmp_path):
    snapshot_path = tmp_path / "snapshot.json"

    with tbm.snapshot_write_lock(snapshot_path, timeout_seconds=0):
        pass

    assert (tmp_path / "snapshot.json.tbm.lock").read_bytes() == b"0"


@pytest.mark.parametrize(
    "timeout_seconds",
    [
        True,
        False,
        -1,
        -0.1,
        float("nan"),
        float("inf"),
        float("-inf"),
        "1",
        FloatLike(1.0),
        10**1_000,
    ],
)
def test_snapshot_write_lock_rejects_invalid_timeout_before_sidecar_creation(
    tmp_path,
    timeout_seconds,
):
    snapshot_path = tmp_path / "snapshot.json"
    lock_path = tmp_path / "snapshot.json.tbm.lock"

    with pytest.raises(
        ValueError,
        match="timeout_seconds must be a non-negative finite number",
    ):
        with tbm.snapshot_write_lock(
            snapshot_path,
            timeout_seconds=timeout_seconds,
        ):
            raise AssertionError("invalid timeout acquired the snapshot lock")

    assert not lock_path.exists()


def test_snapshot_write_lock_canonical_alias_times_out_then_recovers(
    tmp_path,
    monkeypatch,
):
    nested = tmp_path / "nested"
    nested.mkdir()
    snapshot_path = nested / "snapshot.json"
    lock_path = nested / "snapshot.json.tbm.lock"
    monkeypatch.chdir(tmp_path)
    relative_alias = Path("nested") / ".." / "nested" / "snapshot.json"

    with tbm.snapshot_write_lock(relative_alias) as lock_value:
        assert lock_value is None
        with pytest.raises(
            TimeoutError,
            match=r"timed out waiting for snapshot write lock",
        ):
            with tbm.snapshot_write_lock(
                snapshot_path.resolve(),
                timeout_seconds=0,
            ):
                raise AssertionError("canonical alias acquired an owned lock")

    with tbm.snapshot_write_lock(snapshot_path, timeout_seconds=0):
        pass
    assert lock_path.read_bytes() == b"0"


def test_snapshot_write_lock_releases_after_caller_error(tmp_path):
    snapshot_path = tmp_path / "snapshot.json"

    with pytest.raises(RuntimeError, match="injected transaction failure"):
        with tbm.snapshot_write_lock(snapshot_path):
            raise RuntimeError("injected transaction failure")

    with tbm.snapshot_write_lock(snapshot_path, timeout_seconds=0):
        pass


def test_snapshot_write_lock_serializes_python_process_transactions(tmp_path):
    snapshot_path = tmp_path / "snapshot.json"
    TraceBackedMemoryStore().save_json(snapshot_path)
    root = Path(__file__).resolve().parents[1]
    environment = os.environ.copy()
    existing_pythonpath = environment.get("PYTHONPATH")
    environment["PYTHONPATH"] = os.pathsep.join(
        item
        for item in (str(root / "src"), existing_pythonpath)
        if item
    )
    script = """
import sys
import time
from pathlib import Path
from trace_backed_memory import (
    ProjectPolicy,
    TraceBackedMemoryStore,
    snapshot_write_lock,
)

snapshot_path, policy_id, delay, ready_path = sys.argv[1:]
with snapshot_write_lock(snapshot_path, timeout_seconds=5):
    store = TraceBackedMemoryStore.load_json(snapshot_path)
    Path(ready_path).write_text("locked", encoding="utf-8")
    time.sleep(float(delay))
    store.add_project_policy(
        ProjectPolicy(
            policy_id=policy_id,
            policy_text=f"Policy {policy_id}",
            scope={"repo": "repo"},
        )
    )
    store.save_json(snapshot_path)
"""
    first_ready = tmp_path / "first.ready"
    first = subprocess.Popen(
        [
            sys.executable,
            "-c",
            script,
            str(snapshot_path),
            "policy_first",
            "0.5",
            str(first_ready),
        ],
        cwd=root,
        env=environment,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        text=True,
    )
    second: subprocess.Popen[str] | None = None

    try:
        ready_deadline = time.monotonic() + 5
        while not first_ready.exists() and time.monotonic() < ready_deadline:
            assert first.poll() is None
            time.sleep(0.01)
        assert first_ready.exists()
        assert first_ready.read_text(encoding="utf-8") == "locked"
        second = subprocess.Popen(
            [
                sys.executable,
                "-c",
                script,
                str(snapshot_path),
                "policy_second",
                "0",
                str(tmp_path / "second.ready"),
            ],
            cwd=root,
            env=environment,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            text=True,
        )
        time.sleep(0.2)
        assert second.poll() is None

        first_stdout, first_stderr = first.communicate(timeout=10)
        second_stdout, second_stderr = second.communicate(timeout=10)
        assert first.returncode == 0, (
            f"stdout:\n{first_stdout}\nstderr:\n{first_stderr}"
        )
        assert second.returncode == 0, (
            f"stdout:\n{second_stdout}\nstderr:\n{second_stderr}"
        )
    finally:
        for process in (first, second):
            if process is not None and process.poll() is None:
                process.kill()
                process.wait(timeout=5)

    restored = TraceBackedMemoryStore.load_json(snapshot_path)
    assert set(restored.project_policies) == {"policy_first", "policy_second"}
