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
OTHER_API = "https://api.example.test"
OTHER_URL = "https://api.example.test/governance/workflow/authorize"
OTHER_LEDGER_URL = "https://api.example.test/v1/audit/events"
PROD_URL = "https://api.mezmo.com/governance/workflow/authorize"
PROD_LEDGER_URL = "https://api.mezmo.com/v1/audit/events"


class FakeHttp:
    """Governance answers a GET with `status`; the ledger answers a POST with `ledger`."""

    def __init__(self, status=404, body='{"status":404,"code":"ENOENT","message":"workflow not found"}', error=None,
                 ledger=(202, '{"accepted":0}'), mcp=(200, "{}")):
        self.calls = []
        self.status, self.body, self.error, self.ledger, self.mcp = status, body, error, ledger, mcp

    def __call__(self, method, url, headers, body=None, timeout=15.0):
        self.calls.append({"method": method, "url": url, "headers": dict(headers), "body": body})
        if self.error:
            raise self.error
        if url.endswith("/mcp"):
            return self.mcp
        return self.ledger if method == "POST" else (self.status, self.body)


class FakeClaude:
    """Stands in for `claude plugin configure`. Like the real one, it keeps a sensitive value
    when given an empty string, and `inputs` carries only non-sensitive values."""

    def __init__(self, installed=True, saved_url=None, key_set=False, enabled=None, refuse=None, ledger_enabled=None,
                 legacy_url=None):
        self.calls = []
        self.installed, self.saved_url, self.key_set, self.refuse = installed, saved_url, key_set, refuse
        # None: never saved, so the manifest default (on) applies
        self.enabled, self.ledger_enabled = enabled, ledger_enabled
        # A governance_url an older release saved, shown only while the CLI still lists it.
        self.legacy_url = legacy_url
        # The MCP server called mezmo: {"scope", "url"}, or None.
        self.mcp = None

    def __call__(self, args, stdin=None):
        self.calls.append({"args": list(args), "stdin": stdin})
        if args == ["--version"]:
            return 0, "2.1.301 (Claude Code)"
        if args[:1] == ["mcp"]:
            if args[1] == "get":
                if self.mcp is None:
                    return 1, 'No MCP server found with name: "mezmo".'
                return 0, f"mezmo:\n  Scope: {self.mcp['scope']} config\n  Status: ✔ Connected\n  URL: {self.mcp['url']}\n"
            if args[1] == "remove":
                self.mcp = None
                return 0, "Removed"
            if args[1] == "add":
                self.mcp = {"scope": "user", "url": args[args.index("mezmo") + 1]}
                return 0, "Added"
        if not self.installed:
            return 1, f'No installed plugin has the id "{args[2]}".'
        if "--values-stdin" in args:
            if self.refuse:
                return 1, json.dumps({"refused": {"message": self.refuse}})
            values = json.loads(stdin)
            if "api_url" in values:
                self.saved_url = values["api_url"] or None
            if values.get("governance_access_key"):
                self.key_set = True
            if "governance_enabled" in values:
                self.enabled = values["governance_enabled"] == "true"
            if "ledger_enabled" in values:
                self.ledger_enabled = values["ledger_enabled"] == "true"
            return 0, json.dumps({"saved": sorted(values)})
        configured = ((["governance_access_key"] if self.key_set else [])
                      + (["api_url"] if self.saved_url else [])
                      + (["governance_enabled"] if self.enabled is not None else [])
                      + (["ledger_enabled"] if self.ledger_enabled is not None else [])
                      + (["governance_url"] if self.legacy_url else []))
        inputs = {
            "governance_enabled": True if self.enabled is None else self.enabled,
            "ledger_enabled": True if self.ledger_enabled is None else self.ledger_enabled,
        }
        if self.saved_url:
            inputs["api_url"] = self.saved_url
        if self.legacy_url:
            inputs["governance_url"] = self.legacy_url
        names = ("governance_access_key", "api_url", "governance_enabled", "ledger_enabled")
        return 0, json.dumps({
            "configured": configured,
            "unconfigured": [k for k in names if k not in configured],
            "inputs": inputs,
        })


