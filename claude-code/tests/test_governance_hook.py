"""Tests for the Mezmo Governance hook (scripts/governance_hook.py).

The first group is the contract suite of the Claude Code adapter in the governance plugins
repo; the rest cover what changed here: the service's 207 pending answers, governance off
until configured, plugin options, and error messages.
"""

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
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from typing import Any, Dict, List, Mapping, Optional, Tuple, Union
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import governance_hook as hook  # noqa: E402

AUTHORIZE_URL = "https://api.example.test/governance/workflow/authorize"
ENV = {
    "MEZMO_GOVERNANCE_URL": AUTHORIZE_URL,
    "MEZMO_GOVERNANCE_ACCESS_KEY": "sts_test",
    "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "30",
    "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "1",
}
GOVERNANCE_VARS = (
    "MEZMO_GOVERNANCE_URL",
    "MEZMO_GOVERNANCE_ACCESS_KEY",
    "MEZMO_GOVERNANCE_TOKEN",
    "MEZMO_GOVERNANCE_REQUIRED",
    "MEZMO_GOVERNANCE_TIMEOUT_SECONDS",
    "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS",
    "MEZMO_GOVERNANCE_AUDIT_LOG",
    "MEZMO_GOVERNANCE_STATE_DIR",
    "CLAUDE_PLUGIN_OPTION_GOVERNANCE_URL",
    "CLAUDE_PLUGIN_OPTION_GOVERNANCE_ACCESS_KEY",
    "CLAUDE_PLUGIN_OPTION_GOVERNANCE_ENABLED",
    "CLAUDE_PLUGIN_DATA",
)


# Status files and the audit log go here, never to the real ~/.mezmo.
SCRATCH = tempfile.TemporaryDirectory()


def environment(**values: str) -> Dict[str, str]:
    """os.environ without any governance setting from the machine running the tests."""
    env = {k: v for k, v in os.environ.items() if k not in GOVERNANCE_VARS}
    env["MEZMO_GOVERNANCE_STATE_DIR"] = os.path.join(SCRATCH.name, "sessions")
    env["MEZMO_GOVERNANCE_AUDIT_LOG"] = os.path.join(SCRATCH.name, "audit.jsonl")
    env.update(values)
    return env


def hook_event(name: str) -> Dict[str, Any]:
    event: Dict[str, Any] = {
        "hook_event_name": name,
        "session_id": "session-1",
        "turn_id": "turn-1",
        "tool_use_id": "tool-use-1",
        "tool_name": "mcp__kubernetes_demo__pods_delete",
        "tool_input": {"namespace": "staging", "name": "demo-pod"},
        "cwd": "/tmp/demo",
        "permission_mode": "default",
    }
    if name == "PostToolUse":
        event["tool_response"] = {"content": "pod deleted", "isError": False}
    return event


Response = Union[Tuple[int, Dict[str, Any]], BaseException]


class FakeHttp:
    def __init__(self, responses: List[Response]) -> None:
        self.responses = list(responses)
        self.calls: List[Tuple[str, str, Optional[Mapping[str, Any]], float]] = []

    def __call__(
        self, method: str, url: str, payload: Optional[Mapping[str, Any]], timeout: float
    ) -> Tuple[int, Dict[str, Any]]:
        self.calls.append((method, url, payload, timeout))
        response = self.responses.pop(0)
        if isinstance(response, BaseException):
            raise response
        return response


class FakeClock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now

    def sleep(self, seconds: float) -> None:
        self.now += seconds


def reason_of(output: Mapping[str, Any]) -> str:
    return output["hookSpecificOutput"]["permissionDecisionReason"]


def decision_of(output: Mapping[str, Any]) -> str:
    return output["hookSpecificOutput"]["permissionDecision"]


