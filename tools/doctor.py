#!/usr/bin/env python3
"""Check a Mezmo Governance setup on this machine.

    python3 tools/doctor.py           # settings, Mezmo, and each coding agent
    python3 tools/doctor.py --probe   # also send Mezmo one test call

It reads the user-level agent configs and never changes them. It prints one
line per check, `ok`, `warn`, `FAIL` or `skip`, and exits 1 when a check
fails. It never prints the access key.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, NamedTuple, Optional
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, build_opener

REPO = Path(__file__).resolve().parent.parent
HOOK_FILE = "mezmo_governance_hook.py"
DEFAULT_URL = "https://api.mezmo.com/governance/workflow/authorize"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
REQUEST_SECONDS = 15.0
# Nothing listens on port 9, so a hook pointed here must deny.
UNREACHABLE_URL = "http://127.0.0.1:9/governance/workflow/authorize"
LIBRARIES = "LangChain, OpenAI Agents SDK, Claude Agent SDK and Mastra"


def load_hook() -> Any:
    """The Claude Code hook: its settings rules and HTTP client are the hooks' own."""
    spec = importlib.util.spec_from_file_location("mezmo_governance_hook", REPO / "claude-code" / HOOK_FILE)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


hook = load_hook()


class Unknown(Exception):
    """A config that could not be read: the answer is unknown, not "absent"."""


class Smoke(NamedTuple):
    agent: str
    path: Path
    event: dict
    args: tuple = ()


class Report:
    def __init__(self) -> None:
        self.failed = False
        self.secrets: list = []
        try:
            key = hook.configured("MEZMO_GOVERNANCE_ACCESS_KEY")
        except hook.HookContractError:
            return
        # Redact the key with and without its Token or Bearer prefix.
        bare = re.sub(r"^(token|bearer) +", "", key, flags=re.I)
        self.secrets = [secret for secret in (key, bare) if len(secret) >= 4]

    def line(self, status: str, subject: str, detail: str, fix: str = "") -> None:
        text = f"{status:<4}  {subject}: {detail}"
        if fix:
            text += f" Fix: {fix}"
        for secret in self.secrets:
            text = text.replace(secret, "<redacted>")
        print(text, flush=True)
        if status == "FAIL":
            self.failed = True


def home() -> Path:
    return Path(os.path.expanduser("~"))


# Each agent's user config folder, with the override each agent documents.
CONFIG_OVERRIDES = (
    "CLAUDE_CONFIG_DIR",
    "CODEX_HOME",
    "GEMINI_CLI_HOME",
    "COPILOT_HOME",
    "GOOSE_PATH_ROOT",
    "XDG_CONFIG_HOME",
    "PI_CODING_AGENT_DIR",
)


