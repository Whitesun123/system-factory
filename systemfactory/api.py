"""FastAPI service: JSON API over the real workflow store, the dashboard, and a background worker.

Binds to localhost only by default and has no authentication: it is a single-user local tool.
"""
from __future__ import annotations

from contextlib import asynccontextmanager
from pathlib import Path
from typing import Literal

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, Response
from pydantic import BaseModel, Field

import time

from . import evidence as evidence_mod
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
    runtime: Literal["simulated", "ollama"] | None = None
    faults: dict[str, int] = Field(default_factory=dict)
    throttle_ms: int = Field(default=0, ge=0, le=2000)


class SampleIn(BaseModel):
    faults: dict[str, int] = Field(default_factory=dict)
    throttle_ms: int = Field(default=0, ge=0, le=2000)
    coder: Literal["default", "simulated-stubborn"] = "default"
    coder_feedback: bool = True
    runtime: Literal["simulated", "ollama"] | None = None
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
    status_cache: dict = {}

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
        now = time.time()
        if now - status_cache.get("at", 0) > 10:  # do not hammer Ollama from a 1.5 s polling dashboard
            status_cache.update(at=now, info=runtime_info(settings))
        return {"ok": True, "runtime": status_cache["info"], "counts": orch.store.counts_by_state(),
                "worker": {"running": start_worker, "owner": worker.owner, "concurrency": worker.concurrency,
                           "active_workflows": sorted(worker.active)},
                "repo": str(settings.demo_repo)}

    @app.get("/api/samples")
    def samples():
        return [{"key": k, "label": v["label"], "title": v["title"]} for k, v in SAMPLES.items()]

    @app.post("/api/issues", status_code=201)
    def submit_issue(issue: IssueIn):
        wf_id = guard(orch.submit, issue.title, issue.body, source="dashboard", runtime=issue.runtime,
                      options={"faults": issue.faults, "throttle_ms": issue.throttle_ms})
        return {"workflow_id": wf_id}

    @app.post("/api/samples/{key}", status_code=201)
    def submit_sample(key: str, opts: SampleIn | None = None):
        if key not in SAMPLES:
            raise HTTPException(404, f"unknown sample {key}")
        opts = opts or SampleIn()
        s = SAMPLES[key]
        options = {"faults": opts.faults, "throttle_ms": opts.throttle_ms}
        if opts.coder != "default":
            options["coder"] = opts.coder
        if not opts.coder_feedback:
            options["coder_feedback"] = False
        wf_id = guard(orch.submit, s["title"], s["body"], source=f"sample:{key}", runtime=opts.runtime,
                      options=options, budget={"max_iterations": opts.max_iterations} if opts.max_iterations else None)
        return {"workflow_id": wf_id}

    @app.get("/api/reviewers")
    def reviewer_adapters():
        out = []
        for name in [*reviewers_mod.FACTORIES, "ollama-a", "ollama-b",
                     *[n for n in settings.reviewers if n.startswith("command:")]]:
            r = reviewers_mod.make(name, settings.runtime_options("ollama"), settings)
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
                "llm_calls": orch.store.get_llm_calls(wf_id), "tool_calls": orch.store.get_tool_calls(wf_id),
                "test_runs": orch.store.get_test_runs(wf_id),
                "policies": {s["role"]: ROLE_POLICIES.get(s["role"], {}) for s in steps}}

    @app.get("/api/llm_calls/{call_id}")
    def llm_call(call_id: int):
        c = orch.store.get_llm_call(call_id)
        if c is None:
            raise HTTPException(404, f"llm call {call_id} not found")
        return evidence_mod.Sanitizer(settings.data_dir)(c)

    @app.get("/api/workflows/{wf_id}/export")
    def export_evidence(wf_id: int):
        data = guard(evidence_mod.export_zip, orch, wf_id)
        return Response(data, media_type="application/zip",
                        headers={"Content-Disposition": f'attachment; filename="system-factory-wf-{wf_id}-evidence.zip"'})

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
