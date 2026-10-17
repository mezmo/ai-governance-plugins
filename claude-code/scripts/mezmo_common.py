"""Shared helpers for the Mezmo Claude telemetry scripts (standard library only)."""

from __future__ import annotations

import json
import os
import pathlib
import string
import subprocess
import urllib.error
import urllib.parse
import urllib.request
from typing import Dict, Mapping, Optional, Sequence, Tuple

MEZMO_OTEL_ENDPOINT = "https://logs.mezmo.com/otel"
MEZMO_AUTH_HEADER = "apikey"
KEY_ENV_VAR = "MEZMO_INGESTION_KEY"
SIGNALS = ("traces", "logs", "metrics")

_KEY_CHARS = frozenset(string.ascii_letters + string.digits + "-_.~+/")


def mask(secret: Optional[str]) -> str:
    """Show only the first and last four characters of a secret."""
    if not secret:
        return "(not set)"
    if len(secret) < 12:
        return "****"
    return f"{secret[:4]}…{secret[-4:]}"


def validate_ingestion_key(key: str) -> str:
    """Return the trimmed key, or raise ValueError if it can't go in an OTLP header list."""
    key = (key or "").strip()
    if not key:
        raise ValueError("The Mezmo ingestion key is empty.")
    bad = sorted({c for c in key if c not in _KEY_CHARS})
    if bad:
        raise ValueError(
            "The Mezmo ingestion key has characters that don't belong in a key: "
            + " ".join(repr(c) for c in bad)
        )
    return key


def parse_otlp_headers(value: Optional[str]) -> Dict[str, str]:
    """Parse OTEL_EXPORTER_OTLP_HEADERS ("k1=v1,k2=v2", values percent-encoded)."""
    headers: Dict[str, str] = {}
    for item in (value or "").split(","):
        name, sep, val = item.partition("=")
        name = name.strip()
        if not sep or not name:
            continue
        headers[name] = urllib.parse.unquote(val.strip())
    return headers


def format_otlp_headers(headers: Mapping[str, str]) -> str:
    return ",".join(f"{name}={urllib.parse.quote(val, safe='+/')}" for name, val in headers.items())


def signal_url(signal: str, base: str = MEZMO_OTEL_ENDPOINT) -> str:
    return f"{base.rstrip('/')}/v1/{signal}"


def is_mezmo_endpoint(value: Optional[str]) -> bool:
    return bool(value) and value.strip().rstrip("/").lower() == MEZMO_OTEL_ENDPOINT.lower()


def default_settings_path(environ: Mapping[str, str] = os.environ) -> pathlib.Path:
    """The user settings file Claude Code reads: $CLAUDE_CONFIG_DIR or ~/.claude."""
    config_dir = environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return pathlib.Path(config_dir).expanduser() / "settings.json"
    home = environ.get("HOME") or str(pathlib.Path.home())
    return pathlib.Path(home) / ".claude" / "settings.json"


def key_from_settings(path: pathlib.Path) -> Optional[str]:
    """The apikey in a settings file's OTEL_EXPORTER_OTLP_HEADERS, if any."""
    try:
        settings = json.loads(path.read_text())
    except (OSError, ValueError):
        return None
    env = settings.get("env") if isinstance(settings, dict) else None
    if not isinstance(env, dict):
        return None
    return parse_otlp_headers(env.get("OTEL_EXPORTER_OTLP_HEADERS")).get(MEZMO_AUTH_HEADER) or None


def resolve_key(explicit: Optional[str], environ: Mapping[str, str] = os.environ) -> Optional[str]:
    """An explicit --key, else MEZMO_INGESTION_KEY."""
    return explicit or environ.get(KEY_ENV_VAR) or None


class RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Hand a 3xx back as the answer, so a key never goes to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def http_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Optional[bytes] = None,
    timeout: float = 15.0,
) -> Tuple[int, str]:
    """Send a request and return (status, body). Raises OSError on network failures."""
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.build_opener(RefuseRedirect).open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


def run_claude(args: Sequence[str], stdin: Optional[str] = None) -> Tuple[int, str]:
    """Run the claude CLI and return (exit code, stdout and stderr).

    Inside a Claude Code session, use the binary running that session, which matches its
    version even when the `claude` on PATH is another install.
    """
    binary = os.environ.get("CLAUDE_CODE_EXECPATH") or "claude"
    try:
        done = subprocess.run([binary, *args], input=stdin, capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        return 127, "The claude command isn't on PATH."
    except subprocess.TimeoutExpired:
        return 124, f"claude {' '.join(args[:2])} timed out."
    return done.returncode, (done.stdout or "") + (done.stderr or "")
