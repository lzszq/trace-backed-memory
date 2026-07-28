from dataclasses import replace
import json
from pathlib import Path

import pytest

import trace_backed_memory as tbm
import trace_backed_memory.cli as cli
import trace_backed_memory.migration_v3 as migration_v3


def _empty_mapping(
    *,
    ancestry_mode: str = "disabled",
) -> tbm.SnapshotV3MigrationMapping:
    if ancestry_mode == "disabled":
        ancestry = tbm.AncestryPolicy(
            mode="disabled",
            bypass_reason="no commit-bearing evidence exists in this fixture",
        )
    else:
        ancestry = tbm.AncestryPolicy(mode="required")
    return tbm.SnapshotV3MigrationMapping(
        repositories=(),
        tenants=(),
        trace_bindings=(),
        memory_scopes=(),
        regression_evidence=(),
        global_policy_approvals=(),
        ancestry_policy=ancestry,
    )


def _empty_bundle() -> tbm.SnapshotV3MigrationBundle:
    return tbm.create_snapshot_v3_migration_bundle(
        tbm.TraceBackedMemoryStore(),
        _empty_mapping(),
    )


def test_v3_bundle_round_trip_is_deterministic_and_inert():
    bundle = _empty_bundle()

    assert bundle.bundle_version == "tbm.snapshot.v2-to-v3.bundle.v1"
    assert bundle.ready is True
    assert bundle.state == "ready"
    assert bundle.plan.ready is True
    assert {issue.code for issue in bundle.plan.issues} == {
        "TBM_V3_ANCESTRY_DISABLED"
    }
    assert bundle.source_snapshot_version == 2
    assert bundle.target_snapshot_version == 3
    assert bundle.source_snapshot["snapshot_version"] == 2

    encoded = tbm.dumps_snapshot_v3_migration_bundle(bundle)
    parsed = tbm.loads_snapshot_v3_migration_bundle(encoded)
    reparsed = tbm.parse_snapshot_v3_migration_bundle(bundle.to_dict())

    assert parsed == bundle
    assert reparsed == bundle
    assert tbm.dumps_snapshot_v3_migration_bundle(parsed) == encoded
    assert tbm.verify_snapshot_v3_migration_bundle(parsed) == bundle.plan


def test_v3_bundle_freezes_source_and_mapping_inputs():
    store = tbm.TraceBackedMemoryStore()
    mapping_payload = _empty_mapping().to_dict()
    bundle = tbm.create_snapshot_v3_migration_bundle(
        store.to_snapshot(),
        mapping_payload,
    )

    mapping_payload["repositories"].append({"unexpected": True})
    source = bundle.source_snapshot
    source["traces"].append({"unexpected": True})

    assert bundle.mapping.repositories == ()
    assert bundle.source_snapshot["traces"] == []
    assert tbm.verify_snapshot_v3_migration_bundle(bundle) == bundle.plan


def test_v3_bundle_distinguishes_explicit_and_normalized_source_hashes():
    store = tbm.TraceBackedMemoryStore()
    first = store.record_trace(
        tbm.Trace(
            trace_id="trace_a",
            run_id="run_a",
            commit_sha="commit",
            repo="repo",
            tenant="tenant",
        )
    )
    second = store.record_trace(
        tbm.Trace(
            trace_id="trace_b",
            run_id="run_b",
            commit_sha="commit",
            repo="repo",
            tenant="tenant",
        )
    )
    repository = tbm.CanonicalRepository(
        repository_id="repository",
        provider="local",
        provider_repository_id="repository",
        canonical_locator_hash="sha256:" + "1" * 64,
        display_name="repo",
        legacy_aliases=("repo",),
    )
    tenant = tbm.TenantIdentity(
        tenant_id="tenant_id",
        display_name="tenant",
        legacy_aliases=("tenant",),
    )
    mapping = replace(
        _empty_mapping(),
        repositories=(repository,),
        tenants=(tenant,),
        trace_bindings=(
            tbm.TraceIdentityBinding(
                trace_id=first.trace_id,
                repository_id=repository.repository_id,
                tenant_id=tenant.tenant_id,
            ),
            tbm.TraceIdentityBinding(
                trace_id=second.trace_id,
                repository_id=repository.repository_id,
                tenant_id=tenant.tenant_id,
            ),
        ),
    )
    canonical_source = store.to_snapshot()
    reversed_source = {
        **canonical_source,
        "traces": list(reversed(canonical_source["traces"])),
    }

    canonical = tbm.create_snapshot_v3_migration_bundle(
        canonical_source,
        mapping,
    )
    reversed_bundle = tbm.create_snapshot_v3_migration_bundle(
        reversed_source,
        mapping,
    )

    assert canonical.source_snapshot_sha256 != (
        reversed_bundle.source_snapshot_sha256
    )
    assert canonical.normalized_source_snapshot_sha256 == (
        reversed_bundle.normalized_source_snapshot_sha256
    )
    assert canonical.bundle_id != reversed_bundle.bundle_id


