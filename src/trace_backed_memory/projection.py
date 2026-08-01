from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass, field
from datetime import UTC, datetime
import hashlib
import json
from typing import Literal, NoReturn

from .contracts_v3 import V3ContractError
from .event_registry_v1 import EventTypeRegistry
from .ledger_port_v1 import EventLedgerPort, LedgerAccessContext
from .projection_checkpoint import (
    ProjectionActivation,
    ProjectionBlocked,
    ProjectionCheckpoint,
    ProjectionCheckpointStore,
)
from .reducer import (
    ReducerDescriptor,
    ReducerEvent,
    canonical_projection_state,
    execute_reducer_step,
    initial_reducer_state,
)
from .reducer_registry import ReducerRegistry


PROJECTION_PROTOCOL_VERSION = "tbm.projection.v1"
PROJECTION_DEFAULT_PAGE_SIZE = 100
PROJECTION_MAX_PAGE_SIZE = 1000
PROJECTION_DEFAULT_CHECKPOINT_INTERVAL = 100
PROJECTION_MAX_DIFFERENCES = 256

ProjectionRebuildStatus = Literal["completed", "blocked"]
ProjectionDifferenceKind = Literal[
    "added",
    "changed",
    "removed",
    "type_changed",
]

_ALL_CLASSIFICATIONS = (
    "public",
    "internal",
    "confidential",
    "restricted",
)


class ProjectionRuntimeError(V3ContractError):
    """Stable projection rebuild, comparison, activation, or rollback failure."""


@dataclass(frozen=True)
class ProjectionRebuildResult:
    status: ProjectionRebuildStatus
    checkpoint: ProjectionCheckpoint
    blocked: ProjectionBlocked | None
    resumed_from_build_id: str | None
    processed_events: int

    def __post_init__(self) -> None:
        if self.status not in {"completed", "blocked"}:
            _fail(
                "TBM_PROJECTION_REBUILD_INVALID",
                "projection rebuild status is invalid",
            )
        if type(self.checkpoint) is not ProjectionCheckpoint:
            _fail(
                "TBM_PROJECTION_REBUILD_INVALID",
                "projection rebuild checkpoint is invalid",
            )
        if (self.status == "blocked") != (self.blocked is not None):
            _fail(
                "TBM_PROJECTION_REBUILD_INVALID",
                "projection blocked evidence does not match rebuild status",
            )
        if self.resumed_from_build_id is not None:
            _digest(self.resumed_from_build_id, "resumed_from_build_id")
        if type(self.processed_events) is not int or self.processed_events < 0:
            _fail(
                "TBM_PROJECTION_REBUILD_INVALID",
                "processed_events is invalid",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "protocol_version": PROJECTION_PROTOCOL_VERSION,
            "status": self.status,
            "checkpoint": self.checkpoint.to_dict(),
            "blocked": None if self.blocked is None else self.blocked.to_dict(),
            "resumed_from_build_id": self.resumed_from_build_id,
            "processed_events": self.processed_events,
        }


@dataclass(frozen=True)
class ProjectionDifference:
    path: str
    kind: ProjectionDifferenceKind
    active_value_sha256: str | None
    shadow_value_sha256: str | None

    def __post_init__(self) -> None:
        if type(self.path) is not str or not self.path or len(self.path) > 1024:
            _fail(
                "TBM_PROJECTION_COMPARISON_INVALID",
                "projection difference path is invalid",
            )
        if self.kind not in {"added", "changed", "removed", "type_changed"}:
            _fail(
                "TBM_PROJECTION_COMPARISON_INVALID",
                "projection difference kind is invalid",
            )
        if self.active_value_sha256 is not None:
            _digest(self.active_value_sha256, "active_value_sha256")
        if self.shadow_value_sha256 is not None:
            _digest(self.shadow_value_sha256, "shadow_value_sha256")

    def to_dict(self) -> dict[str, object]:
        return {
            "path": self.path,
            "kind": self.kind,
            "active_value_sha256": self.active_value_sha256,
            "shadow_value_sha256": self.shadow_value_sha256,
        }


