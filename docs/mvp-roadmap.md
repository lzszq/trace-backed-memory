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
