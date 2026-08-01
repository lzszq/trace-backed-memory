from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
import hashlib
import json
import re
from threading import RLock
from types import MappingProxyType
from typing import Literal, NoReturn, Protocol

from .contracts_v3 import V3ContractError
from .reducer import (
    REDUCER_PROTOCOL_VERSION,
    ReducerDescriptor,
    canonical_projection_state,
    parse_reducer_descriptor,
    projection_state_sha256,
)


PROJECTION_CHECKPOINT_PROTOCOL_VERSION = "tbm.projection-checkpoint.v1"
PROJECTION_ACTIVATION_PROTOCOL_VERSION = "tbm.projection-activation.v1"
PROJECTION_MAX_CHECKPOINTS_PER_LIST = 10_000
PROJECTION_MAX_ACTIVATIONS_PER_LIST = 10_000

ProjectionActivationOperation = Literal["activate", "rollback"]

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_TIMESTAMP_RE = re.compile(
    r"^[0-9]{4}-[0-9]{2}-[0-9]{2}T[0-9]{2}:[0-9]{2}:[0-9]{2}"
    r"\.[0-9]{6}Z$"
)


class ProjectionCheckpointError(V3ContractError):
    """Stable projection checkpoint, head, or persistence failure."""


class ProjectionCheckpointConflictError(ProjectionCheckpointError):
    """A checkpoint or head CAS conflicts with retained projection state."""


class ProjectionCheckpointNotFoundError(ProjectionCheckpointError):
    """A requested checkpoint or active projection head is absent."""


@dataclass(frozen=True)
class ProjectionCheckpoint:
    reducer_descriptor: ReducerDescriptor
    partition_sha256: str
    global_position: int
    event_high_watermark: int
    state: Mapping[str, object]
    reducer_registry_sha256: str
    event_registry_sha256: str | None
    created_at: str
    owner: str
    rebuild_generation: int
    state_sha256: str = field(init=False)
    build_id: str = field(init=False)
    checkpoint_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        if type(self.reducer_descriptor) is not ReducerDescriptor:
            _fail(
                "TBM_PROJECTION_CHECKPOINT_INVALID",
                "reducer_descriptor must be exactly ReducerDescriptor",
            )
        _digest(self.partition_sha256, "partition_sha256")
        _position(self.global_position, "global_position")
        _position(self.event_high_watermark, "event_high_watermark")
        if self.global_position > self.event_high_watermark:
            _fail(
                "TBM_PROJECTION_CHECKPOINT_INVALID",
                "global_position exceeds event_high_watermark",
            )
        _digest(self.reducer_registry_sha256, "reducer_registry_sha256")
        if self.event_registry_sha256 is not None:
            _digest(self.event_registry_sha256, "event_registry_sha256")
        _timestamp(self.created_at, "created_at")
        _identifier(self.owner, "owner")
        if type(self.rebuild_generation) is not int or not (
            1 <= self.rebuild_generation <= 2**63 - 1
        ):
            _fail(
                "TBM_PROJECTION_CHECKPOINT_INVALID",
                "rebuild_generation is invalid",
            )
        state = canonical_projection_state(self.state)
        state_sha256 = projection_state_sha256(
            self.reducer_descriptor.output_projection,
            self.reducer_descriptor.output_schema_version,
            state,
        )
        semantic = {
            "protocol_version": PROJECTION_CHECKPOINT_PROTOCOL_VERSION,
            "reducer_protocol_version": REDUCER_PROTOCOL_VERSION,
            "reducer_descriptor_sha256": (
                self.reducer_descriptor.descriptor_sha256
            ),
            "partition_sha256": self.partition_sha256,
            "global_position": self.global_position,
            "state_sha256": state_sha256,
            "reducer_registry_sha256": self.reducer_registry_sha256,
            "event_registry_sha256": self.event_registry_sha256,
        }
        build_id = _domain_sha256(b"tbm.projection-build.v1\x00", semantic)
        retained = {
            **semantic,
            "event_high_watermark": self.event_high_watermark,
            "build_id": build_id,
            "created_at": self.created_at,
            "owner": self.owner,
            "rebuild_generation": self.rebuild_generation,
            "state": state,
        }
        object.__setattr__(self, "state", _freeze_json(state))
        object.__setattr__(self, "state_sha256", state_sha256)
        object.__setattr__(self, "build_id", build_id)
        object.__setattr__(
            self,
            "checkpoint_sha256",
            _domain_sha256(b"tbm.projection-checkpoint.v1\x00", retained),
        )

    @property
    def projection_name(self) -> str:
        return self.reducer_descriptor.output_projection

    @property
    def reducer_id(self) -> str:
        return self.reducer_descriptor.reducer_id

    @property
    def reducer_version(self) -> int:
        return self.reducer_descriptor.reducer_version

    def compatible_with(
        self,
        descriptor: ReducerDescriptor,
        *,
        reducer_registry_sha256: str,
        event_registry_sha256: str | None,
    ) -> bool:
        return (
            self.reducer_descriptor == descriptor
            and self.reducer_registry_sha256 == reducer_registry_sha256
            and self.event_registry_sha256 == event_registry_sha256
        )

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": PROJECTION_CHECKPOINT_PROTOCOL_VERSION,
            "checkpoint_sha256": self.checkpoint_sha256,
            "build_id": self.build_id,
            "reducer_descriptor": self.reducer_descriptor.to_dict(),
            "partition_sha256": self.partition_sha256,
            "global_position": self.global_position,
            "event_high_watermark": self.event_high_watermark,
            "state_sha256": self.state_sha256,
            "state": _thaw_json(self.state),
            "reducer_registry_sha256": self.reducer_registry_sha256,
            "event_registry_sha256": self.event_registry_sha256,
            "created_at": self.created_at,
            "owner": self.owner,
            "rebuild_generation": self.rebuild_generation,
        }


