"""SQLite-backed persistent workflow state: issues, workflows, steps, attempts, events, evidence, approvals.

Every write that changes workflow state goes through a short BEGIN IMMEDIATE transaction so that
concurrent workers (threads or processes) cannot double-claim or race transitions.
"""
from __future__ import annotations

import json
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path
from typing import Any, Iterator

from .statemachine import IT, ITERATION_OPEN, STEP, WF, check_iteration_transition, check_transition

SCHEMA = """
CREATE TABLE IF NOT EXISTS issues (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    title TEXT NOT NULL,
    body TEXT NOT NULL,
    source TEXT NOT NULL DEFAULT 'manual',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS workflows (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    issue_id INTEGER NOT NULL REFERENCES issues(id),
    repo TEXT NOT NULL,
    kind TEXT,
    state TEXT NOT NULL,
    current_step TEXT,
    cancel_requested INTEGER NOT NULL DEFAULT 0,
    options TEXT NOT NULL DEFAULT '{}',
    budget TEXT NOT NULL DEFAULT '{}',
    usage TEXT NOT NULL DEFAULT '{}',
    data TEXT NOT NULL DEFAULT '{}',
    branch TEXT,
    worktree TEXT,
    base_commit TEXT,
    error TEXT,
    lease_owner TEXT,
    heartbeat_at REAL,
    created_at REAL NOT NULL,
    updated_at REAL NOT NULL,
    started_at REAL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS steps (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id),
    seq INTEGER NOT NULL,
    name TEXT NOT NULL,
    role TEXT NOT NULL,
    status TEXT NOT NULL,
    attempts INTEGER NOT NULL DEFAULT 0,
    output TEXT NOT NULL DEFAULT '{}',
    error TEXT,
    started_at REAL,
    finished_at REAL,
    UNIQUE(workflow_id, name)
);
CREATE TABLE IF NOT EXISTS attempts (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    step_id INTEGER NOT NULL REFERENCES steps(id),
    workflow_id INTEGER NOT NULL REFERENCES workflows(id),
    n INTEGER NOT NULL,
    status TEXT NOT NULL,
    error TEXT,
    detail TEXT NOT NULL DEFAULT '{}',
    started_at REAL NOT NULL,
    finished_at REAL
);
CREATE TABLE IF NOT EXISTS events (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id),
    ts REAL NOT NULL,
    level TEXT NOT NULL,
    step TEXT,
    actor TEXT NOT NULL,
    message TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}'
);
CREATE TABLE IF NOT EXISTS evidence (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id),
    step TEXT NOT NULL,
    kind TEXT NOT NULL,
    title TEXT NOT NULL,
    content TEXT NOT NULL,
    data TEXT NOT NULL DEFAULT '{}',
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS approvals (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id),
    decision TEXT NOT NULL,
    actor TEXT NOT NULL,
    comment TEXT NOT NULL DEFAULT '',
    ts REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS iterations (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id),
    n INTEGER NOT NULL,
    status TEXT NOT NULL,
    base TEXT,
    "commit" TEXT,
    fingerprint TEXT,
    patch TEXT NOT NULL DEFAULT '{}',
    tests TEXT NOT NULL DEFAULT '{}',
    feedback TEXT NOT NULL DEFAULT '{}',
    failure_signature TEXT,
    blocking_count INTEGER,
    decision TEXT,
    decision_reason TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    UNIQUE(workflow_id, n)
);
CREATE TABLE IF NOT EXISTS reviews (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id),
    iteration INTEGER NOT NULL,
    reviewer TEXT NOT NULL,
    adapter TEXT NOT NULL,
    provider TEXT NOT NULL,
    model TEXT NOT NULL,
    simulated INTEGER NOT NULL,
    verdict TEXT NOT NULL,
    summary TEXT NOT NULL DEFAULT '',
    findings TEXT NOT NULL DEFAULT '[]',
    input_tokens INTEGER NOT NULL DEFAULT 0,
    output_tokens INTEGER NOT NULL DEFAULT 0,
    cost_usd REAL NOT NULL DEFAULT 0,
    seconds REAL NOT NULL DEFAULT 0,
    error TEXT,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS llm_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id),
    iteration INTEGER,
    step TEXT NOT NULL,
    role TEXT NOT NULL,
    provider TEXT NOT NULL,
    execution TEXT NOT NULL,          -- real-local | real-cloud | simulated
    model TEXT NOT NULL,
    served_model TEXT,
    model_digest TEXT,
    model_details TEXT NOT NULL DEFAULT '{}',
    base_url TEXT,
    purpose TEXT,
    status TEXT NOT NULL,             -- ok | unavailable | model_missing | timeout | invalid_output | error
    error TEXT,
    started_at REAL NOT NULL,
    finished_at REAL,
    runtime_seconds REAL,
    load_seconds REAL,
    eval_seconds REAL,
    input_tokens INTEGER,
    output_tokens INTEGER,
    done_reason TEXT,
    prompt_sha256 TEXT,
    response_sha256 TEXT,
    prompt TEXT,
    response TEXT
);
CREATE TABLE IF NOT EXISTS tool_calls (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id),
    iteration INTEGER,
    step TEXT NOT NULL,
    role TEXT NOT NULL,
    llm_call_id INTEGER,
    tool TEXT NOT NULL,
    args TEXT NOT NULL DEFAULT '{}',
    ok INTEGER NOT NULL,
    result TEXT NOT NULL DEFAULT '',
    seconds REAL NOT NULL DEFAULT 0,
    created_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS test_runs (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    workflow_id INTEGER NOT NULL REFERENCES workflows(id),
    iteration INTEGER,
    step TEXT NOT NULL,
    purpose TEXT NOT NULL,
    command TEXT NOT NULL,
    commit_sha TEXT,
    exit_code INTEGER NOT NULL,
    timed_out INTEGER NOT NULL DEFAULT 0,
    passed INTEGER,
    failed INTEGER,
    errors INTEGER,
    summary TEXT,
    seconds REAL NOT NULL,
    output_tail TEXT,
    created_at REAL NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_events_wf ON events(workflow_id, id);
CREATE INDEX IF NOT EXISTS idx_steps_wf ON steps(workflow_id, seq);
CREATE INDEX IF NOT EXISTS idx_wf_state ON workflows(state);
"""

