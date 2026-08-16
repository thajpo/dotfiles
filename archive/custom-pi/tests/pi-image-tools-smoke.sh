#!/usr/bin/env bash
# Disposable compatibility smoke test for pi-image-tools against the pinned Pi.
set -euo pipefail

root=$(CDPATH= cd -- "$(dirname -- "${BASH_SOURCE[0]}")/.." && pwd)
temporary=$(mktemp -d)
cleanup() { rm -rf "$temporary"; }
trap cleanup EXIT
npm install --prefix "$temporary" --no-save --no-package-lock --no-audit --no-fund \
  --legacy-peer-deps \
  "@earendil-works/pi-coding-agent@$(cat "$root/pi/PI_VERSION")" \
  "pi-image-tools@1.4.0" >/dev/null
node - "$temporary/node_modules/pi-image-tools/package.json" <<'NODE'
const fs = require("fs");
const pkg = JSON.parse(fs.readFileSync(process.argv[2], "utf8"));
if (pkg.version !== "1.4.0" || pkg.main !== "./index.ts" || !pkg.pi?.extensions?.includes("./index.ts")) {
  throw new Error("unexpected pi-image-tools package contract");
}
const source = fs.readFileSync(require("path").join(require("path").dirname(process.argv[2]), "src/index.ts"), "utf8");
for (const marker of ["pendingImages", "images: [...(event.images ?? []), ...imagesToAttach]", "readClipboardImage"]) {
  if (!source.includes(marker)) throw new Error(`missing native attachment marker: ${marker}`);
}
NODE
printf 'pi-image-tools 1.4.0 native attachment smoke test passed against Pi %s\n' "$(cat "$root/pi/PI_VERSION")"
