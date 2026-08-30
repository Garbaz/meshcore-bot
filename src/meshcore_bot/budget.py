"""Token-bucket message budget for per-channel throttling.

Mirrors the firmware's ``Dispatcher::updateTxBudget()`` model: each
key (typically a channel) has a budget that refills at a fixed rate
up to a cap, and is debited by one per outgoing message.  When the
budget is exhausted the caller silently drops the reply. This is
how the bot avoids dominating a channel when users spam commands.

The class is intentionally generic (keyed by ``str``) so that future
per-user or per-(channel, sender) throttling can reuse it unchanged
by passing a different key.
"""

import time


class MessageBudget:
    """Token-bucket limiter with per-key budgets.

    Each key starts at ``max_budget`` and refills at ``refill_per_sec``
    messages/second, capped at ``max_budget``.  ``check()`` refills
    then debits one message; returns ``False`` when the key has no
    budget left.
    """

    def __init__(self, max_budget: float, refill_per_sec: float) -> None:
        self._max = max_budget
        self._rate = refill_per_sec
        self._budget: dict[str, float] = {}
        self._last: dict[str, float] = {}

    def check(self, key: str) -> bool:
        """Refill the budget for *key*, then debit one if possible.

        Returns ``True`` if the message is allowed (budget debited),
        ``False`` if throttled (budget exhausted, no debit).
        """
        now = time.monotonic()
        last = self._last.get(key, 0.0)
        budget = self._budget.get(key, self._max)

        elapsed = now - last
        if elapsed > 0:
            budget = min(budget + elapsed * self._rate, self._max)
            self._last[key] = now

        if budget < 1.0:
            self._budget[key] = budget
            return False

        self._budget[key] = budget - 1.0
        return True
