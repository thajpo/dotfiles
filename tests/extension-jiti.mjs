import { createJiti } from "../pi/npm/node_modules/jiti/lib/jiti.mjs";

const runtimeAliases = {
  "typebox/compile": new URL("../pi/npm/node_modules/typebox/build/compile/index.mjs", import.meta.url).pathname,
  typebox: new URL("../pi/npm/node_modules/typebox/build/index.mjs", import.meta.url).pathname,
};

/**
 * Load repository extensions with the same project-owned runtime dependency
 * generation used by the installer, rather than relying on ambient root
 * node_modules state.
 */
export function createExtensionJiti(baseUrl, aliases = {}) {
  return createJiti(baseUrl, { alias: { ...runtimeAliases, ...aliases } });
}
