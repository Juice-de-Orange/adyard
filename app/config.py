"""Central configuration. Everything is path-based and overridable via environment."""
import difflib
import ipaddress
import logging
import os
import secrets
from pathlib import Path

# Every setting is an ADYARD_ environment variable. Until v0.2 the project was
# called maxads and its variables MAXADS_; those names are still read, so an
# existing installation keeps its configuration (each one is reported at start,
# see setting_warnings). The new name wins when both are set.
PREFIX = "ADYARD_"
OLD_PREFIX = "MAXADS_"


def setting(name: str, default: str) -> str:
    """ADYARD_<name>, else the old MAXADS_<name>, else `default`."""
    for prefix in (PREFIX, OLD_PREFIX):
        value = os.environ.get(prefix + name)
        if value is not None:
            return value
    return default


def _keep_old(new: Path, old: Path) -> Path:
    """`new`, unless only `old` -- its name from before the rename -- exists.

    Falling back to the old file is what keeps an existing installation on its
    data: a fresh, empty file under the new name would look like lost data.
    """
    return old if not new.exists() and old.exists() else new


# Root of all application data: originals, delivered media, database, logs.
# Typically a dedicated disk or a container volume.
BASE_DIR = Path(setting("BASE", "")
                or _keep_old(Path("/var/lib/adyard"), Path("/var/lib/maxads")))

RAW_DIR = BASE_DIR / "data" / "raw"
MEDIA_DIR = BASE_DIR / "data" / "media"
DB_DIR = BASE_DIR / "data" / "db"
LOG_DIR = BASE_DIR / "data" / "logs"

DB_PATH = DB_DIR / "adyard.db"
# The database file from before the rename. Used as long as there is no
# adyard.db next to it; nothing renames it behind the operator's back.
OLD_DB_PATH = DB_DIR / "maxads.db"
DB_PATH = _keep_old(DB_PATH, OLD_DB_PATH)

# A file you create on the data disk once it is mounted. Its absence means the
# mount is gone and we would silently write onto the system disk instead.
SENTINEL = BASE_DIR / ".mounted"

# The mount guard is on by default -- it exists for the case where BASE_DIR is a
# separate disk that can drop out. Set ADYARD_REQUIRE_MOUNT=0 when BASE_DIR is a
# plain directory or a container volume that cannot go missing on its own.
REQUIRE_MOUNT = setting("REQUIRE_MOUNT", "1") != "0"

# Wake-up channel between web and worker. Deliberately under /run (tmpfs, i.e.
# RAM): a FIFO on the data disk would wake exactly the idle path we want to keep
# quiet. systemd creates the directory via RuntimeDirectory.
RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIRECTORY", "/run/adyard"))
WAKEUP_FIFO = RUNTIME_DIR / "wakeup"

# Public base URL, used to build embed snippets that work on foreign sites.
PUBLIC_URL = setting("PUBLIC_URL", "http://localhost:8080").rstrip("/")

SECRET_KEY = setting("SECRET_KEY", "")

# Mixed into the daily visitor hash so we never store raw IPs.
COUNT_SALT = setting("COUNT_SALT", "")

# Reverse proxies whose X-Forwarded-For / X-Forwarded-Proto headers are believed:
# comma-separated addresses or networks. From any other peer the headers are
# ignored -- otherwise every client could pick its own address and inflate the
# impressions past the per-day deduplication. The default trusts a proxy on the
# same machine only; an empty value trusts nobody.
TRUSTED_PROXIES = setting("TRUSTED_PROXIES", "127.0.0.1,::1")

# Password for the first admin account. If unset, one is generated on first
# start and printed once to stderr -- never written to the log files.
ADMIN_PASSWORD = setting("ADMIN_PASSWORD", "")

MAX_UPLOAD_BYTES = int(setting("MAX_UPLOAD_MB", "512")) * 1024 * 1024

IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".gif", ".webp"}
VIDEO_EXTENSIONS = {".mp4", ".mov", ".webm", ".m4v"}

# Target width for web delivery; taller/wider originals are scaled down only.
IMAGE_MAX_WIDTH = 1600
VIDEO_MAX_WIDTH = 1280
THUMB_WIDTH = 400


def require_mount() -> None:
    """Refuse to run when the data disk is not mounted.

    Without this check the service happily writes into an empty directory on the
    system disk when the disk drops out -- the failure mode is silent.
    """
    if REQUIRE_MOUNT and not SENTINEL.exists():
        raise SystemExit(
            f"Refusing to start: {SENTINEL} is missing. "
            "The data disk is not mounted (or set ADYARD_REQUIRE_MOUNT=0)."
        )


