# Codex upstream watcher

This watcher notices when OpenAI's configured Codex ref differs from the
manually promoted local pin. An unchanged check only runs `git ls-remote`; it
does not invoke Herdr, Codex, or a model.

The first changed SHA is recorded as the pending candidate for the current
pin. The watcher asks the canonical `~/dotfiles/bin/spawn` launcher for one
Codex worker in a SHA-named isolated worktree. A successful launch is recorded
and not repeated. A failed launch leaves the candidate pending and retries on
the next run. A fixed branch name and a state lock prevent duplicate live work.
Later upstream movement does not create more workers until the checked-in pin
is manually advanced.

The generated task packet requires the worker to preserve sticky composer,
test, commit, and report. It explicitly forbids merging to the primary/fork
base, installing a binary, changing the live symlink, pushing, or cleanup.

## Setup and operation

Review [upstream-watch.conf](upstream-watch.conf), especially the source repo,
fork base, remote/ref, and the paired release/SHA pin. The committed pin is npm
Codex `0.151.0` at `94cbbddafc1776d5e377bca1b05932c697e82238`.
The durable fork base is local `main`; temporary sticky-composer implementation
branches are deliberately not part of the automation configuration.

The service PATH prefers `~/.local/bin`, where the eventual manually promoted
Codex launcher will live, and currently falls back to the installed NVM
`v22.19.0` bin directory. On 2026-08-30, read-only verification found `herdr`
at `~/.local/bin/herdr` and `codex` at
`~/.nvm/versions/node/v22.19.0/bin/codex`; standard PATH entries provide Git,
`jq`, Bash, and `flock`. Before spawning, the watcher checks that the canonical
launcher is executable and that `herdr`, `jq`, and `codex` all resolve. A
failure leaves the candidate pending for a safe retry. Because
`~/.local/bin` is first, the eventual pinned Codex launcher takes precedence
without another unit edit.

Link the units into the user systemd search path, but do not enable them yet:

```bash
systemctl --user link "$HOME/dotfiles/systemd/user/codex-upstream-watch.service" "$HOME/dotfiles/systemd/user/codex-upstream-watch.timer"
systemctl --user daemon-reload
```

Perform a read-only dry run (remote access is required):

```bash
~/dotfiles/bin/codex-upstream-watch --dry-run
```

Run one real check manually with either command:

```bash
~/dotfiles/bin/codex-upstream-watch
systemctl --user start codex-upstream-watch.service
```

Inspect state and logs:

```bash
~/dotfiles/bin/codex-upstream-watch --status
journalctl --user -u codex-upstream-watch.service
HERDR_ENV=1 herdr agent list
git -C "$HOME/OS-tools/codex" worktree list
```

State lives at
`${XDG_STATE_HOME:-$HOME/.local/state}/codex-upstream-watch/`. `pending.env`
is the durable candidate; `spawned.env` is the de-duplication receipt;
`last-attempt.env` and the SHA-specific spawn log explain failures. When the
configured pin changes, these records move to `history/` rather than being
discarded.

Enable the timer explicitly:

```bash
systemctl --user enable --now codex-upstream-watch.timer
```

Disable it without removing state or worktrees:

```bash
systemctl --user disable --now codex-upstream-watch.timer
```

## Manual promotion

Review the worker branch and its report, run any broader validation, and carry
out merge/install/symlink changes manually outside this watcher. Only after
that promotion, edit `CODEX_UPSTREAM_RELEASE` and `CODEX_PIN_SHA` together in
`upstream-watch.conf`. The next check archives the old campaign records and can
open one new campaign if upstream has moved again.

The watcher never merges, installs, activates a build, pushes, or removes a
branch/worktree. Existing branch/worktree conflicts deliberately fail closed
and remain visible in the pending state and spawn log for owner intervention.