@dataclass(frozen=True)
class ProjectionComparison:
    active_build_id: str
    shadow_build_id: str
    active_state_sha256: str
    shadow_state_sha256: str
    active_global_position: int
    shadow_global_position: int
    equivalent: bool
    differences: tuple[ProjectionDifference, ...]
    truncated: bool
    comparison_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        for name in (
            "active_build_id",
            "shadow_build_id",
            "active_state_sha256",
            "shadow_state_sha256",
        ):
            _digest(getattr(self, name), name)
        for name in ("active_global_position", "shadow_global_position"):
            value = getattr(self, name)
            if type(value) is not int or value < 0:
                _fail(
                    "TBM_PROJECTION_COMPARISON_INVALID",
                    f"{name} is invalid",
                )
        if type(self.equivalent) is not bool or type(self.truncated) is not bool:
            _fail(
                "TBM_PROJECTION_COMPARISON_INVALID",
                "comparison flags must be booleans",
            )
        if type(self.differences) is not tuple or any(
            type(item) is not ProjectionDifference for item in self.differences
        ):
            _fail(
                "TBM_PROJECTION_COMPARISON_INVALID",
                "differences must contain ProjectionDifference values",
            )
        if len(self.differences) > PROJECTION_MAX_DIFFERENCES:
            _fail(
                "TBM_PROJECTION_COMPARISON_INVALID",
                "projection comparison exceeds the difference limit",
            )
        if self.equivalent != (
            self.active_state_sha256 == self.shadow_state_sha256
            and self.active_global_position == self.shadow_global_position
        ):
            _fail(
                "TBM_PROJECTION_COMPARISON_INVALID",
                "equivalent flag does not match compared projection digests",
            )
        object.__setattr__(
            self,
            "comparison_sha256",
            _domain_sha256(
                b"tbm.projection-comparison.v1\x00",
                self.to_dict(include_digest=False),
            ),
        )

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "protocol_version": PROJECTION_PROTOCOL_VERSION,
            "active_build_id": self.active_build_id,
            "shadow_build_id": self.shadow_build_id,
            "active_state_sha256": self.active_state_sha256,
            "shadow_state_sha256": self.shadow_state_sha256,
            "active_global_position": self.active_global_position,
            "shadow_global_position": self.shadow_global_position,
            "equivalent": self.equivalent,
            "differences": [item.to_dict() for item in self.differences],
            "truncated": self.truncated,
        }
        if include_digest:
            value["comparison_sha256"] = self.comparison_sha256
        return value


