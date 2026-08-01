from __future__ import annotations

from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
import hashlib
import json
import math
import re
from types import MappingProxyType
from typing import Literal, NoReturn, cast

from .contracts_v3 import V3ContractError
from .event_v1 import (
    CanonicalEvent,
    EventKind,
    EventV1ContractError,
    validate_event_payload,
)


EVENT_REGISTRY_PROTOCOL_VERSION = "tbm.event-registry.v1"
EVENT_REGISTRY_MAX_TYPES = 32
EVENT_REGISTRY_MAX_VERSIONS_PER_TYPE = 32
EVENT_REGISTRY_MAX_UPCASTERS = 2048
EVENT_REGISTRY_MAX_COMPATIBILITY_ROWS = 32_768
EVENT_REGISTRY_MAX_SCHEMA_BYTES = 128 * 1024
EVENT_REGISTRY_MAX_SCHEMA_DEPTH = 24
EVENT_REGISTRY_MAX_SCHEMA_NODES = 8192

EventRegistryResolutionStatus = Literal[
    "known",
    "unknown_type",
    "unknown_version",
]
EventCompatibility = Literal["native", "upcast", "unsupported"]
PayloadUpcaster = Callable[[Mapping[str, object]], Mapping[str, object]]

_EVENT_TYPE_RE = re.compile(
    r"^tbm\.[a-z0-9][a-z0-9_]*(?:\.[a-z0-9][a-z0-9_]*)+$"
)
_PAYLOAD_SCHEMA_RE = re.compile(
    r"^tbm\.[a-z0-9][a-z0-9_.-]*\.v[1-9][0-9]*$"
)
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_SCHEMA_ALLOWED_KEYS = frozenset(
    {
        "$id",
        "$schema",
        "additionalProperties",
        "const",
        "enum",
        "items",
        "maxItems",
        "maxLength",
        "maximum",
        "minItems",
        "minLength",
        "minimum",
        "oneOf",
        "pattern",
        "properties",
        "required",
        "title",
        "type",
        "uniqueItems",
    }
)
_SCHEMA_TYPES = frozenset(
    {"object", "array", "string", "integer", "number", "boolean", "null"}
)
class EventRegistryV1Error(V3ContractError):
    """Stable failure for event type registration and typed consumption."""


class UnknownEventTypeError(EventRegistryV1Error):
    """A canonical event is preserved but cannot be consumed as a typed event."""

    def __init__(
        self,
        event: CanonicalEvent,
        status: EventRegistryResolutionStatus,
    ) -> None:
        self.event = event
        self.status = status
        super().__init__(
            "TBM_EVENT_REGISTRY_UNKNOWN_EVENT",
            "event type or version is not registered for typed consumption",
        )


@dataclass(frozen=True)
class EventPayloadRegistration:
    event_type: str
    event_version: int
    event_kind: EventKind
    payload_schema: str
    schema: Mapping[str, object]
    payload_schema_sha256: str = field(init=False)

    def __post_init__(self) -> None:
        _event_type(self.event_type)
        _positive_version(self.event_version, "event_version")
        if self.event_kind not in {"domain", "observation"}:
            _fail(
                "TBM_EVENT_REGISTRY_REGISTRATION_INVALID",
                "event_kind is not supported",
            )
        if (
            type(self.payload_schema) is not str
            or _PAYLOAD_SCHEMA_RE.fullmatch(self.payload_schema) is None
        ):
            _fail(
                "TBM_EVENT_REGISTRY_REGISTRATION_INVALID",
                "payload_schema is not a versioned tbm schema name",
            )
        schema = _copy_json_schema(self.schema)
        _validate_schema_definition(schema, path="payload", depth=0)
        if schema.get("type") != "object":
            _fail(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "event payload schema root must have object type",
            )
        if schema.get("additionalProperties") is not False:
            _fail(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "event payload schema must reject additional properties",
            )
        schema_sha256 = _domain_sha256(
            b"tbm.event-payload-schema.v1\x00",
            _canonical_json_bytes(schema),
        )
        object.__setattr__(self, "schema", _freeze_json(schema))
        object.__setattr__(self, "payload_schema_sha256", schema_sha256)

    def to_dict(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "event_version": self.event_version,
            "event_kind": self.event_kind,
            "payload_schema": self.payload_schema,
            "payload_schema_sha256": self.payload_schema_sha256,
            "schema": _thaw_json(self.schema),
        }


