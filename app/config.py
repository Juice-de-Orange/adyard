"""Central configuration. Everything is path-based and overridable via environment."""
import ipaddress
import logging
import os
import secrets
from pathlib import Path

# Root of all application data: originals, delivered media, database, logs.
# Typically a dedicated disk or a container volume.
BASE_DIR = Path(os.environ.get("MAXADS_BASE", "/var/lib/maxads"))

RAW_DIR = BASE_DIR / "data" / "raw"
MEDIA_DIR = BASE_DIR / "data" / "media"
DB_DIR = BASE_DIR / "data" / "db"
LOG_DIR = BASE_DIR / "data" / "logs"

DB_PATH = DB_DIR / "maxads.db"

# A file you create on the data disk once it is mounted. Its absence means the
# mount is gone and we would silently write onto the system disk instead.
SENTINEL = BASE_DIR / ".mounted"

# The mount guard is on by default -- it exists for the case where BASE_DIR is a
# separate disk that can drop out. Set MAXADS_REQUIRE_MOUNT=0 when BASE_DIR is a
# plain directory or a container volume that cannot go missing on its own.
REQUIRE_MOUNT = os.environ.get("MAXADS_REQUIRE_MOUNT", "1") != "0"

# Wake-up channel between web and worker. Deliberately under /run (tmpfs, i.e.
# RAM): a FIFO on the data disk would wake exactly the idle path we want to keep
# quiet. systemd creates the directory via RuntimeDirectory.
RUNTIME_DIR = Path(os.environ.get("RUNTIME_DIRECTORY", "/run/maxads"))
WAKEUP_FIFO = RUNTIME_DIR / "wakeup"

# Public base URL, used to build embed snippets that work on foreign sites.
PUBLIC_URL = os.environ.get("MAXADS_PUBLIC_URL", "http://localhost:8080").rstrip("/")

SECRET_KEY = os.environ.get("MAXADS_SECRET_KEY", "")

# Mixed into the daily visitor hash so we never store raw IPs.
COUNT_SALT = os.environ.get("MAXADS_COUNT_SALT", "")

# Reverse proxies whose X-Forwarded-For / X-Forwarded-Proto headers are believed:
# comma-separated addresses or networks. From any other peer the headers are
# ignored -- otherwise every client could pick its own address and inflate the
# impressions past the per-day deduplication. The default trusts a proxy on the
# same machine only; an empty value trusts nobody.
TRUSTED_PROXIES = os.environ.get("MAXADS_TRUSTED_PROXIES", "127.0.0.1,::1")

# Password for the first admin account. If unset, one is generated on first
# start and printed once to stderr -- never written to the log files.
ADMIN_PASSWORD = os.environ.get("MAXADS_ADMIN_PASSWORD", "")

MAX_UPLOAD_BYTES = int(os.environ.get("MAXADS_MAX_UPLOAD_MB", "512")) * 1024 * 1024

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
            "The data disk is not mounted (or set MAXADS_REQUIRE_MOUNT=0)."
        )


def trusted_proxies() -> list[str]:
    """MAXADS_TRUSTED_PROXIES as a list, refusing to start on anything malformed.

    uvicorn would quietly treat a typo as a literal host name that never matches,
    so a mistake here would look like a working setup that counts wrong.
    """
    entries = [e.strip() for e in TRUSTED_PROXIES.split(",") if e.strip()]
    for entry in entries:
        try:
            ipaddress.ip_network(entry, strict=False)
        except ValueError:
            raise SystemExit(
                f"MAXADS_TRUSTED_PROXIES: {entry!r} is not an IP address or network "
                "(expected e.g. 127.0.0.1,10.0.0.0/8)."
            ) from None
    return entries


def ensure_dirs() -> None:
    for path in (RAW_DIR, MEDIA_DIR, DB_DIR, LOG_DIR):
        path.mkdir(parents=True, exist_ok=True)


def load_or_create_secret(name: str, path: Path) -> str:
    """Return a persistent random secret, creating it on first use."""
    env = os.environ.get(name)
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
