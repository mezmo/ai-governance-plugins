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
- Off until configured: with no access key, the hook allows every call and records
  nothing, unless MEZMO_GOVERNANCE_REQUIRED=1, which denies instead. Once configured it
  fails closed, as the adapter does.
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
"""

from __future__ import annotations

import hashlib
import ipaddress
import json
import math
import os
import re
import socket
import stat
import sys
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
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
    key = required_config().access_key
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


@contextmanager
def _locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on `path`.lock, so parallel tool calls don't lose updates."""
    with open(f"{path}.lock", "a") as lock:
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


def handle_pre_tool_use(event: Mapping[str, Any], http: HttpJson, clock: Clock, sleep: Sleeper) -> Optional[Dict[str, Any]]:
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


def handle_event(
    event: Mapping[str, Any],
    http: HttpJson = http_json,
    clock: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> Optional[Dict[str, Any]]:
    event_name = event.get("hook_event_name")
    if event_name in {"PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied"} and not governed(event):
        return None
    configured = load_config() is not None
    if event_name == "PreToolUse":
        if not configured and not governance_required():
            return None
        try:
            return handle_pre_tool_use(event, http, clock, sleep)
        except Exception as error:
            return deny_output(f"Mezmo Governance could not authorize this exact tool call, so Claude Code blocked it: {error}")
    if event_name in {"PostToolUse", "PostToolUseFailure"}:
        if not configured:
            return None
        try:
            handle_post_tool_use(event)
        except Exception as error:
            print(f"Mezmo Governance could not write the local audit log: {error}", file=sys.stderr)
        return None
    if event_name == "PermissionDenied":
        if not configured:
            return None
        try:
            handle_permission_denied(event)
        except Exception as error:
            print(f"Mezmo Governance could not record the auto mode block: {error}", file=sys.stderr)
        return None
    raise HookContractError(f"unsupported hook event: {event_name!r}")


def main() -> int:
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
        return 2
    if output is not None:
        print(json.dumps(output, separators=(",", ":")))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