class ProjectionRuntime:
    """Bounded deterministic reducer orchestration over one ledger partition."""

    def __init__(
        self,
        ledger: EventLedgerPort,
        reducer_registry: ReducerRegistry,
        checkpoint_store: ProjectionCheckpointStore,
        *,
        event_registry: EventTypeRegistry | None = None,
        require_complete_classification_view: bool = True,
    ) -> None:
        if type(reducer_registry) is not ReducerRegistry or not reducer_registry.sealed:
            _fail(
                "TBM_PROJECTION_RUNTIME_INVALID",
                "reducer_registry must be a sealed ReducerRegistry",
            )
        if event_registry is not None and (
            type(event_registry) is not EventTypeRegistry or not event_registry.sealed
        ):
            _fail(
                "TBM_PROJECTION_RUNTIME_INVALID",
                "event_registry must be a sealed EventTypeRegistry",
            )
        if type(require_complete_classification_view) is not bool:
            _fail(
                "TBM_PROJECTION_RUNTIME_INVALID",
                "classification view requirement must be a boolean",
            )
        if not callable(getattr(ledger, "read_global", None)):
            _fail(
                "TBM_PROJECTION_RUNTIME_INVALID",
                "ledger does not implement read_global",
            )
        for method_name in (
            "save_checkpoint",
            "load_checkpoint",
            "load_latest_checkpoint",
            "list_checkpoints",
            "append_activation",
            "current_activation",
            "activation_history",
        ):
            if not callable(getattr(checkpoint_store, method_name, None)):
                _fail(
                    "TBM_PROJECTION_RUNTIME_INVALID",
                    "checkpoint_store does not implement the required protocol",
                )
        self._ledger = ledger
        self._reducer_registry = reducer_registry
        self._checkpoint_store = checkpoint_store
        self._event_registry = event_registry
        self._require_complete_classification_view = (
            require_complete_classification_view
        )

    def rebuild(
        self,
        reducer_id: str,
        reducer_version: int | None,
        *,
        partition_sha256: str,
        owner: str,
        rebuild_generation: int,
        page_size: int = PROJECTION_DEFAULT_PAGE_SIZE,
        checkpoint_interval: int = PROJECTION_DEFAULT_CHECKPOINT_INTERVAL,
        resume: bool = False,
        created_at: str | None = None,
    ) -> ProjectionRebuildResult:
        _digest(partition_sha256, "partition_sha256")
        _identifier(owner, "owner")
        _positive(rebuild_generation, "rebuild_generation")
        _bounded_positive(page_size, "page_size", PROJECTION_MAX_PAGE_SIZE)
        _bounded_positive(
            checkpoint_interval,
            "checkpoint_interval",
            PROJECTION_MAX_PAGE_SIZE,
        )
        if type(resume) is not bool:
            _fail(
                "TBM_PROJECTION_REBUILD_INVALID",
                "resume must be a boolean",
            )
        timestamp = _timestamp_or_now(created_at)
        self._verify_ledger_view(partition_sha256)
        reducer = self._reducer_registry.resolve(reducer_id, reducer_version)
        descriptor = reducer.descriptor
        event_registry_sha256 = self._event_registry_sha256(descriptor)
        registry_sha256 = self._reducer_registry.registry_sha256

        retained: ProjectionCheckpoint | None = None
        if resume:
            retained = self._checkpoint_store.load_latest_checkpoint(
                descriptor.output_projection,
                descriptor.reducer_version,
                partition_sha256,
            )
        if retained is not None:
            if not retained.compatible_with(
                descriptor,
                reducer_registry_sha256=registry_sha256,
                event_registry_sha256=event_registry_sha256,
            ):
                if (
                    retained.reducer_descriptor.reducer_id != descriptor.reducer_id
                    or retained.reducer_descriptor.reducer_version
                    != descriptor.reducer_version
                ):
                    code = "TBM_REDUCER_VERSION_MISMATCH"
                    message = "retained checkpoint reducer identity does not match"
                elif retained.reducer_descriptor.code_sha256 != descriptor.code_sha256:
                    code = "TBM_REDUCER_CODE_HASH_MISMATCH"
                    message = "retained checkpoint reducer code hash does not match"
                elif (
                    retained.reducer_descriptor.configuration_sha256
                    != descriptor.configuration_sha256
                ):
                    code = "TBM_REDUCER_CONFIGURATION_HASH_MISMATCH"
                    message = (
                        "retained checkpoint reducer configuration hash does not match"
                    )
                else:
                    code = "TBM_PROJECTION_CHECKPOINT_INCOMPATIBLE"
                    message = "retained checkpoint is incompatible with the runtime"
                _fail(code, message)
            state = canonical_projection_state(retained.state)
            cursor = retained.global_position
            resumed_from = retained.build_id
        else:
            initial = initial_reducer_state(reducer)
            state = canonical_projection_state(initial.state)
            cursor = 0
            resumed_from = None

        first_page = self._ledger.read_global(cursor, page_size)
        target_high_watermark = first_page.high_watermark_global_position
        if cursor > target_high_watermark:
            _fail(
                "TBM_PROJECTION_CHECKPOINT_AHEAD",
                "retained checkpoint is ahead of the ledger high watermark",
            )
        processed = 0
        page = first_page
        last_good_position = cursor
        while True:
            for source_event in page.events:
                if source_event.global_position > target_high_watermark:
                    break
                try:
                    typed_event = None
                    if (
                        descriptor.accepts(source_event.event_type)
                        and not descriptor.envelope_only
                    ):
                        if self._event_registry is None:
                            _fail(
                                "TBM_PROJECTION_EVENT_REGISTRY_REQUIRED",
                                "typed reducer requires an event type registry",
                            )
                        typed_event = self._event_registry.consume(
                            source_event,
                            target_version=descriptor.target_version_for(
                                source_event.event_type
                            ),
                        )
                    step = execute_reducer_step(
                        reducer,
                        state,
                        ReducerEvent(source_event, typed_event),
                    )
                except (V3ContractError, ValueError) as error:
                    code = getattr(error, "code", "TBM_REDUCER_TRANSITION_FAILED")
                    checkpoint = self._checkpoint(
                        descriptor,
                        partition_sha256,
                        last_good_position,
                        target_high_watermark,
                        state,
                        registry_sha256,
                        event_registry_sha256,
                        timestamp,
                        owner,
                        rebuild_generation,
                    )
                    retained_checkpoint = self._checkpoint_store.save_checkpoint(
                        checkpoint
                    )
                    return ProjectionRebuildResult(
                        "blocked",
                        retained_checkpoint,
                        ProjectionBlocked(
                            event_id=source_event.event_id,
                            event_sha256=source_event.event_sha256,
                            payload_sha256=source_event.payload_sha256,
                            reducer_id=descriptor.reducer_id,
                            reducer_version=descriptor.reducer_version,
                            projection_name=descriptor.output_projection,
                            last_good_position=last_good_position,
                            error_code=code,
                            retryable=False,
                            required_migration=(
                                "event-upcaster"
                                if code.startswith("TBM_EVENT_REGISTRY_")
                                else None
                            ),
                        ),
                        resumed_from,
                        processed,
                    )
                state = canonical_projection_state(step.state)
                last_good_position = source_event.global_position
                processed += 1
                if processed % checkpoint_interval == 0:
                    checkpoint = self._checkpoint(
                        descriptor,
                        partition_sha256,
                        last_good_position,
                        target_high_watermark,
                        state,
                        registry_sha256,
                        event_registry_sha256,
                        timestamp,
                        owner,
                        rebuild_generation,
                    )
                    self._checkpoint_store.save_checkpoint(checkpoint)
            if (
                last_good_position >= target_high_watermark
                or not page.has_more
                or not page.events
            ):
                break
            cursor = page.next_global_position
            if cursor >= target_high_watermark:
                break
            page = self._ledger.read_global(cursor, page_size)

        final_checkpoint = self._checkpoint(
            descriptor,
            partition_sha256,
            target_high_watermark,
            target_high_watermark,
            state,
            registry_sha256,
            event_registry_sha256,
            timestamp,
            owner,
            rebuild_generation,
        )
        final_checkpoint = self._checkpoint_store.save_checkpoint(final_checkpoint)
        return ProjectionRebuildResult(
            "completed",
            final_checkpoint,
            None,
            resumed_from,
            processed,
        )

    def compare(
        self,
        active_build_id: str,
        shadow_build_id: str,
    ) -> ProjectionComparison:
        active = self._checkpoint_store.load_checkpoint(active_build_id)
        shadow = self._checkpoint_store.load_checkpoint(shadow_build_id)
        if (
            active.projection_name != shadow.projection_name
            or active.partition_sha256 != shadow.partition_sha256
        ):
            _fail(
                "TBM_PROJECTION_COMPARISON_SCOPE_MISMATCH",
                "projection comparison targets different projections or partitions",
            )
        differences: list[ProjectionDifference] = []
        truncated = _collect_differences(
            canonical_projection_state(active.state),
            canonical_projection_state(shadow.state),
            path="state",
            sink=differences,
        )
        return ProjectionComparison(
            active_build_id=active.build_id,
            shadow_build_id=shadow.build_id,
            active_state_sha256=active.state_sha256,
            shadow_state_sha256=shadow.state_sha256,
            active_global_position=active.global_position,
            shadow_global_position=shadow.global_position,
            equivalent=(
                active.state_sha256 == shadow.state_sha256
                and active.global_position == shadow.global_position
            ),
            differences=tuple(differences),
            truncated=truncated,
        )

    def activate(
        self,
        shadow_build_id: str,
        *,
        owner: str,
        approved: bool,
        expected_head_version: int,
        expected_current_build_id: str | None,
        comparison: ProjectionComparison | None = None,
        created_at: str | None = None,
    ) -> ProjectionActivation:
        if approved is not True:
            _fail(
                "TBM_PROJECTION_APPROVAL_REQUIRED",
                "projection activation requires explicit approval",
            )
        _identifier(owner, "owner")
        _position(expected_head_version, "expected_head_version")
        if expected_current_build_id is not None:
            _digest(expected_current_build_id, "expected_current_build_id")
        shadow = self._checkpoint_store.load_checkpoint(shadow_build_id)
        if expected_current_build_id is not None:
            if comparison is None:
                _fail(
                    "TBM_PROJECTION_COMPARISON_REQUIRED",
                    "projection replacement requires a bound comparison",
                )
            if (
                comparison.active_build_id != expected_current_build_id
                or comparison.shadow_build_id != shadow_build_id
            ):
                _fail(
                    "TBM_PROJECTION_COMPARISON_MISMATCH",
                    "projection comparison is not bound to the requested head switch",
                )
        activation = ProjectionActivation(
            projection_name=shadow.projection_name,
            partition_sha256=shadow.partition_sha256,
            head_version=expected_head_version + 1,
            target_build_id=shadow.build_id,
            previous_build_id=expected_current_build_id,
            operation="activate",
            owner=owner,
            created_at=_timestamp_or_now(created_at),
        )
        return self._checkpoint_store.append_activation(
            activation,
            expected_head_version=expected_head_version,
            expected_current_build_id=expected_current_build_id,
        )

    def rollback(
        self,
        projection_name: str,
        partition_sha256: str,
        *,
        owner: str,
        expected_head_version: int,
        expected_current_build_id: str,
        target_build_id: str | None = None,
        created_at: str | None = None,
    ) -> ProjectionActivation:
        _identifier(projection_name, "projection_name")
        _digest(partition_sha256, "partition_sha256")
        _identifier(owner, "owner")
        _position(expected_head_version, "expected_head_version")
        _digest(expected_current_build_id, "expected_current_build_id")
        current = self._checkpoint_store.current_activation(
            projection_name,
            partition_sha256,
        )
        if (
            current is None
            or current.head_version != expected_head_version
            or current.target_build_id != expected_current_build_id
        ):
            _fail(
                "TBM_PROJECTION_HEAD_CONFLICT",
                "projection head changed before rollback",
            )
        target = current.previous_build_id if target_build_id is None else target_build_id
        if target is None:
            _fail(
                "TBM_PROJECTION_ROLLBACK_UNAVAILABLE",
                "projection head has no retained predecessor",
            )
        checkpoint = self._checkpoint_store.load_checkpoint(target)
        if (
            checkpoint.projection_name != projection_name
            or checkpoint.partition_sha256 != partition_sha256
        ):
            _fail(
                "TBM_PROJECTION_ROLLBACK_SCOPE_MISMATCH",
                "rollback target belongs to another projection or partition",
            )
        if target_build_id is not None:
            history_targets = {
                item.target_build_id
                for item in self._checkpoint_store.activation_history(
                    projection_name,
                    partition_sha256,
                )
            }
            if target not in history_targets:
                _fail(
                    "TBM_PROJECTION_ROLLBACK_UNAVAILABLE",
                    "rollback target was never an active projection head",
                )
        activation = ProjectionActivation(
            projection_name=projection_name,
            partition_sha256=partition_sha256,
            head_version=expected_head_version + 1,
            target_build_id=target,
            previous_build_id=expected_current_build_id,
            operation="rollback",
            owner=owner,
            created_at=_timestamp_or_now(created_at),
        )
        return self._checkpoint_store.append_activation(
            activation,
            expected_head_version=expected_head_version,
            expected_current_build_id=expected_current_build_id,
        )

    def _checkpoint(
        self,
        descriptor: ReducerDescriptor,
        partition_sha256: str,
        global_position: int,
        event_high_watermark: int,
        state: Mapping[str, object],
        reducer_registry_sha256: str,
        event_registry_sha256: str | None,
        created_at: str,
        owner: str,
        rebuild_generation: int,
    ) -> ProjectionCheckpoint:
        return ProjectionCheckpoint(
            reducer_descriptor=descriptor,
            partition_sha256=partition_sha256,
            global_position=global_position,
            event_high_watermark=event_high_watermark,
            state=state,
            reducer_registry_sha256=reducer_registry_sha256,
            event_registry_sha256=event_registry_sha256,
            created_at=created_at,
            owner=owner,
            rebuild_generation=rebuild_generation,
        )

    def _event_registry_sha256(
        self,
        descriptor: ReducerDescriptor,
    ) -> str | None:
        if descriptor.envelope_only:
            return None
        if self._event_registry is None:
            _fail(
                "TBM_PROJECTION_EVENT_REGISTRY_REQUIRED",
                "typed reducer requires an event type registry",
            )
        digest = self._event_registry.catalog().get("registry_sha256")
        if type(digest) is not str:
            raise AssertionError("event registry digest is invalid")
        return digest

    def _verify_ledger_view(self, partition_sha256: str) -> None:
        access = getattr(self._ledger, "access_context", None)
        if access is None:
            if self._require_complete_classification_view:
                _fail(
                    "TBM_PROJECTION_LEDGER_VIEW_UNVERIFIED",
                    "projection rebuild cannot verify the ledger access view",
                )
            return
        if type(access) is not LedgerAccessContext:
            _fail(
                "TBM_PROJECTION_LEDGER_VIEW_UNVERIFIED",
                "projection rebuild ledger access context is invalid",
            )
        if access.partition.partition_sha256 != partition_sha256:
            _fail(
                "TBM_PROJECTION_PARTITION_MISMATCH",
                "projection partition does not match ledger access",
            )
        if (
            self._require_complete_classification_view
            and access.classification_filter.allowed != _ALL_CLASSIFICATIONS
        ):
            _fail(
                "TBM_PROJECTION_CLASSIFICATION_VIEW_INCOMPLETE",
                "projection rebuild requires every event classification",
            )