@dataclass(frozen=True)
class EventPayloadUpcaster:
    event_type: str
    from_version: int
    to_version: int
    upcaster_id: str
    producer_version: str
    transform: PayloadUpcaster = field(repr=False, compare=False)

    def __post_init__(self) -> None:
        _event_type(self.event_type)
        _positive_version(self.from_version, "from_version")
        _positive_version(self.to_version, "to_version")
        if self.to_version != self.from_version + 1:
            _fail(
                "TBM_EVENT_REGISTRY_UPCASTER_INVALID",
                "upcasters must advance exactly one event version",
            )
        _identifier(self.upcaster_id, "upcaster_id")
        _identifier(self.producer_version, "producer_version")
        if not callable(self.transform):
            _fail(
                "TBM_EVENT_REGISTRY_UPCASTER_INVALID",
                "upcaster transform must be callable",
            )

    def to_dict(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "from_version": self.from_version,
            "to_version": self.to_version,
            "upcaster_id": self.upcaster_id,
            "producer_version": self.producer_version,
        }


@dataclass(frozen=True)
class EventRegistryResolution:
    event: CanonicalEvent
    status: EventRegistryResolutionStatus
    registration: EventPayloadRegistration | None

    @property
    def consumable(self) -> bool:
        return self.status == "known"


@dataclass(frozen=True)
class TypedEventView:
    source_event: CanonicalEvent
    event_type: str
    source_version: int
    target_version: int
    payload_schema: str
    payload: Mapping[str, object]
    applied_upcaster_ids: tuple[str, ...]

    def __post_init__(self) -> None:
        if type(self.source_event) is not CanonicalEvent:
            _fail(
                "TBM_EVENT_REGISTRY_TYPED_VIEW_INVALID",
                "source_event must be exactly CanonicalEvent",
            )
        if self.event_type != self.source_event.event_type:
            _fail(
                "TBM_EVENT_REGISTRY_TYPED_VIEW_INVALID",
                "typed event_type does not match source event",
            )
        if self.source_version != self.source_event.event_version:
            _fail(
                "TBM_EVENT_REGISTRY_TYPED_VIEW_INVALID",
                "typed source_version does not match source event",
            )
        if (
            type(self.target_version) is not int
            or self.target_version < self.source_version
            or (
                self.target_version == self.source_version
                and self.payload_schema != self.source_event.payload_schema
            )
        ):
            _fail(
                "TBM_EVENT_REGISTRY_TYPED_VIEW_INVALID",
                "typed target version or payload schema is inconsistent",
            )
        if (
            type(self.applied_upcaster_ids) is not tuple
            or len(self.applied_upcaster_ids)
            != self.target_version - self.source_version
        ):
            _fail(
                "TBM_EVENT_REGISTRY_TYPED_VIEW_INVALID",
                "typed upcaster chain length is inconsistent",
            )
        for upcaster_id in self.applied_upcaster_ids:
            _identifier(upcaster_id, "applied_upcaster_id")
        if not isinstance(self.payload, Mapping):
            _fail(
                "TBM_EVENT_REGISTRY_TYPED_VIEW_INVALID",
                "typed payload must be an object",
            )
        object.__setattr__(
            self,
            "payload",
            _freeze_json(_copy_payload(self.payload)),
        )


@dataclass(frozen=True)
class EventCompatibilityRow:
    event_type: str
    source_version: int
    target_version: int
    compatibility: EventCompatibility

    def to_dict(self) -> dict[str, object]:
        return {
            "event_type": self.event_type,
            "source_version": self.source_version,
            "target_version": self.target_version,
            "compatibility": self.compatibility,
        }


