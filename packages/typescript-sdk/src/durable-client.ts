import { request as httpRequest, type IncomingMessage } from "node:http";
import {
  request as httpsRequest,
  type RequestOptions as HTTPSRequestOptions,
} from "node:https";
import { isIP } from "node:net";

import { DurableAgentHTTPError } from "./durable-errors.js";
import {
  DURABLE_AGENT_PROTOCOL_VERSION,
  type DurableAbandonRequest,
  type DurableAgentHTTPClientOptions,
  type DurableAgentHTTPResponse,
  type DurableCancelRequest,
  type DurableCapabilities,
  type DurableCompleteRequest,
  type DurableDecideRequest,
  type DurableFinalizeRequest,
  type DurableGetSessionRequest,
  type DurableHealth,
  type DurablePrepareRequest,
  type DurableReplayRequest,
  type DurableRequestByOperation,
  type DurableRequestOptions,
  type DurableResumeRequest,
  type DurableSDKOperation,
  type DurableStartRequest,
  type DurableWireOperation,
} from "./durable-types.js";
import {
  assertDurableRequest,
  parseDurableCapabilities,
  parseDurableHealth,
  parseDurableOpenAPI,
  parseDurableOperationResponse,
  parseDurablePublicError,
} from "./durable-validation.js";
import {
  JSON_MAX_BYTES,
  parseBoundedJson,
  stringifyBoundedJson,
} from "./strict-json.js";
import type { JsonObject } from "./types.js";

const TOKEN_MIN_CHARS = 32;
const TOKEN_MAX_CHARS = 8_192 - "Bearer ".length;
const MAX_ATTEMPTS = 5;
const MAX_RETRY_DELAY_MS = 10_000;
const DURABLE_RESPONSE_MAX_BYTES = 16 * 1024 * 1024;
const EXPLICIT_URL =
  /^(https?):\/\/([^/?#@[\]]+):([0-9]{1,5})\/?$/;

type HttpMethod = "GET" | "POST";

function sdkInput(
  operation: DurableSDKOperation,
  message: string,
): DurableAgentHTTPError {
  return new DurableAgentHTTPError(
    "TBM_DURABLE_SDK_INVALID_INPUT",
    "input",
    operation,
    message,
  );
}

function responseInvalid(
  operation: DurableSDKOperation,
): DurableAgentHTTPError {
  return new DurableAgentHTTPError(
    "TBM_DURABLE_SDK_RESPONSE_INVALID",
    "internal",
    operation,
    "durable HTTP service returned an invalid response",
  );
}

function transportError(
  operation: DurableSDKOperation,
  message = "durable HTTP service could not be reached",
): DurableAgentHTTPError {
  return new DurableAgentHTTPError(
    "TBM_DURABLE_SDK_TRANSPORT_ERROR",
    "internal",
    operation,
    message,
    { retryable: true },
  );
}

function normalizeBaseUrl(baseUrl: string): {
  readonly value: string;
  readonly secure: boolean;
  readonly hostname: string;
} {
  const match = EXPLICIT_URL.exec(baseUrl);
  if (match === null) {
    throw new TypeError(
      "baseUrl must identify HTTP(S) with an explicit host and port",
    );
  }
  const scheme = match[1]!;
  const hostname = match[2]!;
  const port = Number(match[3]);
  if (!Number.isInteger(port) || port < 1 || port > 65_535) {
    throw new TypeError("baseUrl port is invalid");
  }
  if (scheme === "http") {
    const octets = hostname.split(".").map((item) => Number(item));
    if (
      isIP(hostname) !== 4 ||
      octets.length !== 4 ||
      octets[0] !== 127 ||
      octets.some(
        (item) =>
          !Number.isInteger(item) || item < 0 || item > 255,
      )
    ) {
      throw new TypeError(
        "plaintext durable HTTP must use a loopback IPv4 address",
      );
    }
  } else if (isIP(hostname) === 6) {
    throw new TypeError("durable HTTPS does not support IPv6 addresses");
  }
  return {
    value: `${scheme}://${hostname}:${port}`,
    secure: scheme === "https",
    hostname,
  };
}

function validateAttempts(value: unknown, label: string): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 1 ||
    value > MAX_ATTEMPTS
  ) {
    throw new TypeError(`${label} must be between 1 and ${MAX_ATTEMPTS}`);
  }
  return value;
}

