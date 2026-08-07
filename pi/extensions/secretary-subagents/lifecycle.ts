export type ActionAuthorization = { ids: string[]; agents: string[] };
export type LifecycleAction = "interrupt" | "stop" | "resume" | "steer";
export type AuthorizationAction = "list" | "doctor" | "status" | LifecycleAction;
export type LifecycleParams = { id?: unknown; runId?: unknown };
export type LifecycleJob = { status?: string; agents?: string[] };
export type LifecycleState = {
  asyncJobs?: ReadonlyMap<string, LifecycleJob>;
  fleetJobs?: ReadonlyMap<string, LifecycleJob>;
};

const TARGETED_ACTIONS = new Set<LifecycleAction>(["interrupt", "stop", "resume", "steer"]);
const TERMINAL_JOB_STATES = new Set(["complete", "completed", "failed", "paused", "stopped"]);
const RESUMABLE_JOB_STATES = new Set(["complete", "completed", "failed", "paused"]);

function actionWasDenied(value: string, action: AuthorizationAction): boolean {
  const words = action === "list"
    ? "list|show|enumerate"
    : action === "doctor"
      ? "doctor|diagnos(?:e|is)|health"
      : action === "status"
        ? "status|progress|state"
        : action === "interrupt"
          ? "interrupt|pause"
          : action === "stop"
            ? "stop|cancel|terminate"
            : action === "resume"
              ? "resume|continue"
              : "steer|redirect|guide";
  return new RegExp(`(?:^|[.!?,;]\\s*)[^.!?,;]{0,50}\\b(?:don't|do not|never|not|without|avoid|can't|cannot|shouldn't|should not)\\b[^.!?,;]{0,50}\\b(?:${words})\\b`).test(value) ||
    new RegExp(`(?:^|[.!?,;]\\s*)[^.!?,;]{0,50}\\b(?:${words})\\b[^.!?,;]{0,40}\\b(?:not needed|unnecessary|not required)\\b`).test(value);
}

export function actionWasAuthorized(value: string, action: AuthorizationAction): boolean {
  if (actionWasDenied(value, action)) return false;
  if (action === "list") return /\b(?:list|show|enumerate)\b[\s\S]{0,80}\b(?:investigator|agent|subagent)s?\b|\b(?:investigator|agent|subagent)s?\b[\s\S]{0,80}\b(?:available|list)\b/.test(value);
  if (action === "doctor") return /\b(?:doctor|diagnos(?:e|is)|health check)\b/.test(value);
  if (action === "status") return /^\s*(?:status|progress|state)\b/i.test(value) ||
    /\b(?:show|check|get|report|tell me|what is|give me)\b[\s\S]{0,40}\b(?:status|progress|state)\b|\b(?:status|progress|state)\b\s+(?:of|for|on)\b/.test(value);
  const target = "run|investigator|agent|subagent|job|investigation|context-builder|delegate|oracle|planner|researcher|reviewer|scout|worker|child";
  const explicitlyTargets = (verbs: string) => new RegExp(`\\b(?:${verbs})\\b[\\s\\S]{0,50}\\b(?:${target})\\b`).test(value);
  const exactCommand = (verbs: string) => new RegExp(`^\\s*(?:${verbs})\\s*[.!?]*\\s*$`).test(value);
  if (action === "interrupt") return exactCommand("interrupt|pause") || explicitlyTargets("interrupt|pause");
  if (action === "stop") return exactCommand("stop|cancel|terminate") || explicitlyTargets("stop|cancel|terminate");
  if (action === "resume") return exactCommand("resume|continue") || explicitlyTargets("resume|continue");
  // Steering must include an explicit target; a bare verb gives the model no
  // user-authored direction to deliver to a live child.
  return explicitlyTargets("steer|redirect|guide");
}

