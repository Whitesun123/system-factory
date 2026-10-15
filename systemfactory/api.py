"""FastAPI service: JSON API over the real workflow store, the dashboard, and a background worker.

Binds to localhost only by default and has no authentication: it is a single-user local tool.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from . import reviewers as reviewers_mod
from .config import Settings, runtime_info
from .demo import SAMPLES, init_demo_repo
from .orchestrator import Orchestrator, Worker
from .roles import ROLE_POLICIES
from .statemachine import IllegalTransition

STATIC = Path(__file__).parent / "static"


class IssueIn(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    body: str = Field(default="", max_length=20000)
    faults: dict[str, int] = Field(default_factory=dict)
    throttle_ms: int = Field(default=0, ge=0, le=2000)


class SampleIn(BaseModel):
    faults: dict[str, int] = Field(default_factory=dict)
    throttle_ms: int = Field(default=0, ge=0, le=2000)
    coder: Literal["simulated-repair", "simulated-stubborn"] = "simulated-repair"
    max_iterations: int | None = Field(default=None, ge=1, le=10)


class DecisionIn(BaseModel):
    actor: str = Field(min_length=1, max_length=80)
    comment: str = Field(default="", max_length=2000)


def create_app(settings: Settings | None = None, *, start_worker: bool = True) -> FastAPI:
    settings = settings or Settings.from_env()
    orch = Orchestrator(settings)
    worker = Worker(orch)

    @asynccontextmanager
    async def lifespan(app: FastAPI):
        init_demo_repo(settings)  # creates the demo repo only if it does not exist
        if start_worker:
            worker.start()
        yield
        if start_worker:
            worker.stop()

    app = FastAPI(title="System Factory", version="0.1.0", lifespan=lifespan)
    app.state.orch, app.state.worker = orch, worker

    def guard(fn, *args, **kwargs):
        try:
            return fn(*args, **kwargs)
        except KeyError as e:
            raise HTTPException(404, str(e))
        except IllegalTransition as e:
            raise HTTPException(409, str(e))
        except ValueError as e:
            raise HTTPException(400, str(e))

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(STATIC / "index.html")

    @app.get("/api/health")
    def health():
        return {"ok": True, "runtime": runtime_info(), "counts": orch.store.counts_by_state(),
                "worker": {"running": start_worker, "owner": worker.owner, "concurrency": worker.concurrency,
                           "active_workflows": sorted(worker.active)},
                "repo": str(settings.demo_repo)}

    @app.get("/api/samples")
    def samples():
        return [{"key": k, "label": v["label"], "title": v["title"]} for k, v in SAMPLES.items()]

    @app.post("/api/issues", status_code=201)
    def submit_issue(issue: IssueIn):
        wf_id = guard(orch.submit, issue.title, issue.body, source="dashboard",
                      options={"faults": issue.faults, "throttle_ms": issue.throttle_ms})
        return {"workflow_id": wf_id}

    @app.post("/api/samples/{key}", status_code=201)
    def submit_sample(key: str, opts: SampleIn | None = None):
        if key not in SAMPLES:
            raise HTTPException(404, f"unknown sample {key}")
        opts = opts or SampleIn()
        s = SAMPLES[key]
        wf_id = orch.submit(s["title"], s["body"], source=f"sample:{key}",
                            options={"faults": opts.faults, "throttle_ms": opts.throttle_ms, "coder": opts.coder},
                            budget={"max_iterations": opts.max_iterations} if opts.max_iterations else None)
        return {"workflow_id": wf_id}

    @app.get("/api/reviewers")
    def reviewer_adapters():
        out = []
        for name in [*reviewers_mod.FACTORIES, *[n for n in settings.reviewers if n.startswith("command:")]]:
            r = reviewers_mod.make(name)
            ok, why = r.available()
            out.append({"name": name, "provider": r.provider, "model": r.model, "simulated": r.simulated,
                        "technique": r.technique, "available": ok, "reason": why,
                        "configured": name in settings.reviewers})
        return {"configured": settings.reviewers, "min_reviewers": settings.min_reviewers, "adapters": out}

    @app.get("/api/workflows")
    def list_workflows():
        return [{k: w[k] for k in ("id", "issue_title", "issue_source", "kind", "state", "current_step",
                                    "total_attempts", "created_at", "started_at", "finished_at", "updated_at",
                                    "error", "usage")} for w in orch.store.list_workflows()]

    @app.get("/api/workflows/{wf_id}")
    def get_workflow(wf_id: int):
        wf = orch.store.get_workflow(wf_id)
        if wf is None:
            raise HTTPException(404, f"workflow {wf_id} not found")
        steps = orch.store.get_steps(wf_id)
        return {"workflow": wf, "steps": steps, "attempts": orch.store.get_attempts(wf_id),
                "events": orch.store.get_events(wf_id), "evidence": orch.store.get_evidence(wf_id),
                "approvals": orch.store.get_approvals(wf_id),
                "iterations": orch.store.get_iterations(wf_id), "reviews": orch.store.get_reviews(wf_id),
                "policies": {s["role"]: ROLE_POLICIES.get(s["role"], {}) for s in steps}}

    @app.post("/api/workflows/{wf_id}/approve")
    def approve(wf_id: int, d: DecisionIn):
        guard(orch.approve, wf_id, d.actor, d.comment)
        return {"state": orch.store.get_workflow(wf_id)["state"]}

    @app.post("/api/workflows/{wf_id}/reject")
    def reject(wf_id: int, d: DecisionIn):
        guard(orch.reject, wf_id, d.actor, d.comment)
        return {"state": orch.store.get_workflow(wf_id)["state"]}

    @app.post("/api/workflows/{wf_id}/cancel")
    def cancel(wf_id: int):
        guard(orch.cancel, wf_id, "dashboard")
        return {"state": orch.store.get_workflow(wf_id)["state"]}

    @app.post("/api/workflows/{wf_id}/retry")
    def retry(wf_id: int):
        guard(orch.retry, wf_id, "dashboard")
        return {"state": orch.store.get_workflow(wf_id)["state"]}

    return app
