"""Telemetry logging: periodically request and log telemetry from contacts.

Active users are tracked in a JSON state file so logging survives bot
restarts. Telemetry readings are appended as JSON lines to
``<state_dir>/<pubkey>.jsonl``.
"""

import asyncio
import json
import logging
import time
from pathlib import Path
from typing import Any

from meshcore import MeshCore

log = logging.getLogger(__name__)

DEFAULT_PERIOD = 300  # 5 minutes
MIN_PERIOD = 60  # 1 minute
TELEMETRY_TIMEOUT = 10  # seconds

_STATE_FILE = "active.json"


class TelemetryLogger:
    """Manages periodic telemetry polling for subscribed contacts.

    Each active user is identified by their full public key (64-char hex).
    The poll loop runs as a background task, requesting telemetry from each
    active user's companion at their configured period.
    """

    def __init__(self, mc: MeshCore, state_dir: Path) -> None:
        self._mc = mc
        self._state_dir = state_dir
        self._state_dir.mkdir(parents=True, exist_ok=True)
        self._state_path = self._state_dir / _STATE_FILE
        self._active: dict[str, dict[str, Any]] = {}
        self._task: asyncio.Task[None] | None = None

    def start(self) -> None:
        """Load persisted state and resume logging if any users are active."""
        self._load_state()
        if self._active:
            log.info("resuming telemetry logging for %d user(s)", len(self._active))
            self._ensure_task()

    def start_logging(self, pubkey: str, period: int = DEFAULT_PERIOD) -> None:
        """Begin logging telemetry for *pubkey* (full 64-char hex key)."""
        period = max(period, MIN_PERIOD)
        self._active[pubkey] = {
            "period": period,
            "started_at": int(time.time()),
            "readings": 0,
            "next_poll": 0,  # poll immediately on next loop iteration
        }
        self._save_state()
        self._ensure_task()
        log.info("started telemetry logging for %s every %ds", pubkey[:12], period)

    def stop_logging(self, pubkey: str) -> dict[str, Any] | None:
        """Stop logging for *pubkey*. Returns the session info, or None."""
        info = self._active.pop(pubkey, None)
        if info is None:
            return None
        self._save_state()
        if not self._active and self._task is not None:
            self._task.cancel()
            self._task = None
        log.info("stopped telemetry logging for %s", pubkey[:12])
        return info

    def is_logging(self, pubkey: str) -> bool:
        return pubkey in self._active

    def get_status(self, pubkey: str) -> dict[str, Any] | None:
        return self._active.get(pubkey)

    def stop(self) -> None:
        """Cancel the poll loop (called on shutdown)."""
        if self._task is not None:
            self._task.cancel()
            self._task = None

    def _ensure_task(self) -> None:
        if self._task is None or self._task.done():
            self._task = asyncio.create_task(self._poll_loop())

    async def _poll_loop(self) -> None:
        """Periodically request telemetry from all active users."""
        while self._active:
            now = time.time()
            due = [
                (pk, entry)
                for pk, entry in list(self._active.items())
                if now >= entry.get("next_poll", 0)
            ]
            for pubkey, entry in due:
                await self._poll_one(pubkey, entry)

            now = time.time()
            next_wake = min(now + entry["period"] for entry in self._active.values())
            sleep_for = max(1, next_wake - now)
            await asyncio.sleep(sleep_for)

    async def _poll_one(self, pubkey: str, entry: dict[str, Any]) -> None:
        """Request telemetry from one contact and log the result."""
        entry["next_poll"] = time.time() + entry["period"]

        try:
            await self._mc.ensure_contacts()
            contact = self._mc.get_contact_by_key_prefix(pubkey)
            if contact is None:
                log.warning("telemetry: contact %s not found", pubkey[:12])
                return

            lpp = await self._mc.commands.req_telemetry_sync(
                contact, timeout=TELEMETRY_TIMEOUT
            )
            if lpp is None:
                log.info("telemetry %s: no response (timeout)", pubkey[:12])
                return

            entry["readings"] += 1
            record = {"ts": int(time.time()), "data": lpp}
            data_path = self._state_dir / f"{pubkey}.jsonl"
            with data_path.open("a") as f:
                f.write(json.dumps(record) + "\n")
            log.info("telemetry %s: %s", pubkey[:12], json.dumps(lpp))

        except asyncio.CancelledError:
            raise
        except Exception:
            log.warning("telemetry %s: request failed", pubkey[:12], exc_info=True)

    def _load_state(self) -> None:
        """Read active users from the state file."""
        try:
            data = json.loads(self._state_path.read_text())
            if isinstance(data, dict):
                self._active = data
        except (OSError, ValueError):
            pass

    def _save_state(self) -> None:
        """Persist active users to the state file."""
        self._state_path.write_text(json.dumps(self._active))