def env_dir(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(os.path.expanduser(value)) if value else default


def claude_dir() -> Path:
    return env_dir("CLAUDE_CONFIG_DIR", home() / ".claude")


def codex_dir() -> Path:
    return env_dir("CODEX_HOME", home() / ".codex")


def cursor_dir() -> Path:
    return home() / ".cursor"


def gemini_dir() -> Path:
    return env_dir("GEMINI_CLI_HOME", home()) / ".gemini"


def copilot_dir() -> Path:
    return env_dir("COPILOT_HOME", home() / ".copilot")


def goose_dir() -> Path:
    return env_dir("GOOSE_PATH_ROOT", home()) / ".agents" / "plugins" / "mezmo-governance"


def opencode_dir() -> Path:
    return env_dir("XDG_CONFIG_HOME", home() / ".config") / "opencode"


def pi_dir() -> Path:
    return env_dir("PI_CODING_AGENT_DIR", home() / ".pi" / "agent")


def shown(path: Path) -> str:
    text = str(path)
    root = str(home())
    return "~" + text[len(root):] if text.startswith(root + os.sep) else text


def as_dict(value: Any) -> dict:
    return value if isinstance(value, dict) else {}


def as_list(value: Any) -> list:
    return value if isinstance(value, list) else []


def read_text(path: Path) -> Optional[str]:
    if not path.exists():
        return None
    try:
        return path.read_text(encoding="utf-8")
    except OSError as error:
        raise Unknown(f"could not read {shown(path)}: {error}") from error


def read_json(path: Path) -> Any:
    text = read_text(path)
    if text is None:
        return None
    try:
        return json.loads(text)
    except ValueError as error:
        raise Unknown(f"could not parse {shown(path)}: {error}") from error


def toml_table(text: str, header: str) -> Optional[str]:
    """The body of `[header]`, enough for the flat tables Codex writes."""
    match = re.search(r"^\[" + re.escape(header) + r"\][ \t]*$(.*?)(?=^\[|\Z)", text, re.M | re.S)
    return match.group(1) if match else None


def mezmo_handlers(groups: Any, field: str = "command") -> list:
    """(group index, handler index, group, handler) for each entry that runs the hook."""
    found = []
    for g, group in enumerate(as_list(groups)):
        group = as_dict(group)
        nested = "hooks" in group
        for h, handler in enumerate(as_list(group["hooks"]) if nested else [group]):
            handler = as_dict(handler)
            if HOOK_FILE in str(handler.get(field, "")):
                found.append((g, h, group, handler))
    return found


def hook_path(command: str) -> Path:
    """The script a hook command runs, expanded as the shell would."""
    match = re.search(r"[^\s\"']*" + re.escape(HOOK_FILE), command)
    token = match.group(0) if match else HOOK_FILE
    fallback = "${MEZMO_GOVERNANCE_HOOK:-"
    if token.startswith(fallback):
        token = os.environ.get("MEZMO_GOVERNANCE_HOOK") or token[len(fallback):]
    return Path(os.path.expanduser(os.path.expandvars(token)))


def version_tuple(text: str) -> Optional[tuple]:
    match = re.search(r"\d+(?:\.\d+)+", text)
    return tuple(int(part) for part in match.group(0).split(".")) if match else None


def dotted(version: tuple) -> str:
    return ".".join(str(part) for part in version)


def binary_version(path: str) -> Optional[tuple]:
    try:
        result = subprocess.run(
            [path, "--version"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            timeout=20,
            check=False,
        )
    except (OSError, subprocess.SubprocessError):
        return None
    return version_tuple(result.stdout + " " + result.stderr)


def url_problem(url: str) -> Optional[str]:
    parsed = urlparse(url)
    if parsed.scheme == "https" and parsed.hostname:
        return None
    if parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS:
        return None
    if parsed.scheme == "http":
        return "uses http, so the key would cross the network unencrypted"
    return "is not an https URL"


def shown_url(url: str) -> str:
    parsed = urlparse(url)
    try:
        port = f":{parsed.port}" if parsed.port else ""
    except ValueError:
        port = ""
    return f"{parsed.scheme}://{parsed.hostname or ''}{port}{parsed.path}"


# --- settings and Mezmo -------------------------------------------------------


def check_settings(report: Report) -> bool:
    ready = True
    for name, fix in (
        ("MEZMO_GOVERNANCE_URL", f"export MEZMO_GOVERNANCE_URL={DEFAULT_URL}"),
        (
            "MEZMO_GOVERNANCE_ACCESS_KEY",
            'export MEZMO_GOVERNANCE_ACCESS_KEY="<agent access key>", copied from the agent in Mezmo',
        ),
    ):
        try:
            value = hook.configured(name)
        except hook.HookContractError:
            report.line(
                "FAIL",
                name,
                "not set",
                f"{fix}. The Claude Code plugin and OpenCode 2.x keep their own copy;"
                " the other agents and this check read the variable.",
            )
            ready = False
            continue
        source = name if (os.environ.get(name) or "").strip() else hook.PLUGIN_OPTIONS[name]
        if name == "MEZMO_GOVERNANCE_URL":
            problem = url_problem(value)
            if problem:
                report.line("FAIL", name, f"{shown_url(value)} {problem}", f"use {DEFAULT_URL}")
                ready = False
            else:
                local = " (local mock)" if urlparse(value).scheme == "http" else ""
                report.line("ok", name, f"{shown_url(value)}{local}, from {source}")
        else:
            report.line("ok", name, f"set, from {source} (not shown)")
    for name, read in (
        ("MEZMO_GOVERNANCE_TIMEOUT_SECONDS", hook.timeout_seconds),
        ("MEZMO_GOVERNANCE_POLL_INTERVAL_SECONDS", hook.poll_interval_seconds),
    ):
        try:
            seconds = read()
        except hook.HookContractError as error:
            report.line("FAIL", name, str(error), "unset it, or set a number of seconds above zero")
            continue
        raw = os.environ.get(name)
        if raw is None:
            report.line("ok", name, f"{seconds:g} s (default)")
        elif float(raw) != seconds:
            report.line("warn", name, f"{raw} is used as {seconds:g} s, the hooks' limit", f"set {seconds:g} or unset it")
        else:
            report.line("ok", name, f"{seconds:g} s")
    return ready


def get_without_key(url: str) -> int:
    request = Request(url, headers={"Accept": "application/json"}, method="GET")
    try:
        with build_opener(hook.RefuseRedirect).open(request, timeout=REQUEST_SECONDS) as response:
            return response.status
    except HTTPError as error:
        return error.code


def probe_payload() -> dict:
    """The hook's own request for a made-up MCP call, doctor:probe."""
    run = str(uuid.uuid4())
    return hook.approval_payload(
        {
            "hook_event_name": "PreToolUse",
            "session_id": f"doctor-{run}",
            "tool_use_id": f"probe-{run}",
            "tool_name": "mcp__doctor__probe",
            "tool_input": {},
        }
    )


def check_mezmo(report: Report, ready: bool, probe: bool) -> None:
    if not ready:
        report.line("skip", "Mezmo", "not checked: fix the settings above first")
        return
    url = hook.status_url(str(uuid.uuid4()))
    network = "check MEZMO_GOVERNANCE_URL and your network"
    try:
        status = get_without_key(url)
    except Exception as error:  # noqa: BLE001 - any failure means "not reached"
        report.line("FAIL", "Mezmo URL", f"not reached: {hook.error_detail(str(error))}", network)
        return
    if status != 401:
        report.line(
            "FAIL",
            "Mezmo URL",
            f"a status check without a key got HTTP {status}, not 401, so this is not the authorize endpoint",
            f"use the full authorize URL, such as {DEFAULT_URL}",
        )
        return
    report.line("ok", "Mezmo URL", "reached; the endpoint asks for a key")

    try:
        status, body = hook.http_json("GET", url, None, REQUEST_SECONDS)
    except Exception as error:  # noqa: BLE001
        report.line("FAIL", "Mezmo key", f"no answer: {hook.error_detail(str(error))}", network)
        return
    if status == 404:
        report.line("ok", "Mezmo key", "accepted (a made-up decision id gets HTTP 404)")
    else:
        report.line("FAIL", "Mezmo key", hook.http_failure(status, body), "copy a fresh key from the agent in Mezmo")
        return

    if not probe:
        report.line(
            "warn",
            "Mezmo agent",
            "not checked: a key can pass the check above and still belong to no agent",
            "run python3 tools/doctor.py --probe, which sends Mezmo one test call",
        )
        return
    try:
        status, body = hook.http_json("POST", hook.authorize_url(), probe_payload(), REQUEST_SECONDS)
    except TimeoutError:
        report.line(
            "warn",
            "Mezmo agent",
            f"no answer to the test call within {REQUEST_SECONDS:g} s; the policy may wait for a person",
            "decide or ignore the doctor:probe call in Mezmo",
        )
        return
    except Exception as error:  # noqa: BLE001
        report.line("FAIL", "Mezmo agent", f"no answer: {hook.error_detail(str(error))}", network)
        return
    approved = body.get("approved")
    if status == 200 and isinstance(approved, bool):
        verdict = "approved" if approved else "denied"
        report.line("ok", "Mezmo agent", f"the test call doctor:probe was {verdict}; Mezmo logs it like any call")
        return
    fixes = {
        "ENOAGENT": "register an agent in Mezmo Governance and use that agent's access key",
        "EINVAL": "Mezmo refused the request format; update these plugins",
    }
    fix = fixes.get(hook.error_detail(body.get("code")), "check the agent and its policy in Mezmo")
    report.line("FAIL", "Mezmo agent", hook.http_failure(status, body), fix)


# --- coding agents ------------------------------------------------------------

CLAUDE_EVENT = {
    "hook_event_name": "PreToolUse",
    "session_id": "doctor",
    "tool_use_id": "doctor-smoke",
    "tool_name": "Bash",
    "tool_input": {"command": "echo hi"},
}


def fail_closed(report: Report, subject: str, ok: bool, setting: str, example: str) -> None:
    if ok:
        report.line("ok", subject, f"fails closed ({setting})")
    else:
        report.line(
            "FAIL",
            subject,
            f"no {setting} on the PreToolUse hook, so a hook crash lets the tool run",
            f"add it as in {example}",
        )


def check_claude_code(report: Report, version: Optional[tuple]) -> Optional[list]:
    root = claude_dir()
    settings = as_dict(read_json(root / "settings.json"))
    registry = as_dict(read_json(root / "plugins" / "installed_plugins.json"))
    smokes = []
    plugins = [key for key in as_dict(registry.get("plugins")) if key.startswith("mezmo-governance@")]
    for key in plugins:
        install = as_dict((as_list(registry["plugins"][key]) or [None])[0])
        folder = Path(str(install.get("installPath") or ""))
        if as_dict(settings.get("enabledPlugins")).get(key) is True:
            report.line("ok", "Claude Code plugin", f"{key} is installed and enabled")
        else:
            report.line("warn", "Claude Code plugin", f"{key} is installed but not enabled", "enable it in /plugin")
        hooks = as_dict(as_dict(read_json(folder / "hooks" / "hooks.json")).get("hooks"))
        found = mezmo_handlers(hooks.get("PreToolUse"))
        if not found:
            report.line("FAIL", "Claude Code plugin", f"no PreToolUse hook in {shown(folder)}", "reinstall the plugin")
            continue
        fail_closed(
            report,
            "Claude Code plugin",
            all(handler.get("onFailure") == "block" for *_, handler in found),
            'onFailure: "block"',
            "claude-code/hooks/hooks.json",
        )
        smokes.append(Smoke("Claude Code", folder / HOOK_FILE, CLAUDE_EVENT))
    found = mezmo_handlers(as_dict(settings.get("hooks")).get("PreToolUse"))
    for *_, handler in found:
        report.line("ok", "Claude Code settings", f"PreToolUse hook in {shown(root / 'settings.json')}")
        fail_closed(
            report,
            "Claude Code settings",
            handler.get("onFailure") == "block",
            'onFailure: "block"',
            "claude-code/settings.example.json",
        )
        smokes.append(Smoke("Claude Code", hook_path(str(handler["command"])), CLAUDE_EVENT))
    return smokes if plugins or found else None


def check_codex(report: Report, version: Optional[tuple]) -> Optional[list]:
    path = codex_dir() / "hooks.json"
    found = mezmo_handlers(as_dict(as_dict(read_json(path)).get("hooks")).get("PreToolUse"))
    if not found:
        return None
    config = read_text(codex_dir() / "config.toml") or ""
    features = toml_table(config, "features") or ""
    if re.search(r"^\s*hooks\s*=\s*false\b", features, re.M):
        report.line(
            "FAIL", "Codex hooks", "hooks = false under [features] turns every hook off",
            f"remove it from {shown(codex_dir() / 'config.toml')}",
        )
    smokes = []
    for g, h, _, handler in found:
        command = str(handler.get("command"))
        report.line("ok", "Codex hook", f"PreToolUse hook in {shown(path)}")
        fail_closed(report, "Codex hook", "exit 2" in command, "|| exit 2 guard", "codex/hooks.example.json")
        state = toml_table(config, f'hooks.state."{path}:pre_tool_use:{g}:{h}"')
        if state is None or not re.search(r"^\s*trusted_hash\s*=", state, re.M):
            report.line(
                "FAIL", "Codex trust", "the hook is not trusted, so Codex skips it",
                "start codex, run /hooks, and trust the Mezmo hook",
            )
        else:
            report.line(
                "ok", "Codex trust",
                "trusted in /hooks (after an edit Codex asks again; the doctor does not compare the hash)",
            )
        smokes.append(Smoke("Codex", hook_path(command), CLAUDE_EVENT))
    return smokes


def check_cursor(report: Report, version: Optional[tuple]) -> Optional[list]:
    path = cursor_dir() / "hooks.json"
    found = mezmo_handlers(as_dict(as_dict(read_json(path)).get("hooks")).get("preToolUse"))
    if not found:
        return None
    event = {
        "hook_event_name": "preToolUse",
        "conversation_id": "doctor",
        "tool_use_id": "doctor-smoke",
        "tool_name": "Shell",
        "tool_input": {"command": "echo hi"},
    }
    smokes = []
    for *_, handler in found:
        report.line("ok", "Cursor hook", f"preToolUse hook in {shown(path)}")
        fail_closed(report, "Cursor hook", handler.get("failClosed") is True, "failClosed: true", "cursor/hooks.example.json")
        smokes.append(Smoke("Cursor", hook_path(str(handler["command"])), event))
    return smokes


def check_gemini(report: Report, version: Optional[tuple]) -> Optional[list]:
    path = gemini_dir() / "settings.json"
    extension = gemini_dir() / "extensions" / "mezmo-governance"
    found = [
        (path, handler, hook_path(str(handler.get("command"))))
        for *_, handler in mezmo_handlers(as_dict(as_dict(read_json(path)).get("hooks")).get("BeforeTool"))
    ]
    found += [
        (extension, handler, extension / HOOK_FILE)
        for *_, handler in mezmo_handlers(
            as_dict(as_dict(read_json(extension / "hooks" / "hooks.json")).get("hooks")).get("BeforeTool")
        )
    ]
    if not found:
        return None
    event = {
        "hook_event_name": "BeforeTool",
        "session_id": "doctor",
        "tool_name": "run_shell_command",
        "tool_input": {"command": "echo hi"},
    }
    smokes = []
    for where, handler, script in found:
        command = str(handler.get("command"))
        report.line("ok", "Gemini CLI hook", f"BeforeTool hook in {shown(where)}")
        fail_closed(report, "Gemini CLI hook", "exit 2" in command, "|| exit 2 guard", "gemini-cli/hooks/hooks.json")
        smokes.append(Smoke("Gemini CLI", script, event))
    return smokes


def check_copilot(report: Report, version: Optional[tuple]) -> Optional[list]:
    folder = copilot_dir() / "hooks"
    event = {"sessionId": "doctor", "timestamp": 0, "cwd": ".", "toolName": "bash", "toolArgs": '{"command":"echo hi"}'}
    smokes = []
    installed = False
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        data = as_dict(read_json(path))
        hooks = as_dict(data.get("hooks"))
        if data.get("disableAllHooks") is True:
            report.line("FAIL", "Copilot hooks", f"{shown(path)} sets disableAllHooks", "remove disableAllHooks")
        if mezmo_handlers(hooks.get("PreToolUse"), "bash"):
            installed = True
            report.line(
                "FAIL", "Copilot hook", f"{shown(path)} uses PreToolUse; Copilot then sends a payload the hook rejects",
                "use the camelCase preToolUse, as in copilot/hooks.example.json",
            )
        for *_, handler in mezmo_handlers(hooks.get("preToolUse"), "bash"):
            installed = True
            command = str(handler["bash"])
            if re.search(r"\spreToolUse\s*$", command):
                report.line("ok", "Copilot hook", f"preToolUse hook in {shown(path)}")
            else:
                report.line(
                    "FAIL", "Copilot hook", "the command does not end with the event name preToolUse, so every call is denied",
                    "copy the command from copilot/hooks.example.json",
                )
            smokes.append(Smoke("Copilot", hook_path(command), event, ("preToolUse",)))
    return smokes if installed else None


def check_goose(report: Report, version: Optional[tuple]) -> Optional[list]:
    folder = goose_dir()
    if not folder.is_dir():
        return None
    missing = [name for name in ("plugin.json", HOOK_FILE, "hooks/hooks.json") if not (folder / name).is_file()]
    if missing:
        report.line("FAIL", "Goose plugin", f"{shown(folder)} lacks {', '.join(missing)}", "copy the plugin as in goose/README.md")
        return []
    found = mezmo_handlers(as_dict(as_dict(read_json(folder / "hooks" / "hooks.json")).get("hooks")).get("PreToolUse"))
    if not found:
        report.line("FAIL", "Goose plugin", f"no PreToolUse hook in {shown(folder)}", "copy hooks/hooks.json from goose/")
        return []
    report.line("ok", "Goose plugin", f"installed in {shown(folder)}")
    if any(group.get("matcher") == "*" for _, _, group, _ in found):
        report.line("FAIL", "Goose plugin", 'a matcher "*" is invalid, and Goose skips that rule', "remove the matcher")
    fail_closed(
        report,
        "Goose plugin",
        all(handler.get("on_failure") == "block" for *_, handler in found),
        'on_failure: "block"',
        "goose/hooks/hooks.json",
    )
    event = {
        "event": "PreToolUse",
        "session_id": "doctor",
        "tool_call_id": "call_doctor",
        "tool_name": "shell",
        "tool_input": {"command": "echo hi"},
    }
    return [Smoke("Goose", folder / HOOK_FILE, event)]


def opencode_option(report: Report, options: dict, name: str) -> None:
    value = options.get(name)
    if not isinstance(value, str) or not value.strip():
        report.line(
            "FAIL", "OpenCode options", f"no {name}; the background service does not see your shell, so every call is blocked",
            "add it under options, as in opencode/README.md",
        )
        return
    reference = re.fullmatch(r"\{file:(.+)\}", value.strip())
    if reference:
        path = Path(os.path.expanduser(reference.group(1)))
        if not (read_text(path) or "").strip():
            report.line(
                "FAIL", "OpenCode options", f"{name} names {shown(path)}, which is missing or empty, so every call is blocked",
                "write the agent access key to that file",
            )
            return
    elif name == "MEZMO_GOVERNANCE_URL" and url_problem(value):
        report.line("FAIL", "OpenCode options", f"{name} {url_problem(value)}", f"use {DEFAULT_URL}")
        return
    report.line("ok", "OpenCode options", f"{name} is set")


def check_opencode(report: Report, version: Optional[tuple]) -> Optional[list]:
    config = opencode_dir()
    plugins = config / "plugins"
    entries = []
    for item in as_list(as_dict(read_json(config / "opencode.json")).get("plugins")):
        package = item if isinstance(item, str) else as_dict(item).get("package")
        if isinstance(package, str) and Path(package).name == "mezmo-governance":
            entries.append((package, as_dict(as_dict(item).get("options"))))
    file_install = (plugins / "mezmo-governance.ts").is_file()
    if not entries and not file_install:
        return None
    major2 = version is not None and version[0] >= 2
    for package, options in entries:
        folder = Path(os.path.expanduser(package))
        folder = folder if folder.is_absolute() else config / folder
        if (folder / "index.ts").is_file():
            report.line("ok", "OpenCode plugin", f"{package} is named in {shown(config / 'opencode.json')}")
        else:
            report.line("FAIL", "OpenCode plugin", f"{shown(folder)} has no index.ts", "copy the opencode folder as in opencode/README.md")
        for name in ("MEZMO_GOVERNANCE_URL", "MEZMO_GOVERNANCE_ACCESS_KEY"):
            opencode_option(report, options, name)
    if file_install:
        if not (plugins / "mezmo-governance" / "client.ts").is_file():
            report.line("FAIL", "OpenCode plugin", f"{shown(plugins)} lacks mezmo-governance/client.ts", "copy both, as in opencode/README.md")
        elif entries and major2:
            report.line(
                "FAIL", "OpenCode plugin", "loaded twice; the copy in plugins/ has no options and blocks every call",
                f"delete {shown(plugins / 'mezmo-governance.ts')}",
            )
        elif major2:
            report.line(
                "FAIL", "OpenCode plugin", "OpenCode 2.x passes no options to plugins/ files, so every call is blocked",
                'install as in "Install on 2.x" in opencode/README.md',
            )
        else:
            report.line("ok", "OpenCode plugin", f"installed in {shown(plugins)}")
    if (plugins / "client.ts").is_file():
        report.line("warn", "OpenCode plugin", f"{shown(plugins / 'client.ts')} is left from an old install and fails to load", "delete it")
    return []


def pi_packages() -> list:
    """Local folders `pi install` named in settings.json, as absolute paths."""
    folders = []
    for entry in as_list(as_dict(read_json(pi_dir() / "settings.json")).get("packages")):
        source = entry if isinstance(entry, str) else as_dict(entry).get("source")
        if isinstance(source, str) and not re.match(r"^[a-z]+:", source):
            # Pi joins the path to its folder as text, without following links.
            folders.append(Path(os.path.normpath(pi_dir() / os.path.expanduser(source))))
    return folders


def check_pi(report: Report, version: Optional[tuple]) -> Optional[list]:
    extensions = pi_dir() / "extensions"
    folders = [folder for folder in pi_packages() if (folder / "mezmo-governance.ts").is_file()]
    if (extensions / "mezmo-governance").is_dir():
        folders.append(extensions / "mezmo-governance")
    if not folders:
        return None
    for folder in folders:
        missing = [name for name in ("mezmo-governance.ts", "client.ts", "package.json") if not (folder / name).is_file()]
        if missing:
            report.line("FAIL", "Pi extension", f"{shown(folder)} lacks {', '.join(missing)}", "copy the files as in pi/README.md")
        else:
            report.line("ok", "Pi extension", f"installed from {shown(folder)}")
    if (extensions / "client.ts").is_file():
        report.line("warn", "Pi extension", f"Pi loads {shown(extensions / 'client.ts')} as an extension, and it is not one", "delete it")
    return []


class Agent(NamedTuple):
    name: str
    binary: str
    tested: tuple
    minimum: Optional[str]
    readme: str
    check: Any


# Tested versions come from the README's hand-check table.
AGENTS = (
    Agent("Claude Code", "claude", ("2.1.296",), "2.1.295", "claude-code", check_claude_code),
    Agent("Codex", "codex", ("0.162.1",), None, "codex", check_codex),
    Agent("Cursor", "cursor-agent", ("2026.10.14",), None, "cursor", check_cursor),
    Agent("Gemini CLI", "gemini", ("0.64.0",), None, "gemini-cli", check_gemini),
    Agent("Copilot CLI", "copilot", ("1.0.95",), None, "copilot", check_copilot),
    Agent("Goose", "goose", ("1.54.0",), None, "goose", check_goose),
    Agent("OpenCode", "opencode", ("1.19.0", "2.0.25"), None, "opencode", check_opencode),
    Agent("Pi", "pi", ("1.3.0",), None, "pi", check_pi),
)


def check_binary(report: Report, agent: Agent) -> Optional[tuple]:
    path = shutil.which(agent.binary)
    if path is None:
        return None
    version = binary_version(path)
    if version is None:
        report.line("warn", agent.name, f"{agent.binary} found, but its version could not be read")
        return None
    tested = [version_tuple(text) for text in agent.tested]
    same_major = [known for known in tested if known and known[0] == version[0]]
    reference = max(same_major or [known for known in tested if known])
    minimum = version_tuple(agent.minimum) if agent.minimum else None
    if minimum and version < minimum:
        report.line(
            "warn", agent.name, f"{dotted(version)} is older than {agent.minimum}, which the fail-closed setting needs",
            f"upgrade {agent.binary}",
        )
    elif version < reference:
        report.line("warn", agent.name, f"{dotted(version)} is older than the tested {dotted(reference)}", f"upgrade {agent.binary}")
    else:
        report.line("ok", agent.name, dotted(version))
    return version


def check_agents(report: Report, agents: tuple = AGENTS) -> list:
    smokes = []
    for agent in agents:
        found = shutil.which(agent.binary) is not None
        version = check_binary(report, agent)
        try:
            installed = agent.check(report, version)
        except Unknown as error:
            report.line("warn", agent.name, f"unknown: {error}", "fix the file, then run the doctor again")
            continue
        if installed is not None:
            smokes.extend(installed)
        elif found:
            report.line(
                "warn", agent.name, "the Mezmo plugin is not installed at user level, so its tool calls are not governed",
                f"install it as in {agent.readme}/README.md, or ignore this if you do not govern {agent.name}",
            )
        else:
            report.line("skip", agent.name, f"{agent.binary} is not on PATH")
    return smokes


# --- fail-closed smoke test ---------------------------------------------------


def denied(result: subprocess.CompletedProcess) -> bool:
    if result.returncode == 2:
        return True
    try:
        output = json.loads(result.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        return False
    output = as_dict(output)
    nested = as_dict(output.get("hookSpecificOutput")).get("permissionDecision")
    return result.returncode == 0 and (
        nested == "deny"
        or output.get("permission") == "deny"
        or output.get("permissionDecision") == "deny"
        or output.get("decision") in {"deny", "block"}
    )


def check_smokes(report: Report, smokes: list) -> None:
    if not smokes:
        return
    python = shutil.which("python3")
    if python is None:
        report.line("FAIL", "python3", "not on PATH, and the hook commands run python3", "install Python 3.9 or later")
        return
    try:
        result = subprocess.run(
            [python, "-c", "import sys; print('%d.%d.%d' % sys.version_info[:3])"],
            capture_output=True, text=True, timeout=20, check=False,
        )
        version = version_tuple(result.stdout)
    except (OSError, subprocess.SubprocessError):
        version = None
    if version is None:
        report.line("FAIL", "python3", f"{python} did not report its version", "install Python 3.9 or later")
    elif version < (3, 9):
        report.line("FAIL", "python3", f"{dotted(version)} is older than 3.9, which the hooks need", "install Python 3.9 or later")
    else:
        report.line("ok", "python3", f"{dotted(version)} at {shown(Path(python))}")
    env = {key: value for key, value in os.environ.items() if not key.startswith(("MEZMO_GOVERNANCE_", "CLAUDE_PLUGIN_OPTION_"))}
    env.update(
        {
            "MEZMO_GOVERNANCE_URL": UNREACHABLE_URL,
            "MEZMO_GOVERNANCE_ACCESS_KEY": "doctor-smoke",
            "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "10",
        }
    )
    seen = set()
    for smoke in smokes:
        if (smoke.path, smoke.args) in seen:
            continue
        seen.add((smoke.path, smoke.args))
        subject = f"{smoke.agent} smoke"
        if not smoke.path.is_file():
            report.line("FAIL", subject, f"{shown(smoke.path)} is missing", "copy the hook script as in the agent's README")
            continue
        try:
            result = subprocess.run(
                [python, str(smoke.path), *smoke.args],
                input=json.dumps(smoke.event), env=env, capture_output=True, text=True, timeout=30, check=False,
            )
        except subprocess.TimeoutExpired:
            report.line("FAIL", subject, f"{shown(smoke.path)} did not answer within 30 s", "reinstall the hook from this repository")
            continue
        if denied(result):
            report.line("ok", subject, f"{shown(smoke.path)} denies a call when Mezmo cannot be reached")
        else:
            said = hook.error_detail((result.stderr.strip().splitlines() or [""])[-1])
            report.line(
                "FAIL", subject, f"{shown(smoke.path)} did not deny with Mezmo unreachable (exit {result.returncode}) {said}".rstrip(),
                "reinstall the hook from this repository",
            )


def run(agents: tuple = AGENTS, probe: bool = False) -> int:
    report = Report()
    check_mezmo(report, check_settings(report), probe)
    check_smokes(report, check_agents(report, agents))
    report.line("skip", "libraries", f"{LIBRARIES} are set up in app code, so the doctor does not check them; run examples/check.sh")
    return 1 if report.failed else 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    text = "send Mezmo one test call (tool doctor:probe) to check that the key belongs to an agent"
    parser.add_argument("--probe", action="store_true", help=text)
    return run(probe=parser.parse_args(argv).probe)


if __name__ == "__main__":
    sys.exit(main())
