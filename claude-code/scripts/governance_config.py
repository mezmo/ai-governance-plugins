#!/usr/bin/env python3
"""Configure Mezmo Governance and the Mezmo ledger for the mezmo plugin's tool-call hooks.

The hooks (governance_hook.py, ledger_hook.py) read MEZMO_GOVERNANCE_* and MEZMO_LEDGER_*
environment variables first, then the plugin's options: one Mezmo API URL, from which both
endpoints are derived, and the service account token, which Claude Code keeps as a sensitive
option (the macOS Keychain, else ~/.claude/.credentials.json).

  configure  check the token against governance and the ledger, save it (plugin options, or the
             settings file), then connect Mezmo's MCP server with it (mcp_config.py)
  remove     clear the plugin options and the settings-file variables
  status     where the configuration comes from, the hook's last result, and the ledger's last send
  check      ask governance and the ledger whether they accept the token; records nothing
  managed    add the variables to a managed-settings document
  saved-key  whether the Codex setup saved a Mezmo key in the macOS Keychain; never prints it
"""

from __future__ import annotations

import argparse
import dataclasses
import datetime
import json
import os
import pathlib
import re
import sys
import urllib.parse
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import claude_code_otel as cco
import mcp_config
import mezmo_common as mc

run_claude = mc.run_claude

DEFAULT_API_URL = "https://api.mezmo.com"
GOVERNANCE_PATH = "/governance/workflow/authorize"
LEDGER_PATH = "/v1/audit/events"
DEFAULT_URL = DEFAULT_API_URL + GOVERNANCE_PATH
DEFAULT_LEDGER_URL = DEFAULT_API_URL + LEDGER_PATH
PLUGIN_ID = "mezmo@mezmo"
API_URL_OPTION = "api_url"
# The option releases before 0.6.0 kept the full authorize URL in. configure moves it to API_URL_OPTION.
LEGACY_URL_OPTION = "governance_url"
KEY_OPTION = "governance_access_key"
ENABLED_OPTION = "governance_enabled"
LEDGER_ENABLED_OPTION = "ledger_enabled"
# Claude Code keeps a sensitive option's value when given "", so remove overwrites the key
# with this placeholder, which governance_hook treats as unset, and turns the switch off.
REMOVED_KEY = "off"
URL_ENV = "MEZMO_GOVERNANCE_URL"
KEY_ENVS = ("MEZMO_GOVERNANCE_ACCESS_KEY", "MEZMO_GOVERNANCE_TOKEN")
REQUIRED_ENV = "MEZMO_GOVERNANCE_REQUIRED"
LEDGER_URL_ENV = "MEZMO_LEDGER_URL"
LEDGER_KEY_ENV = "MEZMO_LEDGER_KEY"
LEDGER_SPOOL_ENV = "MEZMO_LEDGER_SPOOL"
SETTINGS_KEYS = (URL_ENV,) + KEY_ENVS + (REQUIRED_ENV, LEDGER_URL_ENV, LEDGER_KEY_ENV)
FALSE_VALUES = ("0", "false", "no", "off")
# Written by governance_hook.py, one file per Claude Code session.
DEFAULT_STATE_DIR = pathlib.Path.home() / ".mezmo" / "governance" / "sessions"
# Where ledger.py keeps the events it could not send yet.
DEFAULT_LEDGER_SPOOL = pathlib.Path.home() / ".mezmo" / "ledger-spool.jsonl"

EXIT_ERROR, EXIT_USAGE, EXIT_REJECTED, EXIT_NOT_INSTALLED, EXIT_UNSUPPORTED, EXIT_NO_SAVED_KEY = 1, 2, 3, 4, 5, 6
# Where ledger_hook.py keeps its last send in the session state (ledger.LAST_SEND_FIELD).
LAST_SEND_FIELD = "ledger_last_send"
# A harmless call that makes the hooks check and record something.
TEST_COMMAND = "printf 'mezmo-onboarding-test\\n'"

# What status says of a key kept as a sensitive plugin option, which no script can read back.
OPTION_KEY = "(a sensitive plugin option: in the macOS Keychain, else ~/.claude/.credentials.json)"

Transport = Callable[..., Tuple[int, str]]
ClaudeRunner = Callable[[Sequence[str], Optional[str]], Tuple[int, str]]


@dataclasses.dataclass
class CheckResult:
    ok: bool
    status: Optional[int]
    detail: str


class NotInstalled(Exception):
    pass


class UnsupportedRuntime(RuntimeError):
    pass