class EventTypeRegistry:
    """Build, seal, inspect, and explicitly consume typed canonical events."""

    def __init__(self) -> None:
        self._registrations: dict[
            tuple[str, int], EventPayloadRegistration
        ] = {}
        self._schema_names: set[str] = set()
        self._upcasters: dict[
            tuple[str, int], EventPayloadUpcaster
        ] = {}
        self._upcaster_ids: set[str] = set()
        self._sealed = False

    @property
    def sealed(self) -> bool:
        return self._sealed

    def register(self, registration: EventPayloadRegistration) -> None:
        self._require_mutable()
        if type(registration) is not EventPayloadRegistration:
            _fail(
                "TBM_EVENT_REGISTRY_REGISTRATION_INVALID",
                "registration must be exactly EventPayloadRegistration",
            )
        key = (registration.event_type, registration.event_version)
        if key in self._registrations:
            _fail(
                "TBM_EVENT_REGISTRY_DUPLICATE_TYPE_VERSION",
                "event type and version are already registered",
            )
        if registration.payload_schema in self._schema_names:
            _fail(
                "TBM_EVENT_REGISTRY_DUPLICATE_SCHEMA",
                "payload schema name is already registered",
            )
        versions = sum(
            1
            for event_type, _ in self._registrations
            if event_type == registration.event_type
        )
        if versions >= EVENT_REGISTRY_MAX_VERSIONS_PER_TYPE:
            _fail(
                "TBM_EVENT_REGISTRY_LIMIT_EXCEEDED",
                "event type version limit exceeded",
            )
        event_types = {event_type for event_type, _ in self._registrations}
        if (
            registration.event_type not in event_types
            and len(event_types) >= EVENT_REGISTRY_MAX_TYPES
        ):
            _fail(
                "TBM_EVENT_REGISTRY_LIMIT_EXCEEDED",
                "event type limit exceeded",
            )
        self._registrations[key] = registration
        self._schema_names.add(registration.payload_schema)

    def register_upcaster(self, upcaster: EventPayloadUpcaster) -> None:
        self._require_mutable()
        if type(upcaster) is not EventPayloadUpcaster:
            _fail(
                "TBM_EVENT_REGISTRY_UPCASTER_INVALID",
                "upcaster must be exactly EventPayloadUpcaster",
            )
        if len(self._upcasters) >= EVENT_REGISTRY_MAX_UPCASTERS:
            _fail(
                "TBM_EVENT_REGISTRY_LIMIT_EXCEEDED",
                "upcaster limit exceeded",
            )
        source_key = (upcaster.event_type, upcaster.from_version)
        target_key = (upcaster.event_type, upcaster.to_version)
        if source_key not in self._registrations or target_key not in self._registrations:
            _fail(
                "TBM_EVENT_REGISTRY_UPCASTER_INVALID",
                "upcaster endpoints must both be registered",
            )
        if source_key in self._upcasters:
            _fail(
                "TBM_EVENT_REGISTRY_DUPLICATE_UPCASTER",
                "upcaster source edge is already registered",
            )
        if upcaster.upcaster_id in self._upcaster_ids:
            _fail(
                "TBM_EVENT_REGISTRY_DUPLICATE_UPCASTER",
                "upcaster_id is already registered",
            )
        if (
            self._registrations[source_key].event_kind
            != self._registrations[target_key].event_kind
        ):
            _fail(
                "TBM_EVENT_REGISTRY_UPCASTER_INVALID",
                "upcaster cannot change event_kind",
            )
        self._upcasters[source_key] = upcaster
        self._upcaster_ids.add(upcaster.upcaster_id)

    def seal(self) -> EventTypeRegistry:
        if self._sealed:
            return self
        if not self._registrations:
            _fail(
                "TBM_EVENT_REGISTRY_EMPTY",
                "event registry cannot be sealed without registrations",
            )
        self._sealed = True
        return self

    def inspect(self, event: CanonicalEvent) -> EventRegistryResolution:
        self._require_sealed()
        if type(event) is not CanonicalEvent:
            _fail(
                "TBM_EVENT_REGISTRY_EVENT_INVALID",
                "event must be exactly CanonicalEvent",
            )
        registration = self._registrations.get(
            (event.event_type, event.event_version)
        )
        if registration is not None:
            return EventRegistryResolution(event, "known", registration)
        known_type = any(
            event_type == event.event_type
            for event_type, _ in self._registrations
        )
        status: EventRegistryResolutionStatus = (
            "unknown_version" if known_type else "unknown_type"
        )
        return EventRegistryResolution(event, status, None)

    def consume(
        self,
        event: CanonicalEvent,
        *,
        target_version: int | None = None,
    ) -> TypedEventView:
        resolution = self.inspect(event)
        if not resolution.consumable or resolution.registration is None:
            raise UnknownEventTypeError(event, resolution.status)
        source = resolution.registration
        if event.event_kind != source.event_kind:
            _fail(
                "TBM_EVENT_REGISTRY_PAYLOAD_INVALID",
                "event_kind does not match its typed registration",
            )
        if event.payload_schema != source.payload_schema:
            _fail(
                "TBM_EVENT_REGISTRY_PAYLOAD_INVALID",
                "payload_schema does not match its typed registration",
            )
        payload = _copy_payload(event.payload)
        _validate_payload(source.schema, payload, path="payload")
        target = event.event_version if target_version is None else target_version
        _positive_version(target, "target_version")
        if target < event.event_version:
            _fail(
                "TBM_EVENT_REGISTRY_UPCAST_UNSUPPORTED",
                "typed consumers cannot downcast events",
            )
        target_registration = self._registrations.get((event.event_type, target))
        if target_registration is None:
            _fail(
                "TBM_EVENT_REGISTRY_UPCAST_UNSUPPORTED",
                "target event version is not registered",
            )
        applied: list[str] = []
        current = event.event_version
        while current < target:
            upcaster = self._upcasters.get((event.event_type, current))
            if upcaster is None:
                _fail(
                    "TBM_EVENT_REGISTRY_UPCAST_UNSUPPORTED",
                    "required upcaster edge is not registered",
                )
            try:
                transformed = upcaster.transform(
                    cast(Mapping[str, object], _freeze_json(payload))
                )
            except EventRegistryV1Error:
                raise
            except Exception as error:
                raise EventRegistryV1Error(
                    "TBM_EVENT_REGISTRY_UPCAST_FAILED",
                    "event payload upcaster failed",
                ) from error
            payload = _copy_payload(transformed)
            current += 1
            step_registration = self._registrations[(event.event_type, current)]
            _validate_payload(step_registration.schema, payload, path="payload")
            applied.append(upcaster.upcaster_id)
        return TypedEventView(
            source_event=event,
            event_type=event.event_type,
            source_version=event.event_version,
            target_version=target,
            payload_schema=target_registration.payload_schema,
            payload=payload,
            applied_upcaster_ids=tuple(applied),
        )

    def compatibility_matrix(self) -> tuple[EventCompatibilityRow, ...]:
        self._require_sealed()
        rows: list[EventCompatibilityRow] = []
        by_type: dict[str, list[int]] = {}
        for event_type, version in self._registrations:
            by_type.setdefault(event_type, []).append(version)
        for event_type in sorted(by_type):
            versions = sorted(by_type[event_type])
            for source in versions:
                for target in versions:
                    compatibility: EventCompatibility
                    if source == target:
                        compatibility = "native"
                    elif source < target and self._has_upcast_path(
                        event_type, source, target
                    ):
                        compatibility = "upcast"
                    else:
                        compatibility = "unsupported"
                    rows.append(
                        EventCompatibilityRow(
                            event_type,
                            source,
                            target,
                            compatibility,
                        )
                    )
                    if len(rows) > EVENT_REGISTRY_MAX_COMPATIBILITY_ROWS:
                        _fail(
                            "TBM_EVENT_REGISTRY_LIMIT_EXCEEDED",
                            "compatibility matrix row limit exceeded",
                        )
        return tuple(rows)

    def catalog(self) -> dict[str, object]:
        self._require_sealed()
        unsigned: dict[str, object] = {
            "registry_version": EVENT_REGISTRY_PROTOCOL_VERSION,
            "event_types": [
                registration.to_dict()
                for _, registration in sorted(self._registrations.items())
            ],
            "upcasters": [
                upcaster.to_dict()
                for _, upcaster in sorted(self._upcasters.items())
            ],
            "compatibility": [
                row.to_dict() for row in self.compatibility_matrix()
            ],
        }
        registry_sha256 = _domain_sha256(
            b"tbm.event-registry.v1\x00",
            _canonical_json_bytes(unsigned),
        )
        return {
            "registry_version": EVENT_REGISTRY_PROTOCOL_VERSION,
            "registry_sha256": registry_sha256,
            "event_types": unsigned["event_types"],
            "upcasters": unsigned["upcasters"],
            "compatibility": unsigned["compatibility"],
        }

    def dispatch_schema(self) -> dict[str, object]:
        self._require_sealed()
        choices: list[dict[str, object]] = []
        for _, registration in sorted(self._registrations.items()):
            choices.append(
                {
                    "type": "object",
                    "additionalProperties": False,
                    "required": [
                        "event_type",
                        "event_version",
                        "payload_schema",
                        "payload",
                    ],
                    "properties": {
                        "event_type": {"const": registration.event_type},
                        "event_version": {"const": registration.event_version},
                        "payload_schema": {"const": registration.payload_schema},
                        "payload": _thaw_json(registration.schema),
                    },
                }
            )
        return {
            "$schema": "https://json-schema.org/draft/2020-12/schema",
            "$id": (
                "https://trace-backed-memory.dev/schemas/"
                "event_payload_registry_v1.schema.json"
            ),
            "title": "Trace-backed Memory registered event payloads v1",
            "$comment": (
                "Generated from the sealed tbm.event-registry.v1 catalog. "
                "The canonical event runtime remains the authoritative validator."
            ),
            "oneOf": choices,
        }

    def _has_upcast_path(
        self, event_type: str, source_version: int, target_version: int
    ) -> bool:
        current = source_version
        while current < target_version:
            if (event_type, current) not in self._upcasters:
                return False
            current += 1
        return True

    def _require_mutable(self) -> None:
        if self._sealed:
            _fail(
                "TBM_EVENT_REGISTRY_SEALED",
                "sealed event registry cannot be modified",
            )

    def _require_sealed(self) -> None:
        if not self._sealed:
            _fail(
                "TBM_EVENT_REGISTRY_NOT_SEALED",
                "event registry must be sealed before use",
            )


