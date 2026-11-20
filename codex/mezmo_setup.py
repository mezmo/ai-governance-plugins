#!/usr/bin/env python3
"""Set up the Mezmo Codex hooks and the Mezmo MCP: the Mezmo API URL and key, checked, then saved.

    python3 mezmo_setup.py            # ask for the URL and key, check both, save them
    python3 mezmo_setup.py --api-url URL --key-file PATH --delete-key-file   # the same, without asking
    python3 mezmo_setup.py --keep-key  # the same, with the key setup saved before
    python3 mezmo_setup.py --status   # what is saved, never the key
    python3 mezmo_setup.py --remove   # delete the saved key, ~/.mezmo/codex.json and the MCP entry

Asked, the key is read with getpass, so it never shows on screen, in shell history or in a command
line. At a terminal, setup asks for whatever the flags leave out; the mezmo-setup skill hands the
user `~/.mezmo/bin/mezmo-setup` to run that way. --key-file reads the key from a private file
instead (mezmo_key_file.py create-key-file makes one), for a run with no terminal.
It goes to the macOS Keychain, the Linux keyring (secret-tool), or else ~/.mezmo/codex-key (0600).
~/.mezmo/codex.json (0600) holds the URL, whether the ledger is on, and which store has the key,
never the key. Nothing is saved unless both checks pass. Environment variables still win over
what is saved here: see mezmo_settings.py.

Setup also writes [mcp_servers.mezmo] into Codex's config.toml ($CODEX_HOME, default ~/.codex),
with an http_headers_helper that prints the Authorization header from the same key store, so
neither config.toml nor Codex's environment holds the key. It also writes ~/.mezmo/bin/mezmo-setup,
which runs this script from the newest installed plugin, so the command survives plugin updates.
"""

from __future__ import annotations

import argparse
import getpass
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import uuid
from pathlib import Path
from typing import Any, Callable, List, Optional, Tuple
from urllib.error import HTTPError, URLError
from urllib.parse import urlparse
from urllib.request import HTTPRedirectHandler, Request, build_opener

# mezmo_settings.py sits beside this file, a folder PYTHONSAFEPATH keeps off sys.path.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import mezmo_key_file  # noqa: E402
import mezmo_settings  # noqa: E402
from mezmo_settings import KEY_FILE, KEYCHAIN, SECRET_TOOL  # noqa: E402

try:
    import tomllib  # Python 3.11+; older Pythons edit config.toml without the parse check.
except ImportError:  # pragma: no cover
    tomllib = None  # type: ignore[assignment]

PRODUCTION_MCP_URL = "https://mcp.mezmo.com/mcp"
# The investigate flow's reads. The server also has pipeline tools that make changes, so only these are on.
MCP_TOOLS = ("get_ledger_investigation", "list_ledger_entries")
MCP_MARKER = "# Written by Mezmo setup: run it again to change this table, or with --remove to delete it."
MCP_HEADER = re.compile(r"""^\s*\[\s*mcp_servers\s*\.\s*(?:mezmo|"mezmo"|'mezmo')\s*(\.[^\]]*)?\]\s*(?:#.*)?$""")
TABLE_HEADER = re.compile(r"^\s*\[")
HELPER_FILES = ("mezmo_mcp_headers.py", "mezmo_settings.py")
EXIT_MCP_TAKEN = 4
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


def check_url(url: str, name: str = "the API URL") -> str:
    url = url.strip().rstrip("/")
    parsed = urlparse(url)
    if parsed.username or parsed.password or parsed.query or parsed.fragment:
        raise SetupError(f"{name} must have no user name, password, query or fragment")
    if parsed.scheme == "https" and parsed.hostname:
        return url
    if parsed.scheme == "http" and parsed.hostname in LOCAL_HOSTS:
        return url
    raise SetupError(f"{name} must use https://, or http:// to this machine")


def default_mcp_url(api_url: str) -> Optional[str]:
    """Production's MCP URL for the production API; another API has no known MCP URL."""
    return PRODUCTION_MCP_URL if api_url.strip().rstrip("/").lower() == mezmo_settings.DEFAULT_API_URL else None


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


def saved_key(platform: str = sys.platform) -> Tuple[Optional[str], str]:
    """(key, store) for a key setup saved before: the store codex.json names, else, on macOS, the
    Keychain item setup writes. (None, "") when there is none. Never raises."""
    found = mezmo_settings.stored_key()
    if found.value:
        return found.value, found.source
    if found.source or platform != "darwin" or not shutil.which("security"):
        return None, ""
    try:
        key = mezmo_settings.read_key(KEYCHAIN)
    except (OSError, ValueError, subprocess.SubprocessError):
        return None, ""
    return (key, KEYCHAIN) if key else (None, "")


# --- Codex's MCP entry -------------------------------------------------------------------------