@pytest.mark.parametrize(
    ("field_name", "replacement", "code"),
    [
        (
            "bundle_id",
            "sha256:" + "0" * 64,
            "TBM_V3_BUNDLE_ID_MISMATCH",
        ),
        (
            "source_snapshot_sha256",
            "sha256:" + "0" * 64,
            "TBM_V3_BUNDLE_SOURCE_HASH_MISMATCH",
        ),
        (
            "normalized_source_snapshot_sha256",
            "sha256:" + "0" * 64,
            "TBM_V3_BUNDLE_NORMALIZED_SOURCE_HASH_MISMATCH",
        ),
        (
            "mapping_sha256",
            "sha256:" + "0" * 64,
            "TBM_V3_BUNDLE_MAPPING_HASH_MISMATCH",
        ),
        (
            "plan_sha256",
            "sha256:" + "0" * 64,
            "TBM_V3_BUNDLE_PLAN_HASH_MISMATCH",
        ),
        (
            "state",
            "blocked",
            "TBM_V3_BUNDLE_STATE_MISMATCH",
        ),
    ],
)
def test_v3_bundle_rejects_tampered_manifest_fields(
    field_name,
    replacement,
    code,
):
    payload = _empty_bundle().to_dict()
    payload[field_name] = replacement

    with pytest.raises(tbm.V3MigrationBundleError) as error:
        tbm.parse_snapshot_v3_migration_bundle(payload)

    assert error.value.code == code


def test_v3_bundle_rejects_tampered_embedded_documents():
    payload = _empty_bundle().to_dict()
    payload["source_snapshot"]["traces"].append({"unexpected": True})
    with pytest.raises(tbm.V3MigrationBundleError) as error:
        tbm.parse_snapshot_v3_migration_bundle(payload)
    assert error.value.code == "TBM_V3_INVALID_BUNDLE_SOURCE"

    payload = _empty_bundle().to_dict()
    payload["mapping"]["repositories"].append({"unexpected": True})
    with pytest.raises(tbm.V3MigrationBundleError):
        tbm.parse_snapshot_v3_migration_bundle(payload)

    payload = _empty_bundle().to_dict()
    payload["plan"]["counts"]["warnings"] = 0
    with pytest.raises(tbm.V3MigrationBundleError):
        tbm.parse_snapshot_v3_migration_bundle(payload)


