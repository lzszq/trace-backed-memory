from __future__ import annotations

from collections.abc import Mapping
from dataclasses import dataclass
import json
from pathlib import Path
import re
from typing import Any, Literal, cast

from ._ingestion import (
    SNAPSHOT_FILE_MAX_BYTES,
    read_bounded_utf8,
    unique_json_object_pairs,
)
from .contracts_v3 import (
    V3_MIGRATION_MAPPING_VERSION,
    V3_MIGRATION_PLAN_VERSION,
    V3_SOURCE_SNAPSHOT_VERSION,
    V3_TARGET_SNAPSHOT_VERSION,
    CommitRelationVerifier,
    SnapshotV3MigrationMapping,
    SnapshotV3MigrationPlan,
    V3ContractError,
    canonical_sha256,
    parse_v3_migration_mapping,
    parse_v3_migration_plan,
    plan_snapshot_v3_migration,
)
from .store import TraceBackedMemoryStore


V3_MIGRATION_BUNDLE_VERSION = "tbm.snapshot.v2-to-v3.bundle.v1"
V3_MIGRATION_BUNDLE_MAX_BYTES = 128 * 1024 * 1024
V3_MIGRATION_BUNDLE_MAX_NODES = 2_000_000
V3_MIGRATION_BUNDLE_MAX_DEPTH = 100
V3_MIGRATION_MAPPING_MAX_BYTES = 64 * 1024 * 1024
V3_MIGRATION_PLAN_MAX_BYTES = 64 * 1024 * 1024

V3MigrationBundleState = Literal["blocked", "ready"]
_BUNDLE_STATES = {"blocked", "ready"}
_SHA256_RE = re.compile(r"sha256:[0-9a-f]{64}")
_BUNDLE_FIELDS = {
    "bundle_version",
    "bundle_id",
    "state",
    "source_snapshot_version",
    "target_snapshot_version",
    "source_snapshot_sha256",
    "normalized_source_snapshot_sha256",
    "mapping_sha256",
    "plan_sha256",
    "source_snapshot",
    "mapping",
    "plan",
}


class V3MigrationBundleError(V3ContractError):
    """Stable failure raised for malformed or unverifiable migration bundles."""


