# Machine profiles

Machine profiles contain non-secret values that vary by platform. The profile
is selected from the operating system and architecture, then installed as
`~/.config/dotfiles/machine.env` for platform-specific tooling to read.

Profiles are selected automatically for Apple Silicon macOS and Linux x86_64
by `scripts/agent-workflow-install.sh`. Override automatic selection when
needed:

```bash
DOTFILES_MACHINE=macos-arm64 ~/dotfiles/scripts/agent-workflow-install.sh --skills-only
DOTFILES_MACHINE=linux-x86_64 ~/dotfiles/scripts/agent-workflow-install.sh --skills-only
```

Do not put credentials, tokens, session state, or other secrets in a profile.