def test_v3_bundle_strict_json_rejects_duplicates_nonfinite_and_unicode():
    encoded = tbm.dumps_snapshot_v3_migration_bundle(_empty_bundle())
    duplicate = encoded.replace(
        '"bundle_version":',
        '"bundle_version":"duplicate","bundle_version":',
        1,
    )
    with pytest.raises(tbm.V3MigrationBundleError) as error:
        tbm.loads_snapshot_v3_migration_bundle(duplicate)
    assert error.value.code == "TBM_V3_INVALID_BUNDLE_JSON"

    with pytest.raises(tbm.V3MigrationBundleError) as error:
        tbm.loads_snapshot_v3_migration_bundle('{"value":NaN}')
    assert error.value.code == "TBM_V3_INVALID_BUNDLE_JSON"

    payload = _empty_bundle().to_dict()
    payload["mapping"]["ancestry_policy"]["bypass_reason"] = "\ud800"
    with pytest.raises(tbm.V3MigrationBundleError) as error:
        tbm.parse_snapshot_v3_migration_bundle(payload)
    assert error.value.code in {
        "TBM_V3_BUNDLE_NON_CANONICAL_JSON",
        "TBM_V3_INVALID_CONTRACT",
    }


def test_v3_bundle_required_ancestry_replay_requires_same_trust_port():
    mapping = _empty_mapping(ancestry_mode="required")

    def verifier(_repository_id, _relation):
        return True

    bundle = tbm.create_snapshot_v3_migration_bundle(
        tbm.TraceBackedMemoryStore(),
        mapping,
        commit_relation_verifier=verifier,
    )

    assert bundle.ready is True
    with pytest.raises(tbm.V3MigrationBundleError) as error:
        tbm.verify_snapshot_v3_migration_bundle(bundle)
    assert error.value.code == "TBM_V3_BUNDLE_PLAN_REPLAY_MISMATCH"
    assert (
        tbm.verify_snapshot_v3_migration_bundle(
            bundle,
            commit_relation_verifier=verifier,
        )
        == bundle.plan
    )


def test_v3_plan_parser_rejects_unknown_counts_and_unbounded_values():
    plan_payload = _empty_bundle().plan.to_dict()

    assert tbm.parse_v3_migration_plan(plan_payload) == _empty_bundle().plan

    plan_payload["counts"]["unknown"] = 0
    with pytest.raises(tbm.V3ContractError, match="unknown field"):
        tbm.parse_v3_migration_plan(plan_payload)

    plan_payload = _empty_bundle().plan.to_dict()
    plan_payload["counts"]["traces"] = tbm.V3_MAX_MIGRATION_COUNT + 1
    with pytest.raises(tbm.V3ContractError, match="250000"):
        tbm.parse_v3_migration_plan(plan_payload)


def test_v3_bundle_file_loader_is_bounded_and_strict(tmp_path):
    path = tmp_path / "bundle.json"
    bundle = _empty_bundle()
    path.write_text(
        tbm.dumps_snapshot_v3_migration_bundle(bundle),
        encoding="utf-8",
    )

    assert tbm.load_snapshot_v3_migration_bundle(path) == bundle

    path.write_text('{"bundle_version":"broken"}', encoding="utf-8")
    with pytest.raises(tbm.V3MigrationBundleError):
        tbm.load_snapshot_v3_migration_bundle(path)


def test_v3_bundle_cli_creates_and_verifies_inert_bundle(tmp_path, capsys):
    snapshot_path = tmp_path / "snapshot.json"
    mapping_path = tmp_path / "mapping.json"
    bundle_path = tmp_path / "bundle.json"
    tbm.TraceBackedMemoryStore().save_json(snapshot_path)
    mapping_path.write_text(
        json.dumps(_empty_mapping().to_dict()),
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "migration",
                "bundle-v3",
                str(snapshot_path),
                str(mapping_path),
            ]
        )
        == 0
    )
    encoded = capsys.readouterr().out
    bundle = tbm.loads_snapshot_v3_migration_bundle(encoded)
    bundle_path.write_text(encoded, encoding="utf-8")

    assert bundle.ready is True
    assert not cli._snapshot_lock_path(snapshot_path).exists()
    assert (
        cli.main(
            [
                "migration",
                "verify-v3-bundle",
                str(bundle_path),
            ]
        )
        == 0
    )
    verification = json.loads(capsys.readouterr().out)
    assert verification == {
        "bundle_id": bundle.bundle_id,
        "plan_sha256": bundle.plan_sha256,
        "state": "ready",
        "verified": True,
    }


