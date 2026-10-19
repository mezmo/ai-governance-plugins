#!/usr/bin/env python3
"""Point Claude Code's OpenTelemetry export at Mezmo.

Claude Code reads its telemetry settings from environment variables at startup, and a
plugin can't set those, so this script edits the `env` block of the user settings file
(~/.claude/settings.json, or $CLAUDE_CONFIG_DIR/settings.json).

  configure  write the Mezmo keys into the settings file
  remove     take them out again
  status     show what is configured
  managed    print a managed-settings document for organization-wide rollout
"""

from __future__ import annotations

import argparse
import copy
import dataclasses
import datetime
import json
import os
import pathlib
import shutil
import stat
import sys
import tempfile
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple

import mezmo_common as mc

ENABLE_KEY = "CLAUDE_CODE_ENABLE_TELEMETRY"
TRACES_BETA_KEY = "CLAUDE_CODE_ENHANCED_TELEMETRY_BETA"
ENDPOINT_KEY = "OTEL_EXPORTER_OTLP_ENDPOINT"
PROTOCOL_KEY = "OTEL_EXPORTER_OTLP_PROTOCOL"
HEADERS_KEY = "OTEL_EXPORTER_OTLP_HEADERS"
RESOURCE_KEY = "OTEL_RESOURCE_ATTRIBUTES"
PROMPTS_KEY = "OTEL_LOG_USER_PROMPTS"
RESPONSES_KEY = "OTEL_LOG_ASSISTANT_RESPONSES"
TOOL_DETAILS_KEY = "OTEL_LOG_TOOL_DETAILS"
HEADERS_HELPER_KEY = "otelHeadersHelper"

SELECTOR_KEYS = {
    "metrics": "OTEL_METRICS_EXPORTER",
    "logs": "OTEL_LOGS_EXPORTER",
    "traces": "OTEL_TRACES_EXPORTER",
}
CONTENT_KEYS = (PROMPTS_KEY, RESPONSES_KEY, TOOL_DETAILS_KEY)
# Keys whose only purpose is the Mezmo export; configure rewrites them and remove deletes them.
OWNED_KEYS = (ENDPOINT_KEY, PROTOCOL_KEY, HEADERS_KEY) + CONTENT_KEYS
# Per-signal settings take precedence over the general ones, so they would bypass Mezmo.
PER_SIGNAL_KEYS = tuple(
    f"OTEL_EXPORTER_OTLP_{signal}_{field}"
    for signal in ("METRICS", "LOGS", "TRACES")
    for field in ("ENDPOINT", "PROTOCOL", "HEADERS")
)
PROTOCOL = "http/protobuf"

MANAGED_SETTINGS_DIRS = {
    "darwin": "/Library/Application Support/ClaudeCode",
    "linux": "/etc/claude-code",
    "win32": r"C:\Program Files\ClaudeCode",
}

_RESOURCE_FORBIDDEN = set(' \t\r\n",;\\')

EXIT_ERROR = 1
EXIT_USAGE = 2
EXIT_CONFLICT = 3
EXIT_NOT_CONFIGURED = 4


class SettingsError(Exception):
    """The settings file can't be read or has the wrong shape."""


class ConflictError(Exception):
    """The settings already send OpenTelemetry somewhere other than Mezmo."""

    def __init__(self, keys: Sequence[str]):
        self.keys = list(keys)
        super().__init__(", ".join(self.keys))


class NotConfiguredError(Exception):
    """The settings don't send OpenTelemetry to Mezmo."""


@dataclasses.dataclass
class Options:
    key: str
    metrics: bool = True
    logs: bool = True
    traces: bool = True
    log_prompts: bool = False
    log_responses: bool = False
    log_tool_details: bool = False
    resource_attributes: Optional[str] = None

    def signals(self) -> Dict[str, bool]:
        return {"metrics": self.metrics, "logs": self.logs, "traces": self.traces}


def split_exporters(value: Optional[str]) -> List[str]:
    return [item.strip().lower() for item in (value or "").split(",") if item.strip()]


def parse_resource_attributes(value: Optional[str], strict: bool = True) -> Dict[str, str]:
    attrs: Dict[str, str] = {}
    for item in (value or "").split(","):
        item = item.strip()
        if not item:
            continue
        name, sep, val = item.partition("=")
        bad = any(c in _RESOURCE_FORBIDDEN or not 32 < ord(c) < 127 for c in name + val)
        if not sep or not name or bad:
            if strict:
                raise ValueError(
                    f"Resource attribute {item!r} must be key=value with no spaces, quotes, "
                    "commas, semicolons or backslashes. Percent-encode anything else, "
                    "for example org=Acme%20Corp."
                )
            continue
        attrs[name] = val
    return attrs


