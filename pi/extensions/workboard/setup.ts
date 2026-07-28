import { randomUUID } from "node:crypto";
import { constants as fsConstants, promises as fs } from "node:fs";
import { dirname, join } from "node:path";

export interface SetupResult {
  mainBranch: string;
  created: string[];
}

export type ProjectPathResolver = (projectRelativePath: string) => Promise<string>;

function shellQuote(value: string): string {
  return `'${value.replace(/'/g, `'"'"'`)}'`;
}

async function exists(path: string): Promise<boolean> {
  try {
    await fs.access(path, fsConstants.F_OK);
    return true;
  } catch (error: unknown) {
    if ((error as NodeJS.ErrnoException).code === "ENOENT") return false;
    throw error;
  }
}

async function writeNewFile(path: string, content: string, mode: number): Promise<boolean> {
  if (await exists(path)) return false;
  await fs.mkdir(dirname(path), { recursive: true });
  const temp = `${path}.${process.pid}.${randomUUID()}.tmp`;
  try {
    await fs.writeFile(temp, content, { encoding: "utf8", mode, flag: "wx" });
    try {
      await fs.link(temp, path);
    } catch (error: unknown) {
      if ((error as NodeJS.ErrnoException).code === "EEXIST") return false;
      throw error;
    }
    await fs.chmod(path, mode);
    return true;
  } finally {
    await fs.rm(temp, { force: true });
  }
}

export function renderStartScript(mainBranch: string): string {
  return `#!/usr/bin/env bash
set -euo pipefail

PARENT_ROOT="\${1:-}"
WORKTREE="\${2:-$(pwd)}"
AGENT_ID="\${3:-unknown}"
MAIN_BRANCH=${shellQuote(mainBranch)}

BRANCH="$(git -C "$WORKTREE" rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [[ "$BRANCH" == "HEAD" ]]; then
  BRANCH=""
fi
if [[ -z "$BRANCH" ]]; then
  echo "[side-agent-start] Could not determine current branch in $WORKTREE."
  exit 1
fi

echo "[side-agent-start] agent=$AGENT_ID branch=$BRANCH main=$MAIN_BRANCH"

if [[ "$BRANCH" == "$MAIN_BRANCH" ]]; then
  echo "[side-agent-start] ERROR: child worktree is on $MAIN_BRANCH; expected a dedicated agent branch."
  exit 1
fi

echo "[side-agent-start] Worktree based on parent HEAD ($(git -C "$WORKTREE" rev-parse --short HEAD))."

# Optional project bootstrap hook. Create .pi/side-agent-bootstrap.sh to use it.
if [[ -x "$WORKTREE/.pi/side-agent-bootstrap.sh" ]]; then
  "$WORKTREE/.pi/side-agent-bootstrap.sh"
fi
`;
}

