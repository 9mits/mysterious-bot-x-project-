# AGENTS.md

Instructions for AI coding agents (Claude Code, Cursor, Copilot, Codex, Aider)
working in this repo: a Python Discord moderation bot (~16k LOC, discord.py 2.x,
SQLite). Solo developer, owner `9mits`. This file is the source of truth; tool-
specific files (`CLAUDE.md`) import it.

## Non-negotiables

These three rules override everything else. The rest of this file is guidance.

1. **Merge only after the user says "merge."** Run the whole loop autonomously —
   branch, code, test, push, open PR, watch CI, fix failures — but stop at green
   CI and ask "Ready to merge?" This bot acts on real users and the test suite is
   thin (~38 tests), so a human approves before anything reaches the live bots.
2. **Keep secrets out of git.** `.env*`, `.panel.env`, `config.json`, and
   `database*/` are git-ignored and hold live tokens and user data. Stage files
   by name; read what `git status` shows before committing.
3. **Keep moderators anonymous in user-facing output.** Report DMs, the report
   log, and transcripts must never name or hint at which staff member acted —
   it invades privacy and lets bad actors target moderators.

## Commands

```bash
# Run
python main.py                 # single bot (needs DISCORD_BOT_TOKEN in env or .env)
python start.py                # all bots — one process per .env.bot* file
python run_test.py             # staging: test bot on this machine, loads .env.test only

# Test + lint (CI runs exactly these — run them before every commit)
python -m unittest discover -s tests       # 38 tests, no Discord connection needed
python -m pyflakes core/ cogs/ tests/
python -m py_compile cogs/*.py

# Optional local quality pass (ruff config in pyproject.toml; not yet in CI)
ruff check core/ cogs/ tests/

# Deploy (BisectHosting panel auto-pulls main on restart)
python panel.py restart
python panel.py status         # expect: running
```

## Workflow — the PR loop

Branch off `main`, never commit to it directly (a ruleset rejects direct pushes).
Branch prefixes: `fix/` `feat/` `chore/` `refactor/`. Full human-facing version in
`CONTRIBUTING.md`.

```bash
git checkout main && git pull
git checkout -b fix/short-description
# ...edit, then run the test+lint block above...
git add <specific files>       # stage by name; review what git status shows
git commit -m "fix: what changed and why, under 72 chars"
git push -u origin fix/short-description
& "C:\Program Files\GitHub CLI\gh.exe" pr create --title "..." --body "..." --base main
& "C:\Program Files\GitHub CLI\gh.exe" pr checks <number> --repo 9mits/custom-discord-bot
```

`gh` lives at `C:\Program Files\GitHub CLI\gh.exe` (authed as `9mits`, often not
on PATH — use the full path). Both `test (3.11)` and `test (3.12)` must pass; fix
failures on the branch rather than working around the gate. When CI is green,
report back and wait. After the user says "merge":

```bash
& "C:\Program Files\GitHub CLI\gh.exe" pr merge <number> --squash --delete-branch
git checkout main && git pull
python panel.py restart && python panel.py status
```

`main` is protected by GitHub ruleset `18121569`: PR required, both CI checks
required, 0 approvals (so solo merge works), no force-push or deletion.

## Environments

| Stage | Entry point | Tokens | Runs on |
|---|---|---|---|
| local | `python -m unittest …` | none | dev machine |
| staging | `python run_test.py` | `.env.test` only | dev machine |
| production | `python panel.py restart` | `.env.bot1` + `.env.bot2` | BisectHosting panel |

The staging bot runs locally, not on the panel. The panel runs `start.py`, which
picks up only `.env.bot1` and `.env.bot2` (`.env.test` was removed from the panel
so the test token never double-runs).

## Hosting — BisectHosting (Pterodactyl)

One panel server, ID `19d7e6d1`, at `games.bisecthosting.com`. On each restart it
auto-pulls `main` from `github.com/9mits/custom-discord-bot` and reinstalls pip
deps — so `panel.py restart` is the deploy. There is no SSH or systemd; the panel
API is the only remote control path.

- `panel.py` wraps the Pterodactyl client API (stdlib only). Creds come from the
  git-ignored `.panel.env` next to it.
- `panel.py` sends a browser User-Agent because Cloudflare blocks urllib's default
  (error 1010).
- `core/bot.py:on_ready` prints `successfully finished startup` — the panel scans
  stdout for that exact phrase to flip `starting` → `running`. Keep it.

## Layout