def validate_registered_event_payload(
    registration: EventPayloadRegistration,
    payload: Mapping[str, object],
) -> None:
    if type(registration) is not EventPayloadRegistration:
        _fail(
            "TBM_EVENT_REGISTRY_REGISTRATION_INVALID",
            "registration must be exactly EventPayloadRegistration",
        )
    copied = _copy_payload(payload)
    _validate_payload(registration.schema, copied, path="payload")


def dumps_event_registry_catalog(registry: EventTypeRegistry) -> str:
    if type(registry) is not EventTypeRegistry:
        _fail(
            "TBM_EVENT_REGISTRY_INVALID",
            "registry must be exactly EventTypeRegistry",
        )
    return _canonical_json_bytes(registry.catalog()).decode("utf-8")


def dumps_event_payload_dispatch_schema(registry: EventTypeRegistry) -> str:
    if type(registry) is not EventTypeRegistry:
        _fail(
            "TBM_EVENT_REGISTRY_INVALID",
            "registry must be exactly EventTypeRegistry",
        )
    return json.dumps(
        registry.dispatch_schema(),
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        indent=2,
    ) + "\n"


def build_default_event_type_registry() -> EventTypeRegistry:
    registry = EventTypeRegistry()
    registry.register(
        EventPayloadRegistration(
            event_type="tbm.memory.proposed",
            event_version=1,
            event_kind="domain",
            payload_schema="tbm.memory.proposed.v1",
            schema={
                "type": "object",
                "additionalProperties": False,
                "required": [
                    "memory_revision_id",
                    "proposal_kind",
                    "scope_id",
                ],
                "properties": {
                    "memory_revision_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                    "proposal_kind": {
                        "enum": ["failure_case", "lesson", "policy"],
                    },
                    "scope_id": {
                        "type": "string",
                        "minLength": 1,
                        "maxLength": 128,
                    },
                },
            },
        )
    )
    return registry.seal()


