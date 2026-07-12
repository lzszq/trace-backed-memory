import subprocess
from dataclasses import FrozenInstanceError
from pathlib import Path

import pytest

from trace_backed_memory import (
    CommitAncestryCaptureError,
    CommitAncestryEvidence,
    TraceMetadataCaptureError,
    capture_commit_ancestry,
    capture_trace_metadata,
)


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
        ("git", "merge-base", "--is-ancestor", "ancestor", "current"),
        ("git", "merge-base", "--is-ancestor", "unrelated", "current"),
    ]


def test_capture_commit_ancestry_accepts_empty_anchors_without_running_git():
    def runner(_args: list[str], _cwd: str | None = None) -> int:
        raise AssertionError("Git must not run for empty anchors")

    assert capture_commit_ancestry("current", [], runner=runner) == (
        CommitAncestryEvidence(current_commit_sha="current", commit_relations=())
    )


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

    assert "git merge-base --is-ancestor anchor current" in str(captured.value)
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
