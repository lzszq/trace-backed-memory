import assert from "node:assert/strict";
import { spawnSync } from "node:child_process";

assert.equal(typeof process.env.npm_execpath, "string");
const packed = spawnSync(
  process.execPath,
  [process.env.npm_execpath, "pack", "--dry-run", "--json"],
  {
  cwd: new URL("../", import.meta.url),
  encoding: "utf8",
  windowsHide: true,
  },
);
if (packed.status !== 0) {
  process.stderr.write(packed.stderr ?? String(packed.error ?? ""));
  process.exit(packed.status ?? 1);
}
const report = JSON.parse(packed.stdout);
assert.equal(report.length, 1);
const names = new Set(report[0].files.map((item) => item.path));
for (const required of [
  "LICENSE",
  "README.md",
  "package.json",
  "dist/index.js",
  "dist/index.d.ts",
  "dist/client.js",
  "dist/client.d.ts",
  "dist/durable-client.js",
  "dist/durable-client.d.ts",
  "dist/durable-errors.js",
  "dist/durable-errors.d.ts",
  "dist/durable-types.js",
  "dist/durable-types.d.ts",
  "dist/durable-validation.js",
  "dist/durable-validation.d.ts",
]) {
  assert.equal(names.has(required), true, `package is missing ${required}`);
}
for (const name of names) {
  assert.equal(name.startsWith("src/"), false);
  assert.equal(name.startsWith("tests/"), false);
  assert.equal(name.startsWith("node_modules/"), false);
}
process.stdout.write(`${report[0].filename}\n`);
