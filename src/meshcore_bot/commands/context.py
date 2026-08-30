"""Context passed to command handlers, plus text-size helpers.

``Context`` encapsulates everything a command handler needs: the MeshCore
connection, sender info, channel/scope state, and a ``reply()`` method that
handles chunking and scope selection.
"""

import asyncio
import logging
from collections.abc import Callable
from dataclasses import dataclass, field
from typing import Any

from meshcore import MeshCore

from meshcore_bot.registry import NodeRegistry
from meshcore_bot.telemetry import TelemetryLogger

log = logging.getLogger("meshcore_bot.commands")

MAX_TEXT_PAYLOAD = 160  # firmware MAX_TEXT_LEN (10 * CIPHER_BLOCK_SIZE)
MAX_FRAME_SIZE = 172  # USB serial max frame (firmware 1.16+)

_DM_OVERHEAD = 16  # cmd(1)+type(1)+attempt(1)+ts(4)+pubkey(6)+null(1)+safety(2)
_CHAN_OVERHEAD = 10  # cmd(1)+type(1)+chan_idx(1)+ts(4)+null(1)+safety(2)


def max_dm_text_bytes() -> int:
    """Max UTF-8 bytes for a direct message."""
    return min(MAX_FRAME_SIZE - _DM_OVERHEAD, MAX_TEXT_PAYLOAD)


def max_channel_text_bytes(sender_name: str) -> int:
    """Max UTF-8 bytes for a channel message (firmware prepends 'name: ')."""
    prefix_len = len(sender_name.encode()) + 2  # ": "
    return min(MAX_TEXT_PAYLOAD - prefix_len, MAX_FRAME_SIZE - _CHAN_OVERHEAD)


@dataclass(slots=True)
class Context:
    """Everything a command handler needs to do its job and reply."""

    mc: MeshCore
    registry: NodeRegistry
    sender: str  # display name of the sender ("unknown" if not determinable)
    bot_name: str  # this companion's advertised name
    is_dm: bool
    path_hash_mode: int = 0  # hash mode of the incoming message
    flood_scope: bytes | None = (
        b"\0" * 16
    )  # 16-byte scope key, b"\0"*16 = default, None = unscoped
    flood_scope_name: str = ""  # display name for the flood scope (e.g. "#de-bw")
    verb: str = ""  # the actual name/alias the user invoked (e.g. "route", "trace")
    channel_idx: int | None = None
    channel_name: str = ""  # name of the channel (e.g. "#ping") or "" for DMs
    channel_allowed: set[str] | None = None  # allowed command names, or None for all
    channel_open: bool = False  # channel-level ~: all commands mention-free
    open_cmds: set[str] = field(default_factory=set)  # command-level ~: specific cmds
    msg: dict[str, Any] = field(default_factory=dict)
    contact: dict[str, Any] | None = None
    location: str | None = None
    telemetry: TelemetryLogger | None = None
    budget_check: Callable[[], bool] | None = None  # channel budget; None for DMs

    @property
    def origin(self) -> tuple[float, float] | None:
        """The companion's own advertised coordinates, or None."""
        info = self.mc.self_info or {}
        lat: Any = info.get("adv_lat")
        lon: Any = info.get("adv_lon")
        if lat is None or lon is None or (lat == 0 and lon == 0):
            return None
        return (float(lat), float(lon))

    @property
    def path_hash_width(self) -> int:
        """Path hash width in bytes, falling back through available fields."""
        hs: Any = self.msg.get("path_hash_size")
        if isinstance(hs, int) and hs > 0:
            return hs
        hm: Any = self.msg.get("path_hash_mode")
        if isinstance(hm, int) and hm >= 0:
            return hm + 1
        if self.contact is not None:
            ohm: Any = self.contact.get("out_path_hash_mode")
            if isinstance(ohm, int) and ohm >= 0:
                return ohm + 1
        return 1

    @property
    def reply_text_limit(self) -> int:
        """Max UTF-8 bytes for reply text (excluding the @[sender]: prefix)."""
        if self.is_dm:
            return max_dm_text_bytes()
        prefix_len = (
            len(f"@[{self.sender}]: ".encode()) if self.sender != "unknown" else 0
        )
        return max(1, max_channel_text_bytes(self.bot_name) - prefix_len)

    async def reply(self, text: str) -> None:
        """Send *text* back to the originating DM or channel."""
        dest = "DM" if self.is_dm else self.channel_name or f"chan:{self.channel_idx}"
        scope = (
            "unscoped"
            if self.flood_scope is None
            else self.flood_scope_name or self.flood_scope.hex()[:8]
        )
        path_mode = (
            f"hash{self.path_hash_mode}" if self.path_hash_mode >= 0 else "default"
        )
        log.info(
            "reply to %s (scope=%s, path=%s): %r",
            dest,
            scope,
            path_mode,
            text,
        )
        if self.path_hash_mode >= 0:
            await self.mc.commands.set_path_hash_mode(self.path_hash_mode)

        # Set flood scope for both DMs and channels so the companion uses
        # the correct scope when flooding (no direct path).
        if self.flood_scope is None:
            await self.mc.commands.force_unscoped()
        else:
            await self.mc.commands.set_flood_scope(self.flood_scope)

        if self.is_dm and self.contact is not None:
            chunks = split_lines(text.splitlines(), max_dm_text_bytes())
            for chunk in chunks:
                result = await self.mc.commands.send_msg_with_retry(self.contact, chunk)
                if result is None:
                    log.error("DM send failed (no ack) for %s", self.sender)
                    return
                await asyncio.sleep(0.2)
        elif self.channel_idx is not None:
            chan_limit = max_channel_text_bytes(self.bot_name)
            prefix = f"@[{self.sender}]: " if self.sender != "unknown" else ""
            prefix_len = len(prefix.encode())
            limits = (
                [max(1, chan_limit - prefix_len), chan_limit]
                if prefix
                else [chan_limit]
            )
            chunks = split_lines(text.splitlines(), *limits)
            for i, chunk in enumerate(chunks):
                full = prefix + chunk if i == 0 else chunk
                result = await self.mc.commands.send_chan_msg(self.channel_idx, full)
                if result.is_error():
                    log.error("channel send failed: %s", result.payload)
                    return
                await asyncio.sleep(0.2)


def split_lines(lines: list[str], *limits: int) -> list[str]:
    """Pack lines into chunks, each within a UTF-8 byte limit.

    *limits* gives an optional byte limit for each chunk in order (first,
    second, ...). If fewer limits are given than chunks produced, remaining
    chunks use the last given limit. With no limits, chunks are unbounded.
    """
    default_limit = limits[-1] if limits else 0
    chunks: list[str] = []
    current = ""
    chunk_idx = 0

    def limit_for(i: int) -> int:
        if i < len(limits):
            return limits[i]
        return default_limit

    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        limit = limit_for(chunk_idx)
        if limit == 0 or len(candidate.encode()) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
                chunk_idx += 1
            current = line
    if current:
        chunks.append(current)
    return chunks or [""]


def _hops_str(path_len: Any) -> str:
    """Format a hop count (flood/0/negative show as '0 hops')."""
    if not isinstance(path_len, int) or path_len <= 0 or path_len >= 255:
        return "0 hops"
    return f"{path_len} hop{'s' if path_len != 1 else ''}"
