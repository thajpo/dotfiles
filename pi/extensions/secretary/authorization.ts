export type GitAuthorization = "git-commit" | "git-push" | "git-commit-and-push";
export type GitCleanupAuthorization = "git-cleanup";

/**
 * Recognize an instruction to create a new full/headful workstream without
 * treating a question, proposal, or negation as authorization.  This is kept
 * separate from the existing-workstream, review, landing, Git-write, and
 * cleanup recognizers because those actions have different target/approval
 * rules.
 */
export function promotionWasAuthorized(value: string): boolean {
  const text = value.toLowerCase().replace(/[\u2010-\u2015]/g, "-");
  const target = String.raw`(?:new\s+feature|full\s+(?:agents?|workstreams?|workers?)|headful\s+(?:agents?|workstreams?|workers?)|(?:agents?|workstreams?)(?:\s*/\s*(?:agents?|workstreams?))?)`;
  const action = String.raw`(?:create|spawn|start|launch|promote|spin(?:\s+up)?)`;

  // Questions and discussion are not a grant of authority, even when they
  // contain an otherwise imperative-looking phrase.
  if (text.includes("?") ||
      new RegExp(String.raw`\b(?:can|could|should|would|shall|may)\s+(?:we|you|i|it)\b[\s\S]{0,120}\b${action}\b[\s\S]{0,120}\b${target}\b`).test(text) ||
      /\b(?:how\s+about|is\s+it\s+(?:okay|all\s+right)|do\s+you\s+want|would\s+it\s+make\s+sense|what\s+happens\s+if|what\s+if|whether|discuss(?:ing|ion)?|propos(?:e|es|ed|ing|al)|suggest(?:s|ed|ing|ion)?|consider(?:s|ed|ing|ation)?|explor(?:e|es|ed|ing|ation)?)\b/.test(text) ||
      new RegExp(String.raw`\b(?:(?:we|i)\s+(?:should|could|might|may|can|would)|you\s+(?:should|could|might|would))\b[\s\S]{0,100}\b${target}\b`).test(text) ||
      new RegExp(String.raw`\b(?:i\s+think|maybe|perhaps)\b[\s\S]{0,100}\b${target}\b`).test(text)) {
    return false;
  }

  // A denial wins over an imperative in the same turn.  Keep the window
  // bounded so an unrelated earlier sentence cannot cancel a later command.
  if (new RegExp(String.raw`\b(?:don't|do not|never|no|not|without|avoid|refus(?:e|es|ed|ing)|won't|will not|can't|cannot|shouldn't|should not)\b[\s\S]{0,80}\b${action}\b[\s\S]{0,120}\b${target}\b`).test(text) ||
      new RegExp(String.raw`\b${action}\b[\s\S]{0,80}\b(?:don't|do not|never|no|not|without|avoid|can't|cannot|shouldn't|should not)\b[\s\S]{0,80}\b${target}\b`).test(text)) {
    return false;
  }

  // The action must be expressed as an instruction, not merely as a noun in
  // a status/proposal sentence.  Sentence-start imperatives cover "create …"
  // and "spawn …"; the explicit prefixes cover ordinary natural language.
  return new RegExp(String.raw`(?:^|[.!?,;]\s*|\b(?:please|go\s+ahead\s+and|you\s+can|you\s+may|authorize\s+you\s+to)\s+)${action}\b[\s\S]{0,120}\b${target}\b`).test(text) ||
    new RegExp(String.raw`\b${action}\b\s+(?:\d+\s+|two\s+|three\s+|several\s+|multiple\s+)?${target}\b`).test(text);
}

// Require an imperative or affirmative authorization such as "you can push now"
// or "please commit and push"; merely discussing Git must not authorize it.
export function gitCleanupApplyWasAuthorized(value: string): boolean {
  const target = /\b(?:git|branch(?:es)?|worktrees?|work-trees?|artifacts?|benchmark|side-agent)\b/i;
  const action = /\b(?:clean(?:up|ing|ed)?|remove|delete|rename|prune|apply|execute|perform)\b/i;
  const explicit = /(?:^|[.!?,;]\s*)(?:please\s+|go ahead and\s+|you can\s+|you may\s+|authorize\s+|approved to\s+)?(?:apply|execute|perform|clean(?:up|ing|ed)?|clean-up|remove|delete|rename|prune)\b[\s\S]{0,120}\b(?:git|branch(?:es)?|worktrees?|work-trees?|artifacts?|benchmark|side-agent)\b/i;
  const denied = /\b(?:don't|do not|never|no|not|without|avoid|can't|cannot|shouldn't|should not)\b[\s\S]{0,80}\b(?:clean(?:up)?|clean up|remove|delete|rename|prune|apply|execute|perform)\b/i;
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
