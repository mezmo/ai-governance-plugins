import { AsyncLocalStorage } from "node:async_hooks";
import { createHash } from "node:crypto";
import { mkdir, open } from "node:fs/promises";
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

// The token travels in a header: plain http only to this machine, such as
// tools/mock_governance.py.
const LOOPBACK = new Set(["127.0.0.1", "localhost", "[::1]"]);

export function authorizeUrl(): string {
  const value = requiredEnv("MEZMO_GOVERNANCE_URL");
  let url: URL;
  try {
    url = new URL(value);
  } catch {
    throw new GovernanceError("MEZMO_GOVERNANCE_URL is not a valid URL");
  }
  if (url.protocol !== "https:" && !(url.protocol === "http:" && LOOPBACK.has(url.hostname))) {
    throw new GovernanceError("MEZMO_GOVERNANCE_URL must use https, or http to this machine");
  }
  return value;
}

export function authorizationHeader(): string {
  const key = requiredEnv("MEZMO_GOVERNANCE_TOKEN");
  const scheme = /^(token|bearer) /i.exec(key)?.[0] ?? "";
  // fetch quotes a header value it refuses, so no error may reach it.
  if (!/^[\x21-\x7e]+$/.test(key.slice(scheme.length))) {
    throw new GovernanceError("MEZMO_GOVERNANCE_TOKEN must be printable ASCII without spaces");
  }
  return scheme ? key : `Token ${key}`;
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
  // A hash of the whole head keeps two cut ids apart.
  const head = fullId.slice(0, -digest.length - 1);
  const tail = `~${createHash("sha256").update(head).digest("hex").slice(0, 16)}#${digest}`;
  const requestId = fullId.length <= MAX_REQUEST_ID ? fullId : head.slice(0, MAX_REQUEST_ID - tail.length) + tail;
  return {
    sessionId,
    requestId,
    decisionId: uuid5(fullId),
  };
}

// Shell command parts
//
// A shell call goes to Mezmo as one item per part of its command, with the part's program,
// positional arguments and flags as fields a policy rule can match. The parser only ever leans
// one way: a part it can't fully read gets no program, arguments or flags, so a rule that allows
// by them can't match it, and a command it can't even split is sent whole, as before.
//
// It is a port of Claude Code's parser (claude-code/scripts/governance_hook.py), which splits
// where Claude Code splits commands for its own Bash permission rules (on &&, ||, ;, |, |&, &
// and newlines) and skips the wrappers those rules strip. Like those rules, it can't see through
// sh -c, scripts, aliases, or an option whose value is a separate word (the prod in kubectl -n
// prod lands among the arguments). claude-code/tests/bash_parts_cases.json holds what Claude
// Code makes of each test command, and this port must make the same. Python's \d, \s, strip(),
// isdigit() and isalpha() are Unicode-wide, so the patterns below spell them out.

// Mezmo takes up to 100 items a request. A command with more parts goes whole, as before.
const MAX_BASH_PARTS = 50;
// Each argument is a row in Mezmo, and rules rarely look past the first few.
const MAX_BASH_ARGS = 10;
// The service's longest argument name (ai-governance-service schema/constants.js).
const ARGUMENT_NAME_MAX = 64;
// A part after one of these runs once the part before it has finished.
const SEQUENTIAL_SEPARATORS = new Set(["&&", "||", ";", "\n"]);
// Words the shell reads as syntax rather than a program to run.
const SHELL_KEYWORDS = new Set([
  "!", "{", "}", "[[", "]]", "case", "do", "done", "elif", "else", "esac", "fi", "for",
  "function", "if", "in", "select", "then", "until", "while",
]);
const SIMPLE_WRAPPERS = new Set(["builtin", "command", "nohup", "time", "xargs"]);
// Python's whitespace (str.isspace(), and \s in its patterns), as a character class body.
// JavaScript's trim() and \s differ: they take U+FEFF and leave out U+001C-U+001F and U+0085.
// Generated in Python 3.14 with [hex(c) for c in range(0x110000) if chr(c).isspace()].
const PYTHON_SPACE = String.raw`\t-\r\x1c-\x20\x85\xa0\u1680\u2000-\u200a\u2028\u2029\u202f\u205f\u3000`;
const SPACE = new RegExp(`[${PYTHON_SPACE}]`);
// Python's str.isdigit(), which is true for \p{Nd} and also for the characters whose
// Numeric_Type is Digit, such as U+00B2 and U+2460. Those are listed after \p{Nd}, generated in
// Python 3.14 with [hex(c) for c in range(0x110000) if chr(c).isdigit() and not chr(c).isdecimal()].
const DIGITS =
  /^[\p{Nd}\xb2\xb3\xb9\u1369-\u1371\u19da\u2070\u2074-\u2079\u2080-\u2089\u2460-\u2468\u2474-\u247c\u2488-\u2490\u24ea\u24f5-\u24fd\u24ff\u2776-\u277e\u2780-\u2788\u278a-\u2792\u{10a40}-\u{10a43}\u{10e60}-\u{10e68}\u{11052}-\u{1105a}\u{1f100}-\u{1f10a}]+$/u;
