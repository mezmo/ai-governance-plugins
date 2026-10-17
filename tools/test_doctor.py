"""The setup doctor, run as a user runs it: a temp HOME, fake agents on PATH."""

from __future__ import annotations

import ast
import json
import os
import shutil
import socket
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, Union

DOCTOR = Path(__file__).resolve().with_name("doctor.py")
REPO = DOCTOR.parent.parent
CANARY = "canary-key-7f3e9b21"
ENOAGENT = {
    "status": 403,
    "code": "ENOAGENT",
    "message": "no agent is registered for these credentials",
}
Answer = Union[tuple, Callable[[dict], tuple]]
CONFIG_OVERRIDES = {
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "GEMINI_CLI_HOME",
    "COPILOT_HOME",
    "GOOSE_PATH_ROOT",
    "XDG_CONFIG_HOME",
    "PI_CODING_AGENT_DIR",
    "FACTORY_HOME_OVERRIDE",
}


class Handler(BaseHTTPRequestHandler):
    server: StubMezmo

    def answer(self) -> None:
        length = int(self.headers.get("Content-Length") or 0)
        body = json.loads(self.rfile.read(length)) if length else None
        auth = self.headers.get("Authorization")
        request = {"method": self.command, "path": self.path, "auth": auth, "body": body}
        self.server.requests.append(request)
        answer = self.server.answers.get((self.command, auth is not None), (500, {}))
        status, payload = answer(request) if callable(answer) else answer
        data = json.dumps(payload).encode()
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    do_GET = answer
    do_POST = answer

    def log_message(self, *args: Any) -> None:
        pass


class StubMezmo(ThreadingHTTPServer):
    """Answers by (method, has a key); the default is how Mezmo answered live."""

    def __init__(self, answers: dict | None = None) -> None:
        super().__init__(("127.0.0.1", 0), Handler)
        self.requests: list[dict] = []
        self.answers: dict[tuple, Answer] = {
            ("GET", False): (401, {"code": "EAUTH", "message": "Unauthorized"}),
            ("GET", True): (404, {"code": "ENOENT", "message": "workflow not found"}),
            ("POST", True): (200, {"approved": True}),
            **(answers or {}),
        }
        threading.Thread(target=self.serve_forever, daemon=True).start()

    @property
    def url(self) -> str:
        return f"http://127.0.0.1:{self.server_address[1]}/governance/workflow/authorize"

    def close(self) -> None:
        self.shutdown()
        self.server_close()


def closed_port() -> int:
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return probe.getsockname()[1]


class DoctorTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.home = self.tmp / "home"
        self.bin = self.tmp / "bin"
        self.home.mkdir()
        self.bin.mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        self.stub = StubMezmo()
        self.addCleanup(self.stub.close)

    def agent(self, binary: str, version_line: str) -> None:
        path = self.bin / binary
        path.write_text(f"#!/bin/sh\necho '{version_line}'\n")
        path.chmod(0o755)

    def fake_codex(self, hooks: list, server: str = "") -> None:
        """A codex whose app-server answers hooks/list with these hooks, as Codex 0.163.0 did live."""
        self.calls = self.tmp / "app-server.jsonl"
        fake = self.bin / "codex"
        fake.write_text(
            f"#!{sys.executable}\n"
            "import json, sys\n"
            "if sys.argv[1:] == ['--version']:\n"
            "    print('codex-cli 0.163.0')\n"
            "    sys.exit()\n"
            f"{server}\n"
            f"hooks = {hooks!r}\n"
            "for line in sys.stdin:\n"
            "    message = json.loads(line)\n"
            f"    open({str(self.calls)!r}, 'a').write(json.dumps([sys.argv[1:], message]) + '\\n')\n"
            "    if message.get('id') == 1:\n"
            "        print(json.dumps({'id': 1, 'result': {}}), flush=True)\n"
            "    if message.get('id') == 2:\n"
            "        print(json.dumps({'method': 'remoteControl/status/changed', 'params': {}}), flush=True)\n"
            "        print(json.dumps({'id': 2, 'result': {'data': [{'cwd': '.', 'hooks': hooks}]}}), flush=True)\n"
        )
        fake.chmod(0o755)

    def write(self, relative: str, content: Any) -> Path:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        text = content if isinstance(content, str) else json.dumps(content)
        path.write_text(text)
        return path

    def copy_hook(self, adapter: str, relative: str) -> Path:
        path = self.home / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(REPO / adapter / "mezmo_governance_hook.py", path)
        return path

    def doctor(self, *args: str, **env: str | None) -> subprocess.CompletedProcess[str]:
        base = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("MEZMO_GOVERNANCE_", "CLAUDE_PLUGIN_OPTION_"))
            and key not in CONFIG_OVERRIDES
            and key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
        }
        base.update(
            {
                "HOME": str(self.home),
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "NO_PROXY": "*",
                "MEZMO_GOVERNANCE_URL": self.stub.url,
                "MEZMO_GOVERNANCE_TOKEN": CANARY,
            }
        )
        for key, value in env.items():
            if value is None:
                base.pop(key, None)
            else:
                base[key] = value
        result = subprocess.run(
            [sys.executable, str(DOCTOR), *args],
            env=base,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertNotIn(CANARY, result.stdout + result.stderr)
        return result

    def line(self, result: subprocess.CompletedProcess[str], subject: str) -> str:
        found = [line for line in result.stdout.splitlines() if f"  {subject}:" in line]
        self.assertEqual(len(found), 1, f"{subject!r} in:\n{result.stdout}")
        return found[0]

    def lines(self, result: subprocess.CompletedProcess[str], subject: str) -> list[str]:
        return [line for line in result.stdout.splitlines() if f"  {subject}:" in line]

    def assertStatus(self, result: subprocess.CompletedProcess[str], subject: str, status: str) -> str:
        line = self.line(result, subject)
        self.assertTrue(line.startswith(status + " "), line)
        return line


class SettingsTests(DoctorTestCase):
    def test_missing_settings_fail_and_skip_the_mezmo_check(self) -> None:
        result = self.doctor(MEZMO_GOVERNANCE_URL=None, MEZMO_GOVERNANCE_TOKEN=None)

        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertStatus(result, "MEZMO_GOVERNANCE_URL", "FAIL")
        self.assertStatus(result, "MEZMO_GOVERNANCE_TOKEN", "FAIL")
        self.assertStatus(result, "Mezmo", "skip")
        self.assertEqual(self.stub.requests, [])

    def test_http_is_refused_except_for_a_local_mock(self) -> None:
        cases = [
            ("http://api.example.test/governance/workflow/authorize", "FAIL"),
            ("ftp://api.example.test/authorize", "FAIL"),
            (f"https://127.0.0.1:{closed_port()}/governance/workflow/authorize", "ok"),
            (self.stub.url, "ok"),
        ]
        for url, status in cases:
            with self.subTest(url=url):
                result = self.doctor(MEZMO_GOVERNANCE_URL=url)
                self.assertStatus(result, "MEZMO_GOVERNANCE_URL", status)

    def test_seconds_follow_the_hook_rules(self) -> None:
        cases = [
            ({"MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "nan"}, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS", "FAIL"),
            ({"MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "-1"}, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS", "FAIL"),
            ({"MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "900"}, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS", "warn"),
            ({"MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "120"}, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS", "ok"),
            ({"MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "0.1"}, "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS", "warn"),
            ({"MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS": "x"}, "MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS", "FAIL"),
        ]
        for env, subject, status in cases:
            with self.subTest(env=env):
                result = self.doctor(**env)
                self.assertStatus(result, subject, status)
                self.assertEqual(result.returncode, 1 if status == "FAIL" else 0, result.stdout)


class MezmoTests(DoctorTestCase):
    def test_a_good_setup_without_agents_passes(self) -> None:
        result = self.doctor()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertStatus(result, "Mezmo URL", "ok")
        self.assertStatus(result, "Mezmo token", "ok")
        self.assertIn("--probe", self.assertStatus(result, "Mezmo agent", "warn"))
        for name in ("Claude Code", "Codex", "Cursor", "Gemini CLI", "Copilot CLI", "Goose", "OpenCode", "Pi"):
            self.assertStatus(result, name, "skip")
        self.assertIn("examples/check.sh", self.assertStatus(result, "libraries", "skip"))

    def test_without_probe_no_call_reaches_the_authorize_endpoint(self) -> None:
        self.doctor()

        self.assertEqual([request["method"] for request in self.stub.requests], ["GET", "GET"])
        self.assertIsNone(self.stub.requests[0]["auth"])
        self.assertEqual(self.stub.requests[1]["auth"], f"Token {CANARY}")
        self.assertIn("decision_id=", self.stub.requests[1]["path"])

    def test_a_url_that_answers_404_without_a_key_is_not_the_endpoint(self) -> None:
        self.stub.answers[("GET", False)] = (404, {"code": "ENOENT", "message": "Not Found"})

        result = self.doctor()

        self.assertEqual(result.returncode, 1)
        self.assertIn("not 401", self.assertStatus(result, "Mezmo URL", "FAIL"))
        self.assertEqual(self.lines(result, "Mezmo token"), [])

    def test_unreachable_mezmo_fails(self) -> None:
        url = f"http://127.0.0.1:{closed_port()}/governance/workflow/authorize"

        result = self.doctor(MEZMO_GOVERNANCE_URL=url)

        self.assertEqual(result.returncode, 1)
        self.assertIn("not reached", self.assertStatus(result, "Mezmo URL", "FAIL"))

    def test_a_rejected_key_fails_with_mezmo_code(self) -> None:
        self.stub.answers[("GET", True)] = (401, {"code": "EAUTH", "message": "Unauthorized"})

        result = self.doctor()

        self.assertEqual(result.returncode, 1)
        line = self.assertStatus(result, "Mezmo token", "FAIL")
        self.assertIn("HTTP 401 EAUTH: Unauthorized", line)
        self.assertIn("fresh token", line)
        self.assertIn("service account has Governance permissions", line)

    def test_probe_names_epolicyunassigned_and_its_fix(self) -> None:
        unassigned = {"status": 403, "code": "EPOLICYUNASSIGNED", "message": "this agent has no policy assigned"}
        self.stub.answers[("POST", True)] = (403, unassigned)

        result = self.doctor("--probe")

        self.assertEqual(result.returncode, 1)
        line = self.assertStatus(result, "Mezmo agent", "FAIL")
        self.assertIn("HTTP 403 EPOLICYUNASSIGNED: this agent has no policy assigned", line)
        self.assertIn("assign a policy to the agent", line)

    def test_probe_names_enoagent_and_its_fix(self) -> None:
        self.stub.answers[("POST", True)] = (403, ENOAGENT)

        result = self.doctor("--probe")

        self.assertEqual(result.returncode, 1)
        line = self.assertStatus(result, "Mezmo agent", "FAIL")
        self.assertIn("HTTP 403 ENOAGENT: no agent is registered for these credentials", line)
        self.assertIn("register an agent", line)

    def test_probe_sends_the_hook_request_for_doctor_probe(self) -> None:
        result = self.doctor("--probe")

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("approved", self.assertStatus(result, "Mezmo agent", "ok"))
        post = self.stub.requests[-1]
        self.assertEqual(post["method"], "POST")
        self.assertEqual(post["auth"], f"Token {CANARY}")
        self.assertEqual(
            set(post["body"]), {"version", "decision_id", "request_id", "scope", "origin", "items"}
        )
        item = post["body"]["items"][0]
        self.assertEqual((item["tool_namespace"], item["tool_name"], item["arguments"]), ("doctor", "probe", {}))

    def test_two_probes_open_two_decisions(self) -> None:
        self.doctor("--probe")
        self.doctor("--probe")

        ids = [request["body"]["decision_id"] for request in self.stub.requests if request["method"] == "POST"]
        self.assertEqual(len(set(ids)), 2)

    def test_probe_rejected_body_names_einval(self) -> None:
        self.stub.answers[("POST", True)] = (400, {"code": "EINVAL", "message": "Bad Request"})

        result = self.doctor("--probe")

        self.assertIn("update these plugins", self.assertStatus(result, "Mezmo agent", "FAIL"))

    def test_the_key_never_appears_even_when_mezmo_echoes_it(self) -> None:
        def echo(request: dict) -> tuple:
            return 403, {"code": "EKEY", "message": f"bad {request['auth']}"}

        self.stub.answers[("POST", True)] = echo
        result = self.doctor("--probe")  # doctor() asserts the canary is absent

        self.assertIn("<redacted>", self.assertStatus(result, "Mezmo agent", "FAIL"))

    def test_a_bearer_key_is_redacted_without_its_prefix_too(self) -> None:
        self.stub.answers[("GET", True)] = lambda request: (
            401,
            {"code": "EAUTH", "message": request["auth"].split()[-1]},
        )

        result = self.doctor(MEZMO_GOVERNANCE_TOKEN=f"Bearer {CANARY}")

        self.assertIn("<redacted>", self.assertStatus(result, "Mezmo token", "FAIL"))


CLAUDE_SETTINGS_HOOK = {
    "type": "command",
    "command": 'python3 "${MEZMO_GOVERNANCE_HOOK:-$HOME/.claude/hooks/mezmo_governance_hook.py}"',
    "timeout": 600,
    "onFailure": "block",
}


class ClaudeCodeTests(DoctorTestCase):
    def install_settings(self, handler: dict | None = None) -> None:
        self.write(".claude/settings.json", {"hooks": {"PreToolUse": [{"hooks": [handler or CLAUDE_SETTINGS_HOOK]}]}})
        # A settings install from before the plugin; the Python hooks share their core.
        self.copy_hook("codex", ".claude/hooks/mezmo_governance_hook.py")

    def install_plugin(self, enabled: bool = True, name: str = "mezmo") -> Path:
        folder = self.home / f".claude/plugins/cache/mezmo/{name}/0.2.0"
        shutil.copytree(REPO / "claude-code", folder, dirs_exist_ok=True)
        self.write(
            ".claude/plugins/installed_plugins.json",
            {"version": 2, "plugins": {f"{name}@mezmo": [{"scope": "user", "installPath": str(folder)}]}},
        )
        self.write(".claude/settings.json", {"enabledPlugins": {f"{name}@mezmo": enabled}})
        return folder

    def test_an_agent_without_the_plugin_warns(self) -> None:
        self.agent("claude", "2.1.300 (Claude Code)")

        result = self.doctor()

        self.assertEqual(result.returncode, 0, result.stdout)
        claude = self.lines(result, "Claude Code")
        self.assertTrue(claude[0].startswith("ok    Claude Code: 2.1.300"), claude)
        self.assertIn("not installed", claude[1])
        self.assertTrue(claude[1].startswith("warn"))

    def test_an_agent_on_path_gets_a_sample_mezmo_agent_form(self) -> None:
        self.agent("claude", "2.1.300 (Claude Code)")
        host = socket.gethostname().split(".")[0].lower()

        result = self.doctor()

        self.assertEqual(result.returncode, 0, result.stdout)
        form = self.assertStatus(result, "Mezmo agent form for Claude Code", "info")
        self.assertIn(f'Agent Name "Claude Code on {host}"', form)
        self.assertIn('Runtime Name "claude-code"', form)
        self.assertIn('Version "2.1.300"', form)
        self.assertIn(f'Instance ID "claude-code-{host}"', form)
        self.assertEqual(self.lines(result, "Mezmo agent form for Codex"), [])

    def test_versions_below_tested_or_fail_closed_minimum_warn(self) -> None:
        cases = [("2.1.295 (Claude Code)", "tested 2.1.296"), ("2.1.200 (Claude Code)", "fail-closed setting needs")]
        for version, words in cases:
            with self.subTest(version=version):
                self.agent("claude", version)
                result = self.doctor()
                self.assertIn(words, self.lines(result, "Claude Code")[0])
                self.assertTrue(self.lines(result, "Claude Code")[0].startswith("warn"))

    def test_settings_install_passes_with_a_hook_that_denies(self) -> None:
        self.agent("claude", "2.1.300 (Claude Code)")
        self.install_settings()

        result = self.doctor()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("fails closed", self.lines(result, "Claude Code settings")[1])
        self.assertStatus(result, "python3", "ok")
        self.assertIn("denies a call", self.assertStatus(result, "Claude Code smoke", "ok"))

    def test_settings_without_on_failure_fail(self) -> None:
        self.agent("claude", "2.1.300 (Claude Code)")
        self.install_settings({key: value for key, value in CLAUDE_SETTINGS_HOOK.items() if key != "onFailure"})

        result = self.doctor()

        self.assertEqual(result.returncode, 1)
        self.assertTrue(self.lines(result, "Claude Code settings")[1].startswith("FAIL"))

    def test_mezmo_governance_hook_variable_moves_the_script(self) -> None:
        self.agent("claude", "2.1.300 (Claude Code)")
        self.install_settings()
        moved = self.copy_hook("codex", "elsewhere/mezmo_governance_hook.py")

        result = self.doctor(MEZMO_GOVERNANCE_HOOK=str(moved))

        self.assertIn("~/elsewhere/mezmo_governance_hook.py", self.assertStatus(result, "Claude Code smoke", "ok"))

    def test_plugin_install_passes_and_a_disabled_plugin_fails(self) -> None:
        self.agent("claude", "2.1.300 (Claude Code)")
        self.install_plugin()

        result = self.doctor()

        self.assertEqual(result.returncode, 0, result.stdout)
        plugin = self.lines(result, "Claude Code plugin")
        self.assertTrue(plugin[0].startswith("ok") and "enabled" in plugin[0], plugin)
        self.assertTrue(plugin[1].startswith("ok") and "onFailure" in plugin[1], plugin)
        self.assertStatus(result, "Claude Code smoke", "ok")

        self.install_plugin(enabled=False)
        result = self.doctor()
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("not enabled, so every tool call runs ungoverned", self.lines(result, "Claude Code plugin")[0])
        self.assertTrue(self.lines(result, "Claude Code plugin")[0].startswith("FAIL"))

    def test_the_older_mezmo_governance_plugin_is_still_checked(self) -> None:
        self.agent("claude", "2.1.300 (Claude Code)")
        folder = self.install_plugin(name="mezmo-governance")
        # That plugin ran mezmo_governance_hook.py from its root.
        shutil.copy(REPO / "codex" / "mezmo_governance_hook.py", folder / "mezmo_governance_hook.py")
        hooks = (folder / "hooks" / "hooks.json").read_text(encoding="utf-8")
        (folder / "hooks" / "hooks.json").write_text(hooks.replace("scripts/governance_hook.py", "mezmo_governance_hook.py"))

        result = self.doctor()

        self.assertIn("mezmo-governance@mezmo is installed", self.lines(result, "Claude Code plugin")[0])
        self.assertIn("/mezmo_governance_hook.py denies a call", self.assertStatus(result, "Claude Code smoke", "ok"))

    def test_a_config_it_cannot_parse_is_unknown_not_absent(self) -> None:
        self.agent("claude", "2.1.300 (Claude Code)")
        self.write(".claude/settings.json", "{not json")

        result = self.doctor()

        line = self.lines(result, "Claude Code")[1]
        self.assertTrue(line.startswith("warn") and "unknown: could not parse" in line, line)
        self.assertNotIn("not installed", result.stdout)


CODEX_COMMAND = (
    'python3 "${MEZMO_GOVERNANCE_HOOK:-$HOME/.codex/hooks/mezmo_governance_hook.py}"'
    " || { s=$?; [ $s -eq 2 ] || echo 'Mezmo Governance hook failed; blocking' >&2; exit 2; }"
)


# The guard before ec18454: it printed "hook failed" on every block, a deliberate one too.
OLD_GUARD = " || { echo 'Mezmo Governance hook failed; blocking' >&2; exit 2; }"


class CodexTests(DoctorTestCase):
    def install(self, command: str = CODEX_COMMAND, trusted: bool = True, extra: str = "") -> None:
        hooks = self.write(
            ".codex/hooks.json",
            {"hooks": {"PreToolUse": [{"matcher": ".*", "hooks": [{"type": "command", "command": command}]}]}},
        )
        trust = f'[hooks.state."{hooks}:pre_tool_use:0:0"]\ntrusted_hash = "sha256:00"\n' if trusted else ""
        self.write(".codex/config.toml", f"model = \"x\"\n\n{trust}{extra}")
        self.copy_hook("codex", ".codex/hooks/mezmo_governance_hook.py")
        self.fake_codex([{"key": f"{hooks}:pre_tool_use:0:0", "eventName": "preToolUse", "sourcePath": str(hooks), "pluginId": None,
                          "command": command, "enabled": True, "trustStatus": "trusted" if trusted else "untrusted"}])

    def test_a_guarded_trusted_hook_passes(self) -> None:
        self.install()

        result = self.doctor()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("exit 2", self.lines(result, "Codex hook")[1])
        self.assertStatus(result, "Codex trust", "ok")
        self.assertStatus(result, "Codex smoke", "ok")

    def test_the_file_hook_trust_is_what_codex_lists(self) -> None:
        # config.toml keeps a trusted_hash after the hook changes; only Codex can say it no longer matches.
        cases = [
            ({"trustStatus": "trusted"}, "ok", "hook is trusted"),
            ({"trustStatus": "modified"}, "FAIL", "changed since you trusted it"),
            ({"trustStatus": "trusted", "enabled": False}, "FAIL", "turned off in /hooks"),
            (None, "FAIL", "lists no PreToolUse hook"),
        ]
        for listed, status, words in cases:
            with self.subTest(listed=listed):
                self.install()
                path = self.home / ".codex/hooks.json"
                item = {"key": f"{path}:pre_tool_use:0:0", "eventName": "preToolUse", "sourcePath": str(path), "pluginId": None,
                        "command": CODEX_COMMAND, "enabled": True, "currentHash": "sha256:11", **(listed or {})}
                other = {**item, "key": f"{path}:post_tool_use:0:0", "eventName": "postToolUse", "trustStatus": "untrusted"}
                self.fake_codex([item, other] if listed else [other])

                result = self.doctor()

                self.assertEqual(result.returncode, 1 if status == "FAIL" else 0, result.stdout)
                self.assertIn(words, self.assertStatus(result, "Codex trust", status))

    def test_a_codex_that_cannot_list_hooks_leaves_a_saved_trust_unchecked(self) -> None:
        self.install()
        self.agent("codex", "codex-cli 0.162.1")

        result = self.doctor()

        self.assertEqual(result.returncode, 0, result.stdout)
        line = self.assertStatus(result, "Codex trust", "info")
        self.assertIn("not checked", line)
        self.assertIn("codex app-server gave no answer", line)

    def test_the_old_guard_warns_with_the_reinstall_step(self) -> None:
        self.install(CODEX_COMMAND.split(" ||")[0] + OLD_GUARD)

        result = self.doctor()

        self.assertEqual(result.returncode, 0, result.stdout)
        line = self.assertStatus(result, "Codex guard", "warn")
        self.assertIn('also prints "hook failed"', line)
        self.assertIn("./mezmo-governance uninstall codex", line)
        self.assertIn("/hooks", line)
        self.assertIn("fails closed", self.lines(result, "Codex hook")[1])
        self.install()
        self.assertEqual(self.lines(self.doctor(), "Codex guard"), [])

    def test_failures_in_codex_config(self) -> None:
        cases = [
            ({"command": CODEX_COMMAND.split(" ||")[0]}, "Codex hook", "no || exit 2 guard"),
            ({"trusted": False}, "Codex trust", "not trusted"),
            (
                {"trusted": False, "extra": '[hooks.state."elsewhere:pre_tool_use:0:0"]\ntrusted_hash = "x"\n'},
                "Codex trust",
                "not trusted",
            ),
            ({"extra": "\n[features]\nhooks = false\n"}, "Codex hooks", "turns every hook off"),
        ]
        for kwargs, subject, words in cases:
            with self.subTest(kwargs=kwargs):
                self.install(**kwargs)  # type: ignore[arg-type]
                result = self.doctor()
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertTrue(any(line.startswith("FAIL") and words in line for line in self.lines(result, subject)), result.stdout)


class CodexPluginTests(DoctorTestCase):
    """The native plugin: `codex app-server` hooks/list answers for trust, as Codex 0.163.0 did live."""

    def install(self, trust: str = "trusted", enabled: bool = True, listed: bool = True, server: str = "") -> None:
        folder = self.home / ".codex/plugins/cache/mezmo/mezmo-governance/0.2.0"
        shutil.copytree(REPO / "codex", folder, dirs_exist_ok=True)
        self.write(".codex/config.toml", '[plugins."mezmo-governance@mezmo"]\nenabled = true\n')
        key = "mezmo-governance@mezmo:hooks/hooks.json"
        hooks = [
            {"key": f"{key}:pre_tool_use:0:0", "eventName": "preToolUse", "pluginId": "mezmo-governance@mezmo",
             "command": f'python3 "{folder}/mezmo_governance_hook.py" || {{ exit 2; }}',
             "enabled": enabled, "currentHash": "sha256:aa", "trustStatus": trust},
            {"key": f"{key}:post_tool_use:0:0", "eventName": "postToolUse", "pluginId": "mezmo-governance@mezmo",
             "command": f'python3 "{folder}/mezmo_governance_hook.py"',
             "enabled": True, "currentHash": "sha256:bb", "trustStatus": "untrusted"},
        ]
        self.fake_codex(hooks if listed else [], server)

    def test_a_trusted_plugin_passes(self) -> None:
        for trust in ("trusted", "managed"):
            with self.subTest(trust=trust):
                self.install(trust)

                result = self.doctor()

                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn("mezmo-governance@mezmo is installed", self.assertStatus(result, "Codex plugin", "ok"))
                self.assertIn(f"hook is {trust}", self.assertStatus(result, "Codex plugin trust", "ok"))
                self.assertStatus(result, "Codex smoke", "ok")
                self.assertNotIn("not installed", result.stdout)
                calls = [json.loads(line) for line in self.calls.read_text().splitlines()]
                self.assertEqual([call[1].get("method") for call in calls], ["initialize", "initialized", "hooks/list"])
                self.assertEqual(calls[0][0], ["app-server"])
                self.calls.unlink()

    def test_an_untrusted_changed_off_or_missing_hook_fails(self) -> None:
        cases = [
            ({"trust": "untrusted"}, "not trusted, so Codex skips it"),
            ({"trust": "modified"}, "changed since you trusted it"),
            ({"enabled": False}, "turned off in /hooks"),
            ({"listed": False}, "lists no PreToolUse hook"),
        ]
        for kwargs, words in cases:
            with self.subTest(kwargs=kwargs):
                self.install(**kwargs)  # type: ignore[arg-type]

                result = self.doctor()

                self.assertEqual(result.returncode, 1, result.stdout)
                line = self.assertStatus(result, "Codex plugin trust", "FAIL")
                self.assertIn(words, line)
                self.assertIn("Fix:", line)

    def test_an_untrusted_plugin_names_the_fix(self) -> None:
        self.install("untrusted")

        line = self.assertStatus(self.doctor(), "Codex plugin trust", "FAIL")

        self.assertIn("Fix: start codex, run /hooks, and trust both Mezmo hooks", line)

    def test_an_unknown_answer_is_info_not_a_pass_or_a_fail(self) -> None:
        cases = [
            ("sys.exit(1)", "codex app-server gave no answer"),
            ("print('not json', flush=True)", "codex app-server gave no answer"),
            (
                "for line in sys.stdin:\n"
                "    print(json.dumps({'id': json.loads(line).get('id'), 'error': {'message': 'unknown method'}}), flush=True)\n",
                "unknown method",
            ),
        ]
        for server, words in cases:
            with self.subTest(server=server):
                self.install(server=server)

                result = self.doctor()

                self.assertEqual(result.returncode, 0, result.stdout)
                line = self.assertStatus(result, "Codex plugin trust", "info")
                self.assertIn("not checked", line)
                self.assertIn(words, line)

    def test_without_codex_on_path_trust_is_info(self) -> None:
        self.install()
        (self.bin / "codex").unlink()

        result = self.doctor()

        self.assertIn("codex is not on PATH", self.assertStatus(result, "Codex plugin trust", "info"))

    def test_without_the_native_plugin_there_is_no_plugin_line(self) -> None:
        self.install()
        self.write(".codex/config.toml", 'model = "x"\n')

        result = self.doctor()

        self.assertEqual(self.lines(result, "Codex plugin"), [])
        self.assertEqual(self.lines(result, "Codex plugin trust"), [])
        self.assertFalse(self.calls.exists())
        self.assertIn("not installed", self.lines(result, "Codex")[1])


class OtherHookAgentTests(DoctorTestCase):
    def test_cursor_needs_fail_closed(self) -> None:
        self.agent("cursor-agent", "2026.10.14-941492e")
        self.copy_hook("cursor", ".cursor/hooks/mezmo_governance_hook.py")
        command = 'python3 "${MEZMO_GOVERNANCE_HOOK:-$HOME/.cursor/hooks/mezmo_governance_hook.py}"'
        for fail_closed, status in ((True, "ok"), (False, "FAIL")):
            with self.subTest(fail_closed=fail_closed):
                self.write(".cursor/hooks.json", {"version": 1, "hooks": {"preToolUse": [{"command": command, "failClosed": fail_closed}]}})
                result = self.doctor()
                self.assertTrue(self.lines(result, "Cursor hook")[1].startswith(status), result.stdout)
                self.assertStatus(result, "Cursor smoke", "ok")

    def test_droid_needs_the_guard(self) -> None:
        self.agent("droid", "0.244.0")
        self.copy_hook("droid", ".factory/hooks/mezmo_governance_hook.py")
        command = 'python3 "${MEZMO_GOVERNANCE_HOOK:-$HOME/.factory/hooks/mezmo_governance_hook.py}"'
        for suffix, status in ((" || { echo 'failed' >&2; exit 2; }", "ok"), ("", "FAIL")):
            with self.subTest(guard=bool(suffix)):
                self.write(".factory/hooks.json", {"PreToolUse": [{"hooks": [{"type": "command", "command": command + suffix}]}]})
                result = self.doctor()
                self.assertTrue(self.lines(result, "Droid hook")[1].startswith(status), result.stdout)
                self.assertStatus(result, "Droid smoke", "ok")

    def test_droid_reads_hooks_json_over_settings_json_and_obeys_hooks_disabled(self) -> None:
        # Droid 0.244.0 lays hooks.json over the settings.json hooks, one event at a time.
        self.agent("droid", "0.244.0")
        self.copy_hook("droid", ".factory/hooks/mezmo_governance_hook.py")
        mezmo = [{"hooks": [{"type": "command", "command": 'python3 "$HOME/.factory/hooks/mezmo_governance_hook.py" || exit 2'}]}]
        other = [{"hooks": [{"type": "command", "command": "other-hook"}]}]
        cases = [
            ({"settings.json": {"hooks": {"PreToolUse": mezmo}}}, "ok", "in ~/.factory/settings.json"),
            ({"settings.json": {"hooks": {"PreToolUse": mezmo}}, "hooks.json": {"PreToolUse": other}}, None, ""),
            ({"hooks.json": {"PreToolUse": mezmo, "hooksDisabled": True}}, "FAIL", "hooksDisabled turns every hook off"),
            ({"settings.json": {"hooks": {"hooksDisabled": True}}, "hooks.json": {"PreToolUse": mezmo}}, "FAIL", "hooksDisabled"),
            ({"settings.json": {"hooks": {"hooksDisabled": True}}, "hooks.json": {"PreToolUse": mezmo, "hooksDisabled": False}}, "ok", ""),
        ]
        for files, status, words in cases:
            with self.subTest(files=files):
                for name in ("settings.json", "hooks.json"):
                    (self.home / ".factory" / name).unlink(missing_ok=True)
                for name, content in files.items():
                    self.write(f".factory/{name}", content)
                result = self.doctor()
                if status is None:
                    self.assertIn("not installed", self.lines(result, "Droid")[-1])
                    continue
                shown = self.lines(result, "Droid hooks") or self.lines(result, "Droid hook")
                self.assertTrue(shown[0].startswith(status), result.stdout)
                self.assertIn(words, shown[0])

    def windsurf_config(self, suffix: str, events: tuple = ("pre_run_command", "pre_read_code", "pre_write_code", "pre_mcp_tool_use")) -> dict:
        command = 'python3 "${MEZMO_GOVERNANCE_HOOK:-$HOME/.codeium/windsurf/hooks/mezmo_governance_hook.py}"'
        return {"hooks": {event: [{"command": command + suffix}] for event in events}}

    def test_windsurf_needs_the_guard(self) -> None:
        self.agent("devin-desktop", "1.126.0")
        self.copy_hook("windsurf", ".codeium/windsurf/hooks/mezmo_governance_hook.py")
        for suffix, status in ((" || { echo 'failed' >&2; exit 2; }", "ok"), ("", "FAIL")):
            with self.subTest(guard=bool(suffix)):
                self.write(".codeium/windsurf/hooks.json", self.windsurf_config(suffix))
                result = self.doctor()
                self.assertTrue(self.lines(result, "Windsurf hook")[1].startswith(status), result.stdout)
                self.assertStatus(result, "Windsurf smoke", "ok")
                self.assertTrue(self.lines(result, "Windsurf")[0].startswith("ok    Windsurf: 1.126.0"), result.stdout)

    def test_windsurf_names_each_action_its_hook_does_not_gate(self) -> None:
        self.agent("devin-desktop", "1.126.0")
        self.copy_hook("windsurf", ".codeium/windsurf/hooks/mezmo_governance_hook.py")
        self.write(".codeium/windsurf/hooks.json", self.windsurf_config(" || exit 2", ("pre_run_command", "pre_read_code")))
        result = self.doctor()
        failed = [line for line in self.lines(result, "Windsurf hook") if line.startswith("FAIL")]
        self.assertEqual(len(failed), 2, result.stdout)
        self.assertIn("no Mezmo hook on pre_write_code", failed[0])
        self.assertIn("no Mezmo hook on pre_mcp_tool_use", failed[1])

    def test_windsurf_without_a_command_hook_is_still_checked(self) -> None:
        self.agent("devin-desktop", "1.126.0")
        self.copy_hook("windsurf", ".codeium/windsurf/hooks/mezmo_governance_hook.py")
        self.write(".codeium/windsurf/hooks.json", self.windsurf_config(" || exit 2", ("pre_read_code",)))
        result = self.doctor()
        failed = [line for line in self.lines(result, "Windsurf hook") if line.startswith("FAIL")]
        self.assertEqual(len(failed), 3, result.stdout)
        self.assertIn("no Mezmo hook on pre_run_command", failed[0])
        self.assertStatus(result, "Windsurf smoke", "ok")

    def test_windsurf_fails_a_hooks_json_windsurf_would_drop(self) -> None:
        # Windsurf 1.126.0 drops the whole file when it fails its check, so no hook in it runs.
        self.agent("devin-desktop", "1.126.0")
        self.copy_hook("windsurf", ".codeium/windsurf/hooks/mezmo_governance_hook.py")
        config = self.windsurf_config(" || exit 2")
        for extra, words in (({"version": 1}, "top-level key version"), ({"hooks": {**config["hooks"], "on_stop": []}}, "event on_stop")):
            with self.subTest(extra=extra):
                self.write(".codeium/windsurf/hooks.json", {**config, **extra})
                result = self.doctor()
                shown = self.lines(result, "Windsurf hooks")
                self.assertTrue(shown[0].startswith("FAIL"), result.stdout)
                self.assertIn(words, shown[0])
                self.assertIn("so every tool call runs ungoverned", shown[0])

    def antigravity_config(self, **spec: Any) -> dict:
        return {**json.loads((REPO / "antigravity/hooks.example.json").read_text()), **spec}

    def test_antigravity_fails_closed_without_a_guard(self) -> None:
        # Antigravity CLI 1.4.0 blocked the tool on a hook exit 1, exit 2, crash, non-JSON output and timeout, live.
        self.agent("agy", "1.4.0")
        self.copy_hook("antigravity", ".gemini/config/hooks/mezmo_governance_hook.py")
        self.write(".gemini/config/hooks.json", self.antigravity_config(other={"PreInvocation": [{"command": "other-hook"}]}))
        result = self.doctor()
        hook = self.lines(result, "Antigravity CLI hook")
        self.assertTrue(hook[0].startswith("ok    Antigravity CLI hook: PreToolUse hook mezmo-governance in ~/.gemini/config/hooks.json"), result.stdout)
        self.assertTrue(hook[1].startswith("ok    Antigravity CLI hook: fails closed"), result.stdout)
        self.assertStatus(result, "Antigravity CLI smoke", "ok")
        self.assertEqual(self.lines(result, "Antigravity CLI hooks"), [])

    def test_antigravity_fails_a_disabled_hook_and_a_file_antigravity_would_drop(self) -> None:
        # Antigravity CLI 1.4.0 skips a named hook with "enabled": false, and drops a file it cannot read whole, live.
        self.agent("agy", "1.4.0")
        self.copy_hook("antigravity", ".gemini/config/hooks/mezmo_governance_hook.py")
        mezmo = self.antigravity_config()["mezmo-governance"]
        cases = [
            ({"mezmo-governance": {**mezmo, "enabled": False}}, '"enabled": false'),
            ({"mezmo-governance": {**mezmo, "ENABLED": False}}, '"enabled": false'),
            ({"other": {"PostToolUse": [{"hooks": [{"command": "x", "timeout": 1.5}]}]}}, "timeout"),
        ]
        for extra, words in cases:
            with self.subTest(extra=extra):
                self.write(".gemini/config/hooks.json", self.antigravity_config(**extra))
                result = self.doctor()
                shown = self.lines(result, "Antigravity CLI hooks")
                self.assertTrue(shown[0].startswith("FAIL"), result.stdout)
                self.assertIn(words, shown[0])
                self.assertIn("so every tool call runs ungoverned", shown[0])

    def test_an_antigravity_handler_outside_a_matcher_group_is_not_installed(self) -> None:
        # Antigravity CLI 1.4.0 ignores a flat handler under PreToolUse: it runs only handlers in a matcher group.
        self.agent("agy", "1.4.0")
        handler = self.antigravity_config()["mezmo-governance"]["PreToolUse"][0]["hooks"][0]
        self.write(".gemini/config/hooks.json", {"mezmo-governance": {"PreToolUse": [handler]}})
        result = self.doctor()
        self.assertIn("not installed", self.lines(result, "Antigravity CLI")[-1])

    def test_gemini_needs_the_guard(self) -> None:
        self.agent("gemini", "0.64.0")
        self.copy_hook("gemini-cli", ".gemini/hooks/mezmo_governance_hook.py")
        command = 'python3 "${MEZMO_GOVERNANCE_HOOK:-$HOME/.gemini/hooks/mezmo_governance_hook.py}"'
        for suffix, status in ((" || { echo 'failed' >&2; exit 2; }", "ok"), ("", "FAIL")):
            with self.subTest(guard=bool(suffix)):
                self.write(".gemini/settings.json", {"hooks": {"BeforeTool": [{"matcher": "*", "hooks": [{"type": "command", "command": command + suffix}]}]}})
                result = self.doctor()
                self.assertTrue(self.lines(result, "Gemini CLI hook")[1].startswith(status), result.stdout)
                self.assertStatus(result, "Gemini CLI smoke", "ok")

    def test_gemini_with_the_old_guard_warns(self) -> None:
        self.agent("gemini", "0.64.0")
        self.copy_hook("gemini-cli", ".gemini/hooks/mezmo_governance_hook.py")
        command = 'python3 "${MEZMO_GOVERNANCE_HOOK:-$HOME/.gemini/hooks/mezmo_governance_hook.py}"'
        self.write(".gemini/settings.json", {"hooks": {"BeforeTool": [{"matcher": "*", "hooks": [{"type": "command", "command": command + OLD_GUARD}]}]}})

        result = self.doctor()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("gemini-cli/settings.example.json", self.assertStatus(result, "Gemini CLI guard", "warn"))

    def test_a_disabled_gemini_extension_or_hook_fails(self) -> None:
        self.agent("gemini", "0.64.0")
        shutil.copytree(REPO / "gemini-cli", self.home / ".gemini/extensions/mezmo-governance")
        enablement = ".gemini/extensions/extension-enablement.json"
        # What gemini extensions disable and the /hooks panel wrote, Gemini CLI 0.64.0.
        cases = [
            ({enablement: {"mezmo-governance": {"overrides": [f"!{self.home}/*"]}}}, f"turned off in {self.home}"),
            ({enablement: {"mezmo-governance": {"overrides": [f"{self.home}/*", f"!{self.tmp}/work/*"]}}}, f"turned off in {self.tmp}/work"),
            ({".gemini/settings.json": {"hooksConfig": {"disabled": ["mezmo-governance"]}}}, "hooksConfig.disabled"),
            ({".gemini/settings.json": {"hooksConfig": {"enabled": False}}}, "hooksConfig.enabled"),
        ]
        for files, words in cases:
            with self.subTest(words=words):
                for relative in (enablement, ".gemini/settings.json"):
                    (self.home / relative).unlink(missing_ok=True)
                for relative, content in files.items():
                    self.write(relative, content)

                result = self.doctor()

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertTrue(any(line.startswith("FAIL") and words in line for line in self.lines(result, "Gemini CLI hook")), result.stdout)
        self.write(enablement, {"mezmo-governance": {"overrides": [f"!{self.home}/*", f"{self.home}/*"]}})
        (self.home / ".gemini/settings.json").unlink()
        self.assertEqual(self.doctor().returncode, 0)

    def test_copilot_hook_file_checks(self) -> None:
        self.agent("copilot", "GitHub Copilot CLI 1.0.95.")
        self.copy_hook("copilot", ".copilot/hooks/mezmo_governance_hook.py")
        example = json.loads((REPO / "copilot/hooks.example.json").read_text())
        good = example["hooks"]["preToolUse"][0]
        cases = [
            ({"version": 1, "hooks": {"preToolUse": [good]}}, "Copilot hook", "ok"),
            ({"version": 1, "hooks": {"PreToolUse": [good]}}, "Copilot hook", "FAIL"),
            ({"version": 1, "hooks": {"preToolUse": [{**good, "bash": good["bash"].rsplit(" ", 1)[0]}]}}, "Copilot hook", "FAIL"),
            ({"version": 1, "disableAllHooks": True, "hooks": {"preToolUse": [good]}}, "Copilot hooks", "FAIL"),
        ]
        for config, subject, status in cases:
            with self.subTest(config=config):
                self.write(".copilot/hooks/mezmo-governance.json", config)
                result = self.doctor()
                self.assertTrue(any(line.startswith(status) for line in self.lines(result, subject)), result.stdout)
        self.write(".copilot/hooks/mezmo-governance.json", {"version": 1, "hooks": {"preToolUse": [good]}})
        self.assertStatus(self.doctor(), "Copilot smoke", "ok")

    def test_copilot_home_moves_the_hook_folder(self) -> None:
        self.agent("copilot", "GitHub Copilot CLI 1.0.95.")
        example = json.loads((REPO / "copilot/hooks.example.json").read_text())
        self.write("copilot-home/hooks/mezmo-governance.json", example)

        result = self.doctor(COPILOT_HOME=str(self.home / "copilot-home"))

        self.assertIn("copilot-home", self.assertStatus(result, "Copilot hook", "ok"))

    def install_goose(self, root: Path, hooks: dict | None = None) -> None:
        folder = root / ".agents/plugins/mezmo-governance"
        (folder / "hooks").mkdir(parents=True)
        shutil.copy(REPO / "goose/plugin.json", folder)
        shutil.copy(REPO / "goose/mezmo_governance_hook.py", folder)
        text = json.dumps(hooks) if hooks else (REPO / "goose/hooks/hooks.json").read_text()
        (folder / "hooks/hooks.json").write_text(text)

    def test_goose_plugin_passes_as_shipped(self) -> None:
        self.agent("goose", " 1.54.0")
        self.install_goose(self.home)

        result = self.doctor()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("on_failure", self.lines(result, "Goose plugin")[1])
        self.assertStatus(result, "Goose smoke", "ok")
        self.assertEqual(self.lines(result, "Goose hook copy"), [])

    def test_a_hook_copy_from_an_older_release_warns(self) -> None:
        # An older copy still denies when Mezmo is down, but lacks later fixes, such as the delegate deny.
        self.agent("goose", " 1.54.0")
        self.install_goose(self.home)
        hook = self.home / ".agents/plugins/mezmo-governance/mezmo_governance_hook.py"
        hook.write_text(hook.read_text() + "\n# an older release\n")

        result = self.doctor()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertStatus(result, "Goose smoke", "ok")
        line = self.assertStatus(result, "Goose hook copy", "warn")
        self.assertIn("differs from this repo", line)
        self.assertIn("./mezmo-governance install goose", line)

    def test_goose_without_on_failure_or_with_a_star_matcher_fails(self) -> None:
        self.agent("goose", " 1.54.0")
        shipped = json.loads((REPO / "goose/hooks/hooks.json").read_text())
        handler = dict(shipped["hooks"]["PreToolUse"][0]["hooks"][0])
        no_block = {"hooks": {"PreToolUse": [{"hooks": [{k: v for k, v in handler.items() if k != "on_failure"}]}]}}
        star = {"hooks": {"PreToolUse": [{"matcher": "*", "hooks": [handler]}]}}
        for hooks, words in ((no_block, "on_failure"), (star, 'matcher "*"')):
            with self.subTest(words=words):
                root = self.tmp / words.replace('"', "").replace(" ", "-").replace("*", "star")
                self.install_goose(root, hooks)
                result = self.doctor(GOOSE_PATH_ROOT=str(root))
                self.assertEqual(result.returncode, 1)
                self.assertTrue(any(line.startswith("FAIL") and words in line for line in self.lines(result, "Goose plugin")), result.stdout)

    def test_a_disabled_goose_plugin_fails(self) -> None:
        # Goose 1.54.0 crates/goose/src/plugins/discovery.rs: settings.json disabledPlugins, config.yaml plugins.<root>.enabled.
        self.agent("goose", " 1.54.0")
        self.install_goose(self.home)
        folder = self.home / ".agents/plugins/mezmo-governance"
        cases = [
            (".config/goose/settings.json", json.dumps({"disabledPlugins": ["mezmo-governance"]}), "disabledPlugins"),
            (".config/goose/config.yaml", f"GOOSE_MODE: auto\nplugins:\n  {folder}:\n    enabled: false\n  /other:\n    enabled: true\n", "enabled: false"),
            (".config/goose/config.yaml", f"plugins:\n  '{folder}': {{enabled: false}}\n", "enabled: false"),
        ]
        for relative, text, words in cases:
            with self.subTest(text=text):
                shutil.rmtree(self.home / ".config", ignore_errors=True)
                self.write(relative, text)

                result = self.doctor()

                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertTrue(any(line.startswith("FAIL") and words in line for line in self.lines(result, "Goose plugin")), result.stdout)
        self.write(".config/goose/config.yaml", f"plugins:\n  {folder}:\n    enabled: true\n  /other:\n    enabled: false\n")
        self.assertEqual(self.doctor().returncode, 0)

    def test_goose_incomplete_install_fails(self) -> None:
        self.agent("goose", " 1.54.0")
        (self.home / ".agents/plugins/mezmo-governance").mkdir(parents=True)

        result = self.doctor()

        self.assertIn("lacks plugin.json", self.assertStatus(result, "Goose plugin", "FAIL"))


class TypeScriptAgentTests(DoctorTestCase):
    def opencode_folder(self) -> None:
        shutil.copytree(REPO / "opencode", self.home / ".config/opencode/mezmo-governance")

    def test_opencode_2_with_options_and_a_key_file_passes(self) -> None:
        self.agent("opencode", "opencode v2.0.25")
        self.opencode_folder()
        self.write(".config/mezmo/governance-key", CANARY + "\n")
        options = {
            "MEZMO_GOVERNANCE_URL": "https://api.example.test/governance/workflow/authorize",
            "MEZMO_GOVERNANCE_TOKEN": "{file:~/.config/mezmo/governance-key}",
        }
        self.write(".config/opencode/opencode.json", {"plugins": [{"package": "./mezmo-governance", "options": options}]})

        result = self.doctor()

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertStatus(result, "OpenCode plugin", "ok")
        self.assertEqual([line[:4] for line in self.lines(result, "OpenCode options")], ["ok  ", "ok  "])

    def test_opencode_2_problems_fail(self) -> None:
        self.agent("opencode", "opencode v2.0.25")
        self.opencode_folder()
        entry_without_options = {"plugins": ["./mezmo-governance"]}
        missing_file = {"plugins": [{"package": "./mezmo-governance", "options": {
            "MEZMO_GOVERNANCE_URL": "https://api.example.test/authorize",
            "MEZMO_GOVERNANCE_TOKEN": "{file:~/.config/mezmo/absent}",
        }}]}
        for config, words in ((entry_without_options, "no MEZMO_GOVERNANCE_URL"), (missing_file, "missing or empty")):
            with self.subTest(words=words):
                self.write(".config/opencode/opencode.json", config)
                result = self.doctor()
                self.assertEqual(result.returncode, 1)
                self.assertTrue(any(line.startswith("FAIL") and words in line for line in self.lines(result, "OpenCode options")), result.stdout)

    def test_opencode_1_files_on_2_or_twice_fail(self) -> None:
        self.agent("opencode", "opencode v2.0.25")
        plugins = self.home / ".config/opencode/plugins"
        (plugins / "mezmo-governance").mkdir(parents=True)
        shutil.copy(REPO / "opencode/mezmo-governance.ts", plugins)
        shutil.copy(REPO / "opencode/mezmo-governance/client.ts", plugins / "mezmo-governance")

        result = self.doctor()
        self.assertIn("passes no options", self.assertStatus(result, "OpenCode plugin", "FAIL"))

        self.opencode_folder()
        options = {"MEZMO_GOVERNANCE_URL": "https://api.example.test/a", "MEZMO_GOVERNANCE_TOKEN": "k" * 8}
        self.write(".config/opencode/opencode.json", {"plugins": [{"package": "./mezmo-governance", "options": options}]})
        result = self.doctor()
        self.assertTrue(any("loaded twice" in line for line in self.lines(result, "OpenCode plugin")), result.stdout)

    def test_opencode_1_files_pass_on_1_and_a_stale_client_warns(self) -> None:
        self.agent("opencode", "1.19.0")
        plugins = self.home / ".config/opencode/plugins"
        (plugins / "mezmo-governance").mkdir(parents=True)
        shutil.copy(REPO / "opencode/mezmo-governance.ts", plugins)
        shutil.copy(REPO / "opencode/mezmo-governance/client.ts", plugins / "mezmo-governance")
        shutil.copy(REPO / "opencode/mezmo-governance/client.ts", plugins)

        result = self.doctor()

        statuses = [line[:4] for line in self.lines(result, "OpenCode plugin")]
        self.assertEqual(statuses, ["ok  ", "warn"])
        self.assertTrue(self.lines(result, "OpenCode")[0].startswith("ok    OpenCode: 1.19.0"))

    def test_pi_install_checks(self) -> None:
        self.agent("pi", "1.3.0")
        folder = self.home / ".pi/agent/extensions/mezmo-governance"
        folder.mkdir(parents=True)
        for name in ("mezmo-governance.ts", "client.ts", "package.json"):
            shutil.copy(REPO / "pi" / name, folder)

        result = self.doctor()
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertStatus(result, "Pi extension", "ok")

        shutil.copy(REPO / "pi/client.ts", folder.parent)
        (folder / "client.ts").unlink()
        result = self.doctor()
        self.assertEqual([line[:4] for line in self.lines(result, "Pi extension")], ["FAIL", "warn"])

    def test_an_old_pi_warns(self) -> None:
        self.agent("pi", "1.2.0")

        self.assertIn("tested 1.3.0", self.lines(self.doctor(), "Pi")[0])


class ConfigFolderTests(DoctorTestCase):
    def test_each_override_moves_the_folder_the_doctor_reads(self) -> None:
        # A hook written under the moved folder must be found; HOME holds nothing.
        guarded = 'python3 "{}" || exit 2'
        cases = [
            ("claude", "2.1.300", "CLAUDE_CONFIG_DIR", "settings.json", "Claude Code settings",
             lambda hook: {"hooks": {"PreToolUse": [{"hooks": [{**CLAUDE_SETTINGS_HOOK, "command": f'python3 "{hook}"'}]}]}}),
            ("codex", "0.162.1", "CODEX_HOME", "hooks.json", "Codex hook",
             lambda hook: {"hooks": {"PreToolUse": [{"hooks": [{"command": guarded.format(hook)}]}]}}),
            ("gemini", "0.64.0", "GEMINI_CLI_HOME", ".gemini/settings.json", "Gemini CLI hook",
             lambda hook: {"hooks": {"BeforeTool": [{"hooks": [{"command": guarded.format(hook)}]}]}}),
            ("droid", "0.244.0", "FACTORY_HOME_OVERRIDE", ".factory/hooks.json", "Droid hook",
             lambda hook: {"PreToolUse": [{"hooks": [{"command": guarded.format(hook)}]}]}),
        ]
        for binary, version, variable, relative, subject, config in cases:
            with self.subTest(variable=variable):
                folder = self.tmp / variable.lower()
                hook = folder / "mezmo_governance_hook.py"
                hook.parent.mkdir(parents=True, exist_ok=True)
                shutil.copy(REPO / "codex" / "mezmo_governance_hook.py", hook)
                (folder / relative).parent.mkdir(parents=True, exist_ok=True)
                (folder / relative).write_text(json.dumps(config(hook)))
                self.agent(binary, version)

                result = self.doctor(**{variable: str(folder)})

                self.assertTrue(self.lines(result, subject)[0].startswith("ok"), result.stdout)

    def test_xdg_config_home_moves_opencode_and_pi_dir_moves_pi(self) -> None:
        xdg = self.tmp / "xdg"
        key = xdg / "mezmo" / "governance-key"
        key.parent.mkdir(parents=True)
        key.write_text("not-the-canary\n")
        shutil.copytree(REPO / "opencode", xdg / "opencode" / "mezmo-governance")
        options = {"MEZMO_GOVERNANCE_URL": self.stub.url, "MEZMO_GOVERNANCE_TOKEN": f"{{file:{key}}}"}
        (xdg / "opencode" / "opencode.json").write_text(
            json.dumps({"plugins": [{"package": "./mezmo-governance", "options": options}]})
        )
        self.agent("opencode", "2.0.25")
        pi = self.tmp / "pi-agent"
        pi.mkdir()
        (pi / "settings.json").write_text(json.dumps({"packages": ["npm:other", str(REPO / "pi")]}))
        self.agent("pi", "1.3.0")

        result = self.doctor(XDG_CONFIG_HOME=str(xdg), PI_CODING_AGENT_DIR=str(pi))

        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertTrue(self.lines(result, "OpenCode plugin")[0].startswith("ok"), result.stdout)
        self.assertIn(f"installed from {REPO / 'pi'}", self.assertStatus(result, "Pi extension", "ok"))

    def test_a_gemini_extension_counts_as_installed(self) -> None:
        shutil.copytree(REPO / "gemini-cli", self.home / ".gemini/extensions/mezmo-governance")
        self.agent("gemini", "0.64.0")

        result = self.doctor()

        self.assertEqual(result.returncode, 0, result.stdout)
        hook = self.lines(result, "Gemini CLI hook")
        self.assertIn("~/.gemini/extensions/mezmo-governance", hook[0])
        self.assertIn("fails closed", hook[1])
        self.assertStatus(result, "Gemini CLI smoke", "ok")


class SmokeTests(DoctorTestCase):
    def install(self, script: str | None = None) -> Path:
        self.agent("claude", "2.1.300 (Claude Code)")
        self.write(".claude/settings.json", {"hooks": {"PreToolUse": [{"hooks": [CLAUDE_SETTINGS_HOOK]}]}})
        path = self.home / ".claude/hooks/mezmo_governance_hook.py"
        if script is not None:
            self.write(".claude/hooks/mezmo_governance_hook.py", script)
        return path

    def test_a_hook_that_lets_the_call_through_fails(self) -> None:
        self.install("import sys; sys.stdin.read()\n")

        result = self.doctor()

        self.assertEqual(result.returncode, 1)
        self.assertIn("did not deny", self.assertStatus(result, "Claude Code smoke", "FAIL"))

    def test_a_missing_hook_script_fails(self) -> None:
        self.install()

        result = self.doctor()

        self.assertIn("is missing", self.assertStatus(result, "Claude Code smoke", "FAIL"))

    def test_the_smoke_test_never_sends_the_real_key_or_url(self) -> None:
        seen = self.tmp / "seen.json"
        self.install(
            "import json, os, sys\n"
            "sys.stdin.read()\n"
            f"open({str(seen)!r}, 'w').write(json.dumps([os.environ['MEZMO_GOVERNANCE_URL'],"
            " os.environ['MEZMO_GOVERNANCE_TOKEN']]))\n"
            "sys.exit(2)\n"
        )

        result = self.doctor()

        self.assertStatus(result, "Claude Code smoke", "ok")
        self.assertEqual(
            json.loads(seen.read_text()),
            ["http://127.0.0.1:9/governance/workflow/authorize", "doctor-smoke"],
        )
        self.assertEqual([request["method"] for request in self.stub.requests], ["GET", "GET"])

    def test_an_old_python_fails(self) -> None:
        self.install()
        (self.bin / "python3").unlink()
        (self.bin / "python3").write_text("#!/bin/sh\necho 3.8.10\n")
        (self.bin / "python3").chmod(0o755)

        result = self.doctor()

        self.assertIn("older than 3.9", self.assertStatus(result, "python3", "FAIL"))


class SourceTests(unittest.TestCase):
    def test_the_doctor_parses_as_python_3_9(self) -> None:
        ast.parse(DOCTOR.read_text(encoding="utf-8"), feature_version=(3, 9))

    def test_tested_versions_match_the_readmes(self) -> None:
        sys.path.insert(0, str(DOCTOR.parent))
        self.addCleanup(sys.path.remove, str(DOCTOR.parent))
        import doctor

        readme = (REPO / "README.md").read_text(encoding="utf-8")
        for agent in doctor.AGENTS:
            for version in agent.tested:
                with self.subTest(agent=agent.name, version=version):
                    self.assertIn(version, readme)
            self.assertTrue((REPO / agent.readme / "README.md").is_file())
        claude = (REPO / "claude-code" / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"Claude Code v{doctor.AGENTS[0].minimum} or later", claude)


class AntigravityProblemTests(unittest.TestCase):
    def test_each_file_antigravity_drops_is_named_and_each_it_keeps_is_not(self) -> None:
        # Each kind of drop was seen live in agy 1.4.0 (agy -p /hooks lists none of the file's hooks);
        # the other cases follow from Go's encoding/json, which matches keys in any case and takes null as unset.
        sys.path.insert(0, str(DOCTOR.parent))
        self.addCleanup(sys.path.remove, str(DOCTOR.parent))
        import doctor

        def group(**handler: Any) -> dict:
            return {"x": {"PreToolUse": [{"matcher": "*", "hooks": [{"command": "other-hook", **handler}]}]}}

        dropped = [
            ([], "not a JSON object"),
            ({"x": "on"}, "x is not an object"),
            ({"x": {"enabled": "false"}}, '"enabled"'),
            ({"x": {"Enabled": 0}}, '"enabled"'),
            ({"x": {"PreToolUse": {}}}, "PreToolUse is not a list"),
            ({"x": {"stop": "other-hook"}}, "Stop is not a list"),
            ({"x": {"PreToolUse": ["other-hook"]}}, "PreToolUse entry 0 is not an object"),
            ({"x": {"PreToolUse": [{"matcher": 1, "hooks": []}]}}, "matcher"),
            ({"x": {"PreToolUse": [{"hooks": {}}]}}, "PreToolUse entry 0 hooks is not a list"),
            ({"x": {"PreToolUse": [{"hooks": ["other-hook"]}]}}, "PreToolUse entry 0 hook 0 is not an object"),
            (group(timeout=1.5), "timeout"),
            (group(timeout="10"), "timeout"),
            (group(timeout=True), "timeout"),
            (group(command=["other-hook"]), "command"),
            (group(type="Command"), "type"),
            (group(type="prompt"), "prompt"),
            ({"x": {"PreInvocation": [{"command": "other-hook", "timeout": 2.5}]}}, "PreInvocation entry 0 has a timeout"),
        ]
        for config, words in dropped:
            with self.subTest(config=config):
                problem = doctor.antigravity_problem(config)
                self.assertIsNotNone(problem)
                self.assertIn(words, problem)
        kept = [
            None,
            {},
            {"x": None},
            {"x": {"PreToolUse": None, "enabled": None}},
            {"x": {"Notification": "other-hook", "PreToolUse": [{"command": 5}]}},
            {"x": {"PreToolUse": [{"matcher": "(", "hooks": [{"command": "other-hook"}]}]}},
            {"x": {"PreToolUse": [None, {"hooks": [None]}]}},
            group(type="", timeout=10),
            group(type="command", timeout=None),
        ]
        for config in kept:
            with self.subTest(config=config):
                self.assertIsNone(doctor.antigravity_problem(config))


if __name__ == "__main__":
    unittest.main()