@dataclass(frozen=True)
class SnapshotV3MigrationBundle:
    bundle_id: str
    state: V3MigrationBundleState
    source_snapshot_sha256: str
    normalized_source_snapshot_sha256: str
    mapping_sha256: str
    plan_sha256: str
    mapping: SnapshotV3MigrationMapping
    plan: SnapshotV3MigrationPlan
    _source_snapshot_json: str
    bundle_version: str = V3_MIGRATION_BUNDLE_VERSION
    source_snapshot_version: int = V3_SOURCE_SNAPSHOT_VERSION
    target_snapshot_version: int = V3_TARGET_SNAPSHOT_VERSION

    def __post_init__(self) -> None:
        if self.bundle_version != V3_MIGRATION_BUNDLE_VERSION:
            _invalid_bundle(
                "TBM_V3_BUNDLE_VERSION_MISMATCH",
                f"bundle_version must be {V3_MIGRATION_BUNDLE_VERSION}",
            )
        if (
            type(self.source_snapshot_version) is not int
            or self.source_snapshot_version != V3_SOURCE_SNAPSHOT_VERSION
        ):
            _invalid_bundle(
                "TBM_V3_BUNDLE_SOURCE_VERSION_MISMATCH",
                "bundle source snapshot version must be 2",
            )
        if (
            type(self.target_snapshot_version) is not int
            or self.target_snapshot_version != V3_TARGET_SNAPSHOT_VERSION
        ):
            _invalid_bundle(
                "TBM_V3_BUNDLE_TARGET_VERSION_MISMATCH",
                "bundle target snapshot version must be 3",
            )
        if type(self.state) is not str or self.state not in _BUNDLE_STATES:
            _invalid_bundle(
                "TBM_V3_INVALID_BUNDLE",
                "bundle state must be blocked or ready",
            )
        for field_name in (
            "bundle_id",
            "source_snapshot_sha256",
            "normalized_source_snapshot_sha256",
            "mapping_sha256",
            "plan_sha256",
        ):
            _bundle_digest(getattr(self, field_name), field_name)
        if type(self.mapping) is not SnapshotV3MigrationMapping:
            _invalid_bundle(
                "TBM_V3_INVALID_BUNDLE",
                "bundle mapping must be exactly SnapshotV3MigrationMapping",
            )
        if type(self.plan) is not SnapshotV3MigrationPlan:
            _invalid_bundle(
                "TBM_V3_INVALID_BUNDLE",
                "bundle plan must be exactly SnapshotV3MigrationPlan",
            )
        if type(self._source_snapshot_json) is not str:
            _invalid_bundle(
                "TBM_V3_INVALID_BUNDLE",
                "bundle source snapshot document must be a string",
            )

        source_snapshot = _parse_json_object(
            self._source_snapshot_json,
            description="v3 migration bundle source snapshot",
            max_bytes=SNAPSHOT_FILE_MAX_BYTES,
            max_nodes=V3_MIGRATION_BUNDLE_MAX_NODES,
        )
        if (
            type(source_snapshot.get("snapshot_version")) is not int
            or source_snapshot["snapshot_version"]
            != V3_SOURCE_SNAPSHOT_VERSION
        ):
            _invalid_bundle(
                "TBM_V3_BUNDLE_SOURCE_VERSION_MISMATCH",
                "bundle must embed an explicit version-2 source snapshot",
            )
        try:
            source_store = TraceBackedMemoryStore.from_snapshot(source_snapshot)
        except (
            TypeError,
            ValueError,
            OverflowError,
            RecursionError,
        ) as error:
            raise V3MigrationBundleError(
                "TBM_V3_INVALID_BUNDLE_SOURCE",
                "bundle source snapshot failed strict version-2 validation",
            ) from error
        if _bundle_sha256(source_snapshot) != self.source_snapshot_sha256:
            _invalid_bundle(
                "TBM_V3_BUNDLE_SOURCE_HASH_MISMATCH",
                "bundle source snapshot digest does not match its document",
            )
        if (
            _bundle_sha256(source_store.to_snapshot())
            != self.normalized_source_snapshot_sha256
        ):
            _invalid_bundle(
                "TBM_V3_BUNDLE_NORMALIZED_SOURCE_HASH_MISMATCH",
                "bundle normalized source digest does not match its document",
            )
        if _bundle_sha256(self.mapping.to_dict()) != self.mapping_sha256:
            _invalid_bundle(
                "TBM_V3_BUNDLE_MAPPING_HASH_MISMATCH",
                "bundle mapping digest does not match its document",
            )
        if _bundle_sha256(self.plan.to_dict()) != self.plan_sha256:
            _invalid_bundle(
                "TBM_V3_BUNDLE_PLAN_HASH_MISMATCH",
                "bundle plan digest does not match its document",
            )
        if (
            self.plan.plan_version != V3_MIGRATION_PLAN_VERSION
            or self.mapping.mapping_version != V3_MIGRATION_MAPPING_VERSION
            or self.plan.source_snapshot_version
            != self.source_snapshot_version
            or self.plan.target_snapshot_version
            != self.target_snapshot_version
            or self.plan.source_snapshot_sha256
            != self.source_snapshot_sha256
            or self.plan.mapping_sha256 != self.mapping_sha256
        ):
            _invalid_bundle(
                "TBM_V3_BUNDLE_DOCUMENT_MISMATCH",
                "bundle versions and embedded document digests must agree",
            )
        expected_state = "ready" if self.plan.ready else "blocked"
        if self.state != expected_state:
            _invalid_bundle(
                "TBM_V3_BUNDLE_STATE_MISMATCH",
                "bundle state must match the embedded plan readiness",
            )
        if self.bundle_id != _bundle_id(
            state=self.state,
            source_snapshot_sha256=self.source_snapshot_sha256,
            normalized_source_snapshot_sha256=(
                self.normalized_source_snapshot_sha256
            ),
            mapping_sha256=self.mapping_sha256,
            plan_sha256=self.plan_sha256,
        ):
            _invalid_bundle(
                "TBM_V3_BUNDLE_ID_MISMATCH",
                "bundle_id does not match the immutable bundle manifest",
            )
        _bounded_json(
            self.to_dict(),
            description="v3 migration bundle",
            max_bytes=V3_MIGRATION_BUNDLE_MAX_BYTES,
        )

    @property
    def source_snapshot(self) -> dict[str, object]:
        return _parse_json_object(
            self._source_snapshot_json,
            description="v3 migration bundle source snapshot",
            max_bytes=SNAPSHOT_FILE_MAX_BYTES,
            max_nodes=V3_MIGRATION_BUNDLE_MAX_NODES,
        )

    @property
    def ready(self) -> bool:
        return self.state == "ready"

    def to_dict(self) -> dict[str, object]:
        return {
            "bundle_version": self.bundle_version,
            "bundle_id": self.bundle_id,
            "state": self.state,
            "source_snapshot_version": self.source_snapshot_version,
            "target_snapshot_version": self.target_snapshot_version,
            "source_snapshot_sha256": self.source_snapshot_sha256,
            "normalized_source_snapshot_sha256": (
                self.normalized_source_snapshot_sha256
            ),
            "mapping_sha256": self.mapping_sha256,
            "plan_sha256": self.plan_sha256,
            "source_snapshot": self.source_snapshot,
            "mapping": self.mapping.to_dict(),
            "plan": self.plan.to_dict(),
        }