@patch.dict(os.environ, environment(**ENV), clear=True)
class AdapterContractTests(unittest.TestCase):
    """The adapter's suite, unchanged apart from the module name."""

    def test_normalizes_mcp_identity_without_changing_arguments(self) -> None:
        event = hook_event("PreToolUse")
        normalized = hook.normalize_tool_call(event)
        self.assertEqual(normalized.namespace, "kubernetes_demo")
        self.assertEqual(normalized.name, "pods_delete")
        self.assertEqual(normalized.arguments, event["tool_input"])

    def test_reuses_the_same_identity_for_pre_and_post_events(self) -> None:
        before = hook.call_identity(hook_event("PreToolUse"))
        after = hook.call_identity(hook_event("PostToolUse"))
        self.assertEqual(before, after)
        self.assertRegex(before.request_id, r"^claude_code:session-1:turn-1:tool-use-1#[0-9a-f]{16}$")

    def test_approval_payload_identifies_claude_code_and_the_mcp_namespace(self) -> None:
        payload = hook.approval_payload(hook_event("PreToolUse"))
        self.assertEqual(payload["origin"], {"kind": "claude_code_hook", "reason": "PreToolUse"})
        self.assertEqual(payload["items"][0]["tool_namespace"], "kubernetes_demo")
        self.assertEqual(payload["items"][0]["tool_name"], "pods_delete")

    def test_allows_an_approved_call_without_rewriting_it(self) -> None:
        http = FakeHttp([(200, {"approved": True})])
        output = hook.handle_event(hook_event("PreToolUse"), http=http)
        self.assertIsNone(output)
        self.assertEqual(http.calls[0][0], "POST")
        self.assertEqual(http.calls[0][1], AUTHORIZE_URL)
        self.assertEqual(http.calls[0][2]["items"][0]["arguments"], hook_event("PreToolUse")["tool_input"])

    def test_maps_a_denial_to_a_valid_pre_tool_hook_decision(self) -> None:
        http = FakeHttp([(200, {"approved": False, "reason": "Use a canary first."})])
        output = hook.handle_event(hook_event("PreToolUse"), http=http)
        self.assertEqual(decision_of(output), "deny")
        self.assertIn("Use a canary first.", reason_of(output))

    def test_polls_after_a_client_timeout_then_allows(self) -> None:
        http = FakeHttp([
            TimeoutError("POST held too long"),
            (200, {"status": "pending", "reason": None}),
            (200, {"status": "approved", "reason": "ok"}),
        ])
        clock = FakeClock()
        output = hook.handle_event(hook_event("PreToolUse"), http=http, clock=clock, sleep=clock.sleep)
        self.assertIsNone(output)
        self.assertEqual(http.calls[1][0], "GET")
        self.assertIn("decision_id=", http.calls[1][1])

    def test_polls_after_conflict_then_denies(self) -> None:
        http = FakeHttp([(409, {"code": "ECONFLICT"}), (200, {"status": "denied", "reason": "already decided"})])
        output = hook.handle_event(hook_event("PreToolUse"), http=http)
        self.assertEqual(decision_of(output), "deny")
        self.assertIn("already decided", reason_of(output))

    def test_fails_closed_when_the_governance_request_fails(self) -> None:
        http = FakeHttp([hook.HookContractError("connection refused")])
        output = hook.handle_event(hook_event("PreToolUse"), http=http)
        self.assertEqual(decision_of(output), "deny")
        self.assertIn("connection refused", reason_of(output))

    def test_fails_closed_when_approval_stays_pending(self) -> None:
        clock = FakeClock()
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "1"}):
            http = FakeHttp([
                TimeoutError("held"),
                (200, {"status": "pending", "reason": None}),
                (200, {"status": "pending", "reason": None}),
            ])
            output = hook.handle_event(hook_event("PreToolUse"), http=http, clock=clock, sleep=clock.sleep)
        self.assertEqual(decision_of(output), "deny")
        self.assertIn("approval still pending", reason_of(output))

    def test_malformed_stdin_uses_the_blocking_exit_code(self) -> None:
        with patch.object(sys, "stdin", StringIO("not-json")), patch.object(sys, "stderr", StringIO()):
            exit_code = hook.main()
        self.assertEqual(exit_code, 2)

    def test_reports_post_tool_output_locally_and_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = pathlib.Path(tmp) / "audit.jsonl"
            with patch.dict(os.environ, {"MEZMO_GOVERNANCE_AUDIT_LOG": str(log_path)}):
                output = hook.handle_event(hook_event("PostToolUse"))
            self.assertIsNone(output)
            records = [json.loads(line) for line in log_path.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(len(records), 1)
        self.assertEqual(records[0]["agent"], "claude_code")
        self.assertEqual(records[0]["tool_name"], "pods_delete")
        self.assertEqual(records[0]["status"], "observed")

    def test_post_tool_audit_failure_does_not_block(self) -> None:
        with patch.object(hook, "append_audit", side_effect=OSError("disk full")), \
                patch.object(sys, "stderr", StringIO()) as stderr:
            output = hook.handle_event(hook_event("PostToolUse"))
        self.assertIsNone(output)
        self.assertIn("disk full", stderr.getvalue())

    def test_authorization_header_prefixes_token(self) -> None:
        self.assertEqual(hook.authorization_header(), "Token sts_test")
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_ACCESS_KEY": "Token sts_test"}):
            self.assertEqual(hook.authorization_header(), "Token sts_test")


@patch.dict(os.environ, environment(**ENV), clear=True)
class ServiceProtocolTests(unittest.TestCase):
    """The service answers 207 while a person decides, with {approved, reason} on both calls."""

    def test_pending_post_with_approved_false_is_not_a_denial(self) -> None:
        clock = FakeClock()
        http = FakeHttp([
            (207, {"approved": False, "reason": "No rule in this policy covers this request."}),
            (207, {"approved": False, "reason": None}),
            (200, {"approved": True, "reason": "looked at it"}),
        ])
        output = hook.handle_event(hook_event("PreToolUse"), http=http, clock=clock, sleep=clock.sleep)
        self.assertIsNone(output)
        self.assertEqual([c[0] for c in http.calls], ["POST", "GET", "GET"])
        decision_id = http.calls[0][2]["decision_id"]
        self.assertTrue(http.calls[1][1].endswith(f"?decision_id={decision_id}"))

    def test_human_denial_after_pending(self) -> None:
        clock = FakeClock()
        http = FakeHttp([(207, {"approved": False, "reason": None}), (200, {"approved": False, "reason": "not today"})])
        output = hook.handle_event(hook_event("PreToolUse"), http=http, clock=clock, sleep=clock.sleep)
        self.assertEqual(decision_of(output), "deny")
        self.assertIn("not today", reason_of(output))

    def test_poll_waits_through_404_until_the_decision_exists(self) -> None:
        clock = FakeClock()
        http = FakeHttp([TimeoutError("held"), (404, {"code": "ENOENT"}), (200, {"approved": True, "reason": None})])
        output = hook.handle_event(hook_event("PreToolUse"), http=http, clock=clock, sleep=clock.sleep)
        self.assertIsNone(output)

    def test_still_pending_at_the_deadline_denies(self) -> None:
        clock = FakeClock()
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "2"}):
            http = FakeHttp([(207, {"approved": False, "reason": None})] + [(207, {"approved": False, "reason": None})] * 5)
            output = hook.handle_event(hook_event("PreToolUse"), http=http, clock=clock, sleep=clock.sleep)
        self.assertIn("approval still pending", reason_of(output))

    def test_no_agent_for_the_key(self) -> None:
        body = {"status": 403, "code": "ENOAGENT", "message": "no agent is registered for these credentials"}
        output = hook.handle_event(hook_event("PreToolUse"), http=FakeHttp([(403, body)]))
        self.assertEqual(decision_of(output), "deny")
        self.assertIn("ENOAGENT", reason_of(output))
        self.assertIn("no agent is registered", reason_of(output))

    def test_rejected_key(self) -> None:
        body = {"status": 401, "code": "EAUTH", "message": "Unauthorized"}
        output = hook.handle_event(hook_event("PreToolUse"), http=FakeHttp([(401, body)]))
        self.assertIn("access key", reason_of(output))
        # A session whose plugin was reinstalled keeps sending a key that's gone: say how to recover.
        self.assertIn("/reload-plugins", reason_of(output))
        self.assertIn("/mezmo:setup governance", reason_of(output))

    def test_argument_name_the_service_rejects(self) -> None:
        # The body the dev service returned for a Grep call with "-i".
        body = {
            "status": 400, "code": "EINVAL", "message": "Bad Request", "context": "body",
            "errors": [
                {"keyword": "pattern", "params": {"pattern": "^[a-zA-Z_][a-zA-Z0-9_]*$"},
                 "message": 'must match pattern "^[a-zA-Z_][a-zA-Z0-9_]*$"', "instance_path": "/items/0/arguments",
                 "schema_path": "authorize-item#/properties/arguments/propertyNames/pattern"},
            ],
        }
        event = hook_event("PreToolUse")
        event["tool_name"] = "mcp__search__grep"
        event["tool_input"] = {"pattern": "x", "-i": True, "-C": 2}
        output = hook.handle_event(event, http=FakeHttp([(400, body)]))
        self.assertEqual(decision_of(output), "deny")
        self.assertIn("'-i', '-C'", reason_of(output))
        self.assertIn("argument names", reason_of(output))
        self.assertNotIn("'pattern'", reason_of(output))

    def test_a_server_error_shows_no_server_text(self) -> None:
        # protocol.md rule 9: Mezmo's code and message only on a 4xx.
        body = {"code": "EINTERNAL", "message": "stack trace at db.js:12"}
        output = hook.handle_event(hook_event("PreToolUse"), http=FakeHttp([(500, body)]))
        self.assertEqual(decision_of(output), "deny")
        self.assertIn("Mezmo Governance returned HTTP 500", reason_of(output))
        self.assertNotIn("EINTERNAL", reason_of(output))
        self.assertNotIn("stack trace", reason_of(output))

    def test_a_poll_server_error_shows_no_server_text(self) -> None:
        clock = FakeClock()
        http = FakeHttp([(207, {"approved": False}), (502, {"code": "EGATEWAY", "message": "upstream said no"})])
        output = hook.handle_event(hook_event("PreToolUse"), http=http, clock=clock, sleep=clock.sleep)
        self.assertIn("Mezmo Governance returned HTTP 502 while polling", reason_of(output))
        self.assertNotIn("upstream said no", reason_of(output))

    def test_code_and_message_are_one_bounded_line_each(self) -> None:
        body = {"code": "EBAD\nCODE" + "C" * 300, "message": "first line\r\nsecond\tline " + "m" * 500}
        output = hook.handle_event(hook_event("PreToolUse"), http=FakeHttp([(403, body)]))
        reason = reason_of(output)
        self.assertNotIn("\n", reason)
        self.assertNotIn("\r", reason)
        self.assertNotIn("\t", reason)
        self.assertIn("EBAD CODE", reason)
        self.assertIn("first line second line", reason)
        self.assertNotIn("C" * 201, reason)
        self.assertNotIn("m" * 200, reason)

    def test_nested_error_detail_is_not_shown(self) -> None:
        body = {
            "code": "EINVAL", "message": "Bad Request",
            "errors": [{"message": "must not hold secret-value", "instance_path": "/items/0/tool_name"}],
        }
        output = hook.handle_event(hook_event("PreToolUse"), http=FakeHttp([(400, body)]))
        reason = reason_of(output)
        self.assertIn("HTTP 400 EINVAL: Bad Request", reason)
        self.assertNotIn("secret-value", reason)
        self.assertNotIn("/items/0/tool_name", reason)

    def test_bearer_key_is_sent_unchanged(self) -> None:
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_ACCESS_KEY": "Bearer sts_test"}):
            self.assertEqual(hook.authorization_header(), "Bearer sts_test")


