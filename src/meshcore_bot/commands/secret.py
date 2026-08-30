"""fun commands: frog (secret) and hi (multilingual greeting)."""

from __future__ import annotations

from meshcore_bot.commands.base import Context, command


@command(["frog", "f"], secret=True)
async def _(ctx: Context, args: list[str]) -> None:
    """Send a frog emoji."""
    await ctx.reply("\U0001f438")


@command(
    [
        "hi",
        "hello",
        "howdy",
        "oi",
        "hey",
        "hallo",
        "moin",
        "servus",
        "salut",
        "bonjour",
    ],
    secret=True,
    allowed_everywhere=True,
)
async def _(ctx: Context, args: list[str]) -> None:
    """Greetings."""
    await ctx.reply(f"\U0001f44b {ctx.verb}")
