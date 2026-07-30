import { DurableAgentHTTPError } from "./durable-errors.js";
import {
  DURABLE_AGENT_PROTOCOL_VERSION,
  DURABLE_HTTP_PROFILE,
  type DurableAgentHTTPResponse,
  type DurableCapabilities,
  type DurableDataClassification,
  type DurableErrorCategory,
  type DurableGateSession,
  type DurableGateSessionStatus,
  type DurableHealth,
  type DurableRequestByOperation,
  type DurableResultByOperation,
  type DurableSDKOperation,
  type DurableSessionReference,
  type DurableStorageMode,
  type DurableWireOperation,
} from "./durable-types.js";
import type { JsonObject } from "./types.js";

const ERROR_CODE = /^TBM_[A-Z0-9_]{1,120}$/;
const DIGEST = /^sha256:[0-9a-f]{64}$/;
const REVISION_ID = /^memory_revision_sha256_[0-9a-f]{64}$/;
const IDENTIFIER_MAX_CHARS = 128;
const METADATA_MAX_CHARS = 512;
const REASON_MAX_CHARS = 2_000;
const MAX_DECISIONS = 1_000;
const MAX_ATTRIBUTES = 16;
const MAX_SEMANTIC_DIMENSIONS = 4_096;
const MAX_QUERY_BYTES = 64 * 1024;
const MAX_PROMPT_BYTES = 128_000;
const MAX_RESPONSE_BYTES = 64 * 1024;
const MAX_SESSION_TTL_SECONDS = 31_536_000;
const MAX_SESSION_LEASE_SECONDS = 86_400;
const MAX_OUTCOME_ARTIFACTS = 64;
const MAX_OUTCOME_VALUE = 2_147_483_647;
const MAX_REPLAY_CONTENT_BYTES = 8 * 1024 * 1024;
const TASK_MODES = new Set([
  "planning",
  "repair",
  "debug",
  "eval",
  "production",
]);
const RETRIEVAL_MODES = new Set([
  "metadata",
  "lexical",
  "semantic",
  "evidence_graph",
  "hybrid",
]);
const RISKS = new Set(["low", "medium", "high", "unknown"]);
const INJECTIONS = new Set(["none", "summary", "full"]);
const RUN_RESULTS = new Set(["pass", "fail", "error"]);
const SUPPORTED_ATTRIBUTES = new Set([
  "branch",
  "prompt_version",
  "prompt_family",
  "tool",
  "tool_schema_version",
  "model",
  "model_family",
  "eval_suite",
  "task_type",
  "failure_type",
]);
const STORAGE_MODES = new Set<DurableStorageMode>(["sqlite", "postgres"]);
const STATUSES = new Set<DurableGateSessionStatus>([
  "created",
  "prepared",
  "awaiting_decision",
  "decided",
  "finalized",
  "executing",
  "completed",
  "canceled",
  "expired",
  "abandoned",
]);
const OPERATIONS = new Set<DurableWireOperation>([
  "prepare",
  "decide",
  "finalize",
  "start",
  "resume",
  "abandon",
  "complete",
  "cancel",
  "get_session",
  "export_replay",
]);
const SDK_OPERATIONS = new Set<DurableSDKOperation>([
  "capabilities",
  "openapi",
  "health",
  ...OPERATIONS,
]);
const CATEGORIES = new Set<DurableErrorCategory>([
  "input",
  "authentication",
  "authorization",
  "state",
  "not_found",
  "persistence",
  "provider",
  "evaluator",
  "recovery",
  "internal",
]);
const CLASSIFICATIONS = new Set<DurableDataClassification>([
  "public",
  "internal",
  "confidential",
  "restricted",
]);

