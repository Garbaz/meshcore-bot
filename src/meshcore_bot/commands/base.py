"""Command framework: Context, registry, and help generation."""

from __future__ import annotations

import asyncio
import logging
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
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


CommandFunc = Callable[..., Awaitable[None]]


class Scope(Enum):
    """Where a command is available.

    - DM_ONLY: DMs only, silently ignored in channels
    - MENTION: always in DMs, requires @mention in channels
    - OPEN: always responds (channels + DMs)
    """

    DM_ONLY = "dm_only"
    MENTION = "mention"
    OPEN = "open"


@dataclass
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
        if self.path_hash_mode >= 0:
            await self.mc.commands.set_path_hash_mode(self.path_hash_mode)
        if self.is_dm and self.contact is not None:
            chunks = split_lines(text.splitlines(), max_dm_text_bytes())
            for chunk in chunks:
                result = await self.mc.commands.send_msg_with_retry(self.contact, chunk)
                if result is None:
                    log.error("DM send failed (no ack) for %s", self.sender)
                    return
                await asyncio.sleep(0.2)
        elif self.channel_idx is not None:
            if self.flood_scope is None:
                await self.mc.commands.force_unscoped()
            else:
                await self.mc.commands.set_flood_scope(self.flood_scope)
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
    """Format a hop count, handling flood (255) and direct (0)."""
    if not isinstance(path_len, int) or path_len < 0 or path_len >= 255:
        return "flood"
    if path_len == 0:
        return "direct"
    return f"{path_len} hop{'s' if path_len != 1 else ''}"


@dataclass
class Command:
    aliases: list[str]
    func: CommandFunc
    scope: Scope
    min_args: int
    secret: bool
    allowed_everywhere: bool
    usage: str = ""

    @property
    def name(self) -> str:
        return self.aliases[0]


_commands: dict[str, Command] = {}


def command(
    aliases: list[str] | str,
    *,
    min_args: int = 0,
    secret: bool = False,
    scope: Scope = Scope.MENTION,
    allowed_everywhere: bool = False,
    usage: str = "",
) -> Callable[[CommandFunc], CommandFunc]:
    """Register *func* as a bot command.

    *aliases* is the canonical name (first entry) plus any aliases. A bare
    string is treated as a single-element list. *usage* is the argument hint
    appended to the name in help output (e.g. ``"[place]"``). Secret commands
    are not shown in help. *scope* controls where the command is available.
    *allowed_everywhere* bypasses per-channel command restrictions.
    """
    alias_list = [aliases] if isinstance(aliases, str) else list(aliases)

    def decorator(func: CommandFunc) -> CommandFunc:
        cmd = Command(
            alias_list, func, scope, min_args, secret, allowed_everywhere, usage
        )
        for a in alias_list:
            _commands[a.lower()] = cmd
        return func

    return decorator


def get_command(name: str) -> Command | None:
    return _commands.get(name.lower())


def list_commands(
    is_dm: bool = True, channel_allowed: set[str] | None = None
) -> list[Command]:
    """All registered commands, deduplicated (one entry per canonical name).

    Secret commands are excluded. When *is_dm* is False, DM_ONLY commands
    are also excluded.  When *channel_allowed* is not None (a restricted
    channel), commands that are neither ``allowed_everywhere`` nor in
    *channel_allowed* are also excluded.
    """
    seen: set[str] = set()
    out: list[Command] = []
    for cmd in _commands.values():
        if cmd.name not in seen and not cmd.secret:
            if not is_dm and cmd.scope is Scope.DM_ONLY:
                continue
            if (
                not is_dm
                and channel_allowed is not None
                and not cmd.allowed_everywhere
                and cmd.name not in channel_allowed
            ):
                continue
            seen.add(cmd.name)
            out.append(cmd)
    return out


def usage_line(cmd: Command) -> str:
    """Build the usage string from the command name and argument hint."""
    return f"{cmd.name} {cmd.usage}".strip()


def _doc_body(cmd: Command) -> str:
    """The docstring, stripped."""
    return (cmd.func.__doc__ or "(no help available)").strip() or "(no help available)"


def full_help(is_dm: bool = True, channel_allowed: set[str] | None = None) -> str:
    """The text shown for ``help`` / ``?`` with no arguments."""
    lines = ["mention or DM me:"]
    for cmd in sorted(list_commands(is_dm, channel_allowed), key=lambda c: c.name):
        lines.append(f"  {usage_line(cmd)}")
    lines.append("(?cmd for details)")
    return "\n".join(lines)


def command_help(name: str) -> str | None:
    """The text shown for ``?<name>``: docstring plus aliases."""
    cmd = get_command(name)
    if cmd is None:
        return None
    lines = [_doc_body(cmd)]
    extra = cmd.aliases[1:]
    if extra:
        lines.append(f"Aliases: {', '.join(extra)}")
    return "\n".join(lines)
