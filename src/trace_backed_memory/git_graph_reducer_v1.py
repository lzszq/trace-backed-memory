from __future__ import annotations

from collections.abc import Mapping
import hashlib
import json
import re
from typing import NoReturn, cast

from ._timestamps import canonical_rfc3339
from .git_observation_v1 import (
    GIT_OBSERVATION_MAX_BOUNDARY_COMMITS,
    GIT_OBSERVATION_MAX_SEQUENCE,
    GIT_OBSERVATION_TYPES,
    GitObservationRecordRef,
    GitObservationV1Error,
    parse_git_observation,
)
from .event_v1 import CanonicalEvent
from .reducer import (
    FunctionalReducer,
    ReducerDescriptor,
    ReducerEvent,
    ReducerExecutionError,
    ReducerV1Error,
    canonical_projection_state,
)


GIT_GRAPH_REDUCER_ID = "git-graph"
GIT_GRAPH_PROJECTION_NAME = "git_graph_v1"
GIT_GRAPH_PROJECTION_SCHEMA_VERSION = 1
GIT_GRAPH_MAX_COMMITS = 20_000
GIT_GRAPH_MAX_RELATIONS = 50_000
GIT_GRAPH_MAX_OBJECTS = 20_000
GIT_GRAPH_MAX_CHECKOUTS = 1_024
GIT_GRAPH_MAX_REFS_PER_CHECKOUT = 10_000

_RELATION_VALUES = ("ancestor", "not_ancestor", "unknown")
_AVAILABILITY_VALUES = ("available", "unavailable", "unknown")
_OBJECT_TYPES = frozenset({"commit", "tree", "blob", "tag", "unknown"})
_UNAVAILABLE_REASONS = frozenset(
    {"missing", "shallow_boundary", "not_fetched", "capture_failed", "unknown"}
)
_EVIDENCE_QUALITIES = frozenset(
    {"exact", "verified", "observed", "legacy_partial", "unknown"}
)
_ACTOR_TYPES = frozenset({"principal", "agent_client", "service", "worker"})
_IDENTIFIER_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:-]{0,127}$")
_EVENT_ID_RE = re.compile(r"^evt_[A-Za-z0-9][A-Za-z0-9._:-]{0,123}$")
_COMMIT_SHA_RE = re.compile(r"^(?:[0-9a-f]{40}|[0-9a-f]{64})$")
_DIGEST_RE = re.compile(r"^sha256:[0-9a-f]{64}$")
_ARTIFACT_ID_RE = re.compile(r"^artifact_sha256_[0-9a-f]{64}$")
_GIT_REF_NAME_RE = re.compile(
    r"^(?![-/.])(?!@$)(?!.*//)(?!.*\.\.)(?!.*@\{)"
    r"(?!.*(?:^|/)\.)(?!.*(?:^|/)[^/]*\.lock(?:/|$))"
    r"(?!.*[./]$)[A-Za-z0-9_@+./-]{1,512}$"
)
_STATE_FIELDS = {"scope", "repository", "heads", "last_global_position"}
_SCOPE_FIELDS = {
    "organization_id",
    "tenant_id",
    "repository_id",
    "environment_id",
}
_PROVENANCE_FIELDS = {
    "event_id",
    "event_sha256",
    "stream_id",
    "stream_version",
    "global_position",
    "occurred_at",
    "recorded_at",
    "observed_at",
    "evidence_quality",
    "source_system",
    "source_record_id",
    "authorization_decision_id",
    "principal_id",
    "agent_client_id",
    "actor_type",
    "actor_id",
    "trace_id",
    "run_id",
    "checkout_alias",
}
_HEAD_FIELDS = {
    "stream_version",
    "event_id",
    "event_sha256",
    "global_position",
    "organization_id",
    "tenant_id",
    "repository_id",
    "environment_id",
    "authorization_decision_id",
    "trace_id",
    "run_id",
    "checkout_alias",
}