def format_resource_attributes(attrs: Mapping[str, str]) -> str:
    return ",".join(f"{name}={val}" for name, val in attrs.items())


def build_env(opts: Options) -> Dict[str, str]:
    """The env entries that send Claude Code telemetry to Mezmo."""
    key = mc.validate_ingestion_key(opts.key)
    if not any(opts.signals().values()):
        raise ValueError("Turn on at least one of metrics, events (logs) or traces.")
    env = {ENABLE_KEY: "1"}
    for signal, enabled in opts.signals().items():
        if enabled:
            env[SELECTOR_KEYS[signal]] = "otlp"
    if opts.traces:
        env[TRACES_BETA_KEY] = "1"
    env[PROTOCOL_KEY] = PROTOCOL
    env[ENDPOINT_KEY] = mc.MEZMO_OTEL_ENDPOINT
    env[HEADERS_KEY] = mc.format_otlp_headers({mc.MEZMO_AUTH_HEADER: key})
    if opts.log_prompts:
        env[PROMPTS_KEY] = "1"
        # Responses fall back to the prompt setting when unset, so pin them explicitly.
        env[RESPONSES_KEY] = "1" if opts.log_responses else "0"
    elif opts.log_responses:
        env[RESPONSES_KEY] = "1"
    if opts.log_tool_details:
        env[TOOL_DETAILS_KEY] = "1"
    if opts.resource_attributes:
        env[RESOURCE_KEY] = format_resource_attributes(parse_resource_attributes(opts.resource_attributes))
    return env


def _env_of(settings: Mapping[str, Any]) -> Dict[str, Any]:
    env = settings.get("env", {})
    if not isinstance(env, dict):
        raise SettingsError('"env" in the settings file is not a JSON object.')
    return env


def find_conflicts(settings: Mapping[str, Any]) -> List[str]:
    """Keys that already send OpenTelemetry somewhere else, or would override the Mezmo setup."""
    env = _env_of(settings)
    conflicts = [key for key in PER_SIGNAL_KEYS if key in env]
    if HEADERS_HELPER_KEY in settings:
        conflicts.append(HEADERS_HELPER_KEY)
    if not mc.is_mezmo_endpoint(env.get(ENDPOINT_KEY)):
        if env.get(ENDPOINT_KEY):
            conflicts.append(ENDPOINT_KEY)
        if env.get(HEADERS_KEY):
            conflicts.append(HEADERS_KEY)
        for key in SELECTOR_KEYS.values():
            if "otlp" in split_exporters(env.get(key)):
                conflicts.append(key)
    return conflicts


def _has_real_exporter(env: Mapping[str, Any], key: str) -> bool:
    return any(exporter != "none" for exporter in split_exporters(env.get(key)))


def apply_configure(settings: Mapping[str, Any], opts: Options, force: bool = False) -> Dict[str, Any]:
    """Return a copy of settings that sends Claude Code telemetry to Mezmo."""
    desired = build_env(opts)
    result = copy.deepcopy(dict(settings))
    env = _env_of(result)
    conflicts = find_conflicts(result)
    if conflicts and not force:
        raise ConflictError(conflicts)
    result.pop(HEADERS_HELPER_KEY, None)
    for key in PER_SIGNAL_KEYS + OWNED_KEYS:
        env.pop(key, None)

    for signal, enabled in opts.signals().items():
        key = SELECTOR_KEYS[signal]
        exporters = [e for e in split_exporters(env.get(key)) if e != "otlp"]
        if enabled:
            exporters = [e for e in exporters if e != "none"] + ["otlp"]
        if exporters:
            env[key] = ",".join(exporters)
        else:
            env.pop(key, None)

    if _has_real_exporter(env, SELECTOR_KEYS["traces"]):
        env[TRACES_BETA_KEY] = "1"
    else:
        env.pop(TRACES_BETA_KEY, None)

    if RESOURCE_KEY in desired:
        merged = parse_resource_attributes(env.get(RESOURCE_KEY), strict=False)
        merged.update(parse_resource_attributes(desired[RESOURCE_KEY]))
        env[RESOURCE_KEY] = format_resource_attributes(merged)

    for key, value in desired.items():
        if key not in SELECTOR_KEYS.values() and key not in (TRACES_BETA_KEY, RESOURCE_KEY):
            env[key] = value
    result["env"] = env
    return result


