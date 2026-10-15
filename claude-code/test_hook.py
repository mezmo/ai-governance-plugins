"""Contract tests for the Claude Code Mezmo Governance hook."""

from __future__ import annotations

import ast
import json
import os
import shutil
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


AUTHORIZE_URL = "https://api.example.test/governance/workflow/authorize"
ENV = {
    "MEZMO_GOVERNANCE_URL": AUTHORIZE_URL,
    "MEZMO_GOVERNANCE_ACCESS_KEY": "sts_test",
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



class ClaudeCodeGovernanceHookTests(unittest.TestCase):
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
        self.assertEqual(
            before.request_id,
            "claude_code:session-1:turn-1:tool-use-1",
        )

    def test_approval_payload_identifies_claude_code_and_the_mcp_namespace(self) -> None:
        payload = hook.approval_payload(hook_event("PreToolUse"))

        self.assertEqual(
            payload["origin"],
            {"kind": "claude_code_hook", "reason": "PreToolUse"},
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
            patch.object(sys, "stdin", StringIO("not-json")),
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
            self.assertEqual(records[0]["agent"], "claude_code")
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
            {**ENV, "MEZMO_GOVERNANCE_ACCESS_KEY": "Token sts_test"},
        ):
            self.assertEqual(hook.authorization_header(), "Token sts_test")


class PollingTests(unittest.TestCase):
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


class HookConfigTests(unittest.TestCase):
    def load(self, relative: str) -> dict[str, Any]:
        with (ADAPTER_DIR / relative).open(encoding="utf-8") as handle:
            return json.load(handle)

    def test_example_configs_register_every_event_and_block_on_failure(self) -> None:
        for relative in ("settings.example.json", "hooks/hooks.json"):
            config = self.load(relative)["hooks"]
            self.assertEqual(
                set(config),
                {"PreToolUse", "PostToolUse", "PostToolUseFailure"},
                relative,
            )
            for event, groups in config.items():
                for group in groups:
                    for handler in group["hooks"]:
                        with self.subTest(config=relative, event=event):
                            self.assertEqual(handler["type"], "command")
                            self.assertIn("mezmo_governance_hook.py", handler["command"])
                            self.assertIn("statusMessage", handler)
                            if event == "PreToolUse":
                                self.assertEqual(handler["onFailure"], "block")
                                self.assertGreater(
                                    handler["timeout"],
                                    hook.MAX_TIMEOUT_SECONDS,
                                )
                            else:
                                self.assertNotIn("onFailure", handler)

    def test_plugin_hooks_use_the_plugin_root(self) -> None:
        config = self.load("hooks/hooks.json")["hooks"]
        for groups in config.values():
            for group in groups:
                for handler in group["hooks"]:
                    self.assertIn("${CLAUDE_PLUGIN_ROOT}", handler["command"])
        manifest = self.load(".claude-plugin/plugin.json")
        self.assertEqual(manifest["name"], "mezmo-governance")

    def test_plugin_options_match_what_the_hook_reads(self) -> None:
        options = self.load(".claude-plugin/plugin.json")["userConfig"]
        exported = {f"CLAUDE_PLUGIN_OPTION_{key.upper()}" for key in options}

        self.assertEqual(exported, set(hook.PLUGIN_OPTIONS.values()))
        for key, option in options.items():
            with self.subTest(option=key):
                self.assertEqual(option["type"], "string")
                self.assertIs(option["required"], True)
                self.assertTrue(option["title"] and option["description"])
        self.assertIs(options["access_key"]["sensitive"], True)
        self.assertNotIn("default", options["access_key"])
        self.assertEqual(
            options["governance_url"]["default"],
            "https://api.mezmo.com/governance/workflow/authorize",
        )


class PluginOptionTests(unittest.TestCase):
    URL_OPTION = "CLAUDE_PLUGIN_OPTION_GOVERNANCE_URL"
    KEY_OPTION = "CLAUDE_PLUGIN_OPTION_ACCESS_KEY"
    PLUGIN_URL = "https://plugin.example.test/governance/workflow/authorize"

    def test_falls_back_to_plugin_options(self) -> None:
        env = {self.URL_OPTION: self.PLUGIN_URL, self.KEY_OPTION: "sts_plugin"}
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(hook.authorize_url(), self.PLUGIN_URL)
            self.assertEqual(hook.authorization_header(), "Token sts_plugin")

    def test_environment_wins_over_plugin_options(self) -> None:
        env = {
            **ENV,
            self.URL_OPTION: self.PLUGIN_URL,
            self.KEY_OPTION: "sts_plugin",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(hook.authorize_url(), AUTHORIZE_URL)
            self.assertEqual(hook.authorization_header(), "Token sts_test")

    def test_blank_environment_values_fall_back(self) -> None:
        env = {
            "MEZMO_GOVERNANCE_URL": " ",
            "MEZMO_GOVERNANCE_ACCESS_KEY": "",
            self.URL_OPTION: self.PLUGIN_URL,
            self.KEY_OPTION: "sts_plugin",
        }
        with patch.dict(os.environ, env, clear=True):
            self.assertEqual(hook.authorize_url(), self.PLUGIN_URL)
            self.assertEqual(hook.authorization_header(), "Token sts_plugin")

    def test_missing_both_names_each_source(self) -> None:
        with patch.dict(os.environ, {}, clear=True):
            with self.assertRaisesRegex(
                hook.HookContractError,
                "MEZMO_GOVERNANCE_URL.*CLAUDE_PLUGIN_OPTION_GOVERNANCE_URL",
            ):
                hook.authorize_url()
            with self.assertRaisesRegex(
                hook.HookContractError,
                "MEZMO_GOVERNANCE_ACCESS_KEY.*CLAUDE_PLUGIN_OPTION_ACCESS_KEY",
            ):
                hook.authorization_header()

    @patch.dict(os.environ, {}, clear=True)
    def test_status_url_uses_the_plugin_url(self) -> None:
        os.environ[self.URL_OPTION] = self.PLUGIN_URL
        self.assertEqual(
            hook.status_url("d-1"),
            f"{self.PLUGIN_URL}?decision_id=d-1",
        )


HOOK_PATH = Path(__file__).resolve().with_name("mezmo_governance_hook.py")
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
        source = HOOK_PATH.read_text(encoding="utf-8")
        self.assertEqual(runtime_union_lines(source), [])

    def test_hook_imports_on_the_oldest_supported_python(self) -> None:
        interpreter = floor_interpreter()
        if interpreter is None:
            self.skipTest("no Python 3.9 interpreter on PATH")
        result = subprocess.run(
            [
                interpreter,
                "-I",
                "-c",
                "import sys; sys.path.insert(0, sys.argv[1]); "
                "import mezmo_governance_hook",
                str(HOOK_PATH.parent),
            ],
            capture_output=True,
            text=True,
            check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)


# The six Python adapters are standalone copies. These shared parts must stay
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
    "status_url",
    "positive_seconds",
    "timeout_seconds",
    "poll_interval_seconds",
    "audit_log_path",
    "http_json",
    "remaining_seconds",
    "decision_from_post_body",
    "decision_from_status_body",
    "poll_decision",
    "deadline_watchdog",
    "authorize",
    "result_preview",
    "result_has_known_error",
    "append_audit",
)
SIBLING_ADAPTERS = ("codex", "cursor", "gemini-cli", "copilot", "goose")
# Claude Code also reads plugin options here, so only the siblings match.
SIBLING_ONLY_DEFINITIONS = ("authorize_url", "authorization_header")


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
        first, *others = (top_level_definitions(path) for path in present)
        for name in SIBLING_ONLY_DEFINITIONS:
            self.assertIsNotNone(first.get(name))
            for path, sibling in zip(present[1:], others):
                with self.subTest(adapter=path.parent.name, definition=name):
                    self.assertEqual(sibling.get(name), first[name])


PRE_EVENT = "PreToolUse"
POST_EVENT = "PostToolUse"
ALLOW_OUTPUT = None
ORIGIN_KIND = "claude_code_hook"


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
        threading.Thread(target=self.serve_forever, daemon=True).start()
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
        threading.Thread(target=self.serve_forever, daemon=True).start()
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
        event: dict[str, Any],
        url: str | None,
        extra: Mapping[str, str] | None = None,
    ) -> subprocess.CompletedProcess[str]:
        env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("MEZMO_GOVERNANCE_", "CLAUDE_PLUGIN_OPTION_"))
            and key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
        }
        env.update(
            {
                "NO_PROXY": "*",
                "MEZMO_GOVERNANCE_ACCESS_KEY": "sts_test",
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
            input=json.dumps(event),
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
            ([(409, {}), (200, b"not json")], "invalid JSON response"),
        ]
        for answers, message in cases:
            with self.subTest(answers=answers):
                with StubGovernance(answers) as stub:
                    result = self.run_hook(hook_event(PRE_EVENT), stub.url)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertNotEqual(stdout_json(result), ALLOW_OUTPUT)
                self.assertIn(message, json.dumps(stdout_json(result)))

    def test_unreachable_governance_denies(self) -> None:
        with StubGovernance([]) as stub:
            url = stub.url
        result = self.run_hook(hook_event(PRE_EVENT), url)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("unavailable", json.dumps(stdout_json(result)))

    def test_plugin_options_configure_the_hook(self) -> None:
        options = {"CLAUDE_PLUGIN_OPTION_ACCESS_KEY": "sts_plugin"}
        with StubGovernance([(200, {"approved": True})]) as stub:
            options["CLAUDE_PLUGIN_OPTION_GOVERNANCE_URL"] = stub.url
            result = self.run_hook(hook_event(PRE_EVENT), None, options)

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIsNone(stdout_json(result))
        # MEZMO_GOVERNANCE_ACCESS_KEY is set in run_hook, so it wins.
        self.assertEqual(stub.requests[0]["authorization"], "Token sts_test")

    def test_unsupported_event_exits_2(self) -> None:
        event = hook_event(PRE_EVENT)
        event["hook_event_name"] = "SomethingElse"
        result = self.run_hook(event, None)

        self.assertEqual(result.returncode, 2)
        self.assertEqual(result.stdout, "")
        self.assertIn("unsupported hook event", result.stderr)

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


if __name__ == "__main__":
    unittest.main()
