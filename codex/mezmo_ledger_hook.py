#!/usr/bin/env python3
"""Codex hook that records each tool call in the Mezmo ledger.

Codex runs it alongside mezmo_governance_hook.py for every tool call.

- Before a call it sends `started`. After a call it sends `ran` or `failed`, with `duration_ms`:
  how long the call took.
- Codex does not report a failed Bash command, so those are sent as `ran`.
- A call governance refuses is sent as `not_run`. The governance hook reports it through
  `end_governance_refusal`, because Codex says nothing more about a denied call.
- On whenever there is a key and a ledger URL: the environment, else what mezmo_setup.py saved, else
  the production API (mezmo_settings.py). ledger_enabled false in ~/.mezmo/codex.json turns it off.
  The ledger service takes the account from the key and the investigation from `extra.incident_key`.
- It always exits 0 and prints no hook output, so it can never block or change a tool call.
- A Bash call that runs ledger_investigation.py join or close, and printed its result line, also
  joins or leaves the investigation in the session state and sends `joined` or `closed`.
- A Mezmo Memory brief or Insights read that ran adds its references to the session state, so
  the next call's `started` carries what the agent knew when it acted.
- Events it could not send are saved to ~/.mezmo/ledger-spool.jsonl and resent on a later run.
  Claude Code uses the same file, so the code that handles it must match
  claude-code/scripts/ledger.py. test_ledger.py checks this.
- It must be in the same folder as mezmo_governance_hook.py: each imports the other.
"""

from __future__ import annotations

import errno
import functools
import hashlib
import json
import os
import re
import secrets
import shlex
import stat
import subprocess
import sys
import threading
import time
from contextlib import contextmanager
from datetime import datetime, timezone
from http.client import HTTPException
from itertools import islice
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Tuple
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, build_opener

# Python puts a script's folder first on sys.path, except with PYTHONSAFEPATH set. The governance
# hook beside this file must import either way.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))

from mezmo_governance_hook import (  # noqa: E402
    AGENT,
    HookContractError,
    RefuseRedirect,
    call_identity,
    normalize_tool_call,
    refuse_exposed_token,
)
import mezmo_settings  # noqa: E402

GOVERNANCE_TOKEN_ENV = "MEZMO_GOVERNANCE_TOKEN"
GOVERNANCE_URL_ENV = "MEZMO_GOVERNANCE_URL"
LEDGER_INCIDENT_KEY_ENV = "MEZMO_LEDGER_INCIDENT_KEY"
# Set by ledger_investigation.py launch beside MEZMO_LEDGER_INCIDENT_KEY, to the name of its claim
# file in launches_dir(): the first session to take that file joins the investigation.
LEDGER_LAUNCHED_ENV = "MEZMO_LEDGER_LAUNCHED"
# Where the session state keeps the launch it claimed, so the session never warns about its own.
LAUNCH_CLAIMED_FIELD = "launch_claimed"
# A claim no session has taken within this long is dropped: the launch's session never started.
LAUNCH_CLAIM_MAX_AGE_SECONDS = 24 * 3600
LAUNCH_MARKER_PATTERN = re.compile(r"[0-9a-f]{32}")
# Where the session state keeps the MEZMO_LEDGER_INCIDENT_KEY it warned is ignored, so it warns once.
IGNORED_INCIDENT_KEY_FIELD = "ignored_incident_key"
LEDGER_KEY_ENV = "MEZMO_LEDGER_KEY"
LEDGER_SPOOL_ENV = "MEZMO_LEDGER_SPOOL"
LEDGER_URL_ENV = "MEZMO_LEDGER_URL"
DEFAULT_LEDGER_SPOOL = Path.home() / ".mezmo" / "ledger-spool.jsonl"
# Where a session's investigation and references are read from. A join or a close writes the
# investigation, and a Memory or Insights read its references.
DEFAULT_STATE_DIR = Path.home() / ".mezmo" / "governance" / "codex-sessions"
# Where each call's start time waits for the ledger hook, while the ledger is on.
DEFAULT_CALL_STATE_DIR = Path.home() / ".mezmo" / "governance" / "codex-calls"
# How long a start time may wait for its call to end. Older ones are from calls that never
# reported back, and PostToolUse deletes them.
STATE_RETENTION_SECONDS = 7 * 24 * 3600
LEDGER_APPLICATION = "mezmo-codex-plugin"
# Stop, at the end of a turn, settles what the turn's calls left open and sends its last steps.
LEDGER_EVENTS = frozenset({"PostToolUse", "PreToolUse", "Stop"})
SKIP_LEDGER_MCP_TOOL_NAMES = frozenset({"get_ledger_investigation", "list_ledger_entries"})
# What tool_references reads. The fields an Insights reference takes its id from, in priority order,
# not alphabetical: the first template_id found wins over any edge_key, and an edge_key over any trace_id.
INSIGHTS_ID_FIELDS = ("template_id", "edge_key", "trace_id")
# mcp-service's Insights reads, matched by the tool's name on any MCP server. A tool added there later
# is not read until it is listed here.
INSIGHTS_TOOL_NAMES = frozenset({
    "find_degraded_service_edges",
    "find_log_patterns",
    "find_metric_analysis_anomalies",
    "get_log_dimension_changes",
    "get_log_overview",
    "get_log_pattern_series",
    "get_metric_analysis_overview",
    "get_metric_analysis_series_values",
    "get_service_failure_rates",
    "get_service_failures",
    "get_service_graph",
    "get_trace_spans",
    "list_failed_traces",
    "list_metric_analysis_series",
    "list_service_latency_summaries",
})
# The longest record, id, tool name or read_at a reference may hold, after strip.
MAX_REFERENCE_ID_CHARACTERS = 128
# Response text longer than this is not parsed for references.
MAX_REFERENCE_RESPONSE_CHARACTERS = 1_000_000
# The most values the search for an Insights id visits in one response.
MAX_REFERENCE_VALUES = 10_000
# The largest version a memory reference may hold: the largest integer every JSON reader keeps exact.
MAX_REFERENCE_VERSION = 2**53 - 1
# The most references the session state keeps. Past it the oldest go.
MAX_REFERENCES = 20
# The Memory tool whose records become references.
MEMORY_BRIEF_TOOL_NAME = "mezmo_memory_brief"
# For application.version. A file install has no manifest beside the script, so this is a
# constant, and test_ledger.py checks it against .codex-plugin/plugin.json.
LEDGER_VERSION = "0.9.1"
# The socket timeout of each request this hook makes, and the deadline its main thread waits for
# its ledger threads from when they start. A socket timeout alone bounds neither DNS nor the whole
# request, hence the threads. Read at call time, so a test can patch it.
LEDGER_TIMEOUT_SECONDS = 3.0
# How far past LEDGER_TIMEOUT_SECONDS main() lets a run go before it ends the process, for the
# save and the prints after the wait. A retry file that another process holds locked would
# otherwise hold the call until Codex kills the hook.
LEDGER_EXIT_MARGIN_SECONDS = 1.0
# The longest a join, a close or a reference write waits for the session state's lock. Past it the
# change is not made.
INVESTIGATION_STATE_TIMEOUT_SECONDS = 1.0
# The longest the last send's record waits for the session state's lock. Past it, it isn't kept.
LAST_SEND_STATE_TIMEOUT_SECONDS = 0.25
# How long record_governance_refusal waits for its request, and that request's socket timeout.
# Kept short because it runs inside the governance hook.
REFUSED_TIMEOUT_SECONDS = 2.0
# The most a deny may spend telling the Mezmo ledger once it is printed. Codex runs the tool when
# a hook outlives its timeout, so the handler's timeout must stay above the deadline plus this.
LEDGER_REFUSAL_BUDGET_SECONDS = 3.0
# Saved events resent in one request, small enough to finish well inside the deadline.
LEDGER_DRAIN_BATCH = 10
# Lines the retry file keeps. Past it the oldest go, so a ledger that stays down or a key that
# stays wrong can't grow the file, which every run of this hook reads in full.
LEDGER_SPOOL_MAX_LINES = 200
MAX_DETAIL_CHARACTERS = 300
MAX_INTENT_COMMAND_CHARACTERS = 80
MAX_LEDGER_JSON_CHARACTERS = 4096
MAX_LEDGER_MESSAGE_CHARACTERS = 200
# The first line of an apply_patch result.
EXIT_CODE_LINE = re.compile(r"Exit code: (-?\d+)")
LEDGER_ENDINGS = {"failed": "failed", "no_result": "no result reported", "not_run": "not run"}
# Codex runs no PostToolUse for a failed MCP call, so the hook keeps each started call in the
# session state until its end is recorded, and reads the session's rollout (the hook input's
# transcript_path) for how the rest ended. The state also keeps how far the rollout was read.
PENDING_CALLS_FIELD = "pending_calls"
ROLLOUT_OFFSET_FIELD = "rollout_offset"
# The hook-input fields an end event is built from, kept for a call that may never get its own.
PENDING_FIELDS = ("cwd", "session_id", "tool_input", "tool_name", "tool_use_id", "turn_id")
# Kept beside them: the investigation the call started under, which its ending belongs to.
PENDING_INCIDENT_KEY = "incident_key"
# Set on a shell call already recorded as ran before its exit code was in the rollout: a non-zero
# one found later is sent as a `failed` end, which replaces the `ran` as the call's last record.
PENDING_AWAITING_EXIT = "awaiting_exit"
# A shell call's exit code another hook read from the rollout before this call's own PostToolUse.
PENDING_EXIT_CODE = "exit_code"
MAX_PENDING_CALLS = 200
# The most of the rollout one hook run reads; the rest waits for the next run.
MAX_ROLLOUT_READ_BYTES = 8 * 1024 * 1024
# What Codex says between its calls, sent once each as a step of the investigation.
STEP_EVENT = "agent.investigation.step"
MAX_STEP_CHARACTERS = 2000
# The investigation CLI, and what its join and close print as `action` when they succeed.
INVESTIGATION_ACTIONS = {"close": "closed", "join": "joined"}
# The statuses ledger_investigation.py close takes, and the most of its summary the hook sends.
CLOSE_STATUSES = ("resolved", "mitigated", "ongoing", "no_action")
MAX_SUMMARY_CHARACTERS = 2000
INVESTIGATION_SCRIPT = "ledger_investigation.py"
# What _investigation_call takes for a Python interpreter's file name, and for its flags that take
# no argument, such as -I or -Su.
PYTHON_FLAGS = re.compile(r"-[BEIOPRSbdiqsuvx]+")
PYTHON_INTERPRETER = re.compile(r"python(3(\.[0-9]+)?)?")
# A `NAME=value` word, which before a command sets that variable for it.
SHELL_ASSIGNMENT = re.compile(r"[A-Za-z_][A-Za-z0-9_]*=.*", re.DOTALL)

