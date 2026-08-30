"""TTL-based deduplication for incoming messages.

Flood packets arrive multiple times via different paths. The companion
deduplicates by txt_hash, but the sender may also retransmit if it misses
the ACK from our companion. This catches both cases.
"""

import time
from collections import OrderedDict


class MessageDedup:
    """TTL-based dedup for incoming messages.

    Return True from :meth:`check` if this is a new message, False if
    it was seen within the TTL window.
    """

    _DEDUP_TTL = 30.0  # seconds

    def __init__(self) -> None:
        self._seen: OrderedDict[str, float] = OrderedDict()

    def check(self, key: str) -> bool:
        """Return True if this is a new message, False if duplicate."""
        now = time.monotonic()
        # Evict expired entries.
        while self._seen:
            _k, t = next(iter(self._seen.items()))
            if now - t > self._DEDUP_TTL:
                self._seen.popitem(last=False)
            else:
                break
        if key in self._seen:
            return False
        self._seen[key] = now
        return True
