"""X-Forwarded-For is believed only from configured reverse proxies (#2).

Without that, anyone who reaches the service directly -- the Docker quick start publishes
the port -- sends a different header on every request, gets a fresh visitor hash each time
and inflates the impressions past the per-day deduplication.
"""
import pytest


def _login(client):
    from app import auth, db
    with db.session() as conn:
        auth.create_user(conn, "tester", "correct horse battery")
    r = client.post("/login", data={"username": "tester", "password": "correct horse battery"},
                    follow_redirects=False)
    assert r.status_code == 303, r.text
    return client


def _impressions() -> int:
    from app import db
    with db.session() as conn:
        return conn.execute(
            "SELECT COUNT(*) AS n FROM events WHERE kind='impression'").fetchone()["n"]


def _client(peer: str):
    from fastapi.testclient import TestClient
    from app.main import create_app
    return TestClient(create_app(), client=(peer, 50000))


def test_a_spoofed_forwarded_for_does_not_inflate_impressions(admin_client, make_ad):
    slug = make_ad(admin_client)
    for i in range(5):
        r = admin_client.post("/api/impression", content=slug,
                              headers={"x-forwarded-for": f"198.51.100.{i}"})
        assert r.status_code == 204
    assert _impressions() == 1, "an untrusted peer must not choose its own client address"


def test_a_spoofed_forwarded_for_does_not_inflate_clicks(admin_client, make_ad):
    slug = make_ad(admin_client)
    for i in range(3):
        admin_client.get(f"/c/{slug}", headers={"x-forwarded-for": f"198.51.100.{i}"},
                         follow_redirects=False)
    from app import db
    with db.session() as conn:
        n = conn.execute("SELECT COUNT(*) AS n FROM events WHERE kind='click'").fetchone()["n"]
    assert n == 1


def test_a_configured_proxy_passes_the_real_client_address(app_env, monkeypatch, make_ad):
    monkeypatch.setenv("ADYARD_TRUSTED_PROXIES", "10.0.0.0/8, 192.0.2.1")
    client = _login(_client("10.1.2.3"))
    slug = make_ad(client)
    for visitor in ("198.51.100.1", "198.51.100.2", "198.51.100.2"):
        client.post("/api/impression", content=slug, headers={"x-forwarded-for": visitor})
    assert _impressions() == 2, "two real visitors behind the proxy, one of them twice"


def test_a_peer_outside_the_configured_proxies_is_not_believed(app_env, monkeypatch, make_ad):
    monkeypatch.setenv("ADYARD_TRUSTED_PROXIES", "10.0.0.0/8")
    client = _login(_client("198.51.100.200"))
    slug = make_ad(client)
    for visitor in ("198.51.100.1", "198.51.100.2"):
        client.post("/api/impression", content=slug, headers={"x-forwarded-for": visitor})
    assert _impressions() == 1


def test_the_default_trusts_only_loopback(app_env, make_ad):
    """A proxy on the same machine (the systemd setup) keeps working without configuration."""
    client = _login(_client("127.0.0.1"))
    slug = make_ad(client)
    for visitor in ("198.51.100.1", "198.51.100.2"):
        client.post("/api/impression", content=slug, headers={"x-forwarded-for": visitor})
    assert _impressions() == 2


def test_an_empty_setting_trusts_nobody(app_env, monkeypatch, make_ad):
    monkeypatch.setenv("ADYARD_TRUSTED_PROXIES", "")
    client = _login(_client("127.0.0.1"))
    slug = make_ad(client)
    for visitor in ("198.51.100.1", "198.51.100.2"):
        client.post("/api/impression", content=slug, headers={"x-forwarded-for": visitor})
    assert _impressions() == 1


@pytest.mark.parametrize("value", ["10.0.0.0/33", "proxy.example.com", "10.0.0.1;10.0.0.2"])
def test_a_malformed_proxy_entry_refuses_to_start(app_env, monkeypatch, value):
    """A typo must not silently fall back to some other trust setting."""
    monkeypatch.setenv("ADYARD_TRUSTED_PROXIES", value)
    from app.main import create_app
    with pytest.raises(SystemExit) as exc:
        create_app()
    assert "ADYARD_TRUSTED_PROXIES" in str(exc.value)