def _validate_schema_definition(
    schema: Mapping[str, object], *, path: str, depth: int
) -> None:
    if depth > EVENT_REGISTRY_MAX_SCHEMA_DEPTH:
        _fail(
            "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
            "event payload schema exceeds maximum depth",
        )
    unknown = set(schema) - _SCHEMA_ALLOWED_KEYS
    if unknown:
        _fail(
            "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
            "event payload schema contains unsupported keywords",
        )
    schema_type = schema.get("type")
    if "type" in schema and (
        type(schema_type) is not str or schema_type not in _SCHEMA_TYPES
    ):
        _fail(
            "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
            "event payload schema type is not supported",
        )
    for metadata_name, maximum in (("$id", 2048), ("$schema", 2048), ("title", 256)):
        metadata = schema.get(metadata_name)
        if metadata_name in schema and (
            type(metadata) is not str or not 1 <= len(metadata) <= maximum
        ):
            _fail(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "event payload schema metadata is invalid",
            )
    object_keywords = {"additionalProperties", "properties", "required"}
    array_keywords = {"items", "minItems", "maxItems", "uniqueItems"}
    string_keywords = {"minLength", "maxLength", "pattern"}
    number_keywords = {"minimum", "maximum"}
    if set(schema) & object_keywords and schema_type != "object":
        _fail(
            "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
            "object keywords require object type",
        )
    if set(schema) & array_keywords and schema_type != "array":
        _fail(
            "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
            "array keywords require array type",
        )
    if set(schema) & string_keywords and schema_type != "string":
        _fail(
            "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
            "string keywords require string type",
        )
    if set(schema) & number_keywords and schema_type not in {"integer", "number"}:
        _fail(
            "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
            "numeric keywords require integer or number type",
        )
    if schema_type == "string" and "pattern" in schema:
        pattern = schema["pattern"]
        if type(pattern) is not str or len(pattern) > 512:
            _fail(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "event payload schema pattern is invalid",
            )
        try:
            re.compile(pattern)
        except re.error as error:
            raise EventRegistryV1Error(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "event payload schema pattern is invalid",
            ) from error
    if "enum" in schema:
        enum = schema["enum"]
        if type(enum) is not list or not enum or len(enum) > 256:
            _fail(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "event payload schema enum is invalid",
            )
        encoded = [_canonical_json_bytes(item) for item in enum]
        if len(encoded) != len(set(encoded)):
            _fail(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "event payload schema enum must be unique",
            )
    if schema_type == "object":
        if schema.get("additionalProperties") is not False:
            _fail(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "object schemas must reject additional properties",
            )
        properties = schema.get("properties")
        required = schema.get("required")
        if (
            not isinstance(properties, Mapping)
            or len(properties) > 512
            or type(required) is not list
            or len(required) > 512
        ):
            _fail(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "object schemas require properties and required",
            )
        if (
            any(type(item) is not str for item in required)
            or len(required) != len(set(required))
            or set(required) - set(properties)
        ):
            _fail(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "object schema required fields are invalid",
            )
        for key, child in properties.items():
            if (
                type(key) is not str
                or not 1 <= len(key) <= 256
                or not isinstance(child, Mapping)
            ):
                _fail(
                    "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                    "object schema properties are invalid",
                )
            try:
                validate_event_payload({key: None})
            except EventV1ContractError as error:
                raise EventRegistryV1Error(
                    "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                    "object schema property violates canonical payload policy",
                ) from error
            _validate_schema_definition(
                cast(Mapping[str, object], child),
                path=f"{path}.{key}",
                depth=depth + 1,
            )
    if schema_type == "array":
        items = schema.get("items")
        if not isinstance(items, Mapping):
            _fail(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "array schemas require one item schema",
            )
        _validate_schema_definition(
            cast(Mapping[str, object], items),
            path=f"{path}[]",
            depth=depth + 1,
        )
        minimum_items = schema.get("minItems")
        maximum_items = schema.get("maxItems")
        for name, bound in (
            ("minItems", minimum_items),
            ("maxItems", maximum_items),
        ):
            if name in schema and (
                type(bound) is not int or not 0 <= bound <= 8192
            ):
                _fail(
                    "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                    "array cardinality bound is invalid",
                )
        if (
            "minItems" in schema
            and "maxItems" in schema
            and cast(int, minimum_items) > cast(int, maximum_items)
        ):
            _fail(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "array minimum exceeds maximum",
            )
        if "uniqueItems" in schema and type(schema["uniqueItems"]) is not bool:
            _fail(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "uniqueItems must be a boolean",
            )
    if schema_type == "string":
        minimum_length = schema.get("minLength")
        maximum_length = schema.get("maxLength")
        for name, bound in (
            ("minLength", minimum_length),
            ("maxLength", maximum_length),
        ):
            if name in schema and (
                type(bound) is not int or not 0 <= bound <= 1_048_576
            ):
                _fail(
                    "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                    "string length bound is invalid",
                )
        if (
            "minLength" in schema
            and "maxLength" in schema
            and cast(int, minimum_length) > cast(int, maximum_length)
        ):
            _fail(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "string minimum exceeds maximum",
            )
    if schema_type in {"integer", "number"}:
        minimum = schema.get("minimum")
        maximum = schema.get("maximum")
        for name, bound in (("minimum", minimum), ("maximum", maximum)):
            if name in schema and (
                type(bound) not in {int, float} or not math.isfinite(bound)
            ):
                _fail(
                    "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                    "numeric bound is invalid",
                )
        if (
            "minimum" in schema
            and "maximum" in schema
            and cast(int | float, minimum) > cast(int | float, maximum)
        ):
            _fail(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "schema minimum exceeds maximum",
            )
    if "oneOf" in schema:
        choices = schema["oneOf"]
        if type(choices) is not list or not 1 <= len(choices) <= 32:
            _fail(
                "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                "oneOf must be a bounded non-empty array",
            )
        for choice in choices:
            if not isinstance(choice, Mapping):
                _fail(
                    "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
                    "oneOf entries must be schemas",
                )
            _validate_schema_definition(
                cast(Mapping[str, object], choice),
                path=path,
                depth=depth + 1,
            )


