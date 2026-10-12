"""Back up a MaxAds data directory into one tar file -- safe while the service runs.

    python -m app.backup <target-dir>   # writes <target-dir>/maxads-backup-<UTC time>.tar
    python -m app.backup -              # writes the tar to stdout

The database is copied with SQLite's online backup API. A plain copy of
maxads.db would be wrong in two ways while web and worker run: committed
transactions that still sit in maxads.db-wal would be missing, and a page
written during the copy could leave a torn file. The backup API reads through
SQLite and sees exactly one committed state.

The archive holds that database copy (maxads.db), the originals (data/raw/), the
delivered media (data/media/), the generated secrets .secret_key and .count_salt
if they exist, and MANIFEST.json. Logs are left out. The secrets and the
password hashes make the file sensitive: it is created with mode 0600.
Restore with `python -m app.restore`.
"""
import io
import json
import os
import sqlite3
import sys
import tarfile
import tempfile
import time
from datetime import datetime, timezone
from pathlib import Path

from . import config

FORMAT = "maxads-backup"
VERSION = 1
SECRET_FILES = (".secret_key", ".count_salt")
MEDIA_DIRS = ("data/raw", "data/media")


def say(message: str) -> None:
    # stderr only: with `-` the tar itself is on stdout.
    print(message, file=sys.stderr, flush=True)


def snapshot_database(target: Path) -> dict[str, int]:
    """Copy the live database to `target` and return a few row counts."""
    if not config.DB_PATH.exists():
        raise SystemExit(f"No database at {config.DB_PATH} -- is MAXADS_BASE right?")
    source = sqlite3.connect(config.DB_PATH, timeout=30)
    copy = sqlite3.connect(target)
    try:
        source.execute("PRAGMA busy_timeout=30000")
        source.backup(copy)
        # A self-contained file: no -wal next to it to forget. The app switches
        # it back to WAL on its first start.
        copy.execute("PRAGMA journal_mode=DELETE")
        check = copy.execute("PRAGMA integrity_check").fetchone()[0]
        if check != "ok":
            raise SystemExit(f"The database copy failed its integrity check: {check}")
        return {
            table: copy.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0]
            for table in ("users", "ads", "events")
        }
    finally:
        copy.close()
        source.close()


def _add_bytes(tar: tarfile.TarFile, name: str, data: bytes) -> None:
    info = tarfile.TarInfo(name)
    info.size = len(data)
    info.mtime = int(time.time())
    info.mode = 0o600
    tar.addfile(info, io.BytesIO(data))


def write_archive(tar: tarfile.TarFile, db_copy: Path, counts: dict[str, int]) -> int:
    """Fill `tar`. Returns the number of media files written."""
    manifest = {
        "format": FORMAT,
        "version": VERSION,
        "created": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "counts": counts,
    }
    _add_bytes(tar, "MANIFEST.json", json.dumps(manifest, indent=2).encode())
    tar.add(db_copy, arcname="maxads.db")
    for name in SECRET_FILES:
        path = config.BASE_DIR / name
        if path.is_file():
            tar.add(path, arcname=name)

    files = 0
    for sub in MEDIA_DIRS:
        root = config.BASE_DIR / sub
        tar.add(root, arcname=sub, recursive=False)
        for path in sorted(root.rglob("*")):
            if not path.is_file() or path.is_symlink():
                continue
            arcname = f"{sub}/{path.relative_to(root).as_posix()}"
            try:
                tar.add(path, arcname=arcname, recursive=False)
            except FileNotFoundError:
                # Deleted after the database copy: the copy may still name it,
                # but so would any backup taken a second earlier.
                say(f"skipped {arcname}: deleted during the backup")
                continue
            files += 1
    return files


def main(argv: list[str] | None = None) -> int:
    args = sys.argv[1:] if argv is None else argv
    if len(args) != 1 or args[0] in ("-h", "--help"):
        say(__doc__.split("\n\n")[1])
        return 2
    target = args[0]

    config.require_mount()
    config.ensure_dirs()
    stamp = datetime.now(timezone.utc).strftime("%Y%m%d-%H%M%S")

    with tempfile.TemporaryDirectory(prefix="maxads-backup-") as tmp:
        db_copy = Path(tmp) / "maxads.db"
        counts = snapshot_database(db_copy)

        if target == "-":
            with tarfile.open(fileobj=sys.stdout.buffer, mode="w|") as tar:
                files = write_archive(tar, db_copy, counts)
            sys.stdout.buffer.flush()
            say(f"backup written to stdout: {counts['ads']} ads, {counts['users']} users, "
                f"{counts['events']} events, {files} media files")
            return 0

        out_dir = Path(target)
        out_dir.mkdir(parents=True, exist_ok=True)
        final = out_dir / f"maxads-backup-{stamp}.tar"
        part = out_dir / f".{final.name}.part"
        fd = os.open(part, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        try:
            with os.fdopen(fd, "wb") as fh, tarfile.open(fileobj=fh, mode="w") as tar:
                files = write_archive(tar, db_copy, counts)
            os.replace(part, final)
        except BaseException:
            part.unlink(missing_ok=True)
            raise
    say(f"backup written: {final} ({counts['ads']} ads, {counts['users']} users, "
        f"{counts['events']} events, {files} media files, "
        f"{final.stat().st_size / 1024 / 1024:.1f} MB)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
