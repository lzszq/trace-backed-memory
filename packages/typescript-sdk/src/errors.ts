import type { AgentErrorCategory, AgentOperation } from "./types.js";

export class AgentMemoryError extends Error {
  readonly code: string;
  readonly category: AgentErrorCategory;
  readonly operation: AgentOperation;
  readonly retryable: boolean;
  readonly requestId: string | undefined;
  readonly decisionId: string | undefined;
  readonly status: number | undefined;

  constructor(
    code: string,
    category: AgentErrorCategory,
    operation: AgentOperation,
    message: string,
    options: {
      readonly retryable?: boolean;
      readonly requestId?: string;
      readonly decisionId?: string;
      readonly status?: number;
    } = {},
  ) {
    super(message);
    this.name = "AgentMemoryError";
    this.code = code;
    this.category = category;
    this.operation = operation;
    this.retryable = options.retryable ?? false;
    this.requestId = options.requestId;
    this.decisionId = options.decisionId;
    this.status = options.status;
  }
}
