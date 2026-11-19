#!/usr/bin/env python3
"""Print the Mezmo MCP server's Authorization header for Codex's http_headers_helper.

Codex runs the command in `[mcp_servers.mezmo] http_headers_helper` when it connects to the
server, and sends the JSON object it prints as HTTP headers:

    {"Authorization": "Token <key>"}

The key comes from where setup saved it (mezmo_settings.py), so neither config.toml nor
Codex's environment holds it. Setup copies this file and mezmo_settings.py to
~/.mezmo/codex-mcp/, so a plugin update does not move the path Codex runs.
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

# mezmo_settings.py sits beside this file, a folder PYTHONSAFEPATH keeps off sys.path.
if str(Path(__file__).resolve().parent) not in sys.path:
    sys.path.insert(0, str(Path(__file__).resolve().parent))
import mezmo_settings  # noqa: E402


def header(key: str) -> str:
    return key if key.lower().startswith(("token ", "bearer ")) else f"Token {key}"


def main() -> int:
    key = mezmo_settings.governance_key().value
    if not key:
        print("Mezmo: no Mezmo key is saved; ask Codex to set up Mezmo again", file=sys.stderr)
        return 1
    print(json.dumps({"Authorization": header(key)}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
