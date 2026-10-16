#!/usr/bin/env python3
"""The `./mezmo-governance` command: install, check and try the plugins.

Run it through the `mezmo-governance` script at the repository root, which
finds Python 3.9 or later. It never writes the token into any file.
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
import time
import uuid
from http.server import ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable, NamedTuple, Optional

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
SHELL_TOOLS = ["Bash", "Shell", "run_shell_command", "bash", "shell"]
SMOKE_KEY = "smoke-test"
CLI_SECONDS = 300

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
  smoke [agent...]         run each installed hook against its own mock: one call denied, one approved

Agents: {', '.join(agent.readme for agent in doctor.AGENTS)}
  install --all picks every agent whose binary is on PATH; uninstall --all picks every installed agent.

Examples:
  ./mezmo-governance install claude-code --dry-run
  ./mezmo-governance install claude-code codex
  ./mezmo-governance doctor --probe
  ./mezmo-governance smoke
  ./mezmo-governance mock --deny Bash

Settings:
  MEZMO_GOVERNANCE_URL and MEZMO_GOVERNANCE_TOKEN come from the shell that starts the agent.
  The Claude Code plugin can keep its own copy (/plugin configure); OpenCode 2.x reads plugin
  options in opencode.json, with the key in a file. Install never writes the key.
  Config folders follow each agent's override: CLAUDE_CONFIG_DIR, CODEX_HOME, GEMINI_CLI_HOME,
  COPILOT_HOME, GOOSE_PATH_ROOT, XDG_CONFIG_HOME (OpenCode), PI_CODING_AGENT_DIR, else HOME.

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
    """Write data in the file's own indent and line endings, so the lines install did not change stay the same."""
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
    os.replace(partial, path)


def load_object(path: Path) -> dict:
    data = read_json(path)
    if data is None:
        return {}
    if not isinstance(data, dict):
        raise doctor.Unknown(f"{shown(path)} is not a JSON object")
    return data


def copy_file(source: Path, target: Path) -> None:
    target.parent.mkdir(parents=True, exist_ok=True)
    shutil.copy2(source, target)


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


def run_cli(*args: str) -> None:
    print(f"      $ {' '.join((Path(args[0]).name,) + args[1:])}", flush=True)
    try:
        result = subprocess.run(list(args), timeout=CLI_SECONDS, check=False)
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


def file_hook_agent(adapter: str, folder: Callable[[], Path], event: str, next_step: str, leftover: tuple = ()) -> Installer:
    """Codex and Cursor: a hook script plus entries merged into hooks.json."""

    def installed() -> Optional[str]:
        path = folder() / "hooks.json"
        found = doctor.mezmo_handlers(as_dict(load_object(path).get("hooks")).get(event))
        return shown(path) if found else None

    def install() -> list:
        target = folder() / "hooks" / HOOK_FILE
        steps = [] if installed() else [hook_copy(REPO / adapter / HOOK_FILE, target)]
        data = example(adapter, "hooks.example.json", folder(), doctor.home() / f".{adapter}")
        return steps + merge_steps(folder() / "hooks.json", data, event)

    def uninstall() -> list:
        return unmerge_steps(folder() / "hooks.json", leftover=leftover) + hook_removal(folder() / "hooks" / HOOK_FILE, folder())

    return Installer(installed, install, uninstall, next_step)


def copilot() -> Installer:
    def files() -> tuple:
        hooks = doctor.copilot_dir() / "hooks"
        return hooks / HOOK_FILE, hooks / "mezmo-governance.json"

    def installed() -> Optional[str]:
        script, config = files()
        return shown(config) if script.is_file() and config.is_file() else None

    def install() -> list:
        if installed():
            return []
        script, config = files()
        default = doctor.home() / ".copilot"
        data = example("copilot", "hooks.example.json", doctor.copilot_dir(), default)
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
            return []
        # hooks.json goes last: until it is there, the plugin counts as not installed.
        return [hook_copy(REPO / "goose" / name, doctor.goose_dir() / name) for name in files]

    def uninstall() -> list:
        folder = doctor.goose_dir()
        return [step for name in reversed(files) for step in hook_removal(folder / name, folder.parent)]

    return Installer(installed, install, uninstall, "")


def opencode() -> Installer:
    def paths() -> tuple:
        config = doctor.opencode_dir()
        key_file = doctor.env_dir("XDG_CONFIG_HOME", doctor.home() / ".config") / "mezmo" / "governance-key"
        return config / "opencode.json", config / "mezmo-governance", key_file

    def ours(item: Any) -> bool:
        package = item if isinstance(item, str) else as_dict(item).get("package")
        return isinstance(package, str) and Path(package).name == "mezmo-governance"

    def installed() -> Optional[str]:
        config, folder, _ = paths()
        entries = [item for item in as_list(load_object(config).get("plugins")) if ours(item)]
        return shown(config) if entries and (folder / "index.ts").is_file() else None

    def install() -> list:
        if installed():
            return []
        version = doctor.binary_version(shutil.which("opencode") or "opencode")
        if version is not None and version[0] < 2:
            raise Failure("install does not handle OpenCode 1.x; follow 'Install on 1.x' in opencode/README.md")
        config, folder, key_file = paths()
        url = os.environ.get("MEZMO_GOVERNANCE_URL") or doctor.DEFAULT_URL
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
            if key.startswith("mezmo-governance@") and any(as_dict(item).get("scope") == "user" for item in as_list(installs))
        ]

    def installed() -> Optional[str]:
        keys = plugin_keys()
        settings = load_object(files()[0])
        if keys:
            return f"plugin {keys[0]}"
        return shown(files()[0]) if doctor.mezmo_handlers(as_dict(settings.get("hooks")).get("PreToolUse")) else None

    def install() -> list:
        if installed():
            return []
        if "mezmo" in load_object(files()[2]):
            args = ("plugin", "install", "mezmo-governance@mezmo", "-s", "user")
        else:
            args = ("plugin", "install", "mezmo-governance", "--marketplace", str(REPO), "-s", "user")
        return [Step(f"claude {' '.join(args)}", files(), lambda: run_cli(need("claude"), *args))]

    def uninstall() -> list:
        steps = [
            Step(f"claude plugin uninstall {key} -s user", files(), lambda key=key: run_cli(need("claude"), "plugin", "uninstall", key, "-s", "user"))
            for key in plugin_keys()
        ]
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
        "In Claude Code run /plugin configure mezmo-governance@mezmo to keep the URL and key in the system credential store,"
        " or export them in the shell that starts claude.",
    )


