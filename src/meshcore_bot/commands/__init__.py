"""Command registry and implementations for the meshcore bot.

Commands are registered with the ``@command`` decorator. The function name
(less a ``cmd_`` prefix) becomes the canonical command name. Optional
``aliases`` are not shown in the general help listing but are listed when
viewing ``?<command>`` for a specific command. ``?`` with no argument shows
the full help.
"""

from __future__ import annotations

# Import command modules to trigger registration.
from meshcore_bot.commands import info, misc, secret, weather  # noqa: F401
from meshcore_bot.commands.base import (
    Context,
    command,
    command_help,
    full_help,
    get_command,
    list_commands,
    parse_command,
    usage_line,
)

__all__ = [
    "Context",
    "command",
    "command_help",
    "full_help",
    "get_command",
    "list_commands",
    "parse_command",
    "usage_line",
]