def build_git_graph_reducer() -> FunctionalReducer:
    input_event_types = tuple(sorted(GIT_OBSERVATION_TYPES))
    descriptor = ReducerDescriptor(
        reducer_id=GIT_GRAPH_REDUCER_ID,
        reducer_version=1,
        input_event_types=input_event_types,
        output_projection=GIT_GRAPH_PROJECTION_NAME,
        output_schema_version=GIT_GRAPH_PROJECTION_SCHEMA_VERSION,
        code_sha256=_domain_sha256(
            b"tbm.reducer-code.v1\x00",
            {
                "algorithm": "git-graph",
                "algorithm_version": 1,
                "validation": [
                    "typed-payload",
                    "trusted-scope",
                    "global-order",
                    "stream-head",
                    "immutable-assertion-summary",
                    "unknown-monotonicity",
                    "no-direct-parent-inference",
                    "no-force-push-inference",
                    "no-semantic-role-inference",
                    "no-pr-anchor-inference",
                ],
            },
        ),
        configuration_sha256=_domain_sha256(
            b"tbm.reducer-configuration.v1\x00",
            {"configuration": "none", "version": 1},
        ),
        target_event_versions={event_type: 1 for event_type in input_event_types},
    )

    def initial() -> Mapping[str, object]:
        return {
            "scope": None,
            "repository": None,
            "heads": {},
            "last_global_position": 0,
        }

    def transition(
        state: Mapping[str, object],
        reducer_event: ReducerEvent,
    ) -> Mapping[str, object]:
        source = reducer_event.source_event
        typed = reducer_event.typed_event
        if typed is None:
            _reject("typed Git observation is required")
        if typed.target_version != 1 or typed.event_type != source.event_type:
            _reject("typed Git observation version is invalid")
        if _thaw_json(typed.payload) != _thaw_json(source.payload):
            _reject("typed Git observation payload differs from its source event")
        try:
            reference = parse_git_observation(source)
        except GitObservationV1Error as error:
            raise ReducerExecutionError(
                "TBM_GIT_GRAPH_REDUCER_EVENT_INVALID",
                "Git observation cannot update the graph projection",
            ) from error

        retained_state = _validated_state_copy(state)
        last_global_position = cast(int, retained_state["last_global_position"])
        if source.global_position <= last_global_position:
            _reject("Git observations are not in strict global order")
        scope = _scope(source)
        retained_scope = retained_state["scope"]
        if retained_scope is None:
            projection_scope = scope
        else:
            projection_scope = cast(dict[str, object], retained_scope)
            if projection_scope != scope:
                _reject("Git observation scope differs from the projection scope")

        heads = cast(dict[str, object], retained_state["heads"])
        _verify_stream_head(heads.get(source.stream_id), source, reference)
        retained_repository = retained_state["repository"]
        repository = (
            _new_repository(reference.repository_id)
            if retained_repository is None
            else cast(dict[str, object], retained_repository)
        )
        provenance = _provenance(source, reference)

        commits = _mapping_copy(repository["commits"], "commits")
        for commit_sha in _commit_shas(reference):
            _touch_commit(commits, commit_sha, provenance)
        repository["commits"] = commits

        if reference.observation_kind == "ancestry":
            relations = _mapping_copy(repository["relations"], "relations")
            _apply_relation(relations, reference, provenance)
            repository["relations"] = relations
        if reference.observation_kind == "object_availability":
            objects = _mapping_copy(repository["objects"], "objects")
            missing = _mapping_copy(repository["missing_objects"], "missing_objects")
            _apply_object_availability(objects, missing, reference, provenance)
            repository["objects"] = objects
            repository["missing_objects"] = missing
        if reference.observation_kind in {
            "checkout",
            "ref",
            "worktree_status",
            "diff",
            "shallow_state",
        }:
            checkouts = _mapping_copy(repository["checkouts"], "checkouts")
            _apply_checkout(checkouts, reference, provenance)
            repository["checkouts"] = checkouts

        repository["last_observation"] = provenance
        heads[source.stream_id] = _head(source, reference)
        return {
            "scope": projection_scope,
            "repository": repository,
            "heads": heads,
            "last_global_position": source.global_position,
        }

    return FunctionalReducer(descriptor, initial, transition)


def projected_git_graph(
    state: Mapping[str, object],
    repository_id: str,
) -> dict[str, object]:
    retained_state = _validated_state_copy(state)
    scope = retained_state["scope"]
    repository = retained_state["repository"]
    if (
        not isinstance(scope, dict)
        or scope.get("repository_id") != repository_id
        or not isinstance(repository, dict)
    ):
        _reject("Git graph repository is absent from projection state")
    return repository


def _new_repository(repository_id: str) -> dict[str, object]:
    return {
        "repository_id": repository_id,
        "commits": {},
        "relations": {},
        "objects": {},
        "missing_objects": {},
        "checkouts": {},
        "direct_parent_edges": [],
        "source_fix_verification_relationships": [],
        "pr_anchors": [],
        "last_observation": None,
    }


def _validated_state_copy(state: Mapping[str, object]) -> dict[str, object]:
    try:
        bounded_state = canonical_projection_state(state)
    except ReducerV1Error as error:
        raise ReducerExecutionError(
            "TBM_GIT_GRAPH_REDUCER_EVENT_INVALID",
            "Git graph projection state is invalid",
        ) from error
    retained = _mapping_copy(bounded_state, "Git graph")
    if set(retained) != _STATE_FIELDS:
        _reject("retained Git graph root state is invalid")
    last_global_position = retained.get("last_global_position")
    if type(last_global_position) is not int or last_global_position < 0:
        _reject("retained Git graph global position is invalid")
    heads = _mapping_copy(retained.get("heads"), "heads")
    retained["heads"] = heads
    if last_global_position == 0:
        if retained.get("scope") is not None or retained.get("repository") is not None:
            _reject("retained initial Git graph state is invalid")
        if heads:
            _reject("retained initial Git graph heads are invalid")
        return retained

    scope = _validate_scope(retained.get("scope"))
    repository_id = cast(str, scope["repository_id"])
    repository = _repository_copy(
        retained.get("repository"),
        repository_id,
        last_global_position=last_global_position,
    )
    latest_stream_id, latest_head = _validate_heads(
        heads,
        scope,
        last_global_position,
    )
    last_observation = _mapping_copy(
        repository.get("last_observation"),
        "repository last observation",
    )
    if (
        last_observation.get("global_position") != last_global_position
        or last_observation.get("stream_id") != latest_stream_id
        or any(
            last_observation.get(field) != latest_head.get(field)
            for field in (
                "event_id",
                "event_sha256",
                "stream_version",
                "global_position",
                "authorization_decision_id",
                "trace_id",
                "run_id",
                "checkout_alias",
            )
        )
    ):
        _reject("retained Git graph last observation is invalid")
    retained["scope"] = scope
    retained["repository"] = repository
    return retained


def _validate_scope(value: object) -> dict[str, object]:
    scope = _mapping_copy(value, "scope")
    if set(scope) != _SCOPE_FIELDS:
        _reject("retained Git graph scope is invalid")
    for field in sorted(_SCOPE_FIELDS):
        _identifier(scope.get(field), f"scope {field}")
    return scope


