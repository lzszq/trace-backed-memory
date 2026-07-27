# Local agent protocol

Protocol version: `tbm.agent.v1`.

Discover capabilities:

```text
tbm capabilities
```

Common Python sequence:

```python
with LocalAgentMemory.open_sqlite(".tbm/memory.sqlite3") as memory:
    prepared = memory.prepare(trace, context, task=task)
    finalized = memory.finalize(prepared.request_id, decision)
    execute(finalized.snippet)
    completed = memory.complete(
        finalized.decision_id,
        MemoryRunMeasurement(eval_result="pass"),
    )
```

`AgentPreparedMemory` contains only the public request ID, Trace/run linkage,
bounded prompt, candidate IDs, System-Gate-allowed IDs, and deterministic block
reasons. `AgentFinalizedMemory` contains the audited decision and only the
bounded renderer output. `AgentCompletedRun` confirms measured Trace/decision
completion.

Stable failures use `AgentMemoryError.to_dict()`. Important codes include:

- `TBM_AGENT_REQUEST_NOT_FOUND`: request is absent from this process.
- `TBM_AGENT_INVALID_DECISION`: decision failed the strict contract.
- `TBM_AGENT_DECISION_CONFLICT`: same request received a different retry.
- `TBM_AGENT_PERSISTENCE_FAILED`: durable state is dirty and can be retried.
- `TBM_AGENT_DECISION_CALLBACK_FAILED`: resume with the returned request ID.
- `TBM_AGENT_EXECUTION_CALLBACK_FAILED`: complete the returned decision ID
  after externally determining the measured result.

SQLite and PostgreSQL persist Trace, catalog, usage, and completion records.
They do not persist the pending request token in the current schema.