const REQUEST_KEYS: {
  readonly [O in DurableWireOperation]: {
    readonly required: readonly string[];
    readonly optional: readonly string[];
  };
} = {
  prepare: {
    required: [
      "request_id",
      "trace_id",
      "run_id",
      "task_mode",
      "commit_sha",
      "retrieval_mode",
      "retriever_id",
      "retriever_version",
      "top_k",
      "idempotency_key",
      "expires_in_seconds",
      "lease_seconds",
    ],
    optional: [
      "attributes",
      "evaluation_suite",
      "evaluation_case_id",
      "query_base64",
      "semantic_query",
    ],
  },
  decide: {
    required: [
      "session_id",
      "expected_session_version",
      "prompt_base64",
      "response_base64",
      "provider_request_id",
      "decision_id",
      "reason",
      "risk",
      "recommended_injection",
    ],
    optional: [
      "expected_previous_attempt_id",
      "lease_seconds",
      "final_allowed_revision_ids",
      "final_blocked_revision_ids",
      "input_tokens",
      "output_tokens",
    ],
  },
  finalize: {
    required: ["session_id", "expected_session_version"],
    optional: ["lease_seconds"],
  },
  start: {
    required: ["session_id", "expected_session_version"],
    optional: [],
  },
  resume: {
    required: ["session_id", "expected_session_version"],
    optional: ["lease_seconds"],
  },
  abandon: {
    required: ["session_id", "expected_session_version", "reason"],
    optional: [],
  },
  complete: {
    required: [
      "session_id",
      "expected_session_version",
      "result",
      "evidence_artifact_sha256s",
    ],
    optional: [
      "output_sha256",
      "tool_outputs_sha256",
      "latency_ms",
      "cost_usd",
      "error_code",
    ],
  },
  cancel: {
    required: ["session_id", "expected_session_version", "reason"],
    optional: [],
  },
  get_session: {
    required: ["session_id"],
    optional: [],
  },
  export_replay: {
    required: [
      "session_id",
      "expected_session_version",
      "allowed_classifications",
    ],
    optional: ["max_content_bytes"],
  },
};

const RESULT_KEYS: {
  readonly [O in DurableWireOperation]: readonly string[];
} = {
  prepare: [
    "authorization_event_id",
    "session",
    "retrieval_snapshot",
    "system_gate_evaluation",
    "retrieval_policy",
  ],
  decide: [
    "session",
    "attempt",
    "prompt_artifact",
    "response_artifact",
    "replayed",
  ],
  finalize: [
    "session",
    "usage_decision",
    "injection",
    "manifest",
    "snippet",
    "content_exposed",
    "replayed",
  ],
  start: [
    "session",
    "usage_decision",
    "injection",
    "manifest",
    "transition_authorization_event_id",
    "snippet",
    "content_exposed",
    "execution_required",
    "replayed",
  ],
  resume: [
    "session",
    "usage_decision",
    "injection",
    "manifest",
    "transition_authorization_event_id",
    "snippet",
    "content_exposed",
    "execution_required",
    "replayed",
  ],
  abandon: [
    "session",
    "transition_authorization_event_id",
    "replayed",
  ],
  complete: [
    "session",
    "outcome",
    "outbox_event",
    "outbox_delivery",
    "transition_authorization_event_id",
    "inserted",
    "event_inserted",
    "replayed",
  ],
  cancel: [
    "session",
    "transition_authorization_event_id",
    "replayed",
  ],
  get_session: ["session"],
  export_replay: [
    "session",
    "bundle",
    "read_authorization_event_id",
    "retrieval_authorization_event_id",
    "content_exposed",
  ],
};

function invalid(
  operation: DurableSDKOperation,
  message: string,
): never {
  throw new DurableAgentHTTPError(
    "TBM_DURABLE_SDK_RESPONSE_INVALID",
    "internal",
    operation,
    message,
  );
}

function input(operation: DurableWireOperation, message: string): never {
  throw new DurableAgentHTTPError(
    "TBM_DURABLE_SDK_INVALID_INPUT",
    "input",
    operation,
    message,
  );
}

function record(
  value: unknown,
  operation: DurableSDKOperation,
): Record<string, unknown> {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    ![Object.prototype, null].includes(Object.getPrototypeOf(value))
  ) {
    invalid(operation, "durable HTTP response must be a JSON object");
  }
  return value as Record<string, unknown>;
}

function exact(
  value: Record<string, unknown>,
  keys: readonly string[],
  operation: DurableSDKOperation,
): void {
  const actual = Object.keys(value);
  if (
    actual.length !== keys.length ||
    actual.some((key) => !keys.includes(key))
  ) {
    invalid(operation, "durable HTTP response fields are invalid");
  }
}

function nonblank(
  value: unknown,
  operation: DurableSDKOperation,
): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.trim() !== value
  ) {
    invalid(operation, "durable HTTP response string is invalid");
  }
  return value;
}

function positiveInteger(
  value: unknown,
  operation: DurableSDKOperation,
): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 1
  ) {
    invalid(operation, "durable HTTP response integer is invalid");
  }
  return value;
}

