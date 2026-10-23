import contextlib
import io
import json
import os
import pathlib
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / "scripts"))

import governance_config as gc  # noqa: E402

KEY = "sts_0123456789abcdef0123456789abcdef"
OTHER_URL = "https://governance.example.test/governance/workflow/authorize"
PROD_URL = "https://api.mezmo.com/governance/workflow/authorize"


class FakeHttp:
    def __init__(self, status=404, body='{"status":404,"code":"ENOENT","message":"workflow not found"}', error=None):
        self.calls = []
        self.status, self.body, self.error = status, body, error

    def __call__(self, method, url, headers, body=None, timeout=15.0):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if self.error:
            raise self.error
        return self.status, self.body


class FakeClaude:
    """Stands in for `claude plugin configure`. Like the real one, it keeps a sensitive value
    when given an empty string, and `inputs` carries only non-sensitive values."""

    def __init__(self, installed=True, saved_url=None, key_set=False, enabled=None, refuse=None):
        self.calls = []
        self.installed, self.saved_url, self.key_set, self.refuse = installed, saved_url, key_set, refuse
        self.enabled = enabled  # None: never saved, so the manifest default (on) applies

    def __call__(self, args, stdin=None):
        self.calls.append({"args": list(args), "stdin": stdin})
        if args == ["--version"]:
            return 0, "2.1.301 (Claude Code)"
        if not self.installed:
            return 1, f'No installed plugin has the id "{args[2]}".'
        if "--values-stdin" in args:
            if self.refuse:
                return 1, json.dumps({"refused": {"message": self.refuse}})
            values = json.loads(stdin)
            if "governance_url" in values:
                self.saved_url = values["governance_url"] or None
            if values.get("governance_access_key"):
                self.key_set = True
            if "governance_enabled" in values:
                self.enabled = values["governance_enabled"] == "true"
            return 0, json.dumps({"saved": sorted(values)})
        configured = ((["governance_access_key"] if self.key_set else [])
                      + (["governance_url"] if self.saved_url else [])
                      + (["governance_enabled"] if self.enabled is not None else []))
        inputs = {"governance_enabled": True if self.enabled is None else self.enabled}
        if self.saved_url:
            inputs["governance_url"] = self.saved_url
        return 0, json.dumps({
            "configured": configured,
            "unconfigured": [k for k in ("governance_access_key", "governance_url", "governance_enabled") if k not in configured],
            "inputs": inputs,
        })


class CheckTest(unittest.TestCase):
    def test_unknown_decision_404_means_the_key_works(self):
        http = FakeHttp()
        result = gc.check_access(KEY, OTHER_URL, send=http)
        self.assertTrue(result.ok)
        call = http.calls[0]
        self.assertEqual(call["method"], "GET")
        self.assertTrue(call["url"].startswith(OTHER_URL + "?decision_id="))
        self.assertEqual(call["headers"]["Authorization"], f"Token {KEY}")

    def test_rejected_key(self):
        result = gc.check_access(KEY, OTHER_URL, send=FakeHttp(401, '{"code":"EAUTH","message":"Unauthorized"}'))
        self.assertFalse(result.ok)
        self.assertIn("rejected", result.detail)

    def test_network_error(self):
        result = gc.check_access(KEY, OTHER_URL, send=FakeHttp(error=OSError("nodename nor servname provided")))
        self.assertFalse(result.ok)
        self.assertIn("nodename", result.detail)

    def test_bearer_key_is_sent_unchanged(self):
        http = FakeHttp()
        gc.check_access("Bearer " + KEY, OTHER_URL, send=http)
        self.assertEqual(http.calls[0]["headers"]["Authorization"], "Bearer " + KEY)


