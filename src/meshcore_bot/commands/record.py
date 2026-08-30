"""record command: start/stop telemetry recording for the sender."""

from meshcore_bot.commands.base import command
from meshcore_bot.commands.context import Context

_DEFAULT_PERIOD = 5  # minutes
_MIN_PERIOD = 1  # minute


def _format_status(period: int, readings: int) -> str:
    """Format a recording status line (shared by start/status/stop)."""
    mins = period // 60
    return f"{mins}min interval, {readings} readings"


def _pubkey(ctx: Context) -> str:
    """Extract the sender's full public key."""
    assert ctx.contact is not None  # DM_ONLY: contact always set
    return str(ctx.contact["public_key"])


@command(["record", "log", "r"], dm_only=True, secret=True)
class _:
    """start, stop, or check telemetry recording"""

    default = "status"

    @staticmethod
    async def start(ctx: Context, period: int | None = None) -> None:
        """Start recording (N=min interval, default 5)."""
        assert ctx.telemetry is not None  # always set in main()
        pubkey = _pubkey(ctx)
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
        assert ctx.telemetry is not None
        pubkey = _pubkey(ctx)

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
        assert ctx.telemetry is not None
        pubkey = _pubkey(ctx)

        info = ctx.telemetry.get_status(pubkey)
        if info is None:
            await ctx.reply("Not recording")
            return

        await ctx.reply(
            f"Recording: {_format_status(info['period'], info['readings'])}"
        )
