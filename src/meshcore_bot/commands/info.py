"""ping and path commands (connectivity and routing info)."""

from __future__ import annotations

from typing import Any

from meshcore_bot.commands.base import LOCATION, Context, command, origin


def _compress_route(lines: list[str], max_bytes: int) -> str:
    """Pack route lines into a single message, compressing middle hops if needed.

    Keeps the first and last hop always visible. If the full text exceeds the
    byte limit, replaces middle hops with a summary line until it fits.
    """
    text = "\n".join(lines)
    if len(text.encode("utf-8")) <= max_bytes:
        return text

    while len(lines) > 3:
        _first, *middle, _last = lines[1:]  # skip header
        elided = len(middle)
        lines = [lines[0], f"({elided} hops)", lines[-1]]
        text = "\n".join(lines)
        if len(text.encode("utf-8")) <= max_bytes:
            return text
    return text


@command(["ping", "p", "beep", "test"], require_mention=False)
async def _(ctx: Context, args: list[str]) -> None:
    """Test connectivity.

    Usage: ping
    """
    path_len: Any = ctx.msg.get("path_len")
    hops = (
        f"{path_len} hop(s)"
        if isinstance(path_len, int) and 0 <= path_len < 255
        else "0 hops (flood)"
    )
    word = "Boop" if ctx.verb.lower() == "beep" else "Pong"
    if ctx.verb.lower() == "test":
        word = "Ack"
    if LOCATION:
        await ctx.reply(f"{word} from {LOCATION}: {hops}")
    else:
        await ctx.reply(f"{word}: {hops}")


@command(["path", "r", "route", "trace"])
async def _(ctx: Context, args: list[str]) -> None:
    """Show the hop-by-hop path of the current message.

    Usage: path
    """
    from meshcore_bot.registry import resolve_path

    msg = ctx.msg
    path_len: Any = msg.get("path_len")

    loc = LOCATION if LOCATION else ""

    path_hex = str(msg.get("path") or "")

    if not isinstance(path_len, int) or path_len < 0 or path_len >= 255:
        suffix = (
            f", region {ctx.region_scope}" if not ctx.is_dm and ctx.region_scope else ""
        )
        await ctx.reply(
            f"{loc}: 0 hops (flood){suffix}" if loc else f"0 hops (flood){suffix}"
        )
        return

    if not path_hex:
        suffix = (
            f", region {ctx.region_scope}" if not ctx.is_dm and ctx.region_scope else ""
        )
        await ctx.reply(
            f"{loc}: {path_len} hop(s){suffix}" if loc else f"{path_len} hop(s){suffix}"
        )
        return

    hash_size: Any = msg.get("path_hash_size")
    width = hash_size if isinstance(hash_size, int) and hash_size > 0 else 1

    org = origin(ctx.mc)
    hops = resolve_path(ctx.registry, path_hex, width, org)
    suffix = (
        f" on region {ctx.region_scope}" if not ctx.is_dm and ctx.region_scope else ""
    )
    header = (
        f"{loc}: {path_len} hop(s){suffix}:" if loc else f"{path_len} hop(s){suffix}:"
    )
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
