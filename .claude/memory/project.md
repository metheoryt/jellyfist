<!-- KB refreshed against 9fd979a on 2026-07-28 -->

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
  cannot run from a worktree here at all. See the host memory for the underlying
  toggle.
- **There is no Windows checkout to fall back to.** Probed 2026-07-26: nothing
  named `airdrome` exists anywhere under `C:\Users\methe` on `g614jv` (the empty
  `C--Users-methe-GitHub-airdrome` transcript slug dir is the fossil of a deleted
  clone), and `server` has none either. The base checkout `/home/me/my/airdrome`
  sits in the *same* WSL distro as the Orca worktrees, so it shares the Docker
  breakage. The one fleet box with both an airdrome clone and a working Docker
  daemon is `latitude` (`~/my/airdrome`) — but that clone cannot pull: `git` over
  SSH to GitHub fails there. As of 2026-07-26 it sits at `ef148b8`: current on
  code, behind only on kb/docs commits, so it *can* run the suite against today's
  code. It just can't be updated — every code commit from here widens the gap, and
  unblocking its GitHub SSH is the prerequisite for it staying a usable test box.
  The key is **not** unregistered (an earlier reading of this recorded here and in
  `machines` was wrong): it is on the account, and the fault is local — see
  `agents/hosts/latitude5520.md` in `machines` for the diagnosis and the probe
  commands.
- **Treat "run it on `latitude`" as conditional, not a standing fallback.** It is a
  laptop, not an always-on server, and is routinely off the tailnet — this run found
  it `offline, last seen 9h ago` and `fleet-gather.sh` skipped it as unreachable.
  When a change needs the suite and `latitude` is down, there is no other box: park
  the verification rather than assuming a fallback exists.

## KB refresh cron

- **This file is maintained from a worktree branch, not from `main`.** The daily
  kb-refresh job runs in `orca/workspaces/airdrome/ubuntu26-airdrome-kb-refresh-daily`,
  commits here and pushes `metheoryt/ubuntu26-airdrome-kb-refresh-daily` — merging
  back into `main` is user-gated, so an unattended run never does it. The base
  checkout `/home/me/my/airdrome` and every other worktree therefore load whatever
  copy `main` happens to hold, which can be several refreshes behind. Before
  trusting this file's contents from anywhere else in the repo, check
  `git log --oneline main..metheoryt/ubuntu26-airdrome-kb-refresh-daily`; catching
  up is a fast-forward run from the base checkout.
