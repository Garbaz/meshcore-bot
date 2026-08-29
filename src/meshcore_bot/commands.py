"""Command registry and implementations for the meshcore bot.

Commands are registered with the ``@command`` decorator. The function name
(less a ``cmd_`` prefix) becomes the canonical command name. Optional
``aliases`` are not shown in the general help listing but are listed when
viewing ``?<command>`` for a specific command. ``?`` with no argument shows
the full help.
"""

from __future__ import annotations

import asyncio
import logging
import shlex
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meshcore import MeshCore

    from meshcore_bot.registry import NodeRegistry

log = logging.getLogger("meshcore_bot.commands")

MAX_TEXT_LEN = 160  # firmware limit in UTF-8 bytes

# Shown in ping/path replies. If empty, the "from <location>" part is omitted.
LOCATION = "Freiburg im Bresigau"

# Type alias for a command handler function.
CommandFunc = Callable[..., Awaitable[None]]


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class Context:
    """Everything a command handler needs to do its job and reply."""

    mc: MeshCore
    registry: NodeRegistry
    sender: str  # display name of the sender ("unknown" if not determinable)
    bot_name: str  # this companion's advertised name
    region_scope: str  # default flood scope name (e.g. "#de-bw") or "" if none
    is_dm: bool
    path_hash_mode: int = 0  # hash mode of the incoming message
    verb: str = ""  # the actual name/alias the user invoked (e.g. "route", "trace")
    channel_idx: int | None = None
    msg: dict[str, Any] = field(default_factory=dict)
    contact: dict[str, Any] | None = None

    async def reply(self, text: str) -> None:
        """Send *text* back to the originating DM or channel."""
        # Match the sender's path hash mode and region scope so replies
        # traverse the same routing.
        if self.path_hash_mode >= 0:
            await self.mc.commands.set_path_hash_mode(self.path_hash_mode)
        if self.is_dm and self.contact is not None:
            chunks = _split_lines(text.splitlines(), MAX_TEXT_LEN)
            for chunk in chunks:
                result = await self.mc.commands.send_msg(self.contact, chunk)
                if result.is_error():
                    log.error("send failed: %s", result.payload)
                    return
                await asyncio.sleep(0.2)
        elif self.channel_idx is not None:
            await self.mc.commands.set_flood_scope(self.region_scope or "")
            prefix = f"@[{self.sender}]: " if self.sender != "unknown" else ""
            # First chunk must fit prefix + content within the byte limit.
            limits = [MAX_TEXT_LEN - len(prefix.encode("utf-8"))] if prefix else []
            chunks = _split_lines(text.splitlines(), *limits)
            for i, chunk in enumerate(chunks):
                full = prefix + chunk if i == 0 else chunk
                result = await self.mc.commands.send_chan_msg(self.channel_idx, full)
                if result.is_error():
                    log.error("channel send failed: %s", result.payload)
                    return
                await asyncio.sleep(0.2)


def _split_lines(lines: list[str], *limits: int) -> list[str]:
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
        if limit == 0 or len(candidate.encode("utf-8")) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
                chunk_idx += 1
            current = line
    if current:
        chunks.append(current)
    return chunks or [""]


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------


@dataclass
class Command:
    name: str
    aliases: list[str]
    func: CommandFunc
    require_mention: bool
    min_args: int


_commands: dict[str, Command] = {}


def command(
    aliases: list[str] | None = None, *, require_mention: bool = True, min_args: int = 0
) -> Callable[[CommandFunc], CommandFunc]:
    """Register *func* as a bot command.

    The canonical name is derived from the function name by stripping a
    ``cmd_`` prefix. Aliases are hidden from the general help listing but
    listed under ``?<command>``.
    """
    alias_list = aliases or []

    def decorator(func: CommandFunc) -> CommandFunc:
        name = func.__name__.lstrip("_")
        cmd = Command(name, alias_list, func, require_mention, min_args)
        _commands[name.lower()] = cmd
        for a in alias_list:
            _commands[a.lower()] = cmd
        return func

    return decorator


def get_command(name: str) -> Command | None:
    return _commands.get(name.lower())


def list_commands() -> list[Command]:
    """All registered commands, deduplicated (one entry per canonical name)."""
    seen: set[str] = set()
    out: list[Command] = []
    for cmd in _commands.values():
        if cmd.name not in seen:
            seen.add(cmd.name)
            out.append(cmd)
    return out