def _collect_differences(
    active: object,
    shadow: object,
    *,
    path: str,
    sink: list[ProjectionDifference],
) -> bool:
    if len(sink) >= PROJECTION_MAX_DIFFERENCES:
        return True
    if type(active) is not type(shadow):
        sink.append(
            ProjectionDifference(
                path,
                "type_changed",
                _value_sha256(active),
                _value_sha256(shadow),
            )
        )
        return False
    if isinstance(active, dict) and isinstance(shadow, dict):
        for key in sorted(set(active) | set(shadow)):
            if len(sink) >= PROJECTION_MAX_DIFFERENCES:
                return True
            child_path = f"{path}.{key}"
            if key not in active:
                sink.append(
                    ProjectionDifference(
                        child_path,
                        "added",
                        None,
                        _value_sha256(shadow[key]),
                    )
                )
            elif key not in shadow:
                sink.append(
                    ProjectionDifference(
                        child_path,
                        "removed",
                        _value_sha256(active[key]),
                        None,
                    )
                )
            else:
                truncated = _collect_differences(
                    active[key],
                    shadow[key],
                    path=child_path,
                    sink=sink,
                )
                if truncated:
                    return True
        return False
    if isinstance(active, list) and isinstance(shadow, list):
        if active != shadow:
            sink.append(
                ProjectionDifference(
                    path,
                    "changed",
                    _value_sha256(active),
                    _value_sha256(shadow),
                )
            )
        return False
    if active != shadow:
        sink.append(
            ProjectionDifference(
                path,
                "changed",
                _value_sha256(active),
                _value_sha256(shadow),
            )
        )
    return False


