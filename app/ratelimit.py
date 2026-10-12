"""Login throttle: slows down password guessing.

Counted twice, and both count: per client address (someone trying many names
from one place) and per account name (someone trying one account from many
addresses -- behind a trusted proxy every attempt can carry a new one). After
FREE_FAILURES failures a key is locked for LOCK_SECONDS; every failure after a
lock doubles the wait, up to MAX_LOCK_SECONDS. While a key is locked, no
password is checked at all, the right one included. A successful login clears
both keys.

The state lives in memory. That fits how MaxAds runs -- one web process
(Docker and the systemd unit start a single uvicorn worker) -- and a restart
simply forgets it. With several worker processes each would count on its own.
"""
import threading
import time
from collections import OrderedDict
from dataclasses import dataclass

FREE_FAILURES = 5
LOCK_SECONDS = 30
MAX_LOCK_SECONDS = 15 * 60
# A key without a failure for this long is forgotten.
FORGET_AFTER_SECONDS = 24 * 3600


@dataclass
class _Entry:
    failures: int = 0
    last_failure: float = 0.0
    locked_until: float = 0.0


class LoginThrottle:
    def __init__(self, clock=time.monotonic, max_entries: int = 10_000) -> None:
        self.clock = clock
        # Bounded: invented names or addresses must not grow memory without limit.
        self.max_entries = max_entries
        self._entries: "OrderedDict[str, _Entry]" = OrderedDict()
        self._lock = threading.Lock()  # sync routes run in a thread pool

    @staticmethod
    def _keys(address: str, username: str) -> tuple[str, str]:
        return f"addr:{address}", f"user:{username.strip().lower()}"

    def _forget_idle(self, now: float) -> None:
        while self._entries:
            key, entry = next(iter(self._entries.items()))
            if now - entry.last_failure <= FORGET_AFTER_SECONDS:
                break
            del self._entries[key]

    def retry_after(self, address: str, username: str) -> int:
        """Seconds until a login may be tried again; 0 when it may be tried now."""
        now = self.clock()
        with self._lock:
            waits = [entry.locked_until - now for key in self._keys(address, username)
                     if (entry := self._entries.get(key)) is not None]
        wait = max(waits, default=0)
        return int(wait) + (wait % 1 > 0) if wait > 0 else 0

    def failure(self, address: str, username: str) -> None:
        now = self.clock()
        with self._lock:
            self._forget_idle(now)
            for key in self._keys(address, username):
                entry = self._entries.pop(key, None) or _Entry()
                entry.failures += 1
                entry.last_failure = now
                over = entry.failures - FREE_FAILURES
                if over >= 0:
                    entry.locked_until = now + min(LOCK_SECONDS * 2 ** over, MAX_LOCK_SECONDS)
                self._entries[key] = entry  # most recent at the end
            while len(self._entries) > self.max_entries:
                self._entries.popitem(last=False)

    def success(self, address: str, username: str) -> None:
        with self._lock:
            for key in self._keys(address, username):
                self._entries.pop(key, None)
