"""Tests for the hook's ledger events (scripts/governance_hook.py), sent to Mezmo audit-service.

Every test runs with a clean environment and keeps session state, the audit log and the retry
file in a temp folder. The end-to-end tests run the real script against a server defined here.
"""

import errno
import json
import os
import pathlib
import re
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from datetime import datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from typing import Any, Dict, List, Mapping, Optional, Tuple
from unittest.mock import patch

SCRIPTS = pathlib.Path(__file__).resolve().parents[1] / "scripts"
sys.path.insert(0, str(SCRIPTS))

import governance_hook as hook  # noqa: E402

ACCOUNT = "test-acct"
# Never reached: run_hook passes a fake, and the guard in LedgerTestCase.setUp stops a real
# request to it. `.invalid` never resolves, so this names no real host or port.
LEDGER_URL = "https://audit.ledger.invalid/v1/events"
REAL_POST_LEDGER = hook.post_ledger
AUTHORIZE_URL = "https://api.example.test/governance/workflow/authorize"
GOVERNANCE = {
    "MEZMO_GOVERNANCE_ACCESS_KEY": "sts_test",
    "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "1",
    "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "30",
    "MEZMO_GOVERNANCE_URL": AUTHORIZE_URL,
}
# The epic's check value: account test-acct and this incident key.
INCIDENT_KEY = "pagerduty:Q34FCCWSTU9OI2"
CHECK_THREAD_ID = "ab2763da-3eea-5b82-a1bf-7922641cdf6c"
DROP = object()

# What audit-service's schema/audit-event.js enforces.
REQUIRED_FIELDS = {"actor", "application", "entity_id", "entity_type", "event", "internal", "message", "timestamp"}
TOP_LEVEL_FIELDS = REQUIRED_FIELDS | {"correlation_id", "extra"}
EVENT_NAME = re.compile(r"^[a-z0-9]+(?:[_-][a-z0-9]+)*(?:\.[a-z0-9]+(?:[_-][a-z0-9]+)*){2}$")


def hook_input(name: str = "PreToolUse", **fields: Any) -> Dict[str, Any]:
    """A Bash call's hook input. A field set to DROP is left out."""
    event: Dict[str, Any] = {
        "hook_event_name": name,
        "session_id": "session-1",
        "tool_input": {"command": "ls", "description": "List files"},
        "tool_name": "Bash",
        "tool_use_id": "tool-use-1",
    }
    if name == "PostToolUse":
        event["duration_ms"] = 12
        event["tool_response"] = {"interrupted": False, "isImage": False, "stderr": "", "stdout": "a\nb"}
    elif name == "PostToolUseFailure":
        event["duration_ms"] = 15
        event["error"] = "Exit code 2\nls: /nope: No such file or directory"
    elif name == "PermissionDenied":
        event["reason"] = "Auto mode blocked it"
    for key, value in fields.items():
        if value is DROP:
            event.pop(key, None)
        else:
            event[key] = value
    return event


def mcp_input(name: str = "PreToolUse", **fields: Any) -> Dict[str, Any]:
    fields.setdefault("tool_input", {"name": "demo-pod", "namespace": "staging"})
    fields.setdefault("tool_name", "mcp__kubernetes_demo__pods_delete")
    if name == "PostToolUse":
        fields.setdefault("tool_response", {"content": [{"text": "pod deleted", "type": "text"}]})
    return hook_input(name, **fields)


class FakeLedger:
    """Stands in for post_ledger. Each answer is (status, body), an exception, "hang", or a
    callable that returns or raises one.

    A request for events already in the retry file is a resend. With `resends`, a resend takes its
    answer from there, so the new event and the resend, sent in parallel, each get their own.
    """

    def __init__(self, *answers: Any, resends: Optional[List[Any]] = None, spool: Optional[pathlib.Path] = None) -> None:
        self.answers = list(answers)
        self.calls: List[Dict[str, Any]] = []
        self.during: Optional[Any] = None
        self.release = threading.Event()
        self.resends = resends
        self.spool = spool

    def __call__(self, *, events: Any, key: Optional[str], timeout: float, url: str) -> Tuple[int, bytes]:
        events = json.loads(json.dumps(list(events)))
        resend = self.is_resend(events)
        self.calls.append({"events": events, "key": key, "resend": resend, "timeout": timeout, "url": url})
        if self.during is not None:
            self.during()
        queue = self.resends if resend and self.resends is not None else self.answers
        answer = queue.pop(0) if queue else (202, b"{}")
        if callable(answer):
            answer = answer()
        if answer == "hang":
            self.release.wait(30)
            return 202, b"{}"
        if isinstance(answer, BaseException):
            raise answer
        return answer

    def is_resend(self, events: List[Dict[str, Any]]) -> bool:
        if self.spool is None or not self.spool.exists():
            return False
        saved = []
        for line in self.spool.read_text(encoding="utf-8", errors="replace").splitlines():
            try:
                saved.append(json.loads(line).get("event"))
            except (AttributeError, ValueError):
                continue
        return bool(events) and all(event in saved for event in events)

    def events(self) -> List[Dict[str, Any]]:
        return [event for call in self.calls for event in call["events"]]

    def new_calls(self) -> List[Dict[str, Any]]:
        return [call for call in self.calls if not call["resend"]]

    def resent_calls(self) -> List[Dict[str, Any]]:
        return [call for call in self.calls if call["resend"]]


class GovernanceHttp:
    def __init__(self, *answers: Any) -> None:
        self.answers = list(answers)

    def __call__(self, method: str, url: str, payload: Any, timeout: float) -> Tuple[int, Dict[str, Any]]:
        answer = self.answers.pop(0)
        if isinstance(answer, BaseException):
            raise answer
        return answer


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


class Recorder:
    """What the in-test audit-service answers, and what it was sent."""

    def __init__(self, status: int = 202, body: bytes = b'{"accepted":1}', headers: Optional[Dict[str, str]] = None) -> None:
        self.body = body
        self.hang = False
        self.headers = headers or {}
        self.release = threading.Event()
        self.requests: List[Dict[str, Any]] = []
        self.status = status


def serve(test: unittest.TestCase, recorder: Recorder) -> str:
    """Start a server for `recorder` and return its events URL."""

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