# What resolve_target reads. Demo only, until there is a service catalog: DEMO_DEFAULT_ENV is the
# environment of a call that names none, and DEMO_TARGETS maps a lowercase substring of a kubectl
# call's words or an MCP tool's name to the target it stands for.
DEMO_DEFAULT_ENV = "prod"
DEMO_TARGETS = {"checkout": "checkout/prod"}
# kubectl flags whose value is a file, never a service. In `logs`, `-f` means follow instead.
KUBECTL_FILE_FLAGS = ("--filename", "--kustomize", "-f", "-k")
KUBECTL_KINDS = (
    "deploy", "deployment", "deployments", "service", "services", "statefulset", "statefulsets", "sts", "svc",
)
# `rollout` subcommands that only read. Every other `rollout` writes.
KUBECTL_READ_ROLLOUTS = ("history", "status")
# `create service` subcommands, which sit between kind and name.
KUBECTL_SERVICE_TYPES = ("clusterip", "externalname", "loadbalancer", "nodeport")
# kubectl resource flags whose value is the next word, and which can sit between kind and name.
KUBECTL_TARGET_VALUE_FLAGS = (
    "--clusterip", "--external-name", "--field-selector", "--label-columns", "--node-port", "--output",
    "--selector", "--sort-by", "--tcp", "--template", "--templatefile", "-l", "-o",
)
# kubectl's global flags whose value is the next word.
KUBECTL_VALUE_FLAGS = (
    "--as", "--as-group", "--as-uid", "--as-user-extra", "--cache-dir", "--certificate-authority",
    "--client-certificate", "--client-key", "--cluster", "--context", "--kubeconfig", "--kuberc",
    "--log-flush-frequency", "--namespace", "--password", "--profile", "--profile-output",
    "--request-timeout", "--server", "--tls-server-name", "--token", "--user", "--username", "--v",
    "--vmodule", "-n", "-s", "-v",
)
KUBECTL_WRITE_VERBS = (
    "annotate", "apply", "create", "delete", "drain", "edit", "label", "patch", "replace", "restart",
    "rollout", "scale", "set", "taint",
)
# Above this many characters, _command_words skips shlex, which takes quadratic time on a long word.
MAX_SHLEX_CHARACTERS = 100_000
MCP_ENV_ARGUMENTS = ("env", "environment", "namespace")
MCP_READ_PREFIXES = ("describe", "fetch", "find", "get", "list", "query", "read", "search", "show", "watch")
MCP_SERVICE_ARGUMENTS = ("service", "service_name")
MCP_WRITE_WORDS = ("apply", "create", "delete", "patch", "restart", "rollback", "scale", "undo", "update")
# A service, namespace or environment: a DNS label. _plain_name also refuses one that is all digits.
PLAIN_NAME = re.compile(r"[a-z0-9]([a-z0-9-]{0,61}[a-z0-9])?")
# The characters that become words of their own and end a kubectl call.
SHELL_PUNCTUATION = "();<>|&\n"
# A word of a command shlex doesn't read: a run of SHELL_PUNCTUATION, or of other characters that
# aren't whitespace.
UNQUOTED_WORD = re.compile("[{0}]+|[^\\s{0}]+".format(re.escape(SHELL_PUNCTUATION)))

# Ledger threads never print. They leave messages here, which the main thread prints after its
# wait.
LEDGER_MESSAGES: List[str] = []
# Every ledger thread this process started, so main() knows whether one is still running.
LEDGER_THREADS: List[threading.Thread] = []
# Lets only one thread at a time touch the retry file: the drain thread's rewrite and the main
# thread's save. Needed where there is no flock, as on Windows. It can't hold back another
# process there.
SPOOL_LOCK = threading.Lock()


class LedgerConfig(NamedTuple):
    # The Mezmo access key as configured, or None to send no Authorization header.
    key: Optional[str]
    url: str


class LedgerSendResult(NamedTuple):
    """What send_ledger_events did with new events. It never saves them itself."""

    # Whether the ledger service answered with any status. A network failure or timeout isn't one.
    answered: bool
    # The events the caller saves for retry.
    save: List[Mapping[str, Any]]


LedgerHttp = Callable[..., Tuple[int, bytes]]


# The next three functions are the same as in the Claude Code plugin. A test fails if they differ.


def _setting(*names: str) -> Optional[str]:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return value
    return None


def authorization_header(*, key: str) -> str:
    """The Authorization value for a Mezmo key: `Token <key>`, or the key itself when it names its scheme."""
    if key.lower().startswith("token ") or key.lower().startswith("bearer "):
        return key
    return f"Token {key}"


@contextmanager
def _locked(path: Path, timeout: Optional[float] = None) -> Iterator[None]:
    """Hold an exclusive lock on `path`.lock, so parallel tool calls don't lose updates.

    With a `timeout`, waits that many seconds at most, then raises TimeoutError without running the body.
    """
    held_elsewhere = False
    with open(f"{path}.lock", "a") as lock:
        try:
            import fcntl

            give_up = None if timeout is None else time.monotonic() + timeout
            while True:
                try:
                    fcntl.flock(lock, fcntl.LOCK_EX if give_up is None else fcntl.LOCK_EX | fcntl.LOCK_NB)
                    break
                except BlockingIOError:
                    if give_up is None:
                        raise
                    if time.monotonic() >= give_up:
                        held_elsewhere = True
                        break
                    time.sleep(0.01)
        except (ImportError, OSError):
            pass  # no fcntl on Windows: best effort
        # Raised out here, so a lock held elsewhere can't be taken for the best-effort case above.
        if held_elsewhere:
            raise TimeoutError(f"{lock.name} is held elsewhere")
        yield


def governance_configured() -> bool:
    """Whether the governance hook has both of its settings, so this call has a decision there."""
    return mezmo_settings.governance_key().value is not None and mezmo_settings.governance_url().value is not None


def ledger_configured() -> bool:
    """Whether Mezmo ledger sending is on: there is a key and a ledger URL (see mezmo_settings.py)."""
    return mezmo_settings.ledger_url().value is not None and mezmo_settings.ledger_key().value is not None


def load_ledger_config() -> Optional[LedgerConfig]:
    """The ledger settings, or None when sending is off or the settings aren't safe to use."""
    url = mezmo_settings.ledger_url().value
    # MEZMO_LEDGER_KEY, else the governance key (environment, then key store). No key, no ledger.
    key, key_setting = mezmo_settings.ledger_key()
    if url is None or key is None:
        return None
    # The messages name the setting, never its value.
    try:
        refuse_exposed_token(url, "")
    except (HookContractError, ValueError):
        print(
            f"Mezmo ledger sending is off: the ledger URL ({mezmo_settings.ledger_url().source}) must use https://, "
            "or http:// to this machine, "
            "with no user name or password",
            file=sys.stderr,
        )
        return None
    if key is not None and not (key.isascii() and key.isprintable()):
        print(
            f"Mezmo ledger sending is off: {key_setting} holds a character an HTTP header cannot carry",
            file=sys.stderr,
        )
        return None
    return LedgerConfig(key, url)


def ledger_spool_path() -> Path:
    configured = _setting(LEDGER_SPOOL_ENV)
    return Path(configured).expanduser() if configured else DEFAULT_LEDGER_SPOOL


def state_dir() -> Path:
    configured = os.environ.get("MEZMO_GOVERNANCE_STATE_DIR")
    return Path(configured).expanduser() if configured else DEFAULT_STATE_DIR


def session_state_path(session_id: str) -> Path:
    return state_dir() / (re.sub(r"[^A-Za-z0-9_.-]", "_", session_id)[:128] + ".json")


def call_state_dir() -> Path:
    configured = os.environ.get("MEZMO_GOVERNANCE_STATE_DIR")
    return Path(configured).expanduser() / "codex-calls" if configured else DEFAULT_CALL_STATE_DIR


def start_time_path(decision_id: str) -> Path:
    """The file that holds this call's start time. A decision_id is a UUID, a safe name."""
    return call_state_dir() / decision_id


def _plain_name(value: Any) -> Optional[str]:
    """`value` as a lowercase DNS label that isn't all digits, else None."""
    name = value.strip().lower() if isinstance(value, str) else ""
    return name if PLAIN_NAME.fullmatch(name) and not name.isdigit() else None


def _command_words(command: str) -> List[str]:
    """The command's words. Each run of shell punctuation, newlines included, is its own word.

    Shell comments go first, without touching quoted strings, so an apostrophe in one can't open a
    quote, then backslash-newlines. A command shlex can't read, or one longer than
    MAX_SHLEX_CHARACTERS, is split as UNQUOTED_WORD says, with its quotes left in.
    """
    command = _without_shell_comments(command).replace("\\\r\n", "").replace("\\\n", "")
    # shlex adds to a word one character at a time, so one long word takes quadratic time.
    if len(command) <= MAX_SHLEX_CHARACTERS:
        lexer = shlex.shlex(command, posix=True, punctuation_chars=SHELL_PUNCTUATION)
        lexer.commenters = ""
        lexer.whitespace = " \t\r"
        lexer.whitespace_split = True
        try:
            return list(lexer)
        except ValueError:  # An unclosed quote, or a backslash at the end.
            pass
    return UNQUOTED_WORD.findall(command)


def _without_shell_comments(command: str) -> str:
    """`command` without unquoted shell comments, preserving newlines and quoted strings."""
    result: List[str] = []
    escaped = False
    quote: Optional[str] = None
    start = True
    position = 0
    while position < len(command):
        character = command[position]
        position += 1
        if escaped:
            result.append(character)
            escaped = False
            start = False
            continue
        if character == "\\" and quote != "'":
            result.append(character)
            escaped = True
            start = False
            continue
        if quote is None and character == "#" and start:
            while position < len(command) and command[position] != "\n":
                position += 1
            continue
        result.append(character)
        if quote is not None:
            if character == quote:
                quote = None
            continue
        if character in ("'", '"'):
            quote = character
            start = False
        elif character in SHELL_PUNCTUATION or character.isspace():
            start = True
        else:
            start = False
    return "".join(result)


def _kubectl_calls(words: Sequence[str]) -> List[List[str]]:
    """The words of each kubectl call, from `kubectl` to the next punctuation word or kubectl."""
    calls: List[List[str]] = []
    call: Optional[List[str]] = None
    for word in words:
        if word.rsplit("/", 1)[-1] == "kubectl":
            call = []
            calls.append(call)
        elif word.startswith(tuple(SHELL_PUNCTUATION)):
            call = None
        elif call is not None:
            call.append(word)
    return calls


def _kubectl_parts(words: Sequence[str]) -> Tuple[Optional[str], List[str]]:
    """(namespace, rest) for one kubectl call.

    `rest` is the call's words without the KUBECTL_VALUE_FLAGS and their values, up to a bare `--`
    that follows the verb. `namespace` is None when no flag gives one, "" when a flag gives one
    that can't be read, else the name. The last namespace flag wins.
    """
    namespace: Optional[str] = None
    rest: List[str] = []
    has_verb = False
    position = 0
    while position < len(words):
        word = words[position]
        position += 1
        value = None
        if word == "--":
            if has_verb:
                break
        elif word in ("-n", "--namespace"):
            value = words[position] if position < len(words) else ""
            position += 1
        elif word.startswith(("-n=", "--namespace=")):
            value = word.partition("=")[2]
        elif word.startswith("-n"):  # -n<ns>
            value = word[2:]
        elif word in KUBECTL_VALUE_FLAGS:
            position += 1
        else:
            rest.append(word)
            has_verb = has_verb or not word.startswith("-")
        if value is not None:
            namespace = _plain_name(value) or ""
    return namespace, rest


def _kubectl_access(rest: Sequence[str]) -> str:
    """The call's access: "write" when its verb, its first word that isn't a flag, changes the cluster."""
    plain = [word for word in rest if not word.startswith("-")]
    if not plain or plain[0] not in KUBECTL_WRITE_VERBS:
        return "read"
    if plain[0] == "rollout" and len(plain) > 1 and plain[1] in KUBECTL_READ_ROLLOUTS:
        return "read"
    return "write"


def _demo_target(words: Sequence[str]) -> Tuple[Optional[str], str]:
    """(service, env) for the first DEMO_TARGETS key found inside one of `words`, else (None, "")."""
    for key, target in DEMO_TARGETS.items():
        if any(key in word.lower() for word in words):
            service, _, env = target.partition("/")
            return service, env
    return None, ""


def _kubectl_target_words(words: Sequence[str]) -> List[str]:
    """`words` without flags and flag values that can sit between resource kind and name."""
    kept: List[str] = []
    position = 0
    while position < len(words):
        word = words[position]
        position += 1
        if word in KUBECTL_TARGET_VALUE_FLAGS:
            position += 1
        elif any(word.startswith(f"{flag}=") for flag in KUBECTL_TARGET_VALUE_FLAGS if flag.startswith("--")):
            pass
        elif any(word.startswith(flag) and word != flag for flag in ("-l", "-o")):
            pass
        elif word.startswith("-"):
            pass
        else:
            kept.append(word)
    return kept