def _usage_line(cmd: Command) -> str:
    """Extract the 'Usage:' line from the docstring, or fall back to name only."""
    doc = (cmd.func.__doc__ or "").strip()
    for line in doc.splitlines():
        line = line.strip()
        if line.lower().startswith("usage:"):
            return line.removeprefix("Usage:").removeprefix("usage:").strip()
    return cmd.name


def _doc_body(cmd: Command) -> str:
    """The docstring with the Usage: line removed, stripped."""
    doc = (cmd.func.__doc__ or "(no help available)").strip()
    lines = doc.splitlines()
    body = [ln for ln in lines if not ln.strip().lower().startswith("usage:")]
    return "\n".join(body).strip() or "(no help available)"


def full_help(bot_name: str) -> str:
    """The text shown for ``help`` / ``?`` with no arguments."""
    lines = ["mention/DM me."]
    for cmd in sorted(list_commands(), key=lambda c: c.name):
        usage = _usage_line(cmd)
        lines.append(f"  {usage}")
    lines.append("?cmd for details")
    return "\n".join(lines)


def command_help(name: str) -> str | None:
    """The text shown for ``?<name>``: docstring plus aliases."""
    cmd = get_command(name)
    if cmd is None:
        return None
    lines = [_doc_body(cmd)]
    if cmd.aliases:
        lines.append(f"Aliases: {', '.join(cmd.aliases)}")
    return "\n".join(lines)


# ---------------------------------------------------------------------------
# Parsing
# ---------------------------------------------------------------------------


def parse_command(text: str) -> tuple[str, list[str]] | None:
    """Split *text* into ``(verb, args)`` using shell-style tokenisation."""
    try:
        tokens = shlex.split(text)
    except ValueError:
        return None
    if not tokens:
        return None
    return tokens[0], tokens[1:]


# ---------------------------------------------------------------------------
# Commands
# ---------------------------------------------------------------------------


@command(["?"], require_mention=True)
async def _help(ctx: Context, args: list[str]) -> None:
    """Show help.

    Usage: help [command]
    """
    if args:
        h = command_help(args[0])
        if h is None:
            await ctx.reply(f"Unknown command: {args[0]}")
            return
        await ctx.reply(h)
        return
    await ctx.reply(full_help(ctx.bot_name))


@command(["p", "beep"], require_mention=True)
async def _ping(ctx: Context, args: list[str]) -> None:
    """Test connectivity.

    Usage: ping
    """
    path_len: Any = ctx.msg.get("path_len")
    hops = (
        f"{path_len} hop(s)"
        if isinstance(path_len, int) and 0 <= path_len < 255
        else "0 hops"
    )
    word = "Boop" if ctx.verb.lower() == "beep" else "Pong"
    if LOCATION:
        await ctx.reply(f"{word} from {LOCATION} in {hops}")
    else:
        await ctx.reply(f"{word} in {hops}")


@command(["route", "trace"], require_mention=True)
async def _path(ctx: Context, args: list[str]) -> None:
    """Show the hop-by-hop path of the current message.

    Usage: path
    """
    from meshcore_bot.registry import resolve_path

    msg = ctx.msg
    path_len: Any = msg.get("path_len")

    loc = f"to {LOCATION}" if LOCATION else ""
    label = ctx.verb.capitalize()

    # DMs: no path hex available, only hop count.
    # Channels: msg["path"] has the actual incoming route hex from the packet log.
    path_hex = str(msg.get("path") or "")

    if not isinstance(path_len, int) or path_len < 0 or path_len >= 255:
        suffix = (
            f", region {ctx.region_scope}" if not ctx.is_dm and ctx.region_scope else ""
        )
        await ctx.reply(f"{label} {loc}: 0 hops (flood){suffix}")
        return

    if not path_hex:
        suffix = (
            f", region {ctx.region_scope}" if not ctx.is_dm and ctx.region_scope else ""
        )
        await ctx.reply(f"{label} {loc}: {path_len} hop(s){suffix}")
        return

    hash_size: Any = msg.get("path_hash_size")
    width = hash_size if isinstance(hash_size, int) and hash_size > 0 else 1

    origin = _origin(ctx.mc)
    hops = resolve_path(ctx.registry, path_hex, width, origin)
    suffix = (
        f" on region {ctx.region_scope}" if not ctx.is_dm and ctx.region_scope else ""
    )
    header = f"{label} {loc} in {path_len} hop(s){suffix}:"
    lines = [header]
    for i, hop in enumerate(hops, start=1):
        if hop.node is not None:
            name = hop.node.name or hop.hex
            if hop.ambiguous:
                name += " (?)"
            if hop.node.role == "room":
                name += " [room]"
            lines.append(f"{i}. {hop.hex} {name}")
        else:
            lines.append(f"{i}. {hop.hex} (unknown)")
    await ctx.reply(_compress_route(lines))


