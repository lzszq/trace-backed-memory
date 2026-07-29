import { AgentMemoryError } from "./errors.js";
import {
  AGENT_PROTOCOL_VERSION,
  type AgentCapabilities,
  type AgentCanceledRun,
  type AgentCompletedRun,
  type AgentErrorCategory,
  type AgentEvalResult,
  type AgentFinalizedMemory,
  type AgentHealth,
  type AgentInjection,
  type AgentMode,
  type AgentOperation,
  type AgentPreparedMemory,
  type AgentRisk,
  type AgentStorageMode,
} from "./types.js";

const IDENTIFIER_MAX_CHARS = 128;
const ERROR_MESSAGE_MAX_CHARS = 2_048;
const ERROR_CODE = /^TBM_[A-Z0-9_]+$/;
const STORAGE_MODES = new Set<AgentStorageMode>([
  "memory",
  "sqlite",
  "postgres",
]);
const MODES = new Set<AgentMode>([
  "debug",
  "repair",
  "regression",
  "planning",
  "eval",
  "production",
]);
const OPERATIONS = new Set<AgentOperation>([
  "open",
  "capture",
  "prepare",
  "finalize",
  "complete",
  "cancel",
  "flush",
  "health",
  "run",
  "close",
]);
const CAPABILITY_OPERATIONS = new Set<AgentOperation>([
  "capture",
  "prepare",
  "finalize",
  "complete",
  "cancel",
  "run",
  "flush",
  "health",
]);
const CATEGORIES = new Set<AgentErrorCategory>([
  "input",
  "state",
  "persistence",
  "callback",
  "closed",
  "internal",
]);
const RISKS = new Set<AgentRisk>(["none", "low", "medium", "high"]);
const INJECTIONS = new Set<AgentInjection>([
  "none",
  "short_summary",
  "full_case_summary",
  "pointer_only",
]);
const EVAL_RESULTS = new Set<AgentEvalResult>(["pass", "fail", "error"]);
const CAPABILITY_LIMITS = [
  "gate_candidates",
  "gate_prompt_chars",
  "gate_response_bytes",
  "gate_response_nodes",
  "gate_response_depth",
  "decision_reason_chars",
  "injection_memories",
  "injection_chars",
  "prepared_request_candidates",
  "pending_requests",
  "pending_candidate_references",
  "finalized_request_replays",
] as const;

function invalid(operation: AgentOperation, message: string): never {
  throw new AgentMemoryError(
    "TBM_SDK_RESPONSE_INVALID",
    "callback",
    operation,
    message,
  );
}

function record(value: unknown, operation: AgentOperation): Record<string, unknown> {
  if (value === null || typeof value !== "object" || Array.isArray(value)) {
    invalid(operation, "response must be a JSON object");
  }
  return value as Record<string, unknown>;
}

function exact(
  value: Record<string, unknown>,
  keys: readonly string[],
  operation: AgentOperation,
): void {
  const actual = Object.keys(value).sort();
  const expected = [...keys].sort();
  if (
    actual.length !== expected.length ||
    actual.some((item, index) => item !== expected[index])
  ) {
    invalid(operation, "response fields are invalid");
  }
}

function protocol(
  value: Record<string, unknown>,
  operation: AgentOperation,
): void {
  if (value.protocol_version !== AGENT_PROTOCOL_VERSION) {
    invalid(operation, "protocol_version is invalid");
  }
}

function string(
  value: unknown,
  operation: AgentOperation,
  options: {
    readonly allowEmpty?: boolean;
    readonly maxChars?: number;
  } = {},
): string {
  if (
    typeof value !== "string" ||
    Array.from(value).length > (options.maxChars ?? 8_388_608) ||
    (!(options.allowEmpty ?? false) && value.trim().length === 0)
  ) {
    invalid(operation, "response string is invalid");
  }
  return value;
}

function identifier(value: unknown, operation: AgentOperation): string {
  return string(value, operation, { maxChars: IDENTIFIER_MAX_CHARS });
}

