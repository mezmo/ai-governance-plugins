#!/usr/bin/env python3
"""Check a Mezmo Governance setup on this machine.

    python3 tools/doctor.py           # settings, Mezmo, and each coding agent
    python3 tools/doctor.py --probe   # also send Mezmo one test call

It reads the user-level agent configs and never changes them. It prints one
line per check, `ok`, `warn`, `FAIL` or `skip`, and exits 1 when a check
fails. `info` lines give sample values for the Mezmo add-agent form. It never
prints the token.
"""

from __future__ import annotations

import argparse
import importlib.util
import json
import os
import re
import shutil
import socket
import subprocess
import sys
import threading
import uuid
from pathlib import Path
from typing import Any, NamedTuple, Optional
from urllib.error import HTTPError
from urllib.parse import urlparse
from urllib.request import Request, build_opener

REPO = Path(__file__).resolve().parent.parent
HOOK_FILE = "mezmo_governance_hook.py"
# The Claude Code plugin runs scripts/governance_hook.py; this also matches an older
# mezmo-governance plugin, which ran mezmo_governance_hook.py.
CLAUDE_HOOK_FILE = "governance_hook.py"
CLAUDE_PLUGINS = ("mezmo@", "mezmo-governance@")
DEFAULT_URL = "https://api.mezmo.com/governance/workflow/authorize"
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
REQUEST_SECONDS = 15.0
APP_SERVER_SECONDS = 10.0
# Nothing listens on port 9, so a hook pointed here must deny.
UNREACHABLE_URL = "http://127.0.0.1:9/governance/workflow/authorize"
LIBRARIES = "LangChain, OpenAI Agents SDK, Claude Agent SDK and Mastra"
# Ends each FAIL that means the agent never runs the hook; `agents` then shows "fails closed: no".
UNGOVERNED = "so every tool call runs ungoverned"


def load_hook() -> Any:
    """The Codex hook: its settings rules and HTTP client are every Python hook's own."""
    spec = importlib.util.spec_from_file_location("mezmo_governance_hook", REPO / "codex" / HOOK_FILE)
    module = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(module)  # type: ignore[union-attr]
    return module


hook = load_hook()


def configured(name: str) -> str:
    """A setting from the shell that runs the check, as the hooks read it."""
    value = (os.environ.get(name) or "").strip()
    if not value:
        raise hook.HookContractError(f"set {name}")
    return value


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
            key = configured("MEZMO_GOVERNANCE_TOKEN")
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
    "FACTORY_HOME_OVERRIDE",
)


def env_dir(name: str, default: Path) -> Path:
    value = os.environ.get(name)
    return Path(os.path.expanduser(value)) if value else default


def claude_dir() -> Path:
    return env_dir("CLAUDE_CONFIG_DIR", home() / ".claude")


def claude_managed_dir() -> Path:
    """Where Claude Code reads managed-settings.json and managed-settings.d/ (code.claude.com/docs/en/managed-settings)."""
    if sys.platform == "darwin":
        return Path("/Library/Application Support/ClaudeCode")
    if os.name == "nt":
        return Path(r"C:\Program Files\ClaudeCode")
    return Path("/etc/claude-code")


class ClaudeSettings(NamedTuple):
    path: Path
    data: dict
    managed: bool


def claude_settings() -> list:
    """Each settings file Claude Code reads in this folder, highest precedence first.

    MDM and server-managed policy never reach the disk, so doctor cannot see them.
    """
    managed = claude_managed_dir()
    drop_ins = managed / "managed-settings.d"
    project = Path.cwd() / ".claude"
    paths = [(managed / "managed-settings.json", True)]
    paths += [(path, True) for path in (sorted(drop_ins.glob("*.json")) if drop_ins.is_dir() else [])]
    paths += [(project / "settings.local.json", False), (project / "settings.json", False), (claude_dir() / "settings.json", False)]
    found, seen = [], set()
    for path, is_managed in paths:
        # Run from the home folder, the project settings are the user settings.
        if path.resolve() in seen:
            continue
        seen.add(path.resolve())
        data = read_json(path)
        if isinstance(data, dict):
            found.append(ClaudeSettings(path, data, is_managed))
    return found


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


def droid_dir() -> Path:
    return env_dir("FACTORY_HOME_OVERRIDE", home()) / ".factory"


def windsurf_dir() -> Path:
    # Windsurf has no override; Windsurf Next and Insiders use windsurf-next and windsurf-insiders.
    return home() / ".codeium" / "windsurf"


def antigravity_dir() -> Path:
    # Antigravity CLI 1.4.0 has no override for its user config folder.
    return home() / ".gemini" / "config"


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


def mezmo_handlers(groups: Any, field: str = "command", script: str = HOOK_FILE) -> list:
    """(group index, handler index, group, handler) for each entry that runs the hook."""
    found = []
    for g, group in enumerate(as_list(groups)):
        group = as_dict(group)
        nested = "hooks" in group
        for h, handler in enumerate(as_list(group["hooks"]) if nested else [group]):
            handler = as_dict(handler)
            # The script name as a whole path part, so a user's `not_mezmo_governance_hook.py.wrapper` is not ours.
            if re.search(rf"(?:^|[\s/\\\"'=}}]){re.escape(script)}(?=$|[\s\"'}}])", str(handler.get(field, ""))):
                found.append((g, h, group, handler))
    return found


def hook_path(command: str, script: str = HOOK_FILE) -> Path:
    """The script a hook command runs, expanded as the shell would."""
    match = re.search(r"[^\s\"']*" + re.escape(script), command)
    token = match.group(0) if match else script
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