class LedgerTestCase(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = pathlib.Path(scratch.name)
        self.spool = self.dir / "spool" / "ledger-spool.jsonl"
        self.state_dir = self.dir / "sessions"
        self.audit_log = self.dir / "audit.jsonl"
        self.env = {
            "HOME": str(self.dir),
            "MEZMO_GOVERNANCE_AUDIT_LOG": str(self.audit_log),
            "MEZMO_GOVERNANCE_STATE_DIR": str(self.state_dir),
            "MEZMO_LEDGER_ACCOUNT": ACCOUNT,
            "MEZMO_LEDGER_SPOOL": str(self.spool),
            "MEZMO_LEDGER_URL": LEDGER_URL,
        }
        environment = patch.dict(os.environ, self.env, clear=True)
        environment.start()
        self.addCleanup(environment.stop)
        self.timeout(1.0)
        self.fakes: List[FakeLedger] = []
        # A request to LEDGER_URL means a test reached past its fake. Requests to the servers
        # defined here go through.
        self.leaks: List[str] = []
        guard = patch.object(hook, "post_ledger", self.guarded_post_ledger)
        guard.start()
        self.addCleanup(guard.stop)
        # Runs first: no ledger thread may outlive the test's environment.
        self.addCleanup(self.finish_threads)
        del hook.LEDGER_MESSAGES[:]

    def guarded_post_ledger(self, *, events: Any, key: Optional[str], timeout: float, url: str) -> Tuple[int, bytes]:
        if not url.startswith("http://127.0.0.1:"):
            self.leaks.append(url)
            raise OSError("test guard: no real network")
        return REAL_POST_LEDGER(events=events, key=key, timeout=timeout, url=url)

    def finish_threads(self) -> None:
        for fake in self.fakes:
            fake.release.set()
        for thread in hook.LEDGER_THREADS:
            thread.join(10)
        self.assertEqual(self.leaks, [])
        del hook.LEDGER_THREADS[:]
        del hook.LEDGER_MESSAGES[:]
        for event in [event for fake in self.fakes for event in fake.events()] + self.spooled():
            self.assert_valid(event)

    def timeout(self, seconds: float) -> None:
        """Shorten the ledger deadline, so no test waits seconds for a hanging fake."""
        deadline = patch.object(hook, "LEDGER_TIMEOUT_SECONDS", seconds)
        deadline.start()
        self.addCleanup(deadline.stop)

    def env_update(self, values: Mapping[str, str]) -> None:
        os.environ.update(values)

    def fake(self, *answers: Any, resends: Optional[List[Any]] = None) -> FakeLedger:
        fake = FakeLedger(*answers, resends=resends, spool=self.spool)
        self.fakes.append(fake)
        return fake

    def run_hook(self, event: Mapping[str, Any], fake: Optional[FakeLedger] = None, **kwargs: Any) -> Any:
        with patch.object(sys, "stderr", StringIO()) as stderr:
            output = hook.handle_event(event, ledger_http=fake if fake is not None else self.fake(), **kwargs)
        self.stderr = stderr.getvalue()
        return output

    def spool_lines(self) -> List[str]:
        if not self.spool.exists():
            return []
        return self.spool.read_text(encoding="utf-8").splitlines()

    def spooled(self) -> List[Dict[str, Any]]:
        events = []
        for line in self.spool_lines():
            try:
                entry = json.loads(line)
            except ValueError:
                continue
            if isinstance(entry, dict) and isinstance(entry.get("event"), dict):
                events.append(entry["event"])
        return events

    def write_spool(self, lines: List[str]) -> None:
        self.spool.parent.mkdir(parents=True, exist_ok=True)
        self.spool.write_text("".join(line + "\n" for line in lines), encoding="utf-8")

    def spool_line(self, event: Optional[Dict[str, Any]] = None, url: str = LEDGER_URL) -> str:
        if event is None:
            event = self.build("started", tool_use_id=f"saved-{time.monotonic_ns()}")
        return json.dumps({"event": event, "url": url}, separators=(",", ":"))

    def build(self, kind: str, name: str = "PreToolUse", **fields: Any) -> Dict[str, Any]:
        config = hook.load_ledger_config()
        assert config is not None
        event = hook.ledger_event(config=config, hook_input=hook_input(name, **fields), kind=kind)
        assert event is not None
        return event

    def write_state(self, state: Mapping[str, Any]) -> None:
        self.state_dir.mkdir(parents=True, exist_ok=True)
        hook.session_state_path("session-1").write_text(json.dumps(state), encoding="utf-8")

    def assert_valid(self, event: Mapping[str, Any]) -> None:
        """What audit-service's schema enforces, applied to every event a test captures."""
        self.assertEqual(REQUIRED_FIELDS - set(event), set(), event)
        self.assertEqual(set(event) - TOP_LEVEL_FIELDS, set(), event)
        self.assertEqual(set(event["actor"]) - {"account", "pk", "type"}, set())
        self.assertIn(event["actor"]["type"], ("user", "service", "system"))
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
        self.assertIsInstance(event.get("internal", False), bool)
        datetime.fromisoformat(event["timestamp"])
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
        self.assertEqual(started["extra"]["call_id"], hook.call_identity(hook_input()).decision_id)
        self.assertEqual(started["extra"]["entry_key"], started["extra"]["call_id"] + ":start")
        self.assertEqual(ran["extra"]["entry_key"], ran["extra"]["call_id"] + ":end")
        self.assertEqual(ran["extra"]["exit_status"], 0)
        self.assertEqual([len(call["events"]) for call in fake.calls], [1, 1])
        self.assertEqual(fake.calls[0]["url"], LEDGER_URL)
        self.assertEqual(fake.calls[0]["timeout"], hook.LEDGER_TIMEOUT_SECONDS)
        self.assertEqual(self.spool_lines(), [])

    def test_each_terminal_row_gives_its_event_and_exit_status(self) -> None:
        cases = [
            ("PostToolUse", {"tool_response": {"interrupted": True, "stdout": ""}}, "failed", 1),
            ("PostToolUse", {"tool_response": {"exit_code": 3, "stdout": "x"}}, "failed", 3),
            ("PostToolUse", {"tool_response": {"content": "no", "isError": True}}, "failed", 1),
            ("PostToolUse", {"tool_response": {"is_error": True}}, "failed", 1),
            ("PostToolUse", {}, "ran", 0),
            ("PostToolUseFailure", {"error": "Exit code 7\nboom"}, "failed", 7),
            ("PostToolUseFailure", {"error": "boom"}, "failed", 1),
            ("PostToolUseFailure", {"error": None}, "failed", 1),
        ]
        for name, fields, kind, exit_status in cases:
            with self.subTest(name=name, fields=fields):
                fake = self.fake()
                self.run_hook(hook_input(name, **fields), fake)
                (event,) = fake.events()
                self.assertEqual(event["event"], f"agent.tool_call.{kind}")
                self.assertEqual(event["extra"]["exit_status"], exit_status)
                self.assertEqual(hook.ledger_result(hook_input=hook_input(name, **fields)), (kind, exit_status))

    def test_governance_deny_sends_not_run(self) -> None:
        self.env_update(GOVERNANCE)
        fake = self.fake()
        http = GovernanceHttp((200, {"approved": False, "reason": "Use a\n canary first."}))
        output = self.run_hook(hook_input(), fake, http=http)
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        started, not_run = fake.events()
        self.assertEqual(started["event"], "agent.tool_call.started")
        self.assertEqual(not_run["event"], "agent.tool_call.not_run")
        self.assertEqual(json.loads(not_run["extra"]["output"]), {"outcome": "denied", "reason": "Use a canary first."})
        self.assertEqual(not_run["message"], "List files: not run - Use a canary first.")
        self.assertEqual(not_run["extra"]["entry_key"], not_run["extra"]["call_id"] + ":end")
        self.assertNotIn("exit_status", not_run["extra"])
        self.assertNotIn("duration_ms", not_run["extra"])

    def test_governance_deny_without_reason_uses_the_default(self) -> None:
        self.env_update(GOVERNANCE)
        fake = self.fake()
        self.run_hook(hook_input(), fake, http=GovernanceHttp((200, {"approved": False})))
        self.assertEqual(json.loads(fake.events()[1]["extra"]["output"]), {"outcome": "denied", "reason": "Policy denied the call."})

    def test_governance_expiry_saves_not_run_without_sending(self) -> None:
        self.env_update(dict(GOVERNANCE, MEZMO_GOVERNANCE_TIMEOUT_SECONDS="1"))
        fake = self.fake()
        clock = FakeClock()
        pending = (207, {"approved": False, "reason": None})
        output = self.run_hook(hook_input(), fake, http=GovernanceHttp(pending, pending, pending), clock=clock, sleep=clock.sleep)
        self.assertIn("approval still pending", output["hookSpecificOutput"]["permissionDecisionReason"])
        self.assertEqual([event["event"] for event in fake.events()], ["agent.tool_call.started"])
        (saved,) = self.spooled()
        self.assertEqual(saved["event"], "agent.tool_call.not_run")
        self.assertEqual(json.loads(saved["extra"]["output"]), {"outcome": "expired", "reason": "Mezmo Governance: approval still pending"})

    def test_governance_error_sends_not_run(self) -> None:
        self.env_update(GOVERNANCE)
        fake = self.fake()
        output = self.run_hook(hook_input(), fake, http=GovernanceHttp(hook.HookContractError("connection refused")))
        self.assertIn("connection refused", output["hookSpecificOutput"]["permissionDecisionReason"])
        not_run = fake.events()[1]
        self.assertEqual(json.loads(not_run["extra"]["output"]), {"outcome": "error", "reason": "connection refused"})

    def test_governance_allow_sends_only_started(self) -> None:
        self.env_update(GOVERNANCE)
        fake = self.fake()
        self.assertIsNone(self.run_hook(hook_input(), fake, http=GovernanceHttp((200, {"approved": True}))))
        self.assertEqual([event["event"] for event in fake.events()], ["agent.tool_call.started"])

    def test_auto_mode_block_sends_not_run(self) -> None:
        for reason, expected in (("Auto mode\nblocked it", {"outcome": "blocked_by_auto_mode", "reason": "Auto mode blocked it"}),
                                 (DROP, {"outcome": "blocked_by_auto_mode"}),
                                 (42, {"outcome": "blocked_by_auto_mode"})):
            with self.subTest(reason=reason):
                fake = self.fake()
                self.assertIsNone(self.run_hook(hook_input("PermissionDenied", reason=reason), fake))
                (event,) = fake.events()
                self.assertEqual(event["event"], "agent.tool_call.not_run")
                self.assertEqual(json.loads(event["extra"]["output"]), expected)
                suffix = " - Auto mode blocked it" if "reason" in expected else ""
                self.assertEqual(event["message"], "List files: not run" + suffix)

    def test_post_and_auto_mode_events_are_sent_with_governance_off(self) -> None:
        fake = self.fake()
        for name in ("PostToolUse", "PostToolUseFailure", "PermissionDenied"):
            self.run_hook(hook_input(name), fake)
        self.assertEqual(
            [event["event"] for event in fake.events()],
            ["agent.tool_call.ran", "agent.tool_call.failed", "agent.tool_call.not_run"],
        )
        self.assertFalse(self.audit_log.exists())

    def test_an_unknown_identity_sends_nothing(self) -> None:
        fake = self.fake()
        for name in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied"):
            for fields in ({"tool_use_id": DROP}, {"session_id": " "}, {"tool_input": "ls"}):
                with self.subTest(name=name, fields=fields):
                    self.assertIsNone(self.run_hook(hook_input(name, **fields), fake))
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.spool_lines(), [])

    def test_calls_governance_skips_send_nothing(self) -> None:
        fake = self.fake()
        self.run_hook(hook_input(tool_name="Read", tool_input={"file_path": "/etc/hosts"}), fake)
        self.run_hook(mcp_input(tool_name="mcp__ccd_session__list"), fake)
        self.assertEqual(fake.calls, [])