def _validate_heads(
    heads: dict[str, object],
    scope: dict[str, object],
    last_global_position: int,
) -> tuple[str, dict[str, object]]:
    retained_heads: list[tuple[str, dict[str, object]]] = []
    for stream_id, value in heads.items():
        _identifier(stream_id, "Git graph head stream_id")
        head = _mapping_copy(value, "Git observation head")
        if set(head) != _HEAD_FIELDS:
            _reject("retained Git observation head is invalid")
        stream_version = _positive_int(
            head.get("stream_version"),
            "head stream version",
        )
        if stream_version > GIT_OBSERVATION_MAX_SEQUENCE:
            _reject("retained Git observation head sequence is invalid")
        _event_id(head.get("event_id"), "head event_id")
        _digest(head.get("event_sha256"), "head event_sha256")
        position = _positive_int(head.get("global_position"), "head global position")
        if position > last_global_position:
            _reject("retained Git observation head position is invalid")
        retained_heads.append((stream_id, head))
        for field in sorted(_SCOPE_FIELDS):
            _identifier(head.get(field), f"head {field}")
            if head.get(field) != scope[field]:
                _reject("retained Git observation head scope is invalid")
        for field in (
            "authorization_decision_id",
            "trace_id",
            "run_id",
            "checkout_alias",
        ):
            _identifier(head.get(field), f"head {field}")
    positions = [cast(int, head["global_position"]) for _, head in retained_heads]
    if (
        not positions
        or len(set(positions)) != len(positions)
        or max(positions) != last_global_position
    ):
        _reject("retained Git graph head watermark is invalid")
    return max(
        retained_heads,
        key=lambda item: cast(int, item[1]["global_position"]),
    )


def _repository_copy(
    value: object,
    repository_id: str,
    *,
    last_global_position: int,
) -> dict[str, object]:
    repository = _mapping_copy(value, "repository")
    expected_fields = {
        "repository_id",
        "commits",
        "relations",
        "objects",
        "missing_objects",
        "checkouts",
        "direct_parent_edges",
        "source_fix_verification_relationships",
        "pr_anchors",
        "last_observation",
    }
    if (
        set(repository) != expected_fields
        or repository.get("repository_id") != repository_id
        or repository.get("direct_parent_edges") != []
        or repository.get("source_fix_verification_relationships") != []
        or repository.get("pr_anchors") != []
    ):
        _reject("retained Git graph repository state is invalid")
    _identifier(repository_id, "repository_id")

    commits = _mapping_copy(repository.get("commits"), "commits")
    if len(commits) > GIT_GRAPH_MAX_COMMITS:
        _reject("retained Git graph commit limit is invalid")
    for commit_sha, value in commits.items():
        _commit_sha(commit_sha, "commit key")
        node = _mapping_copy(value, "commit node")
        if (
            set(node) != {"commit_sha", "observation_count", "last_observation"}
            or node.get("commit_sha") != commit_sha
        ):
            _reject("retained Git graph commit node is invalid")
        _positive_int(node.get("observation_count"), "commit observation count")
        _validate_provenance(
            node.get("last_observation"),
            "commit last observation",
            last_global_position,
        )
    commit_shas = set(commits)

    relations = _mapping_copy(repository.get("relations"), "relations")
    if len(relations) > GIT_GRAPH_MAX_RELATIONS:
        _reject("retained Git graph relation limit is invalid")
    for relation_key, value in relations.items():
        _validate_relation(
            relation_key,
            value,
            commit_shas=commit_shas,
            last_global_position=last_global_position,
        )

    objects = _mapping_copy(repository.get("objects"), "objects")
    if len(objects) > GIT_GRAPH_MAX_OBJECTS:
        _reject("retained Git graph object limit is invalid")
    expected_missing: dict[str, object] = {}
    for object_key, value in objects.items():
        retained_object = _validate_object(
            object_key,
            value,
            commit_shas=commit_shas,
            last_global_position=last_global_position,
        )
        if retained_object["availability"] != "available":
            expected_missing[object_key] = {
                "object_sha": retained_object["object_sha"],
                "object_type": retained_object["object_type"],
                "availability": retained_object["availability"],
                "unavailable_reason": retained_object["unavailable_reason"],
                "last_observation": retained_object["last_observation"],
            }
    missing = _mapping_copy(repository.get("missing_objects"), "missing_objects")
    if missing != expected_missing:
        _reject("retained Git graph missing object state is invalid")

    checkouts = _mapping_copy(repository.get("checkouts"), "checkouts")
    if len(checkouts) > GIT_GRAPH_MAX_CHECKOUTS:
        _reject("retained Git graph checkout limit is invalid")
    for checkout_alias, value in checkouts.items():
        _validate_checkout(
            checkout_alias,
            value,
            commit_shas=commit_shas,
            last_global_position=last_global_position,
        )

    _validate_provenance(
        repository.get("last_observation"),
        "repository last observation",
        last_global_position,
    )
    repository["commits"] = commits
    repository["relations"] = relations
    repository["objects"] = objects
    repository["missing_objects"] = missing
    repository["checkouts"] = checkouts
    return repository


def _touch_commit(
    commits: dict[str, object],
    commit_sha: str,
    provenance: dict[str, object],
) -> None:
    raw = commits.get(commit_sha)
    if raw is None:
        if len(commits) >= GIT_GRAPH_MAX_COMMITS:
            _reject("Git graph commit limit exceeded")
        observation_count = 0
    else:
        node = _mapping_copy(raw, "commit node")
        if set(node) != {"commit_sha", "observation_count", "last_observation"}:
            _reject("retained Git graph commit node is invalid")
        observation_count = node.get("observation_count")
        if type(observation_count) is not int or observation_count < 1:
            _reject("retained Git graph commit count is invalid")
    commits[commit_sha] = {
        "commit_sha": commit_sha,
        "observation_count": observation_count + 1,
        "last_observation": provenance,
    }


