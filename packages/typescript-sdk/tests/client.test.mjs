import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { platform } from "node:os";
import { fileURLToPath } from "node:url";
import { once } from "node:events";
import { createInterface } from "node:readline";
import test from "node:test";

import {
  AGENT_PROTOCOL_VERSION,
  AgentHTTPClient,
  AgentMemoryError,
} from "../dist/index.js";

const TOKEN = `typescript_sdk_test_${"a".repeat(32)}`;
const ROOT = new URL("../../../", import.meta.url);

async function example(name) {
  return JSON.parse(
    await readFile(new URL(`examples/${name}.example.json`, ROOT), "utf8"),
  );
}

function jsonResponse(response, status, payload) {
  const body =
    typeof payload === "string"
      ? Buffer.from(payload, "utf8")
      : Buffer.from(JSON.stringify(payload), "utf8");
  response.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": String(body.byteLength),
    "X-TBM-Protocol-Version": AGENT_PROTOCOL_VERSION,
    Connection: "close",
  });
  response.end(body);
}

async function withServer(handler, callback) {
  const server = createServer(handler);
  server.listen(0, "127.0.0.1");
  await once(server, "listening");
  const address = server.address();
  assert.notEqual(address, null);
  assert.equal(typeof address, "object");
  try {
    return await callback(`http://127.0.0.1:${address.port}`);
  } finally {
    server.close();
    await once(server, "close");
  }
}

function readPythonServerConfiguration(child, lines, readStderr) {
  return new Promise((resolve, reject) => {
    const cleanup = () => {
      lines.removeListener("line", onLine);
      child.removeListener("error", onError);
      child.removeListener("exit", onExit);
    };
    const failure = (message, cause) => {
      cleanup();
      const detail = readStderr().trim().slice(-4096);
      reject(
        new Error(detail.length === 0 ? message : `${message}: ${detail}`, {
          cause,
        }),
      );
    };
    const onLine = (line) => {
      cleanup();
      try {
        resolve(JSON.parse(line));
      } catch (error) {
        reject(new Error("Python server returned invalid JSON", {
          cause: error,
        }));
      }
    };
    const onError = (error) => {
      failure("Python server could not be started", error);
    };
    const onExit = (code, signal) => {
      failure(
        `Python server exited before startup (code=${code}, signal=${signal})`,
      );
    };
    lines.once("line", onLine);
    child.once("error", onError);
    child.once("exit", onExit);
  });
}

test("typed client covers all six canonical routes", async () => {
  const fixtures = {
    "/v1/capabilities": await example("agent_capabilities"),
    "/v1/health": await example("agent_health"),
    "/v1/prepare": await example("agent_prepared"),
    "/v1/finalize": await example("agent_finalized"),
    "/v1/complete": await example("agent_completed"),
    "/v1/cancel": await example("agent_canceled"),
  };
  const seen = [];
  await withServer(
    (request, response) => {
      assert.equal(request.headers.authorization, `Bearer ${TOKEN}`);
      assert.equal(request.headers.accept, "application/json");
      const chunks = [];
      request.on("data", (chunk) => chunks.push(chunk));
      request.on("end", () => {
        const body = Buffer.concat(chunks);
        if (request.method === "POST") {
          assert.equal(request.headers["content-type"], "application/json");
          assert.equal(typeof JSON.parse(body.toString("utf8")), "object");
        } else {
          assert.equal(body.byteLength, 0);
        }
        seen.push(`${request.method} ${request.url}`);
        jsonResponse(response, 200, fixtures[request.url]);
      });
    },
    async (baseUrl) => {
      const client = new AgentHTTPClient({ baseUrl, token: TOKEN });
      const capabilities = await client.capabilities();
      assert.equal(capabilities.protocol_version, AGENT_PROTOCOL_VERSION);
      const health = await client.health();
      assert.equal(health.pending_request_count, 0);
      const prepared = await client.prepare({
        task: "repair checkout",
        mode: "repair",
      });
      assert.equal(prepared.protocol_version, AGENT_PROTOCOL_VERSION);
      const finalized = await client.finalize({
        request_id: prepared.request_id,
        use_memory: false,
        allowed_memory_ids: [],
        blocked_memory_ids: [],
        reason: "no applicable memory",
        risk: "none",
        recommended_injection: "none",
      });
      assert.equal(finalized.protocol_version, AGENT_PROTOCOL_VERSION);
      const completed = await client.complete({
        decision_id: finalized.decision_id,
        eval_result: "pass",
      });
      assert.equal(completed.eval_result, "pass");
      const canceled = await client.cancel({
        request_id: prepared.request_id,
      });
      assert.equal(canceled.canceled, true);
    },
  );
  assert.deepEqual(seen, [
    "GET /v1/capabilities",
    "GET /v1/health",
    "POST /v1/prepare",
    "POST /v1/finalize",
    "POST /v1/complete",
    "POST /v1/cancel",
  ]);
});

