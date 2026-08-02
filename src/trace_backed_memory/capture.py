from __future__ import annotations

import os
import signal
import subprocess
import time
from collections.abc import Callable, Iterable
from dataclasses import dataclass
import hashlib
from pathlib import Path
from threading import Event, Thread
from typing import TYPE_CHECKING, BinaryIO

from .models import CommitAncestryEvidence, TraceMetadata
from .policy import METADATA_VALUE_MAX_CHARS

if TYPE_CHECKING:
    from .event_v1 import CanonicalEvent, EventArtifactRef
    from .git_observation_v1 import (
        GitObjectFormat,
        GitObservationDraft,
        GitObservationProvenance,
    )
    from .ledger_port_v1 import EventLedgerPort, LedgerAppendReceipt

CommandRunner = Callable[[list[str], str | None], str]
AncestryRunner = Callable[[list[str], str | None], int]
GitObservationRunner = Callable[
    [list[str], str | None, int], "GitObservationCommandResult"
]
GitDiffArtifactWriter = Callable[[bytes], "EventArtifactRef"]
COMMIT_ANCESTRY_MAX_ANCHORS = 1_000
GIT_CAPTURE_TIMEOUT_SECONDS = 30.0
GIT_CAPTURE_OUTPUT_MAX_BYTES = 64 * 1024
GIT_DIFF_CAPTURE_MAX_BYTES = 64 * 1024 * 1024
_GIT_CAPTURE_READ_CHUNK_BYTES = 8 * 1024
_GIT_CAPTURE_POLL_SECONDS = 0.05
_GIT_CAPTURE_REAP_TIMEOUT_SECONDS = 1.0


class TraceMetadataCaptureError(RuntimeError):
    """Raised when git metadata cannot be captured for a trace."""


class CommitAncestryCaptureError(RuntimeError):
    """Raised when Git ancestry evidence cannot be captured."""


class GitObservationCaptureError(RuntimeError):
    """Raised when the explicit Git observation runtime cannot capture evidence."""


@dataclass(frozen=True)
class GitObservationCommandResult:
    returncode: int
    stdout: bytes

    def __post_init__(self) -> None:
        if type(self.returncode) is not int or not 0 <= self.returncode <= 255:
            raise ValueError("returncode must be a bounded non-negative integer")
        if type(self.stdout) is not bytes:
            raise ValueError("stdout must be exact bytes")


@dataclass(frozen=True)
class TraceMetadataCaptureResult:
    metadata: TraceMetadata
    checkout_id: str
    object_format: GitObjectFormat
    provenance: GitObservationProvenance
    observations: tuple[GitObservationDraft, ...]

    def __post_init__(self) -> None:
        from .git_observation_v1 import GitObservationDraft, GitObservationProvenance

        if type(self.metadata) is not TraceMetadata:
            raise ValueError("metadata must be exactly TraceMetadata")
        if type(self.checkout_id) is not str or not self.checkout_id.startswith(
            "checkout_"
        ):
            raise ValueError("checkout_id must be a derived checkout identifier")
        if self.object_format not in {"sha1", "sha256"}:
            raise ValueError("object_format must be sha1 or sha256")
        if type(self.provenance) is not GitObservationProvenance:
            raise ValueError("provenance must be exactly GitObservationProvenance")
        if (
            type(self.observations) is not tuple
            or len(self.observations) != 5
            or any(type(item) is not GitObservationDraft for item in self.observations)
        ):
            raise ValueError("metadata capture must produce five observation drafts")


@dataclass(frozen=True)
class CommitAncestryCaptureResult:
    evidence: CommitAncestryEvidence | None
    observations: tuple[GitObservationDraft, ...]

    def __post_init__(self) -> None:
        from .git_observation_v1 import GitObservationDraft

        if self.evidence is not None and type(self.evidence) is not CommitAncestryEvidence:
            raise ValueError("evidence must be CommitAncestryEvidence or null")
        if (
            type(self.observations) is not tuple
            or len(self.observations) != 2
            or any(type(item) is not GitObservationDraft for item in self.observations)
        ):
            raise ValueError("ancestry capture must produce two observation drafts")


