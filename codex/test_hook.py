"""Contract tests for the Codex Mezmo Governance hook."""

from __future__ import annotations

import ast
import json
import os
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
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from io import StringIO
from pathlib import Path
from typing import Any
from unittest.mock import patch

import mezmo_governance_hook as hook

# With the ledger settings set, a denying hook reports a `not_run` to the Mezmo ledger. The
# developer's own settings stay out of this process and of every hook the tests start.
for _name in [name for name in os.environ if name.startswith("MEZMO_LEDGER_")]:
    del os.environ[_name]


AUTHORIZE_URL = "https://api.example.test/governance/workflow/authorize"
ENV = {
    "MEZMO_GOVERNANCE_URL": AUTHORIZE_URL,
    "MEZMO_GOVERNANCE_TOKEN": "sts_test",
    "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "30",
    "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "1",
}


def hook_event(name: str) -> dict[str, Any]:
    event: dict[str, Any] = {
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


class FakeHttp:
    def __init__(
        self,
        responses: list[tuple[int, dict[str, Any]] | BaseException],
    ) -> None:
        self.responses = list(responses)
        self.calls: list[tuple[str, str, Mapping[str, Any] | None, float]] = []

    def __call__(
        self,
        method: str,
        url: str,
        payload: Mapping[str, Any] | None,
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
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

class SlowHttp(FakeHttp):
    """Advances the fake clock during each call, like a slow answer."""

    def __init__(
        self,
        clock: FakeClock,
        seconds: float,
        responses: list[tuple[int, dict[str, Any]] | BaseException],
    ) -> None:
        super().__init__(responses)
        self.clock = clock
        self.seconds = seconds

    def __call__(
        self,
        method: str,
        url: str,
        payload: Mapping[str, Any] | None,
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        self.clock.now += self.seconds
        return super().__call__(method, url, payload, timeout)



class CodexGovernanceHookTests(unittest.TestCase):
    def test_normalizes_mcp_identity_without_changing_arguments(self) -> None:
        event = hook_event("PreToolUse")
        normalized = hook.normalize_tool_call(event)

        self.assertEqual(normalized.namespace, "kubernetes_demo")
        self.assertEqual(normalized.name, "pods_delete")
        self.assertEqual(normalized.arguments, event["tool_input"])

    def test_an_mcp_server_named_like_the_agent_is_refused(self) -> None:
        # mcp__<agent>__Bash would pass as the built-in Bash.
        for server in (hook.AGENT, hook.AGENT.upper()):
            event = hook_event("PreToolUse")
            event["tool_name"] = f"mcp__{server}__Bash"
            with self.subTest(server=server), self.assertRaises(hook.HookContractError):
                hook.normalize_tool_call(event)

    def test_reuses_the_same_identity_for_pre_and_post_events(self) -> None:
        pre, post = hook_event("PreToolUse"), hook_event("PostToolUse")
        tool_call = hook.normalize_tool_call(pre)
        before = hook.call_identity(pre, tool_call)
        after = hook.call_identity(post, hook.normalize_tool_call(post))

        self.assertEqual(before, after)
        self.assertEqual(
            before.request_id,
            "codex:session-1:turn-1:tool-use-1#" + hook.call_digest(*tool_call),
        )

    def test_approval_payload_identifies_codex_and_the_mcp_namespace(self) -> None:
        payload = hook.approval_payload(hook_event("PreToolUse"))

        self.assertEqual(
            payload["origin"],
            {"kind": "codex_hook", "reason": "PreToolUse"},
        )
        self.assertEqual(payload["items"][0]["tool_namespace"], "kubernetes_demo")
        self.assertEqual(payload["items"][0]["tool_name"], "pods_delete")

    @patch.dict(os.environ, ENV)
    def test_allows_an_approved_call_without_rewriting_it(self) -> None:
        http = FakeHttp([(200, {"approved": True})])

        output = hook.handle_event(hook_event("PreToolUse"), http=http)

        self.assertIsNone(output)
        self.assertEqual(http.calls[0][0], "POST")
        self.assertEqual(http.calls[0][1], AUTHORIZE_URL)
        self.assertEqual(
            http.calls[0][2]["items"][0]["arguments"],
            hook_event("PreToolUse")["tool_input"],
        )

    @patch.dict(os.environ, ENV)
    def test_maps_a_denial_to_a_valid_pre_tool_hook_decision(self) -> None:
        http = FakeHttp([(200, {"approved": False, "reason": "Use a canary first."})])

        output = hook.handle_event(hook_event("PreToolUse"), http=http)

        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        self.assertIn(
            "Use a canary first.",
            output["hookSpecificOutput"]["permissionDecisionReason"],
        )

    @patch.dict(os.environ, ENV)
    def test_polls_after_a_client_timeout_then_allows(self) -> None:
        http = FakeHttp(
            [
                TimeoutError("POST held too long"),
                (200, {"status": "pending", "reason": None}),
                (200, {"status": "approved", "reason": "ok"}),
            ]
        )
        clock = FakeClock()

        output = hook.handle_event(
            hook_event("PreToolUse"),
            http=http,
            clock=clock,
            sleep=clock.sleep,
        )

        self.assertIsNone(output)
        self.assertEqual(http.calls[1][0], "GET")
        self.assertIn("decision_id=", http.calls[1][1])

    @patch.dict(os.environ, ENV)
    def test_polls_after_conflict_then_denies(self) -> None:
        http = FakeHttp(
            [
                (409, {"code": "ECONFLICT"}),
                (200, {"status": "denied", "reason": "already decided"}),
            ]
        )

        output = hook.handle_event(hook_event("PreToolUse"), http=http)

        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        self.assertIn("already decided", output["hookSpecificOutput"]["permissionDecisionReason"])

    @patch.dict(os.environ, ENV)
    def test_waits_for_a_person_after_207_then_allows(self) -> None:
        http = FakeHttp(
            [
                (207, {"approved": False}),
                (200, {"status": "pending", "reason": None}),
                (200, {"status": "approved", "reason": "looked it over"}),
            ]
        )
        clock = FakeClock()

        output = hook.handle_event(
            hook_event("PreToolUse"),
            http=http,
            clock=clock,
            sleep=clock.sleep,
        )

        self.assertIsNone(output)
        poll_url = f"{AUTHORIZE_URL}?decision_id={http.calls[0][2]['decision_id']}"
        self.assertEqual(
            [(method, url) for method, url, _, _ in http.calls],
            [("POST", AUTHORIZE_URL), ("GET", poll_url), ("GET", poll_url)],
        )
        self.assertEqual(clock.now, 1.0)

    @patch.dict(os.environ, ENV)
    def test_polls_through_207_until_a_person_approves(self) -> None:
        # What Mezmo answers while a person decides: 207 with "approved": false,
        # on the POST and on each poll. Then 200 with the decision.
        http = FakeHttp(
            [
                (207, {"approved": False, "reason": "No rule in this policy covers this request."}),
                (207, {"approved": False, "reason": None}),
                (200, {"approved": True, "reason": "looked it over"}),
            ]
        )
        clock = FakeClock()

        output = hook.handle_event(
            hook_event("PreToolUse"),
            http=http,
            clock=clock,
            sleep=clock.sleep,
        )

        self.assertIsNone(output)
        poll_url = f"{AUTHORIZE_URL}?decision_id={http.calls[0][2]['decision_id']}"
        self.assertEqual(
            [(method, url) for method, url, _, _ in http.calls],
            [("POST", AUTHORIZE_URL), ("GET", poll_url), ("GET", poll_url)],
        )
        self.assertEqual(clock.now, 1.0)

    @patch.dict(os.environ, ENV)
    def test_a_person_denies_through_the_poll(self) -> None:
        http = FakeHttp(
            [
                (207, {"approved": False}),
                (200, {"approved": False, "reason": "not today"}),
            ]
        )
        clock = FakeClock()

        output = hook.handle_event(
            hook_event("PreToolUse"),
            http=http,
            clock=clock,
            sleep=clock.sleep,
        )

        self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        self.assertIn("not today", output["hookSpecificOutput"]["permissionDecisionReason"])

    @patch.dict(os.environ, ENV)
    def test_fails_closed_when_the_governance_request_fails(self) -> None:
        http = FakeHttp([hook.HookContractError("connection refused")])

        output = hook.handle_event(hook_event("PreToolUse"), http=http)

        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        self.assertIn(
            "connection refused",
            output["hookSpecificOutput"]["permissionDecisionReason"],
        )

    @patch.dict(os.environ, ENV)
    def test_fails_closed_when_approval_stays_pending(self) -> None:
        http = FakeHttp(
            [
                (200, {"status": "pending", "reason": None}),
                (200, {"status": "pending", "reason": None}),
            ]
        )
        clock = FakeClock()
        env = {**ENV, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "1"}
        with patch.dict(os.environ, env):
            http = FakeHttp(
                [
                    TimeoutError("held"),
                    (200, {"status": "pending", "reason": None}),
                    (200, {"status": "pending", "reason": None}),
                ]
            )
            output = hook.handle_event(
                hook_event("PreToolUse"),
                http=http,
                clock=clock,
                sleep=clock.sleep,
            )

        self.assertEqual(
            output["hookSpecificOutput"]["permissionDecision"],
            "deny",
        )
        self.assertIn("approval still pending", output["hookSpecificOutput"]["permissionDecisionReason"])

    def test_malformed_stdin_uses_the_blocking_exit_code(self) -> None:
        with (
            patch.object(sys, "stdout", StringIO()), patch.object(sys, "stdin", StringIO("not-json")),
            patch.object(sys, "stderr", StringIO()),
        ):
            exit_code = hook.main()

        self.assertEqual(exit_code, 2)

    @patch.dict(os.environ, ENV)
    def test_reports_post_tool_output_locally_and_does_not_block(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "audit.jsonl"
            env = {**ENV, "MEZMO_GOVERNANCE_AUDIT_LOG": str(log_path)}
            with patch.dict(os.environ, env):
                output = hook.handle_event(hook_event("PostToolUse"))

            self.assertIsNone(output)
            records = [
                json.loads(line)
                for line in log_path.read_text(encoding="utf-8").splitlines()
            ]
            self.assertEqual(len(records), 1)
            self.assertEqual(records[0]["agent"], "codex")
            self.assertEqual(records[0]["tool_name"], "pods_delete")
            self.assertEqual(records[0]["status"], "observed")

    @patch.dict(os.environ, ENV)
    def test_post_tool_audit_failure_does_not_block(self) -> None:
        with (
            patch.object(hook, "append_audit", side_effect=OSError("disk full")),
            patch.object(sys, "stderr", StringIO()) as stderr,
        ):
            output = hook.handle_event(hook_event("PostToolUse"))

        self.assertIsNone(output)
        self.assertIn("disk full", stderr.getvalue())

    def test_authorization_header_prefixes_token(self) -> None:
        with patch.dict(os.environ, ENV):
            self.assertEqual(hook.authorization_header(), "Token sts_test")
        with patch.dict(
            os.environ,
            {**ENV, "MEZMO_GOVERNANCE_TOKEN": "Token sts_test"},
        ):
            self.assertEqual(hook.authorization_header(), "Token sts_test")


class PollingTests(unittest.TestCase):
    @patch.dict(os.environ, ENV)
    def test_a_call_sent_for_approval_polls_until_mezmo_decides(self) -> None:
        # Live Mezmo, 2026-10-16: a call sent for approval answers 207 with
        # approved false, polls 207 while pending, then 200 once decided.
        pending = {"approved": False, "reason": None}
        sent = {"approved": False, "reason": "No rule in this policy covers this request."}
        for approved in (True, False):
            with self.subTest(approved=approved):
                http = FakeHttp([(207, sent), (207, pending), (200, {"approved": approved, "reason": None})])
                clock = FakeClock()

                decision = hook.authorize(
                    {"decision_id": "d"}, http=http, clock=clock, sleep=clock.sleep
                )

                self.assertIs(decision.approved, approved)
                self.assertEqual([call[0] for call in http.calls], ["POST", "GET", "GET"])

    @patch.dict(os.environ, ENV)
    def test_poll_treats_404_as_pending_until_decided(self) -> None:
        http = FakeHttp(
            [
                (409, {"code": "ECONFLICT"}),
                (404, {}),
                (200, {"status": "approved", "reason": None}),
            ]
        )
        clock = FakeClock()

        decision = hook.authorize(
            {"decision_id": "d"}, http=http, clock=clock, sleep=clock.sleep
        )

        self.assertTrue(decision.approved)
        self.assertEqual([call[0] for call in http.calls], ["POST", "GET", "GET"])

    @patch.dict(os.environ, {**ENV, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "2"})
    def test_poll_denies_when_404_lasts_past_the_deadline(self) -> None:
        http = FakeHttp([(409, {})] + [(404, {})] * 5)
        clock = FakeClock()

        with self.assertRaisesRegex(hook.HookContractError, "approval still pending"):
            hook.authorize(
                {"decision_id": "d"}, http=http, clock=clock, sleep=clock.sleep
            )

    @patch.dict(os.environ, {**ENV, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "2"})
    def test_an_approval_posted_after_the_deadline_is_refused(self) -> None:
        clock = FakeClock()
        http = SlowHttp(clock, 5.0, [(200, {"approved": True})])

        with self.assertRaisesRegex(hook.HookContractError, "approval still pending"):
            hook.authorize(
                {"decision_id": "d"}, http=http, clock=clock, sleep=clock.sleep
            )

    @patch.dict(os.environ, {**ENV, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "2"})
    def test_an_approval_polled_after_the_deadline_is_refused(self) -> None:
        clock = FakeClock()
        http = SlowHttp(
            clock, 1.5, [(409, {}), (200, {"status": "approved", "reason": None})]
        )

        with self.assertRaisesRegex(hook.HookContractError, "approval still pending"):
            hook.authorize(
                {"decision_id": "d"}, http=http, clock=clock, sleep=clock.sleep
            )


class DeadlineWatchdogTests(unittest.TestCase):
    def fire(self, stdout: Any) -> list[int]:
        exits: list[int] = []
        fired = threading.Event()

        def fake_exit(code: int) -> None:
            exits.append(code)
            fired.set()

        with patch.object(sys, "stdout", stdout), patch.object(
            sys, "stderr", StringIO()
        ), patch.object(hook.os, "_exit", fake_exit):
            hook.deadline_watchdog(0.01)
            self.assertTrue(fired.wait(5))
        return exits

    def test_prints_the_deny_output_and_exits_with_the_block_code(self) -> None:
        stdout = StringIO()

        self.assertEqual(self.fire(stdout), [2])
        output = json.loads(stdout.getvalue())
        self.assertEqual(set(output), set(deny_schema("x")))
        self.assertIn("approval still pending", stdout.getvalue())

    def test_exits_with_the_block_code_when_stdout_is_gone(self) -> None:
        class ClosedStdout(StringIO):
            def write(self, text: str) -> int:
                raise BrokenPipeError("stdout closed")

        self.assertEqual(self.fire(ClosedStdout()), [2])

    def test_stopping_before_the_deadline_prints_nothing(self) -> None:
        with patch.object(hook.os, "_exit") as fake_exit, patch.object(
            sys, "stdout", StringIO()
        ) as stdout:
            hook.deadline_watchdog(0.05)()
            time.sleep(0.2)

        fake_exit.assert_not_called()
        self.assertEqual(stdout.getvalue(), "")


class DeadlineBoundsTests(unittest.TestCase):
    def seconds(self, name: str, configured: str) -> float:
        with patch.dict(os.environ, {**ENV, name: configured}):
            if name == "MEZMO_GOVERNANCE_TIMEOUT_SECONDS":
                return hook.timeout_seconds()
            return hook.poll_interval_seconds()

    def test_timeout_at_or_above_the_hook_budget_is_capped(self) -> None:
        for configured in ("590", "600", "3600", "1e308"):
            with self.subTest(configured=configured):
                self.assertEqual(
                    self.seconds("MEZMO_GOVERNANCE_TIMEOUT_SECONDS", configured),
                    hook.MAX_TIMEOUT_SECONDS,
                )
        self.assertLess(hook.MAX_TIMEOUT_SECONDS, 600)

    def test_small_positive_timeouts_are_kept(self) -> None:
        for configured, expected in (("30", 30.0), ("5e-324", 5e-324)):
            with self.subTest(configured=configured):
                self.assertEqual(
                    self.seconds("MEZMO_GOVERNANCE_TIMEOUT_SECONDS", configured),
                    expected,
                )

    def test_rejects_seconds_that_are_not_finite_and_positive(self) -> None:
        names = (
            "MEZMO_GOVERNANCE_TIMEOUT_SECONDS",
            "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS",
        )
        for name in names:
            for configured in ("0", "-0", "-1", "nan", "inf", "-inf", "", "ten"):
                with self.subTest(name=name, configured=configured):
                    with self.assertRaises(hook.HookContractError):
                        self.seconds(name, configured)

    def test_poll_interval_has_a_floor(self) -> None:
        for configured in ("5e-324", "0.001"):
            with self.subTest(configured=configured):
                self.assertEqual(
                    self.seconds("MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS", configured),
                    hook.MIN_POLL_INTERVAL_SECONDS,
                )

    def test_a_large_timeout_still_ends_before_the_runtime_kills_the_hook(self) -> None:
        http = FakeHttp([(200, {"approved": True})])
        env = {**ENV, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "3600"}
        with patch.dict(os.environ, env):
            hook.authorize({"decision_id": "d"}, http=http)
        self.assertLessEqual(http.calls[0][3], hook.MAX_TIMEOUT_SECONDS)


ADAPTER_DIR = Path(__file__).resolve().parent
# Handler keys the Codex hooks docs list for command hooks.
CODEX_HANDLER_KEYS = {
    "type",
    "command",
    "commandWindows",
    "timeout",
    "statusMessage",
    "async",
    "additionalContextLimit",
}

# Codex and Gemini CLI run the tool when a hook exits 1, for example on a crash.
CRASH_GUARD = " || { s=$?; [ $s -eq 2 ] || echo 'Mezmo Governance hook failed; blocking' >&2; exit 2; }"


# The two hook configs and the ledger command each one registers: the file install's example
# and the plugin's own.
HOOK_CONFIGS = {
    "hooks.example.json": 'python3 "$HOME/.codex/hooks/mezmo_ledger_hook.py" || true',
    "hooks/hooks.json": 'python3 "${PLUGIN_ROOT}/mezmo_ledger_hook.py" || true',
}
LEDGER_TIMEOUTS = {"PostToolUse": 15, "PreToolUse": 10}


class HookConfigTests(unittest.TestCase):
    def test_each_config_registers_governance_then_the_ledger_for_both_events(self) -> None:
        for name, ledger_command in HOOK_CONFIGS.items():
            config = json.loads((ADAPTER_DIR / name).read_text(encoding="utf-8"))["hooks"]
            self.assertEqual(set(config), {"PreToolUse", "PostToolUse"})
            for event, groups in config.items():
                with self.subTest(config=name, event=event):
                    (group,) = groups
                    self.assertEqual(group["matcher"], ".*")
                    # The ledger handler is second and last, so the governance handler keeps
                    # its place, and with it the trust Codex gave it.
                    governance, ledger = group["hooks"]
                    for handler in (governance, ledger):
                        self.assertLessEqual(set(handler), CODEX_HANDLER_KEYS)
                        self.assertEqual(handler["type"], "command")

                    self.assertIn("mezmo_governance_hook.py", governance["command"])
                    self.assertNotIn("mezmo_ledger_hook.py", governance["command"])
                    if event == "PreToolUse":
                        # Above the deadline. test_ledger.py checks the room a deny then has
                        # for the ledger.
                        self.assertGreater(governance["timeout"], hook.MAX_TIMEOUT_SECONDS)

                    # The ledger can never block: no guard, no exit code but 0, a short timeout
                    # and nothing that would run it in the background.
                    self.assertEqual(ledger["command"], ledger_command)
                    self.assertTrue(ledger["command"].endswith(" || true"))
                    self.assertNotIn("exit 2", ledger["command"])
                    self.assertEqual(set(ledger), {"command", "timeout", "type"})
                    self.assertEqual(ledger["timeout"], LEDGER_TIMEOUTS[event])

    def test_the_ledger_command_exits_0_with_or_without_its_script(self) -> None:
        command = HOOK_CONFIGS["hooks.example.json"]
        with tempfile.TemporaryDirectory() as tmp:
            hooks = Path(tmp) / ".codex" / "hooks"
            hooks.mkdir(parents=True)
            # Python exits 2 on a script it cannot find, which would block the call.
            for installed in (False, True):
                if installed:
                    for path in (HOOK_PATH, LEDGER_PATH):
                        shutil.copy(path, hooks / path.name)
                for stdin in (None, "not json"):
                    with self.subTest(installed=installed, stdin=stdin):
                        result = self.run_command(command, HOOK_PATH, stdin, home=tmp)
                        self.assertEqual(result.returncode, 0, result.stderr)
                        self.assertEqual(result.stdout, "")

    def pre_and_post_commands(self) -> tuple[str, str]:
        with (ADAPTER_DIR / "hooks.example.json").open(encoding="utf-8") as handle:
            config = json.load(handle)["hooks"]
        return (
            config["PreToolUse"][0]["hooks"][0]["command"],
            config["PostToolUse"][0]["hooks"][0]["command"],
        )

    def run_command(
        self, command: str, hook_path: Path, stdin: str | None = None, home: str | None = None
    ) -> subprocess.CompletedProcess[str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("MEZMO_GOVERNANCE_", "MEZMO_LEDGER_"))
        }
        env["MEZMO_GOVERNANCE_HOOK"] = str(hook_path)
        if home is not None:
            env["HOME"] = home
        return subprocess.run(
            ["/bin/sh", "-c", command],
            input=json.dumps(hook_event("PreToolUse")) if stdin is None else stdin,
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def test_only_the_gating_command_carries_the_crash_guard(self) -> None:
        pre, post = self.pre_and_post_commands()

        self.assertTrue(pre.endswith(CRASH_GUARD))
        self.assertNotIn("exit 2", post)

    def test_the_crash_guard_turns_a_crash_into_a_block(self) -> None:
        pre, _ = self.pre_and_post_commands()
        with tempfile.TemporaryDirectory() as tmp:
            crashing = Path(tmp) / "crash.py"
            crashing.write_text("raise RuntimeError('boom')\n", encoding="utf-8")
            result = self.run_command(pre, crashing)

        self.assertEqual(result.returncode, 2)
        self.assertIn("Mezmo Governance hook failed; blocking", result.stderr)

    def test_a_deliberate_block_prints_no_crash_message(self) -> None:
        # The deadline watchdog writes its reason and exits 2; so does the hook on input it cannot read.
        pre, _ = self.pre_and_post_commands()
        with tempfile.TemporaryDirectory() as tmp:
            watchdog = Path(tmp) / "watchdog.py"
            watchdog.write_text(
                "import os, sys\nsys.stderr.write('Mezmo Governance: approval still pending\\n')\nos._exit(2)\n",
                encoding="utf-8",
            )
            for path, stdin in ((watchdog, None), (HOOK_PATH, "not json")):
                with self.subTest(path=path.name):
                    result = self.run_command(pre, path, stdin)
                    self.assertEqual(result.returncode, 2, result.stderr)
                    self.assertNotIn("hook failed", result.stderr)

    def test_a_hook_that_does_not_run_still_blocks(self) -> None:
        pre, _ = self.pre_and_post_commands()
        # Python names a missing script itself, and exits 2.
        result = self.run_command(pre, Path("/nonexistent/mezmo_governance_hook.py"))
        self.assertEqual(result.returncode, 2)
        self.assertIn("/nonexistent/mezmo_governance_hook.py", result.stderr)
        result = self.run_command(pre.replace("python3", "python3-not-on-path", 1), HOOK_PATH)
        self.assertEqual(result.returncode, 2)
        self.assertIn("Mezmo Governance hook failed; blocking", result.stderr)

    def test_the_crash_guard_keeps_the_hook_deny_output(self) -> None:
        pre, _ = self.pre_and_post_commands()
        result = self.run_command(pre, HOOK_PATH)

        self.assertEqual(result.returncode, 0, result.stderr)
        output = json.loads(result.stdout)
        self.assertEqual(set(output), set(deny_schema("x")))
        self.assertIn("MEZMO_GOVERNANCE_URL", result.stdout)


HOOK_PATH = Path(__file__).resolve().with_name("mezmo_governance_hook.py")
LEDGER_PATH = HOOK_PATH.with_name("mezmo_ledger_hook.py")
PYTHON_FLOOR = (3, 9)


def runtime_union_lines(source: str) -> list[int]:
    """Lines that evaluate `X | Y` at runtime, which Python 3.9 rejects for types."""
    tree = ast.parse(source)
    annotations: set[int] = set()
    for node in ast.walk(tree):
        roots: list[ast.AST | None] = []
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
            arguments = node.args
            every = [
                *arguments.posonlyargs,
                *arguments.args,
                *arguments.kwonlyargs,
                arguments.vararg,
                arguments.kwarg,
            ]
            roots.extend(argument.annotation for argument in every if argument)
            roots.append(node.returns)
        elif isinstance(node, ast.AnnAssign):
            roots.append(node.annotation)
        for root in roots:
            if root is not None:
                annotations.update(id(child) for child in ast.walk(root))
    return [
        node.lineno
        for node in ast.walk(tree)
        if isinstance(node, ast.BinOp)
        and isinstance(node.op, ast.BitOr)
        and id(node) not in annotations
    ]


def floor_interpreter() -> str | None:
    for candidate in ("python3.9", "/usr/bin/python3"):
        path = shutil.which(candidate)
        if path is None:
            continue
        probe = subprocess.run(
            [path, "-c", "import sys; print(sys.version_info[:2] == (3, 9))"],
            capture_output=True,
            text=True,
            check=False,
        )
        if probe.stdout.strip() == "True":
            return path
    return None


class PythonFloorTests(unittest.TestCase):
    def test_hook_has_no_runtime_union_syntax(self) -> None:
        for path in (HOOK_PATH, LEDGER_PATH):
            with self.subTest(script=path.name):
                source = path.read_text(encoding="utf-8")
                lines = source.splitlines()
                # The ledger's one runtime `|` joins two flock flags, which are ints.
                found = [
                    number
                    for number in runtime_union_lines(source)
                    if "fcntl.LOCK_EX | fcntl.LOCK_NB" not in lines[number - 1]
                ]
                self.assertEqual(found, [])

    def test_hook_imports_on_the_oldest_supported_python(self) -> None:
        interpreter = floor_interpreter()
        if interpreter is None:
            self.skipTest("no Python 3.9 interpreter on PATH")
        for path in (HOOK_PATH, LEDGER_PATH):
            with self.subTest(script=path.name):
                result = subprocess.run(
                    [
                        interpreter,
                        "-I",
                        "-B",
                        "-c",
                        "import sys; sys.path.insert(0, sys.argv[1]); "
                        f"import {path.stem}",
                        str(path.parent),
                    ],
                    capture_output=True,
                    text=True,
                    check=False,
                )
                self.assertEqual(result.returncode, 0, result.stderr)

    def test_the_ledger_runs_on_the_oldest_supported_python(self) -> None:
        interpreter = floor_interpreter()
        if interpreter is None:
            self.skipTest("no Python 3.9 interpreter on PATH")
        # A deny from the governance hook, which reports it through the ledger file, and then
        # the ledger hook itself. Nothing listens at either URL, so every event is saved.
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = f"http://127.0.0.1:{probe.getsockname()[1]}"
        with tempfile.TemporaryDirectory() as tmp:
            spool = Path(tmp) / "ledger-spool.jsonl"
            env = {
                "HOME": tmp,
                "MEZMO_GOVERNANCE_AUDIT_LOG": str(Path(tmp) / "audit.jsonl"),
                "MEZMO_GOVERNANCE_STATE_DIR": str(Path(tmp) / "codex-sessions"),
                "MEZMO_GOVERNANCE_TOKEN": "sts_test",
                "MEZMO_GOVERNANCE_URL": f"{closed}/governance/workflow/authorize",
                "MEZMO_LEDGER_ACCOUNT": "test-acct",
                "MEZMO_LEDGER_SPOOL": str(spool),
                "MEZMO_LEDGER_URL": f"{closed}/v1/events",
                "NO_PROXY": "*",
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            }
            for module, name, stdout in (
                ("mezmo_governance_hook", PRE_EVENT, "deny"),
                ("mezmo_ledger_hook", PRE_EVENT, ""),
                ("mezmo_ledger_hook", POST_EVENT, ""),
            ):
                with self.subTest(module=module, event=name):
                    result = subprocess.run(
                        [
                            interpreter,
                            "-I",
                            "-B",
                            "-c",
                            "import sys; sys.path.insert(0, sys.argv[1]); "
                            f"import {module}; raise SystemExit({module}.main())",
                            str(HOOK_PATH.parent),
                        ],
                        capture_output=True,
                        check=False,
                        env=env,
                        input=json.dumps(hook_event(name)),
                        text=True,
                        timeout=60,
                    )
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertNotIn("Traceback", result.stderr)
                    self.assertIn(stdout, result.stdout)
                    self.assertEqual(bool(result.stdout), bool(stdout))
            saved = [json.loads(line)["event"]["event"] for line in spool.read_text(encoding="utf-8").splitlines()]
        self.assertEqual(
            saved,
            ["agent.tool_call.not_run", "agent.tool_call.started", "agent.tool_call.ran"],
        )


# The shell-command parser is copied from the Claude Code hook, whole, into every adapter.
BASH_PARTS_DEFINITIONS = (
    "MAX_BASH_PARTS",
    "MAX_BASH_ARGS",
    "ARGUMENT_NAME_MAX",
    "SEQUENTIAL_SEPARATORS",
    "SHELL_KEYWORDS",
    "SIMPLE_WRAPPERS",
    "PRIVILEGE_WRAPPERS",
    "PRIVILEGE_FLAGS",
    "PRIVILEGE_OPTIONS",
    "ASSIGNMENT",
    "GLOB",
    "DURATION",
    "NICENESS",
    "STDBUF_OPTION",
    "FILE_DESCRIPTOR",
    "LETTERS",
    "REDIRECTIONS",
    "FlagValue",
    "BashPart",
    "Unsplittable",
    "split_command",
    "_skip_wrappers",
    "_command_run_by",
    "_flags_of",
    "_read_part",
    "_Scanner",
    "drop_part_fields",
    "bash_items",
)
# The eight Python hook adapters are standalone copies. These shared parts must stay
# identical so a fix in one copy reaches the others.
SHARED_DEFINITIONS = (
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "MIN_POLL_INTERVAL_SECONDS",
    "DEFAULT_AUDIT_LOG",
    "MAX_RESULT_PREVIEW_CHARACTERS",
    "HttpJson",
    "HookContractError",
    "Decision",
    "required_string",
    "parse_object",
    "call_digest",
    "MAX_REQUEST_ID_CHARACTERS",
    "bounded_request_id",
    "status_url",
    "positive_seconds",
    "timeout_seconds",
    "poll_interval_seconds",
    "audit_log_path",
    "RefuseRedirect",
    "http_json",
    "MAX_ERROR_DETAIL_CHARACTERS",
    "error_detail",
    "http_failure",
    "remaining_seconds",
    "decision_from_post_body",
    "decision_from_status_body",
    "poll_decision",
    "deadline_watchdog",
    "authorize",
    "authorize_url",
    "authorization_header",
    "result_preview",
    "result_has_known_error",
    "append_audit",
    *BASH_PARTS_DEFINITIONS,
    "shell_command_field",
)
SIBLING_ADAPTERS = ("cursor", "gemini-cli", "copilot", "goose", "droid", "windsurf", "antigravity")
# The Claude Code hook is its own port (typing spellings, its own config and status file),
# so only these shared parts must match it word for word.
CLAUDE_CODE_HOOK = Path("claude-code") / "scripts" / "governance_hook.py"
CLAUDE_CODE_SHARED_DEFINITIONS = (
    "DEFAULT_TIMEOUT_SECONDS",
    "MAX_TIMEOUT_SECONDS",
    "DEFAULT_POLL_INTERVAL_SECONDS",
    "MIN_POLL_INTERVAL_SECONDS",
    "DEFAULT_AUDIT_LOG",
    "MAX_RESULT_PREVIEW_CHARACTERS",
    "HookContractError",
    "required_string",
    "call_digest",
    "MAX_REQUEST_ID_CHARACTERS",
    "bounded_request_id",
    "timeout_seconds",
    "poll_interval_seconds",
    "audit_log_path",
    "MAX_ERROR_DETAIL_CHARACTERS",
    "error_detail",
    "http_failure",
    "decision_from_post_body",
    "result_preview",
    "append_audit",
)


def top_level_definitions(path: Path) -> dict[str, str]:
    tree = ast.parse(path.read_text(encoding="utf-8"))
    found: dict[str, str] = {}
    for node in tree.body:
        if isinstance(node, (ast.FunctionDef, ast.ClassDef)):
            found[node.name] = ast.dump(node)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                if isinstance(target, ast.Name):
                    found[target.id] = ast.dump(node.value)
    return found


class SharedCoreDriftTests(unittest.TestCase):
    def test_shared_core_is_identical_across_python_adapters(self) -> None:
        reference = top_level_definitions(HOOK_PATH)
        missing = [name for name in SHARED_DEFINITIONS if name not in reference]
        self.assertEqual(missing, [])
        siblings = [
            HOOK_PATH.parent.parent / adapter / HOOK_PATH.name
            for adapter in SIBLING_ADAPTERS
        ]
        present = [path for path in siblings if path.is_file()]
        if not present:
            self.skipTest("sibling adapters are not checked out next to this one")
        for path in present:
            sibling = top_level_definitions(path)
            for name in SHARED_DEFINITIONS:
                with self.subTest(adapter=path.parent.name, definition=name):
                    self.assertEqual(sibling.get(name), reference[name])
            with self.subTest(adapter=path.parent.name, definition="call_identity"):
                self.assertIn("bounded_request_id", sibling["call_identity"])

    def test_shared_core_matches_the_claude_code_hook(self) -> None:
        self.assertTrue(set(CLAUDE_CODE_SHARED_DEFINITIONS) <= set(SHARED_DEFINITIONS))
        reference = top_level_definitions(HOOK_PATH)
        path = HOOK_PATH.parent.parent / CLAUDE_CODE_HOOK
        if not path.is_file():
            self.skipTest("the Claude Code hook is not checked out next to this one")
        claude_code = top_level_definitions(path)
        for name in CLAUDE_CODE_SHARED_DEFINITIONS:
            with self.subTest(definition=name):
                self.assertEqual(claude_code.get(name), reference[name])

    def test_bash_parts_match_the_claude_code_hook(self) -> None:
        path = HOOK_PATH.parent.parent / CLAUDE_CODE_HOOK
        if not path.is_file():
            self.skipTest("the Claude Code hook is not checked out next to this one")
        ours, claude_code = top_level_definitions(HOOK_PATH), top_level_definitions(path)
        for name in BASH_PARTS_DEFINITIONS:
            with self.subTest(definition=name):
                self.assertEqual(ours.get(name), claude_code[name])


class ShellCommandPartsTests(unittest.TestCase):
    def event(self, tool_name="Bash", command="cd /repo && rm -rf build"):
        return {"hook_event_name": "PreToolUse", "session_id": "s", "tool_use_id": "t",
                "tool_name": tool_name, "tool_input": {"command": command}}

    def test_a_bash_call_goes_as_parts(self):
        items = hook.approval_payload(self.event())["items"]
        self.assertEqual(items, [
            {"id": 0, "tool_namespace": "codex", "tool_name": "Bash", "arguments": {
                "full_command": "cd /repo && rm -rf build", "command": "cd /repo", "program": "cd", "arg_1": "/repo"}},
            {"id": 1, "tool_namespace": "codex", "tool_name": "Bash", "arguments": {
                "command": "rm -rf build", "program": "rm", "arg_1": "build", "flag_r": True, "flag_f": True},
             "dependencies": [0]},
        ])

    def test_windows_sends_the_call_whole_with_the_same_identity(self):
        split = hook.approval_payload(self.event())
        with patch.object(hook.os, "name", "nt"):
            whole = hook.approval_payload(self.event())
        self.assertEqual(whole["items"], [{"id": 0, "tool_namespace": "codex", "tool_name": "Bash",
                                           "arguments": {"command": "cd /repo && rm -rf build"}}])
        self.assertEqual((split["decision_id"], split["request_id"]), (whole["decision_id"], whole["request_id"]))

    def test_an_mcp_tool_goes_whole(self):
        items = hook.approval_payload(self.event("mcp__box__Bash"))["items"]
        self.assertEqual(len(items), 1)

    def test_a_command_that_is_not_a_string_goes_whole(self):
        event = self.event()
        event["tool_input"] = {"command": ["rm", "-rf", "build"]}
        items = hook.approval_payload(event)["items"]
        self.assertEqual(items, [{"id": 0, "tool_namespace": "codex", "tool_name": "Bash",
                                  "arguments": {"command": ["rm", "-rf", "build"]}}])

    def test_the_inputs_own_part_fields_never_reach_mezmo(self):
        # Only the parser sets these, so a part it can't read never looks read, split or whole.
        tool_input = {"command": "ls $DIR", "program": "ls", "arg_1": "x", "flag_f": True, "full_command": "ls"}
        event = self.event()
        event["tool_input"] = tool_input
        with patch.object(hook.os, "name", "nt"):
            whole = hook.approval_payload(event)
        for payload in (whole, hook.approval_payload(event)):
            self.assertEqual(payload["items"], [{"id": 0, "tool_namespace": "codex", "tool_name": "Bash",
                                                 "arguments": {"command": "ls $DIR"}}])
        self.assertTrue(whole["request_id"].endswith("#" + hook.call_digest("codex", "Bash", tool_input)))

    def test_splitting_leaves_the_request_id_to_the_whole_input(self):
        parts = hook.approval_payload(self.event())
        self.assertTrue(parts["request_id"].endswith(
            "#" + hook.call_digest("codex", "Bash", {"command": "cd /repo && rm -rf build"})))


PRE_EVENT = "PreToolUse"
POST_EVENT = "PostToolUse"
ALLOW_OUTPUT = None
ORIGIN_KIND = "codex_hook"


def deny_schema(reason: str) -> dict[str, Any]:
    return {
        "hookSpecificOutput": {
            "hookEventName": "PreToolUse",
            "permissionDecision": "deny",
            "permissionDecisionReason": reason,
        }
    }


class StubGovernance(ThreadingHTTPServer):
    """Local Mezmo Governance stand-in that replays scripted answers."""

    def __init__(self, answers: list[tuple[int, dict[str, Any]]]) -> None:
        super().__init__(("127.0.0.1", 0), StubHandler)
        self.answers = list(answers)
        self.requests: list[dict[str, Any]] = []

    def __enter__(self) -> StubGovernance:
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.shutdown()
        self.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/governance/workflow/authorize"


class StubHandler(BaseHTTPRequestHandler):
    server: StubGovernance

    def answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        raw = self.rfile.read(length) if length else b""
        self.server.requests.append(
            {
                "method": self.command,
                "path": self.path,
                "authorization": self.headers.get("Authorization"),
                "body": json.loads(raw) if raw else None,
            }
        )
        status, body = self.server.answers.pop(0)
        # Bytes are sent as they are, to stand in for a broken reply.
        encoded = body if isinstance(body, bytes) else json.dumps(body).encode()
        self.send_response(status)
        if isinstance(body, dict) and "Location" in body:
            self.send_header("Location", body["Location"])
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(encoded)))
        self.end_headers()
        self.wfile.write(encoded)

    do_GET = answer
    do_POST = answer

    def log_message(self, format: str, *args: Any) -> None:
        pass


class DripHandler(BaseHTTPRequestHandler):
    """Sends an approval one byte at a time, each byte inside the socket timeout."""

    server: DripGovernance

    def do_POST(self) -> None:
        self.rfile.read(int(self.headers.get("Content-Length") or 0))
        body = b'{"approved":true' + b" " * 40 + b"}"
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        for byte in body:
            if self.server.stopped.wait(0.3):
                return
            try:
                self.wfile.write(bytes([byte]))
            except OSError:
                return

    def log_message(self, format: str, *args: Any) -> None:
        pass


class DripGovernance(ThreadingHTTPServer):
    def __init__(self) -> None:
        super().__init__(("127.0.0.1", 0), DripHandler)
        self.stopped = threading.Event()

    def __enter__(self) -> DripGovernance:
        threading.Thread(target=self.serve_forever, kwargs={"poll_interval": 0.05}, daemon=True).start()
        return self

    def __exit__(self, *exc_info: object) -> None:
        self.stopped.set()
        self.shutdown()
        self.server_close()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/governance/workflow/authorize"


class EndToEndTests(unittest.TestCase):
    """Run the script as the runtime does: JSON on stdin, decision on stdout."""

    def run_hook(
        self,
        event: dict[str, Any] | str,
        url: str | None,
        extra: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("MEZMO_GOVERNANCE_", "MEZMO_LEDGER_"))
            and key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
        }
        env.update(
            {
                "NO_PROXY": "*",
                "MEZMO_GOVERNANCE_TOKEN": "sts_test",
                "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "20",
                "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "1",
                "MEZMO_GOVERNANCE_AUDIT_LOG": str(Path(self.tmp.name) / "audit.jsonl"),
            }
        )
        if url is not None:
            env["MEZMO_GOVERNANCE_URL"] = url
        env.update(extra or {})
        return subprocess.run(
            [sys.executable, str(HOOK_PATH)],
            input=event if isinstance(event, str) else json.dumps(event),
            env=env,
            capture_output=True,
            text=True,
            timeout=60,
            check=False,
        )

    def setUp(self) -> None:
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)

    def test_approved_call_runs(self) -> None:
        with StubGovernance([(200, {"approved": True})]) as stub:
            result = self.run_hook(hook_event(PRE_EVENT), stub.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(stdout_json(result), ALLOW_OUTPUT)
        self.assertEqual(stub.requests[0]["method"], "POST")
        self.assertEqual(stub.requests[0]["authorization"], "Token sts_test")
        self.assertEqual(stub.requests[0]["body"]["origin"]["kind"], ORIGIN_KIND)

    def test_denied_call_prints_the_runtime_deny_schema(self) -> None:
        with StubGovernance([(200, {"approved": False, "reason": "No."})]) as stub:
            result = self.run_hook(hook_event(PRE_EVENT), stub.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            stdout_json(result),
            deny_schema("Mezmo Governance denied this exact tool call: No."),
        )

    def test_a_redirect_denies_and_never_sends_the_key_elsewhere(self) -> None:
        cases = [
            ([], (200, {"approved": True})),
            ([(409, {})], (200, {"status": "approved"})),
        ]
        for before, approval in cases:
            with self.subTest(after_conflict=bool(before)):
                with StubGovernance([approval]) as elsewhere:
                    redirect = (302, {"Location": elsewhere.url})
                    with StubGovernance([*before, redirect]) as stub:
                        result = self.run_hook(hook_event(PRE_EVENT), stub.url)

                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotEqual(stdout_json(result), ALLOW_OUTPUT)
                self.assertIn("HTTP 302", json.dumps(stdout_json(result)))
                self.assertEqual(elsewhere.requests, [])

    def test_conflict_polls_the_existing_decision(self) -> None:
        answers = [(409, {"code": "ECONFLICT"}), (200, {"status": "denied", "reason": "No."})]
        with StubGovernance(answers) as stub:
            result = self.run_hook(hook_event(PRE_EVENT), stub.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(
            stdout_json(result),
            deny_schema("Mezmo Governance denied this exact tool call: No."),
        )
        decision_id = stub.requests[0]["body"]["decision_id"]
        self.assertEqual(stub.requests[1]["method"], "GET")
        self.assertIn(f"decision_id={decision_id}", stub.requests[1]["path"])

    def test_missing_url_denies(self) -> None:
        result = self.run_hook(hook_event(PRE_EVENT), None)

        self.assertEqual(result.returncode, 0, result.stderr)
        output = stdout_json(result)
        self.assertEqual(set(output), set(deny_schema("x")))
        self.assertIn("MEZMO_GOVERNANCE_URL", json.dumps(output))

    def test_a_reply_it_cannot_read_denies(self) -> None:
        cases = [
            ([(200, b"<html>bad gateway</html>")], "invalid JSON response"),
            ([(200, b"[]")], "non-object response"),
            ([(200, b"")], "did not return an approval decision"),
            ([(200, {"reason": "no field"})], "did not return an approval decision"),
            ([(200, {"approved": "true"})], "did not return an approval decision"),
            ([(200, {"approved": 1})], "did not return an approval decision"),
            ([(409, {}), (200, {"status": "maybe"})], "unexpected status"),
            ([(409, {}), (200, {})], "unexpected status"),
            ([(409, {}), (200, {"approved": "yes"})], "unexpected status"),
            ([(409, {}), (200, b"not json")], "invalid JSON response"),
        ]
        for answers, message in cases:
            with self.subTest(answers=answers):
                with StubGovernance(answers) as stub:
                    result = self.run_hook(hook_event(PRE_EVENT), stub.url)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotEqual(stdout_json(result), ALLOW_OUTPUT)
                self.assertIn(message, json.dumps(stdout_json(result)))

    def test_a_mezmo_error_shows_its_code_and_message(self) -> None:
        enoagent = {
            "status": 403,
            "code": "ENOAGENT",
            "message": "no agent is registered for these credentials",
        }
        noisy = {"code": "EINVAL", "message": "bad\nline\x1b[2J " + "x" * 500, "errors": ["secret-arg"]}
        cases = [
            (
                [(403, enoagent)],
                "Mezmo Governance returned HTTP 403 ENOAGENT: "
                "no agent is registered for these credentials",
            ),
            (
                [(409, {}), (401, {"code": "EAUTH", "message": "Unauthorized"})],
                "Mezmo Governance returned HTTP 401 EAUTH while polling: Unauthorized",
            ),
            ([(400, noisy)], "Mezmo Governance returned HTTP 400 EINVAL: bad line [2J xxx"),
        ]
        for answers, message in cases:
            with self.subTest(answers=answers):
                with StubGovernance(answers) as stub:
                    result = self.run_hook(hook_event(PRE_EVENT), stub.url)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotEqual(stdout_json(result), ALLOW_OUTPUT)
                shown = json.dumps(stdout_json(result))
                self.assertIn(message, shown)
                # One line, bounded, and nothing beyond code and message.
                self.assertNotIn("\\n", shown)
                self.assertNotIn("\\u001b", shown)
                self.assertNotIn("x" * 201, shown)
                self.assertNotIn("secret-arg", shown)

    def test_a_url_that_would_expose_the_token_is_refused_before_any_request(self) -> None:
        urls = [
            "http://192.0.2.1/governance/workflow/authorize",
            "http://api.example.test/governance/workflow/authorize",
            "ftp://192.0.2.1/governance",
            "file:///etc/hosts",
            "https://user:pw_secret@api.example.test/governance/workflow/authorize",
        ]
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_TOKEN": "sts_test"}), patch.object(
            hook, "build_opener"
        ) as opener:
            for url in urls:
                with self.subTest(url=url):
                    with self.assertRaises(hook.HookContractError) as caught:
                        hook.http_json("POST", url, {}, 1.0)
                    self.assertNotIn("pw_secret", str(caught.exception))
        opener.assert_not_called()

    def test_http_to_this_machine_still_works(self) -> None:
        hosts = ["127.0.0.1", "127.0.0.2", "localhost", "[::1]"]
        with patch.dict(os.environ, {"MEZMO_GOVERNANCE_TOKEN": "sts_test"}), patch.object(
            hook, "build_opener"
        ) as opener:
            response = opener.return_value.open.return_value.__enter__.return_value
            response.status = 200
            response.read.return_value = b'{"approved": true}'
            for host in hosts:
                with self.subTest(host=host):
                    url = f"http://{host}:8787/governance/workflow/authorize"
                    self.assertEqual(
                        hook.http_json("POST", url, {}, 1.0), (200, {"approved": True})
                    )

    def test_no_deny_reason_shows_the_token(self) -> None:
        with StubGovernance([]) as stub:
            cases = [
                (stub.url, {"MEZMO_GOVERNANCE_TOKEN": "sts_secret\nX-Injected: 1"}),
                (stub.url.replace("http://", "http://user:sts_secret@"), {}),
            ]
            for url, extra in cases:
                with self.subTest(url=url, extra=extra):
                    result = self.run_hook(hook_event(PRE_EVENT), url, extra=extra)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    self.assertEqual(set(stdout_json(result)), set(deny_schema("x")))
                    self.assertNotIn("sts_secret", result.stdout + result.stderr)
            self.assertEqual(stub.requests, [])

    def test_unreachable_governance_denies(self) -> None:
        with StubGovernance([]) as stub:
            url = stub.url
        result = self.run_hook(hook_event(PRE_EVENT), url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unavailable", json.dumps(stdout_json(result)))

    def test_unsupported_event_exits_2(self) -> None:
        event = hook_event(PRE_EVENT)
        event["hook_event_name"] = "SomethingElse"
        result = self.run_hook(event, None)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(set(stdout_json(result)), set(deny_schema("x")))
        self.assertIn("unsupported hook event", result.stderr)

    def test_input_too_deep_to_read_is_denied_with_the_block_code(self) -> None:
        # Python 3.9 to 3.13 stop at 1000 levels, later versions at the C stack.
        depth = 200_000
        raw = json.dumps(hook_event(PRE_EVENT))[:-1]
        raw += ', "nested": ' + "[" * depth + "]" * depth + "}"
        result = self.run_hook(raw, None)

        self.assertEqual(result.returncode, 2, result.stderr)
        self.assertEqual(set(stdout_json(result)), set(deny_schema("x")))
        self.assertNotIn("Traceback", result.stderr)

    @unittest.skipIf(os.name == "nt", "Windows has no POSIX file modes")
    def test_the_audit_log_is_private_to_its_owner(self) -> None:
        # The log keeps up to 2000 characters of each tool result.
        fresh = Path(self.tmp.name) / "fresh" / "audit.jsonl"
        older = Path(self.tmp.name) / "older.jsonl"
        older.write_text("", encoding="utf-8")
        older.chmod(0o644)
        for path in (fresh, older):
            with self.subTest(log=path.name), StubGovernance([]) as stub:
                result = self.run_hook(
                    hook_event(POST_EVENT),
                    stub.url,
                    extra={"MEZMO_GOVERNANCE_AUDIT_LOG": str(path)},
                )
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(fresh.parent.stat().st_mode), 0o700)

    def test_a_log_sent_to_dev_null_writes_without_a_warning(self) -> None:
        with StubGovernance([]) as stub:
            result = self.run_hook(hook_event(POST_EVENT), stub.url, extra={"MEZMO_GOVERNANCE_AUDIT_LOG": os.devnull})

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stderr, "")

    def test_post_event_logs_without_calling_governance(self) -> None:
        with StubGovernance([]) as stub:
            result = self.run_hook(hook_event(POST_EVENT), stub.url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout, "")
        self.assertEqual(stub.requests, [])
        log = Path(self.tmp.name) / "audit.jsonl"
        self.assertEqual(json.loads(log.read_text(encoding="utf-8"))["agent"], hook.AGENT)

    def test_a_dripping_answer_is_denied_at_the_deadline(self) -> None:
        with DripGovernance() as stub:
            started = time.monotonic()
            result = self.run_hook(
                hook_event(PRE_EVENT),
                stub.url,
                extra={"MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "2"},
            )
            elapsed = time.monotonic() - started

        self.assertLess(elapsed, 10, "the hook waited past its deadline")
        self.assertEqual(result.returncode, 2, result.stderr)
        output = stdout_json(result)
        self.assertEqual(set(output), set(deny_schema("x")))
        self.assertIn("approval still pending", json.dumps(output))


def stdout_json(result: subprocess.CompletedProcess[str]) -> Any:
    if not result.stdout.strip():
        return None
    return json.loads(result.stdout)


class PolicyGovernance:
    """Keeps decisions by decision_id as Mezmo does: a second POST gets 409."""

    def __init__(self, denied_command: str) -> None:
        self.denied_command = denied_command
        self.decisions: dict[str, str] = {}

    def __call__(
        self,
        method: str,
        url: str,
        payload: Mapping[str, Any] | None,
        timeout: float,
    ) -> tuple[int, dict[str, Any]]:
        if method == "GET":
            decision_id = url.split("decision_id=")[1]
            return 200, {"status": self.decisions[decision_id]}
        assert payload is not None
        decision_id = str(payload["decision_id"])
        if decision_id in self.decisions:
            return 409, {}
        command = payload["items"][0]["arguments"].get("command")
        status = "denied" if command == self.denied_command else "approved"
        self.decisions[decision_id] = status
        return 200, {"approved": status == "approved"}


class CallBindingTests(unittest.TestCase):
    """One tool call id with other arguments is another call, not a retry."""

    def run_call(self, governance: PolicyGovernance, command: str) -> Any:
        event = hook_event(PRE_EVENT)
        event["tool_input"] = {"command": command}
        clock = FakeClock()
        with patch.dict(os.environ, ENV):
            return hook.handle_event(event, http=governance, clock=clock, sleep=clock.sleep)

    def test_changed_arguments_under_one_call_id_get_their_own_decision(self) -> None:
        governance = PolicyGovernance(denied_command="delete-important-data")

        self.assertEqual(self.run_call(governance, "ls"), ALLOW_OUTPUT)
        self.assertNotEqual(self.run_call(governance, "delete-important-data"), ALLOW_OUTPUT)
        self.assertEqual(sorted(governance.decisions.values()), ["approved", "denied"])

    def test_a_retry_with_the_same_arguments_reuses_the_decision(self) -> None:
        governance = PolicyGovernance(denied_command="delete-important-data")

        self.assertEqual(self.run_call(governance, "ls"), ALLOW_OUTPUT)
        self.assertEqual(self.run_call(governance, "ls"), ALLOW_OUTPUT)
        self.assertEqual(list(governance.decisions.values()), ["approved"])


def deny_line(reason: str) -> str:
    """What main() prints for a deny: one compact line."""
    return json.dumps(deny_schema(reason), separators=(",", ":")) + "\n"


class RefusalHandOffTests(unittest.TestCase):
    """handle_event notes each refusal in the list main() passes it, and returns what it always did."""

    def refused(
        self, event: dict[str, Any], http: FakeHttp, env: Mapping[str, str] = ENV, **kwargs: Any
    ) -> list[tuple[str, str]]:
        with patch.dict(os.environ, env):
            expected = hook.handle_event(event, http=FakeHttp(http.responses), **kwargs)
            refusal: list[tuple[str, str]] = []
            output = hook.handle_event(event, http=http, refusal=refusal, **kwargs)
        self.assertEqual(output, expected)
        if refusal:
            self.assertEqual(output["hookSpecificOutput"]["permissionDecision"], "deny")
        return refusal

    def test_a_policy_deny_is_noted_as_denied_with_its_reason(self) -> None:
        http = FakeHttp([(200, {"approved": False, "reason": "Use a canary first."})])
        self.assertEqual(self.refused(hook_event("PreToolUse"), http), [("denied", "Use a canary first.")])
        http = FakeHttp([(200, {"approved": False})])
        self.assertEqual(self.refused(hook_event("PreToolUse"), http), [("denied", "Policy denied the call.")])
        http = FakeHttp([(409, {}), (200, {"status": "denied", "reason": "already decided"})])
        self.assertEqual(self.refused(hook_event("PreToolUse"), http), [("denied", "already decided")])

    def test_an_authorize_error_is_noted_as_error(self) -> None:
        cases = [
            (hook.HookContractError("connection refused"), "connection refused"),
            ((403, {"code": "ENOAGENT", "message": "no agent"}), "Mezmo Governance returned HTTP 403 ENOAGENT: no agent"),
            ((200, {"approved": "yes"}), "Mezmo Governance did not return an approval decision"),
            (RuntimeError("boom"), "boom"),
        ]
        for answer, reason in cases:
            with self.subTest(answer=answer):
                self.assertEqual(self.refused(hook_event("PreToolUse"), FakeHttp([answer])), [("error", reason)])

    def test_a_payload_error_is_noted_as_error(self) -> None:
        event = hook_event("PreToolUse")
        del event["tool_use_id"]
        self.assertEqual(self.refused(event, FakeHttp([])), [("error", "tool_use_id must be a non-empty string")])

    def test_an_approval_still_pending_at_the_deadline_is_noted_as_error(self) -> None:
        clock = FakeClock()
        pending = (207, {"approved": False, "reason": None})
        refusal = self.refused(
            hook_event("PreToolUse"),
            FakeHttp([pending] * 4),
            env={**ENV, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "2"},
            clock=clock,
            sleep=clock.sleep,
        )
        self.assertEqual(refusal, [("error", "Mezmo Governance: approval still pending")])

    def test_an_allowed_call_and_a_result_note_nothing(self) -> None:
        self.assertEqual(self.refused(hook_event("PreToolUse"), FakeHttp([(200, {"approved": True})])), [])
        with tempfile.TemporaryDirectory() as tmp:
            env = {**ENV, "MEZMO_GOVERNANCE_AUDIT_LOG": str(Path(tmp) / "audit.jsonl")}
            self.assertEqual(self.refused(hook_event("PostToolUse"), FakeHttp([]), env=env), [])

    def test_handle_pre_tool_use_takes_the_same_list(self) -> None:
        clock = FakeClock()
        refusal: list[tuple[str, str]] = []
        with patch.dict(os.environ, ENV):
            output = hook.handle_pre_tool_use(
                hook_event("PreToolUse"),
                FakeHttp([(200, {"approved": False, "reason": "No."})]),
                clock,
                clock.sleep,
                refusal=refusal,
            )
            self.assertEqual(output, deny_schema("Mezmo Governance denied this exact tool call: No."))
            self.assertEqual(refusal, [("denied", "No.")])
            # The four positional arguments still work alone, as every caller before used them.
            self.assertEqual(
                hook.handle_pre_tool_use(
                    hook_event("PreToolUse"),
                    FakeHttp([(200, {"approved": False, "reason": "No."})]),
                    clock,
                    clock.sleep,
                ),
                output,
            )

    def test_the_ledger_file_is_not_imported_when_the_hook_loads(self) -> None:
        # Only _tell_ledger imports it.
        tree = ast.parse(HOOK_PATH.read_text(encoding="utf-8"))
        imports = [
            alias.name
            for node in tree.body
            if isinstance(node, (ast.Import, ast.ImportFrom))
            for alias in node.names
        ]
        self.assertNotIn("mezmo_ledger_hook", imports)


class RefusalMainTests(unittest.TestCase):
    """main() tells the ledger of a refusal once the deny is printed. The ledger is a stand-in."""

    # With no MEZMO_GOVERNANCE_URL the hook denies before any request.
    REASON = (
        "Mezmo Governance could not authorize this exact tool call, so Codex blocked it: "
        "MEZMO_GOVERNANCE_URL must be a non-empty string"
    )

    def run_main(self, *, env: Mapping[str, str] | None = None, stdin: str | None = None) -> tuple[int, str]:
        """main() on a PreToolUse call: its exit code and stdout. Each call to the ledger is kept in self.calls."""
        self.calls: list[dict[str, Any]] = []

        def tell(name: str, **details: Any) -> None:
            self.calls.append({**details, "name": name, "printed": out.getvalue()})

        out = StringIO()
        with (
            patch.dict(os.environ, {"MEZMO_GOVERNANCE_TOKEN": "sts_test", **(env or {})}, clear=True),
            patch.object(hook, "_tell_ledger", side_effect=tell),
            patch.object(sys, "stdin", StringIO(json.dumps(hook_event(PRE_EVENT)) if stdin is None else stdin)),
            patch.object(sys, "stdout", out),
            patch.object(sys, "stderr", StringIO()),
        ):
            code = hook.main()
        return code, out.getvalue()

    def test_the_ledger_is_told_once_the_deny_is_printed(self) -> None:
        code, stdout = self.run_main()

        self.assertEqual((code, stdout), (0, deny_line(self.REASON)))
        (call,) = self.calls
        self.assertEqual(call["name"], "end_governance_refusal")
        self.assertEqual(call["hook_input"], hook_event(PRE_EVENT))
        self.assertEqual((call["outcome"], call["reason"]), ("error", "MEZMO_GOVERNANCE_URL must be a non-empty string"))
        self.assertEqual(call["printed"], deny_line(self.REASON))

    def test_a_policy_deny_is_told_to_the_ledger_as_denied(self) -> None:
        with StubGovernance([(200, {"approved": False, "reason": "No."})]) as stub:
            code, stdout = self.run_main(env={"MEZMO_GOVERNANCE_URL": stub.url, "NO_PROXY": "*"})

        self.assertEqual((code, stdout), (0, deny_line("Mezmo Governance denied this exact tool call: No.")))
        (call,) = self.calls
        self.assertEqual((call["name"], call["outcome"], call["reason"]), ("end_governance_refusal", "denied", "No."))

    def test_no_refusal_is_told_when_nothing_was_refused(self) -> None:
        with StubGovernance([(200, {"approved": True})]) as stub:
            code, stdout = self.run_main(env={"MEZMO_GOVERNANCE_URL": stub.url, "NO_PROXY": "*"})
        self.assertEqual((code, stdout), (0, ""))
        # Only the approval, for its start time.
        self.assertEqual([call["name"] for call in self.calls], ["save_start_time"])

        # Input main() cannot read into an event blocks with exit code 2, and is not recorded.
        for stdin in ("not json", "[1]", json.dumps({**hook_event(PRE_EVENT), "hook_event_name": "Other"})):
            with self.subTest(stdin=stdin):
                code, stdout = self.run_main(stdin=stdin)
                self.assertEqual(code, 2)
                self.assertEqual(set(json.loads(stdout)), set(deny_schema("x")))
                self.assertEqual(self.calls, [])


class TellLedgerTests(unittest.TestCase):
    """_tell_ledger: the lazy import of the ledger file and the one call into it."""

    def tell(self, module: Any) -> Any:
        with (
            patch.dict(sys.modules, {"mezmo_ledger_hook": module}),
            # It may put its own folder on sys.path, which this process keeps as it was.
            patch.object(sys, "path", list(sys.path)),
        ):
            answer = hook._tell_ledger(
                "end_governance_refusal", hook_input=hook_event(PRE_EVENT), outcome="denied", reason="No."
            )
        return answer

    def fake(self, error: BaseException | None = None) -> Any:
        calls: list[dict[str, Any]] = []

        class Module:
            @staticmethod
            def end_governance_refusal(**kwargs: Any) -> None:
                calls.append(kwargs)
                if error is not None:
                    raise error

        Module.calls = calls  # type: ignore[attr-defined]
        return Module

    def test_it_passes_the_call_on(self) -> None:
        module = self.fake()
        self.assertIsNone(self.tell(module))
        self.assertEqual(module.calls, [{"hook_input": hook_event(PRE_EVENT), "outcome": "denied", "reason": "No."}])

    def test_an_absent_ledger_file_is_a_silent_no_op(self) -> None:
        # None in sys.modules makes the import raise ImportError, as a missing file would.
        with patch.object(sys, "stderr", StringIO()) as stderr:
            self.assertIsNone(self.tell(None))
        self.assertEqual(stderr.getvalue(), "")

    def test_nothing_the_ledger_raises_gets_out(self) -> None:
        for error in (RuntimeError("boom"), KeyboardInterrupt(), SystemExit(7), GeneratorExit()):
            with self.subTest(error=type(error).__name__):
                module = self.fake(error)
                self.assertIsNone(self.tell(module))
                self.assertEqual(len(module.calls), 1)


FAKE_LEDGER_PRELUDE = """
import json, os, sys

def note(*values):
    with open(os.environ["MARKER"], "a") as marker:
        marker.write(json.dumps(values) + "\\n")

note("imported")
"""


def fake_ledger(at_import: str = "", body: str = "") -> str:
    """Source for a stand-in mezmo_ledger_hook.py that runs `at_import` when imported and whose
    end_governance_refusal runs `body`.
    """
    lines = "".join(f"    {line}\n" for line in body.splitlines())
    return (
        FAKE_LEDGER_PRELUDE
        + at_import
        + "\n\ndef end_governance_refusal(*, hook_input, outcome, reason):\n"
        + '    note("called", outcome, reason, hook_input["tool_use_id"])\n'
        + lines
    )


class LedgerBesideTheHookTests(unittest.TestCase):
    """The hook as Codex runs it, from a temp folder with a stand-in ledger file beside it.

    Whatever that file raises, when imported or called, the deny on stdout and the exit code stay
    what they are without it.
    """

    DENY = deny_line("Mezmo Governance denied this exact tool call: No.").encode()

    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = Path(scratch.name)
        self.hooks = self.dir / "hooks"
        self.hooks.mkdir()
        shutil.copy(HOOK_PATH, self.hooks / HOOK_PATH.name)
        self.marker = self.dir / "marker.jsonl"

    def run_hook(self, ledger: str | None, *, url: str | None = None) -> subprocess.CompletedProcess[bytes]:
        """Run the copied hook on a call governance denies, and keep the stand-in's notes from this run alone.

        `ledger` is the stand-in's source. None leaves the folder as it is.
        """
        if ledger is not None:
            (self.hooks / LEDGER_PATH.name).write_text(ledger, encoding="utf-8")
        self.marker.unlink(missing_ok=True)
        with StubGovernance([(200, {"approved": False, "reason": "No."})]) as stub:
            return subprocess.run(
                [sys.executable, str(self.hooks / HOOK_PATH.name)],
                capture_output=True,
                check=False,
                # Built from nothing, so no setting of the developer's own reaches the hook.
                env={
                    "HOME": str(self.dir),
                    "MARKER": str(self.marker),
                    "MEZMO_GOVERNANCE_AUDIT_LOG": str(self.dir / "audit.jsonl"),
                    "MEZMO_GOVERNANCE_TOKEN": "sts_test",
                    "MEZMO_GOVERNANCE_URL": url or stub.url,
                    "NO_PROXY": "*",
                    "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                },
                input=json.dumps(hook_event(PRE_EVENT)).encode(),
                timeout=60,
            )

    def notes(self) -> list[list[Any]]:
        if not self.marker.exists():
            return []
        return [json.loads(line) for line in self.marker.read_text(encoding="utf-8").splitlines()]

    def assert_deny_unchanged(self, result: subprocess.CompletedProcess[bytes]) -> None:
        self.assertEqual((result.returncode, result.stdout), (0, self.DENY), result.stderr)

    def test_the_ledger_is_told_of_the_deny(self) -> None:
        result = self.run_hook(fake_ledger())

        self.assert_deny_unchanged(result)
        self.assertEqual(result.stderr, b"")
        self.assertEqual(self.notes(), [["imported"], ["called", "denied", "No.", "tool-use-1"]])

    def test_an_error_is_told_too(self) -> None:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            closed = f"http://127.0.0.1:{probe.getsockname()[1]}/governance/workflow/authorize"
        told = self.run_hook(fake_ledger(), url=closed)
        _, (_, outcome, reason, _) = self.notes()
        (self.hooks / LEDGER_PATH.name).unlink()
        alone = self.run_hook(None, url=closed)

        self.assertEqual((told.returncode, told.stdout), (alone.returncode, alone.stdout))
        self.assertEqual(told.returncode, 0)
        self.assertEqual(self.notes(), [])
        self.assertEqual(outcome, "error")
        self.assertIn("Mezmo Governance is unavailable", reason)
        self.assertIn(reason, json.loads(told.stdout)["hookSpecificOutput"]["permissionDecisionReason"])

    def test_an_absent_ledger_file_changes_nothing(self) -> None:
        # An older file install has only the governance script.
        result = self.run_hook(None)

        self.assert_deny_unchanged(result)
        self.assertEqual(result.stderr, b"")
        self.assertEqual([path.name for path in self.hooks.iterdir()], [HOOK_PATH.name])

    def test_a_ledger_that_raises_or_exits_cannot_change_the_exit_code(self) -> None:
        for body in ('raise RuntimeError("boom")', "raise KeyboardInterrupt()", "raise SystemExit(7)", "sys.exit(7)"):
            with self.subTest(body=body):
                result = self.run_hook(fake_ledger(body=body))
                self.assert_deny_unchanged(result)
                self.assertEqual([note[0] for note in self.notes()], ["imported", "called"])
        for at_import in ('raise RuntimeError("boom")', "sys.exit(7)", "this is not python"):
            with self.subTest(at_import=at_import):
                result = self.run_hook(fake_ledger(at_import=at_import))
                self.assert_deny_unchanged(result)
                self.assertNotIn("called", [note[0] for note in self.notes()])


if __name__ == "__main__":
    unittest.main()