def _validate_payload(
    schema: Mapping[str, object], value: object, *, path: str
) -> None:
    if "oneOf" in schema:
        choices = cast(list[object], schema["oneOf"])
        matches = 0
        for choice in choices:
            try:
                _validate_payload(
                    cast(Mapping[str, object], choice), value, path=path
                )
            except EventRegistryV1Error:
                continue
            matches += 1
        if matches != 1:
            _payload_invalid("payload does not match exactly one schema choice")
    if "const" in schema and value != schema["const"]:
        _payload_invalid("payload value does not match schema const")
    if "enum" in schema and value not in cast(list[object], schema["enum"]):
        _payload_invalid("payload value is not in schema enum")
    schema_type = schema.get("type")
    if schema_type == "object":
        if not isinstance(value, Mapping):
            _payload_invalid("payload field must be an object")
        properties = cast(Mapping[str, object], schema["properties"])
        required = cast(list[str], schema["required"])
        if not set(required).issubset(value):
            _payload_invalid("payload object is missing required fields")
        if set(value) - set(properties):
            _payload_invalid("payload object contains unknown fields")
        for key, child in value.items():
            _validate_payload(
                cast(Mapping[str, object], properties[key]),
                child,
                path=f"{path}.{key}",
            )
    elif schema_type == "array":
        if type(value) not in {list, tuple}:
            _payload_invalid("payload field must be an array")
        items = cast(Mapping[str, object], schema["items"])
        minimum = cast(int | None, schema.get("minItems"))
        maximum = cast(int | None, schema.get("maxItems"))
        if minimum is not None and len(value) < minimum:
            _payload_invalid("payload array is shorter than allowed")
        if maximum is not None and len(value) > maximum:
            _payload_invalid("payload array is longer than allowed")
        if schema.get("uniqueItems") is True:
            encoded = [_canonical_json_bytes(item) for item in value]
            if len(encoded) != len(set(encoded)):
                _payload_invalid("payload array items must be unique")
        for item in value:
            _validate_payload(items, item, path=f"{path}[]")
    elif schema_type == "string":
        if type(value) is not str:
            _payload_invalid("payload field must be a string")
        minimum = cast(int | None, schema.get("minLength"))
        maximum = cast(int | None, schema.get("maxLength"))
        if minimum is not None and len(value) < minimum:
            _payload_invalid("payload string is shorter than allowed")
        if maximum is not None and len(value) > maximum:
            _payload_invalid("payload string is longer than allowed")
        pattern = cast(str | None, schema.get("pattern"))
        if pattern is not None and re.fullmatch(pattern, value) is None:
            _payload_invalid("payload string does not match schema pattern")
    elif schema_type == "integer":
        if type(value) is not int:
            _payload_invalid("payload field must be an integer")
        _validate_number_bounds(schema, value)
    elif schema_type == "number":
        if type(value) not in {int, float} or not math.isfinite(value):
            _payload_invalid("payload field must be a finite number")
        _validate_number_bounds(schema, value)
    elif schema_type == "boolean" and type(value) is not bool:
        _payload_invalid("payload field must be a boolean")
    elif schema_type == "null" and value is not None:
        _payload_invalid("payload field must be null")


