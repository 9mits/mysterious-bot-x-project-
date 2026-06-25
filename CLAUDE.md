# CLAUDE.md

This project's instructions for all AI coding agents live in **AGENTS.md**, the
cross-tool source of truth. It is imported below so Claude Code loads it in full.

@AGENTS.md

## Claude Code notes

- The **merge gate** in AGENTS.md is absolute: stop at green CI and wait for the
  user's explicit "merge" before merging anything.
- Project memory is machine-local at `~/.claude/projects/<project>/memory/` — it
  is not committed and does not travel with the repo. A repo-level `.claude/memory/`
  folder is **not** auto-loaded, so don't create one expecting it to be read.
- Prefer plan mode before large, multi-file changes.
