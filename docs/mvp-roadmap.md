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
- Provide dependency-free JSON snapshot persistence and active-lessons YAML save/load until DB adapters exist.
- Preserve numeric-looking scope strings through active-lessons YAML round trips.
- Reject stored lessons whose source case is missing, unverified, or lacks regression evidence.
- Store manually maintained project policies, validate policy IDs/text/status/scope/confidence, reject runtime memory ID collisions across failure cases, lessons, and project policies, and include policies in scoped retrieval.

## Phase 5: Memory retrieval and gating

- Retrieve by metadata filter first, requiring all declared scope fields to match.
- Optionally add keyword/vector search.
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