def apply_remove(settings: Mapping[str, Any], force: bool = False) -> Tuple[Dict[str, Any], List[str]]:
    """Return a copy of settings without the Mezmo export, and the keys that changed."""
    result = copy.deepcopy(dict(settings))
    env = _env_of(result)
    if not mc.is_mezmo_endpoint(env.get(ENDPOINT_KEY)) and not force:
        raise NotConfiguredError("Claude Code is not configured to send telemetry to Mezmo.")
    changed = [key for key in OWNED_KEYS if key in env]
    for key in changed:
        del env[key]
    for key in SELECTOR_KEYS.values():
        exporters = split_exporters(env.get(key))
        if "otlp" in exporters:
            changed.append(key)
            rest = [e for e in exporters if e != "otlp"]
            if rest:
                env[key] = ",".join(rest)
            else:
                del env[key]
    if TRACES_BETA_KEY in env and not _has_real_exporter(env, SELECTOR_KEYS["traces"]):
        del env[TRACES_BETA_KEY]
        changed.append(TRACES_BETA_KEY)
    if ENABLE_KEY in env and not any(_has_real_exporter(env, key) for key in SELECTOR_KEYS.values()):
        del env[ENABLE_KEY]
        changed.append(ENABLE_KEY)
    if env:
        result["env"] = env
    else:
        result.pop("env", None)
    return result, changed


def read_settings(path: pathlib.Path) -> Dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return {}
    except OSError as err:
        raise SettingsError(f"Can't read {path}: {err}") from err
    if not text.strip():
        return {}
    try:
        settings = json.loads(text)
    except ValueError as err:
        raise SettingsError(f"{path} is not valid JSON ({err}). Fix it and try again.") from err
    if not isinstance(settings, dict):
        raise SettingsError(f"{path} must hold a JSON object.")
    return settings


def write_settings(path: pathlib.Path, settings: Mapping[str, Any]) -> Optional[pathlib.Path]:
    """Write settings atomically. Returns the backup of the previous file, if there was one."""
    if path.is_symlink():
        path = path.resolve()  # keep a dotfiles symlink pointing at the real file
    path.parent.mkdir(parents=True, exist_ok=True)
    mode = 0o600
    backup = None
    if path.exists():
        mode = stat.S_IMODE(path.stat().st_mode)
        stamp = datetime.datetime.now(datetime.timezone.utc).strftime("%Y%m%dT%H%M%S%fZ")
        backup = path.with_name(f"{path.name}.mezmo-backup-{stamp}")
        shutil.copyfile(path, backup)
        os.chmod(backup, 0o600)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8") as handle:
            handle.write(json.dumps(settings, indent=2, ensure_ascii=False) + "\n")
        os.chmod(tmp, mode)
        os.replace(tmp, path)
    except BaseException:
        if os.path.exists(tmp):
            os.unlink(tmp)
        raise
    return backup


def managed_document(opts: Options) -> Dict[str, Any]:
    return {"env": build_env(opts)}


# Every env key a managed document from this script can hold, so a rerun replaces them all.
MANAGED_KEYS = OWNED_KEYS + tuple(SELECTOR_KEYS.values()) + (TRACES_BETA_KEY, ENABLE_KEY, RESOURCE_KEY)


def merge_managed(existing: Mapping[str, Any], opts: Options) -> Dict[str, Any]:
    """`existing` with this script's telemetry keys replaced. Other keys, such as governance's, stay."""
    document = copy.deepcopy(dict(existing))
    env = {k: v for k, v in _env_of(document).items() if k not in MANAGED_KEYS}
    env.update(build_env(opts))
    document["env"] = env
    return document


def status(settings: Mapping[str, Any]) -> Dict[str, Any]:
    env = _env_of(settings)
    configured = mc.is_mezmo_endpoint(env.get(ENDPOINT_KEY))
    key = mc.parse_otlp_headers(env.get(HEADERS_KEY)).get(mc.MEZMO_AUTH_HEADER) if configured else None
    signals = {
        signal: configured and "otlp" in split_exporters(env.get(selector))
        for signal, selector in SELECTOR_KEYS.items()
    }
    signals["traces"] = signals["traces"] and env.get(TRACES_BETA_KEY) in ("1", "true")
    warnings = []
    if configured:
        if env.get(ENABLE_KEY) not in ("1", "true"):
            warnings.append(f"{ENABLE_KEY} is not 1, so Claude Code exports nothing.")
        if env.get(PROTOCOL_KEY) not in ("http/protobuf", "http/json"):
            warnings.append(f"{PROTOCOL_KEY} is {env.get(PROTOCOL_KEY)!r}; Mezmo accepts only http/protobuf or http/json.")
        if not key:
            warnings.append(f"{HEADERS_KEY} has no apikey header, so Mezmo will reject the data.")
        if not any(signals.values()):
            warnings.append("No signal is exported with otlp.")
    return {
        "configured": configured,
        "endpoint": env.get(ENDPOINT_KEY),
        "protocol": env.get(PROTOCOL_KEY),
        "key": mc.mask(key),
        "signals": signals,
        "content_logging": {
            "prompts": env.get(PROMPTS_KEY) == "1",
            "responses": env.get(RESPONSES_KEY, env.get(PROMPTS_KEY)) == "1",
            "tool_details": env.get(TOOL_DETAILS_KEY) == "1",
        },
        "resource_attributes": env.get(RESOURCE_KEY),
        "conflicts": find_conflicts(settings),
        "warnings": warnings,
    }