function protocol(
  value: Record<string, unknown>,
  operation: DurableSDKOperation,
): void {
  if (value.protocol_version !== DURABLE_AGENT_PROTOCOL_VERSION) {
    invalid(operation, "durable protocol version is invalid");
  }
}

function validateSession(
  value: unknown,
  operation: DurableSDKOperation,
): DurableGateSession {
  const session = record(value, operation);
  const status = nonblank(session.status, operation) as DurableGateSessionStatus;
  if (!STATUSES.has(status)) {
    invalid(operation, "durable GateSession status is invalid");
  }
  nonblank(session.session_id, operation);
  positiveInteger(session.version, operation);
  return session as DurableGateSession;
}

function requestRecord(
  value: unknown,
  operation: DurableWireOperation,
): Record<string, unknown> {
  if (
    value === null ||
    typeof value !== "object" ||
    Array.isArray(value) ||
    ![Object.prototype, null].includes(Object.getPrototypeOf(value))
  ) {
    input(operation, "request must be a plain JSON object");
  }
  return value as Record<string, unknown>;
}

function requestString(
  value: unknown,
  operation: DurableWireOperation,
  maxChars = IDENTIFIER_MAX_CHARS,
): string {
  if (
    typeof value !== "string" ||
    value.length === 0 ||
    value.trim() !== value ||
    Array.from(value).length > maxChars
  ) {
    input(operation, "request contains an invalid string");
  }
  return value;
}

function requestPositiveInteger(
  value: unknown,
  operation: DurableWireOperation,
  maximum = Number.MAX_SAFE_INTEGER,
): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 1 ||
    value > maximum
  ) {
    input(operation, "request contains an invalid positive integer");
  }
  return value;
}

function canonicalBase64(
  value: unknown,
  operation: DurableWireOperation,
  maximumBytes: number,
  allowEmpty = false,
): string {
  if (typeof value !== "string") {
    input(operation, "request contains non-canonical base64");
  }
  const source = value;
  if (source.length === 0 && allowEmpty) {
    return source;
  }
  if (
    source.length % 4 !== 0 ||
    !/^[A-Za-z0-9+/]+={0,2}$/.test(source) ||
    Buffer.from(source, "base64").toString("base64") !== source ||
    Buffer.from(source, "base64").byteLength > maximumBytes
  ) {
    input(operation, "request contains non-canonical base64");
  }
  return source;
}

function sortedUnique(
  value: unknown,
  operation: DurableWireOperation,
  pattern: RegExp,
  maximum: number,
): readonly string[] {
  if (
    !Array.isArray(value) ||
    value.length > maximum ||
    value.some((item) => typeof item !== "string" || !pattern.test(item))
  ) {
    input(operation, "request contains an invalid canonical array");
  }
  const items = value as string[];
  if (
    new Set(items).size !== items.length ||
    items.some((item, index) => index > 0 && items[index - 1]! > item)
  ) {
    input(operation, "request canonical array must be sorted and unique");
  }
  return items;
}

function optionalBoundedInteger(
  value: unknown,
  operation: DurableWireOperation,
  minimum: number,
  maximum: number,
): void {
  if (
    value !== undefined &&
    value !== null &&
    (typeof value !== "number" ||
      !Number.isSafeInteger(value) ||
      value < minimum ||
      value > maximum)
  ) {
    input(operation, "request contains an invalid bounded integer");
  }
}

function optionalDigest(
  value: unknown,
  operation: DurableWireOperation,
): void {
  if (
    value !== undefined &&
    value !== null &&
    (typeof value !== "string" || !DIGEST.test(value))
  ) {
    input(operation, "request contains an invalid digest");
  }
}

