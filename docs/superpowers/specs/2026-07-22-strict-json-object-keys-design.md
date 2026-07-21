# Strict JSON Object Key Uniqueness Design

## Summary

Python's `json.loads()` keeps the last value when one JSON object repeats a
key. The CLI file parser already rejects that ambiguity, but snapshot loading
and the direct memory-context and memory-decision JSON parsers still accept it.
That lets a later snapshot field or LLM Gate field silently replace an earlier
value before normal validation begins.

Phase 43 makes every runtime parser for caller-owned JSON reject duplicate
object keys at every nesting level. Mapping inputs remain unchanged because a
Python mapping cannot preserve duplicate keys.

## Shared Parsing Primitive

Add one private helper in `_ingestion.py` that receives the ordered
`object_pairs_hook` pairs and a caller-provided document description. It builds
the object in encounter order and raises:

```text
<description> JSON contains duplicate object key: <key>
```

on the second occurrence. The helper uses normal dictionary membership and
does not stringify, normalize, or otherwise reinterpret keys or values.

The helper is used by:

- `TraceBackedMemoryStore.load_json()` with `memory store snapshot`;
- `parse_memory_context()` and `parse_memory_decision()` through
  `_json_object()` with their existing labels;
- the CLI JSON file loader through its existing `CLIInputError` boundary.

Using `json.loads(..., object_pairs_hook=...)` applies the check to nested
objects as well as the top-level envelope. Existing non-finite-number,
malformed-JSON, recursion, file-size, item-count, node-count, and depth errors
keep their current boundaries and messages.

## Security and Compatibility

Duplicate names are not a meaningful persisted state: JSON Schema validation
and Python mappings only observe the already-overwritten result. Rejecting the
source document before conversion prevents last-key-wins ambiguity in identity,
provenance, safety flags, scopes, contexts, and Gate decisions.

Canonical JSON written by the package never contains duplicate keys, so valid
snapshots and CLI documents are unaffected. This changes no public signature,
model, dependency, JSON Schema, snapshot version 2, active-lessons YAML,
packaged resource, PostgreSQL DDL, or PostgreSQL schema version 1.

## Tests

- Snapshot file tests reject duplicate top-level envelope fields and duplicate
  fields inside nested records without mutating any existing store.
- Policy tests reject contradictory duplicate context and decision fields while
  preserving mapping input and ordinary JSON behavior.
- CLI tests continue to return exit code 2 and the existing structured input
  error for duplicate keys after adopting the shared primitive.
- Documentation contract tests publish the strict rule and unchanged format
  versions across README, architecture, usage policy, product, and roadmap.

