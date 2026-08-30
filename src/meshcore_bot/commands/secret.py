"""fun commands: frog (secret) and hi (multilingual greeting)."""

from meshcore_bot.commands.base import command
from meshcore_bot.commands.context import Context


@command(["frog", "f"], secret=True)
async def frog_cmd(ctx: Context) -> None:
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
async def hi_cmd(ctx: Context) -> None:
    """Greetings."""
    await ctx.reply(f"\U0001f44b {ctx.verb}")
