# Machine profiles

Machine profiles contain non-secret values that vary by platform, including
the project paths used by the Pi personal workspace. They are selected by
`install.sh` from the operating system and architecture, then installed as
`~/.config/dotfiles/machine.env` for the launchers to read.

Override automatic selection when needed:

```bash
DOTFILES_MACHINE=macos-arm64 ./install.sh
```

Do not put credentials, tokens, session state, or other secrets in a profile.
Linux profiles will be added separately; until then, Linux keeps the existing
launcher defaults.
