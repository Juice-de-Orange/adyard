"""Restore a backup made by `python -m app.backup` into the data directory.

    python -m app.restore <backup.tar>          # or `-` to read the tar from stdin
    python -m app.restore --force <backup.tar>  # despite a leftover maxads.db-wal

Stop web and worker first. Whatever the data directory holds now (database,
originals, media, generated secrets) is not deleted but moved to
$MAXADS_BASE/pre-restore-<UTC time>/; remove that folder once the restored
service runs as it should.

The archive is unpacked and checked in a staging folder first; only a complete,
intact backup replaces anything. The database moves in without the old -wal and
-shm files: SQLite would otherwise replay a stale write-ahead log from the old
database into the restored one.
"""
import json
import os
import shutil
import sqlite3
import sys
import tarfile
from datetime import datetime, timezone
from pathlib import Path

from . import config, db
from .backup import FORMAT, MEDIA_DIRS, SECRET_FILES, VERSION, say

TOP_LEVEL = {"MANIFEST.json", "maxads.db", *SECRET_FILES, "data", *MEDIA_DIRS}


def _allowed(member: tarfile.TarInfo) -> bool:
    name = member.name
    if not (member.isfile() or member.isdir()):
        return False
    if name in TOP_LEVEL:
        return True
    return any(name.startswith(f"{sub}/") for sub in MEDIA_DIRS) and ".." not in name.split("/")


def unpack(source: str, staging: Path) -> None:
    if source != "-" and not Path(source).is_file():
        raise SystemExit(f"No such backup file: {source}")
    try:
        if source == "-":
            tar = tarfile.open(fileobj=sys.stdin.buffer, mode="r|*")
        else:
            tar = tarfile.open(source, mode="r:*")
    except tarfile.TarError as exc:
        raise SystemExit(f"cannot read the backup (not a tar file?): {exc}") from None
    try:
        with tar:
            for member in tar:
                if not _allowed(member):
                    raise SystemExit(f"Not a MaxAds backup: unexpected entry {member.name!r}")
                tar.extract(member, staging, filter="data")
    except (tarfile.TarError, EOFError) as exc:
        raise SystemExit(f"cannot read the backup (truncated or not a tar file?): {exc}") from None


def check(staging: Path) -> dict:
    """Refuse anything that is not a complete, readable MaxAds backup."""
    try:
        manifest = json.loads((staging / "MANIFEST.json").read_text())
    except (OSError, ValueError):
        raise SystemExit("Not a MaxAds backup: MANIFEST.json is missing or unreadable") from None
    if manifest.get("format") != FORMAT or manifest.get("version") != VERSION:
        raise SystemExit(f"Unsupported backup format: {manifest.get('format')!r} "
                         f"version {manifest.get('version')!r}")
    database = staging / "maxads.db"
    if not database.is_file():
        raise SystemExit("The backup holds no maxads.db")
    conn = sqlite3.connect(database)
    try:
        result = conn.execute("PRAGMA integrity_check").fetchone()[0]
        if result != "ok":
            raise SystemExit(f"The database in the backup is damaged: {result}")
        missing = 0
        for row in conn.execute("SELECT raw_path, media_path, thumb_path FROM ads"):
            for value in row:
                if value and not Path(value).is_absolute() and not (staging / value).exists():
                    missing += 1
    finally:
        conn.close()
    if missing:
        say(f"warning: {missing} file(s) named in the database are not in the backup")
    return manifest


def move_aside(stamp: str) -> Path | None:
    """Move the current data out of the way. Returns where it went, if anywhere."""
    aside = config.BASE_DIR / f"pre-restore-{stamp}"
    moved = False
    for name in ("data/db", *MEDIA_DIRS, *SECRET_FILES):
        path = config.BASE_DIR / name
        if not path.exists() or (path.is_dir() and not any(path.iterdir())):
            continue
        (aside / name).parent.mkdir(parents=True, exist_ok=True)
        os.rename(path, aside / name)
        moved = True
    return aside if moved else None


def put_in_place(staging: Path) -> None:
    db_dir = config.DB_PATH.parent
    if db_dir.exists():
        # Only empty leftovers can be here now; a stale -wal/-shm went aside
        # together with its database.
        shutil.rmtree(db_dir)
    db_dir.mkdir(parents=True)
    os.rename(staging / "maxads.db", config.DB_PATH)
    for sub in MEDIA_DIRS:
        target = config.BASE_DIR / sub
        if target.exists():
            target.rmdir()  # empty, see move_aside()
        source = staging / sub
        if source.is_dir():
            os.rename(source, target)
        else:
            target.mkdir(parents=True)
    for name in SECRET_FILES:
        source = staging / name
        if source.is_file():
            os.chmod(source, 0o600)
            os.rename(source, config.BASE_DIR / name)


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    force = "--force" in args
    args = [a for a in args if a != "--force"]
    if len(args) != 1 or args[0] in ("-h", "--help"):
        say(__doc__.split("\n\n")[1])
        return 2

    config.require_mount()
    config.BASE_DIR.mkdir(parents=True, exist_ok=True)
    wal = config.DB_PATH.with_name(config.DB_PATH.name + "-wal")
    if wal.exists() and not force:
        raise SystemExit(
            f"{wal} exists: web or worker still has the database open. Stop both "
            "first. (After a crash with both stopped, add --force.)"
        )

    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")
    # Inside the data directory, so the final moves are renames on one filesystem.
    staging = config.BASE_DIR / f".restore-{stamp}"
    staging.mkdir()
    try:
        unpack(args[0], staging)
        manifest = check(staging)
        aside = move_aside(stamp)
        try:
            put_in_place(staging)
        except BaseException:
            if aside:
                say(f"restore failed half-way; the previous data is in {aside}")
            raise
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    # Bring the restored database to the current schema now, not on the next start.
    db.migrate()
    with db.session() as conn:
        ads = conn.execute("SELECT COUNT(*) FROM ads").fetchone()[0]
        users = conn.execute("SELECT COUNT(*) FROM users").fetchone()[0]
    say(f"restored the backup from {manifest.get('created')}: {ads} ads, {users} users")
    if aside:
        say(f"the previous data is in {aside} -- delete it once all is well")
    say("start web and worker again")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
