import io
import os
import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.capture as capture_module
from trace_backed_memory import (
    CommitAncestryCaptureError,
    CommitAncestryEvidence,
    TraceMetadataCaptureError,
    capture_commit_ancestry,
    capture_trace_metadata,
)


class FakePopenProcess:
    def __init__(
        self,
        args: list[str],
        *,
        stdout: bytes = b"",
        stderr: bytes = b"",
        returncode: int = 0,
        wait_until_killed: bool = False,
        never_reap: bool = False,
    ) -> None:
        self.args = args
        self.stdout = io.BytesIO(stdout)
        self.stderr = io.BytesIO(stderr)
        self.final_returncode = returncode
        self.returncode: int | None = None
        self.wait_until_killed = wait_until_killed
        self.never_reap = never_reap
        self.killed = False
        self.wait_timeouts: list[float | None] = []

    def poll(self) -> int | None:
        return self.returncode

    def kill(self) -> None:
        self.killed = True

    def wait(self, timeout: float | None = None) -> int:
        self.wait_timeouts.append(timeout)
        if self.never_reap:
            raise subprocess.TimeoutExpired(self.args, timeout)
        if self.wait_until_killed and not self.killed:
            raise subprocess.TimeoutExpired(self.args, timeout)
        self.returncode = -9 if self.killed else self.final_returncode
        return self.returncode


def reject_subprocess_run(*_args: object, **_kwargs: object) -> None:
    raise AssertionError("default capture runners must use bounded Popen")


def test_capture_commit_ancestry_default_runner_uses_bounded_binary_process(
    monkeypatch: pytest.MonkeyPatch,
):
    monkeypatch.setenv("CAPTURE_EXISTING_VALUE", "preserved")
    monkeypatch.setenv("GIT_NO_LAZY_FETCH", "0")
    captured_env: dict[str, str] | None = None
    captured_kwargs: dict[str, object] = {}
    process: FakePopenProcess | None = None

    def popen(args: list[str], **kwargs: object) -> FakePopenProcess:
        nonlocal captured_env, process
        captured_env = kwargs.get("env")  # type: ignore[assignment]
        captured_kwargs.update(kwargs)
        process = FakePopenProcess(args)
        return process

    monkeypatch.setattr(capture_module.subprocess, "run", reject_subprocess_run)
    monkeypatch.setattr(capture_module.subprocess, "Popen", popen)

    evidence = capture_commit_ancestry("current", ["anchor"])

    assert evidence.commit_relations == (("anchor", True),)
    assert process is not None
    assert process.wait_timeouts
    assert process.wait_timeouts[0] is not None
    assert captured_kwargs["stdin"] is subprocess.DEVNULL
    assert captured_kwargs["stdout"] is subprocess.PIPE
    assert captured_kwargs["stderr"] is subprocess.PIPE
    assert captured_kwargs["bufsize"] == 0
    assert "text" not in captured_kwargs
    assert "encoding" not in captured_kwargs
    if os.name == "nt":
        assert (
            captured_kwargs["creationflags"]
            == subprocess.CREATE_NEW_PROCESS_GROUP
        )
        assert "start_new_session" not in captured_kwargs
    else:
        assert captured_kwargs["start_new_session"] is True
        assert "creationflags" not in captured_kwargs
    assert capture_module.GIT_CAPTURE_TIMEOUT_SECONDS == 30.0
    assert captured_env is not os.environ
    assert captured_env is not None
    assert captured_env["GIT_NO_LAZY_FETCH"] == "1"
    assert captured_env["CAPTURE_EXISTING_VALUE"] == "preserved"


