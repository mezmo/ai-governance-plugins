"""./mezmo-governance, run as a user runs it: temp config folders, fake agents on PATH.

The fake claude, gemini and pi write what the real CLIs wrote in a temp
folder on 2026-10-15 (Claude Code 2.1.300, Gemini CLI 0.64.0, Pi 1.2.0),
including what each leaves behind after an uninstall.
"""

from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import unittest
from http.server import ThreadingHTTPServer
from pathlib import Path
from unittest import mock
from urllib.request import ProxyHandler, Request, build_opener

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
WRAPPER = REPO / "mezmo-governance"
CANARY = "canary-key-5d1c8e40"
ADMIN = "admin-key-9b27f6d3"
sys.path.insert(0, str(TOOLS))
import mock_governance  # noqa: E402
AGENTS = ("claude-code", "codex", "cursor", "gemini-cli", "copilot", "goose", "opencode", "pi")
VERSIONS = {
    "claude": "2.1.300 (Claude Code)",
    "codex": "codex-cli 0.162.1",
    "cursor-agent": "2026.10.14-abc1234",
    "gemini": "0.64.0",
    "copilot": "GitHub Copilot CLI 1.0.95",
    "goose": "1.54.0",
    "opencode": "2.0.25",
    "pi": "1.3.0",
}

FAKE_CLI = r'''
import json, os, shutil, sys
from pathlib import Path

args = sys.argv[1:]
name = Path(sys.argv[0]).name
if args == ["--version"]:
    print(VERSION)
    sys.exit(0)
with open(os.environ["FAKE_CLI_LOG"], "a") as log:
    log.write(json.dumps([name] + args) + "\n")
# A real agent CLI can stop to ask a question; the installer gives it nothing to read the answer from.
if not os.path.samestat(os.fstat(0), os.stat(os.devnull)):
    sys.exit(f"fake {name}: stdin is not /dev/null")


def load(path):
    return json.loads(path.read_text()) if path.is_file() else {}


def save(path, data):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(data, indent=2))


if name == "claude":
    root = Path(os.environ["CLAUDE_CONFIG_DIR"])
    settings, registry = root / "settings.json", root / "plugins" / "installed_plugins.json"
    known = root / "plugins" / "known_marketplaces.json"
    data, plugins, markets = load(settings), load(registry), load(known)
    plugins.setdefault("version", 2)
    plugins.setdefault("plugins", {})
    if args[:2] == ["plugin", "install"]:
        if "--marketplace" in args:
            source = args[args.index("--marketplace") + 1]
            markets["mezmo"] = {"source": {"source": "directory", "path": source}}
            data.setdefault("extraKnownMarketplaces", {})["mezmo"] = markets["mezmo"]
        plugin = args[2].split("@")[0]
        market = Path(markets["mezmo"]["source"]["path"])
        listed = json.loads((market / ".claude-plugin" / "marketplace.json").read_text())["plugins"]
        if plugin not in [entry["name"] for entry in listed]:
            sys.exit(f"fake claude: plugin {plugin} not found in marketplace mezmo")
        folder = root / "plugins" / "cache" / "mezmo" / plugin / "0.2.0"
        shutil.copytree(market / "claude-code", folder, dirs_exist_ok=True)
        plugins["plugins"][f"{plugin}@mezmo"] = [{"scope": "user", "installPath": str(folder)}]
        data.setdefault("enabledPlugins", {})[f"{plugin}@mezmo"] = True
    elif args[:2] == ["plugin", "uninstall"]:
        plugins["plugins"].pop(args[2])
        data.setdefault("enabledPlugins", {}).pop(args[2], None)
    elif args[:3] == ["plugin", "marketplace", "remove"]:
        markets.pop(args[3])
        data.setdefault("extraKnownMarketplaces", {}).pop(args[3], None)
    else:
        sys.exit(f"fake claude: {args}")
    save(settings, data)
    save(registry, plugins)
    save(known, markets)
elif name == "gemini":
    root = Path(os.environ.get("GEMINI_CLI_HOME") or os.environ["HOME"]) / ".gemini"
    target = root / "extensions" / "mezmo-governance"
    if args[:2] == ["extensions", "install"]:
        # Gemini asks to trust the folder and waits, even on /dev/null. On a "y" it trusts the
        # folder, and --consent then trusts the current folder too, unasked.
        if os.environ.get("GEMINI_CLI_TRUST_WORKSPACE") != "true":
            sys.exit("fake gemini: waits for an answer to 'Do you trust the files in this folder?'")
        shutil.copytree(args[2], target)
    elif args[:2] == ["extensions", "uninstall"]:
        shutil.rmtree(target)
    else:
        sys.exit(f"fake gemini: {args}")
elif name == "pi":
    root = Path(os.environ.get("PI_CODING_AGENT_DIR") or Path(os.environ["HOME"]) / ".pi" / "agent")
    settings = load(root / "settings.json")
    relative = os.path.relpath(args[1], root)
    packages = settings.get("packages", [])
    if args[0] == "install" and relative not in packages:
        settings["packages"] = packages + [relative]
    elif args[0] == "remove":
        settings["packages"] = [entry for entry in packages if entry != relative]
    save(root / "settings.json", settings)
'''

# A config each agent already had, with an entry of the user's own.
ORIGINALS = {
    "claude-code": {"claude/settings.json": {"theme": "dark"}},
    "codex": {"codex/hooks.json": {"hooks": {"PreToolUse": [{"matcher": "Bash", "hooks": [{"type": "command", "command": "other-hook"}]}]}}},
    "cursor": {"home/.cursor/hooks.json": {"version": 1, "hooks": {"preToolUse": [{"command": "other-hook"}]}}},
    "gemini-cli": {"gemini/.gemini/settings.json": {"ui": {"theme": "x"}}},
    "copilot": {"copilot/hooks/other.json": {"version": 1, "hooks": {}}},
    "goose": {"goose/.agents/plugins/other/plugin.json": {"name": "other"}},
    "opencode": {"xdg/opencode/opencode.json": {"plugins": ["other-plugin"], "theme": "x"}},
    "pi": {"pi/settings.json": {"packages": ["npm:other"]}},
}
# What the real CLI leaves behind after uninstall, beyond the user's own files.
LEFTOVERS = {
    "claude-code": ("claude/plugins/installed_plugins.json", "claude/plugins/known_marketplaces.json", "claude/plugins/cache/"),
}


class CliTestCase(unittest.TestCase):
    def setUp(self) -> None:
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp)
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        (self.tmp / "home").mkdir()
        (self.bin / "python3").symlink_to(sys.executable)
        # smoke runs the OpenCode and Pi plugins with node, as make test-pi does.
        (self.bin / "node").symlink_to(shutil.which("node") or "node-is-not-on-PATH")
        for binary, version in VERSIONS.items():
            self.fake(binary, version)
        self.log = self.tmp / "cli.log"
        # Every agent's folder override points into the temp folder.
        self.env = {
            key: value
            for key, value in os.environ.items()
            if not key.startswith(("MEZMO_GOVERNANCE_", "CLAUDE_PLUGIN_OPTION_"))
            and key.lower() not in {"http_proxy", "https_proxy", "all_proxy"}
        }
        self.env.update(
            {
                "HOME": str(self.tmp / "home"),
                "PATH": f"{self.bin}:/usr/bin:/bin",
                "NO_PROXY": "*",
                "FAKE_CLI_LOG": str(self.log),
                "CLAUDE_CONFIG_DIR": str(self.tmp / "claude"),
                "CODEX_HOME": str(self.tmp / "codex"),
                "GEMINI_CLI_HOME": str(self.tmp / "gemini"),
                "COPILOT_HOME": str(self.tmp / "copilot"),
                "GOOSE_PATH_ROOT": str(self.tmp / "goose"),
                "XDG_CONFIG_HOME": str(self.tmp / "xdg"),
                "PI_CODING_AGENT_DIR": str(self.tmp / "pi"),
                "MEZMO_GOVERNANCE_TOKEN": CANARY,
            }
        )

    def fake(self, binary: str, version: str) -> None:
        path = self.bin / binary
        path.write_text(f"#!{sys.executable}\nVERSION = {version!r}\n{FAKE_CLI}")
        path.chmod(0o755)

    def cli(self, *args: str, stdin: str = "y\n", **env: str) -> subprocess.CompletedProcess:
        result = subprocess.run(
            [str(WRAPPER), *args],
            input=stdin,
            env={**self.env, **env},
            cwd=self.tmp,
            capture_output=True,
            text=True,
            timeout=120,
            check=False,
        )
        self.assertNotIn(CANARY, result.stdout + result.stderr)
        return result

    def write(self, files: dict) -> None:
        for relative, content in files.items():
            path = self.tmp / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(json.dumps(content, indent=2))

    def files(self) -> dict:
        found = {}
        for path in self.tmp.rglob("*"):
            relative = path.relative_to(self.tmp).as_posix()
            # Apple's Python 3.9 caches bytecode under ~/Library.
            skipped = relative.startswith(("bin/", "home/Library/")) or relative == "cli.log"
            if path.is_file() and not skipped:
                found[relative] = path.read_bytes()
        return found

    def status(self, agent: str) -> str:
        lines = [line for line in self.cli("agents").stdout.splitlines() if line.startswith(agent + " ")]
        self.assertEqual(len(lines), 1)
        return lines[0]