@dataclass(frozen=True)
class GitObservationRuntimeResult:
    metadata: TraceMetadata
    commit_ancestry: CommitAncestryEvidence | None
    receipt: LedgerAppendReceipt

    def __post_init__(self) -> None:
        from .ledger_port_v1 import LedgerAppendReceipt

        if type(self.metadata) is not TraceMetadata:
            raise ValueError("metadata must be exactly TraceMetadata")
        if self.commit_ancestry is not None and type(
            self.commit_ancestry
        ) is not CommitAncestryEvidence:
            raise ValueError("commit_ancestry must be CommitAncestryEvidence or null")
        if type(self.receipt) is not LedgerAppendReceipt:
            raise ValueError("receipt must be exactly LedgerAppendReceipt")


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
    metadata, _ = _capture_trace_metadata_compat(repo_path, runner=runner)
    return metadata


def _capture_trace_metadata_compat(
    repo_path: str | None,
    *,
    runner: CommandRunner | None,
) -> tuple[TraceMetadata, str]:
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

    return (
        TraceMetadata(
            commit_sha=commit_sha,
            repo=repo_name,
            branch=branch_output or None,
            dirty=bool(status_output.strip()),
        ),
        repo_root,
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


def capture_trace_metadata_detailed(
    repo_path: str | None = None,
    *,
    runner: CommandRunner | None = None,
    observation_runner: GitObservationRunner | None = None,
    diff_artifact_writer: GitDiffArtifactWriter,
    observed_at: str,
    starting_sequence: int = 1,
) -> TraceMetadataCaptureResult:
    """Capture compatibility metadata plus five event-ready Git observations.

    The compatibility projection still executes the original four commands in
    their original order. Detailed capture is explicit: exact diff bytes are
    handed to a trusted Artifact writer and only its protected descriptor is
    admitted to an event draft.
    """

    from .event_v1 import EventArtifactRef
    from .git_observation_v1 import (
        GIT_OBSERVATION_DEFAULT_ALGORITHM_ID,
        GIT_OBSERVATION_DEFAULT_ALGORITHM_VERSION,
        GIT_OBSERVATION_DEFAULT_RUNNER_ID,
        GIT_OBSERVATION_DEFAULT_RUNNER_VERSION,
        GitCheckoutObservation,
        GitCommitObservation,
        GitDiffObservation,
        GitObservationDraft,
        GitObservationProvenance,
        GitRefObservation,
        GitShallowStateObservation,
    )

    if not callable(diff_artifact_writer):
        raise ValueError("diff_artifact_writer must be callable")
    compatibility_runner = runner or _run_no_lazy_metadata_command
    metadata, repo_root = _capture_trace_metadata_compat(
        repo_path,
        runner=compatibility_runner,
    )
    run = observation_runner or _run_observation_command
    git_version = _observation_text(
        run,
        ["git", "--version"],
        repo_path,
        output_name="Git version",
    )
    object_format_text = _observation_text(
        run,
        ["git", "rev-parse", "--show-object-format"],
        repo_path,
        output_name="object format",
    )
    if object_format_text not in {"sha1", "sha256"}:
        raise GitObservationCaptureError(
            "Git object format must be exactly sha1 or sha256"
        )
    object_format = object_format_text
    commit_result = _capture_observation_result(
        run,
        [
            "git",
            "show",
            "-s",
            "--no-show-signature",
            "--format=%H%x00%T%x00%P",
            "--no-abbrev-commit",
            "HEAD",
        ],
        repo_path,
        allowed_returncodes={0},
    )
    commit_parts = commit_result.stdout.rstrip(b"\r\n").split(b"\x00")
    if len(commit_parts) != 3:
        raise GitObservationCaptureError(
            "Git commit observation did not return commit, tree, and parents"
        )
    commit_oid = _decode_observation_text(commit_parts[0], "commit oid")
    tree_oid = _decode_observation_text(commit_parts[1], "tree oid")
    parent_text = _decode_observation_text(
        commit_parts[2], "parent oids", allow_blank=True
    )
    parent_oids = () if not parent_text else tuple(parent_text.split(" "))
    if metadata.commit_sha != commit_oid:
        raise GitObservationCaptureError(
            "compatibility metadata and detailed commit observation disagree"
        )
    ref_result = _capture_observation_result(
        run,
        ["git", "symbolic-ref", "--quiet", "HEAD"],
        repo_path,
        allowed_returncodes={0, 1},
    )
    if ref_result.returncode == 0:
        ref_name = _decode_observation_text(
            ref_result.stdout.strip(), "symbolic ref"
        )
        detached = False
    else:
        if ref_result.stdout.strip():
            raise GitObservationCaptureError(
                "detached ref observation returned unexpected output"
            )
        ref_name = None
        detached = True
    if (
        not detached
        and ref_name is not None
        and metadata.branch != ref_name.removeprefix("refs/heads/")
    ):
        raise GitObservationCaptureError(
            "compatibility branch and detailed ref observation disagree"
        )
    if detached and metadata.branch is not None:
        raise GitObservationCaptureError(
            "compatibility branch and detached ref observation disagree"
        )
    shallow_text = _observation_text(
        run,
        ["git", "rev-parse", "--is-shallow-repository"],
        repo_path,
        output_name="shallow state",
    )
    if shallow_text not in {"true", "false"}:
        raise GitObservationCaptureError(
            "Git shallow state must be exactly true or false"
        )
    diff_result = _capture_observation_result(
        run,
        [
            "git",
            "diff",
            "--binary",
            "--full-index",
            "--no-ext-diff",
            "--no-textconv",
            "HEAD",
            "--",
        ],
        repo_path,
        allowed_returncodes={0},
        stdout_max_bytes=GIT_DIFF_CAPTURE_MAX_BYTES,
    )
    try:
        diff_artifact = diff_artifact_writer(diff_result.stdout)
    except Exception as exc:
        raise GitObservationCaptureError(
            "trusted diff Artifact writer failed"
        ) from exc
    if type(diff_artifact) is not EventArtifactRef:
        raise GitObservationCaptureError(
            "trusted diff Artifact writer returned an invalid descriptor"
        )
    diff_sha256 = "sha256:" + hashlib.sha256(diff_result.stdout).hexdigest()
    provenance = GitObservationProvenance(
        runner_id=GIT_OBSERVATION_DEFAULT_RUNNER_ID,
        runner_version=GIT_OBSERVATION_DEFAULT_RUNNER_VERSION,
        algorithm_id=GIT_OBSERVATION_DEFAULT_ALGORITHM_ID,
        algorithm_version=GIT_OBSERVATION_DEFAULT_ALGORITHM_VERSION,
        git_version=git_version,
    )
    root_identity = os.path.normcase(os.path.abspath(repo_root))
    root_digest = hashlib.sha256(root_identity.encode("utf-8")).hexdigest()
    checkout_id = "checkout_" + root_digest
    root_sha256 = "sha256:" + root_digest
    observations = (
        GitObservationDraft(
            checkout_id=checkout_id,
            sequence=starting_sequence,
            observed_at=observed_at,
            provenance=provenance,
            observation=GitCheckoutObservation(
                root_sha256=root_sha256,
                repository_name=metadata.repo,
                object_format=object_format,
                head_oid=commit_oid,
                dirty=metadata.dirty,
            ),
        ),
        GitObservationDraft(
            checkout_id=checkout_id,
            sequence=starting_sequence + 1,
            observed_at=observed_at,
            provenance=provenance,
            observation=GitRefObservation(
                object_format=object_format,
                target_oid=commit_oid,
                ref_name=ref_name,
                detached=detached,
            ),
        ),
        GitObservationDraft(
            checkout_id=checkout_id,
            sequence=starting_sequence + 2,
            observed_at=observed_at,
            provenance=provenance,
            observation=GitCommitObservation(
                object_format=object_format,
                commit_oid=commit_oid,
                tree_oid=tree_oid,
                parent_oids=parent_oids,
            ),
        ),
        GitObservationDraft(
            checkout_id=checkout_id,
            sequence=starting_sequence + 3,
            observed_at=observed_at,
            provenance=provenance,
            observation=GitDiffObservation(
                object_format=object_format,
                base_oid=commit_oid,
                target="index_and_worktree",
                content_sha256=diff_sha256,
                size_bytes=len(diff_result.stdout),
                artifact_id=diff_artifact.artifact_id,
            ),
            classification=diff_artifact.classification,
            artifact_refs=(diff_artifact,),
        ),
        GitObservationDraft(
            checkout_id=checkout_id,
            sequence=starting_sequence + 4,
            observed_at=observed_at,
            provenance=provenance,
            observation=GitShallowStateObservation(
                state="shallow" if shallow_text == "true" else "full"
            ),
        ),
    )
    return TraceMetadataCaptureResult(
        metadata=metadata,
        checkout_id=checkout_id,
        object_format=object_format,
        provenance=provenance,
        observations=observations,
    )


def capture_commit_ancestry_detailed(
    current_commit_sha: str,
    anchor_commit_shas: Iterable[str],
    repo_path: str | None = None,
    *,
    runner: AncestryRunner | None = None,
    observation_runner: GitObservationRunner | None = None,
    checkout_id: str,
    object_format: GitObjectFormat,
    provenance: GitObservationProvenance,
    observed_at: str,
    starting_sequence: int,
) -> CommitAncestryCaptureResult:
    """Capture object availability before making an ancestry claim.

    Missing or indeterminate objects produce ``unknown`` relations and no
    compatibility evidence. They are never coerced to ``not_ancestor``.
    """

    from .git_observation_v1 import (
        GitAncestryObservation,
        GitAncestryRelation,
        GitObjectAvailability,
        GitObjectAvailabilityObservation,
        GitObservationDraft,
    )

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
    canonical_anchors = tuple(sorted(set(anchors)))
    object_oids = tuple(sorted(set((current_commit_sha, *canonical_anchors))))
    GitObjectAvailabilityObservation(
        object_format=object_format,
        objects=tuple(
            GitObjectAvailability(object_oid=object_oid, status="unknown")
            for object_oid in object_oids
        ),
    )
    run = observation_runner or _run_observation_command
    availability: list[GitObjectAvailability] = []
    for object_oid in object_oids:
        result = _capture_observation_result(
            run,
            [
                "git",
                "rev-parse",
                "--verify",
                "--quiet",
                "--end-of-options",
                f"{object_oid}^{{commit}}",
            ],
            repo_path,
            allowed_returncodes=set(range(256)),
        )
        if result.returncode == 0:
            status = "present"
        elif result.returncode == 1:
            status = "missing"
        else:
            status = "unknown"
        availability.append(
            GitObjectAvailability(object_oid=object_oid, status=status)
        )
    all_present = all(item.status == "present" for item in availability)
    evidence: CommitAncestryEvidence | None = None
    relations: tuple[GitAncestryRelation, ...]
    if all_present:
        try:
            evidence = capture_commit_ancestry(
                current_commit_sha,
                canonical_anchors,
                repo_path,
                runner=runner,
            )
        except CommitAncestryCaptureError:
            relations = tuple(
                GitAncestryRelation(anchor_oid=anchor, status="unknown")
                for anchor in canonical_anchors
            )
        else:
            relations = tuple(
                GitAncestryRelation(
                    anchor_oid=anchor,
                    status="ancestor" if is_ancestor else "not_ancestor",
                )
                for anchor, is_ancestor in evidence.commit_relations
            )
    else:
        relations = tuple(
            GitAncestryRelation(anchor_oid=anchor, status="unknown")
            for anchor in canonical_anchors
        )
    observations = (
        GitObservationDraft(
            checkout_id=checkout_id,
            sequence=starting_sequence,
            observed_at=observed_at,
            provenance=provenance,
            observation=GitObjectAvailabilityObservation(
                object_format=object_format,
                objects=tuple(availability),
            ),
        ),
        GitObservationDraft(
            checkout_id=checkout_id,
            sequence=starting_sequence + 1,
            observed_at=observed_at,
            provenance=provenance,
            observation=GitAncestryObservation(
                object_format=object_format,
                current_oid=current_commit_sha,
                relations=relations,
            ),
        ),
    )
    return CommitAncestryCaptureResult(
        evidence=evidence,
        observations=observations,
    )


def capture_and_append_git_observations(
    ledger: EventLedgerPort,
    anchor_commit_shas: Iterable[str],
    repo_path: str | None = None,
    *,
    metadata_runner: CommandRunner | None = None,
    ancestry_runner: AncestryRunner | None = None,
    observation_runner: GitObservationRunner | None = None,
    diff_artifact_writer: GitDiffArtifactWriter,
    expected_stream_version: int,
    next_global_position: int,
    previous_event: CanonicalEvent | None,
    observed_at: str,
    recorded_at: str,
) -> GitObservationRuntimeResult:
    """Capture all seven Git points and atomically append one event batch."""

    from .git_observation_v1 import append_git_observation_batch

    metadata_result = capture_trace_metadata_detailed(
        repo_path,
        runner=metadata_runner,
        observation_runner=observation_runner,
        diff_artifact_writer=diff_artifact_writer,
        observed_at=observed_at,
        starting_sequence=expected_stream_version + 1,
    )
    ancestry_result = capture_commit_ancestry_detailed(
        metadata_result.metadata.commit_sha,
        anchor_commit_shas,
        repo_path,
        runner=ancestry_runner,
        observation_runner=observation_runner,
        checkout_id=metadata_result.checkout_id,
        object_format=metadata_result.object_format,
        provenance=metadata_result.provenance,
        observed_at=observed_at,
        starting_sequence=expected_stream_version + 6,
    )
    receipt = append_git_observation_batch(
        ledger,
        metadata_result.observations + ancestry_result.observations,
        expected_stream_version=expected_stream_version,
        next_global_position=next_global_position,
        previous_event=previous_event,
        recorded_at=recorded_at,
    )
    return GitObservationRuntimeResult(
        metadata=metadata_result.metadata,
        commit_ancestry=ancestry_result.evidence,
        receipt=receipt,
    )


def _run_observation_command(
    args: list[str], cwd: str | None, stdout_max_bytes: int
) -> GitObservationCommandResult:
    env = os.environ.copy()
    env["GIT_NO_LAZY_FETCH"] = "1"
    completed = _run_bounded_process_bytes(
        args,
        cwd=cwd,
        env=env,
        stdout_max_bytes=stdout_max_bytes,
    )
    return GitObservationCommandResult(
        returncode=completed.returncode,
        stdout=completed.stdout,
    )


def _run_no_lazy_metadata_command(
    args: list[str], cwd: str | None = None
) -> str:
    env = os.environ.copy()
    env["GIT_NO_LAZY_FETCH"] = "1"
    if args == ["git", "status", "--porcelain"]:
        completed = _run_bounded_process(
            args,
            cwd=cwd,
            env=env,
            stdout_max_bytes=1,
            fail_on_stdout_overflow=False,
        )
        completed.check_returncode()
        return "dirty" if completed.stdout else ""
    completed = _run_bounded_process(args, cwd=cwd, env=env)
    completed.check_returncode()
    return completed.stdout


def _capture_observation_result(
    run: GitObservationRunner,
    args: list[str],
    repo_path: str | None,
    *,
    allowed_returncodes: set[int],
    stdout_max_bytes: int = GIT_CAPTURE_OUTPUT_MAX_BYTES,
) -> GitObservationCommandResult:
    command = " ".join(args)
    location = repo_path or "."
    try:
        result = run(args, repo_path, stdout_max_bytes)
    except Exception as exc:
        raise GitObservationCaptureError(
            f"failed to capture Git observation with `{command}` in {location}: "
            f"{_command_error_detail(exc)}"
        ) from exc
    if type(result) is not GitObservationCommandResult:
        raise GitObservationCaptureError(
            f"failed to capture Git observation with `{command}` in {location}: "
            "runner returned an invalid result"
        )
    if result.returncode not in allowed_returncodes:
        raise GitObservationCaptureError(
            f"failed to capture Git observation with `{command}` in {location}: "
            f"Git returned exit code {result.returncode}"
        )
    if len(result.stdout) > stdout_max_bytes:
        raise GitObservationCaptureError(
            f"failed to capture Git observation with `{command}` in {location}: "
            "runner returned oversized output"
        )
    return result


def _observation_text(
    run: GitObservationRunner,
    args: list[str],
    repo_path: str | None,
    *,
    output_name: str,
) -> str:
    result = _capture_observation_result(
        run,
        args,
        repo_path,
        allowed_returncodes={0},
    )
    return _decode_observation_text(result.stdout.strip(), output_name)


def _decode_observation_text(
    value: bytes, output_name: str, *, allow_blank: bool = False
) -> str:
    try:
        text = value.decode("utf-8", errors="strict")
    except UnicodeDecodeError as exc:
        raise GitObservationCaptureError(
            f"Git {output_name} output is not valid UTF-8"
        ) from exc
    if not allow_blank and not text:
        raise GitObservationCaptureError(f"Git {output_name} output is blank")
    if any(ord(character) < 32 for character in text):
        raise GitObservationCaptureError(
            f"Git {output_name} output contains control characters"
        )
    return text


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
    completed = _run_bounded_process_bytes(
        args,
        cwd=cwd,
        env=env,
        stdout_max_bytes=stdout_max_bytes,
        fail_on_stdout_overflow=fail_on_stdout_overflow,
    )
    return subprocess.CompletedProcess(
        completed.args,
        completed.returncode,
        stdout=completed.stdout.decode("utf-8", errors="replace"),
        stderr=completed.stderr.decode("utf-8", errors="replace"),
    )


def _run_bounded_process_bytes(
    args: list[str],
    *,
    cwd: str | None = None,
    env: dict[str, str] | None = None,
    stdout_max_bytes: int = GIT_CAPTURE_OUTPUT_MAX_BYTES,
    fail_on_stdout_overflow: bool = True,
) -> subprocess.CompletedProcess[bytes]:
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

        stdout = bytes(stdout_reader.data)
        stderr = bytes(stderr_reader.data)
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