def _kubectl_target(namespace: Optional[str], rest: Sequence[str]) -> Optional[str]:
    """One call's target: None when it names no service, "unknown" when its namespace can't be read."""
    verb = next((index for index, word in enumerate(rest) if not word.startswith("-")), len(rest))
    after = list(rest[verb + 1:])
    # The words that may name a service, with "" for each file after a KUBECTL_FILE_FLAGS flag.
    file_flags = () if "logs" in rest[verb:verb + 1] else KUBECTL_FILE_FLAGS
    words = ["" if index and after[index - 1] in file_flags else word for index, word in enumerate(after)]
    # `deploy/checkout` anywhere comes before `deploy checkout`. A flag is never a plain name.
    target_words = _kubectl_target_words(words)
    # In `create service clusterip payments`, the service type sits between kind and name.
    if "create" in rest[verb:verb + 1] and target_words[:1] in (["service"], ["svc"]):
        if target_words[1:2] and target_words[1] in KUBECTL_SERVICE_TYPES:
            del target_words[1]
    named = [(kind, name) for kind, _, name in (word.partition("/") for word in target_words)]
    named += [(kind, name) for kind, name in zip(target_words, target_words[1:]) if "/" not in kind]
    services = (_plain_name(name) for kind, name in named if kind.lower().split(".")[0] in KUBECTL_KINDS)
    service = next((found for found in services if found), None)
    env = ""
    if service is None:
        service, env = _demo_target(after)
    if service is None:
        return None
    if namespace == "":
        return "unknown"
    return f"{service}/{namespace or env or DEMO_DEFAULT_ENV}"


def _mcp_target(arguments: Mapping[str, Any], name: str) -> str:
    """An MCP call's target: "unknown" with no service, or with an environment that can't be read."""
    services = (_plain_name(arguments.get(key)) for key in MCP_SERVICE_ARGUMENTS)
    service = next((found for found in services if found), None)
    env: Optional[str] = ""
    if service is None:
        service, env = _demo_target([name])
    if service is None:
        return "unknown"
    given = next((arguments[key] for key in MCP_ENV_ARGUMENTS if arguments.get(key) not in (None, "")), None)
    if given is not None:
        env = _plain_name(given)
        if env is None:
            return "unknown"
    return f"{service}/{env or DEMO_DEFAULT_ENV}"


def _name_tokens(name: str) -> List[str]:
    """`name` split into lowercase words on separators and camelCase boundaries."""
    spaced = re.sub(r"([A-Z]+)([A-Z][a-z])", r"\1 \2", name)
    spaced = re.sub(r"([a-z0-9])([A-Z])", r"\1 \2", spaced)
    return [token.lower() for token in re.split(r"[^A-Za-z0-9]+", spaced) if token]


def _mcp_access(name: str) -> str:
    """An MCP call's access, with read-like leading tokens taking precedence."""
    tokens = _name_tokens(name)
    if tokens and tokens[0] in MCP_READ_PREFIXES:
        return "read"
    return "write" if any(token in MCP_WRITE_WORDS for token in tokens) else "read"


def resolve_target(namespace: str, name: str, arguments: Mapping[str, Any]) -> Tuple[str, str]:
    """(target, access) for a call: "<service>/<env>" or "unknown", and "read" or "write".

    A demo build: it reads the kubectl calls in a Bash command, and the name and arguments of an
    MCP tool. Another built-in tool is ("unknown", "read"). It never raises. On an error the target
    is "unknown" and the access is what was worked out before the error.
    """
    access = "read"
    try:
        if namespace != AGENT:
            access = _mcp_access(name)
            return _mcp_target(arguments, name), access
        command = arguments.get("command")
        if name != "Bash" or not isinstance(command, str):
            return "unknown", access
        calls = [_kubectl_parts(call) for call in _kubectl_calls(_command_words(command))]
        writes = [call for call in calls if _kubectl_access(call[1]) == "write"]
        access = "write" if writes else "read"
        # A reading call never lends its service to a write.
        targets = [_kubectl_target(*call) for call in writes or calls]
        return next((target for target in targets if target), "unknown"), access
    except Exception:
        return "unknown", access


def ledger_json(value: Any, limit: int = MAX_LEDGER_JSON_CHARACTERS) -> str:
    """`value` as text, cut at `limit` characters. JSON-encode only non-strings."""
    if isinstance(value, str):
        return value[:limit]
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


def _stated_exit_code(response: Mapping[str, Any]) -> Optional[int]:
    """The exit code a result object states, or None. A bool is not an exit code."""
    for name in ("exit_code", "exitCode"):
        value = response.get(name)
        if isinstance(value, int) and not isinstance(value, bool):
            return value
    return None


def _is_apply_patch(hook_input: Mapping[str, Any]) -> bool:
    """Whether this call is apply_patch, Codex's built-in tool for editing files."""
    try:
        tool = normalize_tool_call(hook_input)
    except Exception:
        return False
    return (tool.namespace, tool.name) == (AGENT, "apply_patch")


def _skip_ledger_mcp_tool(*, hook_input: Mapping[str, Any]) -> bool:
    """Whether this MCP call reads the ledger and should not itself be recorded in the ledger."""
    tool_name = hook_input.get("tool_name")
    if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
        return False
    parts = tool_name.split("__", 2)
    return len(parts) == 3 and parts[2] in SKIP_LEDGER_MCP_TOOL_NAMES


def ledger_result(*, hook_input: Mapping[str, Any]) -> Tuple[str, Optional[int]]:
    """(kind, exit_status) for a call Codex reports back on. exit_status is None when none is stated.

    `failed` needs a response that states a failure. Codex 0.167.0 sends none: it reports back
    only on a call it counts as successful, and gives a Bash command's output as a plain string
    with no exit code, whatever the command exited with. A string is never parsed as JSON, so
    output that looks like a result object can't decide the event. Only apply_patch, whose result
    Codex writes itself, has its `Exit code: N` first line read.
    """
    response = hook_input.get("tool_response")
    if isinstance(response, dict):
        if response.get("interrupted") is True:
            return "failed", 1
        exit_code = _stated_exit_code(response)
        if response.get("is_error") is True or response.get("isError") is True:
            return "failed", exit_code or 1
        if exit_code is not None:
            return ("failed", exit_code) if exit_code else ("ran", 0)
        if response.get("isError") is False:
            return "ran", 0
        return "ran", None
    if isinstance(response, str) and _is_apply_patch(hook_input):
        lines = response.splitlines()
        match = EXIT_CODE_LINE.fullmatch(lines[0].strip()) if lines else None
        if match:
            exit_code = int(match.group(1))
            return ("failed", exit_code) if exit_code else ("ran", 0)
    return "ran", None


def _reference_string(*, value: Any) -> Optional[str]:
    """`value` stripped, when it is a string of 1 to MAX_REFERENCE_ID_CHARACTERS characters, else None."""
    text = value.strip() if isinstance(value, str) else ""
    return text if 0 < len(text) <= MAX_REFERENCE_ID_CHARACTERS else None


def _reference(*, item: Any) -> Optional[Dict[str, Any]]:
    """`item` rebuilt from a reference's own keys, or None when it is not a valid reference. Never raises.

    A memory reference is {record, type, version} and an insights one {id, read_at, tool, type}. Its
    strings are stripped and hold 1 to MAX_REFERENCE_ID_CHARACTERS characters, and no other key is
    kept. `version` is an int, not a bool, from 0 to MAX_REFERENCE_VERSION.
    """
    try:
        if not isinstance(item, dict):
            return None
        kind = item.get("type")
        if kind == "memory":
            record = _reference_string(value=item.get("record"))
            version = item.get("version")
            if record is None or isinstance(version, bool) or not isinstance(version, int):
                return None
            if not 0 <= version <= MAX_REFERENCE_VERSION:
                return None
            return {"record": record, "type": "memory", "version": version}
        if kind == "insights":
            reference_id = _reference_string(value=item.get("id"))
            read_at = _reference_string(value=item.get("read_at"))
            tool = _reference_string(value=item.get("tool"))
            if reference_id is None or read_at is None or tool is None:
                return None
            return {"id": reference_id, "read_at": read_at, "tool": tool, "type": "insights"}
    except Exception:
        pass
    return None


def _response_object(*, response: Any) -> Optional[Dict[str, Any]]:
    """The result object of an MCP tool's response, or None. Never raises.

    A string is first parsed as JSON, and the object it gives is then read as any object is, once.
    From an object: its `structuredContent` when that is an object, else the first text block of its
    `content` list, or its `content` string, parsed as JSON, else, with neither key, the object itself.
    From a list of content blocks: its first text block parsed as JSON. Parsed text counts only when it
    is a JSON object, and text longer than MAX_REFERENCE_RESPONSE_CHARACTERS is not parsed.
    """
    try:
        if isinstance(response, str):
            response = json.loads(response) if len(response) <= MAX_REFERENCE_RESPONSE_CHARACTERS else None
            if not isinstance(response, dict):
                return None
        content = response
        if isinstance(response, dict):
            if isinstance(response.get("structuredContent"), dict):
                return response["structuredContent"]
            if "content" not in response:
                return None if "structuredContent" in response else response
            content = response["content"]
        if isinstance(content, list):
            blocks = (block for block in content if isinstance(block, dict) and block.get("type") == "text")
            content = next((block.get("text") for block in blocks), None)
        if not isinstance(content, str) or len(content) > MAX_REFERENCE_RESPONSE_CHARACTERS:
            return None
        parsed = json.loads(content)
        return parsed if isinstance(parsed, dict) else None
    except Exception:
        return None


def _insights_id(*, response: Any) -> Optional[str]:
    """The id an Insights response gives its reference, or None. Never raises.

    One depth-first pass with an explicit stack, an object's values in the order the JSON gives them
    and a list's by index, that keeps the first valid value seen for each of INSIGHTS_ID_FIELDS and
    stops after MAX_REFERENCE_VALUES values. The id is the value found for the first field of
    INSIGHTS_ID_FIELDS that has one.
    """
    found: Dict[str, str] = {}
    try:
        # (the key the value has in its object, or None, the value), next to visit last.
        stack: List[Tuple[Any, Any]] = [(None, response)]
        visited = 0
        while stack and visited < MAX_REFERENCE_VALUES and INSIGHTS_ID_FIELDS[0] not in found:
            key, value = stack.pop()
            visited += 1
            if isinstance(key, str) and key in INSIGHTS_ID_FIELDS and key not in found:
                text = _reference_string(value=value)
                if text is not None:
                    found[key] = text
            # Only what the budget can still visit, so a wide object or list costs no more than that.
            remaining = MAX_REFERENCE_VALUES - visited
            if isinstance(value, dict):
                stack.extend(reversed(list(islice(value.items(), remaining))))
            elif isinstance(value, list):
                stack.extend((None, item) for item in reversed(value[:remaining]))
    except Exception:
        pass
    return next((found[field] for field in INSIGHTS_ID_FIELDS if field in found), None)


def merge_references(*, existing: Any, new: Sequence[Any]) -> List[Dict[str, Any]]:
    """The valid items of `existing` and then of `new`, each once, at most MAX_REFERENCES. Never raises.

    Each item is rebuilt by _reference, and one it refuses is dropped. `existing` that isn't a list
    counts as empty. Of the items with one identity, `record` and `version` for memory and `tool` and
    `id` for insights, only the last is kept, so a re-read moves to the end: the list is walked from
    its end, the first item seen for each identity is kept, and the result is reversed. Then only the
    last MAX_REFERENCES are kept.
    """
    try:
        items = (list(existing) if isinstance(existing, list) else []) + list(new)
        kept: List[Dict[str, Any]] = []
        seen: Dict[Tuple[Any, ...], bool] = {}
        for item in reversed(items):
            reference = _reference(item=item)
            if reference is None:
                continue
            if reference["type"] == "memory":
                identity: Tuple[Any, ...] = ("memory", reference["record"], reference["version"])
            else:
                identity = ("insights", reference["tool"], reference["id"])
            if identity not in seen:
                seen[identity] = True
                kept.append(reference)
        kept.reverse()
        return kept[-MAX_REFERENCES:]
    except Exception:
        return []


