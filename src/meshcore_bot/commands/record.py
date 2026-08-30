"""record command: start/stop telemetry logging for the sender."""

from __future__ import annotations

import time

from meshcore_bot.commands.base import Context, Scope, command

_DEFAULT_PERIOD = 5  # minutes
_MIN_PERIOD = 1  # minute


@command(["record", "log", "r"], scope=Scope.DM_ONLY, secret=True, usage="[start|stop]")
async def _(ctx: Context, args: list[str]) -> None:
    """Start or stop telemetry logging for your companion."""
    if ctx.contact is None or ctx.telemetry is None:
        await ctx.reply("record only works in DMs")
        return

    pubkey = str(ctx.contact.get("public_key", ""))
    if not pubkey:
        await ctx.reply("Could not determine your public key")
        return

    if not args or args[0] == "status":
        info = ctx.telemetry.get_status(pubkey)
        if info is None:
            await ctx.reply("Not recording")
        else:
            mins = info["period"] // 60
            await ctx.reply(f"Recording every {mins}min, {info['readings']} readings")
        return

    action = args[0].lower()
    if action == "start":
        period = _DEFAULT_PERIOD
        if len(args) > 1:
            try:
                period = max(int(args[1]), _MIN_PERIOD)
            except ValueError:
                await ctx.reply(f"usage: record start [{_MIN_PERIOD}+ min]")
                return
        if ctx.telemetry.is_logging(pubkey):
            await ctx.reply("Already recording")
            return
        ctx.telemetry.start_logging(pubkey, period * 60)
        await ctx.reply(f"Recording every {period}min")
        return

    if action == "stop":
        info = ctx.telemetry.stop_logging(pubkey)
        if info is None:
            await ctx.reply("Not recording")
            return
        duration = int(time.time()) - info.get("started_at", 0)
        mins = duration // 60
        await ctx.reply(f"Stopped. {info['readings']} readings over {mins}min")
        return

    await ctx.reply("usage: record start|stop")
