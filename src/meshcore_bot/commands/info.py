"""ping and path commands (connectivity and routing info)."""

from __future__ import annotations

from typing import Any

from meshcore_bot.commands.base import Context, command
from meshcore_bot.registry import resolve_path


def _compress_route(lines: list[str], max_bytes: int) -> str:
    """Pack route lines into a single message, compressing middle hops if needed.

    Keeps the first and last hop always visible. If the full text exceeds the
    byte limit, replaces middle hops with a summary line until it fits.
    """
    text = "\n".join(lines)
    if len(text.encode()) <= max_bytes:
        return text

    while len(lines) > 3:
        _first, *middle, _last = lines[1:]  # skip header
        elided = len(middle)
        lines = [lines[0], f"({elided} hops)", lines[-1]]
        text = "\n".join(lines)
        if len(text.encode()) <= max_bytes:
            return text
    return text


@command(["ping", "p", "beep", "test"], require_mention=False)
async def _(ctx: Context, args: list[str]) -> None:
    """Test connectivity."""
    path_len: Any = ctx.msg.get("path_len")
    hops = (
        f"{path_len} hop(s)"
        if isinstance(path_len, int) and 0 <= path_len < 255
        else "0 hops (flood)"
    )
    verb = ctx.verb.lower()
    word = "Boop" if verb == "beep" else "Ack" if verb == "test" else "Pong"
    if ctx.location:
        await ctx.reply(f"{word} from {ctx.location}: {hops}")
    else:
        await ctx.reply(f"{word}: {hops}")


@command(["path", "r", "route", "trace"])
async def _(ctx: Context, args: list[str]) -> None:
    """Show the hop-by-hop path of the current message."""
    msg = ctx.msg
    path_len: Any = msg.get("path_len")

    loc = ctx.location or ""
    region = ctx.region_scope if not ctx.is_dm else ""

    def fmt(body: str) -> str:
        """Wrap *body* with optional location prefix."""
        return f"{loc}: {body}" if loc else body

    path_hex = str(msg.get("path") or "")

    if not isinstance(path_len, int) or path_len < 0 or path_len >= 255:
        r = f", region {region}" if region else ""
        await ctx.reply(fmt(f"0 hops (flood){r}"))
        return

    if not path_hex:
        r = f", region {region}" if region else ""
        await ctx.reply(fmt(f"{path_len} hop(s){r}"))
        return

    org = ctx.origin
    hops = resolve_path(ctx.registry, path_hex, ctx.path_hash_width, org)
    r = f" on region {region}" if region else ""
    header = fmt(f"{path_len} hop(s){r}:")
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
    await ctx.reply(_compress_route(lines, ctx.reply_text_limit))