def test_v3_bundle_cli_preserves_exact_source_record_order(tmp_path, capsys):
    store = tbm.TraceBackedMemoryStore()
    for suffix in ("a", "b"):
        store.record_trace(
            tbm.Trace(
                trace_id=f"trace_{suffix}",
                run_id=f"run_{suffix}",
                commit_sha="commit",
                repo="repo",
                tenant="tenant",
            )
        )
    source = store.to_snapshot()
    source["traces"].reverse()
    snapshot_path = tmp_path / "reordered-snapshot.json"
    mapping_path = tmp_path / "mapping.json"
    snapshot_path.write_text(json.dumps(source), encoding="utf-8")
    mapping_path.write_text(
        json.dumps(_empty_mapping().to_dict()),
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "migration",
                "bundle-v3",
                str(snapshot_path),
                str(mapping_path),
            ]
        )
        == 0
    )
    bundle = tbm.loads_snapshot_v3_migration_bundle(
        capsys.readouterr().out
    )

    assert bundle.source_snapshot["traces"][0]["trace_id"] == "trace_b"
    assert bundle.source_snapshot_sha256 != (
        bundle.normalized_source_snapshot_sha256
    )


def test_v3_bundle_cli_reports_missing_required_trust_port_as_state(
    tmp_path,
    capsys,
):
    def verifier(_repository_id, _relation):
        return True

    bundle = tbm.create_snapshot_v3_migration_bundle(
        tbm.TraceBackedMemoryStore(),
        _empty_mapping(ancestry_mode="required"),
        commit_relation_verifier=verifier,
    )
    bundle_path = tmp_path / "bundle.json"
    bundle_path.write_text(
        tbm.dumps_snapshot_v3_migration_bundle(bundle),
        encoding="utf-8",
    )

    assert (
        cli.main(
            [
                "migration",
                "verify-v3-bundle",
                str(bundle_path),
            ]
        )
        == 3
    )
    error = json.loads(capsys.readouterr().err)["error"]
    assert error["kind"] == "state"
    assert error["code"] == "TBM_V3_BUNDLE_PLAN_REPLAY_MISMATCH"


def test_v3_bundle_schema_publishes_closed_inert_contract():
    root = Path(__file__).resolve().parents[1]
    schema = json.loads(
        (
            root
            / "schemas"
            / "snapshot_v3_migration_bundle.schema.json"
        ).read_text(encoding="utf-8")
    )
    example = json.loads(
        (
            root
            / "examples"
            / "snapshot_v3_migration_bundle.example.json"
        ).read_text(encoding="utf-8")
    )

    assert schema["additionalProperties"] is False
    assert schema["properties"]["bundle_version"]["const"] == (
        tbm.V3_MIGRATION_BUNDLE_VERSION
    )
    assert schema["properties"]["source_snapshot_version"]["const"] == 2
    assert schema["properties"]["target_snapshot_version"]["const"] == 3
    plan_schema = json.loads(
        (
            root
            / "schemas"
            / "snapshot_v3_migration_plan.schema.json"
        ).read_text(encoding="utf-8")
    )
    assert plan_schema["$defs"]["count"]["maximum"] == (
        tbm.V3_MAX_MIGRATION_COUNT
    )
    assert tbm.parse_snapshot_v3_migration_bundle(example).to_dict() == (
        example
    )


def test_v3_bundle_public_exports_are_intentional():
    for name in (
        "SnapshotV3MigrationBundle",
        "V3MigrationBundleError",
        "create_snapshot_v3_migration_bundle",
        "dumps_snapshot_v3_migration_bundle",
        "load_snapshot_v3_migration_bundle",
        "loads_snapshot_v3_migration_bundle",
        "parse_snapshot_v3_migration_bundle",
        "verify_snapshot_v3_migration_bundle",
    ):
        assert name in tbm.__all__