def saved(claude):
    """What the last `claude plugin configure --values-stdin` was given."""
    return [call for call in claude.calls if "--values-stdin" in call["args"]][-1]["stdin"]


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

    def test_the_ledger_takes_an_empty_batch_and_records_nothing(self):
        http = FakeHttp()
        result = gc.check_ledger(KEY, OTHER_LEDGER_URL, send=http)
        self.assertTrue(result.ok, result.detail)
        self.assertEqual(result.status, 202)
        (call,) = http.calls
        self.assertEqual((call["method"], call["url"], call["body"]), ("POST", OTHER_LEDGER_URL, b"[]"))
        self.assertEqual(call["headers"]["Authorization"], f"Token {KEY}")
        self.assertEqual(call["headers"]["Content-Type"], "application/json")

    def test_the_ledger_check_fails_on_anything_but_202_with_nothing_accepted(self):
        for answer, expected in (
            ((401, '{"code":"EAUTH"}'), "rejected"),
            ((202, '{"accepted":1}'), "Unexpected"),
            ((202, "not json"), "Unexpected"),
            ((404, "{}"), "HTTP 404"),
        ):
            with self.subTest(answer=answer):
                result = gc.check_ledger(KEY, OTHER_LEDGER_URL, send=FakeHttp(ledger=answer))
                self.assertFalse(result.ok)
                self.assertIn(expected, result.detail)
        result = gc.check_ledger(KEY, OTHER_LEDGER_URL, send=FakeHttp(error=OSError("refused")))
        self.assertFalse(result.ok)
        self.assertIn("Couldn't reach the Mezmo ledger", result.detail)

    def test_the_api_url_is_the_scheme_and_host_of_any_url_on_it(self):
        for url in (OTHER_API, OTHER_API + "/", OTHER_URL, f" {OTHER_LEDGER_URL}?x=1 "):
            with self.subTest(url=url):
                self.assertEqual(gc.api_base(url), OTHER_API)
        self.assertEqual(gc.api_base("http://127.0.0.1:8080/governance/workflow/authorize"), "http://127.0.0.1:8080")
        for url in ("api.example.test", "ftp://api.example.test", ""):
            with self.subTest(url=url), self.assertRaises(ValueError):
                gc.api_base(url)


class CliCase(unittest.TestCase):
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


