import assert from "node:assert/strict";
import fs from "node:fs";
import os from "node:os";
import path from "node:path";
import { spawnSync } from "node:child_process";
import { pathToFileURL } from "node:url";

const root = path.resolve(import.meta.dirname, "..");
const modulePath = path.join(root, "pi/npm/node_modules/pi-subagents/src/runs/shared/worktree.ts");
const { createWorktrees, diffWorktrees, cleanupWorktrees } = await import(pathToFileURL(modulePath));
const temp = fs.mkdtempSync(path.join(os.tmpdir(), "pi-candidates-test-"));
const repo = path.join(temp, "repo");
const candidates = path.join(temp, "worktrees");
const diffs = path.join(temp, "diffs");
fs.mkdirSync(repo);
function git(cwd, ...args) {
  const result = spawnSync("git", ["-C", cwd, ...args], { encoding: "utf8" });
  if (result.status !== 0) throw new Error(result.stderr || `git ${args.join(" ")} failed`);
  return result.stdout.trim();
}
git(repo, "init", "-b", "feature");
git(repo, "config", "user.name", "Pi Test");
git(repo, "config", "user.email", "pi-test@example.invalid");
fs.writeFileSync(path.join(repo, "file.txt"), "base\n");
git(repo, "add", "file.txt");
git(repo, "commit", "-m", "base");
const route = path.join(temp, "parent-task.json");
fs.writeFileSync(route, "{}");
process.env.PI_TASK_ROUTE_FILE = route;
process.env.PI_TASK_MODE = "trusted-live";
process.env.PI_SUBAGENTS_WORKTREE_DIR = candidates;

const setup = createWorktrees(repo, "run", 2, { agents: ["candidate-a", "candidate-b"] });
try {
  assert.equal(setup.worktrees.length, 2);
  assert.notEqual(setup.worktrees[0].path, setup.worktrees[1].path);
  assert.match(setup.worktrees[0].branch, /^pi\/parent-task\/candidate-1$/);
  assert.match(setup.worktrees[1].branch, /^pi\/parent-task\/candidate-2$/);
  fs.writeFileSync(path.join(setup.worktrees[0].path, "candidate.txt"), "uncommitted\n");
  assert.throws(() => diffWorktrees(setup, ["a", "b"], diffs), /must commit all changes/);
  git(setup.worktrees[0].path, "add", "candidate.txt");
  git(setup.worktrees[0].path, "commit", "-m", "candidate a");
  fs.writeFileSync(path.join(setup.worktrees[1].path, "candidate.txt"), "other\n");
  git(setup.worktrees[1].path, "add", "candidate.txt");
  git(setup.worktrees[1].path, "commit", "-m", "candidate b");
  const captured = diffWorktrees(setup, ["a", "b"], diffs);
  assert.equal(captured.length, 2);
  assert.ok(captured.every((entry) => entry.filesChanged === 1));
  cleanupWorktrees(setup);
  assert.ok(fs.existsSync(setup.worktrees[0].path), "task cleanup must preserve candidate worktrees");
  assert.ok(git(repo, "show-ref", "--verify", `refs/heads/${setup.worktrees[0].branch}`));
} finally {
  for (const worktree of setup.worktrees) {
    spawnSync("git", ["-C", repo, "worktree", "remove", "--force", worktree.path]);
    spawnSync("git", ["-C", repo, "branch", "-D", worktree.branch]);
  }
  fs.rmSync(temp, { recursive: true, force: true });
}
console.log("PASS candidate worktrees");
