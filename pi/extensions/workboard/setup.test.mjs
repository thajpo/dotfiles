import assert from "node:assert/strict";
import { mkdtemp, readFile, stat, writeFile } from "node:fs/promises";
import os from "node:os";
import { join, resolve } from "node:path";
import test from "node:test";
import { ensureSideAgentSetup, renderFinishScript, renderStartScript } from "./setup.ts";

test("lifecycle templates quote unusual integration branch names", () => {
  const branch = "release/'safe";
  assert.match(renderStartScript(branch), /MAIN_BRANCH='release\/'"'"'safe'/);
  assert.match(renderFinishScript(branch), /MAIN_BRANCH='release\/'"'"'safe'/);
});

test("setup creates local lifecycle files with restrictive modes and is idempotent", async () => {
  const root = await mkdtemp(join(os.tmpdir(), "workboard-setup-"));
  const source = join(root, "source-skill.md");
  await writeFile(source, "reference\n", "utf8");
  const projectPath = async (relativePath) => resolve(root, relativePath);

  const first = await ensureSideAgentSetup("main", projectPath, source);
  assert.deepEqual(first.created.sort(), [
    ".pi/side-agent-finish.sh",
    ".pi/side-agent-skills/finish/SKILL.md",
    ".pi/side-agent-start.sh",
    ".pi/side-agents/agent-start~/SKILL.md",
  ].sort());
  assert.equal((await stat(join(root, ".pi/side-agent-start.sh"))).mode & 0o777, 0o700);
  assert.equal((await stat(join(root, ".pi/side-agent-finish.sh"))).mode & 0o777, 0o700);
  assert.equal((await stat(join(root, ".pi/side-agent-skills/finish/SKILL.md"))).mode & 0o777, 0o600);
  assert.match(await readFile(join(root, ".pi/side-agent-skills/finish/SKILL.md"), "utf8"), /LGTM, merge/);

  const second = await ensureSideAgentSetup("main", projectPath, source);
  assert.deepEqual(second.created, []);
});

test("setup preserves an existing user-owned lifecycle file", async () => {
  const root = await mkdtemp(join(os.tmpdir(), "workboard-setup-existing-"));
  const start = join(root, ".pi/side-agent-start.sh");
  const projectPath = async (relativePath) => resolve(root, relativePath);
  await ensureSideAgentSetup("main", projectPath);
  await writeFile(start, "custom\n", "utf8");

  await ensureSideAgentSetup("main", projectPath);
  assert.equal(await readFile(start, "utf8"), "custom\n");
});