class CliTest(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory()
        self.dir = pathlib.Path(self._tmp.name)
        self.settings = self.dir / "settings.json"
        self.sessions = self.dir / "sessions"

    def tearDown(self):
        self._tmp.cleanup()

    def run_cli(self, *args, environ=None, http=None, claude=None):
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = gc.main(
                ["--settings-file", str(self.settings), *args],
                environ=environ or {},
                send=http or FakeHttp(),
                claude=claude or FakeClaude(),
                managed_files=[],
            )
        return code, out.getvalue(), err.getvalue()

    def test_preflight_failures_do_not_touch_tokens_network_or_settings(self):
        cases = [
            ((0, "2.1.212 (Claude Code)"), (0, "{}"), 5),
            ((1, ""), (0, "{}"), 5),
            ((127, "not on PATH"), (0, "{}"), 5),
            ((0, "2.1.301"), (1, "error: unknown command 'configure'"), 5),
            ((0, "2.1.301"), (1, 'No installed plugin has the id "mezmo@mezmo".'), 4),
            ((0, "2.1.301"), (1, "configuration unavailable"), 1),
            ((0, "2.1.301"), (0, "not-json"), 1),
            ((0, "2.1.301"), (0, '{"inputs": ["bad"]}'), 1),
        ]
        for version, options, expected in cases:
            def claude(args, stdin=None):
                self.assertIsNone(stdin)
                return version if args == ["--version"] else options
            with self.subTest(version=version, options=options), \
                    patch.object(gc, "_key_from", side_effect=AssertionError("read token")), \
                    patch.object(gc.cco, "read_settings", side_effect=AssertionError("read settings")), \
                    patch.object(gc.cco, "write_settings", side_effect=AssertionError("write settings")):
                http = FakeHttp()
                code, _, _ = self.run_cli("configure", "--key", KEY, claude=claude, http=http)
                self.assertEqual(code, expected)
                self.assertEqual(http.calls, [])
                self.assertFalse(self.settings.exists())

    def test_old_python_or_missing_claude_stops_before_token_access(self):
        with patch.object(gc.sys, "version_info", (3, 8)), \
                patch.object(gc, "_key_from", side_effect=AssertionError("read token")):
            self.assertEqual(self.run_cli("configure", "--key", KEY)[0], 5)
        def missing(args, stdin=None):
            raise FileNotFoundError("claude")
        self.assertEqual(self.run_cli("preflight", claude=missing)[0], 5)

    def test_settings_fallback_does_not_bypass_runtime_minimum(self):
        def old(args, stdin=None):
            return 0, "2.1.212"
        http = FakeHttp()
        code, _, _ = self.run_cli("configure", "--store", "settings", "--key", KEY, claude=old, http=http)
        self.assertEqual(code, 5)
        self.assertEqual(http.calls, [])
        self.assertFalse(self.settings.exists())

    def test_supported_plugin_dir_can_explicitly_use_settings(self):
        code, _, err = self.run_cli("configure", "--store", "settings", "--key", KEY,
                                    claude=FakeClaude(installed=False))
        self.assertEqual(code, 0, err)
        self.assertTrue(self.settings.exists())

    def test_unavailable_plugin_status_is_unknown_or_uses_readable_settings(self):
        for message in ["Error: could not reach the keychain", "unknown command configure"]:
            def unavailable(args, stdin=None):
                return 1, message
            code, out, err = self.run_cli("status", "--json", claude=unavailable)
            self.assertEqual(code, 0, err)
            report = json.loads(out)
            self.assertIsNone(report["configured"])
            self.assertIsNone(report["plugin_installed"])
            self.assertIn("unknown", report["plugin_error"])
            code, out, err = self.run_cli("status", claude=unavailable, environ={"MEZMO_GOVERNANCE_TOKEN": KEY})
            self.assertEqual(code, 0, err)
            self.assertIn("Governance token is configured", out)
            self.assertNotIn("Setup stopped", out + err)

    def test_remove_cleans_settings_when_plugin_read_or_write_fails(self):
        def unavailable(args, stdin=None):
            return 1, "Error: could not reach the keychain"
        for claude in [unavailable, FakeClaude(key_set=True, refuse="keychain locked")]:
            self.settings.write_text(json.dumps({"env": {"MEZMO_GOVERNANCE_TOKEN": KEY, "KEEP": "yes"}}))
            code, out, err = self.run_cli("remove", claude=claude)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(self.settings.read_text()), {"env": {"KEEP": "yes"}})
            self.assertIn("Removal incomplete", err)
            self.assertNotIn(KEY, out + err)

    def test_check_readable_token_works_when_plugin_is_unavailable(self):
        def unavailable(args, stdin=None):
            return 1, "Error: could not reach the keychain"
        for args, environ in [(("--key", KEY), {}), ((), {"MEZMO_GOVERNANCE_TOKEN": KEY})]:
            http = FakeHttp()
            code, _, err = self.run_cli("check", *args, environ=environ, claude=unavailable, http=http)
            self.assertEqual(code, 0, err)
            self.assertEqual(len(http.calls), 1)
        self.settings.write_text(json.dumps({"env": {"MEZMO_GOVERNANCE_TOKEN": KEY}}))
        self.assertEqual(self.run_cli("check", claude=unavailable)[0], 0)

    def test_check_without_token_reports_unknown_not_off(self):
        code, out, err = self.run_cli("check", claude=lambda args, stdin: (1, "unknown command configure"))
        self.assertEqual(code, 1)
        self.assertIn("unknown", err)
        self.assertNotIn("Setup stopped", out + err)

    def test_preflight_names_selected_executable_and_version(self):
        with patch.object(gc.mc, "claude_executable", return_value="/test/claude"):
            code, out, err = self.run_cli("preflight")
        self.assertEqual(code, 0, err)
        self.assertIn("/test/claude: version 2.1.301", out)

    def test_configure_checks_then_saves_to_the_plugin(self):
        claude = FakeClaude()
        code, out, err = self.run_cli("configure", "--key", KEY, "--url", OTHER_URL, claude=claude)
        self.assertEqual(code, 0, err)
        save = claude.calls[-1]
        self.assertEqual(save["args"][:4], ["plugin", "configure", "mezmo@mezmo", "--values-stdin"])
        self.assertEqual(json.loads(save["stdin"]),
                         {"governance_access_key": KEY, "governance_url": OTHER_URL, "governance_enabled": "true"})
        self.assertNotIn(KEY, out + err)
        self.assertIn("sts_…cdef", out)

    def test_configure_refuses_a_rejected_key(self):
        claude = FakeClaude()
        code, _, err = self.run_cli("configure", "--key", KEY, http=FakeHttp(401, "{}"), claude=claude)
        self.assertEqual(code, 3)
        self.assertFalse(any("--values-stdin" in call["args"] for call in claude.calls))
        self.assertIn("rejected", err)

    def test_configure_production_url_saves_an_empty_url(self):
        claude = FakeClaude()
        self.run_cli("configure", "--key", KEY, claude=claude)
        self.assertEqual(json.loads(claude.calls[-1]["stdin"])["governance_url"], "")

    def test_configure_when_the_plugin_isnt_installed(self):
        code, _, err = self.run_cli("configure", "--key", KEY, claude=FakeClaude(installed=False))
        self.assertEqual(code, 4)
        self.assertIn("--store settings", err)

    def test_configure_into_the_settings_file(self):
        code, out, err = self.run_cli("configure", "--key", KEY, "--url", OTHER_URL, "--store", "settings")
        self.assertEqual(code, 0, err)
        env = json.loads(self.settings.read_text())["env"]
        self.assertEqual(env, {"MEZMO_GOVERNANCE_URL": OTHER_URL, "MEZMO_GOVERNANCE_ACCESS_KEY": KEY})
        self.assertNotIn(KEY, out + err)

    def test_key_from_environment(self):
        claude = FakeClaude()
        code, _, err = self.run_cli("configure", environ={"MEZMO_GOVERNANCE_TOKEN": KEY}, claude=claude)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(claude.calls[-1]["stdin"])["governance_access_key"], KEY)

    def test_remove_clears_plugin_options_and_settings(self):
        self.settings.write_text(json.dumps({"model": "opus", "env": {"MEZMO_GOVERNANCE_ACCESS_KEY": KEY, "FOO": "1"}}))
        claude = FakeClaude(key_set=True, saved_url=OTHER_URL)
        code, out, err = self.run_cli("remove", claude=claude)
        self.assertEqual(code, 0, err)
        # An empty string can't clear a sensitive option, so the key is overwritten and the switch turned off.
        self.assertEqual(json.loads(claude.calls[-1]["stdin"]),
                         {"governance_access_key": "off", "governance_url": "", "governance_enabled": "false"})
        self.assertEqual(json.loads(self.settings.read_text()), {"model": "opus", "env": {"FOO": "1"}})
        code, out, _ = self.run_cli("status", "--json", claude=claude)
        self.assertFalse(json.loads(out)["configured"])

    def test_remove_overwrites_key_even_when_governance_is_disabled(self):
        claude = FakeClaude(key_set=True, enabled=False)
        code, out, _ = self.run_cli("remove", claude=claude)
        self.assertEqual(code, 0)
        self.assertIn("keychain is overwritten", out)
        self.assertEqual(json.loads(claude.calls[-1]["stdin"])["governance_access_key"], "off")
        # The CLI cannot distinguish a removed placeholder from a saved secret.
        code, out, err = self.run_cli("remove", claude=claude)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(claude.calls[-1]["stdin"])["governance_access_key"], "off")

    def test_remove_reports_disabled_key_cleanup_failure_and_cleans_settings(self):
        self.settings.write_text(json.dumps({"env": {"MEZMO_GOVERNANCE_TOKEN": KEY}}))
        claude = FakeClaude(key_set=True, enabled=False, refuse="keychain locked")
        code, out, err = self.run_cli("remove", claude=claude)
        self.assertEqual(code, gc.EXIT_ERROR)
        self.assertIn("Removal incomplete", err)
        self.assertNotIn("env", json.loads(self.settings.read_text()))

    def test_status_switched_off_but_key_kept(self):
        code, out, _ = self.run_cli("status", "--json", claude=FakeClaude(key_set=True, enabled=False))
        report = json.loads(out)
        self.assertFalse(report["configured"])
        self.assertTrue(report["switched_off"])

    def test_status_reports_sources_and_the_newest_session(self):
        self.sessions.mkdir()
        older = self.sessions / "older.json"
        older.write_text(json.dumps({"session_id": "older", "counts": {"allowed": 9}, "pending": {}}))
        os.utime(older, (1, 1))
        (self.sessions / "s-2.json").write_text(json.dumps({
            "session_id": "s-2", "updated": "2026-10-15T20:00:00+00:00",
            "counts": {"allowed": 4, "denied": 1, "expired": 0, "errors": 0},
            "pending": {"d-1": {"tool": "claude_code/Bash", "since_ms": 1}},
            "last": {"ts": "2026-10-15T20:00:00+00:00", "tool": "claude_code/Bash", "outcome": "denied",
                     "detail": "shell is not allowed", "by_person": True},
        }))
        code, out, _ = self.run_cli("status", "--json", "--state-dir", str(self.sessions),
                                    claude=FakeClaude(key_set=True, saved_url=OTHER_URL))
        self.assertEqual(code, 0)
        report = json.loads(out)
        self.assertTrue(report["configured"])
        self.assertEqual(report["source"], "plugin options")
        self.assertEqual(report["url"], OTHER_URL)
        self.assertEqual(report["latest_session"]["session_id"], "s-2")
        self.assertEqual(report["latest_session"]["counts"]["allowed"], 4)
        self.assertEqual(report["latest_session"]["pending"], 1)
        self.assertEqual(report["last_result"]["outcome"], "denied")
        code, text, _ = self.run_cli("status", "--state-dir", str(self.sessions),
                                     claude=FakeClaude(key_set=True, saved_url=OTHER_URL))
        self.assertIn("4 allowed, 1 denied", text)
        self.assertIn("1 waiting for a person", text)
        self.assertIn("decided by a person", text)

    def test_status_without_session_files(self):
        code, out, _ = self.run_cli("status", "--json", "--state-dir", str(self.sessions), claude=FakeClaude())
        self.assertIsNone(json.loads(out)["latest_session"])

    def test_status_environment_wins(self):
        self.settings.write_text(json.dumps({"env": {"MEZMO_GOVERNANCE_ACCESS_KEY": KEY}}))
        code, out, _ = self.run_cli("status", "--json", claude=FakeClaude(key_set=True))
        report = json.loads(out)
        self.assertEqual(report["source"], "user settings")
        self.assertEqual(report["key"], "sts_…cdef")
        self.assertEqual(report["url"], PROD_URL)
        self.assertNotIn(KEY, out)

    def test_status_unconfigured(self):
        code, out, _ = self.run_cli("status", claude=FakeClaude(installed=False))
        self.assertEqual(code, 0)
        self.assertIn("off", out)

    def test_check_uses_the_settings_key(self):
        self.settings.write_text(json.dumps({"env": {"MEZMO_GOVERNANCE_ACCESS_KEY": KEY, "MEZMO_GOVERNANCE_URL": OTHER_URL}}))
        http = FakeHttp()
        code, out, _ = self.run_cli("check", http=http)
        self.assertEqual(code, 0)
        self.assertTrue(http.calls[0]["url"].startswith(OTHER_URL))

    def test_check_without_a_readable_key(self):
        code, _, err = self.run_cli("check", claude=FakeClaude(key_set=True))
        self.assertEqual(code, 2)
        self.assertIn("keychain", err)

    def test_managed_merges_into_an_existing_document(self):
        doc = self.dir / "managed.json"
        doc.write_text(json.dumps({"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "1"}}))
        code, out, err = self.run_cli("managed", "--key", KEY, "--url", OTHER_URL, "--required", "--output", str(doc))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(doc.read_text())["env"], {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "MEZMO_GOVERNANCE_URL": OTHER_URL,
            "MEZMO_GOVERNANCE_ACCESS_KEY": KEY,
            "MEZMO_GOVERNANCE_REQUIRED": "1",
        })
        self.assertNotIn(KEY, out)

    def test_managed_rerun_drops_a_stale_required(self):
        doc = self.dir / "managed.json"
        self.run_cli("managed", "--key", KEY, "--required", "--output", str(doc))
        self.run_cli("managed", "--key", KEY, "--output", str(doc))
        self.assertNotIn("MEZMO_GOVERNANCE_REQUIRED", json.loads(doc.read_text())["env"])


if __name__ == "__main__":
    unittest.main()
