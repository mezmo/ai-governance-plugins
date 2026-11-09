#!/usr/bin/env python3
"""Claude Code PreToolUse / PostToolUse hook for Mezmo Governance.

Before each Bash or MCP tool call, ask Mezmo Governance whether this exact call may run, and
block it when the answer is no. Ported from the Claude Code adapter in the Mezmo Governance
plugins repo, with these changes:

- Only Bash and MCP tool calls go to Mezmo Governance, minus the Claude desktop app's own
  servers (ccd_session, ccd_turn and the like). hooks/hooks.json matches the same calls, so
  other tools don't start the hook at all.
- A Bash call goes as one item per part of its command (split_command), each with its
  program and positional arguments as fields a policy rule can match.
- When auto mode's classifier blocks one of these calls (PermissionDenied), the hook counts it
  and logs it locally. Mezmo isn't asked: the call already didn't run.
- Off until configured: with no access key, the hook allows every call, asks Mezmo nothing
  and logs nothing locally, unless MEZMO_GOVERNANCE_REQUIRED=1, which denies instead. Once
  configured it fails closed, as the adapter does. Ledger sending, below, is separate.
- Configuration comes from MEZMO_GOVERNANCE_* environment variables (managed settings) or,
  failing that, from the plugin's options, which Claude Code exports to plugin hooks as
  CLAUDE_PLUGIN_OPTION_GOVERNANCE_URL and CLAUDE_PLUGIN_OPTION_GOVERNANCE_ACCESS_KEY.
- The service answers HTTP 207 while a person decides, with {approved, reason} on both the
  POST and the status GET. 207 means pending, whatever `approved` says. The adapter's
  {status: ...} poll body is still accepted.
- Deny messages carry the service's error code and message on a 4xx, one line each, plus a fixed
  local hint.
- Each session's counts, pending decisions and last result, never the tool's arguments, go to
  ~/.mezmo/governance/sessions/<session id>.json, for the status mod and /mezmo:test.
- Ledger: when MEZMO_LEDGER_ACCOUNT and MEZMO_LEDGER_URL are both set, with or without an
  access key, each Bash or MCP call also goes to the Mezmo ledger service as a `started` event and,
  when Claude Code reports back, a `ran`, `failed` or `not_run` event, with the call's
  arguments and output. Each hook run sends its event and resends saved ones on two threads, waits
  for both up to one short deadline, never changes a decision, and saves what it couldn't send to
  ~/.mezmo/ledger-spool.jsonl for a later hook run.
"""

from __future__ import annotations

import errno
import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

AGENT = "claude_code"
DEFAULT_URL = "https://api.mezmo.com/governance/workflow/authorize"
DEFAULT_TIMEOUT_SECONDS = 590.0
# hooks.json gives the hook 600 seconds; deny before Claude Code stops it.
MAX_TIMEOUT_SECONDS = 590.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
MIN_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_AUDIT_LOG = Path.home() / ".mezmo" / "governance-audit.jsonl"
# One status file per session, which the plugin's status mod reads. The mod finds it by
# $HOME and its session id, so the path must stay in step with hooks/governance.ts.
DEFAULT_STATE_DIR = Path.home() / ".mezmo" / "governance" / "sessions"
STATE_RETENTION_SECONDS = 7 * 24 * 3600
MAX_RESULT_PREVIEW_CHARACTERS = 2_000
MAX_DETAIL_CHARACTERS = 300
MCP_TOOL_PREFIX = "mcp__"
# The built-in tools Mezmo Governance decides. Every other built-in is left alone.
GOVERNED_BUILT_IN_TOOLS = frozenset({"Bash"})
# MCP servers the Claude desktop app adds for its own sessions, panes and task list. Exact
# names, not a ccd_ prefix, so a server merely named like one is still governed.
CLAUDE_APP_SERVERS = frozenset({
    "ccd_connectors", "ccd_directory", "ccd_host", "ccd_pr", "ccd_session", "ccd_session_mgmt",
    "ccd_settings", "ccd_sidebar", "ccd_turn", "ccd_view", "ccd_window",
})
# Mezmo takes up to 100 items a request. A command with more parts goes whole, as before.
MAX_BASH_PARTS = 50
# Each argument is a row in Mezmo, and rules rarely look past the first few.
MAX_BASH_ARGS = 10
# The service's rule for argument names (ai-governance-service schema/constants.js).
ARGUMENT_NAME = re.compile(r"^[a-zA-Z_][a-zA-Z0-9_]*$")
ARGUMENT_NAME_MAX = 64

URL_ENV = "MEZMO_GOVERNANCE_URL"
KEY_ENVS = ("MEZMO_GOVERNANCE_ACCESS_KEY", "MEZMO_GOVERNANCE_TOKEN")
REQUIRED_ENV = "MEZMO_GOVERNANCE_REQUIRED"
URL_OPTION = "CLAUDE_PLUGIN_OPTION_GOVERNANCE_URL"
KEY_OPTION = "CLAUDE_PLUGIN_OPTION_GOVERNANCE_ACCESS_KEY"
ENABLED_OPTION = "CLAUDE_PLUGIN_OPTION_GOVERNANCE_ENABLED"
# Claude Code can't unset a sensitive option, so /mezmo:remove overwrites the key with this.
REMOVED_KEY = "off"
FALSE_VALUES = ("0", "false", "no", "off")

ERROR_HINTS = {
    # A session that outlives a reinstall of the plugin keeps sending a key that's gone, so name both fixes.
    401: (
        "Mezmo rejected the governance access key. If the plugin was just reinstalled or its settings "
        "changed, run /reload-plugins or start a new session. Otherwise run /mezmo:setup governance "
        "with a current key"
    ),
    402: "the Mezmo plan doesn't include governance",
    "ENOAGENT": "no agent is registered for this access key",
    "EAGENTINACTIVE": "the agent for this access key is inactive",
    "EPOLICYUNASSIGNED": "the agent for this access key has no policy",
}

HttpJson = Callable[[str, str, Optional[Mapping[str, Any]], float], Tuple[int, Dict[str, Any]]]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]


class HookContractError(ValueError):
    pass


class ApprovalExpired(HookContractError):
    """A person was asked, and nobody decided before the deadline."""


class Config(NamedTuple):
    url: str
    access_key: str


class NormalizedToolCall(NamedTuple):
    namespace: str
    name: str
    arguments: Dict[str, Any]


class CallIdentity(NamedTuple):
    decision_id: str
    request_id: str
    session_id: str


class Decision(NamedTuple):
    approved: bool
    reason: Optional[str]


def _setting(*names: str) -> Optional[str]:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def load_config() -> Optional[Config]:
    """The governance settings, or None when no access key is configured.

    An environment key (managed or user settings) wins. A plugin-option key counts only while
    the plugin's governance switch is on and the key isn't the placeholder remove leaves.
    """
    key = _setting(*KEY_ENVS)
    if key is None:
        switched_off = (_setting(ENABLED_OPTION) or "").lower() in FALSE_VALUES
        option_key = _setting(KEY_OPTION)
        if switched_off or option_key is None or option_key.lower() == REMOVED_KEY:
            return None
        key = option_key
    return Config(_setting(URL_ENV, URL_OPTION) or DEFAULT_URL, key)


def governance_required() -> bool:
    return (_setting(REQUIRED_ENV) or "").lower() in ("1", "true", "yes")


def required_config() -> Config:
    config = load_config()
    if config is None:
        raise HookContractError(
            "Mezmo Governance is required but not configured. Run /mezmo:setup, or ask your "
            "administrator for the governance access key."
        )
    return config


def required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HookContractError(f"{field} must be a non-empty string")
    return value.strip()


def parse_object(raw: bytes) -> Dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HookContractError("Mezmo Governance returned an invalid JSON response") from error
    if not isinstance(parsed, dict):
        raise HookContractError("Mezmo Governance returned a non-object response")
    return parsed


def authorize_url() -> str:
    return required_config().url


def status_url(decision_id: str, base: Optional[str] = None) -> str:
    parsed = urlparse(base or authorize_url())
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["decision_id"] = [decision_id]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def authorization_header() -> str:
    return token_authorization(key=required_config().access_key)


def token_authorization(*, key: str) -> str:
    """The Authorization value for a Mezmo key: `Token <key>`, or the key itself when it names its scheme."""
    if key.lower().startswith("token ") or key.lower().startswith("bearer "):
        return key
    return f"Token {key}"


