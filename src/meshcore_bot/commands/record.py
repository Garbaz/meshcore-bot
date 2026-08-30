"""record command: start/stop telemetry recording for the sender."""

from __future__ import annotations

from enum import Enum

from meshcore_bot.commands.base import Context, Scope, command


class RecordAction(Enum):
    """Subcommands for the record command."""

    START = "start"
    STOP = "stop"
    STATUS = "status"


_DEFAULT_PERIOD = 5  # minutes
_MIN_PERIOD = 1  # minute


def _format_status(period: int, readings: int) -> str:
    """Format a recording status line (shared by start/status/stop)."""
    mins = period // 60
    return f"{mins}min interval, {readings} readings"


@command(["record", "log", "r"], scope=Scope.DM_ONLY, secret=True)
class _:
    """Start, stop, or check telemetry recording."""

    default = "status"

    @staticmethod
    async def start(ctx: Context, period: int | None = None) -> None:
        """Start recording (N=min interval, default 5)."""
        if ctx.contact is None or ctx.telemetry is None:
            await ctx.reply("record only works in DMs")
            return

        pubkey = str(ctx.contact.get("public_key", ""))
        if not pubkey:
            await ctx.reply("Could not determine your public key")
            return

        mins = max(period or _DEFAULT_PERIOD, _MIN_PERIOD)

        info = ctx.telemetry.get_status(pubkey)
        if info is not None:
            await ctx.reply(
                f"Already recording: {_format_status(info['period'], info['readings'])}"
            )
            return

        ctx.telemetry.start_logging(pubkey, mins * 60)
        await ctx.reply(f"Started recording: {mins}min interval")

    @staticmethod
    async def stop(ctx: Context) -> None:
        """Stop recording."""
        if ctx.contact is None or ctx.telemetry is None:
            await ctx.reply("record only works in DMs")
            return

        pubkey = str(ctx.contact.get("public_key", ""))
        if not pubkey:
            await ctx.reply("Could not determine your public key")
            return

        info = ctx.telemetry.stop_logging(pubkey)
        if info is None:
            await ctx.reply("Not recording")
            return

        await ctx.reply(
            f"Stopped recording: {_format_status(info['period'], info['readings'])}"
        )

    @staticmethod
    async def status(ctx: Context) -> None:
        """Show recording status."""
        if ctx.contact is None or ctx.telemetry is None:
            await ctx.reply("record only works in DMs")
            return

        pubkey = str(ctx.contact.get("public_key", ""))
        if not pubkey:
            await ctx.reply("Could not determine your public key")
            return

        info = ctx.telemetry.get_status(pubkey)
        if info is None:
            await ctx.reply("Not recording")
            return

        await ctx.reply(
            f"Recording: {_format_status(info['period'], info['readings'])}"
        )