def preflight(claude: ClaudeRunner, plugin_id: str, store: str = "plugin") -> None:
    """Check runtime and storage capability before reading or sending a token."""
    if sys.version_info < (3, 9):
        raise UnsupportedRuntime("Python 3.9 or later is required. Select a supported python3 first.")
    binary = mc.claude_executable()
    try:
        code, output = claude(["--version"], None)
    except OSError:
        raise UnsupportedRuntime(f"Claude Code executable {binary} was not found. Install Claude Code 2.1.295 or later.")
    if code == 127:
        raise UnsupportedRuntime(f"Claude Code executable {binary} was not found. Install Claude Code 2.1.295 or later.")
    match = re.search(r"\b(\d+)\.(\d+)\.(\d+)\b", output)
    if code or not match:
        raise UnsupportedRuntime(f"Could not determine the version of {binary}. Check that executable with --version; 2.1.295 or later is required.")
    print(f"Checked Claude Code executable {binary}: version {match.group(0)}.")
    if tuple(map(int, match.groups())) < (2, 1, 295):
        raise UnsupportedRuntime("Claude Code 2.1.295 or later is required for fail-closed governance. Upgrade before setup; plaintext settings do not fix older hook failure handling.")
    if store == "plugin" and plugin_options(claude, plugin_id) is None:
        raise NotInstalled(plugin_id)


def authorization(key: str) -> str:
    return key if key.lower().startswith(("token ", "bearer ")) else f"Token {key}"


def api_base(url: str) -> str:
    """`url` without its path, query and trailing slash: `https://api.mezmo.com` from any URL on it.

    Raises ValueError when `url` has no http(s) scheme and host.
    """
    parsed = urllib.parse.urlparse(url.strip())
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(f"the Mezmo API URL must be an http(s) URL, such as {DEFAULT_API_URL}")
    return f"{parsed.scheme}://{parsed.netloc}"


def governance_url(api_url: str) -> str:
    return api_url.rstrip("/") + GOVERNANCE_PATH


def ledger_url(api_url: str) -> str:
    return api_url.rstrip("/") + LEDGER_PATH


def check_ledger(key: Optional[str], url: str, send: Transport = mc.http_request) -> CheckResult:
    """POST an empty batch. 202 with {"accepted": 0} means the ledger takes the key; nothing is recorded."""
    headers = {"Accept": "application/json", "Content-Type": "application/json"}
    if key:
        headers["Authorization"] = authorization(key)
    try:
        status, body = send("POST", url, headers, b"[]", 20.0)
    except OSError as err:
        return CheckResult(False, None, f"Couldn't reach the Mezmo ledger at {url}: {err}")
    try:
        accepted = json.loads(body).get("accepted")
    except (ValueError, AttributeError):
        accepted = None
    if status == 202 and accepted == 0:
        return CheckResult(True, status, "The Mezmo ledger accepted the service account token. Nothing was recorded.")
    if status in (401, 403):
        return CheckResult(False, status, f"The Mezmo ledger rejected the service account token (HTTP {status}).")
    return CheckResult(False, status, f"Unexpected answer from the Mezmo ledger (HTTP {status}): {body.strip()[:200]}")


def check_access(key: str, url: str, send: Transport = mc.http_request) -> CheckResult:
    """GET the status of a random decision. 404 means the service accepted the key."""
    probe = f"{url}{'&' if '?' in url else '?'}decision_id={uuid.uuid4()}"
    headers = {"Authorization": authorization(key), "Accept": "application/json"}
    try:
        status, body = send("GET", probe, headers, None, 20.0)
    except OSError as err:
        return CheckResult(False, None, f"Couldn't reach Mezmo Governance at {url}: {err}")
    if status == 404:
        return CheckResult(True, status, "Mezmo Governance accepted the service account token. Nothing was recorded.")
    if status in (401, 403):
        return CheckResult(False, status, f"Mezmo Governance rejected the service account token (HTTP {status}).")
    if status == 402:
        return CheckResult(False, status, "The Mezmo plan doesn't include governance (HTTP 402).")
    return CheckResult(False, status, f"Unexpected answer from Mezmo Governance (HTTP {status}): {body.strip()[:200]}")


