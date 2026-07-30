import type {
  DurableErrorCategory,
  DurableSDKOperation,
} from "./durable-types.js";

export class DurableAgentHTTPError extends Error {
  readonly code: string;
  readonly category: DurableErrorCategory;
  readonly operation: DurableSDKOperation;
  readonly retryable: boolean;
  readonly status: number | undefined;

  constructor(
    code: string,
    category: DurableErrorCategory,
    operation: DurableSDKOperation,
    message: string,
    options: {
      readonly retryable?: boolean;
      readonly status?: number;
    } = {},
  ) {
    super(message);
    this.name = "DurableAgentHTTPError";
    this.code = code;
    this.category = category;
    this.operation = operation;
    this.retryable = options.retryable ?? false;
    this.status = options.status;
  }
}