function integer(
  value: unknown,
  operation: AgentOperation,
  minimum = 0,
): number {
  if (!Number.isSafeInteger(value) || (value as number) < minimum) {
    invalid(operation, "response integer is invalid");
  }
  return value as number;
}

function rate(
  value: unknown,
  operation: AgentOperation,
  allowNull = false,
): number | null {
  if (allowNull && value === null) {
    return null;
  }
  if (
    typeof value !== "number" ||
    !Number.isFinite(value) ||
    value < 0 ||
    value > 1
  ) {
    invalid(operation, "response rate is invalid");
  }
  return value;
}

function stringArray<T extends string>(
  value: unknown,
  operation: AgentOperation,
  options: {
    readonly maxItems?: number;
    readonly maxChars?: number;
    readonly allowed?: ReadonlySet<T>;
  } = {},
): T[] {
  if (
    !Array.isArray(value) ||
    value.length > (options.maxItems ?? 100_000)
  ) {
    invalid(operation, "response array is invalid");
  }
  const output: T[] = [];
  const seen = new Set<string>();
  for (const item of value) {
    const parsed = string(item, operation, {
      maxChars: options.maxChars ?? IDENTIFIER_MAX_CHARS,
    });
    if (
      seen.has(parsed) ||
      (options.allowed !== undefined && !options.allowed.has(parsed as T))
    ) {
      invalid(operation, "response array is invalid");
    }
    seen.add(parsed);
    output.push(parsed as T);
  }
  return output;
}

export function parseCapabilities(value: unknown): AgentCapabilities {
  const operation: AgentOperation = "health";
  const payload = record(value, operation);
  exact(
    payload,
    [
      "protocol_version",
      "snapshot_version",
      "sqlite_schema_version",
      "postgres_schema_version",
      "storage_modes",
      "operations",
      "modes",
      "limits",
      "durable_records",
      "process_local_records",
    ],
    operation,
  );
  protocol(payload, operation);
  const limits = record(payload.limits, operation);
  exact(limits, CAPABILITY_LIMITS, operation);
  for (const item of Object.values(limits)) {
    integer(item, operation);
  }
  return {
    protocol_version: AGENT_PROTOCOL_VERSION,
    snapshot_version: integer(payload.snapshot_version, operation, 1),
    sqlite_schema_version: integer(
      payload.sqlite_schema_version,
      operation,
      1,
    ),
    postgres_schema_version: integer(
      payload.postgres_schema_version,
      operation,
      1,
    ),
    storage_modes: stringArray(payload.storage_modes, operation, {
      allowed: STORAGE_MODES,
    }),
    operations: stringArray(payload.operations, operation, {
      allowed: CAPABILITY_OPERATIONS,
    }) as AgentCapabilities["operations"],
    modes: stringArray(payload.modes, operation, { allowed: MODES }),
    limits: limits as Readonly<Record<string, number>>,
    durable_records: stringArray(payload.durable_records, operation, {
      maxChars: 2_048,
    }),
    process_local_records: stringArray(payload.process_local_records, operation, {
      maxChars: 2_048,
    }),
  };
}

export function parsePrepared(value: unknown): AgentPreparedMemory {
  const operation: AgentOperation = "prepare";
  const payload = record(value, operation);
  exact(
    payload,
    [
      "protocol_version",
      "request_id",
      "trace_id",
      "run_id",
      "candidate_memory_ids",
      "system_allowed_memory_ids",
      "system_blocked",
      "prompt",
    ],
    operation,
  );
  protocol(payload, operation);
  const blocked = record(payload.system_blocked, operation);
  if (Object.keys(blocked).length > 1_000) {
    invalid(operation, "system_blocked is invalid");
  }
  const parsedBlocked: Record<string, string> = Object.create(null);
  for (const [key, item] of Object.entries(blocked)) {
    parsedBlocked[identifier(key, operation)] = string(item, operation, {
      maxChars: 2_000,
    });
  }
  return {
    protocol_version: AGENT_PROTOCOL_VERSION,
    request_id: identifier(payload.request_id, operation),
    trace_id: identifier(payload.trace_id, operation),
    run_id: identifier(payload.run_id, operation),
    candidate_memory_ids: stringArray(payload.candidate_memory_ids, operation, {
      maxItems: 1_000,
    }),
    system_allowed_memory_ids: stringArray(
      payload.system_allowed_memory_ids,
      operation,
      { maxItems: 50 },
    ),
    system_blocked: parsedBlocked,
    prompt: string(payload.prompt, operation, {
      allowEmpty: true,
      maxChars: 32_000,
    }),
  };
}

