#!/usr/bin/env python3
"""Claude Code hook that records each Bash and MCP tool call in the Mezmo ledger.

hooks/hooks.json registers it beside governance_hook.py for the same calls, and Claude Code runs
the two in parallel:

- PreToolUse sends `started`. PostToolUse and PostToolUseFailure send `ran` or `failed`.
  PermissionDenied, auto mode's block, sends `not_run`. A call governance refuses gets its
  `not_run` from governance_hook.py (ledger.record_governance_refusal), because no hook runs
  after a deny.
- Off until MEZMO_LEDGER_ACCOUNT and MEZMO_LEDGER_URL are both set: until then it exits at once.
- It never prints hook output and always exits 0, so it can never block or change a tool call.
- Each run sends its event and resends saved ones on two threads, waits for both up to one short
  deadline, and saves what it couldn't send to ~/.mezmo/ledger-spool.jsonl for a later run.
"""

from __future__ import annotations

import json
import os
import sys
import threading
import time
from typing import Any, Callable, Dict, List, Mapping, Optional

import ledger
from governance_hook import governed

LEDGER_EVENTS = frozenset({"PermissionDenied", "PostToolUse", "PostToolUseFailure", "PreToolUse"})

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


def handle(event: Mapping[str, Any], *, ledger_http: Optional[ledger.LedgerHttp] = None) -> None:
    """Send this call's event while resending saved ones, and wait up to one deadline. Never raises.

    Only this thread saves the new event, so it is saved at most once and never by a thread
    os._exit could cut off. A thread still running at the deadline is abandoned, and the duplicate
    it may still deliver is accepted.
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
        new_event = _new_event(config=config, hook_input=event)
        # Real monotonic time, so nothing a caller passes can stretch the deadline.
        deadline = time.monotonic() + ledger.LEDGER_TIMEOUT_SECONDS
        sent: List[ledger.LedgerSendResult] = []
        threads: List[threading.Thread] = []
        try:
            # Two requests that never share a body, so a bad saved event can't cost the new one.
            if new_event is not None:
                threads.append(
                    start_ledger_thread(
                        work=lambda: sent.append(
                            ledger.send_ledger_events(config=config, events=[new_event], ledger_http=ledger_http)
                        )
                    )
                )
            threads.append(
                start_ledger_thread(work=lambda: ledger.drain_ledger_spool(config=config, ledger_http=ledger_http))
            )
        except Exception as error:
            ledger.ledger_warn(message=f"Mezmo ledger could not start: {str(error) or type(error).__name__}")
        for thread in threads:
            thread.join(max(0.0, deadline - time.monotonic()))
        if new_event is not None:
            result = sent[0] if sent else ledger.LedgerSendResult(answered=False, save=[new_event])
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
