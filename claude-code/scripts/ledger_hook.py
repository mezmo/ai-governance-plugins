#!/usr/bin/env python3
"""Claude Code hook that records each tool call in the Mezmo ledger.

hooks/hooks.json registers it for every tool, in a group of its own beside governance_hook.py's
Bash and MCP group, and Claude Code runs the two in parallel:

- PreToolUse sends `started`. PostToolUse and PostToolUseFailure send `ran` or `failed`.
  PermissionDenied, auto mode's block, sends `not_run`. A call governance refuses gets its
  `not_run` from governance_hook.py (ledger.record_governance_refusal), because no hook runs
  after a deny.
- Off while ledger.load_ledger_config finds no key, or the ledger_enabled option is off: then it
  exits at once.
- A Bash call that ran ledger_investigation.py's join or close puts the session in that
  investigation, or takes it out, in the session state, and sends `joined` or `closed`.
- A Mezmo Memory brief or Insights read that ran adds its references to the session state, so
  the next call's `started` carries what the agent knew when it acted.
- Each run, and a Stop hook at the end of each turn, reads the transcript from where the last run
  stopped and sends what Claude wrote as `agent.investigation.step`, at most MAX_STEPS_PER_HOOK a
  run, each under the investigation the session was in when Claude wrote it. Text written outside
  every investigation isn't sent. With the ledger off, it only moves past what is there.
- It never prints hook output and always exits 0, so it can never block or change a tool call.
- Each run sends its events and resends saved ones on their own threads, waits for all of them up
  to one short deadline, and saves what it couldn't send to ~/.mezmo/ledger-spool.jsonl for a
  later run.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from datetime import datetime, timezone
from functools import partial
from typing import Any, Callable, Dict, List, Mapping, Optional, Tuple

import ledger
from governance_hook import CLAUDE_APP_SERVERS, MCP_TOOL_PREFIX, session_state_path, update_session_state

LEDGER_EVENTS = frozenset({"PermissionDenied", "PostToolUse", "PostToolUseFailure", "PreToolUse"})
# The longest a join, a close or a reference write waits for the session state's lock. Past it the
# change is not made.
INVESTIGATION_STATE_TIMEOUT_SECONDS = 1.0

# The longest the last send's record waits for the session state's lock. Past it, it isn't kept.
LAST_SEND_STATE_TIMEOUT_SECONDS = 0.25

# Every ledger thread this process started, so main() knows whether one is still running.
LEDGER_THREADS: List[threading.Thread] = []


def start_ledger_thread(*, work: Callable[[], None]) -> threading.Thread:
    """Run `work` on a daemon thread that can't raise, and remember the thread."""

    def run() -> None:
        try:
            work()
        except Exception as error:
            ledger.LEDGER_MESSAGES.append(f"Mezmo ledger failed: {str(error) or type(error).__name__}")

    thread = threading.Thread(target=run, name="mezmo-ledger", daemon=True)
    LEDGER_THREADS.append(thread)
    thread.start()
    return thread


def _recorded(event: Mapping[str, Any]) -> bool:
    """Whether the ledger records this call: every tool but the Claude desktop app's own servers,
    whose session, pane and task-list calls aren't work on anything."""
    name = event.get("tool_name")
    if not isinstance(name, str) or not name:
        return False
    if not name.startswith(MCP_TOOL_PREFIX):
        return True
    parts = name.split("__", 2)
    return len(parts) != 3 or parts[1] not in CLAUDE_APP_SERVERS


def _new_event(*, config: ledger.LedgerConfig, hook_input: Mapping[str, Any]) -> Optional[Dict[str, Any]]:
    """The event this hook input gives, or None when there's nothing new to send. Never raises."""
    name = hook_input.get("hook_event_name")
    fields: Dict[str, Any]
    if name == "PreToolUse":
        fields = {"kind": "started"}
    elif name == "PermissionDenied":
        reason = hook_input.get("reason")
        fields = {
            "kind": "not_run",
            "outcome": "blocked_by_auto_mode",
            "reason": reason if isinstance(reason, str) else None,
        }
    else:
        try:
            kind, exit_status = ledger.ledger_result(hook_input=hook_input)
        except Exception:
            kind, exit_status = "failed", 1
        fields = {"exit_status": exit_status, "kind": kind}
    try:
        return ledger.ledger_event(config=config, hook_input=hook_input, **fields)
    except Exception as error:
        ledger.ledger_warn(message=f"Mezmo ledger could not build an event: {str(error) or type(error).__name__}")
        return None


