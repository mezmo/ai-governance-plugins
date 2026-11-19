#!/usr/bin/env python3
"""Where the Codex hooks find the Mezmo API URL and key.

Each value comes from the first of:

1. the environment (MEZMO_GOVERNANCE_URL, MEZMO_GOVERNANCE_TOKEN, MEZMO_LEDGER_URL, MEZMO_LEDGER_KEY);
2. ~/.mezmo/codex.json, written by mezmo_setup.py, plus the key store it names;
3. the production API, https://api.mezmo.com.

codex.json holds api_url, ledger_enabled and key_store, never the key. The key is in the macOS
Keychain, the Linux keyring (secret-tool), or ~/.mezmo/codex-key (0600). A key read from a store is
kept in memory for this process only. A store that cannot be read warns once and counts as no key.

It must be in the same folder as both hooks and mezmo_setup.py, which import it.
"""

from __future__ import annotations

import functools
import json
import os
import stat
import subprocess
import sys
from pathlib import Path
from typing import NamedTuple, Optional

DEFAULT_API_URL = "https://api.mezmo.com"
GOVERNANCE_PATH = "/governance/workflow/authorize"
LEDGER_PATH = "/v1/audit/events"
KEYCHAIN_SERVICE = "mezmo-ai-governance"
KEYCHAIN_ACCOUNT = "codex"
KEYCHAIN = "keychain"
SECRET_TOOL = "secret-tool"
KEY_FILE = "file"
KEY_STORES = (KEYCHAIN, SECRET_TOOL, KEY_FILE)
STORE_SECONDS = 5.0


class Setting(NamedTuple):
    value: Optional[str]
    # Where the value came from, for doctor and setup: "env NAME", "codex.json", "default", a key store, or "".
    source: str


def settings_dir() -> Path:
    return Path.home() / ".mezmo"


def config_path() -> Path:
    return settings_dir() / "codex.json"


def key_path() -> Path:
    return settings_dir() / "codex-key"


def warn(message: str) -> None:
    try:
        print(f"Mezmo: {message}", file=sys.stderr)
    except OSError:
        pass


def _env(name: str) -> Optional[str]:
    value = os.environ.get(name, "").strip()
    return value or None


def read_config() -> dict:
    """codex.json as a dict; {} when it is missing or unreadable (with a warning)."""
    path = config_path()
    try:
        data = json.loads(path.read_text(encoding="utf-8"))
    except FileNotFoundError:
        return {}
    except (OSError, ValueError) as error:
        warn(f"ignoring {path}: {error}")
        return {}
    if not isinstance(data, dict):
        warn(f"ignoring {path}: not a JSON object")
        return {}
    return data


def api_url() -> Setting:
    configured = read_config().get("api_url")
    if isinstance(configured, str) and configured.strip():
        return Setting(configured.strip().rstrip("/"), "codex.json")
    return Setting(DEFAULT_API_URL, "default")


def ledger_enabled() -> bool:
    return read_config().get("ledger_enabled") is not False


def governance_url() -> Setting:
    configured = _env("MEZMO_GOVERNANCE_URL")
    if configured:
        return Setting(configured, "env MEZMO_GOVERNANCE_URL")
    base = api_url()
    return Setting(base.value + GOVERNANCE_PATH, base.source)


def ledger_url() -> Setting:
    """MEZMO_LEDGER_URL, else the ledger beside the governance URL; None when codex.json turns it off.

    A MEZMO_GOVERNANCE_URL from the environment moves the ledger with it, so a dev URL and key
    never send to the production ledger.
    """
    configured = _env("MEZMO_LEDGER_URL")
    if configured:
        return Setting(configured, "env MEZMO_LEDGER_URL")
    if not ledger_enabled():
        return Setting(None, "codex.json ledger_enabled false")
    governance = governance_url()
    base = governance.value or ""
    if base.endswith(GOVERNANCE_PATH):
        base = base[: -len(GOVERNANCE_PATH)]
    return Setting(base.rstrip("/") + LEDGER_PATH, governance.source)


def governance_key() -> Setting:
    configured = _env("MEZMO_GOVERNANCE_TOKEN")
    if configured:
        return Setting(configured, "env MEZMO_GOVERNANCE_TOKEN")
    return stored_key()


def ledger_key() -> Setting:
    configured = _env("MEZMO_LEDGER_KEY")
    if configured:
        return Setting(configured, "env MEZMO_LEDGER_KEY")
    return governance_key()


def stored_key() -> Setting:
    store = read_config().get("key_store")
    if store not in KEY_STORES:
        return Setting(None, "")
    return Setting(_read_store(store, str(settings_dir())), store)


# The cache is keyed by the folder, so a test that moves HOME never sees another test's key.
@functools.lru_cache(maxsize=None)
def _read_store(store: str, folder: str) -> Optional[str]:
    try:
        key = read_key(store)
    except (OSError, ValueError, subprocess.SubprocessError) as error:
        warn(f"could not read the Mezmo key from the {store} store, so there is none: {error}")
        return None
    if key is None:
        warn(f"no Mezmo key in the {store} store; run mezmo_setup.py again")
    return key


def keychain_command(*args: str) -> list:
    return ["security", *args, "-s", KEYCHAIN_SERVICE, "-a", KEYCHAIN_ACCOUNT]