export function assertDurableRequest<O extends DurableWireOperation>(
  operation: O,
  value: unknown,
): asserts value is DurableRequestByOperation[O] {
  const payload = requestRecord(value, operation);
  const shape = REQUEST_KEYS[operation];
  const allowed = [...shape.required, ...shape.optional];
  const keys = Object.keys(payload);
  if (
    shape.required.some((key) => !keys.includes(key)) ||
    keys.some((key) => !allowed.includes(key))
  ) {
    input(
      operation,
      "request fields are invalid; identity fields are never accepted",
    );
  }
  if ("session_id" in payload) {
    requestString(payload.session_id, operation, IDENTIFIER_MAX_CHARS);
  }
  if ("expected_session_version" in payload) {
    requestPositiveInteger(payload.expected_session_version, operation);
  }
  if (operation === "prepare") {
    for (const key of [
      "request_id",
      "trace_id",
      "run_id",
      "commit_sha",
      "retriever_id",
      "retriever_version",
      "idempotency_key",
    ]) {
      requestString(
        payload[key],
        operation,
        key === "commit_sha" ? METADATA_MAX_CHARS : IDENTIFIER_MAX_CHARS,
      );
    }
    for (const key of [
      "top_k",
      "expires_in_seconds",
      "lease_seconds",
    ]) {
      requestPositiveInteger(
        payload[key],
        operation,
        key === "top_k"
          ? 100
          : key === "expires_in_seconds"
            ? MAX_SESSION_TTL_SECONDS
            : MAX_SESSION_LEASE_SECONDS,
      );
    }
    if (
      !TASK_MODES.has(String(payload.task_mode)) ||
      !RETRIEVAL_MODES.has(String(payload.retrieval_mode))
    ) {
      input(operation, "task or retrieval mode is invalid");
    }
    const attributes = requestRecord(
      payload.attributes === undefined ? {} : payload.attributes,
      operation,
    );
    if (
      Object.keys(attributes).length > MAX_ATTRIBUTES ||
      Object.entries(attributes).some(
        ([key, item]) =>
          !SUPPORTED_ATTRIBUTES.has(key) ||
          typeof item !== "string" ||
          item.length === 0 ||
          item.trim() !== item ||
          Array.from(item).length > METADATA_MAX_CHARS,
      )
    ) {
      input(operation, "attributes contain an unsupported or invalid item");
    }
    if (
      (payload.evaluation_suite === null ||
        payload.evaluation_suite === undefined) !==
      (payload.evaluation_case_id === null ||
        payload.evaluation_case_id === undefined)
    ) {
      input(operation, "evaluation suite and case must be paired");
    }
    for (const key of ["evaluation_suite", "evaluation_case_id"]) {
      if (payload[key] !== undefined && payload[key] !== null) {
        requestString(payload[key], operation, 256);
      }
    }
    if (payload.query_base64 !== undefined && payload.query_base64 !== null) {
      canonicalBase64(
        payload.query_base64,
        operation,
        MAX_QUERY_BYTES,
        true,
      );
    }
    if (payload.semantic_query !== undefined && payload.semantic_query !== null) {
      const semantic = requestRecord(payload.semantic_query, operation);
      const semanticKeys = Object.keys(semantic);
      if (
        semanticKeys.length !== 3 ||
        !["provider_id", "provider_version", "vector"].every((key) =>
          semanticKeys.includes(key),
        )
      ) {
        input(operation, "semantic query fields are invalid");
      }
      requestString(semantic.provider_id, operation, IDENTIFIER_MAX_CHARS);
      requestString(semantic.provider_version, operation, IDENTIFIER_MAX_CHARS);
      if (
        !Array.isArray(semantic.vector) ||
        semantic.vector.length === 0 ||
        semantic.vector.length > MAX_SEMANTIC_DIMENSIONS ||
        semantic.vector.some(
          (item) => typeof item !== "number" || !Number.isFinite(item),
        ) ||
        !semantic.vector.some((item) => item !== 0)
      ) {
        input(operation, "semantic vector must be finite and non-zero");
      }
    }
  } else if (operation === "decide") {
    canonicalBase64(payload.prompt_base64, operation, MAX_PROMPT_BYTES);
    canonicalBase64(payload.response_base64, operation, MAX_RESPONSE_BYTES);
    requestString(
      payload.provider_request_id,
      operation,
      IDENTIFIER_MAX_CHARS,
    );
    requestString(payload.decision_id, operation, IDENTIFIER_MAX_CHARS);
    requestString(payload.reason, operation, REASON_MAX_CHARS);
    if (
      !RISKS.has(String(payload.risk)) ||
      !INJECTIONS.has(String(payload.recommended_injection))
    ) {
      input(operation, "semantic decision fields are invalid");
    }
    if (
      payload.expected_previous_attempt_id !== undefined &&
      payload.expected_previous_attempt_id !== null
    ) {
      requestString(
        payload.expected_previous_attempt_id,
        operation,
        IDENTIFIER_MAX_CHARS,
      );
    }
    if (payload.lease_seconds !== undefined) {
      requestPositiveInteger(
        payload.lease_seconds,
        operation,
        MAX_SESSION_LEASE_SECONDS,
      );
    }
    optionalBoundedInteger(
      payload.input_tokens,
      operation,
      0,
      MAX_OUTCOME_VALUE,
    );
    optionalBoundedInteger(
      payload.output_tokens,
      operation,
      0,
      MAX_OUTCOME_VALUE,
    );
    const allowedRevisions = sortedUnique(
      payload.final_allowed_revision_ids === undefined
        ? []
        : payload.final_allowed_revision_ids,
      operation,
      REVISION_ID,
      MAX_DECISIONS,
    );
    const blockedRevisions = sortedUnique(
      payload.final_blocked_revision_ids === undefined
        ? []
        : payload.final_blocked_revision_ids,
      operation,
      REVISION_ID,
      MAX_DECISIONS,
    );
    if (
      allowedRevisions.some((item) => blockedRevisions.includes(item))
    ) {
      input(operation, "allowed and blocked revisions overlap");
    }
  } else if (operation === "finalize" || operation === "resume") {
    if (payload.lease_seconds !== undefined) {
      requestPositiveInteger(
        payload.lease_seconds,
        operation,
        MAX_SESSION_LEASE_SECONDS,
      );
    }
  } else if (operation === "abandon" || operation === "cancel") {
    requestString(payload.reason, operation, 512);
  } else if (operation === "complete") {
    const artifacts = sortedUnique(
      payload.evidence_artifact_sha256s,
      operation,
      DIGEST,
      MAX_OUTCOME_ARTIFACTS,
    );
    if (artifacts.length === 0) {
      input(operation, "completion evidence is required");
    }
    const output = payload.output_sha256;
    const tools = payload.tool_outputs_sha256;
    optionalDigest(output, operation);
    optionalDigest(tools, operation);
    if (
      (typeof output !== "string" || !DIGEST.test(output)) &&
      (typeof tools !== "string" || !DIGEST.test(tools))
    ) {
      input(operation, "completion output evidence is required");
    }
    const result = payload.result;
    if (
      !RUN_RESULTS.has(String(result)) ||
      (result === "error") !==
        (typeof payload.error_code === "string" &&
          payload.error_code.length > 0)
    ) {
      input(operation, "completion result and error_code are inconsistent");
    }
    optionalBoundedInteger(
      payload.latency_ms,
      operation,
      0,
      MAX_OUTCOME_VALUE,
    );
    if (
      payload.cost_usd !== undefined &&
      payload.cost_usd !== null &&
      (typeof payload.cost_usd !== "number" ||
        !Number.isFinite(payload.cost_usd) ||
        payload.cost_usd < 0)
    ) {
      input(operation, "completion cost is invalid");
    }
    if (payload.error_code !== undefined && payload.error_code !== null) {
      requestString(payload.error_code, operation, IDENTIFIER_MAX_CHARS);
    }
  } else if (operation === "export_replay") {
    const classifications = payload.allowed_classifications;
    if (
      !Array.isArray(classifications) ||
      classifications.length === 0 ||
      classifications.length > 4 ||
      classifications.some(
        (item) =>
          typeof item !== "string" ||
          !CLASSIFICATIONS.has(item as DurableDataClassification),
      ) ||
      new Set(classifications).size !== classifications.length
    ) {
      input(operation, "replay classifications are invalid");
    }
    if (payload.max_content_bytes !== undefined) {
      requestPositiveInteger(
        payload.max_content_bytes,
        operation,
        MAX_REPLAY_CONTENT_BYTES,
      );
    }
  }
}