def tool_references(*, hook_input: Mapping[str, Any], read_at: str) -> List[Dict[str, Any]]:
    """The references this MCP call gives, or [] for any other tool. Never raises.

    Only the tool's own name is read, from `mcp__<server>__<tool>`, never the server's. A
    MEMORY_BRIEF_TOOL_NAME call gives one memory reference for each valid entry of the first
    MAX_REFERENCES of the result's `records`, each followed by one for its `remediation` when that is
    valid, and [] when it can't read the result. An INSIGHTS_TOOL_NAMES call gives one insights
    reference, read at `read_at`, even when it can't read the result. Its id is what _insights_id finds
    in the result, else the first 16 hex characters of the sha256 of the tool's name and its arguments
    as sorted JSON. Neither the arguments nor the output are kept.
    """
    try:
        tool_name = hook_input.get("tool_name")
        if not isinstance(tool_name, str) or not tool_name.startswith("mcp__"):
            return []
        parts = tool_name.split("__", 2)
        if len(parts) != 3:
            return []
        tool = parts[2]
        if tool == MEMORY_BRIEF_TOOL_NAME:
            result = _response_object(response=hook_input.get("tool_response"))
            records = result.get("records") if result is not None else None
            references: List[Dict[str, Any]] = []
            for record in records[:MAX_REFERENCES] if isinstance(records, list) else []:
                for entry in (record, record.get("remediation")) if isinstance(record, dict) else ():
                    if not isinstance(entry, dict):
                        continue
                    reference = _reference(
                        item={"record": entry.get("record_id"), "type": "memory", "version": entry.get("version")}
                    )
                    if reference is not None:
                        references.append(reference)
            return references
        if tool not in INSIGHTS_TOOL_NAMES:
            return []
        reference_id = _insights_id(response=_response_object(response=hook_input.get("tool_response")))
        if reference_id is None:
            arguments = hook_input.get("tool_input")
            call = [tool, arguments if isinstance(arguments, dict) else {}]
            text = json.dumps(call, default=str, separators=(",", ":"), sort_keys=True)
            reference_id = hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]
        reference = _reference(item={"id": reference_id, "read_at": read_at, "tool": tool, "type": "insights"})
        return [reference] if reference is not None else []
    except Exception:
        return []


def launches_dir() -> Path:
    """Where ledger_investigation.py launch leaves its one-time claims, beside the session states."""
    return state_dir() / "launches"


def record_launch(incident_key: str) -> str:
    """Leave a one-time claim on `incident_key`'s investigation, and return the MEZMO_LEDGER_LAUNCHED value naming it.

    Claims older than LAUNCH_CLAIM_MAX_AGE_SECONDS are dropped first. Raises OSError when the claim
    can't be written.
    """
    folder = launches_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    cutoff = time.time() - LAUNCH_CLAIM_MAX_AGE_SECONDS
    for entry in folder.iterdir():
        try:
            if entry.stat().st_mtime < cutoff:
                entry.unlink()
        except OSError:
            pass
    marker = secrets.token_hex(16)
    path = folder / marker
    # In a private folder, so the moment before chmod exposes nothing.
    with open(path, "x", encoding="utf-8") as claim:
        claim.write(incident_key + "\n")
    os.chmod(path, 0o600)
    return marker


def is_launch_marker(marker: Optional[str]) -> bool:
    """Whether `marker` has the shape of a MEZMO_LEDGER_LAUNCHED value record_launch gives."""
    return marker is not None and LAUNCH_MARKER_PATTERN.fullmatch(marker) is not None


def _hold_launch_claim(*, holder: str, incident_key: str, marker: str) -> Optional[Path]:
    """Hold `marker`'s claim on `incident_key` for `holder`, and return where it is held. Never raises.

    None when it isn't there, is another key's, or is stale (then it is dropped). Renaming the file
    is the hold: only one process can, so only one session joins. The held file is deleted once the
    session state is saved, or put back if it wasn't, so a failed save doesn't lose the launch.
    """
    try:
        if not is_launch_marker(marker):
            return None
        path = launches_dir() / marker
        if path.read_text(encoding="utf-8").strip() != incident_key:
            return None
        if path.stat().st_mtime < time.time() - LAUNCH_CLAIM_MAX_AGE_SECONDS:
            path.unlink()
            return None
        held = path.with_name(f"{marker}.{holder}")
        os.rename(path, held)
        return held
    except OSError:
        return None


def _session_state(*, hook_input: Mapping[str, Any]) -> Dict[str, Any]:
    """This session's state, or {} when there is none or it can't be read. Never raises."""
    state: Any = {}
    session_id = hook_input.get("session_id")
    if isinstance(session_id, str) and session_id.strip():
        try:
            state = json.loads(session_state_path(session_id.strip()).read_text(encoding="utf-8"))
        except Exception:
            state = {}
    return state if isinstance(state, dict) else {}


def adopt_launched_investigation(
    *, hook_input: Mapping[str, Any], update: Callable[[Callable[[Dict[str, Any]], None]], None]
) -> None:
    """Join the investigation ledger_investigation.py launch opened, once, or warn that MEZMO_LEDGER_INCIDENT_KEY is ignored. Never raises.

    The hooks never read the incident key from the environment directly: a long-lived process,
    such as Codex's app-server, keeps it for every later session, and after the investigation
    closes. Only the first session to take launch's claim joins, in its own state, so a close
    ends it for good. `update(change)` applies `change` to the session state under its lock.
    """
    try:
        incident_key = _setting(LEDGER_INCIDENT_KEY_ENV)
        if incident_key is None:
            return
        marker = _setting(LEDGER_LAUNCHED_ENV) or ""
        state = _session_state(hook_input=hook_input)
        if (marker and state.get(LAUNCH_CLAIMED_FIELD) == marker) or state.get(IGNORED_INCIDENT_KEY_FIELD) == incident_key:
            return
        joined_at = datetime.now(timezone.utc).isoformat()
        holder = hashlib.sha256(str(hook_input.get("session_id")).encode("utf-8")).hexdigest()[:16]
        ignored: List[str] = []
        held: List[Path] = []

        def adopt(state: Dict[str, Any]) -> None:
            if marker and state.get(LAUNCH_CLAIMED_FIELD) == marker:
                return
            claim = held[0] if held else _hold_launch_claim(holder=holder, incident_key=incident_key, marker=marker)
            if claim is not None:
                held[:] = [claim]
                state[LAUNCH_CLAIMED_FIELD] = marker
                current = state.get("investigation")
                current_key = current.get("incident_key") if isinstance(current, dict) else None
                # An investigation the session already joined stays; launch's only fills an empty slot.
                if not (isinstance(current_key, str) and current_key.strip()):
                    state["investigation"] = {"incident_key": incident_key, "joined_at": joined_at}
                return
            if state.get(IGNORED_INCIDENT_KEY_FIELD) != incident_key:
                state[IGNORED_INCIDENT_KEY_FIELD] = incident_key
                ignored.append(incident_key)

        update(adopt)
        if held:
            try:
                if _session_state(hook_input=hook_input).get(LAUNCH_CLAIMED_FIELD) == marker:
                    held[0].unlink()
                else:
                    os.replace(held[0], held[0].with_name(marker))
            except OSError:
                pass
        if ignored:
            LEDGER_MESSAGES.append(
                f"Mezmo ledger ignores {LEDGER_INCIDENT_KEY_ENV}={incident_key}: only the first session"
                " ledger_investigation.py launch starts joins its investigation, so this session's calls are not"
                " filed under it. Join it to work on it, or unset it where it is set; a long-running process keeps"
                " the environment it started with."
            )
    except Exception:
        pass


def ledger_context(*, hook_input: Mapping[str, Any]) -> Dict[str, Any]:
    """{incident_key, references} for this session, absent ones left out.

    The incident key is the session state's investigation, which a launched session joins through
    adopt_launched_investigation. `references` are the state's valid ones as merge_references keeps
    them, left out when none is.
    """
    state = _session_state(hook_input=hook_input)
    context: Dict[str, Any] = {}
    references = merge_references(existing=state.get("references"), new=[])
    if references:
        context["references"] = references
    investigation = state.get("investigation")
    incident_key = investigation.get("incident_key") if isinstance(investigation, dict) else None
    if isinstance(incident_key, str) and incident_key.strip():
        context["incident_key"] = incident_key.strip()
    return context


@functools.lru_cache(maxsize=None)
def person_email() -> Optional[str]:
    """The git email of whoever runs this session, or None. Asks git once per run and never raises."""
    try:
        result = subprocess.run(
            ["git", "config", "--get", "user.email"],
            capture_output=True,
            stdin=subprocess.DEVNULL,
            text=True,
            timeout=1,
        )
    except Exception:
        return None
    email = result.stdout.strip() if result.returncode == 0 else ""
    return email or None


def ledger_envelope(
    *,
    entity_id: Optional[str],
    entity_type: str,
    event: str,
    extra: Mapping[str, Any],
    message: str,
    session_id: str,
) -> Dict[str, Any]:
    """The top-level fields audit-service takes, for any entity. `event` is the event name.

    No account and no correlation_id: the ledger service sets them from the key and from
    `extra.incident_key`, and sets entity_id too when it is None, as for an investigation's events. `extra` gains person_email when git has one and `extra` gives none.
    """
    stamped = dict(extra)
    email = person_email()
    if email is not None:
        stamped.setdefault("person_email", email)
    envelope: Dict[str, Any] = {
        "actor": {"pk": f"{AGENT}:{session_id}", "type": "agent"},
        "application": {"name": LEDGER_APPLICATION, "version": LEDGER_VERSION},
        "entity_type": entity_type,
        "event": event,
        "extra": stamped,
        "internal": False,
        "message": message,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }
    if entity_id is not None:
        envelope["entity_id"] = entity_id
    return envelope


def ledger_event(
    *,
    config: LedgerConfig,
    duration_ms: Optional[int] = None,
    exit_status: Optional[int] = None,
    hook_input: Mapping[str, Any],
    kind: str,
    outcome: Optional[str] = None,
    reason: Optional[str] = None,
    started_under: Optional[Mapping[str, Any]] = None,
) -> Optional[Dict[str, Any]]:
    """One tool-call event: `kind` is started, ran, failed, not_run or no_result. None without an identity.

    `started_under`, {incident_key} or {}, stands in for the session's current investigation: an
    ending built later, from the rollout, belongs to the investigation its call started under.
    """
    try:
        tool = normalize_tool_call(hook_input)
        identity = call_identity(hook_input, tool)
    except Exception:
        return None
    target, access = resolve_target(tool.namespace, tool.name, tool.arguments)
    # Codex gives a call no description, so the intent is its name and the start of its command.
    investigation = _investigation_intent(tool)
    intent = investigation or f"{tool.namespace}.{tool.name}"
    command = tool.arguments.get("command")
    if not investigation and isinstance(command, str) and command.strip():
        intent += f": {_one_line(command)[:MAX_INTENT_COMMAND_CHARACTERS]}"
    context = ledger_context(hook_input=hook_input) if started_under is None else dict(started_under)

    extra: Dict[str, Any] = {"access": access, "agent": AGENT}
    if kind == "started":
        extra["arguments"] = ledger_json(tool.arguments)
    extra["call_id"] = identity.decision_id
    if governance_configured():
        extra["decision_id"] = identity.decision_id
    if kind in ("failed", "ran") and duration_ms is not None:
        extra["duration_ms"] = duration_ms
    extra["entry_key"] = f"{identity.decision_id}:{'start' if kind == 'started' else 'end'}"
    if kind in ("failed", "ran") and exit_status is not None:
        extra["exit_status"] = exit_status
    if "incident_key" in context:
        extra["incident_key"] = context["incident_key"]
    extra["intent_source"] = "investigation" if investigation else "none"

    line = ""
    if kind == "not_run":
        detail = _one_line(reason)[:MAX_DETAIL_CHARACTERS] if isinstance(reason, str) else ""
        result: Dict[str, Any] = {"outcome": outcome}
        if detail:
            result["reason"] = detail
        extra["output"] = ledger_json(result)
        line = detail
    elif kind in ("failed", "ran"):
        if "tool_response" in hook_input:
            extra["output"] = ledger_json(hook_input["tool_response"])
        line = _first_line(_output_text(hook_input.get("tool_response")))

    if "references" in context:
        extra["references"] = context["references"]
    extra["request_id"] = identity.request_id
    extra["session_id"] = identity.session_id
    extra["target"] = target
    extra["tool_name"] = tool.name
    extra["tool_namespace"] = tool.namespace

    message = intent
    if kind in LEDGER_ENDINGS or kind == "ran":
        ending = "ok" if kind == "ran" and exit_status == 0 else LEDGER_ENDINGS.get(kind, kind)
        message = f"{intent}: {ending}" + (f" - {line}" if line else "")
    return ledger_envelope(
        entity_id=target,
        entity_type="tool_call",
        event=f"agent.tool_call.{kind}",
        extra=extra,
        message=message[:MAX_LEDGER_MESSAGE_CHARACTERS],
        session_id=identity.session_id,
    )


