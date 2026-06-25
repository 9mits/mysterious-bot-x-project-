# CLAUDE.md

Instructions for Claude Code when working in this repository.
Read this fully before taking any action. These rules override defaults.

---

## The one rule that matters most

**Never merge to `main` without explicit user confirmation.**

The loop is: branch → code → test → push → PR → watch CI → fix failures →
report back "CI is green, ready to merge?" → wait → user says "merge" → then
merge + deploy. Do not skip the pause. Do not merge proactively. Always wait.

---

## Running the bot

```bash
# Single bot (dev / debugging)
DISCORD_BOT_TOKEN=your_token python main.py

# All bots at once — one process per .env.bot* file found
python start.py

# Staging only (test bot, local machine)
python run_test.py          # loads .env.test, never touches live tokens

# Deploy to production
python panel.py restart     # BisectHosting panel pulls main + restarts
python panel.py status      # check state (expect: running)
```

---

## Tests and lint

Always run both before committing. CI runs exactly these:

```bash
python -m unittest discover -s tests          # 38 tests, no Discord connection needed
python -m pyflakes core/ cogs/ tests/         # lint
python -m py_compile cogs/*.py                # syntax check
```

Tests run without a real Discord connection. `cogs/testkit.py` is loaded only
under `TEST_MODE=1`.

---

## Development workflow (autonomous PR loop)

When working on any change, follow this loop exactly:

### 1. Branch — never commit to `main` directly
```bash
git checkout main && git pull
git checkout -b fix/short-description   # or feat/ chore/ refactor/
```

### 2. Code, then verify locally
```bash
python -m unittest discover -s tests
python -m pyflakes core/ cogs/ tests/
```

### 3. Commit and push
```bash
git add <specific files>    # never git add -A without reviewing what's staged
git commit -m "fix: short description of what changed and why"
git push -u origin <branch>
```

### 4. Open PR via gh CLI
```bash
& "C:\Program Files\GitHub CLI\gh.exe" pr create --title "..." --body "..." --base main
```

### 5. Watch CI — fix failures on the branch, never force-merge
```bash
& "C:\Program Files\GitHub CLI\gh.exe" pr checks <number> --repo 9mits/custom-discord-bot
```
Both `test (3.11)` and `test (3.12)` must be green. If either fails, fix and
re-push. Do not report back until CI is fully green.

### 6. STOP — report back and wait for merge confirmation
Say: "CI is green — ready to merge?" Show what changed. Do not proceed.

### 7. After user says "merge"
```bash
& "C:\Program Files\GitHub CLI\gh.exe" pr merge <number> --squash --delete-branch
git checkout main && git pull
python panel.py restart     # deploy
python panel.py status      # confirm running
```

### Branch protection
`main` is protected by GitHub Ruleset ID `18121569`:
- PR required (no direct pushes — they are blocked)
- `test (3.11)` + `test (3.12)` must pass
- 0 approvals required (solo dev, so auto-merge works)
- No force-push, no branch deletion

### gh CLI
Path: `C:\Program Files\GitHub CLI\gh.exe` (authenticated as `9mits`).
May not be on `PATH` in fresh PowerShell — always use the full path.

---

## Environments

| Stage | Entry point | Tokens used | Where it runs |
|---|---|---|---|
| local | `python -m unittest ...` | none | dev machine |
| staging | `python run_test.py` | `.env.test` only | dev machine (local) |
| production | `python panel.py restart` | `.env.bot1` + `.env.bot2` | BisectHosting panel |

The staging bot is **local only** — it is NOT on the BisectHosting panel.
The panel runs `start.py`, which picks up only `.env.bot1` and `.env.bot2`
(`.env.test` was deleted from the panel to prevent double-running the test token).

---

## Hosting — BisectHosting (Pterodactyl)

- One panel server, ID `19d7e6d1`, at `games.bisecthosting.com`
- On every restart the panel auto-pulls from the `main` branch of
  `github.com/9mits/custom-discord-bot` and reinstalls pip packages
- There is no SSH / shell / systemd — the only deploy path is `panel.py restart`
- `panel.py` wraps the Pterodactyl client API (stdlib only, no dependencies)
- Credentials: git-ignored `.panel.env` — **never commit this file**
- Cloudflare blocks Python's default urllib user-agent; `panel.py` already sends
  a browser UA to work around this
- `core/bot.py:on_ready` prints `"successfully finished startup"` — BisectHosting
  watches for this exact phrase to flip the server state from `starting` → `running`

---

## Files — what everything is

