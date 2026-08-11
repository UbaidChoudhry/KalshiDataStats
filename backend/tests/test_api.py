import time

from fastapi.testclient import TestClient


def test_demo_sync_and_history_endpoints(monkeypatch, tmp_path):
    monkeypatch.setenv("KALSHI_DATA_DIR", str(tmp_path / "outside-data"))
    monkeypatch.setenv("KALSHI_SYNC_MODE", "demo")
    from backend.app.main import app
    with TestClient(app) as client:
        assert client.get("/api/v1/health").json() == {"status": "ok"}
        assert client.get("/api/v1/data/status").json()["has_data"] is False
        run = client.post("/api/v1/sync-runs", json={"window": "all"}).json()
        assert run["status"] in {"queued", "running"}
        for _ in range(50):
            current = client.get("/api/v1/sync-runs/current").json()
            if current["status"] != "running":
                break
            time.sleep(.01)
        assert current["status"] == "completed"
        assert client.get("/api/v1/history/summary?window=all&threshold=80").json()["wrong_markets"] == 3
        bands = client.get("/api/v1/history/bands?window=all&threshold=80").json()["items"]
        filtered = client.get(f"/api/v1/history/misses?window=all&threshold=80&min_percent={bands[0]['min_percent']}&max_percent={bands[0]['max_percent']}").json()
        assert filtered["total"] >= 1
