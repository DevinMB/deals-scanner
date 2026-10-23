from fastapi.testclient import TestClient

from dealwatch import db


def test_migrate_is_idempotent(tmp_path):
    conn = db.connect(tmp_path / "t.db")
    assert db.migrate(conn) == ["001_init.sql"]
    assert db.migrate(conn) == []
    tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
    assert {"watches", "settings", "listings", "price_samples", "alerts", "runs"} <= tables
    assert conn.execute("PRAGMA journal_mode").fetchone()[0] == "wal"


def test_healthz_and_empty_dashboard(tmp_path, monkeypatch):
    monkeypatch.setenv("DATA_DIR", str(tmp_path))
    from dealwatch.app import app

    with TestClient(app) as client:
        assert client.get("/healthz").json() == {"status": "ok"}
        r = client.get("/")
        assert r.status_code == 200
        assert "No watches yet" in r.text
