import assert from "node:assert/strict";
import { spawn } from "node:child_process";
import { once } from "node:events";
import { readFile } from "node:fs/promises";
import { createServer } from "node:http";
import { platform } from "node:os";
import { createInterface } from "node:readline";
import test from "node:test";
import { fileURLToPath } from "node:url";

import {
  DURABLE_AGENT_PROTOCOL_VERSION,
  DURABLE_HTTP_PROFILE,
  DurableAgentHTTPClient,
  DurableAgentHTTPError,
  durableSessionReference,
} from "../dist/index.js";

const TOKEN = `typescript_durable_sdk_${"a".repeat(32)}`;
const ROOT = new URL("../../../", import.meta.url);
const fixture = JSON.parse(
  await readFile(
    new URL("tests/fixtures/durable_client_lifecycle.json", ROOT),
    "utf8",
  ),
);

function durableJsonResponse(response, status, payload, protocol) {
  const body =
    typeof payload === "string"
      ? Buffer.from(payload, "utf8")
      : Buffer.from(JSON.stringify(payload), "utf8");
  response.writeHead(status, {
    "Content-Type": "application/json",
    "Content-Length": String(body.byteLength),
    "X-TBM-Protocol-Version": protocol ?? DURABLE_AGENT_PROTOCOL_VERSION,
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

async function withDurablePythonServer(callback) {
  const python =
    process.env.TBM_PYTHON ?? (platform() === "win32" ? "python" : "python3");
  const child = spawn(python, ["-m", "tests.typescript_durable_sdk_server"], {
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
  try {
    const [line] = await once(lines, "line");
    const configuration = JSON.parse(line);
    return await callback(
      new DurableAgentHTTPClient({
        baseUrl: configuration.base_url,
        token: configuration.token,
      }),
    );
  } finally {
    lines.close();
    child.stdin.end("x");
    const code =
      child.exitCode === null ? (await once(child, "exit"))[0] : child.exitCode;
    assert.equal(code, 0, stderr);
  }
}

test("durable client negotiates and completes a real Python lifecycle", async () => {
  await withDurablePythonServer(async (client) => {
    const capabilities = await client.negotiate();
    assert.equal(capabilities.protocol_version, DURABLE_AGENT_PROTOCOL_VERSION);
    assert.equal(capabilities.transport_profile, DURABLE_HTTP_PROFILE);
    assert.equal(capabilities.caller_identity_fields, false);
    assert.equal(capabilities.durable_sessions, true);
    assert.deepEqual(capabilities.process_local_records, []);
    assert.ok(capabilities.operations.includes("export_replay"));

    const openapi = await client.openapi();
    assert.equal(openapi.openapi, "3.1.0");
    const health = await client.health();
    assert.equal(health.durable_sessions, true);

    const prepared = await client.prepare(fixture.prepare);
    assert.equal(prepared.result.session.status, "prepared");
    const decided = await client.decide({
      ...durableSessionReference(prepared),
      ...fixture.decide,
    });
    assert.equal(decided.result.session.status, "decided");

    const finalized = await client.finalize({
      ...durableSessionReference(decided),
      lease_seconds: 1800,
    });
    assert.equal(finalized.result.session.status, "finalized");
    assert.equal(finalized.result.content_exposed, true);

    const started = await client.start(durableSessionReference(finalized));
    assert.equal(started.result.session.status, "executing");
    const resumed = await client.heartbeat({
      ...durableSessionReference(started),
      ...fixture.resume,
    });
    assert.equal(resumed.operation, "resume");
    assert.equal(resumed.result.session.status, "executing");

    const completionRequest = {
      ...durableSessionReference(resumed),
      ...fixture.complete,
    };
    const completed = await client.complete(completionRequest);
    assert.equal(completed.result.session.status, "completed");
    assert.equal(completed.result.outcome.result, "pass");
    assert.equal(completed.result.replayed, false);

    const replayedCompletion = await client.complete(completionRequest);
    assert.equal(replayedCompletion.result.replayed, true);
    assert.deepEqual(
      replayedCompletion.result.session,
      completed.result.session,
    );

    const current = await client.getSession({
      session_id: completed.result.session.session_id,
    });
    assert.deepEqual(current.result.session, completed.result.session);
    const replay = await client.exportReplay({
      ...durableSessionReference(completed),
      ...fixture.replay,
    });
    assert.equal(replay.result.content_exposed, true);
    assert.ok(replay.result.bundle.artifacts.length > 0);
  });
});

test("durable cancellation is exact and caller identity never enters JSON", async () => {
  await withDurablePythonServer(async (client) => {
    const forged = { ...fixture.prepare, tenant_id: "tenant_forged" };
    await assert.rejects(client.prepare(forged), (error) => {
      assert.ok(error instanceof DurableAgentHTTPError);
      assert.equal(error.code, "TBM_DURABLE_SDK_INVALID_INPUT");
      assert.equal(error.operation, "prepare");
      return true;
    });
    await assert.rejects(
      client.prepare({
        ...fixture.prepare,
        semantic_query: {
          ...fixture.prepare.semantic_query,
          vector: [0, 0],
        },
      }),
      (error) => {
        assert.equal(error.code, "TBM_DURABLE_SDK_INVALID_INPUT");
        return true;
      },
    );
    await assert.rejects(
      client.prepare({ ...fixture.prepare, attributes: null }),
      (error) => {
        assert.equal(error.code, "TBM_DURABLE_SDK_INVALID_INPUT");
        return true;
      },
    );

    const prepared = await client.prepare(fixture.prepare);
    await assert.rejects(
      client.decide({
        ...durableSessionReference(prepared),
        ...fixture.decide,
        final_allowed_revision_ids: null,
      }),
      (error) => {
        assert.equal(error.code, "TBM_DURABLE_SDK_INVALID_INPUT");
        return true;
      },
    );
    await assert.rejects(
      client.cancel({
        session_id: prepared.result.session.session_id,
        expected_session_version: prepared.result.session.version + 1,
        ...fixture.cancel,
      }),
      (error) => {
        assert.ok(error instanceof DurableAgentHTTPError);
        assert.equal(error.operation, "cancel");
        assert.equal(error.category, "state");
        assert.equal(error.status, 409);
        return true;
      },
    );
    const cancelRequest = {
      ...durableSessionReference(prepared),
      ...fixture.cancel,
    };
    const canceled = await client.cancel(cancelRequest);
    const replayed = await client.cancel(cancelRequest);
    assert.equal(canceled.result.session.status, "canceled");
    assert.equal(canceled.result.replayed, false);
    assert.equal(replayed.result.replayed, true);
    assert.deepEqual(replayed.result.session, canceled.result.session);
  });
});

test("durable retries are bounded, opt-in, and reuse the exact operation", async () => {
  let requests = 0;
  await withServer(
    (_request, response) => {
      requests += 1;
      if (requests === 1) {
        durableJsonResponse(response, 503, {
          protocol_version: DURABLE_AGENT_PROTOCOL_VERSION,
          error: {
            code: "TBM_DURABLE_HTTP_UNAVAILABLE",
            category: "persistence",
            message: "service is temporarily unavailable",
            operation: "health",
            retryable: true,
          },
        });
        return;
      }
      durableJsonResponse(response, 200, {
        protocol_version: DURABLE_AGENT_PROTOCOL_VERSION,
        status: "ok",
        storage_mode: "sqlite",
        durable_sessions: true,
        process_local_records: [],
      });
    },
    async (baseUrl) => {
      const client = new DurableAgentHTTPClient({
        baseUrl,
        token: TOKEN,
        maxAttempts: 2,
      });
      const health = await client.health();
      assert.equal(health.status, "ok");
    },
  );
  assert.equal(requests, 2);
});

test("durable errors, protocol versions, and result shapes fail closed", async () => {
  await withServer(
    (_request, response) =>
      durableJsonResponse(response, 409, {
        protocol_version: DURABLE_AGENT_PROTOCOL_VERSION,
        error: {
          code: "TBM_DURABLE_GATE_SESSION_STALE",
          category: "state",
          message: "session version is stale",
          operation: "start",
          retryable: false,
        },
      }),
    async (baseUrl) => {
      const client = new DurableAgentHTTPClient({ baseUrl, token: TOKEN });
      await assert.rejects(client.health(), (error) => {
        assert.ok(error instanceof DurableAgentHTTPError);
        assert.equal(error.code, "TBM_DURABLE_SDK_RESPONSE_INVALID");
        return true;
      });
    },
  );

  await withServer(
    (_request, response) =>
      durableJsonResponse(
        response,
        200,
        {
          protocol_version: DURABLE_AGENT_PROTOCOL_VERSION,
          status: "ok",
          storage_mode: "sqlite",
          durable_sessions: true,
          process_local_records: [],
        },
        "tbm.durable-agent-wire.v2",
      ),
    async (baseUrl) => {
      const client = new DurableAgentHTTPClient({ baseUrl, token: TOKEN });
      await assert.rejects(client.health(), (error) => {
        assert.equal(error.code, "TBM_DURABLE_SDK_RESPONSE_INVALID");
        return true;
      });
    },
  );

  await withServer(
    (_request, response) =>
      durableJsonResponse(response, 200, {
        protocol_version: DURABLE_AGENT_PROTOCOL_VERSION,
        operation: "prepare",
        result: {
          authorization_event_id: "authorization_001",
          session: {
            session_id: "session_001",
            version: 1,
            status: "prepared",
          },
          retrieval_snapshot: {},
          system_gate_evaluation: {},
          retrieval_policy: {},
          unexpected: true,
        },
      }),
    async (baseUrl) => {
      const client = new DurableAgentHTTPClient({ baseUrl, token: TOKEN });
      await assert.rejects(client.prepare(fixture.prepare), (error) => {
        assert.equal(error.code, "TBM_DURABLE_SDK_RESPONSE_INVALID");
        return true;
      });
    },
  );
});

test("durable client rejects unsafe URL, retry, TLS, and abort options", async () => {
  for (const baseUrl of [
    "http://localhost:8766",
    "http://192.0.2.1:8766",
    "http://127.0.0.1",
    "http://127.0.0.1:8766/path",
    "ftp://127.0.0.1:8766",
  ]) {
    assert.throws(
      () => new DurableAgentHTTPClient({ baseUrl, token: TOKEN }),
      TypeError,
    );
  }
  assert.throws(
    () =>
      new DurableAgentHTTPClient({
        baseUrl: "http://127.0.0.1:8766",
        token: TOKEN,
        ca: "certificate",
      }),
    /TLS options/,
  );
  for (const maxAttempts of [0, 6, 1.5]) {
    assert.throws(
      () =>
        new DurableAgentHTTPClient({
          baseUrl: "http://127.0.0.1:8766",
          token: TOKEN,
          maxAttempts,
        }),
      /maxAttempts/,
    );
  }
  assert.doesNotThrow(
    () =>
      new DurableAgentHTTPClient({
        baseUrl: "http://127.0.0.1:8766",
        token: "a".repeat(8185),
      }),
  );
  assert.throws(
    () =>
      new DurableAgentHTTPClient({
        baseUrl: "http://127.0.0.1:8766",
        token: "a".repeat(8186),
      }),
    /token/,
  );

  const controller = new AbortController();
  controller.abort();
  const client = new DurableAgentHTTPClient({
    baseUrl: "http://127.0.0.1:8766",
    token: TOKEN,
  });
  await assert.rejects(
    client.health({ signal: controller.signal }),
    (error) => {
      assert.equal(error.code, "TBM_DURABLE_SDK_TRANSPORT_ERROR");
      assert.equal(error.retryable, true);
      return true;
    },
  );
});

test("durable client preserves abort semantics while streaming a response", async () => {
  await withServer(
    (_request, response) => {
      response.writeHead(200, {
        "Content-Type": "application/json",
        "Content-Length": "1024",
        "X-TBM-Protocol-Version": DURABLE_AGENT_PROTOCOL_VERSION,
        Connection: "close",
      });
      response.write("{");
    },
    async (baseUrl) => {
      const controller = new AbortController();
      const client = new DurableAgentHTTPClient({
        baseUrl,
        token: TOKEN,
      });
      const timer = setTimeout(() => controller.abort(), 25);
      try {
        await assert.rejects(
          client.health({ signal: controller.signal }),
          (error) => {
            assert.ok(error instanceof DurableAgentHTTPError);
            assert.equal(error.code, "TBM_DURABLE_SDK_TRANSPORT_ERROR");
            assert.equal(error.retryable, true);
            return true;
          },
        );
      } finally {
        clearTimeout(timer);
      }
    },
  );

  await withServer(
    (_request, response) => {
      response.writeHead(200, {
        "Content-Type": "application/json",
        "Content-Length": "1024",
        "X-TBM-Protocol-Version": DURABLE_AGENT_PROTOCOL_VERSION,
        Connection: "close",
      });
      response.write("{");
    },
    async (baseUrl) => {
      const client = new DurableAgentHTTPClient({
        baseUrl,
        token: TOKEN,
        timeoutMs: 25,
      });
      await assert.rejects(client.health(), (error) => {
        assert.ok(error instanceof DurableAgentHTTPError);
        assert.equal(error.code, "TBM_DURABLE_SDK_TRANSPORT_ERROR");
        assert.equal(error.retryable, true);
        return true;
      });
    },
  );
});
