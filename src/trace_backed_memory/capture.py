from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterable
from pathlib import Path
from threading import Event, Thread
from typing import BinaryIO

from .models import CommitAncestryEvidence, TraceMetadata
from .policy import METADATA_VALUE_MAX_CHARS

CommandRunner = Callable[[list[str], str | None], str]
AncestryRunner = Callable[[list[str], str | None], int]
COMMIT_ANCESTRY_MAX_ANCHORS = 1_000
GIT_CAPTURE_TIMEOUT_SECONDS = 30.0
GIT_CAPTURE_OUTPUT_MAX_BYTES = 64 * 1024
_GIT_CAPTURE_READ_CHUNK_BYTES = 8 * 1024
_GIT_CAPTURE_POLL_SECONDS = 0.05
_GIT_CAPTURE_REAP_TIMEOUT_SECONDS = 1.0


class TraceMetadataCaptureError(RuntimeError):
    """Raised when git metadata cannot be captured for a trace."""


class CommitAncestryCaptureError(RuntimeError):
    """Raised when Git ancestry evidence cannot be captured."""


class _GitCaptureOutputLimitError(RuntimeError):
    pass


class _BoundedPipeReader:
    def __init__(
        self,
        stream_name: str,
        stream: BinaryIO,
        max_bytes: int,
        *,
        fail_on_overflow: bool,
    ) -> None:
        self.stream_name = stream_name
        self.stream = stream
        self.max_bytes = max_bytes
        self.fail_on_overflow = fail_on_overflow
        self.data = bytearray()
        self.overflow = Event()
        self.failed = Event()
        self.error: Exception | None = None
        self.thread = Thread(
            target=self._read,
            name=f"tbm-git-{stream_name}",
            daemon=True,
        )

    def start(self) -> None:
        self.thread.start()

    def join(self) -> None:
        self.thread.join(timeout=_GIT_CAPTURE_REAP_TIMEOUT_SECONDS)
        if self.thread.is_alive():
            raise RuntimeError(
                f"git command {self.stream_name} reader did not stop"
            )
        if self.error is not None:
            raise RuntimeError(
                f"failed to read git command {self.stream_name}: {self.error}"
            ) from self.error

    def _read(self) -> None:
        try:
            while True:
                chunk = self.stream.read(_GIT_CAPTURE_READ_CHUNK_BYTES)
                if not chunk:
                    return
                remaining = self.max_bytes - len(self.data)
                if remaining > 0:
                    self.data.extend(chunk[:remaining])
                if len(chunk) > max(remaining, 0) and self.fail_on_overflow:
                    self.overflow.set()
        except Exception as exc:  # pragma: no cover - OS pipe failures vary
            self.error = exc
            self.failed.set()


def capture_trace_metadata(
    repo_path: str | None = None,
    *,
    runner: CommandRunner | None = None,
) -> TraceMetadata:
    run = runner or _run_command
    commit_args = ["git", "rev-parse", "HEAD"]
    commit_sha = _capture_metadata_output(
        run,
        commit_args,
        repo_path,
        output_name="commit SHA",
        required=True,
        max_chars=METADATA_VALUE_MAX_CHARS,
    )
    repo_args = ["git", "rev-parse", "--show-toplevel"]
    repo_root = _capture_metadata_output(
        run,
        repo_args,
        repo_path,
        output_name="repository root",
        required=True,
    )
    repo_name = Path(repo_root).name or None
    if repo_name is not None:
        _validate_metadata_output_length(
            repo_name,
            "repository name",
            repo_args,
            repo_path,
        )
    branch_args = ["git", "branch", "--show-current"]
    branch_output = _capture_metadata_output(
        run,
        branch_args,
        repo_path,
        output_name="branch",
        max_chars=METADATA_VALUE_MAX_CHARS,
    )
    status_run = runner or _run_status_command
    status_output = _run_git(
        status_run,
        ["git", "status", "--porcelain"],
        repo_path,
    )

    return TraceMetadata(
        commit_sha=commit_sha,
        repo=repo_name,
        branch=branch_output or None,
        dirty=bool(status_output.strip()),
    )


def capture_commit_ancestry(
    current_commit_sha: str,
    anchor_commit_shas: Iterable[str],
    repo_path: str | None = None,
    *,
    runner: AncestryRunner | None = None,
) -> CommitAncestryEvidence:
    _validate_commit_string(current_commit_sha, "current_commit_sha")
    if isinstance(anchor_commit_shas, (str, bytes)) or not isinstance(
        anchor_commit_shas, Iterable
    ):
        raise ValueError("anchor_commit_shas must be an iterable of commit strings")
    anchors: list[str] = []
    for anchor in anchor_commit_shas:
        if len(anchors) >= COMMIT_ANCESTRY_MAX_ANCHORS:
            raise ValueError(
                "anchor_commit_shas accepts at most "
                f"{COMMIT_ANCESTRY_MAX_ANCHORS} commit strings"
            )
        _validate_commit_string(anchor, "anchor commit")
        anchors.append(anchor)

    run = runner or _run_ancestry_command
    relations: list[tuple[str, bool]] = []
    for anchor in sorted(set(anchors)):
        args = [
            "git",
            "merge-base",
            "--is-ancestor",
            "--",
            anchor,
            current_commit_sha,
        ]
        return_code = _capture_ancestry_result(
            run, args, repo_path, anchor=anchor, current=current_commit_sha
        )
        relations.append((anchor, return_code == 0))
    return CommitAncestryEvidence(
        current_commit_sha=current_commit_sha,
        commit_relations=tuple(relations),
    )