def _investigation_call(words: Sequence[str]) -> Optional[Tuple[str, str, Optional[str]]]:
    """(subcommand, incident_key, service) when the call starts by running the investigation CLI's
    join or close, else None. `service` is None when the command gives none.

    Only the call's first simple command is read: its words up to the first shell punctuation word.
    They must be any `NAME=value` words, a Python interpreter, its flags that take no argument,
    INVESTIGATION_SCRIPT, `join` or `close`, and arguments that give the key as `--incident-key K`
    or `--incident-key=K`, and the service likewise as `--service S`. The last one given wins, as
    argparse takes it, and other arguments are left to the CLI, which refuses them. What follows, such as `2>&1` or a pipe, is not read, so a
    later command, a here-doc's body or an echo or printf of the CLI's words is no call.
    """
    # A separator is a word of nothing but punctuation: a quoted argument, such as a summary that
    # starts with "(", is one word with other characters in it.
    end = next((index for index, word in enumerate(words) if word and not word.strip(SHELL_PUNCTUATION)), len(words))
    position = 0
    while position < end and SHELL_ASSIGNMENT.fullmatch(words[position]):
        position += 1
    if position == end or not PYTHON_INTERPRETER.fullmatch(words[position].rsplit("/", 1)[-1]):
        return None
    position += 1
    while position < end and PYTHON_FLAGS.fullmatch(words[position]):
        position += 1
    if end - position < 2 or words[position].rsplit("/", 1)[-1] != INVESTIGATION_SCRIPT:
        return None
    subcommand = words[position + 1]
    if subcommand not in INVESTIGATION_ACTIONS:
        return None
    given: Dict[str, str] = {}
    for index in range(position + 2, end):
        for name in ("--incident-key", "--service"):
            if words[index] == name and index + 1 < end:
                given[name] = words[index + 1]
            elif words[index].startswith(f"{name}="):
                given[name] = words[index][len(name) + 1:]
    key = given.get("--incident-key", "").strip()
    if not key:
        return None
    service = given.get("--service")
    return subcommand, key, None if service is None else service.strip()


def _investigation_intent(tool: Any) -> Optional[str]:
    """The title of a Bash call that runs the investigation CLI's join or close, "Investigation
    joined" or "Investigation closed", else None. Those calls are records, not tasks, so the
    agent's own wording, which reads as an instruction, is not their title.
    """
    command = tool.arguments.get("command")
    if (tool.namespace, tool.name) != (AGENT, "Bash") or not isinstance(command, str):
        return None
    call = _investigation_call(_command_words(command))
    return f"Investigation {INVESTIGATION_ACTIONS[call[0]]}" if call else None


def investigation_action(
    *, hook_input: Mapping[str, Any]
) -> Optional[Tuple[str, str, Optional[str], Optional[Dict[str, str]]]]:
    """(action, incident_key, service, closing) for a Bash call that joined or closed an investigation, else None.

    `action` is "joined" or "closed". Both must hold: the command starts by running the CLI, as
    _investigation_call reads it, and the output has a line that is a JSON object stating the
    matching action and the key and service the command gave. So a key the command passes through a
    shell variable is not recognized. Codex gives a Bash call no exit code, so that line is the only sign
    the command worked. `closing` is a close's {status, summary} from that line, None for a join or a
    line without a valid pair. Only for PostToolUse. Never raises.
    """
    try:
        if hook_input.get("hook_event_name") != "PostToolUse":
            return None
        tool = normalize_tool_call(hook_input)
        command = tool.arguments.get("command")
        if (tool.namespace, tool.name) != (AGENT, "Bash") or not isinstance(command, str):
            return None
        call = _investigation_call(_command_words(command))
        if call is None:
            return None
        action, incident_key, service = INVESTIGATION_ACTIONS[call[0]], call[1], call[2]
        for line in _output_text(hook_input.get("tool_response")).splitlines():
            if not line.lstrip().startswith("{"):
                continue
            try:
                printed = json.loads(line)
            except ValueError:
                continue
            if isinstance(printed, dict) and printed.get("action") == action \
                    and printed.get("incident_key") == incident_key and printed.get("service") == service:
                return action, incident_key, service, _closing(printed) if action == "closed" else None
        return None
    except Exception:
        return None


def _closing(printed: Mapping[str, Any]) -> Optional[Dict[str, str]]:
    """{status, summary} from a close's printed line, or None unless both are valid."""
    status, summary = printed.get("status"), printed.get("summary")
    if status not in CLOSE_STATUSES or not isinstance(summary, str):
        return None
    summary = _one_line(summary)[:MAX_SUMMARY_CHARACTERS]
    return {"status": status, "summary": summary} if summary else None


