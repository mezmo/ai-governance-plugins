"""./mezmo-governance, run as a user runs it: temp config folders, fake agents on PATH.

The fake claude, gemini and pi write what the real CLIs wrote in a temp
folder on 2026-10-15 (Claude Code 2.1.300, Gemini CLI 0.64.0, Pi 1.2.0),
including what each leaves behind after an uninstall.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

TOOLS = Path(__file__).resolve().parent
REPO = TOOLS.parent
WRAPPER = REPO / "mezmo-governance"
CANARY = "canary-key-5d1c8e40"
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
        folder = root / "plugins" / "cache" / "mezmo" / "mezmo-governance" / "0.2.0"
        shutil.copytree(Path(markets["mezmo"]["source"]["path"]) / "claude-code", folder, dirs_exist_ok=True)
        plugins["plugins"]["mezmo-governance@mezmo"] = [{"scope": "user", "installPath": str(folder)}]
        data.setdefault("enabledPlugins", {})["mezmo-governance@mezmo"] = True
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
        if sys.stdin.readline().strip() != "y":
            sys.exit("fake gemini: the folder was not trusted")
        shutil.copytree(args[2], target)
        trusted = load(root / "trustedFolders.json")
        trusted[args[2].lower()] = "TRUST_FOLDER"
        save(root / "trustedFolders.json", trusted)
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
    "gemini-cli": ("gemini/.gemini/trustedFolders.json",),
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
                "MEZMO_GOVERNANCE_ACCESS_KEY": CANARY,
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
        self.assertEqual(options["MEZMO_GOVERNANCE_ACCESS_KEY"], f"{{file:{self.tmp}/xdg/mezmo/governance-key}}")
        self.assertTrue((self.tmp / "xdg/opencode/mezmo-governance/index.ts").is_file())
        self.assertFalse((self.tmp / "xdg/opencode/mezmo-governance/test").exists())
        self.assertFalse((self.tmp / "xdg/mezmo/governance-key").exists())
        calls = [json.loads(line) for line in self.log.read_text().splitlines()]
        self.assertIn(["claude", "plugin", "install", "mezmo-governance", "--marketplace", str(REPO), "-s", "user"], calls)
        self.assertIn(["gemini", "extensions", "install", str(REPO / "gemini-cli"), "--consent", "--skip-settings"], calls)
        self.assertIn(["pi", "install", str(REPO / "pi")], calls)
        self.assertIn("LangChain: langchain/README.md", result.stdout)
        self.assertIn("Mastra: mastra/README.md", result.stdout)
        self.assertIn("/hooks", result.stdout)

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
                for word in ("agents", "install", "uninstall", "doctor", "mock", "smoke", *AGENTS, "CODEX_HOME"):
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
        lines = self.cli("status").stdout.splitlines()
        self.assertEqual([line.split()[0] for line in lines], list(AGENTS))
        self.assertIn("codex 0.162.1", lines[1])
        self.assertIn("installed: yes", lines[1])
        self.assertIn("fails closed: yes", lines[1])
        self.assertIn("goose not found", lines[5])
        self.assertIn("installed: no", lines[5])
        self.assertIn("fails closed: n/a", lines[7])

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
        self.assertIn("export MEZMO_GOVERNANCE_ACCESS_KEY=local-test", lines[2])
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


class KeyTests(CliTestCase):
    def test_the_key_is_in_no_file_and_no_output(self) -> None:
        # self.cli asserts the canary is in no output.
        for args in (("install", "--all"), ("agents",), ("doctor",), ("smoke",), ("uninstall", "--all")):
            self.cli(*args)
            leaked = [name for name, content in self.files().items() if CANARY.encode() in content]
            self.assertEqual(leaked, [], args)


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