def _validate_number_bounds(schema: Mapping[str, object], value: int | float) -> None:
    minimum = cast(int | float | None, schema.get("minimum"))
    maximum = cast(int | float | None, schema.get("maximum"))
    if minimum is not None and value < minimum:
        _payload_invalid("payload number is lower than allowed")
    if maximum is not None and value > maximum:
        _payload_invalid("payload number is higher than allowed")


def _copy_json_schema(schema: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(schema, Mapping):
        _fail(
            "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
            "event payload schema must be an object",
        )
    copied = _copy_json_value(
        schema,
        max_depth=EVENT_REGISTRY_MAX_SCHEMA_DEPTH,
        max_nodes=EVENT_REGISTRY_MAX_SCHEMA_NODES,
        label="schema",
        error_code="TBM_EVENT_REGISTRY_SCHEMA_INVALID",
    )
    if type(copied) is not dict:
        _fail(
            "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
            "event payload schema must be an object",
        )
    encoded = _canonical_json_bytes(copied)
    if len(encoded) > EVENT_REGISTRY_MAX_SCHEMA_BYTES:
        _fail(
            "TBM_EVENT_REGISTRY_SCHEMA_INVALID",
            "event payload schema exceeds byte limit",
        )
    return cast(dict[str, object], copied)


def _copy_payload(payload: Mapping[str, object]) -> dict[str, object]:
    if not isinstance(payload, Mapping):
        _payload_invalid("typed event payload must be an object")
    copied = _copy_json_value(
        payload,
        max_depth=24,
        max_nodes=8192,
        label="payload",
        error_code="TBM_EVENT_REGISTRY_PAYLOAD_INVALID",
    )
    if type(copied) is not dict:
        _payload_invalid("typed event payload must be an object")
    result = cast(dict[str, object], copied)
    try:
        validate_event_payload(result)
    except EventV1ContractError as error:
        raise EventRegistryV1Error(
            "TBM_EVENT_REGISTRY_PAYLOAD_INVALID",
            "typed payload violates canonical event payload policy",
        ) from error
    return result


def _copy_json_value(
    value: object,
    *,
    max_depth: int,
    max_nodes: int,
    label: str,
    error_code: str,
) -> object:
    active: set[int] = set()
    nodes = 0

    def copy(item: object, depth: int) -> object:
        nonlocal nodes
        if depth > max_depth:
            _fail(
                error_code,
                f"{label} exceeds maximum depth",
            )
        nodes += 1
        if nodes > max_nodes:
            _fail(
                error_code,
                f"{label} exceeds maximum node count",
            )
        if isinstance(item, Mapping):
            identity = id(item)
            if identity in active:
                _fail(
                    error_code,
                    f"{label} contains a cycle",
                )
            active.add(identity)
            try:
                result: dict[str, object] = {}
                for key, child in item.items():
                    if type(key) is not str or not key or len(key) > 256:
                        _fail(
                            error_code,
                            f"{label} keys must be bounded strings",
                        )
                    result[key] = copy(child, depth + 1)
                return result
            finally:
                active.remove(identity)
        if type(item) in {list, tuple}:
            identity = id(item)
            if identity in active:
                _fail(
                    error_code,
                    f"{label} contains a cycle",
                )
            active.add(identity)
            try:
                return [copy(child, depth + 1) for child in item]
            finally:
                active.remove(identity)
        if item is None or type(item) in {bool, int, str}:
            return item
        if type(item) is float and math.isfinite(item):
            return item
        _fail(
            error_code,
            f"{label} contains a non-JSON value",
        )

    return copy(value, 0)


def _freeze_json(value: object) -> object:
    if type(value) is dict:
        return MappingProxyType(
            {key: _freeze_json(item) for key, item in value.items()}
        )
    if type(value) is list:
        return tuple(_freeze_json(item) for item in value)
    return value


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) is tuple:
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
        raise EventRegistryV1Error(
            "TBM_EVENT_REGISTRY_NON_CANONICAL_JSON",
            "event registry value is not canonical JSON",
        ) from error


