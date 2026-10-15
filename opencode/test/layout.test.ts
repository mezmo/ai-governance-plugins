import assert from "node:assert/strict";
import { readdir } from "node:fs/promises";
import { dirname, join, parse } from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

async function topLevelPlugins() {
  const files = (await readdir(ROOT)).filter((name) => name.endsWith(".ts"));
  assert.deepEqual(files, ["mezmo-governance.ts"]);
  return Promise.all(
    files.map(async (file) => ({
      file,
      mod: (await import(pathToFileURL(join(ROOT, file)).href)) as Record<string, unknown>,
    })),
  );
}

// OpenCode 1.19.0 loads every `{plugin,plugins}/*.{ts,js}` file
// (packages/opencode/src/plugin/index.ts applyPlugin, shared.ts readV1Plugin).
// A default export object with `id` takes the V1 module path: `server` must
// be a function, `tui` absent, and a path plugin needs a non-empty `id`.
// Named exports are then ignored.
test("each top-level .ts file loads as an OpenCode 1.x path plugin", async () => {
  for (const { file, mod } of await topLevelPlugins()) {
    const value = mod.default as Record<string, unknown>;
    assert.equal(typeof value, "object", file);
    assert.equal(typeof value.id, "string", file);
    assert.ok((value.id as string).trim(), file);
    assert.equal(typeof value.server, "function", file);
    assert.equal("tui" in value, false, file);
  }
});

// OpenCode 2.0.25 (packages/core/src/plugin/module.ts) decodes the default
// export as { id: string, setup: function } or { id, effect }.
test("each top-level .ts file loads as an OpenCode 2.x plugin", async () => {
  for (const { file, mod } of await topLevelPlugins()) {
    const value = mod.default as Record<string, unknown>;
    assert.equal(typeof value.id, "string", file);
    assert.equal(typeof value.setup, "function", file);
  }
});

// OpenCode 2.x also treats each folder in `plugins/` as a plugin and loads its
// `server` or `index` file (packages/plugin/src/host.ts). The helper folder
// must have neither, or 2.x would load a second plugin from it.
test("the helper folder has no OpenCode 2.x entrypoint", async () => {
  for (const name of await readdir(join(ROOT, "mezmo-governance"))) {
    assert.ok(!["server", "index"].includes(parse(name).name), name);
  }
});
