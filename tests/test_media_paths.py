"""Media paths are stored relative to the data directory (ADYARD_BASE).

v0.1 stored absolute paths, so moving the data directory -- another disk, a container
volume, a restore on a new host -- turned every banner into a 404.
"""
import logging
import shutil
import sys
from pathlib import Path

COLUMNS = ("raw_path", "media_path", "thumb_path")


def _row(slug):
    from app import db
    with db.session() as conn:
        return dict(conn.execute("SELECT * FROM ads WHERE slug = ?", (slug,)).fetchone())


def _restart(monkeypatch, base=None):
    """A fresh process on (possibly another) data directory."""
    from fastapi.testclient import TestClient
    if base is not None:
        monkeypatch.setenv("ADYARD_BASE", str(base))
    for name in [m for m in list(sys.modules) if m.startswith("app")]:
        del sys.modules[name]
    from app.main import create_app
    return TestClient(create_app())


def _served(client, slug):
    return [client.get(f"/m/{slug}").status_code,
            client.get(f"/m/{slug}?thumb=1").status_code,
            client.get(f"/img/{slug}").status_code]


def test_a_new_banner_is_stored_with_relative_paths(admin_client, make_ad):
    slug = make_ad(admin_client)
    row = _row(slug)
    for col in COLUMNS:
        assert not Path(row[col]).is_absolute(), f"{col} = {row[col]!r}"
        assert row[col].startswith("data/")
    assert _served(admin_client, slug) == [200, 200, 200]


def test_banners_survive_moving_the_data_directory(admin_client, app_env, make_ad,
                                                   tmp_path, monkeypatch):
    slug = make_ad(admin_client)
    moved = tmp_path / "elsewhere" / "adyard"
    moved.parent.mkdir()
    shutil.move(str(app_env), str(moved))

    client = _restart(monkeypatch, moved)
    assert _served(client, slug) == [200, 200, 200]


def test_absolute_paths_from_v0_1_are_converted_at_start(admin_client, app_env, make_ad,
                                                         tmp_path, monkeypatch, caplog):
    slug = make_ad(admin_client)
    from app import db
    with db.session() as conn:  # what v0.1 wrote
        for col in COLUMNS:
            conn.execute(f"UPDATE ads SET {col} = ? || '/' || {col}", (str(app_env),))
    assert all(Path(_row(slug)[c]).is_absolute() for c in COLUMNS)

    with caplog.at_level(logging.INFO):
        client = _restart(monkeypatch)
    row = _row(slug)
    assert all(row[c].startswith("data/") for c in COLUMNS), row
    assert "3 media path(s) converted to relative" in caplog.text
    assert _served(client, slug) == [200, 200, 200]

    # Idempotent: a second start finds nothing to do.
    caplog.clear()
    with caplog.at_level(logging.INFO):
        _restart(monkeypatch)
    assert "converted to relative" not in caplog.text
    assert _row(slug) == row

    # And from now on the directory can move.
    moved = tmp_path / "moved"
    shutil.move(str(app_env), str(moved))
    assert _served(_restart(monkeypatch, moved), slug) == [200, 200, 200]


def test_paths_outside_the_data_directory_are_left_and_logged(admin_client, app_env, make_ad,
                                                              tmp_path, monkeypatch, caplog):
    slug = make_ad(admin_client)
    outside = tmp_path / "other-disk"
    outside.mkdir()
    row = _row(slug)
    media = outside / "banner.webp"
    shutil.copy(app_env / row["media_path"], media)
    from app import db
    with db.session() as conn:
        conn.execute("UPDATE ads SET media_path = ? WHERE slug = ?", (str(media), slug))

    with caplog.at_level(logging.WARNING):
        client = _restart(monkeypatch)
    assert _row(slug)["media_path"] == str(media), "a path outside ADYARD_BASE is not touched"
    assert f"ad {slug}: media_path {media} is outside the data directory" in caplog.text
    # Still delivered from where it is.
    assert client.get(f"/m/{slug}").status_code == 200


def test_delete_removes_the_files_behind_relative_paths(admin_client, app_env, make_ad):
    slug = make_ad(admin_client)
    row = _row(slug)
    files = [app_env / row[c] for c in COLUMNS]
    assert all(f.exists() for f in files)
    admin_client.post(f"/ads/{slug}/delete", follow_redirects=False)
    assert not any(f.exists() for f in files)
