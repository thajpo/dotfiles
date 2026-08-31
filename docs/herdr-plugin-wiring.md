# Annotate and Herdr Plus wiring

The plugins are installed globally; this repository owns only their reviewed
Herdr action bindings and the Dotfiles project template. The reviewed versions
are recorded in `herdr/plugin-versions.toml`:

| Source | Plugin ID | Version | Commit |
| --- | --- | --- | --- |
| `cloudmanic/herdr-plus` | `cloudmanic.herdr-plus` | `0.1.24` | `0ede9c763e0feb7800b6d2e3a7401f9198684caf` |
| `plannotator/herdr-annotate` (full) | `annotate` | `0.3.0` | `ba4903b28fbb77dd0a4bc55a4a7ba3c1ef0913ea` |

Both manifests were reviewed against Herdr `0.8.2`. Herdr Plus requires Herdr
`>=0.7.0`; Annotate requires Herdr `>=0.8.0` and Bun. The wiring does not install,
upgrade, disable, or uninstall either plugin and does not touch either plugin's
managed config directory.

## Bindings and collision decision

The tracked `herdr/plugin-action-keys.toml` block supplies every requested
action:

| Key | Action |
| --- | --- |
| `prefix+a` | `annotate.capture` |
| `prefix+shift+a` | `annotate.copy-context` |
| `prefix+m` | `annotate.manage` |
| `prefix+d` | `annotate.open` |
| `prefix+shift+l` | `annotate.last` |
| `prefix+up` | `cloudmanic.herdr-plus.projects` |
| `prefix+down` | `cloudmanic.herdr-plus.quick-actions` |

Annotate upstream suggests `prefix+o` for `annotate.open`, but Herdr `0.8.2`
uses `prefix+o` for `open_notification_target`. The tracked wiring preserves that
notification behavior and uses `prefix+d` for document review. `prefix+shift+l`
mnemonically selects the agent's last reply. Herdr Plus's `prefix+up/down` pair
is its documented example and is free in the inspected defaults and active
dotfiles config. All seven keys also avoid the existing Neovim and Reviewr
bindings (`prefix+shift+e/f/v`).

## Apply without clobbering config

From the merged main worktree, first back up the active config, then apply the
separately marker-owned block:

```bash
export HERDR_ENV=1
test "$HERDR_ENV" = 1
backup=$(mktemp "$HOME/.config/herdr/config.toml.before-plugin-wiring.XXXXXX")
cp ~/.config/herdr/config.toml "$backup"
printf 'backup: %s\n' "$backup"
~/dotfiles/bin/herdr-plugin-wiring apply
herdr config check
herdr server reload-config
```

`herdr-plugin-wiring` honors `HERDR_CONFIG_PATH`, then `XDG_CONFIG_HOME`, then
`~/.config/herdr/config.toml`. It validates the existing TOML, rejects unmatched
markers, duplicate action keys, default-key collisions, active scalar-binding
collisions, and custom-command collisions before an atomic same-directory
write. If the config path is a symlink it updates the resolved target without
replacing the symlink. A second identical apply does not rewrite the file.

The helper owns only the block bounded by:

```text
# >>> dotfiles Annotate and Herdr Plus keys >>>
# <<< dotfiles Annotate and Herdr Plus keys <<<
```

Existing blocks—including the marker-owned Neovim/Reviewr bindings—and all
other configuration remain outside that boundary and are preserved.

Sync the separate Herdr Plus project template after the plugin install:

```bash
HERDR_ENV=1 ~/dotfiles/bin/herdr-plus-sync sync
```

This keeps worktree auto-layout disabled; no tracked `worktrees/` directory is
created.

## Verify

```bash
export HERDR_ENV=1
HERDR_ENV=1 herdr plugin list --json | jq -e '
  any(.result.plugins[];
    .plugin_id == "cloudmanic.herdr-plus" and
    .version == "0.1.24" and
    .source.resolved_commit == "0ede9c763e0feb7800b6d2e3a7401f9198684caf") and
  any(.result.plugins[];
    .plugin_id == "annotate" and
    .version == "0.3.0" and
    .source.resolved_commit == "ba4903b28fbb77dd0a4bc55a4a7ba3c1ef0913ea")
'
~/dotfiles/bin/herdr-plugin-wiring check
~/dotfiles/bin/herdr-plus-sync check
herdr config check
herdr plugin action list --plugin annotate
herdr plugin action list --plugin cloudmanic.herdr-plus
herdr plugin action invoke manage --plugin annotate
herdr plugin action invoke ping --plugin cloudmanic.herdr-plus
herdr plugin log list --plugin annotate --limit 10
herdr plugin log list --plugin cloudmanic.herdr-plus --limit 10
```

After reload, exercise each key from a Herdr pane. `annotate.capture` requires a
terminal selection; `annotate.open` and `annotate.last` are available only in the
full Annotate install on Linux/macOS. Herdr Plus Projects should show the tracked
**Dotfiles** template after its separate sync.

## Roll back

Remove only this integration's action block and reload:

```bash
export HERDR_ENV=1
~/dotfiles/bin/herdr-plugin-wiring remove
herdr config check
herdr server reload-config
```

`remove` is idempotent and leaves the existing Neovim/Reviewr key block, all
unrelated Herdr settings, both plugin installations, and every managed plugin
config file intact. Restore the pre-apply backup only if you intend to roll back
other manual config changes made after it was captured.

To remove the Dotfiles project template separately, follow the file-scoped
rollback in `docs/herdr-plus-dotfiles.md`. Plugin uninstall is intentionally not
part of this wiring rollback.
