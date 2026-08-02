# Product Delivery Program

**English** | [简体中文](product-program.zh-CN.md)

This file preserves historical delivery phases. Use the
[current capability status ledger](status/current-capability-matrix.md) for
the active product boundary; a delivered isolated increment is not
automatically an active user path.

## Phase 0: Project framing

- Define memory object model.
- Define project policy memory for manually maintained prompt/tool/eval rules.
- Define system gate policy.
- Define supported modes: debug, repair, regression, planning, eval, production.
- Define how repo, commit_sha, prompt_version, prompt_family, tool_schema_version, and eval_suite are captured.
- Keep repo, prompt_version, prompt_family, tool_schema_version, model, and eval_suite on trace records for provenance and PR reporting.

## Phase 1: Trace capture

- Record run metadata.
- Attach repo name, git commit SHA, branch, and dirty state.
- Store trace URI, repo, eval result, tool calls, errors, prompt version, and prompt family.
- Reject stored traces with empty identity fields, unsupported eval_result values, or malformed JSON-like trace collections.
- Keep raw trace out of runtime prompt.
- Capture git metadata through a small dependency-free helper.
- Wrap git metadata capture failures with command and repo-path context.
- Preserve eval_suite on traces and use it when matching historical PR/CI failures.
- Preserve repo on traces and use it when matching historical PR/CI failures.

## Phase 2: Failure case extraction

- Generate failure case draft from failed traces.
- Classify failure_type.
- Load failure taxonomy YAML and optionally validate classifier output against it.
- Link source_trace_id and commit_sha.
- Reject stored cases with empty identity fields, unsupported status, or missing verified evidence.
- Reject stored cases whose source_trace_id is missing or whose commit_sha does not match the source trace.
- Add manual review path.
- Record reviewed_by, reviewed_at, review_notes, and reviewed root_cause before verification.
- Use conservative trace heuristics for initial taxonomy classification, with tests for taxonomy precedence and each repository taxonomy category.

## Phase 3: Verification loop

- Allow only draft cases to transition into verified status.
- Validate public lifecycle factory inputs before records reach the store.
- Bind fix_commit_sha.
- Require regression pass before verified status.
- Mark outdated cases as obsolete.
- Expose obsolete transitions for failure cases and derived lessons.

## Phase 4: Lesson memory

- Generate lesson candidates from verified cases.
- Require source_case_id and scope.
- Require scope fields to be known context keys with non-empty string values.
- Require confidence to stay in the inclusive 0.0 to 1.0 range.
- Reject stored lessons with empty IDs, invalid memory type, or invalid status.
- Store active lessons in YAML or DB.
- Provide dependency-free JSON snapshot persistence and active-lessons YAML save/load alongside the PostgreSQL repository.
- Preserve numeric-looking scope strings through active-lessons YAML round trips.
- Reject stored lessons whose source case is missing, unverified, or lacks regression evidence.
- Store manually maintained project policies, validate policy IDs/text/status/scope/confidence, reject runtime memory ID collisions across failure cases, lessons, and project policies, and include policies in scoped retrieval.

## Phase 5: Memory retrieval and gating

- Retrieve by metadata filter first, requiring all declared scope fields to match.
- Support optional keyword filtering and bounded caller-provided semantic/vector scores after metadata filtering.
- Preserve short domain tokens in keyword filtering.
- Run deterministic System Gate.
- Block zero or out-of-range confidence memory before LLM applicability checks.
- Run LLM Gate for semantic applicability.
- Make `prepare_memory()` then `finalize_memory()` the primary runtime path: bind retrieval, System Gate, LLM narrowing, stale-state recheck, trace link, and atomic audit logging in the store.
- Honor injection modes when building runtime snippets, JSON-quote snippet text, and cap injected text.
- Quote and cap task text, context summaries, and candidate memory text inside the LLM gate prompt.
- Persist trace ID, serialized context, candidate status snapshots, and System Gate block reasons with decisions; require failed or errored non-null eval evidence for wrong-memory failures.
- Keep keyword search as a post-metadata retrieval aid, not an approval gate.
- Reject usage logs with empty identities, duplicate imported decision IDs, unsupported enum fields, duplicate, empty-string, or non-string memory ID lists, overlapping used/blocked IDs, or used/blocked memory IDs that were not retrieved as candidates.
- Keep Postgres schema checks aligned with model defaults, non-empty required identities/text, composite case/trace commit provenance, non-null confidence, required audit fields, and JSONB object/array element shapes.
- Publish JSON schemas for stored records and full memory-store snapshots.

## Phase 6: CI / PR integration

- On PR, show related verified, regression-backed historical failures from repo-matched traces.
- Suggest regression tests.
- Warn when prompt/tool schema changes touch known failure areas.
- Generate an in-memory PR report from stored traces and verified, regression-backed failure cases.
- Include trace/case/fix provenance in PR reports.
- Exclude historical PR failures whose trace repo is missing or does not match the current repo.

## Phase 7: Metrics

Track:

- memory candidate count
- allowed vs blocked memory
- pass rate with/without memory
- failures caused by wrong memory
- obsolete memory usage attempts
- lesson confidence over time

## Phase 8: PostgreSQL persistence (implemented)

- Publish the synchronous `PostgresMemoryRepository` behind the optional
  `postgres` dependency extra.
- Require PostgreSQL 12+ because the hardened schema uses
  `jsonb_path_exists`.
- Require a fresh `public` schema installed from `schemas/postgres.sql` at
  schema version 1.
- Synchronize complete store snapshots additively and atomically, with canonical
  comparison, immutable conflict rollback, and forward-only lifecycle updates.
- Load normalized database records through the same store validation contract.
- Support borrowed caller connections and owned connections from `connect()`.
- Keep in-place migrations, connection pooling, and async repository support as
  explicit non-goals.

## Phase 9: Git ancestry applicability (implemented)

- Discover metadata-scoped runtime anchors and PR-report anchors from the
  in-memory store.
- Capture immutable ancestry relations outside the store lock against the
  current context commit.
- Use lesson fix commits and failure-case source commits as runtime anchors;
  leave project policies unanchored for ancestry filtering only.
- Require complete evidence whenever ancestry is supplied, reject evidence
  bound to another current commit, and exclude false ancestry relations.
- Reuse captured evidence for PR reports before related-case reporting,
  regression suggestions, warnings, and provenance.
- Keep ancestry as an applicability filter only; System Gate and LLM Gate
  remain authoritative for safety and semantic relevance.
- Preserve opt-in backward compatibility and all snapshot, YAML, schema, and
  PostgreSQL persistence contracts.

## Phase 10: PR change-set endpoint matching (implemented)

- Accept immutable `PRChangeSet` values for exact old/new endpoint matching on
  prompt, tool, model, and eval trace provenance fields.
- Bind every new endpoint value to the post-change `MemoryContext`, match only
  complete old or complete new configurations, and report `old`, `new`, or
  `both` provenance.
- Reuse the same change set for PR anchor discovery and reporting so ancestry
  evidence is complete for endpoint-matched cases and fails closed when it is
  missing.
- Preserve legacy broad `changed_fields` behavior, reject unsupported exact
  `model_family` matching, and leave snapshot version 2, JSON Schemas,
  active-lessons YAML, and PostgreSQL schema version 1 unchanged.

## Phase 11: Benchmark example leakage classification (implemented)

- Define exact benchmark identity as `(eval_suite, input_hash)` and require
  callers to use a stable suite name, canonicalize each example
  deterministically, and compute a collision-resistant privacy-preserving hash.
  Each trace carries the hash of its own example, and the current
  `MemoryContext` must match the current trace. Source and current traces use
  the same hash only when they represent the same canonical example; different
  examples keep their own hashes.
- Enrich source-derived memory at runtime with ephemeral `source_eval_suite` and
  `source_input_hash`. Candidate `source_eval_suite` and `source_input_hash`
  fields are not serialized into prompts or snippets. The builders do not
  render structured `input_hash` fields; `eval_suite` remains ordinary prompt
  context and may also appear in memory scope.
- Block a complete exact pair in every mode with the automatic block reason
  `memory originates from current benchmark example`. Static `sensitive` and
  `eval_leaking` checks retain precedence and their existing reasons.
- Incomplete identities never trigger a guessed match. Preserve eval-suite-only
  legacy contexts, reject context hashes without suites and malformed partial
  source pairs, and avoid matching different examples or different suites.
- Enforce context/trace binding during finalization and record current identity,
  candidate/status evidence, and the automatic block reason in the usage audit.
- State explicitly that `input_hash` is identity evidence, not memory scope.
- Preserve snapshot version 2 and PostgreSQL schema version 1 with no new
  persisted memory fields; source provenance stays ephemeral and existing trace
  and usage storage carry the required evidence.

## Phase 12: Outcome-aware metrics (implemented)

- Define that `pass`, `fail`, and `error` are evaluated outcomes; `error` is an
  evaluated non-pass. `unknown` and `None` are unevaluated and stay outside
  pass-rate denominators.
- Expose `evaluated_with_memory_count`, `evaluated_without_memory_count`, and
  `unevaluated_decision_count`; for values returned by `store.metrics()`,
  together they equal `decision_count` and make both pass-rate sample sizes
  auditable.
- Keep the with/without split tied to audited `used_memory_ids`. These are
  decision counts, not per-memory causal attribution.
- Metrics remain derived and are not persisted; preserve snapshot version 2,
  JSON Schemas, active-lessons YAML, and PostgreSQL schema version 1.

## Phase 13: Per-memory outcome metrics (implemented)

- Export `MemoryOutcomeMetrics` and expose `memory_outcome_metrics()` as a
  stable memory-ID-sorted tuple for every stored failure case, lesson, and
  project policy, including zero-observation IDs.
- Track `candidate_count`, `used_count`, and `blocked_count`; blocked count
  covers both deterministic and LLM-narrowing blocks.
- For used memory only, track `evaluated_use_count`, `passed_use_count`,
  `failed_or_errored_use_count`, `unevaluated_use_count`, and
  `observed_pass_rate` using the Phase 12 outcome boundary.
- Treat results as observed associations, not causal effectiveness estimates.
  Multi-memory runs associate the outcome with every used ID, and the API does
  not derive per-memory wrong-memory attribution from the log-level flag.
- Metrics remain derived and are not persisted; preserve snapshot version 2,
  JSON Schemas, active-lessons YAML, and PostgreSQL schema version 1.

## Phase 14: Declared Trace provenance binding (implemented)

- Require `repo`, `commit_sha`, and `tenant` always match the linked Trace.
  Bind `branch`, `prompt_version`, `prompt_family`, `tool_schema_version`,
  `model`, and `eval_suite` only when the context declares them.
- Require a declared tool to match an exact plain-string Trace tool call;
  non-string tool names do not satisfy evidence. Omitted optional provenance
  remains broad and allows richer Trace records.
- `model_family`, `task_type`, and `failure_type` remain unbound because
  Trace has no equivalent stored provenance.
- Validate before pending request consumption or usage-log append, preserving
  retry and append atomicity on every mismatch.
- Imported version-2 and supplied legacy context evidence follows the same
  declared-only validation rule.
- Preserve snapshot version 2, JSON Schemas, active-lessons YAML, and
  PostgreSQL schema version 1.

## Phase 15: Deferred decision outcome sealing (implemented)

- Add `record_decision_outcome()` so callers can finalize a memory decision,
  execute with the returned snippet, and attach the measured outcome afterward
  by decision ID.
- Allow only `None` or `unknown` to advance to `pass`, `fail`, or `error` with
  one validated `memory_caused_failure` value. Make exact replay idempotent and
  reject all sealed-result or attribution rewrites atomically.
- Move global and per-memory derived metrics from unevaluated to evaluated as
  soon as the outcome is sealed.
- Let PostgreSQL synchronization update only the same outcome pair on an
  unevaluated usage row. Keep every other usage field immutable, reject stale
  downgrades and conflicting seals, and roll back the update on any later sync
  conflict.
- Preserve `MemoryUsageLog`, snapshot version 2, JSON Schemas, active-lessons
  YAML, `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 16: Deferred Trace completion (implemented)

- Add `complete_trace()` so callers can register a current Trace with
  `eval_result` set to `unknown` before memory finalization and attach measured
  execution evidence afterward.
- Allow one transition to `pass`, `fail`, or `error`, filling only
  `output_hash`, `tool_outputs`, `latency_ms`, `cost_usd`, `error`, and
  `trace_uri`. Preserve omitted and already equal values, and reject rewrites
  of populated completion evidence.
- Keep Trace identity, repo/commit/tenant provenance, prompt/tool/model and eval
  metadata, input hash, retrieved context, tool calls, and creation time
  immutable. Make exact completion replay idempotent and every failure atomic.
- Keep low-level Trace completion separate from `record_decision_outcome()` so
  advanced callers can still own either lifecycle and recover legacy partial
  states without silently mutating the other record.
- Let PostgreSQL synchronization perform the same row-locked forward Trace
  completion, reject stale or conflicting states, and roll back on any later
  synchronization conflict.
- Preserve `Trace`, snapshot version 2, JSON Schemas, active-lessons YAML,
  `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 17: Atomic memory-run completion (implemented)

- Add `complete_memory_run()` as the preferred post-execution API. Require the
  exact linked `trace_id` and `decision_id`, apply one measured result to both
  records, and return a frozen `MemoryRunCompletion` with defensive copies.
