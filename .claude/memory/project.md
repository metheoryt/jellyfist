<!-- KB refreshed against c4d5423 on 2026-07-26 -->

# Airdrome — project memory

Repo-local, git-tracked memory. Durable *workflow* facts that don't belong in
[AGENTS.md](../../AGENTS.md) (current-state map), [README.md](../../README.md) (user-facing)
or [ROADMAP.md](../../ROADMAP.md) (forward-looking).

## Worktrees

- A fresh worktree (Orca or plain `git worktree add`) starts **without `.venv` and
  `.env`** — both are gitignored, so nothing carries over from the base checkout. No
  `uv run` command works until you `uv sync` and create `.env` (`DB_DSN`, `LIBRARY_DIR`
  — see README *Configuration*). The repo has no `.orca/worktree-setup.sh`, so this is
  manual per worktree.
- The test suite additionally needs the compose Postgres up (`docker compose up -d`,
  port 5437) — a doc-only change can be made and committed from a worktree without it,
  but any code change must be verified where Docker is reachable.
- **Docker is *not* reachable from the WSL distro that hosts these Orca worktrees**
  (Ubuntu-26.04 on `g614jv`). `command -v docker` succeeds — Docker Desktop puts a
  shim on `PATH` — but every invocation dies with "The command 'docker' could not be
  found in this WSL 2 distro". So the compose Postgres, and therefore `uv run pytest`,
  cannot run from a worktree here at all; use the Windows checkout or another box.
  See the host memory for the underlying toggle.
