#!/usr/bin/env python3
"""Claude Code hook that records each Bash and MCP tool call in the Mezmo ledger.

hooks/hooks.json registers it beside governance_hook.py for the same calls, and Claude Code runs
the two in parallel:

- PreToolUse sends `started`. PostToolUse and PostToolUseFailure send `ran` or `failed`.
  PermissionDenied, auto mode's block, sends `not_run`. A call governance refuses gets its
  `not_run` from governance_hook.py (ledger.record_governance_refusal), because no hook runs
  after a deny.
- Off until MEZMO_LEDGER_ACCOUNT and MEZMO_LEDGER_URL are both set: until then it exits at once.
- A Bash call that ran ledger_investigation.py's join or close puts the session in that
  investigation, or takes it out, in the session state, and sends `joined` or `closed`.
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
from governance_hook import governed, session_state_path, update_session_state

LEDGER_EVENTS = frozenset({"PermissionDenied", "PostToolUse", "PostToolUseFailure", "PreToolUse"})
# The longest a join or close waits for the session state's lock. Past it the change counts as not made.
INVESTIGATION_STATE_TIMEOUT_SECONDS = 1.0

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


def _state_thread_id(*, hook_input: Mapping[str, Any]) -> Any:
    """The thread_id of the investigation in this session's state, or None. Never raises.

    Read from the state alone: ledger_context would fall back to the environment's investigation.
    """
    try:
        state = json.loads(session_state_path(hook_input["session_id"].strip()).read_text(encoding="utf-8"))
        return state["investigation"]["thread_id"]
    except Exception:
        return None


def _change_state(
    *, change: Callable[[Dict[str, Any]], None], deadline: float, hook_input: Mapping[str, Any]
) -> bool:
    """Apply `change` to the session state on a ledger thread. False when it has not finished in time.

    Waits at most INVESTIGATION_STATE_TIMEOUT_SECONDS and never past `deadline`, so a lock another
    process holds can't cost this call its own event. A change still waiting is abandoned and
    never written.
    """
    abandoned = threading.Event()

    def guarded(state: Dict[str, Any]) -> None:
        # The lock may come free after the wait gave up. The hook has then already said the change
        # was not made, so it raises, and update_session_state writes nothing.
        if abandoned.is_set():
            raise RuntimeError("the wait for the session state ended")
        change(state)

    thread = start_ledger_thread(work=lambda: update_session_state(hook_input, guarded))
    thread.join(max(0.0, min(INVESTIGATION_STATE_TIMEOUT_SECONDS, deadline - time.monotonic())))
    if thread.is_alive():
        abandoned.set()
        return False
    return True


def _join(
    *, config: ledger.LedgerConfig, deadline: float, hook_input: Mapping[str, Any], incident_key: str
) -> Optional[Dict[str, Any]]:
    """Put the session in the investigation, then build `joined`. None when the state didn't take it.

    Never raises.
    """
    try:
        thread_id = ledger.investigation_thread_id(account=config.account, incident_key=incident_key)
        investigation = {
            "incident_key": incident_key,
            "joined_at": datetime.now(timezone.utc).isoformat(),
            "thread_id": thread_id,
        }
        if (
            not _change_state(
                change=lambda state: state.__setitem__("investigation", investigation),
                deadline=deadline,
                hook_input=hook_input,
            )
            or _state_thread_id(hook_input=hook_input) != thread_id
        ):
            ledger.ledger_warn(message=f"Mezmo ledger could not join {incident_key}: the session state was not written")
            return None
        return ledger.investigation_event(
            action="joined", config=config, hook_input=hook_input, incident_key=incident_key, thread_id=thread_id
        )
    except Exception as error:
        ledger.ledger_warn(message=f"Mezmo ledger could not join {incident_key}: {str(error) or type(error).__name__}")
        return None


def _close(
    *, config: ledger.LedgerConfig, deadline: float, hook_input: Mapping[str, Any], incident_key: str
) -> Optional[Dict[str, Any]]:
    """Build `closed`, then take the session out of the investigation when it is in this one.

    `closed` is sent either way. Never raises.
    """
    closed: Optional[Dict[str, Any]] = None
    try:
        thread_id = ledger.investigation_thread_id(account=config.account, incident_key=incident_key)
        closed = ledger.investigation_event(
            action="closed", config=config, hook_input=hook_input, incident_key=incident_key, thread_id=thread_id
        )

        def leave(state: Dict[str, Any]) -> None:
            investigation = state.get("investigation")
            if isinstance(investigation, dict) and investigation.get("thread_id") == thread_id:
                del state["investigation"]

        # No write for another investigation, so a close can't create a state file.
        if _state_thread_id(hook_input=hook_input) == thread_id:
            if (
                not _change_state(change=leave, deadline=deadline, hook_input=hook_input)
                or _state_thread_id(hook_input=hook_input) == thread_id
            ):
                ledger.ledger_warn(message=f"Mezmo ledger could not close {incident_key} in the session state")
    except Exception as error:
        ledger.ledger_warn(message=f"Mezmo ledger could not close {incident_key}: {str(error) or type(error).__name__}")
    return closed


def _send(
    *,
    config: ledger.LedgerConfig,
    event: Mapping[str, Any],
    ledger_http: ledger.LedgerHttp,
    sent: List[ledger.LedgerSendResult],
) -> None:
    """Send `event` in a request of its own and keep the result in `sent`."""
    sent.append(ledger.send_ledger_events(config=config, events=[event], ledger_http=ledger_http))


def handle(event: Mapping[str, Any], *, ledger_http: Optional[ledger.LedgerHttp] = None) -> None:
    """Send this call's events while resending saved ones, and wait up to one deadline. Never raises.

    A join is written to the session state before the call's event is built, so the join's own
    `ran` carries the investigation, and a close is cleared after. Each new event goes in a request
    of its own, so one event's refusal or retry never touches another. Only this thread saves the
    new events, each by its own result, so each is saved at most once and never by a thread os._exit
    could cut off. A thread still running at the deadline is abandoned, and the duplicate it may
    still deliver is accepted.
    """
    try:
        # Looked up per call, not bound at definition, so tests can stand in for it.
        ledger_http = ledger_http or ledger.post_ledger_events
        if event.get("hook_event_name") not in LEDGER_EVENTS or not governed(event):
            return
        if ledger._skip_ledger_mcp_tool(hook_input=event):
            return
        try:
            config = ledger.load_ledger_config()
        except Exception:
            return
        if config is None:
            return
        # Real monotonic time, so nothing a caller passes can stretch the deadline. From before the
        # events are built and the state written, so a slow git lookup or lock can't push the save
        # past it.
        deadline = time.monotonic() + ledger.LEDGER_TIMEOUT_SECONDS
        investigation = ledger.investigation_action(hook_input=event)
        investigation_event: Optional[Dict[str, Any]] = None
        if investigation is not None and investigation[0] == "joined":
            investigation_event = _join(
                config=config, deadline=deadline, hook_input=event, incident_key=investigation[1]
            )
        new_event = _new_event(config=config, hook_input=event)
        if investigation is not None and investigation[0] == "closed":
            investigation_event = _close(
                config=config, deadline=deadline, hook_input=event, incident_key=investigation[1]
            )
        sends: List[Tuple[Dict[str, Any], List[ledger.LedgerSendResult]]] = [
            (new, []) for new in (new_event, investigation_event) if new is not None
        ]
        threads: List[threading.Thread] = []
        try:
            # Requests that never share a body, so a bad saved event can't cost a new one.
            for new, sent in sends:
                threads.append(
                    start_ledger_thread(
                        work=partial(_send, config=config, event=new, ledger_http=ledger_http, sent=sent)
                    )
                )
            threads.append(
                start_ledger_thread(work=lambda: ledger.drain_ledger_spool(config=config, ledger_http=ledger_http))
            )
        except Exception as error:
            ledger.ledger_warn(message=f"Mezmo ledger could not start: {str(error) or type(error).__name__}")
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        for new, sent in sends:
            result = sent[0] if sent else ledger.LedgerSendResult(answered=False, save=[new])
            ledger.save_ledger_events(config=config, events=result.save)
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
