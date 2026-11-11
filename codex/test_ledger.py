"""Tests for the Codex ledger hook, mezmo_ledger_hook.py, and for the refusals
mezmo_governance_hook.py reports through it.

Every test runs with a cleared environment and keeps session state, the audit log and the retry
file in a temp folder, so none reaches a real host or ~/.mezmo. The hook inputs are built by hand
from the shapes in the Codex 0.167.0 source: a Bash result is the output as a plain string, an
apply_patch result a string starting "Exit code: 0", an MCP result an object.
"""

from __future__ import annotations

import ast
import contextlib
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from collections.abc import Mapping
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import mezmo_governance_hook as hook
import mezmo_ledger_hook as ledger
from test_hook import top_level_definitions

ADAPTER_DIR = Path(__file__).resolve().parent
HOOK_PATH = ADAPTER_DIR / "mezmo_governance_hook.py"
LEDGER_PATH = ADAPTER_DIR / "mezmo_ledger_hook.py"
CLAUDE_CODE_SCRIPTS = ADAPTER_DIR.parent / "claude-code" / "scripts"

ACCOUNT = "test-acct"
# Never reached: the in-process tests pass a fake, and the guard in LedgerTestCase.setUp stops a
# real request to it. `.invalid` never resolves, so this names no real host or port.
LEDGER_URL = "https://audit.ledger.invalid/v1/events"
REAL_POST_LEDGER = ledger.post_ledger_events
# LedgerTestCase stands in for it, so no test picks up the developer's own git email.
REAL_OPERATOR_EMAIL = ledger.operator_email
OPERATOR_EMAIL = "operator@example.test"
# Bound from the real home when the module loaded. Only compared, never opened.
HOME_AT_IMPORT = Path.home()
REAL_DEFAULT_LEDGER_SPOOL = ledger.DEFAULT_LEDGER_SPOOL
REAL_DEFAULT_STATE_DIR = ledger.DEFAULT_STATE_DIR
AUTHORIZE_URL = "https://api.example.test/governance/workflow/authorize"
GOVERNANCE = {
    "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "1",
    "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "30",
    "MEZMO_GOVERNANCE_TOKEN": "sts_test",
    "MEZMO_GOVERNANCE_URL": AUTHORIZE_URL,
}
DENY_REASON = "mock policy: Bash is denied"
DENY_STDOUT = (
    json.dumps(hook.deny_output(f"Mezmo Governance denied this exact tool call: {DENY_REASON}"), separators=(",", ":"))
    + "\n"
).encode()
# The epic's check value: account test-acct and this incident key.
INCIDENT_KEY = "pagerduty:Q34FCCWSTU9OI2"
CHECK_THREAD_ID = "ab2763da-3eea-5b82-a1bf-7922641cdf6c"
DROP = object()
PATCH = "*** Begin Patch\n*** Add File: notes.txt\n+hello\n*** End Patch"
PATCH_RESPONSE = "Exit code: 0\nWall time: 0.1 seconds\nOutput:\nSuccess. Updated the following files:\nA notes.txt\n"
MCP_RESPONSE = {"content": [{"text": "pod deleted", "type": "text"}], "isError": False, "structuredContent": None}

# What audit-service's schema/audit-event.js enforces.
REQUIRED_FIELDS = {"actor", "application", "entity_id", "entity_type", "event", "internal", "message", "timestamp"}
TOP_LEVEL_FIELDS = REQUIRED_FIELDS | {"correlation_id", "extra"}
EVENT_NAME = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*(?:\.[a-z0-9]+(?:[_-][a-z0-9]+)*){2}$")
# Every key the epic allows in `extra`.
EXTRA_KEYS = {
    "access", "agent", "arguments", "call_id", "decision_id", "duration_ms", "entry_key", "exit_status",
    "incident_key", "intent_source", "operator_email", "output", "references", "request_id", "session_id", "target",
    "thread_id", "tool_name", "tool_namespace",
}


def hook_input(name: str = "PreToolUse", **fields: Any) -> dict[str, Any]:
    """A Bash call's hook input, as Codex sends it. A field set to DROP is left out."""
    event: dict[str, Any] = {
        "cwd": "/tmp/demo",
        "hook_event_name": name,
        "permission_mode": "default",
        "session_id": "session-1",
        "tool_input": {"command": "ls -la"},
        "tool_name": "Bash",
        "tool_use_id": "tool-use-1",
        "turn_id": "turn-1",
    }
    if name == "PostToolUse":
        event["tool_response"] = "a\nb\n"
    for key, value in fields.items():
        if value is DROP:
            event.pop(key, None)
        else:
            event[key] = value
    return event


def mcp_input(name: str = "PreToolUse", **fields: Any) -> dict[str, Any]:
    fields.setdefault("tool_input", {"name": "demo-pod", "namespace": "staging"})
    fields.setdefault("tool_name", "mcp__kubernetes_demo__pods_delete")
    if name == "PostToolUse":
        fields.setdefault("tool_response", dict(MCP_RESPONSE))
    return hook_input(name, **fields)


def patch_input(name: str = "PreToolUse", **fields: Any) -> dict[str, Any]:
    fields.setdefault("tool_input", {"command": PATCH})
    fields.setdefault("tool_name", "apply_patch")
    if name == "PostToolUse":
        fields.setdefault("tool_response", PATCH_RESPONSE)
    return hook_input(name, **fields)


def identity(event: Mapping[str, Any]) -> hook.CallIdentity:
    return hook.call_identity(event, hook.normalize_tool_call(event))


class FakeLedger:
    """Stands in for post_ledger_events. Each answer is (status, body), an exception, "hang", or a
    callable that returns or raises one.

    A request for events already in the retry file is a resend. With `resends`, a resend takes its
    answer from there, so the new event and the resend, sent in parallel, each get their own. With
    `by_event`, a new request whose first event's name it holds takes its answer from there, so two
    new events sent in parallel each get their own.
    """

    def __init__(
        self,
        *answers: Any,
        by_event: Mapping[str, Any] | None = None,
        resends: list[Any] | None = None,
        spool: Path | None = None,
    ) -> None:
        self.answers = list(answers)
        self.by_event = by_event
        self.calls: list[dict[str, Any]] = []
        self.during: Any = None
        self.release = threading.Event()
        self.resends = resends
        self.spool = spool

    def __call__(self, *, events: Any, key: str | None, timeout: float, url: str) -> tuple[int, bytes]:
        events = json.loads(json.dumps(list(events)))
        resend = self.is_resend(events)
        self.calls.append({"events": events, "key": key, "resend": resend, "timeout": timeout, "url": url})
        if self.during is not None:
            self.during()
        queue = self.resends if resend and self.resends is not None else self.answers
        if not resend and self.by_event is not None and events and events[0].get("event") in self.by_event:
            answer = self.by_event[events[0]["event"]]
        else:
            answer = queue.pop(0) if queue else (202, b"{}")
        if callable(answer):
            answer = answer()
        if answer == "hang":
            self.release.wait(30)
            return 202, b"{}"
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def is_resend(self, events: list[dict[str, Any]]) -> bool:
        if self.spool is None or not self.spool.exists():
            return False
        saved = []
        for line in self.spool.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                saved.append(json.loads(line).get("event"))
            except (AttributeError, ValueError):
                continue
        return bool(events) and all(event in saved for event in events)

    def events(self) -> list[dict[str, Any]]:
        return [event for call in self.calls for event in call["events"]]

    def new_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if not call["resend"]]

    def resent_calls(self) -> list[dict[str, Any]]:
        return [call for call in self.calls if call["resend"]]


class GovernanceHttp:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)

    def __call__(self, method: str, url: str, payload: Any, timeout: float) -> tuple[int, dict[str, Any]]:
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class Recorder:
    """What the in-test audit-service answers, and what it was sent."""

    def __init__(self, *, body: bytes = b'{"accepted":1}', headers: dict[str, str] | None = None, status: int = 202) -> None:
        self.body = body
        self.hang = False
        self.headers = headers or {}
        self.release = threading.Event()
        self.requests: list[dict[str, Any]] = []
        self.status = status


def serve(recorder: Recorder, test: unittest.TestCase) -> str:
    """Start a server for `recorder` on this machine and return its events URL."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            raw = self.rfile.read(int(self.headers.get("Content-Length") or 0))
            recorder.requests.append({"body": json.loads(raw), "headers": dict(self.headers), "path": self.path})
            if recorder.hang:
                recorder.release.wait(30)
                return
            self.send_response(recorder.status)
            for name, value in recorder.headers.items():
                self.send_header(name, value)
            body = b"" if recorder.status == 204 else recorder.body
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    test.addCleanup(recorder.release.set)
    return f"http://127.0.0.1:{server.server_port}/v1/events"


def serve_governance_deny(test: unittest.TestCase, *, delay: float = 0.0) -> str:
    """Start a stand-in for Mezmo Governance that denies every call after `delay` seconds."""

    class Handler(BaseHTTPRequestHandler):
        def do_POST(self) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            time.sleep(delay)
            body = json.dumps({"approved": False, "reason": DENY_REASON}).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return f"http://127.0.0.1:{server.server_port}/governance/workflow/authorize"


def serve_governance_pending(test: unittest.TestCase) -> str:
    """Start a stand-in for Mezmo Governance that sends every call for approval and never decides."""

    class Handler(BaseHTTPRequestHandler):
        def answer(self, status: int, body: dict[str, Any]) -> None:
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            encoded = json.dumps(body).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(encoded)))
            self.end_headers()
            self.wfile.write(encoded)

        def do_GET(self) -> None:
            self.answer(200, {"status": "pending"})

        def do_POST(self) -> None:
            self.answer(207, {"approved": False})

        def log_message(self, *args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
    test.addCleanup(server.server_close)
    test.addCleanup(server.shutdown)
    return f"http://127.0.0.1:{server.server_port}/governance/workflow/authorize"


def closed_port_url(path: str = "/governance/workflow/authorize") -> str:
    """A URL on this machine that refuses connections at once."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        port = probe.getsockname()[1]
    return f"http://127.0.0.1:{port}{path}"


class LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = Path(scratch.name)
        self.spool = self.dir / "spool" / "ledger-spool.jsonl"
        self.state_dir = self.dir / "codex-sessions"
        self.audit_log = self.dir / "audit.jsonl"
        self.env = {
            "HOME": str(self.dir),
            "MEZMO_GOVERNANCE_AUDIT_LOG": str(self.audit_log),
            "MEZMO_GOVERNANCE_STATE_DIR": str(self.state_dir),
            "MEZMO_LEDGER_ACCOUNT": ACCOUNT,
            "MEZMO_LEDGER_SPOOL": str(self.spool),
            "MEZMO_LEDGER_URL": LEDGER_URL,
            "NO_PROXY": "*",
        }
        # The defaults are bound from the real home at import. Point them into the temp folder,
        # so a test that unsets a setting still can't touch ~/.mezmo.
        self.default_spool = self.dir / "home" / ".mezmo" / "ledger-spool.jsonl"
        self.default_state_dir = self.dir / "home" / ".mezmo" / "governance" / "codex-sessions"
        for started in (
            patch.dict(os.environ, self.env, clear=True),
            patch.object(ledger, "DEFAULT_CALL_STATE_DIR", self.dir / "home" / ".mezmo" / "governance" / "codex-calls"),
            patch.object(ledger, "DEFAULT_LEDGER_SPOOL", self.default_spool),
            patch.object(ledger, "DEFAULT_STATE_DIR", self.default_state_dir),
            # No git email unless a test asks for one. OperatorEmailTests puts the real lookup back.
            patch.object(ledger, "operator_email", lambda: None),
            # A request to LEDGER_URL means a test reached past its fake. Requests to the servers
            # defined here go through.
            patch.object(ledger, "post_ledger_events", self.guarded_post_ledger),
        ):
            started.start()
            self.addCleanup(started.stop)
        self.timeout(1.0)
        self.fakes: list[FakeLedger] = []
        self.leaks: list[str] = []
        # Runs first: no ledger thread may outlive the test's environment.
        self.addCleanup(self.finish_threads)
        del ledger.LEDGER_MESSAGES[:]

    def guarded_post_ledger(self, *, events: Any, key: str | None, timeout: float, url: str) -> tuple[int, bytes]:
        if not url.startswith("http://127.0.0.1:"):
            self.leaks.append(url)
            raise OSError("test guard: no real network")
        return REAL_POST_LEDGER(events=events, key=key, timeout=timeout, url=url)

    def finish_threads(self) -> None:
        for fake in self.fakes:
            fake.release.set()
        for thread in ledger.LEDGER_THREADS:
            thread.join(10)
        self.assertEqual(self.leaks, [])
        del ledger.LEDGER_THREADS[:]
        del ledger.LEDGER_MESSAGES[:]
        for event in [event for fake in self.fakes for event in fake.events()] + self.spooled():
            self.assert_valid(event)

    def timeout(self, seconds: float) -> None:
        """Shorten the ledger deadline, so no test waits seconds for a hanging fake."""
        deadline = patch.object(ledger, "LEDGER_TIMEOUT_SECONDS", seconds)
        deadline.start()
        self.addCleanup(deadline.stop)

    def fake(
        self, *answers: Any, by_event: Mapping[str, Any] | None = None, resends: list[Any] | None = None
    ) -> FakeLedger:
        fake = FakeLedger(*answers, by_event=by_event, resends=resends, spool=self.spool)
        self.fakes.append(fake)
        return fake

    def run_hook(self, event: Mapping[str, Any], fake: Any = None) -> Any:
        """ledger.handle on one hook input, its stderr kept in self.stderr. Returns its None."""
        with patch.object(sys, "stderr", StringIO()) as stderr:
            output = ledger.handle(event, ledger_http=fake if fake is not None else self.fake())
        self.stderr = stderr.getvalue()
        return output

    def spool_lines(self) -> list[str]:
        if not self.spool.exists():
            return []
        return self.spool.read_text(encoding="utf-8").splitlines()

    def spooled(self) -> list[dict[str, Any]]:
        events = []
        for line in self.spool_lines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("event"), dict):
                events.append(entry["event"])
        return events

    def write_spool(self, lines: list[str]) -> None:
        self.spool.parent.mkdir(parents=True, exist_ok=True)
        self.spool.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def spool_line(self, event: dict[str, Any] | None = None, url: str = LEDGER_URL) -> str:
        if event is None:
            event = self.build("started", tool_use_id=f"saved-{time.monotonic_ns()}")
        return json.dumps({"event": event, "url": url}, separators=(",", ":"))

    def build(self, kind: str, name: str = "PreToolUse", **fields: Any) -> dict[str, Any]:
        config = ledger.load_ledger_config()
        assert config is not None
        event = ledger.ledger_event(config=config, hook_input=hook_input(name, **fields), kind=kind)
        assert event is not None
        return event

    def write_state(self, state: Any) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        ledger.session_state_path("session-1").write_text(json.dumps(state), encoding="utf-8")

    def assert_valid(self, event: Mapping[str, Any]) -> None:
        """What audit-service's schema enforces, applied to every event a test captures."""
        self.assertEqual(REQUIRED_FIELDS - set(event), set(), event)
        self.assertEqual(set(event) - TOP_LEVEL_FIELDS, set(), event)
        self.assertEqual(set(event["actor"]) - {"account", "pk", "type"}, set())
        self.assertEqual(event["actor"]["type"], "agent")
        self.assertEqual(set(event["application"]) - {"name", "version"}, set())
        for value in (
            event["actor"].get("account"),
            event["application"].get("name"),
            event["application"].get("version"),
            event["entity_id"],
            event["entity_type"],
            event["message"],
        ):
            self.assertIsInstance(value, str)
            self.assertTrue(value)
        self.assertRegex(event["event"], EVENT_NAME)
        self.assertIs(event["internal"], False)
        datetime.fromisoformat(event["timestamp"])
        self.assertEqual(set(event["extra"]) - EXTRA_KEYS, set(), event)
        self.assert_no_null(event["extra"])
        if "correlation_id" in event:
            self.assertIsInstance(event["correlation_id"], str)
            self.assertTrue(event["correlation_id"])

    def assert_no_null(self, value: Any) -> None:
        self.assertIsNotNone(value)
        if isinstance(value, dict):
            for item in value.values():
                self.assert_no_null(item)
        elif isinstance(value, list):
            for item in value:
                self.assert_no_null(item)


