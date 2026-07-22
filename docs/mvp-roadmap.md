# MVP Roadmap

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