JSON_COLUMNS = {"options", "budget", "usage", "data", "output", "detail", "patch", "tests", "feedback", "findings",
                "model_details", "args", "checks"}


def _now() -> float:
    return time.time()


def _row(row: sqlite3.Row | None) -> dict | None:
    if row is None:
        return None
    out = dict(row)
    for k in JSON_COLUMNS & out.keys():
        out[k] = json.loads(out[k]) if out[k] else {}
    return out


class Store:
    def __init__(self, path: Path | str):
        self.path = str(path)
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._conn() as c:
            c.execute("PRAGMA journal_mode=WAL")
            c.executescript(SCHEMA)
            # migrations for databases created by earlier versions
            cols = {r["name"] for r in c.execute("PRAGMA table_info(attempts)")}
            if "iteration" not in cols:
                c.execute("ALTER TABLE attempts ADD COLUMN iteration INTEGER")
            rcols = {r["name"] for r in c.execute("PRAGMA table_info(reviews)")}
            for col, typ in (("execution", "TEXT"), ("model_digest", "TEXT"), ("llm_call_id", "INTEGER"),
                             ("checks", "TEXT"), ("analysis", "TEXT")):
                if col not in rcols:
                    c.execute(f"ALTER TABLE reviews ADD COLUMN {col} {typ}")

    @contextmanager
    def _conn(self) -> Iterator[sqlite3.Connection]:
        c = sqlite3.connect(self.path, timeout=15, isolation_level=None)
        c.row_factory = sqlite3.Row
        c.execute("PRAGMA foreign_keys=ON")
        c.execute("PRAGMA busy_timeout=15000")
        try:
            yield c
        finally:
            c.close()

    @contextmanager
    def tx(self) -> Iterator[sqlite3.Connection]:
        with self._conn() as c:
            c.execute("BEGIN IMMEDIATE")
            try:
                yield c
                c.execute("COMMIT")
            except BaseException:
                c.execute("ROLLBACK")
                raise

    # ---------------------------------------------------------------- issues / workflows
    def create_workflow(self, title: str, body: str, repo: str, *, source: str = "manual",
                        options: dict | None = None, budget: dict | None = None) -> int:
        ts = _now()
        with self.tx() as c:
            issue_id = c.execute(
                "INSERT INTO issues(title, body, source, created_at) VALUES (?,?,?,?)",
                (title, body, source, ts)).lastrowid
            wf_id = c.execute(
                "INSERT INTO workflows(issue_id, repo, state, options, budget, usage, created_at, updated_at)"
                " VALUES (?,?,?,?,?,?,?,?)",
                (issue_id, repo, WF.QUEUED, json.dumps(options or {}), json.dumps(budget or {}),
                 json.dumps({"candidate_evals": 0, "test_runs": 0, "llm_tokens": 0, "cost_usd": 0.0}),
                 ts, ts)).lastrowid
            self._event(c, wf_id, "info", None, "intake", f"Issue #{issue_id} received from {source}: {title}")
        return wf_id

    def get_workflow(self, wf_id: int) -> dict | None:
        with self._conn() as c:
            row = c.execute(
                "SELECT w.*, i.title AS issue_title, i.body AS issue_body, i.source AS issue_source"
                " FROM workflows w JOIN issues i ON i.id = w.issue_id WHERE w.id = ?", (wf_id,)).fetchone()
        return _row(row)

    def list_workflows(self, limit: int = 200) -> list[dict]:
        with self._conn() as c:
            rows = c.execute(
                "SELECT w.*, i.title AS issue_title, i.source AS issue_source,"
                " (SELECT COALESCE(SUM(attempts),0) FROM steps s WHERE s.workflow_id = w.id) AS total_attempts"
                " FROM workflows w JOIN issues i ON i.id = w.issue_id ORDER BY w.id DESC LIMIT ?",
                (limit,)).fetchall()
        return [_row(r) for r in rows]

    def update_workflow(self, wf_id: int, **fields: Any) -> None:
        if not fields:
            return
        fields["updated_at"] = _now()
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = [json.dumps(v) if k in JSON_COLUMNS else v for k, v in fields.items()]
        with self.tx() as c:
            c.execute(f"UPDATE workflows SET {cols} WHERE id = ?", (*vals, wf_id))

    def merge_data(self, wf_id: int, key: str, value: Any) -> None:
        with self.tx() as c:
            row = c.execute("SELECT data FROM workflows WHERE id = ?", (wf_id,)).fetchone()
            data = json.loads(row["data"])
            data[key] = value
            c.execute("UPDATE workflows SET data = ?, updated_at = ? WHERE id = ?",
                      (json.dumps(data), _now(), wf_id))

    def add_usage(self, wf_id: int, **deltas: float) -> dict:
        with self.tx() as c:
            row = c.execute("SELECT usage FROM workflows WHERE id = ?", (wf_id,)).fetchone()
            usage = json.loads(row["usage"])
            for k, v in deltas.items():
                usage[k] = usage.get(k, 0) + v
            c.execute("UPDATE workflows SET usage = ? WHERE id = ?", (json.dumps(usage), wf_id))
        return usage

    def transition(self, wf_id: int, new_state: str, *, actor: str = "orchestrator",
                   reason: str = "", **fields: Any) -> str:
        """Validated state change. Returns the previous state."""
        with self.tx() as c:
            row = c.execute("SELECT state FROM workflows WHERE id = ?", (wf_id,)).fetchone()
            if row is None:
                raise KeyError(f"workflow {wf_id} not found")
            old = row["state"]
            check_transition(old, new_state)
            ts = _now()
            fields["state"] = new_state
            fields["updated_at"] = ts
            if new_state == WF.RUNNING:
                fields.setdefault("started_at", ts)
                fields.setdefault("finished_at", None)
            elif old in (WF.RUNNING, WF.QUEUED):
                # finished_at = when automation stopped; human decision time lives in `approvals`.
                fields.setdefault("finished_at", ts)
            cols = ", ".join(f"{k} = ?" for k in fields)
            vals = [json.dumps(v) if k in JSON_COLUMNS else v for k, v in fields.items()]
            c.execute(f"UPDATE workflows SET {cols} WHERE id = ?", (*vals, wf_id))
            msg = f"State {old} -> {new_state}" + (f": {reason}" if reason else "")
            self._event(c, wf_id, "info", None, actor, msg, {"from": old, "to": new_state})
        return old

    def claim_next(self, owner: str) -> int | None:
        """Atomically move the oldest queued workflow to running and lease it to `owner`."""
        with self.tx() as c:
            row = c.execute("SELECT id FROM workflows WHERE state = ? AND cancel_requested = 0"
                            " ORDER BY id LIMIT 1", (WF.QUEUED,)).fetchone()
            if row is None:
                return None
            ts = _now()
            c.execute("UPDATE workflows SET state = ?, lease_owner = ?, heartbeat_at = ?, updated_at = ?,"
                      " started_at = COALESCE(started_at, ?), finished_at = NULL WHERE id = ?",
                      (WF.RUNNING, owner, ts, ts, ts, row["id"]))
            self._event(c, row["id"], "info", None, "orchestrator",
                        f"State queued -> running: claimed by {owner}", {"from": WF.QUEUED, "to": WF.RUNNING})
            return row["id"]

    def heartbeat(self, wf_id: int) -> None:
        with self._conn() as c:
            c.execute("UPDATE workflows SET heartbeat_at = ? WHERE id = ?", (_now(), wf_id))

    def request_cancel(self, wf_id: int) -> None:
        with self.tx() as c:
            c.execute("UPDATE workflows SET cancel_requested = 1, updated_at = ? WHERE id = ?", (_now(), wf_id))

    def cancel_requested(self, wf_id: int) -> bool:
        with self._conn() as c:
            row = c.execute("SELECT cancel_requested FROM workflows WHERE id = ?", (wf_id,)).fetchone()
        return bool(row and row["cancel_requested"])

    def recover_stale(self, stale_seconds: float = 60.0) -> list[int]:
        """Re-queue running workflows whose worker stopped heart-beating (crash/restart recovery)."""
        cutoff = _now() - stale_seconds
        recovered = []
        with self.tx() as c:
            rows = c.execute("SELECT id, lease_owner FROM workflows WHERE state = ?"
                             " AND (heartbeat_at IS NULL OR heartbeat_at < ?)", (WF.RUNNING, cutoff)).fetchall()
            for r in rows:
                ts = _now()
                c.execute("UPDATE steps SET status = ?, error = ? WHERE workflow_id = ? AND status = ?",
                          (STEP.PENDING, "interrupted: worker lease expired", r["id"], STEP.RUNNING))
                c.execute("UPDATE attempts SET status = 'interrupted', error = ?, finished_at = ?"
                          " WHERE workflow_id = ? AND finished_at IS NULL",
                          ("worker lease expired", ts, r["id"]))
                c.execute("UPDATE workflows SET state = ?, lease_owner = NULL, updated_at = ? WHERE id = ?",
                          (WF.QUEUED, ts, r["id"]))
                self._event(c, r["id"], "warning", None, "orchestrator",
                            f"Recovered stale workflow from {r['lease_owner']}; re-queued",
                            {"from": WF.RUNNING, "to": WF.QUEUED})
                recovered.append(r["id"])
        return recovered

    def counts_by_state(self) -> dict[str, int]:
        with self._conn() as c:
            rows = c.execute("SELECT state, COUNT(*) AS n FROM workflows GROUP BY state").fetchall()
        return {r["state"]: r["n"] for r in rows}

    # ---------------------------------------------------------------- steps / attempts
    def add_steps(self, wf_id: int, steps: list[tuple[str, str]]) -> None:
        with self.tx() as c:
            existing = {r["name"] for r in c.execute("SELECT name FROM steps WHERE workflow_id = ?", (wf_id,))}
            seq = c.execute("SELECT COALESCE(MAX(seq), -1) AS m FROM steps WHERE workflow_id = ?",
                            (wf_id,)).fetchone()["m"]
            for name, role in steps:
                if name in existing:
                    continue
                seq += 1
                c.execute("INSERT INTO steps(workflow_id, seq, name, role, status) VALUES (?,?,?,?,?)",
                          (wf_id, seq, name, role, STEP.PENDING))

    def get_steps(self, wf_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM steps WHERE workflow_id = ? ORDER BY seq", (wf_id,)).fetchall()
        return [_row(r) for r in rows]

    def get_step(self, wf_id: int, name: str) -> dict | None:
        with self._conn() as c:
            return _row(c.execute("SELECT * FROM steps WHERE workflow_id = ? AND name = ?",
                                  (wf_id, name)).fetchone())

    def update_step(self, step_id: int, **fields: Any) -> None:
        cols = ", ".join(f"{k} = ?" for k in fields)
        vals = [json.dumps(v) if k in JSON_COLUMNS else v for k, v in fields.items()]
        with self.tx() as c:
            c.execute(f"UPDATE steps SET {cols} WHERE id = ?", (*vals, step_id))

    def start_attempt(self, wf_id: int, step_id: int, n: int, iteration: int | None = None) -> int:
        with self.tx() as c:
            c.execute("UPDATE steps SET attempts = ? WHERE id = ?", (n, step_id))
            return c.execute("INSERT INTO attempts(step_id, workflow_id, n, status, started_at, iteration)"
                             " VALUES (?,?,?,?,?,?)", (step_id, wf_id, n, "running", _now(), iteration)).lastrowid

    def finish_attempt(self, attempt_id: int, status: str, error: str | None = None,
                       detail: dict | None = None) -> None:
        with self.tx() as c:
            c.execute("UPDATE attempts SET status = ?, error = ?, detail = ?, finished_at = ? WHERE id = ?",
                      (status, error, json.dumps(detail or {}), _now(), attempt_id))

    def get_attempts(self, wf_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT a.*, s.name AS step FROM attempts a JOIN steps s ON s.id = a.step_id"
                             " WHERE a.workflow_id = ? ORDER BY a.id", (wf_id,)).fetchall()
        return [_row(r) for r in rows]

    # ---------------------------------------------------------------- implementation-loop iterations
    def open_iteration(self, wf_id: int) -> dict:
        """Return the open iteration, or start iteration n+1 in phase CODING."""
        with self.tx() as c:
            row = c.execute("SELECT * FROM iterations WHERE workflow_id = ? ORDER BY n DESC LIMIT 1",
                            (wf_id,)).fetchone()
            if row is not None and row["status"] in ITERATION_OPEN:
                return _row(row)
            n = (row["n"] if row else 0) + 1
            c.execute("INSERT INTO iterations(workflow_id, n, status, started_at) VALUES (?,?,?,?)",
                      (wf_id, n, IT.CODING, _now()))
            self._event(c, wf_id, "info", "coding", "orchestrator", f"Iteration {n} started",
                        {"iteration": n})
            return _row(c.execute("SELECT * FROM iterations WHERE workflow_id = ? AND n = ?", (wf_id, n)).fetchone())

    def current_iteration(self, wf_id: int) -> dict | None:
        with self._conn() as c:
            return _row(c.execute("SELECT * FROM iterations WHERE workflow_id = ? ORDER BY n DESC LIMIT 1",
                                  (wf_id,)).fetchone())

    def get_iterations(self, wf_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM iterations WHERE workflow_id = ? ORDER BY n", (wf_id,)).fetchall()
        return [_row(r) for r in rows]

    def update_iteration(self, wf_id: int, n: int, *, status: str | None = None, **fields: Any) -> None:
        """Update an iteration; a status change is validated against ITERATION_TRANSITIONS."""
        with self.tx() as c:
            row = c.execute("SELECT status FROM iterations WHERE workflow_id = ? AND n = ?", (wf_id, n)).fetchone()
            if row is None:
                raise KeyError(f"iteration {n} of workflow {wf_id} not found")
            if status is not None and status != row["status"]:
                check_iteration_transition(row["status"], status)
                fields["status"] = status
                if status not in ITERATION_OPEN:
                    fields.setdefault("finished_at", _now())
                self._event(c, wf_id, "info", None, "orchestrator",
                            f"Iteration {n}: {row['status']} -> {status}", {"iteration": n, "to": status})
            if not fields:
                return
            cols = ", ".join(f'"{k}" = ?' for k in fields)
            vals = [json.dumps(v) if k in JSON_COLUMNS else v for k, v in fields.items()]
            c.execute(f"UPDATE iterations SET {cols} WHERE workflow_id = ? AND n = ?", (*vals, wf_id, n))

    def close_open_iteration(self, wf_id: int, status: str, reason: str) -> None:
        it = self.current_iteration(wf_id)
        if it and it["status"] in ITERATION_OPEN:
            self.update_iteration(wf_id, it["n"], status=status, decision=status, decision_reason=reason)

    def add_review(self, wf_id: int, iteration: int, review: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO reviews(workflow_id, iteration, reviewer, adapter, provider, model, simulated,"
                      " verdict, summary, findings, input_tokens, output_tokens, cost_usd, seconds, error, created_at,"
                      " execution, model_digest, llm_call_id, checks, analysis)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (wf_id, iteration, review["reviewer"], review["adapter"], review["provider"], review["model"],
                       int(review["simulated"]), review["verdict"], review.get("summary", ""),
                       json.dumps(review.get("findings", [])), review.get("input_tokens", 0),
                       review.get("output_tokens", 0), review.get("cost_usd", 0.0), review.get("seconds", 0.0),
                       review.get("error"), _now(),
                       review.get("execution") or ("simulated" if review["simulated"] else "real"),
                       review.get("model_digest"), review.get("llm_call_id"), json.dumps(review.get("checks", [])),
                       review.get("analysis", "")))

    def get_reviews(self, wf_id: int, iteration: int | None = None) -> list[dict]:
        q, args = "SELECT * FROM reviews WHERE workflow_id = ?", [wf_id]
        if iteration is not None:
            q, args = q + " AND iteration = ?", args + [iteration]
        with self._conn() as c:
            rows = c.execute(q + " ORDER BY id", args).fetchall()
        out = [_row(r) for r in rows]
        for r in out:
            r["simulated"] = bool(r["simulated"])
        return out

    # ---------------------------------------------------------------- agent observability
    _LLM_COLS = ["workflow_id", "iteration", "step", "role", "provider", "execution", "model", "served_model",
                 "model_digest", "model_details", "base_url", "purpose", "status", "error", "started_at", "finished_at",
                 "runtime_seconds", "load_seconds", "eval_seconds", "input_tokens", "output_tokens", "done_reason",
                 "prompt_sha256", "response_sha256", "prompt", "response"]

    def add_llm_call(self, rec: dict) -> int:
        cols = [c for c in self._LLM_COLS if c in rec]
        vals = [json.dumps(rec[c]) if c in JSON_COLUMNS else rec[c] for c in cols]
        with self._conn() as c:
            return c.execute(f"INSERT INTO llm_calls({', '.join(cols)}) VALUES ({', '.join('?' * len(cols))})",
                             vals).lastrowid

    def get_llm_calls(self, wf_id: int, with_text: bool = False) -> list[dict]:
        cols = "*" if with_text else ", ".join(c for c in ["id", *self._LLM_COLS] if c not in ("prompt", "response"))
        with self._conn() as c:
            rows = c.execute(f"SELECT {cols} FROM llm_calls WHERE workflow_id = ? ORDER BY id", (wf_id,)).fetchall()
        return [_row(r) for r in rows]

    def get_llm_call(self, call_id: int) -> dict | None:
        with self._conn() as c:
            return _row(c.execute("SELECT * FROM llm_calls WHERE id = ?", (call_id,)).fetchone())

    def add_tool_call(self, wf_id: int, iteration: int | None, step: str, role: str, tool: str, args: dict,
                      ok: bool, result: str, seconds: float, llm_call_id: int | None = None) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO tool_calls(workflow_id, iteration, step, role, llm_call_id, tool, args, ok, result,"
                      " seconds, created_at) VALUES (?,?,?,?,?,?,?,?,?,?,?)",
                      (wf_id, iteration, step, role, llm_call_id, tool, json.dumps(args), int(ok), result[:4000],
                       seconds, _now()))

    def get_tool_calls(self, wf_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM tool_calls WHERE workflow_id = ? ORDER BY id", (wf_id,)).fetchall()
        out = [_row(r) for r in rows]
        for r in out:
            r["ok"] = bool(r["ok"])
        return out

    def add_test_run(self, wf_id: int, rec: dict) -> None:
        with self._conn() as c:
            c.execute("INSERT INTO test_runs(workflow_id, iteration, step, purpose, command, commit_sha, exit_code,"
                      " timed_out, passed, failed, errors, summary, seconds, output_tail, created_at)"
                      " VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                      (wf_id, rec.get("iteration"), rec["step"], rec["purpose"], rec["command"], rec.get("commit_sha"),
                       rec["exit_code"], int(rec.get("timed_out", False)), rec.get("passed"), rec.get("failed"),
                       rec.get("errors"), rec.get("summary"), rec["seconds"], rec.get("output_tail"), _now()))

    def get_test_runs(self, wf_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM test_runs WHERE workflow_id = ? ORDER BY id", (wf_id,)).fetchall()
        out = [_row(r) for r in rows]
        for r in out:
            r["timed_out"] = bool(r["timed_out"])
        return out

    # ---------------------------------------------------------------- audit log / evidence / approvals
    @staticmethod
    def _event(c: sqlite3.Connection, wf_id: int, level: str, step: str | None, actor: str,
               message: str, data: dict | None = None) -> None:
        c.execute("INSERT INTO events(workflow_id, ts, level, step, actor, message, data) VALUES (?,?,?,?,?,?,?)",
                  (wf_id, _now(), level, step, actor, message, json.dumps(data or {})))

    def log(self, wf_id: int, message: str, *, level: str = "info", step: str | None = None,
            actor: str = "orchestrator", data: dict | None = None) -> None:
        with self._conn() as c:
            self._event(c, wf_id, level, step, actor, message, data)

    def get_events(self, wf_id: int, after_id: int = 0) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM events WHERE workflow_id = ? AND id > ? ORDER BY id",
                             (wf_id, after_id)).fetchall()
        return [_row(r) for r in rows]

    def add_evidence(self, wf_id: int, step: str, kind: str, title: str, content: str,
                     data: dict | None = None) -> int:
        with self._conn() as c:
            return c.execute("INSERT INTO evidence(workflow_id, step, kind, title, content, data, created_at)"
                             " VALUES (?,?,?,?,?,?,?)",
                             (wf_id, step, kind, title, content, json.dumps(data or {}), _now())).lastrowid

    def get_evidence(self, wf_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM evidence WHERE workflow_id = ? ORDER BY id", (wf_id,)).fetchall()
        return [_row(r) for r in rows]

    def record_approval(self, wf_id: int, decision: str, actor: str, comment: str = "") -> None:
        with self._conn() as c:
            c.execute("INSERT INTO approvals(workflow_id, decision, actor, comment, ts) VALUES (?,?,?,?,?)",
                      (wf_id, decision, actor, comment, _now()))

    def get_approvals(self, wf_id: int) -> list[dict]:
        with self._conn() as c:
            rows = c.execute("SELECT * FROM approvals WHERE workflow_id = ? ORDER BY id", (wf_id,)).fetchall()
        return [_row(r) for r in rows]
