"""Password guessing is slowed down: per client address and per account.

Without a limit, every wrong password cost an attacker one argon2 check and nothing else.
"""
import pytest

RIGHT = "correct horse battery"


@pytest.fixture()
def clock():
    """A controllable clock for the login throttle."""
    class Clock:
        now = 1000.0

        def __call__(self):
            return self.now
    return Clock()


def _client(clock, peer="testclient"):
    from fastapi.testclient import TestClient
    from app import auth, db
    from app.main import create_app

    app = create_app()
    throttle = getattr(app.state, "login_throttle", None)
    if throttle is not None:  # (absent before the throttle existed: the tests fail on behaviour)
        throttle.clock = clock
    with db.session() as conn:
        if not conn.execute("SELECT 1 FROM users WHERE username='tester'").fetchone():
            auth.create_user(conn, "tester", RIGHT)
    return TestClient(app, client=(peer, 50000))


def _login(client, password, username="tester", **headers):
    return client.post("/login", data={"username": username, "password": password},
                       headers=headers, follow_redirects=False)


def test_five_wrong_passwords_then_429_even_for_the_right_one(app_env, clock):
    client = _client(clock)
    for _ in range(5):
        assert _login(client, "wrong").status_code == 401
    r = _login(client, "wrong")
    assert r.status_code == 429
    assert int(r.headers["retry-after"]) == 30
    assert "Too many failed logins" in r.text
    # While locked, not even the right password is checked.
    assert _login(client, RIGHT).status_code == 429


def test_the_right_password_works_after_the_window_and_resets(app_env, clock):
    client = _client(clock)
    for _ in range(5):
        _login(client, "wrong")
    assert _login(client, RIGHT).status_code == 429
    clock.now += 31
    assert _login(client, RIGHT).headers["location"] == "/"
    client.get("/logout")
    # A success wipes the slate: five fresh attempts before the next lock.
    for _ in range(5):
        assert _login(client, "wrong").status_code == 401
    assert _login(client, "wrong").status_code == 429


def test_the_wait_doubles_with_every_further_failure(app_env, clock):
    client = _client(clock)
    for _ in range(5):
        _login(client, "wrong")
    assert int(_login(client, "wrong").headers["retry-after"]) == 30
    clock.now += 31
    assert _login(client, "wrong").status_code == 401   # the sixth failure
    assert int(_login(client, "wrong").headers["retry-after"]) == 60
    clock.now += 61
    _login(client, "wrong")
    assert int(_login(client, "wrong").headers["retry-after"]) == 120


def test_rotating_addresses_does_not_help_against_one_account(app_env, monkeypatch, clock):
    """Behind a trusted proxy every attempt can come from a new address -- the per-account
    counter still slows the guessing down."""
    monkeypatch.setenv("ADYARD_TRUSTED_PROXIES", "10.0.0.0/8")
    client = _client(clock, peer="10.0.0.5")
    for i in range(5):
        assert _login(client, "wrong", **{"x-forwarded-for": f"198.51.100.{i}"}).status_code == 401
    r = _login(client, RIGHT, **{"x-forwarded-for": "198.51.100.99"})
    assert r.status_code == 429


def test_rotating_usernames_does_not_help_from_one_address(app_env, clock):
    client = _client(clock)
    for i in range(5):
        assert _login(client, "wrong", username=f"guess{i}").status_code == 401
    assert _login(client, "wrong", username="guess-new").status_code == 429


def test_a_forged_header_from_an_untrusted_peer_does_not_reset_the_address(app_env, clock):
    client = _client(clock)
    for i in range(5):
        _login(client, "wrong", username=f"guess{i}", **{"x-forwarded-for": f"198.51.100.{i}"})
    r = _login(client, "wrong", username="another", **{"x-forwarded-for": "198.51.100.200"})
    assert r.status_code == 429


def test_others_can_still_log_in(app_env, monkeypatch, clock):
    """A lock on one address and one account leaves everyone else alone."""
    monkeypatch.setenv("ADYARD_TRUSTED_PROXIES", "10.0.0.0/8")
    client = _client(clock, peer="10.0.0.5")
    from app import auth, db
    with db.session() as conn:
        auth.create_user(conn, "other", "the other password")
    for _ in range(5):
        _login(client, "wrong", **{"x-forwarded-for": "198.51.100.1"})
    r = _login(client, "the other password", username="other",
               **{"x-forwarded-for": "198.51.100.2"})
    assert r.headers["location"] == "/"


def test_the_throttle_forgets_and_stays_bounded(clock):
    """Memory must not grow without limit when an attacker invents keys."""
    from app.ratelimit import LoginThrottle
    throttle = LoginThrottle(clock=clock, max_entries=100)
    for i in range(1000):
        throttle.failure(f"203.0.113.{i % 250}", f"user{i}")
    assert len(throttle._entries) <= 100
    clock.now += 24 * 3600 + 1
    throttle.failure("203.0.113.1", "fresh")
    assert len(throttle._entries) == 2, "idle entries are dropped"
