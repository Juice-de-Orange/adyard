"""The rename from maxads to adyard must not cost an existing installation its
configuration or its data: old MAXADS_ names keep working (with a warning), and
an existing maxads.db is used instead of a fresh, empty adyard.db."""
import io
import json
import logging
import sqlite3
import sys
import tarfile
from pathlib import Path

import pytest


def _restart():
    """Re-import the app, as a new process would, and start it."""
    for name in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[name]
    from fastapi.testclient import TestClient
    from app.main import create_app
    return TestClient(create_app())


def _ads(db_path: Path) -> int:
    conn = sqlite3.connect(db_path)
    try:
        return conn.execute("SELECT COUNT(*) FROM ads").fetchone()[0]
    finally:
        conn.close()


def test_old_variable_names_still_work_with_one_warning_each(app_env, monkeypatch, caplog):
    for name in ("ADYARD_PUBLIC_URL", "ADYARD_COUNT_SALT"):
        monkeypatch.delenv(name)
    monkeypatch.setenv("MAXADS_PUBLIC_URL", "https://old.example.com")
    monkeypatch.setenv("MAXADS_COUNT_SALT", "old-salt")

    with caplog.at_level(logging.WARNING):
        client = _restart()
    from app import config
    assert config.PUBLIC_URL == "https://old.example.com"
    assert client.app.state.count_salt == "old-salt"
    for old, new in (("MAXADS_PUBLIC_URL", "ADYARD_PUBLIC_URL"),
                     ("MAXADS_COUNT_SALT", "ADYARD_COUNT_SALT")):
        assert caplog.text.count(f"{old} still works") == 1
        assert f"rename it to {new}" in caplog.text


def test_the_new_name_wins_and_the_old_one_is_reported(app_env, monkeypatch, caplog):
    monkeypatch.setenv("MAXADS_PUBLIC_URL", "https://old.example.com")

    with caplog.at_level(logging.WARNING):
        _restart()
    from app import config
    assert config.PUBLIC_URL == "https://ads.example.com"
    assert "MAXADS_PUBLIC_URL is ignored because ADYARD_PUBLIC_URL is set" in caplog.text


def test_an_old_name_with_a_typo_points_at_the_new_name(app_env, monkeypatch, caplog):
    monkeypatch.setenv("MAXADS_TRUSTED_PROXIE", "10.0.0.0/8")
    with caplog.at_level(logging.WARNING):
        _restart()
    assert ("unknown setting MAXADS_TRUSTED_PROXIE is ignored -- "
            "did you mean ADYARD_TRUSTED_PROXIES?") in caplog.text


def test_an_existing_maxads_db_is_used_not_a_fresh_one(admin_client, app_env, make_ad, caplog):
    """The installation from before the rename: its data must still be there."""
    make_ad(admin_client)
    admin_client.close()
    db_dir = app_env / "data/db"
    for path in db_dir.glob("adyard.db*"):  # with -wal and -shm, if any
        path.rename(db_dir / path.name.replace("adyard", "maxads"))

    with caplog.at_level(logging.INFO):
        client = _restart()
    from app import config
    assert config.DB_PATH == db_dir / "maxads.db"
    assert not (db_dir / "adyard.db").exists(), "no second, empty database next to it"
    assert "using the database file from before the rename" in caplog.text
    r = client.post("/login", data={"username": "tester", "password": "correct horse battery"},
                    follow_redirects=False)
    assert r.headers["location"] == "/", "the accounts are still there"
    assert _ads(db_dir / "maxads.db") == 1


def test_adyard_db_wins_and_a_leftover_maxads_db_is_reported(app_env, caplog):
    db_dir = app_env / "data/db"
    _restart()  # creates adyard.db
    sqlite3.connect(db_dir / "maxads.db").close()

    with caplog.at_level(logging.WARNING):
        _restart()
    from app import config
    assert config.DB_PATH == db_dir / "adyard.db"
    assert "maxads.db is not used because" in caplog.text


def test_a_new_installation_gets_adyard_db(app_env):
    _restart()
    from app import config
    assert config.DB_PATH == app_env / "data/db/adyard.db"
    assert config.DB_PATH.exists()
    assert not (app_env / "data/db/maxads.db").exists()


def test_a_backup_made_under_the_old_name_restores(populated_backup, tmp_path, monkeypatch):
    archive, ads = populated_backup
    base = tmp_path / "new-host"
    base.mkdir()
    (base / ".mounted").write_text("test")
    monkeypatch.setenv("ADYARD_BASE", str(base))
    for name in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[name]
    from app import config, restore
    assert restore.main([str(archive)]) == 0
    assert config.DB_PATH == base / "data/db/adyard.db"
    assert _ads(config.DB_PATH) == ads


def _rewrite_as_old_backup(source: Path, target: Path) -> None:
    """What `python -m app.backup` wrote before the rename: maxads-backup, maxads.db."""
    with tarfile.open(source) as old, tarfile.open(target, "w") as new:
        for member in old:
            data = old.extractfile(member) if member.isfile() else None
            if member.name == "MANIFEST.json":
                manifest = json.loads(data.read())
                manifest["format"] = "maxads-backup"
                raw = json.dumps(manifest).encode()
                member.size = len(raw)
                data = io.BytesIO(raw)
            elif member.name == "adyard.db":
                member.name = "maxads.db"
            new.addfile(member, data)


@pytest.fixture()
def populated_backup(admin_client, app_env, make_ad, tmp_path):
    make_ad(admin_client, title="One")
    make_ad(admin_client, title="Two")
    from app import backup
    assert backup.main([str(tmp_path / "b")]) == 0
    [archive] = (tmp_path / "b").glob("adyard-backup-*.tar")
    old = tmp_path / "maxads-backup-20261012-120000.tar"
    _rewrite_as_old_backup(archive, old)
    with tarfile.open(old) as tar:
        assert "maxads.db" in tar.getnames() and "adyard.db" not in tar.getnames()
    return old, 2
