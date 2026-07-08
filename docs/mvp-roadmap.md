# MVP Roadmap

## Phase 0: Project framing

- Define memory object model.
- Define system gate policy.
- Define supported modes: debug, repair, regression, planning, eval, production.
- Define how commit_sha, prompt_version, tool_schema_version, and eval_suite are captured.

## Phase 1: Trace capture

- Record run metadata.
- Attach git commit SHA and dirty state.
- Store trace URI, eval result, tool calls, errors, and prompt version.
- Keep raw trace out of runtime prompt.

## Phase 2: Failure case extraction

- Generate failure case draft from failed traces.
- Classify failure_type.
- Link source_trace_id and commit_sha.
- Add manual review path.

## Phase 3: Verification loop

- Bind fix_commit_sha.
- Require regression pass before verified status.
- Mark outdated cases as obsolete.

## Phase 4: Lesson memory

- Generate lesson candidates from verified cases.
- Require source_case_id and scope.
- Store active lessons in YAML or DB.

## Phase 5: Memory retrieval and gating

- Retrieve by metadata filter first.
- Optionally add keyword/vector search.
- Run deterministic System Gate.
- Run LLM Gate for semantic applicability.
- Log decisions.

## Phase 6: CI / PR integration

- On PR, show related historical failures.
- Suggest regression tests.
- Warn when prompt/tool schema changes touch known failure areas.

## Phase 7: Metrics

Track:

- memory candidate count
- allowed vs blocked memory
- pass rate with/without memory
- failures caused by wrong memory
- obsolete memory usage attempts
- lesson confidence over time
