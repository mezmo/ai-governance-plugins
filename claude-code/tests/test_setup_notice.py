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
NO_KEY = (
    "Mezmo is installed but has no service key, so Mezmo Governance, the Mezmo ledger and Mezmo MCP are off. "
    "Run /plugin configure mezmo@mezmo to add it, then /reload-plugins."
)
NO_PLUGIN_KEY = (
    "Mezmo's key comes from your settings, not the plugin's options, so the plugin's Mezmo MCP server can't "
    "connect. Add the key with /plugin configure mezmo@mezmo, then /reload-plugins."
)
OLD_SERVER = (
    "Claude Code has a mezmo MCP server an earlier Mezmo setup added, and the plugin now brings its own, so "
    "their tools are duplicated. Remove the old one: claude mcp remove mezmo -s {scope}"
)


class SetupNoticeTests(unittest.TestCase):
    def setUp(self) -> None:
        scratch = tempfile.TemporaryDirectory()
        self.addCleanup(scratch.cleanup)
        self.dir = Path(scratch.name)
        self.config_dir = self.dir / "claude-config"
        self.project = self.dir / "project"
        self.config_dir.mkdir()
        self.project.mkdir()
        self.sessions = 0

    def run_hook(self, options: Optional[Dict[str, str]] = None, stdin: Optional[str] = None,
                 event: str = "UserPromptSubmit", session: Optional[str] = None) -> Any:
        # Built from nothing, so no key or setting of the developer's own reaches the hook.
        env = {
            "CLAUDE_CONFIG_DIR": str(self.config_dir),
            "HOME": str(self.dir / "home"),
            "MEZMO_GOVERNANCE_STATE_DIR": str(self.dir / "state"),
            "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
            **(options or {}),
        }
        if session is None:
            self.sessions += 1
            session = f"session-{self.sessions}"
        line = json.dumps({"hook_event_name": event, "session_id": session, "cwd": str(self.project)})
        result = subprocess.run(
            [sys.executable, str(SCRIPT)], input=line if stdin is None else stdin,
            capture_output=True, env=env, text=True, timeout=30, check=False,
        )
        self.assertEqual((result.returncode, result.stderr), (0, ""))
        return json.loads(result.stdout) if result.stdout.strip() else None

    def keyed(self) -> Dict[str, str]:
        return {"CLAUDE_PLUGIN_OPTION_GOVERNANCE_ACCESS_KEY": KEY}

    def write_claude_json(self, data: Any) -> None:
        (self.config_dir / ".claude.json").write_text(json.dumps(data), encoding="utf-8")

    def assert_says(self, output: Any, message: str, event: str = "UserPromptSubmit") -> None:
        self.assertEqual(output, {
            "systemMessage": message,
            "hookSpecificOutput": {"hookEventName": event, "additionalContext": message},
        })

    def test_no_key_says_how_to_add_it_at_startup_and_on_the_next_prompt(self) -> None:
        self.assert_says(self.run_hook(event="SessionStart"), NO_KEY, event="SessionStart")
        self.assert_says(self.run_hook(), NO_KEY)

    def test_each_session_is_told_once(self) -> None:
        self.assert_says(self.run_hook(session="s-1", event="SessionStart"), NO_KEY, event="SessionStart")
        self.assertIsNone(self.run_hook(session="s-1"))
        self.assertIsNone(self.run_hook(session="s-1"))
        self.assert_says(self.run_hook(session="s-2"), NO_KEY)

    def test_governance_turned_off_without_a_key_says_nothing(self) -> None:
        for value in ("false", "0", "off", " NO "):
            with self.subTest(value=value):
                self.assertIsNone(self.run_hook({"CLAUDE_PLUGIN_OPTION_GOVERNANCE_ENABLED": value}))

    def test_a_key_with_nothing_missing_says_nothing(self) -> None:
        self.assertIsNone(self.run_hook(self.keyed()))
        # Governance off with a key, as an MCP-only setup saves it, still uses the plugin's server.
        self.assertIsNone(self.run_hook({**self.keyed(), "CLAUDE_PLUGIN_OPTION_GOVERNANCE_ENABLED": "false"}))
        # Another server, one in another project, or a project's own .mcp.json aren't setup's leftovers.
        self.write_claude_json({"mcpServers": {"other": {}}, "projects": {"/elsewhere": {"mcpServers": {"mezmo": {}}}}})
        (self.project / ".mcp.json").write_text(json.dumps({"mcpServers": {"mezmo": {}}}))
        self.assertIsNone(self.run_hook(self.keyed()))
        # An unreadable config counts as no old server, and the hook still exits 0.
        (self.config_dir / ".claude.json").write_text("{not json", encoding="utf-8")
        self.assertIsNone(self.run_hook(self.keyed()))

    def test_an_old_mezmo_server_in_user_or_local_scope_is_named_with_how_to_remove_it(self) -> None:
        self.write_claude_json({"mcpServers": {"mezmo": {"type": "http"}}})
        self.assert_says(self.run_hook(self.keyed()), OLD_SERVER.format(scope="user"))
        self.write_claude_json({"projects": {str(self.project): {"mcpServers": {"mezmo": {}}}}})
        self.assert_says(self.run_hook(self.keyed()), OLD_SERVER.format(scope="local"))

    def test_without_claude_config_dir_it_reads_the_home_config(self) -> None:
        home = self.dir / "home"
        home.mkdir()
        (home / ".claude.json").write_text(json.dumps({"mcpServers": {"mezmo": {}}}), encoding="utf-8")
        self.assertIsNone(self.run_hook(self.keyed()))
        self.assert_says(self.run_hook({**self.keyed(), "CLAUDE_CONFIG_DIR": ""}), OLD_SERVER.format(scope="user"))

    def test_input_it_cant_read_still_exits_0(self) -> None:
        for stdin in ("", "not json", "[1]", '{"cwd": 5}'):
            with self.subTest(stdin=stdin):
                self.assert_says(self.run_hook(stdin=stdin), NO_KEY, event="SessionStart")

    def test_it_runs_at_startup_and_on_each_prompt(self) -> None:
        hooks = json.loads(HOOKS.read_text(encoding="utf-8"))["hooks"]
        command = 'python3 "${CLAUDE_PLUGIN_ROOT}/scripts/setup_notice.py"'
        (start,) = hooks["SessionStart"]
        self.assertEqual(start["matcher"], "startup")
        (prompt,) = hooks["UserPromptSubmit"]
        for group in (start, prompt):
            (handler,) = group["hooks"]
            self.assertEqual(handler["command"], command)
            self.assertLessEqual(handler["timeout"], 5)

    def test_a_key_only_in_settings_never_calls_a_working_server_a_duplicate(self) -> None:
        env_key = {"MEZMO_GOVERNANCE_ACCESS_KEY": KEY}
        self.assert_says(self.run_hook(env_key), NO_PLUGIN_KEY)
        # The user's own mezmo server is then the one that works: leave it alone.
        self.write_claude_json({"mcpServers": {"mezmo": {"type": "http"}}})
        self.assertIsNone(self.run_hook(env_key))

    def test_mcp_turned_off_says_nothing_about_it(self) -> None:
        self.write_claude_json({"mcpServers": {"mezmo": {}}})
        self.assertIsNone(self.run_hook({**self.keyed(), "CLAUDE_PLUGIN_OPTION_MCP_URL": "off"}))
        self.assertIsNone(self.run_hook({"MEZMO_GOVERNANCE_ACCESS_KEY": KEY, "CLAUDE_PLUGIN_OPTION_MCP_URL": "off"}))

    def test_another_api_with_productions_mcp_url_says_to_set_it(self) -> None:
        other = {**self.keyed(), "CLAUDE_PLUGIN_OPTION_API_URL": "https://api.example.test/"}
        message = ("Mezmo MCP points at production, but your Mezmo API URL is https://api.example.test. Set that "
                   "environment's MCP URL with /plugin configure mezmo@mezmo, then /reload-plugins.")
        self.assert_says(self.run_hook(other), message)
        self.assert_says(self.run_hook({**other, "CLAUDE_PLUGIN_OPTION_MCP_URL": "https://mcp.mezmo.com/mcp"}), message)
        self.assertIsNone(self.run_hook({**other, "CLAUDE_PLUGIN_OPTION_MCP_URL": "https://mcp.example.test/mcp"}))
        self.assertIsNone(self.run_hook({**self.keyed(), "CLAUDE_PLUGIN_OPTION_API_URL": "https://api.mezmo.com"}))


class ManifestTests(unittest.TestCase):
    def test_the_plugin_brings_the_mezmo_mcp_server_from_its_options(self) -> None:
        manifest = json.loads((SCRIPT.parent.parent / ".claude-plugin" / "plugin.json").read_text(encoding="utf-8"))
        self.assertEqual(manifest["mcpServers"], {"mezmo": {
            "type": "http",
            "url": "${user_config.mcp_url}",
            "headers": {"Authorization": "Bearer ${user_config.governance_access_key}"},
        }})
        options = manifest["userConfig"]
        self.assertEqual(options["mcp_url"]["default"], "https://mcp.mezmo.com/mcp")
        self.assertNotIn("sensitive", options["mcp_url"])
        self.assertTrue(options["governance_access_key"]["sensitive"])
        # The install dialog says what the key turns on.
        self.assertIn("Mezmo MCP", options["governance_access_key"]["description"])


if __name__ == "__main__":
    unittest.main()
