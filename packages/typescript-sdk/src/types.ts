export const AGENT_PROTOCOL_VERSION = "tbm.agent.v1" as const;

export type AgentMode =
  | "debug"
  | "repair"
  | "regression"
  | "planning"
  | "eval"
  | "production";

export type AgentRisk = "none" | "low" | "medium" | "high";
export type AgentInjection =
  | "none"
  | "short_summary"
  | "full_case_summary"
  | "pointer_only";
export type AgentEvalResult = "pass" | "fail" | "error";
export type AgentStorageMode = "memory" | "sqlite" | "postgres";
export type AgentOperation =
  | "open"
  | "capture"
  | "prepare"
  | "finalize"
  | "complete"
  | "cancel"
  | "flush"
  | "health"
  | "run"
  | "close";
export type AgentErrorCategory =
  | "input"
  | "state"
  | "persistence"
  | "callback"
  | "closed"
  | "internal";

export type JsonScalar = string | number | boolean | null;
export type JsonValue = JsonScalar | JsonObject | readonly JsonValue[];
export interface JsonObject {
  readonly [key: string]: JsonValue;
}

export interface AgentPrepareRequest {
  readonly task: string;
  readonly mode: AgentMode;
  readonly tool?: string | null;
  readonly run_id?: string | null;
  readonly trace_id?: string | null;
  readonly prompt_version?: string | null;
  readonly prompt_family?: string | null;
  readonly tool_schema_version?: string | null;
  readonly model?: string | null;
  readonly model_family?: string | null;
  readonly eval_suite?: string | null;
  readonly input_hash?: string | null;
  readonly task_type?: string | null;
  readonly failure_type?: string | null;
  readonly query?: string | null;
  readonly semantic_scores?: Readonly<Record<string, number>> | null;
  readonly max_candidates?: number | null;
  readonly minimum_score?: number | null;
  readonly context_summary?: string;
}

export interface AgentFinalizeRequest {
  readonly request_id: string;
  readonly use_memory: boolean;
  readonly allowed_memory_ids: readonly string[];
  readonly blocked_memory_ids: readonly string[];
  readonly reason: string;
  readonly risk: AgentRisk;
  readonly recommended_injection: AgentInjection;
}

export interface AgentCompleteRequest {
  readonly decision_id: string;
  readonly eval_result: AgentEvalResult;
  readonly memory_caused_failure?: boolean;
  readonly output_hash?: string | null;
  readonly tool_outputs?: readonly JsonValue[];
  readonly latency_ms?: number | null;
  readonly cost_usd?: number | null;
  readonly error?: string | null;
  readonly trace_uri?: string | null;
}

export interface AgentCancelRequest {
  readonly request_id: string;
}

export interface AgentCapabilities {
  readonly protocol_version: typeof AGENT_PROTOCOL_VERSION;
  readonly snapshot_version: number;
  readonly sqlite_schema_version: number;
  readonly postgres_schema_version: number;
  readonly storage_modes: readonly AgentStorageMode[];
  readonly operations: readonly Exclude<AgentOperation, "open" | "close">[];
  readonly modes: readonly AgentMode[];
  readonly limits: Readonly<Record<string, number>>;
  readonly durable_records: readonly string[];
  readonly process_local_records: readonly string[];
}

export interface AgentPreparedMemory {
  readonly protocol_version: typeof AGENT_PROTOCOL_VERSION;
  readonly request_id: string;
  readonly trace_id: string;
  readonly run_id: string;
  readonly candidate_memory_ids: readonly string[];
  readonly system_allowed_memory_ids: readonly string[];
  readonly system_blocked: Readonly<Record<string, string>>;
  readonly prompt: string;
}

export interface AgentFinalizedMemory {
  readonly protocol_version: typeof AGENT_PROTOCOL_VERSION;
  readonly request_id: string;
  readonly trace_id: string;
  readonly decision_id: string;
  readonly use_memory: boolean;
  readonly allowed_memory_ids: readonly string[];
  readonly blocked_memory_ids: readonly string[];
  readonly reason: string;
  readonly risk: AgentRisk;
  readonly recommended_injection: AgentInjection;
  readonly snippet: string;
}

export interface AgentCompletedRun {
  readonly protocol_version: typeof AGENT_PROTOCOL_VERSION;
  readonly request_id: string | null;
  readonly trace_id: string;
  readonly run_id: string;
  readonly decision_id: string;
  readonly eval_result: AgentEvalResult;
  readonly memory_caused_failure: boolean;
}

export interface AgentCanceledRun {
  readonly protocol_version: typeof AGENT_PROTOCOL_VERSION;
  readonly request_id: string;
  readonly canceled: true;
}

export interface AgentMemoryMetrics {
  readonly candidate_memory_count: number;
  readonly blocked_memory_count: number;
  readonly decision_count: number;
  readonly used_memory_count: number;
  readonly unevaluated_decision_count: number;
  readonly evaluated_with_memory_count: number;
  readonly evaluated_without_memory_count: number;
  readonly pass_rate_with_memory: number | null;
  readonly pass_rate_without_memory: number | null;
  readonly wrong_memory_failure_count: number;
  readonly obsolete_memory_usage_attempts: number;
  readonly average_lesson_confidence: number;
}

export interface AgentMemoryRunMetrics {
  readonly decision_count: number;
  readonly complete_count: number;
  readonly pending_count: number;
  readonly decision_only_count: number;
  readonly trace_only_count: number;
  readonly conflict_count: number;
  readonly recoverable_count: number;
  readonly auto_recoverable_count: number;
  readonly attribution_required_count: number;
}

export interface AgentHealth {
  readonly protocol_version: typeof AGENT_PROTOCOL_VERSION;
  readonly pending_request_count: number;
  readonly finalized_request_replay_count: number;
  readonly memory_metrics: AgentMemoryMetrics;
  readonly memory_run_metrics: AgentMemoryRunMetrics;
}

export interface AgentRequestOptions {
  readonly signal?: AbortSignal;
}

export interface AgentHTTPClientOptions {
  readonly baseUrl: string;
  readonly token: string;
  readonly timeoutMs?: number;
}