def _apply_relation(
    relations: dict[str, object],
    reference: GitObservationRecordRef,
    provenance: dict[str, object],
) -> None:
    details = reference.details
    ancestor_sha = cast(str, details.ancestor_sha)
    descendant_sha = cast(str, details.descendant_sha)
    relation = cast(str, details.relation)
    key = f"{ancestor_sha}:{descendant_sha}"
    raw = relations.get(key)
    if raw is None:
        if len(relations) >= GIT_GRAPH_MAX_RELATIONS:
            _reject("Git graph relation limit exceeded")
        assertion_counts = {value: 0 for value in _RELATION_VALUES}
        assertions: dict[str, object] = {}
        observation_count = 0
    else:
        retained = _mapping_copy(raw, "relation")
        expected_fields = {
            "ancestor_sha",
            "descendant_sha",
            "assertion_counts",
            "assertions",
            "observation_count",
            "latest_assertion",
            "effective_relation",
            "relation_confidence",
            "conflicted",
            "last_observation",
        }
        if (
            set(retained) != expected_fields
            or retained.get("ancestor_sha") != ancestor_sha
            or retained.get("descendant_sha") != descendant_sha
        ):
            _reject("retained Git graph relation is invalid")
        assertion_counts = _bounded_counts(
            retained.get("assertion_counts"),
            _RELATION_VALUES,
            "relation assertion counts",
        )
        assertions = _mapping_copy(retained.get("assertions"), "relation assertions")
        observation_count = retained.get("observation_count")
        if type(observation_count) is not int or observation_count < 1:
            _reject("retained Git graph relation count is invalid")
    assertion_counts[relation] += 1
    assertions[relation] = provenance
    known = {
        value
        for value in ("ancestor", "not_ancestor")
        if assertion_counts[value] > 0
    }
    conflicted = len(known) > 1
    if conflicted or assertion_counts["unknown"] > 0 or not known:
        effective_relation = "unknown"
        confidence = "unknown"
    else:
        effective_relation = next(iter(known))
        effective_provenance = _mapping_copy(
            assertions[effective_relation],
            "effective relation provenance",
        )
        confidence = effective_provenance.get("evidence_quality")
        if type(confidence) is not str:
            _reject("retained Git graph relation confidence is invalid")
    relations[key] = {
        "ancestor_sha": ancestor_sha,
        "descendant_sha": descendant_sha,
        "assertion_counts": assertion_counts,
        "assertions": assertions,
        "observation_count": observation_count + 1,
        "latest_assertion": relation,
        "effective_relation": effective_relation,
        "relation_confidence": confidence,
        "conflicted": conflicted,
        "last_observation": provenance,
    }


def _apply_object_availability(
    objects: dict[str, object],
    missing: dict[str, object],
    reference: GitObservationRecordRef,
    provenance: dict[str, object],
) -> None:
    details = reference.details
    object_sha = cast(str, details.object_sha)
    object_type = cast(str, details.object_type)
    availability = cast(str, details.availability)
    key = f"{object_type}:{object_sha}"
    raw = objects.get(key)
    if raw is None:
        if len(objects) >= GIT_GRAPH_MAX_OBJECTS:
            _reject("Git graph object limit exceeded")
        assertion_counts = {value: 0 for value in _AVAILABILITY_VALUES}
        assertions: dict[str, object] = {}
        observation_count = 0
    else:
        retained = _mapping_copy(raw, "object availability")
        expected_fields = {
            "object_sha",
            "object_type",
            "assertion_counts",
            "assertions",
            "observation_count",
            "availability",
            "unavailable_reason",
            "last_observation",
        }
        if (
            set(retained) != expected_fields
            or retained.get("object_sha") != object_sha
            or retained.get("object_type") != object_type
        ):
            _reject("retained Git graph object state is invalid")
        assertion_counts = _bounded_counts(
            retained.get("assertion_counts"),
            _AVAILABILITY_VALUES,
            "object availability counts",
        )
        assertions = _mapping_copy(
            retained.get("assertions"),
            "object availability assertions",
        )
        observation_count = retained.get("observation_count")
        if type(observation_count) is not int or observation_count < 1:
            _reject("retained Git graph object count is invalid")
    assertion_counts[availability] += 1
    assertions[availability] = provenance
    retained_object = {
        "object_sha": object_sha,
        "object_type": object_type,
        "assertion_counts": assertion_counts,
        "assertions": assertions,
        "observation_count": observation_count + 1,
        "availability": availability,
        "unavailable_reason": details.unavailable_reason,
        "last_observation": provenance,
    }
    objects[key] = retained_object
    if availability == "available":
        missing.pop(key, None)
    else:
        missing[key] = {
            "object_sha": object_sha,
            "object_type": object_type,
            "availability": availability,
            "unavailable_reason": details.unavailable_reason,
            "last_observation": provenance,
        }


