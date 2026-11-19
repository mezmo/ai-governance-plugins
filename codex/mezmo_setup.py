#!/usr/bin/env python3
"""Set up the Mezmo Codex hooks: the Mezmo API URL and key, checked, then saved.

Run it in your own terminal, not through Codex:

    python3 mezmo_setup.py            # ask for the URL and key, check both, save them
    python3 mezmo_setup.py --remove   # delete the saved key and ~/.mezmo/codex.json

The key is read with getpass, so it never shows on screen, in shell history or in a command line.
It goes to the macOS Keychain, the Linux keyring (secret-tool), or else ~/.mezmo/codex-key (0600).
~/.mezmo/codex.json (0600) holds the URL, whether the ledger is on, and which store has the key,
never the key. Nothing is saved unless both checks pass. Environment variables still win over
what is saved here: see mezmo_settings.py.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

# mezmo_settings.py sits beside this file, a folder PYTHONSAFEPATH keeps off sys.path.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import mezmo_settings  # noqa: E402
from mezmo_settings import KEY_FILE, KEYCHAIN, SECRET_TOOL  # noqa: E402

REQUEST_SECONDS = 15.0
LOCAL_HOSTS = {"127.0.0.1", "localhost", "::1"}
STORE_NAMES = {
    KEYCHAIN: "the macOS Keychain",
    SECRET_TOOL: "the desktop keyring (secret-tool)",
    KEY_FILE: "",  # filled in with the path when shown
}

Http = Callable[[str, str, str, Optional[bytes]], Tuple[int, Any]]


class SetupError(Exception):
    pass


class RefuseRedirect(HTTPRedirectHandler):
    def redirect_request(self, *args: Any, **kwargs: Any) -> None:
        return None


def http(method: str, url: str, key: str, body: Optional[bytes]) -> Tuple[int, Any]:
    """(status, parsed JSON body or None). The key goes only in the Authorization header."""
    scheme = "" if key.lower().startswith(("token ", "bearer ")) else "Token "
    headers = {"Authorization": f"{scheme}{key}", "Accept": "application/json"}
    if body is not None:
        headers["Content-Type"] = "application/json"
    request = Request(url, data=body, headers=headers, method=method)
    try:
        with build_opener(RefuseRedirect).open(request, timeout=REQUEST_SECONDS) as response:
            status, raw = response.status, response.read()
    except HTTPError as error:
        status, raw = error.code, error.read()
    except (URLError, OSError) as error:
        raise SetupError(f"{shown_url(url)} was not reached: {getattr(error, 'reason', error)}") from error
    try:
        return status, json.loads(raw) if raw else None
    except ValueError:
        return status, None


def shown_url(url: str) -> str:
    parsed = urlparse(url)
    return f"{parsed.scheme}://{parsed.hostname or ''}{f':{parsed.port}' if parsed.port else ''}{parsed.path}"


def check_url(url: str) -> str:
    url = url.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SetupError("the API URL must have no user name, password, query or fragment")
    if parsed.scheme == "https" and parsed.hostname:
        return url
    if parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS:
        return url
    raise SetupError("the API URL must use https://, or http:// to this machine")


def check_key(key: str) -> str:
    key = key.strip()
    if not key:
        raise SetupError("no key was entered")
    if not (key.isascii() and key.isprintable()):
        raise SetupError("the key holds a character an HTTP header cannot carry")
    return key


def check_governance(api_url: str, key: str, send: Http = http) -> None:
    """The doctor's no-decision check: a status lookup for a made-up decision is 404 when the key is accepted."""
    url = f"{api_url}{mezmo_settings.GOVERNANCE_PATH}?decision_id={uuid.uuid4()}"
    status, _ = send("GET", url, key, None)
    if status == 404:
        return
    if status in (401, 403):
        raise SetupError(f"governance refused the key (HTTP {status}); copy a fresh key from the service account in Mezmo")
    raise SetupError(f"governance answered HTTP {status} at {shown_url(url)}, not 404; check the API URL")