def local_url(url: str) -> bool:
    """An http URL on this machine, such as the mock's."""
    parsed = urlparse(url)
    return parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS


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
            "MEZMO_GOVERNANCE_TOKEN",
            'export MEZMO_GOVERNANCE_TOKEN="<service account token>", copied from the service account in Mezmo',
        ),
    ):
        try:
            value = configured(name)
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
        if name == "MEZMO_GOVERNANCE_URL":
            problem = url_problem(value)
            if problem:
                report.line("FAIL", name, f"{shown_url(value)} {problem}", f"use {DEFAULT_URL}")
                ready = False
            else:
                local = " (local mock)" if local_url(value) else ""
                report.line("ok", name, f"{shown_url(value)}{local}")
        else:
            report.line("ok", name, "set (not shown)")
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
        report.line("FAIL", "Mezmo token", f"no answer: {hook.error_detail(str(error))}", network)
        return
    if status == 404:
        report.line("ok", "Mezmo token", "accepted (a made-up decision id gets HTTP 404)")
    else:
        report.line(
            "FAIL", "Mezmo token", hook.http_failure(status, body),
            "check that the agent's service account has Governance permissions, then copy a fresh token from the service account in Mezmo",
        )
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
        "ENOAGENT": "register an agent in Mezmo Governance with this service account, or use the token of the service account an agent uses",
        "EPOLICYUNASSIGNED": "assign a policy to the agent in Mezmo Governance",
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


GUARD = "|| exit 2 guard"
# The guards the shipped configs end with: bash `|| exit 2` or `|| { ...; exit 2; }`, and PowerShell's
# `; if ($LASTEXITCODE -ne 0) { exit 2 }`. Each must end the command, so its exit 2 is the command's status.
BASH_GUARD = re.compile(r"\|\|\s*(?:exit 2|\{(?P<body>[^{}]*)\bexit 2;\s*\})\s*$")
POWERSHELL_GUARD = re.compile(r";\s*if\s*\(\s*\$LASTEXITCODE\s+-ne\s+0\s*\)\s*\{\s*exit 2\s*\}\s*$", re.I)


def guarded(command: str) -> bool:
    """Whether a hook crash makes the command exit 2; a substring test also took `|| exit 20`."""
    if re.search(r"(^|\s)#", command):
        return False
    bash = BASH_GUARD.search(command)
    if bash:
        return not re.search(r"\bexit\b", bash.group("body") or "")
    return POWERSHELL_GUARD.search(command) is not None


def old_guard(report: Report, subject: str, command: str, fix: str) -> None:
    """Warn about the guard before ec18454: install skips a config that has a Mezmo hook, so it stays until replaced."""
    if "hook failed; blocking" in command and "[ $s -eq 2 ]" not in command:
        report.line("warn", subject, 'the || exit 2 guard is the old one: a deliberate block also prints "hook failed"', fix)


CLAUDE_KEYS = ("MEZMO_GOVERNANCE_ACCESS_KEY", "MEZMO_GOVERNANCE_TOKEN")
CLAUDE_REQUIRED = "MEZMO_GOVERNANCE_REQUIRED"


def required(value: Any) -> bool:
    """MEZMO_GOVERNANCE_REQUIRED as governance_hook.py reads it."""
    return str(value or "").strip().lower() in ("1", "true", "yes")


