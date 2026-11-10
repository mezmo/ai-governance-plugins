"""The Mezmo ledger: tool-call events for Mezmo audit-service, shared by two hooks.

- ledger_hook.py, registered beside governance_hook.py, sends `started` for each Bash or MCP
  call and, when Claude Code reports back, `ran`, `failed` or auto mode's `not_run`.
- governance_hook.py calls `record_governance_refusal` when governance denies a call, its
  approval expires or it errors, since no hook runs after a deny. That is its only use of this
  module.
- Off until MEZMO_LEDGER_ACCOUNT and MEZMO_LEDGER_URL are both set. It doesn't need the governance
  key, and nothing here may change a decision, a deny message, the exit code or the local audit
  log. What isn't sent is saved to ~/.mezmo/ledger-spool.jsonl, and a later ledger_hook.py run
  resends it.
"""

from __future__ import annotations

import errno
import json
import math
import os
import re
import stat
import sys
import threading
import uuid
from contextlib import contextmanager
from datetime import datetime, timezone
from http.client import HTTPException
from pathlib import Path
from typing import Any, Callable, Dict, Iterator, List, Mapping, NamedTuple, Optional, Sequence, Tuple, Union
from urllib.error import HTTPError
from urllib.request import Request, build_opener

from governance_hook import (
    AGENT,
    KEY_ENVS,
    KEY_OPTION,
    MAX_DETAIL_CHARACTERS,
    HookContractError,
    RefuseRedirect,
    _locked,
    _setting,
    authorization_header,
    call_identity,
    load_config as load_governance_config,
    normalize_tool_call,
    refuse_exposed_token,
    result_has_known_error,
    session_state_path,
)

LEDGER_ACCOUNT_ENV = "MEZMO_LEDGER_ACCOUNT"
LEDGER_INCIDENT_KEY_ENV = "MEZMO_LEDGER_INCIDENT_KEY"
LEDGER_INVESTIGATION_ID_ENV = "MEZMO_LEDGER_INVESTIGATION_ID"
LEDGER_KEY_ENV = "MEZMO_LEDGER_KEY"
LEDGER_SPOOL_ENV = "MEZMO_LEDGER_SPOOL"
LEDGER_URL_ENV = "MEZMO_LEDGER_URL"
DEFAULT_LEDGER_SPOOL = Path.home() / ".mezmo" / "ledger-spool.jsonl"
LEDGER_APPLICATION = "mezmo-claude-code-plugin"
# The plugin's manifest, for application.version. This file is in <plugin>/scripts.
PLUGIN_MANIFEST = Path(__file__).resolve().parent.parent / ".claude-plugin" / "plugin.json"
# The socket timeout of each ledger_hook.py request, and the deadline its main thread waits for
# its ledger threads from when they start. A socket timeout alone bounds neither DNS nor the whole
# request, hence the threads. Read at call time, so a test can patch it.
LEDGER_TIMEOUT_SECONDS = 3.0
# Socket timeout for the request that reports a governance refusal.
# Kept short because it runs inside the governance hook.
REFUSED_TIMEOUT_SECONDS = 2.0
# Saved events resent in one request, small enough to finish well inside the deadline.
LEDGER_DRAIN_BATCH = 10
# Lines the retry file keeps. Past it the oldest go, so a ledger that stays down or a key that
# stays wrong can't grow the file, which every ledger_hook.py run reads in full.
LEDGER_SPOOL_MAX_LINES = 200
MAX_LEDGER_JSON_CHARACTERS = 4096
MAX_LEDGER_MESSAGE_CHARACTERS = 200
EXIT_CODE_LINE = re.compile(r"Exit code (-?\d+)")
LEDGER_ENDINGS = {"failed": "failed", "not_run": "not run", "ran": "ok"}

# Ledger threads never print. They leave messages here, which the main thread prints after its
# wait.
LEDGER_MESSAGES: List[str] = []
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
        governance = load_governance_config()
        if governance is not None:
            key = governance.access_key
            key_setting = next((name for name in KEY_ENVS if _setting(name) is not None), KEY_OPTION)
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


def plugin_version() -> str:
    try:
        version = json.loads(PLUGIN_MANIFEST.read_text(encoding="utf-8")).get("version")
    except Exception:
        return "unknown"
    version = version.strip() if isinstance(version, str) else ""
    return version or "unknown"


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
    if load_governance_config() is not None:
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


def record_governance_refusal(
    *, hook_input: Mapping[str, Any], ledger_http: Optional[LedgerHttp] = None, outcome: str, reason: Optional[str]
) -> None:
    """Send governance's refusal of this call as one `not_run` event, in line. Never raises.

    Governance calls this on its main thread, right after it records a denial, an expiry or an
    error. One request with a short socket timeout, no thread and no drain: what isn't sent waits
    in the retry file for the next ledger_hook.py run.
    """
    try:
        config = load_ledger_config()
        if config is None:
            return
        event = ledger_event(config=config, hook_input=hook_input, kind="not_run", outcome=outcome, reason=reason)
        if event is None:
            return
        # Looked up per call, not bound at definition, so tests can stand in for them.
        result = send_ledger_events(
            config=config,
            events=[event],
            ledger_http=ledger_http or post_ledger_events,
            timeout=REFUSED_TIMEOUT_SECONDS,
        )
        save_ledger_events(config=config, events=result.save)
        print_ledger_messages()
    except Exception as error:
        LEDGER_MESSAGES.append(f"Mezmo ledger could not record the refusal: {str(error) or type(error).__name__}")
        print_ledger_messages()