@dataclass(frozen=True)
class ProjectionBlocked:
    event_id: str
    event_sha256: str
    payload_sha256: str
    reducer_id: str
    reducer_version: int
    projection_name: str
    last_good_position: int
    error_code: str
    retryable: bool
    required_migration: str | None

    def __post_init__(self) -> None:
        _identifier(self.event_id, "event_id")
        _digest(self.event_sha256, "event_sha256")
        _digest(self.payload_sha256, "payload_sha256")
        _identifier(self.reducer_id, "reducer_id")
        _position(self.reducer_version, "reducer_version", allow_zero=False)
        _identifier(self.projection_name, "projection_name")
        _position(self.last_good_position, "last_good_position")
        _identifier(self.error_code, "error_code")
        if type(self.retryable) is not bool:
            _fail(
                "TBM_PROJECTION_BLOCKED_INVALID",
                "retryable must be a boolean",
            )
        if self.required_migration is not None:
            _identifier(self.required_migration, "required_migration")

    def to_dict(self) -> dict[str, object]:
        return {
            "event_id": self.event_id,
            "event_sha256": self.event_sha256,
            "payload_sha256": self.payload_sha256,
            "reducer_id": self.reducer_id,
            "reducer_version": self.reducer_version,
            "projection_name": self.projection_name,
            "last_good_position": self.last_good_position,
            "error_code": self.error_code,
            "retryable": self.retryable,
            "required_migration": self.required_migration,
        }


@dataclass(frozen=True)
class ProjectionActivation:
    projection_name: str
    partition_sha256: str
    head_version: int
    target_build_id: str
    previous_build_id: str | None
    operation: ProjectionActivationOperation
    owner: str
    created_at: str
    activation_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.projection_name, "projection_name")
        _digest(self.partition_sha256, "partition_sha256")
        _position(self.head_version, "head_version", allow_zero=False)
        _digest(self.target_build_id, "target_build_id")
        if self.previous_build_id is not None:
            _digest(self.previous_build_id, "previous_build_id")
        if self.operation not in {"activate", "rollback"}:
            _fail(
                "TBM_PROJECTION_ACTIVATION_INVALID",
                "projection activation operation is invalid",
            )
        _identifier(self.owner, "owner")
        _timestamp(self.created_at, "created_at")
        object.__setattr__(
            self,
            "activation_sha256",
            _domain_sha256(
                b"tbm.projection-activation.v1\x00",
                self.to_dict(include_digest=False),
            ),
        )

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "protocol_version": PROJECTION_ACTIVATION_PROTOCOL_VERSION,
            "projection_name": self.projection_name,
            "partition_sha256": self.partition_sha256,
            "head_version": self.head_version,
            "target_build_id": self.target_build_id,
            "previous_build_id": self.previous_build_id,
            "operation": self.operation,
            "owner": self.owner,
            "created_at": self.created_at,
        }
        if include_digest:
            value["activation_sha256"] = self.activation_sha256
        return value


