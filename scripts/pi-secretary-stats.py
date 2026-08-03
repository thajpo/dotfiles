#!/usr/bin/env python3
"""Summarize the secretary's privacy-preserving JSONL run statistics."""

from __future__ import annotations

import argparse
import json
import os
from collections import Counter
from pathlib import Path
from typing import Any


def default_path() -> Path:
    configured = os.environ.get("PI_CODING_AGENT_DIR")
    if configured == "~":
        root = Path.home()
    elif configured and configured.startswith("~/"):
        root = Path.home() / configured[2:]
    elif configured:
        root = Path(configured)
    else:
        root = Path.home() / ".pi" / "agent"
    return root / "secretary-stats.jsonl"


def number(value: Any) -> float:
    return value if isinstance(value, (int, float)) and value >= 0 else 0


def integer(value: Any) -> int:
    return int(number(value))


def zero_metrics() -> dict[str, Any]:
    return {
        "records": 0,
        "durationMs": 0,
        "turns": 0,
        "toolCalls": 0,
        "inputTokens": 0,
        "outputTokens": 0,
        "cacheReadTokens": 0,
        "cacheWriteTokens": 0,
        "totalTokens": 0,
        "costUsd": 0.0,
    }


def add_metrics(target: dict[str, Any], record: dict[str, Any]) -> None:
    tokens = record.get("tokens") if isinstance(record.get("tokens"), dict) else {}
    target["records"] += 1
    target["durationMs"] += integer(record.get("durationMs"))
    target["turns"] += integer(record.get("turns"))
    target["toolCalls"] += integer(record.get("toolCalls"))
    for key in ("inputTokens", "outputTokens", "cacheReadTokens", "cacheWriteTokens", "totalTokens"):
        target[key] += integer(tokens.get(key))
    target["costUsd"] += number(tokens.get("costUsd"))


def load(path: Path, last: int | None) -> list[dict[str, Any]]:
    if not path.exists():
        return []
    records: list[dict[str, Any]] = []
    with path.open(encoding="utf-8") as stream:
        for line in stream:
            try:
                value = json.loads(line)
            except json.JSONDecodeError:
                continue
            if isinstance(value, dict):
                records.append(value)
    return records[-last:] if last else records


def summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    sessions = zero_metrics()
    runs = zero_metrics()
    by_agent: dict[str, dict[str, Any]] = {}
    by_model: dict[str, dict[str, Any]] = {}
    by_project: dict[str, dict[str, Any]] = {}
    failure_kinds: Counter[str] = Counter()
    acceptance_levels: Counter[str] = Counter()

    for record in records:
        kind = record.get("kind")
        if kind == "session":
            add_metrics(sessions, record)
        elif kind == "subagent_run":
            add_metrics(runs, record)
            failure = record.get("failure") if isinstance(record.get("failure"), dict) else None
            if failure:
                failure_kinds[str(failure.get("kind") or "unknown")] += 1
            for step in record.get("steps", []):
                if not isinstance(step, dict):
                    continue
                if isinstance(step.get("acceptanceLevel"), str):
                    acceptance_levels[step["acceptanceLevel"]] += 1
                agent = str(step.get("agent") or "unknown")
                agent_metrics = by_agent.setdefault(agent, zero_metrics())
                add_metrics(agent_metrics, step)
                model = str(step.get("model") or "unknown")
                model_metrics = by_model.setdefault(model, zero_metrics())
                add_metrics(model_metrics, step)

        project = str(record.get("projectAlias") or "unknown")
        project_metrics = by_project.setdefault(project, zero_metrics())
        add_metrics(project_metrics, record)

    def finish(metrics: dict[str, Any]) -> dict[str, Any]:
        metrics = dict(metrics)
        metrics["averageDurationMs"] = (
            round(metrics["durationMs"] / metrics["records"], 2) if metrics["records"] else 0
        )
        metrics["costUsd"] = round(metrics["costUsd"], 8)
        return metrics

    return {
        "records": len(records),
        "sessions": finish(sessions),
        "subagentRuns": finish(runs),
        "stepsByAgent": {key: finish(value) for key, value in sorted(by_agent.items())},
        "stepsByModel": {key: finish(value) for key, value in sorted(by_model.items())},
        "byProject": {key: finish(value) for key, value in sorted(by_project.items())},
        "failures": {
            "total": sum(failure_kinds.values()),
            "byKind": dict(sorted(failure_kinds.items())),
            "stepsByAcceptanceLevel": dict(sorted(acceptance_levels.items())),
        },
    }


def human(summary: dict[str, Any], path: Path) -> str:
    def line(label: str, value: dict[str, Any]) -> str:
        return (
            f"{label}: {value['records']} records, {value['durationMs']}ms total "
            f"({value['averageDurationMs']}ms avg), {value['totalTokens']} tokens, "
            f"{value['turns']} turns, {value['toolCalls']} tools, ${value['costUsd']:.8f}"
        )

    failures = summary["failures"]
    lines = [f"Stats: {path}", line("Sessions", summary["sessions"]), line("Subagent runs", summary["subagentRuns"]),
             f"Failures: {failures['total']}" + (f" ({', '.join(f'{key}={value}' for key, value in failures['byKind'].items())})" if failures["byKind"] else "")]
    if failures["stepsByAcceptanceLevel"]:
        lines.append("Acceptance levels: " + ", ".join(f"{key}={value}" for key, value in failures["stepsByAcceptanceLevel"].items()))
    if summary["stepsByAgent"]:
        lines.append("Agents:")
        lines.extend(f"  {name}: {line('', value).lstrip(': ')}" for name, value in summary["stepsByAgent"].items())
    if summary["stepsByModel"]:
        lines.append("Models:")
        lines.extend(f"  {name}: {line('', value).lstrip(': ')}" for name, value in summary["stepsByModel"].items())
    return "\n".join(lines)


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--file", type=Path, default=default_path(), help="JSONL statistics file")
    parser.add_argument("--last", type=int, metavar="N", help="only summarize the newest N records")
    parser.add_argument("--json", action="store_true", help="emit machine-readable JSON")
    args = parser.parse_args()
    if args.last is not None and args.last < 1:
        parser.error("--last must be positive")
    summary = summarize(load(args.file, args.last))
    print(json.dumps(summary, indent=2, sort_keys=True) if args.json else human(summary, args.file))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