def _state_incident_key(*, hook_input: Mapping[str, Any]) -> Any:
    """The incident key of the investigation in this session's state, or None. Never raises."""
    try:
        state = json.loads(session_state_path(hook_input["session_id"].strip()).read_text(encoding="utf-8"))
        return state["investigation"]["incident_key"]
    except Exception:
        return None


def _lock_timeout(*, deadline: float) -> float:
    """How long a state change may wait for the session state's lock: never past `deadline`."""
    return max(0.0, min(INVESTIGATION_STATE_TIMEOUT_SECONDS, deadline - time.monotonic()))


def _join(
    *,
    config: ledger.LedgerConfig,
    deadline: float,
    hook_input: Mapping[str, Any],
    incident_key: str,
    service: Optional[str],
) -> Optional[Dict[str, Any]]:
    """Put the session in the investigation, then build `joined`. None when the state didn't take it.

    Never raises.
    """
    try:
        joined_at = datetime.now(timezone.utc).isoformat()
        investigation = {"incident_key": incident_key, "joined_at": joined_at}
        if service is not None:
            investigation["service"] = service
        path = hook_input.get("transcript_path")

        def join(state: Dict[str, Any]) -> None:
            if not ledger.investigation_windows(state) and isinstance(path, str) and path:
                # Nothing before the join is filed under it, so there's no need to read it.
                try:
                    state[ledger.TRANSCRIPT_OFFSET_FIELD] = os.path.getsize(path)
                except OSError:
                    pass
            # Joining another investigation ends the one the session was in.
            ledger.close_investigation(state, closed_at=joined_at)
            state["investigation"] = investigation

        update_session_state(hook_input, join, lock_timeout=_lock_timeout(deadline=deadline))
        if _state_incident_key(hook_input=hook_input) != incident_key:
            ledger.ledger_warn(message=f"Mezmo ledger could not join {incident_key}: the session state was not written")
            return None
        return ledger.investigation_event(
            action="joined",
            config=config,
            hook_input=hook_input,
            incident_key=incident_key,
            service=service,
        )
    except Exception as error:
        ledger.ledger_warn(message=f"Mezmo ledger could not join {incident_key}: {str(error) or type(error).__name__}")
        return None


def _close(
    *,
    closing: Optional[Mapping[str, str]],
    config: ledger.LedgerConfig,
    deadline: float,
    hook_input: Mapping[str, Any],
    incident_key: str,
) -> Optional[Dict[str, Any]]:
    """Build `closed`, then take the session out of the investigation when it is in this one.

    `closed` is sent either way. Never raises.
    """
    closed: Optional[Dict[str, Any]] = None
    try:
        closed = ledger.investigation_event(
            action="closed", closing=closing, config=config, hook_input=hook_input, incident_key=incident_key
        )

        def leave(state: Dict[str, Any]) -> None:
            investigation = state.get("investigation")
            if isinstance(investigation, dict) and investigation.get("incident_key") == incident_key:
                ledger.close_investigation(state, closed_at=datetime.now(timezone.utc).isoformat())

        # No write for another investigation, so a close can't create a state file.
        if _state_incident_key(hook_input=hook_input) == incident_key:
            update_session_state(hook_input, leave, lock_timeout=_lock_timeout(deadline=deadline))
            if _state_incident_key(hook_input=hook_input) == incident_key:
                ledger.ledger_warn(message=f"Mezmo ledger could not close {incident_key} in the session state")
    except Exception as error:
        ledger.ledger_warn(message=f"Mezmo ledger could not close {incident_key}: {str(error) or type(error).__name__}")
    return closed


