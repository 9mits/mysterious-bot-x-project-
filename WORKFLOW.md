# Development Workflow

This is how changes get from your editor to the live bots. The goal is simple:
**`main` is always deployable.** Production (the real bots) only ever runs code
that lives on `main` and passed CI. Nothing reaches a live community by accident.

If you internalize one thing, make it this loop:

```
branch  →  code + test locally  →  push  →  Pull Request  →  CI green  →  merge to main  →  deploy
```

This is the same loop professional teams use. It works identically whether
you're solo or on a team of fifty.

---

## Environments

A change moves through three stages. It only advances if the previous one looks
right.

| Stage          | Where                         | Purpose                                          |
| -------------- | ----------------------------- | ------------------------------------------------ |
| **local**      | your machine                  | write code, run the unit tests                   |
| **staging**    | the test bot (`.env.test`)    | verify against the real Discord API before going live |
| **production** | `.env.bot1` / `.env.bot2`     | the live bots acting on real communities         |

The unit tests catch logic bugs. They **cannot** catch Discord-level problems —
wrong permissions, a slash command that won't sync, a panel that won't render, a
mute role that doesn't apply. That's what the staging bot is for: a throwaway
Discord server where a mistake costs nothing.

---

## The loop, step by step

### 1. Branch off `main` — never commit to `main` directly

```bash
git checkout main
git pull                      # make sure you're current
git checkout -b fix/mute-bug  # see naming below
```

Branch names: `fix/...` for bug fixes, `feat/...` for new features,
`chore/...` for tooling/docs/cleanup, `refactor/...` for restructuring.

### 2. Code, then test locally

```bash
python -m unittest discover -s tests   # all tests
python -m pyflakes core/ cogs/ tests/  # lint
```

Commit in small, logical chunks as you go:

```bash
git add <files>
git commit -m "Fix mute role not applying when target has no roles"
```

### 3. (When ready) verify on staging

Run the change on the **test bot** in your private test server:

```bash
# from the branch you're working on
TEST_MODE=1 DISCORD_BOT_TOKEN=<test-token> python main.py
# or: python start.py   (launches the test bot among others)
```

Exercise the actual feature in Discord. This is the step that catches what unit
tests can't.

### 4. Push the branch and open a Pull Request

```bash
git push -u origin fix/mute-bug
```

Then open a PR on GitHub (`base: main`). Opening the PR triggers **CI**
automatically — syntax check, pyflakes, and the unit tests across Python
versions.

### 5. Review your own diff

Read the PR diff top to bottom before merging — yes, even solo. Reviewing your
own change a few minutes later, in the GitHub diff view, catches an embarrassing
amount. This is the habit that makes you a stronger engineer.

### 6. Merge once CI is green

Merge the PR on GitHub. `main` now contains the change, proven to pass CI.

### 7. Deploy to production

The live bots run on the BisectHosting panel, which **auto-pulls from `main` on
restart**. So deploying = restarting the server, which you can do from here:

```bash
python panel.py restart    # panel pulls latest main, bot1 + bot2 restart on it
```

Check it came back up with `python panel.py status` (expect `running`).
Production is now running the merged, CI-passed commit. Done.

> The **test bot** is not on the panel — it runs locally via `run_test.py` for
> staging (see the staging note in step 3), so production only ever runs the two
> live tokens.

---

## Rules of thumb

- **`main` is sacred.** If it's on `main`, it should be safe to deploy right now.
- **One PR = one logical change.** Don't bundle an automod fix with a rename.
- **If CI is red, it doesn't merge.** Fix the branch, don't override the gate.
- **A change isn't "done" because it's written** — it's done when it's tested,
  merged, and deployed.

---

## One-time setup: protect `main`

To stop yourself from accidentally pushing straight to `main`, turn on branch
protection (GitHub web UI — this repo has no `gh` CLI configured):

1. GitHub repo → **Settings** → **Branches** → **Add branch ruleset** (or
   "Add rule" under classic branch protection).
2. Branch name pattern: `main`.
3. Enable **Require a pull request before merging**.
4. Enable **Require status checks to pass before merging**, and select the
   **CI** check.
5. Save.

After this, `main` can only change through a PR whose CI passed — the workflow
above becomes enforced, not just intended.