export function actionAuthorization(value: string, action?: AuthorizationAction): ActionAuthorization {
  const normalized = value.toLowerCase();
  const verbs = action === "interrupt"
    ? "interrupt|pause"
    : action === "stop"
      ? "stop|cancel|terminate"
      : action === "resume"
        ? "resume|continue"
        : action === "steer"
          ? "steer|redirect|guide"
          : undefined;
  // Bind IDs and agent names to the clause containing this action. Otherwise
  // "stop run-a and resume run-b" would authorize either action on both runs.
  const controls = [...normalized.matchAll(/\b(?:interrupt|pause|stop|cancel|terminate|resume|continue|steer|redirect|guide)\b/g)];
  const actionVerb = verbs ? new RegExp(`^(?:${verbs})$`) : undefined;
  const matches = actionVerb ? controls.flatMap((control, index) => {
    if (!actionVerb.test(control[0])) return [];
    const start = control.index ?? 0;
    const next = controls[index + 1]?.index ?? normalized.length;
    return [normalized.slice(start, Math.min(next, start + 200)).split(/[.!?;]/, 1)[0] ?? ""];
  }) : [];
  const scoped = verbs ? matches.join(" ") : normalized;
  // IDs are not necessarily hexadecimal: pi-subagents also uses values such
  // as run-1, nested-run-id, and namespaced session IDs. Extract only tokens
  // that have an ID shape; ordinary prose ("stop the investigation") must
  // remain the deliberate sole-candidate path below.
  const tokens = scoped.match(/\b[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\b/g) ?? [];
  const ids = tokens.filter((token) =>
    /\d/.test(token) || /[-_.:/]/.test(token) || /^[a-f0-9]{8,64}$/i.test(token),
  );
  return {
    ids: [...new Set(ids)],
    agents: [...new Set(scoped.match(/\b(?:context-builder|delegate|investigator|oracle|planner|researcher|reviewer|scout|worker)\b/g) ?? [])],
  };
}

function knownJobs(state: LifecycleState): Array<{ id: string; status: string; agents: string[] }> {
  const jobs = new Map<string, { id: string; status: string; agents: string[] }>();
  for (const source of [state.asyncJobs, state.fleetJobs]) {
    for (const [id, value] of source ?? []) {
      jobs.set(id, {
        id,
        status: typeof value.status === "string" ? value.status.toLowerCase() : "unknown",
        agents: Array.isArray(value.agents) ? value.agents : [],
      });
    }
  }
  return [...jobs.values()];
}

function targetToken(value: string): string {
  return value.trim().replaceAll("\\", "/").split("/").pop() ?? value;
}

function resolveTarget(requested: string, candidates: Array<{ id: string; status: string; agents: string[] }>) {
  const token = targetToken(requested);
  if (!token) return undefined;
  const exact = candidates.filter((candidate) => targetToken(candidate.id) === token);
  if (exact.length === 1) return exact[0];
  if (exact.length > 1) return undefined;
  const prefixed = candidates.filter((candidate) => targetToken(candidate.id).startsWith(token));
  return prefixed.length === 1 ? prefixed[0] : undefined;
}

export function actionTargetIsAuthorized(
  action: string,
  params: LifecycleParams,
  authorization: ActionAuthorization,
  state: LifecycleState,
): boolean {
  if (!TARGETED_ACTIONS.has(action as LifecycleAction)) return true;
  const requested = typeof params.id === "string" ? params.id : typeof params.runId === "string" ? params.runId : undefined;
  const jobs = knownJobs(state);
  const candidates = jobs.filter((job) => action === "resume"
    ? RESUMABLE_JOB_STATES.has(job.status)
    : !TERMINAL_JOB_STATES.has(job.status));
  if (authorization.ids.length > 0) {
    if (requested === undefined) return false;
    const selected = resolveTarget(requested, candidates);
    if (!selected) return false;
    const authorizedTargets = authorization.ids
      .map((id) => resolveTarget(id, candidates))
      .filter((target): target is { id: string; status: string; agents: string[] } => target !== undefined);
    return authorizedTargets.some((target) => target.id === selected.id) &&
      (authorization.agents.length === 0 || selected.agents.some((agent) => authorization.agents.includes(agent.toLowerCase())));
  }
  if (requested !== undefined) {
    const selected = resolveTarget(requested, candidates);
    if (!selected) return false;
    if (authorization.agents.length > 0) return selected.agents.some((agent) => authorization.agents.includes(agent.toLowerCase()));
    return candidates.length === 1;
  }
  if (authorization.agents.length > 0) {
    const matching = candidates.filter((job) => job.agents.some((agent) => authorization.agents.includes(agent.toLowerCase())));
    return matching.length === 1;
  }
  return candidates.length === 1;
}