def _value_sha256(value: object) -> str:
    return _domain_sha256(b"tbm.projection-difference-value.v1\x00", value)


def _timestamp_or_now(value: str | None) -> str:
    if value is None:
        return datetime.now(UTC).strftime("%Y-%m-%dT%H:%M:%S.%fZ")
    if (
        type(value) is not str
        or len(value) != 27
        or not value.endswith("Z")
        or value[10] != "T"
        or value[19] != "."
    ):
        _fail(
            "TBM_PROJECTION_TIMESTAMP_INVALID",
            "projection timestamp is invalid",
        )
    try:
        datetime.strptime(value, "%Y-%m-%dT%H:%M:%S.%fZ")
    except ValueError as error:
        raise ProjectionRuntimeError(
            "TBM_PROJECTION_TIMESTAMP_INVALID",
            "projection timestamp is invalid",
        ) from error
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
        raise ProjectionRuntimeError(
            "TBM_PROJECTION_VALUE_INVALID",
            "projection value is not canonical JSON",
        ) from error


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_json_bytes(value)).hexdigest()


def _identifier(value: object, name: str) -> None:
    if (
        type(value) is not str
        or not value
        or len(value) > 128
        or value.strip() != value
        or any(character not in "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789._:-" for character in value)
    ):
        _fail("TBM_PROJECTION_VALUE_INVALID", f"{name} is invalid")


