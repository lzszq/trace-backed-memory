from trace_backed_memory import TraceMetadataCaptureError, capture_trace_metadata


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