class InstallTests(CliTestCase):
    def test_each_agent_installs_once_and_uninstalls_to_the_original(self) -> None:
        for agent in AGENTS:
            with self.subTest(agent=agent):
                shutil.rmtree(self.tmp / "home")
                for folder in ("claude", "codex", "gemini", "copilot", "goose", "xdg", "pi"):
                    shutil.rmtree(self.tmp / folder, ignore_errors=True)
                (self.tmp / "home").mkdir()
                self.write(ORIGINALS[agent])
                original = self.files()

                first = self.cli("install", agent)
                self.assertEqual(first.returncode, 0, first.stdout + first.stderr)
                self.assertIn("installed: yes", self.status(agent))
                installed = self.files()

                again = self.cli("install", agent)
                self.assertEqual(again.returncode, 0, again.stdout)
                self.assertIn(f"{agent}: already installed, nothing to change", again.stdout)
                self.assertEqual(self.files(), installed)

                gone = self.cli("uninstall", agent)
                self.assertEqual(gone.returncode, 0, gone.stdout + gone.stderr)
                self.assertIn("installed: no", self.status(agent))
                after = self.files()
                backups = {name for name in after if ".mezmo-backup-" in name}
                for name, content in original.items():
                    self.assertIn(name, after, gone.stdout)
                    self.assertEqual(json.loads(after[name]), json.loads(content), f"{name}:\n{gone.stdout}")
                extra = {
                    name for name in set(after) - set(original) - backups
                    if not name.startswith(LEFTOVERS.get(agent, ()))
                }
                self.assertEqual(extra, set(), gone.stdout)
                # The install's backup of each user config equals the original, and its path was printed.
                (name,) = original
                copies = sorted(b for b in backups if b.startswith(name + ".mezmo-backup-"))
                edited = {"claude-code", "codex", "cursor", "opencode", "pi"}
                self.assertEqual(bool(copies), agent in edited, sorted(backups))
                if copies:
                    self.assertEqual(after[copies[0]], original[name])
                    self.assertRegex(first.stdout, rf"backup: \S*{Path(copies[0]).name}\n")

    def test_install_and_uninstall_keep_the_users_json_formatting(self) -> None:
        # Each user writes a different indent and line ending, non-ASCII text and no final newline.
        texts = {}
        for agent, indent, newline in (("codex", "    ", "\n"), ("cursor", "\t", "\n"), ("opencode", " ", "\r\n")):
            ((name, content),) = ORIGINALS[agent].items()
            texts[name] = json.dumps({"note": "café", **content}, indent=indent, ensure_ascii=False).replace("\n", newline)
            (self.tmp / name).parent.mkdir(parents=True, exist_ok=True)
            (self.tmp / name).write_bytes(texts[name].encode("utf-8"))
        result = self.cli("install", "codex", "cursor", "opencode")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name, text in texts.items():
            installed = (self.tmp / name).read_bytes().decode("utf-8")
            self.assertTrue(installed.startswith(text.split("\n")[0] + "\n" + text.split("\n")[1] + "\n"), installed)
            self.assertFalse(installed.endswith("\n"), name)
        result = self.cli("uninstall", "codex", "cursor", "opencode")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for name, text in texts.items():
            self.assertEqual((self.tmp / name).read_bytes().decode("utf-8"), text, name)

    def test_a_rewritten_config_keeps_its_symlink_and_its_mode(self) -> None:
        # A dotfiles user links hooks.json from a repository and keeps opencode.json, which names the key file, 0600.
        target = self.tmp / "dotfiles/hooks.json"
        self.write({"dotfiles/hooks.json": ORIGINALS["codex"]["codex/hooks.json"], **ORIGINALS["opencode"]})
        (self.tmp / "codex").mkdir()
        (self.tmp / "codex/hooks.json").symlink_to(target)
        opencode = self.tmp / "xdg/opencode/opencode.json"
        opencode.chmod(0o600)
        for verb in ("install", "uninstall"):
            with self.subTest(verb=verb):
                result = self.cli(verb, "codex", "opencode")
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                self.assertTrue((self.tmp / "codex/hooks.json").is_symlink())
                self.assertEqual(os.readlink(self.tmp / "codex/hooks.json"), str(target))
                self.assertEqual(oct(opencode.stat().st_mode & 0o777), oct(0o600))
        self.assertEqual(json.loads(target.read_text()), ORIGINALS["codex"]["codex/hooks.json"])
        self.assertFalse(list(self.tmp.rglob("*.mezmo-partial")))

    def test_codex_uninstall_keeps_the_place_of_later_hook_groups(self) -> None:
        # Codex keys hook trust by group index (hooks.state."<file>:pre_tool_use:<group>:<handler>").
        self.write(ORIGINALS["codex"])
        self.assertEqual(self.cli("install", "codex").returncode, 0)
        path = self.tmp / "codex/hooks.json"
        later = {"matcher": "Edit", "hooks": [{"type": "command", "command": "later-hook"}]}
        config = json.loads(path.read_text())
        config["hooks"]["PreToolUse"].append(later)
        path.write_text(json.dumps(config, indent=2))
        result = self.cli("uninstall", "codex")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        hooks = json.loads(path.read_text())["hooks"]
        self.assertEqual(hooks["PreToolUse"], ORIGINALS["codex"]["codex/hooks.json"]["hooks"]["PreToolUse"] + [{"hooks": []}, later])
        self.assertNotIn("PostToolUse", hooks)

    def test_codex_uninstall_refuses_to_move_a_hand_merged_users_handler(self) -> None:
        # The user moved the Mezmo handler into their own group, ahead of their handler:
        # removing it would move other-hook from handler 1 to 0 and Codex would drop its trust.
        self.write({**ORIGINALS["codex"], **ORIGINALS["cursor"]})
        self.assertEqual(self.cli("install", "codex", "cursor").returncode, 0)
        path = self.tmp / "codex/hooks.json"
        config = json.loads(path.read_text())
        mezmo = config["hooks"]["PreToolUse"].pop()["hooks"][0]
        config["hooks"]["PreToolUse"][0]["hooks"].insert(0, mezmo)
        path.write_text(json.dumps(config, indent=2))
        before = {name: content for name, content in self.files().items() if name.startswith("codex/")}
        result = self.cli("uninstall", "codex", "cursor")
        self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
        self.assertIn("FAIL  codex:", result.stdout)
        self.assertIn("PreToolUse group 0 handler 1 (other-hook)", result.stdout)
        self.assertIn("/hooks", result.stdout)
        self.assertEqual({name: content for name, content in self.files().items() if name.startswith("codex/")}, before)
        self.assertIn("installed: yes", self.status("codex"))
        # A refused agent does not stop the others in the same run.
        self.assertIn("installed: no", self.status("cursor"))

    def test_the_older_mezmo_governance_plugin_is_replaced_not_doubled(self) -> None:
        folder = self.tmp / "claude/plugins/cache/mezmo/mezmo-governance/0.2.0"
        self.write(
            {
                "claude/plugins/installed_plugins.json": {"version": 2, "plugins": {"mezmo-governance@mezmo": [{"scope": "user", "installPath": str(folder)}]}},
                "claude/plugins/known_marketplaces.json": {"mezmo": {"source": {"source": "directory", "path": str(REPO)}}},
                "claude/settings.json": {"enabledPlugins": {"mezmo-governance@mezmo": True}},
            }
        )

        result = self.cli("install", "claude-code")

        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        registry = json.loads((self.tmp / "claude/plugins/installed_plugins.json").read_text())
        self.assertEqual(list(registry["plugins"]), ["mezmo@mezmo"])
        self.assertIn("installed: yes", self.status("claude-code"))

    def test_the_installed_files_are_what_each_readme_installs(self) -> None:
        result = self.cli("install", "--all")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        codex = json.loads((self.tmp / "codex/hooks.json").read_text())
        command = codex["hooks"]["PreToolUse"][0]["hooks"][0]["command"]
        self.assertIn(f'{self.tmp}/codex/hooks/mezmo_governance_hook.py', command)
        self.assertTrue(command.endswith("exit 2; }"), command)
        self.assertTrue((self.tmp / "codex/hooks/mezmo_governance_hook.py").is_file())
        cursor = json.loads((self.tmp / "home/.cursor/hooks.json").read_text())
        self.assertEqual(cursor, json.loads((REPO / "cursor/hooks.example.json").read_text()))
        copilot = (self.tmp / "copilot/hooks/mezmo-governance.json").read_text()
        self.assertIn(f"{self.tmp}/copilot/hooks/mezmo_governance_hook.py", copilot)
        self.assertNotIn("$HOME", copilot)
        opencode = json.loads((self.tmp / "xdg/opencode/opencode.json").read_text())
        options = opencode["plugins"][0]["options"]
        self.assertEqual(options["MEZMO_GOVERNANCE_TOKEN"], f"{{file:{self.tmp}/xdg/mezmo/governance-key}}")
        self.assertTrue((self.tmp / "xdg/opencode/mezmo-governance/index.ts").is_file())
        self.assertFalse((self.tmp / "xdg/opencode/mezmo-governance/test").exists())
        self.assertFalse((self.tmp / "xdg/mezmo/governance-key").exists())
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertIn(["claude", "plugin", "install", "mezmo", "--marketplace", str(REPO), "-s", "user"], calls)
        self.assertIn(["gemini", "extensions", "install", str(REPO / "gemini-cli"), "--consent", "--skip-settings"], calls)
        self.assertIn(["pi", "install", str(REPO / "pi")], calls)
        self.assertIn("LangChain: langchain/README.md", result.stdout)
        self.assertIn("Mastra: mastra/README.md", result.stdout)
        self.assertIn("/hooks", result.stdout)

    def test_gemini_install_asks_nothing_and_trusts_no_folder(self) -> None:
        # A user at a terminal would answer "y", and Gemini would then trust this folder too.
        result = self.cli("install", "gemini-cli", stdin="y\n")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertTrue((self.tmp / "gemini/.gemini/extensions/mezmo-governance/hooks/hooks.json").is_file())
        self.assertFalse((self.tmp / "gemini/.gemini/trustedFolders.json").exists())
        self.assertNotIn("trust", result.stdout)

    def test_without_overrides_the_folders_are_under_home(self) -> None:
        env = {key: "" for key in ("CLAUDE_CONFIG_DIR", "CODEX_HOME", "GEMINI_CLI_HOME", "COPILOT_HOME",
                                   "GOOSE_PATH_ROOT", "XDG_CONFIG_HOME", "PI_CODING_AGENT_DIR")}
        result = self.cli("install", "codex", "copilot", "goose", "opencode", "--dry-run", **env)
        self.assertIn("~/.codex/hooks.json", result.stdout)
        self.assertIn("~/.copilot/hooks/mezmo-governance.json", result.stdout)
        self.assertIn("~/.agents/plugins/mezmo-governance/hooks/hooks.json", result.stdout)
        self.assertIn("~/.config/opencode/opencode.json", result.stdout)

    def test_dry_run_changes_nothing(self) -> None:
        for agent, files in ORIGINALS.items():
            self.write(files)
        before = self.files()
        result = self.cli("install", "--all", "--dry-run")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertIn("would add the Mezmo hook entries to", result.stdout)
        self.assertIn("would back up", result.stdout)
        self.assertEqual(self.files(), before)
        self.assertFalse(self.log.exists())

        self.cli("install", "--all")
        installed = self.files()
        result = self.cli("uninstall", "--all", "--dry-run")
        self.assertIn("would remove the Mezmo hook entries from", result.stdout)
        self.assertEqual(self.files(), installed)

    def test_install_all_picks_agents_whose_binary_is_found(self) -> None:
        for binary in ("claude", "gemini", "pi", "copilot", "goose", "opencode"):
            (self.bin / binary).unlink()
        result = self.cli("install", "--all", "--dry-run")
        planned = [line.split()[1].rstrip(":") for line in result.stdout.splitlines() if line.startswith("plan")]
        self.assertEqual(planned, ["codex", "cursor"])

    def test_a_stop_between_steps_is_finished_or_undone_by_the_next_run(self) -> None:
        sys.path.insert(0, str(TOOLS))
        self.addCleanup(sys.path.remove, str(TOOLS))
        import mezmo_governance_cli as cli

        for agent in AGENTS:
            for verb in ("install", "uninstall"):
                with mock.patch.dict(os.environ, self.env, clear=True):
                    if verb == "uninstall":
                        self.cli("install", agent)
                    steps = getattr(cli.INSTALLERS[agent], verb)()
                for stop in range(1, len(steps)):
                    with self.subTest(agent=agent, verb=verb, stop=stop):
                        if verb == "uninstall":
                            self.cli("install", agent)
                        with mock.patch.dict(os.environ, self.env, clear=True):
                            if verb == "install":
                                self.cli("uninstall", agent)
                            for step in getattr(cli.INSTALLERS[agent], verb)()[:stop]:
                                step.run()
                        rerun = self.cli(verb, agent)
                        self.assertEqual(rerun.returncode, 0, rerun.stdout + rerun.stderr)
                        want = "installed: yes" if verb == "install" else "installed: no"
                        self.assertIn(want, self.status(agent))
                self.cli("uninstall", agent)

    def test_a_manual_readme_install_counts_as_installed(self) -> None:
        # What each README's manual steps leave behind; install must not add a second hook.
        gemini = json.loads((REPO / "gemini-cli/settings.example.json").read_text())
        codex = json.loads((REPO / "codex/hooks.example.json").read_text())
        self.write({"gemini/.gemini/settings.json": gemini, "codex/hooks.json": codex})
        shutil.copytree(REPO / "pi", self.tmp / "pi/extensions/mezmo-governance")
        before = self.files()
        result = self.cli("install", "gemini-cli", "codex", "pi")
        self.assertEqual(result.returncode, 0, result.stdout)
        for agent in ("gemini-cli", "codex", "pi"):
            self.assertIn(f"ok    {agent}: already installed, nothing to change", result.stdout)
        self.assertEqual(self.files(), before)
        self.assertFalse(self.log.exists())

    def test_a_config_it_cannot_read_is_left_alone(self) -> None:
        path = self.tmp / "codex/hooks.json"
        path.parent.mkdir(parents=True)
        path.write_text("{not json")
        result = self.cli("install", "codex")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL  codex: could not parse", result.stdout)
        self.assertEqual(path.read_text(), "{not json")

    def test_opencode_install_warns_that_it_keeps_a_local_mock_url(self) -> None:
        # opencode.json keeps the URL, so a mock's URL outlives the mock.
        mock_url = "http://127.0.0.1:8787/governance/workflow/authorize"
        result = self.cli("install", "opencode", "--dry-run", MEZMO_GOVERNANCE_URL=mock_url)
        self.assertIn("warn  opencode: MEZMO_GOVERNANCE_URL http://127.0.0.1:8787/governance/workflow/authorize is a local mock", result.stdout)
        result = self.cli("install", "opencode", MEZMO_GOVERNANCE_URL=mock_url)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        doctor = self.cli("doctor", "opencode", MEZMO_GOVERNANCE_URL=mock_url)
        self.assertIn("ok    OpenCode options: MEZMO_GOVERNANCE_URL is set (local mock)", doctor.stdout)
        result = self.cli("install", "opencode", "--dry-run", MEZMO_GOVERNANCE_URL="https://api.example.test/governance/workflow/authorize")
        self.assertNotIn("warn", result.stdout)

    def test_opencode_1_is_refused(self) -> None:
        self.fake("opencode", "1.19.0")
        result = self.cli("install", "opencode")
        self.assertEqual(result.returncode, 1)
        self.assertIn("Install on 1.x", result.stdout)
        self.assertFalse((self.tmp / "xdg").exists())


