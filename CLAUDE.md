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
