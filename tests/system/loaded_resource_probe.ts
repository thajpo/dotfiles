import { createHash } from "node:crypto";
import { readFileSync, statSync } from "node:fs";

function digest(path: string): string {
  const info = statSync(path);
  if (!info.isFile()) throw new Error(`resource is not a file: ${path}`);
  return `sha256:${createHash("sha256").update(readFileSync(path)).digest("hex")}`;
}

const paths = process.argv.slice(2);
if (!paths.length) throw new Error("at least one resource path is required");
process.stdout.write(`${JSON.stringify({ resources: paths.map((path) => ({ path, digest: digest(path) })) })}\n`);
