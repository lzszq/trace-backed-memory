from __future__ import annotations

import subprocess
from collections.abc import Callable
from pathlib import Path

from .models import TraceMetadata

CommandRunner = Callable[[list[str], str | None], str]


class TraceMetadataCaptureError(RuntimeError):
    """Raised when git metadata cannot be captured for a trace."""


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