test("client maps stable public errors without leaking the token", async () => {
  const payload = await example("agent_error");
  await withServer(
    (_request, response) => jsonResponse(response, 409, payload),
    async (baseUrl) => {
      const client = new AgentHTTPClient({ baseUrl, token: TOKEN });
      await assert.rejects(client.health(), (error) => {
        assert.ok(error instanceof AgentMemoryError);
        assert.match(error.code, /^TBM_/);
        assert.equal(error.status, 409);
        assert.equal(String(error).includes(TOKEN), false);
        assert.equal(error.stack?.includes(TOKEN) ?? false, false);
        return true;
      });
    },
  );
});

test("client rejects duplicate response keys and malformed headers", async () => {
  await withServer(
    (_request, response) =>
      jsonResponse(
        response,
        200,
        '{"protocol_version":"tbm.agent.v1","protocol_version":"tbm.agent.v1"}',
      ),
    async (baseUrl) => {
      const client = new AgentHTTPClient({ baseUrl, token: TOKEN });
      await assert.rejects(client.health(), (error) => {
        assert.equal(error.code, "TBM_SDK_RESPONSE_INVALID");
        return true;
      });
    },
  );
  await withServer(
    (_request, response) => {
      const body = Buffer.from("{}", "utf8");
      response.writeHead(200, {
        "Content-Type": "text/plain",
        "Content-Length": String(body.byteLength),
        "X-TBM-Protocol-Version": AGENT_PROTOCOL_VERSION,
      });
      response.end(body);
    },
    async (baseUrl) => {
      const client = new AgentHTTPClient({ baseUrl, token: TOKEN });
      await assert.rejects(client.health(), (error) => {
        assert.equal(error.code, "TBM_SDK_RESPONSE_INVALID");
        return true;
      });
    },
  );
});

test("client rejects unsafe configuration and unbounded JSON", async () => {
  for (const baseUrl of [
    "https://127.0.0.1:8765",
    "http://localhost:8765",
    "http://user@127.0.0.1:8765",
    "http://127.0.0.1:8765/path",
    "http://127.0.0.1",
  ]) {
    assert.throws(() => new AgentHTTPClient({ baseUrl, token: TOKEN }));
  }
  assert.throws(
    () =>
      new AgentHTTPClient({
        baseUrl: "http://127.0.0.1:8765",
        token: "too-short",
      }),
  );
  assert.throws(
    () =>
      new AgentHTTPClient({
        baseUrl: "http://127.0.0.1:8765",
        token: TOKEN,
        timeoutMs: 0,
      }),
  );
  const client = new AgentHTTPClient({
    baseUrl: "http://127.0.0.1:1",
    token: TOKEN,
  });
  for (const invalid of [undefined, null, [], "request"]) {
    await assert.rejects(client.prepare(invalid), (error) => {
      assert.equal(error.code, "TBM_SDK_INVALID_INPUT");
      return true;
    });
  }
  await assert.rejects(
    client.prepare({
      task: "task",
      mode: "planning",
      minimum_score: Number.NaN,
    }),
    (error) => {
      assert.equal(error.code, "TBM_SDK_INVALID_INPUT");
      return true;
    },
  );
  const cyclic = { task: "task", mode: "planning" };
  cyclic.self = cyclic;
  await assert.rejects(client.prepare(cyclic), (error) => {
    assert.equal(error.code, "TBM_SDK_INVALID_INPUT");
    return true;
  });
});

