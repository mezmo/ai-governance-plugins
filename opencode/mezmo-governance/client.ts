import { AsyncLocalStorage } from "node:async_hooks";
import { createHash } from "node:crypto";
import { appendFile, mkdir } from "node:fs/promises";
import { homedir } from "node:os";
import { dirname, join } from "node:path";

export const AGENT = "opencode";
const DEFAULT_TIMEOUT_SECONDS = 590;
// Finish before a runtime's 600 second hook timeout, whatever is configured.
export const MAX_TIMEOUT_SECONDS = 590;
const DEFAULT_POLL_INTERVAL_SECONDS = 5;
export const MIN_POLL_INTERVAL_SECONDS = 1;
const MAX_RESULT_PREVIEW = 2000;
const MAX_REQUEST_ID = 128;
const NAMESPACE_URL = Buffer.from("6ba7b8119dad11d180b400c04fd430c8", "hex");

export type Decision = { approved: boolean; reason: string | null };

export type FetchLike = (
  input: string,
  init?: {
    method?: string;
    headers?: Record<string, string>;
    body?: string;
    signal?: AbortSignal;
    redirect?: "manual";
  },
) => Promise<{
  status: number;
  text(): Promise<string>;
}>;

export type Clock = () => number;
export type Sleeper = (ms: number) => Promise<void>;

export class GovernanceError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "GovernanceError";
  }
}

export function uuid5(name: string): string {
  const hash = createHash("sha1")
    .update(Buffer.concat([NAMESPACE_URL, Buffer.from(name)]))
    .digest();
  hash[6] = (hash[6] & 0x0f) | 0x50;
  hash[8] = (hash[8] & 0x3f) | 0x80;
  const hex = hash.subarray(0, 16).toString("hex");
  return `${hex.slice(0, 8)}-${hex.slice(8, 12)}-${hex.slice(12, 16)}-${hex.slice(16, 20)}-${hex.slice(20, 32)}`;
}

// Settings handed to the plugin directly (OpenCode plugin options), keyed by
// the variable names. They win over the environment, which a background
// service does not share with the shell that started the agent. Scoped to one
// call, so two plugin instances with different options never mix.
const scoped = new AsyncLocalStorage<Record<string, string>>();

export function withSettings<T>(values: Record<string, unknown> | undefined, fn: () => T): T {
  const settings: Record<string, string> = {};
  for (const [name, value] of Object.entries(values ?? {})) {
    if (name.startsWith("MEZMO_GOVERNANCE_") && (typeof value === "string" || typeof value === "number")) {
      settings[name] = String(value);
    }
  }
  return scoped.run(settings, fn);
}

function setting(name: string): string | undefined {
  return scoped.getStore()?.[name] ?? process.env[name];
}

function requiredEnv(name: string): string {
  const value = setting(name)?.trim();
  if (!value) {
    throw new GovernanceError(`${name} is required`);
  }
  return value;
}

export function authorizeUrl(): string {
  return requiredEnv("MEZMO_GOVERNANCE_URL");
}

export function authorizationHeader(): string {
  const key = requiredEnv("MEZMO_GOVERNANCE_TOKEN");
  if (/^(token|bearer) /i.test(key)) {
    return key;
  }
  return `Token ${key}`;
}

function positiveSeconds(name: string, fallback: number): number {
  const configured = setting(name);
  if (configured === undefined) {
    return fallback;
  }
  const parsed = Number(configured);
  if (!Number.isFinite(parsed) || parsed <= 0) {
    throw new GovernanceError(`${name} must be a finite number greater than zero`);
  }
  return parsed;
}

