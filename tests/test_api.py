from fastapi.testclient import TestClient

from systemfactory.api import create_app
from systemfactory.orchestrator import Worker


def test_api_end_to_end(settings):
    app = create_app(settings, start_worker=False)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        h = client.get("/api/health").json()
        rt = h["runtime"]
        assert rt["mode"] == "simulated" and rt["available_modes"] == ["simulated", "ollama"]
        assert rt["claude_reviewer_available"] is False and rt["paid_inference_allowed"] is False
        assert set(rt["models"]) == {"coder", "reviewer_a", "reviewer_b"}
        assert rt["ollama"]["reachable"] is False  # tests point Ollama at a closed port (conftest)
        assert "System Factory" in client.get("/").text

        wf_id = client.post("/api/samples/shipping", json={}).json()["workflow_id"]
        assert client.post("/api/issues", json={"title": ""}).status_code == 422
        assert client.post("/api/samples/nope").status_code == 404

        Worker(app.state.orch, concurrency=1).run_until_idle()
        d = client.get(f"/api/workflows/{wf_id}").json()
        assert d["workflow"]["state"] == "awaiting_approval"
        assert any(e["kind"] == "diff" for e in d["evidence"])
        assert d["policies"]["coding"]["writes"] == ["<plan.target_files only>"]

        assert client.post(f"/api/workflows/{wf_id}/retry").status_code == 409
        assert client.post(f"/api/workflows/{wf_id}/approve", json={"actor": ""}).status_code == 422
        r = client.post(f"/api/workflows/{wf_id}/approve", json={"actor": "Demo Approver", "comment": "ok"})
        assert r.json() == {"state": "approved"}
        assert client.post(f"/api/workflows/{wf_id}/cancel").status_code == 409
        assert client.get("/api/workflows/9999").status_code == 404

        wf2 = client.post("/api/issues", json={"title": "Question about cart?", "body": "how do i use the cart"}).json()["workflow_id"]
        assert client.post(f"/api/workflows/{wf2}/cancel").json() == {"state": "cancelled"}
        listed = {w["id"]: w["state"] for w in client.get("/api/workflows").json()}
        assert listed == {wf_id: "approved", wf2: "cancelled"}
