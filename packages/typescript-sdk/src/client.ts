import { request as httpRequest, type IncomingMessage } from "node:http";

import { AgentMemoryError } from "./errors.js";
import {
  JSON_MAX_BYTES,
  parseBoundedJson,
  stringifyBoundedJson,
} from "./strict-json.js";
import type {
  AgentCapabilities,
  AgentCancelRequest,
  AgentCanceledRun,
  AgentCompleteRequest,
  AgentCompletedRun,
  AgentFinalizeRequest,
  AgentFinalizedMemory,
  AgentHealth,
  AgentHTTPClientOptions,
  AgentOperation,
  AgentPrepareRequest,
  AgentPreparedMemory,
  AgentRequestOptions,
} from "./types.js";
import { AGENT_PROTOCOL_VERSION } from "./types.js";
import {
  parseCanceled,
  parseCapabilities,
  parseCompleted,
  parseFinalized,
  parseHealth,
  parsePrepared,
  parsePublicError,
} from "./validation.js";

const TOKEN_MIN_CHARS = 32;
const TOKEN_MAX_CHARS = 512;
const LOOPBACK_URL =
  /^http:\/\/(127(?:\.(?:0|[1-9]\d{0,2})){3}):([0-9]{1,5})\/?$/;

type HttpMethod = "GET" | "POST";

function sdkInput(operation: AgentOperation, message: string): AgentMemoryError {
  return new AgentMemoryError(
    "TBM_SDK_INVALID_INPUT",
    "input",
    operation,
    message,
  );
}

function responseInvalid(
  operation: AgentOperation,
  message = "local HTTP service returned an invalid response",
): AgentMemoryError {
  return new AgentMemoryError(
    "TBM_SDK_RESPONSE_INVALID",
    "callback",
    operation,
    message,
  );
}

function transportError(
  operation: AgentOperation,
  message = "local HTTP service could not be reached",
): AgentMemoryError {
  return new AgentMemoryError(
    "TBM_SDK_TRANSPORT_ERROR",
    "callback",
    operation,
    message,
    { retryable: true },
  );
}

function normalizeBaseUrl(baseUrl: string): string {
  const match = LOOPBACK_URL.exec(baseUrl);
  if (match === null) {
    throw new TypeError(
      "baseUrl must identify a loopback HTTP service with a port",
    );
  }
  const octets = match[1]?.split(".").map((item) => Number(item)) ?? [];
  const port = Number(match[2]);
  if (
    octets.length !== 4 ||
    octets.some((item) => !Number.isInteger(item) || item < 0 || item > 255) ||
    !Number.isInteger(port) ||
    port < 1 ||
    port > 65_535
  ) {
    throw new TypeError(
      "baseUrl must identify a loopback HTTP service with a port",
    );
  }
  return `http://${octets.join(".")}:${port}`;
}

async function readBoundedBody(
  response: IncomingMessage,
  expectedLength: number,
  operation: AgentOperation,
): Promise<Uint8Array> {
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    for await (const chunk of response) {
      const bytes =
        typeof chunk === "string" ? Buffer.from(chunk, "utf8") : chunk;
      length += bytes.byteLength;
      if (length > JSON_MAX_BYTES || length > expectedLength) {
        response.destroy();
        throw responseInvalid(operation);
      }
      chunks.push(bytes);
    }
  } catch (error) {
    if (error instanceof AgentMemoryError) {
      throw error;
    }
    throw responseInvalid(operation);
  }
  if (length !== expectedLength) {
    throw responseInvalid(operation);
  }
  const output = new Uint8Array(length);
  let offset = 0;
  for (const chunk of chunks) {
    output.set(chunk, offset);
    offset += chunk.byteLength;
  }
  return output;
}

function singleHeader(
  response: IncomingMessage,
  name: string,
): string | undefined {
  const values = headerValues(response, name);
  return values.length === 1 ? values[0] : undefined;
}

function headerValues(
  response: IncomingMessage,
  name: string,
): string[] {
  const values: string[] = [];
  for (let index = 0; index < response.rawHeaders.length; index += 2) {
    const headerName = response.rawHeaders[index];
    const headerValue = response.rawHeaders[index + 1];
    if (
      headerName?.toLowerCase() === name.toLowerCase() &&
      headerValue !== undefined
    ) {
      values.push(headerValue);
    }
  }
  return values;
}

export class AgentHTTPClient {
  private readonly baseUrl: string;
  private readonly token: string;
  private readonly timeoutMs: number;

  constructor(options: AgentHTTPClientOptions) {
    if (
      options === null ||
      typeof options !== "object" ||
      typeof options.baseUrl !== "string"
    ) {
      throw new TypeError("options.baseUrl must be a string");
    }
    this.baseUrl = normalizeBaseUrl(options.baseUrl);
    const tokenLength =
      typeof options.token === "string" ? Array.from(options.token).length : 0;
    if (
      typeof options.token !== "string" ||
      tokenLength < TOKEN_MIN_CHARS ||
      tokenLength > TOKEN_MAX_CHARS ||
      options.token.trim() !== options.token ||
      options.token.length === 0
    ) {
      throw new TypeError("token must be a bounded nonblank bearer secret");
    }
    const timeoutMs = options.timeoutMs ?? 30_000;
    if (
      typeof timeoutMs !== "number" ||
      !Number.isFinite(timeoutMs) ||
      timeoutMs <= 0 ||
      timeoutMs > 300_000
    ) {
      throw new TypeError("timeoutMs must be between 0 and 300000");
    }
    this.token = options.token;
    this.timeoutMs = timeoutMs;
  }

