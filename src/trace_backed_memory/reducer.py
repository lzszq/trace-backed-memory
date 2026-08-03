from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import re
from types import MappingProxyType
from typing import NoReturn, Protocol, cast

from .contracts_v3 import V3ContractError
from .event_registry_v1 import TypedEventView
from .event_v1 import CanonicalEvent


REDUCER_PROTOCOL_VERSION = "tbm.reducer.v1"
REDUCER_MAX_INPUT_EVENT_TYPES = 256
REDUCER_MAX_STATE_BYTES = 1024 * 1024
REDUCER_MAX_STATE_DEPTH = 32
REDUCER_MAX_STATE_NODES = 32_768

_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_EVENT_TYPE_RE = re.compile(
    r"^tbm\.[a-z0-9][a-z0-9_]*(?:\.[a-z0-9][a-z0-9_]*)+$"
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")

ProjectionState = Mapping[str, object]
ReducerInitialState = Callable[[], Mapping[str, object]]
ReducerTransition = Callable[
    [Mapping[str, object], "ReducerEvent"],
    Mapping[str, object],
]


class ReducerV1Error(V3ContractError):
    """Stable reducer descriptor, execution, or determinism failure."""


class ReducerExecutionError(ReducerV1Error):
    """A reducer rejected one canonical event with a sanitized failure."""


class ReducerDeterminismError(ReducerV1Error):
    """Repeated execution did not produce the same canonical state."""


@dataclass(frozen=True)
class ReducerDescriptor:
    reducer_id: str
    reducer_version: int
    input_event_types: tuple[str, ...]
    output_projection: str
    output_schema_version: int
    code_sha256: str
    configuration_sha256: str
    deterministic: bool = True
    target_event_versions: Mapping[str, int] = field(default_factory=dict)
    envelope_only: bool = False
    descriptor_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _identifier(self.reducer_id, "reducer_id")
        _positive_version(self.reducer_version, "reducer_version")
        _identifier(self.output_projection, "output_projection")
        _positive_version(
            self.output_schema_version,
            "output_schema_version",
        )
        _digest(self.code_sha256, "code_sha256")
        _digest(self.configuration_sha256, "configuration_sha256")
        if self.deterministic is not True:
            _fail(
                "TBM_REDUCER_DESCRIPTOR_INVALID",
                "reducer descriptor must declare deterministic true",
            )
        if type(self.input_event_types) is not tuple:
            _fail(
                "TBM_REDUCER_DESCRIPTOR_INVALID",
                "input_event_types must be a tuple",
            )
        if not 1 <= len(self.input_event_types) <= REDUCER_MAX_INPUT_EVENT_TYPES:
            _fail(
                "TBM_REDUCER_DESCRIPTOR_INVALID",
                "input_event_types count is outside the supported range",
            )
        if self.envelope_only is True:
            if self.input_event_types != ("*",):
                _fail(
                    "TBM_REDUCER_DESCRIPTOR_INVALID",
                    "envelope-only reducers must declare the wildcard input",
                )
        else:
            if any(
                type(event_type) is not str
                or _EVENT_TYPE_RE.fullmatch(event_type) is None
                for event_type in self.input_event_types
            ):
                _fail(
                    "TBM_REDUCER_DESCRIPTOR_INVALID",
                    "input_event_types contain an invalid canonical event type",
                )
            if self.input_event_types != tuple(sorted(set(self.input_event_types))):
                _fail(
                    "TBM_REDUCER_DESCRIPTOR_INVALID",
                    "input_event_types must be sorted and unique",
                )
        if type(self.envelope_only) is not bool:
            _fail(
                "TBM_REDUCER_DESCRIPTOR_INVALID",
                "envelope_only must be a boolean",
            )
        targets = _copy_target_versions(self.target_event_versions)
        if self.envelope_only and targets:
            _fail(
                "TBM_REDUCER_DESCRIPTOR_INVALID",
                "envelope-only reducers cannot request typed event versions",
            )
        if any(event_type not in self.input_event_types for event_type in targets):
            _fail(
                "TBM_REDUCER_DESCRIPTOR_INVALID",
                "target event version is not declared as an input",
            )
        object.__setattr__(
            self,
            "target_event_versions",
            MappingProxyType(targets),
        )
        unsigned = self.to_dict(include_digest=False)
        object.__setattr__(
            self,
            "descriptor_sha256",
            _domain_sha256(b"tbm.reducer-descriptor.v1\x00", unsigned),
        )

    def accepts(self, event_type: str) -> bool:
        return self.envelope_only or event_type in self.input_event_types

    def target_version_for(self, event_type: str) -> int | None:
        return self.target_event_versions.get(event_type)

    def to_dict(self, *, include_digest: bool = True) -> dict[str, object]:
        value: dict[str, object] = {
            "protocol_version": REDUCER_PROTOCOL_VERSION,
            "reducer_id": self.reducer_id,
            "reducer_version": self.reducer_version,
            "input_event_types": list(self.input_event_types),
            "output_projection": self.output_projection,
            "output_schema_version": self.output_schema_version,
            "code_sha256": self.code_sha256,
            "configuration_sha256": self.configuration_sha256,
            "deterministic": self.deterministic,
            "target_event_versions": dict(self.target_event_versions),
            "envelope_only": self.envelope_only,
        }
        if include_digest:
            value["descriptor_sha256"] = self.descriptor_sha256
        return value


@dataclass(frozen=True)
class ReducerEvent:
    source_event: CanonicalEvent
    typed_event: TypedEventView | None

    def __post_init__(self) -> None:
        if type(self.source_event) is not CanonicalEvent:
            _fail(
                "TBM_REDUCER_EVENT_INVALID",
                "source_event must be exactly CanonicalEvent",
            )
        if self.typed_event is not None:
            if type(self.typed_event) is not TypedEventView:
                _fail(
                    "TBM_REDUCER_EVENT_INVALID",
                    "typed_event must be exactly TypedEventView",
                )
            if self.typed_event.source_event != self.source_event:
                _fail(
                    "TBM_REDUCER_EVENT_INVALID",
                    "typed_event is not bound to source_event",
                )


class Reducer(Protocol):
    @property
    def descriptor(self) -> ReducerDescriptor: ...

    def initial_state(self) -> Mapping[str, object]: ...

    def reduce(
        self,
        state: Mapping[str, object],
        event: ReducerEvent,
    ) -> Mapping[str, object]: ...


@dataclass(frozen=True)
class FunctionalReducer:
    descriptor: ReducerDescriptor
    initial_state_factory: ReducerInitialState = field(repr=False, compare=False)
    transition: ReducerTransition = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        if type(self.descriptor) is not ReducerDescriptor:
            _fail(
                "TBM_REDUCER_INVALID",
                "descriptor must be exactly ReducerDescriptor",
            )
        if not callable(self.initial_state_factory) or not callable(self.transition):
            _fail(
                "TBM_REDUCER_INVALID",
                "reducer callbacks must be callable",
            )

    def initial_state(self) -> Mapping[str, object]:
        return self.initial_state_factory()

    def reduce(
        self,
        state: Mapping[str, object],
        event: ReducerEvent,
    ) -> Mapping[str, object]:
        return self.transition(state, event)


@dataclass(frozen=True)
class ReducerStepResult:
    state: Mapping[str, object]
    state_sha256: str
    changed: bool

    def __post_init__(self) -> None:
        copied = canonical_projection_state(self.state)
        _digest(self.state_sha256, "state_sha256")
        object.__setattr__(self, "state", _freeze_json(copied))


def canonical_projection_state(value: object) -> dict[str, object]:
    """Copy and bound one deterministic JSON projection state."""

    if not isinstance(value, Mapping):
        _fail(
            "TBM_REDUCER_STATE_INVALID",
            "projection state root must be a mapping",
        )
    nodes = [0]
    copied = _copy_state(value, path="state", depth=0, nodes=nodes)
    if not isinstance(copied, dict):
        raise AssertionError("projection state copy did not preserve object root")
    encoded = _canonical_json_bytes(copied)
    if len(encoded) > REDUCER_MAX_STATE_BYTES:
        _fail(
            "TBM_REDUCER_STATE_LIMIT_EXCEEDED",
            "projection state exceeds the byte limit",
        )
    return copied


def projection_state_sha256(
    output_projection: str,
    output_schema_version: int,
    state: object,
) -> str:
    _identifier(output_projection, "output_projection")
    _positive_version(output_schema_version, "output_schema_version")
    copied = canonical_projection_state(state)
    return _domain_sha256(
        b"tbm.projection-state.v1\x00",
        {
            "output_projection": output_projection,
            "output_schema_version": output_schema_version,
            "state": copied,
        },
    )


def initial_reducer_state(reducer: Reducer) -> ReducerStepResult:
    descriptor = _reducer_descriptor(reducer)
    try:
        first = canonical_projection_state(reducer.initial_state())
        second = canonical_projection_state(reducer.initial_state())
    except ReducerV1Error:
        raise
    except Exception as error:
        raise ReducerExecutionError(
            "TBM_REDUCER_INITIAL_STATE_FAILED",
            "reducer initial state failed",
        ) from error
    first_digest = projection_state_sha256(
        descriptor.output_projection,
        descriptor.output_schema_version,
        first,
    )
    second_digest = projection_state_sha256(
        descriptor.output_projection,
        descriptor.output_schema_version,
        second,
    )
    if first_digest != second_digest:
        raise ReducerDeterminismError(
            "TBM_REDUCER_NONDETERMINISTIC_INITIAL_STATE",
            "reducer initial state is not deterministic",
        )
    return ReducerStepResult(first, first_digest, True)


def execute_reducer_step(
    reducer: Reducer,
    state: Mapping[str, object],
    event: ReducerEvent,
) -> ReducerStepResult:
    descriptor = _reducer_descriptor(reducer)
    if type(event) is not ReducerEvent:
        _fail(
            "TBM_REDUCER_EVENT_INVALID",
            "event must be exactly ReducerEvent",
        )
    before = canonical_projection_state(state)
    before_digest = projection_state_sha256(
        descriptor.output_projection,
        descriptor.output_schema_version,
        before,
    )
    if not descriptor.accepts(event.source_event.event_type):
        return ReducerStepResult(before, before_digest, False)
    if descriptor.envelope_only:
        if event.typed_event is not None:
            _fail(
                "TBM_REDUCER_EVENT_INVALID",
                "envelope-only reducers must not receive typed payloads",
            )
    elif event.typed_event is None:
        _fail(
            "TBM_REDUCER_TYPED_EVENT_REQUIRED",
            "typed reducer input is missing",
        )
    try:
        first = canonical_projection_state(
            reducer.reduce(_freeze_json(before), event)
        )
        second = canonical_projection_state(
            reducer.reduce(_freeze_json(before), event)
        )
    except ReducerV1Error:
        raise
    except Exception as error:
        raise ReducerExecutionError(
            "TBM_REDUCER_TRANSITION_FAILED",
            "reducer transition failed",
        ) from error
    first_digest = projection_state_sha256(
        descriptor.output_projection,
        descriptor.output_schema_version,
        first,
    )
    second_digest = projection_state_sha256(
        descriptor.output_projection,
        descriptor.output_schema_version,
        second,
    )
    if first_digest != second_digest:
        raise ReducerDeterminismError(
            "TBM_REDUCER_NONDETERMINISTIC_TRANSITION",
            "reducer transition is not deterministic",
        )
    return ReducerStepResult(first, first_digest, first_digest != before_digest)


def build_event_inventory_reducer() -> FunctionalReducer:
    """Return the envelope-only reducer used by operator conformance checks."""

    code_sha256 = _domain_sha256(
        b"tbm.reducer-code.v1\x00",
        {
            "algorithm": "canonical-event-inventory",
            "algorithm_version": 1,
            "fields": [
                "event_count",
                "event_type_counts",
                "last_event_sha256",
                "last_global_position",
            ],
        },
    )
    configuration_sha256 = _domain_sha256(
        b"tbm.reducer-configuration.v1\x00",
        {"configuration": "none", "version": 1},
    )
    descriptor = ReducerDescriptor(
        reducer_id="canonical-event-inventory",
        reducer_version=1,
        input_event_types=("*",),
        output_projection="canonical_event_inventory_v1",
        output_schema_version=1,
        code_sha256=code_sha256,
        configuration_sha256=configuration_sha256,
        envelope_only=True,
    )

    def initial() -> Mapping[str, object]:
        return {
            "event_count": 0,
            "event_type_counts": {},
            "last_event_sha256": None,
            "last_global_position": 0,
        }

    def transition(
        state: Mapping[str, object],
        reducer_event: ReducerEvent,
    ) -> Mapping[str, object]:
        source = reducer_event.source_event
        raw_counts = state["event_type_counts"]
        if not isinstance(raw_counts, Mapping):
            raise ValueError("event_type_counts is invalid")
        counts = {str(key): int(value) for key, value in raw_counts.items()}
        counts[source.event_type] = counts.get(source.event_type, 0) + 1
        return {
            "event_count": int(state["event_count"]) + 1,
            "event_type_counts": counts,
            "last_event_sha256": source.event_sha256,
            "last_global_position": source.global_position,
        }

    return FunctionalReducer(descriptor, initial, transition)


def parse_reducer_descriptor(value: object) -> ReducerDescriptor:
    if not isinstance(value, Mapping):
        _fail(
            "TBM_REDUCER_DESCRIPTOR_INVALID",
            "reducer descriptor must be an object",
        )
    expected = {
        "protocol_version",
        "reducer_id",
        "reducer_version",
        "input_event_types",
        "output_projection",
        "output_schema_version",
        "code_sha256",
        "configuration_sha256",
        "deterministic",
        "target_event_versions",
        "envelope_only",
        "descriptor_sha256",
    }
    if set(value) != expected or value.get("protocol_version") != REDUCER_PROTOCOL_VERSION:
        _fail(
            "TBM_REDUCER_DESCRIPTOR_INVALID",
            "reducer descriptor fields or protocol version are invalid",
        )
    inputs = value.get("input_event_types")
    if type(inputs) is not list or any(type(item) is not str for item in inputs):
        _fail(
            "TBM_REDUCER_DESCRIPTOR_INVALID",
            "input_event_types are invalid",
        )
    descriptor = ReducerDescriptor(
        reducer_id=value.get("reducer_id"),
        reducer_version=value.get("reducer_version"),
        input_event_types=tuple(inputs),
        output_projection=value.get("output_projection"),
        output_schema_version=value.get("output_schema_version"),
        code_sha256=value.get("code_sha256"),
        configuration_sha256=value.get("configuration_sha256"),
        deterministic=value.get("deterministic"),
        target_event_versions=value.get("target_event_versions"),
        envelope_only=value.get("envelope_only"),
    )
    if value.get("descriptor_sha256") != descriptor.descriptor_sha256:
        _fail(
            "TBM_REDUCER_DESCRIPTOR_DIGEST_MISMATCH",
            "reducer descriptor digest does not match",
        )
    return descriptor


def _reducer_descriptor(reducer: Reducer) -> ReducerDescriptor:
    try:
        descriptor = reducer.descriptor
    except Exception as error:
        raise ReducerV1Error(
            "TBM_REDUCER_INVALID",
            "reducer descriptor is unavailable",
        ) from error
    if type(descriptor) is not ReducerDescriptor:
        _fail(
            "TBM_REDUCER_INVALID",
            "reducer descriptor must be exactly ReducerDescriptor",
        )
    if not callable(getattr(reducer, "initial_state", None)) or not callable(
        getattr(reducer, "reduce", None)
    ):
        _fail(
            "TBM_REDUCER_INVALID",
            "reducer must implement initial_state and reduce",
        )
    return descriptor


def _copy_target_versions(value: object) -> dict[str, int]:
    if not isinstance(value, Mapping):
        _fail(
            "TBM_REDUCER_DESCRIPTOR_INVALID",
            "target_event_versions must be a mapping",
        )
    copied: dict[str, int] = {}
    for event_type, version in value.items():
        if type(event_type) is not str or _EVENT_TYPE_RE.fullmatch(event_type) is None:
            _fail(
                "TBM_REDUCER_DESCRIPTOR_INVALID",
                "target_event_versions contains an invalid event type",
            )
        _positive_version(version, "target_event_version")
        copied[event_type] = version
    return dict(sorted(copied.items()))


def _copy_state(
    value: object,
    *,
    path: str,
    depth: int,
    nodes: list[int],
) -> object:
    nodes[0] += 1
    if nodes[0] > REDUCER_MAX_STATE_NODES or depth > REDUCER_MAX_STATE_DEPTH:
        _fail(
            "TBM_REDUCER_STATE_LIMIT_EXCEEDED",
            "projection state exceeds structural limits",
        )
    if value is None or type(value) in {bool, str}:
        return value
    if type(value) is int:
        if not -(2**63) <= value <= 2**63 - 1:
            _fail(
                "TBM_REDUCER_STATE_INVALID",
                f"{path} integer is outside the signed 64-bit range",
            )
        return value
    if type(value) is float:
        _fail(
            "TBM_REDUCER_STATE_INVALID",
            f"{path} floating-point values are not deterministic projection state",
        )
    if isinstance(value, Mapping):
        copied: dict[str, object] = {}
        keys = list(value)
        for key in keys:
            if type(key) is not str or not key or len(key) > 256:
                _fail(
                    "TBM_REDUCER_STATE_INVALID",
                    f"{path} contains an invalid object key",
                )
        for key in sorted(cast(list[str], keys)):
            copied[key] = _copy_state(
                value[key],
                path=f"{path}.{key}",
                depth=depth + 1,
                nodes=nodes,
            )
        return copied
    if type(value) in {list, tuple}:
        sequence = cast(list[object] | tuple[object, ...], value)
        return [
            _copy_state(
                item,
                path=f"{path}[{index}]",
                depth=depth + 1,
                nodes=nodes,
            )
            for index, item in enumerate(sequence)
        ]
    _fail(
        "TBM_REDUCER_STATE_INVALID",
        f"{path} contains an unsupported value",
    )


def _freeze_json(value: object) -> object:
    if isinstance(value, dict):
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if isinstance(value, list):
        return tuple(_freeze_json(item) for item in value)
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
        raise ReducerV1Error(
            "TBM_REDUCER_STATE_INVALID",
            "reducer value is not canonical JSON",
        ) from error


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_json_bytes(value)).hexdigest()


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _fail(
            "TBM_REDUCER_DESCRIPTOR_INVALID",
            f"{name} is invalid",
        )


