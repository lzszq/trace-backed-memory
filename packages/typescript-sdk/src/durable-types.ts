import type { JsonObject, JsonValue } from "./types.js";

export const DURABLE_AGENT_PROTOCOL_VERSION =
  "tbm.durable-agent-wire.v1" as const;
export const DURABLE_HTTP_PROFILE = "durable-v3" as const;

export type DurableTaskMode =
  | "planning"
  | "repair"
  | "debug"
  | "eval"
  | "production";
export type DurableRetrievalMode =
  | "metadata"
  | "lexical"
  | "semantic"
  | "evidence_graph"
  | "hybrid";
export type DurableRisk = "low" | "medium" | "high" | "unknown";
export type DurableInjection = "none" | "summary" | "full";
export type DurableRunResult = "pass" | "fail" | "error";
export type DurableStorageMode = "sqlite" | "postgres";
export type DurableDataClassification =
  | "public"
  | "internal"
  | "confidential"
  | "restricted";
export type DurableGateSessionStatus =
  | "created"
  | "prepared"
  | "awaiting_decision"
  | "decided"
  | "finalized"
  | "executing"
  | "completed"
  | "canceled"
  | "expired"
  | "abandoned";
export type DurableWireOperation =
  | "prepare"
  | "decide"
  | "finalize"
  | "start"
  | "resume"
  | "abandon"
  | "complete"
  | "cancel"
  | "get_session"
  | "export_replay";
export type DurableSDKOperation =
  | "capabilities"
  | "openapi"
  | "health"
  | DurableWireOperation;
export type DurableErrorCategory =
  | "input"
  | "authentication"
  | "authorization"
  | "state"
  | "not_found"
  | "persistence"
  | "provider"
  | "evaluator"
  | "recovery"
  | "internal";

export interface DurableSemanticQueryInput {
  readonly provider_id: string;
  readonly provider_version: string;
  readonly vector: readonly number[];
}

export interface DurablePrepareRequest {
  readonly request_id: string;
  readonly trace_id: string;
  readonly run_id: string;
  readonly task_mode: DurableTaskMode;
  readonly commit_sha: string;
  readonly attributes?: Readonly<Record<string, string>>;
  readonly evaluation_suite?: string | null;
  readonly evaluation_case_id?: string | null;
  readonly retrieval_mode: DurableRetrievalMode;
  readonly retriever_id: string;
  readonly retriever_version: string;
  readonly top_k: number;
  readonly idempotency_key: string;
  readonly expires_in_seconds: number;
  readonly lease_seconds: number;
  readonly query_base64?: string | null;
  readonly semantic_query?: DurableSemanticQueryInput | null;
}

export interface DurableDecideRequest {
  readonly session_id: string;
  readonly expected_session_version: number;
  readonly prompt_base64: string;
  readonly expected_previous_attempt_id?: string | null;
  readonly lease_seconds?: number;
  readonly response_base64: string;
  readonly provider_request_id: string;
  readonly decision_id: string;
  readonly final_allowed_revision_ids?: readonly string[];
  readonly final_blocked_revision_ids?: readonly string[];
  readonly reason: string;
  readonly risk: DurableRisk;
  readonly recommended_injection: DurableInjection;
  readonly input_tokens?: number | null;
  readonly output_tokens?: number | null;
}

export interface DurableSessionRevisionRequest {
  readonly session_id: string;
  readonly expected_session_version: number;
}

export interface DurableFinalizeRequest
  extends DurableSessionRevisionRequest {
  readonly lease_seconds?: number;
}

export type DurableStartRequest = DurableSessionRevisionRequest;

export interface DurableResumeRequest
  extends DurableSessionRevisionRequest {
  readonly lease_seconds?: number;
}

export interface DurableAbandonRequest
  extends DurableSessionRevisionRequest {
  readonly reason: string;
}

export interface DurableCompleteRequest
  extends DurableSessionRevisionRequest {
  readonly result: DurableRunResult;
  readonly evidence_artifact_sha256s: readonly string[];
  readonly output_sha256?: string | null;
  readonly tool_outputs_sha256?: string | null;
  readonly latency_ms?: number | null;
  readonly cost_usd?: number | null;
  readonly error_code?: string | null;
}

export interface DurableCancelRequest
  extends DurableSessionRevisionRequest {
  readonly reason: string;
}

export interface DurableGetSessionRequest {
  readonly session_id: string;
}

export interface DurableReplayRequest
  extends DurableSessionRevisionRequest {
  readonly allowed_classifications: readonly DurableDataClassification[];
  readonly max_content_bytes?: number;
}

