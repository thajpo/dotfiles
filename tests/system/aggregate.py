from __future__ import annotations
try:
    from .evidence import aggregate
except ImportError:
    from evidence import aggregate

def aggregate_evidence(evidence):
    statuses = [item.get("status") for item in evidence]
    return {"exitCode": aggregate(statuses), "statuses": statuses, "falseGreen": not statuses or any(status not in {"PASS", "FAIL", "STOP", "SKIP"} for status in statuses) or any(status == "SKIP" for status in statuses)}

__all__ = ["aggregate_evidence"]