def create_snapshot_v3_migration_bundle(
    source: TraceBackedMemoryStore | Mapping[str, object],
    mapping: SnapshotV3MigrationMapping | Mapping[str, object],
    *,
    commit_relation_verifier: CommitRelationVerifier | None = None,
) -> SnapshotV3MigrationBundle:
    """Freeze one v2 source and its v3 preflight into an inert bundle."""

    source_snapshot = _freeze_source_snapshot(source)
    mapping_record = _freeze_mapping(mapping)
    plan = plan_snapshot_v3_migration(
        source_snapshot,
        mapping_record,
        commit_relation_verifier=commit_relation_verifier,
    )
    source_store = TraceBackedMemoryStore.from_snapshot(source_snapshot)
    source_snapshot_sha256 = _bundle_sha256(source_snapshot)
    normalized_source_snapshot_sha256 = _bundle_sha256(
        source_store.to_snapshot()
    )
    mapping_sha256 = _bundle_sha256(mapping_record.to_dict())
    plan_sha256 = _bundle_sha256(plan.to_dict())
    state: V3MigrationBundleState = "ready" if plan.ready else "blocked"
    bundle_id = _bundle_id(
        state=state,
        source_snapshot_sha256=source_snapshot_sha256,
        normalized_source_snapshot_sha256=normalized_source_snapshot_sha256,
        mapping_sha256=mapping_sha256,
        plan_sha256=plan_sha256,
    )
    return SnapshotV3MigrationBundle(
        bundle_id=bundle_id,
        state=state,
        source_snapshot_sha256=source_snapshot_sha256,
        normalized_source_snapshot_sha256=(
            normalized_source_snapshot_sha256
        ),
        mapping_sha256=mapping_sha256,
        plan_sha256=plan_sha256,
        mapping=mapping_record,
        plan=plan,
        _source_snapshot_json=_canonical_json(source_snapshot),
    )


def parse_snapshot_v3_migration_bundle(
    payload: Mapping[str, object],
) -> SnapshotV3MigrationBundle:
    """Parse and integrity-check a materialized bundle object."""

    root = _closed_object(payload, "v3 migration bundle", _BUNDLE_FIELDS)
    source_snapshot = root["source_snapshot"]
    if type(source_snapshot) is not dict:
        _invalid_bundle(
            "TBM_V3_INVALID_BUNDLE",
            "bundle source_snapshot must be an object",
        )
    mapping_payload = root["mapping"]
    if type(mapping_payload) is not dict:
        _invalid_bundle(
            "TBM_V3_INVALID_BUNDLE",
            "bundle mapping must be an object",
        )
    plan_payload = root["plan"]
    if type(plan_payload) is not dict:
        _invalid_bundle(
            "TBM_V3_INVALID_BUNDLE",
            "bundle plan must be an object",
        )
    try:
        mapping = parse_v3_migration_mapping(mapping_payload)
        plan = parse_v3_migration_plan(plan_payload)
    except V3ContractError as error:
        raise V3MigrationBundleError(
            error.code,
            str(error),
        ) from error
    return SnapshotV3MigrationBundle(
        bundle_version=_required_string(
            root["bundle_version"],
            "bundle_version",
        ),
        bundle_id=_required_string(root["bundle_id"], "bundle_id"),
        state=cast(
            V3MigrationBundleState,
            _required_string(root["state"], "state"),
        ),
        source_snapshot_version=_required_integer(
            root["source_snapshot_version"],
            "source_snapshot_version",
        ),
        target_snapshot_version=_required_integer(
            root["target_snapshot_version"],
            "target_snapshot_version",
        ),
        source_snapshot_sha256=_required_string(
            root["source_snapshot_sha256"],
            "source_snapshot_sha256",
        ),
        normalized_source_snapshot_sha256=_required_string(
            root["normalized_source_snapshot_sha256"],
            "normalized_source_snapshot_sha256",
        ),
        mapping_sha256=_required_string(
            root["mapping_sha256"],
            "mapping_sha256",
        ),
        plan_sha256=_required_string(
            root["plan_sha256"],
            "plan_sha256",
        ),
        mapping=mapping,
        plan=plan,
        _source_snapshot_json=_canonical_json(source_snapshot),
    )


