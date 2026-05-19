# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Running the bot

```bash
# Requires a Discord bot token in the environment
DISCORD_BOT_TOKEN=your_token python3 mbx_main.py

# Or via a .env file (python-dotenv is not a dependency — set env vars manually or use direnv)
```

## Tests

```bash
# Run all tests from the project root
python3 -m unittest discover -s tests

# Run a single test file
python3 -m unittest tests.test_data

# Run a single test case
python3 -m unittest tests.test_bootstrap.MbxBootstrapTests.test_create_bot_does_not_require_token
```

## Static analysis

```bash
python3 -m pyflakes modules/ cogs/ ui/ tests/
python3 -m py_compile modules/commands/*.py  # syntax check
```

## Dependencies

```bash
pip install -r requirements.txt  # discord.py>=2.6, aiohttp>=3.13, aiosqlite>=0.22
```

---

## Architecture

### Entry point

`mbx_main.py` → `modules/bot.py:run()` creates and starts `MGXBot`, a `commands.Bot` subclass. On `setup_hook` it opens the aiosqlite database, loads all data into memory, registers persistent views, and loads the 5 cog extensions.

### Module layout

```
modules/
  bot.py          — MGXBot class, background tasks, lifecycle
  data.py         — DataManager (all persistence), AntiAbuseSystem, resolve_bot_token
  services.py     — Business logic: config validation, escalation matrix, normalization
  constants.py    — IDs, brand strings, colour palette, scope labels, feature flag names
  context.py      — Module-level proxy singletons: bot, tree, abuse_system
  models.py       — Dataclasses: CaseMetadata, EscalationStep, ValidationFinding, CaseNote
  utils.py        — Stateless helpers: parse_duration_str, format_duration, truncate_text, etc.
  commands/       — All slash commands and UI, split by domain (see below)
  automod.py      — Thin re-export shim → modules.commands
  moderation.py   — Thin re-export shim → modules.commands
  modmail.py      — Thin re-export shim → modules.commands
  roles.py        — Thin re-export shim → modules.commands
  system.py       — Thin re-export shim → modules.commands
  logging.py      — send_log / send_punishment_log wrappers
  permissions.py  — Role-based permission helpers
cogs/             — discord.py extension loaders (each calls bot.tree.add_command / add_listener)
ui/               — Small discord.py View/Modal stubs that import from modules.commands
tests/            — unittest suite (no real Discord connection required)
database/         — Runtime data: bot.db (SQLite, auto-created), plus legacy *.json files
```

### commands/ subpackage

All real implementation lives here. Each file is a domain slice:

| File | Contents |
|------|---------|
| `shared.py` | Shared embed builders, log senders, permission checks, path constants, HTTP helpers |
| `cases.py` | Case/punishment helpers, history views, undo/appeal flows |
| `moderation.py` | `execute_punishment`, `ModGroup` slash commands, punish/history menus |
| `roles.py` | Custom booster role CRUD, `role_cmd`, `role_manage`, `role_settings` |
| `modmail.py` | Support ticket relay, `ModmailControlView`, `ModmailPanelView` |
| `automod.py` | Native + smart automod engine, policy views, `automod_cmd` |
| `config.py` | `/setup`, `/config` commands, all settings views |
| `analytics.py` | `/stats`, `/directory`, staff profile views |
| `system.py` | All remaining commands + every event listener (`on_message`, `on_ready`, `on_automod_action`, etc.) |
| `__init__.py` | `from .shared import *` … `from .system import *` — re-exports everything |

The thin shims in `modules/` (e.g. `modules/moderation.py`) do `from modules.commands import *` so cogs can import from either path.

### Data layer

`DataManager` in `modules/data.py` holds all state in-memory and persists to `database/bot.db` (SQLite via aiosqlite). On first startup it auto-migrates any legacy `*.json` files to SQLite and renames them to `*.json.bak`.

In-memory structure mirrors the old JSON shape exactly — `data_manager.punishments` is `{user_id: [records]}`, `data_manager.config` is a flat dict, etc. Use the dirty-flag methods (`mark_config_dirty()`, `save_config()`, `save_punishments()`, etc.) rather than calling `save_all()` directly.

### Circular import pattern

`shared.py` ↔ `automod.py`/`cases.py`/`roles.py` have mutual dependencies. These are resolved with **lazy imports inside function bodies** — if you add a cross-domain call, do the same rather than adding a top-level import.

### Context proxies

`modules/context.py` exposes `bot`, `tree`, and `abuse_system` as module-level proxies set during `setup_hook`. All command files import from here instead of passing the bot instance around.

### Token resolution

`resolve_bot_token()` in `data.py` checks `config.json:"token_env_var"` first, then falls back through `DISCORD_BOT_TOKEN` and `MBX_BOT_TOKEN` environment variables.
