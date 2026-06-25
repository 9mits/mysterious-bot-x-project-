# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the bot

```bash
# Single bot — requires a Discord bot token in the environment
DISCORD_BOT_TOKEN=your_token python3 main.py

# Or place the token in a .env file — read via python-dotenv if present.

# Multiple bots at once — start.py launches one process per env file it finds
# (.env.bot1, .env.bot2, .env.test):
python3 start.py
```

Each per-bot env file sets its own `DISCORD_BOT_TOKEN` (or a custom var named in
`config.json:"token_env_var"`), and typically its own `BOT_DATA_DIR`
(e.g. `database-bot1/`, `database-bot2/`) and `BOT_BRAND_NAME`. Env files and
per-bot database dirs are git-ignored.

## Tests

```bash
# Run all tests from the project root
python3 -m unittest discover -s tests

# Run a single test file
python3 -m unittest tests.test_data

# Run a single test case
python3 -m unittest tests.test_bootstrap.BootstrapTests.test_create_bot_does_not_require_token
```

Tests run without a real Discord connection. `cogs/testkit.py` is a test-only
cog loaded **only** when `TEST_MODE=1`.

## Static analysis

```bash
python3 -m pyflakes core/ cogs/ tests/
python3 -m py_compile cogs/*.py   # syntax check individual cogs
```

## Dependencies

```bash
pip install -r requirements.txt  # discord.py>=2.6, aiohttp>=3.13, aiosqlite>=0.22
```

---

## Architecture

### Entry point

`main.py` → `core/bot.py:run()` creates `MGXBot`, a `commands.Bot` subclass. On
`setup_hook` it opens the aiosqlite database, loads all data into memory,
restores persistent views, and loads the cog extensions in `EXTENSIONS`
(`core/bot.py`). `testkit` is loaded additionally only under `TEST_MODE`.

### Directory layout

```
main.py             — Entry point: from core.bot import run
start.py            — Multi-bot launcher: one process per .env.bot* file
core/               — Internal framework (no discord UI code)
  bot.py            — MGXBot class, intents, background tasks, EXTENSIONS tuple, lifecycle
  data.py           — DataManager (all persistence), AntiAbuseSystem, resolve_bot_token
  services.py       — Business logic: config validation, escalation matrix, normalization
  constants.py      — IDs, brand strings, colour palette, scope labels, TOKEN_ENV_VARS, flags
  context.py        — Module-level proxy singletons: bot, tree, abuse_system
  models.py         — Dataclasses: CaseMetadata, EscalationStep, ValidationFinding, CaseNote
  utils.py          — Stateless helpers: parse_duration_str, format_duration, truncate_text …
cogs/               — discord.py Cog extensions, one per domain
  shared.py         — Shared embed builders, log senders, permission checks (no Cog class)
  cases.py          — Case management: embed builders, interactive views, undo/appeal flows
  history.py        — History + case-panel UI views (split from cases.py)
  case_panel.py     — Case panel views and HTML transcript export (split from cases.py)
  moderation.py     — execute_punishment, ModGroup slash commands, punish/history menus, /punish
  roles.py          — Custom booster role CRUD, role commands, AppealView, build_punish_embed
  derole.py         — /derole bulk role-removal workflow
  modmail.py        — Support ticket relay, control/panel views, ticket management
  automod.py        — Native + smart automod engine, policy views, report flows, /automod
  config.py         — /setup, /config commands, all settings views
  analytics.py      — /stats, /directory, staff profile views
  admin.py          — Admin commands, anti-nuke views, branding (split from system.py)
  events.py         — Event listeners + native AutoMod bridge (split from system.py)
  event_leaderboard.py — Limited-time VC-time leaderboard
  registry.py       — Documents the cog dependency graph / circular-import boundaries
  testkit.py        — Test-only cog, loaded only under TEST_MODE=1
database/           — Runtime: bot.db (SQLite, auto-created on first run)
tests/              — unittest suite (no real Discord connection required)
```

### Cog pattern

Every domain file in `cogs/` (except `shared.py`) defines a `*Cog` class and an
`async def setup(bot)` that calls `await bot.add_cog(...)`. Event listeners are
`@commands.Cog.listener()` methods on the Cog class (raw `@bot.event` listeners
live in `events.py`). Slash commands are module-level `@tree.command` decorated
functions registered to `tree` at import time via `core/context.tree`; `setup()`
only registers the Cog and its listeners.

### Data layer

`DataManager` in `core/data.py` holds all state in-memory and persists to
`<BOT_DATA_DIR>/bot.db` (SQLite via aiosqlite; `BOT_DATA_DIR` defaults to
`database/`). On first startup it auto-migrates any legacy `*.json` files to
SQLite and renames them to `*.json.bak`.

In-memory structure mirrors the old JSON shape exactly — `data_manager.punishments`
is `{user_id: [records]}`, `data_manager.config` is a flat dict, etc. Use the
dirty-flag methods (`mark_config_dirty()`, `save_config()`, `save_punishments()`,
etc.) rather than calling `save_all()` directly.

### Circular import pattern

`shared.py` ↔ `automod.py`/`cases.py`/`roles.py`/`modmail.py`/`admin.py` have
mutual dependencies. These are resolved with **lazy imports inside function
bodies** — if you add a cross-domain call, do the same rather than adding a
top-level import. `cogs/registry.py` documents the full dependency graph.

