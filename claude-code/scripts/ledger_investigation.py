#!/usr/bin/env python3
"""Join, close or launch a Mezmo investigation: tool calls on one incident share one thread_id.

- `id --incident-key K` prints {incident_key, thread_id} as one line of JSON.
- `join --incident-key K` and `close --incident-key K` print {action, incident_key, thread_id} as
  one line of JSON and do nothing else. The ledger hook sees that line in the Bash call's output
  and records the join or close itself.
- `launch --incident-key K -- <command...>` sends `agent.investigation.opened`, sets
  MEZMO_LEDGER_INCIDENT_KEY and MEZMO_LEDGER_INVESTIGATION_ID, and execs the command, so its exit
  code is the command's. An `opened` it can't send is saved to the retry file and the command
  still runs. A command that can't be found exits 127, and one that can't be run 126, with
  nothing sent.
- An incident key is `<source>:<id>`, source one of INCIDENT_SOURCES. Surrounding whitespace is
  trimmed and nothing else is changed, because the ledger service hashes the key as given.
- The same file, byte for byte, ships with the Claude Code and the Codex plugin, and test files in
  both check it. Only `launch` imports the ledger module beside it, for the settings check, the
  envelope and the send: `ledger` in Claude Code, `mezmo_ledger_hook` in Codex.
"""

from __future__ import annotations

import argparse
import errno
import importlib
import json
import os
import shutil
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

LEDGER_ACCOUNT_ENV = "MEZMO_LEDGER_ACCOUNT"
LEDGER_INCIDENT_KEY_ENV = "MEZMO_LEDGER_INCIDENT_KEY"
LEDGER_INVESTIGATION_ID_ENV = "MEZMO_LEDGER_INVESTIGATION_ID"
INCIDENT_SOURCES = ("jira", "pagerduty", "slack")
OPENED_EVENT = "agent.investigation.opened"
# The exit code of a command that can't be found, as a shell gives it.
COMMAND_NOT_FOUND = 127
# The exit code of a command that was found but can't be run, as a shell gives it.
COMMAND_NOT_RUN = 126
USAGE_ERROR = 2


def warn(message: str) -> None:
    print(f"ledger_investigation: {message}", file=sys.stderr)


def parse_incident_key(value: str) -> Optional[str]:
    """`value` with surrounding whitespace trimmed, or None when it isn't `<source>:<id>`."""
    key = value.strip()
    source, colon, identifier = key.partition(":")
    if not colon or source not in INCIDENT_SOURCES or not identifier:
        return None
    if any(character.isspace() for character in identifier):
        return None
    return key


def investigation_id(*, account: str, incident_key: str) -> str:
    """The investigation's thread_id. ledger_context() and the ledger service derive the same one."""
    return str(uuid.uuid5(uuid.NAMESPACE_URL, f"mezmo-investigation:{account}:{incident_key}"))


def print_line(value: Dict[str, Any]) -> None:
    """Print `value` as one line of JSON, the form the ledger hook reads back."""
    print(json.dumps(value, separators=(",", ":"), sort_keys=True))


def _parser() -> argparse.ArgumentParser:
    # No abbreviations, such as --incident, which the ledger hook would not read.
    parser = argparse.ArgumentParser(
        allow_abbrev=False, description="Join, close or launch a Mezmo investigation.", prog="ledger_investigation.py"
    )
    commands = parser.add_subparsers(dest="action", metavar="{id,join,close,launch}")
    commands.required = True
    for name, text in (
        ("id", "print the incident key and its thread_id"),
        ("join", "print the line that joins this session to the investigation"),
        ("close", "print the line that closes the investigation"),
        ("launch", "send `opened`, then run the command after `--` inside the investigation"),
    ):
        command = commands.add_parser(name, allow_abbrev=False, help=text)
        command.add_argument(
            "--incident-key", required=True, help="<source>:<id>, source one of " + ", ".join(INCIDENT_SOURCES)
        )
    return parser


def _ledger_module() -> Any:
    """The ledger module beside this file: `ledger` in Claude Code, else `mezmo_ledger_hook`."""
    here = Path(__file__).resolve().parent
    # Python puts a script's folder first on sys.path, except with PYTHONSAFEPATH set.
    if str(here) not in sys.path:
        sys.path.insert(0, str(here))
    return importlib.import_module("ledger" if (here / "ledger.py").is_file() else "mezmo_ledger_hook")


