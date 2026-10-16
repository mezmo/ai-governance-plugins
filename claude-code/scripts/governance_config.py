#!/usr/bin/env python3
"""Configure Mezmo Governance for the mezmo plugin's tool-call hook.

The hook (governance_hook.py) reads MEZMO_GOVERNANCE_* environment variables first, then
the plugin's options, which keep the access key in the system keychain.

  configure  check the access key, then save it (plugin options, or the settings file)
  remove     clear the plugin options and the settings-file variables
  status     where the configuration comes from, and the hook's last result
  check      ask the service whether it accepts the access key; records nothing
  managed    add the variables to a managed-settings document
"""

from __future__ import annotations

import argparse
import dataclasses
import json
import os
import pathlib
import subprocess
import sys
import uuid
from typing import Any, Callable, Dict, List, Mapping, Optional, Sequence, Tuple

import claude_code_otel as cco
import mezmo_common as mc

DEFAULT_URL = "https://api.mezmo.com/governance/workflow/authorize"
PLUGIN_ID = "mezmo@mezmo"
URL_OPTION = "governance_url"
KEY_OPTION = "governance_access_key"
ENABLED_OPTION = "governance_enabled"
# Claude Code keeps a sensitive option's value when given "", so remove overwrites the key
# with this placeholder, which governance_hook treats as unset, and turns the switch off.
REMOVED_KEY = "off"
URL_ENV = "MEZMO_GOVERNANCE_URL"
KEY_ENVS = ("MEZMO_GOVERNANCE_ACCESS_KEY", "MEZMO_GOVERNANCE_TOKEN")
REQUIRED_ENV = "MEZMO_GOVERNANCE_REQUIRED"
SETTINGS_KEYS = (URL_ENV,) + KEY_ENVS + (REQUIRED_ENV,)
# Written by governance_hook.py, one file per Claude Code session.
DEFAULT_STATE_DIR = pathlib.Path.home() / ".mezmo" / "governance" / "sessions"

EXIT_ERROR, EXIT_USAGE, EXIT_REJECTED, EXIT_NOT_INSTALLED = 1, 2, 3, 4

Transport = Callable[..., Tuple[int, str]]
ClaudeRunner = Callable[[Sequence[str], Optional[str]], Tuple[int, str]]


@dataclasses.dataclass
class CheckResult:
    ok: bool
    status: Optional[int]
    detail: str


class NotInstalled(Exception):
    pass


def run_claude(args: Sequence[str], stdin: Optional[str] = None) -> Tuple[int, str]:
    """Run the claude CLI and return (exit code, stdout and stderr).

    Inside a Claude Code session, use the binary running that session, which matches its
    version even when the `claude` on PATH is another install.
    """
    binary = os.environ.get("CLAUDE_CODE_EXECPATH") or "claude"
    try:
        done = subprocess.run([binary, *args], input=stdin, capture_output=True, text=True, timeout=60)
    except FileNotFoundError:
        return 127, "The claude command isn't on PATH."
    except subprocess.TimeoutExpired:
        return 124, "claude plugin configure timed out."
    return done.returncode, (done.stdout or "") + (done.stderr or "")


def authorization(key: str) -> str:
    return key if key.lower().startswith(("token ", "bearer ")) else f"Token {key}"


def check_access(key: str, url: str, send: Transport = mc.http_request) -> CheckResult:
    """GET the status of a random decision. 404 means the service accepted the key."""
    probe = f"{url}{'&' if '?' in url else '?'}decision_id={uuid.uuid4()}"
    headers = {"Authorization": authorization(key), "Accept": "application/json"}
    try:
        status, body = send("GET", probe, headers, None, 20.0)
    except OSError as err:
        return CheckResult(False, None, f"Couldn't reach Mezmo Governance at {url}: {err}")
    if status == 404:
        return CheckResult(True, status, "Mezmo Governance accepted the access key. Nothing was recorded.")
    if status in (401, 403):
        return CheckResult(False, status, f"Mezmo Governance rejected the access key (HTTP {status}).")
    if status == 402:
        return CheckResult(False, status, "The Mezmo plan doesn't include governance (HTTP 402).")
    return CheckResult(False, status, f"Unexpected answer from Mezmo Governance (HTTP {status}): {body.strip()[:200]}")


def plugin_options(claude: ClaudeRunner, plugin_id: str) -> Optional[Dict[str, Any]]:
    """{'key_set', 'enabled', 'url'} from `claude plugin configure --json`, or None if not installed."""
    code, output = claude(["plugin", "configure", plugin_id, "--json"], None)
    if code != 0:
        return None
    try:
        data = json.loads(output)
    except ValueError:
        return None
    configured = data.get("configured") or []
    inputs = data.get("inputs") or {}
    url = inputs.get(URL_OPTION) if URL_OPTION in configured else None
    enabled = inputs.get(ENABLED_OPTION, True)
    if isinstance(enabled, str):
        enabled = enabled.strip().lower() not in ("0", "false", "no", "off")
    return {"key_set": KEY_OPTION in configured, "enabled": bool(enabled), "url": url or None}


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
    raise RuntimeError(refused or output.strip()[:300] or f"claude plugin configure exited {code}")


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


