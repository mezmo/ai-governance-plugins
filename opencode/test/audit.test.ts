import assert from "node:assert/strict";
import { chmod, mkdtemp, stat, writeFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";
import test from "node:test";

import { appendAudit, resultFailed } from "../mezmo-governance/client.ts";

async function withLog(path: string, fn: () => Promise<void>) {
  const previous = process.env.MEZMO_GOVERNANCE_AUDIT_LOG;
  const umask = process.umask(0o022);
  process.env.MEZMO_GOVERNANCE_AUDIT_LOG = path;
  try {
    await fn();
  } finally {
    process.umask(umask);
    if (previous === undefined) {
      delete process.env.MEZMO_GOVERNANCE_AUDIT_LOG;
    } else {
      process.env.MEZMO_GOVERNANCE_AUDIT_LOG = previous;
    }
  }
}

const mode = async (path: string) => (await stat(path)).mode & 0o777;

test("the audit log holds tool output, so only its owner can read it", async () => {
  const dir = join(await mkdtemp(join(tmpdir(), "mezmo-gov-")), "logs");
  const path = join(dir, "audit.jsonl");
  await withLog(path, async () => {
    await appendAudit({ tool_name: "bash", status: "observed" });
    assert.equal((await mode(path)).toString(8), "600");
    assert.equal((await mode(dir)).toString(8), "700");
  });
});

test("an audit log an older version left readable is tightened", async () => {
  const path = join(await mkdtemp(join(tmpdir(), "mezmo-gov-")), "audit.jsonl");
  await writeFile(path, "");
  await chmod(path, 0o644);
  await withLog(path, async () => {
    await appendAudit({ tool_name: "bash", status: "observed" });
    assert.equal((await mode(path)).toString(8), "600");
  });
});

test("a nonzero exit code in either spelling marks the result failed", () => {
  assert.equal(resultFailed({ exit_code: 2 }), true);
  assert.equal(resultFailed({ exitCode: 2 }), true);
  assert.equal(resultFailed({ exitCode: 0 }), false);
  assert.equal(resultFailed({ exit_code: 0, exitCode: 2 }), false);
});
