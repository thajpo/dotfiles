"""Exact-revision review requests and authority-bound receipts for Phase 9."""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

from .errors import ConstraintError, ControlPlaneError, IdempotencyConflictError, InvalidRequestError, NotFoundError
from .events import append_event_in_transaction
from .models import bounded_text, canonical_json, json_digest, new_id, parse_canonical_json, utc_now, validate_id
from .run_manifest import capability_hash

_VERDICTS = frozenset({"accept", "changes_requested", "comment"})


class ReviewError(ControlPlaneError):
    pass


@dataclass(frozen=True)
class ReviewRecord:
    review_id: str
    change_id: str
    revision: int
    reviewer_conversation_id: str | None
    reviewer_run_id: str | None
    reviewer_actor_id: str | None
    reviewer_source: Mapping[str, Any]
    dependency_review_digest: str | None
    verdict: str | None
    summary: str | None
    findings: str | None
    evidence: Mapping[str, Any]
    state: str
    created_at: str
    submitted_at: str | None

    def as_dict(self) -> dict[str, Any]:
        return {
            "reviewId": self.review_id,
            "changeId": self.change_id,
            "revision": self.revision,
            "reviewerConversationId": self.reviewer_conversation_id,
            "reviewerRunId": self.reviewer_run_id,
            "reviewerActorId": self.reviewer_actor_id,
            "reviewerSource": dict(self.reviewer_source),
            "dependencyReviewDigest": self.dependency_review_digest,
            "verdict": self.verdict,
            "summary": self.summary,
            "findings": self.findings,
            "evidence": dict(self.evidence),
            "state": self.state,
            "createdAt": self.created_at,
            "submittedAt": self.submitted_at,
        }


def _record(row: Mapping[str, Any]) -> ReviewRecord:
    source_json = row["reviewer_source_json"]
    return ReviewRecord(
        review_id=str(row["review_id"]),
        change_id=str(row["change_id"]),
        revision=int(row["revision"]),
        reviewer_conversation_id=row["reviewer_conversation_id"],
        reviewer_run_id=row["reviewer_run_id"],
        reviewer_actor_id=row["reviewer_actor_id"],
        reviewer_source=parse_canonical_json(str(source_json)) if source_json is not None else {},
        dependency_review_digest=row["dependency_review_digest"],
        verdict=row["verdict"],
        summary=row["summary"],
        findings=row["findings"],
        evidence=parse_canonical_json(str(row["evidence_json"])),
        state=str(row["state"]),
        created_at=str(row["created_at"]),
        submitted_at=row["submitted_at"],
    )


def _reviewer_binding(
    store: Any,
    *,
    project_id: str,
    reviewer_conversation_id: str | None,
    reviewer_run_id: str | None,
    reviewer_actor_id: str | None,
    reviewer_capability_secret: str | None,
    require_active: bool = True,
) -> dict[str, str]:
    if reviewer_conversation_id is None or reviewer_run_id is None or reviewer_actor_id is None:
        raise ConstraintError("review requires an authenticated reviewer conversation, run, and actor")
    validate_id(reviewer_conversation_id, prefix="conv")
    validate_id(reviewer_run_id, prefix="run")
    actor = bounded_text(reviewer_actor_id, name="reviewer actor", limit=256)
    conversation = store.conn.execute(
        "SELECT project_id,working_copy_id,role FROM conversations WHERE conversation_id=?",
        (reviewer_conversation_id,),
    ).fetchone()
    if conversation is None or conversation["project_id"] != project_id or conversation["role"] not in {"review", "reviewer"}:
        raise ConstraintError("reviewer conversation is not bound to the change project")
    run = store.conn.execute(
        "SELECT * FROM runs WHERE run_id=? AND conversation_id=?",
        (reviewer_run_id, reviewer_conversation_id),
    ).fetchone()
    if run is None or run["authority"] not in {"read-only", "host-read-only"} or run["project_id"] != project_id or run["working_copy_id"] != conversation["working_copy_id"]:
        raise ConstraintError("reviewer run is not bound to the review conversation")
    if require_active:
        ongoing = run["desired_state"] == "running" and run["observed_state"] == "running"
        cleanly_completed = run["desired_state"] == "stopped" and run["observed_state"] == "stopped"
        if not (ongoing or cleanly_completed):
            raise ConstraintError("reviewer run is not active or cleanly completed")
    capability_hash_value: str | None = None
    if "capability_hash" in run.keys():
        if not isinstance(reviewer_capability_secret, str) or len(reviewer_capability_secret) < 32 or "\x00" in reviewer_capability_secret:
            raise ConstraintError("reviewer capability is invalid")
        capability_hash_value = capability_hash(reviewer_capability_secret)
        if capability_hash_value != run["capability_hash"]:
            raise ConstraintError("reviewer capability is stale")
    return {"conversationId": reviewer_conversation_id, "runId": reviewer_run_id, "actorId": actor, "capabilityHash": capability_hash_value}