const ASSIGNMENT = /^[A-Za-z_][A-Za-z0-9_]*\+?=/;
const GLOB = /[*?[]/;
// Python's \d is \p{Nd}; JavaScript's is ASCII only.
const DURATION = /^\p{Nd}+(\.\p{Nd}+)?[smhd]?$/u;
const NICENESS = /^-?\p{Nd}+$/u;
const STDBUF_OPTION = new RegExp(`^(?:-[ioe][^${PYTHON_SPACE}]+|--(input|output|error)=[^${PYTHON_SPACE}]+)$`, "u");
const FILE_DESCRIPTOR = /^(?:\p{Nd}+|-)$/u;
const LETTERS = /^[A-Za-z]+$/;
// Longest first, so <<< isn't read as <<.
const REDIRECTIONS = ["&>>", "&>", "<<<", "<<", "<>", "<&", "<(", "<", ">>", ">|", ">&", ">(", ">"];

type FlagValue = boolean | string;

export type BashPart = {
  text: string; // the part as written
  program: string | null; // null when the part can't be fully read
  args: string[]; // positional arguments, flags left out
  flags: [string, FlagValue][]; // [name, true or the value written with it]
  after: string | null; // the separator before it, null for the first part
};

// The command can't be split safely: an unclosed quote, a substitution, a heredoc or a final backslash.
class Unsplittable extends Error {}

// The command's parts, or null when it can't be split safely or holds nothing to run.
export function splitCommand(command: string): BashPart[] | null {
  let parts: BashPart[];
  try {
    parts = new Scanner(command).run();
  } catch (error) {
    if (error instanceof Unsplittable) {
      return null;
    }
    throw error;
  }
  return parts.length > 0 ? parts : null;
}

// Python's str.strip(). Its whitespace is all in the BMP, so one UTF-16 unit is one character.
function strip(text: string): string {
  let start = 0;
  let end = text.length;
  while (start < end && SPACE.test(text[start])) {
    start += 1;
  }
  while (end > start && SPACE.test(text[end - 1])) {
    end -= 1;
  }
  return text.slice(start, end);
}

// Drop the wrappers Claude Code strips, in the forms whose options are certain.
function skipWrappers(words: string[]): string[] {
  while (words.length > 0) {
    const [head, ...rest] = words;
    if (SIMPLE_WRAPPERS.has(head) && rest.length > 0 && !rest[0].startsWith("-")) {
      words = rest;
    } else if (head === "nice" && rest.length > 0) {
      if (rest[0] === "-n" && rest.length > 2 && NICENESS.test(rest[1])) {
        words = rest.slice(2);
      } else if (/^-\p{Nd}+$/u.test(rest[0]) && rest.length > 1) {
        words = rest.slice(1);
      } else if (!rest[0].startsWith("-")) {
        words = rest;
      } else {
        break;
      }
    } else if (head === "timeout" && rest.length > 1 && DURATION.test(rest[0])) {
      words = rest.slice(1);
    } else if (head === "stdbuf") {
      let count = 0;
      while (count < rest.length && STDBUF_OPTION.test(rest[count])) {
        count += 1;
      }
      if (count < rest.length && !rest[count].startsWith("-")) {
        words = rest.slice(count);
      } else {
        break;
      }
    } else {
      break;
    }
  }
  return words;
}

// The flags one word sets. Without knowing the tool, it reads them the common ways.
function flagsOf(word: string): [string, FlagValue][] {
  if (word.startsWith("--")) {
    const body = word.slice(2);
    const equals = body.indexOf("=");
    const name = equals < 0 ? body : body.slice(0, equals);
    return name ? [[name, equals < 0 ? true : body.slice(equals + 1)]] : [];
  }
  const body = word.slice(1);
  if (LETTERS.test(body)) {
    const flags: [string, FlagValue][] = [...body].map((letter) => [letter, true]);
    if (body.length > 2) {
      flags.push([body, true]); // find, go and java take long options after one dash
    }
    return flags;
  }
  if (/^\p{L}/u.test(body)) {
    const [letter, ...value] = [...body]; // by code point, as Python indexes
    return [[letter, value.join("")]]; // head -n5
  }
  return [[body, true]]; // kill -9
}

function readPart(words: string[]): [string | null, string[], [string, FlagValue][]] {
  words = skipWrappers(words);
  if (words.length === 0) {
    return [null, [], []];
  }
  const program = words[0];
  // An assignment changes what runs (PATH=/tmp ls), and a glob or keyword isn't a program.
  if (!program || SHELL_KEYWORDS.has(program) || ASSIGNMENT.test(program) || GLOB.test(program)) {
    return [null, [], []];
  }
  const args: string[] = [];
  // A Map, like Python's dict: a repeated flag keeps its first place and its last value.
  const flags = new Map<string, FlagValue>();
  let optionsDone = false;
  for (const word of words.slice(1)) {
    if (!optionsDone && word === "--") {
      optionsDone = true;
    } else if (optionsDone || !word.startsWith("-") || word === "-") {
      args.push(word);
    } else {
      for (const [name, value] of flagsOf(word)) {
        flags.set(name, value);
      }
    }
  }
  return [program, args, [...flags]];
}

class Scanner {
  text: string;
  i = 0;
  parts: BashPart[] = [];
  start = 0; // where the current part begins
  after: string | null = null; // the separator before the current part
  words: string[] = [];
  word: string[] | null = null; // the word being read, null between words
  readable = true;
  redirect: string | null = null; // a redirection waiting for its target

  constructor(text: string) {
    this.text = text;
  }

  // It steps by UTF-16 unit. Every character it acts on is ASCII, so the two halves of any
  // other character land in the same word, as Python's one code point would.
  run(): BashPart[] {
    const text = this.text;
    while (this.i < text.length) {
      const c = text[this.i];
      if (c === "'") {
        this.singleQuoted();
      } else if (c === '"') {
        this.doubleQuoted();
      } else if (c === "\\") {
        this.escaped();
      } else if (c === "`") {
        throw new Unsplittable();
      } else if (c === "$") {
        this.dollar();
      } else if (" \t\r".includes(c)) {
        this.endWord();
        this.i += 1;
      } else if (c === "#" && this.word === null) {
        const end = text.indexOf("\n", this.i);
        this.i = end < 0 ? text.length : end;
      } else if ("&|;\n".includes(c)) {
        this.separator();
      } else if ("<>".includes(c)) {
        this.redirection();
      } else if ("()".includes(c)) {
        // A subshell or group: the parts inside run, but not as written here.
        this.readable = false;
        this.endWord();
        this.i += 1;
      } else {
        this.add(c);
        this.i += 1;
      }
    }
    this.endPart(text.length);
    return this.parts;
  }

  add(chars: string): void {
    if (this.word === null) {
      this.word = [];
    }
    this.word.push(chars);
  }

  singleQuoted(): void {
    const end = this.text.indexOf("'", this.i + 1);
    if (end < 0) {
      throw new Unsplittable();
    }
    this.add(this.text.slice(this.i + 1, end));
    this.i = end + 1;
  }

  doubleQuoted(): void {
    const text = this.text;
    const chars: string[] = [];
    let i = this.i + 1;
    while (i < text.length) {
      let c = text[i];
      if (c === '"') {
        this.add(chars.join(""));
        this.i = i + 1;
        return;
      }
      if (c === "`" || (c === "$" && text.startsWith("(", this.nextAt(i)))) {
        throw new Unsplittable();
      }
      if (c === "$") {
        this.readable = false;
      }
      if (c === "\\" && text.startsWith("\n", i + 1)) {
        i += 2; // a line continuation, which bash drops here too
        continue;
      }
      if (c === "\\" && i + 1 < text.length && '$`"\\'.includes(text[i + 1])) {
        i += 1;
        c = text[i];
      }
      chars.push(c);
      i += 1;
    }
    throw new Unsplittable();
  }

  escaped(): void {
    if (this.text.startsWith("\n", this.i + 1)) {
      this.i += 2; // a line continuation: bash drops it, so a word it splits stays one word
    } else if (this.i + 1 < this.text.length) {
      this.add(this.text[this.i + 1]);
      this.i += 2;
    } else {
      throw new Unsplittable(); // bash drops it, or joins it to whatever the agent runs after the command
    }
  }

  dollar(): void {
    // $( and $(( can hold separators of their own, so the command can't be split.
    if (this.text.startsWith("(", this.nextAt(this.i))) {
      throw new Unsplittable();
    }
    this.readable = false;
    this.add("$");
    this.i += 1;
  }

  separator(): void {
    const text = this.text;
    const i = this.i;
    const j = this.nextAt(i);
    const two = text[i] + text.slice(j, j + 1);
    if (two === "&>") {
      this.redirection();
      return;
    }
    const [sep, end] = two === "&&" || two === "||" || two === "|&" ? [two, j + 1] : [text[i], i + 1];
    this.endPart(i);
    this.i = this.start = end;
    this.after = sep;
  }

  redirection(): void {
    // Digits right before the operator name a file descriptor, not an argument.
    if (this.word !== null && DIGITS.test(this.word.join(""))) {
      this.word = null;
    } else {
      this.endWord();
    }
    const at = [this.i, this.nextAt(this.i)];
    at.push(this.nextAt(at[1]));
    const ahead = at.map((j) => this.text.slice(j, j + 1)).join("");
    const op = REDIRECTIONS.find((candidate) => ahead.startsWith(candidate)) as string;
    if (op === "<(" || op === ">(" || op === "<<") {
      throw new Unsplittable(); // a process substitution, or a heredoc whose lines aren't commands
    }
    this.i = at[op.length - 1] + 1;
    this.redirect = op;
  }

  // Where the character after the one at i is. Bash drops line continuations, even inside &&.
  nextAt(i: number): number {
    i += 1;
    while (this.text.startsWith("\\\n", i)) {
      i += 2;
    }
    return i;
  }

  endWord(): void {
    if (this.word === null) {
      return;
    }
    const value = this.word.join("");
    this.word = null;
    if (this.redirect === null) {
      this.words.push(value);
      return;
    }
    const op = this.redirect;
    this.redirect = null;
    if (op === "<" || op === "<<<") {
      return; // reads input
    }
    if ((op === "<&" || op === ">&") && FILE_DESCRIPTOR.test(value)) {
      return; // points one stream at another
    }
    if (value !== "/dev/null") {
      this.readable = false; // writes a file
    }
  }

  endPart(end: number): void {
    this.endWord();
    if (this.redirect !== null) {
      // an operator with nothing after it
      this.readable = false;
      this.redirect = null;
    }
    const text = strip(this.text.slice(this.start, end));
    const { words, readable } = this;
    this.words = [];
    this.readable = true;
    // A part with nothing to run is dropped, unless it does something unreadable (> file).
    if (words.length === 0 && readable) {
      return;
    }
    const [program, args, flags] = readable ? readPart(words) : [null, [], []];
    this.parts.push({ text, program, args, flags, after: this.parts.length > 0 ? this.after : null });
  }
}

// A shell call's input without program, full_command, arg_<digits> or any flag_ name.
//
// Only the parser writes these. Taken from the agent's input, they could make a part it can't
// read look read, so a shell call never carries them from there, whether it goes as parts or
// whole.
export function dropPartFields(args: Record<string, unknown>): Record<string, unknown> {
  return Object.fromEntries(
    Object.entries(args).filter(
      ([name]) =>
        name !== "program" && name !== "full_command" && !name.startsWith("flag_") && !/^arg_[0-9]+$/.test(name),
    ),
  );
}

// One item per part of a shell command, or null to send the call whole.
//
// The first item leads with the agent's input as received and the whole command, then each item
// has its part's fields. The input's own program, full_command, arg_<digits> and flag_ names are
// dropped, since only the parser sets those (dropPartFields). A parser failure only means the
// call goes whole: it never blocks it.
export function bashItems(
  args: Record<string, unknown>,
  toolName = "Bash",
  commandField = "command",
): Record<string, unknown>[] | null {
  const command = args[commandField];
  if (typeof command !== "string") {
    return null;
  }
  let parts: BashPart[] | null;
  try {
    parts = splitCommand(command);
  } catch {
    return null;
  }
  if (!parts || parts.length > MAX_BASH_PARTS) {
    return null;
  }
  const rest = Object.fromEntries(Object.entries(dropPartFields(args)).filter(([name]) => name !== "command"));
  const items: Record<string, unknown>[] = [];
  for (const [index, part] of parts.entries()) {
    let fields: Record<string, unknown>;
    if (parts.length === 1) {
      fields = { command, ...rest };
    } else if (index === 0) {
      fields = { full_command: command, ...rest, command: part.text };
    } else {
      fields = { command: part.text };
    }
    if (part.program !== null) {
      fields.program = part.program;
      part.args.slice(0, MAX_BASH_ARGS).forEach((value, n) => {
        fields[`arg_${n + 1}`] = value;
      });
      for (const [name, value] of part.flags) {
        const field = "flag_" + name.replace(/[^A-Za-z0-9_]/gu, "_");
        if ([...field].length <= ARGUMENT_NAME_MAX) {
          fields[field] = value;
        }
      }
    }
    const item: Record<string, unknown> = { id: index, tool_namespace: AGENT, tool_name: toolName, arguments: fields };
    if (index > 0 && part.after !== null && SEQUENTIAL_SEPARATORS.has(part.after)) {
      item.dependencies = [index - 1];
    }
    items.push(item);
  }
  return items;
}

export function approvalPayload(params: {
  sessionId: string;
  toolCallId: string;
  toolName: string;
  arguments: Record<string, unknown>;
  eventName?: string;
  // The field holding the command, when the call is to the agent's shell tool. The command then
  // goes as parts, unless splitShellCommand is false, as on a host whose shell the parser would
  // misread. Split or whole, the call never takes the parser's field names from its input.
  shellCommandField?: string;
  splitShellCommand?: boolean;
}) {
  const identity = callIdentity(params.sessionId, params.toolCallId, params.toolName, params.arguments);
  const tool = splitToolName(params.toolName);
  const isShell = Boolean(params.shellCommandField) && tool.namespace === AGENT;
  const splits = isShell && params.splitShellCommand !== false;
  const items = (splits ? bashItems(params.arguments, tool.name, params.shellCommandField) : null) ?? [
    {
      id: 0,
      tool_namespace: tool.namespace,
      tool_name: tool.name,
      arguments: isShell ? dropPartFields(params.arguments) : params.arguments,
    },
  ];
  return {
    version: 1,
    decision_id: identity.decisionId,
    request_id: identity.requestId,
    scope: { kind: "single", session_id: identity.sessionId },
    origin: {
      kind: "opencode_plugin",
      reason: params.eventName ?? "tool.execute.before",
    },
    items,
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
    if (error instanceof GovernanceError) {
      throw error;
    }
    // A fetch error can quote the token or a user:pass URL: say nothing of it.
    throw new GovernanceError("Mezmo Governance is unavailable");
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
  const reason = typeof body.reason === "string" ? body.reason : null;
  // Mezmo answers the poll with {approved, reason}. A status field is the older shape.
  if (typeof body.approved === "boolean") {
    return body.approved
      ? { approved: true, reason }
      : { approved: false, reason: reason ?? "Policy denied the call." };
  }
  const status = body.status;
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
  const exitCode = record.exit_code ?? record.exitCode;
  return typeof exitCode === "number" && exitCode !== 0;
}

// The log holds tool output: only its owner may read it.
export async function appendAudit(record: Record<string, unknown>): Promise<void> {
  const path = auditLogPath();
  await mkdir(dirname(path), { recursive: true, mode: 0o700 });
  const handle = await open(path, "a", 0o600);
  try {
    // An older version created the log 0644.
    if ((await handle.stat()).mode & 0o077) {
      await handle.chmod(0o600);
    }
    await handle.appendFile(`${JSON.stringify(record)}\n`, "utf8");
  } finally {
    await handle.close();
  }
}
