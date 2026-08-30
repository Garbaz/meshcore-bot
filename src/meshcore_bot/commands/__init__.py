"""Command registry and implementations for the meshcore bot.

Commands are registered with the ``@command`` decorator. The first entry in
*aliases* is the canonical name; the rest are hidden aliases. The optional
*usage* string gives the argument hint shown in help. ``?`` with no argument
shows the full help; ``?<name>`` shows details for a specific command.
"""

from __future__ import annotations

# Import command modules to trigger registration.
from meshcore_bot.commands import info, misc, record, secret, weather  # noqa: F401
from meshcore_bot.commands.base import (
    Context,
    Scope,
    command,
    command_help,
    full_help,
    get_command,
    list_commands,
    usage_line,
)

__all__ = [
    "Context",
    "Scope",
    "command",
    "command_help",
    "full_help",
    "get_command",
    "list_commands",
    "usage_line",
]