```
main.py                  Entry point: from core.bot import run
start.py                 Multi-bot launcher: one process per .env.bot* file
run_test.py              Staging launcher: loads .env.test only, never live tokens
panel.py                 BisectHosting panel API wrapper (deploy / status)
.panel.env               Panel credentials — git-ignored, NEVER commit
WORKFLOW.md              Human-readable version of this workflow

core/
  bot.py                 MGXBot class, intents, background tasks, EXTENSIONS, lifecycle
  data.py                DataManager (all persistence), AntiAbuseSystem, resolve_bot_token
  services.py            Business logic: config validation, escalation matrix, normalization
  constants.py           IDs, brand strings, colour palette, scope labels, TOKEN_ENV_VARS
  context.py             Module-level proxy singletons: bot, tree, abuse_system
  models.py              Dataclasses: CaseMetadata, EscalationStep, ValidationFinding, CaseNote
  utils.py               Stateless helpers: parse_duration_str, format_duration, truncate_text

cogs/
  shared.py              Shared embed builders, log senders, permission checks (no Cog class)
  cases.py               Case management: embed builders, interactive views, undo/appeal flows
  history.py             History + case-panel UI views
  case_panel.py          Case panel views and HTML transcript export
  moderation.py          execute_punishment, ModGroup slash commands, /punish
  roles.py               Custom booster role CRUD, AppealView, build_punish_embed
  derole.py              /derole bulk role-removal workflow
  modmail.py             Support ticket relay, control/panel views, ticket management
  automod.py             Native + smart automod engine, policy views, report flows, /automod
  config.py              /setup, /config commands, all settings views
  analytics.py           /stats, /directory, staff profile views
  admin.py               Admin commands, anti-nuke views, branding
  events.py              Event listeners + native AutoMod bridge
  event_leaderboard.py   Limited-time VC-time leaderboard (EVENT_CONTROL=1 flag)
  registry.py            Cog dependency graph / circular-import boundaries
  testkit.py             Test-only cog, loaded only under TEST_MODE=1

.github/
  workflows/ci.yml                  Matrix CI: py_compile + pyflakes + unittest (3.11 + 3.12)
  PULL_REQUEST_TEMPLATE.md          PR checklist

.claude/memory/                     Project memory files (workflow context, decisions)

database/                           Runtime: bot.db (SQLite, auto-created — git-ignored)
tests/                              unittest suite
```

---

## Architecture

### Entry point
`main.py` → `core/bot.py:run()` creates `MGXBot` (`commands.Bot` subclass).
`setup_hook` opens the aiosqlite database, loads all data into memory, restores
persistent views, loads cog extensions from `EXTENSIONS`, starts background tasks.
`testkit` is loaded additionally only under `TEST_MODE=1`.

### Cog pattern
Every domain file in `cogs/` (except `shared.py`) defines a `*Cog` class and
`async def setup(bot)` that calls `bot.add_cog(...)`. Slash commands are
module-level `@tree.command` functions registered via `core/context.tree` at
import time; `setup()` only registers the Cog and its listeners.

### Data layer
`DataManager` in `core/data.py` holds all state in-memory, persists to
`<BOT_DATA_DIR>/bot.db` (SQLite via aiosqlite; defaults to `database/`).
On first startup it auto-migrates legacy `*.json` files to SQLite.
Use dirty-flag methods (`mark_config_dirty()`, `save_punishments()`, etc.)
rather than `save_all()` directly.

### Circular imports
`shared.py` ↔ `automod.py`/`cases.py`/`roles.py`/`modmail.py`/`admin.py` have
mutual dependencies. Resolved with **lazy imports inside function bodies** — do
the same for any new cross-domain calls. Full graph in `cogs/registry.py`.

### Context proxies
`core/context.py` exposes `bot`, `tree`, `abuse_system` as module-level proxies
set during `setup_hook`. Import from here instead of passing the bot around.

### Token resolution
`resolve_bot_token()` checks `config.json:"token_env_var"` first, then falls
back through `TOKEN_ENV_VARS` (`DISCORD_BOT_TOKEN`, `MBX_BOT_TOKEN`).

### Dependencies
```bash
pip install -r requirements.txt   # discord.py>=2.6, aiohttp>=3.13, aiosqlite>=0.22, python-dotenv
```

---

## Project conventions

- **No decorative emoji in user-facing output.** Only exceptions: functional
  reactions with no non-emoji equivalent — the public-execution vote `✅`
  (`moderation.py` adds it, `events.py` counts it) and modmail relay markers
  `✅`/`📨` (`events.py`). Internal comments/docstrings are exempt.

- **Embed footers are the brand name only** — no scope label, so they don't
  wrap on narrow clients. Set via `make_embed`/`brand_embed` in `shared.py`.

- **Never expose a moderator's identity in user-facing output.** AutoMod report
  DMs and the report log must not include a "Responder" field or similar.

- **Resolve targets to a full guild `Member` when acting on them.** The native
  slash `user:` picker can silently fail to select some members. Use
  `resolve_member()` and `UserSelect` pickers. `/punish` has a `user_id:`
  fallback for members the native picker can't reach.

- **Several panels use Components V2** (buttons + dropdowns) — match the
  surrounding style when editing a given surface.

- **Commit messages:** `type: short description` format. Types: `fix`, `feat`,
  `chore`, `refactor`. Keep the subject line under 72 chars.

---

## What NOT to do

- Do not push directly to `main` — branch protection will reject it.
- Do not merge a PR without explicit "merge" from the user.
- Do not commit `.env.*`, `.panel.env`, `config.json`, `database*/` — git-ignored, contain live secrets.
- Do not add `git add -A` blindly — stage specific files and review what's included.
- Do not add decorative emoji, trailing summaries, or verbose commentary to responses.
- Do not add error handling for impossible cases or features the user didn't ask for.
- Do not write comments that explain what the code does — only write them when the WHY is non-obvious.
