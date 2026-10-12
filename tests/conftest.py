import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))


@pytest.fixture()
def app_env(tmp_path, monkeypatch):
    """Point the app at a throwaway directory that looks like a mounted data disk."""
    base = tmp_path / "maxads"
    for sub in ("data/raw", "data/media", "data/db", "data/logs", "venv"):
        (base / sub).mkdir(parents=True, exist_ok=True)
    (base / ".mounted").write_text("test")

    monkeypatch.setenv("MAXADS_BASE", str(base))
    monkeypatch.setenv("MAXADS_SECRET_KEY", "test-secret-key")
    monkeypatch.setenv("MAXADS_COUNT_SALT", "test-salt")
    monkeypatch.setenv("MAXADS_PUBLIC_URL", "https://ads.example.com")
    monkeypatch.delenv("MAXADS_REQUIRE_MOUNT", raising=False)
    monkeypatch.delenv("MAXADS_ADMIN_PASSWORD", raising=False)

    # config caches paths at import time, so drop the modules and re-import.
    for name in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[name]
    return base


@pytest.fixture()
def client(app_env):
    from fastapi.testclient import TestClient
    from app.main import create_app
    return TestClient(create_app())


@pytest.fixture()
def admin_client(client, app_env):
    """A client already logged in as a regular user."""
    from app import auth, db
    with db.session() as conn:
        auth.create_user(conn, "tester", "correct horse battery")
    r = client.post("/login", data={"username": "tester", "password": "correct horse battery"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return client


@pytest.fixture()
def make_ad(app_env):
    """Upload a small image through `client` and run the worker on it; returns the slug."""
    def make(client, title="Banner", target="https://example.com/x", size=(300, 100)):
        from PIL import Image
        from app import db
        from app.worker import claim_job, process

        src = Path(app_env) / f"{title}.png"
        Image.new("RGB", size, (10, 90, 200)).save(src)
        with src.open("rb") as fh:
            r = client.post("/ads", data={"title": title, "target_url": target},
                            files={"file": (f"{title}.png", fh, "image/png")},
                            follow_redirects=False)
        assert r.status_code == 303, r.text
        with db.session() as conn:
            process(conn, claim_job(conn))
            return conn.execute("SELECT slug FROM ads ORDER BY id DESC LIMIT 1").fetchone()["slug"]
    return make