def _validate_commit_string(value: object, field_name: str) -> None:
    if not isinstance(value, str) or not value:
        raise ValueError(f"{field_name} must be a non-empty string")
    if len(value) > METADATA_VALUE_MAX_CHARS:
        raise ValueError(
            f"{field_name} must be at most {METADATA_VALUE_MAX_CHARS} characters"
        )


def _run_ancestry_command(args: list[str], cwd: str | None = None) -> int:
    env = os.environ.copy()
    env["GIT_NO_LAZY_FETCH"] = "1"
    completed = _run_bounded_process(
        args,
        cwd=cwd,
        env=env,
    )
    if completed.returncode not in {0, 1}:
        raise subprocess.CalledProcessError(
            completed.returncode,
            args,
            output=completed.stdout,
            stderr=completed.stderr,
        )
    return completed.returncode


def _capture_ancestry_result(
    run: AncestryRunner,
    args: list[str],
    repo_path: str | None,
    *,
    anchor: str,
    current: str,
) -> int:
    command = " ".join(args)
    location = repo_path or "."
    try:
        result = run(args, repo_path)
    except Exception as exc:
        raise CommitAncestryCaptureError(
            f"failed to capture git ancestry with `{command}` in {location}: "
            f"{_command_error_detail(exc)}"
        ) from exc
    if type(result) is not int or result not in {0, 1}:
        raise CommitAncestryCaptureError(
            f"failed to capture git ancestry with `{command}` in {location}: "
            f"runner returned invalid exit code {result!r} for {anchor} against {current}"
        )
    return result


def _run_command(args: list[str], cwd: str | None = None) -> str:
    completed = _run_bounded_process(args, cwd=cwd)
    completed.check_returncode()
    return completed.stdout


def _run_status_command(args: list[str], cwd: str | None = None) -> str:
    completed = _run_bounded_process(
        args,
        cwd=cwd,
        stdout_max_bytes=1,
        fail_on_stdout_overflow=False,
    )
    completed.check_returncode()
    return "dirty" if completed.stdout else ""


def _run_bounded_process(
    args: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stdout_max_bytes: int = GIT_CAPTURE_OUTPUT_MAX_BYTES,
    fail_on_stdout_overflow: bool = True,
) -> subprocess.CompletedProcess[str]:
    process_group_options: dict[str, object]
    if os.name == "nt":
        process_group_options = {
            "creationflags": subprocess.CREATE_NEW_PROCESS_GROUP,
        }
    else:
        process_group_options = {"start_new_session": True}
    process = subprocess.Popen(
        args,
        cwd=cwd,
        env=env,
        stdin=subprocess.DEVNULL,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        bufsize=0,
        **process_group_options,
    )
    if process.stdout is None or process.stderr is None:  # pragma: no cover
        _kill_and_reap(process)
        raise RuntimeError("git command did not expose captured pipes")

    stdout_reader = _BoundedPipeReader(
        "stdout",
        process.stdout,
        stdout_max_bytes,
        fail_on_overflow=fail_on_stdout_overflow,
    )
    stderr_reader = _BoundedPipeReader(
        "stderr",
        process.stderr,
        GIT_CAPTURE_OUTPUT_MAX_BYTES,
        fail_on_overflow=True,
    )
    readers = (stdout_reader, stderr_reader)
    started_readers: list[_BoundedPipeReader] = []
    failure: tuple[str, int] | str | None = None
    returncode: int | None = None
    deadline = time.monotonic() + GIT_CAPTURE_TIMEOUT_SECONDS
    try:
        for reader in readers:
            reader.start()
            started_readers.append(reader)

        while True:
            if any(reader.failed.is_set() for reader in readers):
                failure = "reader"
                break
            overflow = _capture_overflow(readers)
            if overflow is not None:
                failure = overflow
                break
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                failure = "timeout"
                break
            try:
                returncode = process.wait(
                    timeout=min(_GIT_CAPTURE_POLL_SECONDS, remaining)
                )
                break
            except subprocess.TimeoutExpired:
                time.sleep(0)

        if failure is not None:
            returncode = _kill_and_reap(process)

        _join_pipe_readers(readers)

        if failure is None:
            failure = _capture_overflow(readers)

        stdout = bytes(stdout_reader.data).decode("utf-8", errors="replace")
        stderr = bytes(stderr_reader.data).decode("utf-8", errors="replace")
        if failure == "timeout":
            raise subprocess.TimeoutExpired(
                args,
                GIT_CAPTURE_TIMEOUT_SECONDS,
                output=stdout,
                stderr=stderr,
            )
        if failure == "reader":  # pragma: no cover - join raises first
            raise RuntimeError("git command pipe reader failed")
        if isinstance(failure, tuple):
            stream_name, max_bytes = failure
            raise _GitCaptureOutputLimitError(
                f"git command {stream_name} exceeded {max_bytes} bytes"
            )
        if returncode is None:  # pragma: no cover - defensive process contract
            raise RuntimeError("git command did not report an exit code")
        return subprocess.CompletedProcess(
            args,
            returncode,
            stdout=stdout,
            stderr=stderr,
        )
    except BaseException:
        if process.poll() is None:
            try:
                _kill_and_reap(process)
            except Exception:
                pass
        _join_pipe_readers(tuple(started_readers), suppress_errors=True)
        raise
    finally:
        process.stdout.close()
        process.stderr.close()


