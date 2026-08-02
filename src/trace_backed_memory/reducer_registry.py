from __future__ import annotations

from dataclasses import dataclass
import hashlib
import json
from typing import NoReturn

from .contracts_v3 import V3ContractError
from .effect_reducer_v1 import build_effect_queue_reducer
from .finalization_reducer_v1 import build_finalization_reducer
from .gate_evidence_reducer_v1 import build_gate_evidence_reducer
from .gate_session_reducer_v1 import build_gate_session_reducer
from .outcome_reducer_v1 import (
    build_outcome_attribution_reducer,
    build_outcome_current_reducer,
)
from .semantic_gate_attempt_reducer_v1 import (
    build_semantic_gate_attempt_reducer,
)
from .reducer import (
    REDUCER_PROTOCOL_VERSION,
    Reducer,
    ReducerDescriptor,
    build_event_inventory_reducer,
)


REDUCER_REGISTRY_PROTOCOL_VERSION = "tbm.reducer-registry.v1"
REDUCER_REGISTRY_MAX_REDUCERS = 256


class ReducerRegistryError(V3ContractError):
    """Stable reducer registration, lookup, or compatibility failure."""


@dataclass(frozen=True)
class ReducerRegistryEntry:
    descriptor: ReducerDescriptor
    reducer: Reducer

    def __post_init__(self) -> None:
        if type(self.descriptor) is not ReducerDescriptor:
            _fail(
                "TBM_REDUCER_REGISTRY_INVALID",
                "entry descriptor must be exactly ReducerDescriptor",
            )
        if self.reducer.descriptor != self.descriptor:
            _fail(
                "TBM_REDUCER_REGISTRY_INVALID",
                "entry reducer does not match its descriptor",
            )