@command(["key"], require_mention=True)
async def _add(ctx: Context, args: list[str]) -> None:
    """Show this bot's contact URI for adding it as a contact.

    Usage: add
    """
    result = await ctx.mc.commands.export_contact()
    if result.is_error():
        await ctx.reply(f"Error: {result.payload}")
        return
    uri = result.payload.get("uri", "")
    await ctx.reply(f"Add me: {uri}")


@command(["w"], require_mention=True)
async def _weather(ctx: Context, args: list[str]) -> None:
    """Show current weather.

    Usage: weather [place]
    """
    if args:
        place = " ".join(args)
        coords = await _geocode(place)
        if coords is None:
            await ctx.reply(f"Could not find: {place}")
            return
        lat, lon, name = coords
    else:
        coords = _path_location(ctx)
        if coords is not None:
            lat, lon, name = coords
        else:
            lat, lon = 0.0, 0.0
            name = LOCATION or "here"

    result = await _fetch_weather(lat, lon)
    if result is None:
        await ctx.reply("Weather data unavailable.")
        return
    weather, rain_min = result

    temp = weather["temperature_2m"]
    feels = weather.get("apparent_temperature")
    code = weather.get("weather_code", 0)
    cond = _WMO_CODES.get(code, "unknown")
    wind = weather.get("wind_speed_10m")
    humidity = weather.get("relative_humidity_2m")

    line1 = name
    parts = [cond]
    parts.append("\U0001f321\ufe0f " + f"{temp:.0f}C")
    if feels is not None and abs(feels - temp) >= 2:
        parts.append(f"({feels:.0f}C)")
    if wind is not None:
        parts.append("\U0001f32c\ufe0f " + f"{wind:.0f}kmh")
    if humidity is not None:
        parts.append("\U0001f4a7 " + f"{humidity:.0f}%")
    if rain_min == 0:
        parts.append("\u2614\ufe0f raining")
    elif rain_min is not None:
        parts.append("\u2614\ufe0f " + f"in {rain_min}min")
    else:
        parts.append("\U0001f302 no rain")
    line2 = " ".join(parts)
    await ctx.reply(f"{line1}\n{line2}")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _compress_route(lines: list[str]) -> str:
    """Pack route lines into a single message, compressing middle hops if needed.

    Keeps the first and last hop always visible. If the full text exceeds the
    byte limit, replaces middle hops with a summary line until it fits.
    """
    text = "\n".join(lines)
    if len(text.encode("utf-8")) <= MAX_TEXT_LEN:
        return text

    while len(lines) > 3:
        _first, *middle, _last = lines[1:]  # skip header
        elided = len(middle)
        lines = [lines[0], f"({elided} hops)", lines[-1]]
        text = "\n".join(lines)
        if len(text.encode("utf-8")) <= MAX_TEXT_LEN:
            return text
    return text


def _origin(mc: MeshCore) -> tuple[float, float] | None:
    info = mc.self_info or {}
    lat: Any = info.get("adv_lat")
    lon: Any = info.get("adv_lon")
    if lat is None or lon is None or (lat == 0 and lon == 0):
        return None
    return (float(lat), float(lon))


def _path_location(ctx: Context) -> tuple[float, float, str] | None:
    """Find the nearest located repeater on the message path.

    For channel messages, ``msg["path"]`` is the incoming route (sender side
    first). For DMs, the contact's ``out_path`` is the reply route (our side
    first). In both cases we want the hop closest to the sender that has a
    known location.
    """
    from meshcore_bot.registry import resolve_path

    path_hex = str(ctx.msg.get("path") or "")
    if not path_hex and ctx.contact is not None:
        path_hex = str(ctx.contact.get("out_path") or "")

    if not path_hex:
        return None

    hash_size: Any = ctx.msg.get("path_hash_size")
    if not isinstance(hash_size, int) or hash_size <= 0:
        hm: Any = ctx.msg.get("path_hash_mode")
        if isinstance(hm, int) and hm >= 0:
            hash_size = hm + 1
        elif ctx.contact is not None:
            ohm: Any = ctx.contact.get("out_path_hash_mode")
            hash_size = (ohm + 1) if isinstance(ohm, int) and ohm >= 0 else 1
        else:
            hash_size = 1

    origin = _origin(ctx.mc)
    hops = resolve_path(ctx.registry, path_hex, hash_size, origin)

    # For incoming path (channels): first hop is closest to sender.
    # For out_path (DMs): last hop is closest to sender.
    is_incoming = bool(ctx.msg.get("path"))
    ordered = hops if is_incoming else list(reversed(hops))

    for hop in ordered:
        if (
            hop.node is not None
            and hop.node.lat is not None
            and hop.node.lon is not None
        ):
            name = hop.node.name or hop.hex
            return (hop.node.lat, hop.node.lon, name)
    return None