def codex_config_path() -> Path:
    home = os.environ.get("CODEX_HOME", "").strip()
    return (Path(home).expanduser() if home else Path.home() / ".codex") / "config.toml"


def helper_dir() -> Path:
    return mezmo_settings.settings_dir() / "codex-mcp"


def helper_command() -> str:
    return "python3 " + shlex.quote(str(helper_dir() / HELPER_FILES[0]))


def toml_string(value: str) -> str:
    # A JSON string is a valid TOML basic string.
    return json.dumps(value)


def mcp_block(url: str) -> List[str]:
    return [
        MCP_MARKER,
        "[mcp_servers.mezmo]",
        f"url = {toml_string(url)}",
        f"http_headers_helper = {toml_string(helper_command())}",
        "enabled_tools = [" + ", ".join(toml_string(name) for name in MCP_TOOLS) + "]",
        'default_tools_approval_mode = "auto"',
    ]


def mcp_tables(lines: List[str]) -> Tuple[List[Tuple[int, int]], bool]:
    """([start, end) line ranges of [mcp_servers.mezmo] and its sub-tables, whether setup wrote them).

    A range starts at the marker line when there is one and leaves out the comments and blank lines
    that end it, which may belong to the next table."""
    ranges: List[Tuple[int, int]] = []
    owned = False
    index = 0
    while index < len(lines):
        match = MCP_HEADER.match(lines[index])
        if not match:
            index += 1
            continue
        start, end = index, index + 1
        while end < len(lines) and not TABLE_HEADER.match(lines[end]):
            end += 1
        index = end
        while end > start + 1 and (not lines[end - 1].strip() or lines[end - 1].lstrip().startswith("#")):
            end -= 1
        if start > 0 and lines[start - 1].strip() == MCP_MARKER:
            start -= 1
            owned = owned or match.group(1) is None
        ranges.append((start, end))
    return ranges, owned


def parse_toml(text: str, path: Path) -> Optional[dict]:
    if tomllib is None:
        return None
    try:
        return tomllib.loads(text)
    except ValueError as error:
        raise SetupError(f"{path} is not valid TOML ({error}); fix it, then run setup again") from error


def read_codex_config() -> Tuple[Path, str]:
    """(the real config.toml, its text), following a symlink so a dotfiles link stays a link."""
    path = Path(os.path.realpath(codex_config_path()))
    try:
        return path, path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return path, ""
    except (OSError, UnicodeError) as error:
        raise SetupError(f"could not read {path}: {error}") from error


def mcp_taken() -> Optional[str]:
    """Why setup must not write the MCP entry, or None: an [mcp_servers.mezmo] setup did not write."""
    path, text = read_codex_config()
    lines = text.splitlines()
    ranges, owned = mcp_tables(lines)
    data = parse_toml(text, path)
    defined = bool(ranges) or (data is not None and "mezmo" in (data.get("mcp_servers") or {}))
    if defined and not owned:
        return f"{path} already has an MCP server named mezmo that setup did not write"
    if data is not None and "mezmo" in (data.get("mcp_servers") or {}) and not ranges:
        return f"{path} defines the mezmo MCP server in a form setup cannot edit"
    return None


def write_codex_config(path: Path, old: str, new: str) -> Optional[Path]:
    """Replace config.toml with new, keeping the old text in a backup beside it. Returns the backup."""
    backup = None
    mode = 0o600
    if old:
        mode = path.stat().st_mode & 0o777
        backup = path.with_name(path.name + ".mezmo-backup")
        write_private(backup, old)
    path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
    write_private(path, new)
    os.chmod(path, mode)
    return backup


def edit_mcp(url: Optional[str], replace: bool = False) -> Optional[Path]:
    """Write [mcp_servers.mezmo] for url, or with url None delete the one setup wrote. Every other line
    of config.toml stays as it was. Returns the backup, or None when nothing changed."""
    path, text = read_codex_config()
    parse_toml(text, path)
    lines = text.splitlines()
    ranges, owned = mcp_tables(lines)
    if ranges and not owned and not replace:
        if url is None:
            return None
        raise SetupError(f"{path} already has an MCP server named mezmo that setup did not write")
    position = ranges[0][0] if ranges else len(lines)
    for start, end in reversed(ranges):
        del lines[start:end]
        if start < position:
            position -= end - start
    if url is not None:
        block = mcp_block(url)
        if position == len(lines) and lines and lines[-1].strip():
            block = [""] + block
        lines[position:position] = block
    new = "\n".join(lines).rstrip("\n") + "\n" if lines else ""
    if new == text:
        return None
    data = parse_toml(new, path)
    if data is not None and url is not None and (data.get("mcp_servers") or {}).get("mezmo", {}).get("url") != url:
        raise SetupError(f"could not write the mezmo MCP server into {path}; nothing was changed")
    try:
        return write_codex_config(path, text, new)
    except OSError as error:
        raise SetupError(f"could not write {path}: {error}") from error