# Every setting that the service or docker-compose.yml reads, without its prefix.
KNOWN_SETTINGS = (
    "BASE", "REQUIRE_MOUNT", "PUBLIC_URL", "PORT", "SECRET_KEY", "COUNT_SALT",
    "ADMIN_PASSWORD", "MAX_UPLOAD_MB", "TRUSTED_PROXIES",
)


def setting_warnings() -> list[str]:
    """Warnings for variables that do not do what their author meant.

    An unknown ADYARD_ or MAXADS_ name is almost always a typo, and a typo is
    silent: the default stays in place and the service runs as if the line were
    not there. An old MAXADS_ name still works, but is reported once per name
    so that it gets renamed -- and when the ADYARD_ name is set as well, the old
    one is ignored, which is said too.
    """
    known = [PREFIX + name for name in KNOWN_SETTINGS]
    warnings = []
    for name in sorted(os.environ):
        prefix = next((p for p in (PREFIX, OLD_PREFIX) if name.startswith(p)), None)
        if prefix is None:
            continue
        suffix = name[len(prefix):]
        if suffix not in KNOWN_SETTINGS:
            close = difflib.get_close_matches(PREFIX + suffix, known, n=1)
            hint = f" -- did you mean {close[0]}?" if close else ""
            warnings.append(f"unknown setting {name} is ignored{hint}")
        elif prefix == OLD_PREFIX and PREFIX + suffix in os.environ:
            warnings.append(f"{name} is ignored because {PREFIX + suffix} is set "
                            f"(MAXADS_ is the old prefix) -- remove it")
        elif prefix == OLD_PREFIX:
            warnings.append(f"{name} still works, but the project is called adyard "
                            f"now -- rename it to {PREFIX + suffix}")
    return warnings


def database_note() -> tuple[int, str] | None:
    """(log level, line) for the start-up log when the database file is not the usual one."""
    if DB_PATH == OLD_DB_PATH:
        return logging.INFO, (
            f"using the database file from before the rename, {DB_PATH} -- fine as it "
            "is; to rename it to adyard.db, stop web and worker first")
    if OLD_DB_PATH.exists():
        return logging.WARNING, (
            f"{OLD_DB_PATH} is not used because {DB_PATH} exists -- "
            "check which of the two holds your data")
    return None


def trusted_proxies() -> list[str]:
    """ADYARD_TRUSTED_PROXIES as a list, refusing to start on anything malformed.

    uvicorn would quietly treat a typo as a literal host name that never matches,
    so a mistake here would look like a working setup that counts wrong.
    """
    entries = [e.strip() for e in TRUSTED_PROXIES.split(",") if e.strip()]
    for entry in entries:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            raise SystemExit(
                f"ADYARD_TRUSTED_PROXIES: {entry!r} is not an IP address or network "
                "(expected e.g. 127.0.0.1,10.0.0.0/8)."
            ) from None
    return entries


def stored_path(path: Path) -> str:
    """A file under BASE_DIR as the database keeps it: relative to BASE_DIR.

    Relative so that the data directory can move -- another disk, a container
    volume, a restore on a new host -- without breaking a single banner.
    """
    return Path(path).relative_to(BASE_DIR).as_posix()


def media_file(stored: str) -> Path:
    """The file behind a path from the database.

    An absolute path is one from before v0.2 that lies outside BASE_DIR, so the
    start-up conversion (db.relativize_media_paths) left it as it was.
    """
    path = Path(stored)
    return path if path.is_absolute() else BASE_DIR / path


def ensure_dirs() -> None:
    for path in (RAW_DIR, MEDIA_DIR, DB_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_or_create_secret(name: str, path: Path) -> str:
    """Return a persistent random secret, creating it on first use."""
    env = setting(name, "")
    if env:
        return env
    if path.exists():
        return path.read_text().strip()
    value = secrets.token_urlsafe(48)
    path.write_text(value)
    path.chmod(0o600)
    return value


def file_handler(name: str) -> "logging.Handler":
    """A log handler that does not touch the disk for every single line.

    Writing each line straight through would be the one routine that keeps the
    disk busy in normal operation, so lines are collected in memory and flushed
    in batches -- immediately for anything at WARNING or above, so a real
    problem is never stuck in the buffer.
    """
    import logging
    import logging.handlers

    target = logging.FileHandler(LOG_DIR / name, delay=True)
    buffered = logging.handlers.MemoryHandler(
        capacity=200, flushLevel=logging.WARNING, target=target, flushOnClose=True,
    )
    return buffered
