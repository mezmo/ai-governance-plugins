"""Contract tests for the Cursor Mezmo Governance hook."""

from __future__ import annotations

import json
import os
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
            hook_event("preToolUse"),
            http=http,
            clock=clock,
            sleep=clock.sleep,
        )

        self.assertEqual(output["permission"], "allow")
        poll_url = f"{AUTHORIZE_URL}?decision_id={http.calls[0][2]['decision_id']}"
        self.assertEqual(
            [(method, url) for method, url, _, _ in http.calls],
            [("POST", AUTHORIZE_URL), ("GET", poll_url), ("GET", poll_url)],
        )
        self.assertEqual(clock.now, 1.0)

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


if __name__ == "__main__":
    unittest.main()
