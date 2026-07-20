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