export interface DurableCapabilities {
  readonly protocol_version: typeof DURABLE_AGENT_PROTOCOL_VERSION;
  readonly transport_profile: typeof DURABLE_HTTP_PROFILE;
  readonly durable_agent_contract_version: "tbm.durable-agent.v3";
  readonly storage_mode: DurableStorageMode;
  readonly operations: readonly DurableWireOperation[];
  readonly gate_session_statuses: readonly DurableGateSessionStatus[];
  readonly identity_source: "trusted_adapter";
  readonly transport_authentication: "required";
  readonly caller_identity_fields: false;
  readonly durable_sessions: true;
  readonly process_local_records: readonly [];
  readonly injection_content_exposed: boolean;
  readonly replay_content_exposed: boolean;
  readonly limits: Readonly<Record<string, number>>;
}

export interface DurableHealth {
  readonly protocol_version: typeof DURABLE_AGENT_PROTOCOL_VERSION;
  readonly status: "ok";
  readonly storage_mode: DurableStorageMode;
  readonly durable_sessions: true;
  readonly process_local_records: readonly [];
}

export interface DurableGateSession extends JsonObject {
  readonly session_id: string;
  readonly version: number;
  readonly status: DurableGateSessionStatus;
}

export interface DurablePrepareResult extends JsonObject {
  readonly authorization_event_id: string;
  readonly session: DurableGateSession;
  readonly retrieval_snapshot: JsonObject;
  readonly system_gate_evaluation: JsonObject;
  readonly retrieval_policy: JsonObject;
}

export interface DurableDecideResult extends JsonObject {
  readonly session: DurableGateSession;
  readonly attempt: JsonObject;
  readonly prompt_artifact: JsonObject;
  readonly response_artifact: JsonObject;
  readonly replayed: boolean;
}

export interface DurableFinalizeResult extends JsonObject {
  readonly session: DurableGateSession;
  readonly usage_decision: JsonObject;
  readonly injection: JsonObject;
  readonly manifest: JsonObject;
  readonly snippet: string | null;
  readonly content_exposed: boolean;
  readonly replayed: boolean;
}

export interface DurableExecutionResult extends DurableFinalizeResult {
  readonly transition_authorization_event_id: string;
  readonly execution_required: boolean;
}

export interface DurableTerminalResult extends JsonObject {
  readonly session: DurableGateSession;
  readonly transition_authorization_event_id: string;
  readonly replayed: boolean;
}

export interface DurableCompleteResult extends DurableTerminalResult {
  readonly outcome: JsonObject;
  readonly outbox_event: JsonObject;
  readonly outbox_delivery: JsonObject;
  readonly inserted: boolean;
  readonly event_inserted: boolean;
}

export interface DurableGetSessionResult extends JsonObject {
  readonly session: DurableGateSession;
}

export interface DurableReplayResult extends JsonObject {
  readonly session: DurableGateSession;
  readonly bundle: JsonObject;
  readonly read_authorization_event_id: string;
  readonly retrieval_authorization_event_id: string;
  readonly content_exposed: boolean;
}

export interface DurableResultByOperation {
  readonly prepare: DurablePrepareResult;
  readonly decide: DurableDecideResult;
  readonly finalize: DurableFinalizeResult;
  readonly start: DurableExecutionResult;
  readonly resume: DurableExecutionResult;
  readonly abandon: DurableTerminalResult;
  readonly complete: DurableCompleteResult;
  readonly cancel: DurableTerminalResult;
  readonly get_session: DurableGetSessionResult;
  readonly export_replay: DurableReplayResult;
}

export interface DurableAgentHTTPResponse<
  O extends DurableWireOperation = DurableWireOperation,
> {
  readonly protocol_version: typeof DURABLE_AGENT_PROTOCOL_VERSION;
  readonly operation: O;
  readonly result: DurableResultByOperation[O];
}

export interface DurableRequestOptions {
  readonly signal?: AbortSignal;
  readonly maxAttempts?: number;
  readonly retryDelayMs?: number;
}

export interface DurableAgentHTTPClientOptions {
  readonly baseUrl: string;
  readonly token: string;
  readonly timeoutMs?: number;
  readonly maxAttempts?: number;
  readonly retryDelayMs?: number;
  readonly ca?: string | Buffer | readonly (string | Buffer)[];
  readonly servername?: string;
}

export interface DurableSessionReference {
  readonly session_id: string;
  readonly expected_session_version: number;
}

export type DurableRequestByOperation = {
  readonly prepare: DurablePrepareRequest;
  readonly decide: DurableDecideRequest;
  readonly finalize: DurableFinalizeRequest;
  readonly start: DurableStartRequest;
  readonly resume: DurableResumeRequest;
  readonly abandon: DurableAbandonRequest;
  readonly complete: DurableCompleteRequest;
  readonly cancel: DurableCancelRequest;
  readonly get_session: DurableGetSessionRequest;
  readonly export_replay: DurableReplayRequest;
};

export type DurableJSON = JsonValue;
