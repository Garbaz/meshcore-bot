"""help and add commands."""

from __future__ import annotations

from urllib.parse import quote

from meshcore_bot.commands.base import Context, command, command_help, full_help


@command(["help", "h", "?"], allowed_everywhere=True)
async def help_cmd(ctx: Context, cmd: str | None = None) -> None:
    """Show help for a command, or list all commands."""
    if cmd is not None:
        # Support "?record start" syntax via the verb passed through dispatch.
        h = command_help(cmd)
        if h is None:
            await ctx.reply(f"Unknown command: {cmd}")
            return
        await ctx.reply(h)
        return
    await ctx.reply(full_help(ctx.is_dm, ctx.channel_allowed))


@command(["add", "a", "key"])
async def add_cmd(ctx: Context) -> None:
    """Show this bot's share link for adding it as a contact."""
    name = str(ctx.mc.self_info.get("name", ""))
    pubkey = str(ctx.mc.self_info.get("public_key", ""))
    if not pubkey:
        await ctx.reply("Error: no public key available")
        return
    uri = f"meshcore://contact/add?name={quote(name)}&public_key={pubkey}&type=1"
    await ctx.reply(uri)