test("client rejects version zero and escaped lone surrogates", async () => {
  const capabilities = await example("agent_capabilities");
  capabilities.snapshot_version = 0;
  await withServer(
    (_request, response) => jsonResponse(response, 200, capabilities),
    async (baseUrl) => {
      const client = new AgentHTTPClient({ baseUrl, token: TOKEN });
      await assert.rejects(client.capabilities(), (error) => {
        assert.equal(error.code, "TBM_SDK_RESPONSE_INVALID");
        return true;
      });
    },
  );

  const escaped = await example("agent_capabilities");
  escaped.durable_records = ["\ud800"];
  await withServer(
    (_request, response) => jsonResponse(response, 200, escaped),
    async (baseUrl) => {
      const client = new AgentHTTPClient({ baseUrl, token: TOKEN });
      await assert.rejects(client.capabilities(), (error) => {
        assert.equal(error.code, "TBM_SDK_RESPONSE_INVALID");
        return true;
      });
    },
  );
});

test("client rejects any Transfer-Encoding response", async () => {
  await withServer(
    (_request, response) => {
      response.writeHead(200, {
        "Content-Type": "application/json",
        "X-TBM-Protocol-Version": AGENT_PROTOCOL_VERSION,
        "Transfer-Encoding": "chunked",
      });
      response.end("{}");
    },
    async (baseUrl) => {
      const client = new AgentHTTPClient({ baseUrl, token: TOKEN });
      await assert.rejects(client.health(), (error) => {
        assert.equal(error.code, "TBM_SDK_RESPONSE_INVALID");
        return true;
      });
    },
  );
});

test("AbortSignal and timeout fail closed without automatic retries", async () => {
  let requestCount = 0;
  await withServer(
    (_request, response) => {
      requestCount += 1;
      setTimeout(
        () =>
          jsonResponse(response, 200, {
            protocol_version: AGENT_PROTOCOL_VERSION,
          }),
        200,
      );
    },
    async (baseUrl) => {
      const client = new AgentHTTPClient({
        baseUrl,
        token: TOKEN,
        timeoutMs: 20,
      });
      await assert.rejects(client.health(), (error) => {
        assert.equal(error.code, "TBM_SDK_TRANSPORT_ERROR");
        assert.equal(error.retryable, true);
        return true;
      });
      const controller = new AbortController();
      controller.abort();
      await assert.rejects(
        client.health({ signal: controller.signal }),
        (error) => {
          assert.equal(error.code, "TBM_SDK_TRANSPORT_ERROR");
          return true;
        },
      );
    },
  );
  assert.equal(requestCount, 1);
});

test("Node SDK completes a real Python HTTP lifecycle", async () => {
  const python =
    process.env.TBM_PYTHON ?? (platform() === "win32" ? "python" : "python3");
  const child = spawn(python, ["tests/typescript_sdk_server.py"], {
    cwd: fileURLToPath(ROOT),
    stdio: ["pipe", "pipe", "pipe"],
    windowsHide: true,
  });
  let stderr = "";
  child.stderr.setEncoding("utf8");
  child.stderr.on("data", (chunk) => {
    stderr += chunk;
  });
  const lines = createInterface({ input: child.stdout });
  let operationFailed = false;
  try {
    const configuration = await readPythonServerConfiguration(
      child,
      lines,
      () => stderr,
    );
    const client = new AgentHTTPClient({
      baseUrl: configuration.base_url,
      token: configuration.token,
    });
    const capabilities = await client.capabilities();
    assert.ok(capabilities.operations.includes("prepare"));
    const prepared = await client.prepare({
      task: "real TypeScript SDK lifecycle",
      mode: "planning",
    });
    const finalized = await client.finalize({
      request_id: prepared.request_id,
      use_memory: false,
      allowed_memory_ids: [],
      blocked_memory_ids: [],
      reason: "no applicable memory",
      risk: "none",
      recommended_injection: "none",
    });
    const completed = await client.complete({
      decision_id: finalized.decision_id,
      eval_result: "pass",
    });
    assert.equal(completed.request_id, prepared.request_id);
    const abandoned = await client.prepare({
      task: "abandoned TypeScript SDK lifecycle",
      mode: "planning",
    });
    const canceled = await client.cancel({
      request_id: abandoned.request_id,
    });
    assert.equal(canceled.canceled, true);
    const health = await client.health();
    assert.equal(health.pending_request_count, 0);
  } catch (error) {
    operationFailed = true;
    throw error;
  } finally {
    lines.close();
    if (child.stdin.writable && !child.stdin.destroyed) {
      child.stdin.end("x");
    }
    const code =
      child.exitCode === null ? (await once(child, "exit"))[0] : child.exitCode;
    if (!operationFailed) {
      assert.equal(code, 0, stderr);
    }
  }
});