class ProjectionCheckpointStore(Protocol):
    def save_checkpoint(
        self,
        checkpoint: ProjectionCheckpoint,
    ) -> ProjectionCheckpoint: ...

    def load_checkpoint(
        self,
        build_id: str,
    ) -> ProjectionCheckpoint: ...

    def load_latest_checkpoint(
        self,
        projection_name: str,
        reducer_version: int,
        partition_sha256: str,
    ) -> ProjectionCheckpoint | None: ...

    def list_checkpoints(
        self,
        projection_name: str | None = None,
        partition_sha256: str | None = None,
    ) -> tuple[ProjectionCheckpoint, ...]: ...

    def append_activation(
        self,
        activation: ProjectionActivation,
        *,
        expected_head_version: int,
        expected_current_build_id: str | None,
    ) -> ProjectionActivation: ...

    def current_activation(
        self,
        projection_name: str,
        partition_sha256: str,
    ) -> ProjectionActivation | None: ...

    def activation_history(
        self,
        projection_name: str,
        partition_sha256: str,
    ) -> tuple[ProjectionActivation, ...]: ...


class InMemoryProjectionCheckpointStore:
    """Thread-safe reference store used by the storage-neutral runtime."""

    def __init__(self) -> None:
        self._checkpoints: dict[str, ProjectionCheckpoint] = {}
        self._checkpoint_keys: dict[
            tuple[str, int, str, int],
            str,
        ] = {}
        self._activations: dict[
            tuple[str, str],
            list[ProjectionActivation],
        ] = {}
        self._lock = RLock()

    def save_checkpoint(
        self,
        checkpoint: ProjectionCheckpoint,
    ) -> ProjectionCheckpoint:
        if type(checkpoint) is not ProjectionCheckpoint:
            _fail(
                "TBM_PROJECTION_CHECKPOINT_INVALID",
                "checkpoint must be exactly ProjectionCheckpoint",
            )
        key = (
            checkpoint.projection_name,
            checkpoint.reducer_version,
            checkpoint.partition_sha256,
            checkpoint.global_position,
        )
        with self._lock:
            retained_build = self._checkpoint_keys.get(key)
            if retained_build is not None:
                retained = self._checkpoints[retained_build]
                if retained.build_id != checkpoint.build_id:
                    _conflict(
                        "TBM_PROJECTION_CHECKPOINT_CONFLICT",
                        "checkpoint position retained a different projection digest",
                    )
                return retained
            retained = self._checkpoints.get(checkpoint.build_id)
            if retained is not None:
                return retained
            self._checkpoints[checkpoint.build_id] = checkpoint
            self._checkpoint_keys[key] = checkpoint.build_id
            return checkpoint

    def load_checkpoint(self, build_id: str) -> ProjectionCheckpoint:
        _digest(build_id, "build_id")
        with self._lock:
            checkpoint = self._checkpoints.get(build_id)
            if checkpoint is None:
                _not_found("projection checkpoint is not retained")
            return checkpoint

    def load_latest_checkpoint(
        self,
        projection_name: str,
        reducer_version: int,
        partition_sha256: str,
    ) -> ProjectionCheckpoint | None:
        _identifier(projection_name, "projection_name")
        _position(reducer_version, "reducer_version", allow_zero=False)
        _digest(partition_sha256, "partition_sha256")
        with self._lock:
            matches = [
                checkpoint
                for checkpoint in self._checkpoints.values()
                if checkpoint.projection_name == projection_name
                and checkpoint.reducer_version == reducer_version
                and checkpoint.partition_sha256 == partition_sha256
            ]
            return max(matches, key=lambda item: item.global_position, default=None)

    def list_checkpoints(
        self,
        projection_name: str | None = None,
        partition_sha256: str | None = None,
    ) -> tuple[ProjectionCheckpoint, ...]:
        if projection_name is not None:
            _identifier(projection_name, "projection_name")
        if partition_sha256 is not None:
            _digest(partition_sha256, "partition_sha256")
        with self._lock:
            matches = [
                checkpoint
                for checkpoint in self._checkpoints.values()
                if (
                    projection_name is None
                    or checkpoint.projection_name == projection_name
                )
                and (
                    partition_sha256 is None
                    or checkpoint.partition_sha256 == partition_sha256
                )
            ]
            if len(matches) > PROJECTION_MAX_CHECKPOINTS_PER_LIST:
                _fail(
                    "TBM_PROJECTION_LIST_LIMIT_EXCEEDED",
                    "projection checkpoint list exceeds the bounded limit",
                )
            return tuple(
                sorted(
                    matches,
                    key=lambda item: (
                        item.projection_name,
                        item.reducer_version,
                        item.partition_sha256,
                        item.global_position,
                    ),
                )
            )

    def append_activation(
        self,
        activation: ProjectionActivation,
        *,
        expected_head_version: int,
        expected_current_build_id: str | None,
    ) -> ProjectionActivation:
        if type(activation) is not ProjectionActivation:
            _fail(
                "TBM_PROJECTION_ACTIVATION_INVALID",
                "activation must be exactly ProjectionActivation",
            )
        _position(expected_head_version, "expected_head_version")
        if expected_current_build_id is not None:
            _digest(expected_current_build_id, "expected_current_build_id")
        key = (activation.projection_name, activation.partition_sha256)
        with self._lock:
            if activation.target_build_id not in self._checkpoints:
                _not_found("activation target checkpoint is not retained")
            history = self._activations.setdefault(key, [])
            current = history[-1] if history else None
            current_version = 0 if current is None else current.head_version
            current_build = None if current is None else current.target_build_id
            if (
                current_version != expected_head_version
                or current_build != expected_current_build_id
            ):
                _conflict(
                    "TBM_PROJECTION_HEAD_CONFLICT",
                    "projection head changed before activation",
                )
            if (
                activation.head_version != current_version + 1
                or activation.previous_build_id != current_build
            ):
                _fail(
                    "TBM_PROJECTION_ACTIVATION_INVALID",
                    "activation does not advance the retained projection head",
                )
            history.append(activation)
            return activation

    def current_activation(
        self,
        projection_name: str,
        partition_sha256: str,
    ) -> ProjectionActivation | None:
        _identifier(projection_name, "projection_name")
        _digest(partition_sha256, "partition_sha256")
        with self._lock:
            history = self._activations.get(
                (projection_name, partition_sha256),
                [],
            )
            return history[-1] if history else None

    def activation_history(
        self,
        projection_name: str,
        partition_sha256: str,
    ) -> tuple[ProjectionActivation, ...]:
        _identifier(projection_name, "projection_name")
        _digest(partition_sha256, "partition_sha256")
        with self._lock:
            history = tuple(
                self._activations.get(
                    (projection_name, partition_sha256),
                    [],
                )
            )
            if len(history) > PROJECTION_MAX_ACTIVATIONS_PER_LIST:
                _fail(
                    "TBM_PROJECTION_LIST_LIMIT_EXCEEDED",
                    "projection activation list exceeds the bounded limit",
                )
            return history


