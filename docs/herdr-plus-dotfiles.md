# Herdr Plus: Dotfiles project

This repository tracks one focused
[`cloudmanic/herdr-plus`](https://github.com/cloudmanic/herdr-plus) project
template. Opening **Dotfiles** creates a workspace rooted at `~/dotfiles` with:

1. a `codex` tab that starts `codex`, suitable for the project-owner session;
2. a plain `shell` tab for review, Git, and operator commands.

There is deliberately no `worktrees/` auto-layout. `herdr-spawn` and `reviewr`
already react to worktree creation, and a third layout owner could create
duplicate panes or agents. The project template itself adds no quick actions or
global settings. The separately owned action bindings for Herdr Plus and
Annotate are documented in `docs/herdr-plugin-wiring.md`.

## Reviewed compatibility

The template and workflow were reviewed on 2026-08-30 against:

- Herdr `0.8.2`, using read-only help/version inspection of the `plugin install`,
  `plugin config-dir`, `plugin action`, `plugin list`, and `config check` CLI
  contracts;
- herdr-plus `main` at `0ede9c763e0feb7800b6d2e3a7401f9198684caf`
  (manifest version `0.1.24`, three commits after tag `v0.1.24`).

The exact Herdr Plus and full Annotate plugin assumptions are tracked together
in `herdr/plugin-versions.toml`.

That manifest requires Herdr `>=0.7.0`. The helper intentionally uses the
upstream-recommended unpinned install command, so a later run follows the then
current upstream default branch. For strict reproducibility, inspect the new
upstream revision first and install with Herdr's supported `--ref <tag>` option
manually; then run the helper's `sync` subcommand.

## Install and sync

Run this from the merged main worktree, not from a worker worktree:

```bash
export HERDR_ENV=1
test "$HERDR_ENV" = 1
~/dotfiles/bin/herdr-plus-sync install
```

The `install` subcommand runs exactly:

```bash
herdr plugin install cloudmanic/herdr-plus
```

It then asks Herdr for the installation's durable managed config directory:

```bash
herdr plugin config-dir cloudmanic.herdr-plus
```

and atomically syncs the tracked template to
`<config-dir>/projects/dotfiles.toml`. Re-running `install` upgrades/reinstalls
the plugin as upstream documents; re-running `sync` only updates the template.
Both preserve every other project, quick action, plugin setting, and file in the
managed config directory. If a different, unmarked `projects/dotfiles.toml`
already exists, the helper stops instead of overwriting it: rename or back up
that file explicitly, review the two versions, then retry.

## Verify

Keep Herdr running, then verify registration, actions, and the exact synced
file:

```bash
export HERDR_ENV=1
~/dotfiles/bin/herdr-plus-sync check
herdr plugin list --plugin cloudmanic.herdr-plus
herdr plugin action list --plugin cloudmanic.herdr-plus
herdr plugin action invoke ping --plugin cloudmanic.herdr-plus
herdr plugin log list --plugin cloudmanic.herdr-plus --limit 10
```

`check` is read-only and fails if the tracked and managed project templates
differ. The `ping` action exercises the plugin-to-Herdr connection; its result is
captured in the plugin log.

## Open the project

From inside Herdr, open the plugin action menu, select **Herdr Plus: Projects**,
then select **Dotfiles** and press Enter. The same supported action can be
invoked from a Herdr pane shell without adding a keybinding:

```bash
HERDR_ENV=1 herdr plugin action invoke projects --plugin cloudmanic.herdr-plus
```

After applying the reviewed shared wiring, `prefix+up` opens Projects and
`prefix+down` opens Quick Actions.

Use normal Enter, not the picker's worktree shortcut (`ctrl+g`): this project is
for the main `~/dotfiles` checkout, while workers continue to be created through
`~/dotfiles/bin/spawn` under the existing coordination workflow.

## Roll back

First discover and record the managed directory, then remove only the tracked
project file:

```bash
export HERDR_ENV=1
config_dir=$(herdr plugin config-dir cloudmanic.herdr-plus)
cp "$config_dir/projects/dotfiles.toml" \
  "$config_dir/projects/dotfiles.toml.rollback-backup"
rm -- "$config_dir/projects/dotfiles.toml"
```

This leaves all unrelated herdr-plus configuration intact. To remove the plugin
registration and clone as well:

```bash
herdr plugin uninstall cloudmanic.herdr-plus
```

Herdr preserves the managed config directory across uninstall and upgrade, so
the explicit project-file removal above is what prevents **Dotfiles** from
returning after a reinstall. Restore the backup to the original filename and
run `~/dotfiles/bin/herdr-plus-sync check` to re-enable the tracked project.
