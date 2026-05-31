# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the bot

```bash
# Requires a Discord bot token in the environment
DISCORD_BOT_TOKEN=your_token python3 main.py

# Or place the token in a .env file — the bot reads it via python-dotenv if present
```

## Tests

```bash
# Run all tests from the project root
python3 -m unittest discover -s tests

# Run a single test file
python3 -m unittest tests.test_data

# Run a single test case
python3 -m unittest tests.test_bootstrap.BootstrapTests.test_create_bot_does_not_require_token
```

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

`main.py` → `core/bot.py:run()` creates `MGXBot`, a `commands.Bot` subclass. On `setup_hook` it opens the aiosqlite database, loads all data into memory, registers persistent views, and loads all 9 cog extensions from `cogs/`.

### Directory layout

```
main.py             — Entry point: from core.bot import run
core/               — Internal framework (no discord UI code)
  bot.py            — MGXBot class, background tasks, lifecycle, EXTENSIONS tuple
  data.py           — DataManager (all persistence), AntiAbuseSystem, resolve_bot_token
  services.py       — Business logic: config validation, escalation matrix, normalization
  constants.py      — IDs, brand strings, colour palette, scope labels, feature flag names
  context.py        — Module-level proxy singletons: bot, tree, abuse_system
  models.py         — Dataclasses: CaseMetadata, EscalationStep, ValidationFinding, CaseNote
  utils.py          — Stateless helpers: parse_duration_str, format_duration, truncate_text …
cogs/               — discord.py Cog extensions, one per domain
  shared.py         — Shared embed builders, log senders, permission checks (no Cog class)
  cases.py          — Case/punishment helpers, history views, undo/appeal flows
  moderation.py     — execute_punishment, ModGroup slash commands, punish/history menus
  roles.py          — Custom booster role CRUD, role_cmd, role_manage, role_settings
  derole.py         — /derole bulk role-removal workflow
  modmail.py        — Support ticket relay, ModmailControlView, ModmailPanelView
  automod.py        — Native + smart automod engine, policy views, automod_cmd
  config.py         — /setup, /config commands, all settings views
  analytics.py      — /stats, /directory, staff profile views
  system.py         — All remaining commands + every event listener
database/           — Runtime: bot.db (SQLite, auto-created on first run)
tests/              — unittest suite (no real Discord connection required)
```

### Cog pattern

Every file in `cogs/` (except `shared.py`) defines a `*Cog` class and an `async def setup(bot)` that calls `await bot.add_cog(...)`. Event listeners are `@commands.Cog.listener()` methods on the Cog class. Slash commands are module-level `@tree.command` decorated functions registered to `tree` at import time via `context.tree`; `setup()` only registers the Cog and its listeners.

### Data layer

`DataManager` in `core/data.py` holds all state in-memory and persists to `database/bot.db` (SQLite via aiosqlite). On first startup it auto-migrates any legacy `*.json` files to SQLite and renames them to `*.json.bak`.

In-memory structure mirrors the old JSON shape exactly — `data_manager.punishments` is `{user_id: [records]}`, `data_manager.config` is a flat dict, etc. Use the dirty-flag methods (`mark_config_dirty()`, `save_config()`, `save_punishments()`, etc.) rather than calling `save_all()` directly.

### Circular import pattern

`shared.py` ↔ `automod.py`/`cases.py`/`roles.py` have mutual dependencies. These are resolved with **lazy imports inside function bodies** — if you add a cross-domain call, do the same rather than adding a top-level import.

### Context proxies

`core/context.py` exposes `bot`, `tree`, and `abuse_system` as module-level proxies set during `setup_hook`. All cog files import from here instead of passing the bot instance around.

### Token resolution

`resolve_bot_token()` in `core/data.py` checks `config.json:"token_env_var"` first, then falls back through `DISCORD_BOT_TOKEN` and `MBX_BOT_TOKEN` environment variables.
