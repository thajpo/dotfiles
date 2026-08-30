# Hermes control layer

This directory is the working directory for the persistent Hermes control
session. Hermes loads `AGENTS.md` and `SOUL.md` from here on startup.

The launcher uses a detached tmux session named `hermes-control` and the
isolated `hermes-ops` profile. It resumes the named `control` conversation,
pins inference to Codex OAuth with `gpt-5.6-sol`, and enables only the terminal,
todo, memory, and session-search toolsets. File, skills management, browser,
messaging, delegation, and cron tools are not enabled in the initial profile.

```bash
hermes-control start
hermes-control status
hermes-control attach
hermes-control snapshot
hermes-control logs
hermes-control stop
```

`snapshot` performs read-only status collection for Hermes, Herdr, and the
dotfiles repository. Mutating Herdr actions remain governed by `AGENTS.md` and
the primary coordination contract in `../agent/AGENTS.md`.

The optional user service starts the tmux session at login:

```bash
systemctl --user enable --now hermes-control.service
```

Configuration can be overridden without editing the launcher:

```bash
HERMES_CONTROL_MODEL=gpt-5.6-sol hermes-control restart
HERMES_CONTROL_TOOLSETS=terminal,todo,memory,session_search hermes-control restart
```
