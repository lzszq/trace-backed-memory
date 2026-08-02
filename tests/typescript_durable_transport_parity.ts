import { readFile } from "node:fs/promises";

import {
  DurableAgentHTTPClient,
  durableSessionReference,
} from "../packages/typescript-sdk/src/index.ts";

const [baseUrl, token, fixturePath] = process.argv.slice(2);
if (baseUrl === undefined || token === undefined || fixturePath === undefined) {
  throw new Error("durable parity arguments are required");
}

const fixture = JSON.parse(await readFile(fixturePath, "utf8"));
const client = new DurableAgentHTTPClient({ baseUrl, token });
await client.negotiate();
const prepared = await client.prepare(fixture.prepare);
const evaluation = prepared.result.system_gate_evaluation as {
  decisions: Array<{
    memory_revision_id: string;
    outcome: string;
  }>;
};
const allowedRevisionIds = evaluation.decisions
  .filter((decision) => decision.outcome === "allowed")
  .map((decision) => decision.memory_revision_id);
const decided = await client.decide({
  ...durableSessionReference(prepared),
  ...fixture.decide,
  final_allowed_revision_ids: allowedRevisionIds,
});
const finalized = await client.finalize({
  ...durableSessionReference(decided),
  lease_seconds: 1800,
});
const started = await client.start(durableSessionReference(finalized));
const resumed = await client.heartbeat({
  ...durableSessionReference(started),
  ...fixture.resume,
});
const completed = await client.complete({
  ...durableSessionReference(resumed),
  ...fixture.complete,
});
process.stdout.write(`${completed.result.session.session_id}\n`);