def _domain_sha256(domain: bytes, value: bytes) -> str:
    return "sha256:" + hashlib.sha256(domain + value).hexdigest()


def _event_type(value: object) -> None:
    if type(value) is not str or _EVENT_TYPE_RE.fullmatch(value) is None:
        _fail(
            "TBM_EVENT_REGISTRY_REGISTRATION_INVALID",
            "event_type is not canonical",
        )


def _identifier(value: object, name: str) -> None:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _fail(
            "TBM_EVENT_REGISTRY_REGISTRATION_INVALID",
            f"{name} is not a bounded identifier",
        )


def _positive_version(value: object, name: str) -> None:
    if type(value) is not int or not 1 <= value <= 2_147_483_647:
        _fail(
            "TBM_EVENT_REGISTRY_REGISTRATION_INVALID",
            f"{name} must be a bounded positive integer",
        )


def _payload_invalid(message: str) -> NoReturn:
    _fail("TBM_EVENT_REGISTRY_PAYLOAD_INVALID", message)


def _fail(code: str, message: str) -> NoReturn:
    raise EventRegistryV1Error(code, message)


DEFAULT_EVENT_TYPE_REGISTRY = build_default_event_type_registry()


__all__ = [
    "DEFAULT_EVENT_TYPE_REGISTRY",
    "EVENT_REGISTRY_MAX_SCHEMA_BYTES",
    "EVENT_REGISTRY_MAX_SCHEMA_DEPTH",
    "EVENT_REGISTRY_MAX_SCHEMA_NODES",
    "EVENT_REGISTRY_MAX_COMPATIBILITY_ROWS",
    "EVENT_REGISTRY_MAX_TYPES",
    "EVENT_REGISTRY_MAX_UPCASTERS",
    "EVENT_REGISTRY_MAX_VERSIONS_PER_TYPE",
    "EVENT_REGISTRY_PROTOCOL_VERSION",
    "EventCompatibility",
    "EventCompatibilityRow",
    "EventPayloadRegistration",
    "EventPayloadUpcaster",
    "EventRegistryResolution",
    "EventRegistryResolutionStatus",
    "EventRegistryV1Error",
    "EventTypeRegistry",
    "PayloadUpcaster",
    "TypedEventView",
    "UnknownEventTypeError",
    "build_default_event_type_registry",
    "dumps_event_payload_dispatch_schema",
    "dumps_event_registry_catalog",
    "validate_registered_event_payload",
]