  capabilities(options?: AgentRequestOptions): Promise<AgentCapabilities> {
    return this.request(
      "GET",
      "/v1/capabilities",
      undefined,
      "health",
      parseCapabilities,
      options,
    );
  }

  health(options?: AgentRequestOptions): Promise<AgentHealth> {
    return this.request(
      "GET",
      "/v1/health",
      undefined,
      "health",
      parseHealth,
      options,
    );
  }

  prepare(
    payload: AgentPrepareRequest,
    options?: AgentRequestOptions,
  ): Promise<AgentPreparedMemory> {
    return this.request(
      "POST",
      "/v1/prepare",
      payload,
      "prepare",
      parsePrepared,
      options,
    );
  }

  finalize(
    payload: AgentFinalizeRequest,
    options?: AgentRequestOptions,
  ): Promise<AgentFinalizedMemory> {
    return this.request(
      "POST",
      "/v1/finalize",
      payload,
      "finalize",
      parseFinalized,
      options,
    );
  }

  complete(
    payload: AgentCompleteRequest,
    options?: AgentRequestOptions,
  ): Promise<AgentCompletedRun> {
    return this.request(
      "POST",
      "/v1/complete",
      payload,
      "complete",
      parseCompleted,
      options,
    );
  }

  cancel(
    payload: AgentCancelRequest,
    options?: AgentRequestOptions,
  ): Promise<AgentCanceledRun> {
    return this.request(
      "POST",
      "/v1/cancel",
      payload,
      "cancel",
      parseCanceled,
      options,
    );
  }

  private async request<T>(
    method: HttpMethod,
    path: string,
    payload: unknown,
    operation: AgentOperation,
    parse: (value: unknown) => T,
    options: AgentRequestOptions | undefined,
  ): Promise<T> {
    let body: string | undefined;
    if (method === "POST" && payload === undefined) {
      throw sdkInput(operation, "request must be a plain JSON object");
    }
    if (payload !== undefined) {
      if (
        payload === null ||
        typeof payload !== "object" ||
        Array.isArray(payload) ||
        ![Object.prototype, null].includes(Object.getPrototypeOf(payload))
      ) {
        throw sdkInput(operation, "request must be a plain JSON object");
      }
      try {
        body = stringifyBoundedJson(payload);
      } catch {
        throw sdkInput(operation, "request is not bounded JSON data");
      }
    }
    const callerSignal = options?.signal;
    if (callerSignal?.aborted === true) {
      throw transportError(operation, "local HTTP request was aborted");
    }
    const controller = new AbortController();
    const abort = (): void => controller.abort();
    callerSignal?.addEventListener("abort", abort, { once: true });
    const timer = setTimeout(abort, this.timeoutMs);
    try {
      const headers: Record<string, string> = {
        Accept: "application/json",
        Authorization: `Bearer ${this.token}`,
      };
      if (body !== undefined) {
        headers["Content-Type"] = "application/json";
      }
      if (body !== undefined) {
        headers["Content-Length"] = String(Buffer.byteLength(body, "utf8"));
      }
      let response: IncomingMessage;
      try {
        response = await new Promise<IncomingMessage>((resolve, reject) => {
          const request = httpRequest(
            this.baseUrl + path,
            {
              method,
              headers,
              agent: false,
              signal: controller.signal,
            },
            resolve,
          );
          request.once("error", reject);
          request.end(body);
        });
      } catch {
        throw transportError(operation);
      }
      const protocol = singleHeader(response, "x-tbm-protocol-version");
      const contentType = singleHeader(response, "content-type");
      const contentLength = singleHeader(response, "content-length");
      if (
        protocol !== AGENT_PROTOCOL_VERSION ||
        contentType === null ||
        contentType === undefined ||
        contentType.split(";", 1)[0]?.trim().toLowerCase() !==
          "application/json" ||
        headerValues(response, "transfer-encoding").length !== 0 ||
        contentLength === undefined ||
        !/^[0-9]+$/.test(contentLength)
      ) {
        response.destroy();
        throw responseInvalid(operation);
      }
      const normalizedLength = contentLength.replace(/^0+(?=\d)/, "");
      if (
        normalizedLength.length > String(JSON_MAX_BYTES).length ||
        Number(normalizedLength) > JSON_MAX_BYTES
      ) {
        response.destroy();
        throw responseInvalid(operation);
      }
      let value: unknown;
      try {
        value = parseBoundedJson(
          await readBoundedBody(
            response,
            Number(normalizedLength),
            operation,
          ),
        );
      } catch (error) {
        if (error instanceof AgentMemoryError) {
          throw error;
        }
        throw responseInvalid(operation);
      }
      const status = response.statusCode;
      if (status === undefined || status < 200 || status > 299) {
        if (status === undefined || status < 400 || status > 599) {
          throw responseInvalid(operation);
        }
        throw parsePublicError(value, operation, status);
      }
      return parse(value);
    } finally {
      clearTimeout(timer);
      callerSignal?.removeEventListener("abort", abort);
    }
  }
}