def plugin_options(claude: ClaudeRunner, plugin_id: str) -> Optional[Dict[str, Any]]:
    """{'key_set', 'enabled', 'api_url', 'ledger_enabled', 'legacy_url'} from `claude plugin configure --json`,
    or None if not installed. `legacy_url` is a governance_url an older release saved, when the CLI still shows it."""
    code, output = claude(["plugin", "configure", plugin_id, "--json"], None)
    if code != 0:
        if "No installed plugin" in output:
            return None
        if "unknown command" in output.lower():
            raise UnsupportedRuntime("This Claude Code does not support plugin configure. Install a supported official CLI before setup.")
        raise RuntimeError("Could not read plugin configuration. Check claude plugin configure.")
    try:
        data = json.loads(output)
        if not isinstance(data, dict):
            raise ValueError()
    except ValueError:
        raise RuntimeError("Claude Code returned invalid plugin configuration JSON.")
    configured = data.get("configured") or []
    inputs = data.get("inputs") or {}
    if not isinstance(configured, list) or not isinstance(inputs, dict):
        raise RuntimeError("Claude Code returned invalid plugin configuration fields.")

    def switch(name: str) -> bool:
        value = inputs.get(name, True)
        return value.strip().lower() not in FALSE_VALUES if isinstance(value, str) else bool(value)

    def text(name: str) -> Optional[str]:
        value = inputs.get(name) if name in configured else None
        return value.strip() or None if isinstance(value, str) else None

    return {
        "key_set": KEY_OPTION in configured,
        "enabled": switch(ENABLED_OPTION),
        "api_url": text(API_URL_OPTION),
        "ledger_enabled": switch(LEDGER_ENABLED_OPTION),
        "legacy_url": text(LEGACY_URL_OPTION),
    }


def available_plugin_options(claude: ClaudeRunner, plugin_id: str) -> Tuple[Optional[Dict[str, Any]], Optional[str]]:
    try:
        return plugin_options(claude, plugin_id), None
    except (RuntimeError, OSError):
        return None, "Plugin configuration is unavailable; its saved token and enabled state are unknown."


def save_plugin_options(claude: ClaudeRunner, plugin_id: str, values: Mapping[str, str]) -> None:
    code, output = claude(["plugin", "configure", plugin_id, "--values-stdin", "--json"], json.dumps(values))
    if code == 0:
        return
    if "No installed plugin" in output:
        raise NotInstalled(plugin_id)
    try:
        refused = json.loads(output).get("refused", {}).get("message")
    except (ValueError, AttributeError):
        refused = None
    detail = refused or output.strip()[:300] or f"claude plugin configure exited {code}"
    if values.get(KEY_OPTION):
        detail = detail.replace(values[KEY_OPTION], mc.mask(values[KEY_OPTION]))
    raise RuntimeError(detail)


def env_sources(settings: Mapping[str, Any], environ: Mapping[str, str], managed_files: Sequence[pathlib.Path]) -> List[Tuple[str, Dict[str, str]]]:
    """Governance variables by source, highest precedence first."""
    sources = []
    for path in managed_files:
        try:
            env = json.loads(path.read_text(encoding="utf-8")).get("env", {})
        except (OSError, ValueError, AttributeError):
            continue
        found = {k: v for k, v in env.items() if k in SETTINGS_KEYS and isinstance(v, str) and v.strip()}
        if found:
            sources.append((f"managed settings ({path})", found))
    env = settings.get("env") if isinstance(settings.get("env"), dict) else {}
    found = {k: v for k, v in env.items() if k in SETTINGS_KEYS and isinstance(v, str) and v.strip()}
    if found:
        sources.append(("user settings", found))
    found = {k: v for k, v in environ.items() if k in SETTINGS_KEYS and v.strip()}
    if found:
        sources.append(("environment", found))
    return sources


def default_managed_files(platform: str = sys.platform) -> List[pathlib.Path]:
    base = cco.MANAGED_SETTINGS_DIRS.get(platform)
    if not base:
        return []
    root = pathlib.Path(base)
    files = [root / "managed-settings.json"]
    dropins = root / "managed-settings.d"
    if dropins.is_dir():
        files += sorted(dropins.glob("*.json"))
    return [f for f in files if f.is_file()]


def _option_api_url(options: Optional[Mapping[str, Any]]) -> str:
    return (options or {}).get("api_url") or DEFAULT_API_URL


def _first(sources: Sequence[Tuple[str, Mapping[str, str]]], names: Sequence[str]) -> Optional[Tuple[str, str]]:
    """(source, value) of the first of `names` set in the highest source that sets one, else None."""
    for source, values in sources:
        for name in names:
            if values.get(name):
                return source, values[name]
    return None