class CliTest(CliCase):
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
            self.assertEqual([call["method"] for call in http.calls], ["GET", "POST"])
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

    def test_configure_checks_both_endpoints_then_saves_to_the_plugin(self):
        claude = FakeClaude()
        http = FakeHttp()
        code, out, err = self.run_cli("configure", "--key", KEY, "--api-url", OTHER_API + "/", "--no-mcp",
                                      claude=claude, http=http)
        self.assertEqual(code, 0, err)
        self.assertEqual([(call["method"], call["url"].split("?")[0]) for call in http.calls],
                         [("GET", OTHER_URL), ("POST", OTHER_LEDGER_URL)])
        save = [call for call in claude.calls if "--values-stdin" in call["args"]][-1]
        self.assertEqual(save["args"][:4], ["plugin", "configure", "mezmo@mezmo", "--values-stdin"])
        self.assertEqual(json.loads(save["stdin"]), {
            "governance_access_key": KEY, "api_url": OTHER_API, "governance_enabled": "true", "ledger_enabled": "true",
            "mcp_url": "off",
        })
        self.assertNotIn(KEY, out + err)
        self.assertIn("sts_…cdef", out)
        self.assertIn(f"recorded at {OTHER_LEDGER_URL}", out)
        self.assertIn("ledger_enabled", out)
        self.assertNotIn("Warning", out)

    def test_configure_saves_when_only_the_ledger_check_fails(self):
        claude = FakeClaude()
        code, out, err = self.run_cli("configure", "--key", KEY, claude=claude, http=FakeHttp(ledger=(401, "{}")))
        self.assertEqual(code, 0, err)
        self.assertIn("ledger rejected", out)
        self.assertIn("Warning: the ledger check failed", out)
        self.assertTrue(claude.key_set)

    def test_configure_moves_an_older_governance_url_option_to_the_api_url(self):
        self.settings.write_text(json.dumps({"model": "opus", "pluginConfigs": {"mezmo@mezmo": {"options": {
            "governance_url": OTHER_URL, "other": "kept",
        }}}}))
        for claude in (FakeClaude(), FakeClaude(legacy_url=OTHER_URL)):
            with self.subTest(shown_by_the_cli=bool(claude.legacy_url)):
                http = FakeHttp()
                code, out, err = self.run_cli("configure", "--key", KEY, "--no-mcp", claude=claude, http=http)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(saved(claude))["api_url"], OTHER_API)
                self.assertTrue(http.calls[0]["url"].startswith(OTHER_URL))
                self.assertIn(f"to api_url ({OTHER_API})", out)
                self.assertEqual(json.loads(self.settings.read_text()),
                                 {"model": "opus", "pluginConfigs": {"mezmo@mezmo": {"options": {"other": "kept"}}}})
                if claude.legacy_url is None:
                    self.settings.write_text(json.dumps({"model": "opus", "pluginConfigs": {"mezmo@mezmo": {"options": {
                        "other": "kept"}}}}))
        # An API URL given on the command line wins over the older option.
        claude = FakeClaude(legacy_url=OTHER_URL)
        self.run_cli("configure", "--key", KEY, "--api-url", "https://api.mezmo.com", claude=claude)
        self.assertEqual(json.loads(saved(claude))["api_url"], "")

    def test_configure_refuses_a_rejected_key(self):
        claude = FakeClaude()
        code, _, err = self.run_cli("configure", "--key", KEY, http=FakeHttp(401, "{}"), claude=claude)
        self.assertEqual(code, 3)
        self.assertFalse(any("--values-stdin" in call["args"] for call in claude.calls))
        self.assertIn("rejected", err)

    def test_configure_production_url_saves_an_empty_url(self):
        claude = FakeClaude()
        self.run_cli("configure", "--key", KEY, claude=claude)
        self.assertEqual(json.loads(saved(claude))["api_url"], "")

    def test_configure_when_the_plugin_isnt_installed(self):
        code, _, err = self.run_cli("configure", "--key", KEY, claude=FakeClaude(installed=False))
        self.assertEqual(code, 4)
        self.assertIn("--store settings", err)

    def test_configure_into_the_settings_file(self):
        code, out, err = self.run_cli("configure", "--key", KEY, "--api-url", OTHER_API, "--store", "settings", "--no-mcp")
        self.assertEqual(code, 0, err)
        env = json.loads(self.settings.read_text())["env"]
        self.assertEqual(env, {
            "MEZMO_GOVERNANCE_URL": OTHER_URL, "MEZMO_LEDGER_URL": OTHER_LEDGER_URL, "MEZMO_GOVERNANCE_ACCESS_KEY": KEY,
        })
        self.assertNotIn(KEY, out + err)

    def test_key_from_environment(self):
        claude = FakeClaude()
        code, _, err = self.run_cli("configure", environ={"MEZMO_GOVERNANCE_TOKEN": KEY}, claude=claude)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(saved(claude))["governance_access_key"], KEY)

    def test_remove_clears_plugin_options_and_settings(self):
        self.settings.write_text(json.dumps({"model": "opus", "env": {"MEZMO_GOVERNANCE_ACCESS_KEY": KEY, "FOO": "1"}}))
        claude = FakeClaude(key_set=True, saved_url=OTHER_URL)
        code, out, err = self.run_cli("remove", claude=claude)
        self.assertEqual(code, 0, err)
        # An empty string can't clear a sensitive option, so the key is overwritten and the switch turned off.
        self.assertEqual(json.loads(saved(claude)), {
            "governance_access_key": "off", "api_url": "", "governance_enabled": "false", "ledger_enabled": "false",
            # The plugin's MCP server would otherwise keep connecting with "Bearer off".
            "mcp_url": "off",
        })
        self.assertEqual(json.loads(self.settings.read_text()), {"model": "opus", "env": {"FOO": "1"}})
        code, out, _ = self.run_cli("status", "--json", claude=claude)
        self.assertFalse(json.loads(out)["configured"])
        self.assertFalse(json.loads(out)["ledger"]["on"])

    def test_remove_mcp_only_turns_off_the_plugins_server_and_an_earlier_one(self):
        claude = FakeClaude(key_set=True)
        claude.mcp = {"scope": "user", "url": PROD_MCP_URL}
        code, out, err = self.run_cli("remove", "--mcp-only", claude=claude)
        self.assertEqual(code, 0, err)
        # Only the MCP URL changes: governance and the ledger keep their key.
        self.assertEqual(json.loads(saved(claude)), {"mcp_url": "off"})
        self.assertIsNone(claude.mcp)
        self.assertIn("connects nowhere", out)
        self.assertEqual(out.splitlines()[-1], "Run /reload-plugins (or start a new Claude Code session) to apply it.")

    def test_remove_overwrites_key_even_when_governance_is_disabled(self):
        claude = FakeClaude(key_set=True, enabled=False)
        code, out, _ = self.run_cli("remove", claude=claude)
        self.assertEqual(code, 0)
        self.assertIn("saved key is overwritten", out)
        self.assertEqual(json.loads(saved(claude))["governance_access_key"], "off")
        # The CLI cannot distinguish a removed placeholder from a saved secret.
        code, out, err = self.run_cli("remove", claude=claude)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(saved(claude))["governance_access_key"], "off")

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
                                    claude=FakeClaude(key_set=True, saved_url=OTHER_API))
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
                                     claude=FakeClaude(key_set=True, saved_url=OTHER_API))
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

    def test_status_warns_of_ledger_variables_the_hooks_ignore(self):
        marker = "0123456789abcdef" * 2
        code, out, _ = self.run_cli("status", "--json")
        self.assertEqual((code, json.loads(out)["warnings"]), (0, []))
        environ = {"MEZMO_LEDGER_INCIDENT_KEY": "pagerduty:TEST-1", "MEZMO_LEDGER_ACCOUNT": "leftover"}
        code, out, _ = self.run_cli("status", environ=environ)
        self.assertEqual(code, 0)
        self.assertIn("Warning: MEZMO_LEDGER_INCIDENT_KEY=pagerduty:TEST-1 is set (from environment), but not by "
                      "ledger_investigation.py launch, so the ledger hooks ignore it", out)
        self.assertIn("Warning: MEZMO_LEDGER_ACCOUNT is set (from environment), but unused since 0.6.0. Remove it.", out)
        # Set by launch: nothing to warn of.
        code, out, _ = self.run_cli("status", "--json", environ={**environ, "MEZMO_LEDGER_LAUNCHED": marker})
        self.assertEqual(json.loads(out)["warnings"], [
            "MEZMO_LEDGER_ACCOUNT is set (from environment), but unused since 0.6.0. Remove it.",
        ])
        # A marker that isn't launch's shape vouches for nothing.
        code, out, _ = self.run_cli("status", "--json", environ={**environ, "MEZMO_LEDGER_LAUNCHED": "1"})
        self.assertEqual(len(json.loads(out)["warnings"]), 2)
        # Another key in user settings wins, and the launch marker does not vouch for it.
        self.settings.write_text(json.dumps({"env": {"MEZMO_LEDGER_INCIDENT_KEY": "jira:LOG-1"}}))
        code, out, _ = self.run_cli("status", "--json", environ={"MEZMO_LEDGER_LAUNCHED": marker})
        (warning,) = json.loads(out)["warnings"]
        self.assertIn("MEZMO_LEDGER_INCIDENT_KEY=jira:LOG-1 is set (from user settings)", warning)

    def test_status_shows_the_ledger_and_its_retry_file(self):
        spool = self.dir / "ledger-spool.jsonl"
        spool.write_text('{"event":{},"url":"x"}\n{"event":{},"url":"x"}\n')
        environ = {"MEZMO_LEDGER_SPOOL": str(spool)}
        cases = [
            # (plugin, environment, on, url)
            (FakeClaude(key_set=True, saved_url=OTHER_API), {}, True, OTHER_LEDGER_URL),
            # The ledger has its own switch: governance's leaves it on.
            (FakeClaude(key_set=True, enabled=False), {}, True, PROD_LEDGER_URL),
            (FakeClaude(key_set=True, ledger_enabled=False), {}, False, None),
            (FakeClaude(key_set=True, ledger_enabled=False), {"MEZMO_LEDGER_URL": OTHER_LEDGER_URL}, True, OTHER_LEDGER_URL),
            (FakeClaude(), {}, False, None),
            (FakeClaude(), {"MEZMO_GOVERNANCE_TOKEN": KEY}, True, PROD_LEDGER_URL),
        ]
        for claude, env, on, url in cases:
            with self.subTest(env=env, ledger_enabled=claude.ledger_enabled, key_set=claude.key_set):
                code, out, err = self.run_cli("status", "--json", environ={**environ, **env}, claude=claude)
                self.assertEqual(code, 0, err)
                ledger = json.loads(out)["ledger"]
                self.assertEqual((ledger["on"], ledger["url"], ledger["waiting"]), (on, url, 2))
                self.assertNotIn("readable_key", ledger)
                self.assertNotIn(KEY, out)
        code, out, _ = self.run_cli("status", environ=environ, claude=FakeClaude(key_set=True, ledger_enabled=False))
        self.assertIn("The Mezmo ledger is off: the plugin's ledger_enabled option is off.", out)
        self.assertIn("Waiting in the retry file", out)

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
        # MEZMO_GOVERNANCE_URL names governance only: the ledger URL comes from the API URL option or production.
        self.assertEqual(http.calls[1]["url"], PROD_LEDGER_URL)

    def test_check_fails_when_the_ledger_does_and_skips_a_ledger_switched_off(self):
        environ = {"MEZMO_GOVERNANCE_TOKEN": KEY}
        code, out, _ = self.run_cli("check", environ=environ, http=FakeHttp(ledger=(401, "{}")))
        self.assertEqual(code, gc.EXIT_REJECTED)
        self.assertIn(f"FAIL  {PROD_LEDGER_URL}", out)
        http = FakeHttp(ledger=(401, "{}"))
        code, out, _ = self.run_cli("check", environ=environ, http=http, claude=FakeClaude(ledger_enabled=False))
        self.assertEqual(code, 0, out)
        self.assertIn("skip  the Mezmo ledger is off", out)
        self.assertEqual([call["method"] for call in http.calls], ["GET"])

    def test_check_without_a_readable_key(self):
        code, _, err = self.run_cli("check", claude=FakeClaude(key_set=True))
        self.assertEqual(code, 2)
        self.assertIn("sensitive plugin option", err)

    def test_managed_merges_into_an_existing_document(self):
        doc = self.dir / "managed.json"
        doc.write_text(json.dumps({"env": {"CLAUDE_CODE_ENABLE_TELEMETRY": "1"}}))
        code, out, err = self.run_cli("managed", "--key", KEY, "--api-url", OTHER_API, "--required", "--output", str(doc))
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(doc.read_text())["env"], {
            "CLAUDE_CODE_ENABLE_TELEMETRY": "1",
            "MEZMO_GOVERNANCE_URL": OTHER_URL,
            "MEZMO_GOVERNANCE_ACCESS_KEY": KEY,
            "MEZMO_GOVERNANCE_REQUIRED": "1",
            "MEZMO_LEDGER_URL": OTHER_LEDGER_URL,
        })
        self.assertNotIn(KEY, out)

    def test_managed_rerun_drops_a_stale_required(self):
        doc = self.dir / "managed.json"
        self.run_cli("managed", "--key", KEY, "--required", "--output", str(doc))
        self.run_cli("managed", "--key", KEY, "--output", str(doc))
        self.assertNotIn("MEZMO_GOVERNANCE_REQUIRED", json.loads(doc.read_text())["env"])