def claude_options(plugin: str) -> Optional[dict]:
    """The plugin's saved option names and its governance switch, from `claude plugin configure --json`, or None when unknown.

    Its JSON holds no sensitive value: the key stays in the keychain (claude-code/scripts/governance_config.py).
    """
    binary = shutil.which("claude")
    if binary is None:
        return None
    try:
        result = subprocess.run(
            [binary, "plugin", "configure", plugin, "--json"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, check=False,
        )
        data = json.loads(result.stdout) if result.returncode == 0 else None
    except (OSError, subprocess.SubprocessError, ValueError):
        return None
    if not isinstance(data, dict):
        return None
    enabled = as_dict(data.get("inputs")).get("governance_enabled", True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("0", "false", "no", "off")
    return {"key": "governance_access_key" in as_list(data.get("configured")), "enabled": bool(enabled)}


def claude_key(report: Report, plugin: str, settings: dict, path: Path) -> bool:
    """Whether a call with no key is blocked: governance_hook.py allows it unless MEZMO_GOVERNANCE_REQUIRED=1.

    The hook takes its key from the env of settings.json or the shell, else the plugin options while governance_enabled is on.
    """
    env = as_dict(settings.get("env"))
    if required(env.get(CLAUDE_REQUIRED)):
        report.line("ok", "Claude Code key", f"{CLAUDE_REQUIRED}=1 under env in {shown(path)}, so a call with no key is denied")
        return True
    if any(str(env.get(name) or "").strip() for name in CLAUDE_KEYS):
        report.line("ok", "Claude Code key", f"a key is set under env in {shown(path)} (not shown)")
        return True
    options = claude_options(plugin)
    if options is not None and options["key"] and options["enabled"]:
        report.line("ok", "Claude Code key", "a key is saved in the plugin options (not shown)")
        return True
    fix = (
        f'add "{CLAUDE_REQUIRED}": "1" under env in {shown(path)}, so a call with no key is denied,'
        " then run /mezmo:setup governance; an export in your shell does not reach Claude Code started from the Dock or another app"
    )
    shell = required(os.environ.get(CLAUDE_REQUIRED)) or any((os.environ.get(name) or "").strip() for name in CLAUDE_KEYS)
    if shell:
        detail = f"a key or {CLAUDE_REQUIRED} is set only in this shell; Claude Code started without it lets every call run"
        report.line("warn", "Claude Code key", detail, fix)
    elif options is None:
        detail = "could not read the plugin options (claude plugin configure); with no key the plugin lets every call run"
        report.line("warn", "Claude Code key", detail, fix)
    else:
        why = "the plugin's governance switch is off" if options["key"] else "no key is saved in the plugin options or settings"
        report.line("FAIL", "Claude Code key", f"{why}, so the plugin lets every call run, {UNGOVERNED}", fix)
    return False


def check_claude_code(report: Report, version: Optional[tuple]) -> Optional[list]:
    root = claude_dir()
    settings = as_dict(read_json(root / "settings.json"))
    sources = claude_settings()
    managed = [source for source in sources if source.managed]
    # Managed allowManagedHooksOnly runs only managed hooks and plugins managed enabledPlugins forces on;
    # strictPluginOnlyCustomization with hooks drops user and project hooks but keeps plugin ones.
    only_managed = next((s.path for s in managed if s.data.get("allowManagedHooksOnly") is True), None)
    strict = next(
        (s.path for s in managed if s.data.get("strictPluginOnlyCustomization") is True
         or "hooks" in as_list(s.data.get("strictPluginOnlyCustomization"))),
        None,
    )
    registry = as_dict(read_json(root / "plugins" / "installed_plugins.json"))
    smokes = []
    plugins = [key for key in as_dict(registry.get("plugins")) if key.startswith(CLAUDE_PLUGINS)]
    forced = {key for key in plugins if any(as_dict(s.data.get("enabledPlugins")).get(key) is True for s in managed)}
    for key in plugins:
        install = as_dict((as_list(registry["plugins"][key]) or [None])[0])
        folder = Path(str(install.get("installPath") or ""))
        # The highest settings file that names the plugin decides.
        decider = next((s for s in sources if key in as_dict(s.data.get("enabledPlugins"))), None)
        enabled = decider is not None and decider.data["enabledPlugins"][key] is True
        if enabled:
            report.line("ok", "Claude Code plugin", f"{key} is installed and enabled")
        elif decider is not None and decider.path != root / "settings.json":
            report.line(
                "FAIL", "Claude Code plugin", f"{shown(decider.path)} turns {key} off, {UNGOVERNED}", f"remove {key} from enabledPlugins there",
            )
        else:
            report.line("FAIL", "Claude Code plugin", f"{key} is installed but not enabled, {UNGOVERNED}", "enable it in /plugin")
        if only_managed and key not in forced:
            report.line(
                "FAIL", "Claude Code plugin",
                f"{shown(only_managed)} sets allowManagedHooksOnly and does not force {key} on, {UNGOVERNED}",
                f'ask your administrator to set "{key}": true under enabledPlugins in managed settings',
            )
        hooks = as_dict(as_dict(read_json(folder / "hooks" / "hooks.json")).get("hooks"))
        # The older plugin ran mezmo_governance_hook.py, the current one governance_hook.py.
        found = mezmo_handlers(hooks.get("PreToolUse"), script=CLAUDE_HOOK_FILE) or mezmo_handlers(hooks.get("PreToolUse"))
        if not found:
            report.line("FAIL", "Claude Code plugin", f"no PreToolUse hook in {shown(folder)}", "reinstall the plugin")
            continue
        scripts = []
        for *_, handler in found:
            command = str(handler.get("command")).replace("${CLAUDE_PLUGIN_ROOT}", str(folder))
            scripts.append(hook_path(command, CLAUDE_HOOK_FILE))
            smokes.append(Smoke("Claude Code", scripts[-1], CLAUDE_EVENT))
        blocks = all(handler.get("onFailure") == "block" for *_, handler in found)
        # The older mezmo-governance plugin's mezmo_governance_hook.py denies with no key; governance_hook.py allows.
        keyed = not blocks or not enabled or all(script.name != CLAUDE_HOOK_FILE for script in scripts)
        if keyed or claude_key(report, key, settings, root / "settings.json"):
            fail_closed(report, "Claude Code plugin", blocks, 'onFailure: "block"', "claude-code/hooks/hooks.json")
        else:
            report.line("warn", "Claude Code plugin", 'onFailure: "block" blocks a hook crash only once a key reaches the hook')
    found = mezmo_handlers(as_dict(settings.get("hooks")).get("PreToolUse"))
    for *_, handler in found:
        report.line("ok", "Claude Code settings", f"PreToolUse hook in {shown(root / 'settings.json')}")
        fail_closed(
            report,
            "Claude Code settings",
            handler.get("onFailure") == "block",
            'onFailure: "block"',
            "claude-code/hooks/hooks.json",
        )
        smokes.append(Smoke("Claude Code", hook_path(str(handler["command"])), CLAUDE_EVENT))
        if only_managed or strict:
            switch = "allowManagedHooksOnly" if only_managed else "strictPluginOnlyCustomization"
            report.line(
                "FAIL", "Claude Code settings", f"{shown(only_managed or strict)} sets {switch}, which drops this hook, {UNGOVERNED}",
                "install the mezmo plugin instead (claude-code/README.md)",
            )
    if not plugins and not found:
        return None
    for source in sources:
        if source.data.get("disableAllHooks") is not True:
            continue
        # Outside managed settings, disableAllHooks spares plugins that managed enabledPlugins forces on.
        if not source.managed and not found and plugins and set(plugins) <= forced:
            continue
        report.line("FAIL", "Claude Code hooks", f"{shown(source.path)} sets disableAllHooks, {UNGOVERNED}", "remove disableAllHooks")
    return smokes


def codex_hooks(binary: str) -> list:
    """The hooks `codex app-server` lists, each with its key, trust status and command.

    Codex hashes a normalized form of each hook, so only Codex can say whether a
    saved trusted_hash still matches.
    """
    requests = [
        {"id": 1, "method": "initialize", "params": {"clientInfo": {"name": "mezmo-doctor", "version": "1"}}},
        {"method": "initialized"},
        {"id": 2, "method": "hooks/list", "params": {"cwds": [str(home())]}},
    ]
    try:
        server = subprocess.Popen(
            [binary, "app-server"], stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True,
        )
    except OSError as error:
        raise Unknown(f"could not start codex app-server: {error}") from error
    timer = threading.Timer(APP_SERVER_SECONDS, server.kill)
    timer.start()
    try:
        # Codex drops a request still open when stdin closes, so stdin stays open until the answer.
        server.stdin.write("".join(json.dumps(request) + "\n" for request in requests))  # type: ignore[union-attr]
        server.stdin.flush()  # type: ignore[union-attr]
        for line in server.stdout:  # type: ignore[union-attr]
            answer = as_dict(json.loads(line))
            if answer.get("id") != 2:
                continue
            if "error" in answer:
                raise Unknown(f"codex app-server refused hooks/list: {hook.error_detail(as_dict(answer['error']).get('message'))}")
            entries = as_list(as_dict(answer.get("result")).get("data"))
            return [as_dict(item) for entry in entries for item in as_list(as_dict(entry).get("hooks"))]
    except (OSError, ValueError) as error:
        raise Unknown(f"codex app-server gave no answer: {hook.error_detail(str(error))}") from error
    finally:
        timer.cancel()
        server.kill()
        server.wait()
    raise Unknown(f"codex app-server gave no answer to hooks/list (the doctor waits {APP_SERVER_SECONDS:g} s)")


def codex_listed() -> tuple:
    """(the hooks `codex app-server` lists, "") or (None, why they could not be read)."""
    binary = shutil.which("codex")
    if binary is None:
        return None, "codex is not on PATH"
    try:
        return codex_hooks(binary), ""
    except Unknown as error:
        return None, str(error)


def codex_trust(report: Report, subject: str, item: dict, fix: str) -> None:
    """One line for a hook Codex lists: Codex runs it only when it is on and trusted with its current hash."""
    status = item.get("trustStatus")
    if item.get("enabled") is False:
        report.line("FAIL", subject, f"the PreToolUse hook is turned off in /hooks, so Codex skips it, {UNGOVERNED}", fix)
    elif status in ("trusted", "managed"):
        report.line("ok", subject, f"the PreToolUse hook is {status}")
    elif status == "modified":
        report.line("FAIL", subject, f"the PreToolUse hook changed since you trusted it, so Codex skips it, {UNGOVERNED}", fix)
    else:
        report.line("FAIL", subject, f"the PreToolUse hook is not trusted, so Codex skips it, {UNGOVERNED}", fix)


def check_codex_plugin(report: Report, plugins: list, hooks: Optional[list], why: str) -> list:
    """The native install: `codex plugin add` records it under [plugins."<name>@<marketplace>"]."""
    smokes: list = []
    trust_fix = "start codex, run /hooks, and trust both Mezmo hooks"
    for plugin in plugins:
        report.line("ok", "Codex plugin", f"{plugin} is installed")
    if hooks is None:
        report.line("info", "Codex plugin trust", f"not checked: {why}", trust_fix)
        return smokes
    for plugin in plugins:
        found = [item for item in hooks if item.get("pluginId") == plugin and item.get("eventName") == "preToolUse"]
        if not found:
            report.line(
                "FAIL", "Codex plugin trust", f"Codex lists no PreToolUse hook for {plugin}, {UNGOVERNED}",
                f'set enabled = true under [plugins."{plugin}"] in {shown(codex_dir() / "config.toml")}, or run codex plugin add {plugin}',
            )
        for item in found:
            codex_trust(report, "Codex plugin trust", item, trust_fix)
            smokes.append(Smoke("Codex", hook_path(str(item.get("command"))), CLAUDE_EVENT))
    return smokes


def check_codex(report: Report, version: Optional[tuple]) -> Optional[list]:
    path = codex_dir() / "hooks.json"
    found = mezmo_handlers(as_dict(as_dict(read_json(path)).get("hooks")).get("PreToolUse"))
    config = read_text(codex_dir() / "config.toml") or ""
    plugins = re.findall(r'^\[plugins\."(mezmo-governance@[^"]+)"\][ \t]*$', config, re.M)
    if not found and not plugins:
        return None
    features = toml_table(config, "features") or ""
    if re.search(r"^\s*hooks\s*=\s*false\b", features, re.M):
        report.line(
            "FAIL", "Codex hooks", f"hooks = false under [features] turns every hook off, {UNGOVERNED}",
            f"remove it from {shown(codex_dir() / 'config.toml')}",
        )
    smokes = []
    hooks, why = codex_listed()
    trust_fix = "start codex, run /hooks, and trust the Mezmo hook"
    for g, h, _, handler in found:
        command = str(handler.get("command"))
        report.line("ok", "Codex hook", f"PreToolUse hook in {shown(path)}")
        fail_closed(report, "Codex hook", guarded(command), GUARD, "codex/hooks.example.json")
        old_guard(
            report, "Codex guard", command,
            "run ./mezmo-governance uninstall codex, then install codex, then trust the new hook in /hooks",
        )
        key = f":pre_tool_use:{g}:{h}"
        state = toml_table(config, f'hooks.state."{path}{key}"')
        listed = [
            item for item in hooks or []
            if same_file(item.get("sourcePath"), path) and str(item.get("key", "")).endswith(key)
        ]
        if listed:
            codex_trust(report, "Codex trust", listed[0], trust_fix)
        elif hooks is not None:
            report.line("FAIL", "Codex trust", f"Codex lists no PreToolUse hook at {shown(path)}{key}, {UNGOVERNED}", trust_fix)
        elif state is None or not re.search(r"^\s*trusted_hash\s*=", state, re.M):
            report.line("FAIL", "Codex trust", f"the hook is not trusted, so Codex skips it, {UNGOVERNED}", trust_fix)
        else:
            # A saved hash outlives an edit to the hook; only Codex can compare it.
            report.line("info", "Codex trust", f"not checked: {why}; config.toml has a trusted_hash, but it may be for an older hook", trust_fix)
        smokes.append(Smoke("Codex", hook_path(command), CLAUDE_EVENT))
    if plugins:
        smokes += check_codex_plugin(report, plugins, hooks, why)
    return smokes


def same_file(text: Any, path: Path) -> bool:
    return isinstance(text, str) and (Path(text) == path or Path(text).resolve() == path.resolve())


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
    settings = as_dict(read_json(path))
    hooks_config = as_dict(settings.get("hooksConfig"))
    if hooks_config.get("enabled") is False:
        report.line("FAIL", "Gemini CLI hook", f"hooksConfig.enabled is false in {shown(path)}, {UNGOVERNED}", "remove it")
    disabled = as_list(hooks_config.get("disabled"))
    for where, handler, script in found:
        command = str(handler.get("command"))
        report.line("ok", "Gemini CLI hook", f"BeforeTool hook in {shown(where)}")
        fail_closed(report, "Gemini CLI hook", guarded(command), GUARD, "gemini-cli/hooks/hooks.json")
        if where == extension:
            fix = "run ./mezmo-governance uninstall gemini-cli, then install gemini-cli"
        else:
            fix = f"copy the BeforeTool command from gemini-cli/settings.example.json into {shown(path)}"
        old_guard(report, "Gemini CLI guard", command, fix)
        # Gemini names a hook by its name, else its command.
        if (handler.get("name") or command) in disabled:
            report.line(
                "FAIL", "Gemini CLI hook", f"hooksConfig.disabled in {shown(path)} turns the hook off, {UNGOVERNED}",
                "in gemini, run /hooks panel and enable it",
            )
        smokes.append(Smoke("Gemini CLI", script, event))
    if any(where == extension for where, _, _ in found):
        for folder in gemini_disabled():
            report.line(
                "FAIL", "Gemini CLI hook", f"the extension is turned off in {folder}, {UNGOVERNED}",
                "run gemini extensions enable mezmo-governance",
            )
    return smokes


def gemini_disabled() -> list:
    """The folders where extension-enablement.json turns the extension off.

    Gemini CLI 0.64.0: the last override that matches a folder decides; `!` turns it off, a trailing `*` takes subfolders.
    """
    path = gemini_dir() / "extensions" / "extension-enablement.json"
    rules = [rule for rule in as_list(as_dict(as_dict(read_json(path)).get("mezmo-governance")).get("overrides")) if isinstance(rule, str)]

    def enabled(folder: str) -> bool:
        result = True
        for rule in rules:
            base = rule.lstrip("!")
            subfolders = base.endswith("*")
            base = base.rstrip("*").rstrip("/") + "/"
            if (folder.startswith(base) if subfolders else folder == base):
                result = not rule.startswith("!")
        return result

    folders = [rule.lstrip("!").rstrip("*").rstrip("/") for rule in rules if rule.startswith("!")]
    return [folder for folder in dict.fromkeys(folders) if not enabled(folder + "/")]


def check_copilot(report: Report, version: Optional[tuple]) -> Optional[list]:
    folder = copilot_dir() / "hooks"
    event = {"sessionId": "doctor", "timestamp": 0, "cwd": ".", "toolName": "bash", "toolArgs": '{"command":"echo hi"}'}
    smokes = []
    installed = False
    for path in sorted(folder.glob("*.json")) if folder.is_dir() else []:
        data = as_dict(read_json(path))
        hooks = as_dict(data.get("hooks"))
        if data.get("disableAllHooks") is True:
            report.line("FAIL", "Copilot hooks", f"{shown(path)} sets disableAllHooks, {UNGOVERNED}", "remove disableAllHooks")
        if mezmo_handlers(hooks.get("PreToolUse"), "bash"):
            installed = True
            report.line(
                "FAIL", "Copilot hook",
                f"{shown(path)} uses PreToolUse; Copilot then names tools as Claude does (Bash, not bash),"
                " and next to preToolUse it asks Mezmo twice for each call",
                "use the camelCase preToolUse, as in copilot/hooks.example.json; VS Code Local reads it too",
            )
        # Copilot runs bash on macOS and Linux and powershell on Windows; a handler may carry either or both.
        handlers = {(g, h): handler for field in ("bash", "powershell") for g, h, _, handler in mezmo_handlers(hooks.get("preToolUse"), field)}
        for handler in handlers.values():
            installed = True
            for field, subject in (("bash", "Copilot hook"), ("powershell", "Copilot hook (PowerShell)")):
                command = handler.get(field)
                if not isinstance(command, str) or not mezmo_handlers([handler], field):
                    continue
                if re.search(r"\spreToolUse\s*(\|\||;|$)", command):
                    report.line("ok", subject, f"preToolUse hook in {shown(path)}")
                else:
                    report.line(
                        "FAIL", subject, f"the {field} command does not pass the event name preToolUse, so every call is denied",
                        "copy the command from copilot/hooks.example.json",
                    )
                # VS Code Local runs the tool when the hook exits other than 0 or 2; Copilot blocks it.
                fail_closed(report, subject, guarded(command), GUARD, "copilot/hooks.example.json")
                if field == ("powershell" if os.name == "nt" else "bash"):
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
    for problem in goose_disabled(folder):
        report.line("FAIL", "Goose plugin", f"{problem}, {UNGOVERNED}", "remove that entry")
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


def goose_disabled(folder: Path) -> list:
    """Where Goose 1.54.0 turns the plugin off (crates/goose/src/plugins/discovery.rs)."""
    root = os.environ.get("GOOSE_PATH_ROOT")
    root = Path(root) if root and Path(root).is_absolute() else None
    settings = (root or home()) / ".config" / "goose" / "settings.json"
    config = root / "config" / "config.yaml" if root else env_dir("XDG_CONFIG_HOME", home() / ".config") / "goose" / "config.yaml"
    found = []
    if folder.name in as_list(as_dict(read_json(settings)).get("disabledPlugins")):
        found.append(f"{shown(settings)} lists it in disabledPlugins")
    if yaml_plugin_off(read_text(config) or "", {str(folder), str(folder.resolve())}):
        found.append(f"{shown(config)} sets enabled: false under plugins")
    return found


def yaml_plugin_off(text: str, keys: set) -> bool:
    """Whether the `plugins:` map of a Goose config.yaml has `enabled: false` under one of the keys."""
    block = re.search(r"^plugins:[ \t]*\n((?:[ \t]+.*(?:\n|$)|[ \t]*\n)*)", text, re.M)
    lines = block.group(1).splitlines() if block else []
    for number, line in enumerate(lines):
        match = re.match(r"^([ \t]+)(['\"]?)(.+?)\2:(?:[ \t]+(.*))?$", line)
        if not match or match.group(3) not in keys:
            continue
        if match.group(4):
            return re.search(r"\benabled:[ \t]*false\b", match.group(4)) is not None
        for inner in lines[number + 1:]:
            if inner.strip() and len(inner) - len(inner.lstrip()) <= len(match.group(1)):
                break
            if re.match(r"^[ \t]+enabled:[ \t]*false\b", inner):
                return True
    return False


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
                "write the service account token to that file",
            )
            return
    elif name == "MEZMO_GOVERNANCE_URL" and url_problem(value):
        report.line("FAIL", "OpenCode options", f"{name} {url_problem(value)}", f"use {DEFAULT_URL}")
        return
    local = " (local mock)" if name == "MEZMO_GOVERNANCE_URL" and local_url(value.strip()) else ""
    report.line("ok", "OpenCode options", f"{name} is set{local}")


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
        for name in ("MEZMO_GOVERNANCE_URL", "MEZMO_GOVERNANCE_TOKEN"):
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


def check_droid(report: Report, version: Optional[tuple]) -> Optional[list]:
    path = droid_dir() / "hooks.json"
    settings = droid_dir() / "settings.json"
    config = as_dict(read_json(path))
    # Droid 0.244.0 lays hooks.json over the hooks in settings.json, one event at a time.
    hooks = {**as_dict(as_dict(read_json(settings)).get("hooks")), **config}
    found = mezmo_handlers(hooks.get("PreToolUse"))
    if not found:
        return None
    where = path if "PreToolUse" in config else settings
    if hooks.get("hooksDisabled") is True:
        report.line("FAIL", "Droid hooks", f"hooksDisabled turns every hook off, {UNGOVERNED}", f"remove it from {shown(where)}")
    event = {
        "hook_event_name": "PreToolUse",
        "session_id": "doctor",
        "tool_name": "Execute",
        "tool_input": {"command": "echo hi"},
    }
    smokes = []
    for *_, handler in found:
        command = str(handler.get("command"))
        report.line("ok", "Droid hook", f"PreToolUse hook in {shown(where)}")
        fail_closed(report, "Droid hook", guarded(command), GUARD, "droid/hooks.example.json")
        smokes.append(Smoke("Droid", hook_path(command), event))
    return smokes


# The hook events Windsurf 1.126.0 knows (exa/hooks.ActionFromString), and the four that gate an action.
WINDSURF_EVENTS = (
    "pre_read_code", "post_read_code", "pre_write_code", "post_write_code",
    "pre_mcp_tool_use", "post_mcp_tool_use", "pre_run_command", "post_run_command",
    "pre_user_prompt", "post_cascade_response", "post_setup_worktree",
    "post_cascade_response_with_transcript",
)
WINDSURF_GATES = ("pre_run_command", "pre_read_code", "pre_write_code", "pre_mcp_tool_use")


def windsurf_problem(config: Any) -> Optional[str]:
    """Why Windsurf 1.126.0 drops this hooks.json whole (exa/hooks.Validate), or None."""
    if not isinstance(config, dict):
        return "it is not a JSON object"
    for key in config:
        if key != "hooks":
            return f"it has the top-level key {key}, and Windsurf allows only hooks"
    hooks = config.get("hooks", {})
    if not isinstance(hooks, dict):
        return 'its "hooks" is not an object'
    for event, entries in hooks.items():
        if event.lower() not in WINDSURF_EVENTS:
            return f"it has the event {event}, which Windsurf does not know"
        if not isinstance(entries, list):
            return f"its {event} is not a list"
        for index, entry in enumerate(entries):
            if not any(isinstance(as_dict(entry).get(key), str) and as_dict(entry)[key] for key in ("command", "powershell")):
                return f"its {event} entry {index} has no command or powershell string"
    return None


def check_windsurf(report: Report, version: Optional[tuple]) -> Optional[list]:
    path = windsurf_dir() / "hooks.json"
    config = read_json(path)
    hooks = as_dict(as_dict(config).get("hooks"))
    if not any(mezmo_handlers(hooks.get(gate)) for gate in WINDSURF_GATES):
        return None
    problem = windsurf_problem(config)
    if problem:
        report.line("FAIL", "Windsurf hooks", f"Windsurf drops {shown(path)} because {problem}, {UNGOVERNED}", "fix the file as in windsurf/README.md")
    event = {
        "agent_action_name": "pre_run_command",
        "trajectory_id": "doctor",
        "execution_id": "doctor",
        "tool_info": {"command_line": "echo hi", "cwd": "."},
    }
    smokes = []
    for gate in WINDSURF_GATES:
        found = mezmo_handlers(hooks.get(gate))
        if not found:
            report.line("FAIL", "Windsurf hook", f"no Mezmo hook on {gate}, so those actions run ungoverned", "add it as in windsurf/hooks.example.json")
        for *_, handler in found:
            command = str(handler.get("command"))
            report.line("ok", "Windsurf hook", f"{gate} hook in {shown(path)}")
            fail_closed(report, "Windsurf hook", guarded(command), GUARD, "windsurf/hooks.example.json")
            smokes.append(Smoke("Windsurf", hook_path(command), event))
    return smokes


# The events of an Antigravity CLI named hook: two hold matcher groups, the rest hold handlers.
ANTIGRAVITY_EVENTS = {name.lower(): name for name in ("PreToolUse", "PostToolUse", "SessionStart", "PreInvocation", "PostInvocation", "Stop")}
ANTIGRAVITY_GROUPED = ("pretooluse", "posttooluse")


def go_fields(value: dict) -> dict:
    """The object's keys lowercased: Go's encoding/json, which Antigravity reads hooks.json with, matches them in any case."""
    return {str(key).lower(): item for key, item in value.items()}


def antigravity_handler_problem(where: str, handler: Any) -> Optional[str]:
    if not isinstance(handler, dict):
        return f"{where} is not an object"
    handler = go_fields(handler)
    kind, command, timeout = handler.get("type"), handler.get("command"), handler.get("timeout")
    if kind == "prompt" and command is not None:
        return f'{where} is a prompt hook with a command'
    if kind not in (None, "", "command", "prompt"):
        return f'{where} has the type {json.dumps(kind)}, and Antigravity runs only "command"'
    if command is not None and not isinstance(command, str):
        return f"{where} has a command that is not a string"
    if timeout is not None and (isinstance(timeout, bool) or not isinstance(timeout, int)):
        return f"{where} has a timeout that is not a whole number"
    return None


def antigravity_problem(config: Any) -> Optional[str]:
    """Why Antigravity CLI 1.4.0 drops this hooks.json whole, or None; Go takes a null anywhere as unset."""
    if config is None:
        return None
    if not isinstance(config, dict):
        return "it is not a JSON object"
    for name, spec in config.items():
        if spec is None:
            continue
        if not isinstance(spec, dict):
            return f"its {name} is not an object"
        spec = go_fields(spec)
        if spec.get("enabled") is not None and not isinstance(spec["enabled"], bool):
            return f'its {name} "enabled" is not true or false'
        for key, event in ANTIGRAVITY_EVENTS.items():
            entries = spec.get(key)
            if entries is None:
                continue
            if not isinstance(entries, list):
                return f"its {name} {event} is not a list"
            for g, entry in enumerate(entries):
                where = f"its {name} {event} entry {g}"
                if entry is None:
                    continue
                if key not in ANTIGRAVITY_GROUPED:
                    problem = antigravity_handler_problem(where, entry)
                    if problem:
                        return problem
                    continue
                if not isinstance(entry, dict):
                    return f"{where} is not an object"
                group = go_fields(entry)
                if group.get("matcher") is not None and not isinstance(group["matcher"], str):
                    return f"{where} has a matcher that is not a string"
                handlers = group.get("hooks")
                if handlers is None:
                    continue
                if not isinstance(handlers, list):
                    return f"{where} hooks is not a list"
                for h, handler in enumerate(handlers):
                    problem = None if handler is None else antigravity_handler_problem(f"{where} hook {h}", handler)
                    if problem:
                        return problem
    return None


def antigravity_hooks(config: Any) -> list:
    """(hook name, named hook, handler) for each PreToolUse handler that runs the hook.

    Antigravity runs only handlers in a matcher group there, and ignores a flat one.
    """
    found = []
    for name, spec in as_dict(config).items():
        spec = go_fields(as_dict(spec))
        for group in as_list(spec.get("pretooluse")):
            for handler in as_list(go_fields(as_dict(group)).get("hooks")):
                command = go_fields(as_dict(handler)).get("command")
                if isinstance(command, str) and HOOK_FILE in command:
                    found.append((name, spec, command))
    return found


def check_antigravity(report: Report, version: Optional[tuple]) -> Optional[list]:
    path = antigravity_dir() / "hooks.json"
    config = read_json(path)
    found = antigravity_hooks(config)
    if not found:
        return None
    problem = antigravity_problem(config)
    if problem:
        report.line("FAIL", "Antigravity CLI hooks", f"Antigravity drops {shown(path)} because {problem}, {UNGOVERNED}", "fix the file as in antigravity/README.md")
    event = {"conversationId": "doctor", "stepIdx": 1, "toolCall": {"name": "run_command", "args": {"CommandLine": "echo hi"}}}
    smokes = []
    for name, spec, command in found:
        if spec.get("enabled") is False:
            report.line("FAIL", "Antigravity CLI hooks", f'the {name} hook has "enabled": false, {UNGOVERNED}', f"remove it from {shown(path)}")
        report.line("ok", "Antigravity CLI hook", f"PreToolUse hook {name} in {shown(path)}")
        fail_closed(report, "Antigravity CLI hook", True, "Antigravity blocks the tool when a hook fails", "antigravity/hooks.example.json")
        smokes.append(Smoke("Antigravity CLI", hook_path(command), event, ("PreToolUse",)))
    return smokes


class Agent(NamedTuple):
    name: str
    binary: str
    tested: tuple
    minimum: Optional[str]
    readme: str
    check: Any
    namespace: str  # the tool_namespace the adapter sends for a built-in tool (AGENT in its hook)
    shell: str
    # None where the agent sends no read-only tool: Codex's apply_patch writes files, and Claude Code only sends Bash and MCP tools
    read: Optional[str]


# Tested versions come from the live-run table in docs/live-testing.md; tool names from each adapter README.
AGENTS = (
    Agent("Claude Code", "claude", ("2.1.301",), "2.1.295", "claude-code", check_claude_code, "claude_code", "Bash", None),
    Agent("Codex", "codex", ("0.162.1",), None, "codex", check_codex, "codex", "Bash", None),
    Agent("Cursor", "cursor-agent", ("2026.10.14",), None, "cursor", check_cursor, "cursor", "Shell", "Read"),
    Agent("Gemini CLI", "gemini", ("0.64.0",), None, "gemini-cli", check_gemini, "gemini_cli", "run_shell_command", "read_file"),
    Agent("Copilot CLI", "copilot", ("1.0.95", "1.0.98"), None, "copilot", check_copilot, "copilot", "bash", "view"),
    Agent("Goose", "goose", ("1.54.0",), None, "goose", check_goose, "goose", "shell", "tree"),
    Agent("OpenCode", "opencode", ("1.19.0", "2.0.25"), None, "opencode", check_opencode, "opencode", "shell", "read"),
    Agent("Pi", "pi", ("1.3.0",), None, "pi", check_pi, "pi", "bash", "read"),
    Agent("Droid", "droid", ("0.244.0",), None, "droid", check_droid, "droid", "Execute", "Read"),
    Agent("Windsurf", "devin-desktop", ("1.126.0",), None, "windsurf", check_windsurf, "windsurf", "run_command", "read_code"),
    Agent("Antigravity CLI", "agy", ("1.4.0", "1.4.1"), None, "antigravity", check_antigravity, "antigravity", "run_command", "view_file"),
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


def install_copies() -> dict:
    """Each hook copy ./mezmo-governance install writes, and refreshes when it differs, with its adapter folder."""
    return {
        codex_dir() / "hooks" / HOOK_FILE: "codex",
        cursor_dir() / "hooks" / HOOK_FILE: "cursor",
        copilot_dir() / "hooks" / HOOK_FILE: "copilot",
        goose_dir() / HOOK_FILE: "goose",
        droid_dir() / "hooks" / HOOK_FILE: "droid",
        windsurf_dir() / "hooks" / HOOK_FILE: "windsurf",
        antigravity_dir() / "hooks" / HOOK_FILE: "antigravity",
    }


def script_agent(report: Report, smoke: Smoke) -> None:
    """Fail a hook script made for another agent: it sends that agent's tool_namespace, so this agent's rules never match.

    MEZMO_GOVERNANCE_HOOK can point any agent at any script. The AGENT constant is read from the text; the script never runs here.
    """
    agent = next((item for item in AGENTS if item.name == smoke.agent), None)
    subject = f"{smoke.agent} hook script"
    try:
        match = re.search(r"^AGENT\s*=\s*[\"']([^\"']*)[\"']", smoke.path.read_text(encoding="utf-8"), re.M)
    except (OSError, ValueError) as error:
        report.line("warn", subject, f"unknown: could not read {shown(smoke.path)}: {error}")
        return
    if agent is None or (match and match.group(1) == agent.namespace):
        return
    if not match:
        report.line("warn", subject, f"unknown: {shown(smoke.path)} sets no AGENT, so the doctor cannot tell which agent it is for")
        return
    found = match.group(1)
    report.line(
        "FAIL", subject,
        f'{shown(smoke.path)} has AGENT = "{found}", so Mezmo gets tool_namespace {found} and the {smoke.agent} rules never match',
        f"use the hook from {agent.readme}/; if MEZMO_GOVERNANCE_HOOK is set, point it there or unset it",
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
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=20, check=False,
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
            "MEZMO_GOVERNANCE_TOKEN": "doctor-smoke",
            "MEZMO_GOVERNANCE_TIMEOUT_SECONDS": "10",
            # The Claude Code plugin keeps session state for its status display and
            # skips it when it cannot write; no folder can exist under /dev/null.
            "MEZMO_GOVERNANCE_STATE_DIR": os.devnull + "/mezmo-governance",
        }
    )
    # Each agent's own pair: MEZMO_GOVERNANCE_HOOK can give every agent the same script, which the smoke runs once.
    for smoke in {(smoke.agent, smoke.path): smoke for smoke in smokes}.values():
        if smoke.path.is_file():
            script_agent(report, smoke)
    seen = set()
    for smoke in smokes:
        if (smoke.path, smoke.args) in seen:
            continue
        seen.add((smoke.path, smoke.args))
        subject = f"{smoke.agent} smoke"
        if not smoke.path.is_file():
            report.line("FAIL", subject, f"{shown(smoke.path)} is missing", "copy the hook script as in the agent's README")
            continue
        adapter = install_copies().get(smoke.path)
        try:
            stale = adapter is not None and smoke.path.read_bytes() != (REPO / adapter / HOOK_FILE).read_bytes()
        except OSError as error:
            report.line("warn", f"{smoke.agent} hook copy", f"unknown: could not compare {shown(smoke.path)}: {error}")
        else:
            if stale:
                report.line(
                    "warn", f"{smoke.agent} hook copy", f"{shown(smoke.path)} differs from this repo's {adapter}/{HOOK_FILE}",
                    f"run ./mezmo-governance install {adapter}",
                )
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


def agent_forms(report: Report, agents: tuple) -> None:
    """Sample answers for the Mezmo "add agent" form; Mezmo gets none of them from the plugins."""
    host = socket.gethostname().split(".")[0].lower()
    for agent in agents:
        path = shutil.which(agent.binary)
        version = binary_version(path) if path else None
        if version is None:
            continue
        report.line(
            "info", f"Mezmo agent form for {agent.name}",
            f'Agent Name "{agent.name} on {host}", Description "{agent.name} with the mezmo-governance plugin", '
            f'Runtime Name "{agent.readme}", Version "{dotted(version)}", Instance ID "{agent.readme}-{host}"',
        )


def run(agents: tuple = AGENTS, probe: bool = False) -> int:
    report = Report()
    check_mezmo(report, check_settings(report), probe)
    check_smokes(report, check_agents(report, agents))
    agent_forms(report, agents)
    report.line("skip", "libraries", f"{LIBRARIES} are set up in app code, so the doctor does not check them; run examples/check.sh")
    return 1 if report.failed else 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    text = "send Mezmo one test call (tool doctor:probe) to check that the key belongs to an agent"
    parser.add_argument("--probe", action="store_true", help=text)
    return run(probe=parser.parse_args(argv).probe)


if __name__ == "__main__":
    sys.exit(main())
