#!/usr/bin/env python3
"""Codex hook that records each tool call in the Mezmo ledger.

Codex runs it alongside mezmo_governance_hook.py for every tool call.

- Before a call it sends `started`. After a call it sends `ran` or `failed`, with `duration_ms`:
  how long the call took.
- Codex does not report a failed Bash command, so those are sent as `ran`.
- A call governance refuses is sent as `not_run`. The governance hook reports it through
  `end_governance_refusal`, because Codex says nothing more about a denied call.
- Off unless MEZMO_LEDGER_ACCOUNT and MEZMO_LEDGER_URL are both set.
- It always exits 0 and prints no hook output, so it can never block or change a tool call.
- Events it could not send are saved to ~/.mezmo/ledger-spool.jsonl and resent on a later run.
  Claude Code uses the same file, so the code that handles it must match
  claude-code/scripts/ledger.py. test_ledger.py checks this.
- It must be in the same folder as mezmo_governance_hook.py: each imports the other.
"""

from __future__ import annotations

import errno
import json
import os
import re
import stat
import sys
import threading
import time
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Tuple
from urllib.error import HTTPError
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

GOVERNANCE_TOKEN_ENV = "MEZMO_GOVERNANCE_TOKEN"
GOVERNANCE_URL_ENV = "MEZMO_GOVERNANCE_URL"
LEDGER_ACCOUNT_ENV = "MEZMO_LEDGER_ACCOUNT"
LEDGER_INCIDENT_KEY_ENV = "MEZMO_LEDGER_INCIDENT_KEY"
LEDGER_INVESTIGATION_ID_ENV = "MEZMO_LEDGER_INVESTIGATION_ID"
LEDGER_KEY_ENV = "MEZMO_LEDGER_KEY"
LEDGER_SPOOL_ENV = "MEZMO_LEDGER_SPOOL"
LEDGER_URL_ENV = "MEZMO_LEDGER_URL"
DEFAULT_LEDGER_SPOOL = Path.home() / ".mezmo" / "ledger-spool.jsonl"
# Where a session's investigation and references are read from. Nothing here writes it.
DEFAULT_STATE_DIR = Path.home() / ".mezmo" / "governance" / "codex-sessions"
# Where each call's start time waits for the ledger hook, while the ledger is on.
DEFAULT_CALL_STATE_DIR = Path.home() / ".mezmo" / "governance" / "codex-calls"
# How long a start time may wait for its call to end. Older ones are from calls that never
# reported back, and PostToolUse deletes them.
STATE_RETENTION_SECONDS = 7 * 24 * 3600
LEDGER_APPLICATION = "mezmo-codex-plugin"
LEDGER_EVENTS = frozenset({"PostToolUse", "PreToolUse"})
# For application.version. A file install has no manifest beside the script, so this is a
# constant, and test_ledger.py checks it against .codex-plugin/plugin.json.
LEDGER_VERSION = "0.4.0"
# The socket timeout of each request this hook makes, and the deadline its main thread waits for
# its ledger threads from when they start. A socket timeout alone bounds neither DNS nor the whole
# request, hence the threads. Read at call time, so a test can patch it.
LEDGER_TIMEOUT_SECONDS = 3.0
# How far past LEDGER_TIMEOUT_SECONDS main() lets a run go before it ends the process, for the
# save and the prints after the wait. A retry file that another process holds locked would
# otherwise hold the call until Codex kills the hook.
LEDGER_EXIT_MARGIN_SECONDS = 1.0
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
LEDGER_ENDINGS = {"failed": "failed", "not_run": "not run", "ran": "ok"}

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
    account: str
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
def _locked(path: Path) -> Iterator[None]:
    """Hold an exclusive lock on `path`.lock, so parallel tool calls don't lose updates."""
    with open(f"{path}.lock", "a") as lock:
        try:
            import fcntl

            fcntl.flock(lock, fcntl.LOCK_EX)
        except (ImportError, OSError):
            pass  # no fcntl on Windows: best effort
        yield


def governance_configured() -> bool:
    """Whether the governance hook has both of its settings, so this call has a decision there."""
    return _setting(GOVERNANCE_TOKEN_ENV) is not None and _setting(GOVERNANCE_URL_ENV) is not None


def ledger_configured() -> bool:
    """Whether Mezmo ledger sending is on: both of its settings are set. Prints nothing."""
    return _setting(LEDGER_ACCOUNT_ENV) is not None and _setting(LEDGER_URL_ENV) is not None


def load_ledger_config() -> Optional[LedgerConfig]:
    """The ledger settings, or None when sending is off or the settings aren't safe to use."""
    account = _setting(LEDGER_ACCOUNT_ENV)
    url = _setting(LEDGER_URL_ENV)
    if account is None or url is None:
        return None
    # MEZMO_LEDGER_KEY, else the governance token, else no header.
    key = _setting(LEDGER_KEY_ENV)
    key_setting = LEDGER_KEY_ENV
    if key is None:
        key = _setting(GOVERNANCE_TOKEN_ENV)
        key_setting = GOVERNANCE_TOKEN_ENV
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
    if key is not None and not (key.isascii() and key.isprintable()):
        print(
            f"Mezmo ledger sending is off: {key_setting} holds a character an HTTP header cannot carry",
            file=sys.stderr,
        )
        return None
    return LedgerConfig(account, key, url)


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


def resolve_target(namespace: str, name: str, arguments: Mapping[str, Any]) -> Tuple[str, str]:
    """(target, access) for a call. C3's (capture epic) resolver replaces this stub."""
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
        "actor": {"account": account, "pk": f"{AGENT}:{session_id}", "type": "agent"},
        "application": {"name": LEDGER_APPLICATION, "version": LEDGER_VERSION},
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


