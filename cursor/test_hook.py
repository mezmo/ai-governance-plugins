"""Contract tests for the Cursor Mezmo Governance hook."""

from __future__ import annotations

import ast
import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from collections.abc import Mapping
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
        "conversation_id": "conv-1",
        "generation_id": "gen-1",
        "tool_use_id": "tool-use-1",
        "tool_name": "MCP:pods_delete",
        "tool_input": {"namespace": "staging", "name": "demo-pod"},
        "mcp_server_name": "kubernetes_demo",
        "cursor_version": "1.7.2",
    }
    if name == "postToolUse":
        event["tool_output"] = json.dumps({"ok": True})
    if name == "beforeMCPExecution":
        event["tool_name"] = "pods_delete"
        event["tool_input"] = json.dumps({"namespace": "staging", "name": "demo-pod"})
    if name in {"beforeShellExecution", "afterShellExecution"}:
        event.pop("tool_name", None)
        event.pop("tool_input", None)
        event["command"] = "kubectl delete pod demo-pod -n staging"
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


class CursorGovernanceHookTests(unittest.TestCase):
    def test_normalizes_mcp_colon_names(self) -> None:
        normalized = hook.normalize_tool_call(hook_event("preToolUse"))
        self.assertEqual(normalized.namespace, "kubernetes_demo")
        self.assertEqual(normalized.name, "pods_delete")

    def test_parses_string_mcp_arguments(self) -> None:
        normalized = hook.normalize_tool_call(hook_event("beforeMCPExecution"))
        self.assertEqual(normalized.namespace, "kubernetes_demo")
        self.assertEqual(normalized.arguments["name"], "demo-pod")

    def test_normalizes_shell_commands(self) -> None:
        normalized = hook.normalize_tool_call(hook_event("beforeShellExecution"))
        self.assertEqual(normalized.name, "Shell")
        self.assertIn("kubectl", normalized.arguments["command"])

    def test_reuses_identity_across_pre_and_post(self) -> None:
        before = hook.call_identity(hook_event("preToolUse"))
        after = hook.call_identity(hook_event("postToolUse"))
        self.assertEqual(before, after)
        self.assertEqual(before.request_id, "cursor:conv-1:tool-use-1")

    def test_payload_origin(self) -> None:
        payload = hook.approval_payload(hook_event("preToolUse"))
        self.assertEqual(
            payload["origin"],
            {"kind": "cursor_hook", "reason": "preToolUse"},
        )

    @patch.dict(os.environ, ENV)
    def test_allows_with_permission_field(self) -> None:
        http = FakeHttp([(200, {"approved": True})])
        output = hook.handle_event(hook_event("preToolUse"), http=http)
        self.assertEqual(output["permission"], "allow")

    @patch.dict(os.environ, ENV)
    def test_denies_with_agent_message(self) -> None:
        http = FakeHttp([(200, {"approved": False, "reason": "Use a canary first."})])
        output = hook.handle_event(hook_event("preToolUse"), http=http)
        self.assertEqual(output["permission"], "deny")
        self.assertIn("Use a canary first.", output["agent_message"])
        self.assertIn("Use a canary first.", output["user_message"])

    @patch.dict(os.environ, ENV)
    def test_polls_after_timeout(self) -> None:
        http = FakeHttp(
            [
                TimeoutError("held"),
                (200, {"status": "pending", "reason": None}),
                (200, {"status": "approved", "reason": "ok"}),
            ]
        )
        clock = FakeClock()
        output = hook.handle_event(
            hook_event("beforeMCPExecution"),
            http=http,
            clock=clock,
            sleep=clock.sleep,
        )
        self.assertEqual(output["permission"], "allow")
        self.assertEqual(http.calls[1][0], "GET")

    @patch.dict(os.environ, ENV)
    def test_fails_closed_on_transport_error(self) -> None:
        http = FakeHttp([hook.HookContractError("connection refused")])
        output = hook.handle_event(hook_event("preToolUse"), http=http)
        self.assertEqual(output["permission"], "deny")
        self.assertIn("connection refused", output["agent_message"])

    def test_malformed_stdin_uses_the_blocking_exit_code(self) -> None:
        with (
            patch.object(sys, "stdin", StringIO("not-json")),
            patch.object(sys, "stderr", StringIO()),
        ):
            self.assertEqual(hook.main(), 2)

    @patch.dict(os.environ, ENV)
    def test_post_tool_writes_audit(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "audit.jsonl"
            env = {**ENV, "MEZMO_GOVERNANCE_AUDIT_LOG": str(log_path)}
            with patch.dict(os.environ, env):
                output = hook.handle_event(hook_event("postToolUse"))
            self.assertIsNone(output)
            record = json.loads(log_path.read_text(encoding="utf-8").splitlines()[0])
            self.assertEqual(record["agent"], "cursor")
            self.assertEqual(record["tool_name"], "pods_delete")


class CursorToolOutputTests(unittest.TestCase):
    def audit(self, event: dict[str, Any]) -> dict[str, Any]:
        with tempfile.TemporaryDirectory() as tmp:
            log_path = Path(tmp) / "audit.jsonl"
            env = {**ENV, "MEZMO_GOVERNANCE_AUDIT_LOG": str(log_path)}
            with patch.dict(os.environ, env):
                self.assertIsNone(hook.handle_event(event))
            lines = log_path.read_text(encoding="utf-8").splitlines()
        self.assertEqual(len(lines), 1)
        return json.loads(lines[0])

    def test_json_string_tool_output_with_an_error_is_failed(self) -> None:
        for output in (
            {"exitCode": 1, "stdout": "", "stderr": "boom"},
            {"exit_code": 2},
            {"isError": True, "content": []},
        ):
            with self.subTest(output=output):
                event = hook_event("postToolUse")
                event["tool_output"] = json.dumps(output)
                self.assertEqual(self.audit(event)["status"], "failed")

    def test_json_string_tool_output_without_an_error_is_observed(self) -> None:
        for text in (json.dumps({"exitCode": 0}), "not json", "", "[1, 2]"):
            with self.subTest(text=text):
                event = hook_event("postToolUse")
                event["tool_output"] = text
                self.assertEqual(self.audit(event)["status"], "observed")

    def test_after_mcp_result_json_with_an_error_is_failed(self) -> None:
        event = hook_event("beforeMCPExecution")
        event["hook_event_name"] = "afterMCPExecution"
        event["result_json"] = json.dumps({"isError": True, "content": []})
        record = self.audit(event)
        self.assertEqual(record["status"], "failed")
        self.assertIn("isError", record["result_preview"])

    def test_after_shell_output_is_previewed(self) -> None:
        event = hook_event("afterShellExecution")
        event["output"] = "pod \"demo-pod\" deleted"
        record = self.audit(event)
        self.assertEqual(record["status"], "observed")
        self.assertIn("demo-pod", record["result_preview"])


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


if __name__ == "__main__":
    unittest.main()