- Build and validate both candidates under one store lock before either
  assignment. Reject conflicting results, attribution, Trace evidence, or
  linkage atomically without leaving a half-completed audit.
- Support exact replay and partial recovery when `complete_trace()` or
  `record_decision_outcome()` already recorded the same result. Keep both
  low-level methods public for separately owned lifecycles and recovery.
- Reuse the existing PostgreSQL transaction so linked Trace and usage updates
  commit together; prove a usage conflict rolls the earlier Trace update back.
- Persist no `MemoryRunCompletion` wrapper or new field. Preserve snapshot
  version 2, JSON Schemas, active-lessons YAML, `schemas/postgres.sql`, and
  PostgreSQL schema version 1.

## Phase 18: Memory-run audit view (implemented)

- Export frozen `MemoryRunAudit` values from `memory_run_audits()`, with one
  record for every usage decision sorted by `decision_id` and linked to its
  exact `trace_id` and `run_id`.
- Classify both unevaluated as `pending`, Trace-only measurement as
  `trace_only`, decision-only measurement as `decision_only`, equal measured
  results as `complete`, and different measured results as `conflict`.
- Use one-sided states to identify partial recovery through
  `complete_memory_run()`. Keep conflicts observable for manual review and
  never auto-repair or silently choose one historical result.
- Keep traces without usage decisions outside the decision-oriented view and
  preserve separate audit records for multiple decisions linked to one Trace.
- Keep the view derived and not persisted. Reproduce it after snapshot and
  PostgreSQL loads while preserving snapshot version 2, JSON Schemas,
  active-lessons YAML, `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 19: Safe memory-run recovery (implemented)

- Add `recover_memory_run()` keyed only by `decision_id`. It does not accept
  `trace_id` or `eval_result`; derive both from the linked validated records and
  return `MemoryRunCompletion`.
- Recover `trace_only` from the Trace result and `decision_only` from the sealed
  decision result. Preserve `memory_caused_failure` from a sealed decision and
  make `complete` an exact replay through `complete_memory_run()`.
- Require explicit `memory_caused_failure` for failed or errored Trace-only
  recovery. Reject `pending` and `conflict`; recovery never guesses a missing
  result, causal attribution, or authoritative side.
- Delegate to the existing atomic completion operation under one reentrant
  store lock so immutable evidence, validation, defensive copies, and
  all-or-nothing assignment remain centralized.
- Reuse existing PostgreSQL forward updates and preserve snapshot version 2,
  JSON Schemas, active-lessons YAML, `schemas/postgres.sql`, and PostgreSQL
  schema version 1.

## Phase 20: Memory-run health metrics (implemented)

- Add and export frozen `MemoryRunMetrics` from `memory_run_metrics()`. Count
  one usage decision per audit row, including separate decisions linked to the
  same Trace.
- Expose `decision_count`, `pending_count`, `trace_only_count`,
  `decision_only_count`, `complete_count`, `conflict_count`, and
  `recoverable_count` without changing existing outcome-oriented metrics.
- Keep the five statuses mutually exclusive so their sum equals
  `decision_count`. `recoverable_count` is the sum of
  `trace_only_count` and `decision_only_count`; never classify pending or
  conflicting runs as automatically recoverable.
- Reuse the locked `memory_run_audits()` view as the classification source so
  per-run details and aggregate health cannot drift.
- Keep the summary derived and not persisted. Reproduce it after snapshot and
  PostgreSQL loads while preserving snapshot version 2, JSON Schemas,
  active-lessons YAML, `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 21: Atomic batch memory-run recovery (implemented)

- Add `recover_memory_runs()` for a non-empty tuple of unique decision IDs and
  an optional `memory_caused_failures` mapping. It preserves request order in
  the returned defensive `MemoryRunCompletion` tuple.
- Resolve only entry-state `trace_only`, `decision_only`, and `complete` items.
  Reject the whole batch on `pending`, `conflict`, missing failed-run
  attribution, or invalid input; every failure is all-or-nothing.
- Group decisions by shared Trace and require their independently derived
  measured results to agree. Never let an entry-state pending item borrow a
  result from another item staged in the same batch.
- Reuse the same recovery-state resolver as `recover_memory_run()`. Batch
  recovery does not accept `trace_id` or `eval_result` and omits completion
  evidence; use the single-item API when Trace evidence must be attached.
- Keep the batch wrapper derived and not persisted. Reuse existing PostgreSQL
  transactions and preserve snapshot version 2, JSON Schemas, active-lessons
  YAML, `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 22: Atomic batch memory-run completion (implemented)

- Export `MeasuredEvalResult` and frozen `MemoryRunResult` commands carrying a
  decision ID, measured outcome, attribution, and optional Trace evidence.
- Add `complete_memory_runs()` for a non-empty tuple with unique decision IDs.
  It derives `trace_id` from validated decision linkage and preserves request
  order in defensive `MemoryRunCompletion` values.
- Define evidence omission explicitly: `None` means omitted, while
  `tool_outputs` is a request tuple converted to a Trace list.
- Require outcomes on a shared Trace to agree. Normalize each request against
  original state and merge only disjoint or equal evidence fields; reject all
  result, already sealed per-decision attribution, evidence, or partial-state
  conflicts all-or-nothing.
- Reuse one non-mutating stager for `complete_memory_runs()` and
  `recover_memory_runs()` while preserving the recovery API's stricter derived
  result semantics and existing `complete_memory_run()` behavior.
- Keep `MemoryRunResult` ephemeral and not persisted. Reuse existing PostgreSQL
  transactions and preserve snapshot version 2, JSON Schemas, active-lessons
  YAML, `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 23: Memory-run remediation plan (implemented)

- Export frozen `MemoryRunRemediation` records and the
  `MemoryRunRemediationAction` alias.
- Add `memory_run_remediations()` to map every decision-sorted audit to
  `measure`, `recover`, `recover_with_attribution`, `investigate`, or `none`.
- Publish raw audit state plus `resolved_eval_result` and
  `resolved_memory_caused_failure` only when current records establish safe
  recovery values.
- Keep plans advisory: stale state is revalidated by `complete_memory_runs()`
  and `recover_memory_runs()`, shared Trace batch compatibility is rechecked,
  and conflicts are never auto-repaired.
- Extend `MemoryRunMetrics` with `auto_recoverable_count` and
  `attribution_required_count`; their sum equals `recoverable_count`.
- Keep remediation data derived and not persisted. Reconstruct it after
  snapshot and PostgreSQL loads while preserving snapshot version 2, JSON
  Schemas, active-lessons YAML, `schemas/postgres.sql`, and PostgreSQL schema
  version 1.

## Phase 24: Atomic ready memory-run recovery (implemented)

- Add no-argument `recover_ready_memory_runs()` to derive and apply every
  current remediation whose action is `recover` under one reentrant lock.
- Preserve `decision_id` order in defensive `MemoryRunCompletion` results and
  return an empty tuple when no decision is ready.
- Skip pending, `recover_with_attribution`, conflicting, and complete work;
  explicit failed-run attribution remains on `recover_memory_run()` and
  `recover_memory_runs()`.
- Reuse shared Trace agreement, all-or-nothing candidate staging, and rollback
  behavior from batch recovery.
- Serialize concurrent sweeps so a later caller re-plans and does not replay
  work completed by the first caller.
- Sweep selection is not persisted; synchronize only existing Trace and usage
  rows. Preserve snapshot version 2, JSON Schemas, active-lessons YAML,
  `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 25: Snapshot Operations CLI (implemented)

- Expose the dependency-free `tbm` console script and equivalent
  `python -m trace_backed_memory` module entry point.
- Add `snapshot validate`, `snapshot stats`, `audit`, `metrics`, and
  `remediation` reads over one local snapshot loaded through
  `TraceBackedMemoryStore.load_json()`.
- Add `recover`, `recover-batch`, and `recover-ready` by delegating to the
  existing single, batch, and ready recovery APIs without duplicating state
  classification or validation.
- Make every recovery a dry-run by default. Require explicit `--write` before
  reusing `save_json()` for same-path atomic replacement after full success.
- Emit one deterministic JSON result on success and one structured JSON error
  on failure. Define exit codes 0 through 4 for success, internal, input,
  recovery-state, and write outcomes.
- Reject duplicate batch decision IDs and malformed, duplicate, or unrequested
  attribution entries before store recovery. Preserve request order and the
  store's all-or-nothing mutation boundary.
- Build and install wheel/sdist artifacts in CI, smoke-test both entry points,
  and test Python 3.11, 3.12, and 3.13.
- Persist no CLI, audit, metrics, remediation, or completion wrapper state.
  Preserve snapshot version 2, JSON Schemas, active-lessons YAML,
  `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 26: Synchronous memory-run execution (implemented)

- Export one dependency-free `run_memory_execution()` entry point for the
  common prepare, decide, finalize, execute, and atomic complete sequence.
- Define `MemoryDecisionCallback` over the public `MemoryGateRequest` and
  `MemoryExecutionCallback` over the finalized `GatedMemoryResult`, leaving LLM
  and harness adapters in caller code.
- Add frozen `MemoryRunMeasurement` without a decision ID. Always complete with
  the Store-produced `decision_id`, forward only non-`None` optional evidence,
  and treat an empty tool-output tuple as explicit evidence.
- Add `MemoryRunExecutionError` with four post-preparation phases, request,
  finalized result, request ID, decision ID, and original exception cause.
  Never infer an outcome or failure attribution from an exception.
- Keep Store preparation errors unchanged. Wrap later Store failures with
  recoverable orchestration context while retaining their exact cause, linkage,
  replay, conflict, immutable evidence, and all-or-nothing completion behavior.
- Keep the low-level Store workflow available for advanced callers that pause,
  retry, or own separate lifecycle and recovery policy.
- Persist no measurement, callback type, execution error, or orchestration
  state. Preserve snapshot version 2, JSON Schemas, active-lessons YAML,
  `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 27: Packaged distribution resources (implemented)

- Ship byte-identical copies of all 18 canonical `schemas/`, `memory/`, and
  `examples/` files in wheel, source-distribution, and editable installs.
- Add immutable `PackagedResource` descriptions plus strict
  `packaged_resources()`, `read_packaged_resource()`, and
  `export_packaged_resource()` operations. Resolve only fixed allowlisted names
  through `importlib.resources`; never depend on a checkout or package path.
- Add structured `PackagedResourceError` lookup/read/export context, exact-byte
  SHA-256 metadata, explicit overwrite policy, temporary-file cleanup, and
  same-directory atomic publication.
- Make `load_failure_taxonomy()` use the packaged canonical taxonomy by
  default while preserving explicit caller-owned path loading.
- Add deterministic `tbm resource list/read/export` commands. Keep unknown
  names as exit 2, installed-data failures as exit 1, exports as exit 4, and a
  completed export successful when stdout closes afterward.
- Mark the distribution with `py.typed` and `Typing :: Typed`. Verify exact
  resource contents in both built artifacts, then install and smoke-test the
  wheel and source distribution independently in CI.
- Persist no resource catalog or export record. Preserve canonical top-level
  authoring files, snapshot version 2, JSON Schemas, active-lessons YAML,
  `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 28: Evidence ingestion integrity (implemented)

- Extend conservative failure extraction to explicit top-level `error`
  evidence from `tool_outputs` after existing Trace and tool-call evidence.
- Preserve classifier precedence, symptom preference, and root-cause ordering.
  Never classify from successful output names, arbitrary output fields, or
  nested result text.
- Reject duplicate failure-taxonomy descriptions instead of replacing the
  first description for an ID. Preserve existing duplicate-ID rejection.
- Reject duplicate active-lesson record fields and duplicate scope keys while
  parsing the complete document, before any Store mutation.
- Construct and validate every imported lesson against staged state before one
  all-or-nothing commit; duplicate IDs or later semantic failures import none.
- Keep both dependency-free constrained YAML shapes, scalar behavior,
  provenance validation, and valid-input results unchanged.
- Persist no parser or extraction state. Preserve snapshot version 2, JSON
  Schemas, active-lessons YAML, `schemas/postgres.sql`, packaged resource bytes,
  and PostgreSQL schema version 1.

## Phase 29: Measured memory-run completion CLI (implemented)

- Add `complete` for one fresh measured result, requiring the exact snapshot,
  Trace ID, decision ID, and `--eval-result` of `pass`, `fail`, or `error`.
- Expose optional failure attribution and Trace evidence. The command does not
  infer either, and preserves omitted evidence by forwarding only supplied
  options.
- Accept structured tool evidence only through `--tool-outputs-file` as strict
  UTF-8 JSON containing an array of objects; reject malformed input before any
  write.
- Delegate linkage, replay, attribution, evidence, and atomic assignment to
  `complete_memory_run()` rather than adding a CLI completion state machine.
- Keep completion a dry-run by default and require explicit `--write` for the
  existing same-path atomic snapshot replacement.
- Reuse the deterministic completion envelope, structured errors, exit codes,
  serialization-before-persistence rule, and post-commit stdout behavior.
- Persist no measured completion command or wrapper. Preserve snapshot version
  2, JSON Schemas, active-lessons YAML, `schemas/postgres.sql`, and PostgreSQL
  schema version 1.

## Phase 30: Lesson YAML persistence integrity (implemented)

- Route `save_json()` and `save_lessons_yaml()` through one sibling temporary
  file boundary with canonical LF output, flush, `os.fsync()`, close, and
  `os.replace()` publication.
- Preserve the existing destination and remove the temporary file when
  serialization, sync, or replacement fails.