def loads_snapshot_v3_migration_bundle(
    source: str,
) -> SnapshotV3MigrationBundle:
    payload = _parse_json_object(
        source,
        description="v3 migration bundle",
        max_bytes=V3_MIGRATION_BUNDLE_MAX_BYTES,
        max_nodes=V3_MIGRATION_BUNDLE_MAX_NODES,
    )
    return parse_snapshot_v3_migration_bundle(payload)


def load_snapshot_v3_migration_bundle(
    path: str | Path,
) -> SnapshotV3MigrationBundle:
    try:
        source = read_bounded_utf8(
            path,
            max_bytes=V3_MIGRATION_BUNDLE_MAX_BYTES,
            description="v3 migration bundle",
        )
    except (OSError, UnicodeError, ValueError) as error:
        raise V3MigrationBundleError(
            "TBM_V3_BUNDLE_READ_FAILED",
            "failed to read bounded UTF-8 v3 migration bundle",
        ) from error
    return loads_snapshot_v3_migration_bundle(source)


def dumps_snapshot_v3_migration_bundle(
    bundle: SnapshotV3MigrationBundle,
) -> str:
    if type(bundle) is not SnapshotV3MigrationBundle:
        _invalid_bundle(
            "TBM_V3_INVALID_BUNDLE",
            "bundle must be exactly SnapshotV3MigrationBundle",
        )
    encoded = json.dumps(
        bundle.to_dict(),
        allow_nan=False,
        ensure_ascii=False,
        indent=2,
        sort_keys=True,
    ) + "\n"
    _bounded_text(
        encoded,
        description="v3 migration bundle",
        max_bytes=V3_MIGRATION_BUNDLE_MAX_BYTES,
    )
    return encoded


def verify_snapshot_v3_migration_bundle(
    bundle: SnapshotV3MigrationBundle,
    *,
    commit_relation_verifier: CommitRelationVerifier | None = None,
) -> SnapshotV3MigrationPlan:
    """Re-run the preflight and require an exact embedded-plan replay."""

    if type(bundle) is not SnapshotV3MigrationBundle:
        _invalid_bundle(
            "TBM_V3_INVALID_BUNDLE",
            "bundle must be exactly SnapshotV3MigrationBundle",
        )
    replay = plan_snapshot_v3_migration(
        bundle.source_snapshot,
        bundle.mapping,
        commit_relation_verifier=commit_relation_verifier,
    )
    if replay != bundle.plan:
        _invalid_bundle(
            "TBM_V3_BUNDLE_PLAN_REPLAY_MISMATCH",
            "bundle plan does not match a fresh trusted preflight replay",
        )
    return replay


def _freeze_source_snapshot(
    source: TraceBackedMemoryStore | Mapping[str, object],
) -> dict[str, object]:
    if type(source) is TraceBackedMemoryStore:
        payload = source.to_snapshot()
    elif isinstance(source, Mapping):
        payload = _freeze_object(source, "source snapshot")
    else:
        _invalid_bundle(
            "TBM_V3_INVALID_BUNDLE_SOURCE",
            "bundle source must be a TraceBackedMemoryStore or snapshot object",
        )
    if (
        type(payload.get("snapshot_version")) is not int
        or payload["snapshot_version"] != V3_SOURCE_SNAPSHOT_VERSION
    ):
        _invalid_bundle(
            "TBM_V3_BUNDLE_SOURCE_VERSION_MISMATCH",
            "bundle source must be an explicit version-2 snapshot",
        )
    try:
        TraceBackedMemoryStore.from_snapshot(payload)
    except (
        TypeError,
        ValueError,
        OverflowError,
        RecursionError,
    ) as error:
        raise V3MigrationBundleError(
            "TBM_V3_INVALID_BUNDLE_SOURCE",
            "bundle source snapshot failed strict version-2 validation",
        ) from error
    _bounded_json(
        payload,
        description="v3 migration bundle source snapshot",
        max_bytes=SNAPSHOT_FILE_MAX_BYTES,
    )
    return payload


