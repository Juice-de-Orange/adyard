"""Removing accounts from the dashboard (#3)."""
import pytest


def _id(username):
    from app import db
    with db.session() as conn:
        row = conn.execute("SELECT id FROM users WHERE username = ?", (username,)).fetchone()
    return row["id"] if row else None


def test_remove_an_account_hands_its_banners_to_the_remover(admin_client, app_env, make_ad):
    from fastapi.testclient import TestClient
    from app import db

    admin_client.post("/users", data={"username": "bob", "password": "bobs long password"})
    # bob logs in elsewhere, uploads a banner and brings in carol.
    bob = TestClient(admin_client.app)
    bob.post("/login", data={"username": "bob", "password": "bobs long password"})
    slug = make_ad(bob, title="Bobs banner")
    bob.post("/users", data={"username": "carol", "password": "carols long password"})
    with db.session() as conn:
        assert conn.execute("SELECT created_by FROM ads WHERE slug=?", (slug,)).fetchone()[0] \
            == _id("bob")

    r = admin_client.post(f"/users/{_id('bob')}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert _id("bob") is None
    with db.session() as conn:
        ad = conn.execute("SELECT created_by, active FROM ads WHERE slug=?", (slug,)).fetchone()
        carol = conn.execute("SELECT created_by FROM users WHERE username='carol'").fetchone()
    assert ad["created_by"] == _id("tester"), "the banner moves to whoever removed bob"
    assert ad["active"] == 1, "and keeps running"
    assert carol["created_by"] == _id("tester")
    assert admin_client.get(f"/m/{slug}").status_code == 200
    # bob's open session ends with the account.
    assert bob.get("/", follow_redirects=False).headers["location"] == "/login"


def test_you_cannot_remove_yourself(admin_client):
    r = admin_client.post(f"/users/{_id('tester')}/delete")
    assert r.status_code == 400
    assert "your own account" in r.text
    assert _id("tester") is not None


def test_the_last_account_cannot_be_removed(admin_client):
    from app import auth, db
    admin_client.post(f"/users/{_id('admin')}/delete", follow_redirects=False)
    assert _id("admin") is None
    with db.session() as conn:
        assert conn.execute("SELECT COUNT(*) FROM users").fetchone()[0] == 1
        # Not reachable through the dashboard (that would be removing yourself),
        # but the rule holds on its own.
        with pytest.raises(auth.AccountError, match="last account"):
            auth.delete_user(conn, _id("tester"), acting_id=_id("tester") + 100)
    assert _id("tester") is not None


def test_an_unknown_account(admin_client):
    r = admin_client.post("/users/9999/delete")
    assert r.status_code == 404
    assert 'role="alert"' in r.text


def test_removing_needs_a_login(client):
    r = client.post(f"/users/{_id('admin')}/delete", follow_redirects=False)
    assert r.headers["location"] == "/login"
    assert _id("admin") is not None


def test_the_dashboard_offers_removal_for_others_only(admin_client):
    admin_client.post("/users", data={"username": "dave", "password": "daves long password"})
    page = admin_client.get("/").text
    assert f'action="/users/{_id("dave")}/delete"' in page
    assert f'action="/users/{_id("admin")}/delete"' in page
    assert f'action="/users/{_id("tester")}/delete"' not in page
    assert 'data-confirm="Remove dave from the crew?' in page