class FieldTests(LedgerTestCase):
    def test_messages_and_intent_source(self) -> None:
        cases = [
            (hook_input, {}, "List files", "tool_description"),
            (hook_input, {"tool_input": {"command": "ls", "description": "  List\n\tfiles  "}}, "List files", "tool_description"),
            (hook_input, {"tool_input": {"command": "ls"}}, "claude_code.Bash", "none"),
            (hook_input, {"tool_input": {"command": "ls", "description": "   "}}, "claude_code.Bash", "none"),
            (hook_input, {"tool_input": {"command": "ls", "description": 5}}, "claude_code.Bash", "none"),
            (mcp_input, {}, "kubernetes_demo.pods_delete", "none"),
            (mcp_input, {"tool_input": {"description": "Delete it"}}, "kubernetes_demo.pods_delete", "none"),
        ]
        for builder, fields, intent, source in cases:
            with self.subTest(fields=fields, builder=builder.__name__):
                fake = self.fake()
                self.run_hook(builder("PreToolUse", **fields), fake)
                self.run_hook(builder("PostToolUse", **fields), fake)
                self.run_hook(builder("PostToolUseFailure", **fields), fake)
                self.run_hook(builder("PermissionDenied", **fields), fake)
                started, ran, failed, not_run = fake.events()
                self.assertEqual(started["message"], intent)
                self.assertTrue(ran["message"].startswith(f"{intent}: ok"))
                self.assertEqual(failed["message"], f"{intent}: failed - ls: /nope: No such file or directory")
                self.assertEqual(not_run["message"], f"{intent}: not run - Auto mode blocked it")
                for event in (started, ran, failed, not_run):
                    self.assertEqual(event["extra"]["intent_source"], source)

    def test_bash_and_mcp_names(self) -> None:
        fake = self.fake()
        self.run_hook(hook_input(), fake)
        self.run_hook(mcp_input(), fake)
        bash, mcp = fake.events()
        self.assertEqual((bash["extra"]["tool_namespace"], bash["extra"]["tool_name"]), ("claude_code", "Bash"))
        self.assertEqual((mcp["extra"]["tool_namespace"], mcp["extra"]["tool_name"]), ("kubernetes_demo", "pods_delete"))
        for event in (bash, mcp):
            self.assertEqual(event["actor"], {"account": ACCOUNT, "pk": "claude_code:session-1", "type": "service"})
            self.assertEqual(event["entity_type"], "tool_call")
            self.assertEqual(event["entity_id"], "unknown")
            self.assertIs(event["internal"], False)
            self.assertEqual(event["extra"]["agent"], "claude_code")
            self.assertEqual(event["extra"]["access"], "read")
            self.assertEqual(event["extra"]["target"], "unknown")
            self.assertEqual(event["extra"]["session_id"], "session-1")
            self.assertEqual(event["extra"]["request_id"], hook.call_identity(hook_input()).request_id if event is bash
                             else hook.call_identity(mcp_input()).request_id)
            self.assertNotIn("correlation_id", event)

    def test_output_text_shapes_give_the_message_line(self) -> None:
        cases = [
            ("plain text\nmore", "ok - plain text"),
            ({"stderr": "", "stdout": "\n\n  first   line \nsecond"}, "ok - first line"),
            ({"stderr": "warning: x", "stdout": "  "}, "ok - warning: x"),
            ({"stderr": "warning: x"}, "ok - warning: x"),
            ({"stdout": 5, "stderr": "err"}, "ok - err"),
            ({"content": "pod deleted"}, "ok - pod deleted"),
            ({"content": [{"type": "image"}, {"text": "\nfrom blocks", "type": "text"}, "loose"]}, "ok - from blocks"),
            ([{"text": "listed", "type": "text"}], "ok - listed"),
            ({"stdout": "", "stderr": ""}, "ok"),
            (42, "ok"),
            ({"other": "x"}, "ok"),
        ]
        for response, ending in cases:
            with self.subTest(response=response):
                fake = self.fake()
                self.run_hook(hook_input("PostToolUse", tool_response=response), fake)
                self.assertEqual(fake.events()[0]["message"], f"List files: {ending}")

    def test_failure_text_skips_the_exit_code_line(self) -> None:
        cases = [
            ("Exit code 1\n\n  first  real line\nnext", "failed - first real line"),
            ("Exit code 1", "failed"),
            ("plain error", "failed - plain error"),
            ({"stderr": "from a dict"}, "failed - from a dict"),
        ]
        for error, ending in cases:
            with self.subTest(error=error):
                fake = self.fake()
                self.run_hook(hook_input("PostToolUseFailure", error=error), fake)
                self.assertEqual(fake.events()[0]["message"], f"List files: {ending}")

    def test_a_failed_post_tool_use_prefers_error_for_output(self) -> None:
        fake = self.fake()
        self.run_hook(hook_input("PostToolUse", error="bad thing", tool_response={"exit_code": 1, "stdout": "x"}), fake)
        self.run_hook(hook_input("PostToolUse", tool_response={"exit_code": 1, "stdout": "x"}), fake)
        with_error, without = fake.events()
        self.assertEqual(with_error["extra"]["output"], '"bad thing"')
        self.assertEqual(with_error["message"], "List files: failed - bad thing")
        self.assertEqual(without["extra"]["output"], '{"exit_code":1,"stdout":"x"}')

    def test_output_is_rendered_as_sorted_compact_json(self) -> None:
        fake = self.fake()
        self.run_hook(hook_input("PostToolUse", tool_response={"b": "é", "a": [1, 2]}), fake)
        self.assertEqual(fake.events()[0]["extra"]["output"], '{"a":[1,2],"b":"é"}')
        self.assertEqual(hook.ledger_json({1: "x", "a": "y"}), str({1: "x", "a": "y"}))
        self.assertEqual(hook.ledger_json("abcdef", 3), '"ab')

    def test_arguments_output_and_message_are_cut(self) -> None:
        fake = self.fake()
        long = "x" * 10_000
        self.run_hook(hook_input(tool_input={"command": long, "description": "d" * 500}), fake)
        self.run_hook(hook_input("PostToolUse", tool_input={"command": long}, tool_response={"stdout": long}), fake)
        self.run_hook(hook_input("PermissionDenied", reason="r" * 1000), fake)
        started, ran, not_run = fake.events()
        self.assertEqual(len(started["extra"]["arguments"]), 4096)
        self.assertEqual(len(started["message"]), 200)
        self.assertEqual(len(ran["extra"]["output"]), 4096)
        self.assertEqual(len(ran["message"]), 200)
        reason = json.loads(not_run["extra"]["output"])["reason"]
        self.assertEqual(len(reason), 300)
        self.assertEqual(len(not_run["message"]), 200)

    def test_arguments_only_on_started_and_never_in_session_state(self) -> None:
        self.env_update(GOVERNANCE)
        fake = self.fake()
        canary = "CANARY_ARGUMENT_VALUE"
        tool_input = {"command": f"echo {canary}", "description": "Echo"}
        self.run_hook(hook_input(tool_input=tool_input), fake, http=GovernanceHttp((200, {"approved": True})))
        self.run_hook(hook_input("PostToolUse", tool_input=tool_input, tool_response={"stdout": canary}), fake)
        started, ran = fake.events()
        self.assertEqual(json.loads(started["extra"]["arguments"]), tool_input)
        self.assertNotIn("arguments", ran["extra"])
        state = hook.session_state_path("session-1").read_text(encoding="utf-8")
        self.assertIn("allowed", state)
        self.assertNotIn(canary, state)

    def test_output_is_omitted_without_a_tool_response(self) -> None:
        fake = self.fake()
        self.run_hook(hook_input("PostToolUse", tool_response=DROP), fake)
        self.run_hook(hook_input("PostToolUseFailure", error=None), fake)
        self.run_hook(hook_input(), fake)
        ran, failed, started = fake.events()
        self.assertNotIn("output", ran["extra"])
        self.assertEqual(ran["message"], "List files: ok")
        self.assertNotIn("output", failed["extra"])
        self.assertNotIn("output", started["extra"])

    def test_duration_ms_is_passed_through_only_when_a_number(self) -> None:
        cases = [
            (12, 12), (0, 0), (3.5, 3.5), (DROP, None), (True, None), (-1, None), ("12", None), (float("inf"), None),
            (10 ** 400, None),
        ]
        for value, expected in cases:
            with self.subTest(value=value):
                fake = self.fake()
                self.run_hook(hook_input("PostToolUse", duration_ms=value), fake)
                self.assertEqual(fake.events()[0]["extra"].get("duration_ms"), expected)

    def test_decision_id_only_when_governance_is_configured(self) -> None:
        fake = self.fake()
        self.run_hook(hook_input("PostToolUse"), fake)
        self.env_update(GOVERNANCE)
        self.run_hook(hook_input("PostToolUse"), fake)
        off, on = fake.events()
        self.assertNotIn("decision_id", off["extra"])
        self.assertEqual(on["extra"]["decision_id"], on["extra"]["call_id"])

    def test_application_version_comes_from_plugin_json(self) -> None:
        manifest = json.loads((SCRIPTS.parent / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        event = self.build("started")
        self.assertEqual(event["application"], {"name": "mezmo-claude-code-plugin", "version": manifest["version"]})
        for content in (None, "not json", "[]", '{"version": 3}', '{"version": "  "}', "{}"):
            with self.subTest(content=content):
                path = self.dir / "plugin.json"
                if content is None:
                    path.unlink(missing_ok=True)
                else:
                    path.write_text(content, encoding="utf-8")
                with patch.object(hook, "PLUGIN_MANIFEST", path):
                    self.assertEqual(self.build("started")["application"]["version"], "unknown")

class InvestigationTests(LedgerTestCase):
    def sent(self, name: str = "PreToolUse") -> Dict[str, Any]:
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
                    self.write_state(state)  # type: ignore[arg-type]
                event = self.sent()
                self.assertEqual(event["extra"]["thread_id"], "env-thread")
                self.assertEqual(event["extra"]["incident_key"], INCIDENT_KEY)
                self.assertEqual(event["correlation_id"], "env-thread")

    def test_an_unreadable_state_file_means_no_investigation(self) -> None:
        self.state_dir.mkdir(parents=True)
        hook.session_state_path("session-1").write_text("{not json", encoding="utf-8")
        event = self.sent()
        self.assertNotIn("thread_id", event["extra"])
        self.assertNotIn("references", event["extra"])

    def test_thread_id_is_derived_from_the_incident_key(self) -> None:
        os.environ["MEZMO_LEDGER_INCIDENT_KEY"] = INCIDENT_KEY
        event = self.sent()
        self.assertEqual(event["extra"]["thread_id"], CHECK_THREAD_ID)
        self.assertEqual(event["correlation_id"], CHECK_THREAD_ID)
        self.assertEqual(event["extra"]["incident_key"], INCIDENT_KEY)

    def test_only_the_investigation_id(self) -> None:
        os.environ["MEZMO_LEDGER_INVESTIGATION_ID"] = "env-thread"
        event = self.sent()
        self.assertEqual(event["extra"]["thread_id"], "env-thread")
        self.assertNotIn("incident_key", event["extra"])

    def test_references_that_are_not_a_list_are_left_out(self) -> None:
        self.write_state({"references": {"kind": "pr"}})
        self.assertNotIn("references", self.sent()["extra"])

    def test_no_investigation_fields_without_a_source(self) -> None:
        event = self.sent()
        self.assertNotIn("correlation_id", event)
        for field in ("incident_key", "references", "thread_id"):
            self.assertNotIn(field, event["extra"])


class SettingsTests(LedgerTestCase):
    def test_sending_is_off_when_either_setting_is_missing(self) -> None:
        for missing in (("MEZMO_LEDGER_URL",), ("MEZMO_LEDGER_ACCOUNT",), ("MEZMO_LEDGER_URL", "MEZMO_LEDGER_ACCOUNT")):
            with self.subTest(missing=missing):
                fake = self.fake()
                with patch.dict(os.environ, {name: "  " for name in missing}):
                    for name in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied"):
                        self.assertIsNone(self.run_hook(hook_input(name), fake))
                        self.assertEqual(self.stderr, "")
                    self.assertIsNone(hook.load_ledger_config())
                self.assertEqual(fake.calls, [])
                self.assertFalse(self.spool.exists())
                self.assertEqual(hook.LEDGER_THREADS, [])

    def test_sending_off_leaves_governance_output_unchanged(self) -> None:
        os.environ.update(GOVERNANCE)
        del os.environ["MEZMO_LEDGER_URL"]
        fake = self.fake()
        output = self.run_hook(hook_input(), fake, http=GovernanceHttp((200, {"approved": False, "reason": "no"})))
        self.assertEqual(
            output,
            hook.deny_output("Mezmo Governance denied this exact tool call: no"),
        )
        self.assertEqual(fake.calls, [])

    def test_post_ledger_sends_the_key_as_given(self) -> None:
        recorder = Recorder()
        url = serve(self, recorder)
        cases = [(None, None), ("Bearer abc", "Bearer abc"), ("Token abc", "Token abc"), ("abc", "abc")]
        for key, header in cases:
            with self.subTest(key=key):
                status, _ = hook.post_ledger(events=[{"a": 1}], key=key, timeout=2.0, url=url)
                self.assertEqual(status, 202)
                headers = recorder.requests[-1]["headers"]
                self.assertEqual(headers.get("Authorization"), header)
                self.assertEqual(headers.get("Content-Type"), "application/json")
                self.assertEqual(headers.get("Accept"), "application/json")
                self.assertEqual(recorder.requests[-1]["body"], [{"a": 1}])

    def test_the_key_setting_reaches_the_request(self) -> None:
        os.environ["MEZMO_LEDGER_KEY"] = "secret-key"
        fake = self.fake()
        self.run_hook(hook_input(), fake)
        self.assertEqual(fake.calls[0]["key"], "Token secret-key")

    def sent_authorization(self, name: str = "PostToolUse") -> Optional[str]:
        """The Authorization header handle_event sends to a server defined here."""
        recorder = Recorder()
        os.environ["MEZMO_LEDGER_URL"] = serve(self, recorder)
        with patch.object(sys, "stderr", StringIO()):
            hook.handle_event(hook_input(name))
        (request,) = recorder.requests
        return request["headers"].get("Authorization")

    def test_no_authorization_without_a_ledger_key_or_governance(self) -> None:
        for name in ("PreToolUse", "PostToolUse", "PermissionDenied"):
            with self.subTest(name=name):
                self.assertIsNone(self.sent_authorization(name))
        self.assertIsNone(hook.load_ledger_config().key)

    def test_the_governance_key_is_sent_without_a_ledger_key(self) -> None:
        cases = [
            ({"MEZMO_GOVERNANCE_ACCESS_KEY": "sts_env"}, "Token sts_env"),
            ({"MEZMO_GOVERNANCE_TOKEN": "Bearer sts_token"}, "Bearer sts_token"),
            ({"CLAUDE_PLUGIN_OPTION_GOVERNANCE_ACCESS_KEY": "sts_option"}, "Token sts_option"),
            ({"CLAUDE_PLUGIN_OPTION_GOVERNANCE_ACCESS_KEY": "sts_option", "CLAUDE_PLUGIN_OPTION_GOVERNANCE_ENABLED": "false"}, None),
        ]
        for values, header in cases:
            with self.subTest(values=values), patch.dict(os.environ, values):
                self.assertEqual(self.sent_authorization(), header)

    def test_the_ledger_key_wins_over_the_governance_key(self) -> None:
        os.environ.update(GOVERNANCE)
        os.environ["MEZMO_LEDGER_KEY"] = "ledger-key"
        self.assertEqual(self.sent_authorization(), "Token ledger-key")

    def test_a_ledger_key_alone_leaves_governance_off(self) -> None:
        os.environ["MEZMO_LEDGER_KEY"] = "ledger-key"
        self.assertIsNone(hook.load_config())
        http = GovernanceHttp((200, {"approved": False, "reason": "no"}))
        fake = self.fake()
        self.assertIsNone(self.run_hook(hook_input(), fake, http=http))
        self.assertEqual(len(http.answers), 1)
        self.assertEqual([call["key"] for call in fake.calls], ["Token ledger-key"])
        self.assertFalse(self.audit_log.exists())

    def test_the_key_is_sent_as_a_token_unless_it_names_its_scheme(self) -> None:
        cases = [("abc", "Token abc"), ("Token abc", "Token abc"), ("Bearer abc", "Bearer abc"), ("bearer abc", "bearer abc")]
        for key, header in cases:
            for setting in ("MEZMO_GOVERNANCE_ACCESS_KEY", "MEZMO_LEDGER_KEY"):
                with self.subTest(key=key, setting=setting), patch.dict(os.environ, {setting: key}):
                    self.assertEqual(hook.load_ledger_config().key, header)
                    self.assertEqual(self.sent_authorization(), header)

    def test_an_unsafe_url_or_key_turns_sending_off(self) -> None:
        cases = [
            ({"MEZMO_LEDGER_URL": "http://audit.example.test/v1/events"}, "MEZMO_LEDGER_URL", "audit.example.test"),
            ({"MEZMO_LEDGER_URL": "https://user:pw@audit.example.test/v1/events"}, "MEZMO_LEDGER_URL", "pw@"),
            ({"MEZMO_LEDGER_URL": "ftp://127.0.0.1/v1/events"}, "MEZMO_LEDGER_URL", "ftp"),
            ({"MEZMO_LEDGER_KEY": "bad\nkey"}, "MEZMO_LEDGER_KEY", "bad"),
            ({"MEZMO_GOVERNANCE_ACCESS_KEY": "bad\nkey"}, "MEZMO_GOVERNANCE_ACCESS_KEY", "bad"),
            ({"MEZMO_GOVERNANCE_TOKEN": "bad\x7fkey"}, "MEZMO_GOVERNANCE_TOKEN", "bad"),
            ({"CLAUDE_PLUGIN_OPTION_GOVERNANCE_ACCESS_KEY": "bad\u00e9key"}, "CLAUDE_PLUGIN_OPTION_GOVERNANCE_ACCESS_KEY", "bad"),
            ({"MEZMO_GOVERNANCE_ACCESS_KEY": "bad\nkey", "MEZMO_LEDGER_KEY": "worse\nkey"}, "MEZMO_LEDGER_KEY", "worse"),
        ]
        for values, setting, secret in cases:
            with self.subTest(values=values), patch.dict(os.environ, values):
                fake = self.fake()
                # PostToolUse, so a configured governance key never asks governance.
                self.run_hook(hook_input("PostToolUse"), fake)
                self.assertEqual(fake.calls, [])
                self.assertFalse(self.spool.exists())
                self.assertEqual(len(self.stderr.splitlines()), 1)
                self.assertIn(setting, self.stderr)
                self.assertNotIn(secret, self.stderr)
                for other in ("MEZMO_LEDGER_KEY", "MEZMO_GOVERNANCE_ACCESS_KEY", "MEZMO_GOVERNANCE_TOKEN"):
                    if other != setting and not setting.endswith(other):
                        self.assertNotIn(other, self.stderr)

    def test_https_and_loopback_http_are_allowed(self) -> None:
        for url in ("https://audit.example.test/v1/events", "http://localhost:65530/v1/events", "http://[::1]:65530/v1/events"):
            with self.subTest(url=url), patch.dict(os.environ, {"MEZMO_LEDGER_URL": url}):
                self.assertEqual(hook.load_ledger_config(), hook.LedgerConfig(ACCOUNT, None, url))

    def test_a_redirect_is_not_followed_and_the_events_are_dropped(self) -> None:
        elsewhere = Recorder()
        target = serve(self, elsewhere)
        redirect = Recorder(status=307, body=b"", headers={"Location": target})
        os.environ["MEZMO_LEDGER_URL"] = serve(self, redirect)
        with patch.object(sys, "stderr", StringIO()) as stderr:
            self.assertIsNone(hook.handle_event(hook_input()))
        self.assertEqual(len(redirect.requests), 1)
        self.assertEqual(elsewhere.requests, [])
        self.assertEqual(self.spool_lines(), [])
        self.assertIn("HTTP 307", stderr.getvalue())

    def test_success_statuses_are_done(self) -> None:
        recorder = Recorder()
        os.environ["MEZMO_LEDGER_URL"] = serve(self, recorder)
        for status, body in ((200, b"{}"), (204, b""), (202, b"accepted, not json")):
            with self.subTest(status=status):
                recorder.status, recorder.body = status, body
                self.run_hook(hook_input(tool_use_id=f"call-{status}"), hook.post_ledger)
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
        for status in (400, 403, 404, 409, 422, 302):
            with self.subTest(status=status):
                self.run_hook(hook_input(), self.fake((status, b'{"code":"EINVAL"}')))
                self.assertEqual(self.spool_lines(), [])
                self.assertIn(f"HTTP {status}", self.stderr)

    def test_a_207_saves_only_the_rejected_events(self) -> None:
        config = hook.load_ledger_config()
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
            hook.send_ledger(config=config, events=events, ledger_http=self.fake((207, body))),
            hook.LedgerSendResult(answered=True, save=[events[1]]),
        )
        self.assertEqual(self.spool_lines(), [])

    def test_a_207_without_a_usable_list_saves_them_all(self) -> None:
        config = hook.load_ledger_config()
        events = [self.build("started", tool_use_id=f"call-{n}") for n in range(2)]
        for body in (b"not json", b"[]", b'{"events": "x"}'):
            with self.subTest(body=body):
                self.assertEqual(
                    hook.send_ledger(config=config, events=events, ledger_http=self.fake((207, body))),
                    hook.LedgerSendResult(answered=True, save=events),
                )

    def test_send_ledger_says_whether_the_service_answered_and_never_saves(self) -> None:
        config = hook.load_ledger_config()
        event = self.build("started")
        cases = [
            ((202, b"{}"), hook.LedgerSendResult(answered=True, save=[])),
            ((400, b"{}"), hook.LedgerSendResult(answered=True, save=[])),
            ((503, b""), hook.LedgerSendResult(answered=True, save=[event])),
            (OSError("refused"), hook.LedgerSendResult(answered=False, save=[event])),
            (TimeoutError("timed out"), hook.LedgerSendResult(answered=False, save=[event])),
            (RuntimeError("boom"), hook.LedgerSendResult(answered=False, save=[])),
        ]
        for answer, expected in cases:
            with self.subTest(answer=answer):
                self.assertEqual(hook.send_ledger(config=config, events=[event], ledger_http=self.fake(answer)), expected)
        self.assertEqual(self.spool_lines(), [])

    def test_started_that_gets_no_answer_saves_not_run_without_a_request(self) -> None:
        os.environ.update(GOVERNANCE)
        fake = self.fake(OSError("refused"))
        output = self.run_hook(hook_input(), fake, http=GovernanceHttp((200, {"approved": False, "reason": "no"})))
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual([event["event"] for event in self.spooled()], ["agent.tool_call.started", "agent.tool_call.not_run"])

    def test_started_that_gets_a_503_still_sends_not_run(self) -> None:
        os.environ.update(GOVERNANCE)
        fake = self.fake((503, b""), (202, b"{}"))
        output = self.run_hook(hook_input(), fake, http=GovernanceHttp((200, {"approved": False, "reason": "no"})))
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual([event["event"] for event in fake.events()], ["agent.tool_call.started", "agent.tool_call.not_run"])
        self.assertEqual([event["event"] for event in self.spooled()], ["agent.tool_call.started"])

    def test_not_run_is_saved_without_a_request_when_started_never_started(self) -> None:
        os.environ.update(GOVERNANCE)
        fake = self.fake()
        with patch.object(hook, "start_ledger", side_effect=RuntimeError("no threads")):
            output = self.run_hook(hook_input(), fake, http=GovernanceHttp((200, {"approved": False, "reason": "no"})))
        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertEqual(fake.calls, [])
        self.assertEqual([event["event"] for event in self.spooled()], ["agent.tool_call.started", "agent.tool_call.not_run"])


class RetryFileTests(LedgerTestCase):
    def test_a_network_failure_is_resent_by_the_next_run(self) -> None:
        self.run_hook(hook_input("PostToolUse"), self.fake(OSError("refused")))
        (saved,) = self.spooled()
        self.assertEqual(json.loads(self.spool_lines()[0])["url"], LEDGER_URL)
        fake = self.fake()
        self.run_hook(hook_input("PostToolUse", tool_use_id="tool-use-2"), fake)
        self.assertEqual(len(fake.calls), 2)
        self.assertEqual([call["events"] for call in fake.resent_calls()], [[saved]])
        self.assertEqual([event["extra"]["call_id"] for call in fake.new_calls() for event in call["events"]],
                         [hook.call_identity(hook_input(tool_use_id="tool-use-2")).decision_id])
        self.assertEqual(self.spool_lines(), [])

    def test_a_resent_event_keeps_its_time_and_investigation(self) -> None:
        os.environ["MEZMO_LEDGER_INCIDENT_KEY"] = INCIDENT_KEY
        self.run_hook(hook_input(), self.fake(OSError("refused")))
        (saved,) = self.spooled()
        del os.environ["MEZMO_LEDGER_INCIDENT_KEY"]
        fake = self.fake()
        self.run_hook(hook_input(tool_use_id="tool-use-2"), fake)
        self.assertEqual(fake.resent_calls()[0]["events"], [saved])
        self.assertEqual(saved["extra"]["thread_id"], CHECK_THREAD_ID)
        self.assertNotIn("thread_id", fake.new_calls()[0]["events"][0]["extra"])

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

    def test_a_400_on_the_drain_removes_the_lines_it_sent(self) -> None:
        other = self.spool_line(url="https://elsewhere.example.test/v1/events")
        self.write_spool([self.spool_line(), other, self.spool_line()])
        fake = self.fake((202, b"{}"), resends=[(400, b'{"code":"EINVAL"}')])
        self.run_hook(hook_input("PermissionDenied"), fake)
        self.assertEqual([len(call["events"]) for call in fake.resent_calls()], [2])
        self.assertEqual(self.spool_lines(), [other])
        self.assertIn("HTTP 400", self.stderr)

    def test_a_207_on_the_drain_finishes_only_the_accepted_lines(self) -> None:
        lines = [self.spool_line() for _ in range(3)]
        self.write_spool(lines)
        body = json.dumps({"events": [{"index": 0, "status": "accepted"}, {"index": 1, "status": "rejected"}]}).encode()
        hook.drain_ledger_spool(config=hook.load_ledger_config(), ledger_http=self.fake((207, body)))
        self.assertEqual(self.spool_lines(), lines[1:])
        hook.drain_ledger_spool(config=hook.load_ledger_config(), ledger_http=self.fake((207, b"x")))
        self.assertEqual(self.spool_lines(), lines[1:])

    def test_bad_lines_are_removed(self) -> None:
        good = self.spool_line()
        bad = ["not json", "[]", '{"event": 1, "url": "x"}', '{"event": {}, "url": 5}', '{"url": "x"}', ""]
        self.write_spool(bad[:3] + [good] + bad[3:])
        fake = self.fake()
        hook.drain_ledger_spool(config=hook.load_ledger_config(), ledger_http=fake)
        self.assertEqual(fake.calls[0]["events"], [json.loads(good)["event"]])
        self.assertEqual(self.spool_lines(), [])

    def test_a_line_that_is_not_utf8_is_removed(self) -> None:
        good = self.spool_line()
        inside = self.spool_line().replace('"session_id":"session-1"', '"session_id":"session-\udcff"').encode("utf-8", "surrogateescape")
        self.assertIn(b"\xff", inside)
        self.spool.parent.mkdir(parents=True, exist_ok=True)
        self.spool.write_bytes(b"\xff\xfe broken\n" + inside + b"\n" + good.encode() + b"\n")
        fake = self.fake()
        hook.drain_ledger_spool(config=hook.load_ledger_config(), ledger_http=fake)
        self.assertEqual(fake.calls[0]["events"], [json.loads(good)["event"]])
        self.assertEqual(self.spool.read_bytes(), b"")
        self.assertEqual(hook.LEDGER_MESSAGES, [])

    def test_only_bad_lines_rewrite_without_a_request(self) -> None:
        self.write_spool(["not json", self.spool_line(url="https://elsewhere.example.test/v1/events")])
        fake = self.fake()
        hook.drain_ledger_spool(config=hook.load_ledger_config(), ledger_http=fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(len(self.spool_lines()), 1)

    def test_nothing_to_resend_means_no_request_and_no_rewrite(self) -> None:
        self.write_spool([self.spool_line(url="https://elsewhere.example.test/v1/events")])
        before = self.spool.stat().st_mtime_ns
        fake = self.fake()
        hook.drain_ledger_spool(config=hook.load_ledger_config(), ledger_http=fake)
        self.assertEqual(fake.calls, [])
        self.assertEqual(self.spool.stat().st_mtime_ns, before)
        self.assertFalse(pathlib.Path(f"{self.spool}.tmp").exists())

    def test_a_line_for_another_url_is_not_sent_and_stays(self) -> None:
        other = self.spool_line(url="http://127.0.0.1:9999/v1/events")
        mine = self.spool_line()
        self.write_spool([other, mine])
        fake = self.fake()
        self.run_hook(hook_input(), fake)
        self.assertEqual([call["events"] for call in fake.resent_calls()], [[json.loads(mine)["event"]]])
        self.assertEqual(self.spool_lines(), [other])

    def test_a_line_appended_during_the_request_survives_the_rewrite(self) -> None:
        sent = self.spool_line()
        self.write_spool([sent])
        config = hook.load_ledger_config()
        late = self.build("started", tool_use_id="late")
        fake = self.fake()
        fake.during = lambda: hook._spool_ledger(config=config, events=[late])
        hook.drain_ledger_spool(config=config, ledger_http=fake)
        self.assertEqual(self.spooled(), [late])

    def test_a_duplicate_line_is_removed_once(self) -> None:
        line = self.spool_line()
        self.write_spool([line])
        config = hook.load_ledger_config()
        fake = self.fake()

        def append() -> None:
            with self.spool.open("a", encoding="utf-8") as handle:
                handle.write(line + "\n")

        fake.during = append
        hook.drain_ledger_spool(config=config, ledger_http=fake)
        self.assertEqual(self.spool_lines(), [line])

    def test_the_drain_is_skipped_while_another_hook_holds_its_lock(self) -> None:
        try:
            import fcntl
        except ImportError:
            self.skipTest("no fcntl")
        self.write_spool([self.spool_line()])
        with open(f"{self.spool}.drain.lock", "a") as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            fake = self.fake()
            hook.drain_ledger_spool(config=hook.load_ledger_config(), ledger_http=fake)
            self.assertEqual(fake.calls, [])
        self.assertEqual(len(self.spool_lines()), 1)
        hook.drain_ledger_spool(config=hook.load_ledger_config(), ledger_http=fake)
        self.assertEqual(len(fake.calls), 1)

    def test_the_drain_runs_when_the_lock_fails_for_another_reason(self) -> None:
        try:
            import fcntl  # noqa: F401
        except ImportError:
            self.skipTest("no fcntl")
        self.write_spool([self.spool_line()])
        fake = self.fake()
        with patch("fcntl.flock", side_effect=OSError(errno.ENOLCK, "No locks available")):
            hook.drain_ledger_spool(config=hook.load_ledger_config(), ledger_http=fake)
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual(self.spool_lines(), [])

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
        hook.drain_ledger_spool(config=hook.load_ledger_config(), ledger_http=self.fake())
        self.assertEqual(self.spool_lines(), [])
        self.assertEqual(stat.S_IMODE(self.spool.stat().st_mode), 0o600)


class DeadlineTests(LedgerTestCase):
    """One deadline for the new event and the resend, sent in parallel."""

    def test_slow_governance_and_a_hanging_ledger_overlap(self) -> None:
        os.environ.update(GOVERNANCE)
        self.timeout(0.5)

        def slow_allow(method: str, url: str, payload: Any, timeout: float) -> Tuple[int, Dict[str, Any]]:
            threading.Event().wait(0.5)
            return 200, {"approved": True}

        # A frozen clock must not stretch the deadline: it runs on real monotonic time.
        for clock in (time.monotonic, FakeClock()):
            with self.subTest(clock=clock):
                self.spool.unlink(missing_ok=True)
                began = time.monotonic()
                output = self.run_hook(hook_input(), self.fake("hang"), clock=clock, http=slow_allow)
                self.assertLess(time.monotonic() - began, 0.8)
                self.assertIsNone(output)
                self.assertEqual([event["event"] for event in self.spooled()], ["agent.tool_call.started"])

    def test_the_new_event_and_the_resend_are_in_flight_together(self) -> None:
        self.write_spool([self.spool_line()])
        lock = threading.Lock()
        arrived: List[int] = []
        together = threading.Event()
        overlapped: List[bool] = []

        def answer() -> Tuple[int, bytes]:
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
        self.assertLess(time.monotonic() - began, 1)
        self.assertEqual([call["events"][0]["event"] for call in fake.new_calls()], ["agent.tool_call.ran"])
        self.assertEqual(self.spool_lines(), lines)

    def test_a_send_that_fails_after_the_deadline_leaves_one_line(self) -> None:
        self.timeout(0.2)
        late = threading.Event()

        def fail_late() -> Any:
            late.wait(5)
            raise OSError("timed out")

        self.run_hook(hook_input(), self.fake(fail_late))
        late.set()
        for thread in hook.LEDGER_THREADS:
            thread.join(5)
        self.assertEqual([event["event"] for event in self.spooled()], ["agent.tool_call.started"])

    def test_a_resend_that_finishes_in_time_removes_its_lines_before_the_hook_returns(self) -> None:
        self.write_spool([self.spool_line() for _ in range(3)])

        def slow_accept() -> Tuple[int, bytes]:
            threading.Event().wait(0.1)
            return 202, b"{}"

        self.run_hook(hook_input("PostToolUse"), self.fake(resends=[slow_accept]))
        self.assertEqual(self.spool_lines(), [])

    def test_a_save_while_a_resend_rewrites_the_file_loses_no_line(self) -> None:
        try:
            import fcntl  # noqa: F401
        except ImportError:
            self.skipTest("no fcntl")
        config = hook.load_ledger_config()
        self.write_spool([self.spool_line()])
        late = self.build("started", tool_use_id="late")
        real_replace = os.replace
        saved = threading.Event()

        def save() -> None:
            hook._save_ledger(config=config, events=[late])
            saved.set()

        def replace(source: Any, target: Any) -> None:
            # Another thread saves mid-rewrite. It must wait for the rewrite, even with no flock.
            threading.Thread(target=save, daemon=True).start()
            saved.wait(0.2)
            real_replace(source, target)

        with patch("fcntl.flock", side_effect=OSError(errno.ENOLCK, "No locks available")), \
                patch.object(hook.os, "replace", replace):
            hook.drain_ledger_spool(config=config, ledger_http=self.fake())
            self.assertTrue(saved.wait(5))
        self.assertEqual(self.spooled(), [late])


class SafetyTests(LedgerTestCase):
    def governed_outputs(self, ledger_http: Any) -> List[Any]:
        outputs = []
        for answer in ((200, {"approved": True}), (200, {"approved": False, "reason": "no"}), hook.HookContractError("down")):
            outputs.append(self.run_hook(hook_input(), ledger_http, http=GovernanceHttp(answer)))
        return outputs

    def test_decisions_and_deny_text_ignore_ledger_failures(self) -> None:
        os.environ.update(GOVERNANCE)
        expected = self.governed_outputs(self.fake())
        for failure in (OSError("refused"), RuntimeError("boom"), ValueError("bad"), (500, b""), (400, b"")):
            with self.subTest(failure=failure):
                self.assertEqual(self.governed_outputs(self.fake(failure, failure, failure, failure, failure, failure)), expected)
        with patch.object(hook, "ledger_event", side_effect=RuntimeError("build failed")):
            self.assertEqual(self.governed_outputs(self.fake()), expected)
        with patch.object(hook, "start_ledger", side_effect=RuntimeError("no threads")):
            self.assertEqual(self.governed_outputs(self.fake()), expected)
        with patch.object(hook, "_spool_ledger", side_effect=OSError("disk full")):
            self.assertEqual(self.governed_outputs(self.fake(OSError("refused"))), expected)
        with patch.object(hook, "load_ledger_config", side_effect=RuntimeError("odd")):
            self.assertEqual(self.governed_outputs(self.fake()), expected)

    def test_the_local_audit_line_ignores_ledger_failures(self) -> None:
        os.environ.update(GOVERNANCE)
        self.run_hook(hook_input("PostToolUse"), self.fake(RuntimeError("boom")))
        self.run_hook(hook_input("PermissionDenied"), self.fake(OSError("refused")))
        records = [json.loads(line) for line in self.audit_log.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([record["status"] for record in records], ["observed", "blocked_by_auto_mode"])
        self.assertEqual(
            sorted(records[0]),
            ["agent", "decision_id", "request_id", "result_preview", "status", "tool_name", "tool_namespace", "ts"],
        )

    def test_a_stderr_that_raises_cannot_change_the_decision(self) -> None:
        os.environ.update(GOVERNANCE)

        class Broken(StringIO):
            def write(self, text: str) -> int:
                raise OSError("stderr is gone")

        denied = hook.deny_output("Mezmo Governance denied this exact tool call: no")
        for target in ("ledger_event", "start_ledger"):
            with self.subTest(target=target), patch.object(hook, target, side_effect=RuntimeError("boom")), \
                    patch.object(sys, "stderr", Broken()):
                output = hook.handle_event(
                    hook_input(), GovernanceHttp((200, {"approved": False, "reason": "no"})), ledger_http=self.fake()
                )
                self.assertEqual(output, denied)
                self.assertIsNone(hook.handle_event(hook_input("PostToolUse"), ledger_http=self.fake()))
                self.assertIsNone(hook.handle_event(hook_input("PermissionDenied"), ledger_http=self.fake()))

    def run_main(self, event: Mapping[str, Any]) -> Tuple[Any, str, Any]:
        """main() in this process, with os._exit replaced so a live ledger thread can't end the run."""
        with patch.object(sys, "stdin", StringIO(json.dumps(event))), \
                patch.object(sys, "stdout", StringIO()) as stdout, \
                patch.object(sys, "stderr", StringIO()), \
                patch.object(os, "_exit") as exit_now:
            code = hook.main()
        return code, stdout.getvalue(), exit_now

    def test_main_exits_as_before_when_the_ledger_fails(self) -> None:
        fake = self.fake(RuntimeError("boom"))
        with patch.object(hook, "post_ledger", fake):
            code, stdout, exit_now = self.run_main(hook_input())
        self.assertEqual(code, 0)
        self.assertEqual(stdout, "")
        exit_now.assert_not_called()
        self.assertEqual(len(fake.calls), 1)

    def test_main_ends_the_process_while_a_ledger_thread_runs(self) -> None:
        self.timeout(0.2)
        fake = self.fake("hang")
        with patch.object(hook, "post_ledger", fake):
            _, stdout, exit_now = self.run_main(hook_input())
        exit_now.assert_called_once_with(0)
        self.assertEqual(stdout, "")
        self.assertEqual(len(fake.calls), 1)

    def test_a_hanging_ledger_call_leaves_the_decision_and_saves_the_events(self) -> None:
        os.environ.update(GOVERNANCE)
        self.timeout(0.2)
        fake = self.fake("hang", "hang", "hang")
        began = time.monotonic()
        output = self.run_hook(hook_input(), fake, http=GovernanceHttp((200, {"approved": False, "reason": "no"})))
        self.assertLess(time.monotonic() - began, 2)
        self.assertEqual(output, hook.deny_output("Mezmo Governance denied this exact tool call: no"))
        self.assertEqual(len(fake.calls), 1)
        self.assertEqual([event["event"] for event in self.spooled()], ["agent.tool_call.started", "agent.tool_call.not_run"])

        began = time.monotonic()
        self.assertIsNone(self.run_hook(hook_input("PostToolUse", tool_use_id="tool-use-2"), fake))
        self.assertLess(time.monotonic() - began, 2)
        self.assertEqual(self.spooled()[-1]["event"], "agent.tool_call.ran")


class EndToEndTests(unittest.TestCase):
    """The real script, run as Claude Code runs it, against an audit-service defined here."""

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = pathlib.Path(scratch.name)
        self.spool = self.dir / "ledger-spool.jsonl"
        self.recorder = Recorder()
        url = serve(self, self.recorder)
        self.env = {
            "HOME": str(self.dir),
            "MEZMO_GOVERNANCE_AUDIT_LOG": str(self.dir / "audit.jsonl"),
            "MEZMO_GOVERNANCE_STATE_DIR": str(self.dir / "sessions"),
            "MEZMO_LEDGER_ACCOUNT": ACCOUNT,
            "MEZMO_LEDGER_SPOOL": str(self.spool),
            "MEZMO_LEDGER_URL": url,
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
        }

    def run_hook(self, event: Mapping[str, Any]) -> subprocess.CompletedProcess:
        return subprocess.run(
            [sys.executable, str(SCRIPTS / "governance_hook.py")],
            input=json.dumps(event),
            capture_output=True,
            text=True,
            env=self.env,
            timeout=30,
        )

    def events(self) -> List[Dict[str, Any]]:
        return [event for request in self.recorder.requests for event in request["body"]]

    def test_started_and_ran_reach_the_server(self) -> None:
        for name in ("PreToolUse", "PostToolUse"):
            result = self.run_hook(hook_input(name))
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertEqual(result.stdout, "")
        for request in self.recorder.requests:
            self.assertIsInstance(request["body"], list)
            self.assertEqual(request["path"], "/v1/events")
        started, ran = self.events()
        self.assertEqual((started["event"], ran["event"]), ("agent.tool_call.started", "agent.tool_call.ran"))
        self.assertEqual(started["extra"]["call_id"], ran["extra"]["call_id"])
        self.assertFalse(self.spool.exists())

    def test_events_saved_on_a_503_are_delivered_by_a_later_run(self) -> None:
        self.recorder.status = 503
        self.run_hook(hook_input("PreToolUse"))
        self.run_hook(hook_input("PostToolUse"))
        saved = [json.loads(line)["event"] for line in self.spool.read_text(encoding="utf-8").splitlines()]
        self.assertEqual([event["event"] for event in saved], ["agent.tool_call.started", "agent.tool_call.ran"])
        self.recorder.status = 202
        self.recorder.requests.clear()
        result = self.run_hook(hook_input("PreToolUse", tool_use_id="tool-use-2"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(saved, [request["body"] for request in self.recorder.requests])
        self.assertEqual(len(self.recorder.requests), 2)
        self.assertEqual(self.spool.read_text(encoding="utf-8"), "")

    def test_a_server_that_never_answers_costs_at_most_the_deadline(self) -> None:
        self.recorder.hang = True
        began = time.monotonic()
        result = self.run_hook(hook_input("PreToolUse"))
        elapsed = time.monotonic() - began
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertLess(elapsed, hook.LEDGER_TIMEOUT_SECONDS + 2)
        (line,) = self.spool.read_text(encoding="utf-8").splitlines()
        self.assertEqual(json.loads(line)["event"]["event"], "agent.tool_call.started")


if __name__ == "__main__":
    unittest.main()