def _capture_overflow(
    readers: tuple[_BoundedPipeReader, _BoundedPipeReader],
) -> tuple[str, int] | None:
    for reader in readers:
        if reader.overflow.is_set():
            return reader.stream_name, reader.max_bytes
    return None


def _join_pipe_readers(
    readers: tuple[_BoundedPipeReader, ...],
    *,
    suppress_errors: bool = False,
) -> None:
    first_error: Exception | None = None
    for reader in readers:
        try:
            reader.join()
        except Exception as exc:
            if first_error is None:
                first_error = exc
    if first_error is not None and not suppress_errors:
        raise first_error


def _kill_and_reap(process: subprocess.Popen[bytes]) -> int:
    if process.poll() is None:
        _kill_process_tree(process)
    try:
        return process.wait(timeout=_GIT_CAPTURE_REAP_TIMEOUT_SECONDS)
    except subprocess.TimeoutExpired as exc:
        raise RuntimeError("failed to reap terminated git command") from exc


def _kill_process_tree(process: subprocess.Popen[bytes]) -> None:
    pid = getattr(process, "pid", None)
    if os.name == "nt" and isinstance(pid, int):
        system_root = os.environ.get("SystemRoot")
        if system_root:
            taskkill = Path(system_root) / "System32" / "taskkill.exe"
            try:
                subprocess.run(
                    [str(taskkill), "/PID", str(pid), "/T", "/F"],
                    check=False,
                    stdin=subprocess.DEVNULL,
                    stdout=subprocess.DEVNULL,
                    stderr=subprocess.DEVNULL,
                    timeout=_GIT_CAPTURE_REAP_TIMEOUT_SECONDS,
                )
            except Exception:
                pass
        if process.poll() is None:
            process.kill()
        return

    if isinstance(pid, int):
        try:
            os.killpg(pid, signal.SIGKILL)
            return
        except (OSError, ProcessLookupError):
            pass
    process.kill()


def _run_git(run: CommandRunner, args: list[str], repo_path: str | None) -> str:
    try:
        result = run(args, repo_path)
    except Exception as exc:  # pragma: no cover - exact exception type depends on runner/subprocess
        raise _metadata_capture_error(
            args,
            repo_path,
            _command_error_detail(exc),
        ) from exc
    if not isinstance(result, str):
        raise _metadata_capture_error(
            args,
            repo_path,
            "runner returned non-string output",
        )
    return str(result)


def _capture_metadata_output(
    run: CommandRunner,
    args: list[str],
    repo_path: str | None,
    *,
    output_name: str,
    required: bool = False,
    max_chars: int | None = None,
) -> str:
    output = _run_git(run, args, repo_path).strip()
    if required and not output:
        raise _metadata_capture_error(
            args,
            repo_path,
            f"runner returned blank {output_name} output",
        )
    if max_chars is not None:
        _validate_metadata_output_length(
            output,
            output_name,
            args,
            repo_path,
            max_chars=max_chars,
        )
    return output


def _validate_metadata_output_length(
    output: str,
    output_name: str,
    args: list[str],
    repo_path: str | None,
    *,
    max_chars: int = METADATA_VALUE_MAX_CHARS,
) -> None:
    if len(output) > max_chars:
        raise _metadata_capture_error(
            args,
            repo_path,
            f"{output_name} output must be at most {max_chars} characters",
        )


def _metadata_capture_error(
    args: list[str],
    repo_path: str | None,
    detail: str,
) -> TraceMetadataCaptureError:
    command = " ".join(args)
    location = repo_path or "."
    return TraceMetadataCaptureError(
        f"failed to capture git metadata with `{command}` in {location}: {detail}"
    )


def _command_error_detail(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = (exc.stderr or "").strip()
        if stderr:
            return stderr
    return str(exc)
