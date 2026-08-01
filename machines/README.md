# Machine profiles

Machine profiles contain non-secret values that vary by platform, including
the project paths used by the Pi personal workspace. They are selected by
`install.sh` from the operating system and architecture, then installed as
`~/.config/dotfiles/machine.env` for the launchers to read.

Profiles are selected automatically for Apple Silicon macOS and Linux x86_64.
Override automatic selection when needed:

```bash
DOTFILES_MACHINE=macos-arm64 ./install.sh
DOTFILES_MACHINE=linux-x86_64 ./install.sh
```

The Linux profile assumes the host project root is `~/Projects`; adjust or add
an explicit profile before installing on a different layout.

Do not put credentials, tokens, session state, or other secrets in a profile.