def check_ledger(url: str, key: str, send: Http = http) -> None:
    """An empty batch to the ledger URL: the ledger takes it with 202 {"accepted": 0} and records nothing."""
    status, body = send("POST", url, key, b"[]")
    if status == 202 and isinstance(body, dict) and body.get("accepted") == 0:
        return
    if status in (401, 403):
        raise SetupError(f"the ledger refused the key (HTTP {status})")
    raise SetupError(f"the ledger answered HTTP {status} at {shown_url(url)}, not 202 {{\"accepted\": 0}}")


# --- key stores ---------------------------------------------------------------------------------


def run(args: list, stdin: str = "") -> subprocess.CompletedProcess:
    """Run a store command. A secret only ever travels on stdin, never in args."""
    return subprocess.run(args, input=stdin, capture_output=True, text=True, timeout=30, check=False)


def keychain_quote(value: str) -> str:
    return '"' + value.replace("\\", "\\\\").replace('"', '\\"') + '"'


def write_keychain(key: str) -> None:
    # `security add-generic-password -w <key>` would put the key in the process list. security -i reads
    # the same command from stdin instead, so its argv is only `security -i`.
    command = " ".join(
        ["add-generic-password", "-U", "-s", mezmo_settings.KEYCHAIN_SERVICE, "-a", mezmo_settings.KEYCHAIN_ACCOUNT,
         "-l", keychain_quote("Mezmo AI Governance (Codex)"), "-w", keychain_quote(key)]
    )
    result = run(["security", "-i"], stdin=command + "\n")
    # security -i exits 0 even when the command fails, so read the key back.
    if result.returncode != 0 or mezmo_settings.read_key(KEYCHAIN) != key:
        raise SetupError("the Keychain did not take the key")


def write_secret_tool(key: str) -> None:
    result = run(
        ["secret-tool", "store", "--label=Mezmo AI Governance (Codex)", *mezmo_settings.secret_tool_attributes()],
        stdin=key,
    )
    if result.returncode != 0:
        raise SetupError("secret-tool did not take the key; is a keyring unlocked?")


def private_dir() -> Path:
    folder = mezmo_settings.settings_dir()
    folder.mkdir(mode=0o700, parents=True, exist_ok=True)
    if os.name != "nt":
        os.chmod(folder, 0o700)
    return folder