function validateDelay(value: unknown, label: string): number {
  if (
    typeof value !== "number" ||
    !Number.isSafeInteger(value) ||
    value < 0 ||
    value > MAX_RETRY_DELAY_MS
  ) {
    throw new TypeError(
      `${label} must be between 0 and ${MAX_RETRY_DELAY_MS}`,
    );
  }
  return value;
}

async function readBoundedBody(
  response: IncomingMessage,
  expectedLength: number,
  operation: DurableSDKOperation,
  signal: AbortSignal,
): Promise<Uint8Array> {
  const chunks: Uint8Array[] = [];
  let length = 0;
  try {
    for await (const chunk of response) {
      const bytes =
        typeof chunk === "string" ? Buffer.from(chunk, "utf8") : chunk;
      length += bytes.byteLength;
      if (length > DURABLE_RESPONSE_MAX_BYTES || length > expectedLength) {
        response.destroy();
        throw responseInvalid(operation);
      }
      chunks.push(bytes);
    }
  } catch (error) {
    if (error instanceof DurableAgentHTTPError) {
      throw error;
    }
    if (signal.aborted) {
      throw transportError(
        operation,
        "durable HTTP request was aborted",
      );
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

function singleHeader(
  response: IncomingMessage,
  name: string,
): string | undefined {
  const values = headerValues(response, name);
  return values.length === 1 ? values[0] : undefined;
}

function waitForRetry(
  delayMs: number,
  signal: AbortSignal | undefined,
  operation: DurableSDKOperation,
): Promise<void> {
  if (signal?.aborted === true) {
    return Promise.reject(
      transportError(operation, "durable HTTP request was aborted"),
    );
  }
  if (delayMs === 0) {
    return Promise.resolve();
  }
  return new Promise((resolve, reject) => {
    const complete = (): void => {
      signal?.removeEventListener("abort", abort);
      resolve();
    };
    const timer = setTimeout(complete, delayMs);
    const abort = (): void => {
      clearTimeout(timer);
      signal?.removeEventListener("abort", abort);
      reject(transportError(operation, "durable HTTP request was aborted"));
    };
    signal?.addEventListener("abort", abort, { once: true });
  });
}

export class DurableAgentHTTPClient {
  private readonly baseUrl: string;
  private readonly secure: boolean;
  private readonly token: string;
  private readonly timeoutMs: number;
  private readonly maxAttempts: number;
  private readonly retryDelayMs: number;
  private readonly tlsOptions: HTTPSRequestOptions;

  constructor(options: DurableAgentHTTPClientOptions) {
    if (
      options === null ||
      typeof options !== "object" ||
      typeof options.baseUrl !== "string"
    ) {
      throw new TypeError("options.baseUrl must be a string");
    }
    const normalized = normalizeBaseUrl(options.baseUrl);
    const tokenLength =
      typeof options.token === "string" ? Array.from(options.token).length : 0;
    if (
      typeof options.token !== "string" ||
      tokenLength < TOKEN_MIN_CHARS ||
      tokenLength > TOKEN_MAX_CHARS ||
      options.token.trim() !== options.token ||
      Array.from(options.token).some(
        (character) => {
          const code = character.codePointAt(0)!;
          return code < 32 || code === 127;
        },
      )
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
    if (!normalized.secure && (options.ca !== undefined || options.servername)) {
      throw new TypeError("TLS options require an https baseUrl");
    }
    if (
      options.servername !== undefined &&
      (typeof options.servername !== "string" ||
        options.servername.length === 0 ||
        options.servername.trim() !== options.servername)
    ) {
      throw new TypeError("servername must be a nonblank string");
    }
    this.baseUrl = normalized.value;
    this.secure = normalized.secure;
    this.token = options.token;
    this.timeoutMs = timeoutMs;
    this.maxAttempts = validateAttempts(
      options.maxAttempts ?? 1,
      "maxAttempts",
    );
    this.retryDelayMs = validateDelay(
      options.retryDelayMs ?? 0,
      "retryDelayMs",
    );
    let ca: string | Buffer | (string | Buffer)[] | undefined;
    if (options.ca === undefined) {
      ca = undefined;
    } else if (typeof options.ca === "string" || Buffer.isBuffer(options.ca)) {
      ca = options.ca;
    } else {
      ca = [...options.ca];
    }
    this.tlsOptions = {
      rejectUnauthorized: true,
      ...(ca === undefined ? {} : { ca }),
      ...(options.servername === undefined
        ? {}
        : { servername: options.servername }),
    };
  }

  negotiate(options?: DurableRequestOptions): Promise<DurableCapabilities> {
    return this.capabilities(options);
  }

  capabilities(options?: DurableRequestOptions): Promise<DurableCapabilities> {
    return this.request(
      "GET",
      "/durable/v1/capabilities",
      undefined,
      "capabilities",
      parseDurableCapabilities,
      options,
    );
  }

  openapi(options?: DurableRequestOptions): Promise<JsonObject> {
    return this.request(
      "GET",
      "/durable/v1/openapi",
      undefined,
      "openapi",
      parseDurableOpenAPI,
      options,
    );
  }

  health(options?: DurableRequestOptions): Promise<DurableHealth> {
    return this.request(
      "GET",
      "/durable/v1/health",
      undefined,
      "health",
      parseDurableHealth,
      options,
    );
  }

  prepare(
    payload: DurablePrepareRequest,
    options?: DurableRequestOptions,
  ): Promise<DurableAgentHTTPResponse<"prepare">> {
    return this.operation("prepare", payload, options);
  }

  decide(
    payload: DurableDecideRequest,
    options?: DurableRequestOptions,
  ): Promise<DurableAgentHTTPResponse<"decide">> {
    return this.operation("decide", payload, options);
  }

  finalize(
    payload: DurableFinalizeRequest,
    options?: DurableRequestOptions,
  ): Promise<DurableAgentHTTPResponse<"finalize">> {
    return this.operation("finalize", payload, options);
  }

  start(
    payload: DurableStartRequest,
    options?: DurableRequestOptions,
  ): Promise<DurableAgentHTTPResponse<"start">> {
    return this.operation("start", payload, options);
  }

  resume(
    payload: DurableResumeRequest,
    options?: DurableRequestOptions,
  ): Promise<DurableAgentHTTPResponse<"resume">> {
    return this.operation("resume", payload, options);
  }

  heartbeat(
    payload: DurableResumeRequest,
    options?: DurableRequestOptions,
  ): Promise<DurableAgentHTTPResponse<"resume">> {
    return this.resume(payload, options);
  }

  abandon(
    payload: DurableAbandonRequest,
    options?: DurableRequestOptions,
  ): Promise<DurableAgentHTTPResponse<"abandon">> {
    return this.operation("abandon", payload, options);
  }

  complete(
    payload: DurableCompleteRequest,
    options?: DurableRequestOptions,
  ): Promise<DurableAgentHTTPResponse<"complete">> {
    return this.operation("complete", payload, options);
  }

  cancel(
    payload: DurableCancelRequest,
    options?: DurableRequestOptions,
  ): Promise<DurableAgentHTTPResponse<"cancel">> {
    return this.operation("cancel", payload, options);
  }

  getSession(
    payload: DurableGetSessionRequest,
    options?: DurableRequestOptions,
  ): Promise<DurableAgentHTTPResponse<"get_session">> {
    return this.operation("get_session", payload, options);
  }

  exportReplay(
    payload: DurableReplayRequest,
    options?: DurableRequestOptions,
  ): Promise<DurableAgentHTTPResponse<"export_replay">> {
    return this.operation("export_replay", payload, options);
  }

  private async operation<O extends DurableWireOperation>(
    operation: O,
    payload: DurableRequestByOperation[O],
    options: DurableRequestOptions | undefined,
  ): Promise<DurableAgentHTTPResponse<O>> {
    assertDurableRequest(operation, payload);
    const route = operation.replaceAll("_", "-");
    return await this.request(
      "POST",
      `/durable/v1/${route}`,
      payload,
      operation,
      (value) => parseDurableOperationResponse(value, operation),
      options,
    );
  }

  private async request<T>(
    method: HttpMethod,
    path: string,
    payload: unknown,
    operation: DurableSDKOperation,
    parse: (value: unknown) => T,
    options: DurableRequestOptions | undefined,
  ): Promise<T> {
    const maxAttempts = validateAttempts(
      options?.maxAttempts ?? this.maxAttempts,
      "request maxAttempts",
    );
    const retryDelayMs = validateDelay(
      options?.retryDelayMs ?? this.retryDelayMs,
      "request retryDelayMs",
    );
    let body: string | undefined;
    if (payload !== undefined) {
      try {
        body = stringifyBoundedJson(payload);
      } catch {
        throw sdkInput(operation, "request is not bounded JSON data");
      }
    }
    let lastError: DurableAgentHTTPError | undefined;
    for (let attempt = 1; attempt <= maxAttempts; attempt += 1) {
      try {
        return await this.requestOnce(
          method,
          path,
          body,
          operation,
          parse,
          options?.signal,
        );
      } catch (error) {
        if (
          !(error instanceof DurableAgentHTTPError) ||
          !error.retryable ||
          options?.signal?.aborted === true ||
          attempt === maxAttempts
        ) {
          throw error;
        }
        lastError = error;
        await waitForRetry(retryDelayMs, options?.signal, operation);
      }
    }
    throw lastError ?? transportError(operation);
  }

  private async requestOnce<T>(
    method: HttpMethod,
    path: string,
    body: string | undefined,
    operation: DurableSDKOperation,
    parse: (value: unknown) => T,
    callerSignal: AbortSignal | undefined,
  ): Promise<T> {
    if (callerSignal?.aborted === true) {
      throw transportError(operation, "durable HTTP request was aborted");
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
        headers["Content-Length"] = String(Buffer.byteLength(body, "utf8"));
      }
      let response: IncomingMessage;
      try {
        response = await new Promise<IncomingMessage>((resolve, reject) => {
          const callback = this.secure ? httpsRequest : httpRequest;
          const request = callback(
            this.baseUrl + path,
            {
              method,
              headers,
              agent: false,
              signal: controller.signal,
              ...(this.secure ? this.tlsOptions : {}),
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
        protocol !== DURABLE_AGENT_PROTOCOL_VERSION ||
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
        normalizedLength.length > String(DURABLE_RESPONSE_MAX_BYTES).length ||
        Number(normalizedLength) > DURABLE_RESPONSE_MAX_BYTES
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
            controller.signal,
          ),
          DURABLE_RESPONSE_MAX_BYTES,
        );
      } catch (error) {
        if (error instanceof DurableAgentHTTPError) {
          throw error;
        }
        throw responseInvalid(operation);
      }
      const status = response.statusCode ?? 0;
      if (status < 200 || status >= 300) {
        throw parseDurablePublicError(value, operation, status);
      }
      return parse(value);
    } finally {
      clearTimeout(timer);
      callerSignal?.removeEventListener("abort", abort);
    }
  }
}
