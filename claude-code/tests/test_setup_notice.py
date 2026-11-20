import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, Dict, Optional

SCRIPT = Path(__file__).resolve().parent.parent / "scripts" / "setup_notice.py"
HOOKS = Path(__file__).resolve().parent.parent / "hooks" / "hooks.json"
KEY = "sts_notice-test"
NO_KEY = "Mezmo Governance is off: run /mezmo:setup to turn it on."
NO_MCP = "Mezmo is on, but Claude can't read the Mezmo ledger yet: run /mezmo:setup mcp to connect it."


class SetupNoticeTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = Path(scratch.name)
        self.config_dir = self.dir / "claude-config"
        self.project = self.dir / "project"
        self.config_dir.mkdir()
        self.project.mkdir()

    def run_hook(self, options: Optional[Dict[str, str]] = None, stdin: Optional[str] = None) -> Any:
        # Built from nothing, so no key or setting of the developer's own reaches the hook.
        env = {
            "CLAUDE_CONFIG_DIR": str(self.config_dir),
            "HOME": str(self.dir / "home"),
            "MEZMO_GOVERNANCE_STATE_DIR": str(self.dir / "state"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            **(options or {}),
        }
        event = json.dumps({"hook_event_name": "SessionStart", "source": "startup", "cwd": str(self.project)})
        result = subprocess.run(
            [sys.executable, str(SCRIPT)], input=event if stdin is None else stdin,
            capture_output=True, env=env, text=True, timeout=30, check=False,
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return json.loads(result.stdout) if result.stdout.strip() else None

    def keyed(self) -> Dict[str, str]:
        return {"CLAUDE_PLUGIN_OPTION_GOVERNANCE_ACCESS_KEY": KEY}

    def write_claude_json(self, data: Any) -> None:
        (self.config_dir / ".claude.json").write_text(json.dumps(data), encoding="utf-8")

    def assert_says(self, output: Any, message: str) -> None:
        self.assertEqual(output, {
            "systemMessage": message,
            "hookSpecificOutput": {"hookEventName": "SessionStart", "additionalContext": message},
        })

    def test_no_key_names_setup(self) -> None:
        self.assert_says(self.run_hook(), NO_KEY)

    def test_governance_turned_off_says_nothing(self) -> None:
        for value in ("false", "0", "off", " NO "):
            with self.subTest(value=value):
                self.assertIsNone(self.run_hook({"CLAUDE_PLUGIN_OPTION_GOVERNANCE_ENABLED": value}))

    def test_a_key_without_the_mcp_names_setup_mcp(self) -> None:
        self.assert_says(self.run_hook(self.keyed()), NO_MCP)
        # Another server, or one in another project, isn't Mezmo's here.
        self.write_claude_json({"mcpServers": {"other": {}}, "projects": {"/elsewhere": {"mcpServers": {"mezmo": {}}}}})
        self.assert_says(self.run_hook(self.keyed()), NO_MCP)
        # An unreadable config counts as no server, and the hook still exits 0.
        (self.config_dir / ".claude.json").write_text("{not json", encoding="utf-8")
        self.assert_says(self.run_hook(self.keyed()), NO_MCP)

    def test_a_key_and_the_mcp_in_any_scope_say_nothing(self) -> None:
        for name, write in (
            ("user", lambda: self.write_claude_json({"mcpServers": {"mezmo": {"type": "http"}}})),
            ("local", lambda: self.write_claude_json({"projects": {str(self.project): {"mcpServers": {"mezmo": {}}}}})),
            ("project", lambda: (self.project / ".mcp.json").write_text(json.dumps({"mcpServers": {"mezmo": {}}}))),
        ):
            with self.subTest(scope=name):
                for path in (self.config_dir / ".claude.json", self.project / ".mcp.json"):
                    path.unlink(missing_ok=True)
                write()
                self.assertIsNone(self.run_hook(self.keyed()))

    def test_without_claude_config_dir_it_reads_the_home_config(self) -> None:
        home = self.dir / "home"
        home.mkdir()
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {"mezmo": {}}}), encoding="utf-8")
        self.assert_says(self.run_hook(self.keyed()), NO_MCP)
        self.assertIsNone(self.run_hook({**self.keyed(), "CLAUDE_CONFIG_DIR": ""}))

    def test_input_it_cant_read_still_exits_0(self) -> None:
        for stdin in ("", "not json", "[1]", '{"cwd": 5}'):
            with self.subTest(stdin=stdin):
                self.assert_says(self.run_hook(self.keyed(), stdin=stdin), NO_MCP)

    def test_it_runs_once_at_startup(self) -> None:
        (group,) = json.loads(HOOKS.read_text(encoding="utf-8"))["hooks"]["SessionStart"]
        self.assertEqual(group["matcher"], "startup")
        (handler,) = group["hooks"]
        self.assertEqual(handler["command"], 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_notice.py"')
        self.assertLessEqual(handler["timeout"], 5)


if __name__ == "__main__":
    unittest.main()