def test_capture_commit_ancestry_default_runner_wraps_utf8_replaced_stderr(
    monkeypatch: pytest.MonkeyPatch,
):
    stderr = b"fatal: Not a valid object name missing \xff\n"

    def popen(args: list[str], **_kwargs: object) -> FakePopenProcess:
        return FakePopenProcess(args, returncode=128, stderr=stderr)

    monkeypatch.setattr(capture_module.subprocess, "run", reject_subprocess_run)
    monkeypatch.setattr(capture_module.subprocess, "Popen", popen)

    with pytest.raises(CommitAncestryCaptureError) as captured:
        capture_commit_ancestry("current", ["missing"])

    assert "fatal: Not a valid object name missing �" in str(captured.value)
    assert isinstance(captured.value.__cause__, subprocess.CalledProcessError)
    assert captured.value.__cause__.returncode == 128
    assert captured.value.__cause__.stderr == stderr.decode("utf-8", errors="replace")


def test_capture_commit_ancestry_timeout_kills_and_reaps_process(
    monkeypatch: pytest.MonkeyPatch,
):
    process: FakePopenProcess | None = None

    def popen(args: list[str], **_kwargs: object) -> FakePopenProcess:
        nonlocal process
        process = FakePopenProcess(args, wait_until_killed=True)
        return process

    monkeypatch.setattr(capture_module.subprocess, "run", reject_subprocess_run)
    monkeypatch.setattr(capture_module.subprocess, "Popen", popen)
    monkeypatch.setattr(capture_module, "GIT_CAPTURE_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(CommitAncestryCaptureError, match="timed out") as captured:
        capture_commit_ancestry("current", ["anchor"])

    assert isinstance(captured.value.__cause__, subprocess.TimeoutExpired)
    assert process is not None
    assert process.killed is True
    assert process.returncode == -9
    assert len(process.wait_timeouts) >= 1


def test_capture_commit_ancestry_bounds_stderr_and_kills_process(
    monkeypatch: pytest.MonkeyPatch,
):
    limit = capture_module.GIT_CAPTURE_OUTPUT_MAX_BYTES
    process: FakePopenProcess | None = None

    def popen(args: list[str], **_kwargs: object) -> FakePopenProcess:
        nonlocal process
        process = FakePopenProcess(
            args,
            stderr=b"e" * (limit + 1),
            wait_until_killed=True,
        )
        return process

    monkeypatch.setattr(capture_module.subprocess, "run", reject_subprocess_run)
    monkeypatch.setattr(capture_module.subprocess, "Popen", popen)

    with pytest.raises(
        CommitAncestryCaptureError,
        match=rf"stderr exceeded {limit} bytes",
    ):
        capture_commit_ancestry("current", ["anchor"])

    assert process is not None
    assert process.killed is True
    assert process.returncode == -9


def test_capture_commit_ancestry_wraps_reader_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    class FailingStream(io.BytesIO):
        def read(self, _size: int = -1) -> bytes:
            raise OSError("pipe read failed")

    def popen(args: list[str], **_kwargs: object) -> FakePopenProcess:
        process = FakePopenProcess(args)
        process.stdout = FailingStream()
        return process

    monkeypatch.setattr(capture_module.subprocess, "run", reject_subprocess_run)
    monkeypatch.setattr(capture_module.subprocess, "Popen", popen)

    with pytest.raises(
        CommitAncestryCaptureError,
        match="failed to read git command stdout: pipe read failed",
    ) as captured:
        capture_commit_ancestry("current", ["anchor"])

    assert isinstance(captured.value.__cause__, RuntimeError)


def test_capture_commit_ancestry_wraps_reap_timeout(
    monkeypatch: pytest.MonkeyPatch,
):
    process: FakePopenProcess | None = None

    def popen(args: list[str], **_kwargs: object) -> FakePopenProcess:
        nonlocal process
        process = FakePopenProcess(args, never_reap=True)
        return process

    monkeypatch.setattr(capture_module.subprocess, "run", reject_subprocess_run)
    monkeypatch.setattr(capture_module.subprocess, "Popen", popen)
    monkeypatch.setattr(capture_module, "GIT_CAPTURE_TIMEOUT_SECONDS", 0.0)

    with pytest.raises(
        CommitAncestryCaptureError,
        match="failed to reap terminated git command",
    ) as captured:
        capture_commit_ancestry("current", ["anchor"])

    assert isinstance(captured.value.__cause__, RuntimeError)
    assert process is not None
    assert process.killed is True


def test_capture_commit_ancestry_sorts_deduplicates_and_records_false():
    calls: list[tuple[str, ...]] = []

    def runner(args: list[str], cwd: str | None = None) -> int:
        calls.append(tuple(args))
        return {"ancestor": 0, "unrelated": 1}[args[-2]]

    evidence = capture_commit_ancestry(
        "current",
        ["unrelated", "ancestor", "ancestor"],
        repo_path="C:/work/repo",
        runner=runner,
    )

    assert evidence == CommitAncestryEvidence(
        current_commit_sha="current",
        commit_relations=(("ancestor", True), ("unrelated", False)),
    )
    assert calls == [
        ("git", "merge-base", "--is-ancestor", "--", "ancestor", "current"),
        ("git", "merge-base", "--is-ancestor", "--", "unrelated", "current"),
    ]


def test_capture_commit_ancestry_places_option_like_revisions_after_terminator():
    calls: list[tuple[str, ...]] = []

    def runner(args: list[str], _cwd: str | None = None) -> int:
        calls.append(tuple(args))
        return 1

    evidence = capture_commit_ancestry(
        "--octopus",
        ["--all"],
        runner=runner,
    )

    assert evidence.commit_relations == (("--all", False),)
    assert calls == [
        ("git", "merge-base", "--is-ancestor", "--", "--all", "--octopus")
    ]


def test_capture_commit_ancestry_accepts_empty_anchors_without_running_git():
    def runner(_args: list[str], _cwd: str | None = None) -> int:
        raise AssertionError("Git must not run for empty anchors")

    assert capture_commit_ancestry("current", [], runner=runner) == (
        CommitAncestryEvidence(current_commit_sha="current", commit_relations=())
    )


def test_capture_commit_ancestry_accepts_exact_anchor_budget():
    calls: list[tuple[str, ...]] = []
    anchors = [
        f"anchor_{index:04d}"
        for index in range(tbm.COMMIT_ANCESTRY_MAX_ANCHORS)
    ]

    def runner(args: list[str], _cwd: str | None = None) -> int:
        calls.append(tuple(args))
        return 0

    evidence = capture_commit_ancestry(
        "current",
        anchors,
        runner=runner,
    )

    assert len(evidence.commit_relations) == tbm.COMMIT_ANCESTRY_MAX_ANCHORS
    assert evidence.commit_relations[0] == ("anchor_0000", True)
    assert evidence.commit_relations[-1] == ("anchor_0999", True)
    assert len(calls) == tbm.COMMIT_ANCESTRY_MAX_ANCHORS
    assert calls[0] == (
        "git",
        "merge-base",
        "--is-ancestor",
        "--",
        "anchor_0000",
        "current",
    )
    assert calls[-1][-2:] == ("anchor_0999", "current")


def test_capture_commit_ancestry_bounds_duplicate_generator_before_git():
    pulls = 0

    def anchors():
        nonlocal pulls
        for _index in range(tbm.COMMIT_ANCESTRY_MAX_ANCHORS + 2):
            pulls += 1
            yield "anchor"

    def runner(_args: list[str], _cwd: str | None = None) -> int:
        raise AssertionError("Git must not run for oversized anchors")

    with pytest.raises(
        ValueError,
        match="^anchor_commit_shas accepts at most 1000 commit strings$",
    ):
        capture_commit_ancestry("current", anchors(), runner=runner)

    assert pulls == tbm.COMMIT_ANCESTRY_MAX_ANCHORS + 1


def test_commit_ancestry_evidence_is_frozen():
    evidence = CommitAncestryEvidence("current", (("anchor", True),))

    with pytest.raises(FrozenInstanceError):
        evidence.current_commit_sha = "other"  # type: ignore[misc]


@pytest.mark.parametrize(
    ("current", "anchors", "message"),
    [
        ("", [], "current_commit_sha must be a non-empty string"),
        ([], [], "current_commit_sha must be a non-empty string"),
        ("x" * 513, [], "current_commit_sha must be at most 512 characters"),
        ("current", "ancestor", "anchor_commit_shas must be an iterable of commit strings"),
        ("current", [""], "anchor commit must be a non-empty string"),
        ("current", [1], "anchor commit must be a non-empty string"),
        ("current", ["x" * 513], "anchor commit must be at most 512 characters"),
    ],
)
def test_capture_commit_ancestry_rejects_malformed_inputs(
    current: object, anchors: object, message: str
):
    with pytest.raises(ValueError, match=message):
        capture_commit_ancestry(current, anchors)  # type: ignore[arg-type]


@pytest.mark.parametrize("return_code", [True, -1, 2, "0"])
def test_capture_commit_ancestry_rejects_invalid_runner_results(return_code: object):
    with pytest.raises(CommitAncestryCaptureError, match="git merge-base"):
        capture_commit_ancestry(
            "current",
            ["anchor"],
            runner=lambda _args, _cwd=None: return_code,  # type: ignore[return-value]
        )


def test_capture_commit_ancestry_wraps_command_failures_with_context():
    failure = subprocess.CalledProcessError(
        128,
        ["git", "merge-base"],
        stderr="fatal: bad object anchor",
    )

    def runner(_args: list[str], _cwd: str | None = None) -> int:
        raise failure

    with pytest.raises(CommitAncestryCaptureError) as captured:
        capture_commit_ancestry(
            "current", ["anchor"], repo_path="C:/work/repo", runner=runner
        )

    assert "git merge-base --is-ancestor -- anchor current" in str(captured.value)
    assert "C:/work/repo" in str(captured.value)
    assert "fatal: bad object anchor" in str(captured.value)
    assert captured.value.__cause__ is failure


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", *args],
        cwd=repo,
        check=True,
        capture_output=True,
        text=True,
    ).stdout.strip()


