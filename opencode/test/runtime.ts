import { mkdtemp, readFile } from "node:fs/promises";
import { tmpdir } from "node:os";
import { join } from "node:path";

const ENV = {
  MEZMO_GOVERNANCE_URL: "https://api.example.test/governance/workflow/authorize",
  MEZMO_GOVERNANCE_TOKEN: "sts_test",
  MEZMO_GOVERNANCE_TIMEOUT_SECONDS: "30",
  MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS: "1",
};

export type FakeFetch = (url: string, init?: { body?: string }) => Promise<unknown>;

// Point the plugin at a fake Mezmo (a stubbed fetch) and a temporary audit log.
export async function withFakeMezmo(fetchImpl: FakeFetch, fn: (audit: string) => Promise<void>) {
  const dir = await mkdtemp(join(tmpdir(), "mezmo-gov-oc-"));
  const audit = join(dir, "audit.jsonl");
  const values: Record<string, string> = { ...ENV, MEZMO_GOVERNANCE_AUDIT_LOG: audit };
  const previous = new Map<string, string | undefined>();
  for (const [key, value] of Object.entries(values)) {
    previous.set(key, process.env[key]);
    process.env[key] = value;
  }
  const realFetch = globalThis.fetch;
  globalThis.fetch = fetchImpl as typeof fetch;
  try {
    await fn(audit);
  } finally {
    globalThis.fetch = realFetch;
    for (const [key, value] of previous) {
      if (value === undefined) {
        delete process.env[key];
      } else {
        process.env[key] = value;
      }
    }
  }
}

export function decision(body: unknown): FakeFetch {
  return async () => ({ status: 200, text: async () => JSON.stringify(body) });
}

export async function auditLines(path: string) {
  return (await readFile(path, "utf8"))
    .trim()
    .split("\n")
    .map((line) => JSON.parse(line));
}