export function timeoutSeconds(): number {
  return Math.min(
    positiveSeconds("MEZMO_GOVERNANCE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
    MAX_TIMEOUT_SECONDS,
  );
}

export function pollIntervalSeconds(): number {
  return Math.max(
    positiveSeconds("MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS),
    MIN_POLL_INTERVAL_SECONDS,
  );
}

export function auditLogPath(): string {
  return (
    setting("MEZMO_GOVERNANCE_AUDIT_LOG")?.trim() ||
    join(homedir(), ".mezmo", "governance-audit.jsonl")
  );
}

export function statusUrl(decisionId: string, base = authorizeUrl()): string {
  const url = new URL(base);
  url.searchParams.set("decision_id", decisionId);
  return url.toString();
}

export function splitToolName(toolName: string): { namespace: string; name: string } {
  if (toolName.startsWith("mcp__")) {
    const parts = toolName.split("__");
    if (parts.length !== 3 || !parts[1] || !parts[2]) {
      throw new GovernanceError("MCP tool names must use mcp__<server>__<tool>");
    }
    return { namespace: parts[1], name: parts[2] };
  }
  return { namespace: AGENT, name: toolName };
}

export function argumentsObject(input: unknown): Record<string, unknown> {
  if (input && typeof input === "object" && !Array.isArray(input)) {
    return { ...(input as Record<string, unknown>) };
  }
  throw new GovernanceError("tool arguments must be a JSON object");
}

// JSON with sorted keys, so one call always hashes the same.
function canonicalJson(value: unknown): string {
  return JSON.stringify(value, (_key, item) =>
    item && typeof item === "object" && !Array.isArray(item)
      ? Object.fromEntries(Object.keys(item).sort().map((key) => [key, item[key]]))
      : item,
  );
}

// Hash the exact call, so an edited call never reuses an earlier decision.
export function callDigest(toolName: string, args: unknown): string {
  const tool = splitToolName(toolName);
  return createHash("sha256")
    .update(canonicalJson([tool.namespace, tool.name, args]))
    .digest("hex")
    .slice(0, 16);
}

export function callIdentity(sessionId: string, toolCallId: string, toolName: string, args: unknown) {
  const digest = callDigest(toolName, args);
  const fullId = `${AGENT}:${sessionId}:${toolCallId}#${digest}`;
  // Mezmo answers HTTP 400 to a longer request_id. The decision id hashes the full id,
  // so a cut id still gets its own decision.
  const requestId =
    fullId.length <= MAX_REQUEST_ID ? fullId : `${fullId.slice(0, MAX_REQUEST_ID - digest.length - 1)}#${digest}`;
  return {
    sessionId,
    requestId,
    decisionId: uuid5(fullId),
  };
}

export function approvalPayload(params: {
  sessionId: string;
  toolCallId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  eventName?: string;
}) {
  const identity = callIdentity(params.sessionId, params.toolCallId, params.toolName, params.arguments);
  const tool = splitToolName(params.toolName);
  return {
    version: 1,
    decision_id: identity.decisionId,
    request_id: identity.requestId,
    scope: { kind: "single", session_id: identity.sessionId },
    origin: {
      kind: "opencode_plugin",
      reason: params.eventName ?? "tool.execute.before",
    },
    items: [
      {
        id: 0,
        tool_namespace: tool.namespace,
        tool_name: tool.name,
        arguments: params.arguments,
      },
    ],
  };
}

async function parseObject(response: { status: number; text(): Promise<string> }) {
  const raw = await response.text();
  if (!raw) {
    return {};
  }
  try {
    const parsed = JSON.parse(raw);
    if (parsed && typeof parsed === "object" && !Array.isArray(parsed)) {
      return parsed as Record<string, unknown>;
    }
  } catch {
    throw new GovernanceError("Mezmo Governance returned an invalid JSON response");
  }
  throw new GovernanceError("Mezmo Governance returned a non-object response");
}

export async function httpJson(
  method: string,
  url: string,
  payload: Record<string, unknown> | null,
  timeoutMs: number,
  fetchImpl: FetchLike,
): Promise<{ status: number; body: Record<string, unknown> }> {
  const headers: Record<string, string> = {
    Authorization: authorizationHeader(),
    Accept: "application/json",
  };
  if (payload !== null) {
    headers["Content-Type"] = "application/json";
  }
  const controller = new AbortController();
  const timer = setTimeout(() => controller.abort(), timeoutMs);
  try {
    const response = await fetchImpl(url, {
      method,
      headers,
      body: payload === null ? undefined : JSON.stringify(payload),
      signal: controller.signal,
      // A 3xx comes back as itself and denies, so the token never goes
      // to another URL.
      redirect: "manual",
    });
    return { status: response.status, body: await parseObject(response) };
  } catch (error) {
    if (controller.signal.aborted || (error instanceof Error && error.name === "AbortError")) {
      throw new TimeoutError("Mezmo Governance request timed out");
    }
    throw new GovernanceError(
      `Mezmo Governance is unavailable: ${error instanceof Error ? error.message : String(error)}`,
    );
  } finally {
    clearTimeout(timer);
  }
}

export class TimeoutError extends Error {
  constructor(message: string) {
    super(message);
    this.name = "TimeoutError";
  }
}

const MAX_ERROR_DETAIL = 200;

// Mezmo's own error text on one bounded line, or "" when there is none.
function errorDetail(value: unknown): string {
  if (typeof value !== "string") {
    return "";
  }
  return value.replace(/[\p{C}\p{Z}]+/gu, " ").trim().slice(0, MAX_ERROR_DETAIL);
}

// Name the status and, on a 4xx, Mezmo's code and message; nothing else.
function httpFailure(status: number, body: Record<string, unknown>, when = ""): string {
  const detailed = status >= 400 && status < 500;
  const code = detailed ? errorDetail(body.code) : "";
  const message = detailed ? errorDetail(body.message) : "";
  return (
    `Mezmo Governance returned HTTP ${status}` +
    (code ? ` ${code}` : "") +
    when +
    (message ? `: ${message}` : "")
  );
}

function remainingMs(deadline: number, now: Clock): number {
  const leftover = deadline - now();
  if (leftover <= 0) {
    throw new GovernanceError("Mezmo Governance: approval still pending");
  }
  return leftover;
}

// A response that lands after the deadline does not count, even an approval:
// the caller may already have given up on this call.
function inTime(deadline: number, now: Clock) {
  if (now() >= deadline) {
    throw new GovernanceError("Mezmo Governance answered after the deadline");
  }
}

function decisionFromPost(body: Record<string, unknown>): Decision {
  if (typeof body.approved !== "boolean") {
    throw new GovernanceError("Mezmo Governance did not return an approval decision");
  }
  return {
    approved: body.approved,
    reason: typeof body.reason === "string" ? body.reason : null,
  };
}

function decisionFromStatus(body: Record<string, unknown>): Decision | null {
  let status = body.status;
  const reason = typeof body.reason === "string" ? body.reason : null;
  // Live Mezmo answers a decided poll with HTTP 200 and the POST body shape.
  if (status === undefined && typeof body.approved === "boolean") {
    status = body.approved ? "approved" : "denied";
  }
  if (status === "approved") {
    return { approved: true, reason };
  }
  if (status === "denied") {
    return { approved: false, reason: reason ?? "Policy denied the call." };
  }
  if (status === "pending") {
    return null;
  }
  throw new GovernanceError(`Mezmo Governance returned an unexpected status: ${String(status)}`);
}

async function pollDecision(
  decisionId: string,
  deadline: number,
  fetchImpl: FetchLike,
  now: Clock,
  sleep: Sleeper,
): Promise<Decision> {
  const url = statusUrl(decisionId);
  const intervalMs = pollIntervalSeconds() * 1000;
  while (true) {
    const leftover = remainingMs(deadline, now);
    let status: number;
    let body: Record<string, unknown>;
    try {
      ({ status, body } = await httpJson("GET", url, null, leftover, fetchImpl));
    } catch (error) {
      if (error instanceof TimeoutError) {
        throw new GovernanceError(`Mezmo Governance is unavailable: ${error.message}`);
      }
      throw error;
    }
    inTime(deadline, now);
    // 404: not recorded yet. 207: Mezmo still waits for a person.
    if (status === 404 || status === 207) {
      await sleep(Math.min(intervalMs, leftover));
      continue;
    }
    if (status !== 200) {
      throw new GovernanceError(httpFailure(status, body, " while polling"));
    }
    const decided = decisionFromStatus(body);
    if (decided) {
      return decided;
    }
    const wait = Math.min(intervalMs, remainingMs(deadline, now));
    await sleep(wait);
  }
}

export async function authorize(
  payload: Record<string, unknown>,
  options: {
    fetchImpl?: FetchLike;
    now?: Clock;
    sleep?: Sleeper;
  } = {},
): Promise<Decision> {
  const fetchImpl = options.fetchImpl ?? fetch;
  const now = options.now ?? Date.now;
  const sleep = options.sleep ?? ((ms) => new Promise((resolve) => setTimeout(resolve, ms)));
  const deadline = now() + timeoutSeconds() * 1000;
  const leftover = remainingMs(deadline, now);
  let status: number;
  let body: Record<string, unknown>;
  try {
    ({ status, body } = await httpJson(
      "POST",
      authorizeUrl(),
      payload,
      leftover,
      fetchImpl,
    ));
  } catch (error) {
    if (error instanceof TimeoutError) {
      return pollDecision(String(payload.decision_id), deadline, fetchImpl, now, sleep);
    }
    throw error;
  }
  inTime(deadline, now);
  // 207: sent for approval; its approved: false is not the decision.
  if (status === 207 || status === 409) {
    return pollDecision(String(payload.decision_id), deadline, fetchImpl, now, sleep);
  }
  if (status !== 200) {
    throw new GovernanceError(httpFailure(status, body));
  }
  return decisionFromPost(body);
}

export function resultPreview(value: unknown): string {
  // JSON.stringify gives "{}" for an Error: its fields are not enumerable.
  const shown = value instanceof Error ? { name: value.name, message: value.message } : value;
  try {
    return JSON.stringify(shown).slice(0, MAX_RESULT_PREVIEW);
  } catch {
    return String(value).slice(0, MAX_RESULT_PREVIEW);
  }
}

export function resultFailed(value: unknown): boolean {
  if (!value || typeof value !== "object" || Array.isArray(value)) {
    return false;
  }
  const record = value as Record<string, unknown>;
  if (record.isError === true || record.is_error === true) {
    return true;
  }
  return typeof record.exit_code === "number" && record.exit_code !== 0;
}

export async function appendAudit(record: Record<string, unknown>): Promise<void> {
  const path = auditLogPath();
  await mkdir(dirname(path), { recursive: true });
  await appendFile(path, `${JSON.stringify(record)}\n`, "utf8");
}