class EventTests(LedgerTestCase):
    def test_started_then_ran_share_one_call_id(self) -> None:
        fake = self.fake()
        self.assertIsNone(self.run_hook(hook_input("PreToolUse"), fake))
        self.assertIsNone(self.run_hook(hook_input("PostToolUse"), fake))
        started, ran = fake.events()
        self.assertEqual(started["event"], "agent.tool_call.started")
        self.assertEqual(ran["event"], "agent.tool_call.ran")
        self.assertEqual(started["extra"]["call_id"], ran["extra"]["call_id"])
        self.assertEqual(started["extra"]["call_id"], identity(hook_input()).decision_id)
        self.assertEqual(started["extra"]["entry_key"], started["extra"]["call_id"] + ":start")
        self.assertEqual(ran["extra"]["entry_key"], ran["extra"]["call_id"] + ":end")
        # A Bash result is only its output: Codex states no exit code.
        self.assertNotIn("exit_status", ran["extra"])
        self.assertEqual([len(call["events"]) for call in fake.calls], [1, 1])
        self.assertEqual(fake.calls[0]["url"], LEDGER_URL)
        self.assertEqual(fake.calls[0]["timeout"], ledger.LEDGER_TIMEOUT_SECONDS)
        self.assertEqual(self.spool_lines(), [])

    def test_each_response_gives_its_event_and_exit_status(self) -> None:
        cases = [
            # What Codex 0.167.0 sends.
            (hook_input, "a\nb\n", "ran", None),
            (patch_input, PATCH_RESPONSE, "ran", 0),
            (mcp_input, dict(MCP_RESPONSE), "ran", 0),
            (hook_input, ["Plan updated"], "ran", None),
            (hook_input, DROP, "ran", None),
            # Bash output is never parsed, whatever it looks like.
            (hook_input, '{"exit_code": 1}', "ran", None),
            (hook_input, '{"isError": true}', "ran", None),
            (hook_input, "Exit code: 1\nboom", "ran", None),
            (mcp_input, "Exit code: 1\nboom", "ran", None),
            (patch_input, '{"exit_code": 1}', "ran", None),
            (patch_input, "Success. Updated the following files:\nA notes.txt", "ran", None),
            (patch_input, "note\nExit code: 1", "ran", None),
            (patch_input, "", "ran", None),
            # A response that states a failure, which Codex 0.167.0 never sends.
            (patch_input, "Exit code: 3\nWall time: 0 seconds\nOutput:\nbad patch", "failed", 3),
            (mcp_input, {"content": [{"text": "no", "type": "text"}], "isError": True}, "failed", 1),
            (hook_input, {"is_error": True}, "failed", 1),
            (hook_input, {"exit_code": 4, "is_error": True}, "failed", 4),
            (hook_input, {"exitCode": 5, "isError": True}, "failed", 5),
            (hook_input, {"exit_code": 0, "isError": True}, "failed", 1),
            (hook_input, {"interrupted": True}, "failed", 1),
            (hook_input, {"exit_code": 0, "interrupted": True}, "failed", 1),
            (hook_input, {"exit_code": 3, "stdout": "x"}, "failed", 3),
            (hook_input, {"exitCode": 2}, "failed", 2),
            (hook_input, {"exit_code": 0, "stdout": "x"}, "ran", 0),
            (hook_input, {"exit_code": 0, "isError": False}, "ran", 0),
            # A bool is never an exit code, and only true and false are read as flags.
            (hook_input, {"exit_code": True}, "ran", None),
            (hook_input, {"exit_code": False}, "ran", None),
            (hook_input, {"exitCode": True, "isError": True}, "failed", 1),
            (hook_input, {"exit_code": "3"}, "ran", None),
            (hook_input, {"isError": "true"}, "ran", None),
            (hook_input, {"isError": None}, "ran", None),
            (hook_input, {"interrupted": "yes"}, "ran", None),
            (hook_input, {"content": "x"}, "ran", None),
            (hook_input, 42, "ran", None),
            (hook_input, None, "ran", None),
        ]
        for builder, response, kind, exit_status in cases:
            with self.subTest(builder=builder.__name__, response=response):
                event = builder("PostToolUse", tool_response=response)
                self.assertEqual(ledger.ledger_result(hook_input=event), (kind, exit_status))
                fake = self.fake()
                self.run_hook(event, fake)
                (sent,) = fake.events()
                self.assertEqual(sent["event"], f"agent.tool_call.{kind}")
                self.assertEqual(sent["extra"].get("exit_status"), exit_status)
                self.assertEqual("exit_status" in sent["extra"], exit_status is not None)

    def test_a_string_response_is_never_parsed_as_json(self) -> None:
        with patch.object(ledger.json, "loads", side_effect=AssertionError("parsed")):
            for builder in (hook_input, patch_input, mcp_input):
                event = builder("PostToolUse", tool_response='{"exit_code": 1, "isError": true}')
                self.assertEqual(ledger.ledger_result(hook_input=event), ("ran", None))

    def test_a_result_it_cannot_read_is_ran_without_an_exit_status(self) -> None:
        fake = self.fake()
        with patch.object(ledger, "ledger_result", side_effect=RuntimeError("odd shape")):
            self.run_hook(hook_input("PostToolUse"), fake)
        (event,) = fake.events()
        self.assertEqual(event["event"], "agent.tool_call.ran")
        self.assertNotIn("exit_status", event["extra"])

    def test_every_hooked_tool_is_recorded(self) -> None:
        fake = self.fake()
        tools = [
            ("apply_patch", {"command": PATCH}, "codex", "apply_patch"),
            ("Bash", {"command": "ls"}, "codex", "Bash"),
            ("mcp__kubernetes_demo__pods_delete", {"name": "demo-pod"}, "kubernetes_demo", "pods_delete"),
            ("update_plan", {"plan": [{"status": "pending", "step": "Look"}]}, "codex", "update_plan"),
            ("view_image", {"path": "/tmp/a.png"}, "codex", "view_image"),
        ]
        for tool_name, tool_input, _, _ in tools:
            self.run_hook(hook_input(tool_input=tool_input, tool_name=tool_name), fake)
        self.assertEqual(
            [(event["extra"]["tool_namespace"], event["extra"]["tool_name"]) for event in fake.events()],
            [(namespace, name) for _, _, namespace, name in tools],
        )

    def test_other_hook_events_send_nothing(self) -> None:
        fake = self.fake()
        for name in ("PermissionRequest", "PostToolUseFailure", "SessionStart", "Stop", None, 5):
            with self.subTest(name=name):
                self.assertIsNone(self.run_hook(hook_input("PostToolUse", hook_event_name=name), fake))
        self.assertEqual(fake.calls, [])
        self.assertEqual(ledger.LEDGER_THREADS, [])

    def test_ledger_read_mcp_tools_are_not_recorded(self) -> None:
        fake = self.fake()
        for event_name in ("PreToolUse", "PostToolUse"):
            for tool_name in ("get_ledger_investigation", "list_ledger_entries"):
                with self.subTest(event_name=event_name, tool_name=tool_name):
                    event = mcp_input(event_name, tool_name=f"mcp__any_server__{tool_name}")
                    self.assertIsNone(self.run_hook(event, fake))
        self.assertEqual(fake.calls, [])
        self.assertEqual(ledger.LEDGER_THREADS, [])

    def test_non_mcp_ledger_tool_names_are_recorded(self) -> None:
        fake = self.fake()
        self.run_hook(hook_input(tool_name="get_ledger_investigation"), fake)
        (event,) = fake.events()
        self.assertEqual(event["extra"]["tool_name"], "get_ledger_investigation")

    def test_an_unknown_identity_sends_nothing(self) -> None:
        fake = self.fake()
        for name in ("PreToolUse", "PostToolUse"):
            for fields in (
                {"tool_use_id": DROP}, {"session_id": " "}, {"tool_input": "ls"}, {"tool_name": "mcp__codex__Bash"},
            ):
                with self.subTest(name=name, fields=fields):
                    self.assertIsNone(self.run_hook(hook_input(name, **fields), fake))
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.spool_lines(), [])