class CommandTests(CliTestCase):
    def test_help_lists_every_command_and_agent(self) -> None:
        for args in ((), ("help",), ("-h",), ("--help",)):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 0)
                for word in ("agents", "install", "uninstall", "doctor", "mock", "smoke", "policy show", "policy apply", "--agent NAME|ID", "smoke [agent...] [--live]",
                             "MEZMO_GOVERNANCE_ADMIN_KEY", *AGENTS, "CODEX_HOME"):
                    self.assertIn(word, result.stdout)

    def test_an_unknown_agent_or_command_exits_2_with_help(self) -> None:
        for args in (("install", "nope"), ("smoke", "nope"), ("frobnicate",), ("install",), ("install", "--force", "codex")):
            with self.subTest(args=args):
                result = self.cli(*args)
                self.assertEqual(result.returncode, 2)
                self.assertIn("Usage: ./mezmo-governance", result.stderr)
        self.assertIn("unknown agent: nope", self.cli("install", "nope").stderr)

    def test_agents_shows_binary_version_install_and_fail_closed(self) -> None:
        (self.bin / "goose").unlink()
        self.cli("install", "codex", "pi")
        # Trusted in /hooks; the fake codex lists no hooks, so the saved hash goes unchecked.
        (self.tmp / "codex/config.toml").write_text(f'[hooks.state."{self.tmp}/codex/hooks.json:pre_tool_use:0:0"]\ntrusted_hash = "sha256:00"\n')
        lines = self.cli("status").stdout.splitlines()
        self.assertEqual([line.split()[0] for line in lines], list(AGENTS))
        self.assertIn("codex 0.162.1", lines[1])
        self.assertIn("installed: yes", lines[1])
        self.assertIn("fails closed: yes", lines[1])
        self.assertIn("goose not found", lines[5])
        self.assertIn("installed: no", lines[5])
        self.assertIn("fails closed: n/a", lines[7])

    def test_agents_says_a_disabled_install_does_not_fail_closed(self) -> None:
        self.cli("install", "claude-code")
        settings = self.tmp / "claude/settings.json"
        settings.write_text(json.dumps({**json.loads(settings.read_text()), "enabledPlugins": {"mezmo-governance@mezmo": False}}))
        line = self.status("claude-code")
        self.assertIn("installed: yes", line)
        self.assertIn("fails closed: no", line)
        self.assertIn("problems: 1", line)

    def test_doctor_checks_only_the_named_agents(self) -> None:
        self.cli("install", "codex")
        result = self.cli("doctor", "codex", MEZMO_GOVERNANCE_URL="http://127.0.0.1:9/governance/workflow/authorize")
        self.assertIn("Codex hook", result.stdout)
        self.assertNotIn("Claude Code", result.stdout)
        self.assertIn("ok    Codex smoke", result.stdout)

    def test_mock_prints_the_export_lines(self) -> None:
        process = subprocess.Popen(
            [str(WRAPPER), "mock", "--port", "0", "--deny", "Bash"],
            env=self.env, stdout=subprocess.PIPE, text=True,
        )
        self.addCleanup(process.wait)
        self.addCleanup(process.terminate)
        lines = [process.stdout.readline() for _ in range(4)]
        process.stdout.close()
        self.assertIn("export MEZMO_GOVERNANCE_URL=http://127.0.0.1:", lines[1])
        self.assertIn("export MEZMO_GOVERNANCE_TOKEN=local-test", lines[2])
        self.assertIn("deny: ['Bash']", lines[3])


