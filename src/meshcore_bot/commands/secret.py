"""fun commands: frog (secret) and hi (multilingual greeting)."""

from __future__ import annotations

from meshcore_bot.commands.base import Context, command

_GREETINGS = {
    "hi": "hi",
    "hello": "hello",
    "hallo": "hallo",
    "moin": "moin",
    "salut": "salut",
    "bonjour": "bonjour",
    "hola": "hola",
    "howdy": "howdy",
    "oi": "oi",
}


@command(["frog", "f"], secret=True)
async def _(ctx: Context, args: list[str]) -> None:
    """Send a frog emoji.

    Usage: frog
    """
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
    ]
)
async def _(ctx: Context, args: list[str]) -> None:
    """Greetings.

    Usage: hi
    """
    greeting = _GREETINGS.get(ctx.verb.lower(), "hi")
    await ctx.reply(f"\U0001f44b {greeting}")