export function parseFinalized(value: unknown): AgentFinalizedMemory {
  const operation: AgentOperation = "finalize";
  const payload = record(value, operation);
  exact(
    payload,
    [
      "protocol_version",
      "request_id",
      "trace_id",
      "decision_id",
      "use_memory",
      "allowed_memory_ids",
      "blocked_memory_ids",
      "reason",
      "risk",
      "recommended_injection",
      "snippet",
    ],
    operation,
  );
  protocol(payload, operation);
  if (typeof payload.use_memory !== "boolean") {
    invalid(operation, "use_memory is invalid");
  }
  const risk = string(payload.risk, operation) as AgentRisk;
  const injection = string(
    payload.recommended_injection,
    operation,
  ) as AgentInjection;
  if (!RISKS.has(risk) || !INJECTIONS.has(injection)) {
    invalid(operation, "decision enum is invalid");
  }
  return {
    protocol_version: AGENT_PROTOCOL_VERSION,
    request_id: identifier(payload.request_id, operation),
    trace_id: identifier(payload.trace_id, operation),
    decision_id: identifier(payload.decision_id, operation),
    use_memory: payload.use_memory,
    allowed_memory_ids: stringArray(payload.allowed_memory_ids, operation, {
      maxItems: 20,
    }),
    blocked_memory_ids: stringArray(payload.blocked_memory_ids, operation, {
      maxItems: 1_000,
    }),
    reason: string(payload.reason, operation, { maxChars: 2_000 }),
    risk,
    recommended_injection: injection,
    snippet: string(payload.snippet, operation, {
      allowEmpty: true,
      maxChars: 12_000,
    }),
  };
}

export function parseCompleted(value: unknown): AgentCompletedRun {
  const operation: AgentOperation = "complete";
  const payload = record(value, operation);
  exact(
    payload,
    [
      "protocol_version",
      "request_id",
      "trace_id",
      "run_id",
      "decision_id",
      "eval_result",
      "memory_caused_failure",
    ],
    operation,
  );
  protocol(payload, operation);
  if (typeof payload.memory_caused_failure !== "boolean") {
    invalid(operation, "memory_caused_failure is invalid");
  }
  const result = string(payload.eval_result, operation) as AgentEvalResult;
  if (!EVAL_RESULTS.has(result)) {
    invalid(operation, "eval_result is invalid");
  }
  return {
    protocol_version: AGENT_PROTOCOL_VERSION,
    request_id:
      payload.request_id === null
        ? null
        : identifier(payload.request_id, operation),
    trace_id: identifier(payload.trace_id, operation),
    run_id: identifier(payload.run_id, operation),
    decision_id: identifier(payload.decision_id, operation),
    eval_result: result,
    memory_caused_failure: payload.memory_caused_failure,
  };
}

export function parseCanceled(value: unknown): AgentCanceledRun {
  const operation: AgentOperation = "cancel";
  const payload = record(value, operation);
  exact(payload, ["protocol_version", "request_id", "canceled"], operation);
  protocol(payload, operation);
  if (payload.canceled !== true) {
    invalid(operation, "canceled is invalid");
  }
  return {
    protocol_version: AGENT_PROTOCOL_VERSION,
    request_id: identifier(payload.request_id, operation),
    canceled: true,
  };
}