def positive_seconds(environment_name: str, default: float) -> float:
    configured = os.environ.get(environment_name)
    if configured is None:
        return default
    try:
        value = float(configured)
    except ValueError as error:
        raise HookContractError(f"{environment_name} must be a number") from error
    if not math.isfinite(value) or value <= 0:
        raise HookContractError(f"{environment_name} must be a finite number greater than zero")
    return value


def timeout_seconds() -> float:
    return min(positive_seconds("MEZMO_GOVERNANCE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS), MAX_TIMEOUT_SECONDS)


def poll_interval_seconds() -> float:
    return max(
        positive_seconds("MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS", DEFAULT_POLL_INTERVAL_SECONDS),
        MIN_POLL_INTERVAL_SECONDS,
    )


def audit_log_path() -> Path:
    configured = os.environ.get("MEZMO_GOVERNANCE_AUDIT_LOG")
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_AUDIT_LOG


def state_dir() -> Path:
    configured = os.environ.get("MEZMO_GOVERNANCE_STATE_DIR")
    return Path(configured).expanduser() if configured else DEFAULT_STATE_DIR


def session_state_path(session_id: str) -> Path:
    return state_dir() / (re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:128] + ".json")


def server_namespace(name: str) -> str:
    """Refuse a server or extension named like the agent: its tools would pass as built-ins."""
    if name.lower() == AGENT:
        raise HookContractError(f"a tool namespace named {name!r} would pass as a built-in tool")
    return name


def normalize_tool_call(event: Mapping[str, Any]) -> NormalizedToolCall:
    canonical_name = required_string(event.get("tool_name"), "tool_name")
    raw_input = event.get("tool_input")
    if not isinstance(raw_input, dict):
        raise HookContractError("tool_input must be a JSON object")
    if canonical_name.startswith(MCP_TOOL_PREFIX):
        parts = canonical_name.split("__", 2)
        if len(parts) != 3 or not parts[1] or not parts[2]:
            raise HookContractError("MCP tool names must use mcp__<server>__<tool>")
        return NormalizedToolCall(server_namespace(parts[1]), parts[2], dict(raw_input))
    return NormalizedToolCall(AGENT, canonical_name, dict(raw_input))


def call_digest(namespace: str, name: str, arguments: Mapping[str, Any]) -> str:
    """Hash the exact call, so an edited call never reuses an earlier decision."""
    canonical = json.dumps([namespace, name, dict(arguments)], sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode()).hexdigest()[:16]


# Mezmo answers HTTP 400 to a longer request_id. The decision_id hashes the full id,
# so a cut id still gets its own decision.
MAX_REQUEST_ID_CHARACTERS = 128


def bounded_request_id(request_id: str) -> str:
    if len(request_id) <= MAX_REQUEST_ID_CHARACTERS:
        return request_id
    # A hash of the whole head keeps two cut ids apart.
    head, digest = request_id.rsplit("#", 1)
    tail = "~" + hashlib.sha256(head.encode()).hexdigest()[:16] + "#" + digest
    return head[: MAX_REQUEST_ID_CHARACTERS - len(tail)] + tail


def call_identity(event: Mapping[str, Any]) -> CallIdentity:
    session_id = required_string(event.get("session_id"), "session_id")
    tool_use_id = required_string(event.get("tool_use_id"), "tool_use_id")
    turn_id = event.get("turn_id")
    if isinstance(turn_id, str) and turn_id.strip():
        request_id = f"{AGENT}:{session_id}:{turn_id.strip()}:{tool_use_id}"
    else:
        request_id = f"{AGENT}:{session_id}:{tool_use_id}"
    request_id += "#" + call_digest(*normalize_tool_call(event))
    decision_id = str(uuid.uuid5(uuid.NAMESPACE_URL, request_id))
    return CallIdentity(decision_id, bounded_request_id(request_id), session_id)


def origin(event: Mapping[str, Any]) -> Dict[str, Any]:
    return {"kind": "claude_code_hook", "reason": str(event.get("hook_event_name") or "Claude Code tool hook")}


# Bash command parts
#
# A Bash call goes to Mezmo as one item per part of its command, with the part's program,
# positional arguments and flags as fields a policy rule can match. The parser only ever leans
# one way: a part it can't fully read gets no program, arguments or flags, so a rule that
# allows by them can't match it, and a command it can't even split is sent whole, as before.
#
# It splits where Claude Code splits commands for its own Bash permission rules (on &&, ||, ;,
# |, |&, & and newlines) and skips the wrappers those rules strip. Like those rules, it can't
# see through sh -c, scripts, aliases, or an option whose value is a separate word (the prod
# in kubectl -n prod lands among the arguments). It lives in this file because the hook is
# installed as one copied file.

# A part after one of these runs once the part before it has finished.
SEQUENTIAL_SEPARATORS = frozenset({"&&", "||", ";", "\n"})
# Words the shell reads as syntax rather than a program to run.
SHELL_KEYWORDS = frozenset({
    "!", "{", "}", "[[", "]]", "case", "do", "done", "elif", "else", "esac", "fi", "for",
    "function", "if", "in", "select", "then", "until", "while",
})
SIMPLE_WRAPPERS = frozenset({"builtin", "command", "nohup", "time", "xargs"})
ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*\+?=")
GLOB = re.compile(r"[*?\[]")
DURATION = re.compile(r"\d+(\.\d+)?[smhd]?")
NICENESS = re.compile(r"-?\d+")
STDBUF_OPTION = re.compile(r"-[ioe]\S+|--(input|output|error)=\S+")
FILE_DESCRIPTOR = re.compile(r"\d+|-")
LETTERS = re.compile(r"[A-Za-z]+")
# Longest first, so <<< isn't read as <<.
REDIRECTIONS = ("&>>", "&>", "<<<", "<<", "<>", "<&", "<(", "<", ">>", ">|", ">&", ">(", ">")


FlagValue = Union[bool, str]


class BashPart(NamedTuple):
    text: str  # the part as written
    program: Optional[str]  # None when the part can't be fully read
    args: Tuple[str, ...]  # positional arguments, flags left out
    flags: Tuple[Tuple[str, FlagValue], ...]  # (name, True or the value written with it)
    after: Optional[str]  # the separator before it, None for the first part


class Unsplittable(Exception):
    """The command can't be split safely: an unclosed quote, a substitution or a heredoc."""


def split_command(command: str) -> Optional[List[BashPart]]:
    """The command's parts, or None when it can't be split safely or holds nothing to run."""
    try:
        parts = _Scanner(command.replace("\\\n", " ")).run()
    except Unsplittable:
        return None
    return parts or None


def _skip_wrappers(words: Sequence[str]) -> List[str]:
    """Drop the wrappers Claude Code strips, in the forms whose options are certain."""
    words = list(words)
    while words:
        head, rest = words[0], words[1:]
        if head in SIMPLE_WRAPPERS and rest and not rest[0].startswith("-"):
            words = rest
        elif head == "nice" and rest:
            if rest[0] == "-n" and len(rest) > 2 and NICENESS.fullmatch(rest[1]):
                words = rest[2:]
            elif re.fullmatch(r"-\d+", rest[0]) and len(rest) > 1:
                words = rest[1:]
            elif not rest[0].startswith("-"):
                words = rest
            else:
                break
        elif head == "timeout" and len(rest) > 1 and DURATION.fullmatch(rest[0]):
            words = rest[1:]
        elif head == "stdbuf":
            count = 0
            while count < len(rest) and STDBUF_OPTION.fullmatch(rest[count]):
                count += 1
            if count < len(rest) and not rest[count].startswith("-"):
                words = rest[count:]
            else:
                break
        else:
            break
    return words


def _flags_of(word: str) -> List[Tuple[str, FlagValue]]:
    """The flags one word sets. Without knowing the tool, it reads them the common ways."""
    if word.startswith("--"):
        name, has_value, value = word[2:].partition("=")
        return [(name, value if has_value else True)] if name else []
    body = word[1:]
    if LETTERS.fullmatch(body):
        flags: List[Tuple[str, FlagValue]] = [(letter, True) for letter in body]
        if len(body) > 2:
            flags.append((body, True))  # find, go and java take long options after one dash
        return flags
    if body[:1].isalpha():
        return [(body[0], body[1:])]  # head -n5
    return [(body, True)]  # kill -9


def _read_part(words: Sequence[str]) -> Tuple[Optional[str], Tuple[str, ...], Tuple[Tuple[str, FlagValue], ...]]:
    words = _skip_wrappers(words)
    if not words:
        return None, (), ()
    program = words[0]
    # An assignment changes what runs (PATH=/tmp ls), and a glob or keyword isn't a program.
    if not program or program in SHELL_KEYWORDS or ASSIGNMENT.match(program) or GLOB.search(program):
        return None, (), ()
    args: List[str] = []
    flags: Dict[str, FlagValue] = {}
    options_done = False
    for word in words[1:]:
        if not options_done and word == "--":
            options_done = True
        elif options_done or not word.startswith("-") or word == "-":
            args.append(word)
        else:
            flags.update(_flags_of(word))
    return program, tuple(args), tuple(flags.items())


class _Scanner:
    def __init__(self, text: str) -> None:
        self.text = text
        self.i = 0
        self.parts: List[BashPart] = []
        self.start = 0  # where the current part begins
        self.after: Optional[str] = None  # the separator before the current part
        self.words: List[str] = []
        self.word: Optional[List[str]] = None  # the word being read, None between words
        self.readable = True
        self.redirect: Optional[str] = None  # a redirection waiting for its target

    def run(self) -> List[BashPart]:
        text = self.text
        while self.i < len(text):
            c = text[self.i]
            if c == "'":
                self.single_quoted()
            elif c == '"':
                self.double_quoted()
            elif c == "\\":
                self.escaped()
            elif c == "`":
                raise Unsplittable
            elif c == "$":
                self.dollar()
            elif c in " \t\r":
                self.end_word()
                self.i += 1
            elif c == "#" and self.word is None:
                end = text.find("\n", self.i)
                self.i = len(text) if end < 0 else end
            elif c in "&|;\n":
                self.separator()
            elif c in "<>":
                self.redirection()
            elif c in "()":
                # A subshell or group: the parts inside run, but not as written here.
                self.readable = False
                self.end_word()
                self.i += 1
            else:
                self.add(c)
                self.i += 1
        self.end_part(len(text))
        return self.parts

    def add(self, chars: str) -> None:
        if self.word is None:
            self.word = []
        self.word.append(chars)

    def single_quoted(self) -> None:
        end = self.text.find("'", self.i + 1)
        if end < 0:
            raise Unsplittable
        self.add(self.text[self.i + 1 : end])
        self.i = end + 1

    def double_quoted(self) -> None:
        text, i, chars = self.text, self.i + 1, []
        while i < len(text):
            c = text[i]
            if c == '"':
                self.add("".join(chars))
                self.i = i + 1
                return
            if c == "`" or text.startswith("$(", i):
                raise Unsplittable
            if c == "$":
                self.readable = False
            if c == "\\" and i + 1 < len(text) and text[i + 1] in '$`"\\':
                i += 1
                c = text[i]
            chars.append(c)
            i += 1
        raise Unsplittable

    def escaped(self) -> None:
        if self.i + 1 < len(self.text):
            self.add(self.text[self.i + 1])
            self.i += 2
        else:
            self.add("\\")
            self.i += 1

    def dollar(self) -> None:
        # $( and $(( can hold separators of their own, so the command can't be split.
        if self.text.startswith("$(", self.i):
            raise Unsplittable
        self.readable = False
        self.add("$")
        self.i += 1

    def separator(self) -> None:
        text, i = self.text, self.i
        if text.startswith("&>", i):
            self.redirection()
            return
        two = text[i : i + 2]
        sep = two if two in ("&&", "||", "|&") else text[i]
        self.end_part(i)
        self.i = self.start = i + len(sep)
        self.after = sep

    def redirection(self) -> None:
        # Digits right before the operator name a file descriptor, not an argument.
        if self.word is not None and "".join(self.word).isdigit():
            self.word = None
        else:
            self.end_word()
        op = next(op for op in REDIRECTIONS if self.text.startswith(op, self.i))
        if op in ("<(", ">(", "<<"):
            raise Unsplittable  # a process substitution, or a heredoc whose lines aren't commands
        self.i += len(op)
        self.redirect = op

    def end_word(self) -> None:
        if self.word is None:
            return
        value = "".join(self.word)
        self.word = None
        if self.redirect is None:
            self.words.append(value)
            return
        op, self.redirect = self.redirect, None
        if op in ("<", "<<<"):
            return  # reads input
        if op in ("<&", ">&") and FILE_DESCRIPTOR.fullmatch(value):
            return  # points one stream at another
        if value != "/dev/null":
            self.readable = False  # writes a file

    def end_part(self, end: int) -> None:
        self.end_word()
        if self.redirect is not None:  # an operator with nothing after it
            self.readable = False
            self.redirect = None
        text = self.text[self.start : end].strip()
        words, readable = self.words, self.readable
        self.words, self.readable = [], True
        # A part with nothing to run is dropped, unless it does something unreadable (> file).
        if not words and readable:
            return
        program, args, flags = _read_part(words) if readable else (None, (), ())
        self.parts.append(BashPart(text, program, args, flags, self.after if self.parts else None))


def bash_items(arguments: Mapping[str, Any]) -> Optional[List[Dict[str, Any]]]:
    """One item per part of a Bash command, or None to send the call whole.

    The first item leads with the call's own input and the whole command, then each item has
    its part's fields. A parser failure only means the call goes whole: it never blocks it.
    """
    command = arguments.get("command")
    if not isinstance(command, str):
        return None
    try:
        parts = split_command(command)
    except Exception:
        return None
    if not parts or len(parts) > MAX_BASH_PARTS:
        return None
    rest = {name: value for name, value in arguments.items() if name != "command"}
    items = []
    for index, part in enumerate(parts):
        if len(parts) == 1:
            fields = {"command": command, **rest}
        elif index == 0:
            fields = {"full_command": command, **rest, "command": part.text}
        else:
            fields = {"command": part.text}
        if part.program is not None:
            fields["program"] = part.program
            fields.update((f"arg_{n}", value) for n, value in enumerate(part.args[:MAX_BASH_ARGS], 1))
            for name, value in part.flags:
                field = "flag_" + re.sub(r"[^A-Za-z0-9_]", "_", name)
                if len(field) <= ARGUMENT_NAME_MAX:
                    fields[field] = value
        item: Dict[str, Any] = {"id": index, "tool_namespace": AGENT, "tool_name": "Bash", "arguments": fields}
        if index and part.after in SEQUENTIAL_SEPARATORS:
            item["dependencies"] = [index - 1]
        items.append(item)
    return items


def approval_payload(event: Mapping[str, Any]) -> Dict[str, Any]:
    tool_call = normalize_tool_call(event)
    identity = call_identity(event)
    is_bash = (tool_call.namespace, tool_call.name) == (AGENT, "Bash")
    items = (bash_items(tool_call.arguments) if is_bash else None) or [
        {
            "id": 0,
            "tool_namespace": tool_call.namespace,
            "tool_name": tool_call.name,
            "arguments": tool_call.arguments,
        }
    ]
    return {
        "version": 1,
        "decision_id": identity.decision_id,
        "request_id": identity.request_id,
        "scope": {"kind": "single", "session_id": identity.session_id},
        "origin": origin(event),
        "items": items,
    }


class RefuseRedirect(HTTPRedirectHandler):
    """Hand a 3xx back as an error, so the key never goes to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def is_loopback(host: Optional[str]) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def refuse_exposed_token(url: str, authorization: str) -> None:
    """Refuse a request that would expose the token. The errors quote neither."""
    parsed = urlparse(url)
    if parsed.username is not None or parsed.password is not None:
        raise HookContractError("the Mezmo Governance URL must not hold a user name or password")
    if not (parsed.scheme == "https" or (parsed.scheme == "http" and is_loopback(parsed.hostname))):
        raise HookContractError("the Mezmo Governance URL must use https://; http:// is allowed only to this machine")
    # http.client quotes a header value it refuses, which would show the token.
    if not (authorization.isascii() and authorization.isprintable()):
        raise HookContractError("the Mezmo Governance token holds a character an HTTP header cannot carry")


def http_json(method: str, url: str, payload: Optional[Mapping[str, Any]], timeout: float) -> Tuple[int, Dict[str, Any]]:
    body = None if payload is None else json.dumps(payload, separators=(",", ":")).encode()
    authorization = authorization_header()
    refuse_exposed_token(url, authorization)
    headers = {"Authorization": authorization, "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with build_opener(RefuseRedirect).open(request, timeout=timeout) as response:
            return response.status, parse_object(response.read())
    except HTTPError as error:
        try:
            parsed = parse_object(error.read())
        except HookContractError:
            parsed = {}
        return error.code, parsed
    except TimeoutError:
        raise
    except URLError as error:
        reason = error.reason
        if isinstance(reason, (TimeoutError, socket.timeout)) or "timed out" in str(error).lower():
            raise TimeoutError(str(error)) from error
        raise HookContractError(f"Mezmo Governance is unavailable: {error}") from error
    except socket.timeout as error:
        raise TimeoutError(str(error)) from error


def rejected_argument_names(errors: Sequence[Any], payload: Optional[Mapping[str, Any]]) -> List[str]:
    """Argument names the service refused. Its 400 points at /items/N/arguments, not at the name."""
    if not any(isinstance(e, dict) and str(e.get("instance_path", "")).endswith("/arguments") for e in errors):
        return []
    names: List[str] = []
    for item in (payload or {}).get("items", []):
        for name in (item.get("arguments") or {}):
            if (not ARGUMENT_NAME.match(name) or len(name) > ARGUMENT_NAME_MAX) and name not in names:
                names.append(name)
    return names


MAX_ERROR_DETAIL_CHARACTERS = 200


def error_detail(value: Any) -> str:
    """Mezmo's own error text on one bounded line, or "" when there is none."""
    if not isinstance(value, str):
        return ""
    printable = "".join(ch if ch.isprintable() else " " for ch in value)
    return " ".join(printable.split())[:MAX_ERROR_DETAIL_CHARACTERS]


def http_failure(status: int, body: Mapping[str, Any], when: str = "") -> str:
    """Name the status and, on a 4xx, Mezmo's code and message; nothing else."""
    code = message = ""
    if 400 <= status < 500:
        code = error_detail(body.get("code"))
        message = error_detail(body.get("message"))
    text = f"Mezmo Governance returned HTTP {status}"
    if code:
        text += f" {code}"
    text += when
    if message:
        text += f": {message}"
    return text


def describe_failure(
    status: int, body: Mapping[str, Any], payload: Optional[Mapping[str, Any]] = None, when: str = ""
) -> str:
    """http_failure, then a fixed local hint. No other text from the reply is shown."""
    text = http_failure(status, body, when)
    if not 400 <= status < 500:
        return text
    errors = body.get("errors") if isinstance(body.get("errors"), list) else []
    names = rejected_argument_names(errors, payload)
    if names:
        # The names come from this call's own arguments, not from the reply.
        listed = ", ".join(repr(n) for n in names)[:MAX_ERROR_DETAIL_CHARACTERS]
        noun, verb = ("argument name", "isn't") if len(names) == 1 else ("argument names", "aren't")
        text += (
            f" ({noun} {listed} {verb} accepted; names must match {ARGUMENT_NAME.pattern} "
            f"and be at most {ARGUMENT_NAME_MAX} characters)"
        )
    hint = ERROR_HINTS.get(error_detail(body.get("code"))) or ERROR_HINTS.get(status)
    if hint:
        text += f". {hint[0].upper()}{hint[1:]}"
    return text


def remaining_seconds(deadline: float, clock: Clock) -> float:
    leftover = deadline - clock()
    if leftover <= 0:
        raise ApprovalExpired("Mezmo Governance: approval still pending")
    return leftover


def decision_from_post_body(body: Mapping[str, Any]) -> Decision:
    approved = body.get("approved")
    if not isinstance(approved, bool):
        raise HookContractError("Mezmo Governance did not return an approval decision")
    reason = body.get("reason")
    return Decision(approved, reason if isinstance(reason, str) else None)


def decision_from_status_body(body: Mapping[str, Any]) -> Optional[Decision]:
    """A final answer from the status GET, or None while pending."""
    reason = body.get("reason")
    detail = reason if isinstance(reason, str) else None
    if isinstance(body.get("approved"), bool):
        return Decision(body["approved"], detail)
    status = body.get("status")
    if status == "approved":
        return Decision(True, detail)
    if status == "denied":
        return Decision(False, detail or "Policy denied the call.")
    if status == "pending":
        return None
    raise HookContractError(f"Mezmo Governance returned an unexpected status: {status!r}")


def poll_decision(decision_id: str, deadline: float, http: HttpJson, clock: Clock, sleep: Sleeper) -> Decision:
    url = status_url(decision_id)
    interval = poll_interval_seconds()
    while True:
        leftover = remaining_seconds(deadline, clock)
        try:
            status, body = http("GET", url, None, leftover)
        except TimeoutError as error:
            raise HookContractError(f"Mezmo Governance is unavailable: {error}") from error
        # An answer that arrives after the deadline is too late to act on.
        leftover = remaining_seconds(deadline, clock)
        if status in (207, 404):  # still pending, or not visible yet
            sleep(min(interval, leftover))
            continue
        if status != 200:
            raise HookContractError(describe_failure(status, body, when=" while polling"))
        decided = decision_from_status_body(body)
        if decided is not None:
            return decided
        sleep(min(interval, leftover))


OnPending = Callable[[Optional[str], float], None]


def authorize(
    payload: Mapping[str, Any],
    http: HttpJson = http_json,
    clock: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
    on_pending: Optional[OnPending] = None,
) -> Decision:
    """Ask for a decision. `on_pending(reason, seconds_left)` runs once if it has to wait."""
    deadline = clock() + timeout_seconds()
    url = authorize_url()
    leftover = remaining_seconds(deadline, clock)

    def wait(reason: Optional[str]) -> Decision:
        if on_pending is not None:
            on_pending(reason, max(deadline - clock(), 0.0))
        return poll_decision(str(payload["decision_id"]), deadline, http, clock, sleep)

    try:
        status, body = http("POST", url, payload, leftover)
    except TimeoutError:
        return wait(None)
    # An answer that arrives after the deadline is too late to act on.
    remaining_seconds(deadline, clock)
    # 207: a person has to decide. 409: this decision_id exists already (a retry).
    if status in (207, 409):
        reason = body.get("reason") if status == 207 else None
        return wait(reason if isinstance(reason, str) else None)
    if status != 200:
        raise HookContractError(describe_failure(status, body, payload))
    return decision_from_post_body(body)


def result_preview(value: Any) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        rendered = str(value)
    return rendered[:MAX_RESULT_PREVIEW_CHARACTERS]


def result_has_known_error(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    if value.get("isError") is True or value.get("is_error") is True:
        return True
    exit_code = value.get("exit_code")
    return isinstance(exit_code, int) and exit_code != 0


def append_audit(record: Mapping[str, Any]) -> None:
    # The log keeps tool results, so only its owner may read it.
    path = audit_log_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

    def private(name: str, flags: int) -> int:
        return os.open(name, flags, 0o600)

    with open(path, "a", encoding="utf-8", opener=private) as handle:
        # A log an older version wrote keeps its mode until narrowed here.
        mode = os.fstat(handle.fileno()).st_mode
        if os.name != "nt" and stat.S_ISREG(mode) and mode & 0o077:
            os.fchmod(handle.fileno(), 0o600)
        handle.write(json.dumps(record, separators=(",", ":")) + "\n")


# Makes this hook's threads take turns on the retry file. flock already does this on
# macOS and Linux; this covers Windows, which has no flock.
FILE_LOCK = threading.Lock()


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on `path`.lock, so parallel tool calls don't lose updates."""
    with FILE_LOCK, open(f"{path}.lock", "a") as lock:
        try:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass  # no fcntl on Windows: best effort
        yield


def _prune_old_state(directory: Path) -> None:
    cutoff = time.time() - STATE_RETENTION_SECONDS
    for entry in directory.iterdir():
        try:
            if entry.suffix in (".json", ".lock") and entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass


def update_session_state(event: Mapping[str, Any], change: Callable[[Dict[str, Any]], None]) -> None:
    """Apply `change` to this session's status file. Never raises: the file is only for display."""
    session_id = event.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return
    try:
        path = session_state_path(session_id.strip())
        path.parent.mkdir(parents=True, exist_ok=True)
        with _locked(path):
            is_new = not path.exists()
            try:
                state = json.loads(path.read_text(encoding="utf-8")) if not is_new else {}
            except (OSError, ValueError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            state.update(version=1, session_id=session_id.strip())
            counts = state.setdefault("counts", {})
            for name in ("allowed", "denied", "expired", "errors"):
                counts.setdefault(name, 0)
            state.setdefault("pending", {})
            change(state)
            config = load_config()
            state["host"] = urlparse(config.url).hostname if config else None
            state["updated"] = datetime.now(timezone.utc).isoformat()
            temporary = path.with_name(path.name + ".tmp")
            temporary.write_text(json.dumps(state, indent=1) + "\n", encoding="utf-8")
            os.replace(temporary, path)
        if is_new:
            _prune_old_state(path.parent)
    except Exception:
        pass


def _tool_labels(event: Mapping[str, Any]) -> Tuple[str, str]:
    """('namespace/name', the tool name as Claude Code knows it)."""
    raw = str(event.get("tool_name") or "")
    try:
        tool = normalize_tool_call(event)
        return f"{tool.namespace}/{tool.name}", raw
    except HookContractError:
        return raw, raw


def record_pending(event: Mapping[str, Any], decision_id: str, reason: Optional[str], seconds_left: float) -> None:
    tool, tool_name = _tool_labels(event)
    now_ms = int(time.time() * 1000)
    entry = {
        "tool": tool,
        "tool_name": tool_name,
        "reason": (reason or "")[:MAX_DETAIL_CHARACTERS] or None,
        "since_ms": now_ms,
        "deadline_ms": now_ms + int(seconds_left * 1000),
    }
    update_session_state(event, lambda state: state["pending"].__setitem__(decision_id, entry))


def record_result(
    event: Mapping[str, Any], decision_id: Optional[str], outcome: str, detail: Optional[str], by_person: bool
) -> None:
    """Count a finished check and keep it as the last one. Never the tool's arguments."""
    tool, tool_name = _tool_labels(event)

    def change(state: Dict[str, Any]) -> None:
        if decision_id:
            state["pending"].pop(decision_id, None)
        counter = outcome if outcome in ("allowed", "denied", "expired") else "errors"
        state["counts"][counter] = state["counts"].get(counter, 0) + 1
        state["last"] = {
            "ts": datetime.now(timezone.utc).isoformat(),
            "decision_id": decision_id,
            "tool": tool,
            "tool_name": tool_name,
            "outcome": outcome,
            "detail": (detail or "")[:MAX_DETAIL_CHARACTERS] or None,
            "by_person": by_person,
        }

    update_session_state(event, change)


def deny_output(reason: str) -> Dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


def handle_pre_tool_use(
    event: Mapping[str, Any],
    http: HttpJson,
    clock: Clock,
    sleep: Sleeper,
    on_denied: Optional[Callable[[str], None]] = None,
) -> Optional[Dict[str, Any]]:
    """Ask Mezmo about this call. `on_denied(detail)` runs just before a denial is returned."""
    payload = approval_payload(event)
    decision_id = str(payload["decision_id"])
    waited: List[bool] = []

    def on_pending(reason: Optional[str], seconds_left: float) -> None:
        waited.append(True)
        record_pending(event, decision_id, reason, seconds_left)

    try:
        decision = authorize(payload, http=http, clock=clock, sleep=sleep, on_pending=on_pending)
    except ApprovalExpired as error:
        record_result(event, decision_id, "expired", str(error), bool(waited))
        raise
    except Exception as error:
        record_result(event, decision_id, "error", str(error), bool(waited))
        raise
    if decision.approved:
        record_result(event, decision_id, "allowed", decision.reason, bool(waited))
        return None
    detail = decision.reason or "Policy denied the call."
    record_result(event, decision_id, "denied", detail, bool(waited))
    if on_denied is not None:
        on_denied(detail)
    return deny_output(f"Mezmo Governance denied this exact tool call: {detail}")


def handle_post_tool_use(event: Mapping[str, Any]) -> None:
    tool_call = normalize_tool_call(event)
    identity = call_identity(event)
    response = event.get("tool_response")
    failed = event.get("hook_event_name") == "PostToolUseFailure" or result_has_known_error(response)
    preview = event.get("error") if failed and event.get("error") is not None else response
    append_audit(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent": AGENT,
            "decision_id": identity.decision_id,
            "request_id": identity.request_id,
            "tool_namespace": tool_call.namespace,
            "tool_name": tool_call.name,
            "status": "failed" if failed else "observed",
            "result_preview": result_preview(preview),
        }
    )


def handle_permission_denied(event: Mapping[str, Any]) -> None:
    """Count and log a call auto mode's classifier blocked. Never its arguments."""
    try:
        tool_call = normalize_tool_call(event)
        namespace, name = tool_call.namespace, tool_call.name
    except HookContractError:
        namespace, name = None, str(event.get("tool_name") or "")
    try:
        identity = call_identity(event)
        decision_id, request_id = identity.decision_id, identity.request_id
    except HookContractError:
        decision_id = request_id = None
    reason = event.get("reason")
    append_audit(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent": AGENT,
            "decision_id": decision_id,
            "request_id": request_id,
            "tool_namespace": namespace,
            "tool_name": name,
            "status": "blocked_by_auto_mode",
            "reason": reason[:MAX_DETAIL_CHARACTERS] if isinstance(reason, str) else None,
        }
    )

    def count(state: Dict[str, Any]) -> None:
        state["counts"]["auto_mode_blocked"] = state["counts"].get("auto_mode_blocked", 0) + 1

    update_session_state(event, count)


def governed(event: Mapping[str, Any]) -> bool:
    """Whether Mezmo Governance decides this call. A call without a usable tool name counts, so it gets denied."""
    name = event.get("tool_name")
    if not isinstance(name, str) or not name:
        return True
    if name.startswith(MCP_TOOL_PREFIX):
        parts = name.split("__", 2)
        return len(parts) != 3 or parts[1] not in CLAUDE_APP_SERVERS
    return name in GOVERNED_BUILT_IN_TOOLS


# Ledger
#
# With MEZMO_LEDGER_ACCOUNT and MEZMO_LEDGER_URL set, each Bash or MCP call also goes to Mezmo
# audit-service: `started` before governance is asked, then `ran`, `failed` or `not_run`. It
# doesn't need the governance key, and nothing here may change a decision, a deny message, the
# exit code or the local audit log. All network I/O runs on daemon threads: one sends the new
# event, one resends saved events, and the main thread waits for both up to one deadline. Only
# the main thread saves a new event that wasn't sent to the retry file.

LEDGER_ACCOUNT_ENV = "MEZMO_LEDGER_ACCOUNT"
LEDGER_INCIDENT_KEY_ENV = "MEZMO_LEDGER_INCIDENT_KEY"
LEDGER_INVESTIGATION_ID_ENV = "MEZMO_LEDGER_INVESTIGATION_ID"
LEDGER_KEY_ENV = "MEZMO_LEDGER_KEY"
LEDGER_SPOOL_ENV = "MEZMO_LEDGER_SPOOL"
LEDGER_URL_ENV = "MEZMO_LEDGER_URL"
DEFAULT_LEDGER_SPOOL = Path.home() / ".mezmo" / "ledger-spool.jsonl"
LEDGER_APPLICATION = "mezmo-claude-code-plugin"
# The plugin's manifest, for application.version. The hook runs from <plugin>/scripts.
PLUGIN_MANIFEST = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
# The socket timeout of each ledger request, and the deadline the main thread waits for its
# ledger threads from when they start. A socket timeout alone bounds neither DNS nor the whole
# request, hence the threads. Governance stops at 590 s of the pre-tool hook's 600.
LEDGER_TIMEOUT_SECONDS = 3.0
# Saved events resent in one request, small enough to finish well inside the deadline.
LEDGER_DRAIN_BATCH = 10
MAX_LEDGER_JSON_CHARACTERS = 4096
MAX_LEDGER_MESSAGE_CHARACTERS = 200
EXIT_CODE_LINE = re.compile(r"Exit code (-?\d+)")
LEDGER_ENDINGS = {"failed": "failed", "not_run": "not run", "ran": "ok"}

# Every ledger thread this process started, so main() knows whether one is still running.
LEDGER_THREADS: List[threading.Thread] = []
# Ledger threads never print. They leave messages here, which the main thread prints after its
# wait.
LEDGER_MESSAGES: List[str] = []


class LedgerConfig(NamedTuple):
    account: str
    # The whole Authorization value, or None for no header.
    key: Optional[str]
    url: str


class LedgerSendResult(NamedTuple):
    """What send_ledger did with new events. It never saves them itself."""

    # Whether the ledger service answered with any status. A network failure or timeout isn't one.
    answered: bool
    # The events the caller saves for retry.
    save: List[Mapping[str, Any]]


LedgerHttp = Callable[..., Tuple[int, bytes]]


def load_ledger_config() -> Optional[LedgerConfig]:
    """The ledger settings, or None when sending is off or the settings aren't safe to use."""
    account = _setting(LEDGER_ACCOUNT_ENV)
    url = _setting(LEDGER_URL_ENV)
    if account is None or url is None:
        return None
    # MEZMO_LEDGER_KEY, else the governance key when governance is configured, else no header.
    key = _setting(LEDGER_KEY_ENV)
    key_setting = LEDGER_KEY_ENV
    if key is None:
        governance = load_config()
        if governance is not None:
            key = governance.access_key
            key_setting = next((name for name in KEY_ENVS if _setting(name) is not None), KEY_OPTION)
    authorization = None if key is None else token_authorization(key=key)
    # The messages name the setting, never its value.
    try:
        refuse_exposed_token(url, "")
    except (HookContractError, ValueError):
        print(
            f"Mezmo ledger sending is off: {LEDGER_URL_ENV} must use https://, or http:// to this machine, "
            "with no user name or password",
            file=sys.stderr,
        )
        return None
    if authorization is not None and not (authorization.isascii() and authorization.isprintable()):
        print(
            f"Mezmo ledger sending is off: {key_setting} holds a character an HTTP header cannot carry",
            file=sys.stderr,
        )
        return None
    return LedgerConfig(account, authorization, url)


def ledger_spool_path() -> Path:
    configured = _setting(LEDGER_SPOOL_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_LEDGER_SPOOL


def plugin_version() -> str:
    try:
        version = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8")).get("version")
    except Exception:
        return "unknown"
    version = version.strip() if isinstance(version, str) else ""
    return version or "unknown"


def resolve_target(namespace: str, name: str, arguments: Mapping[str, Any]) -> Tuple[str, str]:
    """(target, access) for a call. LOG-25341 replaces this stub with the real resolver."""
    return "unknown", "read"


def ledger_json(value: Any, limit: int = MAX_LEDGER_JSON_CHARACTERS) -> str:
    """`value` as compact JSON, cut at `limit` characters. A cut string needn't be valid JSON."""
    try:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    except Exception:
        rendered = str(value)
    return rendered[:limit]


def _one_line(text: str) -> str:
    return " ".join(text.split())


def _first_line(text: str) -> str:
    return _one_line(next((line for line in text.splitlines() if line.strip()), ""))


def _block_text(blocks: Sequence[Any]) -> str:
    return "\n".join(
        block["text"] for block in blocks if isinstance(block, dict) and isinstance(block.get("text"), str)
    )


def _output_text(value: Any) -> str:
    """The readable text of a tool's output, or "" when it has none."""
    if isinstance(value, str):
        return value
    if isinstance(value, list):
        return _block_text(value)
    if not isinstance(value, dict):
        return ""
    stdout, stderr = value.get("stdout"), value.get("stderr")
    if isinstance(stdout, str) or isinstance(stderr, str):
        if isinstance(stdout, str) and stdout.strip():
            return stdout
        return stderr if isinstance(stderr, str) else ""
    content = value.get("content")
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return _block_text(content)
    return ""


def _skip_exit_code(error: str) -> str:
    lines = error.splitlines()
    if lines and EXIT_CODE_LINE.fullmatch(lines[0].strip()):
        lines = lines[1:]
    return "\n".join(lines)


def ledger_result(*, hook_input: Mapping[str, Any]) -> Tuple[str, int]:
    """(kind, exit_status) for a call Claude Code reports back on."""
    if hook_input.get("hook_event_name") == "PostToolUseFailure":
        error = hook_input.get("error")
        lines = error.splitlines() if isinstance(error, str) else []
        match = EXIT_CODE_LINE.fullmatch(lines[0].strip()) if lines else None
        return "failed", int(match.group(1)) if match else 1
    response = hook_input.get("tool_response")
    if isinstance(response, dict) and response.get("interrupted") is True:
        return "failed", 1
    if result_has_known_error(response):
        exit_code = response.get("exit_code")
        if isinstance(exit_code, int) and not isinstance(exit_code, bool) and exit_code != 0:
            return "failed", exit_code
        return "failed", 1
    return "ran", 0


def ledger_context(*, account: str, hook_input: Mapping[str, Any]) -> Dict[str, Any]:
    """{incident_key, references, thread_id} for this session, absent ones left out.

    The session state's investigation wins over the environment's, and the two are never mixed.
    """
    state: Any = {}
    session_id = hook_input.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        try:
            state = json.loads(session_state_path(session_id.strip()).read_text(encoding="utf-8"))
        except Exception:
            state = {}
    if not isinstance(state, dict):
        state = {}
    context: Dict[str, Any] = {}
    if isinstance(state.get("references"), list):
        context["references"] = state["references"]
    investigation = state.get("investigation")
    if isinstance(investigation, dict) and isinstance(investigation.get("thread_id"), str) \
            and investigation["thread_id"].strip():
        incident_key = investigation.get("incident_key")
        if isinstance(incident_key, str) and incident_key.strip():
            context["incident_key"] = incident_key.strip()
        context["thread_id"] = investigation["thread_id"].strip()
        return context
    incident_key = _setting(LEDGER_INCIDENT_KEY_ENV)
    thread_id = _setting(LEDGER_INVESTIGATION_ID_ENV)
    if thread_id is None and incident_key is not None:
        thread_id = str(uuid.uuid5(uuid.NAMESPACE_URL, f"mezmo-investigation:{account}:{incident_key}"))
    if incident_key is not None:
        context["incident_key"] = incident_key
    if thread_id is not None:
        context["thread_id"] = thread_id
    return context


def ledger_envelope(
    *,
    account: str,
    correlation_id: Optional[str],
    entity_id: str,
    entity_type: str,
    event: str,
    extra: Mapping[str, Any],
    message: str,
    session_id: str,
) -> Dict[str, Any]:
    """The top-level fields audit-service takes, for any entity. `event` is the event name."""
    envelope: Dict[str, Any] = {
        "actor": {"account": account, "pk": f"{AGENT}:{session_id}", "type": "service"},
        "application": {"name": LEDGER_APPLICATION, "version": plugin_version()},
    }
    if correlation_id is not None:
        envelope["correlation_id"] = correlation_id
    envelope.update(
        {
            "entity_id": entity_id,
            "entity_type": entity_type,
            "event": event,
            "extra": dict(extra),
            "internal": False,
            "message": message,
            "timestamp": datetime.now(timezone.utc).isoformat(),
        }
    )
    return envelope


def _duration_ms(*, value: Any) -> Optional[Union[int, float]]:
    """A finite, non-negative number, or None. An int too big for a float counts as missing."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        finite = math.isfinite(value)
    except OverflowError:
        return None
    return value if finite and value >= 0 else None


def ledger_event(
    *,
    config: LedgerConfig,
    exit_status: Optional[int] = None,
    hook_input: Mapping[str, Any],
    kind: str,
    outcome: Optional[str] = None,
    reason: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """One tool-call event: `kind` is started, ran, failed or not_run. None without an identity."""
    try:
        tool = normalize_tool_call(hook_input)
        identity = call_identity(hook_input)
    except Exception:
        return None
    target, access = resolve_target(tool.namespace, tool.name, tool.arguments)
    description = tool.arguments.get("description") if (tool.namespace, tool.name) == (AGENT, "Bash") else None
    described = isinstance(description, str) and bool(description.strip())
    intent = _one_line(description) if described else f"{tool.namespace}.{tool.name}"
    context = ledger_context(account=config.account, hook_input=hook_input)

    extra: Dict[str, Any] = {"access": access, "agent": AGENT}
    if kind == "started":
        extra["arguments"] = ledger_json(tool.arguments)
    extra["call_id"] = identity.decision_id
    if load_config() is not None:
        extra["decision_id"] = identity.decision_id
    duration = _duration_ms(value=hook_input.get("duration_ms"))
    if kind in ("failed", "ran") and duration is not None:
        extra["duration_ms"] = duration
    extra["entry_key"] = f"{identity.decision_id}:{'start' if kind == 'started' else 'end'}"
    if kind in ("failed", "ran") and exit_status is not None:
        extra["exit_status"] = exit_status
    if "incident_key" in context:
        extra["incident_key"] = context["incident_key"]
    extra["intent_source"] = "tool_description" if described else "none"

    line = ""
    if kind == "not_run":
        detail = _one_line(reason)[:MAX_DETAIL_CHARACTERS] if isinstance(reason, str) else ""
        result: Dict[str, Any] = {"outcome": outcome}
        if detail:
            result["reason"] = detail
        extra["output"] = ledger_json(result)
        line = detail
    elif kind in ("failed", "ran"):
        error = hook_input.get("error") if kind == "failed" else None
        if error is not None:
            extra["output"] = ledger_json(error)
        elif "tool_response" in hook_input:
            extra["output"] = ledger_json(hook_input["tool_response"])
        if hook_input.get("hook_event_name") == "PostToolUseFailure" and isinstance(error, str):
            line = _first_line(_skip_exit_code(error))
        else:
            line = _first_line(_output_text(error if error is not None else hook_input.get("tool_response")))

    if "references" in context:
        extra["references"] = context["references"]
    extra["request_id"] = identity.request_id
    extra["session_id"] = identity.session_id
    extra["target"] = target
    if "thread_id" in context:
        extra["thread_id"] = context["thread_id"]
    extra["tool_name"] = tool.name
    extra["tool_namespace"] = tool.namespace

    message = intent
    if kind in LEDGER_ENDINGS:
        message = f"{intent}: {LEDGER_ENDINGS[kind]}" + (f" - {line}" if line else "")
    return ledger_envelope(
        account=config.account,
        correlation_id=context.get("thread_id"),
        entity_id=target,
        entity_type="tool_call",
        event=f"agent.tool_call.{kind}",
        extra=extra,
        message=message[:MAX_LEDGER_MESSAGE_CHARACTERS],
        session_id=identity.session_id,
    )


def post_ledger(*, events: Sequence[Mapping[str, Any]], key: Optional[str], timeout: float, url: str) -> Tuple[int, bytes]:
    """POST `events` as one JSON array. (status, body); OSError on a network failure or timeout.

    `key` is the whole Authorization value, sent as given; None sends no header.
    """
    refuse_exposed_token(url, key or "")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = key
    body = json.dumps(list(events), separators=(",", ":")).encode()
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        # A 3xx comes back as its status, so the events and key never go to another URL.
        with build_opener(RefuseRedirect).open(request, timeout=timeout) as response:
            return response.status, response.read()
    except HTTPError as error:
        try:
            return error.code, error.read()
        except (HTTPException, OSError):
            return error.code, b""
    except HTTPException as error:
        # A reply cut short is a network failure too.
        raise OSError(str(error) or type(error).__name__) from error


def _ledger_statuses(body: Any) -> Optional[Dict[int, str]]:
    """Each event's status by index from a 207 body, or None when it has no usable list."""
    try:
        parsed = json.loads(body)
    except Exception:
        return None
    entries = parsed.get("events") if isinstance(parsed, dict) else None
    if not isinstance(entries, list):
        return None
    statuses: Dict[int, str] = {}
    for entry in entries:
        if isinstance(entry, dict) and isinstance(entry.get("index"), int) and isinstance(entry.get("status"), str):
            statuses[entry["index"]] = entry["status"]
    return statuses


def _retryable(status: int) -> bool:
    # A 401 is a key to fix, so the events wait for it.
    return status in (401, 408, 429) or 500 <= status < 600


def _open_private(*, mode: str, path: Path) -> Any:
    """Open `path` for writing as its owner's alone, as append_audit does."""

    def private(name: str, flags: int) -> int:
        return os.open(name, flags, 0o600)

    # surrogateescape writes back, byte for byte, a line _read_lines read with a bad byte.
    handle = open(path, mode, encoding="utf-8", errors="surrogateescape", opener=private)
    # A file left from before keeps its mode until narrowed here.
    file_mode = os.fstat(handle.fileno()).st_mode
    if os.name != "nt" and stat.S_ISREG(file_mode) and file_mode & 0o077:
        os.fchmod(handle.fileno(), 0o600)
    return handle


def _spool_ledger(*, config: LedgerConfig, events: Sequence[Mapping[str, Any]]) -> None:
    """Append `events` to the retry file. The file keeps arguments and output, so it's private."""
    if not events:
        return
    path = ledger_spool_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    text = "".join(json.dumps({"event": event, "url": config.url}, separators=(",", ":")) + "\n" for event in events)
    with _locked(path):
        with _open_private(mode="a", path=path) as handle:
            handle.write(text)


def _save_ledger(*, config: LedgerConfig, events: Sequence[Mapping[str, Any]]) -> None:
    try:
        _spool_ledger(config=config, events=events)
    except Exception as error:
        LEDGER_MESSAGES.append(f"Mezmo ledger could not save {len(events)} event(s) for retry: {error}")


def send_ledger(
    *, config: LedgerConfig, events: Sequence[Mapping[str, Any]], ledger_http: LedgerHttp
) -> LedgerSendResult:
    """Send new events in one request and say which to save for retry. Never raises.

    Nothing here writes the retry file: the caller saves `save`, so an event is saved at most
    once. Events the server refuses for good are dropped.
    """
    events = list(events)
    try:
        try:
            status, body = ledger_http(events=events, key=config.key, timeout=LEDGER_TIMEOUT_SECONDS, url=config.url)
        except OSError:
            return LedgerSendResult(answered=False, save=events)
        if status == 207:
            statuses = _ledger_statuses(body)
            if statuses is None:
                return LedgerSendResult(answered=True, save=events)
            return LedgerSendResult(
                answered=True, save=[event for index, event in enumerate(events) if statuses.get(index) == "rejected"]
            )
        if 200 <= status < 300:
            return LedgerSendResult(answered=True, save=[])
        if _retryable(status):
            return LedgerSendResult(answered=True, save=events)
        LEDGER_MESSAGES.append(f"Mezmo ledger dropped {len(events)} event(s): the ledger service answered HTTP {status}")
        return LedgerSendResult(answered=True, save=[])
    except Exception as error:
        LEDGER_MESSAGES.append(f"Mezmo ledger dropped {len(events)} event(s): {str(error) or type(error).__name__}")
        return LedgerSendResult(answered=False, save=[])


def _read_lines(path: Path) -> List[str]:
    # A bad byte must not stop every later drain: it reads as a lone surrogate, and its line is bad.
    lines = path.read_text(encoding="utf-8", errors="surrogateescape").split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return lines


@contextmanager
def _drain_lock(path: Path) -> Iterator[bool]:
    """Try for `path`.drain.lock without waiting. Yields whether this run may drain."""
    with open(f"{path}.drain.lock", "a") as lock:
        held = True
        try:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except ImportError:
            pass  # no fcntl on Windows: drain anyway
        except OSError as error:
            # Only a lock that's taken means another hook is draining. Any other failure, such
            # as ENOLCK, drains anyway, as _locked carries on without its lock.
            held = error.errno not in (errno.EAGAIN, errno.EWOULDBLOCK)
        yield held


def drain_ledger_spool(*, config: LedgerConfig, ledger_http: LedgerHttp) -> None:
    """Resend up to LEDGER_DRAIN_BATCH saved events for this URL in one request. Never raises.

    A resent event is never saved a second time: its line stays until the server takes it or
    refuses it for good.
    """
    try:
        path = ledger_spool_path()
        if not path.exists():
            return
        with _drain_lock(path) as held:
            if not held:
                return
            with _locked(path):
                lines = _read_lines(path)
            taken: List[Tuple[str, Dict[str, Any]]] = []
            finished: List[str] = []
            for line in lines:
                try:
                    line.encode("utf-8")  # a line with a bad byte is bad
                    entry = json.loads(line)
                except ValueError:
                    entry = None
                usable = isinstance(entry, dict) and isinstance(entry.get("event"), dict) and isinstance(entry.get("url"), str)
                if not usable:
                    finished.append(line)  # a bad line
                elif entry["url"] == config.url and len(taken) < LEDGER_DRAIN_BATCH:
                    taken.append((line, entry["event"]))
            if taken:
                finished.extend(_resend_ledger(config=config, ledger_http=ledger_http, taken=taken))
            if not finished:
                return
            with _locked(path):
                remaining = _read_lines(path)
                for line in finished:
                    if line in remaining:
                        remaining.remove(line)
                temporary = Path(f"{path}.tmp")
                with _open_private(mode="w", path=temporary) as handle:
                    handle.write("".join(line + "\n" for line in remaining))
                os.replace(temporary, path)
    except Exception as error:
        LEDGER_MESSAGES.append(f"Mezmo ledger could not resend saved events: {str(error) or type(error).__name__}")


def _resend_ledger(
    *, config: LedgerConfig, ledger_http: LedgerHttp, taken: Sequence[Tuple[str, Dict[str, Any]]]
) -> List[str]:
    """Send saved events. Returns the lines that are finished: delivered, or refused for good."""
    try:
        status, body = ledger_http(
            events=[event for _, event in taken], key=config.key, timeout=LEDGER_TIMEOUT_SECONDS, url=config.url
        )
    except OSError:
        return []
    except Exception as error:
        LEDGER_MESSAGES.append(f"Mezmo ledger could not resend saved events: {str(error) or type(error).__name__}")
        return []
    if status == 207:
        statuses = _ledger_statuses(body) or {}
        return [line for index, (line, _) in enumerate(taken) if statuses.get(index) == "accepted"]
    if 200 <= status < 300:
        return [line for line, _ in taken]
    if _retryable(status):
        return []
    LEDGER_MESSAGES.append(f"Mezmo ledger dropped {len(taken)} saved event(s): the ledger service answered HTTP {status}")
    return [line for line, _ in taken]


def start_ledger(*, work: Callable[[], None]) -> threading.Thread:
    """Run `work` on a daemon thread that can't raise, and remember the thread."""

    def run() -> None:
        try:
            work()
        except Exception as error:
            LEDGER_MESSAGES.append(f"Mezmo ledger failed: {str(error) or type(error).__name__}")

    thread = threading.Thread(target=run, name="mezmo-ledger", daemon=True)
    LEDGER_THREADS.append(thread)
    thread.start()
    return thread


def _ledger_warn(*, message: str) -> None:
    """Print to stderr. A stderr that fails must not stop the hook, so this never raises."""
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass


def _print_ledger_messages() -> None:
    while LEDGER_MESSAGES:
        _ledger_warn(message=LEDGER_MESSAGES.pop(0))


def _build_ledger_event(**fields: Any) -> Optional[Dict[str, Any]]:
    try:
        return ledger_event(**fields)
    except Exception as error:
        _ledger_warn(message=f"Mezmo ledger could not build an event: {str(error) or type(error).__name__}")
        return None


def _ledger_start(
    *, config: LedgerConfig, drain: bool, event: Dict[str, Any], ledger_http: LedgerHttp
) -> Callable[[], bool]:
    """Send `event` on one thread and, when `drain`, resend saved events on another. Never raises.

    Both share one deadline from now. Returns `finish`, which waits for them until the deadline,
    saves `event` unless the send thread finished and said not to, prints the ledger messages,
    and returns whether the ledger service answered. A thread still running is abandoned: it
    never saves, and the duplicate it may still deliver is accepted.
    """
    deadline = time.monotonic() + LEDGER_TIMEOUT_SECONDS
    sent: List[LedgerSendResult] = []
    threads: List[threading.Thread] = []
    try:
        threads.append(
            start_ledger(work=lambda: sent.append(send_ledger(config=config, events=[event], ledger_http=ledger_http)))
        )
        if drain:
            threads.append(start_ledger(work=lambda: drain_ledger_spool(config=config, ledger_http=ledger_http)))
    except Exception as error:
        _ledger_warn(message=f"Mezmo ledger could not start: {str(error) or type(error).__name__}")

    def finish() -> bool:
        try:
            for thread in threads:
                thread.join(max(0.0, deadline - time.monotonic()))
            result = sent[0] if sent else LedgerSendResult(answered=False, save=[event])
            _save_ledger(config=config, events=result.save)
            _print_ledger_messages()
            return result.answered
        except Exception as error:
            _ledger_warn(message=f"Mezmo ledger failed: {str(error) or type(error).__name__}")
            return False

    return finish


def _ledger_report(*, config: LedgerConfig, ledger_http: LedgerHttp, **fields: Any) -> None:
    """Build one event, send it while resending saved events, and wait. Never raises."""
    try:
        event = _build_ledger_event(config=config, **fields)
        if event is not None:
            _ledger_start(config=config, drain=True, event=event, ledger_http=ledger_http)()
    except Exception as error:
        _ledger_warn(message=f"Mezmo ledger failed: {str(error) or type(error).__name__}")


def _ledger_not_run(
    *,
    config: LedgerConfig,
    hook_input: Mapping[str, Any],
    ledger_http: LedgerHttp,
    outcome: str,
    reason: Optional[str],
    send: bool,
) -> None:
    """Record governance's refusal: sent alone when `send`, otherwise saved without a request."""
    try:
        event = _build_ledger_event(config=config, hook_input=hook_input, kind="not_run", outcome=outcome, reason=reason)
        if event is None:
            return
        if send:
            _ledger_start(config=config, drain=False, event=event, ledger_http=ledger_http)()
            return
        _save_ledger(config=config, events=[event])
        _print_ledger_messages()
    except Exception as error:
        _ledger_warn(message=f"Mezmo ledger failed: {str(error) or type(error).__name__}")


def handle_event(
    event: Mapping[str, Any],
    http: HttpJson = http_json,
    clock: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
    *,
    ledger_http: Optional[LedgerHttp] = None,
) -> Optional[Dict[str, Any]]:
    # Looked up per call, not bound at definition, so tests can stand in for it.
    ledger_http = ledger_http or post_ledger
    event_name = event.get("hook_event_name")
    if event_name in {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied"} and not governed(event):
        return None
    configured = load_config() is not None
    ledger: Optional[LedgerConfig] = None
    if event_name in {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied"}:
        try:
            ledger = load_ledger_config()
        except Exception:
            ledger = None
    if event_name == "PreToolUse":
        governance_on = configured or governance_required()
        if not governance_on and ledger is None:
            return None
        # Started before governance runs, so governance's time counts against the deadline.
        finish_started: Optional[Callable[[], bool]] = None
        if ledger is not None:
            try:
                started = _build_ledger_event(config=ledger, hook_input=event, kind="started")
                if started is not None:
                    finish_started = _ledger_start(config=ledger, drain=True, event=started, ledger_http=ledger_http)
            except Exception:
                pass  # governance runs regardless
        refusal: List[Tuple[str, Optional[str]]] = []
        output = None
        if governance_on:
            try:
                output = handle_pre_tool_use(
                    event, http, clock, sleep, on_denied=lambda detail: refusal.append(("denied", detail))
                )
            except ApprovalExpired as error:
                refusal.append(("expired", str(error)))
                output = deny_output(f"Mezmo Governance could not authorize this exact tool call, so Claude Code blocked it: {error}")
            except Exception as error:
                refusal.append(("error", str(error)))
                output = deny_output(f"Mezmo Governance could not authorize this exact tool call, so Claude Code blocked it: {error}")
        if ledger is not None:
            answered = finish_started() if finish_started is not None else False
            if output is not None and refusal:
                outcome, reason = refusal[0]
                # Sending is only worth the delay when the service just answered `started`.
                _ledger_not_run(
                    config=ledger,
                    hook_input=event,
                    ledger_http=ledger_http,
                    outcome=outcome,
                    reason=reason,
                    send=answered and outcome != "expired",
                )
        return output
    if event_name in {"PostToolUse", "PostToolUseFailure"}:
        if configured:
            try:
                handle_post_tool_use(event)
            except Exception as error:
                print(f"Mezmo Governance could not write the local audit log: {error}", file=sys.stderr)
        if ledger is not None:
            try:
                kind, exit_status = ledger_result(hook_input=event)
            except Exception:
                kind, exit_status = "failed", 1
            _ledger_report(
                config=ledger, exit_status=exit_status, hook_input=event, kind=kind, ledger_http=ledger_http
            )
        return None
    if event_name == "PermissionDenied":
        if configured:
            try:
                handle_permission_denied(event)
            except Exception as error:
                print(f"Mezmo Governance could not record the auto mode block: {error}", file=sys.stderr)
        if ledger is not None:
            reason = event.get("reason")
            _ledger_report(
                config=ledger,
                hook_input=event,
                kind="not_run",
                ledger_http=ledger_http,
                outcome="blocked_by_auto_mode",
                reason=reason if isinstance(reason, str) else None,
            )
        return None
    raise HookContractError(f"unsupported hook event: {event_name!r}")


def main() -> int:
    first_thread = len(LEDGER_THREADS)

    def finish(code: int) -> int:
        # A ledger thread still waiting on the network would hold up the interpreter's exit.
        if any(thread.is_alive() for thread in LEDGER_THREADS[first_thread:]):
            try:
                sys.stdout.flush()
                sys.stderr.flush()
            except Exception:
                pass
            os._exit(code)
        return code

    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise HookContractError("hook input must be a JSON object")
        output = handle_event(event)
    except Exception as error:
        # Any failure blocks, such as a RecursionError on deeply nested input.
        reason = f"Mezmo Governance blocked this tool call: {str(error) or type(error).__name__}"
        try:
            print(json.dumps(deny_output(reason), separators=(",", ":")))
            print(reason, file=sys.stderr)
        except OSError:
            pass
        return finish(2)
    if output is not None:
        print(json.dumps(output, separators=(",", ":")))
    return finish(0)


if __name__ == "__main__":
    raise SystemExit(main())
