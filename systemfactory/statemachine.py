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


class IT:
    """Phases of one coding/review iteration inside the implementation loop."""
    CODING = "coding"
    TESTING = "testing"
    REVIEWING = "reviewing"
    AGGREGATING = "aggregating"
    REVISE = "revise"            # closed: blocking feedback, another iteration follows
    PASSED = "passed"            # closed: no blocking findings, proceeds to the quality gate
    STALLED = "stalled"          # closed: repeated solution / identical failures / no progress -> escalate
    EXHAUSTED = "exhausted"      # closed: iteration, time or cost budget reached -> escalate
    FAILED = "failed"            # closed: a step in the iteration failed permanently
    CANCELLED = "cancelled"


ITERATION_OPEN = {IT.CODING, IT.TESTING, IT.REVIEWING, IT.AGGREGATING}

ITERATION_TRANSITIONS: dict[str, set[str]] = {
    IT.CODING: {IT.TESTING, IT.STALLED, IT.FAILED, IT.CANCELLED},
    IT.TESTING: {IT.REVIEWING, IT.FAILED, IT.CANCELLED},
    IT.REVIEWING: {IT.AGGREGATING, IT.STALLED, IT.EXHAUSTED, IT.FAILED, IT.CANCELLED},
    IT.AGGREGATING: {IT.REVISE, IT.PASSED, IT.STALLED, IT.EXHAUSTED, IT.FAILED, IT.CANCELLED},
    IT.REVISE: set(), IT.PASSED: set(), IT.STALLED: set(), IT.EXHAUSTED: set(),
    IT.FAILED: set(), IT.CANCELLED: set(),
}


def check_iteration_transition(current: str, new: str) -> None:
    if current != new and new not in ITERATION_TRANSITIONS.get(current, set()):
        raise IllegalTransition(f"illegal iteration transition {current} -> {new}")


# The implementation loop: these steps repeat (one pass = one iteration) until the feedback step
# decides "pass" (-> quality_gate) or escalates (workflow -> blocked).
LOOP_STEPS = ["coding", "testing", "review", "feedback"]

_CODE_TAIL = [("coding", "coding"), ("testing", "qa"), ("review", "review-panel"),
              ("feedback", "aggregator"), ("quality_gate", "quality-gate"), ("approval", "human")]

# Step pipelines per work type. Triage decides the type; remaining steps are added after it.
PIPELINES: dict[str, list[tuple[str, str]]] = {
    "bug": [("triage", "triage"), ("requirements", "requirements"), ("plan", "architecture"),
            ("workspace", "infra"), ("reproduce", "qa"), *_CODE_TAIL],
    "feature": [("triage", "triage"), ("requirements", "requirements"), ("plan", "architecture"),
                ("workspace", "infra"), *_CODE_TAIL],
    "support": [("triage", "triage"), ("support", "support"), ("answer_review", "review"),
                ("approval", "human")],
}