```
main.py / start.py / run_test.py / panel.py   entry points (see Commands)
pyproject.toml   project metadata + tool config (ruff); deps stay in requirements.txt
core/      framework, no Discord UI code
  bot.py        MGXBot class, intents, background tasks, EXTENSIONS, lifecycle
  data.py       DataManager (persistence), AntiAbuseSystem, resolve_bot_token
  services.py   config validation, escalation matrix, normalization
  constants.py  IDs, brand strings, colours, scope labels, TOKEN_ENV_VARS
  context.py    proxy singletons: bot, tree, abuse_system
  models.py     dataclasses: CaseMetadata, EscalationStep, ValidationFinding, CaseNote
  utils.py      stateless helpers: parse_duration_str, format_duration, truncate_text
cogs/      one discord.py extension per domain
  shared.py            embed builders, log senders, permission checks (no Cog class)
  cases.py / history.py / case_panel.py   case mgmt, history UI, transcript export
  moderation.py        execute_punishment, ModGroup commands, /punish
  roles.py / derole.py custom booster roles; bulk role removal
  modmail.py           ticket relay, control/panel views
  automod.py           native + smart automod engine, /automod
  config.py            /setup, /config and settings views
  analytics.py         /stats, /directory, staff profiles
  admin.py             admin commands, anti-nuke, branding
  events.py            raw @bot.event listeners + native AutoMod bridge
  event_leaderboard.py VC-time leaderboard (gated on EVENT_CONTROL=1)
  registry.py          documents the cog dependency graph
  testkit.py           test-only cog, loaded only under TEST_MODE=1
tests/     unittest suite (no real Discord connection)
```

## Architecture notes

Read the relevant file when you touch an area; these are the non-obvious points.

- **Startup:** `core/bot.py:run()` → `setup_hook` opens the SQLite DB, loads all
  state into memory, restores persistent views, loads `EXTENSIONS`, starts the
  background task loops. `testkit` loads only under `TEST_MODE=1`.
- **Data:** `DataManager` holds everything in memory and persists to
  `<BOT_DATA_DIR>/bot.db` (aiosqlite; defaults to `database/`). First run
  auto-migrates legacy `*.json` to SQLite. Persist through the dirty-flag methods
  (`mark_config_dirty()`, `save_punishments()`, …), not `save_all()` directly.
- **Cogs:** each domain file defines a `*Cog` and `async def setup(bot)`. Slash
  commands are module-level `@tree.command` functions registered via
  `core/context.tree` at import; `setup()` only adds the Cog and its listeners.
- **Command sync:** `setup_hook` auto-syncs the tree (guild-scoped, instant) on
  startup — to `TEST_GUILD_ID` under `TEST_MODE=1`, else the configured
  `guild_id`. Each single-guild instance keeps its own guild current on deploy
  (= panel restart), so there's no manual step. A command-set fingerprint is
  cached in `config[synced_command_fingerprint_<guild>]` so unchanged restarts
  skip the API call (rate-limit safety). The `!sync` prefix command in
  `admin.py` remains as a manual override; a sync failure never blocks startup.
- **Circular imports:** `shared.py` ↔ `automod.py`/`cases.py`/`roles.py`/
  `modmail.py`/`admin.py` are mutually dependent. Resolve any new cross-domain
  call with a lazy import inside the function body, matching the existing pattern.
  Do not hoist these to module top level — it reintroduces the cycle.
  `cogs/registry.py` has the full graph.
- **Context proxies:** import `bot`, `tree`, `abuse_system` from `core/context.py`
  rather than threading the bot instance through call signatures.
- **Tokens:** `resolve_bot_token()` checks `config.json:"token_env_var"`, then
  falls back through `TOKEN_ENV_VARS` (`DISCORD_BOT_TOKEN`, `MBX_BOT_TOKEN`).
- **Deps:** `pip install -r requirements.txt` (discord.py>=2.6, aiohttp>=3.13,
  aiosqlite>=0.22, python-dotenv).

## Conventions

Match the surrounding code; these are the project-specific choices that aren't
obvious from a single file.

- **Write user-facing output without decorative emoji.** The only allowed emoji
  are functional reactions with no text equivalent: the public-execution vote `✅`
  (added in `moderation.py`, counted in `events.py`) and the modmail relay markers
  `✅`/`📨` (`events.py`). Code comments and docstrings are exempt.
- **Set embed footers to the brand name alone** (no scope label) so they don't
  wrap on narrow clients — done in `make_embed`/`brand_embed` in `shared.py`.
- **Resolve a target to a full guild `Member` before acting on it** with
  `resolve_member()` and the `UserSelect` pickers; the native slash `user:` picker
  is client-side and silently drops some real members. `/punish` keeps a `user_id:`
  fallback for members the picker can't reach.
- **Match Components V2 (buttons + dropdowns) on the panels that already use it**
  rather than dropping back to plain embeds on that surface.
- **Write commit subjects as `type: summary`** (`fix`/`feat`/`chore`/`refactor`),
  under 72 chars.
- **Add a comment only when the "why" is non-obvious;** skip comments that restate
  what the code already says.