def _apply_checkout(
    checkouts: dict[str, object],
    reference: GitObservationRecordRef,
    provenance: dict[str, object],
) -> None:
    alias = reference.checkout_alias
    raw = checkouts.get(alias)
    if raw is None:
        if len(checkouts) >= GIT_GRAPH_MAX_CHECKOUTS:
            _reject("Git graph checkout limit exceeded")
        checkout: dict[str, object] = {
            "checkout_alias": alias,
            "commit_sha": None,
            "ref_name": None,
            "detached": None,
            "dirty": None,
            "diff_artifact_id": None,
            "diff_base_commit_sha": None,
            "remote_sha256": None,
            "shallow": None,
            "boundary_commit_shas": [],
            "refs": {},
            "last_observation": None,
        }
    else:
        checkout = _mapping_copy(raw, "checkout")
        expected_fields = {
            "checkout_alias",
            "commit_sha",
            "ref_name",
            "detached",
            "dirty",
            "diff_artifact_id",
            "diff_base_commit_sha",
            "remote_sha256",
            "shallow",
            "boundary_commit_shas",
            "refs",
            "last_observation",
        }
        if set(checkout) != expected_fields or checkout.get("checkout_alias") != alias:
            _reject("retained Git graph checkout state is invalid")
    details = reference.details
    if reference.observation_kind in {"checkout", "ref", "worktree_status"}:
        checkout["commit_sha"] = details.commit_sha
        checkout["ref_name"] = details.ref_name
        checkout["detached"] = details.detached
        if details.ref_name is not None:
            refs = _mapping_copy(checkout["refs"], "checkout refs")
            _apply_ref(refs, details.ref_name, cast(str, details.commit_sha), provenance)
            checkout["refs"] = refs
    if reference.observation_kind == "checkout":
        checkout["remote_sha256"] = details.remote_sha256
    elif reference.observation_kind == "worktree_status":
        checkout["dirty"] = details.dirty
        checkout["diff_artifact_id"] = details.diff_artifact_id
    elif reference.observation_kind == "diff":
        checkout["commit_sha"] = details.commit_sha
        checkout["diff_artifact_id"] = details.diff_artifact_id
        checkout["diff_base_commit_sha"] = details.base_commit_sha
    elif reference.observation_kind == "shallow_state":
        checkout["shallow"] = details.shallow
        checkout["boundary_commit_shas"] = list(details.boundary_commit_shas)
    checkout["last_observation"] = provenance
    checkouts[alias] = checkout


def _apply_ref(
    refs: dict[str, object],
    ref_name: str,
    commit_sha: str,
    provenance: dict[str, object],
) -> None:
    raw = refs.get(ref_name)
    if raw is None:
        if len(refs) >= GIT_GRAPH_MAX_REFS_PER_CHECKOUT:
            _reject("Git graph ref limit exceeded")
        observed_commit_shas: list[str] = []
        observation_count = 0
    else:
        retained = _mapping_copy(raw, "ref")
        expected_fields = {
            "ref_name",
            "current_commit_sha",
            "observed_commit_shas",
            "observation_count",
            "moved",
            "movement_classification",
            "last_observation",
        }
        if set(retained) != expected_fields or retained.get("ref_name") != ref_name:
            _reject("retained Git graph ref state is invalid")
        observed = retained.get("observed_commit_shas")
        if type(observed) is not list or any(type(item) is not str for item in observed):
            _reject("retained Git graph ref commits are invalid")
        observed_commit_shas = cast(list[str], observed)
        observation_count = retained.get("observation_count")
        if type(observation_count) is not int or observation_count < 1:
            _reject("retained Git graph ref count is invalid")
    observed_commit_shas = sorted(set((*observed_commit_shas, commit_sha)))
    moved = len(observed_commit_shas) > 1
    refs[ref_name] = {
        "ref_name": ref_name,
        "current_commit_sha": commit_sha,
        "observed_commit_shas": observed_commit_shas,
        "observation_count": observation_count + 1,
        "moved": moved,
        "movement_classification": "unknown" if moved else "unmoved",
        "last_observation": provenance,
    }


def _commit_shas(reference: GitObservationRecordRef) -> tuple[str, ...]:
    details = reference.details
    values = {
        value
        for value in (
            details.commit_sha,
            details.base_commit_sha,
            details.ancestor_sha,
            details.descendant_sha,
        )
        if value is not None
    }
    if details.object_type == "commit" and details.object_sha is not None:
        values.add(details.object_sha)
    values.update(details.boundary_commit_shas)
    return tuple(sorted(values))


def _verify_stream_head(
    raw_head: object,
    source: CanonicalEvent,
    reference: GitObservationRecordRef,
) -> None:
    if raw_head is None:
        if reference.sequence != 1:
            _reject("Git graph projection cannot start after stream version 1")
        return
    head = _mapping_copy(raw_head, "Git observation head")
    expected_fields = {
        "stream_version",
        "event_id",
        "event_sha256",
        "global_position",
        "organization_id",
        "tenant_id",
        "repository_id",
        "environment_id",
        "authorization_decision_id",
        "trace_id",
        "run_id",
        "checkout_alias",
    }
    if (
        set(head) != expected_fields
        or head.get("stream_version") != reference.sequence - 1
        or head.get("event_sha256") != source.previous_stream_event_sha256
        or type(head.get("global_position")) is not int
        or cast(int, head["global_position"])
        >= source.global_position
        or head.get("organization_id") != source.organization_id
        or head.get("tenant_id") != source.tenant_id
        or head.get("repository_id") != reference.repository_id
        or head.get("environment_id") != source.environment_id
        or head.get("trace_id") != reference.trace_id
        or head.get("run_id") != reference.run_id
        or head.get("checkout_alias") != reference.checkout_alias
    ):
        _reject("Git observation does not extend the retained graph head")


