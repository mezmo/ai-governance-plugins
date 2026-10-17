#!/usr/bin/env python3
"""The `./mezmo-governance` command: install, check and try the plugins.

Run it through the `mezmo-governance` script at the repository root, which
finds Python 3.9 or later. It never writes the token into any file.
"""

from __future__ import annotations

import getpass
import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional
from urllib.error import HTTPError
from urllib.parse import parse_qs, quote, urlparse, urlunparse
from urllib.request import Request, build_opener

sys.path.insert(0, str(Path(__file__).resolve().parent))
import doctor  # noqa: E402
import mock_governance  # noqa: E402
from doctor import HOOK_FILE, REPO, as_dict, as_list, read_json, shown  # noqa: E402

LIBRARIES = (
    ("LangChain", "langchain/README.md"),
    ("OpenAI Agents SDK", "openai-agents/README.md"),
    ("Claude Agent SDK", "claude-agent-sdk/README.md"),
    ("Mastra", "mastra/README.md"),
)
# The mock denies each agent's shell tool and approves the rest.
SHELL_TOOLS = list(dict.fromkeys(agent.shell for agent in doctor.AGENTS))
SMOKE_KEY = "smoke-test"
CLI_SECONDS = 300
POLICY_URL = "https://api.mezmo.com/v3/governance"
ADMIN_KEY = "MEZMO_GOVERNANCE_ADMIN_KEY"
PAGE_LIMIT = 50  # the most policies Mezmo returns a page
# The starter rules policy apply adds, keyed by the agent's tool they list; an agent without that tool gets none.
STARTER = {
    "shell": {"recourse": "deny", "code": "FORBIDDEN_TOOL", "error_message": "Shell blocked by Mezmo policy"},
    "read": {"recourse": "allow"},
}

HELP = f"""\
Usage: ./mezmo-governance <command> [agent...] [options]

Commands:
  help                     show this text (also -h, --help, or no command)
  agents                   one line per agent: binary, version, installed, fails closed (also: status)
  install <agent...>|--all add the Mezmo hook to each agent's user config; --dry-run shows the changes only
  uninstall <agent...>|--all
                           remove only what install added; --dry-run shows the changes only
  doctor [agent...]        check the settings, Mezmo and each agent; --probe sends Mezmo one test call
  mock [--deny PATTERN...] start a local stand-in for Mezmo and print the two export lines
  smoke [agent...] [--live]
                           run each installed hook against its own mock: one call denied, one approved;
                           --live sends the shell and read call to Mezmo and prints what the policy decided
  policy show              print the Mezmo agents, policies and rules, and which rule covers each
                           installed agent's shell and read tool
  policy apply [agent...]  add a deny rule for the shell tools and an allow rule for the read tools
                           that no rule covers; --yes writes them through a Mezmo draft
                           Both take --agent NAME|ID: the Mezmo agent whose policy applies to every
                           installed agent. Without it, the only Mezmo agent, else the one whose
                           Runtime Name is the agent's.

Agents: {', '.join(agent.readme for agent in doctor.AGENTS)}
  install --all picks every agent whose binary is on PATH; uninstall --all picks every installed agent.

Examples:
  ./mezmo-governance install claude-code --dry-run
  ./mezmo-governance install claude-code codex
  ./mezmo-governance doctor --probe
  ./mezmo-governance smoke
  ./mezmo-governance smoke --live codex
  ./mezmo-governance mock --deny Bash
  ./mezmo-governance policy apply claude-code --yes

Settings:
  MEZMO_GOVERNANCE_URL and MEZMO_GOVERNANCE_TOKEN come from the shell that starts the agent.
  The Claude Code plugin can keep its own copy (/mezmo:setup); OpenCode 2.x reads plugin
  options in opencode.json, with the key in a file. Install never writes the key.
  Config folders follow each agent's override: CLAUDE_CONFIG_DIR, CODEX_HOME, GEMINI_CLI_HOME,
  COPILOT_HOME, GOOSE_PATH_ROOT, XDG_CONFIG_HOME (OpenCode), PI_CODING_AGENT_DIR,
  FACTORY_HOME_OVERRIDE (Droid), else HOME.
  policy reads MEZMO_GOVERNANCE_ADMIN_KEY, or asks for it, and never MEZMO_GOVERNANCE_TOKEN: every
  governed agent can read that one. --url BASE sets the policy API, else it comes from
  MEZMO_GOVERNANCE_URL, else https://api.mezmo.com/v3/governance.

Libraries ({', '.join(name for name, _ in LIBRARIES)}) are set up in app code: see each README.
`python3 tools/doctor.py` is the same check as `doctor`.
"""


class Usage(Exception):
    pass


class Failure(Exception):
    pass


class Step(NamedTuple):
    text: str
    files: tuple  # config files the step edits; each gets a backup first
    run: Callable[[], None]


# --- small file helpers -------------------------------------------------------


