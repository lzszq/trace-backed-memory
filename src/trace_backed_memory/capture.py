from __future__ import annotations

import subprocess
from collections.abc import Callable, Iterable
from pathlib import Path

from .models import CommitAncestryEvidence, TraceMetadata
from .policy import METADATA_VALUE_MAX_CHARS

CommandRunner = Callable[[list[str], str | None], str]
AncestryRunner = Callable[[list[str], str | None], int]


class TraceMetadataCaptureError(RuntimeError):
    """Raised when git metadata cannot be captured for a trace."""


class CommitAncestryCaptureError(RuntimeError):
    """Raised when Git ancestry evidence cannot be captured."""


def capture_trace_metadata(
    repo_path: str | None = None,
    *,
    runner: CommandRunner | None = None,
) -> TraceMetadata:
    run = runner or _run_command
    commit_sha = _run_git(run, ["git", "rev-parse", "HEAD"], repo_path).strip()
    repo_root = _run_git(run, ["git", "rev-parse", "--show-toplevel"], repo_path).strip()
    branch_output = _run_git(run, ["git", "branch", "--show-current"], repo_path).strip()
    status_output = _run_git(run, ["git", "status", "--porcelain"], repo_path)

    return TraceMetadata(
        commit_sha=commit_sha,
        repo=Path(repo_root).name if repo_root else None,
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
    anchors = list(anchor_commit_shas)
    for anchor in anchors:
        _validate_commit_string(anchor, "anchor commit")

    run = runner or _run_ancestry_command
    relations: list[tuple[str, bool]] = []
    for anchor in sorted(set(anchors)):
        args = ["git", "merge-base", "--is-ancestor", anchor, current_commit_sha]
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
    completed = subprocess.run(
        args,
        cwd=cwd,
        check=False,
        capture_output=True,
        text=True,
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
    completed = subprocess.run(args, cwd=cwd, check=True, capture_output=True, text=True)
    return completed.stdout


def _run_git(run: CommandRunner, args: list[str], repo_path: str | None) -> str:
    try:
        return run(args, repo_path)
    except Exception as exc:  # pragma: no cover - exact exception type depends on runner/subprocess
        command = " ".join(args)
        location = repo_path or "."
        detail = _command_error_detail(exc)
        raise TraceMetadataCaptureError(
            f"failed to capture git metadata with `{command}` in {location}: {detail}"
        ) from exc


def _command_error_detail(exc: Exception) -> str:
    if isinstance(exc, subprocess.CalledProcessError):
        stderr = (exc.stderr or "").strip()
        if stderr:
            return stderr
    return str(exc)