def _positive_version(value: object, name: str) -> None:
    if type(value) is not int or not 1 <= value <= 2**63 - 1:
        _fail(
            "TBM_REDUCER_DESCRIPTOR_INVALID",
            f"{name} is invalid",
        )


def _digest(value: object, name: str) -> None:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _fail(
            "TBM_REDUCER_DESCRIPTOR_INVALID",
            f"{name} is invalid",
        )


def _fail(code: str, message: str) -> NoReturn:
    raise ReducerV1Error(code, message)


__all__ = [
    "REDUCER_MAX_INPUT_EVENT_TYPES",
    "REDUCER_MAX_STATE_BYTES",
    "REDUCER_MAX_STATE_DEPTH",
    "REDUCER_MAX_STATE_NODES",
    "REDUCER_PROTOCOL_VERSION",
    "FunctionalReducer",
    "ProjectionState",
    "Reducer",
    "ReducerDescriptor",
    "ReducerDeterminismError",
    "ReducerEvent",
    "ReducerExecutionError",
    "ReducerInitialState",
    "ReducerStepResult",
    "ReducerTransition",
    "ReducerV1Error",
    "build_event_inventory_reducer",
    "canonical_projection_state",
    "execute_reducer_step",
    "initial_reducer_state",
    "parse_reducer_descriptor",
    "projection_state_sha256",
]