class SmokeTests(CliTestCase):
    def test_each_installed_hook_denies_one_call_and_approves_one(self) -> None:
        self.assertEqual(self.cli("install", "--all").returncode, 0)
        result = self.cli("smoke")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        for agent in AGENTS:
            lines = [line for line in result.stdout.splitlines() if line.startswith(f"ok    {agent}:")]
            self.assertEqual([("denied" in line, "approved" in line) for line in lines], [(True, False), (False, True)], agent)
        self.assertIn("ok    opencode: bash was denied by the mock (", result.stdout)
        self.assertIn("/xdg/opencode/mezmo-governance/index.ts)", result.stdout)
        self.assertIn(f"ok    pi: bash was denied by the mock ({REPO}/pi/mezmo-governance.ts)", result.stdout)

    def test_without_node_the_typescript_plugins_fail_not_pass(self) -> None:
        self.cli("install", "opencode")
        (self.bin / "node").unlink()
        result = self.cli("smoke", "opencode")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL  opencode: node is not on PATH", result.stdout)

    def test_a_hook_that_lets_every_call_through_fails(self) -> None:
        self.cli("install", "codex")
        (self.tmp / "codex/hooks/mezmo_governance_hook.py").write_text("import sys; sys.stdin.read()\n")
        result = self.cli("smoke", "codex")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL  codex: Bash should be denied", result.stdout)

    def test_a_hook_that_fails_closed_without_asking_fails(self) -> None:
        # Denying everything is not a pass: the denied call must reach the mock.
        self.cli("install", "codex")
        (self.tmp / "codex/hooks/mezmo_governance_hook.py").write_text("import sys; sys.exit(2)\n")
        result = self.cli("smoke", "codex")
        self.assertEqual(result.returncode, 1)
        self.assertIn("FAIL  codex: Bash should be denied", result.stdout)
        self.assertIn("FAIL  codex: Read should be approved", result.stdout)

    def test_smoke_with_nothing_installed_fails(self) -> None:
        result = self.cli("smoke")
        self.assertEqual(result.returncode, 1)
        self.assertIn("no installed hook", result.stdout)