def _steps(*, deadline: float, hook_input: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """The step events for what Claude wrote since the last read, at most MAX_STEPS_PER_HOOK. Never raises.

    Each goes to the investigation whose join and close bracket the time Claude wrote it. The
    offset is saved without the lock having to be won: two runs reading the same text send it with
    the same step_id, and readers keep one.
    """
    try:
        path = hook_input.get("transcript_path")
        if not isinstance(path, str) or not path:
            return []
        state = ledger._session_state(hook_input=hook_input)
        windows = ledger.investigation_windows(state)
        if not windows:
            return []
        offset = state.get(ledger.TRANSCRIPT_OFFSET_FIELD)
        offset = offset if isinstance(offset, int) and not isinstance(offset, bool) and offset >= 0 else 0
        read_to = offset
        events: List[Dict[str, Any]] = []
        last_read: Optional[datetime] = None
        for record, read_to in ledger.transcript_records(path=path, offset=offset):
            if record is None:
                continue
            last_read = ledger._parse_time(record.get("timestamp")) or last_read
            step = ledger.transcript_step(record)
            if step is not None:
                text, step_id, written_at = step
                for incident_key, joined_at, closed_at in reversed(windows):
                    if joined_at <= written_at and (closed_at is None or written_at <= closed_at):
                        event = ledger.step_event(
                            hook_input=hook_input,
                            incident_key=incident_key,
                            step_id=step_id,
                            text=text,
                            written_at=written_at,
                        )
                        if event is not None:
                            events.append(event)
                        break
            if len(events) >= ledger.MAX_STEPS_PER_HOOK:
                # The rest wait for the next hook, unread, so a backlog can't crowd out this call's record.
                break

        def advance(state: Dict[str, Any]) -> None:
            state[ledger.TRANSCRIPT_OFFSET_FIELD] = read_to
            closed = state.get(ledger.CLOSED_INVESTIGATIONS_FIELD)
            if last_read is None or not isinstance(closed, list):
                return
            # A closed investigation the read has passed can't get another step.
            kept = [entry for entry in closed if isinstance(entry, dict) and (
                (ledger._parse_time(entry.get("closed_at")) or last_read) >= last_read
            )]
            if kept:
                state[ledger.CLOSED_INVESTIGATIONS_FIELD] = kept
            else:
                state.pop(ledger.CLOSED_INVESTIGATIONS_FIELD, None)

        update_session_state(hook_input, advance, lock_timeout=_lock_timeout(deadline=deadline))
        return events
    except Exception as error:
        ledger.ledger_warn(message=f"Mezmo ledger could not read the transcript: {str(error) or type(error).__name__}")
        return []


def _skip_transcript(*, hook_input: Mapping[str, Any]) -> None:
    """With the ledger off, move past what Claude wrote, so turning it back on doesn't send what
    was written while it was off. Only for a session that has steps to send, or a launched one that
    may join when the ledger comes on. Never raises."""
    try:
        path = hook_input.get("transcript_path")
        if not isinstance(path, str) or not path:
            return
        if not ledger.investigation_windows(ledger._session_state(hook_input=hook_input)) \
                and ledger._setting(ledger.LEDGER_INCIDENT_KEY_ENV) is None:
            return

        def change(state: Dict[str, Any]) -> None:
            state.pop(ledger.CLOSED_INVESTIGATIONS_FIELD, None)
            state[ledger.TRANSCRIPT_OFFSET_FIELD] = os.path.getsize(path)

        update_session_state(hook_input, change, lock_timeout=LAST_SEND_STATE_TIMEOUT_SECONDS)
    except Exception:
        pass


def _record_references(*, deadline: float, hook_input: Mapping[str, Any]) -> None:
    """Add the references this call gives to the session state. Never raises, and never warns.

    A call that gives none writes nothing, so it never creates or rewrites a state file. The merge
    runs on the state update_session_state read under its lock, which it waits for as a join does.
    """
    try:
        references = ledger.tool_references(hook_input=hook_input, read_at=datetime.now(timezone.utc).isoformat())
        if not references:
            return

        def add(state: Dict[str, Any]) -> None:
            state["references"] = ledger.merge_references(existing=state.get("references"), new=references)

        update_session_state(hook_input, add, lock_timeout=_lock_timeout(deadline=deadline))
    except Exception:
        pass


def _send(
    *,
    config: ledger.LedgerConfig,
    events: List[Dict[str, Any]],
    ledger_http: ledger.LedgerHttp,
    outcome: Dict[str, Any],
    sent: List[ledger.LedgerSendResult],
) -> None:
    """Send `events` in a request of their own, keep the result in `sent` and the HTTP status or error in `outcome`."""
    recording = ledger.recording_http(ledger_http=ledger_http, outcome=outcome)
    sent.append(ledger.send_ledger_events(config=config, events=events, ledger_http=recording))


def _record_last_send(
    *, config: ledger.LedgerConfig, hook_input: Mapping[str, Any], sends: List[Tuple[int, Dict[str, Any]]]
) -> None:
    """Keep this run's sends as the session's last send, for /mezmo:test and doctor. Never raises."""
    try:
        if not sends:
            return
        entry = ledger.last_send(config=config, sends=sends)
        update_session_state(
            hook_input,
            lambda state: state.__setitem__(ledger.LAST_SEND_FIELD, entry),
            lock_timeout=LAST_SEND_STATE_TIMEOUT_SECONDS,
        )
    except Exception:
        pass


def handle(event: Mapping[str, Any], *, ledger_http: Optional[ledger.LedgerHttp] = None) -> None:
    """Send this call's events while resending saved ones, and wait up to one deadline. Never raises.

    A join is written to the session state before the call's event is built, so the join's own
    `ran` carries the investigation, and a close is cleared after. A call that ran and read Mezmo
    Memory or Insights adds its references to the state once the requests are started, so its own
    `ran` doesn't carry them. Each waits for the state's lock at most
    INVESTIGATION_STATE_TIMEOUT_SECONDS and never past the deadline. The call's event and an
    investigation event each go in a request of their own, so one's refusal or retry never touches
    another, and the steps go together in one more. Only this thread saves the new events, each
    request's by its own result, so each is saved at most once and never by a thread os._exit
    could cut off. A thread still running at the deadline is abandoned, and the duplicate it may
    still deliver is accepted.
    """
    try:
        # Looked up per call, not bound at definition, so tests can stand in for it.
        ledger_http = ledger_http or ledger.post_ledger_events
        stop = event.get("hook_event_name") == "Stop"
        if not stop and (event.get("hook_event_name") not in LEDGER_EVENTS or not _recorded(event)):
            return
        if not stop and ledger._skip_ledger_mcp_tool(hook_input=event):
            return
        try:
            config = ledger.load_ledger_config()
        except Exception:
            return
        if config is None:
            _skip_transcript(hook_input=event)
            return
        # Real monotonic time, so nothing a caller passes can stretch the deadline. From before the
        # events are built and the state written, so a slow git lookup or lock can't push the save
        # past it.
        deadline = time.monotonic() + ledger.LEDGER_TIMEOUT_SECONDS
        ledger.adopt_launched_investigation(
            hook_input=event,
            update=lambda change: update_session_state(event, change, lock_timeout=_lock_timeout(deadline=deadline)),
        )
        investigation = ledger.investigation_action(hook_input=event)
        investigation_event: Optional[Dict[str, Any]] = None
        if investigation is not None and investigation[0] == "joined":
            investigation_event = _join(
                config=config,
                deadline=deadline,
                hook_input=event,
                incident_key=investigation[1],
                service=investigation[2],
            )
        new_event = None if stop else _new_event(config=config, hook_input=event)
        if investigation is not None and investigation[0] == "closed":
            investigation_event = _close(
                closing=investigation[3],
                config=config,
                deadline=deadline,
                hook_input=event,
                incident_key=investigation[1],
            )
        steps = _steps(deadline=deadline, hook_input=event)
        # The call's own record first, each in a request of its own; the steps together in one more.
        batches = [[new] for new in (new_event, investigation_event) if new is not None] + ([steps] if steps else [])
        sends: List[Tuple[List[Dict[str, Any]], List[ledger.LedgerSendResult], Dict[str, Any]]] = [
            (batch, [], {}) for batch in batches
        ]
        threads: List[threading.Thread] = []
        try:
            # Requests that never share a body, so a bad saved event can't cost a new one.
            for new, sent, outcome in sends:
                threads.append(
                    start_ledger_thread(
                        work=partial(
                            _send, config=config, events=new, ledger_http=ledger_http, outcome=outcome, sent=sent
                        )
                    )
                )
            threads.append(
                start_ledger_thread(work=lambda: ledger.drain_ledger_spool(config=config, ledger_http=ledger_http))
            )
        except Exception as error:
            ledger.ledger_warn(message=f"Mezmo ledger could not start: {str(error) or type(error).__name__}")
        # While the requests run, and after this call's own event was built, so its `ran` never
        # claims what it read and the next call's `started` does.
        if event.get("hook_event_name") == "PostToolUse" and new_event is not None \
                and new_event.get("event") == "agent.tool_call.ran":
            _record_references(deadline=deadline, hook_input=event)
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        for new, sent, _ in sends:
            result = sent[0] if sent else ledger.LedgerSendResult(answered=False, save=list(new))
            ledger.save_ledger_events(config=config, events=result.save)
        # A copy of each outcome, so a thread still running can't change it while it is read.
        _record_last_send(config=config, hook_input=event, sends=[(len(new), dict(outcome)) for new, _, outcome in sends])
        ledger.print_ledger_messages()
    except Exception as error:
        ledger.ledger_warn(message=f"Mezmo ledger failed: {str(error) or type(error).__name__}")


def main() -> int:
    try:
        event = json.load(sys.stdin)
        if isinstance(event, dict):
            handle(event)
        else:
            ledger.ledger_warn(message="Mezmo ledger skipped this call: hook input must be a JSON object")
    except Exception as error:
        # Such as a body that isn't JSON, or a RecursionError on deeply nested input.
        ledger.ledger_warn(message=f"Mezmo ledger skipped this call: {str(error) or type(error).__name__}")
    try:
        sys.stdout.flush()
        sys.stderr.flush()
    except Exception:
        pass
    # A ledger thread still waiting on the network would hold up the interpreter's exit.
    if any(thread.is_alive() for thread in LEDGER_THREADS):
        os._exit(0)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
