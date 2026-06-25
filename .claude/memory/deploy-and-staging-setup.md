---
name: deploy-and-staging-setup
description: "BisectHosting panel, panel.py, staging bot, environment model — fully set up as of 2026-06-25"
metadata:
  type: project
---

## Environments

| Stage | Command | Purpose |
|---|---|---|
| local | `python -m unittest discover -s tests` | logic / regression |
| staging | `python run_test.py` | runs test bot locally via `.env.test` |
| production | `python panel.py restart` | deploys live bots via panel API |

## Hosting (BisectHosting / Pterodactyl)

- ONE panel server, ID `19d7e6d1`, at `games.bisecthosting.com`
- Auto-pulls from `main` branch of `github.com/9mits/custom-discord-bot` on every restart — no SSH/systemd
- `panel.py` wraps the Pterodactyl client API: `python panel.py {status|start|stop|restart}`
- Credentials in git-ignored `.panel.env` (`PANEL_URL`, `PANEL_SERVER_ID`, `PANEL_API_KEY`) — NEVER commit
- Must send a browser User-Agent — Cloudflare blocks Python's default urllib UA (Error 1010). Already fixed in `panel.py`
- `core/bot.py:on_ready` prints `"successfully finished startup"` — required for the panel to show `running` state

## Staging is LOCAL only

The test bot (`.env.test`) runs on the dev machine via `run_test.py`. NOT on the panel. The panel runs only `.env.bot1` / `.env.bot2` via `start.py`. `.env.test` was deleted from the panel to avoid double-running the test token.

## Outstanding items (as of 2026-06-25)

- Test token in local `.env.test` is INVALID (`LoginFailure: Improper token`). User must reset at https://discord.com/developers/applications → test bot → Bot → Reset Token → update `.env.test`
- Panel API key may be exposed (was pasted in chat) — consider regenerating in panel Account API Credentials

See [[autonomous-pr-workflow]] for the PR/merge loop.