class LiveSmokeTests(CliTestCase):
    """smoke --live, pointed at a mock port in place of Mezmo."""

    def setUp(self) -> None:
        super().setUp()
        # The mock denies shell tools and holds Claude Code's read tool for approval.
        server = ThreadingHTTPServer(("127.0.0.1", 0), mock_governance.make_handler(["Bash", "Shell"], ["Read"]))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.env["MEZMO_GOVERNANCE_URL"] = f"http://127.0.0.1:{server.server_port}{mock_governance.PATH}"
        self.env["MEZMO_GOVERNANCE_TIMEOUT_SECONDS"] = "1"
        self.env["MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS"] = "1"

    def test_each_call_is_reported_as_the_policy_decided(self) -> None:
        self.cli("install", "claude-code", "codex")
        result = self.cli("smoke", "--live")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("live  claude-code: Bash denied: mock policy: claude_code:Bash is denied (", result.stdout)
        self.assertIn("live  claude-code: Read pending approval (", result.stdout)
        self.assertIn("live  codex: Bash denied: mock policy: codex:Bash is denied (", result.stdout)
        # Codex has no read-only tool, so it gets the shell call only.
        self.assertNotIn("apply_patch", result.stdout)
        self.assertNotIn("mock  Mezmo stand-in", result.stdout)
        self.assertNotIn("FAIL", result.stdout)
        # The smoke calls stay out of the user's audit log.
        self.assertFalse((self.tmp / "home/.mezmo").exists())

    def test_opencode_gets_its_opencode_json_options(self) -> None:
        # OpenCode passes the plugin its options, with {file:...} read; the shell's variables lose to them.
        self.cli("install", "opencode")
        key = self.tmp / "xdg/mezmo/governance-key"
        key.parent.mkdir(parents=True, exist_ok=True)
        key.write_text("from-the-key-file\n")
        path = self.tmp / "xdg/opencode/opencode.json"
        config = json.loads(path.read_text())
        config["plugins"][0]["options"]["MEZMO_GOVERNANCE_URL"] = self.env["MEZMO_GOVERNANCE_URL"]
        path.write_text(json.dumps(config))
        result = self.cli("smoke", "--live", "opencode", MEZMO_GOVERNANCE_URL=doctor_unreachable())
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("live  opencode: shell allowed (", result.stdout)
        self.assertIn("live  opencode: read allowed (", result.stdout)
        # The mock smoke still sends the plugin to its own mock, never to the URL in opencode.json.
        config["plugins"][0]["options"]["MEZMO_GOVERNANCE_URL"] = doctor_unreachable()
        path.write_text(json.dumps(config))
        result = self.cli("smoke", "opencode")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)

    def test_a_call_that_reaches_no_decision_exits_1(self) -> None:
        self.cli("install", "codex")
        result = self.cli("smoke", "--live", MEZMO_GOVERNANCE_URL=doctor_unreachable())
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FAIL  codex: Bash reached no decision: ", result.stdout)
        self.assertIn("unavailable", result.stdout)

    def test_url_and_token_are_required(self) -> None:
        self.cli("install", "codex")
        for name in ("MEZMO_GOVERNANCE_URL", "MEZMO_GOVERNANCE_TOKEN"):
            with self.subTest(name=name):
                result = self.cli("smoke", "--live", **{name: ""})
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"FAIL  smoke --live: set {name}", result.stdout)
                self.assertNotIn("live  codex", result.stdout)

    def test_a_held_call_waits_15_seconds_unless_the_user_set_a_limit(self) -> None:
        import mezmo_governance_cli as cli

        scratch = self.tmp / "scratch"
        base = {"MEZMO_GOVERNANCE_URL": "https://example.test/governance/workflow/authorize", "MEZMO_GOVERNANCE_TOKEN": CANARY,
                "MEZMO_GOVERNANCE_AUDIT_LOG": "/somewhere/audit.jsonl"}
        for limit, want in (("", "15"), ("40", "40")):
            with self.subTest(limit=limit), mock.patch.dict(os.environ, {**base, "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": limit}):
                env = cli.live_env(scratch)
                self.assertEqual(env["MEZMO_GOVERNANCE_TIMEOUT_SECONDS"], want)
                self.assertEqual(env["MEZMO_GOVERNANCE_AUDIT_LOG"], str(scratch / "audit.jsonl"))
                self.assertEqual(env["MEZMO_GOVERNANCE_TOKEN"], CANARY)


def doctor_unreachable() -> str:
    # Nothing listens on port 9.
    return "http://127.0.0.1:9/governance/workflow/authorize"


class KeyTests(CliTestCase):
    def test_the_key_is_in_no_file_and_no_output(self) -> None:
        # self.cli asserts the canary is in no output.
        for args in (("install", "--all"), ("agents",), ("doctor",), ("smoke",), ("uninstall", "--all")):
            self.cli(*args)
            leaked = [name for name, content in self.files().items() if CANARY.encode() in content]
            self.assertEqual(leaked, [], args)


def flat(tool: dict, recourse: str, namespace: dict = None) -> dict:
    """A rule as the policy API returns it."""
    definition = {"recourse": recourse, "tool": tool, **({"namespace": namespace} if namespace else {})}
    return mock_governance.compiled(definition)