- Emit active lesson text as canonical `lesson_text: |` blocks while accepting
  both `|` and the legacy `>` form on input; retain the constrained adapter's
  historical literal-line behavior rather than claiming YAML folding/chomping.
- Preserve blank lines, leading and trailing LF characters, and intra-line
  spaces across lesson YAML save/load round trips.
- Keep the constrained dependency-free parser and its duplicate-field,
  staged-validation, and all-or-nothing import guarantees.
- Add no record or column. Preserve snapshot version 2, JSON Schemas,
  active-lessons YAML semantics, `schemas/postgres.sql`, packaged resource
  bytes, and PostgreSQL schema version 1.

## Phase 31: Batch measured memory-run completion CLI (implemented)

- Add `complete-batch SNAPSHOT MEASUREMENTS_JSON [--write]` for an ordered set
  of fresh evaluator results.
- Read `MEASUREMENTS_JSON` as strict UTF-8 JSON containing a non-empty array of
  allowlisted `MemoryRunResult` objects; reject duplicate object keys, missing
  or unknown fields, wrong types, and non-finite numbers before completion.
- Omit caller-supplied Trace linkage, convert tool-output arrays to immutable
  tuples, and delegate exactly once to `complete_memory_runs()`.
- Preserve manifest order in the deterministic completion envelope. Keep the
  whole operation all-or-nothing for duplicate or unknown decisions,
  shared-Trace disagreement, invalid attribution, evidence conflicts, and
  later-item failure.
- Keep dry-run as the default and require explicit `--write` for synchronized
  same-path atomic snapshot replacement and the existing post-commit stdout
  rule.
- Persist no manifest, `MemoryRunResult`, or command record. Preserve snapshot
  version 2, JSON Schemas, active-lessons YAML, packaged resource bytes,
  `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 32: Bounded local document ingestion (implemented)

- Read caller-owned paths through a single file handle and reject oversized
  bytes before strict UTF-8 decoding.
- Bound snapshot JSON at 64 MiB, 100,000 records per collection, and 250,000
  total records before Store construction.
- Bound active-lessons YAML at 8 MiB and 10,000 lessons, and bound
  failure-taxonomy YAML at 1 MiB and 1,000 failure types before Store mutation.
- Bound CLI measurement and tool-output JSON at 8 MiB, 10,000 top-level items,
  100,000 JSON nodes, and depth 100 while preserving structured input errors
  and no-write failures.
- Expose keyword-only Python controls such as `max_bytes`; allow explicit
  `None` only for trusted offline migrations while keeping CLI defaults fixed.
- Persist no ingestion limit metadata. Preserve snapshot version 2, JSON
  Schemas, active-lessons YAML, packaged resource bytes,
  `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 33: PR report CLI (implemented)

- Add the read-only `pr-report SNAPSHOT CONTEXT_JSON CHANGE_SET_JSON
  --repo-path REPO_PATH` command for CI pipelines.
- Parse strict bounded context and `field_changes` documents into a validated
  `MemoryContext` and immutable value-aware `PRChangeSet`; reject unknown,
  missing, malformed, duplicate, unsupported, or context-mismatched values.
- Reuse the same change set with `pr_report_commit_anchors()` and
  `pr_memory_report()`, with `capture_commit_ancestry()` outside the Store lock
  against the explicit repository.
- Emit deterministic `commit_ancestry` and `report` output. Keep document
  failures at exit 2 and Git capture or report-state failures at exit 3.
- Disable lazy Git fetching and place an option terminator before revision
  arguments so revision text cannot become a command option.
- Add no `--write`, legacy changed-fields mode, supplied ancestry input, record,
  or Schema. Preserve snapshot version 2, JSON Schemas, active-lessons YAML,
  packaged resource bytes, `schemas/postgres.sql`, and PostgreSQL schema
  version 1.

## Phase 34: Active lessons portability CLI (implemented)

- Add `lessons export SNAPSHOT DESTINATION [--overwrite]` and `lessons import
  SNAPSHOT SOURCE_YAML [--write]` to both installed entry points.
- Reuse `save_lessons_yaml()` for active-only, Store-ordered canonical output.
  Refuse existing destinations by default through atomic no-replace
  publication, and reject any destination alias of the source snapshot.
- Reuse `load_lessons_yaml()` exactly once with fixed 8 MiB and 10,000-record
  limits. Preserve constrained parsing, duplicate rejection, shared-ID and
  provenance validation, source order, merge semantics, and all-or-nothing
  Store mutation.
- Keep import a complete validation dry-run by default. Require explicit
  `--write` before same-path atomic snapshot replacement; export alone writes
  its explicit destination and requires `--overwrite` for replacement.
- Serialize deterministic result envelopes before publication, classify lesson
  input failures as exit 2 and publication failures as exit 4, and treat a
  closed stdout after a committed export or import as success.
- Extend `save_lessons_yaml()` with a backward-compatible keyword-only
  `overwrite` argument whose default preserves Python replacement behavior.
- Persist no command, import manifest, or export metadata. Preserve snapshot
  version 2, JSON Schemas, active-lessons YAML shape, all 18 packaged resource
  bytes, `schemas/postgres.sql`, public exports, and PostgreSQL schema version
  1.

## Phase 35: Memory obsolescence CLI (implemented)

- Add `obsolete SNAPSHOT {failure-case,lesson,project-policy} MEMORY_ID
  [--write]` to both installed entry points with an explicit, non-inferred
  memory kind.
- Delegate exactly once to `obsolete_failure_case()`, `obsolete_lesson()`, or
  `obsolete_project_policy()`. Preserve forward-only status rules, same-state
  idempotence, and the Store's validation boundary.
- For failure cases, preview the sorted active→obsolete dependent lesson IDs
  produced by the Store's atomic cascade. Do not include unrelated or already
  obsolete lessons, and do not duplicate the cascade state machine in the CLI.
- Emit only kind, ID, previous/current status, change flag, cascade IDs/count,
  and `written`; never echo memory text, scope, Trace data, or tool evidence.
- Keep every operation a dry-run until explicit `--write` reuses same-path
  atomic snapshot replacement. Serialize before persistence and treat stdout
  closure after a committed write as success.
- Do not add reactivation, actor/reason fields, PostgreSQL access, or a CLI
  batch loop within this single-record phase. Phase 36 below closes the
  multi-record gap with one Store-level all-or-nothing API.
- Persist no command or cascade manifest. Preserve snapshot version 2, JSON
  Schemas, active-lessons YAML, all 18 packaged resource bytes,
  `schemas/postgres.sql`, public exports, and PostgreSQL schema version 1.

## Phase 36: Atomic batch memory obsolescence (implemented)

- Add public `MemoryKind` and frozen `MemoryObsolescenceRequest` records plus
  `obsolete_memories()` for an exact non-empty tuple with unique memory IDs.
- Resolve explicit `failure_case`, `lesson`, and `project_policy` targets from
  one entry state. Stage every requested record and the full active
  failure-case cascade, validate all candidates, then publish all-or-nothing.
- Preserve request order in returned deep copies. Permit an explicitly
  requested lesson to overlap a requested case's cascade without order
  dependence or double counting.
- Add `obsolete-batch SNAPSHOT REQUESTS_JSON [--write]` with strict UTF-8 JSON,
  a non-empty exact-object array, and the fixed 8 MiB, 10,000-item, node, and
  depth limits. Call the Store batch method exactly once.
- Emit only manifest-ordered status results, sorted cascade IDs,
  `changed_count`, union-based `affected_count`, and `written`. Never expose
  memory text, scope, Trace data, tool evidence, actor, or reason fields.
- Keep dry-run as the default and use same-snapshot atomic publication only
  after the whole batch and output serialization succeed. Preserve forward-only
  idempotence and committed stdout-failure retry safety.
- Persist no request or batch record. Preserve snapshot version 2, every JSON
  Schema, active-lessons YAML, all 18 packaged resource bytes,
  `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 37: Required PostgreSQL and Windows CI coverage (implemented)

- Preserve the PostgreSQL fixture's optional local behavior, but route missing
  server executables and an illegal `initdb` user through one test-runtime
  boundary.
- Add CI-only `TBM_REQUIRE_POSTGRES=1`; under that exact value, environmental
  PostgreSQL skips become test failures with the original diagnostic.
- Add a dedicated `ubuntu-latest` PostgreSQL job that installs and preflights
  `initdb`, `pg_ctl`, `psql`, and `psycopg`, then runs the integration and
  repository modules together against the private session cluster.
- Keep the POSIX cluster's Unix socket in its pytest-owned data directory rather
  than a distribution runtime path; continue using explicit TCP loopback and
  omit that option on Windows.
- Add a `windows-latest` Python 3.13 job that runs the complete pytest suite,
  while retaining the Ubuntu Python 3.11-3.13 matrix and package job.
- Keep the switch entirely under `tests/` and CI. Preserve application runtime,
  package dependencies, snapshot version 2, every JSON Schema, active-lessons
  YAML, all 18 packaged resource bytes, `schemas/postgres.sql`, and PostgreSQL
  schema version 1.

## Phase 38: Deferred decision outcome CLI (implemented)

- Add `outcome SNAPSHOT DECISION_ID --eval-result {pass,fail,error}
  [--memory-caused-failure true|false] [--write]` to both installed entry
  points.
- Capture the prior outcome pair and delegate exactly once to
  `record_decision_outcome()`; never complete or modify the linked Trace.
- Keep the command a full validation dry-run by default. Serialize the complete
  result before explicit same-path atomic publication, and retain committed
  stdout-failure retry safety.
- Emit only the decision ID, previous/current result and attribution,
  `changed`, and `written`. Never expose context, reason, risk, memory ID lists,
  Trace data, tool evidence, or the complete usage log.
- Preserve one-way sealing, exact-pair idempotence, attribution invariants, and
  Store-owned state errors. Add independent wheel console-script and sdist
  module-entry smoke.
- Persist no command or result wrapper. Preserve snapshot version 2, every JSON
  Schema, active-lessons YAML, all 18 packaged resource bytes,
  `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 39: PostgreSQL consistent snapshots and lifecycle row locks (implemented)

- Acquire ordered `SHARE` locks on traces, failure cases, lessons, project
  policies, and usage decisions before `load()` reads its first collection.
  Preserve concurrent readers while making external writers wait until the
  coherent load transaction ends.
- Preserve borrowed connections and caller-owned transactions: repository
  operations still use nested savepoints and never change the caller's
  connection-level isolation configuration. Document that successful locks
  remain held until the caller's outer commit or rollback and that PostgreSQL
  12 requires a schema owner or table-level write privileges for `SHARE` locks.
- Select every existing sync target `FOR UPDATE`, adding the missing
  failure-case, lesson, and project-policy row locks before canonical conflict
  validation.
- Require exactly one affected row from post-select lifecycle updates, and keep
  schema, conflict, driver, rollback, and sanitized-error behavior unchanged.
- Add real-cluster regressions for five-table load locks, direct external writer
  exclusion, and protected-field changes committed while each lifecycle sync is
  waiting on a row lock.
