#!/usr/bin/env python3
"""Windsurf Cascade pre_* / post_* hook for Mezmo Governance."""

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
import threading
import time
import uuid
from collections.abc import Callable, Mapping
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, NamedTuple, Optional, Sequence, Tuple, Union
from urllib.error import HTTPError, URLError
from urllib.parse import parse_qs, urlencode, urlparse, urlunparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

AGENT = "windsurf"
DEFAULT_TIMEOUT_SECONDS = 590.0
MAX_TIMEOUT_SECONDS = 590.0
DEFAULT_POLL_INTERVAL_SECONDS = 5.0
MIN_POLL_INTERVAL_SECONDS = 1.0
DEFAULT_AUDIT_LOG = Path.home() / ".mezmo" / "governance-audit.jsonl"
MAX_RESULT_PREVIEW_CHARACTERS = 2_000
# Each built-in Cascade action, with the tool_info field the call cannot be judged without.
BUILT_IN_ACTIONS = {"run_command": "command_line", "read_code": "file_path", "write_code": "file_path"}
MCP_ACTION = "mcp_tool_use"
# Mezmo takes up to 100 items a request. A command with more parts goes whole.
MAX_BASH_PARTS = 50
# Each argument is a row in Mezmo, and rules rarely look past the first few.
MAX_BASH_ARGS = 10
# The service's limit on an argument name (ai-governance-service schema/constants.js).
ARGUMENT_NAME_MAX = 64

HttpJson = Callable[[str, str, Optional[Mapping[str, Any]], float], tuple[int, dict[str, Any]]]
Sleeper = Callable[[float], None]
Clock = Callable[[], float]


class HookContractError(ValueError):
    pass


class NormalizedToolCall(NamedTuple):
    namespace: str
    name: str
    arguments: dict[str, Any]


class CallIdentity(NamedTuple):
    decision_id: str
    request_id: str
    session_id: str


class Decision(NamedTuple):
    approved: bool
    reason: str | None


def required_string(value: Any, field: str) -> str:
    if not isinstance(value, str) or not value.strip():
        raise HookContractError(f"{field} must be a non-empty string")
    return value.strip()


def parse_object(raw: bytes) -> dict[str, Any]:
    if not raw:
        return {}
    try:
        parsed = json.loads(raw)
    except json.JSONDecodeError as error:
        raise HookContractError(
            "Mezmo Governance returned an invalid JSON response"
        ) from error
    if not isinstance(parsed, dict):
        raise HookContractError("Mezmo Governance returned a non-object response")
    return parsed


def authorize_url() -> str:
    return required_string(
        os.environ.get("MEZMO_GOVERNANCE_URL"),
        "MEZMO_GOVERNANCE_URL",
    )


def status_url(decision_id: str, base: str | None = None) -> str:
    parsed = urlparse(base or authorize_url())
    query = parse_qs(parsed.query, keep_blank_values=True)
    query["decision_id"] = [decision_id]
    return urlunparse(parsed._replace(query=urlencode(query, doseq=True)))


def authorization_header() -> str:
    key = required_string(
        os.environ.get("MEZMO_GOVERNANCE_TOKEN"),
        "MEZMO_GOVERNANCE_TOKEN",
    )
    if key.lower().startswith("token ") or key.lower().startswith("bearer "):
        return key
    return f"Token {key}"


def positive_seconds(environment_name: str, default: float) -> float:
    configured = os.environ.get(environment_name)
    if configured is None:
        return default
    try:
        seconds = float(configured)
    except ValueError as error:
        raise HookContractError(f"{environment_name} must be a number") from error
    if not math.isfinite(seconds) or seconds <= 0:
        raise HookContractError(
            f"{environment_name} must be a finite number greater than zero"
        )
    return seconds


def timeout_seconds() -> float:
    # The runtime kills the hook at its 600 second timeout; finish first.
    return min(
        positive_seconds("MEZMO_GOVERNANCE_TIMEOUT_SECONDS", DEFAULT_TIMEOUT_SECONDS),
        MAX_TIMEOUT_SECONDS,
    )


def poll_interval_seconds() -> float:
    return max(
        positive_seconds(
            "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS",
            DEFAULT_POLL_INTERVAL_SECONDS,
        ),
        MIN_POLL_INTERVAL_SECONDS,
    )


def audit_log_path() -> Path:
    configured = os.environ.get("MEZMO_GOVERNANCE_AUDIT_LOG")
    if configured:
        return Path(configured).expanduser()
    return DEFAULT_AUDIT_LOG


def server_namespace(name: str) -> str:
    """Refuse a server or extension named like the agent: its tools would pass as built-ins."""
    if name.lower() == AGENT:
        raise HookContractError(
            f"a tool namespace named {name!r} would pass as a built-in tool"
        )
    return name