class PolicyTests(CliTestCase):
    """policy show and apply against the mock's in-memory agent and policy."""

    def setUp(self) -> None:
        super().setUp()
        self.governance = mock_governance.governance_state()
        server = ThreadingHTTPServer(("127.0.0.1", 0), mock_governance.make_handler([], [], self.governance))
        threading.Thread(target=server.serve_forever, daemon=True).start()
        self.addCleanup(server.server_close)
        self.addCleanup(server.shutdown)
        self.base = f"http://127.0.0.1:{server.server_port}/v3/governance"
        self.env["MEZMO_GOVERNANCE_ADMIN_KEY"] = ADMIN
        (self.mock_policy,) = self.governance["policies"]
        (self.seeded,) = self.mock_policy["rules"]

    def policy(self, *args: str, **env: str) -> subprocess.CompletedProcess:
        result = self.cli("policy", *args, "--url", self.base, stdin="", **env)
        self.assertNotIn(ADMIN, result.stdout + result.stderr)
        return result

    def register(self, *runtimes: str) -> None:
        for runtime in runtimes:
            self.governance["agents"].append(
                {"agent_id": mock_governance.object_id(), "name": f"{runtime} on example.test", "runtime_name": runtime,
                 "policy_id": self.mock_policy["policy_id"]}
            )

    def writes(self) -> list:
        return [(method, path) for method, path, _, _ in self.governance["calls"] if method != "GET"]

    def test_show_prints_agents_rules_default_and_coverage(self) -> None:
        self.cli("install", "claude-code", "codex")
        result = self.policy("show")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        policy_id = self.mock_policy["policy_id"]
        self.assertIn(f"agent   Claude Code on example.test  runtime claude-code  policy {policy_id}", result.stdout)
        self.assertIn(f"policy  Mock policy ({policy_id})", result.stdout)
        self.assertIn(f"  rule     allow doctor tool eq probe: - (rule_id {self.seeded['rule_id']})", result.stdout)
        self.assertIn("  default  approval_required: No rule in this policy covers this request.", result.stdout)
        self.assertIn("cover   claude-code in Mock policy: shell Bash not covered; read Read not covered", result.stdout)
        # Mezmo has one agent, so every installed agent, which sends its token, gets its policy.
        self.assertIn("cover   codex in Mock policy: shell Bash not covered\n", result.stdout)
        self.assertNotIn("cursor", result.stdout)
        self.assertEqual(self.writes(), [])

    def test_a_rule_covers_by_tool_eq_or_in_with_no_or_the_agents_namespace(self) -> None:
        self.cli("install", "claude-code")
        cases = (
            (flat({"op": "in", "value": ["Bash", "Shell"]}, "deny"), "shell Bash covered (deny"),
            (flat({"op": "eq", "value": "Bash"}, "deny", {"op": "eq", "value": "claude_code"}), "shell Bash covered (deny"),
            (flat({"op": "eq", "value": "Bash"}, "deny", {"op": "in", "value": ["codex", "claude_code"]}), "shell Bash covered (deny"),
            (flat({"op": "eq", "value": "Bash"}, "deny", {"op": "eq", "value": "codex"}), "shell Bash not covered"),
            (flat({"op": "neq", "value": "Read"}, "deny"), "shell Bash covered (deny"),
            (flat({"op": "nin", "value": ["Read"]}, "deny"), "shell Bash covered (deny"),
            (flat({"op": "neq", "value": "Bash"}, "deny"), "shell Bash not covered"),
            (flat({"op": "eq", "value": "Bash"}, "deny", {"op": "neq", "value": "claude_code"}), "shell Bash not covered"),
            (flat({"op": "eq", "value": "Bash"}, "deny", {"op": "nin", "value": ["codex"]}), "shell Bash covered (deny"),
            ({**flat({"op": "eq", "value": "Bash"}, "deny"), "definition": {"recourse": "deny", "tool": {"op": "eq", "value": "Bash"},
              "args": [{"field": "command", "op": "eq", "value": "rm"}]}}, "shell Bash not covered"),
        )
        for rule, want in cases:
            with self.subTest(rule=rule["definition"]):
                self.policy_rules([rule])
                result = self.policy("show")
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn(want, result.stdout)

    def test_coverage_is_the_most_restrictive_matching_rule(self) -> None:
        # Mezmo decides from every matching rule, deny over approval_required over allow, in no list order.
        self.cli("install", "claude-code")
        allow = flat({"op": "eq", "value": "Bash"}, "allow")
        held = flat({"op": "nin", "value": ["Read"]}, "approval_required")
        deny = flat({"op": "in", "value": ["Bash", "Shell"]}, "deny")
        for rules, want in (([allow, held], f"approval_required, rule_id {held['rule_id']}"), ([allow, held, deny], f"deny, rule_id {deny['rule_id']}")):
            with self.subTest(rules=len(rules)):
                self.policy_rules(rules)
                result = self.policy("show")
                self.assertIn(f"shell Bash covered ({want})", result.stdout)

    def policy_rules(self, rules: list) -> None:
        self.mock_policy["rules"] = [self.seeded, *rules]

    def test_apply_without_yes_prints_the_plan_and_changes_nothing(self) -> None:
        self.cli("install", "claude-code", "cursor", "copilot")
        self.register("cursor", "copilot")
        before = json.dumps(self.governance["policies"])
        result = self.policy("apply")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"policy  Mock policy ({self.mock_policy['policy_id']}) for claude-code, cursor, copilot:", result.stdout)
        self.assertIn(f"  keep     allow doctor tool eq probe: - (rule_id {self.seeded['rule_id']})", result.stdout)
        self.assertIn("  would add deny in claude_code, cursor, copilot tool in Bash, Shell, bash: Shell blocked by Mezmo policy", result.stdout)
        self.assertIn("  would add allow in claude_code, cursor, copilot tool in Read, view: -", result.stdout)
        self.assertIn("--yes", result.stdout)
        self.assertEqual(json.dumps(self.governance["policies"]), before)
        self.assertEqual(self.writes(), [])

    def test_apply_yes_writes_through_a_draft_and_keeps_every_rule(self) -> None:
        self.cli("install", "claude-code")
        kept = flat({"op": "eq", "value": "Write"}, "approval_required")
        self.policy_rules([kept])
        result = self.policy("apply", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        root = f"/v3/governance/policy/{self.mock_policy['policy_id']}"
        draft = self.governance["calls"][3][1].rsplit("/", 1)[1]
        self.assertEqual(self.writes(), [("POST", f"{root}/draft"), ("PUT", f"{root}/draft/{draft}"), ("PATCH", root)])
        self.assertIsNone(self.mock_policy["draft"])
        self.assertEqual(self.mock_policy["description"], "A stand-in for trying policy show and apply")
        rules = self.mock_policy["rules"]
        self.assertEqual([rule["rule_id"] for rule in rules[:2]], [self.seeded["rule_id"], kept["rule_id"]])
        self.assertEqual([rule["definition"] for rule in rules[:2]], [self.seeded["definition"], kept["definition"]])
        self.assertEqual(
            [rule["definition"] for rule in rules[2:]],
            [
                {"recourse": "deny", "tool": {"op": "in", "value": ["Bash"]}, "namespace": {"op": "in", "value": ["claude_code"]},
                 "code": "FORBIDDEN_TOOL", "error_message": "Shell blocked by Mezmo policy"},
                {"recourse": "allow", "tool": {"op": "in", "value": ["Read"]}, "namespace": {"op": "in", "value": ["claude_code"]}},
            ],
        )
        self.assertIn("  add      deny in claude_code tool in Bash: Shell blocked by Mezmo policy", result.stdout)
        self.assertIn("ok    Mock policy: 2 rules added, 2 kept", result.stdout)
        # A second run finds both tools covered and opens no draft.
        self.governance["calls"].clear()
        again = self.policy("apply", "--yes")
        self.assertEqual(again.returncode, 0, again.stdout)
        self.assertIn("nothing to add", again.stdout)
        self.assertEqual(self.writes(), [])

    def test_starter_rules_name_the_agents_namespaces_and_never_allow_codex_edits(self) -> None:
        # Codex's apply_patch writes files, so no allow rule may name it; each rule names only its agents' namespaces.
        self.cli("install", "claude-code", "codex")
        result = self.policy("apply", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertNotIn("apply_patch", result.stdout)
        self.assertEqual(
            [rule["definition"] for rule in self.mock_policy["rules"][1:]],
            [
                {"recourse": "deny", "tool": {"op": "in", "value": ["Bash"]}, "namespace": {"op": "in", "value": ["claude_code", "codex"]},
                 "code": "FORBIDDEN_TOOL", "error_message": "Shell blocked by Mezmo policy"},
                {"recourse": "allow", "tool": {"op": "in", "value": ["Read"]}, "namespace": {"op": "in", "value": ["claude_code"]}},
            ],
        )
        self.assertIn("  add      deny in claude_code, codex tool in Bash: Shell blocked by Mezmo policy", result.stdout)
        show = self.policy("show")
        self.assertIn("cover   codex in Mock policy: shell Bash covered (deny", show.stdout)
        self.assertNotIn("apply_patch", show.stdout)
        # The rules cover each tool only in the namespaces they name.
        self.assertNotIn("not covered", show.stdout)

    def test_a_draft_write_keeps_the_approver_and_channels_it_leaves_out(self) -> None:
        # Mezmo's draft PUT keeps an omitted approver_role_id and hitl_approval_channels, and clears an omitted description.
        self.mock_policy.update(approver_role_id="role-1", hitl_approval_channels=["channel-1"])
        root = f"/policy/{self.mock_policy['policy_id']}"
        draft = self.call("POST", f"{root}/draft", {})["data"]
        self.assertEqual((draft["approver_role_id"], draft["hitl_approval_channels"]), ("role-1", ["channel-1"]))
        written = self.call("PUT", f"{root}/draft/{draft['draft_id']}", {"name": "x", "default_action": {"recourse": "deny"}, "rules": []})["data"]
        self.assertEqual((written["approver_role_id"], written["hitl_approval_channels"], written["description"]), ("role-1", ["channel-1"], None))
        self.call("PUT", f"{root}/draft/{draft['draft_id']}", {"name": "x", "default_action": {"recourse": "deny"}, "approver_role_id": "role-2"})
        self.call("PATCH", root, {"draft_id": draft["draft_id"]})
        self.assertEqual((self.mock_policy["approver_role_id"], self.mock_policy["hitl_approval_channels"]), ("role-2", ["channel-1"]))

    def test_with_one_mezmo_agent_apply_covers_every_installed_agent(self) -> None:
        # Live setup on 2026-10-16: one Mezmo agent, runtime claude-code, and Codex sends its token.
        self.cli("install", "codex")
        result = self.policy("apply", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn(f"policy  Mock policy ({self.mock_policy['policy_id']}) for codex:", result.stdout)
        tools = [rule["definition"]["tool"]["value"] for rule in self.mock_policy["rules"][1:]]
        self.assertEqual(tools, [["Bash"]])

    def test_with_several_mezmo_agents_runtime_name_picks_unless_agent_is_given(self) -> None:
        self.cli("install", "codex")
        other = {**json.loads(json.dumps(self.mock_policy)), "policy_id": mock_governance.object_id(), "name": "Other policy"}
        self.governance["policies"].append(other)
        self.governance["agents"].append({"agent_id": mock_governance.object_id(), "name": "Cursor on example.test",
                                          "runtime_name": "cursor", "policy_id": other["policy_id"]})
        result = self.policy("show")
        self.assertIn("cover   codex: no Mezmo agent has Runtime Name codex; pass --agent", result.stdout)
        result = self.policy("apply")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FAIL  codex: no Mezmo agent has Runtime Name codex; pass --agent", result.stdout)
        claude = self.governance["agents"][0]
        for chosen, policy in ((claude["name"], "Mock policy"), (claude["agent_id"], "Mock policy"), ("Cursor on example.test", "Other policy")):
            with self.subTest(agent=chosen):
                result = self.policy("show", "--agent", chosen)
                self.assertEqual(result.returncode, 0, result.stdout)
                self.assertIn(f"cover   codex in {policy}: shell Bash not covered", result.stdout)
        result = self.policy("apply", "--agent", "Cursor on example.test", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(other["rules"]), 2)
        self.assertEqual(self.mock_policy["rules"], [self.seeded])
        result = self.policy("show", "--agent", "nobody")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FAIL  policy show: no Mezmo agent has the name or agent_id nobody", result.stdout)
        self.assertEqual(self.cli("policy", "show", "--agent").returncode, 2)

    def test_apply_skips_a_tool_a_rule_already_covers(self) -> None:
        self.cli("install", "claude-code")
        self.policy_rules([flat({"op": "eq", "value": "Bash"}, "allow", {"op": "eq", "value": "claude_code"})])
        result = self.policy("apply")
        self.assertIn("  would add allow in claude_code tool in Read: -", result.stdout)
        self.assertNotIn("would add deny", result.stdout)

    def test_apply_takes_named_agents_and_refuses_unknown_ones(self) -> None:
        self.register("codex")
        result = self.policy("apply", "codex")
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn("would add deny in codex tool in Bash", result.stdout)
        self.assertNotIn("would add allow", result.stdout)
        for args in (("apply", "nope"), ("show", "codex"), ("apply", "--force"), ("frob",), ()):
            with self.subTest(args=args):
                result = self.cli("policy", *args)
                self.assertEqual(result.returncode, 2, result.stdout)
                self.assertIn("Usage: ./mezmo-governance", result.stderr)

    def test_an_open_draft_stops_apply_and_is_left_alone(self) -> None:
        # A run stopped after the draft opened, or after it was written, leaves the draft open.
        self.cli("install", "claude-code")
        for stop in ("POST", "PUT"):
            with self.subTest(stop=stop):
                draft = self.open_draft(write=stop == "PUT")
                self.governance["calls"].clear()
                result = self.policy("apply", "--yes")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"draft {draft} is already open on this policy; finish or delete it in Mezmo", result.stdout)
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.mock_policy["draft"], draft)
                self.assertIn(draft, self.policy("show").stdout)
                # Deleting it in Mezmo lets the next run finish.
                self.call("DELETE", f"/policy/{self.mock_policy['policy_id']}/draft/{draft}")
        result = self.policy("apply", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(len(self.mock_policy["rules"]), 3)

    def test_mezmo_handing_back_an_existing_draft_stops_apply(self) -> None:
        # The policy read showed no draft, but the draft call returned one (200, not 201).
        self.cli("install", "claude-code")
        draft = self.open_draft()
        self.mock_policy["draft"] = None
        self.governance["calls"].clear()
        result = self.policy("apply", "--yes")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn(f"Mezmo handed back draft {draft}, which was already open; finish or delete it in Mezmo", result.stdout)
        self.assertEqual([method for method, _ in self.writes()], ["POST"])
        self.assertIn(draft, self.governance["drafts"])

    def test_a_failed_write_or_accept_deletes_the_draft(self) -> None:
        self.cli("install", "claude-code")
        for method, step, calls in (
            ("PUT", "write the draft", ["POST", "PUT", "DELETE"]),
            ("PATCH", "accept the draft", ["POST", "PUT", "PATCH", "DELETE"]),
        ):
            with self.subTest(method=method):
                self.governance["calls"].clear()
                self.governance["fail"] = {method: 400}
                result = self.policy("apply", "--yes")
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"FAIL  Mock policy: {step}: HTTP 400 EMOCK: mock: {method} set to fail; deleted the draft", result.stdout)
                self.assertEqual([m for m, _ in self.writes()], calls)
                self.assertEqual(self.mock_policy["rules"], [self.seeded])
                self.assertIsNone(self.mock_policy["draft"])
                self.assertEqual(self.governance["drafts"], {})

    def test_an_accept_with_no_answer_is_unknown_and_the_draft_is_deleted(self) -> None:
        self.cli("install", "claude-code")
        self.governance["fail"] = {"PATCH": 0}
        result = self.policy("apply", "--yes")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("FAIL  Mock policy: accept the draft: PATCH /v3/governance/policy/", result.stdout)
        self.assertIn("got no answer", result.stdout)
        self.assertIn("its result is unknown", result.stdout)
        self.assertIn("deleted the draft", result.stdout)
        self.assertIn("policy show", result.stdout)
        self.assertEqual(self.mock_policy["rules"], [self.seeded])

    def test_apply_writes_only_to_the_policy_of_the_agents_runtime(self) -> None:
        self.cli("install", "claude-code")
        other = {**json.loads(json.dumps(self.mock_policy)), "policy_id": mock_governance.object_id(), "name": "Codex policy"}
        self.governance["policies"].append(other)
        self.governance["agents"].append({"agent_id": mock_governance.object_id(), "name": "Codex", "runtime_name": "codex",
                                          "policy_id": other["policy_id"]})
        before = json.dumps(other)
        result = self.policy("apply", "--yes")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertNotIn("Codex policy", result.stdout)
        self.assertEqual(json.dumps(other), before)
        self.assertEqual(len(self.mock_policy["rules"]), 3)

    def test_a_draft_it_cannot_delete_is_named(self) -> None:
        self.cli("install", "claude-code")
        self.governance["fail"] = {"PUT": 400, "DELETE": 500}
        result = self.policy("apply", "--yes")
        self.assertEqual(result.returncode, 1, result.stdout)
        (draft,) = self.governance["drafts"]
        self.assertIn(f"could not delete draft {draft} (HTTP 500); delete it in Mezmo", result.stdout)
        self.assertEqual(self.mock_policy["rules"], [self.seeded])

    def test_a_failed_read_is_a_failure_not_an_empty_policy(self) -> None:
        self.cli("install", "claude-code")
        self.governance["fail"] = {"GET": 500}
        for action in ("show", "apply"):
            with self.subTest(action=action):
                result = self.policy(action)
                self.assertEqual(result.returncode, 1, result.stdout)
                self.assertIn(f"FAIL  policy {action}: read agents: HTTP 500", result.stdout)
                self.assertNotIn("not covered", result.stdout)
        result = self.cli("policy", "show", "--url", "http://127.0.0.1:9/v3/governance")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("got no answer", result.stdout)

    def test_every_policy_page_is_read(self) -> None:
        # Mezmo returns at most 50 policies a page.
        for number in range(60):
            self.governance["policies"].append({**self.mock_policy, "policy_id": mock_governance.object_id(), "name": f"extra {number}"})
        result = self.policy("show")
        self.assertEqual(result.returncode, 0, result.stdout)
        self.assertEqual(result.stdout.count("\npolicy  "), 61)
        self.assertIn("extra 59", result.stdout)

    def test_a_page_link_it_cannot_follow_fails(self) -> None:
        import mezmo_governance_cli as cli

        for pages, words in (
            ([{"meta": {"page": {"next": "/v3/governance/policy?limit=50&after=a"}}, "data": [{}]}] * 2, "repeats cursor a"),
            ([{"meta": {"page": {"next": "/v3/governance/policy?page=2"}}, "data": [{}]}], "no after cursor"),
            ([{"meta": {"page": {"next": 7}}, "data": [{}]}], "no after cursor"),
        ):
            with self.subTest(words=words), mock.patch.object(cli, "api", side_effect=[(200, page) for page in pages]):
                with self.assertRaisesRegex(cli.Failure, words):
                    cli.listed("https://example.test/v3/governance", ADMIN, "/policy", "read policies")

    def test_the_admin_key_is_sent_and_never_the_hook_token(self) -> None:
        self.cli("install", "claude-code")
        self.policy("apply", "--yes")
        self.assertEqual({auth for _, _, auth, _ in self.governance["calls"]}, {f"Token {ADMIN}"})
        self.governance["calls"].clear()
        result = self.policy("show", MEZMO_GOVERNANCE_ADMIN_KEY="")
        self.assertEqual(result.returncode, 1, result.stdout)
        self.assertIn("set MEZMO_GOVERNANCE_ADMIN_KEY", result.stdout)
        self.assertIn("never MEZMO_GOVERNANCE_TOKEN", result.stdout)
        self.assertEqual(self.governance["calls"], [])

    def test_a_terminal_is_asked_for_the_key(self) -> None:
        import mezmo_governance_cli as cli

        with mock.patch.dict(os.environ, {"MEZMO_GOVERNANCE_ADMIN_KEY": ""}), mock.patch.object(cli.sys, "stdin") as stdin, \
                mock.patch.object(cli.getpass, "getpass", return_value=" typed-key \n") as asked:
            stdin.isatty.return_value = True
            self.assertEqual(cli.admin_key(), "typed-key")
        self.assertIn("MEZMO_GOVERNANCE_ADMIN_KEY", asked.call_args[0][0])

    def test_the_base_url_comes_from_url_then_the_hook_url_then_the_default(self) -> None:
        import mezmo_governance_cli as cli

        cases = (
            ("https://example.test/v3/governance/", {}, "https://example.test/v3/governance"),
            (None, {"MEZMO_GOVERNANCE_URL": "https://api.example.test/governance/workflow/authorize"}, "https://api.example.test/v3/governance"),
            (None, {"MEZMO_GOVERNANCE_URL": "http://127.0.0.1:8787/governance/workflow/authorize"}, "http://127.0.0.1:8787/v3/governance"),
            (None, {}, "https://api.mezmo.com/v3/governance"),
        )
        for url, env, want in cases:
            with self.subTest(url=url, env=env), mock.patch.dict(os.environ, {"MEZMO_GOVERNANCE_URL": "", **env}):
                self.assertEqual(cli.policy_base(url), want)
        for url, env, said in (
            ("http://example.test/v3/governance", {}, "uses http"),
            (None, {"MEZMO_GOVERNANCE_URL": "https://api.example.test/other"}, "pass --url"),
        ):
            with self.subTest(url=url, env=env), mock.patch.dict(os.environ, {"MEZMO_GOVERNANCE_URL": "", **env}):
                with self.assertRaisesRegex(cli.Failure, said):
                    cli.policy_base(url)

    def open_draft(self, write: bool = False) -> str:
        root = f"/policy/{self.mock_policy['policy_id']}"
        draft = self.call("POST", f"{root}/draft", {})["data"]["draft_id"]
        if write:
            self.call("PUT", f"{root}/draft/{draft}", {"name": "x", "default_action": {"recourse": "deny"}, "rules": []})
        return draft

    def call(self, method: str, path: str, body: dict = None) -> dict:
        data = None if body is None else json.dumps(body).encode()
        request = Request(self.base + path, data=data, method=method, headers={"Authorization": "Token other", "Content-Type": "application/json"})
        with build_opener(ProxyHandler({})).open(request, timeout=10) as response:
            raw = response.read()
        return json.loads(raw) if raw else {}


class ManifestTests(unittest.TestCase):
    def test_each_agents_namespace_is_the_one_its_adapter_sends(self) -> None:
        import doctor

        for agent in doctor.AGENTS:
            with self.subTest(agent=agent.readme):
                sources = [
                    *(REPO / agent.readme).glob("mezmo_governance_hook.py"),
                    *(REPO / agent.readme).glob("scripts/governance_hook.py"),
                    *(REPO / agent.readme).glob("**/client.ts"),
                ]
                found = {
                    match.group(1)
                    for path in sources if "node_modules" not in path.parts
                    for match in re.finditer(r'^(?:export const )?AGENT = "([^"]+)"', path.read_text(), re.M)
                }
                self.assertEqual(found, {agent.namespace})

    def test_each_native_plugin_points_at_its_own_adapter_and_hook(self) -> None:
        # marketplace, plugin manifest, hook config, and the example's hook path the plugin swaps out.
        cases = (
            (".agents/plugins/marketplace.json", ".codex-plugin/plugin.json", "codex",
             {'${MEZMO_GOVERNANCE_HOOK:-$HOME/.codex/hooks/mezmo_governance_hook.py}': "${PLUGIN_ROOT}/mezmo_governance_hook.py"}),
            (".github/plugin/marketplace.json", "plugin.json", "copilot",
             {'${MEZMO_GOVERNANCE_HOOK:-$HOME/.copilot/hooks/mezmo_governance_hook.py}': "${PLUGIN_ROOT}/mezmo_governance_hook.py",
              "$HOME/.copilot/hooks/mezmo_governance_hook.py": "$env:PLUGIN_ROOT/mezmo_governance_hook.py"}),
        )
        for marketplace, manifest, adapter, swaps in cases:
            with self.subTest(adapter=adapter):
                market = json.loads((REPO / marketplace).read_text())
                (entry,) = market["plugins"]
                self.assertEqual((market["name"], entry["name"], entry["source"]), ("mezmo", "mezmo-governance", f"./{adapter}"))
                root = REPO / entry["source"]
                self.assertEqual(json.loads((root / manifest).read_text())["name"], "mezmo-governance")
                self.assertTrue((root / "mezmo_governance_hook.py").is_file())
                text = (root / "hooks.example.json").read_text()
                for old, new in swaps.items():
                    text = text.replace(old, new)
                plugin = json.loads((root / "hooks/hooks.json").read_text())
                self.assertEqual(plugin["hooks"], json.loads(text)["hooks"])


class WrapperTests(CliTestCase):
    def test_it_runs_from_another_folder_and_passes_the_arguments(self) -> None:
        elsewhere = self.tmp / "elsewhere"
        elsewhere.mkdir()
        result = subprocess.run(
            ["sh", str(WRAPPER), "install", "nope"], cwd=elsewhere, env=self.env,
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 2)
        self.assertIn("unknown agent: nope", result.stderr)
        result = subprocess.run(
            [str(WRAPPER), "agents"], cwd=elsewhere, env=self.env, capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(result.stdout.startswith("claude-code "), result.stdout)

    def test_only_an_old_python_exits_1_with_the_fix(self) -> None:
        # The fallbacks are absolute paths that exist on this machine, so a copy points them elsewhere.
        text = WRAPPER.read_text()
        fallbacks = ("/opt/homebrew/bin/python3", "/usr/local/bin/python3")
        for path in fallbacks:
            self.assertEqual(text.count(path), 1, path)
            text = text.replace(path, str(self.tmp / "missing" / "python3"))
        copy = self.tmp / "wrapper" / "mezmo-governance"
        copy.parent.mkdir()
        copy.write_text(text)
        old = self.tmp / "old-bin"
        old.mkdir()
        (old / "dirname").symlink_to(shutil.which("dirname"))
        # Python 3.8 runs the check and exits 1; anything else it is asked means the wrapper ran it.
        (old / "python3").write_text('#!/bin/sh\n[ "$1" = -c ] && exit 1\necho "ran with 3.8"\n')
        (old / "python3").chmod(0o755)
        result = subprocess.run(
            ["/bin/sh", str(copy), "agents"], env={"PATH": str(old), "HOME": str(self.tmp / "home")},
            capture_output=True, text=True, timeout=60, check=False,
        )
        self.assertEqual(result.returncode, 1)
        self.assertEqual(result.stdout, "")
        self.assertEqual(result.stderr, "mezmo-governance needs Python 3.9 or later; on macOS run: brew install python\n")

    def test_the_wrapper_is_short_posix_sh_and_executable(self) -> None:
        self.assertTrue(os.access(WRAPPER, os.X_OK))
        self.assertTrue(WRAPPER.read_text().startswith("#!/bin/sh\n"))
        self.assertLessEqual(len(WRAPPER.read_text().splitlines()), 25)
        self.assertEqual(subprocess.run(["sh", "-n", str(WRAPPER)], check=False).returncode, 0)


if __name__ == "__main__":
    unittest.main()