def parse_projection_checkpoint(value: object) -> ProjectionCheckpoint:
    if not isinstance(value, Mapping):
        _fail(
            "TBM_PROJECTION_CHECKPOINT_INVALID",
            "projection checkpoint must be an object",
        )
    expected = {
        "protocol_version",
        "checkpoint_sha256",
        "build_id",
        "reducer_descriptor",
        "partition_sha256",
        "global_position",
        "event_high_watermark",
        "state_sha256",
        "state",
        "reducer_registry_sha256",
        "event_registry_sha256",
        "created_at",
        "owner",
        "rebuild_generation",
    }
    if (
        set(value) != expected
        or value.get("protocol_version") != PROJECTION_CHECKPOINT_PROTOCOL_VERSION
    ):
        _fail(
            "TBM_PROJECTION_CHECKPOINT_INVALID",
            "projection checkpoint fields or protocol version are invalid",
        )
    checkpoint = ProjectionCheckpoint(
        reducer_descriptor=parse_reducer_descriptor(
            value.get("reducer_descriptor")
        ),
        partition_sha256=value.get("partition_sha256"),
        global_position=value.get("global_position"),
        event_high_watermark=value.get("event_high_watermark"),
        state=value.get("state"),
        reducer_registry_sha256=value.get("reducer_registry_sha256"),
        event_registry_sha256=value.get("event_registry_sha256"),
        created_at=value.get("created_at"),
        owner=value.get("owner"),
        rebuild_generation=value.get("rebuild_generation"),
    )
    if (
        value.get("state_sha256") != checkpoint.state_sha256
        or value.get("build_id") != checkpoint.build_id
        or value.get("checkpoint_sha256") != checkpoint.checkpoint_sha256
    ):
        _fail(
            "TBM_PROJECTION_CHECKPOINT_DIGEST_MISMATCH",
            "projection checkpoint digest does not match",
        )
    return checkpoint