def hook_action(event: Mapping[str, Any]) -> tuple[str, str]:
    """("pre" or "post", the action) from agent_action_name, such as pre_run_command."""
    name = required_string(event.get("agent_action_name"), "agent_action_name")
    when, _, action = name.partition("_")
    if when not in ("pre", "post") or (action not in BUILT_IN_ACTIONS and action != MCP_ACTION):
        raise HookContractError(f"unsupported hook event: {name!r}")
    return when, action


def normalize_tool_call(event: Mapping[str, Any]) -> NormalizedToolCall:
    _, action = hook_action(event)
    tool_info = event.get("tool_info")
    if not isinstance(tool_info, dict):
        raise HookContractError("tool_info must be a JSON object")

    if action == MCP_ACTION:
        server = required_string(tool_info.get("mcp_server_name"), "mcp_server_name")
        name = required_string(tool_info.get("mcp_tool_name"), "mcp_tool_name")
        arguments = tool_info.get("mcp_tool_arguments")
        if not isinstance(arguments, dict):
            raise HookContractError("mcp_tool_arguments must be a JSON object")
        return NormalizedToolCall(server_namespace(server), name, dict(arguments))

    required_string(tool_info.get(BUILT_IN_ACTIONS[action]), BUILT_IN_ACTIONS[action])
    return NormalizedToolCall(AGENT, action, dict(tool_info))


def call_digest(namespace: str, name: str, arguments: Mapping[str, Any]) -> str:
    """Hash the exact call, so an edited call never reuses an earlier decision."""
    canonical = json.dumps(
        [namespace, name, dict(arguments)], sort_keys=True, separators=(",", ":")
    )
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


def call_identity(
    event: Mapping[str, Any], tool_call: NormalizedToolCall
) -> CallIdentity:
    # The trajectory is the Cascade conversation.
    session_id = required_string(event.get("trajectory_id"), "trajectory_id")
    # Windsurf sends no tool call id. A fresh id per run keeps a repeated
    # identical call from reusing an earlier decision through HTTP 409.
    request_id = f"{AGENT}:{session_id}:run-{uuid.uuid4()}"
    request_id += "#" + call_digest(*tool_call)
    decision_id = str(uuid.uuid5(uuid.NAMESPACE_URL, request_id))
    return CallIdentity(decision_id, bounded_request_id(request_id), session_id)


def origin(event: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "kind": "windsurf_hook",
        "reason": str(event.get("agent_action_name") or "Windsurf Cascade hook"),
    }


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
# in kubectl -n prod lands among the arguments). This copy must match Claude Code's, which
# codex/test_hook.py checks.

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


def drop_part_fields(arguments: Mapping[str, Any]) -> Dict[str, Any]:
    """A shell call's input without program, full_command, arg_<digits> or any flag_ name.

    Only the parser writes these. Taken from the agent's input, they could make a part it can't
    read look read, so a shell call never carries them from there, whether it goes as parts or
    whole.
    """
    return {
        name: value
        for name, value in arguments.items()
        if name not in ("program", "full_command")
        and not name.startswith("flag_")
        and not re.fullmatch(r"arg_[0-9]+", name)
    }


def bash_items(
    arguments: Mapping[str, Any], tool_name: str = "Bash", command_field: str = "command"
) -> Optional[List[Dict[str, Any]]]:
    """One item per part of a shell command, or None to send the call whole.

    The first item leads with the agent's input as received and the whole command, then each
    item has its part's fields. The input's own program, full_command, arg_<digits> and flag_
    names are dropped, since only the parser sets those (drop_part_fields). A parser failure
    only means the call goes whole: it never blocks it.
    """
    command = arguments.get(command_field)
    if not isinstance(command, str):
        return None
    try:
        parts = split_command(command)
    except Exception:
        return None
    if not parts or len(parts) > MAX_BASH_PARTS:
        return None
    rest = {name: value for name, value in drop_part_fields(arguments).items() if name != "command"}
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
        item: Dict[str, Any] = {"id": index, "tool_namespace": AGENT, "tool_name": tool_name, "arguments": fields}
        if index and part.after in SEQUENTIAL_SEPARATORS:
            item["dependencies"] = [index - 1]
        items.append(item)
    return items


# The shell tools a command is split for, each with the input field that holds the command.
SHELL_TOOLS = {"run_command": "command_line"}


def shell_command_field(tool_call: NormalizedToolCall) -> Optional[str]:
    """The input field holding the command of a shell tool the agent runs, else None."""
    if tool_call.namespace != AGENT:
        return None
    return SHELL_TOOLS.get(tool_call.name)