def request_review(
    store: Any,
    *,
    change_id: str,
    revision: int,
    reviewer_conversation_id: str | None = None,
    reviewer_run_id: str | None = None,
    reviewer_actor_id: str | None = None,
    reviewer_capability_secret: str | None = None,
    evidence: Mapping[str, Any] | None = None,
    review_id: str | None = None,
    dependency_review_digest: str | None = None,
) -> ReviewRecord:
    validate_id(change_id, prefix="chg")
    if not isinstance(revision, int) or revision < 1:
        raise InvalidRequestError("review revision is invalid")
    evidence_value = dict(evidence or {})
    canonical_json(evidence_value, max_bytes=16 * 1024)
    review_id = review_id or new_id("review")
    validate_id(review_id, prefix="review")
    with store.transaction():
        revision_row = store.conn.execute(
            "SELECT c.project_id,c.current_revision,c.state,r.base_oid,r.tip_oid,r.tree_oid,r.ref_name,r.source_head_oid FROM changes c JOIN change_revisions r ON r.change_id=c.change_id AND r.revision=? WHERE c.change_id=?",
            (revision, change_id),
        ).fetchone()
        if revision_row is None or int(revision_row["current_revision"]) != revision:
            raise ConstraintError("review must bind the current exact change revision")
        if revision_row["state"] not in {"open", "draft"}:
            raise ConstraintError("reviews are not accepted for a change that is not open")
        binding = _reviewer_binding(
            store,
            project_id=str(revision_row["project_id"]),
            reviewer_conversation_id=reviewer_conversation_id,
            reviewer_run_id=reviewer_run_id,
            reviewer_actor_id=reviewer_actor_id,
            reviewer_capability_secret=reviewer_capability_secret,
        )
        reviewer_source = {
            "changeId": change_id,
            "revision": revision,
            "baseOid": revision_row["base_oid"],
            "tipOid": revision_row["tip_oid"],
            "treeOid": revision_row["tree_oid"],
            "refName": revision_row["ref_name"],
            "sourceHeadOid": revision_row["source_head_oid"],
        }
        existing = store.conn.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone()
        if existing is not None:
            same_request = (
                existing["change_id"] == change_id
                and int(existing["revision"]) == revision
                and existing["reviewer_conversation_id"] == binding["conversationId"]
                and existing["reviewer_run_id"] == binding["runId"]
                and existing["reviewer_actor_id"] == binding["actorId"]
                and existing["reviewer_capability_hash"] == binding["capabilityHash"]
                and existing["dependency_review_digest"] == dependency_review_digest
                and parse_canonical_json(str(existing["evidence_json"])) == evidence_value
            )
            if same_request:
                return _record(existing)
            raise IdempotencyConflictError(review_id, existing_digest=json_digest({"changeId": existing["change_id"], "revision": existing["revision"], "reviewerRunId": existing["reviewer_run_id"], "reviewerActorId": existing["reviewer_actor_id"], "dependencyReviewDigest": existing["dependency_review_digest"], "evidence": parse_canonical_json(str(existing["evidence_json"]))}), request_digest=json_digest({"changeId": change_id, "revision": revision, "reviewerRunId": binding["runId"], "reviewerActorId": binding["actorId"], "dependencyReviewDigest": dependency_review_digest, "evidence": evidence_value}))
        now = utc_now()
        store.conn.execute(
            "INSERT INTO reviews(review_id,change_id,revision,reviewer_conversation_id,reviewer_run_id,reviewer_actor_id,reviewer_capability_hash,dependency_review_digest,reviewer_source_json,verdict,summary,findings,evidence_json,state,created_at) VALUES(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
            (
                review_id,
                change_id,
                revision,
                binding["conversationId"],
                binding["runId"],
                binding["actorId"],
                binding["capabilityHash"],
                dependency_review_digest,
                canonical_json(reviewer_source),
                None,
                None,
                None,
                canonical_json(evidence_value),
                "requested",
                now,
            ),
        )
        append_event_in_transaction(
            store.conn,
            event_kind="review.requested",
            resource_type="review",
            resource_id=review_id,
            payload={
                "reviewId": review_id,
                "changeId": change_id,
                "revision": revision,
                "reviewerRunId": binding["runId"],
                "reviewerActorId": binding["actorId"],
            },
        )
        return _record(store.conn.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone())