- Preserve the public API, snapshot version 2, every JSON Schema,
  active-lessons YAML, all 18 packaged resource bytes,
  `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 40: PostgreSQL bounded load materialization (implemented)

- After schema validation and the ordered five-table `SHARE` locks, run one
  scalar five-table `count(*)` count preflight before the first collection
  selector or decoder.
- Enforce the existing snapshot defaults of 100,000 records per collection and
  250,000 records in total, preserving the exact Store limit error messages.
- Require exactly one mapping result with non-negative integer counts for all
  five snapshot collections, and retain sanitized PostgreSQL error wrapping.
- Keep the table locks through count validation and bounded reads so external
  writers cannot invalidate the accepted counts before materialization.
- Reuse centralized private count validators and repeat normal Store validation
  after loading as defense in depth. Leave oversized individual JSONB or text
  values as a separate hardening concern.
- Preserve the public API, snapshot version 2, every JSON Schema,
  active-lessons YAML, all 18 packaged resource bytes,
  `schemas/postgres.sql`, and PostgreSQL schema version 1.

## Phase 41: Runtime collection cardinality limits (implemented)

- Limit each caller-supplied LLM decision `allowed_memory_ids` and
  `blocked_memory_ids` list to the existing 50-candidate gate budget before
  per-ID validation, duplicate detection, set construction, or copying.
- Apply the decision limit to both JSON/mapping parsing and direct
  `apply_llm_gate_decision()` calls, without discarding internally derived
  System Gate block audit records.
- Publish `maxItems: 50` in the canonical and packaged memory-decision JSON
  Schema copies.
- Export `COMMIT_ANCESTRY_MAX_ANCHORS` at 1,000 submitted values and replace
  eager iterable materialization with bounded, per-item validation.
- Count duplicate anchors before deduplication, consume at most 1,001 values,
  and start no Git command when an ancestry call overflows.
- Preserve snapshot version 2, active-lessons YAML, all 18 packaged resource
  paths, PostgreSQL DDL/schema version 1, models, and dependencies. Only the
  memory-decision Schema resource bytes intentionally change.

## Phase 42: PostgreSQL concurrent insert revalidation (implemented)

- Attempt every absent-row PostgreSQL INSERT inside a nested savepoint, keeping
  the existing plain INSERT statements and database triggers intact.
- Recover SQLSTATE `23505` and the runtime memory registry trigger's exact
  `P0001` signal, then rerun the same primary-key selector `FOR UPDATE` after
  the failed statement and trigger effects have rolled back.
- Classify a concurrently committed same-primary-key row with the existing
  canonical rules: exact replay is `unchanged`, a supported forward transition
  is `updated`, and a protected difference raises `PostgresConflictError` and
  rolls back the whole synchronization.
- Re-raise a recognized collision when the target row remains absent, so a
  cross-kind runtime memory ID collision remains `PostgresPersistenceError`;
  let every other driver error bypass revalidation.
- Cover all five persisted record kinds with real lock-wait races, including
  runtime memory registration triggers, full repository rollback, external row
  preservation, and post-error connection reuse.
- Preserve the public API, snapshot version 2, every JSON Schema,
  active-lessons YAML, all 18 packaged resource paths and bytes, PostgreSQL DDL
  and schema version 1, models, and dependencies.

## Phase 43: Strict JSON object key uniqueness (implemented)

- Add one shared ordered-pairs parser that rejects a duplicate object key on
  its second occurrence with a document-specific error.
- Use it for `TraceBackedMemoryStore.load_json()`, `parse_memory_context()`, and
  `parse_memory_decision()` so top-level and nested objects cannot apply
  last-key-wins before semantic validation.
- Reuse the same primitive behind CLI JSON file parsing while preserving its
  structured `CLIInputError`, exit code 2, count, node, depth, and byte limits.
- Keep direct Mapping inputs and canonical package-written JSON compatible;
  JSON object keys and values are not normalized or reinterpreted.
- Cover duplicate snapshot envelopes, nested records, runtime contexts, LLM
  decisions, and existing CLI manifests with deterministic regression tests.
- Preserve the public API, dependencies, snapshot version 2, every JSON Schema,
  active-lessons YAML, all 18 packaged resources, PostgreSQL DDL, and
  PostgreSQL schema version 1.

## Phase 44: Bounded recover-batch arguments (implemented)

- Cap submitted `recover-batch` arguments at 10,000 decision IDs and 10,000
  attribution options, counting values before duplicate detection.
- Enforce both ceilings immediately after argparse and before snapshot loading,
  tuple/set/dictionary construction, Store recovery, or publication.
- Report overflow through the existing structured input error and exit code 2;
  `--write` cannot read or replace the snapshot after a cardinality failure.
- Preserve accepted-batch request ordering, unique decision IDs, exact
  `DECISION_ID=true|false` parsing, dry-run defaults, and Store-owned
  all-or-nothing recovery.
- Cover the exact configured boundary and one-item overflow for both argument
  lists, including proof that overflow never reaches snapshot loading.
- Preserve the public API, dependencies, snapshot version 2, every JSON Schema,
  active-lessons YAML, all 18 packaged resources, PostgreSQL DDL, and
  PostgreSQL schema version 1.

## Phase 45: Non-negative trace latency (implemented)

- Define `latency_ms` as `None` or a non-negative integer across Trace
  recording, snapshot reconstruction, execution, and single/batch completion;
  retain zero as the inclusive boundary.
- Keep the Store authoritative for the range rule so scalar and manifest CLI
  negatives remain structured `state` errors with exit code 3 and cannot reach
  `--write`; malformed input remains exit code 2.
- Add `minimum: 0` to both Trace Schema copies and the named
  `traces_latency_ms_non_negative` CHECK to both fresh-install PostgreSQL DDL
  copies; verify null, zero, and negative behavior on a real database.
- Preserve existing huge-integer errors by applying the sign check after the
  JSON serialization bound, and leave the finite `cost_usd` contract unchanged.
- Document that existing schema-version-1 databases are not migrated in place;
  direct-SQL operators own an equivalent constraint migration.
- Preserve public APIs, dependencies, snapshot version 2, active-lessons YAML,
  all 18 packaged resource names/count, and PostgreSQL schema version 1. The
  Trace Schema and PostgreSQL DDL bytes intentionally change.

## Phase 46: Public project-policy obsolescence export (implemented)

- Export the existing `obsolete_project_policy()` lifecycle helper from the
  package root and include it in `__all__`, matching the documented
  failure-case and lesson helpers.
- Keep the root export identical to the lifecycle function rather than adding a
  wrapper or second transition implementation.
- Extend the executable README API example and tests across import, behavior,
  input immutability, source-package identity, and isolated-wheel installation.
- Preserve Store/CLI lifecycle orchestration and every existing function
  signature. This additive export does not change models, dependencies,
  snapshot version 2, JSON Schemas, active-lessons YAML, any of the 18 packaged
  resource paths or bytes, PostgreSQL DDL, or PostgreSQL schema version 1.

## Phase 47: PostgreSQL-compatible trace latency range (implemented)

- Define `latency_ms` as `None` or an integer in the inclusive range 0 through
  2,147,483,647, matching the existing PostgreSQL signed `INTEGER` column.
- Apply the upper bound in the shared Store validator after exact-type, JSON
  serialization, and non-negative checks, preserving huge-integer and negative
  error priority across record, snapshot, execution, and completion paths.
- Keep scalar and manifest CLI completion delegated to the Store so overflow is
  a structured `state` error with exit code 3 and cannot partially write.
- Add `maximum: 2147483647` to both Trace Schema copies and verify the inclusive
  maximum plus one-value overflow against a real PostgreSQL cluster.
- Preserve public signatures, dependencies, snapshot version 2,
  active-lessons YAML, all 18 packaged resource paths/count, PostgreSQL DDL,
  and PostgreSQL schema version 1. Only Trace Schema bytes change; existing
  schema-version-1 databases already enforce the upper range and need no
  migration.

## Phase 48: PostgreSQL bounded load payloads (implemented)

- After the existing five-table count preflight succeeds, run one scalar
  payload query while the ordered `SHARE` locks remain held and before the
  first collection selector.
- Convert each persisted row to a PostgreSQL JSON object and measure its UTF-8
  representation, returning only `max_record_bytes` and `total_bytes` to the
  client.
- Accept both exact 64 MiB boundaries and reject a largest row or five-table
  aggregate one byte above the limit before psycopg fetches collection rows.
- Require exactly one mapping result with non-negative exact integers, reject
  malformed or impossible maximum/total pairs, and preserve sanitized
  `PostgresPersistenceError` wrapping plus connection reuse.
- Keep count validation first so a count-overflow database is rejected without
  detoasting payloads; retain the stable table locks through both preflights
  and all bounded collection reads.
- Leave `sync()` behavior unchanged because its Store is already caller-owned
  client memory. Preserve public APIs, dependencies, snapshot version 2, every
  JSON Schema, active-lessons YAML, all 18 packaged resources, PostgreSQL DDL,
  and PostgreSQL schema version 1.

## Phase 49: Portable nonblank persisted strings (implemented)

- Require at least one non-whitespace character in persisted identity,
  linkage, required failure text, lesson/policy scope, Memory Context values,
  and usage-audit mapping keys and values before Store mutation or sync.
- Preserve accepted strings byte-for-byte. Keep optional Trace metadata,
  unrelated Failure Case narrative fields, and candidate/used/blocked
  memory-ID arrays on their existing contracts.
- Add `pattern: "\\S"` to the six affected canonical/package Schema pairs;
  preserve snapshot version 2, active-lessons YAML, and all 18 packaged
  resource paths/count.
- Keep snapshot CLI read failures as structured input errors with exit code 2
  and prove rejected reads do not rewrite their source file.
- Lock the real PostgreSQL boundary: schema-version-1 `btrim` checks reject
  ordinary spaces but are narrower than Python/JSON Schema whitespace rules.
  Repository writes use the stronger Store prevalidation; direct-SQL operators
  own cleanup of out-of-contract whitespace-only rows.
- Preserve public signatures, dependencies, PostgreSQL DDL, and PostgreSQL
  schema version 1.

## Phase 50: Conservative failure extraction accuracy (implemented)

- Require truthy top-level `error` evidence on a tool call before its name can
  label a tool-failure symptom; otherwise fall through to an errored output,
  `Trace.error`, or the trace-ID fallback.
- Keep explicit `invalid argument` evidence, but replace the bare `required`
  shortcut with `required argument`, `required parameter`, `required field`,
  and `required property` tool-error markers.
- Preserve missing-context precedence, every later taxonomy fallback, stored
  evidence order, symptom wording, and root-cause priority.
- Change no public signature, dependency, model, snapshot field, JSON Schema,
  active-lessons YAML, packaged resource, PostgreSQL DDL, or schema version 1.

## Phase 51: Linear snapshot usage-log validation (implemented)

- Replace the quadratic prior-log duplicate scan with a load-local
  `decision_id` set while retaining complete per-log validation before the
  duplicate error.
- Reuse one known-memory set, one legacy `run_id` index, and lazy per-trace
  tool-name sets during a load instead of rebuilding or rescanning them for
  every usage log.
- Use per-log candidate and blocked ID sets for candidate/used/blocked
  relationship checks while retaining stored-order diagnostics and exact
  errors.
- Keep average O(n) validation in snapshot records and nested ID/tool evidence
  without wall-clock test thresholds.
- Change no public signature, dependency, model, snapshot field, JSON Schema,
  active-lessons YAML, packaged resource, PostgreSQL DDL, or schema version 1.

## Phase 52: Indexed usage-log operations (implemented)

- Maintain a private derived `decision_id`-to-list-position index and next
  numeric suffix across snapshot import, finalization, and direct logging.
- Preserve max numeric suffix allocation for sparse imported IDs, index
  nonnumeric IDs without advancing the counter, and consume no ID on failed
  writes.
- Resolve allocation, duplicate checks, and single-ID operations in average
  O(1), and requested batch lookups in average O(k), under the existing Store
  `RLock`.
- Keep replacement IDs and list positions stable, retain canonical snapshot
  sorting, and do not serialize the derived index.
- Change no public signature, dependency, model, snapshot field, JSON Schema,
  active-lessons YAML, packaged resource, PostgreSQL DDL, or schema version 1.

## Phase 53: Indexed run-to-Trace lookup (implemented)

- Maintain a private derived `run_id`-to-ordered-`trace_id` index through the
  sole `record_trace()` insertion boundary.
- Commit the copied Trace and index entry under the existing Store `RLock`,
  and roll the Trace insertion back if index publication fails.
- Resolve missing, unique, and ambiguous run IDs in average O(1) without
  scanning Trace history; preserve duplicate run IDs as valid but ambiguous.
- Store Trace IDs so completion replacements remain current, and rebuild the
  nonserialized index through validated snapshot reconstruction.
- Keep legacy migration and canonical snapshot ordering unchanged. Change no
  public signature, dependency, model, snapshot field, JSON Schema,
  active-lessons YAML, packaged resource, PostgreSQL DDL, snapshot version 2,
  or PostgreSQL schema version 1.

## Phase 54: Referenced live memory-ID validation (implemented)

- Validate live usage-log memory existence by checking only its distinct
  referenced IDs against the three authoritative Store dictionaries.
- Keep average O(r) live validation, where `r` is the number of referenced
  IDs, without copying the complete memory catalog.
- Continue passing one reused `known_memory_ids` set through snapshot
  reconstruction so its existing average O(n) behavior and object reuse stay
  intact.
- Add no new derived index; preserve relationship validation, deduplication,
  sorted unknown-ID errors, and exact validation order.
- Change no public signature, dependency, model, snapshot field, JSON Schema,
  active-lessons YAML, packaged resource, PostgreSQL DDL, snapshot version 2,
  or PostgreSQL schema version 1.

## Phase 55: Single-pass Store metrics (implemented)

- Aggregate every usage-log-derived `metrics()` field in one usage-log pass
  with O(1) accumulator space.
- Replace evaluated cohort result lists with pass and total counters while
  preserving empty-cohort `None` and nonempty zero-pass `0.0`.
- Keep cohort membership based on persisted `used_memory_ids`; retain the
  evaluated/unevaluated result boundary, obsolete candidate-status count, and
  wrong-memory attribution count.
- Leave lesson confidence, `memory_outcome_metrics()`, memory-run ordering, and
  CLI public-API call boundaries unchanged.
- Change no public signature, dependency, model, snapshot field, JSON Schema,
  active-lessons YAML, packaged resource, PostgreSQL DDL, snapshot version 2,
  or PostgreSQL schema version 1.

## Phase 56: Single-pass memory-run metrics (implemented)

- Aggregate `memory_run_metrics()` in one usage-log pass without sorting and
  with O(1) accumulator space.
- Reuse one private log-to-audit constructor so Trace lookup, status
  classification, and remediation action semantics retain one source of truth.
- Keep `memory_run_audits()` and remediations in decision-ID order while the
  unordered metrics path avoids presentation sorting and tuple materialization.
- Preserve all status and recovery conservation identities, point-in-time lock
  scope, and derived-only persistence behavior.
- Change no public signature, dependency, model, snapshot field, JSON Schema,
  active-lessons YAML, packaged resource, PostgreSQL DDL, snapshot version 2,
  or PostgreSQL schema version 1.

## Phase 57: Serialized snapshot CLI writes (implemented)

- Acquire a canonical sibling `.tbm.lock` exclusive advisory lock before
  snapshot load for every explicit snapshot `--write` command.
- Hold the lock across the complete read-modify-write transaction through
  success serialization and atomic publication, then release it before stdout.
- Use POSIX `flock` or a Windows one-byte locking region so descriptor close or
  process exit releases ownership without stale sentinel recovery.
- Initialize the non-sensitive sidecar with one placeholder byte and keep it
  persistent to preserve one stable coordination inode.
- Retry contention for at most 30 seconds, then return a write error with exit
  code 4 before snapshot load; leave dry runs, read-only commands, lessons
  export, and resource export lock-free.
- Preserve command payloads, error classes, BrokenPipe behavior, snapshot
  version 2, PostgreSQL schema version 1, and all packaged resources.

## Phase 58: Active-only lesson imports (implemented)

- Enforce the portable active-only artifact domain inside
  `load_lessons_yaml()` after general Lesson and provenance validation.
- Reject `status: obsolete` before staged insertion; preserve source-order
  diagnostics and all-or-nothing mutation for mixed documents.
- Map the CLI rejection to an input error with exit code 2 and leave an
  explicit `--write` snapshot byte-for-byte unchanged.
- Keep `add_lesson()`, full snapshots, PostgreSQL loading, obsolescence,
  System Gate auditing, and metrics able to preserve obsolete lifecycle
  history.
- Preserve every public signature, dependency, YAML field, snapshot version 2,
  PostgreSQL schema version 1, and all packaged resources.

## Phase 59: Bounded PR change sets (implemented)

- Derive the exact `PRChangeSet` maximum from its six supported unique fields
  and accept at most 6 entries.
- Reject a seventh item before entry shape, endpoint, or historical PR case
  scanning in both Store report interfaces.
- Validate accepted field names in one pass with bounded seen, unsupported, and
  duplicate sets while retaining unsupported-before-duplicate diagnostics.
- Return CLI oversized input as an input error with exit code 2 and stop
  without Git ancestry capture.
- Preserve the exact six-field boundary, canonical sorting, legacy
  `changed_fields`, snapshot version 2, PostgreSQL schema version 1, and all
  packaged resources.

## Phase 60: Linear legacy PR warnings (implemented)

- Validate permissive legacy `changed_fields` in one pass before ancestry or
  historical case scanning.
- Retain the first occurrence of at most 7 supported warning names while
  continuing to accept duplicate, unknown, and empty-list inputs.
- Prevent caller input length from multiplying case-level warning construction
  and make stable suggestion/warning deduplication set-backed.
- Reduce expected legacy warning work from `O(C * W)` plus quadratic stable
  deduplication to `O(W + C)` without changing text or order.
- Preserve exact `PRChangeSet`, matching, ancestry, provenance, snapshot
  version 2, PostgreSQL schema version 1, and all packaged resources.

## Phase 61: Bounded Git capture (implemented)

- Run default metadata and ancestry Git commands with `stdin=DEVNULL`, binary
  pipes, explicit UTF-8 replacement decoding, and a 30 seconds timeout.
- Retain at most 64 KiB for each ordinary stdout/stderr stream and kill/reap
  the process on timeout or output overflow.
- Retain only the first byte of `git status --porcelain` for dirty detection
  while draining and discarding the remaining output.
- Preserve injected runner signatures, exact commands and order,
  `GIT_NO_LAZY_FETCH=1`, ancestry 0/1 meaning, and CLI state-error mapping.
- Change no public model, snapshot version 2, PostgreSQL schema version 1, or
  packaged resource.

## Phase 62: Durable atomic publish (implemented)

- Keep canonical LF serialization, sibling temporary files, temporary-file
  flush/`fsync()`, and `os.replace()`/`os.link()` publication unchanged.
- After a successful atomic publish and normal temporary-name cleanup, open and
  `fsync()` the parent directory on POSIX; retain portable atomic publication on
  non-POSIX platforms.
- Close every opened directory descriptor, including when its `fsync()` fails.
- Preserve old destinations for every pre-publication failure, while propagating
  a post-publication directory-sync error as an indeterminate durability result
  because the new target may already be visible.
- Preserve public signatures, serialized bytes, snapshot version 2, PostgreSQL
  schema version 1, and all packaged resources.

## Phase 63: Bounded semantic top-k (implemented)

- Skip semantic catalog construction entirely for metadata-only, keyword, and
  semantic-only-option error paths.
- Validate semantic IDs through a non-copying membership view over failure-case,
  lesson, and policy catalogs without iterating the complete ID universe.
- Stream metadata- and ancestry-eligible candidates through bounded semantic
  top-k heap selection instead of materializing a full sort.
- Preserve complete mapping validation, inclusive minimum scores,
  score-descending and memory-ID-ascending output, and the 1-through-50 limit.
- Reduce ranking to `O(K log k)` time and `O(k)` storage while preserving public
  signatures, snapshot version 2, PostgreSQL schema version 1, and packaged
  resources.

## Phase 64: Public snapshot write lock (implemented)

- Extract the CLI's cross-platform lock backend into a dependency-free module
  and export `snapshot_write_lock()` from the package root.
- Let Python callers coordinate the complete load, mutate, and `save_json()`
  read-modify-write transaction through the same canonical `.tbm.lock`.
- Accept an explicit finite non-negative `timeout_seconds`, validate it before
  filesystem access, and preserve immediate zero-timeout acquisition semantics.
- Document the advisory, non-reentrant contract and keep persistent placeholder,
  alias normalization, exception release, and process-exit ownership semantics.
- Keep the CLI private wrapper, 30-second default, write error/exit code 4,
  dry-run/read-only behavior, snapshot version 2, PostgreSQL schema version 1,
  and packaged resources unchanged.

## Phase 65: Bounded runtime Trace JSON (implemented)

- Share one fixed budget across `retrieved_context`, `tool_calls`, and
  `tool_outputs` for every Trace candidate validation.
- Accept at most 100,000 aggregate JSON nodes and 8 MiB of aggregate UTF-8
  object-key/string text while preserving the existing depth-100 boundary.
- Reject wide containers before traversal-stack or `dict.items()` expansion,
  and reject lone surrogates before defensive copying or persistence.
- Apply the same contract to direct record, completion, snapshot import, and
  PostgreSQL load paths; keep failures atomic and boundary values valid.
- Preserve public signatures, accepted serialized bytes, snapshot version 2,
  PostgreSQL schema version 1, JSON Schemas, and all packaged resources.

## Phase 66: PostgreSQL loaded-row payloads (implemented)

- Keep the post-lock, post-count, prefetch scalar payload query and both exact
  64 MiB boundaries unchanged.
- Measure compact PostgreSQL JSON for the actual loaded-row projections used
  by collection loaders rather than complete physical rows.
- Exclude only internal `updated_at` from failure cases, lessons, and project
  policies; retain every physical Trace and usage-decision column.
- Pin JSONB subtraction, functions, casts, and tables to `pg_catalog`/`public`,
  and return only maximum-record and aggregate byte counts to the client.
- Preserve sanitized failures, connection reuse, public APIs, snapshot version
  2, PostgreSQL schema version 1, DDL, JSON Schemas, and packaged resources.

## Phase 67: Snapshot lock sidecar safety (implemented)

- Require the canonical `.tbm.lock` sidecar to be one single-link regular file
  before placeholder initialization or advisory lock acquisition.
- Create absent sidecars exclusively; validate existing sidecars with no-follow
  metadata plus pre-open, descriptor, and post-open file identity checks, then
  revalidate descriptor/path identity after OS lock acquisition.
- Reject symbolic links, Windows reparse points, hard links, and special files
  without modifying an alias target or loading the snapshot.
- Preserve persistent sidecars, placeholder bytes, canonical snapshot aliases,
  30-second contention, structured write error/exit code 4, and descriptor
  release semantics.
- Change no public signature, successful CLI payload, snapshot version 2,
  PostgreSQL schema version 1, DDL, JSON Schema, or packaged resource.

## Phase 68: Git metadata output validation (implemented)

- Require every injected trace-metadata command to return a string and wrap
  non-string output in command-specific `TraceMetadataCaptureError`.
- Reject a blank commit SHA or blank repository root before starting the next
  Git command, without echoing malformed output.
- Enforce the existing 512-character metadata limit for commit SHA, branch,
  and final repository basename at the capture boundary.
- Preserve blank branch as detached HEAD, blank status as clean, command order,
  injected runner signatures, and the bounded default runner.
- Change no public signature, model, serialized byte, snapshot version 2,
  PostgreSQL schema version 1, DDL, JSON Schema, or packaged resource.

## Phase 69: Explicit failure text classification (implemented)

- Classify failures only from `Trace.error` and explicit top-level `error`
  values on tool calls and tool outputs.
- Never select a failure taxonomy entry from a tool name, regardless of whether
  that record also carries an error.
- Preserve errored tool names as deterministic symptom labels and retain trace,
  call, then output root-cause priority.
- Preserve taxonomy IDs, keyword precedence, evaluator fallbacks, public APIs,
  snapshot version 2, PostgreSQL schema version 1, JSON Schemas, and all 18
  packaged resources.

## Phase 70: Recover attribution final delimiter (implemented)

- Parse each `recover-batch --attribution DECISION_ID=true|false` value at the
  final `=` delimiter.
- Preserve the complete non-empty decision-ID prefix, including earlier `=`
  characters, without trimming or normalization.
- Keep exact lowercase `true`/`false` suffixes and existing structured input
  errors with exit code 2 for malformed, unrequested, or duplicate entries.
- Preserve request ordering, Store-owned recovery atomicity, public APIs,
  snapshot version 2, PostgreSQL schema version 1, JSON Schemas, and all 18
  packaged resources.

## Phase 71: Review-driven trust and bounded LLM decisions (implemented)

- Require every Failure Case to reference a `fail` or `error` Trace, require
  reviewer/root-cause/timestamp evidence before verification, and prevent a
  dirty source Trace from activating a Lesson.
- Enforce the same promotion invariants in the Store, JSON Schema, and
  fresh-install PostgreSQL DDL. Existing schema-version-1 databases require an
  operator migration, and older version-2 snapshots may need review evidence
  before loading.
- Bound each LLM decision response to 65,536 UTF-8 bytes, 1,000 JSON nodes,
  depth 20, and a 2,000-character reason before it enters persistent audit.
- Account for every system-approved candidate omitted by LLM narrowing as
  blocked; deterministically retain the first 50 approved candidates and audit
  overflow with a stable system-gate reason.
- Give `short_summary` and `full_case_summary` distinct renderers, including
  reviewed failure/fix provenance for Store-owned full summaries, and make
  keyword filtering Unicode-aware.
- Preserve snapshot version 2, PostgreSQL schema version 1, public lifecycle
  signatures, and the 18 packaged resource paths.

## Phase 72: SQLite repository choice (implemented)

- Add a standard-library `SQLiteMemoryRepository` alongside the optional
  `PostgresMemoryRepository`, with matching additive `sync()` and validated
  `load()` lifecycle semantics.
- Publish canonical and byte-identical packaged `schemas/sqlite.sql` at SQLite
  schema version 1, increasing the packaged resource allowlist from 18 to 19.
- Use `BEGIN IMMEDIATE` for top-level writes and nested savepoints inside
  caller-owned transactions; preserve borrowed/owned connection boundaries.
- Support exact replay, Store-approved forward transitions, Failure Case to
  Lesson obsolescence cascade, and all-or-nothing rollback on conflicts.
- Enforce per-collection and total count limits plus largest-record and
  aggregate 64 MiB UTF-8 payload limits before returning a fully validated
  Store; reject unsupported direct-SQL payload mutation during reconstruction.
- Document SQLite and PostgreSQL as separate embedded and server SQL choices,
  with bilingual README, architecture, product, and usage-policy coverage.
- Preserve snapshot version 2 and PostgreSQL schema version 1; SQLite starts at
  its own schema version 1.

## Phase 73: Review-driven runtime and persistence hardening (implemented)

- Bound query text, semantic score mappings, batch operations, process-local
  pending requests and finalized tombstones, aggregate pending candidate
  references, per-request candidates, and persisted lesson/policy text.
- Add explicit Gate request cancellation, bind high-level requests to Trace/run
  identity, and persist final `request_id` linkage in usage audit.
- Reject special files during bounded local ingestion; make nested SQLite
  cleanup fail safe by aborting the outer transaction when savepoint rollback
  itself fails; serialize same-instance SQLite operations and preserve the
  primary failure across top-level rollback cleanup.
- Protect PostgreSQL Trace and usage audit records with immutable and
  forward-only triggers, make Failure Case and Lesson source bindings
  immutable, lock usage Trace context reads, and advance the PostgreSQL schema
  version 2 contract.
- Enforce one strict microsecond-bounded RFC 3339 timestamp contract across
  lifecycle APIs, snapshots, JSON Schemas, SQLite, and PostgreSQL.
- Add Ruff, mypy, branch-coverage, and dependency-audit CI gates in an isolated
  quality environment.
- Publish the atomic `schemas/postgres-v1-to-v2.sql` operator migration as the
  twentieth (20th) packaged resource and test fresh installs, migration, replay
  rejection, wheel, sdist, Windows, SQLite, and real PostgreSQL execution.
- Preserve snapshot version 2 and SQLite schema version 1.

## Agent integration foundation (implemented)

- Add `LocalAgentMemory` as the focused local application boundary over the
  existing Store, Gate, completion, SQLite, and PostgreSQL contracts.
- Add explicit Git-backed pending Trace capture, capability discovery, stable
  bounded agent errors, cancellation, callback recovery IDs, and same-runtime
  exact-decision idempotency.
- Publish separately versioned `tbm.agent.v1` capability, prepared, finalized,
  completed, and error schemas with byte-identical packaged examples.
- Add `tbm capabilities`, root and nested `AGENTS.md`, repository-local
  maintainer/runtime skills, Codex integration guidance, and one cross-platform
  verification command.
- Keep pending Gate requests process-local and report that boundary honestly;
  do not claim durable MCP/HTTP sessions before the coordinated schema-version-3
  work.
- Preserve snapshot version 2, SQLite schema version 1, and PostgreSQL schema
  version 2.

## Local STDIO MCP runtime (implemented)

- Add optional `trace-backed-memory[mcp]` packaging and the `tbm-mcp` console
  entry without adding a third-party dependency to the core runtime.
- Expose only capability/health discovery and the
  prepare/finalize/complete/cancel runtime lifecycle over one long-running
  STDIO process; expose no curator, activation, raw Store, snapshot, or
  migration operation.
- Fix repository provenance and optional declared tenant in server
  configuration, capture complete Git ancestry before retrieval, and read
  PostgreSQL conninfo only from a named environment variable.
- Bound every input frame before SDK dispatch to 8 MiB, 100,000 JSON nodes,
  and depth 100; reject duplicate keys, invalid UTF-8, non-finite numbers,
  unknown request fields, and malformed strict types.
- Preserve process-local pending requests and replay tombstones, and verify
  through an actual MCP client that an unfinalized request cannot be resumed
  after server restart even when durable SQLite records are retained. Give
  every Store runtime a fresh 128-bit request namespace so a stale handle
  cannot collide with a new request after restart.
- Publish synchronized Codex project configuration, runtime policy,
  architecture, product, README, and repository-skill guidance.
- Preserve snapshot version 2, SQLite schema version 1, PostgreSQL schema
  version 2, and the 50-resource distribution contract.

## Phase 74: Deployable trust boundaries and replayable audit (in progress)

- Deliver the read-only `tbm.snapshot.v2-to-v3.mapping.v1` and
  `tbm.snapshot.v2-to-v3.plan.v1` preflight contracts, strict Python value
  objects, stable issue codes, canonical SHA-256 binding, packaged
  Schema/examples, and `tbm migration plan-v3`.
- Require explicit Trace repository/tenant bindings, memory authorization
  scopes, structured regression evidence, privileged global-policy approval,
  and `required`/audited-`disabled` ancestry policy before a mapping is ready.
- Require a trusted application verifier (or explicitly mapped local Git
  object databases in the CLI) for `required` ancestry, reject legacy
  versionless snapshots, normalize semantically unordered mapping fields
  before hashing, and report every disabled ancestry bypass.
- Preserve the active snapshot/SQLite/PostgreSQL/agent versions while the
  preflight is read-only; do not emit an unusable partial version-3 snapshot.
- Deliver inert `tbm.snapshot.v2-to-v3.bundle.v1` artifacts with exact and
  normalized source hashes, strict plan replay, bounded duplicate-rejecting
  JSON, and content-derived identities.
- Add an immutable, side-by-side SQLite staging repository plus version-gated
  PostgreSQL staging and rollback scripts. Keep all staging invisible to
  runtime v2 adapters and expose no activation operation.
- Add the opt-in unified SQLite v3 runtime bundle and ordered 15-component
  manifest. Install the complete non-migration authority catalog in one outer
  transaction on one connection, bind immutable bundle/component metadata,
  and fail closed on any main/temp table, index, automatic-index, trigger, or
  version drift. Keep migration staging outside the bundle and preserve the
  active SQLite v1 transport boundary.
- Publish the immutable `tbm.gate-session.v3` domain contract, explicit
  lifecycle transition graph, optimistic revision checks, lease/expiry
  invariants, bounded strict JSON parser, and packaged Schema/example. Keep
  the domain contract persistence-neutral. Its opt-in SQLite and isolated
  PostgreSQL adapters do not make GateSession active runtime authority;
  workers, authorization, and service integration remain outstanding.
- Publish storage-neutral `tbm.replay.v3` content-addressed artifact,
  injection, and fixed-component decision-manifest contracts with canonical
  self-hashes, strict bounded JSON, packaged Schemas/examples, and explicit
  `complete` versus `legacy_partial` semantics. Keep active v2 adapters from
  claiming artifact persistence or exact decision replay.
- Add an opt-in isolated SQLite replay ledger for exact artifact bytes,
  injection descriptors, and decision manifests, with atomic bundle writes,
  immutable rows, exact idempotency/conflicts, foreign-key linkage, canonical
  schema drift checks, bounded defensive loads, byte rehashing, caller
  savepoints, and concurrent replay tests. Keep access control, encryption,
  retention, GateSession linkage, and active integration outstanding.
- Add version-gated isolated PostgreSQL replay-ledger install and fail-closed
  rollback resources with bounded exact bytes, immutable injection/manifest
  descriptors, relational linkage, fixed-search-path mutation guards, active
  metadata locking, and exact catalog verification. Keep the PostgreSQL
  repository adapter with exact-byte/descriptor revalidation, exact
  idempotency/conflict handling, caller savepoints, schema/function/trigger
  drift checks, bounded loads, and concurrency conformance. Keep
  authorization/encryption/retention, GateSession transaction linkage, and
  active integration outstanding.
- Publish `tbm.regression-evidence.v3` as a storage-neutral, content-addressed
  verification record with distinct submitter/verifier principals, exact
  source/fix/verification commit relationships, evaluator and environment
  provenance, expected/observed outcomes, artifact hashes, and attestation.
  This does not replace the active v2 boolean or authorize publication; the
  immutable MemoryRevision and service integration remain later work.
- Publish `tbm.fix-evidence.v3` as a storage-neutral, content-addressed record
  with exact case/Trace and source/fix commit linkage, verified ancestry,
  bounded artifact hashes, and independent submitter/reviewer principals.
  Add a strict MemoryRevision evidence-bundle preflight that binds fix and
  regression evidence to the same case, source Trace, and commits. Keep
  persistence, approval, and activation as later service work.
- Publish proposal-only `tbm.memory-revision.v3` as a storage-neutral,
  content-derived immutable revision with exact parent, content artifact,
  canonical scope, case/fix/structured-evidence references, and server-owned
  proposer/client attestation context. Keep approval and activation out of the
  contract until authenticated authorization and audit service operations are
  delivered.
- Add an opt-in isolated SQLite immutable MemoryRevision proposal ledger that
  atomically stores the exact FixEvidence/regression bundle, enforces linear
  parent/revision continuity, supports exact idempotent replay and caller
  savepoints, and reads back before commit. Keep approval, activation, active
  v2 projection, authorization, and retention outside this ledger.
- Add the isolated PostgreSQL peer with an install/fail-closed rollback pair,
  exact catalog fingerprint validation, caller-compatible transactions,
  immutable evidence closure, linear parent continuity, and replay that
  rejects rather than repairs tampered stored proposals. Keep it proposal-only
  and outside active v2 projection, approval, activation, and authorization.
- Publish separate storage-neutral
  `tbm.memory-revision-approval.v3` and
  `tbm.memory-revision-activation.v3` content-derived events. Re-verify exact
  artifact bytes, evidence closure, proposal lineage, historical approval
  authorization, current activation authorization, actor separation, target,
  and linear predecessor linkage. Forbid global MemoryRevision publication and
  target relocation. Keep persistence, audit linkage, attestation
  authentication, and active-v2 projection for the publication-authority
  stage, including durable current-head locking.
- Add opt-in SQLite and isolated PostgreSQL MemoryRevision publication
  authorities. Persist immutable approval/activation events with their exact
  policy/request/decision descriptors and attestation-verifier identity;
  revalidate the stored proposal/evidence/artifact bytes; lock a
  tenant/repository/memory head and advance it by compare-and-swap; provide
  exact idempotent replay, nested savepoints, immutable database guards,
  catalog/schema drift checks, pre-commit read-back, and fail-closed
  PostgreSQL rollback. Keep artifact storage, retention/encryption, active-v2
  projection, and active Agent/MCP integration outstanding.
- Publish content-addressed `tbm.retrieval-snapshot.v3` and nested
  RetrievalHit/IndexVersion records with exact authorization/context/query
  linkage, ordered revision hits, candidate hashes, finite stage/fusion scores,
  retriever/index versions, bounds, and truncation reasons. Reject oversized
  strings before UTF-8 encoding and validate direct-parser object shape and
  collection cardinality before avoidable allocation. Keep System and Semantic
  Gate outcomes separate and active Store/GateSession integration outstanding.
- Publish content-addressed System Gate evaluation and Semantic Gate attempt
  contracts with exact retrieval/policy/provider/model/prompt/response
  provenance, success/failure shapes, ordered retry parents, bounded metrics,
  and cross-record verification that semantic decisions can only narrow
  deterministic System Gate results. Add a strict bounded whole-chain
  verifier and reject oversized direct-parser inputs before avoidable
  allocation. At this contract-only increment, artifact validation, durable
  adapter parity, and active runtime integration were still outstanding.
- Add an opt-in side-by-side SQLite SemanticGateAttempt ledger that depends on
  immutable Gate evidence, enforces one bounded linear chain per System Gate
  evaluation through unique sequence and CAS head, supports exact idempotent
  replay, preserves caller transactions with savepoints, detects canonical
  schema drift, and revalidates the full chain on every read. Keep
  GateSession transaction linkage and active Agent/MCP emission outstanding;
  exact bytes are provided by a separate opt-in repository below.
- Add the isolated PostgreSQL SemanticGateAttempt peer with active-v2 and Gate
  evidence install gates, parent-before-head locks, one row-locked CAS head,
  deferred commit-time chain consistency, exact descriptor/whole-chain
  read-back, complete security-catalog fingerprinting, caller savepoints,
  concurrent exact replay/fork conformance, and fail-closed `RESTRICT`
  rollback. Exact bytes are provided by the separate opt-in PostgreSQL
  repository below; keep provider authentication, GateSession/replay
  transaction linkage, and active adapter emission outstanding.
- Publish storage-neutral `tbm.semantic-gate-artifact.v3` bindings that join
  exact non-empty prompt/response bytes to one SemanticGateAttempt role and
  digest, retain classification/encryption/redaction metadata, enforce the
  prompt and response byte limits, reject response artifacts for failed
  attempts, and provide bounded duplicate-key-rejecting JSON plus canonical
  Schema/example resources. Keep provider authentication, trusted timestamps,
  GateSession/replay transactions, and active emission outstanding.
- Add the independent version-1 SQLite Semantic Gate artifact schema and
  `SQLiteSemanticGateArtifactV3Repository`. One outer transaction/savepoint
  atomically appends the attempt, exact public/internal prompt/response bytes,
  and role bindings; exact replay is deduplicated and fully read back. SQL
  recomputes byte digests and derived IDs, compares every descriptor field,
  enforces role/status/size/media constraints, blocks replacement writes even
  with recursive triggers disabled, and rejects unexpected managed objects.
  Keep encrypted sensitive storage, provider trust, GateSession/replay
  linkage, and active emission outstanding.
- Add the independent version-1 PostgreSQL Semantic Gate artifact schema and
  `PostgresSemanticGateArtifactV3Repository`. One outer transaction/savepoint
  atomically appends the attempt, exact public/internal prompt/response bytes,
  and role bindings. Database triggers recompute SHA-256 and derived IDs,
  compare descriptor fields, enforce role/status/size/media constraints, and
  block mutation. Operations validate the full security catalog, preserve
  caller transactions, support concurrent exact replay, and ship a
  fail-closed fingerprinted `RESTRICT` rollback. Keep encrypted sensitive
  storage, provider authentication/trusted time, GateSession/replay linkage,
  and active emission outstanding.
- Add storage-neutral `AuthenticatedSemanticGateService` over both artifact
  authorities. Exact-match a transport-authenticated provider,
  authenticator, and credential identifier against server-owned registration;
  reload the Gate evidence and expected retry parent before provider work;
  own provider/model/template/config provenance and trusted start/finish time;
  atomically retain exact prompt/response bytes and require durable read-back.
  Persist arbitrary provider failures only as sanitized prompt-only attempts.
  Keep encryption/retention/access control, signed provider attestation,
  GateSession/replay transaction linkage, and active emission outstanding.
- Add the storage-neutral encrypted Artifact Authority contract, a caller-owned
  authenticated-encryption provider boundary, `AuthenticatedArtifactService`,
  an isolated immutable SQLite version-1 repository, and an isolated
  active-v2-gated PostgreSQL version-1 peer. Persist and read back exact
  `artifact:write/read` decisions before storage access; bind scope,
  authorization, provider/key, trusted time, and retention into AAD; decrypt
  and verify plaintext before append and on every read. The PostgreSQL peer
  fixes `search_path`, locks and fingerprints the managed catalog, verifies
  ciphertext digests in the database, preserves caller savepoints, supports
  concurrent exact replay, and provides fail-closed `RESTRICT` rollback. Keep
  object storage parity, physical purge/key destruction, provider
  authentication, MemoryRevision/GateSession linkage, and active emission
  outstanding.
- Add storage-neutral exact approval/activation provenance read bundles to the
  SQLite and PostgreSQL publication authorities, then add
  `ActivatedRevisionSource`. One authorized read starts from the durable head,
  reloads proposal/evidence/publication authorization, requires trusted
  append-time attestation-verifier identities, performs a separately
  authorized/decrypted Artifact read, verifies the full activation, and
  rejects a head that changes before candidate return. Keep applicability,
  indexing/RetrievalSnapshot/Gates/rendering, proposal-signature byte replay,
  and active Agent/MCP projection outstanding.
- Publish content-addressed `tbm.run-outcome.v3` and
  `tbm.outcome-attribution.v3` contracts that bind completed GateSessions to
  explicit evaluator evidence while keeping observed association separate
  from independently verified causal claims.
- Add `GateSessionCompletionService`, the isolated version-1 SQLite
  RunOutcome schema, and `SQLiteOutcomeV3Repository`. One trusted timestamp
  and one outer transaction/savepoint build the content-addressed outcome,
  CAS-append the `EXECUTING` to `COMPLETED` session revision, insert the
  immutable outcome, and read both records back. Exact terminal replay is
  idempotent; conflicting measurements, schema drift, clock rollback, or
  partial writes fail closed.
- Add PostgreSQL RunOutcome parity with an isolated version-1 install and
  fail-closed exact-catalog rollback, database-time sampling after the
  GateSession head lock, CAS completion, immutable outcome insertion, exact
  replay/read-back, caller savepoints, and concurrent single-insert behavior.
  Keep authenticated evaluator/artifact checks and active
  Agent/MCP/HTTP/SDK integration outstanding.
- Add opt-in isolated SQLite and PostgreSQL OutcomeAttribution ledgers with
  independent version-1 schemas, exact content-ID replay, immutable
  multi-claim storage, completed outcome/session/usage/revision linkage,
  canonical descriptor revalidation, replacement-write guards, schema/catalog
  drift checks, caller savepoints, and concurrent idempotency. PostgreSQL adds
  database-side validation, row locking, and fail-closed rollback. Keep
  authenticated evaluator/verifier derivation, trusted-time construction,
  artifact authorization, attribution outbox delivery, and active runtime
  integration outstanding.
- Publish storage-neutral `tbm.completion-outbox-event.v3` and
  `tbm.completion-outbox-delivery.v3` contracts, then add opt-in isolated
  SQLite and PostgreSQL authorities that atomically complete the GateSession,
  insert the RunOutcome and immutable event, and create the initial append-only
  delivery revision/head. Add bounded claims, expiring lease reclaim,
  exact-version acknowledgement, retry/dead-letter transitions, canonical
  read-back, schema/catalog-drift checks, caller savepoints, concurrent
  single-claim behavior, PostgreSQL database-time/row-lock/CAS parity and
  fail-closed rollback, and explicit at-least-once consumer semantics. Add a
  storage-neutral bounded delivery worker that validates the full claim page
  before callbacks, persists sanitized consumer error codes, verifies exact
  transition/read-back receipts, applies configured retry/dead-letter limits,
  and reports superseded or recovery-required write uncertainty. Keep a
  concrete network transport, authenticated evaluator/artifact verification,
  and active Agent/MCP/HTTP/SDK integration outstanding.
- Publish storage-neutral `tbm.audit-event.v3` and
  `tbm.recovery-action.v3` contracts with content-derived identity, exact
  stream parents, authenticated actor slots, typed references, explicit
  request digests, and cross-record verification against GateSession and
  derived MemoryRunRemediation state.
- Add an opt-in isolated SQLite audit ledger with immutable stream events,
  exact parent/head CAS, atomic RecoveryAction/event append, session-scoped
  request-digest uniqueness, canonical read revalidation, schema-drift
  detection, caller savepoints, and concurrent idempotency. Keep authenticated
  actor derivation, the underlying GateSession/remediation transition,
  and active service integration outstanding.
- Add the matching opt-in PostgreSQL audit ledger with version-gated isolated
  install, fail-closed exact-catalog rollback, deterministic collation,
  row-lock stream CAS, deferred stream and RecoveryAction/event consistency,
  canonical read revalidation, caller savepoints, concurrent idempotency, and
  catalog/function-body drift checks. Keep active PostgreSQL schema version 2,
  authenticated actor derivation, and the wider service transaction unchanged.
- Add an opt-in side-by-side SQLite GateSession repository with append-only
  canonical revisions, a scoped atomic idempotency index, trusted-clock CAS
  transitions and lease renewal, schema-drift detection, caller savepoints,
  concurrency tests, and bounded due discovery. Keep active SQLite schema
  version 1 and the process-local Agent/MCP request token unchanged.
- Add the matching opt-in PostgreSQL GateSession repository in an isolated
  schema, with version-gated install/fail-closed rollback, deterministic
  identity collation, database-time-after-row-lock semantics, append-only
  trigger enforcement, exact-version CAS, catalog drift checks, caller
  savepoints, and concurrent idempotency tests. Keep active PostgreSQL schema
  version 2 and the Agent/MCP lifecycle unchanged.
- Publish storage-neutral authorization-v3 policy and decision contracts with
  canonical repository/tenant bindings, exact aliases, principal/client
  registries, explicit global/tenant/repository role bindings, point-in-time
  evaluation, exact policy/request verification, strict bounded JSON, and
  packaged Schemas/examples. Keep authenticated identity context, persistence,
  and active adapter enforcement outstanding.
- Extend publication authorization without broadening ordinary repository
  operations: tenant-owned review and activation may target the tenant or one
  exact repository, repository bindings cannot authorize tenant-wide requests,
  and existing targetless global policy creation and approval remain
  independently assignable.
- Add an opt-in isolated SQLite authorization authority that durably stores
  immutable policy bundles and linked allow/deny decisions, verifies the exact
  request before append, enforces unique request identity, revalidates stored
  descriptors, detects exact schema drift, and preserves caller savepoints.
  Keep caller authentication and active Store/Agent/MCP integration
  outstanding.
- Add the matching isolated PostgreSQL authorization authority with atomic
  active-v2-gated install, fail-closed exact-catalog rollback, immutable
  triggers, concurrent exact-replay idempotency, descriptor revalidation, and
  caller-savepoint preservation. Keep authenticated service integration
  outstanding.
- Publish the storage-neutral, content-addressed `tbm.entity-registry.v3`
  snapshot. Add Organization, formal Tenant, and Environment identities while
  reusing the authorization-v3 Principal, AgentClient, canonical Repository,
  alias, and role-binding records. Enforce organization/tenant closure and
  same-tenant environment/repository linkage. Keep normalized persistence and
  authenticated service enforcement outstanding.
- Add an opt-in isolated SQLite normalized entity-registry authority. Store
  every snapshot entity, binding, permission, and attribute under composite
  foreign keys; treat canonical JSON as an integrity witness; revalidate all
  rows on read; enforce immutable rows, exact replay, schema drift checks, and
  caller savepoints. Keep PostgreSQL parity and active service integration
  outstanding.
- Add PostgreSQL parity for the normalized entity registry with active-v2
  install gating, immutable DML/TRUNCATE guards, complete catalog and ACL
  fingerprinting, concurrent exact replay, caller savepoints, and fail-closed
  exact-catalog rollback. Keep active service integration outstanding.
- Add a storage-neutral `AuthenticatedRetrievalService` kernel that accepts
  only trusted Principal/AgentClient records and server-owned target context,
  evaluates and persists the exact allow/deny decision, reads it back,
  rechecks complete registry rotation and environment binding, and invokes
  retrieval only after every check passes. Add an opt-in
  `AuthenticatedLocalAgentMemory` facade that authorizes before Trace
  registration and binds canonical server-owned tenant/repository identities.
  Add an opt-in local `tbm-mcp --auth-*` profile with a bounded trusted
  registry, SQLite authorization authority, server-selected identities, no
  request identity fields, and facade-owned lifecycle handles. Keep transport
  authentication and general CLI/HTTP/SDK integration outstanding.
- Compose authorization with SQLite/PostgreSQL GateSession authorities through
  `AuthenticatedGateSessionService`: durably create/read back the scoped
  session before preparation, suppress idempotent duplicate retrieval, require
  trusted retrieval/System-Gate evidence verification, CAS-publish
  `PREPARED`, and compensate failures with version-checked cancellation or
  explicit recovery-required state. Keep later lifecycle phases and active
  adapter integration outstanding.
- Add a storage-neutral bounded `GateSessionRecoveryWorker` over SQLite and
  PostgreSQL due discovery. Prevalidate each complete page, expire only
  session-expired prepared/awaiting heads with exact CAS/read-back, report
  lease-only and graph-blocked states for explicit recovery, and classify
  concurrent revisions as superseded without blind retry.
- Add an opt-in immutable SQLite RetrievalSnapshot/SystemGateEvaluation
  authority and a storage-neutral durable evidence verifier. Store each exact
  pair atomically, prevent replacement-delete bypasses with recursive
  triggers, and bind PREPARED evidence to the authorized session, Trace, run,
  and identity scope.
- Add PostgreSQL parity for immutable RetrievalSnapshot/SystemGateEvaluation
  evidence with active-v2 install gating, exact descriptor read-back,
  concurrent idempotent replay, complete security-catalog fingerprinting,
  immutable DML/TRUNCATE guards, caller savepoints, and fail-closed
  `RESTRICT` rollback. Keep active adapter emission outstanding.
- Add content-addressed retrieval policy and a storage-neutral authenticated
  preparation kernel. Authorize before discovery; load verified current
  ActivatedRevision candidates under the same scope; enforce classification,
  exact applicability, eval-leakage, required/disabled ancestry, deterministic
  weighted fusion, minimum/top-K/payload bounds, and task-mode System Gate;
  then emit paired RetrievalSnapshot/SystemGateEvaluation evidence and recheck
  every selected head plus the policy. At this increment, keep managed
  production indexes, Semantic Gate, durable GateSession attachment, and
  active adapter emission outstanding.
- Add an opt-in bounded managed-index bundle and concrete discovery adapter.
  Build exactly versioned metadata, deterministic Unicode lexical, explicit
  local semantic-vector, structured-evidence graph, and immutable Git-DAG
  views over verified ActivatedRevision candidates. Bind semantic
  provider/version/vector evidence to the raw-query digest and prepared
  context. Add exact-byte immutable SQLite and active-v2-gated isolated
  PostgreSQL repositories with scope-head CAS, concurrent replay, catalog and
  function-body drift checks, and fail-closed explicit rollback. At this
  increment, keep production sharding/workers, external FTS/ANN providers,
  durable GateSession attachment, Semantic Gate, and active adapter emission
  outstanding.
- Add `DurableRetrievalPreparationService` as the opt-in same-scope bridge
  across authenticated retrieval preparation, Gate evidence, and GateSession
  authorities. Derive one complete request fingerprint, authorize once, create
  and read back `CREATED`, prepare with that exact session, atomically store
  and verify the evidence pair, and CAS-publish `PREPARED`. Cover exact replay,
  sanitized cancellation/recovery, immutable orphan-evidence behavior, and
  caller-owned same-connection rollback for SQLite and PostgreSQL. Keep
  Semantic Gate, later lifecycle transitions, and active adapter emission
  outstanding.
- Add `AuthenticatedSemanticGateSessionService` as the opt-in bridge from
  durable `PREPARED` evidence through `AWAITING_DECISION` to `DECIDED`.
  Authenticate and preflight the provider/evidence/attempt chain before the
  provider call; retain failed prompt-only attempts for explicit parent-bound
  retry; store the complete ordered attempt chain and successful decision in
  the session; and recover a retained success without repeating the external
  call. Reuse identical prompt/response content descriptors across retry
  bindings. Cover exact decided replay, stale version/parent rejection,
  sanitized recovery-required states, same-connection SQLite rollback, and
  PostgreSQL parity. Keep rendering/injection, replay-manifest finalization,
  later lifecycle transitions, and active adapter emission outstanding.
- Add `UsageDecision` as the content-addressed final-use audit and
  `DurableFinalizationService` as the opt-in `DECIDED -> FINALIZED`
  composition. Bind the current authorization event, complete monotonic Gate
  evidence, active revision heads, policy, deterministic bounded renderer,
  exact injection, and fixed eight-component replay manifest. Atomically
  retain the UsageDecision plus all replay component bytes, require exact
  read-back, provide ordered recovery when the session CAS is unconfirmed, and
  cover SQLite/PostgreSQL success, replay, conflict, and caller-owned outer
  rollback. Keep protected-content encryption, active adapters, retention,
  and replay-read authorization outstanding.
- Add `DurableExecutionService` as the opt-in authenticated runtime back half.
  Verify the exact retained finalization bundle before
  `FINALIZED -> EXECUTING`; require the original retrieval authorization plus
  a current owner-matched `gate_session:transition` decision; inherit rather
  than silently replace the finalization lease; and provide exact-version
  resume and abandonment. Invoke a trusted authenticator on every completion
  to match live transport proof to a current server-owned evaluator
  registration, then compose the existing atomic
  RunOutcome, `COMPLETED`, completion event, and leased delivery authority.
  Cover SQLite/PostgreSQL parity, exact replay, forged permission/evaluator
  rejection, and caller-owned rollback. Keep external executor effects
  idempotent by `run_id`; they cannot join a database transaction. Keep
  durable transition-event linkage, active adapter wiring, protected-content
  encryption, retention, and replay-read authorization outstanding.
- Add `AuthenticatedDurableAgentMemory` as the adapter-neutral composition of
  durable preparation, Semantic Gate, finalization, execution, cancellation,
  and completion. Recover the original retrieval scope only from retained
  RetrievalSnapshot authorization linkage and the current registry; reject a
  mismatched authorization/session/evidence/semantic/revision service graph;
  append a fresh `gate_session:transition` decision for each post-prepare
  GateSession mutation; and expose exact-version cancel/current-state
  operations without process-local handles. Cover the complete SQLite lifecycle, PostgreSQL continuation
  parity, policy/target rotation, owner rejection, exact cancel replay, and
  service-graph mismatch. Keep transport authentication, default MCP/HTTP/SDK
  wiring, protected-content replay, and durable transition-event linkage
  outstanding.
- Add a shared strict `AgentProtocolDispatcher` for active `tbm.agent.v1`
  transports, refactor STDIO MCP to use it, and add an optional loopback-only
  bearer-authenticated `tbm-http` adapter plus dependency-free typed Python
  client. Bound and strictly parse every HTTP request/response, disable client
  proxies and redirects, publish cancel/error schemas, and cover real-socket
  lifecycle, cross-dispatch conformance, concurrent prepare, authentication,
  malformed input, CLI startup, and restart invalidation. Keep this as a
  single-host version-2 profile: the durable facade, service identities,
  remote/shared deployment, and TypeScript SDK remain outstanding.
- Complete the machine-readable local Agent contract with four strict request
  schemas, a health schema/example, and canonical OpenAPI 3.1 referencing every
  request, success, and error envelope. Package exact bytes for every contract
  and example, and run one normalized lifecycle through the dispatcher, a real
  STDIO MCP process, and HTTP. Keep `tbm.agent.v1`, snapshot version 2, SQLite
  schema version 1, PostgreSQL schema version 2, and the single-host security
  boundary unchanged.
- Add the dependency-free `AsyncAgentHTTPClient` without blocking the event
  loop and a standalone dependency-free Node.js TypeScript SDK for all six
  local HTTP routes. Keep strict loopback URL/token/body/protocol/error checks,
  reject duplicate response keys, use direct non-proxy `node:http`, define
  abort/timeout as non-retrying wait cancellation, and run TypeScript against a
  real Python HTTP lifecycle. Pin the TypeScript toolchain and verify build,
  contract drift, tests, and package contents in CI.
- Add `tbm.replay-export.v3` as a bounded, content-addressed, portable read
  envelope over retained decision replay records. Export the exact manifest,
  optional injection descriptor, and every present component byte in canonical
  order; require an explicit classification allowlist, enforce caller and
  global content limits, strictly parse canonical base64/JSON, and verify every
  digest and linkage. Reuse both opt-in replay repositories through a
  storage-neutral reader protocol without schema changes. Keep replay-read
  authorization and Agent/HTTP/MCP exposure outstanding.
- Add session-bound replay-read authorization to
  `AuthenticatedDurableAgentMemory`. Resolve one unique manifest from the
  retained GateSession decision/usage/injection linkage rather than a
  caller-selected content ID; persist and read back a fresh repository-scoped
  `artifact:read` decision; bind the request to the exact session version,
  explicit classification allowlist, and byte limit; and recheck authorization
  and unchanged session state after export. Add descriptor-only
  `load_manifest_for_session()` parity to SQLite/PostgreSQL so manifest lookup
  never loads injection bytes before export preflight. Cover allow/deny,
  stale-version, ambiguous-linkage, no-preauthorization-byte-read, exact bundle,
  and PostgreSQL lifecycle parity. Keep transport-authenticated HTTP/MCP/SDK
  exposure, protected-content encryption, and retention outstanding.
- Add `tbm.durable-agent-wire.v1` as the optional strict adapter-neutral
  request/response dispatcher over `AuthenticatedDurableAgentMemory`. Map
  prepare, Semantic decision, finalization, start/resume/abandon, completion,
  cancellation, current-state reads, and explicitly enabled replay export.
  Keep all caller/provider/evaluator/scope identities outside request JSON;
  resolve canonical repositories and evaluator registrations through
  server-owned callbacks; require canonical base64 for exact bytes; reject a
  mismatched decided response replay; and default injection/replay content
  exposure to disabled. Cover the complete SQLite lifecycle, exact cancel and
  abandonment replay, identity-field rejection, stale revisions, content
  profiles, and authorized replay. Keep transport authentication and active
  HTTP/MCP/CLI/SDK selection outstanding.
- Add the explicit `tbm-http --profile durable-v3` product adapter over the
  durable wire and sole runtime factory. Load runtime dependencies and trusted
  identity contexts from an operator-controlled application factory; require a
  bounded local bearer before context derivation; reject request identity
  fields, ambiguous HTTP framing, malformed canonical base64, and stale
  revisions; keep injection/replay content disabled by default; and support
  bounded TLS handshakes for non-loopback binds. Cover a real-socket lifecycle,
  SQLite runtime/server reopen after every lifecycle state, idempotent and
  stale transitions, replay allow/deny, and sanitized failures. Keep durable
  MCP, TypeScript, local daemon, and remote multi-tenant service work
  outstanding.
- Add the explicit trusted-local `tbm-mcp --profile durable-v3` adapter over
  the same durable wire and runtime factory. Load dependencies plus fixed
  service/provider/evaluator contexts from an operator-controlled application
  factory; keep every identity outside tool JSON; expose only the eleven
  runtime lifecycle tools with annotations and bounded STDIO; hide content by
  default; and persist no process-local session handle. Cover an in-process
  complete lifecycle and a real MCP client across three consecutive child
  processes for prepare, restart continuation, exact completion retry, and
  replay export. State explicitly that local STDIO has no independent peer
  authentication and is not shared-service MCP. Keep TypeScript, local daemon,
  and remote multi-tenant service work outstanding.
- Add the dependency-free Node.js `DurableAgentHTTPClient` as the explicit
  TypeScript selector for `tbm.durable-agent-wire.v1`. Export typed requests,
  exact operation responses, capability negotiation, stable error mapping,
  exact-version session references, bounded opt-in retry, and the execution
  `heartbeat()`/`resume()` path while rejecting every caller/provider/evaluator
  identity field before serialization. Run the same lifecycle fixture through
  synchronous/asynchronous Python and TypeScript clients, including
  cancellation, completion replay, session read, and replay export. Keep the
  compatibility client/default profile unchanged and keep CLI durable
  selection, local daemon, and remote multi-tenant service work outstanding.
- Add the explicit `tbmd init/local/doctor/health` local SQLite v3 process
  owner. Require an operator application factory with trusted MCP/HTTP
  contexts and an outbox consumer; create owner-controlled fixed local state;
  hold one race-checked cross-platform lock; and share one runtime,
  dispatcher, connection, and operation lock across bounded STDIO MCP,
  loopback HTTP, GateSession recovery, and completion-outbox delivery. Stop
  HTTP, workers, runtime, and the state lock in order. Cover real MCP plus HTTP,
  concurrent clients, second-process exclusion, hard crash/reopen, expired
  session recovery, pre-ack outbox lease reclaim, permission/alias rejection,
  doctor/health, packaging, and deterministic public errors. Keep the default
  compatibility profile, SQLite v1 data, shared-service workers, and migration
  cutover unchanged.

- Replace the regression boolean with structured Trace/run/evaluator evidence
  and verifiable source/fix/regression commit relationships.
- Wire transport-authenticated service-owned identities and the published
  pre-retrieval authorization kernel into shared-service MCP and active
  CLI/HTTP/SDK adapters so scope becomes an enforceable transport boundary.
- Persist Gate requests or use signed envelopes with idempotency, expiry,
  cancellation, capacity control, and crash recovery.
- Wire the authenticated durable Agent composition into
  transport-authenticated active adapters. Add
  production index sharding/workers and external FTS/ANN provider profiles
  without weakening the bounded reference contract.
- Deliver these breaking contracts together as snapshot schema version 3 and
  PostgreSQL schema version 3 with documented migrations.

## Full Persistence release train (planned; current priority)

[ADR-0006](adr/0006-full-persistence-reducer-native-memory.md) supersedes the
next-delivery priorities of the earlier durable-v3 cutover plan without
rewriting the historical phases above. The repository remains on the authority
graph until the event-first cutover is verified; `full_persistence` stays
`false` throughout the train until the complete exit gate passes.

F0-01 through F0-05 are now delivered. The accepted bilingual architecture
decision is backed by the strict storage-neutral `tbm.event.v1` envelope; a
sealed typed registry with strict payload schemas, unknown-event behavior,
compatibility reporting, and explicit upcasters; and the storage-neutral event
ledger port for atomic append, exact replay, bounded reads, verification, and
subscriptions. A machine-readable authority registry and repository gate
classify every current registered SQLite/PostgreSQL persistence module and
reject new unregistered sources of truth. These are contract/governance
foundations; the active composition and source-of-truth model remain unchanged.

F1-01 through F1-06 are now delivered as opt-in persistence/reducer
foundations.
`SQLiteEventLedgerV1` supplies WAL, single-owner locking, atomic batch/head/
idempotency commits, integrity verification, and backup/restore inside the
sixteen-component unified v3 bundle. `PostgresEventLedgerV1` supplies the same
port through an isolated schema, fixed row-lock order, exact catalog digest,
caller savepoints, concurrency, and fail-closed rollback. Both retain exact
Artifact descriptors without reading protected bytes, and cross-backend tests
require identical receipts and pages. The storage-neutral `tbm.reducer.v1`
framework now adds sealed reducer versioning, code/configuration hashes,
double-execution determinism checks, bounded canonical projection state,
typed-event/upcaster integration, checkpoint/resume, poison-event evidence,
shadow comparison, approved CAS activation, and append-only rollback.
SQLite/PostgreSQL retain exact checkpoints and projection-head history in their
event-ledger schemas. Explicit `tbmd ledger` and `tbmd projection` commands
verify, inspect, rebuild, compare, activate, and roll back an operator-selected
SQLite ledger. One committed golden digest is checked on Python 3.11-3.13 on
Windows and Linux. F2 is now in progress: both durable backends append typed
GateSession revision events before synchronous revision-row projections, the
GateSession reducer supports exact row parity, and SQLite exercises persistent
rebuild/resume, comparison, activation, and rollback. RetrievalSnapshot and
SystemGateEvaluation writes also append compact Artifact-linked events before
their synchronous rows in both backends, with a pure current-linkage reducer.
Semantic Gate attempts now require the retained System Gate parent and append
compact failed/succeeded events before attempt/exact-byte projections; their
pure reducer binds canonical events to fieldwise authority parity. Finalization
now appends `UsageDecisionFinalized` then `InjectionRendered` in one atomic
SQLite/PostgreSQL unit with the session and replay projections. The
`final-decision-injection` reducer verifies exact authority parity, and the
explicit durable replay reader reconstructs metadata from the ledger while
loading exact bytes from the authenticated replay authority. Outcome/
attribution events and reducers now rebuild exact durable rows, and completion
outbox operations append local effect request/delivery/dead-letter evidence
that `effect-queue` verifies with exact history parity. Provider receipts,
unknown-result reconciliation, durable compensation, Memory/index/audit/
metrics reducers, migration, complete lifecycle integration, and the remaining
event-first cutover remain outstanding. The SQLite local daemon now also has a
real child-process `SIGKILL`/reopen sweep after acknowledged `PREPARED`,
`DECIDED`, `FINALIZED`, `EXECUTING`, and `COMPLETED` commits. Each restart
requires exact command replay, and the final ledger is reducer-checked against
the durable GateSession row without duplicate logical transitions. Fine-grained
auth/`CREATED`/evidence/provider/replay-retention and outbox pre/post-ack crash
failpoints remain open, so the complete crash matrix is not yet delivered.

- **F0 — Architecture freeze (delivered):** ADR-0006, canonical event contract
  and registry, ledger ports, and a guard against new independent authorities.
- **F1 — Ledger and reducer kernel (opt-in foundation delivered):** opt-in
  SQLite/PostgreSQL event ledgers, Artifact references, versioned reducer
  runtime, projection operator CLI, and six-cell cross-platform determinism
  verification are delivered; the F1 foundation alone and default compatibility
  lifecycle do not select them, while the explicit durable F2 slices do.
- **F2 — Durable lifecycle event-first cutover (in progress):** GateSession
  revision events/reducer, Retrieval/System Gate evidence events/reducer, and
  Semantic attempt-chain and final decision/injection events/reducers are
  implemented as transactional migration increments; ledger-backed replay
  export is selected by explicit durable runtimes. Outcome/attribution and
  local completion-effect projections through dead letter are delivered.
  A local-daemon hard-restart sweep covers five acknowledged lifecycle commits,
  but the finer transaction/effect failpoints remain open. Provider receipt/
  reconciliation, durable compensation, full transport parity, and the complete
  crash matrix remain open.
- **F3 — Trace, Git, and effect evidence:** ordered Trace/Git observations,
  Git-graph projection, external-effect receipts, Codex hooks, and governed
  retention/crypto-erasure.
- **F4 — Governed memory projections:** failure extraction, structured evidence,
  MemoryRevision publication, ActivatedRevision retrieval, policy/index, and
  remaining Memory projections become reducer-native; the opt-in outcome/
  effect reducers are already delivered in F2.
- **F5 — Migration and cutover:** import compatibility and durable-v3 sources,
  verify and shadow-compare rebuilt state, select the ledger by default, freeze
  old writes, and retain a read-only rollback window.
- **F6 — Shared service and stable release:** authenticated remote transports,
  PostgreSQL tenant isolation, Review Console, GitHub PR Check, observability,
  backup/DR, security governance, and stable-release qualification.

The first fifteen dependency-ordered foundations are now delivered as
candidate work: F0-01 through F0-05, F1-01 through F1-06, GateSession event
adapter/reducer, replay exporter reducer, and outcome/effect reducers. New
standalone authorities, protocol families, and SQL components remain frozen
except for documented security/corruption fixes or ledger, reducer, and
migration blockers while the remaining cutover gates are completed.
