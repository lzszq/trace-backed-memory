import assert from "node:assert/strict";
import { readFile } from "node:fs/promises";

const root = new URL("../../../", import.meta.url);
const openapi = JSON.parse(
  await readFile(new URL("schemas/agent-http-v1.openapi.json", root), "utf8"),
);
assert.equal(openapi.openapi, "3.1.0");
assert.equal(
  openapi.jsonSchemaDialect,
  "https://json-schema.org/draft/2020-12/schema",
);
assert.deepEqual(Object.keys(openapi.paths).sort(), [
  "/v1/cancel",
  "/v1/capabilities",
  "/v1/complete",
  "/v1/finalize",
  "/v1/health",
  "/v1/prepare",
]);
assert.equal(openapi["x-tbm-transport"].scope, "single-user, single-host, IPv4 loopback only");
assert.equal(openapi["x-tbm-transport"].requestBodyMaxBytes, 8_388_608);
assert.equal(openapi["x-tbm-transport"].responseBodyMaxBytes, 8_388_608);
assert.equal(openapi["x-tbm-transport"].pendingRequestsAreProcessLocal, true);

const packageMetadata = JSON.parse(
  await readFile(new URL("../package.json", import.meta.url), "utf8"),
);
assert.equal(packageMetadata.dependencies, undefined);
assert.deepEqual(packageMetadata.engines, { node: ">=20" });

const durableClient = await readFile(
  new URL("../src/durable-client.ts", import.meta.url),
  "utf8",
);
const durableTypes = await readFile(
  new URL("../src/durable-types.ts", import.meta.url),
  "utf8",
);
assert.equal(durableTypes.includes('"tbm.durable-agent-wire.v1"'), true);
assert.equal(durableTypes.includes('"durable-v3"'), true);
for (const route of ["capabilities", "openapi", "health"]) {
  assert.equal(
    durableClient.includes(`/durable/v1/${route}`),
    true,
    `durable client is missing ${route}`,
  );
}
for (const operation of [
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
]) {
  assert.equal(
    durableTypes.includes(`| "${operation}"`) ||
      durableTypes.includes(`readonly ${operation}:`),
    true,
    `durable contract is missing ${operation}`,
  );
}
assert.equal(durableClient.includes('operation.replaceAll("_", "-")'), true);
