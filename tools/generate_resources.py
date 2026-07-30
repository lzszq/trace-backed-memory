from __future__ import annotations

import argparse
import ast
import hashlib
import json
import os
from pathlib import Path
import tempfile
from typing import Any, NoReturn


ROOT = Path(__file__).resolve().parents[1]
MANIFEST_PATH = ROOT / "resources" / "manifest.json"
RESOURCE_MODULE_PATH = ROOT / "src" / "trace_backed_memory" / "resources.py"
PYPROJECT_PATH = ROOT / "pyproject.toml"
INSTALLED_ROOT = ROOT / "src" / "trace_backed_memory" / "_resources"
INSTALLED_MANIFEST_PATH = (
    ROOT / "src" / "trace_backed_memory" / "_resource_manifest.json"
)
ENGLISH_INDEX_PATH = ROOT / "docs" / "resources.md"
CHINESE_INDEX_PATH = ROOT / "docs" / "resources.zh-CN.md"
MANIFEST_VERSION = "tbm.resource-manifest.v1"
RESOURCE_BEGIN = "# BEGIN GENERATED RESOURCE SPECS"
RESOURCE_END = "# END GENERATED RESOURCE SPECS"
PACKAGE_BEGIN = "# BEGIN GENERATED RESOURCE PACKAGE DATA"
PACKAGE_END = "# END GENERATED RESOURCE PACKAGE DATA"
KINDS = {"schema", "memory", "example"}


class ResourceManifestError(RuntimeError):
    pass


def _parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate or verify the canonical packaged-resource manifest."
    )
    mode = parser.add_mutually_exclusive_group()
    mode.add_argument(
        "--check",
        action="store_true",
        help="verify the manifest and every generated/copy representation",
    )
    mode.add_argument(
        "--write",
        action="store_true",
        help="write generated representations from the existing manifest",
    )
    mode.add_argument(
        "--refresh",
        action="store_true",
        help="refresh listed sizes/digests, then write generated outputs",
    )
    mode.add_argument(
        "--bootstrap",
        action="store_true",
        help="create the first manifest from the current strict runtime tuple",
    )
    return parser