export function parseDurableCapabilities(value: unknown): DurableCapabilities {
  const operation: DurableSDKOperation = "capabilities";
  const payload = record(value, operation);
  exact(
    payload,
    [
      "protocol_version",
      "transport_profile",
      "durable_agent_contract_version",
      "storage_mode",
      "operations",
      "gate_session_statuses",
      "identity_source",
      "transport_authentication",
      "caller_identity_fields",
      "durable_sessions",
      "process_local_records",
      "injection_content_exposed",
      "replay_content_exposed",
      "limits",
    ],
    operation,
  );
  protocol(payload, operation);
  if (
    payload.transport_profile !== DURABLE_HTTP_PROFILE ||
    payload.durable_agent_contract_version !== "tbm.durable-agent.v3" ||
    !STORAGE_MODES.has(payload.storage_mode as DurableStorageMode) ||
    !Array.isArray(payload.operations) ||
    payload.operations.some(
      (item) =>
        typeof item !== "string" ||
        !OPERATIONS.has(item as DurableWireOperation),
    ) ||
    !Array.isArray(payload.gate_session_statuses) ||
    payload.gate_session_statuses.some(
      (item) =>
        typeof item !== "string" ||
        !STATUSES.has(item as DurableGateSessionStatus),
    ) ||
    payload.identity_source !== "trusted_adapter" ||
    payload.transport_authentication !== "required" ||
    payload.caller_identity_fields !== false ||
    payload.durable_sessions !== true ||
    !Array.isArray(payload.process_local_records) ||
    payload.process_local_records.length !== 0 ||
    typeof payload.injection_content_exposed !== "boolean" ||
    typeof payload.replay_content_exposed !== "boolean"
  ) {
    invalid(operation, "durable capabilities are invalid");
  }
  record(payload.limits, operation);
  return payload as unknown as DurableCapabilities;
}