export function parseHealth(value: unknown): AgentHealth {
  const operation: AgentOperation = "health";
  const payload = record(value, operation);
  exact(
    payload,
    [
      "protocol_version",
      "pending_request_count",
      "finalized_request_replay_count",
      "memory_metrics",
      "memory_run_metrics",
    ],
    operation,
  );
  protocol(payload, operation);
  const memory = record(payload.memory_metrics, operation);
  const memoryKeys = [
    "candidate_memory_count",
    "blocked_memory_count",
    "decision_count",
    "used_memory_count",
    "unevaluated_decision_count",
    "evaluated_with_memory_count",
    "evaluated_without_memory_count",
    "pass_rate_with_memory",
    "pass_rate_without_memory",
    "wrong_memory_failure_count",
    "obsolete_memory_usage_attempts",
    "average_lesson_confidence",
  ] as const;
  exact(memory, memoryKeys, operation);
  const runs = record(payload.memory_run_metrics, operation);
  const runKeys = [
    "decision_count",
    "complete_count",
    "pending_count",
    "decision_only_count",
    "trace_only_count",
    "conflict_count",
    "recoverable_count",
    "auto_recoverable_count",
    "attribution_required_count",
  ] as const;
  exact(runs, runKeys, operation);
  for (const key of memoryKeys) {
    if (
      !key.startsWith("pass_rate_") &&
      key !== "average_lesson_confidence"
    ) {
      integer(memory[key], operation);
    }
  }
  for (const key of runKeys) {
    integer(runs[key], operation);
  }
  rate(memory.pass_rate_with_memory, operation, true);
  rate(memory.pass_rate_without_memory, operation, true);
  rate(memory.average_lesson_confidence, operation);
  return {
    protocol_version: AGENT_PROTOCOL_VERSION,
    pending_request_count: integer(payload.pending_request_count, operation),
    finalized_request_replay_count: integer(
      payload.finalized_request_replay_count,
      operation,
    ),
    memory_metrics: memory as unknown as AgentHealth["memory_metrics"],
    memory_run_metrics: runs as unknown as AgentHealth["memory_run_metrics"],
  };
}

export function parsePublicError(
  value: unknown,
  requestedOperation: AgentOperation,
  status: number,
): AgentMemoryError {
  const payload = record(value, requestedOperation);
  exact(payload, ["protocol_version", "error"], requestedOperation);
  protocol(payload, requestedOperation);
  const detail = record(payload.error, requestedOperation);
  const keys = Object.keys(detail);
  const required = ["code", "category", "message", "operation", "retryable"];
  if (
    required.some((key) => !keys.includes(key)) ||
    keys.some(
      (key) =>
        !required.includes(key) &&
        key !== "request_id" &&
        key !== "decision_id",
    )
  ) {
    invalid(requestedOperation, "error fields are invalid");
  }
  const code = string(detail.code, requestedOperation);
  const category = string(
    detail.category,
    requestedOperation,
  ) as AgentErrorCategory;
  const operation = string(detail.operation, requestedOperation) as AgentOperation;
  if (
    !ERROR_CODE.test(code) ||
    !CATEGORIES.has(category) ||
    !OPERATIONS.has(operation) ||
    typeof detail.retryable !== "boolean"
  ) {
    invalid(requestedOperation, "error fields are invalid");
  }
  const options: {
    retryable: boolean;
    status: number;
    requestId?: string;
    decisionId?: string;
  } = {
    retryable: detail.retryable,
    status,
  };
  if ("request_id" in detail) {
    options.requestId = identifier(detail.request_id, requestedOperation);
  }
  if ("decision_id" in detail) {
    options.decisionId = identifier(detail.decision_id, requestedOperation);
  }
  return new AgentMemoryError(
    code,
    category,
    operation,
    string(detail.message, requestedOperation, {
      maxChars: ERROR_MESSAGE_MAX_CHARS,
    }),
    options,
  );
}
