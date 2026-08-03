# Pi TODO

## Security hardening: outside-project-root context mounts

The control-plane route now has a deliberately narrow, read-only aperture for
Pi's pinned `docs/` and `examples/` directories outside the project root.
Before treating this as a durable security boundary:

- run the real Docker create/reuse integration tests against the activated
  patched `pi-sandbox` package;
- verify route provenance, exact source identity, package version/hash, and
  TOCTOU behavior immediately before every create/reuse;
- decide whether a sanitized immutable mirror is safer than direct host binds;
- test recursive symlink, permission, ownership, duplicate, stale-container,
  and unexpected-mount cases;
- prove ordinary trusted and isolated projects cannot receive these resources
  and that sessions, credentials, host commands, sockets, and the Docker
  socket remain unavailable;
- activate only through reviewed `pi-host` installation and record rollback
  evidence.