# ---------------------------------------------------------------------------
# Weather (Open-Meteo, no API key required)
# ---------------------------------------------------------------------------

_WMO_CODES: dict[int, str] = {
    0: "\u2600\ufe0f clear",
    1: "\U0001f324\ufe0f mainly clear",
    2: "\u26c5 partly cloudy",
    3: "\u2601\ufe0f overcast",
    45: "\U0001f32b\ufe0f fog",
    48: "\U0001f32b\ufe0f rime fog",
    51: "\U0001f327\ufe0f light drizzle",
    53: "\U0001f327\ufe0f drizzle",
    55: "\U0001f327\ufe0f dense drizzle",
    56: "\U0001f327\ufe0f freezing drizzle",
    57: "\U0001f327\ufe0f freezing drizzle",
    61: "\U0001f327\ufe0f light rain",
    63: "\U0001f327\ufe0f rain",
    65: "\U0001f327\ufe0f heavy rain",
    66: "\U0001f327\ufe0f freezing rain",
    67: "\U0001f327\ufe0f freezing rain",
    71: "\u2744\ufe0f light snow",
    73: "\u2744\ufe0f snow",
    75: "\u2744\ufe0f heavy snow",
    77: "\u2744\ufe0f snow grains",
    80: "\U0001f326\ufe0f light showers",
    81: "\U0001f326\ufe0f showers",
    82: "\U0001f326\ufe0f violent showers",
    85: "\u2744\ufe0f snow showers",
    86: "\u2744\ufe0f snow showers",
    95: "\U0001f329\ufe0f thunderstorm",
    96: "\U0001f329\ufe0f thunderstorm+hail",
    99: "\U0001f329\ufe0f thunderstorm+hail",
}

_RAIN_CODES = {51, 53, 55, 56, 57, 61, 63, 65, 66, 67, 80, 81, 82, 95, 96, 99}

_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"


async def _geocode(place: str) -> tuple[float, float, str] | None:
    """Resolve a place name to (lat, lon, display_name) via Open-Meteo geocoding."""
    import httpx

    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_GEOCODE_URL, params={"name": place, "count": 1})
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    results = data.get("results")
    if not results:
        return None
    r = results[0]
    name_parts = [r.get("name", ""), r.get("country", "")]
    name = ", ".join(p for p in name_parts if p) or place
    return (r["latitude"], r["longitude"], name)


async def _fetch_weather(
    lat: float, lon: float
) -> tuple[dict[str, Any], int | None] | None:
    """Fetch current weather and nearest rain from Open-Meteo.

    Returns (current_conditions, rain_in_minutes) or None. rain_in_minutes is
    None if no precipitation forecast in the next 3 hours.
    """
    import httpx

    params: dict[str, str | float] = {
        "latitude": lat,
        "longitude": lon,
        "current": "temperature_2m,apparent_temperature,weather_code,wind_speed_10m,relative_humidity_2m",
        "minutely_15": "precipitation",
        "forecast_minutely_15": "12",
        "timezone": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_WEATHER_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        return None
    current = data.get("current")
    if not isinstance(current, dict):
        return None

    rain_min: int | None = None
    minute_data = data.get("minutely_15")
    if isinstance(minute_data, dict):
        precip = minute_data.get("precipitation", [])
        if isinstance(precip, list) and precip:
            if isinstance(precip[0], (int, float)) and precip[0] > 0:
                rain_min = 0
            else:
                for i, val in enumerate(precip[1:], start=1):
                    if isinstance(val, (int, float)) and val > 0:
                        rain_min = i * 15
                        break

    return current, rain_min