def gemini_cli() -> Installer:
    def extension() -> Path:
        return doctor.gemini_dir() / "extensions" / "mezmo-governance"

    def touched() -> tuple:
        return doctor.gemini_dir() / "trustedFolders.json", doctor.gemini_dir() / "extensions" / "extension-enablement.json"

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
        return [Step(f"gemini {' '.join(args)} (it asks to trust that folder)", touched(), lambda: run_cli(need("gemini"), *args))]

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


INSTALLERS = {
    "claude-code": claude_code(),
    "codex": file_hook_agent(
        "codex", doctor.codex_dir, "PreToolUse",
        "Start codex, run /hooks, and trust the Mezmo hook entries: Codex skips a hook until you trust it.",
    ),
    "cursor": file_hook_agent("cursor", doctor.cursor_dir, "preToolUse", "", leftover=("version",)),
    "gemini-cli": gemini_cli(),
    "copilot": copilot(),
    "goose": goose(),
    "opencode": opencode(),
    "pi": pi(),
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
        if any(status == "FAIL" and "lets the tool run" in detail for status, detail in report.lines):
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


def smoke(names: list) -> int:
    server = ThreadingHTTPServer(("127.0.0.1", 0), mock_governance.make_handler(SHELL_TOOLS, []))
    threading.Thread(target=server.serve_forever, daemon=True).start()
    url = f"http://127.0.0.1:{server.server_port}{mock_governance.PATH}"
    scratch = Path(tempfile.mkdtemp(prefix="mezmo-governance-smoke-"))
    env = {key: value for key, value in os.environ.items() if not key.startswith(("MEZMO_GOVERNANCE_", "CLAUDE_PLUGIN_OPTION_"))}
    env.update(
        {
            "MEZMO_GOVERNANCE_URL": url,
            "MEZMO_GOVERNANCE_TOKEN": SMOKE_KEY,
            "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "10",
            # Keep the smoke calls out of the user's audit log.
            "MEZMO_GOVERNANCE_AUDIT_LOG": str(scratch / "audit.jsonl"),
        }
    )
    python = shutil.which("python3") or sys.executable
    print(f"mock  Mezmo stand-in on {url}, denies {', '.join(SHELL_TOOLS)}", flush=True)
    failed = False
    tried = 0
    try:
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
                    return run([python, str(item.path), *item.args], json.dumps(event), env, item.path)

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
                for path in files:

                    def plugin(tool: str, path: Path = path) -> subprocess.CompletedProcess:
                        driver = Path(__file__).resolve().with_name("smoke_plugin.ts")
                        return run([node, str(driver), name, str(path), tool], "", env, path)

                    calls.append((path, "bash", plugin))
            for path, shell, call in calls:
                tried += 1
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
    finally:
        server.shutdown()
        server.server_close()
        shutil.rmtree(scratch, ignore_errors=True)
    if not tried and not failed:
        print("FAIL  smoke: no installed hook to run; install one with ./mezmo-governance install <agent>", flush=True)
        return 1
    return 1 if failed else 0


def run(command: list, stdin: str, env: dict, script: Path) -> subprocess.CompletedProcess:
    if not script.is_file():
        return subprocess.CompletedProcess(command, 1, "", f"{shown(script)} is missing")
    try:
        return subprocess.run(command, input=stdin, env=env, capture_output=True, text=True, timeout=30, check=False)
    except subprocess.TimeoutExpired:
        return subprocess.CompletedProcess(command, 1, "", "no answer within 30 s")


FLAGS = {
    "agents": set(),
    "status": set(),
    "install": {"--all", "--dry-run"},
    "uninstall": {"--all", "--dry-run"},
    "doctor": {"--probe"},
    "smoke": set(),
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
    if verb in ("agents", "status"):
        return agents(chosen)
    if verb == "doctor":
        return doctor.run(tuple(AGENTS[name] for name in chosen), "--probe" in flags)
    if verb == "smoke":
        return smoke(chosen)
    if not chosen:
        print(f"ok    {verb} --all: no agent {'binary on PATH' if verb == 'install' else 'is installed'}", flush=True)
        return 0
    return change(verb, chosen, "--dry-run" in flags)


if __name__ == "__main__":
    sys.exit(main())