def _freeze_mapping(
    mapping: SnapshotV3MigrationMapping | Mapping[str, object],
) -> SnapshotV3MigrationMapping:
    if type(mapping) is SnapshotV3MigrationMapping:
        payload = mapping.to_dict()
    elif isinstance(mapping, Mapping):
        payload = _freeze_object(mapping, "migration mapping")
    else:
        _invalid_bundle(
            "TBM_V3_INVALID_BUNDLE_MAPPING",
            "bundle mapping must be a SnapshotV3MigrationMapping or object",
        )
    _bounded_json(
        payload,
        description="v3 migration bundle mapping",
        max_bytes=V3_MIGRATION_MAPPING_MAX_BYTES,
    )
    try:
        return parse_v3_migration_mapping(payload)
    except V3ContractError as error:
        raise V3MigrationBundleError(error.code, str(error)) from error


def _freeze_object(
    value: Mapping[str, object],
    description: str,
) -> dict[str, object]:
    try:
        encoded = _canonical_json(value)
    except V3ContractError as error:
        raise V3MigrationBundleError(error.code, str(error)) from error
    return _parse_json_object(
        encoded,
        description=description,
        max_bytes=V3_MIGRATION_BUNDLE_MAX_BYTES,
        max_nodes=V3_MIGRATION_BUNDLE_MAX_NODES,
    )


def _bundle_id(
    *,
    state: V3MigrationBundleState,
    source_snapshot_sha256: str,
    normalized_source_snapshot_sha256: str,
    mapping_sha256: str,
    plan_sha256: str,
) -> str:
    return _bundle_sha256(
        {
            "bundle_version": V3_MIGRATION_BUNDLE_VERSION,
            "state": state,
            "source_snapshot_version": V3_SOURCE_SNAPSHOT_VERSION,
            "target_snapshot_version": V3_TARGET_SNAPSHOT_VERSION,
            "source_snapshot_sha256": source_snapshot_sha256,
            "normalized_source_snapshot_sha256": (
                normalized_source_snapshot_sha256
            ),
            "mapping_sha256": mapping_sha256,
            "plan_sha256": plan_sha256,
        }
    )


def _bundle_sha256(value: object) -> str:
    try:
        return canonical_sha256(value)
    except V3ContractError as error:
        raise V3MigrationBundleError(error.code, str(error)) from error


def _canonical_json(value: object) -> str:
    try:
        return json.dumps(
            value,
            allow_nan=False,
            ensure_ascii=False,
            separators=(",", ":"),
            sort_keys=True,
        )
    except (TypeError, ValueError, UnicodeError, RecursionError) as error:
        raise V3MigrationBundleError(
            "TBM_V3_BUNDLE_NON_CANONICAL_JSON",
            "bundle value cannot be encoded as finite canonical JSON",
        ) from error


def _parse_json_object(
    source: str,
    *,
    description: str,
    max_bytes: int,
    max_nodes: int,
) -> dict[str, object]:
    _bounded_text(
        source,
        description=description,
        max_bytes=max_bytes,
    )

    def reject_non_finite(value: str) -> Any:
        raise V3MigrationBundleError(
            "TBM_V3_INVALID_BUNDLE_JSON",
            f"{description} contains a non-finite number: {value}",
        )

    try:
        payload = json.loads(
            source,
            parse_constant=reject_non_finite,
            object_pairs_hook=lambda pairs: unique_json_object_pairs(
                pairs,
                description=description,
            ),
        )
    except V3MigrationBundleError:
        raise
    except (json.JSONDecodeError, RecursionError, ValueError) as error:
        raise V3MigrationBundleError(
            "TBM_V3_INVALID_BUNDLE_JSON",
            f"{description} must be strict JSON",
        ) from error
    if type(payload) is not dict:
        _invalid_bundle(
            "TBM_V3_INVALID_BUNDLE_JSON",
            f"{description} must be a JSON object",
        )
    _validate_json_tree(
        payload,
        description=description,
        max_nodes=max_nodes,
    )
    return cast(dict[str, object], payload)