def _commit_file(repo: Path, name: str, content: str) -> str:
    (repo / name).write_text(content, encoding="utf-8")
    _git(repo, "add", name)
    _git(repo, "commit", "-m", name)
    return _git(repo, "rev-parse", "HEAD")


def test_capture_commit_ancestry_against_real_git_dag(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Trace Tests")
    base = _commit_file(repo, "base.txt", "base")
    _git(repo, "checkout", "-b", "side")
    side = _commit_file(repo, "side.txt", "side")
    _git(repo, "checkout", "main")
    current = _commit_file(repo, "current.txt", "current")

    evidence = capture_commit_ancestry(
        current,
        [side, current, base],
        repo_path=str(repo),
    )

    assert dict(evidence.commit_relations) == {
        base: True,
        current: True,
        side: False,
    }


def test_capture_trace_metadata_reads_commit_branch_and_dirty_state():
    commands: list[tuple[str, ...]] = []

    def runner(args: list[str], cwd: str | None = None) -> str:
        commands.append(tuple(args))
        if args == ["git", "rev-parse", "HEAD"]:
            return "abc123\n"
        if args == ["git", "rev-parse", "--show-toplevel"]:
            return "C:/work/agent-harness\n"
        if args == ["git", "branch", "--show-current"]:
            return "main\n"
        if args == ["git", "status", "--porcelain"]:
            return " M README.md\n"
        raise AssertionError(f"unexpected command: {args}")

    metadata = capture_trace_metadata(repo_path=".", runner=runner)

    assert metadata.commit_sha == "abc123"
    assert metadata.repo == "agent-harness"
    assert metadata.branch == "main"
    assert metadata.dirty is True
    assert commands == [
        ("git", "rev-parse", "HEAD"),
        ("git", "rev-parse", "--show-toplevel"),
        ("git", "branch", "--show-current"),
        ("git", "status", "--porcelain"),
    ]


def test_capture_trace_metadata_marks_clean_repo():
    def runner(args: list[str], cwd: str | None = None) -> str:
        if args == ["git", "rev-parse", "HEAD"]:
            return "abc123\n"
        if args == ["git", "rev-parse", "--show-toplevel"]:
            return "/tmp/trace-backed-memory\n"
        if args == ["git", "branch", "--show-current"]:
            return "\n"
        if args == ["git", "status", "--porcelain"]:
            return ""
        raise AssertionError(f"unexpected command: {args}")

    metadata = capture_trace_metadata(runner=runner)

    assert metadata.commit_sha == "abc123"
    assert metadata.repo == "trace-backed-memory"
    assert metadata.branch is None
    assert metadata.dirty is False


_VALID_METADATA_OUTPUTS = {
    ("git", "rev-parse", "HEAD"): "abc123\n",
    ("git", "rev-parse", "--show-toplevel"): "C:/work/repo\n",
    ("git", "branch", "--show-current"): "main\n",
    ("git", "status", "--porcelain"): "",
}


@pytest.mark.parametrize(
    ("invalid_command", "output_name", "expected_calls"),
    [
        (("git", "rev-parse", "HEAD"), "commit SHA", 1),
        (
            ("git", "rev-parse", "--show-toplevel"),
            "repository root",
            2,
        ),
    ],
)
def test_capture_trace_metadata_rejects_blank_required_runner_output(
    invalid_command,
    output_name,
    expected_calls,
):
    commands: list[tuple[str, ...]] = []

    def runner(args: list[str], cwd: str | None = None) -> str:
        command = tuple(args)
        commands.append(command)
        if command == invalid_command:
            return " \t\n"
        return _VALID_METADATA_OUTPUTS[command]

    with pytest.raises(
        TraceMetadataCaptureError,
        match=rf"runner returned blank {output_name} output",
    ) as captured:
        capture_trace_metadata(repo_path="C:/work/repo", runner=runner)

    assert " ".join(invalid_command) in str(captured.value)
    assert "C:/work/repo" in str(captured.value)
    assert commands == list(_VALID_METADATA_OUTPUTS)[:expected_calls]


@pytest.mark.parametrize("invalid_command", list(_VALID_METADATA_OUTPUTS))
def test_capture_trace_metadata_rejects_non_string_runner_output(
    invalid_command,
):
    class SensitiveOutput:
        def __repr__(self) -> str:
            return "DO_NOT_ECHO_RUNNER_OUTPUT"

    commands: list[tuple[str, ...]] = []

    def runner(args: list[str], cwd: str | None = None) -> object:
        command = tuple(args)
        commands.append(command)
        if command == invalid_command:
            return SensitiveOutput()
        return _VALID_METADATA_OUTPUTS[command]

    with pytest.raises(
        TraceMetadataCaptureError,
        match="runner returned non-string output",
    ) as captured:
        capture_trace_metadata(
            repo_path="C:/work/repo",
            runner=runner,  # type: ignore[arg-type]
        )

    message = str(captured.value)
    assert " ".join(invalid_command) in message
    assert "DO_NOT_ECHO_RUNNER_OUTPUT" not in message
    expected_index = list(_VALID_METADATA_OUTPUTS).index(invalid_command)
    assert commands == list(_VALID_METADATA_OUTPUTS)[: expected_index + 1]


def test_capture_trace_metadata_accepts_exact_metadata_character_limits():
    limit = capture_module.METADATA_VALUE_MAX_CHARS
    outputs = {
        **_VALID_METADATA_OUTPUTS,
        ("git", "rev-parse", "HEAD"): "c" * limit,
        ("git", "rev-parse", "--show-toplevel"): (
            "C:/work/" + "r" * limit
        ),
        ("git", "branch", "--show-current"): "b" * limit,
    }

    metadata = capture_trace_metadata(
        runner=lambda args, _cwd=None: outputs[tuple(args)]
    )

    assert metadata.commit_sha == "c" * limit
    assert metadata.repo == "r" * limit
    assert metadata.branch == "b" * limit
    assert metadata.dirty is False


@pytest.mark.parametrize(
    ("invalid_command", "output_name", "invalid_output"),
    [
        (
            ("git", "rev-parse", "HEAD"),
            "commit SHA",
            "c" * (capture_module.METADATA_VALUE_MAX_CHARS + 1),
        ),
        (
            ("git", "rev-parse", "--show-toplevel"),
            "repository name",
            "C:/work/" + "r" * (capture_module.METADATA_VALUE_MAX_CHARS + 1),
        ),
        (
            ("git", "branch", "--show-current"),
            "branch",
            "b" * (capture_module.METADATA_VALUE_MAX_CHARS + 1),
        ),
    ],
)
def test_capture_trace_metadata_rejects_oversized_metadata_output(
    invalid_command,
    output_name,
    invalid_output,
):
    outputs = {**_VALID_METADATA_OUTPUTS, invalid_command: invalid_output}

    with pytest.raises(
        TraceMetadataCaptureError,
        match=(
            rf"{output_name} output must be at most "
            rf"{capture_module.METADATA_VALUE_MAX_CHARS} characters"
        ),
    ) as captured:
        capture_trace_metadata(
            runner=lambda args, _cwd=None: outputs[tuple(args)]
        )

    assert invalid_output not in str(captured.value)


def test_capture_trace_metadata_maps_filesystem_root_repo_to_none():
    outputs = {
        **_VALID_METADATA_OUTPUTS,
        ("git", "rev-parse", "--show-toplevel"): Path.cwd().anchor,
    }

    metadata = capture_trace_metadata(
        runner=lambda args, _cwd=None: outputs[tuple(args)]
    )

    assert metadata.repo is None


def test_capture_trace_metadata_default_runner_discards_large_status_output(
    monkeypatch: pytest.MonkeyPatch,
):
    outputs = {
        ("git", "rev-parse", "HEAD"): b"abc123\n",
        ("git", "rev-parse", "--show-toplevel"): b"C:/work/repo\n",
        ("git", "branch", "--show-current"): b"main\n",
        ("git", "status", "--porcelain"): b" " + b"x" * 1_000_000,
    }
    calls: list[tuple[str, ...]] = []

    def popen(args: list[str], **kwargs: object) -> FakePopenProcess:
        calls.append(tuple(args))
        assert kwargs["stdin"] is subprocess.DEVNULL
        assert kwargs["stdout"] is subprocess.PIPE
        assert kwargs["stderr"] is subprocess.PIPE
        return FakePopenProcess(args, stdout=outputs[tuple(args)])

    monkeypatch.setattr(capture_module.subprocess, "run", reject_subprocess_run)
    monkeypatch.setattr(capture_module.subprocess, "Popen", popen)

    metadata = capture_trace_metadata(repo_path="C:/work/repo")

    assert metadata.commit_sha == "abc123"
    assert metadata.repo == "repo"
    assert metadata.branch == "main"
    assert metadata.dirty is True
    assert calls == [
        ("git", "rev-parse", "HEAD"),
        ("git", "rev-parse", "--show-toplevel"),
        ("git", "branch", "--show-current"),
        ("git", "status", "--porcelain"),
    ]


def test_capture_status_runner_retains_only_presence_and_handles_space(
    monkeypatch: pytest.MonkeyPatch,
):
    captured_kwargs: dict[str, object] = {}

    def run(
        args: list[str],
        **kwargs: object,
    ) -> subprocess.CompletedProcess[str]:
        captured_kwargs.update(kwargs)
        return subprocess.CompletedProcess(args, 0, stdout=" ", stderr="")

    monkeypatch.setattr(capture_module, "_run_bounded_process", run)

    assert capture_module._run_status_command(
        ["git", "status", "--porcelain"]
    ) == "dirty"
    assert captured_kwargs["stdout_max_bytes"] == 1
    assert captured_kwargs["fail_on_stdout_overflow"] is False


def test_capture_default_runner_accepts_exact_output_budget(
    monkeypatch: pytest.MonkeyPatch,
):
    limit = capture_module.GIT_CAPTURE_OUTPUT_MAX_BYTES

    def popen(args: list[str], **_kwargs: object) -> FakePopenProcess:
        return FakePopenProcess(args, stdout=b"x" * limit)

    monkeypatch.setattr(capture_module.subprocess, "run", reject_subprocess_run)
    monkeypatch.setattr(capture_module.subprocess, "Popen", popen)

    output = capture_module._run_command(["git", "rev-parse", "HEAD"])

    assert len(output.encode("utf-8")) == limit


def test_capture_trace_metadata_default_runner_bounds_stdout(
    monkeypatch: pytest.MonkeyPatch,
):
    limit = capture_module.GIT_CAPTURE_OUTPUT_MAX_BYTES
    process: FakePopenProcess | None = None

    def popen(args: list[str], **_kwargs: object) -> FakePopenProcess:
        nonlocal process
        process = FakePopenProcess(
            args,
            stdout=b"x" * (limit + 1),
            wait_until_killed=True,
        )
        return process

    monkeypatch.setattr(capture_module.subprocess, "run", reject_subprocess_run)
    monkeypatch.setattr(capture_module.subprocess, "Popen", popen)

    with pytest.raises(
        TraceMetadataCaptureError,
        match=rf"stdout exceeded {limit} bytes",
    ):
        capture_trace_metadata()

    assert process is not None
    assert process.killed is True
    assert process.returncode == -9


def test_capture_trace_metadata_wraps_process_start_failure(
    monkeypatch: pytest.MonkeyPatch,
):
    failure = OSError("git executable unavailable")

    def popen(_args: list[str], **_kwargs: object) -> FakePopenProcess:
        raise failure

    monkeypatch.setattr(capture_module.subprocess, "run", reject_subprocess_run)
    monkeypatch.setattr(capture_module.subprocess, "Popen", popen)

    with pytest.raises(
        TraceMetadataCaptureError,
        match="git executable unavailable",
    ) as captured:
        capture_trace_metadata()

    assert captured.value.__cause__ is failure


def test_capture_trace_metadata_default_runner_marks_real_git_dirty(tmp_path):
    repo = tmp_path / "repo"
    repo.mkdir()
    _git(repo, "init", "-b", "main")
    _git(repo, "config", "user.email", "tests@example.com")
    _git(repo, "config", "user.name", "Trace Tests")
    _commit_file(repo, "tracked.txt", "tracked")

    clean = capture_trace_metadata(repo_path=str(repo))
    (repo / "untracked.txt").write_text("untracked", encoding="utf-8")
    dirty = capture_trace_metadata(repo_path=str(repo))

    assert clean.dirty is False
    assert dirty.dirty is True


def test_capture_trace_metadata_wraps_git_command_failures():
    def runner(args: list[str], cwd: str | None = None) -> str:
        raise RuntimeError("git unavailable")

    try:
        capture_trace_metadata(repo_path="C:/work/repo", runner=runner)
    except TraceMetadataCaptureError as exc:
        message = str(exc)
        assert "git rev-parse HEAD" in message
        assert "C:/work/repo" in message
        assert "git unavailable" in message
    else:
        raise AssertionError("git metadata capture should wrap command failures")