def _digest(value: object, name: str) -> None:
    if (
        type(value) is not str
        or len(value) != 71
        or not value.startswith("sha256:")
        or any(character not in "0123456789abcdef" for character in value[7:])
    ):
        _fail("TBM_PROJECTION_VALUE_INVALID", f"{name} is invalid")


def _positive(value: object, name: str) -> None:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        _fail("TBM_PROJECTION_VALUE_INVALID", f"{name} is invalid")


def _position(value: object, name: str) -> None:
    if type(value) is not int or not 0 <= value <= 2**63 - 1:
        _fail("TBM_PROJECTION_VALUE_INVALID", f"{name} is invalid")


def _bounded_positive(value: object, name: str, maximum: int) -> None:
    if type(value) is not int or not 1 <= value <= maximum:
        _fail("TBM_PROJECTION_VALUE_INVALID", f"{name} is invalid")


def _fail(code: str, message: str) -> NoReturn:
    raise ProjectionRuntimeError(code, message)


__all__ = [
    "PROJECTION_DEFAULT_CHECKPOINT_INTERVAL",
    "PROJECTION_DEFAULT_PAGE_SIZE",
    "PROJECTION_MAX_DIFFERENCES",
    "PROJECTION_MAX_PAGE_SIZE",
    "PROJECTION_PROTOCOL_VERSION",
    "ProjectionComparison",
    "ProjectionDifference",
    "ProjectionDifferenceKind",
    "ProjectionRebuildResult",
    "ProjectionRebuildStatus",
    "ProjectionRuntime",
    "ProjectionRuntimeError",
]