def parse_projection_activation(value: object) -> ProjectionActivation:
    if not isinstance(value, Mapping):
        _fail(
            "TBM_PROJECTION_ACTIVATION_INVALID",
            "projection activation must be an object",
        )
    expected = {
        "protocol_version",
        "projection_name",
        "partition_sha256",
        "head_version",
        "target_build_id",
        "previous_build_id",
        "operation",
        "owner",
        "created_at",
        "activation_sha256",
    }
    if (
        set(value) != expected
        or value.get("protocol_version") != PROJECTION_ACTIVATION_PROTOCOL_VERSION
    ):
        _fail(
            "TBM_PROJECTION_ACTIVATION_INVALID",
            "projection activation fields or protocol version are invalid",
        )
    activation = ProjectionActivation(
        projection_name=value.get("projection_name"),
        partition_sha256=value.get("partition_sha256"),
        head_version=value.get("head_version"),
        target_build_id=value.get("target_build_id"),
        previous_build_id=value.get("previous_build_id"),
        operation=value.get("operation"),
        owner=value.get("owner"),
        created_at=value.get("created_at"),
    )
    if value.get("activation_sha256") != activation.activation_sha256:
        _fail(
            "TBM_PROJECTION_ACTIVATION_DIGEST_MISMATCH",
            "projection activation digest does not match",
        )
    return activation


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if isinstance(value, tuple):
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json_bytes(value: object) -> bytes:
    try:
        return json.dumps(
            value,
            ensure_ascii=False,
            allow_nan=False,
            sort_keys=True,
            separators=(",", ":"),
        ).encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise ProjectionCheckpointError(
            "TBM_PROJECTION_CHECKPOINT_INVALID",
            "projection checkpoint is not canonical JSON",
        ) from error


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_json_bytes(value)).hexdigest()


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _fail(
            "TBM_PROJECTION_CHECKPOINT_INVALID",
            f"{name} is invalid",
        )


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _fail(
            "TBM_PROJECTION_CHECKPOINT_INVALID",
            f"{name} is invalid",
        )


def _timestamp(value: object, name: str) -> None:
    if type(value) is not str or _TIMESTAMP_RE.fullmatch(value) is None:
        _fail(
            "TBM_PROJECTION_CHECKPOINT_INVALID",
            f"{name} is invalid",
        )


def _position(value: object, name: str, *, allow_zero: bool = True) -> None:
    minimum = 0 if allow_zero else 1
    if type(value) is not int or not minimum <= value <= 2**63 - 1:
        _fail(
            "TBM_PROJECTION_CHECKPOINT_INVALID",
            f"{name} is invalid",
        )


def _not_found(message: str) -> NoReturn:
    raise ProjectionCheckpointNotFoundError(
        "TBM_PROJECTION_NOT_FOUND",
        message,
    )


def _conflict(code: str, message: str) -> NoReturn:
    raise ProjectionCheckpointConflictError(code, message)


def _fail(code: str, message: str) -> NoReturn:
    raise ProjectionCheckpointError(code, message)


__all__ = [
    "PROJECTION_ACTIVATION_PROTOCOL_VERSION",
    "PROJECTION_CHECKPOINT_PROTOCOL_VERSION",
    "PROJECTION_MAX_ACTIVATIONS_PER_LIST",
    "PROJECTION_MAX_CHECKPOINTS_PER_LIST",
    "InMemoryProjectionCheckpointStore",
    "ProjectionActivation",
    "ProjectionActivationOperation",
    "ProjectionBlocked",
    "ProjectionCheckpoint",
    "ProjectionCheckpointConflictError",
    "ProjectionCheckpointError",
    "ProjectionCheckpointNotFoundError",
    "ProjectionCheckpointStore",
    "parse_projection_activation",
    "parse_projection_checkpoint",
]
