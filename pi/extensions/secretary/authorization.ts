export type GitAuthorization = "git-commit" | "git-push" | "git-commit-and-push";
export type GitCleanupAuthorization = "git-cleanup";

// Require an imperative or affirmative authorization such as "you can push now"
// or "please commit and push"; merely discussing Git must not authorize it.
export function gitCleanupApplyWasAuthorized(value: string): boolean {
  const target = /\b(?:git|branch(?:es)?|worktrees?|work-trees?|artifacts?|benchmark|side-agent)\b/i;
  const action = /\b(?:clean(?:up|ing|ed)?|remove|delete|rename|prune|apply|execute|perform)\b/i;
  const explicit = /(?:^|[.!?,;]\s*)(?:please\s+|go ahead and\s+|you can\s+|you may\s+|authorize\s+|approved to\s+)?(?:apply|execute|perform|clean(?:up|ing|ed)?|clean-up|remove|delete|rename|prune)\b[\s\S]{0,120}\b(?:git|branch(?:es)?|worktrees?|work-trees?|artifacts?|benchmark|side-agent)\b/i;
  const denied = /\b(?:don't|do not|never|not|without|avoid|can't|cannot|shouldn't|should not)\b[\s\S]{0,80}\b(?:clean|remove|delete|rename|prune|apply|execute|perform)\b/i;
  return target.test(value) && action.test(value) && explicit.test(value) && !denied.test(value);
}

export function gitWriteWasAuthorized(value: string): GitAuthorization[] {
  const form = (verb: "commit" | "push") => `${verb}(?:ted|ting|es|ed)?`;
  const denied = (verb: "commit" | "push") => new RegExp(
    `(?:^|[.!?,;]\\s*)[^.!?,;]{0,50}\\b(?:don't|do not|never|not|without|avoid|can't|cannot|shouldn't|should not)\\b[^.!?,;]{0,50}\\b${form(verb)}\\b`,
  ).test(value) || new RegExp(
    `(?:^|[.!?,;]\\s*)[^.!?,;]{0,50}\\b${form(verb)}\\b[^.!?,;]{0,40}\\b(?:not needed|unnecessary|not required|not now|without)\\b`,
  ).test(value);
  const authorized = (verb: "commit" | "push") => {
    const action = form(verb);
    return new RegExp(`(?:^|[.!?,;]\\s*|\\b(?:but|then)\\s+)${action}\\b`).test(value) ||
      new RegExp(`\\b(?:please|go ahead and|you can|you may|you should|we can|we should|authorize you to|authorized to|permission to|okay to|ok to|can you)\\s+${action}\\b`).test(value);
  };
  const joint = new RegExp(`\\b${form("commit")}\\b[\\s\\S]{0,30}\\b(?:and|then)\\b[\\s\\S]{0,30}\\b${form("push")}\\b`).test(value);
  const commit = (authorized("commit") || joint) && !denied("commit");
  const push = (authorized("push") || joint) && !denied("push");
  if (commit && push) return ["git-commit-and-push"];
  if (commit) return ["git-commit"];
  if (push) return ["git-push"];
  return [];
}