def backup(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    target = path.with_name(f"{path.name}.mezmo-backup-{stamp}")
    count = 1
    while target.exists():
        count += 1
        target = path.with_name(f"{path.name}.mezmo-backup-{stamp}-{count}")
    shutil.copy2(path, target)
    return target


def write_json(path: Path, data: Any) -> None:
    """Write data as JSON in the file's own indent, line endings and final newline.

    json.dumps lays out every value again: a value the user kept on one line or aligned by hand comes out one item a line.
    """
    # Write through a symlink, so a config linked from elsewhere stays linked.
    path = path.resolve()
    indent, newline, ending = "  ", "\n", "\n"
    if path.is_file():
        text = path.read_bytes().decode("utf-8")
        found = re.search(r"\n([ \t]*)\S", text)
        indent, ending = (found.group(1) if found else None), text[len(text.rstrip()):]
        newline = "\r\n" if "\r\n" in text else "\n"
    path.parent.mkdir(parents=True, exist_ok=True)
    partial = path.with_name(path.name + ".mezmo-partial")
    output = json.dumps(data, indent=indent, ensure_ascii=False).replace("\n", newline) + ending
    partial.write_bytes(output.encode("utf-8"))
    if path.is_file():
        # Keep the file's mode, such as 0600.
        shutil.copymode(path, partial)
    os.replace(partial, path)


def load_object(path: Path) -> dict:
    data = read_json(path)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise doctor.Unknown(f"{shown(path)} is not a JSON object")
    return data


def copy_file(source: Path, target: Path) -> None:
    # Swap the file in whole: an agent may run the hook while it is being replaced.
    target = target.resolve()
    target.parent.mkdir(parents=True, exist_ok=True)
    partial = target.with_name(target.name + ".mezmo-partial")
    shutil.copy2(source, partial)
    os.replace(partial, target)


def differs(target: Path, content: bytes) -> bool:
    """Whether target is there but holds other content, as an install from an older release does."""
    try:
        return target.is_file() and target.read_bytes() != content
    except OSError as error:
        raise doctor.Unknown(f"could not read {shown(target)}: {error}") from error


def remove_file(path: Path, root: Path) -> None:
    """Delete the file, then each folder it leaves empty, up to the agent's folder."""
    if path.is_file():
        path.unlink()
    parent = path.parent
    while parent != root and root in parent.parents and parent.is_dir() and not any(parent.iterdir()):
        parent.rmdir()
        parent = parent.parent


def at(folder: Path, default: Path, text: str) -> str:
    """Example text with the default folder swapped for the one in use."""
    if folder == default:
        return text
    if any(char in str(folder) for char in '"$`\\'):
        raise Failure(f"{folder} holds a character the hook command cannot quote")
    return text.replace("$HOME/" + str(default.relative_to(doctor.home())), str(folder))


def run_cli(*args: str, env: Optional[dict] = None) -> None:
    print(f"      $ {' '.join((Path(args[0]).name,) + args[1:])}", flush=True)
    try:
        # No stdin: a CLI that stops to ask a question fails instead of waiting on the terminal.
        result = subprocess.run(list(args), stdin=subprocess.DEVNULL, env=env, timeout=CLI_SECONDS, check=False)
    except (OSError, subprocess.SubprocessError) as error:
        raise Failure(f"{args[0]} did not run: {error}") from error
    if result.returncode != 0:
        raise Failure(f"{' '.join(args[:3])} exited {result.returncode}")


def need(binary: str) -> str:
    path = shutil.which(binary)
    if path is None:
        raise Failure(f"{binary} is not on PATH; install it first")
    return path


# --- hook entries in JSON configs ---------------------------------------------


def runs_hook(handler: Any, field: str) -> bool:
    return HOOK_FILE in str(as_dict(handler).get(field, ""))


def merged(config: dict, example: dict, field: str) -> dict:
    """The config plus the example's entries for each event that lacks them."""
    result = json.loads(json.dumps(config))
    for key, value in example.items():
        if key != "hooks":
            result.setdefault(key, value)
    hooks = result.setdefault("hooks", {})
    for event, groups in example["hooks"].items():
        if not doctor.mezmo_handlers(hooks.get(event), field):
            hooks[event] = as_list(hooks.get(event)) + groups
    return result


def without_hook(config: dict, field: str) -> dict:
    """The config with every entry that runs the hook taken out.

    Codex keys hook trust by group index, so a removed group that has groups after it
    leaves an empty group in its place: the later groups keep their index and their trust.
    """
    result = json.loads(json.dumps(config))
    hooks = as_dict(result.get("hooks"))
    for event in list(hooks):
        kept = []
        for group in as_list(hooks[event]):
            if isinstance(group, dict) and isinstance(group.get("hooks"), list):
                inner = [handler for handler in group["hooks"] if not runs_hook(handler, field)]
                if inner or not group["hooks"]:
                    kept.append({**group, "hooks": inner})
                else:
                    kept.append(None)
            elif not runs_hook(group, field):
                kept.append(group)
        while kept and kept[-1] is None:
            kept.pop()
        kept = [{"hooks": []} if group is None else group for group in kept]
        if kept:
            hooks[event] = kept
        else:
            del hooks[event]
    if "hooks" in result and not hooks:
        del result["hooks"]
    return result


def moved_handlers(config: dict, field: str) -> list:
    """'<event> group <g> handler <h> (<command>)' for each other handler that without_hook moves up.

    Codex has no handler type that neither runs nor warns (codex-rs/hooks/src/engine/discovery.rs,
    rust-v0.163.0), so a Mezmo handler ahead of another in its group cannot leave a placeholder.
    """
    moved = []
    for event, groups in as_dict(config.get("hooks")).items():
        for g, group in enumerate(as_list(groups)):
            kept = 0
            for h, handler in enumerate(as_list(as_dict(group).get("hooks"))):
                if not runs_hook(handler, field):
                    if kept != h:
                        moved.append(f"{event} group {g} handler {h} ({as_dict(handler).get(field)})")
                    kept += 1
    return moved


def merge_steps(path: Path, example: dict, event: str, field: str = "command") -> list:
    config = load_object(path)
    if doctor.mezmo_handlers(as_dict(config.get("hooks")).get(event), field):
        return []
    return [Step(f"add the Mezmo hook entries to {shown(path)}", (path,), lambda: write_json(path, merged(load_object(path), example, field)))]


def unmerge_steps(path: Path, field: str = "command", leftover: tuple = ()) -> list:
    config = load_object(path)
    if without_hook(config, field) == config:
        return []

    def run() -> None:
        rest = without_hook(load_object(path), field)
        if set(rest) <= set(leftover):
            path.unlink()
        else:
            write_json(path, rest)

    return [Step(f"remove the Mezmo hook entries from {shown(path)}", (path,), run)]


def hook_copy(source: Path, target: Path) -> Step:
    return Step(f"copy {source.relative_to(REPO)} to {shown(target)}", (), lambda: copy_file(source, target))


def hook_refresh(source: Path, target: Path) -> list:
    """A step to replace an installed copy that differs from this repository's file."""
    if not differs(target, source.read_bytes()):
        return []
    return [Step(f"update {shown(target)} from {source.relative_to(REPO)}", (), lambda: copy_file(source, target))]


def hook_removal(target: Path, root: Path) -> list:
    return [Step(f"delete {shown(target)}", (), lambda: remove_file(target, root))] if target.is_file() else []


# --- each agent: is it installed, and the steps to install or remove it --------


class Installer(NamedTuple):
    installed: Callable[[], Optional[str]]
    install: Callable[[], list]
    uninstall: Callable[[], list]
    next: str


def example(adapter: str, name: str, folder: Path, default: Path) -> dict:
    return json.loads(at(folder, default, (REPO / adapter / name).read_text(encoding="utf-8")))


def file_hook_agent(
    adapter: str, folder: Callable[[], Path], event: str, next_step: str, leftover: tuple = (), trust_by_position: bool = False
) -> Installer:
    """Codex and Cursor: a hook script plus entries merged into hooks.json."""

    def installed() -> Optional[str]:
        path = folder() / "hooks.json"
        found = doctor.mezmo_handlers(as_dict(load_object(path).get("hooks")).get(event))
        return shown(path) if found else None

    def install() -> list:
        source, target = REPO / adapter / HOOK_FILE, folder() / "hooks" / HOOK_FILE
        steps = hook_refresh(source, target) if installed() else [hook_copy(source, target)]
        data = example(adapter, "hooks.example.json", folder(), doctor.home() / f".{adapter}")
        return steps + merge_steps(folder() / "hooks.json", data, event)

    def uninstall() -> list:
        path = folder() / "hooks.json"
        moved = moved_handlers(load_object(path), "command") if trust_by_position else []
        if moved:
            raise Failure(
                f"removing the Mezmo handler from {shown(path)} would give a lower handler index to"
                f" {'; '.join(moved)}, and {adapter} would stop trusting them. Nothing changed. To finish by hand, delete each handler"
                f" that runs {HOOK_FILE} from {shown(path)}, run this command again to delete the hook script,"
                f" then start {adapter}, run /hooks and trust the moved hooks again"
            )
        return unmerge_steps(path, leftover=leftover) + hook_removal(folder() / "hooks" / HOOK_FILE, folder())

    return Installer(installed, install, uninstall, next_step)


def copilot() -> Installer:
    def files() -> tuple:
        hooks = doctor.copilot_dir() / "hooks"
        return hooks / HOOK_FILE, hooks / "mezmo-governance.json"

    def installed() -> Optional[str]:
        script, config = files()
        return shown(config) if script.is_file() and config.is_file() else None

    def install() -> list:
        script, config = files()
        default = doctor.home() / ".copilot"
        data = example("copilot", "hooks.example.json", doctor.copilot_dir(), default)
        if installed():
            steps = hook_refresh(REPO / "copilot" / HOOK_FILE, script)
            if load_object(config) != data:
                steps.append(Step(f"update {shown(config)} from copilot/hooks.example.json", (config,), lambda: write_json(config, data)))
            return steps
        return [
            hook_copy(REPO / "copilot" / HOOK_FILE, script),
            Step(f"write {shown(config)}", (), lambda: write_json(config, data)),
        ]

    def uninstall() -> list:
        script, config = files()
        return hook_removal(config, doctor.copilot_dir()) + hook_removal(script, doctor.copilot_dir())

    return Installer(installed, install, uninstall, "")


def goose() -> Installer:
    files = ("plugin.json", HOOK_FILE, "hooks/hooks.json")

    def installed() -> Optional[str]:
        folder = doctor.goose_dir()
        hooks = as_dict(load_object(folder / "hooks" / "hooks.json").get("hooks"))
        complete = all((folder / name).is_file() for name in files)
        return shown(folder) if complete and doctor.mezmo_handlers(hooks.get("PreToolUse")) else None

    def install() -> list:
        if installed():
            return [step for name in files for step in hook_refresh(REPO / "goose" / name, doctor.goose_dir() / name)]
        # hooks.json goes last: until it is there, the plugin counts as not installed.
        return [hook_copy(REPO / "goose" / name, doctor.goose_dir() / name) for name in files]

    def uninstall() -> list:
        folder = doctor.goose_dir()
        return [step for name in reversed(files) for step in hook_removal(folder / name, folder.parent)]

    return Installer(installed, install, uninstall, "")


def ours(item: Any) -> bool:
    """Whether an opencode.json plugins entry names this plugin."""
    package = item if isinstance(item, str) else as_dict(item).get("package")
    return isinstance(package, str) and Path(package).name == "mezmo-governance"


def opencode() -> Installer:
    def paths() -> tuple:
        config = doctor.opencode_dir()
        key_file = doctor.env_dir("XDG_CONFIG_HOME", doctor.home() / ".config") / "mezmo" / "governance-key"
        return config / "opencode.json", config / "mezmo-governance", key_file

    def installed() -> Optional[str]:
        config, folder, _ = paths()
        entries = [item for item in as_list(load_object(config).get("plugins")) if ours(item)]
        return shown(config) if entries and (folder / "index.ts").is_file() else None

    def install() -> list:
        if installed():
            source, folder = REPO / "opencode", paths()[1]
            steps = []
            for path in sorted(source.rglob("*")):
                target = folder / path.relative_to(source)
                if path.is_file() and not {"test", "node_modules", "README.md"} & set(path.relative_to(source).parts):
                    steps += hook_refresh(path, target) if target.is_file() else [hook_copy(path, target)]
            return steps
        version = doctor.binary_version(shutil.which("opencode") or "opencode")
        if version is not None and version[0] < 2:
            raise Failure("install does not handle OpenCode 1.x; follow 'Install on 1.x' in opencode/README.md")
        config, folder, key_file = paths()
        url = os.environ.get("MEZMO_GOVERNANCE_URL") or doctor.DEFAULT_URL
        if doctor.local_url(url):
            print(
                f"warn  opencode: MEZMO_GOVERNANCE_URL {doctor.shown_url(url)} is a local mock, and opencode.json keeps it"
                " after the mock stops; set MEZMO_GOVERNANCE_URL in opencode.json to Mezmo's when you are done",
                flush=True,
            )
        entry = {
            "package": "./mezmo-governance",
            "options": {"MEZMO_GOVERNANCE_URL": url, "MEZMO_GOVERNANCE_TOKEN": f"{{file:{key_file}}}"},
        }

        def add_entry() -> None:
            data = load_object(config)
            data["plugins"] = [item for item in as_list(data.get("plugins")) if not ours(item)] + [entry]
            write_json(config, data)

        def copy() -> None:
            ignore = shutil.ignore_patterns("test", "node_modules", "README.md")
            shutil.copytree(REPO / "opencode", folder, ignore=ignore, dirs_exist_ok=True)

        return [
            Step(f"copy opencode/ to {shown(folder)}", (), copy),
            Step(f"name it in {shown(config)}, URL {doctor.shown_url(url)}, key from {shown(key_file)}", (config,), add_entry),
        ]

    def uninstall() -> list:
        config, folder, _ = paths()
        steps = []
        data = load_object(config)
        if any(ours(item) for item in as_list(data.get("plugins"))):

            def remove_entry() -> None:
                rest = load_object(config)
                rest["plugins"] = [item for item in as_list(rest.get("plugins")) if not ours(item)]
                if not rest["plugins"]:
                    del rest["plugins"]
                if rest:
                    write_json(config, rest)
                else:
                    config.unlink()

            steps.append(Step(f"remove the mezmo-governance entry from {shown(config)}", (config,), remove_entry))
        if (folder / "index.ts").is_file():
            steps.append(Step(f"delete {shown(folder)}", (), lambda: shutil.rmtree(folder)))
        return steps

    _, _, key_file = paths()
    return Installer(
        installed,
        install,
        uninstall,
        f"Put the service account token in {shown(key_file)}, readable only by you:"
        f" (umask 077; printf '%s\\n' '<service account token>' > {shown(key_file)})",
    )


def claude_code() -> Installer:
    def files() -> tuple:
        root = doctor.claude_dir()
        return root / "settings.json", root / "plugins" / "installed_plugins.json", root / "plugins" / "known_marketplaces.json"

    def plugin_keys() -> list:
        registry = as_dict(load_object(files()[1]).get("plugins"))
        return [
            key
            for key, installs in registry.items()
            if key.startswith(doctor.CLAUDE_PLUGINS) and any(as_dict(item).get("scope") == "user" for item in as_list(installs))
        ]

    def installed() -> Optional[str]:
        keys = [key for key in plugin_keys() if key.startswith("mezmo@")]
        settings = load_object(files()[0])
        if keys:
            return f"plugin {keys[0]}"
        return shown(files()[0]) if doctor.mezmo_handlers(as_dict(settings.get("hooks")).get("PreToolUse")) else None

    def uninstall_steps(keys: list) -> list:
        return [
            Step(f"claude plugin uninstall {key} -s user", files(), lambda key=key: run_cli(need("claude"), "plugin", "uninstall", key, "-s", "user"))
            for key in keys
        ]

    def install() -> list:
        if installed():
            return []
        # The older mezmo-governance plugin would check every call a second time.
        steps = uninstall_steps([key for key in plugin_keys() if key.startswith("mezmo-governance@")])
        if "mezmo" in load_object(files()[2]):
            args = ("plugin", "install", "mezmo@mezmo", "-s", "user")
        else:
            args = ("plugin", "install", "mezmo", "--marketplace", str(REPO), "-s", "user")
        return steps + [Step(f"claude {' '.join(args)}", files(), lambda: run_cli(need("claude"), *args))]

    def uninstall() -> list:
        steps = uninstall_steps(plugin_keys())
        source = as_dict(as_dict(load_object(files()[2]).get("mezmo")).get("source"))
        if source.get("path") == str(REPO):
            steps.append(Step("claude plugin marketplace remove mezmo", files(), lambda: run_cli(need("claude"), "plugin", "marketplace", "remove", "mezmo")))
        if steps:

            def tidy() -> None:
                # Claude Code leaves these two empty; drop them so the file matches what it was.
                settings = load_object(files()[0])
                left = {key: value for key, value in settings.items() if not (key in ("enabledPlugins", "extraKnownMarketplaces") and value == {})}
                if left != settings:
                    write_json(files()[0], left)

            steps.append(Step(f"drop empty enabledPlugins and extraKnownMarketplaces from {shown(files()[0])}", (), tidy))
        return steps

    return Installer(
        installed,
        install,
        uninstall,
        "In Claude Code run /mezmo:setup governance to keep the key in the system credential store,"
        " or export MEZMO_GOVERNANCE_URL and MEZMO_GOVERNANCE_TOKEN in the shell that starts claude.",
    )


def gemini_cli() -> Installer:
    def extension() -> Path:
        return doctor.gemini_dir() / "extensions" / "mezmo-governance"

    def touched() -> tuple:
        return (doctor.gemini_dir() / "extensions" / "extension-enablement.json",)

    def installed() -> Optional[str]:
        if (extension() / "hooks" / "hooks.json").is_file():
            return shown(extension())
        settings = load_object(doctor.gemini_dir() / "settings.json")
        found = doctor.mezmo_handlers(as_dict(settings.get("hooks")).get("BeforeTool"))
        return shown(doctor.gemini_dir() / "settings.json") if found else None

    def install() -> list:
        if installed():
            return []
        args = ("extensions", "install", str(REPO / "gemini-cli"), "--consent", "--skip-settings")
        # Gemini asks to trust an untrusted source folder and waits even on /dev/null; with --consent
        # it also trusts the current folder unasked. This trusts both for this one command only.
        trust = {**os.environ, "GEMINI_CLI_TRUST_WORKSPACE": "true"}
        return [Step(f"gemini {' '.join(args)}", touched(), lambda: run_cli(need("gemini"), *args, env=trust))]

    def uninstall() -> list:
        if not extension().is_dir():
            return []
        return [Step("gemini extensions uninstall mezmo-governance", touched(), lambda: run_cli(need("gemini"), "extensions", "uninstall", "mezmo-governance"))]

    return Installer(installed, install, uninstall, "In gemini, run /hooks panel and check that both Mezmo hooks are listed and enabled.")


def pi() -> Installer:
    source = (REPO / "pi").resolve()

    def settings() -> Path:
        return doctor.pi_dir() / "settings.json"

    def installed() -> Optional[str]:
        if source in doctor.pi_packages():
            return f"package {shown(source)}"
        manual = doctor.pi_dir() / "extensions" / "mezmo-governance"
        return shown(manual) if manual.is_dir() else None

    def install() -> list:
        if installed():
            return []
        return [Step(f"pi install {shown(source)}", (settings(),), lambda: run_cli(need("pi"), "install", str(source)))]

    def uninstall() -> list:
        if source not in doctor.pi_packages():
            return []
        return [Step(f"pi remove {shown(source)}", (settings(),), lambda: run_cli(need("pi"), "remove", str(source)))]

    return Installer(installed, install, uninstall, "Pi loads the extension from this checkout, so keep the checkout where it is.")


def droid() -> Installer:
    """Like Codex, but Droid's hooks.json holds the events at its top level, with no "hooks" key."""

    def paths() -> tuple:
        return doctor.droid_dir() / "hooks.json", doctor.droid_dir() / "hooks" / HOOK_FILE

    def events(config: dict) -> dict:
        return {key: value for key, value in config.items() if isinstance(value, list)}

    def installed() -> Optional[str]:
        path, _ = paths()
        return shown(path) if doctor.mezmo_handlers(load_object(path).get("PreToolUse")) else None

    def hidden(data: dict) -> list:
        """The settings.json hook events a new hooks.json entry would stop Droid from reading."""
        path, _ = paths()
        settings = events(as_dict(load_object(doctor.droid_dir() / "settings.json").get("hooks")))
        config = load_object(path)
        # Droid 0.244.0 reads an event from hooks.json once hooks.json has it; its docs say
        # settings.json hooks are read only while hooks.json is absent.
        wanted = set(data) if path.is_file() else set(settings)
        return sorted(event for event in wanted & set(settings) if settings[event] and event not in config)

    def install() -> list:
        path, script = paths()
        source = REPO / "droid" / HOOK_FILE
        if installed():
            return hook_refresh(source, script)
        legacy = doctor.droid_dir() / "hooks" / "hooks.json"
        if not path.is_file() and legacy.is_file():
            raise Failure(
                f"Droid reads {shown(legacy)} only while {shown(path)} is absent, so a new {path.name} would turn its hooks off."
                f" Nothing changed. Start droid, run /hooks and save once to move it to {shown(path)}, then run this command again"
            )
        data = example("droid", "hooks.example.json", doctor.droid_dir(), doctor.home() / ".factory")
        lost = hidden(data)
        if lost:
            raise Failure(
                f"{shown(doctor.droid_dir() / 'settings.json')} has {', '.join(lost)} hooks, which Droid stops reading once"
                f" {shown(path)} has those events. Nothing changed. Move them into {shown(path)}, then run this command again"
            )

        def add() -> None:
            config = load_object(path)
            write_json(path, {**config, **merged({"hooks": events(config)}, {"hooks": data}, "command")["hooks"]})

        return [hook_copy(source, script), Step(f"add the Mezmo hook entries to {shown(path)}", (path,), add)]

    def without(config: dict) -> dict:
        rest = as_dict(without_hook({"hooks": events(config)}, "command").get("hooks"))
        return {key: rest.get(key, value) for key, value in config.items() if key in rest or not isinstance(value, list)}

    def uninstall() -> list:
        path, script = paths()
        steps = []
        if without(load_object(path)) != load_object(path):

            def run() -> None:
                rest = without(load_object(path))
                if rest:
                    write_json(path, rest)
                else:
                    path.unlink()

            steps.append(Step(f"remove the Mezmo hook entries from {shown(path)}", (path,), run))
        return steps + hook_removal(script, doctor.droid_dir())

    return Installer(installed, install, uninstall, "Start droid and run /hooks to check that both Mezmo hooks are listed: Droid reads hooks at startup.")


INSTALLERS = {
    "claude-code": claude_code(),
    "codex": file_hook_agent(
        "codex", doctor.codex_dir, "PreToolUse",
        "Start codex, run /hooks, and trust the Mezmo hook entries: Codex skips a hook until you trust it.",
        trust_by_position=True,
    ),
    "cursor": file_hook_agent("cursor", doctor.cursor_dir, "preToolUse", "", leftover=("version",)),
    "gemini-cli": gemini_cli(),
    "copilot": copilot(),
    "goose": goose(),
    "opencode": opencode(),
    "pi": pi(),
    "droid": droid(),
}
AGENTS = {agent.readme: agent for agent in doctor.AGENTS}


# --- commands -----------------------------------------------------------------


def pick(names: list, every: bool, verb: str) -> list:
    unknown = [name for name in names if name not in AGENTS]
    if unknown:
        raise Usage(f"unknown agent: {', '.join(unknown)}")
    if every and names:
        raise Usage(f"{verb}: name agents or pass --all, not both")
    if every:
        if verb == "uninstall":
            return [name for name in AGENTS if installed_at(name)]
        return [name for name, agent in AGENTS.items() if shutil.which(agent.binary)]
    return names or list(AGENTS)


def installed_at(name: str) -> Optional[str]:
    try:
        return INSTALLERS[name].installed()
    except doctor.Unknown:
        return "unknown"


def change(verb: str, names: list, dry_run: bool) -> int:
    failed = False
    for name in names:
        installer = INSTALLERS[name]
        try:
            steps = installer.install() if verb == "install" else installer.uninstall()
        except (doctor.Unknown, Failure) as error:
            print(f"FAIL  {name}: {error}", flush=True)
            failed = True
            continue
        if not steps:
            state = "already installed" if verb == "install" else "not installed"
            print(f"ok    {name}: {state}, nothing to change", flush=True)
            continue
        print(f"{'plan' if dry_run else verb:<4}  {name}:", flush=True)
        saved: set = set()
        try:
            for step in steps:
                for path in step.files:
                    if path.is_file() and path not in saved:
                        saved.add(path)
                        if dry_run:
                            print(f"      would back up {shown(path)}", flush=True)
                        else:
                            print(f"      backup: {shown(backup(path))}", flush=True)
                print(f"      {'would ' if dry_run else ''}{step.text}", flush=True)
                if not dry_run:
                    step.run()
        except (OSError, doctor.Unknown, Failure) as error:
            print(f"FAIL  {name}: {error}; run the same command again to finish", flush=True)
            failed = True
            continue
        if verb == "install" and installer.next:
            print(f"      next: {installer.next}", flush=True)
    if verb == "install":
        print(
            "Next: export MEZMO_GOVERNANCE_URL and MEZMO_GOVERNANCE_TOKEN in the shell that starts the agent,"
            " then run ./mezmo-governance doctor and ./mezmo-governance smoke.",
            flush=True,
        )
        print(
            "Libraries are set up in app code, not installed here: "
            + "; ".join(f"{library}: {readme}" for library, readme in LIBRARIES),
            flush=True,
        )
    return 1 if failed else 0


class Quiet(doctor.Report):
    def __init__(self) -> None:
        super().__init__()
        self.lines: list = []

    def line(self, status: str, subject: str, detail: str, fix: str = "") -> None:
        self.lines.append((status, detail))
        if status == "FAIL":
            self.failed = True


def agents(names: list) -> int:
    for name in names:
        agent = AGENTS[name]
        path = shutil.which(agent.binary)
        version = doctor.binary_version(path) if path else None
        binary = f"{agent.binary} {doctor.dotted(version) if version else '?'}" if path else f"{agent.binary} not found"
        report = Quiet()
        try:
            agent.check(report, version)
            where = installed_at(name)
        except doctor.Unknown as error:
            print(f"{name:<12} {binary:<24} installed: unknown ({error})", flush=True)
            continue
        if any(status == "FAIL" and ("lets the tool run" in detail or doctor.UNGOVERNED in detail) for status, detail in report.lines):
            closed = "no"
        elif any(status == "ok" and "fails closed" in detail for status, detail in report.lines):
            closed = "yes"
        else:
            closed = "n/a"
        problems = sum(status == "FAIL" for status, _ in report.lines)
        state = f"yes ({where})" if where else "no"
        extra = f"  problems: {problems}, run ./mezmo-governance doctor {name}" if problems and where else ""
        print(f"{name:<12} {binary:<24} installed: {state}  fails closed: {closed if where else '-'}{extra}", flush=True)
    return 0


def fresh(event: dict, name: Optional[str] = None) -> dict:
    """The event with a new call id, and a new tool name when given."""
    result = {**event}
    for field in ("tool_use_id", "tool_call_id"):
        if field in result:
            result[field] = f"smoke-{uuid.uuid4()}"
    if name:
        result["toolName" if "toolName" in result else "tool_name"] = name
    return result


def plugin_files(name: str) -> list:
    """The installed OpenCode 2.x or Pi entry file, which the agent loads itself."""
    if name == "opencode":
        candidates = [doctor.opencode_dir() / "mezmo-governance" / "index.ts"]
    else:
        candidates = [folder / "mezmo-governance.ts" for folder in doctor.pi_packages()]
        candidates.append(doctor.pi_dir() / "extensions" / "mezmo-governance" / "mezmo-governance.ts")
    return [path for path in candidates if path.is_file()][:1]


def plugin_options(name: str, env: dict, live: bool) -> dict:
    """The options OpenCode passes the plugin: its opencode.json entry's, with each {file:...} read, as OpenCode reads them.

    The smoke's own timeout and audit log win, and against the mock its URL and token do too.
    """
    if name != "opencode":
        return {}
    config = doctor.opencode_dir() / "opencode.json"
    entry = next((item for item in as_list(load_object(config).get("plugins")) if ours(item)), None)
    forced = ["MEZMO_GOVERNANCE_TIMEOUT_SECONDS", "MEZMO_GOVERNANCE_AUDIT_LOG"]
    if not live:
        forced += ["MEZMO_GOVERNANCE_URL", "MEZMO_GOVERNANCE_TOKEN"]
    options = {**as_dict(as_dict(entry).get("options")), **{key: env[key] for key in forced if key in env}}

    def read(match: re.Match) -> str:
        path = Path(os.path.expanduser(match.group(1)))
        text = doctor.read_text(path if path.is_absolute() else config.parent / path)
        if text is None:
            raise Failure(f"{shown(config)} names {{file:{match.group(1)}}}, which is missing")
        return text.strip()

    return {key: re.sub(r"\{file:([^}]+)\}", read, value) if isinstance(value, str) else value for key, value in options.items()}


def live_env(scratch: Path) -> dict:
    """The user's environment for smoke --live: their URL and token, a 15 s limit unless they set one, a scratch audit log."""
    env = dict(os.environ)
    for name in ("MEZMO_GOVERNANCE_URL", "MEZMO_GOVERNANCE_TOKEN"):
        if not (env.get(name) or "").strip():
            raise Failure(f"set {name} in this shell, as for the hooks")
    if not (env.get("MEZMO_GOVERNANCE_TIMEOUT_SECONDS") or "").strip():
        # A call held for approval then reports pending after 15 s, not 590.
        env["MEZMO_GOVERNANCE_TIMEOUT_SECONDS"] = "15"
    env["MEZMO_GOVERNANCE_AUDIT_LOG"] = str(scratch / "audit.jsonl")
    env["MEZMO_GOVERNANCE_STATE_DIR"] = str(scratch / "state")
    return env


def strings(value: Any) -> list:
    if isinstance(value, str):
        return [value]
    if isinstance(value, dict):
        value = list(value.values())
    return [text for item in value for text in strings(item)] if isinstance(value, list) else []


DENIED = "denied this exact tool call: "


def live_outcome(result: subprocess.CompletedProcess) -> tuple:
    """('allowed' | 'denied' | 'pending approval' | 'error', detail) from one hook or plugin run."""
    texts = []
    for line in (result.stdout + "\n" + result.stderr).splitlines():
        try:
            texts += strings(json.loads(line))
        except ValueError:
            texts.append(line)
    if any("approval still pending" in text for text in texts):
        return "pending approval", ""
    for text in texts:
        if DENIED in text:
            return "denied", doctor.hook.error_detail(text.split(DENIED, 1)[1])
    if result.returncode == 0 and not doctor.denied(result):
        return "allowed", ""
    return "error", doctor.hook.error_detail(next((text for text in reversed(texts) if text.strip()), "no output"))


def smoke(names: list, live: bool = False) -> int:
    scratch = Path(tempfile.mkdtemp(prefix="mezmo-governance-smoke-"))
    server = None
    seconds = 30.0
    try:
        if live:
            try:
                env = live_env(scratch)
            except Failure as error:
                print(f"FAIL  smoke --live: {error}", flush=True)
                return 1
            limit = env["MEZMO_GOVERNANCE_TIMEOUT_SECONDS"]
            try:
                seconds = float(limit) + 15
            except ValueError:
                pass
            print(f"live  Mezmo at {doctor.shown_url(env['MEZMO_GOVERNANCE_URL'])}; the policy decides each call, a held one waits {limit} s", flush=True)
        else:
            server = ThreadingHTTPServer(("127.0.0.1", 0), mock_governance.make_handler(SHELL_TOOLS, []))
            threading.Thread(target=server.serve_forever, daemon=True).start()
            url = f"http://127.0.0.1:{server.server_port}{mock_governance.PATH}"
            env = {key: value for key, value in os.environ.items() if not key.startswith(("MEZMO_GOVERNANCE_", "CLAUDE_PLUGIN_OPTION_"))}
            env.update(
                {
                    "MEZMO_GOVERNANCE_URL": url,
                    "MEZMO_GOVERNANCE_TOKEN": SMOKE_KEY,
                    "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "10",
                    # Keep the smoke calls out of the user's audit log and the plugin's status display.
                    "MEZMO_GOVERNANCE_AUDIT_LOG": str(scratch / "audit.jsonl"),
                    "MEZMO_GOVERNANCE_STATE_DIR": str(scratch / "state"),
                }
            )
            print(f"mock  Mezmo stand-in on {url}, denies {', '.join(SHELL_TOOLS)}", flush=True)
        return smoke_calls(names, env, live, seconds)
    finally:
        if server is not None:
            server.shutdown()
            server.server_close()
        shutil.rmtree(scratch, ignore_errors=True)


def smoke_calls(names: list, env: dict, live: bool, seconds: float) -> int:
    python = shutil.which("python3") or sys.executable
    failed = False
    tried = 0
    for name in names:
        agent = AGENTS[name]
        try:
            smokes = agent.check(Quiet(), doctor.binary_version(shutil.which(agent.binary) or agent.binary))
        except doctor.Unknown as error:
            print(f"FAIL  {name}: unknown: {error}", flush=True)
            failed = True
            continue
        if smokes is None:
            print(f"skip  {name}: not installed", flush=True)
            continue
        calls = []
        for item in {(smoke.path, smoke.args): smoke for smoke in smokes}.values():
            shell = item.event.get("tool_name") or item.event.get("toolName")

            def python_hook(tool: str, item: doctor.Smoke = item, shell: str = shell) -> subprocess.CompletedProcess:
                event = fresh(item.event, None if tool == shell else tool)
                return run([python, str(item.path), *item.args], json.dumps(event), env, item.path, seconds)

            calls.append((item.path, shell, python_hook))
        if not smokes:
            files = plugin_files(name)
            if not files:
                print(f"FAIL  {name}: no plugin entry file found; reinstall with ./mezmo-governance install {name}", flush=True)
                failed = True
                continue
            node = shutil.which("node")
            if node is None:
                print(f"FAIL  {name}: node is not on PATH, so the plugin could not be run", flush=True)
                failed = True
                continue
            try:
                options = json.dumps(plugin_options(name, env, live))
            except (doctor.Unknown, Failure) as error:
                print(f"FAIL  {name}: {error}", flush=True)
                failed = True
                continue
            for path in files:

                def plugin(tool: str, path: Path = path) -> subprocess.CompletedProcess:
                    driver = Path(__file__).resolve().with_name("smoke_plugin.ts")
                    return run([node, str(driver), name, str(path), tool], options, env, path, seconds)

                calls.append((path, "bash", plugin))
        for path, shell, call in calls:
            tried += 1
            if live:
                # The policy decides, so each call is reported, not judged; only a call with no decision fails.
                for tool in [tool for tool in (agent.shell, agent.read) if tool]:
                    outcome, detail = live_outcome(call(tool))
                    if outcome == "error":
                        failed = True
                        print(f"FAIL  {name}: {tool} reached no decision: {detail}", flush=True)
                    else:
                        print(f"live  {name}: {tool} {outcome}{': ' + detail if detail else ''} ({shown(path)})", flush=True)
                continue
            for tool, label, want in ((shell, "denied", True), ("Read", "approved", False)):
                result = call(tool)
                said = result.stdout + result.stderr
                if want:
                    ok = doctor.denied(result) and "mock policy" in said
                else:
                    ok = result.returncode == 0 and not doctor.denied(result)
                if ok:
                    print(f"ok    {name}: {tool} was {label} by the mock ({shown(path)})", flush=True)
                else:
                    failed = True
                    detail = doctor.hook.error_detail(said.strip().splitlines()[-1] if said.strip() else "no output")
                    print(f"FAIL  {name}: {tool} should be {label}, exit {result.returncode}: {detail}", flush=True)
    if not tried and not failed:
        print("FAIL  smoke: no installed hook to run; install one with ./mezmo-governance install <agent>", flush=True)
        return 1
    return 1 if failed else 0


def run(command: list, stdin: str, env: dict, script: Path, seconds: float = 30.0) -> subprocess.CompletedProcess:
    if not script.is_file():
        return subprocess.CompletedProcess(command, 1, "", f"{shown(script)} is missing")
    try:
        return subprocess.run(command, input=stdin, env=env, capture_output=True, text=True, timeout=seconds, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 1, "", f"no answer within {seconds:g} s")


# --- policy: read the Mezmo policies, add starter rules -----------------------


def admin_key() -> str:
    key = (os.environ.get(ADMIN_KEY) or "").strip()
    if not key and sys.stdin.isatty():
        key = getpass.getpass(f"{ADMIN_KEY} (a Mezmo key that may write policies; not shown): ").strip()
    if not key:
        raise Failure(
            f"set {ADMIN_KEY} to a Mezmo key that may read and write Governance policies."
            " policy uses it and never MEZMO_GOVERNANCE_TOKEN: every governed agent can read that token,"
            " and with policy rights an agent could remove its own rules"
        )
    return key


def policy_base(url: Optional[str]) -> str:
    """--url, else MEZMO_GOVERNANCE_URL with the authorize path swapped for the policy API's, else Mezmo's."""
    base = url or POLICY_URL
    hook_url = (os.environ.get("MEZMO_GOVERNANCE_URL") or "").strip()
    if not url and hook_url:
        parsed = urlparse(hook_url)
        if not parsed.path.endswith(mock_governance.PATH):
            raise Failure(f"MEZMO_GOVERNANCE_URL {doctor.shown_url(hook_url)} does not end in {mock_governance.PATH}; pass --url BASE")
        path = parsed.path[: -len(mock_governance.PATH)] + mock_governance.API
        base = urlunparse(parsed._replace(path=path, query="", fragment=""))
    base = base.rstrip("/")
    problem = doctor.url_problem(base)
    if problem:
        raise Failure(f"{doctor.shown_url(base)} {problem}")
    return base


def api(what: str, key: str, method: str, url: str, body: Any = None) -> tuple:
    """(status, JSON body) of one policy API call. No answer raises Failure: the result is unknown."""
    data = None if body is None else json.dumps(body).encode()
    headers = {"Authorization": key if key.lower().startswith(("token ", "bearer ")) else f"Token {key}", "Accept": "application/json"}
    if data is not None:
        headers["Content-Type"] = "application/json"
    path = urlparse(url).path
    try:
        with build_opener(doctor.hook.RefuseRedirect).open(Request(url, data, headers, method=method), timeout=doctor.REQUEST_SECONDS) as response:
            status, raw = response.status, response.read()
    except HTTPError as error:
        status, raw = error.code, error.read()
    except (OSError, ValueError) as error:
        raise Failure(f"{what}: {method} {path} got no answer ({error}); its result is unknown") from error
    try:
        parsed = json.loads(raw) if raw.strip() else {}
    except ValueError:
        parsed = {}
    return status, as_dict(parsed)


def answer(call: tuple, want: int, what: str) -> dict:
    status, body = call
    if status != want:
        code = f" {body['code']}" if body.get("code") else ""
        raise Failure(f"{what}: HTTP {status}{code}: {body.get('message') or body.get('error') or 'no detail'}")
    return body


def listed(base: str, key: str, path: str, what: str) -> list:
    """Every item of a list, following meta.page.next. A link it cannot follow leaves the list unknown."""
    items = []
    url = f"{base}{path}?limit={PAGE_LIMIT}" if path == "/policy" else base + path
    seen: set = set()
    while True:
        body = answer(api(what, key, "GET", url), 200, what)
        items += as_list(body.get("data"))
        link = as_dict(as_dict(body.get("meta")).get("page")).get("next")
        if link is None or link == "":
            return items
        after = parse_qs(urlparse(link).query).get("after") if isinstance(link, str) else None
        if not after:
            raise Failure(f"{what}: the next page link {link!r} has no after cursor, so the list is incomplete")
        if after[0] in seen:
            raise Failure(f"{what}: the next page link repeats cursor {after[0]}, so the list is incomplete")
        seen.add(after[0])
        url = f"{base}{path}?limit={PAGE_LIMIT}&after={quote(after[0])}"


# Mezmo decides from every matching rule, the most restrictive first, in no list order.
RESTRICTIVE = ("allow", "approval_required", "deny")


def matches(condition: Any, value: str) -> bool:
    """Whether a tool or namespace condition matches; no condition matches every value."""
    if not condition:
        return True
    condition = as_dict(condition)
    op, want = condition.get("op", "eq"), condition.get("value")
    names = want if isinstance(want, list) else None
    return (
        (op == "eq" and want == value)
        or (op == "neq" and isinstance(want, str) and want != value)
        or (op == "in" and names is not None and value in names)
        or (op == "nin" and names is not None and value not in names)
    )


def covering(rules: list, namespace: str, tool: str) -> Optional[dict]:
    """The rule that decides every call of the tool in the namespace: the most restrictive match without argument checks.

    A rule with argument checks decides only some calls, so it covers none.
    """
    def rank(rule: dict) -> int:
        recourse = as_dict(rule).get("recourse") or as_dict(as_dict(rule).get("definition")).get("recourse")
        # A recourse it does not know counts as the most restrictive, so it never hides a deny.
        return RESTRICTIVE.index(recourse) if recourse in RESTRICTIVE else len(RESTRICTIVE)

    found = []
    for rule in rules:
        definition = as_dict(as_dict(rule).get("definition"))
        if matches(definition.get("tool"), tool) and matches(definition.get("namespace"), namespace) and not definition.get("args"):
            found.append(rule)
    return max(found, key=rank, default=None)


def condition_text(condition: Any) -> str:
    condition = as_dict(condition)
    value = condition.get("value")
    return f"{condition.get('op', 'eq')} {', '.join(map(str, value)) if isinstance(value, list) else value}"


def rule_text(definition: dict) -> str:
    """'<recourse> <namespace or *> tool <op> <value>: <message>', from an authored rule."""
    space = as_dict(definition.get("namespace"))
    shown_space = "*" if not space else str(space.get("value")) if space.get("op", "eq") == "eq" else condition_text(space)
    tool = condition_text(definition["tool"]) if definition.get("tool") else "*"
    checks = f" args {len(definition['args'])}" if definition.get("args") else ""
    return f"{definition.get('recourse')} {shown_space} tool {tool}{checks}: {definition.get('error_message') or '-'}"


def kept_text(rule: Any) -> str:
    return f"{rule_text(as_dict(as_dict(rule).get('definition')))} (rule_id {as_dict(rule).get('rule_id')})"


def mezmo_agent(records: list, chosen: Optional[str]) -> Optional[dict]:
    """The Mezmo agent every installed agent maps to: --agent, else the only one; None maps by Runtime Name.

    Mezmo picks the policy from the token's service account, and every adapter may send one agent's token.
    """
    if chosen:
        found = [record for record in map(as_dict, records) if chosen in (record.get("name"), record.get("agent_id"))]
        if not found:
            raise Failure(f"no Mezmo agent has the name or agent_id {chosen}")
        if len(found) > 1:
            raise Failure(f"{len(found)} Mezmo agents are named {chosen}; pass its agent_id")
        return found[0]
    return as_dict(records[0]) if len(records) == 1 else None


def assigned(name: str, records: list, policies: list, fixed: Optional[dict]) -> tuple:
    """The policies the agent's token gets, from the fixed Mezmo agent or by Runtime Name, or ([], why not)."""
    matched = [fixed] if fixed else [record for record in map(as_dict, records) if record.get("runtime_name") == name]
    if not matched:
        return [], f"no Mezmo agent has Runtime Name {name}; pass --agent <name or agent_id>"
    ids = {str(record.get("policy_id")) for record in matched}
    found = [policy for policy in policies if str(as_dict(policy).get("policy_id")) in ids]
    return found, "" if found else f"Mezmo agent {matched[0].get('name')} has no policy that this key can read"


def starter_rules(rules: list, names: list) -> list:
    """The starter rules, each listing the agents' tools that no rule covers yet, in those agents' namespaces only."""
    added = []
    for kind, fields in STARTER.items():
        agents = [AGENTS[name] for name in names if getattr(AGENTS[name], kind)]
        agents = [agent for agent in agents if not covering(rules, agent.namespace, getattr(agent, kind))]
        if agents:
            tools = list(dict.fromkeys(getattr(agent, kind) for agent in agents))
            spaces = list(dict.fromkeys(agent.namespace for agent in agents))
            added.append({**fields, "tool": {"op": "in", "value": tools}, "namespace": {"op": "in", "value": spaces}})
    return added


def draft_of(policy: dict) -> str:
    draft = policy.get("draft")
    return str((as_dict(draft).get("draft_id") if isinstance(draft, dict) else draft) or "")


def policy_show(records: list, policies: list, fixed: Optional[dict]) -> int:
    for record in map(as_dict, records):
        print(f"agent   {record.get('name')}  runtime {record.get('runtime_name')}  policy {record.get('policy_id') or '-'}", flush=True)
    if not records:
        print("agent   none in Mezmo", flush=True)
    for policy in map(as_dict, policies):
        draft = f", draft {draft_of(policy)} open" if draft_of(policy) else ""
        print(f"policy  {policy.get('name')} ({policy.get('policy_id')}){draft}", flush=True)
        for rule in as_list(policy.get("rules")):
            print(f"  rule     {kept_text(rule)}", flush=True)
        default = as_dict(policy.get("default_action"))
        print(f"  default  {default.get('recourse')}: {default.get('error_message') or '-'}", flush=True)
    for name in [name for name in AGENTS if installed_at(name)]:
        agent = AGENTS[name]
        found, why = assigned(name, records, policies, fixed)
        if not found:
            print(f"cover   {name}: {why}", flush=True)
        for policy in found:
            parts = []
            for kind in [kind for kind in STARTER if getattr(agent, kind)]:
                tool = getattr(agent, kind)
                rule = covering(as_list(policy.get("rules")), agent.namespace, tool)
                parts.append(f"{kind} {tool} " + (f"covered ({rule.get('recourse')}, rule_id {rule.get('rule_id')})" if rule else "not covered"))
            print(f"cover   {name} in {policy.get('name')}: {'; '.join(parts)}", flush=True)
    return 0


def write_rules(base: str, key: str, policy: dict, names: list) -> tuple:
    """Open a draft, write the kept rules plus the starter rules, accept it; on a failure, delete the draft.

    Returns (added, kept). A run stopped between steps leaves the draft open, and the next run stops on it.
    """
    root = f"{base}/policy/{quote(str(policy.get('policy_id')), safe='')}"
    if draft_of(policy):
        raise Failure(f"draft {draft_of(policy)} is already open on this policy; finish or delete it in Mezmo, then run this again")
    status, body = api("open a draft", key, "POST", f"{root}/draft", {})
    if status == 200:
        raise Failure(
            f"Mezmo handed back draft {as_dict(body.get('data')).get('draft_id')}, which was already open;"
            " finish or delete it in Mezmo, then run this again"
        )
    draft = as_dict(answer((status, body), 201, "open a draft").get("data"))
    draft_id = str(draft.get("draft_id") or "")
    if not draft_id:
        raise Failure("Mezmo opened a draft but sent no draft_id; finish or delete it in Mezmo")
    try:
        rules = as_list(draft.get("rules"))
        kept = [{**as_dict(rule.get("definition")), "rule_id": rule.get("rule_id")} for rule in map(as_dict, rules)]
        added = starter_rules(rules, names)
        content = {"name": draft.get("name"), "default_action": draft.get("default_action"), "rules": kept + added}
        # Mezmo clears a description the write leaves out.
        if draft.get("description"):
            content["description"] = draft["description"]
        answer(api("write the draft", key, "PUT", f"{root}/draft/{quote(draft_id, safe='')}", content), 200, "write the draft")
        answer(api("accept the draft", key, "PATCH", root, {"draft_id": draft_id}), 200, "accept the draft")
    except Failure as error:
        try:
            status, _ = api("delete the draft", key, "DELETE", f"{root}/draft/{quote(draft_id, safe='')}")
            undone = "deleted the draft" if status in (200, 204) else f"could not delete draft {draft_id} (HTTP {status}); delete it in Mezmo"
        except Failure as cause:
            undone = f"could not delete draft {draft_id} ({cause}); delete it in Mezmo"
        raise Failure(f"{error}; {undone}. Run ./mezmo-governance policy show to see the rules now") from error
    return added, kept


def policy_apply(base: str, key: str, records: list, policies: list, fixed: Optional[dict], names: list, yes: bool) -> int:
    failed = False
    plans: dict = {}
    for name in names:
        found, why = assigned(name, records, policies, fixed)
        if not found:
            print(f"FAIL  {name}: {why}", flush=True)
            failed = True
        for policy in found:
            plans.setdefault(str(policy.get("policy_id")), (policy, []))[1].append(name)
    planned = False
    for policy, chosen in plans.values():
        rules = as_list(policy.get("rules"))
        added = starter_rules(rules, chosen)
        print(f"policy  {policy.get('name')} ({policy.get('policy_id')}) for {', '.join(chosen)}:", flush=True)
        for rule in rules:
            print(f"  keep     {kept_text(rule)}", flush=True)
        for rule in added:
            print(f"  {'add' if yes else 'would add':<8} {rule_text(rule)}", flush=True)
        if not added:
            print(f"ok    {policy.get('name')}: a rule covers each shell and read tool, nothing to add", flush=True)
            continue
        planned = True
        if not yes:
            continue
        try:
            added, kept = write_rules(base, key, policy, chosen)
        except Failure as error:
            print(f"FAIL  {policy.get('name')}: {error}", flush=True)
            failed = True
            continue
        print(f"ok    {policy.get('name')}: {len(added)} rules added, {len(kept)} kept", flush=True)
    if planned and not yes:
        print("Nothing changed. Run again with --yes to add these rules through a Mezmo draft.", flush=True)
    return 1 if failed else 0


def policy_args(args: list) -> tuple:
    action = args.pop(0) if args else ""
    if action not in ("show", "apply"):
        raise Usage("policy: say show or apply")
    values: dict = {}
    for option in ("--url", "--agent"):
        if option in args:
            at = args.index(option)
            if at + 1 == len(args):
                raise Usage(f"policy: {option} needs a value")
            values[option] = args.pop(at + 1)
            args.pop(at)
    flags = [arg for arg in args if arg.startswith("-")]
    names = [arg for arg in args if not arg.startswith("-")]
    unknown = set(flags) - ({"--yes"} if action == "apply" else set())
    if unknown:
        raise Usage(f"policy {action}: unknown option {' '.join(sorted(unknown))}")
    if action == "show" and names:
        raise Usage("policy show: takes no agent names")
    pick(names, False, "policy")
    return action, names, "--yes" in flags, values.get("--url"), values.get("--agent")


def policy(action: str, names: list, yes: bool, url: Optional[str], chosen_agent: Optional[str]) -> int:
    try:
        base = policy_base(url)
        key = admin_key()
        records = listed(base, key, "/agent", "read agents")
        policies = listed(base, key, "/policy", "read policies")
        fixed = mezmo_agent(records, chosen_agent)
        if action == "show":
            return policy_show(records, policies, fixed)
        chosen = names or [name for name in AGENTS if installed_at(name)]
        if not chosen:
            print("ok    policy apply: no agent is installed; name the agents to add rules for", flush=True)
            return 0
        return policy_apply(base, key, records, policies, fixed, chosen, yes)
    except Failure as error:
        print(f"FAIL  policy {action}: {error}", flush=True)
        return 1


FLAGS = {
    "agents": set(),
    "status": set(),
    "install": {"--all", "--dry-run"},
    "uninstall": {"--all", "--dry-run"},
    "doctor": {"--probe"},
    "smoke": {"--live"},
}


def main(argv: Optional[list] = None) -> int:
    args = list(sys.argv[1:] if argv is None else argv)
    verb = args.pop(0) if args else "help"
    if verb in ("help", "-h", "--help"):
        print(HELP, end="")
        return 0
    if verb == "mock":
        return mock_governance.main(args)
    try:
        if verb == "policy":
            parsed = policy_args(args)
        else:
            if verb not in FLAGS:
                raise Usage(f"unknown command: {verb}")
            flags = [arg for arg in args if arg.startswith("-")]
            names = [arg for arg in args if not arg.startswith("-")]
            if set(flags) - FLAGS[verb]:
                raise Usage(f"{verb}: unknown option {' '.join(sorted(set(flags) - FLAGS[verb]))}")
            if verb in ("install", "uninstall") and not names and "--all" not in flags:
                raise Usage(f"{verb}: name at least one agent, or pass --all")
            chosen = pick(names, "--all" in flags, verb)
    except Usage as error:
        print(f"mezmo-governance: {error}\n", file=sys.stderr)
        print(HELP, end="", file=sys.stderr)
        return 2
    if verb == "policy":
        return policy(*parsed)
    if verb in ("agents", "status"):
        return agents(chosen)
    if verb == "doctor":
        return doctor.run(tuple(AGENTS[name] for name in chosen), "--probe" in flags)
    if verb == "smoke":
        return smoke(chosen, "--live" in flags)
    if not chosen:
        print(f"ok    {verb} --all: no agent {'binary on PATH' if verb == 'install' else 'is installed'}", flush=True)
        return 0
    return change(verb, chosen, "--dry-run" in flags)


if __name__ == "__main__":
    sys.exit(main())