class ReducerRegistry:
    """Mutable-until-sealed registry for explicit reducer versions."""

    def __init__(self) -> None:
        self._entries: dict[tuple[str, int], ReducerRegistryEntry] = {}
        self._projection_versions: set[tuple[str, int]] = set()
        self._sealed = False

    @property
    def sealed(self) -> bool:
        return self._sealed

    def register(self, reducer: Reducer) -> None:
        self._require_mutable()
        if len(self._entries) >= REDUCER_REGISTRY_MAX_REDUCERS:
            _fail(
                "TBM_REDUCER_REGISTRY_LIMIT_EXCEEDED",
                "reducer registry limit exceeded",
            )
        try:
            descriptor = reducer.descriptor
        except Exception as error:
            raise ReducerRegistryError(
                "TBM_REDUCER_REGISTRY_INVALID",
                "reducer descriptor is unavailable",
            ) from error
        if type(descriptor) is not ReducerDescriptor:
            _fail(
                "TBM_REDUCER_REGISTRY_INVALID",
                "reducer descriptor must be exactly ReducerDescriptor",
            )
        key = (descriptor.reducer_id, descriptor.reducer_version)
        if key in self._entries:
            _fail(
                "TBM_REDUCER_REGISTRY_DUPLICATE",
                "reducer ID and version are already registered",
            )
        projection_key = (
            descriptor.output_projection,
            descriptor.reducer_version,
        )
        if projection_key in self._projection_versions:
            _fail(
                "TBM_REDUCER_REGISTRY_PROJECTION_CONFLICT",
                "projection and reducer version are already owned",
            )
        self._entries[key] = ReducerRegistryEntry(descriptor, reducer)
        self._projection_versions.add(projection_key)

    def seal(self) -> ReducerRegistry:
        if self._sealed:
            return self
        if not self._entries:
            _fail(
                "TBM_REDUCER_REGISTRY_EMPTY",
                "reducer registry cannot be sealed without reducers",
            )
        self._sealed = True
        return self

    def resolve(
        self,
        reducer_id: str,
        reducer_version: int | None = None,
        *,
        expected_code_sha256: str | None = None,
        expected_configuration_sha256: str | None = None,
    ) -> Reducer:
        self._require_sealed()
        candidates = [
            entry
            for (registered_id, _), entry in self._entries.items()
            if registered_id == reducer_id
        ]
        if reducer_version is None:
            if not candidates:
                _not_found()
            entry = max(
                candidates,
                key=lambda item: item.descriptor.reducer_version,
            )
        else:
            if type(reducer_version) is not int or reducer_version < 1:
                _fail(
                    "TBM_REDUCER_REGISTRY_LOOKUP_INVALID",
                    "reducer_version is invalid",
                )
            entry = self._entries.get((reducer_id, reducer_version))
            if entry is None:
                _not_found()
        descriptor = entry.descriptor
        if (
            expected_code_sha256 is not None
            and descriptor.code_sha256 != expected_code_sha256
        ):
            _fail(
                "TBM_REDUCER_CODE_HASH_MISMATCH",
                "registered reducer code hash does not match",
            )
        if (
            expected_configuration_sha256 is not None
            and descriptor.configuration_sha256
            != expected_configuration_sha256
        ):
            _fail(
                "TBM_REDUCER_CONFIGURATION_HASH_MISMATCH",
                "registered reducer configuration hash does not match",
            )
        return entry.reducer

    def descriptors(self) -> tuple[ReducerDescriptor, ...]:
        self._require_sealed()
        return tuple(
            entry.descriptor
            for _, entry in sorted(self._entries.items())
        )

    def catalog(self) -> dict[str, object]:
        descriptors = [
            descriptor.to_dict()
            for descriptor in self.descriptors()
        ]
        unsigned: dict[str, object] = {
            "registry_version": REDUCER_REGISTRY_PROTOCOL_VERSION,
            "reducer_protocol_version": REDUCER_PROTOCOL_VERSION,
            "reducers": descriptors,
        }
        return {
            **unsigned,
            "registry_sha256": _domain_sha256(
                b"tbm.reducer-registry.v1\x00",
                unsigned,
            ),
        }

    @property
    def registry_sha256(self) -> str:
        value = self.catalog()["registry_sha256"]
        if type(value) is not str:
            raise AssertionError("registry digest is not a string")
        return value

    def _require_mutable(self) -> None:
        if self._sealed:
            _fail(
                "TBM_REDUCER_REGISTRY_SEALED",
                "reducer registry is sealed",
            )

    def _require_sealed(self) -> None:
        if not self._sealed:
            _fail(
                "TBM_REDUCER_REGISTRY_UNSEALED",
                "reducer registry must be sealed before use",
            )


def build_default_reducer_registry() -> ReducerRegistry:
    registry = ReducerRegistry()
    registry.register(build_event_inventory_reducer())
    registry.register(build_gate_evidence_reducer())
    registry.register(build_gate_session_reducer())
    registry.register(build_semantic_gate_attempt_reducer())
    registry.register(build_finalization_reducer())
    registry.register(build_outcome_current_reducer())
    registry.register(build_outcome_attribution_reducer())
    registry.register(build_effect_queue_reducer())
    return registry.seal()


DEFAULT_REDUCER_REGISTRY = build_default_reducer_registry()


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
        raise ReducerRegistryError(
            "TBM_REDUCER_REGISTRY_INVALID",
            "reducer registry is not canonical JSON",
        ) from error


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_json_bytes(value)).hexdigest()


def _not_found() -> NoReturn:
    _fail(
        "TBM_REDUCER_NOT_FOUND",
        "requested reducer ID or version is not registered",
    )


def _fail(code: str, message: str) -> NoReturn:
    raise ReducerRegistryError(code, message)


__all__ = [
    "DEFAULT_REDUCER_REGISTRY",
    "REDUCER_REGISTRY_MAX_REDUCERS",
    "REDUCER_REGISTRY_PROTOCOL_VERSION",
    "ReducerRegistry",
    "ReducerRegistryEntry",
    "ReducerRegistryError",
    "build_default_reducer_registry",
]
