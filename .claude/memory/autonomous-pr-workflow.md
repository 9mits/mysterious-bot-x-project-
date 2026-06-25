---
name: autonomous-pr-workflow
description: "How Claude should drive the dev workflow — auto up to green CI, then pause for human merge OK"
metadata:
  type: feedback
---

For this Discord bot project, run the change loop autonomously **up to green CI**, then **pause for the user's merge OK**.

Loop: branch → code → commit → push → open PR via `gh` → watch CI → fix failures and re-push until green → come back with "CI is green, ready to merge?" and show what changed. **Do NOT merge without explicit user confirmation.** After they say "merge", merge → delete branch → sync local main → `python panel.py restart` to deploy.

**Why:** This is a moderation bot acting on real users in real communities. The test suite is thin (~38 tests / 16k LOC). CI alone isn't a strong enough gate to merge unattended — the user wants a human eye before anything reaches the live bots.

**How to apply:**
- `gh` CLI path: `C:\Program Files\GitHub CLI\gh.exe` (authenticated as `9mits`; may not be on PATH in fresh shells — use full path in PowerShell)
- `main` protected by ruleset ID `18121569`: PR required, `test (3.11)` + `test (3.12)` must pass, 0 approvals (so merge works without a reviewer)
- Never push directly to `main` — ruleset blocks it
- Branch naming: `fix/` `feat/` `chore/` `refactor/`
- Always run `python -m unittest discover -s tests` + `python -m pyflakes core/ cogs/ tests/` locally before pushing

See [[deploy-and-staging-setup]] for how deploy works after merge.