class ConfigurationTests(unittest.TestCase):
    def test_unconfigured_allows_without_calling_the_service(self) -> None:
        http = FakeHttp([])
        with patch.dict(os.environ, environment(), clear=True):
            self.assertIsNone(hook.handle_event(hook_event("PreToolUse"), http=http))
        self.assertEqual(http.calls, [])

    def test_unconfigured_writes_no_audit_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = pathlib.Path(tmp) / "audit.jsonl"
            with patch.dict(os.environ, environment(MEZMO_GOVERNANCE_AUDIT_LOG=str(log_path)), clear=True):
                hook.handle_event(hook_event("PostToolUse"))
            self.assertFalse(log_path.exists())

    def test_required_but_unconfigured_denies(self) -> None:
        with patch.dict(os.environ, environment(MEZMO_GOVERNANCE_REQUIRED="1"), clear=True):
            output = hook.handle_event(hook_event("PreToolUse"), http=FakeHttp([]))
        self.assertEqual(decision_of(output), "deny")
        self.assertIn("required", reason_of(output))

    def test_plugin_option_key_with_the_default_url(self) -> None:
        http = FakeHttp([(200, {"approved": True})])
        with patch.dict(os.environ, environment(CLAUDE_PLUGIN_OPTION_GOVERNANCE_ACCESS_KEY="sts_opt"), clear=True):
            self.assertIsNone(hook.handle_event(hook_event("PreToolUse"), http=http))
            self.assertEqual(hook.authorization_header(), "Token sts_opt")
        self.assertEqual(http.calls[0][1], "https://api.mezmo.com/governance/workflow/authorize")

    def test_plugin_option_url(self) -> None:
        values = {"CLAUDE_PLUGIN_OPTION_GOVERNANCE_ACCESS_KEY": "sts_opt",
                  "CLAUDE_PLUGIN_OPTION_GOVERNANCE_URL": "https://governance.example.test/governance/workflow/authorize"}
        with patch.dict(os.environ, environment(**values), clear=True):
            self.assertEqual(hook.load_config().url, values["CLAUDE_PLUGIN_OPTION_GOVERNANCE_URL"])

    def test_environment_wins_over_plugin_options(self) -> None:
        values = {"CLAUDE_PLUGIN_OPTION_GOVERNANCE_ACCESS_KEY": "sts_opt",
                  "CLAUDE_PLUGIN_OPTION_GOVERNANCE_URL": "https://opt.example.test/authorize",
                  "MEZMO_GOVERNANCE_ACCESS_KEY": "sts_env",
                  "MEZMO_GOVERNANCE_URL": "https://env.example.test/authorize"}
        with patch.dict(os.environ, environment(**values), clear=True):
            config = hook.load_config()
        self.assertEqual((config.url, config.access_key), ("https://env.example.test/authorize", "sts_env"))

    def test_aura_token_variable_is_accepted(self) -> None:
        with patch.dict(os.environ, environment(MEZMO_GOVERNANCE_TOKEN="sts_aura"), clear=True):
            self.assertEqual(hook.load_config().access_key, "sts_aura")

    def test_switched_off_plugin_option_turns_governance_off(self) -> None:
        values = {"CLAUDE_PLUGIN_OPTION_GOVERNANCE_ACCESS_KEY": "sts_opt", "CLAUDE_PLUGIN_OPTION_GOVERNANCE_ENABLED": "false"}
        with patch.dict(os.environ, environment(**values), clear=True):
            self.assertIsNone(hook.load_config())

    def test_placeholder_key_left_by_remove_is_unset(self) -> None:
        with patch.dict(os.environ, environment(CLAUDE_PLUGIN_OPTION_GOVERNANCE_ACCESS_KEY="off"), clear=True):
            self.assertIsNone(hook.load_config())

    def test_managed_key_ignores_the_plugin_switch(self) -> None:
        values = {"MEZMO_GOVERNANCE_ACCESS_KEY": "sts_env", "CLAUDE_PLUGIN_OPTION_GOVERNANCE_ENABLED": "false"}
        with patch.dict(os.environ, environment(**values), clear=True):
            self.assertEqual(hook.load_config().access_key, "sts_env")

    def test_blank_values_count_as_unset(self) -> None:
        values = {"CLAUDE_PLUGIN_OPTION_GOVERNANCE_ACCESS_KEY": "  ", "MEZMO_GOVERNANCE_ACCESS_KEY": ""}
        with patch.dict(os.environ, environment(**values), clear=True):
            self.assertIsNone(hook.load_config())