def install_helper() -> None:
    """Copy the header helper and the settings loader it imports to ~/.mezmo/codex-mcp, a path that a
    plugin update does not move."""
    folder = helper_dir()
    private_dir()
    folder.mkdir(mode=0o700, exist_ok=True)
    os.chmod(folder, 0o700)
    for name in HELPER_FILES:
        write_private(folder / name, (Path(__file__).resolve().parent / name).read_text(encoding="utf-8"))


def remove_helper() -> None:
    folder = helper_dir()
    for name in HELPER_FILES:
        (folder / name).unlink(missing_ok=True)
    try:
        folder.rmdir()
    except OSError:
        pass


def mcp_status() -> str:
    try:
        path, text = read_codex_config()
    except SetupError as error:
        return str(error)
    ranges, owned = mcp_tables(text.splitlines())
    if not ranges:
        return f"not set up in {path}"
    match = re.search(r'^\s*url\s*=\s*"([^"]*)"', "\n".join(text.splitlines()[ranges[0][0]:ranges[0][1]]), re.M)
    url = match.group(1) if match else "no url"
    return f"{url} in {path}" + ("" if owned else " (not written by setup)")


# --- commands -----------------------------------------------------------------------------------


def interactive() -> bool:
    """Whether a person can answer: stdin is a terminal. Never raises."""
    try:
        return sys.stdin.isatty()
    except (AttributeError, OSError, ValueError):
        return False


class Choices:
    """What setup was told on the command line. All None/False: ask, as before."""

    def __init__(self, api_url: Optional[str] = None, mcp_url: Optional[str] = None, key_file: Optional[Path] = None,
                 keep_key: bool = False, replace_mcp: bool = False) -> None:
        self.api_url, self.mcp_url, self.key_file = api_url, mcp_url, key_file
        self.keep_key, self.replace_mcp = keep_key, replace_mcp

    @property
    def asking(self) -> bool:
        return not (self.api_url or self.mcp_url or self.key_file or self.keep_key)


def read_inputs(choices: Choices, old: dict, platform: str) -> Tuple[str, Optional[str], str, Optional[str]]:
    """(api_url, mcp_url, key, the store the key is already in or None).

    With no flags it asks for everything. At a terminal it also asks for whatever the flags leave
    out, so `--api-url URL` alone asks for the MCP URL and the key; without one, nothing is asked.
    """
    default = old.get("api_url") if isinstance(old.get("api_url"), str) else mezmo_settings.DEFAULT_API_URL
    ask = choices.asking or interactive()
    if choices.api_url:
        api_url = check_url(choices.api_url)
    elif ask:
        api_url = check_url(input(f"Mezmo API URL [{default}]: ") or default)
    else:
        api_url = check_url(default)
    mcp_url = choices.mcp_url or default_mcp_url(api_url)
    if mcp_url is None and ask:
        mcp_url = input("Mezmo MCP URL (blank to skip the MCP): ").strip() or None
    if mcp_url is not None:
        mcp_url = check_url(mcp_url, "the MCP URL")
    if choices.keep_key:
        key, store = saved_key(platform)
        if key is None:
            raise SetupError("no Mezmo key is saved yet, so there is none to keep; give a key file instead")
        return api_url, mcp_url, check_key(key), store
    if choices.key_file is not None:
        try:
            key = mezmo_key_file.read_key_file(choices.key_file)
        except ValueError as error:
            raise SetupError(str(error).rstrip(".")) from error
        return api_url, mcp_url, check_key(key), None
    if not ask:
        raise SetupError("no key to save: run this in a terminal to be asked for it, or give --key-file or --keep-key")
    return api_url, mcp_url, check_key(getpass.getpass("Mezmo key (not shown): ")), None