export function parseDurableHealth(value: unknown): DurableHealth {
  const operation: DurableSDKOperation = "health";
  const payload = record(value, operation);
  exact(
    payload,
    [
      "protocol_version",
      "status",
      "storage_mode",
      "durable_sessions",
      "process_local_records",
    ],
    operation,
  );
  protocol(payload, operation);
  if (
    payload.status !== "ok" ||
    !STORAGE_MODES.has(payload.storage_mode as DurableStorageMode) ||
    payload.durable_sessions !== true ||
    !Array.isArray(payload.process_local_records) ||
    payload.process_local_records.length !== 0
  ) {
    invalid(operation, "durable health response is invalid");
  }
  return payload as unknown as DurableHealth;
}

export function parseDurableOpenAPI(value: unknown): JsonObject {
  const operation: DurableSDKOperation = "openapi";
  const payload = record(value, operation);
  if (payload.openapi !== "3.1.0") {
    invalid(operation, "durable OpenAPI response is invalid");
  }
  record(payload.paths, operation);
  record(payload.components, operation);
  return payload as JsonObject;
}

export function parseDurableOperationResponse<
  O extends DurableWireOperation,
>(value: unknown, operation: O): DurableAgentHTTPResponse<O> {
  const payload = record(value, operation);
  exact(payload, ["protocol_version", "operation", "result"], operation);
  protocol(payload, operation);
  if (payload.operation !== operation) {
    invalid(operation, "durable response operation is invalid");
  }
  const result = record(payload.result, operation);
  exact(result, RESULT_KEYS[operation], operation);
  validateSession(result.session, operation);
  for (const key of ["replayed", "content_exposed", "execution_required"]) {
    if (key in result && typeof result[key] !== "boolean") {
      invalid(operation, "durable response boolean is invalid");
    }
  }
  if (
    "snippet" in result &&
    result.snippet !== null &&
    typeof result.snippet !== "string"
  ) {
    invalid(operation, "durable response snippet is invalid");
  }
  return {
    protocol_version: DURABLE_AGENT_PROTOCOL_VERSION,
    operation,
    result: result as unknown as DurableResultByOperation[O],
  };
}

export function parseDurablePublicError(
  value: unknown,
  requestedOperation: DurableSDKOperation,
  status: number,
): DurableAgentHTTPError {
  const payload = record(value, requestedOperation);
  exact(payload, ["protocol_version", "error"], requestedOperation);
  protocol(payload, requestedOperation);
  const detail = record(payload.error, requestedOperation);
  exact(
    detail,
    ["code", "category", "message", "operation", "retryable"],
    requestedOperation,
  );
  const code = nonblank(detail.code, requestedOperation);
  const category = nonblank(
    detail.category,
    requestedOperation,
  ) as DurableErrorCategory;
  const operation = nonblank(
    detail.operation,
    requestedOperation,
  ) as DurableSDKOperation;
  const message = nonblank(detail.message, requestedOperation);
  if (
    !ERROR_CODE.test(code) ||
    !CATEGORIES.has(category) ||
    !SDK_OPERATIONS.has(operation) ||
    operation !== requestedOperation ||
    typeof detail.retryable !== "boolean" ||
    Array.from(message).length > 2_000
  ) {
    invalid(requestedOperation, "durable public error is invalid");
  }
  return new DurableAgentHTTPError(code, category, operation, message, {
    retryable: detail.retryable,
    status,
  });
}

export function durableSessionReference(
  response: DurableAgentHTTPResponse,
): DurableSessionReference {
  const session = validateSession(
    response.result.session,
    response.operation,
  );
  return {
    session_id: session.session_id,
    expected_session_version: session.version,
  };
}