def managed_settings_with_otel(platform: str = sys.platform) -> List[str]:
    """Managed settings files on this machine that set telemetry; they override user settings."""
    base = MANAGED_SETTINGS_DIRS.get(platform)
    if not base:
        return []
    root = pathlib.Path(base)
    candidates = [root / "managed-settings.json"]
    dropins = root / "managed-settings.d"
    if dropins.is_dir():
        candidates += sorted(dropins.glob("*.json"))
    found = []
    for candidate in candidates:
        try:
            env = json.loads(candidate.read_text(encoding="utf-8")).get("env", {})
        except (OSError, ValueError, AttributeError):
            continue
        if isinstance(env, dict) and any(k == ENABLE_KEY or k.startswith("OTEL_") for k in env):
            found.append(str(candidate))
    return found


def _describe_signals(signals: Mapping[str, bool]) -> str:
    names = {"metrics": "metrics", "logs": "events (logs)", "traces": "traces (beta)"}
    on = [names[s] for s, enabled in signals.items() if enabled]
    return ", ".join(on) if on else "none"


def _describe_content(content: Mapping[str, bool]) -> str:
    return ", ".join(f"{name.replace('_', ' ')}: {'on' if on else 'off'}" for name, on in content.items())


def _print_status(path: pathlib.Path, report: Mapping[str, Any]) -> None:
    print(f"  Settings file: {path}")
    print(f"  Endpoint:      {report['endpoint'] or '(not set)'} ({report['protocol'] or 'no protocol'})")
    print(f"  Ingestion key: {report['key']}")
    print(f"  Signals:       {_describe_signals(report['signals'])}")
    print(f"  Content:       {_describe_content(report['content_logging'])}")
    if report["resource_attributes"]:
        print(f"  Attributes:    {report['resource_attributes']}")
    for warning in report["warnings"]:
        print(f"  Warning: {warning}")
    if report["conflicts"]:
        print(f"  Conflicting settings: {', '.join(report['conflicts'])}")
    for managed in report.get("managed_settings_files", []):
        print(f"  Note: {managed} sets telemetry too, and managed settings override this file.")


def configured_key(settings: Mapping[str, Any]) -> Optional[str]:
    """The apikey of an existing Mezmo setup, if there is one."""
    env = _env_of(settings)
    if not mc.is_mezmo_endpoint(env.get(ENDPOINT_KEY)):
        return None
    return mc.parse_otlp_headers(env.get(HEADERS_KEY)).get(mc.MEZMO_AUTH_HEADER) or None


def _options_from_args(
    args: argparse.Namespace, environ: Mapping[str, str], fallback_key: Optional[str] = None
) -> Options:
    key = mc.resolve_key(mc.key_argument(args), environ) or fallback_key
    if not key:
        raise ValueError(f"No ingestion key. Pass --key or set {mc.KEY_ENV_VAR}.")
    return Options(
        key=key,
        metrics=not args.no_metrics,
        logs=not args.no_logs,
        traces=not args.no_traces,
        log_prompts=args.log_prompts,
        log_responses=args.log_responses,
        log_tool_details=args.log_tool_details,
        resource_attributes=args.resource_attributes,
    )