def ledger_event(
    *,
    config: LedgerConfig,
    duration_ms: Optional[int] = None,
    exit_status: Optional[int] = None,
    hook_input: Mapping[str, Any],
    kind: str,
    outcome: Optional[str] = None,
    reason: Optional[str] = None,
) -> Optional[Dict[str, Any]]:
    """One tool-call event: `kind` is started, ran, failed or not_run. None without an identity."""
    try:
        tool = normalize_tool_call(hook_input)
        identity = call_identity(hook_input, tool)
    except Exception:
        return None
    target, access = resolve_target(tool.namespace, tool.name, tool.arguments)
    # Codex gives a call no description, so the intent is its name and the start of its command.
    intent = f"{tool.namespace}.{tool.name}"
    command = tool.arguments.get("command")
    if isinstance(command, str) and command.strip():
        intent += f": {_one_line(command)[:MAX_INTENT_COMMAND_CHARACTERS]}"
    context = ledger_context(account=config.account, hook_input=hook_input)

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
    extra["intent_source"] = "none"

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
    return statuses


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


def _spool_ledger_events(*, config: LedgerConfig, events: Sequence[Mapping[str, Any]]) -> None:
    """Append `events` to the retry file. The file keeps arguments and output, so it's private."""
    if not events:
        return
    path = ledger_spool_path()
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    text = "".join(json.dumps({"event": event, "url": config.url}, separators=(",", ":")) + "\n" for event in events)
    with _spool_locked(path):
        with _open_retry_file(mode="a", path=path) as handle:
            handle.write(text)


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
    refuses it for good. A line saved for another URL is removed unsent, and past
    LEDGER_SPOOL_MAX_LINES the oldest lines are removed.
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
        statuses = _ledger_statuses(body) or {}
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
    a hook outlives its timeout, so the request runs on a daemon thread and this waits for it at
    most REFUSED_TIMEOUT_SECONDS of wall-clock time. This thread then saves what wasn't sent,
    for the next run of this hook to resend. No drain, and nothing here ends the process: it
    returns whether the request is still running, for the caller to act on.
    """
    thread: Optional[threading.Thread] = None
    try:
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
            thread.join(REFUSED_TIMEOUT_SECONDS)
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
    """Delete the start times of calls approved more than STATE_RETENTION_SECONDS ago. Never raises.

    A file another run deleted first is passed over, so two runs need no lock.
    """
    try:
        cutoff = time.time() - STATE_RETENTION_SECONDS
        with os.scandir(call_state_dir()) as entries:
            for entry in entries:
                try:
                    if entry.is_file(follow_symlinks=False) and entry.stat(follow_symlinks=False).st_mtime < cutoff:
                        os.unlink(entry.path)
                except OSError:
                    pass
    except Exception:
        pass


def _new_event(*, config: LedgerConfig, hook_input: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The event this hook input gives, or None when there's nothing new to send. Never raises."""
    fields: Dict[str, Any]
    if hook_input.get("hook_event_name") == "PreToolUse":
        fields = {"kind": "started"}
    else:
        try:
            kind, exit_status = ledger_result(hook_input=hook_input)
        except Exception:
            # Codex reports back only on a call that ran, so a result that can't be read is one.
            kind, exit_status = "ran", None
        fields = {"duration_ms": _call_duration_ms(hook_input=hook_input), "exit_status": exit_status, "kind": kind}
    try:
        return ledger_event(config=config, hook_input=hook_input, **fields)
    except Exception as error:
        ledger_warn(message=f"Mezmo ledger could not build an event: {str(error) or type(error).__name__}")
        return None


def handle(event: Mapping[str, Any], *, ledger_http: Optional[LedgerHttp] = None) -> None:
    """Send this call's event while resending saved ones, and wait up to one deadline. Never raises.

    Every hooked tool is recorded. Only this thread saves the new event, so it is saved at most
    once and never by a thread os._exit could cut off. A thread still running at the deadline is
    abandoned, and the duplicate it may still deliver is accepted.
    """
    try:
        # Looked up per call, not bound at definition, so tests can stand in for it.
        ledger_http = ledger_http or post_ledger_events
        if event.get("hook_event_name") not in LEDGER_EVENTS:
            return
        try:
            config = load_ledger_config()
        except Exception:
            return
        if config is None:
            return
        new_event = _new_event(config=config, hook_input=event)
        # Real monotonic time, so nothing a caller passes can stretch the deadline.
        deadline = time.monotonic() + LEDGER_TIMEOUT_SECONDS
        sent: List[LedgerSendResult] = []
        threads: List[threading.Thread] = []
        try:
            # Two requests that never share a body, so a bad saved event can't cost the new one.
            if new_event is not None:
                threads.append(
                    start_ledger_thread(
                        work=lambda: sent.append(
                            send_ledger_events(config=config, events=[new_event], ledger_http=ledger_http)
                        )
                    )
                )
            threads.append(start_ledger_thread(work=lambda: drain_ledger_spool(config=config, ledger_http=ledger_http)))
        except Exception as error:
            ledger_warn(message=f"Mezmo ledger could not start: {str(error) or type(error).__name__}")
        # Done while the requests run, so the clean-up adds nothing to the wait.
        if event.get("hook_event_name") == "PostToolUse":
            _remove_old_start_times()
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        if new_event is not None:
            result = sent[0] if sent else LedgerSendResult(answered=False, save=[new_event])
            save_ledger_events(config=config, events=result.save)
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