@pytest.mark.parametrize(
    ("field", "value"),
    [
        ("bundle_version", "bad"),
        ("source_snapshot_version", 1),
        ("target_snapshot_version", 2),
        ("state", "invalid"),
        ("state", "blocked"),
        ("bundle_id", "invalid"),
        ("mapping", object()),
        ("plan", object()),
        ("_source_snapshot_json", object()),
        ("_source_snapshot_json", '{"snapshot_version":1}'),
    ],
)
def test_v3_bundle_record_rejects_invalid_field_shapes(field, value):
    bundle = _empty_bundle()
    with pytest.raises(tbm.V3MigrationBundleError):
        replace(bundle, **{field: value})


@pytest.mark.parametrize("field", ["source_snapshot", "mapping", "plan"])
def test_v3_bundle_parser_requires_nested_objects(field):
    payload = _empty_bundle().to_dict()
    payload[field] = None
    with pytest.raises(tbm.V3MigrationBundleError):
        tbm.parse_snapshot_v3_migration_bundle(payload)


def test_v3_bundle_public_helpers_reject_wrong_types_and_missing_files(
    tmp_path,
):
    with pytest.raises(tbm.V3MigrationBundleError):
        tbm.dumps_snapshot_v3_migration_bundle(object())
    with pytest.raises(tbm.V3MigrationBundleError):
        tbm.verify_snapshot_v3_migration_bundle(object())
    with pytest.raises(tbm.V3MigrationBundleError, match="failed to read"):
        tbm.load_snapshot_v3_migration_bundle(tmp_path / "missing.json")


@pytest.mark.parametrize(
    "operation",
    [
        lambda: migration_v3._freeze_source_snapshot(object()),
        lambda: migration_v3._freeze_source_snapshot(
            {"snapshot_version": 1}
        ),
        lambda: migration_v3._freeze_source_snapshot(
            {"snapshot_version": 2, "traces": "invalid"}
        ),
        lambda: migration_v3._freeze_mapping(object()),
        lambda: migration_v3._freeze_mapping({"mapping_version": "bad"}),
        lambda: migration_v3._freeze_object(
            {"value": object()},
            "test",
        ),
        lambda: migration_v3._bundle_sha256(object()),
        lambda: migration_v3._canonical_json(float("nan")),
        lambda: migration_v3._canonical_json(object()),
        lambda: migration_v3._parse_json_object(
            "[]",
            description="test",
            max_bytes=100,
            max_nodes=100,
        ),
        lambda: migration_v3._parse_json_object(
            "NaN",
            description="test",
            max_bytes=100,
            max_nodes=100,
        ),
        lambda: migration_v3._validate_json_tree(
            json.loads("[" * 102 + "null" + "]" * 102),
            description="test",
            max_nodes=200,
        ),
        lambda: migration_v3._validate_json_tree(
            [1, 2],
            description="test",
            max_nodes=1,
        ),
        lambda: migration_v3._validate_json_tree(
            "\ud800",
            description="test",
            max_nodes=10,
        ),
        lambda: migration_v3._validate_json_tree(
            {1: "value"},
            description="test",
            max_nodes=10,
        ),
        lambda: migration_v3._validate_json_tree(
            {"\ud800": "value"},
            description="test",
            max_nodes=10,
        ),
        lambda: migration_v3._closed_object(
            object(),
            "test",
            {"field"},
        ),
        lambda: migration_v3._closed_object({}, "test", {"field"}),
        lambda: migration_v3._closed_object(
            {"field": 1, "extra": 2},
            "test",
            {"field"},
        ),
        lambda: migration_v3._required_string("", "field"),
        lambda: migration_v3._required_integer("1", "field"),
        lambda: migration_v3._bundle_digest("invalid", "field"),
        lambda: migration_v3._bounded_text(
            "\ud800",
            description="test",
            max_bytes=10,
        ),
        lambda: migration_v3._bounded_text(
            "too long",
            description="test",
            max_bytes=1,
        ),
    ],
)
def test_v3_bundle_strict_helpers_fail_closed(operation):
    with pytest.raises(tbm.V3MigrationBundleError):
        operation()