PROD_MCP_URL = "https://mcp.mezmo.com/mcp"
OTHER_MCP_URL = "https://mcp.example.test/mcp"


class FakeSecurity:
    """Stands in for macOS's `security`: `key` is the Keychain item's password, or None for no item."""

    def __init__(self, key=None):
        self.key, self.calls = key, []

    def __call__(self, args):
        self.calls.append(list(args))
        if self.key is None:
            return 44, ""
        return 0, (self.key + "\n") if "-w" in args else 'keychain: "login.keychain-db"\n'


class SetupConnectsMcpTest(CliCase):
    """The plugin declares the mezmo MCP server from its options, so configure saves the MCP URL with
    the key, checks the key against it, and adds no server of its own."""

    def mcp_calls(self, claude, verb):
        return [call["args"] for call in claude.calls if call["args"][:2] == ["mcp", verb]]

    def test_production_saves_the_mcp_url_and_checks_the_key_without_adding_a_server(self):
        claude, http = FakeClaude(), FakeHttp()
        code, out, err = self.run_cli("configure", "--key", KEY, claude=claude, http=http)
        self.assertEqual(code, 0, err)
        self.assertEqual(json.loads(saved(claude))["mcp_url"], PROD_MCP_URL)
        self.assertEqual(self.mcp_calls(claude, "add"), [])
        (check,) = [call for call in http.calls if call["url"] == PROD_MCP_URL]
        self.assertEqual(check["headers"]["Authorization"], f"Bearer {KEY}")
        self.assertIn(f"Mezmo MCP: the plugin connects to {PROD_MCP_URL} with this key.", out)
        self.assertEqual(out.splitlines()[-1], "Run /reload-plugins (or start a new Claude Code session) to apply it.")
        self.assertNotIn(KEY, out + err)

    def test_another_api_url_needs_its_mcp_url_before_anything_is_checked_or_saved(self):
        claude, http = FakeClaude(), FakeHttp()
        code, _, err = self.run_cli("configure", "--key", KEY, "--api-url", OTHER_API, claude=claude, http=http)
        self.assertEqual(code, gc.EXIT_USAGE)
        self.assertIn("--mcp-url", err)
        self.assertEqual(http.calls, [])
        self.assertFalse(any("--values-stdin" in call["args"] for call in claude.calls))

    def test_another_api_url_saves_the_mcp_url_given(self):
        claude = FakeClaude()
        code, _, err = self.run_cli("configure", "--key", KEY, "--api-url", OTHER_API, "--mcp-url", OTHER_MCP_URL,
                                    claude=claude)
        self.assertEqual(code, 0, err)
        values = json.loads(saved(claude))
        self.assertEqual((values["api_url"], values["mcp_url"]), (OTHER_API, OTHER_MCP_URL))

    def test_an_mcp_url_that_is_not_https_is_refused(self):
        code, _, err = self.run_cli("configure", "--key", KEY, "--mcp-url", "http://mcp.example.test/mcp")
        self.assertEqual(code, gc.EXIT_ERROR)
        self.assertIn("https://", err)

    def test_a_rejected_mcp_key_still_saves_and_warns(self):
        claude = FakeClaude()
        code, out, err = self.run_cli("configure", "--key", KEY, claude=claude, http=FakeHttp(mcp=(401, "{}")))
        self.assertEqual(code, 0, err)
        self.assertTrue(claude.key_set)
        self.assertIn(f"Warning: Mezmo MCP at {PROD_MCP_URL} didn't accept the key", out)

    def test_an_earlier_user_scope_server_is_removed_and_a_project_one_is_named(self):
        claude = FakeClaude()
        claude.mcp = {"scope": "user", "url": PROD_MCP_URL}
        code, out, err = self.run_cli("configure", "--key", KEY, claude=claude)
        self.assertEqual((code, claude.mcp), (0, None), err)
        self.assertEqual(self.mcp_calls(claude, "remove"), [["mcp", "remove", "mezmo", "-s", "user"]])
        self.assertIn("the plugin brings its own", out)
        claude = FakeClaude()
        claude.mcp = {"scope": "project", "url": OTHER_MCP_URL}
        code, out, err = self.run_cli("configure", "--key", KEY, claude=claude)
        self.assertEqual((code, self.mcp_calls(claude, "remove")), (0, []), err)
        self.assertIn("Remove it there to avoid duplicate tools", out)

    def test_no_mcp_turns_the_plugins_server_off_so_the_key_goes_nowhere(self):
        # The plugin's server always sends the saved key; an empty URL would mean production's.
        for args in ((), ("--api-url", OTHER_API)):
            with self.subTest(args=args):
                claude, http = FakeClaude(), FakeHttp()
                code, out, err = self.run_cli("configure", "--key", KEY, "--no-mcp", *args, claude=claude, http=http)
                self.assertEqual(code, 0, err)
                self.assertEqual(json.loads(saved(claude))["mcp_url"], "off")
                self.assertFalse(any(call["args"][:1] == ["mcp"] for call in claude.calls))
                self.assertNotIn(PROD_MCP_URL, [call["url"] for call in http.calls])
                self.assertIn("Mezmo MCP is off", out)

    def test_mcp_only_saves_the_key_with_governance_and_the_ledger_off(self):
        claude, http = FakeClaude(), FakeHttp()
        code, out, err = self.run_cli("configure", "--key", KEY, "--mcp-only", claude=claude, http=http)
        self.assertEqual(code, 0, err)
        values = json.loads(saved(claude))
        self.assertEqual((values["governance_enabled"], values["ledger_enabled"]), ("false", "false"))
        self.assertEqual([call["url"] for call in http.calls], [PROD_MCP_URL])
        self.assertIn("Mezmo Governance and the ledger are off", out)
        for args in (("--no-mcp",), ("--store", "settings")):
            with self.subTest(args=args):
                self.assertEqual(self.run_cli("configure", "--key", KEY, "--mcp-only", *args)[0], gc.EXIT_USAGE)