def _send_opened(*, config: Any, incident_key: str, ledger: Any, thread_id: str) -> None:
    """Send `opened` on a daemon thread, wait up to the ledger deadline, and save what wasn't sent.

    Never raises: a send that fails must not stop the command.
    """
    event: Optional[Dict[str, Any]] = None
    try:
        event = ledger.ledger_envelope(
            account=config.account,
            correlation_id=thread_id,
            entity_id=thread_id,
            entity_type="investigation",
            event=OPENED_EVENT,
            extra={"agent": ledger.AGENT, "incident_key": incident_key, "opened_by": "alert", "thread_id": thread_id},
            message=f"{incident_key}: opened by an alert",
            session_id="launcher",
        )
        sent: List[Any] = []

        def send() -> None:
            # send_ledger_events never raises. post_ledger_events is looked up here, as the hooks do.
            sent.append(ledger.send_ledger_events(config=config, events=[event], ledger_http=ledger.post_ledger_events))

        thread = threading.Thread(target=send, name="mezmo-ledger", daemon=True)
        thread.start()
        thread.join(ledger.LEDGER_TIMEOUT_SECONDS)
        # A thread still running at the deadline is abandoned, and the duplicate it may still
        # deliver is accepted.
        save = sent[0].save if sent else [event]
        if save:
            warn(f"Mezmo ledger did not deliver {OPENED_EVENT}; saving it for retry")
            ledger.save_ledger_events(config=config, events=save)
        ledger.print_ledger_messages()
    except Exception as error:
        warn(f"Mezmo ledger could not send {OPENED_EVENT}: {str(error) or type(error).__name__}")
        try:
            if event is not None:
                ledger.save_ledger_events(config=config, events=[event])
            ledger.print_ledger_messages()
        except Exception:
            pass


def launch(*, command: Sequence[str], incident_key: str) -> int:
    """Send `opened`, then exec `command` with the investigation in its environment.

    Returns only when nothing ran: on a usage or settings error, or a command that can't be run.
    """
    try:
        ledger = _ledger_module()
        config = ledger.load_ledger_config()
    except Exception as error:
        warn(f"could not load the Mezmo ledger beside this file: {str(error) or type(error).__name__}")
        return USAGE_ERROR
    if config is None:
        warn(f"launch needs {LEDGER_ACCOUNT_ENV} and MEZMO_LEDGER_URL set to safe values; nothing was run")
        return USAGE_ERROR
    # Looked up first, so a command that isn't there, or is there but can't be run, gets no `opened`.
    if shutil.which(command[0], mode=os.F_OK) is None:
        warn(f"command not found: {command[0]}")
        return COMMAND_NOT_FOUND
    if shutil.which(command[0]) is None:
        warn(f"could not run {command[0]}: {os.strerror(errno.EACCES)}")
        return COMMAND_NOT_RUN
    thread_id = investigation_id(account=config.account, incident_key=incident_key)
    _send_opened(config=config, incident_key=incident_key, ledger=ledger, thread_id=thread_id)
    environment = dict(os.environ)
    environment[LEDGER_INCIDENT_KEY_ENV] = incident_key
    environment[LEDGER_INVESTIGATION_ID_ENV] = thread_id
    # exec replaces this process without flushing its buffers.
    sys.stdout.flush()
    sys.stderr.flush()
    try:
        os.execvpe(command[0], list(command), environment)
    except FileNotFoundError:
        warn(f"command not found: {command[0]}")
        return COMMAND_NOT_FOUND
    except OSError as error:
        warn(f"could not run {command[0]}: {error.strerror or error}")
        return COMMAND_NOT_RUN


def _split_command(argv: Sequence[str]) -> Tuple[List[str], Optional[List[str]]]:
    """The arguments before the first `--`, and the command after it, or None with no `--`."""
    arguments = list(argv)
    if "--" not in arguments:
        return arguments, None
    index = arguments.index("--")
    return arguments[:index], arguments[index + 1:]


def main(argv: Optional[Sequence[str]] = None) -> int:
    arguments, command = _split_command(sys.argv[1:] if argv is None else argv)
    parsed = _parser().parse_args(arguments)
    key = parse_incident_key(parsed.incident_key)
    if key is None:
        warn(
            f"incident key must be <source>:<id>, source one of {', '.join(INCIDENT_SOURCES)}, "
            f"and an id with no whitespace: {parsed.incident_key!r}"
        )
        return USAGE_ERROR
    if parsed.action == "launch":
        if not command:
            warn("launch needs a command after `--`; nothing was run")
            return USAGE_ERROR
        return launch(command=command, incident_key=key)
    if command is not None:
        warn(f"{parsed.action} takes no command")
        return USAGE_ERROR
    account = os.environ.get(LEDGER_ACCOUNT_ENV, "").strip()
    if not account:
        warn(f"{LEDGER_ACCOUNT_ENV} is not set")
        return USAGE_ERROR
    thread_id = investigation_id(account=account, incident_key=key)
    if parsed.action == "id":
        print_line({"incident_key": key, "thread_id": thread_id})
        return 0
    action = "joined" if parsed.action == "join" else "closed"
    print_line({"action": action, "incident_key": key, "thread_id": thread_id})
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