def submit_review(
    store: Any,
    *,
    review_id: str,
    verdict: str,
    summary: str = "",
    findings: str = "",
    evidence: Mapping[str, Any] | None = None,
    reviewer_run_id: str | None = None,
    reviewer_actor_id: str | None = None,
    reviewer_capability_secret: str | None = None,
) -> ReviewRecord:
    validate_id(review_id, prefix="review")
    if verdict not in _VERDICTS:
        raise InvalidRequestError("review verdict is invalid")
    summary = bounded_text(summary, name="review summary", limit=4096, allow_empty=True)
    findings = bounded_text(findings, name="review findings", limit=16 * 1024, allow_empty=True)
    evidence_value = dict(evidence or {})
    canonical_json(evidence_value, max_bytes=16 * 1024)
    request_digest = json_digest(
        {
            "verdict": verdict,
            "summary": summary,
            "findings": findings,
            "evidence": evidence_value,
            "reviewerRunId": reviewer_run_id,
            "reviewerActorId": reviewer_actor_id,
        }
    )
    with store.transaction():
        row = store.conn.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone()
        if row is None:
            raise NotFoundError("review was not found", detail={"review_id": review_id})
        project = store.conn.execute("SELECT project_id FROM changes WHERE change_id=?", (row["change_id"],)).fetchone()
        if project is None:
            raise NotFoundError("review change was not found", detail={"change_id": row["change_id"]})
        _reviewer_binding(
            store,
            project_id=str(project["project_id"]),
            reviewer_conversation_id=row["reviewer_conversation_id"],
            reviewer_run_id=reviewer_run_id,
            reviewer_actor_id=reviewer_actor_id,
            reviewer_capability_secret=reviewer_capability_secret,
            require_active=row["state"] != "submitted",
        )
        if reviewer_run_id != row["reviewer_run_id"] or reviewer_actor_id != row["reviewer_actor_id"]:
            raise ConstraintError("review receipt identity does not match the requested reviewer")
        if row["state"] == "submitted":
            prior = {
                "verdict": row["verdict"],
                "summary": row["summary"] or "",
                "findings": row["findings"] or "",
                "evidence": parse_canonical_json(str(row["evidence_json"])),
                "reviewerRunId": row["reviewer_run_id"],
                "reviewerActorId": row["reviewer_actor_id"],
            }
            if json_digest(prior) != request_digest:
                raise IdempotencyConflictError(review_id, existing_digest=json_digest(prior), request_digest=request_digest)
            return _record(row)
        if row["state"] not in {"requested", "running"}:
            raise ReviewError("review is not accepting a receipt")
        now = utc_now()
        cursor = store.conn.execute(
            "UPDATE reviews SET verdict=?,summary=?,findings=?,evidence_json=?,state='submitted',submitted_at=? WHERE review_id=? AND state IN ('requested','running')",
            (verdict, summary, findings, canonical_json(evidence_value), now, review_id),
        )
        if cursor.rowcount != 1:
            current = store.conn.execute("SELECT state FROM reviews WHERE review_id=?", (review_id,)).fetchone()
            raise ReviewError(f"review state changed during receipt submission: {current['state'] if current is not None else 'missing'}")
        append_event_in_transaction(
            store.conn,
            event_kind="review.submitted",
            resource_type="review",
            resource_id=review_id,
            payload={
                "reviewId": review_id,
                "changeId": row["change_id"],
                "revision": row["revision"],
                "verdict": verdict,
                "reviewerRunId": row["reviewer_run_id"],
                "reviewerActorId": row["reviewer_actor_id"],
                "evidenceDigest": json_digest(evidence_value),
            },
        )
        return _record(store.conn.execute("SELECT * FROM reviews WHERE review_id=?", (review_id,)).fetchone())


def list_reviews(store: Any, *, change_id: str, revision: int | None = None) -> list[ReviewRecord]:
    validate_id(change_id, prefix="chg")
    if revision is None:
        rows = store.conn.execute("SELECT * FROM reviews WHERE change_id=? ORDER BY created_at", (change_id,))
    else:
        rows = store.conn.execute("SELECT * FROM reviews WHERE change_id=? AND revision=? ORDER BY created_at", (change_id, revision))
    return [_record(row) for row in rows]


__all__ = ["ReviewError", "ReviewRecord", "list_reviews", "request_review", "submit_review"]