def approval_payload(event: Mapping[str, Any]) -> dict[str, Any]:
    tool_call = normalize_tool_call(event)
    identity = call_identity(event, tool_call)
    field = shell_command_field(tool_call)
    # On Windows the agent runs commands in PowerShell or cmd, which the bash parser would
    # misread, so a shell call goes whole there.
    splits = field is not None and os.name != "nt"
    items = (bash_items(tool_call.arguments, tool_call.name, field) if splits else None) or [
        {
            "id": 0,
            "tool_namespace": tool_call.namespace,
            "tool_name": tool_call.name,
            # Split or whole, a shell call never takes the parser's field names from the input.
            "arguments": drop_part_fields(tool_call.arguments) if field else tool_call.arguments,
        }
    ]
    return {
        "version": 1,
        "decision_id": identity.decision_id,
        "request_id": identity.request_id,
        "scope": {
            "kind": "single",
            "session_id": identity.session_id,
        },
        "origin": origin(event),
        "items": items,
    }


class RefuseRedirect(HTTPRedirectHandler):
    """Hand a 3xx back as an error, so the token never goes to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def is_loopback(host: str | None) -> bool:
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
        raise HookContractError(
            "the Mezmo Governance URL must not hold a user name or password"
        )
    if not (
        parsed.scheme == "https"
        or (parsed.scheme == "http" and is_loopback(parsed.hostname))
    ):
        raise HookContractError(
            "the Mezmo Governance URL must use https://; "
            "http:// is allowed only to this machine"
        )
    # http.client quotes a header value it refuses, which would show the token.
    if not (authorization.isascii() and authorization.isprintable()):
        raise HookContractError(
            "the Mezmo Governance token holds a character an HTTP header cannot carry"
        )


def http_json(
    method: str,
    url: str,
    payload: Mapping[str, Any] | None,
    timeout: float,
) -> tuple[int, dict[str, Any]]:
    body = (
        None
        if payload is None
        else json.dumps(payload, separators=(",", ":")).encode()
    )
    authorization = authorization_header()
    refuse_exposed_token(url, authorization)
    headers = {
        "Authorization": authorization,
        "Accept": "application/json",
    }
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
        if isinstance(reason, (TimeoutError, socket.timeout)):
            raise TimeoutError(str(error)) from error
        if "timed out" in str(error).lower():
            raise TimeoutError(str(error)) from error
        raise HookContractError(f"Mezmo Governance is unavailable: {error}") from error
    except socket.timeout as error:
        raise TimeoutError(str(error)) from error


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


def remaining_seconds(deadline: float, clock: Clock) -> float:
    leftover = deadline - clock()
    if leftover <= 0:
        raise HookContractError("Mezmo Governance: approval still pending")
    return leftover


def decision_from_post_body(body: Mapping[str, Any]) -> Decision:
    approved = body.get("approved")
    if not isinstance(approved, bool):
        raise HookContractError("Mezmo Governance did not return an approval decision")
    reason = body.get("reason")
    return Decision(approved, reason if isinstance(reason, str) else None)


def decision_from_status_body(body: Mapping[str, Any]) -> Decision | None:
    reason = body.get("reason")
    detail = reason if isinstance(reason, str) else None
    # Mezmo answers the poll with {approved, reason}. A status field is the older shape.
    approved = body.get("approved")
    if isinstance(approved, bool):
        return Decision(True, detail) if approved else Decision(False, detail or "Policy denied the call.")
    status = body.get("status")
    if status == "approved":
        return Decision(True, detail)
    if status == "denied":
        return Decision(False, detail or "Policy denied the call.")
    if status == "pending":
        return None
    raise HookContractError(
        f"Mezmo Governance returned an unexpected status: {status!r}"
    )


def poll_decision(
    decision_id: str,
    deadline: float,
    http: HttpJson,
    clock: Clock,
    sleep: Sleeper,
) -> Decision:
    url = status_url(decision_id)
    interval = poll_interval_seconds()
    while True:
        leftover = remaining_seconds(deadline, clock)
        try:
            status, body = http("GET", url, None, leftover)
        except TimeoutError as error:
            raise HookContractError(
                f"Mezmo Governance is unavailable: {error}"
            ) from error
        # An answer that arrives after the deadline is too late to act on.
        leftover = remaining_seconds(deadline, clock)
        # 404: not recorded yet. 207: Mezmo still waits for a person.
        if status in (404, 207):
            sleep(min(interval, leftover))
            continue
        if status != 200:
            raise HookContractError(http_failure(status, body, " while polling"))
        decided = decision_from_status_body(body)
        if decided is not None:
            return decided
        sleep(min(interval, leftover))


def deadline_watchdog(seconds: float) -> Callable[[], None]:
    """Deny and exit at the deadline, even while a socket read is still alive.

    The opener's timeout bounds each socket wait, not the whole answer, so a
    server that sends one byte at a time could keep the hook past the
    runtime's own hook timeout, and several runtimes then run the tool.
    """
    lock = threading.Lock()
    finished = False

    def fire() -> None:
        with lock:
            if finished:
                return
            reason = "Mezmo Governance: approval still pending"
            try:
                sys.stdout.write(
                    json.dumps(deny_output(reason), separators=(",", ":")) + "\n"
                )
                sys.stdout.flush()
                sys.stderr.write(reason + "\n")
                sys.stderr.flush()
            finally:
                # Every supported runtime blocks the tool on exit code 2, so
                # exit with it even when stdout is gone.
                os._exit(2)

    timer = threading.Timer(seconds, fire)
    timer.daemon = True
    timer.start()

    def stop() -> None:
        nonlocal finished
        with lock:
            finished = True
        timer.cancel()

    return stop


def authorize(
    payload: Mapping[str, Any],
    http: HttpJson = http_json,
    clock: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> Decision:
    budget = timeout_seconds()
    deadline = clock() + budget
    url = authorize_url()
    stop_watchdog = deadline_watchdog(budget)
    try:
        leftover = remaining_seconds(deadline, clock)
        try:
            status, body = http("POST", url, payload, leftover)
        except TimeoutError:
            return poll_decision(
                str(payload["decision_id"]), deadline, http, clock, sleep
            )

        # An answer that arrives after the deadline is too late to act on.
        remaining_seconds(deadline, clock)
        # 207: sent for approval; its approved: false is not the decision.
        if status in (207, 409):
            return poll_decision(
                str(payload["decision_id"]), deadline, http, clock, sleep
            )
        if status != 200:
            raise HookContractError(http_failure(status, body))
        return decision_from_post_body(body)
    finally:
        stop_watchdog()


def result_preview(value: Any) -> str:
    try:
        rendered = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    except (TypeError, ValueError):
        rendered = str(value)
    return rendered[:MAX_RESULT_PREVIEW_CHARACTERS]


def result_has_known_error(value: Any) -> bool:
    if isinstance(value, str):
        # Some runtimes send the tool result as JSON text.
        try:
            value = json.loads(value)
        except json.JSONDecodeError:
            return False
    if not isinstance(value, dict):
        return False
    if value.get("isError") is True or value.get("is_error") is True:
        return True
    exit_code = value.get("exit_code", value.get("exitCode"))
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


def handle_pre_tool_use(
    event: Mapping[str, Any],
    http: HttpJson,
    clock: Clock,
    sleep: Sleeper,
) -> dict[str, Any] | None:
    decision = authorize(approval_payload(event), http=http, clock=clock, sleep=sleep)
    if decision.approved:
        return None
    detail = decision.reason or "Policy denied the call."
    return deny_output(f"Mezmo Governance denied this exact tool call: {detail}")


def handle_post_tool_use(event: Mapping[str, Any]) -> None:
    tool_call = normalize_tool_call(event)
    # Only post_mcp_tool_use carries a result; the other post events repeat the call.
    response = event["tool_info"].get("mcp_result")
    append_audit(
        {
            "ts": datetime.now(timezone.utc).isoformat(),
            "agent": AGENT,
            # Without a tool call id the record cannot be joined to its decision.
            "decision_id": None,
            "request_id": None,
            "tool_namespace": tool_call.namespace,
            "tool_name": tool_call.name,
            "status": "failed" if result_has_known_error(response) else "observed",
            "result_preview": result_preview(response),
        }
    )


def deny_output(reason: str) -> dict[str, Any]:
    # Windsurf reads no stdout: exit code 2 blocks, and stderr is the reason it shows.
    return {"decision": "deny", "reason": reason}


def handle_event(
    event: Mapping[str, Any],
    http: HttpJson = http_json,
    clock: Clock = time.monotonic,
    sleep: Sleeper = time.sleep,
) -> dict[str, Any] | None:
    when, _ = hook_action(event)
    if when == "pre":
        try:
            return handle_pre_tool_use(event, http, clock, sleep)
        except Exception as error:
            return deny_output(
                "Mezmo Governance could not authorize this exact tool call, so "
                f"Windsurf blocked it: {error}"
            )
    try:
        handle_post_tool_use(event)
    except Exception as error:
        print(
            f"Mezmo Governance could not write the local audit log: {error}",
            file=sys.stderr,
        )
    return None


def block(reason: str) -> int:
    try:
        print(json.dumps(deny_output(reason), separators=(",", ":")))
        print(reason, file=sys.stderr)
    except OSError:
        pass
    return 2


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if not isinstance(event, dict):
            raise HookContractError("hook input must be a JSON object")
        output = handle_event(event)
    except Exception as error:
        # Any failure blocks, such as a RecursionError on deeply nested input.
        return block(f"Mezmo Governance blocked this tool call: {str(error) or type(error).__name__}")

    if output is not None:
        return block(output["reason"])
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