class SavedKeyTest(CliCase):
    """The key the Codex setup saved in the macOS Keychain, used only when asked and never printed."""

    def keychain(self, key, macos=True):
        security = FakeSecurity(key)
        for started in (patch.object(gc.mc, "run_security", security), patch.object(gc.mc, "is_macos", lambda: macos)):
            started.start()
            self.addCleanup(started.stop)
        return security

    def test_saved_key_says_whether_there_is_one_without_reading_it(self):
        security = self.keychain(KEY)
        (self.dir / ".mezmo").mkdir()
        (self.dir / ".mezmo" / "codex.json").write_text(json.dumps({"api_url": OTHER_API + "/"}))
        code, out, err = self.run_cli("saved-key", environ={"HOME": str(self.dir)})
        self.assertEqual(code, 0, err)
        self.assertIn("A Mezmo key is saved on this machine", out)
        self.assertIn(f"Mezmo API URL {OTHER_API}.", out)
        self.assertEqual(security.calls, [["find-generic-password", "-s", "mezmo-ai-governance", "-a", "codex"]])
        self.assertNotIn(KEY, out + err)

    def test_saved_key_without_one_or_off_macos(self):
        self.keychain(None)
        code, out, _ = self.run_cli("saved-key", environ={"HOME": str(self.dir)})
        self.assertEqual((code, out.strip()), (gc.EXIT_NO_SAVED_KEY, "No Mezmo key is saved on this machine."))
        security = self.keychain(KEY, macos=False)
        self.assertEqual(self.run_cli("saved-key")[0], gc.EXIT_NO_SAVED_KEY)
        self.assertEqual(security.calls, [])

    def test_configure_uses_the_saved_key_for_governance_the_ledger_and_mcp(self):
        security = self.keychain(KEY)
        claude, http = FakeClaude(), FakeHttp()
        code, out, err = self.run_cli("configure", "--use-saved-key", claude=claude, http=http)
        self.assertEqual(code, 0, err)
        self.assertEqual(security.calls, [["find-generic-password", "-s", "mezmo-ai-governance", "-a", "codex", "-w"]])
        self.assertEqual(json.loads(saved(claude))["governance_access_key"], KEY)
        self.assertEqual({call["headers"]["Authorization"] for call in http.calls}, {f"Token {KEY}", f"Bearer {KEY}"})
        self.assertNotIn(KEY, out + err)

    def test_configure_with_no_saved_key_saves_nothing(self):
        self.keychain(None)
        claude, http = FakeClaude(), FakeHttp()
        code, _, err = self.run_cli("configure", "--use-saved-key", claude=claude, http=http)
        self.assertEqual(code, gc.EXIT_ERROR)
        self.assertIn("No usable Mezmo key", err)
        self.assertEqual(http.calls, [])
        self.assertFalse(claude.key_set)