def secret_tool_attributes() -> list:
    return ["service", KEYCHAIN_SERVICE, "account", KEYCHAIN_ACCOUNT]


def read_key(store: str) -> Optional[str]:
    """The key in the store, or None when it holds none. Raises when the store cannot be read."""
    if store == KEYCHAIN:
        result = subprocess.run(
            keychain_command("find-generic-password") + ["-w"],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=STORE_SECONDS, check=False,
        )
        # 44: the item is not there.
        if result.returncode == 44:
            return None
        if result.returncode != 0:
            raise OSError(f"security exited {result.returncode}")
        return result.stdout.rstrip("\n").strip() or None
    if store == SECRET_TOOL:
        result = subprocess.run(
            ["secret-tool", "lookup", *secret_tool_attributes()],
            stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=STORE_SECONDS, check=False,
        )
        if result.returncode != 0:
            # secret-tool exits 1 both for "not found" and for a locked or missing keyring.
            return None
        return result.stdout.rstrip("\n").strip() or None
    return read_key_file(key_path())


def read_key_file(path: Path) -> Optional[str]:
    """The key file's text. Refuses a symlink, a file others may read or write, or one another user owns."""
    try:
        info = os.lstat(path)
    except FileNotFoundError:
        return None
    if stat.S_ISLNK(info.st_mode):
        raise ValueError(f"{path} is a symlink; delete it and run mezmo_setup.py again")
    if not stat.S_ISREG(info.st_mode):
        raise ValueError(f"{path} is not a regular file")
    if os.name != "nt":
        if info.st_mode & 0o077:
            raise ValueError(f"{path} has mode {stat.S_IMODE(info.st_mode):o}; run chmod 600 on it")
        if info.st_uid != os.getuid():
            raise ValueError(f"{path} belongs to another user")
    flags = os.O_RDONLY | getattr(os, "O_NOFOLLOW", 0)
    with os.fdopen(os.open(path, flags), encoding="utf-8") as handle:
        return handle.read().strip() or None


def governance_configured() -> bool:
    """Whether a key was given at all: MEZMO_GOVERNANCE_TOKEN, or a codex.json.

    Only a missing codex.json means never set up (`--remove` deletes it). One that exists counts
    whatever it holds, so a broken or edited one keeps the hook failing closed, as a configured key
    that can't be read does.
    """
    if _env("MEZMO_GOVERNANCE_TOKEN"):
        return True
    try:
        config_path().stat()
    except FileNotFoundError:
        return False
    except OSError:
        pass
    return True


def governance_required() -> bool:
    """MEZMO_GOVERNANCE_REQUIRED=1: deny every call until a key is configured, as in Claude Code."""
    return (_env("MEZMO_GOVERNANCE_REQUIRED") or "").lower() in ("1", "true", "yes")


# The setup launcher: a path that stays the same across plugin updates, for the user to run in a
# terminal when Codex cannot run setup outside its sandbox.
LAUNCHER = '''#!/usr/bin/env python3
"""Written by Mezmo setup: run mezmo_setup.py from the newest Mezmo plugin Codex has installed.

Arguments go to mezmo_setup.py, so `mezmo-setup --status` works too.
"""
import os
import re
import sys
from pathlib import Path

INSTALLED = {installed!r}


def version(path):
    return tuple(int(part) for part in re.findall(r"[0-9]+", path.parent.name))


home = Path(os.environ.get("CODEX_HOME") or Path.home() / ".codex").expanduser()
found = sorted((home / "plugins" / "cache" / "mezmo" / "mezmo-governance").glob("*/mezmo_setup.py"), key=version)
setup = found[-1] if found else Path(INSTALLED)
if not setup.is_file():
    sys.exit("mezmo-setup: no Mezmo plugin for Codex is installed: run codex plugin add mezmo-governance@mezmo")
os.execv(sys.executable, [sys.executable, str(setup), *sys.argv[1:]])
'''


def launcher_path() -> Path:
    return settings_dir() / "bin" / "mezmo-setup"


def install_launcher() -> Path:
    """Write ~/.mezmo/bin/mezmo-setup, which runs this plugin's setup whatever its version. Raises OSError."""
    path = launcher_path()
    for folder in (settings_dir(), path.parent):
        folder.mkdir(mode=0o700, exist_ok=True)
    text = LAUNCHER.format(installed=str(Path(__file__).resolve().with_name("mezmo_setup.py")))
    temporary = path.with_name(f".{path.name}.{os.getpid()}")
    with open(temporary, "w", encoding="utf-8", opener=lambda name, flags: os.open(name, flags, 0o700)) as handle:
        handle.write(text)
    os.chmod(temporary, 0o700)
    os.replace(temporary, path)
    return path


def describe() -> list:
    """(name, shown value, source) for each setting, for doctor. The key is never shown."""
    rows: list = []
    for name, setting in (("governance URL", governance_url()), ("ledger URL", ledger_url())):
        rows.append((name, setting.value or "off", setting.source))
    for name, setting in (("governance key", governance_key()), ("ledger key", ledger_key())):
        rows.append((name, "found" if setting.value else "not found", setting.source))
    return rows