### Context proxies

`core/context.py` exposes `bot`, `tree`, and `abuse_system` as module-level
proxies set during `setup_hook`. All cog files import from here instead of
passing the bot instance around.

### Token resolution

`resolve_bot_token()` in `core/data.py` checks `config.json:"token_env_var"`
first, then falls back through `TOKEN_ENV_VARS` (`DISCORD_BOT_TOKEN`,
`MBX_BOT_TOKEN`).

---

## Development workflow (autonomous PR loop)

The owner of this repo (9mits) runs an **autonomous PR workflow**. When working
on any change, Claude Code should:

1. **Branch** off `main` (`fix/`, `feat/`, `chore/`, `refactor/` prefix).
2. **Code + test** locally — run `python -m unittest discover -s tests` and
   `python -m pyflakes core/ cogs/ tests/` before committing.
3. **Push** the branch and open a PR via `gh pr create`.
4. **Watch CI** (`gh pr checks <number>`) — fix any failures on the branch
   before reporting back.
5. **Stop and ask the user: "CI is green — ready to merge?"** Do NOT merge
   automatically. Wait for the user to say "merge" (or similar).
6. On merge confirmation: `gh pr merge <number> --squash --delete-branch`,
   then `git checkout main && git pull`.
7. **Deploy**: `python panel.py restart` — BisectHosting auto-pulls `main` on
   restart, so this deploys the live bots (bot1 + bot2).

`main` is protected by GitHub Ruleset 18121569 (active): PR required, both
`test (3.11)` and `test (3.12)` CI checks must pass, no force-push/deletion.

GitHub CLI is at `C:\Program Files\GitHub CLI\gh.exe`, authenticated as
`9mits`. Use the full path or `& "C:\Program Files\GitHub CLI\gh.exe"` in
PowerShell.

### Environments

| Stage | How to run | Purpose |
|---|---|---|
| **local** | `python -m unittest discover -s tests` | logic / regression tests |
| **staging** | `python run_test.py` | runs test bot locally via `.env.test` |
| **production** | `python panel.py restart` | live bots on BisectHosting panel |

The staging bot (`.env.test`) runs **locally on the dev machine** — it is NOT
on the BisectHosting panel. The panel runs only the two live tokens (`.env.bot1`
/ `.env.bot2`) via `start.py`.

### Hosting (BisectHosting / Pterodactyl)

- One panel server, ID `19d7e6d1`, at `games.bisecthosting.com`.
- The panel auto-pulls from the `main` branch of `github.com/9mits/custom-discord-bot`
  and reinstalls pip packages every time the server starts.
- There is no SSH/shell/systemd — everything is via the panel API or the web UI.
- `panel.py` controls the server: `python panel.py {status|start|stop|restart}`.
- Credentials live in git-ignored `.panel.env` (NEVER commit this file).
- The server prints `successfully finished startup` via `on_ready` in
  `core/bot.py` so the panel correctly detects the running state.

### Key files (workflow-related)

- `run_test.py` — local staging launcher, loads `.env.test` only.
- `panel.py` — Pterodactyl client API wrapper; requires `.panel.env`.
- `.panel.env` — git-ignored; holds `PANEL_URL`, `PANEL_SERVER_ID`, `PANEL_API_KEY`.
- `WORKFLOW.md` — human-readable version of this workflow.
- `.github/workflows/ci.yml` — matrix CI: py_compile + pyflakes + unittest on
  Python 3.11 and 3.12.
- `.github/PULL_REQUEST_TEMPLATE.md` — PR checklist template.

### What NOT to do

- Do not push directly to `main` — branch protection will reject it.
- Do not merge a PR without the user's explicit "merge" confirmation.
- Do not commit `.env.*`, `.panel.env`, `config.json`, or `database*/` — all
  git-ignored and contain live secrets/data.

---

## Project conventions

These reflect decisions made for this bot; honour them in new work.

- **No decorative emoji in user-facing output.** Messages, embeds, and exported
  transcripts must not contain decorative emoji. The only emoji that may remain
  are **functional reactions** that drive a feature and have no non-emoji
  equivalent: the public-execution vote `✅` (`moderation.py` adds it,
  `events.py` counts it) and the modmail relay status markers `✅`/`📨`
  (`events.py`). Internal code comments/docstrings (e.g. the `→`/`↔` arrows in
  `registry.py`) are not user-facing and are exempt.

- **Embed footers are the brand name only** (no scope label appended), so they
  don't overflow/wrap on narrow clients. Footer text is set in `make_embed`/
  `brand_embed` in `shared.py`.

- **Never expose a moderator's identity in user-facing moderation output.**
  AutoMod report DMs and the report log must not include a "Responder" field or
  otherwise reveal which staff member acted — it invades privacy and lets bad
  actors target moderators.

- **Resolve targets to a full guild `Member` when acting on them.** The native
  slash `user:` picker is client-side and can silently fail to select some real
  members; prefer `resolve_member()` and the `UserSelect` pickers. `/punish`
  exposes a `user_id:` fallback (accepts an ID or mention, resolved bot-side)
  for members the native picker can't reach.

- **Several panels use Components V2** (buttons + dropdowns) rather than plain
  embeds; match the surrounding style when editing a given surface.
