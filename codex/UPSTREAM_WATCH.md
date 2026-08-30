# Codex upstream watcher

This watcher notices when OpenAI publishes a newer stable Codex Rust release.
It lists `rust-v*` tags with `git ls-remote`, accepts only exact stable
`rust-vX.Y.Z` names, compares the numeric release components, and uses the
tag's `^{}` commit when the tag is annotated. Alpha and other prerelease tags
are ignored. A check with no newer stable version does not invoke Herdr, Codex,
or a model.

The latest stable release is recorded with its release, tag, and resolved
commit when it advances the current manual promotion checkpoint.
The watcher asks the canonical `~/dotfiles/bin/spawn` launcher for one Codex
worker in a release-and-SHA-named isolated worktree. A successful launch is
recorded and not repeated. A failed launch leaves the candidate pending and
retries on the next run. A fixed branch name and a state lock prevent duplicate
live work. Later stable releases do not create more workers until the
checked-in promotion facts are manually advanced.

The generated task packet requires the worker to preserve sticky composer,
test, commit, and report. It explicitly forbids merging to the primary/fork
base, installing a binary, changing the live symlink, pushing, or cleanup.

## Setup and operation

Review [upstream-watch.conf](upstream-watch.conf), especially the source repo,
fork base, upstream tag prefix, and two paired promotion facts:

- The installed official npm release is `0.151.0`.
- The exact upstream baseline integrated by the sticky fork is
  `94cbbddafc1776d5e377bca1b05932c697e82238`.

These are deliberately distinct. In particular, this documentation does not
claim that the `rust-v0.151.0` tag resolves to the fork baseline. At review
time, that stable tag dereferenced to
`78c290807ce710180111df227df3b7a4fe845452`, while the fork baseline above is
76 commits later. The watcher re-resolves stable tags on every check. The
durable fork base is local `main`; temporary sticky-composer implementation
branches are not part of the automation configuration.

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
`${XDG_STATE_HOME:-$HOME/.local/state}/codex-upstream-watch/`. `observed.env`
records the latest stable release/tag commit and both current promotion facts.
`pending.env` records the candidate release, stable tag, and dereferenced tag
commit; `spawned.env` is the de-duplication receipt. `last-attempt.env` and the
release-and-SHA-specific spawn log explain failures. When both configured
promotion facts change, these records move to `history/` rather than being
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
that promotion, update `CODEX_INSTALLED_RELEASE` to the promoted official
release and `CODEX_FORK_UPSTREAM_BASELINE_SHA` to the exact integrated upstream
commit, together, in `upstream-watch.conf`. A half-updated checkpoint fails
closed while a candidate is pending. The next successful check archives the
old campaign records and can open one new campaign if another stable release
has appeared.

The watcher never merges, installs, activates a build, pushes, or removes a
branch/worktree. Existing branch/worktree conflicts deliberately fail closed
and remain visible in the pending state and spawn log for owner intervention.