export function renderFinishScript(mainBranch: string): string {
  return `#!/usr/bin/env bash
set -euo pipefail

PARENT_ROOT="\${PI_SIDE_PARENT_REPO:-\${1:-}}"
AGENT_ID="\${PI_SIDE_AGENT_ID:-\${2:-unknown}}"
MAIN_BRANCH=${shellQuote(mainBranch)}
BRANCH="$(git rev-parse --abbrev-ref HEAD 2>/dev/null || true)"
if [[ "$BRANCH" == "HEAD" ]]; then
  BRANCH=""
fi

if [[ -z "$PARENT_ROOT" ]]; then
  echo "[side-agent-finish] Missing parent checkout path."
  echo "Usage: PI_SIDE_PARENT_REPO=/path/to/parent .pi/side-agent-finish.sh"
  exit 1
fi
if [[ -z "$BRANCH" ]]; then
  echo "[side-agent-finish] Could not determine current branch."
  exit 1
fi
if [[ "$BRANCH" == "$MAIN_BRANCH" ]]; then
  echo "[side-agent-finish] Refusing to finish from integration branch $MAIN_BRANCH."
  exit 1
fi

LOCK_DIR="$PARENT_ROOT/.pi/side-agents"
LOCK_FILE="$LOCK_DIR/merge.lock"
mkdir -p "$LOCK_DIR"
MERGE_LOCK_TIMEOUT=120

iso_now() {
  date -u +"%Y-%m-%dT%H:%M:%SZ"
}

acquire_lock() {
  local payload started elapsed holder_pid
  payload="{\"agentId\":\"$AGENT_ID\",\"pid\":$$,\"acquiredAt\":\"$(iso_now)\"}"
  started=$(date +%s)
  while true; do
    if ( set -o noclobber; printf '%s\\n' "$payload" > "$LOCK_FILE" ) 2>/dev/null; then
      return 0
    fi
    elapsed=$(( $(date +%s) - started ))
    if [[ -f "$LOCK_FILE" ]]; then
      holder_pid="$(grep -o '"pid":[0-9]*' "$LOCK_FILE" 2>/dev/null | head -n 1 | grep -o '[0-9]*' || true)"
      if [[ -n "$holder_pid" ]] && ! kill -0 "$holder_pid" 2>/dev/null; then
        echo "[side-agent-finish] Removing stale merge lock (pid $holder_pid no longer running)."
        rm -f "$LOCK_FILE"
        continue
      fi
    fi
    if [[ "$elapsed" -ge "$MERGE_LOCK_TIMEOUT" ]]; then
      echo "[side-agent-finish] Timed out after \${MERGE_LOCK_TIMEOUT}s waiting for merge lock."
      echo "[side-agent-finish] Inspect: $LOCK_FILE"
      exit 3
    fi
    sleep 1
  done
}

release_lock() {
  rm -f "$LOCK_FILE" || true
}
trap 'release_lock' EXIT

while true; do
  echo "[side-agent-finish] Reconciling child branch: git rebase $MAIN_BRANCH"
  if ! git rebase "$MAIN_BRANCH"; then
    echo "[side-agent-finish] Conflict while rebasing $BRANCH onto $MAIN_BRANCH."
    echo "Resolve conflicts, continue the rebase, then rerun .pi/side-agent-finish.sh"
    exit 2
  fi

  acquire_lock
  set +e
  (
    cd "$PARENT_ROOT" || exit 1
    git checkout "$MAIN_BRANCH" >/dev/null 2>&1 || exit 1
    git merge --ff-only "$BRANCH"
  )
  merge_status=$?
  set -e
  release_lock

  if [[ "$merge_status" -eq 0 ]]; then
    echo "[side-agent-finish] Success: fast-forwarded $MAIN_BRANCH to include $BRANCH."
    rm -f "$(pwd)/.pi/active.lock" || true
    exit 0
  fi

  echo "[side-agent-finish] Parent fast-forward failed; rebasing again because $MAIN_BRANCH may have moved."
  sleep 1
done
`;
}

export function renderFinishSkill(mainBranch: string): string {
  return `---
name: finish
description: Rebase and fast-forward the approved branch only after the user explicitly says "LGTM, merge"
---

# Parallel-agent finish workflow

Do not merge, push, publish, or approve by yourself.

Only when the user sends the explicit instruction **LGTM, merge**:

1. Confirm that exact approval applies to the current implementation.
2. Run:

\`\`\`bash
PI_SIDE_PARENT_REPO="$PI_SIDE_PARENT_REPO" .pi/side-agent-finish.sh
\`\`\`

3. If rebasing onto ${mainBranch} conflicts, remain in this worktree, resolve the conflict, continue the rebase, and rerun the script.
4. If the parent checkout is dirty or safe integration is uncertain, stop and escalate instead of forcing anything.
5. After success, report the landed commits and suggest \`/quit\`.
`;
}

export async function ensureSideAgentSetup(
  mainBranch: string,
  resolveProjectPath: ProjectPathResolver,
  referenceSkillSource?: string,
): Promise<SetupResult> {
  if (!mainBranch.trim()) throw new Error("Cannot initialize side agents without an integration branch.");

  const files: Array<{ relativePath: string; content: string; mode: number }> = [
    { relativePath: ".pi/side-agent-start.sh", content: renderStartScript(mainBranch), mode: 0o700 },
    { relativePath: ".pi/side-agent-finish.sh", content: renderFinishScript(mainBranch), mode: 0o700 },
    { relativePath: ".pi/side-agent-skills/finish/SKILL.md", content: renderFinishSkill(mainBranch), mode: 0o600 },
  ];
  const created: string[] = [];
  for (const file of files) {
    const path = await resolveProjectPath(file.relativePath);
    if (await writeNewFile(path, file.content, file.mode)) created.push(file.relativePath);
  }

  if (referenceSkillSource && (await exists(referenceSkillSource))) {
    const relativePath = ".pi/side-agents/agent-start~/SKILL.md";
    const target = await resolveProjectPath(relativePath);
    const content = await fs.readFile(referenceSkillSource, "utf8");
    if (await writeNewFile(target, content, 0o600)) created.push(relativePath);
  }

  return { mainBranch, created };
}
