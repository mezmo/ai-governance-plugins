"""Shared helpers for the Mezmo Claude telemetry scripts (standard library only)."""

from __future__ import annotations

import argparse
import ipaddress
import json
import os
import pathlib
import shutil
import stat
import string
import subprocess
import sys
import tempfile
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


def add_key_arguments(parser: argparse.ArgumentParser, help_text: str) -> None:
    group = parser.add_mutually_exclusive_group()
    group.add_argument("--key", help=help_text + " (prefer --key-file in governed sessions)")
    group.add_argument("--key-file", type=pathlib.Path, help="Read a private UTF-8 key file; never put its contents in a shell command")
    parser.add_argument("--delete-key-file", action="store_true", help="Delete the private key file after this action, including on failure")


def _private_key_fd(path: pathlib.Path) -> Tuple[int, int]:
    """Open without following the file or parent symlink; reject shared files before reading."""
    directory = os.open(str(path.parent), os.O_RDONLY | os.O_DIRECTORY | os.O_NOFOLLOW)
    fd = None
    try:
        parent = os.fstat(directory)
        if parent.st_uid != os.getuid() or parent.st_mode & 0o077:
            raise ValueError("Key file must be in a private directory owned by this user (mode 0700).")
        fd = os.open(path.name, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK, dir_fd=directory)
        info = os.fstat(fd)
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077 or info.st_nlink != 1:
            raise ValueError("Key file must be a regular, unshared file owned by this user (mode 0600).")
        return directory, fd
    except Exception:
        if fd is not None:
            os.close(fd)
        os.close(directory)
        raise


def read_key_file(path: pathlib.Path) -> str:
    try:
        directory, fd = _private_key_fd(path)
        try:
            with os.fdopen(fd, "rb") as stream:
                raw = stream.read(16385)
            if len(raw) > 16384:
                raise ValueError("Key file is too large.")
            key = raw.decode("utf-8").strip()
            if not key or any(c in key for c in "\r\n\x00"):
                raise ValueError("Key file must contain one nonempty token line.")
            return key
        finally:
            os.close(directory)
    except (OSError, UnicodeError):
        raise ValueError("Could not read a private UTF-8 key file; check its ownership, permissions and path.") from None


def key_argument(args: argparse.Namespace) -> Optional[str]:
    if args.delete_key_file and not args.key_file:
        raise ValueError("--delete-key-file requires --key-file.")
    return read_key_file(args.key_file) if args.key_file else args.key


def delete_key_file(path: pathlib.Path) -> None:
    directory, fd = _private_key_fd(path)
    try:
        # Check that the path still names the opened file before unlinking.
        current = os.stat(path.name, dir_fd=directory, follow_symlinks=False)
        opened = os.fstat(fd)
        if (current.st_dev, current.st_ino) != (opened.st_dev, opened.st_ino):
            raise ValueError("Key file changed before cleanup; remove it manually.")
        os.unlink(path.name, dir_fd=directory)
    finally:
        os.close(fd)
        os.close(directory)
    if path.parent.name.startswith("mezmo-key-"):
        try:
            path.parent.rmdir()  # Only an empty helper-created directory.
        except OSError:
            pass


def cleanup_key_file(args: argparse.Namespace) -> None:
    if getattr(args, "delete_key_file", False) and getattr(args, "key_file", None):
        try:
            delete_key_file(args.key_file)
        except (OSError, ValueError):
            print("Warning: private key-file cleanup failed. Remove the temporary file manually.", file=sys.stderr)


def key_file_main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Prepare or remove a private temporary key file; accepts no secret arguments.")
    sub = parser.add_subparsers(dest="command", required=True)
    sub.add_parser("create-key-file")
    remove = sub.add_parser("delete-key-file")
    remove.add_argument("path", type=pathlib.Path)
    args = parser.parse_args(argv)
    try:
        if args.command == "create-key-file":
            folder = pathlib.Path(tempfile.mkdtemp(prefix="mezmo-key-"))
            os.chmod(folder, 0o700)
            path = folder / "key"
            fd = os.open(str(path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o600)
            os.close(fd)
            print(path)
        else:
            delete_key_file(args.path)
        return 0
    except (OSError, ValueError):
        print("Could not prepare or remove the private key file. Check permissions; never print its contents.", file=sys.stderr)
        return 1


def resolve_key(explicit: Optional[str], environ: Mapping[str, str] = os.environ) -> Optional[str]:
    """An explicit --key, else MEZMO_INGESTION_KEY."""
    return explicit or environ.get(KEY_ENV_VAR) or None


class RefuseRedirect(urllib.request.HTTPRedirectHandler):
    """Hand a 3xx back as the answer, so a key never goes to another URL."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):  # type: ignore[no-untyped-def]
        return None


def is_loopback(host: Optional[str]) -> bool:
    if host == "localhost":
        return True
    try:
        return ipaddress.ip_address(host or "").is_loopback
    except ValueError:
        return False


def refuse_exposed_key(url: str, headers: Mapping[str, str]) -> None:
    """Refuse a request that would expose a key. The errors quote neither the URL nor the key."""
    parsed = urllib.parse.urlparse(url)
    if parsed.username is not None or parsed.password is not None:
        raise OSError("the URL must not hold a user name or password")
    if not (parsed.scheme == "https" or (parsed.scheme == "http" and is_loopback(parsed.hostname))):
        raise OSError("the URL must use https://; http:// is allowed only to this machine")
    # http.client quotes a header value it refuses, which would show the key.
    if not all(value.isascii() and value.isprintable() for value in headers.values()):
        raise OSError("a key holds a character an HTTP header cannot carry")


def http_request(
    method: str,
    url: str,
    headers: Mapping[str, str],
    body: Optional[bytes] = None,
    timeout: float = 15.0,
) -> Tuple[int, str]:
    """Send a request and return (status, body). Raises OSError on network failures."""
    refuse_exposed_key(url, headers)
    request = urllib.request.Request(url, data=body, headers=dict(headers), method=method)
    try:
        with urllib.request.build_opener(RefuseRedirect).open(request, timeout=timeout) as response:
            return response.status, response.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as err:
        return err.code, err.read().decode("utf-8", "replace")


def claude_executable() -> str:
    selected = os.environ.get("CLAUDE_CODE_EXECPATH") or "claude"
    found = shutil.which(selected)
    return str(pathlib.Path(found).resolve()) if found else selected


def run_claude(args: Sequence[str], stdin: Optional[str] = None) -> Tuple[int, str]:
    """Run the claude CLI and return (exit code, stdout and stderr).

    Inside a Claude Code session, use the binary running that session, which matches its
    version even when the `claude` on PATH is another install.
    """
    binary = claude_executable()
    try:
        done = subprocess.run([binary, *args], input=stdin, capture_output=True, text=True, timeout=90)
    except FileNotFoundError:
        return 127, "The claude command isn't on PATH."
    except subprocess.TimeoutExpired:
        return 124, f"claude {' '.join(args[:2])} timed out."
    return done.returncode, (done.stdout or "") + (done.stderr or "")


if __name__ == "__main__":
    sys.exit(key_file_main())
