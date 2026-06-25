"""
panel.py — Control the BisectHosting (Pterodactyl) server from the command line.

Lets you check status and send power signals (start / stop / restart) to the
host without opening the web panel. Because the server auto-pulls from the repo
on (re)start, `python panel.py restart` is effectively "deploy latest main to
the live bots".

Credentials are read from a git-ignored `.panel.env` next to this file:

    PANEL_URL=https://games.bisecthosting.com
    PANEL_SERVER_ID=19d7e6d1
    PANEL_API_KEY=ptlc_xxxxxxxx

They can also come from environment variables of the same name (so a CI job /
GitHub Action can supply them as secrets instead of a file).

Usage:
    python panel.py status
    python panel.py restart
    python panel.py start
    python panel.py stop
"""
from __future__ import annotations

import json
import os
import sys
import urllib.error
import urllib.request
from pathlib import Path

ENV_FILE = Path(__file__).with_name(".panel.env")
VALID_SIGNALS = {"start", "stop", "restart", "kill"}


def _load_config() -> dict[str, str]:
    cfg: dict[str, str] = {}
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text().splitlines():
            line = line.strip()
            if not line or line.startswith("#") or "=" not in line:
                continue
            key, _, val = line.partition("=")
            cfg[key.strip()] = val.strip()
    # Environment variables override / supplement the file (for CI secrets).
    for key in ("PANEL_URL", "PANEL_SERVER_ID", "PANEL_API_KEY"):
        if os.environ.get(key):
            cfg[key] = os.environ[key]
    missing = [k for k in ("PANEL_URL", "PANEL_SERVER_ID", "PANEL_API_KEY") if not cfg.get(k)]
    if missing:
        sys.exit(f"Missing panel credentials: {', '.join(missing)} (set in {ENV_FILE.name} or env vars).")
    return cfg


def _request(cfg: dict[str, str], path: str, *, body: dict | None = None) -> bytes:
    url = f"{cfg['PANEL_URL'].rstrip('/')}/api/client/servers/{cfg['PANEL_SERVER_ID']}{path}"
    data = json.dumps(body).encode() if body is not None else None
    req = urllib.request.Request(url, data=data, method="POST" if data else "GET")
    req.add_header("Authorization", f"Bearer {cfg['PANEL_API_KEY']}")
    req.add_header("Accept", "application/json")
    # Cloudflare in front of the panel bans the default urllib user-agent (error
    # 1010), so present a normal browser-style one.
    req.add_header("User-Agent", "Mozilla/5.0 (Windows NT 10.0; Win64; x64) panel.py")
    if data:
        req.add_header("Content-Type", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read()
    except urllib.error.HTTPError as exc:
        sys.exit(f"Panel API error {exc.code}: {exc.read().decode(errors='replace')}")
    except urllib.error.URLError as exc:
        sys.exit(f"Could not reach panel: {exc.reason}")


def status(cfg: dict[str, str]) -> None:
    raw = _request(cfg, "/resources")
    state = json.loads(raw)["attributes"]["current_state"]
    print(f"Server {cfg['PANEL_SERVER_ID']} state: {state}")


def power(cfg: dict[str, str], signal: str) -> None:
    _request(cfg, "/power", body={"signal": signal})
    print(f"Sent '{signal}' to server {cfg['PANEL_SERVER_ID']}.")


def main(argv: list[str]) -> None:
    if len(argv) != 1 or argv[0] not in {"status", *VALID_SIGNALS}:
        sys.exit("Usage: python panel.py {status|start|stop|restart|kill}")
    cfg = _load_config()
    if argv[0] == "status":
        status(cfg)
    else:
        power(cfg, argv[0])


if __name__ == "__main__":
    main(sys.argv[1:])