def resolve(sources: Sequence[Tuple[str, Mapping[str, str]]], options: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """What the hook will use: the first source with a key wins, as in governance_hook.load_config."""
    report: Dict[str, Any] = {"configured": False, "source": None, "url": None, "key": "(not set)", "readable_key": None}
    url = (_first(sources, (URL_ENV,)) or (None, None))[1] or governance_url(_option_api_url(options))
    found = _first(sources, KEY_ENVS)
    if found:
        report.update(configured=True, source=found[0], url=url, key=mc.mask(found[1]), readable_key=found[1])
        return report
    report["switched_off"] = bool(options and options.get("key_set") and not options.get("enabled"))
    if options and options.get("key_set") and options.get("enabled"):
        report.update(configured=True, source="plugin options", url=url, key=OPTION_KEY)
    return report


def ledger_report(
    sources: Sequence[Tuple[str, Mapping[str, str]]], options: Optional[Mapping[str, Any]], environ: Mapping[str, str]
) -> Dict[str, Any]:
    """What ledger.load_ledger_config will use, and how many records wait in the retry file."""
    report: Dict[str, Any] = {
        "on": False, "why": None, "url": None, "key": "(not set)", "source": None, "readable_key": None,
        "switched_off": False,
    }
    spool = pathlib.Path(environ.get(LEDGER_SPOOL_ENV) or DEFAULT_LEDGER_SPOOL).expanduser()
    try:
        report["waiting"] = sum(1 for line in spool.read_text(encoding="utf-8", errors="replace").splitlines() if line.strip())
    except OSError:
        report["waiting"] = 0
    report["retry_file"] = str(spool)
    url = _first(sources, (LEDGER_URL_ENV,))
    if url is None and options is not None and not options.get("ledger_enabled", True):
        report.update(why="the plugin's ledger_enabled option is off", switched_off=True)
        return report
    found = _first(sources, (LEDGER_KEY_ENV,)) or _first(sources, KEY_ENVS)
    if found:
        report.update(source=found[0], key=mc.mask(found[1]), readable_key=found[1])
    elif options and options.get("key_set"):
        report.update(source="plugin options", key=OPTION_KEY)
    elif url is None:
        report["why"] = "no service account token is configured"
        return report
    report.update(on=True, url=url[1] if url else ledger_url(_option_api_url(options)))
    report["why"] = f"{LEDGER_URL_ENV} from {url[0]}" if url else "a service account token is configured"
    return report


def _saved_options(settings: Mapping[str, Any], plugin_id: str) -> Optional[Dict[str, Any]]:
    """The plugin's non-sensitive options in the settings file's pluginConfigs, or None."""
    configs = settings.get("pluginConfigs")
    plugin = configs.get(plugin_id) if isinstance(configs, dict) else None
    options = plugin.get("options") if isinstance(plugin, dict) else None
    return options if isinstance(options, dict) else None


def legacy_url_option(settings: Mapping[str, Any], plugin_id: str) -> Optional[str]:
    """The governance_url option an older release saved in the settings file, or None."""
    value = (_saved_options(settings, plugin_id) or {}).get(LEGACY_URL_OPTION)
    return value.strip() or None if isinstance(value, str) else None


def drop_legacy_url_option(settings_file: pathlib.Path, plugin_id: str) -> None:
    """Remove the governance_url option from the settings file, when it is there."""
    settings = cco.read_settings(settings_file)
    options = _saved_options(settings, plugin_id)
    if options is not None and LEGACY_URL_OPTION in options:
        del options[LEGACY_URL_OPTION]
        cco.write_settings(settings_file, settings)


def is_required(sources: Sequence[Tuple[str, Mapping[str, str]]]) -> Optional[str]:
    for name, values in sources:
        if values.get(REQUIRED_ENV, "").lower() in ("1", "true", "yes"):
            return name
    return None


def latest_session(state_dir: pathlib.Path) -> Optional[Dict[str, Any]]:
    """The most recently updated session status file the hook wrote, or None."""
    try:
        files = sorted(state_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in files:
        try:
            state = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            continue
        if isinstance(state, dict):
            return state
    return None


def latest_ledger_send(state_dir: pathlib.Path) -> Optional[Dict[str, Any]]:
    """The last send ledger_hook.py kept in the most recently updated session that has one, or None."""
    try:
        files = sorted(state_dir.glob("*.json"), key=lambda f: f.stat().st_mtime, reverse=True)
    except OSError:
        return None
    for path in files:
        try:
            found = json.loads(path.read_text(encoding="utf-8")).get(LAST_SEND_FIELD)
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(found, dict):
            return found
    return None


def describe_last_send(
    last: Optional[Mapping[str, Any]], now: Optional[datetime.datetime] = None
) -> Tuple[Optional[bool], str]:
    """(whether it worked, or None when nothing was sent, and a line such as
    "last send 16:14:40 UTC, HTTP 202, 2 events"). A send before today shows its date too."""
    if not last:
        return None, (f"no send recorded yet. In a new Claude Code session, ask Claude to run `{TEST_COMMAND}`, "
                      "then check again.")
    try:
        when = datetime.datetime.fromisoformat(str(last.get("ts"))).astimezone(datetime.timezone.utc)
        today = (now or datetime.datetime.now(datetime.timezone.utc)).astimezone(datetime.timezone.utc).date()
        stamp = when.strftime("%H:%M:%S UTC" if when.date() == today else "%Y-%m-%d %H:%M:%S UTC")
    except ValueError:
        stamp = "at an unknown time"
    count = last.get("events")
    events = f"{count} event{'' if count == 1 else 's'}" if isinstance(count, int) else "unknown events"
    host = f" to {last['host']}" if last.get("host") else ""
    status = last.get("status")
    if status == 202:
        return True, f"last send {stamp}{host}, HTTP 202, {events}"
    if status in (401, 403):
        return False, (f"last send {stamp}{host}, HTTP {status}, {events}: the ledger rejected the token, a wrong "
                       "key or a key for another account. Rerun /mezmo:setup governance with the right service "
                       "account token.")
    if status is None:
        return False, (f"last send {stamp}{host} failed, {events}: {last.get('error') or 'no answer'}. Check the "
                       "API URL, proxy, VPN or firewall.")
    return False, (f"last send {stamp}{host}, HTTP {status}, {events}, not 202. Check the API URL in "
                   "/plugin configure mezmo@mezmo.")


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="governance_config.py", description="Configure Mezmo Governance and the ledger for Claude Code.")
    parser.add_argument("--settings-file", type=pathlib.Path,
                        help="User settings file (default: $CLAUDE_CONFIG_DIR/settings.json or ~/.claude/settings.json)")
    parser.add_argument("--plugin-id", default=PLUGIN_ID, help=f"Installed plugin id (default: {PLUGIN_ID})")
    sub = parser.add_subparsers(dest="command", required=True)

    pre = sub.add_parser("preflight", help="Check prerequisites without reading or sending a token")
    pre.add_argument("--store", choices=("plugin", "settings"), default="plugin")

    api_help = f"Mezmo API URL (default: {DEFAULT_API_URL})"
    configure = sub.add_parser("configure", help="Check the service account token, save it, and connect Mezmo MCP")
    mc.add_key_arguments(configure, "Service account token (default: $MEZMO_GOVERNANCE_ACCESS_KEY or $MEZMO_GOVERNANCE_TOKEN)")
    mc.add_saved_key_argument(configure)
    mcp = configure.add_mutually_exclusive_group()
    mcp.add_argument("--mcp-url", help=f"Mezmo MCP URL; required unless the API URL is {DEFAULT_API_URL} ({mcp_config.MCP_URL})")
    mcp.add_argument("--no-mcp", action="store_true", help="Don't connect Mezmo's MCP server")
    configure.add_argument("--api-url", help=f"{api_help[:-1]}, or the API URL of a governance_url an older release saved)")
    configure.add_argument("--store", choices=("plugin", "settings"), default="plugin",
                           help="plugin: plugin options, key kept as a sensitive option (default); settings: the settings file's env")
    configure.add_argument("--skip-check", action="store_true", help="Save without checking the key first")

    sub.add_parser("remove", help="Clear the plugin options and the settings-file variables")

    sub.add_parser("saved-key", help="Whether the Codex setup saved a Mezmo key in the macOS Keychain; never prints it")

    status = sub.add_parser("status", help="Show where the configuration comes from and the last result")
    status.add_argument("--json", action="store_true")
    status.add_argument("--state-dir", type=pathlib.Path,
                        help="Where the hook keeps session status files (default: ~/.mezmo/governance/sessions)")

    check = sub.add_parser("check", help="Check the service account token against governance and the ledger; records nothing")
    mc.add_key_arguments(check, "Service account token to check (default: the configured one, when readable)")
    check.add_argument("--api-url", help="Mezmo API URL (default: the configured URLs)")

    managed = sub.add_parser("managed", help="Add the variables to a managed-settings document")
    mc.add_key_arguments(managed, "Service account token (default: $MEZMO_GOVERNANCE_ACCESS_KEY or $MEZMO_GOVERNANCE_TOKEN)")
    managed.add_argument("--api-url", default=DEFAULT_API_URL, help=api_help)
    managed.add_argument("--required", action="store_true", help="Deny every Bash and MCP tool call when governance isn't configured")
    managed.add_argument("--output", type=pathlib.Path, help="Write, or merge into, this file instead of printing")
    return parser


def _codex_api_url(environ: Mapping[str, str]) -> Optional[str]:
    """The api_url the Codex setup saved in ~/.mezmo/codex.json, or None for production or no file."""
    home = environ.get("HOME") or str(pathlib.Path.home())
    try:
        value = json.loads((pathlib.Path(home) / ".mezmo" / "codex.json").read_text(encoding="utf-8")).get("api_url")
    except (OSError, ValueError, AttributeError):
        return None
    return value.strip().rstrip("/") or None if isinstance(value, str) else None


def _key_from(args_key: Optional[str], environ: Mapping[str, str]) -> Optional[str]:
    return args_key or next((environ[k] for k in KEY_ENVS if environ.get(k, "").strip()), None)


def main(
    argv: Optional[Sequence[str]] = None,
    environ: Optional[Mapping[str, str]] = None,
    send: Transport = mc.http_request,
    claude: ClaudeRunner = run_claude,
    managed_files: Optional[Sequence[pathlib.Path]] = None,
) -> int:
    environ = os.environ if environ is None else environ
    args = _build_parser().parse_args(argv)
    settings_file = args.settings_file or mc.default_settings_path(environ)
    managed_files = default_managed_files() if managed_files is None else managed_files
    try:
        if args.command in ("preflight", "configure"):
            preflight(claude, args.plugin_id, args.store)
            if args.command == "preflight":
                print("Ready: Python 3.9+, Claude Code 2.1.295+, and the requested configuration store are available.")
                return 0
        if args.command == "configure":
            key = _key_from(mc.key_argument(args), environ)
            if not key:
                print("Error: no service account token. Pass --key or set MEZMO_GOVERNANCE_ACCESS_KEY.", file=sys.stderr)
                return EXIT_USAGE
            options = plugin_options(claude, args.plugin_id) if args.store == "plugin" else None
            legacy = (options or {}).get("legacy_url") or legacy_url_option(cco.read_settings(settings_file), args.plugin_id)
            api = api_base(args.api_url or legacy or DEFAULT_API_URL)
            url, ledger = governance_url(api), ledger_url(api)
            mcp_url = None
            if not args.no_mcp:
                mcp_url = mcp_config.mcp_url(args.mcp_url) if args.mcp_url else mcp_config.url_for_api(api)
                if mcp_url is None:
                    print(f"Error: {api} isn't the production API, so give its Mezmo MCP URL with --mcp-url, "
                          "or leave MCP out with --no-mcp. Nothing was saved.", file=sys.stderr)
                    return EXIT_USAGE
            ledger_result = None
            if not args.skip_check:
                result = check_access(key, url, send)
                if not result.ok:
                    print(f"Not saved. {result.detail}", file=sys.stderr)
                    return EXIT_REJECTED
                print(result.detail)
                ledger_result = check_ledger(key, ledger, send)
                print(ledger_result.detail)
            if args.store == "plugin":
                try:
                    save_plugin_options(claude, args.plugin_id, {
                        KEY_OPTION: key,
                        API_URL_OPTION: "" if api == DEFAULT_API_URL else api,
                        ENABLED_OPTION: "true",
                        LEDGER_ENABLED_OPTION: "true",
                    })
                except NotInstalled:
                    print(
                        f"The plugin {args.plugin_id} isn't installed, so its options can't be saved. Install it "
                        "(/plugin install mezmo@mezmo inside Claude Code), or run again with --store settings to keep "
                        "the key in the settings file's env instead.",
                        file=sys.stderr,
                    )
                    return EXIT_NOT_INSTALLED
                drop_legacy_url_option(settings_file, args.plugin_id)
                if legacy and not args.api_url:
                    print(f"Moved the governance_url option ({legacy}) to api_url ({api}).")
                where = f"plugin options; the key is {OPTION_KEY[1:-1]}"
            else:
                settings = cco.read_settings(settings_file)
                env = dict(settings.get("env") or {})
                for name in (URL_ENV, LEDGER_URL_ENV) + KEY_ENVS:
                    env.pop(name, None)
                if api != DEFAULT_API_URL:
                    env[URL_ENV] = url
                    env[LEDGER_URL_ENV] = ledger
                env[KEY_ENVS[0]] = key
                cco.write_settings(settings_file, {**settings, "env": env})
                where = f"the env block of {settings_file}"
            print(f"Saved Mezmo service account token {mc.mask(key)} for {api} in {where}.")
            print(f"Mezmo Governance is on: every Bash and MCP tool call is checked at {url} before it runs.")
            print(f"The Mezmo ledger is on: every Bash and MCP tool call is recorded at {ledger}. "
                  "Set the plugin's ledger_enabled option to false to turn it off.")
            if ledger_result is not None and not ledger_result.ok:
                print("Warning: the ledger check failed, so records wait in the retry file until the ledger takes them.")
            if mcp_url:
                if mcp_config.connect(mcp_config.clean_key(key), mcp_url, claude, send,
                                      force=True, check=not args.skip_check) != 0:
                    print("Warning: Mezmo MCP isn't connected, so Claude can't read the ledger. Governance and the "
                          "ledger are set up. Run /mezmo:setup mcp to connect it.")
            print("Start a new Claude Code session to apply it.")
            return 0

        if args.command == "saved-key":
            if not mc.saved_key_present():
                print("No Mezmo key is saved on this machine." if mc.is_macos()
                      else "A saved Mezmo key is looked up only in the macOS Keychain, so not on this system.")
                return EXIT_NO_SAVED_KEY
            print("A Mezmo key is saved on this machine: the Codex setup keeps it in the macOS Keychain.")
            codex_api = _codex_api_url(environ)
            print(f"Codex uses it with the Mezmo API URL {codex_api}." if codex_api
                  else f"Codex uses it with the production Mezmo API URL, {DEFAULT_API_URL}.")
            return 0

        if args.command == "remove":
            removed = []
            options, plugin_error = available_plugin_options(claude, args.plugin_id)
            # A disabled plugin can still hold a real token. The CLI hides sensitive
            # values, so always overwrite a configured key, even on repeated removal.
            if options and (options["key_set"] or options["api_url"]):
                try:
                    save_plugin_options(claude, args.plugin_id, {
                        KEY_OPTION: REMOVED_KEY, API_URL_OPTION: "", ENABLED_OPTION: "false", LEDGER_ENABLED_OPTION: "false",
                    })
                    removed.append(f"the {args.plugin_id} plugin options (the saved key is overwritten)")
                except (RuntimeError, OSError, NotInstalled):
                    plugin_error = "Could not clear the plugin configuration; its saved token may remain."
            settings = cco.read_settings(settings_file)
            env = dict(settings.get("env") or {})
            dropped = [k for k in SETTINGS_KEYS if k in env]
            if dropped:
                for name in dropped:
                    del env[name]
                updated = {**settings, "env": env} if env else {k: v for k, v in settings.items() if k != "env"}
                cco.write_settings(settings_file, updated)
                removed.append(f"{', '.join(dropped)} from {settings_file}")
            print(f"Removed {' and '.join(removed)}." if removed else "No settings were removed." if plugin_error else "Mezmo Governance had nothing to remove.")
            still = [name for name, _ in env_sources({}, environ, managed_files)]
            if still:
                print(f"Still set by {', '.join(still)}; only whoever set those can remove them.")
            print("Start a new Claude Code session to apply it.")
            if plugin_error:
                print(f"Removal incomplete: {plugin_error} Retry removal when Claude configuration is available.", file=sys.stderr)
                return EXIT_ERROR
            return 0

        settings = cco.read_settings(settings_file)
        sources = env_sources(settings, environ, managed_files)

        if args.command == "status":
            options, plugin_error = available_plugin_options(claude, args.plugin_id)
            report = resolve(sources, options)
            if plugin_error and not report["configured"]:
                report["configured"] = None
            report["plugin_error"] = plugin_error
            report.pop("readable_key")
            report["required_by"] = is_required(sources)
            report["plugin_installed"] = None if plugin_error else options is not None
            state_dir = args.state_dir or pathlib.Path(environ.get("MEZMO_GOVERNANCE_STATE_DIR") or DEFAULT_STATE_DIR)
            session = latest_session(pathlib.Path(state_dir).expanduser())
            report["latest_session"] = None if session is None else {
                "session_id": session.get("session_id"),
                "updated": session.get("updated"),
                "counts": session.get("counts") or {},
                "pending": len(session.get("pending") or {}),
            }
            report["last_result"] = (session or {}).get("last")
            ledger = ledger_report(sources, options, environ)
            ledger.pop("readable_key")
            ledger["last_send"] = latest_ledger_send(pathlib.Path(state_dir).expanduser())
            ledger["last_send_ok"], last_send_line = describe_last_send(ledger["last_send"])
            report["ledger"] = ledger
            if args.json:
                print(json.dumps(report, indent=2))
                return 0
            if report["configured"]:
                print("A Governance token is configured, but the plugin state is unknown." if plugin_error
                      else "Mezmo Governance is on: every Bash and MCP tool call is checked before it runs.")
                print(f"  URL:        {report['url']}")
                print(f"  Service account token: {report['key']}, from {report['source']}")
            elif plugin_error:
                print("Mezmo Governance state is unknown: plugin configuration could not be read.")
            elif report["required_by"]:
                print(f"Mezmo Governance is required by {report['required_by']} but not configured: every Bash and MCP tool call is denied.")
            elif report["switched_off"]:
                print("Mezmo Governance is switched off in the plugin options: Bash and MCP tool calls run without a governance check.")
            else:
                print("Mezmo Governance is off: tool calls run without a governance check.")
            if report["required_by"] and report["configured"]:
                print(f"  Required by {report['required_by']}.")
            if plugin_error:
                print(f"  Warning: {plugin_error}")
            latest = report["latest_session"]
            if latest:
                counts = latest["counts"]
                line = (f"  Latest session ({latest['updated']}): {counts.get('allowed', 0)} allowed, "
                        f"{counts.get('denied', 0)} denied, {counts.get('expired', 0)} not decided in time, "
                        f"{counts.get('errors', 0)} errors")
                if latest["pending"]:
                    line += f", {latest['pending']} waiting for a person"
                print(line)
            last = report["last_result"]
            if last:
                detail = f": {last['detail']}" if last.get("detail") else ""
                who = " (decided by a person)" if last.get("by_person") else ""
                print(f"  Last check: {last.get('ts')} {last.get('tool')} {last.get('outcome')}{who}{detail}")
            if ledger["on"]:
                print(f"The Mezmo ledger is on ({ledger['why']}): every Bash and MCP tool call is recorded.")
                print(f"  URL:        {ledger['url']}")
                print(f"  Service account token: {ledger['key']}, from {ledger['source']}")
            else:
                print(f"The Mezmo ledger is off: {ledger['why']}.")
            if ledger["on"] or ledger["last_send"]:
                failed = "FAIL: " if ledger["last_send_ok"] is False else ""
                print(f"  {failed}Ledger: {last_send_line}")
            print(f"  Waiting in the retry file ({ledger['retry_file']}): {ledger['waiting']}")
            return 0

        if args.command == "check":
            # A readable token wins over plugin options, just as it does in the hook.
            options, plugin_error = available_plugin_options(claude, args.plugin_id)
            report = resolve(sources, options)
            key = _key_from(mc.key_argument(args), {}) or report["readable_key"]
            if not key:
                if plugin_error:
                    print(f"Cannot check the token: {plugin_error} Supply --key-file or a configured environment token.", file=sys.stderr)
                    return EXIT_ERROR
                where = ("The service account token is a sensitive plugin option, which this script can't read. Use "
                         "--key-file to check it, or see the hook's last result in status." if report["configured"]
                         else "Mezmo Governance isn't configured.")
                print(f"Error: {where}", file=sys.stderr)
                return EXIT_USAGE
            api = api_base(args.api_url) if args.api_url else None
            url = governance_url(api) if api else report["url"] or DEFAULT_URL
            result = check_access(key, url, send)
            print(f"{'ok  ' if result.ok else 'FAIL'}  {url}  key {mc.mask(key)}  {result.detail}")
            ledger = ledger_report(sources, options, environ)
            if ledger["switched_off"] and not api:
                print(f"skip  the Mezmo ledger is off: {ledger['why']}")
                return 0 if result.ok else EXIT_REJECTED
            ledger_key = _key_from(mc.key_argument(args), {}) or ledger["readable_key"] or key
            ledger_target = ledger_url(api) if api else ledger["url"] or ledger_url(_option_api_url(options))
            ledger_result = check_ledger(ledger_key, ledger_target, send)
            print(f"{'ok  ' if ledger_result.ok else 'FAIL'}  {ledger_target}  key {mc.mask(ledger_key)}  {ledger_result.detail}")
            return 0 if result.ok and ledger_result.ok else EXIT_REJECTED

        if args.command == "managed":
            if args.key_file and not args.output:
                raise ValueError("--output is required with --key-file; do not print managed credentials.")
            key = _key_from(mc.key_argument(args), environ)
            if not key:
                print("Error: no service account token. Pass --key or set MEZMO_GOVERNANCE_ACCESS_KEY.", file=sys.stderr)
                return EXIT_USAGE
            api = api_base(args.api_url)
            env = {URL_ENV: governance_url(api), KEY_ENVS[0]: key, LEDGER_URL_ENV: ledger_url(api)}
            if args.required:
                env[REQUIRED_ENV] = "1"
            if not args.output:
                print(json.dumps({"env": env}, indent=2))
                return 0
            document = cco.read_settings(args.output) if args.output.exists() else {}
            # Replace this script's keys, so a rerun without --required drops it; keep the rest,
            # such as the telemetry keys claude_code_otel.py managed writes to the same file.
            kept = {k: v for k, v in (document.get("env") or {}).items() if k not in SETTINGS_KEYS}
            document["env"] = {**kept, **env}
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            os.chmod(args.output, 0o600)
            print(f"Added Mezmo Governance and the Mezmo ledger ({mc.mask(key)}) to {args.output}. It contains the service account token; handle it like a secret.")
            return 0
    except UnsupportedRuntime as err:
        print(f"Setup stopped: {err} No token was checked or saved.", file=sys.stderr)
        return EXIT_UNSUPPORTED
    except NotInstalled:
        print(f"Plugin {args.plugin_id} is not installed. Install mezmo@mezmo, or explicitly choose --store settings for a supported --plugin-dir session (plaintext token).", file=sys.stderr)
        return EXIT_NOT_INSTALLED
    except RuntimeError as err:
        print(f"Error: {err}", file=sys.stderr)
        return EXIT_ERROR
    except (cco.SettingsError, OSError, ValueError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        mc.cleanup_key_file(args)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