def _validate_relation(
    relation_key: str,
    value: object,
    *,
    commit_shas: set[str],
    last_global_position: int,
) -> None:
    retained = _mapping_copy(value, "relation")
    expected_fields = {
        "ancestor_sha",
        "descendant_sha",
        "assertion_counts",
        "assertions",
        "observation_count",
        "latest_assertion",
        "effective_relation",
        "relation_confidence",
        "conflicted",
        "last_observation",
    }
    ancestor_sha = retained.get("ancestor_sha")
    descendant_sha = retained.get("descendant_sha")
    if set(retained) != expected_fields:
        _reject("retained Git graph relation is invalid")
    _commit_sha(ancestor_sha, "relation ancestor_sha")
    _commit_sha(descendant_sha, "relation descendant_sha")
    if (
        relation_key != f"{ancestor_sha}:{descendant_sha}"
        or ancestor_sha not in commit_shas
        or descendant_sha not in commit_shas
    ):
        _reject("retained Git graph relation key is invalid")
    counts = _bounded_counts(
        retained.get("assertion_counts"),
        _RELATION_VALUES,
        "relation assertion counts",
    )
    assertions = _mapping_copy(retained.get("assertions"), "relation assertions")
    expected_assertions = {key for key, count in counts.items() if count > 0}
    if set(assertions) != expected_assertions:
        _reject("retained Git graph relation assertions are invalid")
    for assertion, provenance in assertions.items():
        if assertion not in _RELATION_VALUES:
            _reject("retained Git graph relation assertion is invalid")
        _validate_provenance(
            provenance,
            "relation assertion provenance",
            last_global_position,
        )
    observation_count = _positive_int(
        retained.get("observation_count"),
        "relation observation count",
    )
    if observation_count != sum(counts.values()):
        _reject("retained Git graph relation count is invalid")
    latest_assertion = retained.get("latest_assertion")
    if latest_assertion not in expected_assertions:
        _reject("retained Git graph latest assertion is invalid")
    last_observation = _validate_provenance(
        retained.get("last_observation"),
        "relation last observation",
        last_global_position,
    )
    if last_observation != assertions[cast(str, latest_assertion)]:
        _reject("retained Git graph relation provenance is invalid")
    known = {key for key in ("ancestor", "not_ancestor") if counts[key] > 0}
    conflicted = len(known) > 1
    effective_relation = retained.get("effective_relation")
    confidence = retained.get("relation_confidence")
    if conflicted or counts["unknown"] > 0 or not known:
        expected_relation = "unknown"
        expected_confidence = "unknown"
    else:
        expected_relation = next(iter(known))
        expected_confidence = cast(
            dict[str, object],
            assertions[expected_relation],
        ).get("evidence_quality")
    if (
        retained.get("conflicted") is not conflicted
        or effective_relation != expected_relation
        or confidence != expected_confidence
    ):
        _reject("retained Git graph effective relation is invalid")


def _validate_object(
    object_key: str,
    value: object,
    *,
    commit_shas: set[str],
    last_global_position: int,
) -> dict[str, object]:
    retained = _mapping_copy(value, "object availability")
    expected_fields = {
        "object_sha",
        "object_type",
        "assertion_counts",
        "assertions",
        "observation_count",
        "availability",
        "unavailable_reason",
        "last_observation",
    }
    object_sha = retained.get("object_sha")
    object_type = retained.get("object_type")
    if set(retained) != expected_fields:
        _reject("retained Git graph object state is invalid")
    _commit_sha(object_sha, "object_sha")
    if (
        object_type not in _OBJECT_TYPES
        or object_key != f"{object_type}:{object_sha}"
        or (object_type == "commit" and object_sha not in commit_shas)
    ):
        _reject("retained Git graph object identity is invalid")
    counts = _bounded_counts(
        retained.get("assertion_counts"),
        _AVAILABILITY_VALUES,
        "object availability counts",
    )
    assertions = _mapping_copy(
        retained.get("assertions"),
        "object availability assertions",
    )
    expected_assertions = {key for key, count in counts.items() if count > 0}
    if set(assertions) != expected_assertions:
        _reject("retained Git graph object assertions are invalid")
    for assertion, provenance in assertions.items():
        if assertion not in _AVAILABILITY_VALUES:
            _reject("retained Git graph object assertion is invalid")
        _validate_provenance(
            provenance,
            "object assertion provenance",
            last_global_position,
        )
    observation_count = _positive_int(
        retained.get("observation_count"),
        "object observation count",
    )
    if observation_count != sum(counts.values()):
        _reject("retained Git graph object count is invalid")
    availability = retained.get("availability")
    if availability not in expected_assertions:
        _reject("retained Git graph object availability is invalid")
    reason = retained.get("unavailable_reason")
    if availability == "available":
        if reason is not None:
            _reject("retained available Git object reason is invalid")
    elif reason not in _UNAVAILABLE_REASONS:
        _reject("retained unavailable Git object reason is invalid")
    last_observation = _validate_provenance(
        retained.get("last_observation"),
        "object last observation",
        last_global_position,
    )
    if last_observation != assertions[cast(str, availability)]:
        _reject("retained Git graph object provenance is invalid")
    return retained


