def run(journey_id: str, action_ids: list[str], *, capability: bool = False):
    return {"scenarioId": journey_id, "actionIds": action_ids, "status": "PASS" if capability else "STOP", "reason": None if capability else "required process/staged capability is unavailable"}