class LedgerLastSendTest(CliCase):
    """status reports the ledger hook's last send from the session state."""

    def session(self, name, last, age=0):
        self.sessions.mkdir(exist_ok=True)
        path = self.sessions / f"{name}.json"
        path.write_text(json.dumps({"session_id": name, "counts": {}, "pending": {}, **({"ledger_last_send": last} if last else {})}))
        if age:
            os.utime(path, (path.stat().st_mtime - age,) * 2)

    def status(self):
        code, out, err = self.run_cli("status", "--state-dir", str(self.sessions), claude=FakeClaude(key_set=True),
                                      environ={"MEZMO_LEDGER_SPOOL": str(self.dir / "spool.jsonl")})
        self.assertEqual(code, 0, err)
        return out

    def last(self, status, error=None, events=2):
        return {"ts": "2026-11-18T16:14:40.500000+00:00", "host": "api.mezmo.com", "status": status, "error": error,
                "events": events}

    def test_a_202_is_reported(self):
        self.session("s-1", self.last(202), age=60)
        # The newest session without a send doesn't hide an older one with it.
        self.session("s-2", None)
        self.assertIn("  Ledger: last send 2026-11-18 16:14:40 UTC to api.mezmo.com, HTTP 202, 2 events\n", self.status())
        # Today's send shows only its time.
        today = gc.datetime.datetime(2026, 11, 18, 18, 0, tzinfo=gc.datetime.timezone.utc)
        self.assertEqual(gc.describe_last_send(self.last(202), now=today),
                         (True, "last send 16:14:40 UTC to api.mezmo.com, HTTP 202, 2 events"))

    def test_the_newest_send_wins_whatever_file_changed_last(self):
        # Governance rewrites session files too, so file times don't order the sends.
        old_ok = {**self.last(202), "ts": "2026-11-18T10:00:00+00:00"}
        new_fail = {**self.last(401, events=1), "ts": "2026-11-18T12:00:00+00:00"}
        self.session("s-new", new_fail, age=3600)
        self.session("s-old", old_ok)
        self.assertEqual(gc.latest_ledger_send(self.sessions), new_fail)

    def test_a_401_is_a_failure_with_the_fix(self):
        self.session("s-1", self.last(401, events=1))
        out = self.status()
        self.assertIn("FAIL: Ledger: last send 2026-11-18 16:14:40 UTC to api.mezmo.com, HTTP 401, 1 event:", out)
        self.assertIn("Rerun /mezmo:setup governance", out)
        code, text, _ = self.run_cli("status", "--json", "--state-dir", str(self.sessions), claude=FakeClaude(key_set=True),
                                     environ={"MEZMO_LEDGER_SPOOL": str(self.dir / "spool.jsonl")})
        ledger = json.loads(text)["ledger"]
        self.assertEqual((ledger["last_send"]["status"], ledger["last_send_ok"]), (401, False))

    def test_a_network_error_and_another_status_are_failures(self):
        self.session("s-1", self.last(None, error="timed out"))
        self.assertIn("failed, 2 events: timed out", self.status())
        self.session("s-1", self.last(404))
        self.assertIn("HTTP 404, 2 events, not 202", self.status())

    def test_no_send_yet_suggests_a_harmless_command(self):
        out = self.status()
        self.assertIn("Ledger: no send recorded yet", out)
        self.assertIn("printf 'mezmo-onboarding-test\\n'", out)


if __name__ == "__main__":
    unittest.main()