def _validate_checkout(
    checkout_alias: str,
    value: object,
    *,
    commit_shas: set[str],
    last_global_position: int,
) -> None:
    _identifier(checkout_alias, "checkout alias")
    checkout = _mapping_copy(value, "checkout")
    expected_fields = {
        "checkout_alias",
        "commit_sha",
        "ref_name",
        "detached",
        "dirty",
        "diff_artifact_id",
        "diff_base_commit_sha",
        "remote_sha256",
        "shallow",
        "boundary_commit_shas",
        "refs",
        "last_observation",
    }
    if set(checkout) != expected_fields or checkout.get("checkout_alias") != checkout_alias:
        _reject("retained Git graph checkout state is invalid")
    checkout_commit_sha = checkout.get("commit_sha")
    _optional_commit_sha(checkout_commit_sha, "checkout commit_sha")
    if checkout_commit_sha is not None and checkout_commit_sha not in commit_shas:
        _reject("retained Git graph checkout commit is invalid")
    ref_name = checkout.get("ref_name")
    if ref_name is not None:
        _git_ref_name(ref_name, "checkout ref_name")
    detached = checkout.get("detached")
    if detached is not None and type(detached) is not bool:
        _reject("retained Git graph checkout detached state is invalid")
    if detached is True and ref_name is not None:
        _reject("retained detached Git graph checkout ref is invalid")
    if detached is False and ref_name is None:
        _reject("retained attached Git graph checkout ref is invalid")
    for field in ("dirty", "shallow"):
        field_value = checkout.get(field)
        if field_value is not None and type(field_value) is not bool:
            _reject(f"retained Git graph checkout {field} is invalid")
    artifact_id = checkout.get("diff_artifact_id")
    if artifact_id is not None:
        _artifact_id(artifact_id, "checkout diff_artifact_id")
    diff_base_commit_sha = checkout.get("diff_base_commit_sha")
    _optional_commit_sha(
        diff_base_commit_sha,
        "checkout diff_base_commit_sha",
    )
    if diff_base_commit_sha is not None and diff_base_commit_sha not in commit_shas:
        _reject("retained Git graph checkout diff base is invalid")
    remote_sha256 = checkout.get("remote_sha256")
    if remote_sha256 is not None:
        _digest(remote_sha256, "checkout remote_sha256")
    boundaries = checkout.get("boundary_commit_shas")
    if (
        type(boundaries) is not list
        or len(boundaries) > GIT_OBSERVATION_MAX_BOUNDARY_COMMITS
        or any(_COMMIT_SHA_RE.fullmatch(item) is None for item in boundaries if type(item) is str)
        or any(type(item) is not str for item in boundaries)
        or boundaries != sorted(set(boundaries))
        or any(item not in commit_shas for item in boundaries)
        or (checkout.get("shallow") is False and boundaries)
    ):
        _reject("retained Git graph shallow boundary state is invalid")
    refs = _mapping_copy(checkout.get("refs"), "checkout refs")
    if len(refs) > GIT_GRAPH_MAX_REFS_PER_CHECKOUT:
        _reject("retained Git graph ref limit is invalid")
    for retained_ref_name, retained_ref in refs.items():
        _validate_ref(
            retained_ref_name,
            retained_ref,
            commit_shas=commit_shas,
            last_global_position=last_global_position,
        )
    _validate_provenance(
        checkout.get("last_observation"),
        "checkout last observation",
        last_global_position,
    )


def _validate_ref(
    ref_name: str,
    value: object,
    *,
    commit_shas: set[str],
    last_global_position: int,
) -> None:
    _git_ref_name(ref_name, "ref name")
    retained = _mapping_copy(value, "ref")
    expected_fields = {
        "ref_name",
        "current_commit_sha",
        "observed_commit_shas",
        "observation_count",
        "moved",
        "movement_classification",
        "last_observation",
    }
    if set(retained) != expected_fields or retained.get("ref_name") != ref_name:
        _reject("retained Git graph ref state is invalid")
    current_commit_sha = retained.get("current_commit_sha")
    _commit_sha(current_commit_sha, "ref current_commit_sha")
    observed = retained.get("observed_commit_shas")
    if (
        type(observed) is not list
        or len(observed) > GIT_GRAPH_MAX_COMMITS
        or any(type(item) is not str or _COMMIT_SHA_RE.fullmatch(item) is None for item in observed)
        or observed != sorted(set(observed))
        or current_commit_sha not in observed
        or any(item not in commit_shas for item in observed)
    ):
        _reject("retained Git graph ref commits are invalid")
    observation_count = _positive_int(
        retained.get("observation_count"),
        "ref observation count",
    )
    if observation_count < len(observed):
        _reject("retained Git graph ref count is invalid")
    moved = len(observed) > 1
    if (
        retained.get("moved") is not moved
        or retained.get("movement_classification")
        != ("unknown" if moved else "unmoved")
    ):
        _reject("retained Git graph ref movement is invalid")
    _validate_provenance(
        retained.get("last_observation"),
        "ref last observation",
        last_global_position,
    )


def _validate_provenance(
    value: object,
    name: str,
    last_global_position: int,
) -> dict[str, object]:
    provenance = _mapping_copy(value, name)
    if set(provenance) != _PROVENANCE_FIELDS:
        _reject(f"retained {name} is invalid")
    _event_id(provenance.get("event_id"), f"{name} event_id")
    _digest(provenance.get("event_sha256"), f"{name} event_sha256")
    for field in (
        "stream_id",
        "source_system",
        "source_record_id",
        "authorization_decision_id",
        "principal_id",
        "agent_client_id",
        "actor_id",
        "trace_id",
        "run_id",
        "checkout_alias",
    ):
        _identifier(provenance.get(field), f"{name} {field}")
    stream_version = _positive_int(
        provenance.get("stream_version"),
        f"{name} stream version",
    )
    if stream_version > GIT_OBSERVATION_MAX_SEQUENCE:
        _reject(f"retained {name} stream version is invalid")
    global_position = _positive_int(
        provenance.get("global_position"),
        f"{name} global position",
    )
    if global_position > last_global_position:
        _reject(f"retained {name} position is invalid")
    for field in ("occurred_at", "recorded_at", "observed_at"):
        _timestamp(provenance.get(field), f"{name} {field}")
    if provenance.get("evidence_quality") not in _EVIDENCE_QUALITIES:
        _reject(f"retained {name} evidence quality is invalid")
    if provenance.get("actor_type") not in _ACTOR_TYPES:
        _reject(f"retained {name} actor type is invalid")
    return provenance