def _reject_duplicates(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ResourceManifestError(f"duplicate JSON key: {key}")
        result[key] = value
    return result


def _canonical_paths() -> tuple[Path, ...]:
    paths: list[Path] = []
    resolved_root = ROOT.resolve()
    for directory in ("examples", "memory", "schemas"):
        for path in (ROOT / directory).rglob("*"):
            if path.is_symlink():
                raise ResourceManifestError(
                    "canonical resources may not be symbolic links: "
                    f"{path.relative_to(ROOT).as_posix()}"
                )
            if (
                path.is_file()
                and path.name != "AGENTS.md"
                and "__pycache__" not in path.parts
            ):
                resolved = path.resolve()
                if not resolved.is_relative_to(resolved_root):
                    raise ResourceManifestError(
                        "canonical resource escapes repository root: "
                        f"{path.relative_to(ROOT).as_posix()}"
                    )
                paths.append(path)
    return tuple(
        sorted(
            paths,
            key=lambda path: path.relative_to(ROOT).as_posix(),
        )
    )


def _digest(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def _entry(
    name: str,
    kind: str,
    media_type: str,
    data: bytes,
) -> dict[str, object]:
    return {
        "name": name,
        "kind": kind,
        "media_type": media_type,
        "source": name,
        "installed": (
            "src/trace_backed_memory/_resources/" + name
        ),
        "size_bytes": len(data),
        "sha256": _digest(data),
    }


def _bootstrap_manifest() -> dict[str, object]:
    if MANIFEST_PATH.exists():
        raise ResourceManifestError("resource manifest already exists")
    module = ast.parse(RESOURCE_MODULE_PATH.read_text(encoding="utf-8"))
    declarations = [
        node.value
        for node in module.body
        if isinstance(node, ast.AnnAssign)
        and isinstance(node.target, ast.Name)
        and node.target.id == "_RESOURCE_SPECS"
    ]
    if len(declarations) != 1 or not isinstance(declarations[0], ast.Tuple):
        raise ResourceManifestError("runtime resource tuple is invalid")
    specs: dict[str, tuple[str, str]] = {}
    for node in declarations[0].elts:
        value = ast.literal_eval(node)
        if (
            type(value) is not tuple
            or len(value) != 3
            or any(type(item) is not str for item in value)
        ):
            raise ResourceManifestError("runtime resource entry is invalid")
        name, kind, media_type = value
        if name in specs:
            raise ResourceManifestError(f"duplicate runtime resource: {name}")
        specs[name] = (kind, media_type)

    canonical = {
        path.relative_to(ROOT).as_posix(): path
        for path in _canonical_paths()
    }
    if set(specs) != set(canonical):
        raise ResourceManifestError(
            "runtime resource tuple does not match canonical files"
        )
    entries = [
        _entry(name, *specs[name], canonical[name].read_bytes())
        for name in sorted(specs)
    ]
    return {
        "manifest_version": MANIFEST_VERSION,
        "resources": entries,
    }


def _load_manifest() -> dict[str, object]:
    try:
        value = json.loads(
            MANIFEST_PATH.read_text(encoding="utf-8"),
            object_pairs_hook=_reject_duplicates,
            parse_constant=lambda value: _fail(
                f"non-finite JSON value: {value}"
            ),
        )
    except (OSError, UnicodeError, json.JSONDecodeError) as error:
        raise ResourceManifestError("resource manifest could not be read") from error
    if type(value) is not dict:
        raise ResourceManifestError("resource manifest must be an object")
    return value


def _manifest_entries(
    manifest: dict[str, object],
    *,
    verify_digests: bool,
) -> tuple[dict[str, object], ...]:
    if set(manifest) != {"manifest_version", "resources"}:
        raise ResourceManifestError("resource manifest fields are invalid")
    if manifest["manifest_version"] != MANIFEST_VERSION:
        raise ResourceManifestError("resource manifest version is unsupported")
    raw_entries = manifest["resources"]
    if type(raw_entries) is not list or not raw_entries:
        raise ResourceManifestError("resource manifest entries are invalid")

    entries: list[dict[str, object]] = []
    names: list[str] = []
    exact_fields = {
        "name",
        "kind",
        "media_type",
        "source",
        "installed",
        "size_bytes",
        "sha256",
    }
    for raw in raw_entries:
        if type(raw) is not dict or set(raw) != exact_fields:
            raise ResourceManifestError("resource manifest entry fields are invalid")
        name = raw["name"]
        kind = raw["kind"]
        media_type = raw["media_type"]
        source = raw["source"]
        installed = raw["installed"]
        size_bytes = raw["size_bytes"]
        sha256 = raw["sha256"]
        if (
            type(name) is not str
            or not name
            or name != source
            or "\\" in name
            or name.startswith("/")
            or ".." in Path(name).parts
            or name.split("/", 1)[0] not in {"examples", "memory", "schemas"}
            or kind not in KINDS
            or type(media_type) is not str
            or not media_type
            or installed != "src/trace_backed_memory/_resources/" + name
            or type(size_bytes) is not int
            or size_bytes < 0
            or type(sha256) is not str
            or len(sha256) != 64
            or any(character not in "0123456789abcdef" for character in sha256)
        ):
            raise ResourceManifestError(
                f"resource manifest entry is invalid: {name!r}"
            )
        names.append(name)
        entries.append(raw)

    if names != sorted(names) or len(names) != len(set(names)):
        raise ResourceManifestError(
            "resource manifest names must be unique and sorted"
        )
    canonical_names = tuple(
        path.relative_to(ROOT).as_posix() for path in _canonical_paths()
    )
    if tuple(names) != canonical_names:
        raise ResourceManifestError(
            "resource manifest does not match canonical resource files"
        )
    if verify_digests:
        for entry in entries:
            data = (ROOT / str(entry["source"])).read_bytes()
            if (
                len(data) != entry["size_bytes"]
                or _digest(data) != entry["sha256"]
            ):
                raise ResourceManifestError(
                    f"canonical resource digest drift: {entry['name']}"
                )
    return tuple(entries)


def _refresh_manifest(
    manifest: dict[str, object],
) -> dict[str, object]:
    entries = _manifest_entries(manifest, verify_digests=False)
    return {
        "manifest_version": MANIFEST_VERSION,
        "resources": [
            _entry(
                str(entry["name"]),
                str(entry["kind"]),
                str(entry["media_type"]),
                (ROOT / str(entry["source"])).read_bytes(),
            )
            for entry in entries
        ],
    }


def _render_manifest(manifest: dict[str, object]) -> str:
    return json.dumps(
        manifest,
        ensure_ascii=False,
        indent=2,
    ) + "\n"


def _render_runtime(entries: tuple[dict[str, object], ...]) -> str:
    lines = [
        "_RESOURCE_SPECS: tuple[",
        '    tuple[str, Literal["schema", "memory", "example"], str],',
        "    ...,",
        "] = (",
    ]
    for entry in entries:
        lines.extend(
            (
                "    (",
                f'        "{entry["name"]}",',
                f'        "{entry["kind"]}",',
                f'        "{entry["media_type"]}",',
                "    ),",
            )
        )
    lines.append(")")
    return "\n".join(lines)


def _render_package_data(entries: tuple[dict[str, object], ...]) -> str:
    lines = [
        "trace_backed_memory = [",
        '    "py.typed",',
        '    "_resource_manifest.json",',
    ]
    lines.extend(
        f'    "_resources/{entry["name"]}",' for entry in entries
    )
    lines.append("]")
    return "\n".join(lines)


def _render_index(
    entries: tuple[dict[str, object], ...],
    *,
    chinese: bool,
) -> str:
    if chinese:
        heading = "# Packaged resource 索引"
        language = "[English](resources.md) | **简体中文**"
        intro = (
            "本文件由 `resources/manifest.json` 确定性生成，请勿手工编辑。"
        )
        count = f"当前共有 **{len(entries)}** 份严格 allowlisted resource。"
    else:
        heading = "# Packaged resource index"
        language = "**English** | [简体中文](resources.zh-CN.md)"
        intro = (
            "This file is generated deterministically from "
            "`resources/manifest.json`; do not edit it by hand."
        )
        count = f"The strict allowlist currently contains **{len(entries)}** resources."
    lines = [
        heading,
        "",
        language,
        "",
        intro,
        "",
        count,
        "",
        "| Name | Kind | Media type | Bytes | SHA-256 |",
        "|---|---|---|---:|---|",
    ]
    lines.extend(
        "| `{name}` | `{kind}` | `{media}` | {size} | `{digest}` |".format(
            name=entry["name"],
            kind=entry["kind"],
            media=entry["media_type"],
            size=entry["size_bytes"],
            digest=entry["sha256"],
        )
        for entry in entries
    )
    lines.append("")
    return "\n".join(lines)


def _replace_generated(
    source: str,
    begin: str,
    end: str,
    generated: str,
) -> str:
    begin_token = begin + "\n"
    end_token = "\n" + end
    if source.count(begin_token) != 1 or source.count(end_token) != 1:
        raise ResourceManifestError(
            f"generated markers are invalid: {begin}"
        )
    prefix, remainder = source.split(begin_token, 1)
    _, suffix = remainder.split(end_token, 1)
    return prefix + begin_token + generated + end_token + suffix


def _expected_outputs(
    entries: tuple[dict[str, object], ...],
) -> dict[Path, str]:
    resources_source = RESOURCE_MODULE_PATH.read_text(encoding="utf-8")
    pyproject_source = PYPROJECT_PATH.read_text(encoding="utf-8")
    return {
        RESOURCE_MODULE_PATH: _replace_generated(
            resources_source,
            RESOURCE_BEGIN,
            RESOURCE_END,
            _render_runtime(entries),
        ),
        PYPROJECT_PATH: _replace_generated(
            pyproject_source,
            PACKAGE_BEGIN,
            PACKAGE_END,
            _render_package_data(entries),
        ),
        ENGLISH_INDEX_PATH: _render_index(entries, chinese=False),
        CHINESE_INDEX_PATH: _render_index(entries, chinese=True),
    }


def _atomic_write(path: Path, data: bytes) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    descriptor, temporary_name = tempfile.mkstemp(
        dir=path.parent,
        prefix=f".{path.name}.",
        suffix=".tmp",
    )
    temporary = Path(temporary_name)
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(data)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        try:
            temporary.unlink()
        except FileNotFoundError:
            pass


def _write_outputs(
    manifest: dict[str, object],
    entries: tuple[dict[str, object], ...],
) -> None:
    for path, text in _expected_outputs(entries).items():
        _atomic_write(path, text.encode("utf-8"))
    _atomic_write(
        INSTALLED_MANIFEST_PATH,
        _render_manifest(manifest).encode("utf-8"),
    )
    for entry in entries:
        source = ROOT / str(entry["source"])
        installed = ROOT / str(entry["installed"])
        _atomic_write(installed, source.read_bytes())


def _check_outputs(
    manifest: dict[str, object],
    entries: tuple[dict[str, object], ...],
) -> None:
    for path, expected in _expected_outputs(entries).items():
        try:
            actual = path.read_text(encoding="utf-8")
        except (OSError, UnicodeError) as error:
            raise ResourceManifestError(
                f"generated resource output is missing: {path.relative_to(ROOT)}"
            ) from error
        if actual != expected:
            raise ResourceManifestError(
                f"generated resource output drift: {path.relative_to(ROOT)}"
            )
    try:
        installed_manifest = INSTALLED_MANIFEST_PATH.read_bytes()
    except OSError as error:
        raise ResourceManifestError(
            "installed resource manifest is missing"
        ) from error
    if installed_manifest != _render_manifest(manifest).encode("utf-8"):
        raise ResourceManifestError("installed resource manifest byte drift")
    for entry in entries:
        source = ROOT / str(entry["source"])
        installed = ROOT / str(entry["installed"])
        try:
            installed_bytes = installed.read_bytes()
        except OSError as error:
            raise ResourceManifestError(
                f"installed resource is missing: {entry['name']}"
            ) from error
        if installed_bytes != source.read_bytes():
            raise ResourceManifestError(
                f"installed resource byte drift: {entry['name']}"
            )


def _fail(message: str) -> NoReturn:
    raise ResourceManifestError(message)


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        if args.bootstrap:
            manifest = _bootstrap_manifest()
            _atomic_write(
                MANIFEST_PATH,
                _render_manifest(manifest).encode("utf-8"),
            )
        else:
            manifest = _load_manifest()
        if args.refresh:
            manifest = _refresh_manifest(manifest)
            _atomic_write(
                MANIFEST_PATH,
                _render_manifest(manifest).encode("utf-8"),
            )
        entries = _manifest_entries(manifest, verify_digests=True)
        if args.write or args.refresh or args.bootstrap:
            _write_outputs(manifest, entries)
        _check_outputs(manifest, entries)
    except ResourceManifestError as error:
        print(f"resource manifest verification failed: {error}")
        return 1
    print(f"resource manifest verified: {len(entries)} resources")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