def setup(ledger: bool = True, send: Optional[Http] = None, platform: str = sys.platform, choices: Optional[Choices] = None) -> int:
    choices = choices or Choices()
    send = send or http
    old = mezmo_settings.read_config()
    try:
        api_url, mcp_url, key, kept = read_inputs(choices, old, platform)
        taken = mcp_taken() if mcp_url and not choices.replace_mcp else None
        if taken:
            print(
                f"FAIL  {taken}. Nothing was saved. Remove it, or run setup again with --replace-mcp to replace it"
                " (config.toml is backed up first).", file=sys.stderr,
            )
            return EXIT_MCP_TAKEN
        check_governance(api_url, key, send)
        print("ok    governance accepted the key")
        if ledger:
            check_ledger(api_url + mezmo_settings.LEDGER_PATH, key, send)
            print("ok    the ledger accepted the key")
        store = kept or store_key(key, platform)
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
    print(("kept the key in " if kept else "saved the key in ") + f"{store_name(store)}, and the settings in {mezmo_settings.config_path()}")
    try:
        launcher = mezmo_settings.install_launcher()
    except OSError as error:
        print(f"warn  could not write {mezmo_settings.launcher_path()}: {error}", file=sys.stderr)
    else:
        print(f"ok    {launcher} runs this setup again, whatever the plugin's version")
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
    code = 0
    mcp_changed = False
    if mcp_url is None:
        # A table setup wrote for an earlier API URL would get this key for the old environment.
        try:
            removed = edit_mcp(None)
        except (SetupError, OSError) as error:
            print(f"FAIL  could not remove the Mezmo MCP setup wrote before: {error}", file=sys.stderr)
            code = 1
        else:
            if removed:
                print(f"removed the Mezmo MCP setup wrote for an earlier API URL ({codex_config_path()})")
        print("skip  the Mezmo MCP: no MCP URL for this API URL; run setup again with --mcp-url to add it")
    else:
        try:
            install_helper()
            before = read_codex_config()[1]
            backup = edit_mcp(mcp_url, replace=choices.replace_mcp)
            mcp_changed = read_codex_config()[1] != before
        except (SetupError, OSError) as error:
            print(f"FAIL  the Mezmo MCP was not set up: {error}. Governance and the ledger are saved.", file=sys.stderr)
            code = 1
        else:
            print(f"ok    Codex connects to the Mezmo MCP at {mcp_url} as server mezmo ({codex_config_path()})")
            if backup:
                print(f"      the previous config.toml is in {backup}")
    print("next: start a new Codex session, run /hooks, and trust the five Mezmo hooks")
    if mcp_changed:
        # Codex reads [mcp_servers] when a session starts.
        print("Start a new Codex session to use the Mezmo MCP")
    return code


def status(platform: str = sys.platform) -> int:
    config = mezmo_settings.read_config()
    url = mezmo_settings.api_url()
    print(f"API URL: {url.value} ({'saved' if url.source == 'codex.json' else 'default, nothing saved'})")
    print(f"ledger: {'on' if mezmo_settings.ledger_enabled() else 'off'}")
    key, store = saved_key(platform)
    print(f"key: saved in {store_name(store)}" if key else "key: none saved")
    print(f"Codex MCP: {mcp_status()}")
    return 0 if config or key else 1


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
    try:
        backup = edit_mcp(None)
    except SetupError as error:
        print(f"FAIL  {error}; the mezmo MCP server is still in it", file=sys.stderr)
        return 1
    if backup:
        print(f"deleted the mezmo MCP server from {codex_config_path()} (the previous file is in {backup})")
    elif mcp_tables(read_codex_config()[1].splitlines())[0]:
        print(f"left the mezmo MCP server in {codex_config_path()}: setup did not write it")
    remove_helper()
    return 0


def main(argv: Optional[list] = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[1])
    parser.add_argument("--remove", action="store_true", help="delete the saved key and settings, and the MCP entry setup wrote")
    parser.add_argument("--status", action="store_true", help="show what is saved, never the key")
    parser.add_argument("--no-ledger", action="store_true", help="save the ledger as off and skip its check")
    parser.add_argument("--api-url", help="the Mezmo API URL (default: the saved one, else production); asks nothing")
    parser.add_argument("--mcp-url", help="the Mezmo MCP URL (default: production's for the production API)")
    key = parser.add_mutually_exclusive_group()
    key.add_argument("--key-file", type=Path, help="read the key from this private 0600 file; asks nothing")
    key.add_argument("--keep-key", action="store_true", help="use the key setup saved before; asks nothing")
    parser.add_argument("--delete-key-file", action="store_true", help="delete --key-file afterwards, on failure too")
    parser.add_argument("--replace-mcp", action="store_true", help="replace an [mcp_servers.mezmo] setup did not write")
    args = parser.parse_args(argv)
    try:
        if args.delete_key_file and not args.key_file:
            parser.error("--delete-key-file requires --key-file")
        if args.remove:
            return remove()
        if args.status:
            return status()
        choices = Choices(args.api_url, args.mcp_url, args.key_file, args.keep_key, args.replace_mcp)
        return setup(ledger=not args.no_ledger, choices=choices)
    except (EOFError, KeyboardInterrupt):
        print("\nstopped; nothing was saved", file=sys.stderr)
        return 1
    finally:
        if args.delete_key_file and args.key_file:
            try:
                mezmo_key_file.delete_key_file(args.key_file)
            except FileNotFoundError:
                pass
            except (OSError, ValueError):
                print(f"warn  could not delete {args.key_file}; delete it by hand", file=sys.stderr)


if __name__ == "__main__":
    raise SystemExit(main())
