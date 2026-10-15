import assert from "node:assert/strict";
import { readdir } from "node:fs/promises";
import { dirname, join } from "node:path";
import test from "node:test";
import { fileURLToPath, pathToFileURL } from "node:url";

const ROOT = dirname(dirname(fileURLToPath(import.meta.url)));

// OpenCode 1.x loads every `{plugin,plugins}/*.{ts,js}` file as a plugin and
// throws "Plugin export is not a function" when any export of such a file is
// not a function (packages/opencode/src/plugin/index.ts, getLegacyPlugins).
// The README copies the top-level .ts files into that directory.
test("every top-level .ts file loads as an OpenCode 1.x plugin", async () => {
  const files = (await readdir(ROOT)).filter((name) => name.endsWith(".ts"));
  assert.ok(files.includes("mezmo-governance.ts"));
  for (const file of files) {
    const mod = (await import(pathToFileURL(join(ROOT, file)).href)) as Record<string, unknown>;
    for (const [name, value] of Object.entries(mod)) {
      assert.equal(typeof value, "function", `${file} export ${name} is not a function`);
    }
    // OpenCode calls every distinct exported function as a plugin factory.
    assert.equal(new Set(Object.values(mod)).size, 1, `${file} must export only the plugin`);
  }
});
