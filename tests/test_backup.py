"""Backup and restore (#4): a consistent copy while the service runs, back into an empty
or a broken data directory."""
import io
import shutil
import sqlite3
import sys
import tarfile
from pathlib import Path

import pytest


def _reload(monkeypatch, base: Path):
    monkeypatch.setenv("ADYARD_BASE", str(base))
    for name in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[name]


def _counts():
    from app import db
    with db.session() as conn:
        return {t: conn.execute(f"SELECT COUNT(*) FROM {t}").fetchone()[0]
                for t in ("users", "ads", "events", "sites")}


def _fresh_base(tmp_path: Path, name: str) -> Path:
    base = tmp_path / name
    base.mkdir()
    (base / ".mounted").write_text("test")
    return base


@pytest.fixture()
def populated(admin_client, app_env, make_ad):
    """Two banners with an impression each; returns their slugs."""
    slugs = [make_ad(admin_client, title="One"), make_ad(admin_client, title="Two")]
    for slug in slugs:
        admin_client.post("/api/impression", content=slug,
                          headers={"referer": "https://blog.example.org/post"})
    return slugs


def test_backup_while_running_restores_into_an_empty_directory(
        populated, app_env, tmp_path, monkeypatch):
    from app import backup, config

    # A long-lived connection, like the running worker's, keeps the newest
    # commits in adyard.db-wal: a plain file copy of adyard.db misses them.
    holder = sqlite3.connect(config.DB_PATH)
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("INSERT INTO events(ad_id, kind, day, visitor) "
                   "VALUES (1, 'impression', date('now'), 'only-in-the-wal')")
    holder.commit()
    naive = tmp_path / "naive.db"
    shutil.copy(config.DB_PATH, naive)
    assert sqlite3.connect(naive).execute(
        "SELECT COUNT(*) FROM events WHERE visitor='only-in-the-wal'").fetchone()[0] == 0, \
        "precondition: the newest row is only in the WAL"

    before = _counts()
    secret = (app_env / ".count_salt").read_text() if (app_env / ".count_salt").exists() else None
    assert backup.main([str(tmp_path / "backups")]) == 0
    holder.close()
    [archive] = (tmp_path / "backups").glob("adyard-backup-*.tar")
    assert archive.stat().st_mode & 0o777 == 0o600, "the archive holds secrets and hashes"

    new_base = _fresh_base(tmp_path, "restored")
    _reload(monkeypatch, new_base)
    from app import restore
    assert restore.main([str(archive)]) == 0

    from fastapi.testclient import TestClient
    from app.main import create_app
    client = TestClient(create_app())
    assert _counts() == before
    from app import db
    with db.session() as conn:
        assert conn.execute(
            "SELECT COUNT(*) FROM events WHERE visitor='only-in-the-wal'").fetchone()[0] == 1
        slugs = [r["slug"] for r in conn.execute("SELECT slug FROM ads")]
    assert set(slugs) == set(populated)
    for slug in slugs:
        assert client.get(f"/m/{slug}").status_code == 200
        assert client.get(f"/m/{slug}?thumb=1").status_code == 200
    # The accounts come back with their passwords.
    r = client.post("/login", data={"username": "tester", "password": "correct horse battery"},
                    follow_redirects=False)
    assert r.headers["location"] == "/"
    if secret is not None:
        assert (new_base / ".count_salt").read_text() == secret


def test_backup_to_stdout_and_restore_from_stdin(populated, app_env, tmp_path, monkeypatch):
    from app import backup

    out = io.BytesIO()
    with monkeypatch.context() as m:
        m.setattr(sys, "stdout", io.TextIOWrapper(out))
        assert backup.main(["-"]) == 0
        data = out.getvalue()
    names = tarfile.open(fileobj=io.BytesIO(data)).getnames()
    assert "MANIFEST.json" in names and "adyard.db" in names
    assert any(n.startswith("data/media/") for n in names)
    assert not any(n.startswith("data/logs") or n.startswith("data/db") for n in names)

    new_base = _fresh_base(tmp_path, "from-stdin")
    _reload(monkeypatch, new_base)
    monkeypatch.setattr(sys, "stdin", io.TextIOWrapper(io.BytesIO(data)))
    from app import restore
    assert restore.main(["-"]) == 0
    assert _counts()["ads"] == 2


def test_restore_over_existing_data_moves_it_aside(populated, app_env, admin_client,
                                                   make_ad, tmp_path, monkeypatch):
    from app import backup, config
    backup.main([str(tmp_path / "b")])
    [archive] = (tmp_path / "b").glob("*.tar")

    # Things go wrong after the backup: a third banner, then the database breaks.
    make_ad(admin_client, title="Three")
    admin_client.close()
    config.DB_PATH.write_bytes(b"this is not a database")

    _reload(monkeypatch, app_env)
    from app import restore
    assert restore.main([str(archive)]) == 0
    assert _counts()["ads"] == 2

    [aside] = app_env.glob("pre-restore-*")
    assert (aside / "data/db/adyard.db").read_bytes() == b"this is not a database"
    assert len(list((aside / "data/media").glob("*_thumb.webp"))) == 3, "nothing is deleted"
    assert not list(app_env.glob(".restore-*")), "the staging folder is cleaned up"


def test_restore_refuses_while_the_database_is_open(populated, app_env, tmp_path, monkeypatch):
    from app import backup, config
    backup.main([str(tmp_path / "b")])
    [archive] = (tmp_path / "b").glob("*.tar")
    holder = sqlite3.connect(config.DB_PATH)  # like a running worker
    holder.execute("PRAGMA journal_mode=WAL")
    holder.execute("INSERT INTO events(ad_id, kind, day, visitor) "
                   "VALUES (1, 'click', date('now'), 'x')")
    holder.commit()
    try:
        _reload(monkeypatch, app_env)
        from app import restore
        with pytest.raises(SystemExit) as exc:
            restore.main([str(archive)])
        assert "Stop both" in str(exc.value)
        assert not list(app_env.glob("pre-restore-*")), "nothing was touched"
    finally:
        holder.close()


def test_restore_never_unpacks_outside_the_data_directory(app_env, tmp_path, monkeypatch):
    evil = tmp_path / "evil.tar"
    with tarfile.open(evil, "w") as tar:
        payload = tmp_path / "payload"
        payload.write_text("x")
        tar.add(payload, arcname="MANIFEST.json")
        tar.add(payload, arcname="data/media/../../../escaped")
    _reload(monkeypatch, app_env)
    from app import restore
    with pytest.raises(SystemExit) as exc:
        restore.main([str(evil)])
    assert "unexpected entry" in str(exc.value)
    assert not (tmp_path / "escaped").exists()


def test_restore_refuses_a_damaged_backup_and_keeps_the_data(populated, app_env, tmp_path,
                                                            monkeypatch):
    from app import backup
    backup.main([str(tmp_path / "b")])
    [archive] = (tmp_path / "b").glob("*.tar")
    cut = tmp_path / "cut.tar"
    cut.write_bytes(archive.read_bytes()[: archive.stat().st_size // 3])

    _reload(monkeypatch, app_env)
    from app import restore
    with pytest.raises(SystemExit) as exc:
        restore.main([str(cut)])
    assert "cannot read the backup" in str(exc.value)
    assert not list(app_env.glob("pre-restore-*")), "the current data stays where it is"
    assert _counts()["ads"] == 2
