# Contributing

This is a solo project, but it runs on a real workflow so `main` stays
deployable. The full reference — commands, environments, architecture, and
conventions — lives in **[AGENTS.md](AGENTS.md)**. This page is the short version.

## The loop

```
branch  →  code + test locally  →  push  →  PR  →  CI green  →  merge  →  deploy
```

1. **Branch** off `main` (never commit to `main` directly — a ruleset blocks it):
   ```bash
   git checkout main && git pull
   git checkout -b fix/short-description     # fix/ feat/ chore/ refactor/
   ```
2. **Test + lint** before pushing:
   ```bash
   python -m unittest discover -s tests
   python -m pyflakes core/ cogs/ tests/
   ```
3. **Push** and open a PR against `main`. Both CI checks (`test (3.11)` and
   `test (3.12)`) must pass.
4. **Merge** once CI is green and you've reviewed the diff.
5. **Deploy**: `python panel.py restart` — the BisectHosting panel auto-pulls
   `main` on restart.

## Conventions

- Commit subjects: `type: summary` (`fix`/`feat`/`chore`/`refactor`), under 72 chars.
- One PR = one logical change.
- Never commit secrets: `.env*`, `.panel.env`, `config.json`, and `database*/`
  are git-ignored and must stay that way.

See [AGENTS.md](AGENTS.md) for the detailed workflow, the three-environment model
(local / staging / production), and hosting notes.