def _identifier(value: object, name: str) -> str:
    if type(value) is not str or _IDENTIFIER_RE.fullmatch(value) is None:
        _reject(f"retained {name} is invalid")
    return value


def _event_id(value: object, name: str) -> str:
    if type(value) is not str or _EVENT_ID_RE.fullmatch(value) is None:
        _reject(f"retained {name} is invalid")
    return value


def _commit_sha(value: object, name: str) -> str:
    if type(value) is not str or _COMMIT_SHA_RE.fullmatch(value) is None:
        _reject(f"retained {name} is invalid")
    return value


def _optional_commit_sha(value: object, name: str) -> None:
    if value is not None:
        _commit_sha(value, name)


def _digest(value: object, name: str) -> str:
    if type(value) is not str or _DIGEST_RE.fullmatch(value) is None:
        _reject(f"retained {name} is invalid")
    return value


def _artifact_id(value: object, name: str) -> str:
    if type(value) is not str or _ARTIFACT_ID_RE.fullmatch(value) is None:
        _reject(f"retained {name} is invalid")
    return value


def _git_ref_name(value: object, name: str) -> str:
    if type(value) is not str or _GIT_REF_NAME_RE.fullmatch(value) is None:
        _reject(f"retained {name} is invalid")
    return value


def _positive_int(value: object, name: str) -> int:
    if type(value) is not int or value < 1:
        _reject(f"retained {name} is invalid")
    return value


def _timestamp(value: object, name: str) -> str:
    if type(value) is not str:
        _reject(f"retained {name} is invalid")
    try:
        canonical = canonical_rfc3339(value)
    except (TypeError, ValueError):
        _reject(f"retained {name} is invalid")
    if canonical != value:
        _reject(f"retained {name} is invalid")
    return value


def _head(
    source: CanonicalEvent,
    reference: GitObservationRecordRef,
) -> dict[str, object]:
    return {
        "stream_version": reference.sequence,
        "event_id": source.event_id,
        "event_sha256": source.event_sha256,
        "global_position": source.global_position,
        "organization_id": source.organization_id,
        "tenant_id": source.tenant_id,
        "repository_id": reference.repository_id,
        "environment_id": source.environment_id,
        "authorization_decision_id": reference.authorization_event_id,
        "trace_id": reference.trace_id,
        "run_id": reference.run_id,
        "checkout_alias": reference.checkout_alias,
    }


def _scope(source: CanonicalEvent) -> dict[str, object]:
    return {
        "organization_id": source.organization_id,
        "tenant_id": source.tenant_id,
        "repository_id": source.repository_id,
        "environment_id": source.environment_id,
    }


def _provenance(
    source: CanonicalEvent,
    reference: GitObservationRecordRef,
) -> dict[str, object]:
    return {
        "event_id": source.event_id,
        "event_sha256": source.event_sha256,
        "stream_id": source.stream_id,
        "stream_version": source.stream_version,
        "global_position": source.global_position,
        "occurred_at": reference.occurred_at,
        "recorded_at": source.recorded_at,
        "observed_at": reference.source.observed_at,
        "evidence_quality": reference.source.evidence_quality,
        "source_system": reference.source.source_system,
        "source_record_id": reference.source.source_record_id,
        "authorization_decision_id": reference.authorization_event_id,
        "principal_id": source.principal_id,
        "agent_client_id": source.agent_client_id,
        "actor_type": source.actor_type,
        "actor_id": source.actor_id,
        "trace_id": reference.trace_id,
        "run_id": reference.run_id,
        "checkout_alias": reference.checkout_alias,
    }


def _bounded_counts(
    value: object,
    keys: tuple[str, ...],
    name: str,
) -> dict[str, int]:
    counts = _mapping_copy(value, name)
    if set(counts) != set(keys):
        _reject(f"retained {name} are invalid")
    result: dict[str, int] = {}
    for key in keys:
        count = counts[key]
        if type(count) is not int or count < 0:
            _reject(f"retained {name} are invalid")
        result[key] = count
    return result


def _mapping_copy(value: object, name: str) -> dict[str, object]:
    if not isinstance(value, Mapping):
        _reject(f"{name} projection state is invalid")
    copied = _thaw_json(value)
    if type(copied) is not dict:
        _reject(f"{name} projection state is invalid")
    return cast(dict[str, object], copied)


def _thaw_json(value: object) -> object:
    if isinstance(value, Mapping):
        return {key: _thaw_json(item) for key, item in value.items()}
    if type(value) in {list, tuple}:
        return [_thaw_json(item) for item in value]
    return value


def _canonical_json_bytes(value: object) -> bytes:
    return json.dumps(
        value,
        ensure_ascii=False,
        allow_nan=False,
        sort_keys=True,
        separators=(",", ":"),
    ).encode("utf-8")


def _domain_sha256(domain: bytes, value: object) -> str:
    return "sha256:" + hashlib.sha256(domain + _canonical_json_bytes(value)).hexdigest()


def _reject(message: str) -> NoReturn:
    raise ReducerExecutionError(
        "TBM_GIT_GRAPH_REDUCER_EVENT_INVALID",
        message,
    )


__all__ = [
    "GIT_GRAPH_MAX_CHECKOUTS",
    "GIT_GRAPH_MAX_COMMITS",
    "GIT_GRAPH_MAX_OBJECTS",
    "GIT_GRAPH_MAX_REFS_PER_CHECKOUT",
    "GIT_GRAPH_MAX_RELATIONS",
    "GIT_GRAPH_PROJECTION_NAME",
    "GIT_GRAPH_PROJECTION_SCHEMA_VERSION",
    "GIT_GRAPH_REDUCER_ID",
    "build_git_graph_reducer",
    "projected_git_graph",
]