class SessionStateTests(unittest.TestCase):
    """The per-session status file the status mod reads: counts, pending decisions, the last result."""

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name) / "sessions"
        self.state = self.dir / "session-1.json"
        self._env = patch.dict(
            os.environ, environment(**ENV, MEZMO_GOVERNANCE_STATE_DIR=str(self.dir)), clear=True
        )
        self._env.start()

    def tearDown(self) -> None:
        self._env.stop()
        self._tmp.cleanup()

    def read(self) -> Dict[str, Any]:
        return json.loads(self.state.read_text())

    def test_counts_an_allow_without_writing_arguments(self) -> None:
        hook.handle_event(hook_event("PreToolUse"), http=FakeHttp([(200, {"approved": True})]))
        state = self.read()
        self.assertEqual(state["session_id"], "session-1")
        self.assertEqual(state["counts"], {"allowed": 1, "denied": 0, "expired": 0, "errors": 0})
        self.assertEqual(state["pending"], {})
        self.assertEqual(state["last"]["outcome"], "allowed")
        self.assertEqual(state["last"]["tool"], "kubernetes_demo/pods_delete")
        self.assertEqual(state["last"]["tool_name"], "mcp__kubernetes_demo__pods_delete")
        self.assertFalse(state["last"]["by_person"])
        self.assertEqual(state["host"], "api.example.test")
        self.assertNotIn("demo-pod", self.state.read_text())

    def test_records_a_pending_decision_while_it_waits(self) -> None:
        seen: List[Dict[str, Any]] = []
        clock = FakeClock()

        def sleep(seconds: float) -> None:
            seen.append(self.read())
            clock.sleep(seconds)

        http = FakeHttp([
            (207, {"approved": False, "reason": "No rule in this policy covers this request."}),
            (207, {"approved": False, "reason": None}),
            (200, {"approved": True, "reason": "fine"}),
        ])
        before = int(time.time() * 1000)
        hook.handle_event(hook_event("PreToolUse"), http=http, clock=clock, sleep=sleep)

        decision_id = http.calls[0][2]["decision_id"]
        pending = seen[0]["pending"][decision_id]
        self.assertEqual(pending["tool_name"], "mcp__kubernetes_demo__pods_delete")
        self.assertEqual(pending["reason"], "No rule in this policy covers this request.")
        self.assertGreaterEqual(pending["since_ms"], before)
        self.assertAlmostEqual(pending["deadline_ms"] - pending["since_ms"], 30_000, delta=1_000)
        state = self.read()
        self.assertEqual(state["pending"], {})
        self.assertEqual(state["last"]["outcome"], "allowed")
        self.assertTrue(state["last"]["by_person"])
        self.assertEqual(state["last"]["detail"], "fine")
        self.assertEqual(state["last"]["decision_id"], decision_id)

    def test_a_person_denial(self) -> None:
        clock = FakeClock()
        http = FakeHttp([(207, {"approved": False, "reason": None}), (200, {"approved": False, "reason": "not today"})])
        hook.handle_event(hook_event("PreToolUse"), http=http, clock=clock, sleep=clock.sleep)
        state = self.read()
        self.assertEqual(state["counts"]["denied"], 1)
        self.assertEqual(state["last"]["outcome"], "denied")
        self.assertTrue(state["last"]["by_person"])
        self.assertEqual(state["last"]["detail"], "not today")

    def test_nobody_decides_in_time(self) -> None:
        clock = FakeClock()
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "2"}):
            http = FakeHttp([(207, {"approved": False, "reason": None})] * 6)
            hook.handle_event(hook_event("PreToolUse"), http=http, clock=clock, sleep=clock.sleep)
        state = self.read()
        self.assertEqual(state["pending"], {})
        self.assertEqual(state["counts"]["expired"], 1)
        self.assertEqual(state["last"]["outcome"], "expired")
        self.assertTrue(state["last"]["by_person"])

    def test_counts_an_error(self) -> None:
        hook.handle_event(hook_event("PreToolUse"), http=FakeHttp([(403, {"code": "ENOAGENT", "message": "no agent"})]))
        state = self.read()
        self.assertEqual(state["counts"]["errors"], 1)
        self.assertEqual(state["last"]["outcome"], "error")
        self.assertIn("ENOAGENT", state["last"]["detail"])

    def test_counts_accumulate(self) -> None:
        for answer in ({"approved": True}, {"approved": False, "reason": "no"}, {"approved": True}):
            hook.handle_event(hook_event("PreToolUse"), http=FakeHttp([(200, answer)]))
        self.assertEqual(self.read()["counts"], {"allowed": 2, "denied": 1, "expired": 0, "errors": 0})

    def test_parallel_calls_do_not_lose_counts(self) -> None:
        def call() -> None:
            hook.handle_event(hook_event("PreToolUse"), http=FakeHttp([(200, {"approved": True})]))

        threads = [threading.Thread(target=call) for _ in range(12)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        self.assertEqual(self.read()["counts"]["allowed"], 12)

    def test_unconfigured_writes_nothing(self) -> None:
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_ACCESS_KEY": "", "MEZMO_GOVERNANCE_URL": ""}):
            hook.handle_event(hook_event("PreToolUse"), http=FakeHttp([]))
        self.assertFalse(self.state.exists())

    def test_a_state_file_it_cannot_write_does_not_block(self) -> None:
        self.dir.parent.mkdir(parents=True, exist_ok=True)
        self.dir.write_text("not a directory")
        output = hook.handle_event(hook_event("PreToolUse"), http=FakeHttp([(200, {"approved": True})]))
        self.assertIsNone(output)

    def test_old_session_files_are_pruned(self) -> None:
        self.dir.mkdir(parents=True)
        old = self.dir / "old-session.json"
        old.write_text("{}")
        week_ago = time.time() - 8 * 24 * 3600
        os.utime(old, (week_ago, week_ago))
        hook.handle_event(hook_event("PreToolUse"), http=FakeHttp([(200, {"approved": True})]))
        self.assertFalse(old.exists())
        self.assertTrue(self.state.exists())

    def test_defaults_to_the_home_directory(self) -> None:
        os.environ.pop("MEZMO_GOVERNANCE_STATE_DIR")
        self.assertEqual(hook.session_state_path("abc"), pathlib.Path.home() / ".mezmo" / "governance" / "sessions" / "abc.json")



@patch.dict(os.environ, environment(**ENV), clear=True)
class CallBindingTests(unittest.TestCase):
    def test_changed_arguments_under_one_call_id_get_their_own_decision(self) -> None:
        # A 409 hands back the decision of an earlier POST with the same id.
        first = hook_event("PreToolUse")
        edited = hook_event("PreToolUse")
        edited["tool_input"] = {"namespace": "production", "name": "demo-pod"}
        self.assertNotEqual(hook.call_identity(first).decision_id, hook.call_identity(edited).decision_id)
        self.assertNotEqual(hook.call_identity(first).request_id, hook.call_identity(edited).request_id)

    def test_a_long_request_id_fits_mezmo_and_keeps_its_digest(self) -> None:
        # Mezmo answers HTTP 400 to a request_id over 128 characters.
        event = hook_event("PreToolUse")
        event["session_id"] = "s" * 300
        other = dict(event, session_id="s" * 299 + "t")
        identity = hook.call_identity(event)
        self.assertLessEqual(len(identity.request_id), 128)
        self.assertRegex(identity.request_id, r"#[0-9a-f]{16}$")
        self.assertNotEqual(identity.request_id, hook.call_identity(other).request_id)
        full = f"claude_code:{'s' * 300}:turn-1:tool-use-1#" + identity.request_id.rsplit("#", 1)[1]
        self.assertEqual(identity.decision_id, str(uuid.uuid5(uuid.NAMESPACE_URL, full)))


class RedirectTests(unittest.TestCase):
    def test_a_redirect_denies_and_never_sends_the_key_elsewhere(self) -> None:
        seen: List[Optional[str]] = []

        class Elsewhere(BaseHTTPRequestHandler):
            def do_GET(self) -> None:
                seen.append(self.headers.get("Authorization"))
                body = json.dumps({"approved": True}).encode()
                self.send_response(200)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            do_POST = do_GET

            def log_message(self, *args: Any) -> None:
                pass

        class Redirect(Elsewhere):
            def do_POST(self) -> None:
                self.send_response(302)
                self.send_header("Location", target)
                self.send_header("Content-Length", "0")
                self.end_headers()

        servers = [ThreadingHTTPServer(("127.0.0.1", 0), handler) for handler in (Elsewhere, Redirect)]
        for server in servers:
            threading.Thread(target=server.serve_forever, daemon=True).start()
            self.addCleanup(server.server_close)
            self.addCleanup(server.shutdown)
        target = f"http://127.0.0.1:{servers[0].server_port}/elsewhere"
        url = f"http://127.0.0.1:{servers[1].server_port}/governance/workflow/authorize"

        with patch.dict(os.environ, environment(**dict(ENV, MEZMO_GOVERNANCE_URL=url)), clear=True):
            output = hook.handle_event(hook_event("PreToolUse"))

        self.assertEqual(seen, [])
        self.assertEqual(decision_of(output), "deny")
        self.assertIn("HTTP 302", reason_of(output))


class UnreadableReplyTests(unittest.TestCase):
    def test_a_reply_it_cannot_read_denies(self) -> None:
        cases = [
            ([(200, b"<html>bad gateway</html>")], "invalid JSON response"),
            ([(200, b"[]")], "non-object response"),
            ([(200, b"")], "did not return an approval decision"),
            ([(200, b'{"reason": "no field"}')], "did not return an approval decision"),
            ([(200, b'{"approved": "true"}')], "did not return an approval decision"),
            ([(200, b'{"approved": 1}')], "did not return an approval decision"),
            ([(409, b"{}"), (200, b'{"status": "maybe"}')], "unexpected status"),
            ([(409, b"{}"), (200, b"{}")], "unexpected status"),
            ([(409, b"{}"), (200, b'{"approved": "yes"}')], "unexpected status"),
            ([(409, b"{}"), (200, b"not json")], "invalid JSON response"),
        ]
        for answers, message in cases:
            queue = list(answers)

            class Raw(BaseHTTPRequestHandler):
                def do_POST(self) -> None:
                    status, body = queue.pop(0)
                    self.send_response(status)
                    self.send_header("Content-Length", str(len(body)))
                    self.end_headers()
                    self.wfile.write(body)

                do_GET = do_POST

                def log_message(self, *args: Any) -> None:
                    pass

            server = ThreadingHTTPServer(("127.0.0.1", 0), Raw)
            threading.Thread(target=server.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{server.server_port}/governance/workflow/authorize"
            try:
                with self.subTest(answers=answers), patch.dict(os.environ, environment(**dict(ENV, MEZMO_GOVERNANCE_URL=url)), clear=True):
                    output = hook.handle_event(hook_event("PreToolUse"))
                    self.assertEqual(decision_of(output), "deny")
                    self.assertIn(message, reason_of(output))
            finally:
                server.shutdown()
                server.server_close()


@patch.dict(os.environ, environment(**ENV), clear=True)
class DeadlineBoundsTests(unittest.TestCase):
    def test_a_timeout_that_is_not_finite_denies_without_asking(self) -> None:
        for value in ("nan", "inf", "-inf"):
            with self.subTest(value=value), patch.dict(os.environ, {"MEZMO_GOVERNANCE_TIMEOUT_SECONDS": value}):
                http = FakeHttp([(200, {"approved": True})])
                output = hook.handle_event(hook_event("PreToolUse"), http=http)
                self.assertEqual(decision_of(output), "deny")
                self.assertIn("MEZMO_GOVERNANCE_TIMEOUT_SECONDS", reason_of(output))
                self.assertEqual(http.calls, [])

    def test_a_large_timeout_still_ends_before_the_hook_timeout(self) -> None:
        # hooks.json gives the hook 600 seconds; the deny must come first.
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "3600"}):
            http = FakeHttp([(200, {"approved": True})])
            hook.handle_event(hook_event("PreToolUse"), http=http, clock=FakeClock())
        self.assertLessEqual(http.calls[0][3], 590)

    def test_poll_interval_has_a_one_second_floor(self) -> None:
        # protocol.md: poll no more often than once a second, whatever the setting says.
        for configured in ("5e-324", "0.001", "0.5"):
            with self.subTest(configured=configured), \
                    patch.dict(os.environ, {"MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": configured}):
                self.assertEqual(hook.poll_interval_seconds(), 1.0)
                clock = FakeClock()
                slept: List[float] = []

                def sleep(seconds: float) -> None:
                    slept.append(seconds)
                    clock.sleep(seconds)

                http = FakeHttp([(207, {"approved": False, "reason": None}), (207, {}), (207, {}), (200, {"approved": True})])
                hook.handle_event(hook_event("PreToolUse"), http=http, clock=clock, sleep=sleep)
                self.assertEqual(slept, [1.0, 1.0])


