"""Tests for the Mezmo Governance hook (scripts/governance_hook.py).

The first group is the contract suite of the Claude Code adapter in the governance plugins
repo; the rest cover what changed here: the service's 207 pending answers, governance off
until configured, plugin options, and error messages.
"""

import json
import os
import pathlib
import sys
import tempfile
import threading
import time
import unittest
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
        self.assertEqual(before.request_id, "claude_code:session-1:turn-1:tool-use-1")

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
        event["tool_name"] = "Grep"
        event["tool_input"] = {"pattern": "x", "-i": True, "-C": 2}
        output = hook.handle_event(event, http=FakeHttp([(400, body)]))
        self.assertEqual(decision_of(output), "deny")
        self.assertIn("'-i', '-C'", reason_of(output))
        self.assertIn("argument names", reason_of(output))
        self.assertNotIn("'pattern'", reason_of(output))

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


class HookConfigTests(unittest.TestCase):
    def test_a_pre_tool_hook_that_fails_blocks_the_tool(self) -> None:
        # Without onFailure, Claude Code runs the tool when the hook crashes, times out
        # or finds no python3.
        config = json.loads((pathlib.Path(__file__).resolve().parents[1] / "hooks" / "hooks.json").read_text())
        handlers = [handler for group in config["hooks"]["PreToolUse"] for handler in group["hooks"]]
        self.assertTrue(handlers)
        for handler in handlers:
            self.assertEqual(handler.get("onFailure"), "block", handler)


if __name__ == "__main__":
    unittest.main()
