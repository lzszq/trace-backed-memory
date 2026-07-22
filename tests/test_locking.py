import errno
import inspect
import os
import subprocess
import sys
import time
from pathlib import Path

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.locking as locking
from trace_backed_memory import TraceBackedMemoryStore


class FloatLike(float):
    pass


def _symlink_or_skip(link_path: Path, target_path: Path) -> None:
    try:
        link_path.symlink_to(target_path)
    except NotImplementedError as error:
        pytest.skip(f"symbolic links are unavailable: {error}")
    except OSError as error:
        unsupported_errors = {
            errno.EACCES,
            errno.ENOSYS,
            errno.EPERM,
            getattr(errno, "ENOTSUP", errno.EPERM),
            getattr(errno, "EOPNOTSUPP", errno.EPERM),
        }
        if (
            error.errno not in unsupported_errors
            and getattr(error, "winerror", None) != 1314
        ):
            raise
        pytest.skip(f"symbolic links are unavailable: {error}")


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


def test_snapshot_write_lock_rejects_symbolic_link_sidecar_without_writing(
    tmp_path,
):
    snapshot_path = tmp_path / "snapshot.json"
    lock_path = tmp_path / "snapshot.json.tbm.lock"
    target_path = tmp_path / "unrelated-empty-file"
    target_path.write_bytes(b"")
    _symlink_or_skip(lock_path, target_path)

    with pytest.raises(
        OSError,
        match="snapshot lock sidecar must be a single-link regular file",
    ):
        with tbm.snapshot_write_lock(snapshot_path, timeout_seconds=0):
            raise AssertionError("a symbolic-link sidecar acquired the lock")

    assert lock_path.is_symlink()
    assert target_path.read_bytes() == b""


def test_snapshot_write_lock_rejects_hard_link_sidecar_without_writing(
    tmp_path,
):
    snapshot_path = tmp_path / "snapshot.json"
    lock_path = tmp_path / "snapshot.json.tbm.lock"
    target_path = tmp_path / "unrelated-empty-file"
    target_path.write_bytes(b"")
    os.link(target_path, lock_path)

    with pytest.raises(
        OSError,
        match="snapshot lock sidecar must be a single-link regular file",
    ):
        with tbm.snapshot_write_lock(snapshot_path, timeout_seconds=0):
            raise AssertionError("a hard-link sidecar acquired the lock")

    assert target_path.read_bytes() == b""


def test_snapshot_write_lock_rejects_sidecar_replaced_before_open(
    tmp_path,
    monkeypatch,
):
    snapshot_path = tmp_path / "snapshot.json"
    lock_path = tmp_path / "snapshot.json.tbm.lock"
    original_path = tmp_path / "original-lock-file"
    lock_path.write_bytes(b"0")
    original_open = locking.os.open
    replaced = False

    def replacing_open(path, flags, mode=0o777):
        nonlocal replaced
        if Path(path) == lock_path and not flags & os.O_EXCL and not replaced:
            replaced = True
            lock_path.replace(original_path)
            lock_path.write_bytes(b"")
        return original_open(path, flags, mode)

    monkeypatch.setattr(locking.os, "open", replacing_open)

    with pytest.raises(
        OSError,
        match="snapshot lock sidecar must be a single-link regular file",
    ):
        with tbm.snapshot_write_lock(snapshot_path, timeout_seconds=0):
            raise AssertionError("a replaced sidecar acquired the lock")

    assert replaced is True
    assert original_path.read_bytes() == b"0"
    assert lock_path.read_bytes() == b""


def test_snapshot_write_lock_rejects_sidecar_replaced_during_acquisition(
    tmp_path,
    monkeypatch,
):
    snapshot_path = tmp_path / "snapshot.json"
    lock_path = tmp_path / "snapshot.json.tbm.lock"
    replacement_path = tmp_path / "replacement-lock-file"
    replacement_path.write_bytes(b"0")
    original_acquire = locking._acquire_file_lock
    original_lstat = locking.os.lstat
    acquired = False

    def acquire_then_report_replacement(lock_file, path, timeout_seconds):
        nonlocal acquired
        original_acquire(lock_file, path, timeout_seconds)
        acquired = True

    def replacement_lstat(path):
        if acquired and Path(path) == lock_path:
            return original_lstat(replacement_path)
        return original_lstat(path)

    monkeypatch.setattr(
        locking,
        "_acquire_file_lock",
        acquire_then_report_replacement,
    )
    monkeypatch.setattr(locking.os, "lstat", replacement_lstat)

    with pytest.raises(
        OSError,
        match="snapshot lock sidecar must be a single-link regular file",
    ):
        with tbm.snapshot_write_lock(snapshot_path, timeout_seconds=0):
            raise AssertionError("a replaced lock identity reached the caller")

    assert acquired is True
    assert lock_path.read_bytes() == b"0"
    assert replacement_path.read_bytes() == b"0"


def test_snapshot_write_lock_rejects_windows_reparse_metadata(
    tmp_path,
    monkeypatch,
):
    lock_path = tmp_path / "snapshot.json.tbm.lock"
    lock_path.write_bytes(b"0")
    file_stat = lock_path.stat()

    class ReparseStat:
        st_mode = file_stat.st_mode
        st_nlink = 1
        st_file_attributes = 0x400

    monkeypatch.setattr(
        locking.stat,
        "FILE_ATTRIBUTE_REPARSE_POINT",
        0x400,
        raising=False,
    )

    with pytest.raises(
        OSError,
        match="snapshot lock sidecar must be a single-link regular file",
    ):
        locking._validate_lock_sidecar_stat(lock_path, ReparseStat())


def test_snapshot_write_lock_closes_descriptor_when_wrapping_fails(
    tmp_path,
    monkeypatch,
):
    snapshot_path = tmp_path / "snapshot.json"
    descriptors: list[int] = []
    original_open = locking.os.open

    def tracked_open(path, flags, mode=0o777):
        descriptor = original_open(path, flags, mode)
        descriptors.append(descriptor)
        return descriptor

    def reject_fdopen(_descriptor, _mode):
        raise RuntimeError("injected descriptor wrapping failure")

    monkeypatch.setattr(locking.os, "open", tracked_open)
    monkeypatch.setattr(locking.os, "fdopen", reject_fdopen)

    with pytest.raises(
        RuntimeError,
        match="injected descriptor wrapping failure",
    ):
        with tbm.snapshot_write_lock(snapshot_path, timeout_seconds=0):
            raise AssertionError("a failed wrapper acquired the lock")

    assert len(descriptors) == 1
    with pytest.raises(OSError):
        os.fstat(descriptors[0])


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
