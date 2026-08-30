"""help and add commands."""

from __future__ import annotations

from urllib.parse import quote

from meshcore_bot.commands.base import Context, command, command_help, full_help


@command(["help", "h", "?"], usage="[command]", allowed_everywhere=True)
async def _(ctx: Context, args: list[str]) -> None:
    """Show help."""
    if args:
        h = command_help(args[0])
        if h is None:
            await ctx.reply(f"Unknown command: {args[0]}")
            return
        await ctx.reply(h)
        return
    await ctx.reply(full_help(ctx.is_dm, ctx.channel_allowed))


@command(["add", "a", "key"])
async def _(ctx: Context, args: list[str]) -> None:
    """Show this bot's share link for adding it as a contact."""
    name = str(ctx.mc.self_info.get("name", ""))
    pubkey = str(ctx.mc.self_info.get("public_key", ""))
    if not pubkey:
        await ctx.reply("Error: no public key available")
        return
    uri = f"meshcore://contact/add?name={quote(name)}&public_key={pubkey}&type=1"
    await ctx.reply(uri)