def _validate_json_tree(
    payload: object,
    *,
    description: str,
    max_nodes: int,
) -> None:
    nodes = 0
    pending = [(payload, 0)]
    while pending:
        value, depth = pending.pop()
        if depth > V3_MIGRATION_BUNDLE_MAX_DEPTH:
            _invalid_bundle(
                "TBM_V3_BUNDLE_LIMIT_EXCEEDED",
                (
                    f"{description} exceeds maximum depth "
                    f"{V3_MIGRATION_BUNDLE_MAX_DEPTH}"
                ),
            )
        nodes += 1
        if nodes > max_nodes:
            _invalid_bundle(
                "TBM_V3_BUNDLE_LIMIT_EXCEEDED",
                f"{description} contains more than {max_nodes} nodes",
            )
        if type(value) is str:
            try:
                value.encode("utf-8")
            except UnicodeEncodeError as error:
                raise V3MigrationBundleError(
                    "TBM_V3_INVALID_BUNDLE_JSON",
                    f"{description} contains invalid Unicode",
                ) from error
        elif type(value) is list:
            pending.extend((item, depth + 1) for item in value)
        elif type(value) is dict:
            for key in value:
                if type(key) is not str:
                    _invalid_bundle(
                        "TBM_V3_INVALID_BUNDLE_JSON",
                        f"{description} object keys must be strings",
                    )
                try:
                    key.encode("utf-8")
                except UnicodeEncodeError as error:
                    raise V3MigrationBundleError(
                        "TBM_V3_INVALID_BUNDLE_JSON",
                        f"{description} contains invalid Unicode",
                    ) from error
            pending.extend((item, depth + 1) for item in value.values())


def _closed_object(
    value: Mapping[str, object],
    description: str,
    fields: set[str],
) -> dict[str, object]:
    if type(value) is not dict:
        _invalid_bundle(
            "TBM_V3_INVALID_BUNDLE",
            f"{description} must be an object",
        )
    keys = set(value)
    if keys != fields:
        missing = sorted(fields - keys)
        unknown = sorted(keys - fields)
        if missing:
            _invalid_bundle(
                "TBM_V3_INVALID_BUNDLE",
                f"{description} missing required field: {missing[0]}",
            )
        _invalid_bundle(
            "TBM_V3_INVALID_BUNDLE",
            f"{description} has unknown field: {unknown[0]}",
        )
    return cast(dict[str, object], value)


def _required_string(value: object, field_name: str) -> str:
    if type(value) is not str or not value:
        _invalid_bundle(
            "TBM_V3_INVALID_BUNDLE",
            f"{field_name} must be a non-empty string",
        )
    return cast(str, value)


def _required_integer(value: object, field_name: str) -> int:
    if type(value) is not int:
        _invalid_bundle(
            "TBM_V3_INVALID_BUNDLE",
            f"{field_name} must be an integer",
        )
    return cast(int, value)


def _bundle_digest(value: object, field_name: str) -> None:
    if type(value) is not str or _SHA256_RE.fullmatch(value) is None:
        _invalid_bundle(
            "TBM_V3_INVALID_BUNDLE",
            f"{field_name} must use sha256:<64 lowercase hex>",
        )


def _bounded_json(
    value: object,
    *,
    description: str,
    max_bytes: int,
) -> None:
    encoded = _canonical_json(value)
    _bounded_text(
        encoded,
        description=description,
        max_bytes=max_bytes,
    )


def _bounded_text(
    value: str,
    *,
    description: str,
    max_bytes: int,
) -> None:
    try:
        size = len(value.encode("utf-8"))
    except UnicodeEncodeError as error:
        raise V3MigrationBundleError(
            "TBM_V3_INVALID_BUNDLE_JSON",
            f"{description} contains invalid Unicode",
        ) from error
    if size > max_bytes:
        _invalid_bundle(
            "TBM_V3_BUNDLE_LIMIT_EXCEEDED",
            f"{description} exceeds maximum size of {max_bytes} bytes",
        )


def _invalid_bundle(code: str, message: str) -> None:
    raise V3MigrationBundleError(code, message)