class FieldTests(LedgerTestCase):
    def test_messages_and_intent_source(self) -> None:
        long = "echo " + "x" * 200
        cases = [
            (hook_input, {}, "codex.Bash: ls -la"),
            (hook_input, {"tool_input": {"command": "  kubectl get pods \\\n\t-n staging  "}},
             "codex.Bash: kubectl get pods \\ -n staging"),
            (hook_input, {"tool_input": {"command": long}}, "codex.Bash: " + long[:80]),
            (hook_input, {"tool_input": {"command": "ls", "description": "List files"}}, "codex.Bash: ls"),
            (hook_input, {"tool_input": {"command": ["ls", "-la"]}}, "codex.Bash"),
            (hook_input, {"tool_input": {"command": "  \n "}}, "codex.Bash"),
            (hook_input, {"tool_input": {}}, "codex.Bash"),
            (patch_input, {}, "codex.apply_patch: *** Begin Patch *** Add File: notes.txt +hello *** End Patch"),
            (mcp_input, {}, "kubernetes_demo.pods_delete"),
            (mcp_input, {"tool_input": {"command": "delete it"}}, "kubernetes_demo.pods_delete: delete it"),
        ]
        for builder, fields, intent in cases:
            with self.subTest(builder=builder.__name__, fields=fields):
                config = ledger.load_ledger_config()
                started = ledger.ledger_event(config=config, hook_input=builder("PreToolUse", **fields), kind="started")
                ran = ledger.ledger_event(
                    config=config, hook_input=builder("PostToolUse", tool_response="first\nsecond", **fields), kind="ran"
                )
                failed = ledger.ledger_event(
                    config=config, hook_input=builder("PostToolUse", tool_response={"content": "bad", "isError": True}, **fields),
                    kind="failed",
                )
                not_run = ledger.ledger_event(
                    config=config, hook_input=builder("PreToolUse", **fields), kind="not_run", outcome="denied", reason="No."
                )
                self.assertEqual(started["message"], intent)
                self.assertEqual(ran["message"], f"{intent}: ran - first")
                self.assertEqual(failed["message"], f"{intent}: failed - bad")
                self.assertEqual(not_run["message"], f"{intent}: not run - No.")
                for event in (started, ran, failed, not_run):
                    self.assert_valid(event)
                    self.assertEqual(event["extra"]["intent_source"], "none")

    def test_the_envelope_names_codex_as_an_agent(self) -> None:
        fake = self.fake()
        self.run_hook(hook_input(), fake)
        self.run_hook(mcp_input(), fake)
        bash, mcp = fake.events()
        self.assertEqual((bash["extra"]["tool_namespace"], bash["extra"]["tool_name"]), ("codex", "Bash"))
        self.assertEqual((mcp["extra"]["tool_namespace"], mcp["extra"]["tool_name"]), ("kubernetes_demo", "pods_delete"))
        for event, source in ((bash, hook_input()), (mcp, mcp_input())):
            self.assertEqual(event["actor"], {"account": ACCOUNT, "pk": "codex:session-1", "type": "agent"})
            self.assertEqual(event["application"], {"name": "mezmo-codex-plugin", "version": ledger.LEDGER_VERSION})
            self.assertEqual(event["entity_type"], "tool_call")
            self.assertEqual(event["entity_id"], "unknown")
            self.assertIs(event["internal"], False)
            self.assertEqual(event["extra"]["agent"], "codex")
            # `pods_delete` holds a write word but names no service.
            self.assertEqual(event["extra"]["access"], "write" if event is mcp else "read")
            self.assertEqual(event["extra"]["target"], "unknown")
            self.assertEqual(event["extra"]["session_id"], "session-1")
            self.assertEqual(event["extra"]["request_id"], identity(source).request_id)
            self.assertNotIn("correlation_id", event)

    def test_the_version_matches_the_plugin_manifest(self) -> None:
        manifest = json.loads((ADAPTER_DIR / ".codex-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(ledger.LEDGER_VERSION, manifest["version"])
        self.assertEqual(self.build("started")["application"]["version"], manifest["version"])

    def test_output_text_shapes_give_the_message_line(self) -> None:
        cases = [
            ("plain text\nmore", "ran - plain text"),
            ("\n\n  first   line \nsecond", "ran - first line"),
            (PATCH_RESPONSE, "ran - Exit code: 0"),
            (dict(MCP_RESPONSE), "ok - pod deleted"),
            ({"content": "pod deleted"}, "ran - pod deleted"),
            ({"content": [{"type": "image"}, {"text": "\nfrom blocks", "type": "text"}, "loose"]}, "ran - from blocks"),
            ([{"text": "listed", "type": "text"}], "ran - listed"),
            ({"stderr": "warning: x", "stdout": "  "}, "ran - warning: x"),
            ("", "ran"),
            (42, "ran"),
            ({"other": "x"}, "ran"),
            ("ls: /nope: No such file or directory", "ran - ls: /nope: No such file or directory"),
        ]
        for response, ending in cases:
            with self.subTest(response=response):
                fake = self.fake()
                self.run_hook(hook_input("PostToolUse", tool_response=response), fake)
                self.assertEqual(fake.events()[0]["message"], f"codex.Bash: ls -la: {ending}")

    def test_output_is_rendered_as_text_or_sorted_compact_json(self) -> None:
        fake = self.fake()
        self.run_hook(hook_input("PostToolUse", tool_response={"b": "é", "a": [1, 2]}), fake)
        self.run_hook(hook_input("PostToolUse", tool_response="a\nb\n"), fake)
        as_object, as_text = fake.events()
        self.assertEqual(as_object["extra"]["output"], '{"a":[1,2],"b":"é"}')
        self.assertEqual(as_text["extra"]["output"], "a\nb\n")
        self.assertEqual(ledger.ledger_json("Exit code 1\nboom"), "Exit code 1\nboom")

    def test_arguments_output_and_message_are_cut(self) -> None:
        fake = self.fake()
        long = "x" * 10_000
        self.run_hook(hook_input(tool_input={"command": long}), fake)
        self.run_hook(hook_input("PostToolUse", tool_input={"command": long}, tool_response=long), fake)
        started, ran = fake.events()
        not_run = ledger.ledger_event(
            config=ledger.load_ledger_config(), hook_input=hook_input(), kind="not_run", outcome="denied", reason="r " * 1000
        )
        self.assertEqual(len(started["extra"]["arguments"]), 4096)
        self.assertEqual(started["message"], "codex.Bash: " + "x" * 80)
        self.assertEqual(len(ran["extra"]["output"]), 4096)
        self.assertEqual(len(ran["message"]), 200)
        self.assertEqual(len(json.loads(not_run["extra"]["output"])["reason"]), 300)
        self.assertEqual(len(not_run["message"]), 200)

    def test_arguments_only_on_started(self) -> None:
        fake = self.fake()
        tool_input = {"command": "echo CANARY", "workdir": "/tmp/demo"}
        self.run_hook(hook_input(tool_input=tool_input), fake)
        self.run_hook(hook_input("PostToolUse", tool_input=tool_input), fake)
        started, ran = fake.events()
        self.assertEqual(json.loads(started["extra"]["arguments"]), tool_input)
        self.assertNotIn("arguments", ran["extra"])
        self.assertNotIn("output", started["extra"])

    def test_output_is_omitted_without_a_tool_response(self) -> None:
        fake = self.fake()
        self.run_hook(hook_input("PostToolUse", tool_response=DROP), fake)
        (ran,) = fake.events()
        self.assertNotIn("output", ran["extra"])
        self.assertEqual(ran["message"], "codex.Bash: ls -la: ran")

    def start_time(self, event: Mapping[str, Any], approved_at: float) -> Path:
        """The file the governance hook writes when it approves `event`."""
        path = ledger.start_time_path(identity(event).decision_id)
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(str(approved_at), encoding="utf-8")
        return path

    def test_a_start_time_gives_the_duration_and_is_removed(self) -> None:
        fake = self.fake()
        event = hook_input("PostToolUse")
        start_time = self.start_time(event, time.time() - 1.5)
        self.run_hook(event, fake)
        (ran,) = fake.events()
        self.assertIsInstance(ran["extra"]["duration_ms"], int)
        self.assertTrue(1500 <= ran["extra"]["duration_ms"] < 60_000)
        self.assertEqual(list(ran["extra"]), sorted(ran["extra"]))
        self.assertFalse(start_time.exists())

    def test_no_duration_is_sent_without_a_start_time(self) -> None:
        # Codex sends none, so one in the hook input is not the call's.
        fake = self.fake()
        self.run_hook(hook_input("PostToolUse", duration_ms=12, started_at="2026-11-10T00:00:00Z"), fake)
        (ran,) = fake.events()
        self.assertNotIn("duration_ms", ran["extra"])
        self.assertNotIn("started_at", ran["extra"])

    def test_a_start_time_older_than_seven_days_is_removed(self) -> None:
        eight_days_ago = time.time() - 8 * 24 * 3600
        lost = self.start_time(hook_input("PostToolUse", tool_use_id="lost"), eight_days_ago)
        os.utime(lost, (eight_days_ago, eight_days_ago))
        running = self.start_time(hook_input("PostToolUse", tool_use_id="running"), time.time())
        self.run_hook(hook_input("PostToolUse"))
        self.assertFalse(lost.exists())
        self.assertTrue(running.exists())

    def test_decision_id_only_when_governance_has_both_settings(self) -> None:
        fake = self.fake()
        self.run_hook(hook_input("PostToolUse"), fake)
        os.environ["MEZMO_GOVERNANCE_TOKEN"] = "sts_test"
        self.run_hook(hook_input("PostToolUse"), fake)
        os.environ["MEZMO_GOVERNANCE_URL"] = "  "
        self.run_hook(hook_input("PostToolUse"), fake)
        os.environ["MEZMO_GOVERNANCE_URL"] = AUTHORIZE_URL
        self.assertTrue(ledger.governance_configured())
        self.run_hook(hook_input("PostToolUse"), fake)
        *off, on = fake.events()
        for event in off:
            self.assertNotIn("decision_id", event["extra"])
        self.assertEqual(on["extra"]["decision_id"], on["extra"]["call_id"])


class StartTimeTests(LedgerTestCase):
    """An approved call leaves the ledger hook the time of its approval, only while the ledger is on."""

    def approve(self, env: Mapping[str, str]) -> Any:
        with patch.dict(os.environ, {**GOVERNANCE, **env}):
            return hook.handle_event(hook_input(), http=GovernanceHttp((200, {"approved": True})))

    def test_an_approved_call_saves_its_start_time_only_while_the_ledger_is_on(self) -> None:
        self.assertIsNone(self.approve({"MEZMO_LEDGER_URL": ""}))
        self.assertFalse(self.state_dir.exists())

        before = time.time()
        self.assertIsNone(self.approve({}))
        decision_id = identity(hook_input()).decision_id
        start_time = self.state_dir / "codex-calls" / decision_id
        self.assertEqual(ledger.start_time_path(decision_id), start_time)
        self.assertTrue(before <= float(start_time.read_text(encoding="utf-8")) <= time.time())
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(start_time.parent.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(start_time.stat().st_mode), 0o600)

    def test_a_start_time_that_cannot_be_written_leaves_the_approval_alone(self) -> None:
        self.state_dir.write_text("a file where the folder should be", encoding="utf-8")
        self.assertIsNone(self.approve({}))


class InvestigationTests(LedgerTestCase):
    def sent(self, name: str = "PreToolUse") -> dict[str, Any]:
        fake = self.fake()
        self.run_hook(hook_input(name), fake)
        (event,) = fake.events()
        return event

    def test_investigation_and_references_come_from_session_state(self) -> None:
        self.write_state({
            "investigation": {"incident_key": "jira:LOG-1", "joined_at": "2026-11-09T00:00:00Z", "thread_id": "thread-1"},
            "references": [{"kind": "pr", "url": "https://example.test/1"}],
        })
        for name in ("PreToolUse", "PostToolUse"):
            event = self.sent(name)
            self.assertEqual(event["correlation_id"], "thread-1")
            self.assertEqual(event["extra"]["thread_id"], "thread-1")
            self.assertEqual(event["extra"]["incident_key"], "jira:LOG-1")
            self.assertEqual(event["extra"]["references"], [{"kind": "pr", "url": "https://example.test/1"}])

    def test_the_state_file_is_the_codex_one(self) -> None:
        self.assertEqual(REAL_DEFAULT_STATE_DIR, HOME_AT_IMPORT / ".mezmo" / "governance" / "codex-sessions")
        self.assertEqual(ledger.session_state_path("session-1"), self.state_dir / "session-1.json")
        self.assertEqual(ledger.session_state_path("a/b:c" + "x" * 200).name, "a_b_c" + "x" * 123 + ".json")
        del os.environ["MEZMO_GOVERNANCE_STATE_DIR"]
        self.assertEqual(ledger.session_state_path("session-1"), self.default_state_dir / "session-1.json")

    def test_the_state_file_is_only_read(self) -> None:
        self.run_hook(hook_input("PreToolUse"))
        self.run_hook(hook_input("PostToolUse"))
        self.assertFalse(ledger.record_governance_refusal(hook_input=hook_input(), ledger_http=self.fake(), outcome="denied", reason="No."))
        self.assertFalse(self.state_dir.exists())
        self.write_state({"investigation": {"thread_id": "thread-1"}})
        before = ledger.session_state_path("session-1").read_bytes()
        self.run_hook(hook_input("PostToolUse"))
        self.assertEqual(ledger.session_state_path("session-1").read_bytes(), before)
        self.assertEqual([path.name for path in self.state_dir.iterdir()], ["session-1.json"])

    def test_session_state_wins_over_the_environment_without_mixing(self) -> None:
        os.environ.update({"MEZMO_LEDGER_INCIDENT_KEY": INCIDENT_KEY, "MEZMO_LEDGER_INVESTIGATION_ID": "env-thread"})
        self.write_state({"investigation": {"thread_id": "state-thread"}})
        event = self.sent()
        self.assertEqual(event["extra"]["thread_id"], "state-thread")
        self.assertEqual(event["correlation_id"], "state-thread")
        self.assertNotIn("incident_key", event["extra"])

    def test_the_environment_is_used_without_a_state_investigation(self) -> None:
        os.environ.update({"MEZMO_LEDGER_INCIDENT_KEY": INCIDENT_KEY, "MEZMO_LEDGER_INVESTIGATION_ID": "env-thread"})
        for state in (None, {"investigation": None}, {"investigation": {"thread_id": "  "}}, {"investigation": "x"}, [1]):
            with self.subTest(state=state):
                if state is not None:
                    self.write_state(state)
                event = self.sent()
                self.assertEqual(event["extra"]["thread_id"], "env-thread")
                self.assertEqual(event["extra"]["incident_key"], INCIDENT_KEY)
                self.assertEqual(event["correlation_id"], "env-thread")

    def test_an_unreadable_state_file_means_no_investigation(self) -> None:
        self.state_dir.mkdir(parents=True)
        ledger.session_state_path("session-1").write_text("{not json", encoding="utf-8")
        event = self.sent()
        self.assertNotIn("thread_id", event["extra"])
        self.assertNotIn("references", event["extra"])

    def test_thread_id_is_derived_from_the_incident_key(self) -> None:
        os.environ["MEZMO_LEDGER_INCIDENT_KEY"] = INCIDENT_KEY
        event = self.sent()
        self.assertEqual(event["extra"]["thread_id"], CHECK_THREAD_ID)
        self.assertEqual(event["correlation_id"], CHECK_THREAD_ID)
        self.assertEqual(event["extra"]["incident_key"], INCIDENT_KEY)

    def test_no_investigation_fields_without_a_source(self) -> None:
        self.write_state({"references": {"kind": "pr"}})
        event = self.sent()
        self.assertNotIn("correlation_id", event)
        for field in ("incident_key", "references", "thread_id"):
            self.assertNotIn(field, event["extra"])


class InvestigationChangeTests(LedgerTestCase):
    """A Bash call that runs ledger_investigation.py join or close, and prints its result line."""

    def cli_input(self, command: str, action: str) -> dict[str, Any]:
        # The printed thread_id is the agent's to read. The hook derives its own.
        line = json.dumps({"action": action, "incident_key": INCIDENT_KEY, "thread_id": "printed-thread"})
        cli = "python3 ~/.codex/hooks/ledger_investigation.py"
        return hook_input(
            "PostToolUse",
            tool_input={"command": f"{cli} {command} --incident-key {INCIDENT_KEY}"},
            tool_response=f"starting\n{line}\n",
        )

    def state(self) -> dict[str, Any]:
        return json.loads(ledger.session_state_path("session-1").read_text(encoding="utf-8"))

    def assert_investigation_event(self, event: Mapping[str, Any], action: str) -> None:
        self.assertEqual(event["event"], f"agent.investigation.{action}")
        self.assertEqual(event["entity_type"], "investigation")
        self.assertEqual(event["entity_id"], CHECK_THREAD_ID)
        self.assertEqual(event["correlation_id"], CHECK_THREAD_ID)
        self.assertEqual(
            event["extra"],
            {"agent": "codex", "incident_key": INCIDENT_KEY, "session_id": "session-1", "thread_id": CHECK_THREAD_ID},
        )

    def test_only_a_real_join_or_close_with_its_own_key_is_recognized(self) -> None:
        # Compact: claude-code/tests/test_ledger.py covers _investigation_call, which is the same text.
        cli = "python3 ~/.codex/hooks/ledger_investigation.py"
        joined = json.dumps({"action": "joined", "incident_key": INCIDENT_KEY, "thread_id": "printed-thread"})
        runbook = f"{cli} join --incident-key {INCIDENT_KEY}\n{joined}\n"
        cases = (
            (f"{cli} join --incident-key {INCIDENT_KEY}", ("joined", INCIDENT_KEY)),
            (f"{cli} join --incident-key={INCIDENT_KEY} 2>&1", ("joined", INCIDENT_KEY)),
            (f"{cli} join --incident-key {INCIDENT_KEY} | tail -n 5", ("joined", INCIDENT_KEY)),
            (f"{cli} join --incident-key jira:OPS-1", None),
            (f'{cli} join --incident-key "$KEY"', None),
            (f"cd /tmp && {cli} join --incident-key {INCIDENT_KEY}", None),
            (f"tee runbook.md <<'EOF'\n{runbook}EOF", None),
            (f"printf '%s\\n' '(example)' {cli} join --incident-key {INCIDENT_KEY} '{joined}'", None),
        )
        for command, expected in cases:
            with self.subTest(command=command):
                event = hook_input("PostToolUse", tool_input={"command": command}, tool_response=f"run\n{joined}\n")
                self.assertEqual(ledger.investigation_action(hook_input=event), expected)

    def test_a_join_writes_the_state_and_sends_joined(self) -> None:
        fake = self.fake()
        self.run_hook(self.cli_input("join", "joined"), fake)
        state = self.state()
        joined_at = state["investigation"]["joined_at"]
        datetime.fromisoformat(joined_at)
        self.assertEqual(
            state["investigation"], {"incident_key": INCIDENT_KEY, "joined_at": joined_at, "thread_id": CHECK_THREAD_ID}
        )
        self.assertEqual((state["session_id"], state["version"]), ("session-1", 1))
        if os.name != "nt":
            self.assertEqual(stat.S_IMODE(self.state_dir.stat().st_mode), 0o700)
            self.assertEqual(stat.S_IMODE(ledger.session_state_path("session-1").stat().st_mode), 0o600)
        # One request each, and the join call's own event is stamped with the thread it joined.
        self.assertEqual([len(call["events"]) for call in fake.new_calls()], [1, 1])
        joined, ran = sorted(fake.events(), key=lambda event: event["event"])
        self.assert_investigation_event(joined, "joined")
        self.assertEqual(ran["event"], "agent.tool_call.ran")
        self.assertEqual(ran["extra"]["thread_id"], CHECK_THREAD_ID)
        self.assertEqual(self.spool_lines(), [])
        self.assertEqual(self.stderr, "")

    def test_a_close_clears_the_state_and_sends_closed(self) -> None:
        references = [{"kind": "pr", "url": "https://example.test/1"}]
        joined_at = "2026-11-09T00:00:00+00:00"
        self.write_state({
            "investigation": {"incident_key": INCIDENT_KEY, "joined_at": joined_at, "thread_id": CHECK_THREAD_ID},
            "references": references,
        })
        fake = self.fake()
        self.run_hook(self.cli_input("close", "closed"), fake)
        state = self.state()
        self.assertNotIn("investigation", state)
        self.assertEqual(state["references"], references)
        self.assertEqual([len(call["events"]) for call in fake.new_calls()], [1, 1])
        closed, ran = sorted(fake.events(), key=lambda event: event["event"])
        self.assert_investigation_event(closed, "closed")
        # Built before the state was cleared.
        self.assertEqual(ran["extra"]["thread_id"], CHECK_THREAD_ID)
        self.assertEqual(self.spool_lines(), [])
        self.assertEqual(self.stderr, "")
        # A session that never joined still sends `closed`, and gets no state file from it.
        never_joined = self.fake()
        self.run_hook({**self.cli_input("close", "closed"), "session_id": "session-2"}, never_joined)
        self.assertIn("agent.investigation.closed", [event["event"] for event in never_joined.events()])
        self.assertFalse(ledger.session_state_path("session-2").exists())

    def test_a_join_the_state_did_not_take_warns_and_sends_only_the_calls_event(self) -> None:
        # The environment names the same investigation, so only a check of the state itself fails here.
        os.environ["MEZMO_LEDGER_INCIDENT_KEY"] = INCIDENT_KEY
        fake = self.fake()
        with patch.object(ledger, "update_session_state", lambda *, change, lock_timeout, session_id: None):
            self.run_hook(self.cli_input("join", "joined"), fake)
        self.assertIn(f"did not join investigation {INCIDENT_KEY}", self.stderr)
        self.assertEqual([event["event"] for event in fake.events()], ["agent.tool_call.ran"])
        self.assertEqual(self.spool_lines(), [])

    def test_a_join_that_outwaits_the_state_lock_is_never_written(self) -> None:
        try:
            import fcntl
        except ImportError:
            self.skipTest("no fcntl")
        with patch.object(ledger, "INVESTIGATION_STATE_TIMEOUT_SECONDS", 0.05):
            self.state_dir.mkdir(parents=True, exist_ok=True)
            path = ledger.session_state_path("session-1")
            # Another open file's lock, as another process would hold it. Freed after the hook's wait
            # ends, so a hook that waits for it without a bound fails here instead of hanging.
            lock = open(f"{path}.lock", "a")
            self.addCleanup(lock.close)
            fcntl.flock(lock, fcntl.LOCK_EX)
            release = threading.Timer(0.3, lock.close)
            self.addCleanup(release.cancel)
            release.start()
            fake = self.fake()
            self.run_hook(self.cli_input("join", "joined"), fake)
        self.assertIn(f"did not join investigation {INCIDENT_KEY}", self.stderr)
        self.assertEqual([event["event"] for event in fake.events()], ["agent.tool_call.ran"])
        # Once the lock is free, nothing left over from the hook may still write the state.
        release.join(5)
        for thread in ledger.LEDGER_THREADS:
            thread.join(5)
        state = json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}
        self.assertNotIn("investigation", state)

    def test_only_the_refused_event_is_saved_for_retry(self) -> None:
        names = ["agent.investigation.joined", "agent.tool_call.ran"]
        for refused in names:
            with self.subTest(refused=refused):
                fake = self.fake(by_event={refused: (503, b"")})
                self.run_hook(self.cli_input("join", "joined"), fake)
                self.assertEqual(sorted(event["event"] for event in fake.events()), names)
                self.assertEqual([event["event"] for event in self.spooled()], [refused])
                self.spool.unlink()


class OperatorEmailTests(LedgerTestCase):
    """Every event carries the git email of whoever runs the session, when git has one."""

    def git(self, answer: Any) -> list[Any]:
        """Point operator_email at a stand-in git: (stdout, exit code), or an exception it raises.

        Returns the list of commands it was asked to run.
        """
        calls: list[Any] = []

        def run(command: Any, **kwargs: Any) -> subprocess.CompletedProcess[str]:
            calls.append(command)
            if isinstance(answer, BaseException):
                raise answer
            stdout, returncode = answer
            return subprocess.CompletedProcess(command, returncode, stdout=stdout, stderr="")

        for started in (
            patch.object(ledger, "operator_email", REAL_OPERATOR_EMAIL),
            patch.object(ledger.subprocess, "run", run),
        ):
            started.start()
            self.addCleanup(started.stop)
        REAL_OPERATOR_EMAIL.cache_clear()
        self.addCleanup(REAL_OPERATOR_EMAIL.cache_clear)
        return calls

    def test_every_event_carries_the_git_email(self) -> None:
        calls = self.git((f"{OPERATOR_EMAIL}\n", 0))
        config = ledger.load_ledger_config()
        assert config is not None
        post = hook_input("PostToolUse")
        events = [
            ledger.ledger_event(config=config, hook_input=hook_input(), kind="started"),
            ledger.ledger_event(config=config, hook_input=post, kind="ran"),
            ledger.ledger_event(config=config, exit_status=1, hook_input=post, kind="failed"),
            ledger.ledger_event(config=config, hook_input=hook_input(), kind="not_run", outcome="denied", reason="No."),
        ] + [
            ledger.investigation_event(
                action=action, config=config, hook_input=post, incident_key=INCIDENT_KEY, thread_id=CHECK_THREAD_ID
            )
            for action in ("closed", "joined")
        ]
        for event in events:
            assert event is not None
            with self.subTest(event=event["event"]):
                self.assertEqual(event["extra"]["operator_email"], OPERATOR_EMAIL)
                self.assert_valid(event)
        self.assertEqual(calls, [["git", "config", "--get", "user.email"]])

    def test_no_email_leaves_the_event_unchanged(self) -> None:
        def without_time(event: Mapping[str, Any]) -> dict[str, Any]:
            return {key: value for key, value in event.items() if key != "timestamp"}

        expected = without_time(self.build("started"))
        cases = {
            "git missing": FileNotFoundError("git"),
            "git timed out": subprocess.TimeoutExpired(["git"], 1),
            "non-zero exit": (f"{OPERATOR_EMAIL}\n", 1),
            "only whitespace": (" \n", 0),
        }
        for case, answer in cases.items():
            with self.subTest(case=case):
                self.git(answer)
                event = self.build("started")
                self.assertNotIn("operator_email", event["extra"])
                self.assert_no_null(event["extra"])
                self.assertEqual(without_time(event), expected)


class SettingsTests(LedgerTestCase):
    def test_sending_is_off_when_either_setting_is_missing(self) -> None:
        for missing in (("MEZMO_LEDGER_URL",), ("MEZMO_LEDGER_ACCOUNT",), ("MEZMO_LEDGER_URL", "MEZMO_LEDGER_ACCOUNT")):
            for blank in (True, False):
                with self.subTest(missing=missing, blank=blank), patch.dict(os.environ):
                    for name in missing:
                        if blank:
                            os.environ[name] = "  "
                        else:
                            del os.environ[name]
                    fake = self.fake()
                    for name in ("PreToolUse", "PostToolUse"):
                        self.assertIsNone(self.run_hook(hook_input(name), fake))
                        self.assertEqual(self.stderr, "")
                    self.assertIsNone(ledger.load_ledger_config())
                    self.assertEqual(fake.calls, [])
                    self.assertFalse(self.spool.exists())
                    self.assertEqual(ledger.LEDGER_THREADS, [])

    def test_the_spool_path_is_shared_with_claude_code(self) -> None:
        self.assertEqual(REAL_DEFAULT_LEDGER_SPOOL, HOME_AT_IMPORT / ".mezmo" / "ledger-spool.jsonl")
        self.assertEqual(ledger.ledger_spool_path(), self.spool)
        del os.environ["MEZMO_LEDGER_SPOOL"]
        self.assertEqual(ledger.ledger_spool_path(), self.default_spool)

    def test_post_ledger_events_builds_the_authorization_header_from_the_key(self) -> None:
        recorder = Recorder()
        url = serve(recorder, self)
        cases = [(None, None), ("Bearer abc", "Bearer abc"), ("Token abc", "Token abc"), ("abc", "Token abc")]
        for key, header in cases:
            with self.subTest(key=key):
                status, _ = ledger.post_ledger_events(events=[{"a": 1}], key=key, timeout=2.0, url=url)
                self.assertEqual(status, 202)
                headers = recorder.requests[-1]["headers"]
                self.assertEqual(headers.get("Authorization"), header)
                self.assertEqual(headers.get("Content-Type"), "application/json")
                self.assertEqual(headers.get("Accept"), "application/json")
                self.assertEqual(recorder.requests[-1]["body"], [{"a": 1}])

    def sent_authorization(self, name: str = "PostToolUse") -> str | None:
        """The Authorization header ledger.handle sends to a server defined here."""
        recorder = Recorder()
        os.environ["MEZMO_LEDGER_URL"] = serve(recorder, self)
        with patch.object(sys, "stderr", StringIO()):
            ledger.handle(hook_input(name))
        (request,) = recorder.requests
        return request["headers"].get("Authorization")

    def test_the_key_is_the_ledger_key_else_the_governance_token_else_none(self) -> None:
        cases = [
            ({}, None),
            ({"MEZMO_GOVERNANCE_URL": AUTHORIZE_URL}, None),
            # The token alone is enough: the governance URL plays no part.
            ({"MEZMO_GOVERNANCE_TOKEN": "sts_token"}, "Token sts_token"),
            ({"MEZMO_GOVERNANCE_TOKEN": "Bearer sts_token"}, "Bearer sts_token"),
            ({"MEZMO_GOVERNANCE_TOKEN": "sts_token", "MEZMO_GOVERNANCE_URL": AUTHORIZE_URL}, "Token sts_token"),
            ({"MEZMO_GOVERNANCE_TOKEN": "sts_token", "MEZMO_LEDGER_KEY": "ledger-key"}, "Token ledger-key"),
            ({"MEZMO_GOVERNANCE_TOKEN": "sts_token", "MEZMO_LEDGER_KEY": "  "}, "Token sts_token"),
            ({"MEZMO_LEDGER_KEY": "bearer ledger-key"}, "bearer ledger-key"),
        ]
        for values, header in cases:
            with self.subTest(values=values), patch.dict(os.environ, values):
                for name in ("PreToolUse", "PostToolUse"):
                    self.assertEqual(self.sent_authorization(name), header)

    def test_an_unsafe_url_or_key_turns_sending_off(self) -> None:
        cases = [
            ({"MEZMO_LEDGER_URL": "http://audit.example.test/v1/events"}, "MEZMO_LEDGER_URL", "audit.example.test"),
            ({"MEZMO_LEDGER_URL": "https://user:pw@audit.example.test/v1/events"}, "MEZMO_LEDGER_URL", "pw@"),
            ({"MEZMO_LEDGER_URL": "https://user@audit.example.test/v1/events"}, "MEZMO_LEDGER_URL", "user@"),
            ({"MEZMO_LEDGER_URL": "ftp://127.0.0.1/v1/events"}, "MEZMO_LEDGER_URL", "ftp"),
            ({"MEZMO_LEDGER_URL": "audit.example.test/v1/events"}, "MEZMO_LEDGER_URL", "audit.example.test"),
            ({"MEZMO_LEDGER_KEY": "bad\nkey"}, "MEZMO_LEDGER_KEY", "bad"),
            ({"MEZMO_GOVERNANCE_TOKEN": "bad\x7fkey"}, "MEZMO_GOVERNANCE_TOKEN", "bad"),
            ({"MEZMO_GOVERNANCE_TOKEN": "badékey"}, "MEZMO_GOVERNANCE_TOKEN", "bad"),
            ({"MEZMO_GOVERNANCE_TOKEN": "bad\nkey", "MEZMO_LEDGER_KEY": "worse\nkey"}, "MEZMO_LEDGER_KEY", "worse"),
        ]
        for values, setting, secret in cases:
            with self.subTest(values=values), patch.dict(os.environ, values):
                fake = self.fake()
                self.run_hook(hook_input("PostToolUse"), fake)
                self.assertIsNone(ledger.load_ledger_config())
                self.assertEqual(fake.calls, [])
                self.assertFalse(self.spool.exists())
                self.assertEqual(len(self.stderr.splitlines()), 1)
                self.assertIn(setting, self.stderr)
                self.assertNotIn(secret, self.stderr)
                for other in ("MEZMO_LEDGER_KEY", "MEZMO_GOVERNANCE_TOKEN"):
                    if other != setting:
                        self.assertNotIn(other, self.stderr)

    def test_https_and_loopback_http_are_allowed(self) -> None:
        for url in ("https://audit.example.test/v1/events", "http://localhost:65530/v1/events", "http://[::1]:65530/v1/events"):
            with self.subTest(url=url), patch.dict(os.environ, {"MEZMO_LEDGER_URL": url}):
                self.assertEqual(ledger.load_ledger_config(), ledger.LedgerConfig(ACCOUNT, None, url))

    def test_post_ledger_events_refuses_an_unsafe_url_before_any_request(self) -> None:
        with patch.object(ledger, "build_opener") as opener:
            for url in ("http://audit.example.test/v1/events", "https://user:pw_secret@audit.example.test/v1/events"):
                with self.subTest(url=url), self.assertRaises(hook.HookContractError) as caught:
                    REAL_POST_LEDGER(events=[{"a": 1}], key="abc", timeout=1.0, url=url)
                self.assertNotIn("pw_secret", str(caught.exception))
        opener.assert_not_called()

    def test_a_redirect_is_not_followed_and_the_events_are_dropped(self) -> None:
        elsewhere = Recorder()
        target = serve(elsewhere, self)
        redirect = Recorder(body=b"", headers={"Location": target}, status=307)
        os.environ["MEZMO_LEDGER_URL"] = serve(redirect, self)
        os.environ["MEZMO_LEDGER_KEY"] = "ledger-key"
        with patch.object(sys, "stderr", StringIO()) as stderr:
            self.assertIsNone(ledger.handle(hook_input()))
        self.assertEqual(len(redirect.requests), 1)
        self.assertEqual(elsewhere.requests, [])
        self.assertEqual(self.spool_lines(), [])
        self.assertIn("HTTP 307", stderr.getvalue())

    def test_success_statuses_are_done(self) -> None:
        recorder = Recorder()
        os.environ["MEZMO_LEDGER_URL"] = serve(recorder, self)
        for status, body in ((200, b"{}"), (204, b""), (202, b"accepted, not json")):
            with self.subTest(status=status):
                recorder.status, recorder.body = status, body
                self.run_hook(hook_input(tool_use_id=f"call-{status}"), ledger.post_ledger_events)
                self.assertEqual(self.spool_lines(), [])
                self.assertEqual(self.stderr, "")
        self.assertEqual(len(recorder.requests), 3)

    def test_retryable_statuses_save_the_events(self) -> None:
        for status in (401, 408, 429, 500, 503):
            with self.subTest(status=status):
                self.spool.unlink(missing_ok=True)
                self.run_hook(hook_input(), self.fake((status, b"{}")))
                (saved,) = self.spooled()
                self.assertEqual(saved["event"], "agent.tool_call.started")

    def test_other_client_errors_drop_the_events(self) -> None:
        # 400: MEZMO_LEDGER_ACCOUNT isn't the key's account. 403: the key may not write.
        for status in (400, 403, 404, 409, 422, 302):
            with self.subTest(status=status):
                self.run_hook(hook_input(), self.fake((status, b'{"code":"EINVAL"}')))
                self.assertEqual(self.spool_lines(), [])
                self.assertIn(f"HTTP {status}", self.stderr)

    def test_a_207_saves_only_the_rejected_events(self) -> None:
        config = ledger.load_ledger_config()
        events = [self.build("started", tool_use_id=f"call-{n}") for n in range(3)]
        body = json.dumps({
            "accepted": 2,
            "events": [
                {"audit_event_id": "a", "error": None, "index": 0, "status": "accepted"},
                {"audit_event_id": None, "error": "bad", "index": 1, "status": "rejected"},
                {"audit_event_id": "c", "error": None, "index": 2, "status": "accepted"},
            ],
            "rejected": 1,
        }).encode()
        self.assertEqual(
            ledger.send_ledger_events(config=config, events=events, ledger_http=self.fake((207, body))),
            ledger.LedgerSendResult(answered=True, save=[events[1]]),
        )
        for unusable in (b"not json", b"[]", b'{"events": "x"}', b'{"events": []}'):
            with self.subTest(body=unusable):
                self.assertEqual(
                    ledger.send_ledger_events(config=config, events=events, ledger_http=self.fake((207, unusable))),
                    ledger.LedgerSendResult(answered=True, save=[]),
                )
        self.assertEqual(
            ledger.LEDGER_MESSAGES,
            [
                "Mezmo ledger dropped 3 event(s): "
                "the ledger service answered HTTP 207 without usable event statuses",
            ] * 4,
        )
        self.assertEqual(self.spool_lines(), [])


class RetryFileTests(LedgerTestCase):
    def test_a_network_failure_is_resent_by_the_next_run(self) -> None:
        self.run_hook(hook_input("PostToolUse"), self.fake(OSError("refused")))
        (saved,) = self.spooled()
        self.assertEqual(json.loads(self.spool_lines()[0])["url"], LEDGER_URL)
        fake = self.fake()
        self.run_hook(hook_input("PostToolUse", tool_use_id="tool-use-2"), fake)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual([call["events"] for call in fake.resent_calls()], [[saved]])
        self.assertEqual(
            [event["extra"]["call_id"] for call in fake.new_calls() for event in call["events"]],
            [identity(hook_input(tool_use_id="tool-use-2")).decision_id],
        )
        self.assertEqual(self.spool_lines(), [])

    def test_at_most_ten_lines_are_resent_in_one_run(self) -> None:
        self.write_spool([self.spool_line() for _ in range(15)])
        fake = self.fake()
        self.run_hook(hook_input(), fake)
        self.assertEqual([len(call["events"]) for call in fake.resent_calls()], [10])
        self.assertEqual([len(call["events"]) for call in fake.new_calls()], [1])
        self.assertEqual(len(self.spool_lines()), 5)

    def test_a_503_or_401_on_the_drain_keeps_the_lines(self) -> None:
        for status in (401, 503):
            with self.subTest(status=status):
                lines = [self.spool_line() for _ in range(3)]
                self.write_spool(lines)
                fake = self.fake((202, b"{}"), resends=[(status, b"")])
                self.run_hook(hook_input(), fake)
                self.assertEqual(len(fake.calls), 2)
                self.assertEqual(self.spool_lines(), lines)

    def test_a_400_or_403_on_the_drain_removes_the_lines_it_sent(self) -> None:
        for status in (400, 403):
            with self.subTest(status=status):
                lines = [self.spool_line() for _ in range(11)]
                self.write_spool(lines)
                fake = self.fake((202, b"{}"), resends=[(status, b'{"code":"EINVAL"}')])
                self.run_hook(hook_input("PostToolUse"), fake)
                self.assertEqual([len(call["events"]) for call in fake.resent_calls()], [10])
                self.assertEqual(self.spool_lines(), lines[10:])
                self.assertIn(f"HTTP {status}", self.stderr)

    def test_a_207_on_the_drain_finishes_only_the_accepted_lines(self) -> None:
        lines = [self.spool_line() for _ in range(3)]
        self.write_spool(lines)
        body = json.dumps({"events": [{"index": 0, "status": "accepted"}, {"index": 1, "status": "rejected"}]}).encode()
        ledger.drain_ledger_spool(config=ledger.load_ledger_config(), ledger_http=self.fake((207, body)))
        self.assertEqual(self.spool_lines(), lines[1:])
        ledger.drain_ledger_spool(config=ledger.load_ledger_config(), ledger_http=self.fake((207, b"x")))
        self.assertEqual(self.spool_lines(), [])
        self.assertEqual(
            ledger.LEDGER_MESSAGES,
            [
                "Mezmo ledger dropped 2 saved event(s): "
                "the ledger service answered HTTP 207 without usable event statuses",
            ],
        )

    def test_bad_lines_are_removed(self) -> None:
        good = self.spool_line()
        bad = ["not json", "[]", '{"event": 1, "url": "x"}', '{"event": {}, "url": 5}', '{"url": "x"}', ""]
        self.write_spool(bad[:3] + [good] + bad[3:])
        fake = self.fake()
        ledger.drain_ledger_spool(config=ledger.load_ledger_config(), ledger_http=fake)
        self.assertEqual(fake.calls[0]["events"], [json.loads(good)["event"]])
        self.assertEqual(self.spool_lines(), [])

    def test_a_line_for_another_url_is_not_sent_and_is_removed(self) -> None:
        other = self.spool_line(url="http://127.0.0.1:9999/v1/events")
        mine = self.spool_line()
        self.write_spool([other, mine])
        fake = self.fake()
        self.run_hook(hook_input(), fake)
        self.assertEqual([call["events"] for call in fake.resent_calls()], [[json.loads(mine)["event"]]])
        self.assertEqual(self.spool_lines(), [])
        self.assertIn("dropped 1 saved event(s): saved for another URL", self.stderr)

    def test_a_line_claude_code_saved_for_this_url_is_resent(self) -> None:
        # The two plugins share the file, so a line is whatever either saved for this URL.
        saved = self.build("started", tool_use_id="from-claude-code")
        saved["actor"]["pk"] = "claude_code:session-9"
        saved["application"] = {"name": "mezmo-claude-code-plugin", "version": "0.9.0"}
        saved["extra"]["agent"] = "claude_code"
        self.write_spool([self.spool_line(saved)])
        fake = self.fake()
        self.run_hook(hook_input(), fake)
        self.assertEqual([call["events"] for call in fake.resent_calls()], [[saved]])
        self.assertEqual(self.spool_lines(), [])

    def test_lines_over_the_limit_drop_the_oldest(self) -> None:
        self.assertEqual(ledger.LEDGER_SPOOL_MAX_LINES, 200)
        lines = [self.spool_line() for _ in range(ledger.LEDGER_SPOOL_MAX_LINES + 5)]
        self.write_spool(lines)
        ledger.drain_ledger_spool(config=ledger.load_ledger_config(), ledger_http=self.fake((401, b"")))
        self.assertEqual(self.spool_lines(), lines[5:])
        self.assertEqual(
            ledger.LEDGER_MESSAGES,
            [f"Mezmo ledger dropped 5 saved event(s): the retry file keeps at most {ledger.LEDGER_SPOOL_MAX_LINES}"],
        )

    def test_saving_lines_over_the_limit_drops_the_oldest(self) -> None:
        self.assertEqual(ledger.LEDGER_SPOOL_MAX_LINES, 200)
        lines = [self.spool_line() for _ in range(ledger.LEDGER_SPOOL_MAX_LINES)]
        self.write_spool(lines)
        config = ledger.load_ledger_config()
        late = self.build("started", tool_use_id="late")

        ledger.save_ledger_events(config=config, events=[late])

        self.assertEqual(self.spool_lines(), lines[1:] + [self.spool_line(late)])
        self.assertEqual(
            ledger.LEDGER_MESSAGES,
            [f"Mezmo ledger dropped 1 saved event(s): the retry file keeps at most {ledger.LEDGER_SPOOL_MAX_LINES}"],
        )

    def test_a_file_at_the_limit_is_not_rewritten(self) -> None:
        lines = [self.spool_line() for _ in range(ledger.LEDGER_SPOOL_MAX_LINES)]
        self.write_spool(lines)
        before = self.spool.stat().st_mtime_ns
        ledger.drain_ledger_spool(config=ledger.load_ledger_config(), ledger_http=self.fake((503, b"")))
        self.assertEqual(self.spool_lines(), lines)
        self.assertEqual(self.spool.stat().st_mtime_ns, before)
        self.assertEqual(ledger.LEDGER_MESSAGES, [])

    def test_a_line_appended_during_the_request_survives_the_rewrite(self) -> None:
        self.write_spool([self.spool_line()])
        config = ledger.load_ledger_config()
        late = self.build("started", tool_use_id="late")
        fake = self.fake()
        fake.during = lambda: ledger._spool_ledger_events(config=config, events=[late])
        ledger.drain_ledger_spool(config=config, ledger_http=fake)
        self.assertEqual(self.spooled(), [late])

    def test_the_drain_is_skipped_while_another_hook_holds_its_lock(self) -> None:
        try:
            import fcntl
        except ImportError:
            self.skipTest("no fcntl")
        self.write_spool([self.spool_line()])
        with open(f"{self.spool}.drain.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fake = self.fake()
            ledger.drain_ledger_spool(config=ledger.load_ledger_config(), ledger_http=fake)
            self.assertEqual(fake.calls, [])
        self.assertEqual(len(self.spool_lines()), 1)
        ledger.drain_ledger_spool(config=ledger.load_ledger_config(), ledger_http=fake)
        self.assertEqual(len(fake.calls), 1)

    @unittest.skipIf(os.name == "nt", "POSIX modes")
    def test_the_file_is_private_after_an_append_and_a_rewrite(self) -> None:
        self.run_hook(hook_input(), self.fake(OSError("refused")))
        self.assertEqual(stat.S_IMODE(self.spool.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(self.spool.parent.stat().st_mode), 0o700)
        os.chmod(self.spool, 0o644)
        self.run_hook(hook_input(tool_use_id="tool-use-2"), self.fake(OSError("refused"), resends=[(503, b"")]))
        self.assertEqual(stat.S_IMODE(self.spool.stat().st_mode), 0o600)
        self.write_spool(["not json", self.spool_line()])
        os.chmod(self.spool, 0o644)
        ledger.drain_ledger_spool(config=ledger.load_ledger_config(), ledger_http=self.fake())
        self.assertEqual(self.spool_lines(), [])
        self.assertEqual(stat.S_IMODE(self.spool.stat().st_mode), 0o600)


class DeadlineTests(LedgerTestCase):
    """ledger.handle: one deadline for the new event and the resend, sent in parallel."""

    def test_the_new_event_and_the_resend_are_in_flight_together(self) -> None:
        self.write_spool([self.spool_line()])
        lock = threading.Lock()
        arrived: list[int] = []
        together = threading.Event()
        overlapped: list[bool] = []

        def answer() -> tuple[int, bytes]:
            with lock:
                arrived.append(1)
                if len(arrived) == 2:
                    together.set()
            overlapped.append(together.wait(0.5))
            return 202, b"{}"

        fake = self.fake(answer, resends=[answer])
        self.run_hook(hook_input("PostToolUse"), fake)
        self.assertEqual(overlapped, [True, True])
        self.assertEqual((len(fake.new_calls()), len(fake.resent_calls())), (1, 1))
        self.assertEqual(self.spool_lines(), [])

    def test_a_hanging_resend_does_not_hold_up_the_new_event(self) -> None:
        self.timeout(0.3)
        lines = [self.spool_line() for _ in range(2)]
        self.write_spool(lines)
        fake = self.fake((202, b"{}"), resends=["hang"])
        began = time.monotonic()
        self.run_hook(hook_input("PostToolUse"), fake)
        # Back by the deadline, with room for a busy machine, not after the 30 s hang.
        self.assertLess(time.monotonic() - began, ledger.LEDGER_TIMEOUT_SECONDS + 2)
        self.assertEqual([call["events"][0]["event"] for call in fake.new_calls()], ["agent.tool_call.ran"])
        self.assertEqual(self.spool_lines(), lines)

    def test_a_hanging_ledger_call_saves_the_event_by_the_deadline(self) -> None:
        self.timeout(0.2)
        fake = self.fake("hang", "hang")
        began = time.monotonic()
        self.assertIsNone(self.run_hook(hook_input(), fake))
        self.assertLess(time.monotonic() - began, 2)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual([event["event"] for event in self.spooled()], ["agent.tool_call.started"])

    def test_a_send_that_fails_after_the_deadline_leaves_one_line(self) -> None:
        self.timeout(0.2)
        late = threading.Event()

        def fail_late() -> Any:
            late.wait(5)
            raise OSError("timed out")

        self.run_hook(hook_input(), self.fake(fail_late))
        late.set()
        for thread in ledger.LEDGER_THREADS:
            thread.join(5)
        self.assertEqual([event["event"] for event in self.spooled()], ["agent.tool_call.started"])


class SafetyTests(LedgerTestCase):
    def ledger_failures(self) -> list[Any]:
        """Patches that make one part of the ledger raise."""
        return [
            patch.object(ledger, "load_ledger_config", side_effect=RuntimeError("odd")),
            patch.object(ledger, "ledger_event", side_effect=RuntimeError("build failed")),
            patch.object(ledger, "_spool_ledger_events", side_effect=OSError("disk full")),
            patch.object(ledger, "start_ledger_thread", side_effect=RuntimeError("no threads")),
        ]

    def test_the_hook_returns_normally_whatever_fails(self) -> None:
        for failure in (OSError("refused"), RuntimeError("boom"), ValueError("bad"), (500, b""), (400, b"")):
            for name in ("PreToolUse", "PostToolUse"):
                with self.subTest(failure=failure, name=name):
                    self.assertIsNone(self.run_hook(hook_input(name), self.fake(failure, failure)))
        for failure in self.ledger_failures():
            for name in ("PreToolUse", "PostToolUse"):
                with self.subTest(target=failure.attribute, name=name), failure:
                    self.assertIsNone(self.run_hook(hook_input(name), self.fake(OSError("refused"))))

    def test_an_event_is_saved_when_no_thread_can_start(self) -> None:
        with patch.object(ledger, "start_ledger_thread", side_effect=RuntimeError("no threads")):
            self.run_hook(hook_input(), self.fake())
        self.assertEqual([event["event"] for event in self.spooled()], ["agent.tool_call.started"])
        self.assertIn("no threads", self.stderr)

    def test_a_stderr_that_raises_cannot_stop_the_hook(self) -> None:
        class Broken(StringIO):
            def write(self, text: str) -> int:
                raise OSError("stderr is gone")

        failures = [(None, None), (ledger, "ledger_event"), (ledger, "start_ledger_thread")]
        for module, target in failures:
            failure = contextlib.nullcontext() if module is None else patch.object(module, target, side_effect=RuntimeError("boom"))
            with self.subTest(target=target), failure, patch.object(sys, "stderr", Broken()):
                for name in ("PreToolUse", "PostToolUse"):
                    self.assertIsNone(ledger.handle(hook_input(name), ledger_http=self.fake((400, b"{}"))))
                self.assertFalse(
                    ledger.record_governance_refusal(
                        hook_input=hook_input(), ledger_http=self.fake((400, b"{}")), outcome="denied", reason="No."
                    )
                )

    def test_only_main_and_end_governance_refusal_end_the_process(self) -> None:
        tree = ast.parse(LEDGER_PATH.read_text(encoding="utf-8"))
        exits = []
        for node in tree.body:
            for child in ast.walk(node):
                if isinstance(child, ast.Attribute) and child.attr == "_exit":
                    exits.append(getattr(node, "name", None))
                if isinstance(child, ast.Name) and child.id in ("exit", "quit"):
                    exits.append(getattr(node, "name", None))
                if isinstance(child, ast.Attribute) and child.attr == "exit":
                    exits.append(getattr(node, "name", None))
        # main() and end_governance_refusal each arm a timer that calls os._exit and call it themselves.
        self.assertEqual(set(exits), {"end_governance_refusal", "main"})

    def test_only_these_names_come_from_the_governance_hook(self) -> None:
        tree = ast.parse(LEDGER_PATH.read_text(encoding="utf-8"))
        found = 0
        for node in tree.body:
            if isinstance(node, ast.ImportFrom) and node.module == "mezmo_governance_hook":
                found += 1
                # Nothing the plan leaves out: not the zero-argument authorization_header, and
                # not result_has_known_error.
                self.assertEqual(
                    sorted(alias.name for alias in node.names),
                    sorted([
                        "AGENT", "HookContractError", "RefuseRedirect", "call_identity", "normalize_tool_call",
                        "refuse_exposed_token",
                    ]),
                )
        self.assertEqual(found, 1)


class RefusedTests(LedgerTestCase):
    """ledger.record_governance_refusal, which governance calls once it has printed its deny."""

    def refuse(self, fake: Any = None, outcome: str = "denied", reason: str | None = "Use a\n canary first.") -> bool:
        """record_governance_refusal for the default Bash call, its stderr kept in self.stderr."""
        with patch.object(sys, "stderr", StringIO()) as stderr, patch.object(sys, "stdout", StringIO()) as stdout:
            alive = ledger.record_governance_refusal(hook_input=hook_input(), ledger_http=fake, outcome=outcome, reason=reason)
        self.stderr = stderr.getvalue()
        self.assertEqual(stdout.getvalue(), "")
        self.assertIsInstance(alive, bool)
        return alive

    def test_refused_sends_not_run_with_outcome_and_reason(self) -> None:
        cases = [
            ("denied", "Use a\n canary first.", {"outcome": "denied", "reason": "Use a canary first."},
             "codex.Bash: ls -la: not run - Use a canary first."),
            ("error", "Mezmo Governance: approval still pending",
             {"outcome": "error", "reason": "Mezmo Governance: approval still pending"},
             "codex.Bash: ls -la: not run - Mezmo Governance: approval still pending"),
            ("error", None, {"outcome": "error"}, "codex.Bash: ls -la: not run"),
        ]
        for outcome, reason, output, message in cases:
            with self.subTest(outcome=outcome, reason=reason):
                fake = self.fake((202, b'{"accepted":1}'))
                self.assertFalse(self.refuse(fake, outcome=outcome, reason=reason))
                self.assertEqual(len(fake.calls), 1)
                (event,) = fake.events()
                self.assertEqual(event["event"], "agent.tool_call.not_run")
                self.assertEqual(json.loads(event["extra"]["output"]), output)
                self.assertEqual(event["message"], message)
                self.assertEqual(event["extra"]["call_id"], identity(hook_input()).decision_id)
                self.assertEqual(event["extra"]["entry_key"], event["extra"]["call_id"] + ":end")
                self.assertNotIn("arguments", event["extra"])
                self.assertNotIn("exit_status", event["extra"])
                self.assertEqual(self.spool_lines(), [])
                self.assertEqual(self.stderr, "")

    def test_refused_sends_on_a_daemon_thread_with_the_refused_timeout(self) -> None:
        threads: list[threading.Thread] = []
        fake = self.fake(lambda: threads.append(threading.current_thread()) or (202, b"{}"))
        with patch.object(ledger, "REFUSED_TIMEOUT_SECONDS", 0.25):
            self.refuse(fake)
        self.refuse(fake)
        self.assertEqual([call["timeout"] for call in fake.calls], [0.25, 2.0])
        self.assertEqual(ledger.REFUSED_TIMEOUT_SECONDS, 2.0)
        (thread,) = threads
        self.assertIsNot(thread, threading.current_thread())
        self.assertTrue(thread.daemon)

    def assert_saved_once(self) -> None:
        (line,) = self.spool_lines()
        entry = json.loads(line)
        self.assertEqual(entry["url"], LEDGER_URL)
        self.assertEqual(entry["event"]["event"], "agent.tool_call.not_run")
        self.assertEqual(json.loads(entry["event"]["extra"]["output"]), {"outcome": "denied", "reason": "Use a canary first."})

    def test_refused_saves_what_was_not_sent(self) -> None:
        for answer in ((503, b""), (401, b""), OSError("refused"), socket.timeout("timed out")):
            with self.subTest(answer=answer):
                self.spool.unlink(missing_ok=True)
                fake = self.fake(answer)
                self.assertFalse(self.refuse(fake))
                self.assertEqual(len(fake.calls), 1)
                self.assert_saved_once()
                self.assertEqual(self.stderr, "")

    def test_a_send_that_hangs_is_left_after_the_wait_and_saved_once(self) -> None:
        fake = self.fake("hang")
        with patch.object(ledger, "REFUSED_TIMEOUT_SECONDS", 0.2):
            began = time.monotonic()
            alive = self.refuse(fake)
            elapsed = time.monotonic() - began
        self.assertTrue(alive)
        self.assertGreaterEqual(elapsed, 0.2)
        self.assertLess(elapsed, 2)
        self.assert_saved_once()
        # The send the hook gave up on may still arrive, and nothing saves it a second time.
        fake.release.set()
        for thread in ledger.LEDGER_THREADS:
            thread.join(5)
        self.assert_saved_once()

    def test_refused_saves_the_event_when_no_thread_can_start(self) -> None:
        fake = self.fake()
        with patch.object(ledger, "start_ledger_thread", side_effect=RuntimeError("no threads")):
            self.assertFalse(self.refuse(fake))
        self.assertEqual(fake.calls, [])
        self.assert_saved_once()
        self.assertIn("no threads", self.stderr)

    def test_refused_drops_on_400_and_403(self) -> None:
        for status in (400, 403):
            with self.subTest(status=status):
                self.assertFalse(self.refuse(self.fake((status, b'{"code":"EINVAL"}'))))
                self.assertEqual(self.spool_lines(), [])
                self.assertEqual(len(self.stderr.splitlines()), 1)
                self.assertIn(f"HTTP {status}", self.stderr)

    def test_refused_is_a_no_op_when_unconfigured(self) -> None:
        for missing in ("MEZMO_LEDGER_URL", "MEZMO_LEDGER_ACCOUNT"):
            with self.subTest(missing=missing), patch.dict(os.environ, {missing: " "}):
                fake = self.fake()
                self.assertFalse(self.refuse(fake))
                self.assertEqual(fake.calls, [])
                self.assertFalse(self.spool.parent.exists())
                self.assertEqual(self.stderr, "")
        self.assertEqual(ledger.LEDGER_THREADS, [])

    def test_refused_without_an_identity_sends_nothing(self) -> None:
        fake = self.fake()
        with patch.object(sys, "stderr", StringIO()):
            alive = ledger.record_governance_refusal(
                hook_input=hook_input(tool_use_id=DROP), ledger_http=fake, outcome="error", reason="tool_use_id must be a non-empty string"
            )
        self.assertFalse(alive)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.spool_lines(), [])

    def test_refused_never_raises(self) -> None:
        fake = self.fake()
        with patch.object(ledger, "ledger_event", side_effect=RuntimeError("build failed")):
            self.assertFalse(self.refuse(fake))
        self.assertEqual(fake.calls, [])
        self.assertEqual(len(self.stderr.splitlines()), 1)
        self.assertIn("build failed", self.stderr)
        with patch.object(ledger, "post_ledger_events", side_effect=RuntimeError("boom")) as post:
            self.assertFalse(self.refuse())
        self.assertEqual(post.call_count, 1)
        self.assertEqual(len(self.stderr.splitlines()), 1)
        self.assertIn("boom", self.stderr)
        with patch.object(ledger, "_spool_ledger_events", side_effect=OSError("disk full")):
            self.assertFalse(self.refuse(self.fake(OSError("refused"))))
        self.assertIn("disk full", self.stderr)
        self.assertEqual(self.spool_lines(), [])
        self.assertEqual(ledger.LEDGER_MESSAGES, [])

    def test_refused_does_not_drain(self) -> None:
        line = self.spool_line()
        self.write_spool([line])
        fake = self.fake()
        self.assertFalse(self.refuse(fake))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(fake.resent_calls(), [])
        self.assertEqual(fake.events()[0]["event"], "agent.tool_call.not_run")
        self.assertEqual(self.spool_lines(), [line])


class Stream(StringIO):
    """A stdout or stderr that notes each flush in `steps`."""

    def __init__(self, label: str, steps: list[Any]) -> None:
        super().__init__()
        self.label = label
        self.steps = steps

    def flush(self) -> None:
        self.steps.append(f"flush {self.label}")
        super().flush()


class EndGovernanceRefusalTests(LedgerTestCase):
    """ledger.end_governance_refusal once the deny is printed: os._exit is patched and
    record_governance_refusal is a stand-in.
    """

    def end(self, *, record: Any = None, stderr: Stream | None = None, stdout: Stream | None = None) -> tuple[Any, str, str, Any]:
        """end_governance_refusal on a denied call. Returns what it returned, or the exception it raised."""
        self.steps: list[Any] = []
        self.calls: list[dict[str, Any]] = []

        def stand_in(**kwargs: Any) -> Any:
            self.steps.append("ledger")
            self.calls.append(
                {
                    **kwargs,
                    "stdout_is_stderr": sys.stdout is sys.stderr,
                    "timers": [
                        (thread.daemon, thread.interval)
                        for thread in threading.enumerate()
                        if isinstance(thread, threading.Timer)
                    ],
                }
            )
            return record(**kwargs) if record is not None else False

        out = stdout or Stream("stdout", self.steps)
        with patch.object(ledger, "record_governance_refusal", side_effect=stand_in), \
                patch.object(ledger.os, "_exit", side_effect=lambda code: self.steps.append(f"exit {code}")) as exit_now, \
                patch.object(sys, "stdout", out), \
                patch.object(sys, "stderr", stderr or Stream("stderr", self.steps)) as err:
            try:
                answer: Any = ledger.end_governance_refusal(hook_input=hook_input(), outcome="denied", reason="No.")
            except BaseException as error:  # noqa: BLE001 - the test asserts on it
                answer = error
        return answer, out.getvalue(), err.getvalue(), exit_now

    def test_the_deny_is_flushed_before_the_ledger_is_told(self) -> None:
        answer, stdout, stderr, exit_now = self.end()

        self.assertEqual((answer, stdout, stderr), (None, "", ""))
        self.assertEqual(self.steps[:2], ["flush stdout", "ledger"])
        (call,) = self.calls
        self.assertEqual(call["hook_input"], hook_input())
        self.assertEqual((call["outcome"], call["reason"]), ("denied", "No."))
        # Nothing the ledger prints can reach stdout, and a daemon timer bounds the whole call.
        self.assertTrue(call["stdout_is_stderr"])
        self.assertIn((True, ledger.LEDGER_REFUSAL_BUDGET_SECONDS), call["timers"])
        # Then stderr is flushed and the process ends, so the interpreter's exit can't change the code.
        self.assertEqual(self.steps, ["flush stdout", "ledger", "flush stderr", "exit 0"])
        exit_now.assert_called_once_with(0)

    def test_what_the_ledger_prints_goes_to_stderr(self) -> None:
        def noisy(**kwargs: Any) -> bool:
            print("ledger noise")
            sys.stdout.write("more noise\n")
            return False

        _, stdout, stderr, _ = self.end(record=noisy)

        self.assertEqual(stdout, "")
        self.assertEqual(stderr, "ledger noise\nmore noise\n")

    def test_the_timer_is_stopped_once_the_ledger_returns(self) -> None:
        with patch.object(ledger, "LEDGER_REFUSAL_BUDGET_SECONDS", 0.05):
            _, _, _, exit_now = self.end()
            time.sleep(0.3)

        # Only its own exit: the timer did not fire.
        exit_now.assert_called_once_with(0)
        self.assertEqual(
            [
                thread
                for thread in threading.enumerate()
                if isinstance(thread, threading.Timer) and thread.interval == 0.05
            ],
            [],
        )

    def test_the_timer_ends_the_process_with_the_deny_exit_code_when_the_ledger_overruns(self) -> None:
        with patch.object(ledger, "LEDGER_REFUSAL_BUDGET_SECONDS", 0.05):
            self.end(record=lambda **kwargs: time.sleep(0.5))

        # The real os._exit ends the process there. The patched one lets it carry on to its own.
        self.assertEqual(self.steps, ["flush stdout", "ledger", "exit 0", "flush stderr", "exit 0"])

    def test_without_a_timer_the_ledger_is_not_told(self) -> None:
        with patch.object(ledger.threading, "Timer", side_effect=RuntimeError("can't start new thread")):
            answer, _, _, exit_now = self.end()

        self.assertIsNone(answer)
        self.assertEqual(self.calls, [])
        exit_now.assert_not_called()

    def test_a_stderr_flush_that_fails_still_ends_with_the_deny_exit_code(self) -> None:
        class Broken(Stream):
            def flush(self) -> None:
                super().flush()
                raise BrokenPipeError("stderr closed")

        flushed: list[Any] = []
        _, _, _, exit_now = self.end(stderr=Broken("stderr", flushed))

        self.assertEqual(flushed, ["flush stderr"])
        exit_now.assert_called_once_with(0)

    def test_with_no_stdout_the_ledger_is_not_told(self) -> None:
        # Python sets sys.stdout to None when the hook starts with no fd 1. The deny reached nobody,
        # so Codex runs the call, and the governance hook returns 0 as it does with the ledger off.
        with patch.object(ledger, "record_governance_refusal") as record, \
                patch.object(ledger.os, "_exit") as exit_now, \
                patch.object(sys, "stdout", None), \
                patch.object(sys, "stderr", StringIO()) as err:
            ledger.end_governance_refusal(hook_input=hook_input(), outcome="denied", reason="No.")

        self.assertEqual(err.getvalue(), "")
        record.assert_not_called()
        exit_now.assert_not_called()

    def test_a_flush_that_fails_propagates_and_skips_the_ledger(self) -> None:
        class Closed(Stream):
            def flush(self) -> None:
                raise BrokenPipeError("stdout closed")

        answer, _, _, exit_now = self.end(stdout=Closed("stdout", []))

        self.assertIsInstance(answer, BrokenPipeError)
        self.assertEqual(self.calls, [])
        exit_now.assert_not_called()

    def test_the_ledger_is_left_alone_while_either_setting_is_unset(self) -> None:
        for name in ("MEZMO_LEDGER_ACCOUNT", "MEZMO_LEDGER_URL"):
            for value in (None, "  "):
                with self.subTest(name=name, value=value), patch.dict(os.environ):
                    if value is None:
                        del os.environ[name]
                    else:
                        os.environ[name] = value
                    answer, stdout, stderr, exit_now = self.end()
                    self.assertEqual((answer, stdout, stderr), (None, "", ""))
                    self.assertEqual(self.calls, [])
                    # Exactly as before the ledger existed: no flush, no timer, no exit.
                    self.assertEqual(self.steps, [])
                    exit_now.assert_not_called()


class GovernanceMainTests(LedgerTestCase):
    """mezmo_governance_hook.main() in this process with the real ledger behind it. os._exit is patched."""

    def run_main(self, event: Mapping[str, Any], fake: Any) -> tuple[int, str, str, Any]:
        with patch.object(ledger, "post_ledger_events", fake), \
                patch.object(sys, "stdin", StringIO(json.dumps(event))), \
                patch.object(sys, "stdout", StringIO()) as stdout, \
                patch.object(sys, "stderr", StringIO()) as stderr, \
                patch.object(os, "_exit") as exit_now:
            code = hook.main()
        return code, stdout.getvalue(), stderr.getvalue(), exit_now

    def test_a_deny_reaches_the_ledger_as_not_run_and_stdout_is_only_the_deny(self) -> None:
        os.environ.update({**GOVERNANCE, "MEZMO_GOVERNANCE_URL": serve_governance_deny(self)})
        for answer in ((202, b"{}"), (400, b"{}"), OSError("refused"), RuntimeError("boom")):
            with self.subTest(answer=answer):
                fake = self.fake(answer)
                code, stdout, _, exit_now = self.run_main(hook_input(), fake)
                self.assertEqual((code, stdout.encode()), (0, DENY_STDOUT))
                # Every refusal the ledger is told of ends in os._exit, with the deny's exit code.
                exit_now.assert_called_once_with(0)
                (not_run,) = fake.events()
                self.assertEqual(not_run["event"], "agent.tool_call.not_run")
                self.assertEqual(json.loads(not_run["extra"]["output"]), {"outcome": "denied", "reason": DENY_REASON})
                self.assertEqual(not_run["extra"]["decision_id"], identity(hook_input()).decision_id)
                self.assertEqual(fake.calls[0]["key"], "sts_test")

    def test_an_error_reaches_the_ledger_as_not_run(self) -> None:
        os.environ.update({**GOVERNANCE, "MEZMO_GOVERNANCE_URL": closed_port_url()})
        fake = self.fake()
        code, stdout, _, exit_now = self.run_main(hook_input(), fake)
        self.assertEqual(code, 0)
        reason = json.loads(stdout)["hookSpecificOutput"]["permissionDecisionReason"]
        self.assertIn("could not authorize this exact tool call", reason)
        exit_now.assert_called_once_with(0)
        (not_run,) = fake.events()
        output = json.loads(not_run["extra"]["output"])
        self.assertEqual(output["outcome"], "error")
        self.assertIn("Mezmo Governance is unavailable", output["reason"])
        self.assertTrue(reason.endswith(output["reason"]))

    def test_a_send_still_running_ends_the_process_after_the_deny(self) -> None:
        os.environ.update({**GOVERNANCE, "MEZMO_GOVERNANCE_URL": serve_governance_deny(self)})
        fake = self.fake("hang")
        with patch.object(ledger, "REFUSED_TIMEOUT_SECONDS", 0.2):
            code, stdout, _, exit_now = self.run_main(hook_input(), fake)
        self.assertEqual((code, stdout.encode()), (0, DENY_STDOUT))
        exit_now.assert_called_once_with(0)
        self.assertEqual([event["event"] for event in self.spooled()], ["agent.tool_call.not_run"])

    def test_an_allowed_call_and_a_result_send_nothing_from_governance(self) -> None:
        os.environ.update(GOVERNANCE)
        fake = self.fake()
        with patch.object(ledger, "post_ledger_events", fake), patch.object(sys, "stderr", StringIO()):
            self.assertIsNone(hook.handle_event(hook_input(), http=GovernanceHttp((200, {"approved": True}))))
            self.assertIsNone(hook.handle_event(hook_input("PostToolUse")))
        self.assertEqual(fake.calls, [])
        self.assertEqual(ledger.LEDGER_THREADS, [])


class LedgerHookMainTests(LedgerTestCase):
    """ledger.main() in this process, with stdin and os._exit patched."""

    def run_main(self, *, order: list[str] | None = None, stdin: str) -> tuple[int, str, str, Any]:
        """main() on `stdin`. `order`, when given, collects each flush and the os._exit call in turn."""

        class Stream(StringIO):
            def __init__(self, label: str) -> None:
                super().__init__()
                self.label = label

            def flush(self) -> None:
                if order is not None:
                    order.append(self.label)
                super().flush()

        def exit_now(code: int) -> None:
            if order is not None:
                order.append(f"exit {code}")

        with patch.object(sys, "stdin", StringIO(stdin)), \
                patch.object(sys, "stdout", Stream("stdout")) as stdout, \
                patch.object(sys, "stderr", Stream("stderr")) as stderr, \
                patch.object(os, "_exit", side_effect=exit_now) as exit_mock:
            code = ledger.main()
        return code, stdout.getvalue(), stderr.getvalue(), exit_mock

    def test_main_exits_0_on_a_body_that_is_not_an_object(self) -> None:
        for body in ("[1]", '"text"', "null", "not json", ""):
            with self.subTest(body=body):
                code, stdout, stderr, exit_now = self.run_main(stdin=body)
                self.assertEqual(code, 0)
                self.assertEqual(stdout, "")
                self.assertEqual(len(stderr.splitlines()), 1)
                exit_now.assert_not_called()

    def test_main_exits_0_when_handle_raises(self) -> None:
        with patch.object(ledger, "handle", side_effect=RuntimeError("boom")):
            code, stdout, stderr, exit_now = self.run_main(stdin=json.dumps(hook_input()))
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        self.assertIn("boom", stderr)
        exit_now.assert_not_called()

    def test_main_ends_the_process_while_a_thread_runs(self) -> None:
        self.timeout(0.2)
        fake = self.fake("hang")
        order: list[str] = []
        with patch.object(ledger, "post_ledger_events", fake):
            code, stdout, _, exit_now = self.run_main(order=order, stdin=json.dumps(hook_input()))
        exit_now.assert_called_once_with(0)
        self.assertEqual(order[-3:], ["stdout", "stderr", "exit 0"])
        self.assertEqual((code, stdout), (0, ""))
        self.assertEqual(len(fake.calls), 1)

    def test_main_arms_a_daemon_timer_for_the_whole_run_and_stops_it_at_the_end(self) -> None:
        # A budget the sleep below outlasts, so a timer left running would call os._exit.
        self.timeout(0.05)
        armed: list[tuple[bool, float]] = []

        def timers() -> None:
            armed.extend(
                (thread.daemon, thread.interval) for thread in threading.enumerate() if isinstance(thread, threading.Timer)
            )

        with patch.object(ledger, "handle", side_effect=lambda event: timers()), \
                patch.object(ledger, "LEDGER_EXIT_MARGIN_SECONDS", 0.05):
            code, stdout, _, exit_now = self.run_main(stdin=json.dumps(hook_input()))
            time.sleep(0.3)

        self.assertIn((True, 0.1), armed)
        self.assertEqual((code, stdout), (0, ""))
        exit_now.assert_not_called()

    def test_main_sends_nothing_without_a_timer(self) -> None:
        # With no timer the hook's time has no bound.
        with patch.object(ledger.threading, "Timer", side_effect=RuntimeError("can't start new thread")), \
                patch.object(ledger, "handle") as handle:
            code, stdout, stderr, exit_now = self.run_main(stdin=json.dumps(hook_input()))

        handle.assert_not_called()
        self.assertEqual((code, stdout), (0, ""))
        self.assertIn("can't start new thread", stderr)
        exit_now.assert_not_called()

    def test_main_prints_no_hook_output_and_returns_when_no_thread_is_alive(self) -> None:
        fake = self.fake()
        with patch.object(ledger, "post_ledger_events", fake):
            for builder in (hook_input, patch_input, mcp_input):
                for name in ("PreToolUse", "PostToolUse"):
                    with self.subTest(builder=builder.__name__, name=name):
                        code, stdout, stderr, exit_now = self.run_main(stdin=json.dumps(builder(name)))
                        self.assertEqual((code, stdout, stderr), (0, "", ""))
                        exit_now.assert_not_called()
        self.assertEqual(len(fake.new_calls()), 6)
        self.assertFalse(any(thread.is_alive() for thread in ledger.LEDGER_THREADS))


class ScriptTestCase(unittest.TestCase):
    """The real scripts, copied side by side into a temp folder and run as Codex runs them."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = Path(scratch.name)
        self.hooks = self.dir / "hooks"
        self.hooks.mkdir()
        for path in (HOOK_PATH, LEDGER_PATH):
            shutil.copy(path, self.hooks / path.name)
        self.spool = self.dir / "spool" / "ledger-spool.jsonl"
        self.recorder = Recorder()
        # Built from nothing, so no setting of the developer's own reaches the scripts. The GIT_
        # settings keep the developer's git email out too: no system config, and no repository.
        self.env = {
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_DIR": str(self.dir / "no-git"),
            "HOME": str(self.dir),
            "MEZMO_GOVERNANCE_AUDIT_LOG": str(self.dir / "audit.jsonl"),
            "MEZMO_GOVERNANCE_STATE_DIR": str(self.dir / "codex-sessions"),
            "MEZMO_LEDGER_ACCOUNT": ACCOUNT,
            "MEZMO_LEDGER_SPOOL": str(self.spool),
            "MEZMO_LEDGER_URL": serve(self.recorder, self),
            "NO_PROXY": "*",
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }

    def run_script(self, event: Any, script: str = "mezmo_ledger_hook.py") -> subprocess.CompletedProcess[bytes]:
        """Run `script` on `event`: a hook input, or the raw text of the body. Keeps the time in self.elapsed."""
        began = time.monotonic()
        result = subprocess.run(
            [sys.executable, str(self.hooks / script)],
            capture_output=True,
            check=False,
            env=self.env,
            input=(event if isinstance(event, str) else json.dumps(event)).encode(),
            timeout=30,
        )
        self.elapsed = time.monotonic() - began
        return result

    def govern(self, *, delay: float = 0.0) -> None:
        """Point the governance hook at a stand-in that denies every call."""
        self.env.update({"MEZMO_GOVERNANCE_TOKEN": "local-test", "MEZMO_GOVERNANCE_URL": serve_governance_deny(self, delay=delay)})

    def events(self) -> list[dict[str, Any]]:
        return [event for request in self.recorder.requests for event in request["body"]]

    def saved(self) -> list[dict[str, Any]]:
        if not self.spool.exists():
            return []
        return [json.loads(line)["event"] for line in self.spool.read_text(encoding="utf-8").splitlines()]


class EndToEndTests(ScriptTestCase):
    def test_started_and_ran_reach_the_server(self) -> None:
        for builder in (hook_input, patch_input, mcp_input):
            for name in ("PreToolUse", "PostToolUse"):
                result = self.run_script(builder(name))
                self.assertEqual((result.returncode, result.stdout), (0, b""), result.stderr)
        for request in self.recorder.requests:
            self.assertIsInstance(request["body"], list)
            self.assertEqual(request["path"], "/v1/events")
            self.assertNotIn("Authorization", request["headers"])
        events = self.events()
        self.assertEqual([event["event"].rsplit(".", 1)[1] for event in events], ["started", "ran"] * 3)
        self.assertEqual([event["extra"].get("exit_status") for event in events], [None, None, None, 0, None, 0])
        for started, ran in zip(events[::2], events[1::2]):
            self.assertEqual(started["extra"]["call_id"], ran["extra"]["call_id"])
        self.assertFalse(self.spool.exists())
        # The state file is only read.
        self.assertFalse((self.dir / "codex-sessions").exists())

    def test_events_saved_on_a_503_are_delivered_by_a_later_run(self) -> None:
        self.recorder.status = 503
        self.run_script(hook_input("PreToolUse"))
        self.run_script(hook_input("PostToolUse"))
        saved = self.saved()
        self.assertEqual([event["event"] for event in saved], ["agent.tool_call.started", "agent.tool_call.ran"])
        self.recorder.status = 202
        self.recorder.requests.clear()
        result = self.run_script(hook_input("PreToolUse", tool_use_id="tool-use-2"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(saved, [request["body"] for request in self.recorder.requests])
        self.assertEqual(len(self.recorder.requests), 2)
        self.assertEqual(self.spool.read_text(encoding="utf-8"), "")

    def test_a_server_that_never_answers_costs_at_most_the_deadline(self) -> None:
        self.recorder.hang = True
        result = self.run_script(hook_input("PreToolUse"))
        self.assertEqual((result.returncode, result.stdout), (0, b""), result.stderr)
        self.assertLess(self.elapsed, ledger.LEDGER_TIMEOUT_SECONDS + 2)
        self.assertEqual([event["event"] for event in self.saved()], ["agent.tool_call.started"])

    def test_input_it_cannot_read_still_exits_0_with_nothing_on_stdout(self) -> None:
        depth = 200_000
        for body in ("not json", "[1]", "", '{"hook_event_name": "PreToolUse", "nested": ' + "[" * depth + "]" * depth + "}"):
            with self.subTest(body=body[:40]):
                result = self.run_script(body)
                self.assertEqual((result.returncode, result.stdout), (0, b""), result.stderr)
                self.assertNotIn(b"Traceback", result.stderr)
        self.assertEqual(self.recorder.requests, [])

    def test_unset_settings_exit_at_once_without_a_request(self) -> None:
        for name in ("MEZMO_LEDGER_ACCOUNT", "MEZMO_LEDGER_SPOOL", "MEZMO_LEDGER_URL"):
            del self.env[name]
        for name in ("PreToolUse", "PostToolUse"):
            with self.subTest(name=name):
                result = self.run_script(hook_input(name))
                self.assertLess(self.elapsed, ledger.LEDGER_TIMEOUT_SECONDS)
                self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertEqual(self.recorder.requests, [])
        self.assertFalse((self.dir / ".mezmo").exists())

    def test_a_governance_deny_reaches_the_ledger_as_not_run(self) -> None:
        self.govern()
        result = self.run_script(hook_input("PreToolUse"), script="mezmo_governance_hook.py")
        self.assertEqual((result.returncode, result.stdout), (0, DENY_STDOUT), result.stderr)
        (not_run,) = self.events()
        self.assertEqual(not_run["event"], "agent.tool_call.not_run")
        self.assertEqual(json.loads(not_run["extra"]["output"]), {"outcome": "denied", "reason": DENY_REASON})
        self.assertEqual(not_run["actor"], {"account": ACCOUNT, "pk": "codex:session-1", "type": "agent"})
        # With no MEZMO_LEDGER_KEY, the governance token is what the ledger is sent.
        self.assertEqual(self.recorder.requests[0]["headers"].get("Authorization"), "Token local-test")
        self.assertFalse(self.spool.exists())
        # The ledger hook, run beside governance, sends `started` for the same call.
        result = self.run_script(hook_input("PreToolUse"))
        self.assertEqual((result.returncode, result.stdout), (0, b""), result.stderr)
        _, started = self.events()
        self.assertEqual(started["event"], "agent.tool_call.started")
        self.assertEqual(started["extra"]["call_id"], not_run["extra"]["call_id"])

    # PYTHONSAFEPATH keeps a script's folder off sys.path, where each script finds the other.
    @unittest.skipIf(sys.version_info < (3, 11), "PYTHONSAFEPATH is new in Python 3.11")
    def test_the_ledger_hook_finds_the_governance_hook_with_pythonsafepath_set(self) -> None:
        self.env["PYTHONSAFEPATH"] = "1"

        result = self.run_script(hook_input("PreToolUse"))

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, b"", b""))
        self.assertEqual([event["event"] for event in self.events()], ["agent.tool_call.started"])

    @unittest.skipIf(sys.version_info < (3, 11), "PYTHONSAFEPATH is new in Python 3.11")
    def test_the_governance_hook_finds_the_ledger_with_pythonsafepath_set(self) -> None:
        self.env["PYTHONSAFEPATH"] = "1"
        self.govern()

        result = self.run_script(hook_input("PreToolUse"), script="mezmo_governance_hook.py")

        self.assertEqual((result.returncode, result.stdout, result.stderr), (0, DENY_STDOUT, b""))
        self.assertEqual([event["event"] for event in self.events()], ["agent.tool_call.not_run"])

    def test_the_deny_is_byte_for_byte_the_same_with_the_ledger_on_or_off(self) -> None:
        self.govern()
        on = self.run_script(hook_input("PreToolUse"), script="mezmo_governance_hook.py")
        for name in ("MEZMO_LEDGER_ACCOUNT", "MEZMO_LEDGER_SPOOL", "MEZMO_LEDGER_URL"):
            del self.env[name]
        off = self.run_script(hook_input("PreToolUse"), script="mezmo_governance_hook.py")
        self.assertEqual((on.returncode, on.stdout, on.stderr), (0, DENY_STDOUT, b""))
        self.assertEqual((off.returncode, off.stdout, off.stderr), (0, DENY_STDOUT, b""))
        self.assertEqual(len(self.recorder.requests), 1)
        self.assertFalse((self.dir / ".mezmo").exists())


class StreamFailureTests(ScriptTestCase):
    """Once the governance hook takes the ledger path, a stdout or stderr that fails cannot change
    its exit code. Only a deny that never reached Codex may.
    """

    # Closes stdout, then runs the command after it, so the hook starts with no fd 1.
    WITHOUT_STDOUT = "import os, sys; os.close(1); os.execv(sys.argv[1], sys.argv[1:])"

    def deny_with(self, *, closed_stdout: bool = False, stderr: Any = subprocess.PIPE) -> tuple[int, bytes | None]:
        """Run the governance hook on a call it denies. (exit code, stdout), stdout None when closed."""
        command = [sys.executable, str(self.hooks / "mezmo_governance_hook.py")]
        if closed_stdout:
            command = [sys.executable, "-c", self.WITHOUT_STDOUT, *command]
        process = subprocess.Popen(
            command,
            env=self.env,
            stderr=stderr,
            stdin=subprocess.PIPE,
            stdout=None if closed_stdout else subprocess.PIPE,
        )
        stdout, _ = process.communicate(json.dumps(hook_input("PreToolUse")).encode(), timeout=30)
        return process.returncode, stdout

    def test_a_stderr_whose_reader_is_gone_cannot_change_the_exit_code(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX pipes")
        self.govern()
        # The ledger is told, refuses the URL and says so on stderr, which nobody reads.
        self.env["MEZMO_LEDGER_URL"] = "http://ledger.invalid/v1/events"
        read, write = os.pipe()
        os.close(read)
        try:
            code, stdout = self.deny_with(stderr=write)
        finally:
            os.close(write)

        self.assertEqual((code, stdout), (0, DENY_STDOUT))
        self.assertEqual(self.recorder.requests, [])

    def test_a_closed_stdout_exits_as_it_does_with_the_ledger_off(self) -> None:
        if os.name == "nt":
            self.skipTest("POSIX file descriptors")
        self.govern()

        on = self.deny_with(closed_stdout=True)
        for name in ("MEZMO_LEDGER_ACCOUNT", "MEZMO_LEDGER_SPOOL", "MEZMO_LEDGER_URL"):
            del self.env[name]
        off = self.deny_with(closed_stdout=True)

        self.assertEqual(on, (0, None))
        self.assertEqual(off, (0, None))
        # The deny reached nobody, so Codex runs the call: it is no `not_run`.
        self.assertEqual(self.recorder.requests, [])
        self.assertEqual(self.saved(), [])


# The governance hook's main(), with its deadline watchdog off. At the deadline the watchdog and
# the poll loop race to deny, and only the poll loop's deny goes through the ledger.
STEADY_DEADLINE = """
import sys
sys.path.insert(0, sys.argv[1])
import mezmo_governance_hook as hook
hook.deadline_watchdog = lambda seconds: (lambda: None)
sys.exit(hook.main())
"""
HOLD_LOCK = """
import fcntl, sys, time
with open(sys.argv[1], "a") as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    print("locked", flush=True)
    time.sleep(60)
"""


class RefusalBudgetTests(ScriptTestCase):
    """Codex runs the tool when a hook outlives its timeout, so a deny must end within
    LEDGER_REFUSAL_BUDGET_SECONDS of being printed, whatever the ledger is waiting on. Each test
    costs about that long.
    """

    # Room for the interpreter to start and for a busy machine.
    SLACK = 2.0
    # Room for a busy machine, timed from the deny's arrival, so no interpreter start.
    PHASE_SLACK = 1.0

    def deny(self) -> subprocess.CompletedProcess[bytes]:
        result = self.run_script(hook_input("PreToolUse"), script="mezmo_governance_hook.py")
        self.assertEqual((result.returncode, result.stdout), (0, DENY_STDOUT), result.stderr)
        return result

    def test_a_retry_file_lock_held_by_another_process(self) -> None:
        try:
            import fcntl  # noqa: F401
        except ImportError:
            self.skipTest("no fcntl")
        self.govern()
        # Nothing listens, so the send fails at once and the event goes to the retry file.
        self.env["MEZMO_LEDGER_URL"] = closed_port_url("/v1/events")
        self.spool.parent.mkdir(parents=True)
        holder = subprocess.Popen([sys.executable, "-c", HOLD_LOCK, f"{self.spool}.lock"], stdout=subprocess.PIPE)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), b"locked")

        self.deny()

        self.assertIsNone(holder.poll(), "the lock was let go before the hook ended")
        # The timer is what ended it: the save was still waiting for the lock.
        self.assertGreaterEqual(self.elapsed, ledger.LEDGER_REFUSAL_BUDGET_SECONDS - 0.5)
        self.assertLess(self.elapsed, ledger.LEDGER_REFUSAL_BUDGET_SECONDS + self.SLACK)
        self.assertEqual(self.saved(), [])

    def test_a_ledger_send_that_hangs(self) -> None:
        self.govern()
        self.recorder.hang = True

        self.deny()

        self.assertEqual(len(self.recorder.requests), 1)
        self.assertGreaterEqual(self.elapsed, ledger.REFUSED_TIMEOUT_SECONDS - 0.5)
        self.assertLess(self.elapsed, ledger.LEDGER_REFUSAL_BUDGET_SECONDS + self.SLACK)
        (saved,) = self.saved()
        self.assertEqual(saved["event"], "agent.tool_call.not_run")

    def deny_timed(self, *, command: list[str] | None = None, stdout: bytes = DENY_STDOUT) -> float:
        """Run the governance hook on a call it denies, check its deny, and return the seconds from
        the deny's arrival to the hook's exit: the time the ledger adds, without the interpreter's
        start or governance's answer.
        """
        process = subprocess.Popen(
            command or [sys.executable, str(self.hooks / "mezmo_governance_hook.py")],
            env=self.env,
            stderr=subprocess.PIPE,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
        )
        try:
            process.stdin.write(json.dumps(hook_input("PreToolUse")).encode())
            process.stdin.close()
            process.stdin = None
            # The hook flushes the deny before it tells the ledger.
            deny = process.stdout.readline()
            arrived = time.monotonic()
            rest, stderr = process.communicate(timeout=30)
        except BaseException:
            process.kill()
            process.communicate()
            raise
        phase = time.monotonic() - arrived
        self.assertEqual((process.returncode, deny + rest), (0, stdout), stderr)
        return phase

    def test_a_deny_that_arrives_just_before_the_deadline(self) -> None:
        # The deny lands 1 s into a 2 s deadline, then the ledger hangs. The hook still ends
        # within the budget of printing the deny, as a deny at 590 s stays inside Codex's 600 s.
        self.govern(delay=1.0)
        self.env["MEZMO_GOVERNANCE_TIMEOUT_SECONDS"] = "2"
        self.recorder.hang = True

        phase = self.deny_timed()

        self.assertGreaterEqual(phase, ledger.REFUSED_TIMEOUT_SECONDS - 0.5)
        self.assertLess(phase, ledger.LEDGER_REFUSAL_BUDGET_SECONDS + self.PHASE_SLACK)
        (saved,) = self.saved()
        self.assertEqual(json.loads(saved["extra"]["output"]), {"outcome": "denied", "reason": DENY_REASON})

    def test_a_call_still_pending_at_the_deadline(self) -> None:
        # The poll loop's own "still pending" error is the deny, then the ledger hangs.
        self.env.update(
            {
                "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "2",
                "MEZMO_GOVERNANCE_TOKEN": "local-test",
                "MEZMO_GOVERNANCE_URL": serve_governance_pending(self),
            }
        )
        self.recorder.hang = True
        reason = "Mezmo Governance: approval still pending"
        deny = (
            json.dumps(
                hook.deny_output(f"Mezmo Governance could not authorize this exact tool call, so Codex blocked it: {reason}"),
                separators=(",", ":"),
            )
            + "\n"
        ).encode()

        phase = self.deny_timed(command=[sys.executable, "-c", STEADY_DEADLINE, str(self.hooks)], stdout=deny)

        self.assertGreaterEqual(phase, ledger.REFUSED_TIMEOUT_SECONDS - 0.5)
        self.assertLess(phase, ledger.LEDGER_REFUSAL_BUDGET_SECONDS + self.PHASE_SLACK)
        (saved,) = self.saved()
        self.assertEqual(json.loads(saved["extra"]["output"]), {"outcome": "error", "reason": reason})

    def test_the_budget_fits_inside_the_handler_timeout(self) -> None:
        self.assertEqual(ledger.LEDGER_REFUSAL_BUDGET_SECONDS, 3.0)
        self.assertLess(ledger.REFUSED_TIMEOUT_SECONDS, ledger.LEDGER_REFUSAL_BUDGET_SECONDS)
        for name in ("hooks.example.json", "hooks/hooks.json"):
            config = json.loads((ADAPTER_DIR / name).read_text(encoding="utf-8"))["hooks"]
            with self.subTest(config=name):
                governance = config["PreToolUse"][0]["hooks"][0]
                self.assertGreater(governance["timeout"], hook.MAX_TIMEOUT_SECONDS + ledger.LEDGER_REFUSAL_BUDGET_SECONDS)
                for event in ("PreToolUse", "PostToolUse"):
                    self.assertGreater(
                        config[event][0]["hooks"][1]["timeout"],
                        ledger.LEDGER_TIMEOUT_SECONDS + ledger.LEDGER_EXIT_MARGIN_SECONDS,
                    )


class LedgerHookBudgetTests(ScriptTestCase):
    """The ledger hook ends within LEDGER_TIMEOUT_SECONDS plus LEDGER_EXIT_MARGIN_SECONDS of starting,
    whatever it is waiting on, so it never holds a call until Codex kills it.
    """

    # Room for the interpreter to start and for a busy machine.
    SLACK = 2.0

    def test_the_ledger_hook_ends_by_its_own_budget_while_another_process_holds_the_lock(self) -> None:
        try:
            import fcntl  # noqa: F401
        except ImportError:
            self.skipTest("no fcntl")
        # Nothing listens, so the send fails at once and the event goes to the retry file.
        self.env["MEZMO_LEDGER_URL"] = closed_port_url("/v1/events")
        self.spool.parent.mkdir(parents=True)
        holder = subprocess.Popen([sys.executable, "-c", HOLD_LOCK, f"{self.spool}.lock"], stdout=subprocess.PIPE)
        self.addCleanup(holder.stdout.close)
        self.addCleanup(holder.wait)
        self.addCleanup(holder.kill)
        self.assertEqual(holder.stdout.readline().strip(), b"locked")
        budget = ledger.LEDGER_TIMEOUT_SECONDS + ledger.LEDGER_EXIT_MARGIN_SECONDS

        result = self.run_script(hook_input("PreToolUse"))

        self.assertEqual((result.returncode, result.stdout), (0, b""), result.stderr)
        self.assertIsNone(holder.poll(), "the lock was let go before the hook ended")
        # The timer is what ended it: the save was still waiting for the lock.
        self.assertGreaterEqual(self.elapsed, budget - 0.5)
        self.assertLess(self.elapsed, budget + self.SLACK)
        self.assertEqual(self.saved(), [])


class ResolveTargetTests(unittest.TestCase):
    def test_the_tickets_cases(self) -> None:
        # The rest are in claude-code/tests/test_resolve_target.py: the drift test keeps the copies identical.
        def bash(command: str) -> tuple[str, str, dict[str, Any]]:
            return ledger.AGENT, "Bash", {"command": command}

        cases = [
            (bash("kubectl -n prod logs deploy/checkout"), ("checkout/prod", "read")),
            (bash("kubectl get pods -n prod"), ("unknown", "read")),
            (
                bash("kubectl get pods -n prod && kubectl scale deployment payments --replicas=0 --namespace=staging"),
                ("payments/staging", "write"),
            ),
            (
                bash("kubectl get deploy payments # don't change staging\nkubectl delete svc checkout # team's cleanup"),
                ("checkout/prod", "write"),
            ),
            (bash("kubectl get deploy -o yaml payments -n prod"), ("payments/prod", "read")),
            (bash("kubectl get deploy --sort-by x payments -n prod"), ("payments/prod", "read")),
            (bash("kubectl get deploy -A payments -n prod"), ("payments/prod", "read")),
            (bash("kubectl delete deploy payments -n prod -n stage"), ("payments/stage", "write")),
            (bash("kubectl drain node-1"), ("unknown", "write")),
            (bash("kubectl label deploy payments tier=web -n prod"), ("payments/prod", "write")),
            (bash("kubectl taint nodes node-1 key=value:NoSchedule"), ("unknown", "write")),
            (bash("kubectl rollout undo deploy/checkout -n prod --to-revision=40"), ("checkout/prod", "write")),
            (("demo_server", "get_service_failure_rates", {"service": "checkout"}), ("checkout/prod", "read")),
            (("demo_server", "get_update_history", {"service": "checkout"}), ("checkout/prod", "read")),
            (("demo_server", "getRecentUpdates", {"service": "checkout"}), ("checkout/prod", "read")),
            (("demo_server", "updateService", {"service": "checkout"}), ("checkout/prod", "write")),
        ]
        for call, expected in cases:
            with self.subTest(call=call):
                self.assertEqual(ledger.resolve_target(*call), expected)


# Both plugins read and write ~/.mezmo/ledger-spool.jsonl, so the code that touches the file or
# sends a batch must stay identical to claude-code/scripts/ledger.py: a fix in one reaches the
# other, and neither can leave a line the other misreads. The same holds for how both read a join
# or close and derive an investigation's thread id.
CLAUDE_CODE_LEDGER_DEFINITIONS = (
    "DEFAULT_LEDGER_SPOOL",
    "INVESTIGATION_ACTIONS",
    "INVESTIGATION_SCRIPT",
    "LEDGER_ACCOUNT_ENV",
    "LEDGER_DRAIN_BATCH",
    "LEDGER_KEY_ENV",
    "LEDGER_SPOOL_ENV",
    "LEDGER_SPOOL_MAX_LINES",
    "LEDGER_TIMEOUT_SECONDS",
    "LEDGER_URL_ENV",
    "LedgerConfig",
    "LedgerHttp",
    "LedgerSendResult",
    "MAX_LEDGER_JSON_CHARACTERS",
    "MAX_LEDGER_MESSAGE_CHARACTERS",
    "PYTHON_FLAGS",
    "PYTHON_INTERPRETER",
    "SHELL_ASSIGNMENT",
    "SKIP_LEDGER_MCP_TOOL_NAMES",
    "SPOOL_LOCK",
    "_drain_lock",
    "_investigation_call",
    "_ledger_statuses",
    "_open_retry_file",
    "_read_lines",
    "_resend_ledger_events",
    "_retryable",
    "_skip_ledger_mcp_tool",
    "_spool_ledger_events",
    "_spool_locked",
    "drain_ledger_spool",
    "investigation_thread_id",
    "ledger_json",
    "ledger_spool_path",
    "operator_email",
    "post_ledger_events",
    "save_ledger_events",
    "send_ledger_events",
)
# The ledger file's own copies of helpers the Claude Code ledger imports from its governance hook.
CLAUDE_CODE_HOOK_DEFINITIONS = ("_locked", "_setting", "authorization_header")
# Both plugins file a call under the same target and access, so the resolver must stay identical to
# claude-code/scripts/ledger.py's. Its tests are in claude-code/tests/test_resolve_target.py.
CLAUDE_CODE_RESOLVER_DEFINITIONS = (
    "DEMO_DEFAULT_ENV",
    "DEMO_TARGETS",
    "KUBECTL_FILE_FLAGS",
    "KUBECTL_KINDS",
    "KUBECTL_READ_ROLLOUTS",
    "KUBECTL_SERVICE_TYPES",
    "KUBECTL_TARGET_VALUE_FLAGS",
    "KUBECTL_VALUE_FLAGS",
    "KUBECTL_WRITE_VERBS",
    "MAX_SHLEX_CHARACTERS",
    "MCP_ENV_ARGUMENTS",
    "MCP_READ_PREFIXES",
    "MCP_SERVICE_ARGUMENTS",
    "MCP_WRITE_WORDS",
    "PLAIN_NAME",
    "SHELL_PUNCTUATION",
    "UNQUOTED_WORD",
    "_command_words",
    "_demo_target",
    "_kubectl_access",
    "_kubectl_calls",
    "_kubectl_parts",
    "_kubectl_target",
    "_kubectl_target_words",
    "_mcp_target",
    "_mcp_access",
    "_name_tokens",
    "_plain_name",
    "_without_shell_comments",
    "resolve_target",
)


class SharedLedgerDriftTests(unittest.TestCase):
    def assert_identical(self, names: tuple[str, ...], path: Path) -> None:
        reference = top_level_definitions(LEDGER_PATH)
        self.assertEqual([name for name in names if name not in reference], [])
        if not path.is_file():
            self.skipTest("the Claude Code plugin is not checked out next to this one")
        claude_code = top_level_definitions(path)
        for name in names:
            with self.subTest(definition=name):
                self.assertEqual(claude_code.get(name), reference[name])

    def test_the_retry_file_and_transport_match_the_claude_code_ledger(self) -> None:
        self.assert_identical(CLAUDE_CODE_LEDGER_DEFINITIONS, CLAUDE_CODE_SCRIPTS / "ledger.py")

    def test_the_helper_copies_match_the_claude_code_hook(self) -> None:
        self.assert_identical(CLAUDE_CODE_HOOK_DEFINITIONS, CLAUDE_CODE_SCRIPTS / "governance_hook.py")

    def test_the_resolver_matches_the_claude_code_ledger(self) -> None:
        self.assert_identical(CLAUDE_CODE_RESOLVER_DEFINITIONS, CLAUDE_CODE_SCRIPTS / "ledger.py")


if __name__ == "__main__":
    unittest.main()
