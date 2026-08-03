export type ActionAuthorization = { ids: string[]; agents: string[] };
export type LifecycleAction = "interrupt" | "stop" | "resume" | "steer";
export type LifecycleParams = { id?: unknown; runId?: unknown };
export type LifecycleJob = { status?: string; agents?: string[] };
export type LifecycleState = {
  asyncJobs?: ReadonlyMap<string, LifecycleJob>;
  fleetJobs?: ReadonlyMap<string, LifecycleJob>;
};

const TARGETED_ACTIONS = new Set<LifecycleAction>(["interrupt", "stop", "resume", "steer"]);
const TERMINAL_JOB_STATES = new Set(["complete", "completed", "failed", "paused", "stopped"]);

export function actionAuthorization(value: string): ActionAuthorization {
  // IDs are not necessarily hexadecimal: pi-subagents also uses values such
  // as run-1, nested-run-id, and namespaced session IDs. Extract only tokens
  // that have an ID shape; ordinary prose ("stop the investigation") must
  // remain the deliberate sole-candidate path below.
  const tokens = value.match(/\b[A-Za-z0-9][A-Za-z0-9._:/-]{0,127}\b/g) ?? [];
  const ids = tokens.filter((token) =>
    /\d/.test(token) || /[-_.:/]/.test(token) || /^[a-f0-9]{8,64}$/i.test(token),
  );
  return {
    ids: [...new Set(ids)],
    agents: [...new Set(value.match(/\b(?:scout|researcher|oracle|worker|investigator)\b/g) ?? [])],
  };
}

function knownJobs(state: LifecycleState): Array<{ id: string; status: string; agents: string[] }> {
  const jobs = new Map<string, { id: string; status: string; agents: string[] }>();
  for (const source of [state.asyncJobs, state.fleetJobs]) {
    for (const [id, value] of source ?? []) {
      jobs.set(id, { id, status: value.status ?? "unknown", agents: Array.isArray(value.agents) ? value.agents : [] });
    }
  }
  return [...jobs.values()];
}

function targetMatches(requested: string, candidate: string): boolean {
  const left = requested.trim().replaceAll("\\", "/").split("/").pop() ?? requested;
  return Boolean(left && candidate && left === candidate);
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
    ? !["complete", "completed", "stopped"].includes(job.status)
    : !TERMINAL_JOB_STATES.has(job.status));
  if (authorization.ids.length > 0) {
    if (requested === undefined) return false;
    const selected = candidates.find((job) => targetMatches(requested, job.id));
    return selected !== undefined && authorization.ids.some((id) => targetMatches(requested, id)) &&
      (authorization.agents.length === 0 || selected.agents.some((agent) => authorization.agents.includes(agent.toLowerCase())));
  }
  if (requested !== undefined) {
    const matching = candidates.filter((job) => targetMatches(requested, job.id));
    if (authorization.agents.length > 0) return matching.some((job) => job.agents.some((agent) => authorization.agents.includes(agent.toLowerCase())));
    return matching.length === 1 && candidates.length === 1;
  }
  if (authorization.agents.length > 0) {
    const matching = candidates.filter((job) => job.agents.some((agent) => authorization.agents.includes(agent.toLowerCase())));
    return matching.length === 1;
  }
  return candidates.length === 1;
}