def _build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="claude_code_otel.py",
        description="Send Claude Code OpenTelemetry metrics, events and traces to Mezmo.",
    )
    parser.add_argument(
        "--settings-file",
        type=pathlib.Path,
        help="Settings file to edit (default: $CLAUDE_CONFIG_DIR/settings.json or ~/.claude/settings.json)",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    def add_export_options(p: argparse.ArgumentParser) -> None:
        mc.add_key_arguments(p, f"Mezmo ingestion key (default: ${mc.KEY_ENV_VAR})")
        p.add_argument("--no-metrics", action="store_true", help="Don't export metrics")
        p.add_argument("--no-logs", action="store_true", help="Don't export events (OTel logs)")
        p.add_argument("--no-traces", action="store_true", help="Don't export traces (beta)")
        p.add_argument("--log-prompts", action="store_true", help="Include prompt text in events and spans")
        p.add_argument("--log-responses", action="store_true", help="Include Claude's response text in events")
        p.add_argument("--log-tool-details", action="store_true", help="Include tool inputs, commands and file paths")
        p.add_argument("--resource-attributes", help="Extra attributes, e.g. team.id=platform,cost_center=eng-123")

    configure = sub.add_parser("configure", help="Write the Mezmo settings")
    add_export_options(configure)
    configure.add_argument("--force", action="store_true", help="Replace another OpenTelemetry destination")

    remove = sub.add_parser("remove", help="Remove the Mezmo settings")
    remove.add_argument("--force", action="store_true", help="Remove the OTLP settings even if they don't point at Mezmo")

    status_cmd = sub.add_parser("status", help="Show the current settings")
    status_cmd.add_argument("--json", action="store_true", help="Print JSON")

    managed = sub.add_parser("managed", help="Print a managed-settings document instead of editing a file")
    add_export_options(managed)
    managed.add_argument("--output", type=pathlib.Path, help="Write the document to this file instead of stdout")
    return parser


def main(argv: Optional[Sequence[str]] = None, environ: Optional[Mapping[str, str]] = None) -> int:
    environ = os.environ if environ is None else environ
    args = _build_parser().parse_args(argv)
    path = args.settings_file or mc.default_settings_path(environ)
    try:
        if args.command == "managed":
            if args.key_file and not args.output:
                raise ValueError("--output is required with --key-file; do not print managed credentials.")
            opts = _options_from_args(args, environ)
            if not args.output:
                sys.stdout.write(json.dumps(managed_document(opts), indent=2) + "\n")
                return 0
            # Merge, so governance_config.py managed can write to the same file in either order.
            document = merge_managed(read_settings(args.output), opts)
            args.output.parent.mkdir(parents=True, exist_ok=True)
            args.output.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
            os.chmod(args.output, 0o600)
            print(f"Wrote telemetry to {args.output}. It contains the ingestion key; copy it into place and delete it.")
            return 0

        settings = read_settings(path)

        if args.command == "status":
            report = status(settings)
            report["settings_file"] = str(path)
            report["managed_settings_files"] = managed_settings_with_otel()
            if args.json:
                print(json.dumps(report, indent=2))
            else:
                print("Claude Code sends telemetry to Mezmo." if report["configured"]
                      else "Claude Code is not configured to send telemetry to Mezmo.")
                _print_status(path, report)
            return 0

        if args.command == "configure":
            opts = _options_from_args(args, environ, fallback_key=configured_key(settings))
            updated = apply_configure(settings, opts, force=args.force)
            backup = write_settings(path, updated)
            report = status(updated)
            report["managed_settings_files"] = managed_settings_with_otel()
            print("Configured Claude Code to send telemetry to Mezmo.")
            _print_status(path, report)
            print(f"  Backup:        {backup or 'none (new file)'}")
            print("Start a new Claude Code session for the change to take effect.")
            return 0

        if args.command == "remove":
            updated, changed = apply_remove(settings, force=args.force)
            if not changed:
                print(f"Nothing to remove from {path}.")
                return 0
            backup = write_settings(path, updated)
            print(f"Removed {', '.join(changed)} from {path}.")
            print(f"  Backup: {backup}")
            print("Start a new Claude Code session for the change to take effect.")
            return 0
    except ConflictError as err:
        print(
            f"{path} already sends OpenTelemetry somewhere else, or has settings that would "
            f"override Mezmo: {', '.join(err.keys)}.\n"
            "Claude Code exports to one OTLP endpoint. Run again with --force to replace them "
            "(the current file is backed up first).",
            file=sys.stderr,
        )
        return EXIT_CONFLICT
    except NotConfiguredError as err:
        print(f"{err} ({path}). Use --force to remove the OTLP settings anyway.", file=sys.stderr)
        return EXIT_NOT_CONFIGURED
    except ValueError as err:
        print(f"Error: {err}", file=sys.stderr)
        return EXIT_USAGE
    except (SettingsError, OSError) as err:
        print(f"Error: {err}", file=sys.stderr)
        return EXIT_ERROR
    finally:
        mc.cleanup_key_file(args)
    return EXIT_USAGE


if __name__ == "__main__":
    sys.exit(main())