def resolve(sources: Sequence[Tuple[str, Mapping[str, str]]], options: Optional[Mapping[str, Any]]) -> Dict[str, Any]:
    """What the hook will use: the first source with a key wins, as in governance_hook.load_config."""
    report: Dict[str, Any] = {"configured": False, "source": None, "url": None, "key": "(not set)", "readable_key": None}
    for name, values in sources:
        key = next((values[k] for k in KEY_ENVS if k in values), None)
        if key:
            url = next((v.get(URL_ENV) for _, v in sources if v.get(URL_ENV)), None)
            url = url or (options or {}).get("url") or DEFAULT_URL
            report.update(configured=True, source=name, url=url, key=mc.mask(key), readable_key=key)
            return report
    report["switched_off"] = bool(options and options.get("key_set") and not options.get("enabled"))
    if options and options.get("key_set") and options.get("enabled"):
        url = next((v.get(URL_ENV) for _, v in sources if v.get(URL_ENV)), None) or options.get("url") or DEFAULT_URL
        report.update(configured=True, source="plugin options", url=url, key="(in the system keychain)")
    return report


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


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="governance_config.py", description="Configure Mezmo Governance for Claude Code.")
    parser.add_argument("--settings-file", type=pathlib.Path,
                        help="User settings file (default: $CLAUDE_CONFIG_DIR/settings.json or ~/.claude/settings.json)")
    parser.add_argument("--plugin-id", default=PLUGIN_ID, help=f"Installed plugin id (default: {PLUGIN_ID})")
    sub = parser.add_subparsers(dest="command", required=True)

    configure = sub.add_parser("configure", help="Check the access key, then save it")
    configure.add_argument("--key", help="Agent access key (default: $MEZMO_GOVERNANCE_ACCESS_KEY or $MEZMO_GOVERNANCE_TOKEN)")
    configure.add_argument("--url", default=DEFAULT_URL, help=f"Authorize URL (default: {DEFAULT_URL})")
    configure.add_argument("--store", choices=("plugin", "settings"), default="plugin",
                           help="plugin: plugin options, key in the system keychain (default); settings: the settings file's env")
    configure.add_argument("--skip-check", action="store_true", help="Save without checking the key first")

    sub.add_parser("remove", help="Clear the plugin options and the settings-file variables")

    status = sub.add_parser("status", help="Show where the configuration comes from and the last result")
    status.add_argument("--json", action="store_true")
    status.add_argument("--state-dir", type=pathlib.Path,
                        help="Where the hook keeps session status files (default: ~/.mezmo/governance/sessions)")

    check = sub.add_parser("check", help="Check the access key against the service; records nothing")
    check.add_argument("--key", help="Access key to check (default: the configured one, when readable)")
    check.add_argument("--url", help="Authorize URL (default: the configured one)")

    managed = sub.add_parser("managed", help="Add the variables to a managed-settings document")
    managed.add_argument("--key", help="Agent access key (default: $MEZMO_GOVERNANCE_ACCESS_KEY or $MEZMO_GOVERNANCE_TOKEN)")
    managed.add_argument("--url", default=DEFAULT_URL)
    managed.add_argument("--required", action="store_true", help="Deny every tool call when governance isn't configured")
    managed.add_argument("--output", type=pathlib.Path, help="Write, or merge into, this file instead of printing")
    return parser


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
        if args.command == "configure":
            key = _key_from(args.key, environ)
            if not key:
                print("Error: no access key. Pass --key or set MEZMO_GOVERNANCE_ACCESS_KEY.", file=sys.stderr)
                return EXIT_USAGE
            if not args.skip_check:
                result = check_access(key, args.url, send)
                if not result.ok:
                    print(f"Not saved. {result.detail}", file=sys.stderr)
                    return EXIT_REJECTED
                print(result.detail)
            if args.store == "plugin":
                try:
                    save_plugin_options(claude, args.plugin_id, {
                        KEY_OPTION: key,
                        URL_OPTION: "" if args.url == DEFAULT_URL else args.url,
                        ENABLED_OPTION: "true",
                    })
                except NotInstalled:
                    print(
                        f"The plugin {args.plugin_id} isn't installed, so its options can't be saved. Install it "
                        "(claude plugin install mezmo@mezmo), or run again with --store settings to keep the key "
                        "in the settings file's env instead.",
                        file=sys.stderr,
                    )
                    return EXIT_NOT_INSTALLED
                where = "plugin options; the key is in the system keychain"
            else:
                settings = cco.read_settings(settings_file)
                env = dict(settings.get("env") or {})
                for name in (URL_ENV,) + KEY_ENVS:
                    env.pop(name, None)
                if args.url != DEFAULT_URL:
                    env[URL_ENV] = args.url
                env[KEY_ENVS[0]] = key
                cco.write_settings(settings_file, {**settings, "env": env})
                where = f"the env block of {settings_file}"
            print(f"Saved Mezmo Governance access key {mc.mask(key)} for {args.url} in {where}.")
            print("Every tool call is now checked with Mezmo Governance. Start a new Claude Code session to apply it.")
            return 0

        if args.command == "remove":
            removed = []
            options = plugin_options(claude, args.plugin_id)
            if options and ((options["key_set"] and options["enabled"]) or options["url"]):
                save_plugin_options(claude, args.plugin_id, {
                    KEY_OPTION: REMOVED_KEY, URL_OPTION: "", ENABLED_OPTION: "false",
                })
                removed.append(f"the {args.plugin_id} plugin options (the key in the keychain is overwritten)")
            settings = cco.read_settings(settings_file)
            env = dict(settings.get("env") or {})
            dropped = [k for k in SETTINGS_KEYS if k in env]
            if dropped:
                for name in dropped:
                    del env[name]
                updated = {**settings, "env": env} if env else {k: v for k, v in settings.items() if k != "env"}
                cco.write_settings(settings_file, updated)
                removed.append(f"{', '.join(dropped)} from {settings_file}")
            print(f"Removed {' and '.join(removed)}." if removed else "Mezmo Governance had nothing to remove.")
            still = [name for name, _ in env_sources({}, environ, managed_files)]
            if still:
                print(f"Still set by {', '.join(still)}; only whoever set those can remove them.")
            print("Start a new Claude Code session to apply it.")
            return 0

        settings = cco.read_settings(settings_file)
        sources = env_sources(settings, environ, managed_files)

        if args.command == "status":
            options = plugin_options(claude, args.plugin_id)
            report = resolve(sources, options)
            report.pop("readable_key")
            report["required_by"] = is_required(sources)
            report["plugin_installed"] = options is not None
            state_dir = args.state_dir or pathlib.Path(environ.get("MEZMO_GOVERNANCE_STATE_DIR") or DEFAULT_STATE_DIR)
            session = latest_session(pathlib.Path(state_dir).expanduser())
            report["latest_session"] = None if session is None else {
                "session_id": session.get("session_id"),
                "updated": session.get("updated"),
                "counts": session.get("counts") or {},
                "pending": len(session.get("pending") or {}),
            }
            report["last_result"] = (session or {}).get("last")
            if args.json:
                print(json.dumps(report, indent=2))
                return 0
            if report["configured"]:
                print("Mezmo Governance is on: every tool call is checked before it runs.")
                print(f"  URL:        {report['url']}")
                print(f"  Access key: {report['key']}, from {report['source']}")
            elif report["required_by"]:
                print(f"Mezmo Governance is required by {report['required_by']} but not configured: every tool call is denied.")
            elif report["switched_off"]:
                print("Mezmo Governance is switched off in the plugin options: tool calls run without a governance check.")
            else:
                print("Mezmo Governance is off: tool calls run without a governance check.")
            if report["required_by"] and report["configured"]:
                print(f"  Required by {report['required_by']}.")
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
            return 0

        if args.command == "check":
            report = resolve(sources, plugin_options(claude, args.plugin_id))
            key = _key_from(args.key, {}) or report["readable_key"]
            if not key:
                where = ("The access key is in the system keychain, where this script can't read it. Pass --key to "
                         "check it, or see the hook's last result in status." if report["configured"]
                         else "Mezmo Governance isn't configured.")
                print(f"Error: {where}", file=sys.stderr)
                return EXIT_USAGE
            url = args.url or report["url"] or DEFAULT_URL
            result = check_access(key, url, send)
            print(f"{'ok  ' if result.ok else 'FAIL'}  {url}  key {mc.mask(key)}  {result.detail}")
            return 0 if result.ok else EXIT_REJECTED

        if args.command == "managed":
            key = _key_from(args.key, environ)
            if not key:
                print("Error: no access key. Pass --key or set MEZMO_GOVERNANCE_ACCESS_KEY.", file=sys.stderr)
                return EXIT_USAGE
            env = {URL_ENV: args.url, KEY_ENVS[0]: key}
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
            print(f"Added Mezmo Governance ({mc.mask(key)}) to {args.output}. It contains the access key; handle it like a secret.")
            return 0
    except RuntimeError as err:
        print(f"Error: claude plugin configure refused the values: {err}", file=sys.stderr)
        return EXIT_ERROR
    except (cco.SettingsError, OSError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return EXIT_ERROR
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