def write_private(path: Path, text: str) -> None:
    """Write text to a new 0600 file, then move it over path, so no reader sees a partial or looser file."""
    temporary = path.with_name(f".{path.name}.{os.getpid()}.tmp")
    descriptor = os.open(temporary, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    try:
        with os.fdopen(descriptor, "w", encoding="utf-8") as handle:
            handle.write(text)
        os.replace(temporary, path)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise


def write_key_file(key: str) -> None:
    private_dir()
    write_private(mezmo_settings.key_path(), key + "\n")


def delete_key(store: str) -> bool:
    """Delete the key from the store. True when one was there, False when none was. Raises SetupError when the
    store could not be changed, so the caller keeps the settings that name it."""
    try:
        if store == KEYCHAIN:
            code = run(mezmo_settings.keychain_command("delete-generic-password")).returncode
            if code == 44:  # the item is not there
                return False
            if code != 0:
                raise SetupError(f"security exited {code}")
            return True
        if store == SECRET_TOOL:
            # secret-tool clear exits 0 whether or not it found the key.
            code = run(["secret-tool", "clear", *mezmo_settings.secret_tool_attributes()]).returncode
            if code != 0:
                raise SetupError(f"secret-tool exited {code}")
            return True
        path = mezmo_settings.key_path()
        if not os.path.lexists(path):
            return False
        path.unlink()
        return True
    except (OSError, subprocess.SubprocessError) as error:
        raise SetupError(str(error) or type(error).__name__) from error


def store_key(key: str, platform: str = sys.platform) -> str:
    """Save the key in the best store this machine has. Returns the store's name."""
    if platform == "darwin" and shutil.which("security"):
        write_keychain(key)
        return KEYCHAIN
    if platform.startswith("linux") and shutil.which("secret-tool"):
        try:
            write_secret_tool(key)
            return SECRET_TOOL
        except (SetupError, OSError, subprocess.SubprocessError) as error:
            # A keyring that is locked, missing or never answers (TimeoutExpired) counts as no keyring.
            reason = f"secret-tool did not answer within {error.timeout:g} s" if isinstance(error, subprocess.TimeoutExpired) else error
            print(f"{reason}; using a protected file instead")
    write_key_file(key)
    return KEY_FILE


def store_name(store: str) -> str:
    return STORE_NAMES.get(store) or str(mezmo_settings.key_path())


# --- commands -----------------------------------------------------------------------------------


def setup(ledger: bool = True, send: Http = http, platform: str = sys.platform) -> int:
    old = mezmo_settings.read_config()
    default = old.get("api_url") if isinstance(old.get("api_url"), str) else mezmo_settings.DEFAULT_API_URL
    try:
        api_url = check_url(input(f"Mezmo API URL [{default}]: ") or default)
        key = check_key(getpass.getpass("Mezmo key (not shown): "))
        check_governance(api_url, key, send)
        print("ok    governance accepted the key")
        if ledger:
            check_ledger(api_url + mezmo_settings.LEDGER_PATH, key, send)
            print("ok    the ledger accepted the key")
        store = store_key(key, platform)
    except (SetupError, OSError, subprocess.SubprocessError) as error:
        print(f"FAIL  {error}. Nothing was saved.", file=sys.stderr)
        return 1
    # The new settings are committed before the old store's key is deleted, so a failed save leaves the old setup working.
    config = {"api_url": api_url, "ledger_enabled": ledger, "key_store": store}
    try:
        private_dir()
        write_private(mezmo_settings.config_path(), json.dumps(config, indent=2) + "\n")
    except OSError as error:
        print(f"FAIL  could not save {mezmo_settings.config_path()}: {error}. The previous settings are unchanged.", file=sys.stderr)
        return 1
    print(f"saved the key in {store_name(store)}, and the settings in {mezmo_settings.config_path()}")
    previous = old.get("key_store")
    if previous in mezmo_settings.KEY_STORES and previous != store:
        try:
            delete_key(previous)
        except SetupError as error:
            print(f"warn  could not delete the old key from {store_name(previous)}: {error}; delete it by hand", file=sys.stderr)
    if not ledger:
        print("the ledger is off; run setup again without --no-ledger to turn it on")
    if any(os.environ.get(name) for name in ("MEZMO_GOVERNANCE_URL", "MEZMO_GOVERNANCE_TOKEN", "MEZMO_LEDGER_URL", "MEZMO_LEDGER_KEY")):
        print("note: MEZMO_* variables set in this shell still win over these settings")
    print("next: start codex, run /hooks, trust the four Mezmo hooks, then run ./mezmo-governance doctor codex")
    return 0


def remove() -> int:
    # Without codex.json nothing records a store, so only a leftover key file is deleted.
    store = mezmo_settings.read_config().get("key_store")
    name = store if store in mezmo_settings.KEY_STORES else KEY_FILE
    try:
        found = delete_key(name)
    except SetupError as error:
        # codex.json stays, so running --remove again still knows which store holds the key.
        print(f"FAIL  could not delete the key from {store_name(name)}: {error}. Nothing else was removed.", file=sys.stderr)
        return 1
    print(f"deleted the key from {store_name(name)}" if found else f"no key was in {store_name(name)}")
    path = mezmo_settings.config_path()
    if os.path.lexists(path):
        path.unlink()
        print(f"deleted {path}")
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--remove", action="store_true", help="delete the saved key and settings")
    parser.add_argument("--no-ledger", action="store_true", help="save the ledger as off and skip its check")
    args = parser.parse_args(argv)
    if args.remove:
        return remove()
    try:
        return setup(ledger=not args.no_ledger)
    except (EOFError, KeyboardInterrupt):
        print("\nstopped; nothing was saved", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
