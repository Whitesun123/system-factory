"""Workflow and step states, and the only legal transitions between workflow states."""
from __future__ import annotations


class WF:
    QUEUED = "queued"
    RUNNING = "running"
    AWAITING_APPROVAL = "awaiting_approval"
    BLOCKED = "blocked"          # needs human input (cannot reproduce, unsupported work, ...)
    FAILED = "failed"            # retries exhausted or permanent error; human may retry
    APPROVED = "approved"
    REJECTED = "rejected"
    CANCELLED = "cancelled"


class STEP:
    PENDING = "pending"
    RUNNING = "running"
    SUCCEEDED = "succeeded"
    FAILED = "failed"
    WAITING = "waiting"          # waiting on a human (approval)
    BLOCKED = "blocked"
    CANCELLED = "cancelled"


TERMINAL = {WF.APPROVED, WF.REJECTED, WF.CANCELLED}

TRANSITIONS: dict[str, set[str]] = {
    WF.QUEUED: {WF.RUNNING, WF.CANCELLED},
    # RUNNING -> QUEUED is crash recovery (stale lease re-queued).
    WF.RUNNING: {WF.AWAITING_APPROVAL, WF.BLOCKED, WF.FAILED, WF.CANCELLED, WF.QUEUED},
    WF.AWAITING_APPROVAL: {WF.APPROVED, WF.REJECTED, WF.CANCELLED},
    WF.BLOCKED: {WF.QUEUED, WF.CANCELLED},
    WF.FAILED: {WF.QUEUED, WF.CANCELLED},
    WF.APPROVED: set(),
    WF.REJECTED: set(),
    WF.CANCELLED: set(),
}


class IllegalTransition(Exception):
    pass


def check_transition(current: str, new: str) -> None:
    if new not in TRANSITIONS.get(current, set()):
        raise IllegalTransition(f"illegal workflow transition {current} -> {new}")


# Step pipelines per work type. Triage decides the type; remaining steps are added after it.
PIPELINES: dict[str, list[tuple[str, str]]] = {
    "bug": [
        ("triage", "triage"), ("requirements", "requirements"), ("plan", "architecture"),
        ("workspace", "infra"), ("reproduce", "qa"), ("coding", "coding"),
        ("testing", "qa"), ("security", "security"), ("review", "review"), ("approval", "human"),
    ],
    "feature": [
        ("triage", "triage"), ("requirements", "requirements"), ("plan", "architecture"),
        ("workspace", "infra"), ("coding", "coding"), ("testing", "qa"),
        ("security", "security"), ("review", "review"), ("approval", "human"),
    ],
    "support": [
        ("triage", "triage"), ("support", "support"), ("review", "review"), ("approval", "human"),
    ],
}