class HookConfigTests(unittest.TestCase):
    def test_a_pre_tool_hook_that_fails_blocks_the_tool(self) -> None:
        # Without onFailure, Claude Code runs the tool when the hook crashes, times out
        # or finds no python3.
        config = json.loads((pathlib.Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text())
        handlers = [handler for group in config["hooks"]["PreToolUse"] for handler in group["hooks"]]
        self.assertTrue(handlers)
        for handler in handlers:
            self.assertEqual(handler.get("onFailure"), "block", handler)


@patch.dict(os.environ, environment(**ENV), clear=True)
class AuditLogTests(unittest.TestCase):
    @unittest.skipIf(os.name == "nt", "Windows has no POSIX file modes")
    def test_the_audit_log_is_private_to_its_owner(self) -> None:
        # The log keeps up to 2000 characters of each tool result.
        with tempfile.TemporaryDirectory() as tmp:
            fresh = pathlib.Path(tmp) / "fresh" / "audit.jsonl"
            older = pathlib.Path(tmp) / "older.jsonl"
            older.write_text("", encoding="utf-8")
            older.chmod(0o644)
            for path in (fresh, older):
                with self.subTest(log=path.name), patch.dict(os.environ, {"MEZMO_GOVERNANCE_AUDIT_LOG": str(path)}):
                    self.assertIsNone(hook.handle_event(hook_event("PostToolUse")))
                    self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
            self.assertEqual(stat.S_IMODE(fresh.parent.stat().st_mode), 0o700)

    def test_a_log_sent_to_dev_null_writes_without_a_warning(self) -> None:
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_AUDIT_LOG": os.devnull}), \
                patch.object(sys, "stderr", StringIO()) as stderr:
            self.assertIsNone(hook.handle_event(hook_event("PostToolUse")))
        self.assertEqual(stderr.getvalue(), "")


@patch.dict(os.environ, environment(**ENV), clear=True)
class ExposedTokenTests(unittest.TestCase):
    def test_a_url_that_is_not_https_denies_without_a_request(self) -> None:
        for url in ("http://api.example.test/authorize", "ftp://api.example.test/authorize"):
            with self.subTest(url=url), patch.dict(os.environ, {"MEZMO_GOVERNANCE_URL": url}), \
                    patch.object(hook, "build_opener") as opener:
                output = hook.handle_event(hook_event("PreToolUse"))
                self.assertEqual(decision_of(output), "deny")
                self.assertIn("must use https://", reason_of(output))
                opener.assert_not_called()

    def test_a_url_with_credentials_denies_without_quoting_them(self) -> None:
        url = "https://user:url-secret@api.example.test/authorize"
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_URL": url}):
            output = hook.handle_event(hook_event("PreToolUse"))
        self.assertEqual(decision_of(output), "deny")
        self.assertIn("must not hold a user name or password", reason_of(output))
        self.assertNotIn("url-secret", reason_of(output))

    def test_a_token_a_header_cannot_carry_denies_without_quoting_it(self) -> None:
        for token in ("sts_te\nst-secret", "sts_t\u00e9st-secret"):
            with self.subTest(token=token), patch.dict(os.environ, {"MEZMO_GOVERNANCE_ACCESS_KEY": token}):
                output = hook.handle_event(hook_event("PreToolUse"))
                self.assertEqual(decision_of(output), "deny")
                self.assertIn("an HTTP header cannot carry", reason_of(output))
                self.assertNotIn("secret", reason_of(output))


@patch.dict(os.environ, environment(**ENV), clear=True)
class HostileInputTests(unittest.TestCase):
    def test_deeply_nested_input_denies_without_a_traceback(self) -> None:
        depth = 200_000
        event = json.dumps(hook_event("PreToolUse")).replace(
            '"tool_input": {', '"tool_input": {"deep": ' + "[" * depth + "]" * depth + ", ", 1
        )
        script = pathlib.Path(hook.__file__).resolve()
        result = subprocess.run(
            [sys.executable, str(script)], input=event, capture_output=True, text=True,
            env=dict(os.environ), timeout=60,
        )
        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertNotIn("Traceback", result.stderr)
        self.assertEqual(decision_of(json.loads(result.stdout)), "deny")

    def test_an_mcp_server_named_like_the_agent_denies_without_asking(self) -> None:
        for name in ("mcp__claude_code__Bash", "mcp__Claude_Code__Bash"):
            with self.subTest(name=name):
                event = {**hook_event("PreToolUse"), "tool_name": name, "tool_input": {"command": "ls"}}
                http = FakeHttp([(200, {"approved": True})])
                output = hook.handle_event(event, http=http)
                self.assertEqual(decision_of(output), "deny")
                self.assertIn("would pass as a built-in tool", reason_of(output))
                self.assertEqual(http.calls, [])


@patch.dict(os.environ, environment(**ENV), clear=True)
class WhichCallsAreGovernedTests(unittest.TestCase):
    """Mezmo Governance decides Bash and MCP tool calls. Other built-in tools and the Claude app's own servers stay out."""

    def call(self, name: str, event_name: str = "PreToolUse", http: Optional[FakeHttp] = None):
        event = {**hook_event(event_name), "tool_name": name, "tool_input": {"command": "ls"}}
        http = http if http is not None else FakeHttp([(200, {"approved": True})])
        return hook.handle_event(event, http=http), http

    def test_bash_is_governed(self) -> None:
        output, http = self.call("Bash")
        self.assertIsNone(output)
        item = http.calls[0][2]["items"][0]
        self.assertEqual((item["tool_namespace"], item["tool_name"]), ("claude_code", "Bash"))

    def test_other_built_in_tools_are_not_sent(self) -> None:
        for name in ("Read", "Edit", "Write", "ToolSearch", "Agent", "WebFetch"):
            with self.subTest(name=name):
                output, http = self.call(name)
                self.assertIsNone(output)
                self.assertEqual(http.calls, [])

    def test_the_claude_apps_own_servers_are_not_sent(self) -> None:
        for name in ("mcp__ccd_turn__wrap_up", "mcp__ccd_turn__update_plan", "mcp__ccd_session__start_session",
                     "mcp__ccd_view__show_pane"):
            with self.subTest(name=name):
                output, http = self.call(name)
                self.assertIsNone(output)
                self.assertEqual(http.calls, [])

    def test_a_server_only_named_like_the_apps_is_governed(self) -> None:
        # Only the app's exact server names are skipped, so a look-alike can't slip past.
        output, http = self.call("mcp__ccd_turn_extra__wrap_up")
        self.assertIsNone(output)
        self.assertEqual(len(http.calls), 1)

    def test_other_mcp_servers_are_governed(self) -> None:
        for name in ("mcp__mezmo__list_pipelines", "mcp__kubernetes_demo__pods_delete", "mcp__terminal__run_in_terminal"):
            with self.subTest(name=name):
                output, http = self.call(name)
                self.assertIsNone(output)
                self.assertEqual(http.calls[0][2]["items"][0]["tool_name"], name.split("__", 2)[2])

    def test_a_call_without_a_tool_name_is_still_denied(self) -> None:
        event = {**hook_event("PreToolUse"), "tool_input": {}}
        del event["tool_name"]
        http = FakeHttp([])
        self.assertEqual(decision_of(hook.handle_event(event, http=http)), "deny")
        self.assertEqual(http.calls, [])

    def test_required_governance_only_blocks_governed_calls(self) -> None:
        with patch.dict(os.environ, environment(MEZMO_GOVERNANCE_REQUIRED="1"), clear=True):
            output, _ = self.call("Read")
            self.assertIsNone(output)
            for name in ("Bash", "mcp__mezmo__publish_pipeline"):
                with self.subTest(name=name):
                    output, _ = self.call(name, http=FakeHttp([]))
                    self.assertEqual(decision_of(output), "deny")

    def test_post_tool_events_for_other_built_ins_write_no_audit_record(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log = pathlib.Path(tmp) / "audit.jsonl"
            with patch.dict(os.environ, {"MEZMO_GOVERNANCE_AUDIT_LOG": str(log)}):
                self.call("Read", event_name="PostToolUse")
                self.assertFalse(log.exists())
                self.call("Bash", event_name="PostToolUse")
                self.assertTrue(log.exists())

    def test_the_plugin_only_runs_the_hook_for_bash_and_mcp_tools(self) -> None:
        # Claude Code tests a matcher like this one as a regular expression against the tool name.
        hooks = json.loads((pathlib.Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text())["hooks"]
        for event_name in ("PreToolUse", "PostToolUse", "PostToolUseFailure", "PermissionDenied"):
            with self.subTest(event=event_name):
                (entry,) = hooks[event_name]
                matcher = re.compile(entry["matcher"])
                for name in ("Bash", "mcp__mezmo__list_pipelines", "mcp__ccd_turn__wrap_up"):
                    self.assertTrue(matcher.search(name), name)
                for name in ("Read", "BashOutput", "ToolSearch", "Xmcp__a__b"):
                    self.assertFalse(matcher.search(name), name)


def bash_event(command: str, **tool_input: Any) -> Dict[str, Any]:
    return {**hook_event("PreToolUse"), "tool_name": "Bash", "tool_input": {"command": command, **tool_input}}


@patch.dict(os.environ, environment(**ENV), clear=True)
class BashPartsTests(unittest.TestCase):
    """A Bash call goes to Mezmo as one item per part, with fields a policy rule can match."""

    def items(self, command: str, **tool_input: Any) -> List[Dict[str, Any]]:
        return hook.approval_payload(bash_event(command, **tool_input))["items"]

    def test_a_one_part_command_keeps_its_input_and_gains_its_program(self) -> None:
        (item,) = self.items("git status --short", description="Show changes")
        self.assertEqual(
            list(item["arguments"].items()),
            [("command", "git status --short"), ("description", "Show changes"), ("program", "git"), ("arg_1", "status"),
             ("flag_short", True)],
        )
        self.assertNotIn("dependencies", item)

    def test_flags_become_fields_a_rule_can_match(self) -> None:
        (item,) = self.items("git push --force-with-lease=main -fu origin main")
        self.assertEqual(
            [(name, value) for name, value in item["arguments"].items() if name.startswith("flag_")],
            [("flag_force_with_lease", "main"), ("flag_f", True), ("flag_u", True)],
        )
        # A name Mezmo wouldn't accept is made one it does, and one too long for it is left out.
        (item,) = self.items("tool --odd.name --" + "x" * 70 + " target")
        self.assertEqual([name for name in item["arguments"] if name.startswith("flag_")], ["flag_odd_name"])

    def test_each_part_is_an_item_and_the_first_shows_the_whole_command(self) -> None:
        command = "cd /repo && git push origin main | tee push.log"
        items = self.items(command, description="Push main", timeout=60000)
        self.assertEqual([item["id"] for item in items], [0, 1, 2])
        self.assertTrue(all((i["tool_namespace"], i["tool_name"]) == ("claude_code", "Bash") for i in items))
        # The whole command and why come first, then this part's own fields.
        self.assertEqual(
            list(items[0]["arguments"].items()),
            [("full_command", command), ("description", "Push main"), ("timeout", 60000),
             ("command", "cd /repo"), ("program", "cd"), ("arg_1", "/repo")],
        )
        self.assertEqual(
            items[1]["arguments"],
            {"command": "git push origin main", "program": "git", "arg_1": "push", "arg_2": "origin", "arg_3": "main"},
        )
        self.assertEqual(items[2]["arguments"], {"command": "tee push.log", "program": "tee", "arg_1": "push.log"})
        # git push runs once cd has finished; tee runs alongside it.
        self.assertEqual(items[1]["dependencies"], [0])
        self.assertNotIn("dependencies", items[2])

    def test_a_part_it_cannot_read_has_no_program(self) -> None:
        items = self.items("ls $HOME && npm test")
        self.assertEqual(items[0]["arguments"], {"full_command": "ls $HOME && npm test", "command": "ls $HOME"})
        self.assertEqual(items[1]["arguments"]["program"], "npm")

    def test_a_command_it_cannot_split_goes_whole_as_before(self) -> None:
        for command in ("echo $(whoami) && rm -rf x", "cat <<EOF\nhi\nEOF"):
            with self.subTest(command=command):
                (item,) = self.items(command, description="d")
                self.assertEqual(item["arguments"], {"command": command, "description": "d"})

    def test_a_parser_failure_never_blocks_the_call(self) -> None:
        http = FakeHttp([(200, {"approved": True})])
        with patch.object(hook.bash_parts, "split_command", side_effect=RecursionError("deep")):
            self.assertIsNone(hook.handle_event(bash_event("ls && pwd"), http=http))
        self.assertEqual(http.calls[0][2]["items"], [{"id": 0, "tool_namespace": "claude_code", "tool_name": "Bash", "arguments": {"command": "ls && pwd"}}])

    def test_a_command_with_too_many_parts_goes_whole(self) -> None:
        command = "; ".join(["true"] * (hook.MAX_BASH_PARTS + 1))
        (item,) = self.items(command)
        self.assertEqual(item["arguments"], {"command": command})

    def test_only_the_first_arguments_become_fields(self) -> None:
        (item,) = self.items("touch " + " ".join(f"f{n}" for n in range(1, 30)))
        names = [name for name in item["arguments"] if name.startswith("arg_")]
        self.assertEqual(names, [f"arg_{n}" for n in range(1, hook.MAX_BASH_ARGS + 1)])

    def test_mcp_calls_stay_one_item(self) -> None:
        event = {**hook_event("PreToolUse"), "tool_name": "mcp__terminal__run", "tool_input": {"command": "ls && pwd"}}
        (item,) = hook.approval_payload(event)["items"]
        self.assertEqual(item["arguments"], {"command": "ls && pwd"})

    def test_the_decision_stays_bound_to_the_exact_call(self) -> None:
        payload = hook.approval_payload(bash_event("ls && pwd"))
        self.assertEqual(payload["decision_id"], hook.call_identity(bash_event("ls && pwd")).decision_id)
        self.assertNotEqual(payload["decision_id"], hook.approval_payload(bash_event("ls && pwd -P"))["decision_id"])


def blocked_event(tool_name: str = "Bash") -> Dict[str, Any]:
    return {
        **hook_event("PermissionDenied"),
        "tool_name": tool_name,
        "tool_input": {"command": "rm -rf build"},
        "reason": "[Irreversible Local Destruction] Deletes the build directory",
    }


class AutoModeBlockTests(unittest.TestCase):
    """When auto mode's classifier blocks a governed call, the hook records it locally. Mezmo isn't asked."""

    def setUp(self) -> None:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        self.dir = pathlib.Path(tmp.name)
        # One patch, made here: a class-level patch would apply after setUp and replace these paths.
        paths = {
            "MEZMO_GOVERNANCE_AUDIT_LOG": str(self.dir / "audit.jsonl"),
            "MEZMO_GOVERNANCE_STATE_DIR": str(self.dir / "sessions"),
        }
        patcher = patch.dict(os.environ, environment(**ENV, **paths), clear=True)
        patcher.start()
        self.addCleanup(patcher.stop)

    def state(self) -> Dict[str, Any]:
        return json.loads((self.dir / "sessions" / "session-1.json").read_text())

    def test_a_block_is_counted_and_logged_without_the_command(self) -> None:
        http = FakeHttp([])
        self.assertIsNone(hook.handle_event(blocked_event(), http=http))
        self.assertEqual(http.calls, [])
        self.assertEqual(self.state()["counts"]["auto_mode_blocked"], 1)
        # The status mod toasts the last result, so a block leaves it alone.
        self.assertNotIn("last", self.state())
        (record,) = [json.loads(line) for line in (self.dir / "audit.jsonl").read_text().splitlines()]
        self.assertEqual(
            (record["tool_namespace"], record["tool_name"], record["status"]),
            ("claude_code", "Bash", "blocked_by_auto_mode"),
        )
        self.assertEqual(record["reason"], "[Irreversible Local Destruction] Deletes the build directory")
        self.assertNotIn("rm -rf", json.dumps(record))

    def test_a_block_without_a_call_id_is_still_counted(self) -> None:
        event = blocked_event()
        del event["tool_use_id"]
        hook.handle_event(event)
        self.assertEqual(self.state()["counts"]["auto_mode_blocked"], 1)

    def test_nothing_is_recorded_without_governance_or_for_other_tools(self) -> None:
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_ACCESS_KEY": ""}):
            hook.handle_event(blocked_event())
        hook.handle_event(blocked_event("Edit"))
        self.assertFalse((self.dir / "audit.jsonl").exists())
        self.assertFalse((self.dir / "sessions").exists())


class SlowHttp(FakeHttp):
    """Each answer arrives `delay` seconds after the request, on the fake clock."""

    def __init__(self, clock: FakeClock, delay: float, responses: List[Response]) -> None:
        super().__init__(responses)
        self.clock = clock
        self.delay = delay

    def __call__(
        self, method: str, url: str, payload: Optional[Mapping[str, Any]], timeout: float
    ) -> Tuple[int, Dict[str, Any]]:
        self.clock.now += self.delay
        return super().__call__(method, url, payload, timeout)


@patch.dict(os.environ, environment(**{**ENV, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "2"}), clear=True)
class LateAnswerTests(unittest.TestCase):
    """protocol.md rule 8: an answer that arrives after the deadline is a deny."""

    def test_an_approval_posted_after_the_deadline_is_refused(self) -> None:
        clock = FakeClock()
        http = SlowHttp(clock, 5.0, [(200, {"approved": True})])
        output = hook.handle_event(hook_event("PreToolUse"), http=http, clock=clock, sleep=clock.sleep)
        self.assertIsNotNone(output)
        self.assertEqual(decision_of(output), "deny")
        self.assertIn("Mezmo Governance: approval still pending", reason_of(output))

    def test_an_approval_polled_after_the_deadline_is_refused(self) -> None:
        clock = FakeClock()
        http = SlowHttp(clock, 1.5, [(207, {"approved": False, "reason": None}), (200, {"approved": True})])
        output = hook.handle_event(hook_event("PreToolUse"), http=http, clock=clock, sleep=clock.sleep)
        self.assertIsNotNone(output)
        self.assertEqual(decision_of(output), "deny")
        self.assertIn("Mezmo Governance: approval still pending", reason_of(output))


if __name__ == "__main__":
    unittest.main()