def investigation_event(
    *,
    action: str,
    config: LedgerConfig,
    hook_input: Mapping[str, Any],
    incident_key: str,
    service: Optional[str] = None,
    closing: Optional[Mapping[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """The `joined` or `closed` event for this session and investigation. None without an identity.

    It has no entity_id: the ledger service sets it to the investigation it derives from the incident key.
    A close's `closing`, {status, summary}, goes in its extra.
    """
    try:
        session_id = call_identity(hook_input, normalize_tool_call(hook_input)).session_id
    except Exception:
        return None
    extra = {"agent": AGENT, "incident_key": incident_key, "session_id": session_id}
    if service is not None:
        extra["service"] = service
    if closing is not None:
        extra.update(status=closing["status"], summary=closing["summary"])
    return ledger_envelope(
        entity_id=None,
        entity_type="investigation",
        event=f"agent.investigation.{action}",
        extra=extra,
        message=f"{AGENT} {action} investigation {incident_key}"[:MAX_LEDGER_MESSAGE_CHARACTERS],
        session_id=session_id,
    )


def post_ledger_events(*, events: Sequence[Mapping[str, Any]], key: Optional[str], timeout: float, url: str) -> Tuple[int, bytes]:
    """POST `events` as one JSON array. (status, body); OSError on a network failure or timeout.

    `key` is the Mezmo access key, sent in the Authorization header; None sends no header.
    """
    refuse_exposed_token(url, key or "")
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if key is not None:
        headers["Authorization"] = authorization_header(key=key)
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
    return statuses or None


def _retryable(status: int) -> bool:
    # A 401 means the key is wrong, not the event.
    # Keep the event so it can be sent once the key is fixed.
    return status in (401, 408, 429) or 500 <= status < 600


def _open_retry_file(*, mode: str, path: Path) -> Any:
    """Open the retry file for writing, readable only by its owner (mode 0600), as append_audit does."""

    def private(name: str, flags: int) -> int:
        return os.open(name, flags, 0o600)

    # surrogateescape writes back, byte for byte, a line _read_lines read with a bad byte.
    handle = open(path, mode, encoding="utf-8", errors="surrogateescape", opener=private)
    # A file left from before keeps its mode until narrowed here.
    file_mode = os.fstat(handle.fileno()).st_mode
    if os.name != "nt" and stat.S_ISREG(file_mode) and file_mode & 0o077:
        os.fchmod(handle.fileno(), 0o600)
    return handle


@contextmanager
def _spool_locked(path: Path) -> Iterator[None]:
    """Hold SPOOL_LOCK and then `_locked(path)`, for every read and write of the retry file."""
    with SPOOL_LOCK, _locked(path):
        yield


def key_fingerprint(key: Optional[str]) -> str:
    """A non-secret id of the key a record was queued with, so a retry never sends it with another account's key."""
    return hashlib.sha256((key or "").encode("utf-8")).hexdigest()[:16]


def _spool_ledger_events(*, config: LedgerConfig, events: Sequence[Mapping[str, Any]]) -> None:
    """Append `events` to the retry file. The file keeps arguments and output, so it's private.

    Past LEDGER_SPOOL_MAX_LINES the oldest lines are removed here too, not only by
    drain_ledger_spool, whose resend can still be running when the hook ends.
    """
    if not events:
        return
    path = ledger_spool_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    key_id = key_fingerprint(config.key)
    text = "".join(
        json.dumps({"event": event, "key_id": key_id, "url": config.url}, separators=(",", ":")) + "\n" for event in events
    )
    with _spool_locked(path):
        with _open_retry_file(mode="a", path=path) as handle:
            handle.write(text)
        lines = _read_lines(path)
        over = max(0, len(lines) - LEDGER_SPOOL_MAX_LINES)
        if over:
            temporary = Path(f"{path}.tmp")
            with _open_retry_file(mode="w", path=temporary) as handle:
                handle.write("".join(line + "\n" for line in lines[over:]))
            os.replace(temporary, path)
    if over:
        LEDGER_MESSAGES.append(
            f"Mezmo ledger dropped {over} saved event(s): the retry file keeps at most {LEDGER_SPOOL_MAX_LINES}"
        )


def save_ledger_events(*, config: LedgerConfig, events: Sequence[Mapping[str, Any]]) -> None:
    try:
        _spool_ledger_events(config=config, events=events)
    except Exception as error:
        LEDGER_MESSAGES.append(f"Mezmo ledger could not save {len(events)} event(s) for retry: {error}")


def send_ledger_events(
    *,
    config: LedgerConfig,
    events: Sequence[Mapping[str, Any]],
    ledger_http: LedgerHttp,
    timeout: Optional[float] = None,
) -> LedgerSendResult:
    """Send new events in one request and say which to save for retry. Never raises.

    Nothing here writes the retry file: the caller saves `save`, so an event is saved at most
    once. Events the server refuses for good are dropped. `timeout` defaults to
    LEDGER_TIMEOUT_SECONDS, read at call time.
    """
    events = list(events)
    try:
        if timeout is None:
            timeout = LEDGER_TIMEOUT_SECONDS
        try:
            status, body = ledger_http(events=events, key=config.key, timeout=timeout, url=config.url)
        except OSError:
            return LedgerSendResult(answered=False, save=events)
        if status == 207:
            statuses = _ledger_statuses(body)
            if statuses is None:
                LEDGER_MESSAGES.append(
                    f"Mezmo ledger dropped {len(events)} event(s): "
                    "the ledger service answered HTTP 207 without usable event statuses"
                )
                return LedgerSendResult(answered=True, save=[])
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


# Where ledger_hook keeps its last send in the session state, for /mezmo:test and doctor.
LAST_SEND_FIELD = "ledger_last_send"


def recording_http(*, ledger_http: LedgerHttp, outcome: Dict[str, Any]) -> LedgerHttp:
    """`ledger_http`, keeping in `outcome` the HTTP status it got, or the error that kept it from one."""

    def send(**request: Any) -> Tuple[int, bytes]:
        try:
            status, body = ledger_http(**request)
        except Exception as error:
            outcome["error"] = str(error) or type(error).__name__
            raise
        outcome["status"] = status
        return status, body

    return send


def last_send(*, config: LedgerConfig, sends: Sequence[Tuple[int, Mapping[str, Any]]]) -> Dict[str, Any]:
    """What the session state keeps of one run's sends, each (event count, its recording_http outcome).

    The time, the URL's host, the HTTP status or the error, and the event count: never the key or
    an event. A send that wasn't answered 202 outranks one that was, so a failure is never hidden.
    """
    outcomes = [outcome for _, outcome in sends]
    worst = next((outcome for outcome in outcomes if outcome.get("status") != 202), outcomes[0] if outcomes else {})
    status = worst.get("status")
    error = worst.get("error")
    if status is None and error is None:
        error = f"no answer within {LEDGER_TIMEOUT_SECONDS:g} seconds"
    if error is not None:
        if config.key:
            error = error.replace(config.key, "<key>")
        error = _one_line(error)[:MAX_LEDGER_MESSAGE_CHARACTERS]
    return {
        "error": error,
        "events": sum(count for count, _ in sends),
        "host": urlparse(config.url).hostname,
        "status": status,
        "ts": datetime.now(timezone.utc).isoformat(),
    }


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
    refuses it for good. A line saved for another URL is removed unsent. A line saved with another
    key stays, unsent, for that key. Past LEDGER_SPOOL_MAX_LINES the oldest lines are removed.
    """
    try:
        path = ledger_spool_path()
        if not path.exists():
            return
        with _drain_lock(path) as held:
            if not held:
                return
            with _spool_locked(path):
                lines = _read_lines(path)
            taken: List[Tuple[str, Dict[str, Any]]] = []
            finished: List[str] = []
            other_url = 0
            key_id = key_fingerprint(config.key)
            for line in lines:
                try:
                    line.encode("utf-8")  # a line with a bad byte is bad
                    entry = json.loads(line)
                except ValueError:
                    entry = None
                usable = isinstance(entry, dict) and isinstance(entry.get("event"), dict) and isinstance(entry.get("url"), str)
                if not usable:
                    finished.append(line)  # a bad line
                elif entry["url"] != config.url:
                    finished.append(line)  # MEZMO_LEDGER_URL has changed, so nothing would resend it
                    other_url += 1
                elif entry.get("key_id") != key_id:
                    # Queued with another key, or before records named their key: left for that key, never sent with this one.
                    continue
                elif len(taken) < LEDGER_DRAIN_BATCH:
                    taken.append((line, entry["event"]))
            if taken:
                finished.extend(_resend_ledger_events(config=config, ledger_http=ledger_http, taken=taken))
            if not finished and len(lines) <= LEDGER_SPOOL_MAX_LINES:
                return
            with _spool_locked(path):
                remaining = _read_lines(path)
                for line in finished:
                    if line in remaining:
                        remaining.remove(line)
                # Applied after the resend, so a line the server just took isn't counted.
                over = max(0, len(remaining) - LEDGER_SPOOL_MAX_LINES)
                del remaining[:over]
                temporary = Path(f"{path}.tmp")
                with _open_retry_file(mode="w", path=temporary) as handle:
                    handle.write("".join(line + "\n" for line in remaining))
                os.replace(temporary, path)
            if other_url:
                LEDGER_MESSAGES.append(f"Mezmo ledger dropped {other_url} saved event(s): saved for another URL")
            if over:
                LEDGER_MESSAGES.append(
                    f"Mezmo ledger dropped {over} saved event(s): the retry file keeps at most {LEDGER_SPOOL_MAX_LINES}"
                )
    except Exception as error:
        LEDGER_MESSAGES.append(f"Mezmo ledger could not resend saved events: {str(error) or type(error).__name__}")


def _resend_ledger_events(
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
        statuses = _ledger_statuses(body)
        if statuses is None:
            LEDGER_MESSAGES.append(
                f"Mezmo ledger dropped {len(taken)} saved event(s): "
                "the ledger service answered HTTP 207 without usable event statuses"
            )
            return [line for line, _ in taken]
        return [line for index, (line, _) in enumerate(taken) if statuses.get(index) == "accepted"]
    if 200 <= status < 300:
        return [line for line, _ in taken]
    if _retryable(status):
        return []
    LEDGER_MESSAGES.append(f"Mezmo ledger dropped {len(taken)} saved event(s): the ledger service answered HTTP {status}")
    return [line for line, _ in taken]


def ledger_warn(*, message: str) -> None:
    """Print to stderr. A stderr that fails must not stop the hook, so this never raises."""
    try:
        print(message, file=sys.stderr)
    except Exception:
        pass


def print_ledger_messages() -> None:
    """Print and clear LEDGER_MESSAGES to stderr. Never raises."""
    while LEDGER_MESSAGES:
        ledger_warn(message=LEDGER_MESSAGES.pop(0))


def start_ledger_thread(*, work: Callable[[], None]) -> threading.Thread:
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


def record_governance_refusal(
    *, hook_input: Mapping[str, Any], ledger_http: Optional[LedgerHttp] = None, outcome: str, reason: Optional[str]
) -> bool:
    """Send governance's refusal of this call as one `not_run` event. Never raises.

    end_governance_refusal calls this once the deny is printed and flushed. Codex runs the tool when
    a hook outlives its timeout, so the request runs on a daemon thread and this waits for it until
    REFUSED_TIMEOUT_SECONDS of wall-clock time after it was called, so building the event counts.
    This thread then saves what wasn't sent, for the next run of this hook to resend. No drain,
    and nothing here ends the process: it returns whether the request is still running, for the
    caller to act on.
    """
    thread: Optional[threading.Thread] = None
    # From before the event is built, so the git lookup for person_email counts against the wait.
    deadline = time.monotonic() + REFUSED_TIMEOUT_SECONDS
    try:
        if _skip_ledger_mcp_tool(hook_input=hook_input):
            return False
        config = load_ledger_config()
        if config is None:
            return False
        event = ledger_event(config=config, hook_input=hook_input, kind="not_run", outcome=outcome, reason=reason)
        if event is None:
            return False
        # Looked up per call, not bound at definition, so tests can stand in for it.
        send = ledger_http or post_ledger_events
        sent: List[LedgerSendResult] = []
        try:
            thread = start_ledger_thread(
                work=lambda: sent.append(
                    send_ledger_events(config=config, events=[event], ledger_http=send, timeout=REFUSED_TIMEOUT_SECONDS)
                )
            )
            thread.join(max(0.0, deadline - time.monotonic()))
        except Exception as error:
            LEDGER_MESSAGES.append(f"Mezmo ledger could not start: {str(error) or type(error).__name__}")
        # Only this thread saves the event, so it is saved at most once. A request still running
        # may yet deliver it, and that duplicate is accepted.
        result = sent[0] if sent else LedgerSendResult(answered=False, save=[event])
        save_ledger_events(config=config, events=result.save)
        print_ledger_messages()
    except Exception as error:
        LEDGER_MESSAGES.append(f"Mezmo ledger could not record the refusal: {str(error) or type(error).__name__}")
        print_ledger_messages()
    return thread is not None and thread.is_alive()


def end_governance_refusal(*, hook_input: Mapping[str, Any], outcome: str, reason: Optional[str]) -> None:
    """Tell the ledger governance refused this call, then end the process with exit code 0.

    mezmo_governance_hook.py calls this once its deny is printed. Telling the ledger must not lose
    the deny, change it or hold the hook, so: flush it, send the ledger's prints to stderr, and end
    the process once the ledger is done or LEDGER_REFUSAL_BUDGET_SECONDS is up, whatever stderr
    does. While the ledger is off it returns at once and touches nothing.
    """
    if not ledger_configured():
        return
    if sys.stdout is None:
        # No stdout, so the deny reached nobody and Codex runs the call: nothing to tell.
        return
    sys.stdout.flush()
    sys.stdout = sys.stderr
    try:
        timer = threading.Timer(LEDGER_REFUSAL_BUDGET_SECONDS, os._exit, args=(0,))
        timer.daemon = True
        timer.start()
    except Exception:
        # With no timer the ledger's time has no bound, so it is not told.
        return
    try:
        record_governance_refusal(hook_input=hook_input, outcome=outcome, reason=reason)
    except BaseException:
        pass
    # The interpreter's exit would flush stderr again and turn a failure there into exit 120,
    # and would wait for any thread the ledger left running.
    try:
        sys.stderr.flush()
    except Exception:
        pass
    timer.cancel()
    os._exit(0)


def save_start_time(*, decision_id: str) -> None:
    """Save this call's start time, the moment governance approved it, for duration_ms. Never raises.

    mezmo_governance_hook.py calls this once the call is allowed, so it cannot change the decision.
    Only while the ledger is on. One file per call, which PostToolUse reads and deletes, so no lock.
    """
    try:
        if not ledger_configured():
            return
        path = start_time_path(decision_id)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        def private(name: str, flags: int) -> int:
            return os.open(name, flags, 0o600)

        # Wall-clock time: PostToolUse runs in another process, so a monotonic clock can't compare.
        with open(path, "w", encoding="utf-8", opener=private) as handle:
            handle.write(str(time.time()))
    except Exception:
        pass


def update_session_state(
    *, change: Callable[[Dict[str, Any]], None], lock_timeout: Optional[float] = None, session_id: str
) -> None:
    """Apply `change` to this session's state, under its lock, keeping what else it holds. Never raises.

    The file is replaced whole, so a reader never sees half of it, and it is private, as
    save_start_time's files are. A file that can't be read starts again empty. With a
    `lock_timeout`, a lock still held elsewhere after that many seconds means nothing is read or
    written.
    """
    try:
        path = session_state_path(session_id)
        path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)

        def private(name: str, flags: int) -> int:
            return os.open(name, flags, 0o600)

        with _locked(path, lock_timeout):
            try:
                state = json.loads(path.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                state = {}
            if not isinstance(state, dict):
                state = {}
            change(state)
            state.update(session_id=session_id, updated=datetime.now(timezone.utc).isoformat(), version=1)
            temporary = path.with_name(path.name + ".tmp")
            with open(temporary, "w", encoding="utf-8", opener=private) as handle:
                # A file left from before keeps its mode until narrowed here.
                if os.name != "nt":
                    os.fchmod(handle.fileno(), 0o600)
                handle.write(json.dumps(state, indent=1) + "\n")
            os.replace(temporary, path)
    except Exception:
        pass


def _call_duration_ms(*, hook_input: Mapping[str, Any]) -> Optional[int]:
    """Milliseconds from this call's start time to now. Never raises.

    Reads the start time save_start_time wrote, and deletes its file. None without a readable
    start time, as for a call approved while the ledger was off, or a time that is not finite
    or lies ahead, as after the clock was set back.
    """
    try:
        path = start_time_path(call_identity(hook_input, normalize_tool_call(hook_input)).decision_id)
        text = path.read_text(encoding="utf-8")
    except Exception:
        return None
    try:
        path.unlink()
    except OSError:
        pass
    try:
        # round raises on a time that is not finite.
        duration = round((time.time() - float(text)) * 1000)
    except (OverflowError, ValueError):
        return None
    return duration if duration >= 0 else None


def _remove_old_start_times() -> None:
    """Delete the start times of calls approved, and the session states last changed, more than
    STATE_RETENTION_SECONDS ago. Never raises.

    A file another run deleted first is passed over, so two runs need no lock.
    """
    cutoff = time.time() - STATE_RETENTION_SECONDS
    # Never a session's .lock: another hook may hold it, and a new file at that path would not be
    # locked against it.
    for folder, suffixes in ((call_state_dir(), None), (state_dir(), (".json",))):
        try:
            with os.scandir(folder) as entries:
                for entry in entries:
                    try:
                        if suffixes is not None and not entry.name.endswith(suffixes):
                            continue
                        if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                            os.unlink(entry.path)
                    except OSError:
                        pass
        except Exception:
            pass


def _is_shell(hook_input: Mapping[str, Any]) -> bool:
    try:
        tool = normalize_tool_call(hook_input)
    except Exception:
        return False
    return (tool.namespace, tool.name) == (AGENT, "Bash")


def _new_event(
    *, config: LedgerConfig, hook_input: Mapping[str, Any], exits: Optional[Mapping[str, int]] = None
) -> Optional[Dict[str, Any]]:
    """The event this hook input gives, or None when there's nothing new to send. Never raises.

    Codex gives a shell call's PostToolUse its output but no exit code, so `exits`, the exit codes
    the rollout showed by call id, decides between `ran` and `failed` for one.
    """
    fields: Dict[str, Any]
    if hook_input.get("hook_event_name") == "PreToolUse":
        fields = {"kind": "started"}
    else:
        try:
            kind, exit_status = ledger_result(hook_input=hook_input)
        except Exception:
            # Codex reports back only on a call that ran, so a result that can't be read is one.
            kind, exit_status = "ran", None
        exit_code = (exits or {}).get(str(hook_input.get("tool_use_id")))
        if kind == "ran" and exit_status is None and isinstance(exit_code, int) and _is_shell(hook_input):
            kind, exit_status = ("failed", exit_code) if exit_code else ("ran", 0)
        fields = {"duration_ms": _call_duration_ms(hook_input=hook_input), "exit_status": exit_status, "kind": kind}
    try:
        return ledger_event(config=config, hook_input=hook_input, **fields)
    except Exception as error:
        ledger_warn(message=f"Mezmo ledger could not build an event: {str(error) or type(error).__name__}")
        return None


def _state_incident_key(*, session_id: str) -> Any:
    """The incident key of the investigation in this session's state, or None. Never raises."""
    try:
        state = json.loads(session_state_path(session_id).read_text(encoding="utf-8"))
        return state["investigation"]["incident_key"]
    except Exception:
        return None


def _lock_timeout(*, deadline: float) -> float:
    """How long a state change may wait for the session state's lock: never past `deadline`."""
    return max(0.0, min(INVESTIGATION_STATE_TIMEOUT_SECONDS, deadline - time.monotonic()))


def _change_investigation(
    *,
    action: str,
    config: LedgerConfig,
    deadline: float,
    hook_input: Mapping[str, Any],
    incident_key: str,
    service: Optional[str],
    closing: Optional[Mapping[str, str]] = None,
) -> Optional[Dict[str, Any]]:
    """Join or leave the investigation in the session state, and return its event. Never raises.

    `joined` is returned only once the session state holds this incident key. `closed` is returned
    whatever the state holds, which is cleared only when it holds this incident key.
    """
    closed: Optional[Dict[str, Any]] = None
    try:
        session_id = hook_input.get("session_id")
        session_id = session_id.strip() if isinstance(session_id, str) else ""
        joined_at = datetime.now(timezone.utc).isoformat()

        def join(state: Dict[str, Any]) -> None:
            state["investigation"] = {"incident_key": incident_key, "joined_at": joined_at}
            if service is not None:
                state["investigation"]["service"] = service

        def leave(state: Dict[str, Any]) -> None:
            current = state.get("investigation")
            if isinstance(current, dict) and current.get("incident_key") == incident_key:
                del state["investigation"]

        if action == "joined":
            if session_id:
                update_session_state(change=join, lock_timeout=_lock_timeout(deadline=deadline), session_id=session_id)
            if not session_id or _state_incident_key(session_id=session_id) != incident_key:
                ledger_warn(
                    message=f"Mezmo ledger did not join investigation {incident_key}: the session state was not written"
                )
                return None
            return investigation_event(
                action=action,
                config=config,
                hook_input=hook_input,
                incident_key=incident_key,
                service=service,
            )
        closed = investigation_event(
            action=action, closing=closing, config=config, hook_input=hook_input, incident_key=incident_key
        )
        # No write for another investigation, so a close can't create a state file.
        if session_id and _state_incident_key(session_id=session_id) == incident_key:
            update_session_state(change=leave, lock_timeout=_lock_timeout(deadline=deadline), session_id=session_id)
            if _state_incident_key(session_id=session_id) == incident_key:
                ledger_warn(
                    message=f"Mezmo ledger did not leave investigation {incident_key}: the session state still holds it"
                )
        return closed
    except Exception as error:
        ledger_warn(message=f"Mezmo ledger could not record the investigation: {str(error) or type(error).__name__}")
        # A close sends its `closed` even when the state change failed.
        return closed


def _track_pending(
    *,
    hook_input: Mapping[str, Any],
    update: Callable[[Callable[[Dict[str, Any]], None]], None],
    awaiting_exit: bool = False,
) -> None:
    """Keep a started call in the session state until its end is recorded, or drop an ended one. Never raises.

    With `awaiting_exit`, an ended shell call whose exit code the rollout didn't show yet stays,
    marked, so a later run can correct its `ran`.
    """
    try:
        tool_use_id = hook_input.get("tool_use_id")
        if not isinstance(tool_use_id, str) or not tool_use_id:
            return
        started = hook_input.get("hook_event_name") == "PreToolUse"
        kept = {name: hook_input[name] for name in PENDING_FIELDS if name in hook_input}
        if started:
            incident_key = ledger_context(hook_input=hook_input).get("incident_key")
            kept[PENDING_INCIDENT_KEY] = incident_key if isinstance(incident_key, str) else None

        def change(state: Dict[str, Any]) -> None:
            pending = state.get(PENDING_CALLS_FIELD)
            pending = dict(pending) if isinstance(pending, dict) else {}
            if started and len(pending) < MAX_PENDING_CALLS:
                pending[tool_use_id] = kept
            elif awaiting_exit and isinstance(pending.get(tool_use_id), dict):
                pending[tool_use_id] = {**pending[tool_use_id], PENDING_AWAITING_EXIT: True}
            elif not started:
                pending.pop(tool_use_id, None)
            _set_pending(state, pending)

        update(change)
    except Exception:
        pass


def _set_pending(state: Dict[str, Any], pending: Dict[str, Any]) -> None:
    """Keep `pending` in the state, or no field at all once nothing is left open."""
    if pending:
        state[PENDING_CALLS_FIELD] = pending
    else:
        state.pop(PENDING_CALLS_FIELD, None)


def _rollout_records(*, path: str, offset: int) -> Tuple[List[Dict[str, Any]], int]:
    """The rollout's JSON records after `offset`, and the offset past the last whole line read.

    A rollout shorter than `offset` was replaced, so it is read from the start. A line longer than
    MAX_ROLLOUT_READ_BYTES is skipped: its first part is passed over here, and the rest, which is
    no JSON on its own, on the next read.
    """
    with open(path, "rb") as rollout:
        if offset > os.fstat(rollout.fileno()).st_size:
            offset = 0
        rollout.seek(offset)
        data = rollout.read(MAX_ROLLOUT_READ_BYTES)
    end = data.rfind(b"\n")
    if end < 0:
        # A whole read with no newline is one oversized line; a shorter one is a line still being written.
        return [], offset + len(data) if len(data) == MAX_ROLLOUT_READ_BYTES else offset
    records: List[Dict[str, Any]] = []
    for line in data[:end].splitlines():
        try:
            record = json.loads(line)
        except ValueError:
            continue
        if isinstance(record, dict):
            records.append(record)
    return records, offset + end + 1


def _item_text(item: Mapping[str, Any]) -> str:
    """The text of a rollout item: an agent message's, or a failed call's error."""
    content = item.get("content")
    if item.get("type") != "AgentMessage":
        result = item.get("result")
        content = result.get("content") if isinstance(result, dict) else None
        if not isinstance(content, list):
            error = item.get("error") or item.get("aggregated_output")
            return error if isinstance(error, str) else ""
    if not isinstance(content, list):
        return ""
    return "\n".join(
        part["text"] for part in content if isinstance(part, dict) and isinstance(part.get("text"), str)
    )


def _item_duration_ms(item: Mapping[str, Any]) -> Optional[int]:
    duration = item.get("duration")
    if not isinstance(duration, dict):
        return None
    seconds, nanos = duration.get("secs"), duration.get("nanos")
    if not isinstance(seconds, int) or not isinstance(nanos, int):
        return None
    return seconds * 1000 + nanos // 1_000_000


def step_event(*, hook_input: Mapping[str, Any], incident_key: str, text: str) -> Optional[Dict[str, Any]]:
    """`agent.investigation.step`: what the agent said between its calls, in its own words."""
    session_id = hook_input.get("session_id")
    if not isinstance(session_id, str) or not session_id.strip():
        return None
    text = text.strip()[:MAX_STEP_CHARACTERS]
    extra = {"agent": AGENT, "incident_key": incident_key, "session_id": session_id.strip(), "text": text}
    # No entity_id: the ledger service sets it to the investigation it derives from the incident key.
    return ledger_envelope(
        entity_id=None,
        entity_type="investigation",
        event=STEP_EVENT,
        extra=extra,
        message=_one_line(text)[:MAX_LEDGER_MESSAGE_CHARACTERS],
        session_id=session_id.strip(),
    )


def _catch_up(
    *,
    config: LedgerConfig,
    hook_input: Mapping[str, Any],
    update: Callable[[Callable[[Dict[str, Any]], None]], None],
    exits: Optional[Dict[str, int]] = None,
) -> List[Dict[str, Any]]:
    """Events for what the rollout shows since it was last read. Never raises.

    A call it shows failed, which Codex gives no PostToolUse, ends as `failed` with its error. A
    shell call already recorded as ran before its exit code showed is corrected to `failed` when
    that exit code isn't 0. On Stop, a call still kept has no end coming, so it ends as
    `no_result`, unless it was already recorded as ran. Each message the agent wrote is a step,
    sent only while the session is in an investigation. Each shell command's exit code goes into
    `exits` by call id. The rollout is read and the state changed under the state's lock, so two
    runs never send the same end or step.
    """
    try:
        path = hook_input.get("transcript_path")
        if not isinstance(path, str) or not path:
            return []
        stop = hook_input.get("hook_event_name") == "Stop"
        ended: List[Tuple[Dict[str, Any], str, Dict[str, Any]]] = []
        steps: List[str] = []
        found_exits: Dict[str, int] = {} if exits is None else exits

        def change(state: Dict[str, Any]) -> None:
            pending = state.get(PENDING_CALLS_FIELD)
            pending = dict(pending) if isinstance(pending, dict) else {}
            offset = state.get(ROLLOUT_OFFSET_FIELD)
            offset = offset if isinstance(offset, int) and offset >= 0 else 0
            records, offset = _rollout_records(path=path, offset=offset)
            # Read to the end: anything still awaited won't show up in this rollout.
            read_all = offset >= os.path.getsize(path)
            for record in records:
                payload = record.get("payload")
                item = payload.get("item") if isinstance(payload, dict) and payload.get("type") == "item_completed" else None
                if not isinstance(item, dict):
                    continue
                item_id = item.get("id")
                exit_code = item.get("exit_code")
                if item.get("type") == "CommandExecution" and isinstance(item_id, str) \
                        and isinstance(exit_code, int) and not isinstance(exit_code, bool):
                    found_exits[item_id] = exit_code
                kept = pending.get(item_id)
                if item.get("type") == "AgentMessage":
                    text = _item_text(item).strip()
                    if text:
                        steps.append(text)
                elif isinstance(kept, dict) and kept.get(PENDING_AWAITING_EXIT) and item_id in found_exits:
                    pending.pop(item_id)
                    if found_exits[item_id]:
                        ended.append((kept, "failed", item))
                elif isinstance(kept, dict) and item_id in found_exits:
                    # Read by another call's hook before this call's PostToolUse: kept for it, since
                    # the offset moves past it now.
                    pending[item_id] = {**kept, PENDING_EXIT_CODE: found_exits[item_id]}
                elif item.get("status") == "failed" and isinstance(kept, dict) and not kept.get(PENDING_AWAITING_EXIT):
                    ended.append((pending.pop(item_id), "failed", item))
            for call_id, kept in pending.items():
                saved = kept.get(PENDING_EXIT_CODE) if isinstance(kept, dict) else None
                if isinstance(saved, int) and not isinstance(saved, bool):
                    found_exits.setdefault(call_id, saved)
            if stop:
                ended.extend(
                    (kept, "no_result", {}) for kept in pending.values()
                    if isinstance(kept, dict) and not kept.get(PENDING_AWAITING_EXIT)
                )
                # A call already recorded as ran keeps waiting for its exit code until the rollout
                # has been read to the end.
                pending = {} if read_all else {
                    call_id: kept for call_id, kept in pending.items()
                    if isinstance(kept, dict) and kept.get(PENDING_AWAITING_EXIT)
                }
            _set_pending(state, pending)
            state[ROLLOUT_OFFSET_FIELD] = offset

        update(change)
        events: List[Dict[str, Any]] = []
        for kept, kind, item in ended:
            incident_key = kept.get(PENDING_INCIDENT_KEY)
            call = {name: kept[name] for name in PENDING_FIELDS if name in kept}
            call.update(hook_event_name="PostToolUse", transcript_path=path)
            if kind == "failed":
                call["tool_response"] = _item_text(item)
            exit_code = item.get("exit_code")
            event = ledger_event(
                config=config,
                duration_ms=_item_duration_ms(item),
                exit_status=exit_code if isinstance(exit_code, int) and not isinstance(exit_code, bool) else None,
                hook_input=call,
                kind=kind,
                started_under={"incident_key": incident_key} if isinstance(incident_key, str) and incident_key else {},
            )
            if event is not None:
                events.append(event)
        incident_key = ledger_context(hook_input=hook_input).get("incident_key")
        if incident_key:
            for text in steps:
                event = step_event(hook_input=hook_input, incident_key=incident_key, text=text)
                if event is not None:
                    events.append(event)
        return events
    except Exception as error:
        ledger_warn(message=f"Mezmo ledger could not read the session's rollout: {str(error) or type(error).__name__}")
        return []


def _record_references(*, deadline: float, hook_input: Mapping[str, Any]) -> None:
    """Add the references this call gives to the session state. Never raises, and never warns.

    A call that gives none, or has no session id, writes nothing, so it never creates or rewrites
    a state file. The session id is stripped as _change_investigation strips it, so the write lands
    in the file ledger_context reads. The merge runs on the state update_session_state read under
    its lock, which it waits for as a join does.
    """
    try:
        references = tool_references(hook_input=hook_input, read_at=datetime.now(timezone.utc).isoformat())
        session_id = hook_input.get("session_id")
        session_id = session_id.strip() if isinstance(session_id, str) else ""
        if not references or not session_id:
            return

        def add(state: Dict[str, Any]) -> None:
            state["references"] = merge_references(existing=state.get("references"), new=references)

        update_session_state(change=add, lock_timeout=_lock_timeout(deadline=deadline), session_id=session_id)
    except Exception:
        pass


def _record_last_send(
    *, config: LedgerConfig, hook_input: Mapping[str, Any], sends: List[Tuple[int, Dict[str, Any]]]
) -> None:
    """Keep this run's sends as the session's last send, for doctor. Never raises."""
    try:
        session_id = hook_input.get("session_id")
        session_id = session_id.strip() if isinstance(session_id, str) else ""
        if not sends or not session_id:
            return
        entry = last_send(config=config, sends=sends)
        update_session_state(
            change=lambda state: state.__setitem__(LAST_SEND_FIELD, entry),
            lock_timeout=LAST_SEND_STATE_TIMEOUT_SECONDS,
            session_id=session_id,
        )
    except Exception:
        pass


def handle(event: Mapping[str, Any], *, ledger_http: Optional[LedgerHttp] = None) -> None:
    """Send this call's event while resending saved ones, and wait up to one deadline. Never raises.

    Every hooked tool is recorded. Only this thread saves the new events, so each is saved at most
    once and never by a thread os._exit could cut off. A thread still running at the deadline is
    abandoned, and the duplicate it may still deliver is accepted.

    A join or close of an investigation changes the session state before any send starts, since
    main() may end the process at the deadline: a join before this call's event is built, so the
    event is stamped with it, and a close after. Each waits for the state's lock at most
    INVESTIGATION_STATE_TIMEOUT_SECONDS and never past the deadline. Its `joined` or `closed` goes in
    its own request.

    A call that ran and read Mezmo Memory or Insights adds its references to the state once the
    requests are started, so its own `ran` doesn't carry them and the next call's `started` does.
    It waits for the state's lock as a join does.
    """
    try:
        # Looked up per call, not bound at definition, so tests can stand in for it.
        ledger_http = ledger_http or post_ledger_events
        if event.get("hook_event_name") not in LEDGER_EVENTS:
            return
        if _skip_ledger_mcp_tool(hook_input=event):
            return
        try:
            config = load_ledger_config()
        except Exception:
            return
        if config is None:
            return
        # Real monotonic time, so nothing a caller passes can stretch the deadline. From before the
        # events are built and the state written, so a slow git lookup or lock can't push the save
        # past main()'s timer.
        deadline = time.monotonic() + LEDGER_TIMEOUT_SECONDS
        session_id = event.get("session_id")
        session = session_id.strip() if isinstance(session_id, str) else ""

        def update(change: Callable[[Dict[str, Any]], None]) -> None:
            if session:
                update_session_state(change=change, lock_timeout=_lock_timeout(deadline=deadline), session_id=session)

        if session:
            adopt_launched_investigation(hook_input=event, update=update)
        # Before this call's own event, so ends and steps that came first are sent first.
        exits: Dict[str, int] = {}
        caught_up = _catch_up(config=config, exits=exits, hook_input=event, update=update) if session else []
        if event.get("hook_event_name") == "Stop" and not caught_up:
            return
        action, incident_key, service, closing = investigation_action(hook_input=event) or ("", "", None, None)
        investigation: Optional[Dict[str, Any]] = None
        if action == "joined":
            investigation = _change_investigation(
                action=action,
                config=config,
                deadline=deadline,
                hook_input=event,
                incident_key=incident_key,
                service=service,
            )
        new_event = None if event.get("hook_event_name") == "Stop" else _new_event(config=config, exits=exits, hook_input=event)
        if new_event is not None:
            # A shell call whose exit code the rollout didn't show yet stays tracked, to correct later.
            awaiting = event.get("hook_event_name") == "PostToolUse" and _is_shell(event) \
                and isinstance(event.get("transcript_path"), str) and bool(event.get("transcript_path")) \
                and str(event.get("tool_use_id")) not in exits and new_event.get("event") == "agent.tool_call.ran" \
                and new_event.get("extra", {}).get("exit_status") is None
            _track_pending(awaiting_exit=awaiting, hook_input=event, update=update)
        if action == "closed":
            investigation = _change_investigation(
                action=action,
                config=config,
                deadline=deadline,
                hook_input=event,
                incident_key=incident_key,
                service=None,
                closing=closing,
            )
        sent: List[LedgerSendResult] = []
        sent_investigation: List[LedgerSendResult] = []
        sent_caught_up: List[LedgerSendResult] = []
        outcome: Dict[str, Any] = {}
        outcome_investigation: Dict[str, Any] = {}
        outcome_caught_up: Dict[str, Any] = {}
        threads: List[threading.Thread] = []
        try:
            # Requests that never share a body, so a bad saved event can't cost a new one, and one
            # new event's refusal never touches the other.
            if new_event is not None:
                threads.append(
                    start_ledger_thread(
                        work=lambda: sent.append(
                            send_ledger_events(
                                config=config,
                                events=[new_event],
                                ledger_http=recording_http(ledger_http=ledger_http, outcome=outcome),
                            )
                        )
                    )
                )
            if investigation is not None:
                threads.append(
                    start_ledger_thread(
                        work=lambda: sent_investigation.append(
                            send_ledger_events(
                                config=config,
                                events=[investigation],
                                ledger_http=recording_http(ledger_http=ledger_http, outcome=outcome_investigation),
                            )
                        )
                    )
                )
            if caught_up:
                threads.append(
                    start_ledger_thread(
                        work=lambda: sent_caught_up.append(
                            send_ledger_events(
                                config=config,
                                events=caught_up,
                                ledger_http=recording_http(ledger_http=ledger_http, outcome=outcome_caught_up),
                            )
                        )
                    )
                )
            threads.append(start_ledger_thread(work=lambda: drain_ledger_spool(config=config, ledger_http=ledger_http)))
        except Exception as error:
            ledger_warn(message=f"Mezmo ledger could not start: {str(error) or type(error).__name__}")
        # Done while the requests run, so the clean-up adds nothing to the wait.
        if event.get("hook_event_name") == "PostToolUse":
            _remove_old_start_times()
        # While the requests run, and after this call's own event was built, so its `ran` never
        # claims what it read and the next call's `started` does.
        if event.get("hook_event_name") == "PostToolUse" and new_event is not None \
                and new_event.get("event") == "agent.tool_call.ran":
            _record_references(deadline=deadline, hook_input=event)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        # Each event by its own result: one whose thread did not finish is saved.
        for new, results in ((new_event, sent), (investigation, sent_investigation)):
            if new is not None:
                result = results[0] if results else LedgerSendResult(answered=False, save=[new])
                save_ledger_events(config=config, events=result.save)
        if caught_up:
            result = sent_caught_up[0] if sent_caught_up else LedgerSendResult(answered=False, save=caught_up)
            save_ledger_events(config=config, events=result.save)
        # A copy of each outcome, so a thread still running can't change it while it is read.
        _record_last_send(
            config=config,
            hook_input=event,
            sends=[
                (1, dict(found))
                for new, found in ((new_event, outcome), (investigation, outcome_investigation))
                if new is not None
            ]
            + ([(len(caught_up), dict(outcome_caught_up))] if caught_up else []),
        )
        print_ledger_messages()
    except Exception as error:
        ledger_warn(message=f"Mezmo ledger failed: {str(error) or type(error).__name__}")


def main() -> int:
    # Ends the run, whatever it is waiting on, by the deadline plus a margin. An event it cuts off
    # is lost.
    try:
        timer = threading.Timer(LEDGER_TIMEOUT_SECONDS + LEDGER_EXIT_MARGIN_SECONDS, os._exit, args=(0,))
        timer.daemon = True
        timer.start()
    except Exception as error:
        # With no timer the run's time has no bound, so it sends nothing.
        ledger_warn(message=f"Mezmo ledger skipped this call: {str(error) or type(error).__name__}")
        return 0
    try:
        event = json.load(sys.stdin)
        if isinstance(event, dict):
            handle(event)
        else:
            ledger_warn(message="Mezmo ledger skipped this call: hook input must be a JSON object")
    except Exception as error:
        # Such as a body that isn't JSON, or a RecursionError on deeply nested input.
        ledger_warn(message=f"Mezmo ledger skipped this call: {str(error) or type(error).__name__}")
    try:
        sys.stdout.flush()
        sys.stderr.flush()
        flushed = True
    except Exception:
        flushed = False
    timer.cancel()
    # A ledger thread still waiting on the network would hold up the interpreter's exit, and a
    # stream that failed its flush would fail again there and turn exit 0 into 120.
    if not flushed or any(thread.is_alive() for thread in LEDGER_THREADS):
        os._exit(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
