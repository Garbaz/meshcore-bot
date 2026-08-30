"""Command framework: Context, registry, arg parsing, and help generation.

Commands are registered with ``@command``.  The decorator introspects the
handler's signature to build typed argument parsing and auto-generated usage
strings — no manual ``usage=...`` or ``min_args=...`` needed.

For commands with subcommands (e.g. ``record start|stop|status``), pass a
class with ``staticmethod`` async methods instead of a function::

    @command(["record"], scope=Scope.DM_ONLY, secret=True)
    class _:
        default = "status"

        @staticmethod
        async def start(ctx: Context, period: int | None = None) -> None: ...

        @staticmethod
        async def stop(ctx: Context) -> None: ...

Supported parameter types (after ``ctx``):

    str               — required single token
    str | None        — optional single token (0 or 1)
    list[str]         — consume all remaining tokens
    list[str] | None  — same as list[str] but optional
    int               — required integer token
    int | None        — optional integer token
    Enum subclass     — required token validated against enum values
    Enum | None       — optional token validated against enum values
"""

from __future__ import annotations

import asyncio
import inspect
import logging
import types
import typing
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from enum import Enum
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from meshcore import MeshCore

    from meshcore_bot.registry import NodeRegistry
    from meshcore_bot.telemetry import TelemetryLogger

log = logging.getLogger("meshcore_bot.commands")

MAX_TEXT_PAYLOAD = 160  # firmware MAX_TEXT_LEN (10 * CIPHER_BLOCK_SIZE)
MAX_FRAME_SIZE = 172  # USB serial max frame (firmware 1.16+)

_DM_OVERHEAD = 16  # cmd(1)+type(1)+attempt(1)+ts(4)+pubkey(6)+null(1)+safety(2)
_CHAN_OVERHEAD = 10  # cmd(1)+type(1)+chan_idx(1)+ts(4)+null(1)+safety(2)


def max_dm_text_bytes() -> int:
    """Max UTF-8 bytes for a direct message."""
    return min(MAX_FRAME_SIZE - _DM_OVERHEAD, MAX_TEXT_PAYLOAD)


def max_channel_text_bytes(sender_name: str) -> int:
    """Max UTF-8 bytes for a channel message (firmware prepends 'name: ')."""
    prefix_len = len(sender_name.encode()) + 2  # ": "
    return min(MAX_TEXT_PAYLOAD - prefix_len, MAX_FRAME_SIZE - _CHAN_OVERHEAD)


# ---------------------------------------------------------------------------
# Scope
# ---------------------------------------------------------------------------


class Scope(Enum):
    """Where a command is available.

    - DM_ONLY: DMs only, silently ignored in channels
    - MENTION: always in DMs, requires @mention in channels
    - OPEN: always responds (channels + DMs)
    """

    DM_ONLY = "dm_only"
    MENTION = "mention"
    OPEN = "open"


# ---------------------------------------------------------------------------
# Context
# ---------------------------------------------------------------------------


@dataclass
class Context:
    """Everything a command handler needs to do its job and reply."""

    mc: MeshCore
    registry: NodeRegistry
    sender: str  # display name of the sender ("unknown" if not determinable)
    bot_name: str  # this companion's advertised name
    is_dm: bool
    path_hash_mode: int = 0  # hash mode of the incoming message
    flood_scope: bytes | None = (
        b"\0" * 16
    )  # 16-byte scope key, b"\0"*16 = default, None = unscoped
    flood_scope_name: str = ""  # display name for the flood scope (e.g. "#de-bw")
    verb: str = ""  # the actual name/alias the user invoked (e.g. "route", "trace")
    channel_idx: int | None = None
    channel_name: str = ""  # name of the channel (e.g. "#ping") or "" for DMs
    channel_allowed: set[str] | None = None  # allowed command names, or None for all
    msg: dict[str, Any] = field(default_factory=dict)
    contact: dict[str, Any] | None = None
    location: str | None = None
    telemetry: TelemetryLogger | None = None
    budget_check: Callable[[], bool] | None = None  # channel budget; None for DMs

    @property
    def origin(self) -> tuple[float, float] | None:
        """The companion's own advertised coordinates, or None."""
        info = self.mc.self_info or {}
        lat: Any = info.get("adv_lat")
        lon: Any = info.get("adv_lon")
        if lat is None or lon is None or (lat == 0 and lon == 0):
            return None
        return (float(lat), float(lon))

    @property
    def path_hash_width(self) -> int:
        """Path hash width in bytes, falling back through available fields."""
        hs: Any = self.msg.get("path_hash_size")
        if isinstance(hs, int) and hs > 0:
            return hs
        hm: Any = self.msg.get("path_hash_mode")
        if isinstance(hm, int) and hm >= 0:
            return hm + 1
        if self.contact is not None:
            ohm: Any = self.contact.get("out_path_hash_mode")
            if isinstance(ohm, int) and ohm >= 0:
                return ohm + 1
        return 1

    @property
    def reply_text_limit(self) -> int:
        """Max UTF-8 bytes for reply text (excluding the @[sender]: prefix)."""
        if self.is_dm:
            return max_dm_text_bytes()
        prefix_len = (
            len(f"@[{self.sender}]: ".encode()) if self.sender != "unknown" else 0
        )
        return max(1, max_channel_text_bytes(self.bot_name) - prefix_len)

    async def reply(self, text: str) -> None:
        """Send *text* back to the originating DM or channel."""
        if self.path_hash_mode >= 0:
            await self.mc.commands.set_path_hash_mode(self.path_hash_mode)
        if self.is_dm and self.contact is not None:
            chunks = split_lines(text.splitlines(), max_dm_text_bytes())
            for chunk in chunks:
                result = await self.mc.commands.send_msg_with_retry(self.contact, chunk)
                if result is None:
                    log.error("DM send failed (no ack) for %s", self.sender)
                    return
                await asyncio.sleep(0.2)
        elif self.channel_idx is not None:
            if self.flood_scope is None:
                await self.mc.commands.force_unscoped()
            else:
                await self.mc.commands.set_flood_scope(self.flood_scope)
            chan_limit = max_channel_text_bytes(self.bot_name)
            prefix = f"@[{self.sender}]: " if self.sender != "unknown" else ""
            prefix_len = len(prefix.encode())
            limits = (
                [max(1, chan_limit - prefix_len), chan_limit]
                if prefix
                else [chan_limit]
            )
            chunks = split_lines(text.splitlines(), *limits)
            for i, chunk in enumerate(chunks):
                full = prefix + chunk if i == 0 else chunk
                result = await self.mc.commands.send_chan_msg(self.channel_idx, full)
                if result.is_error():
                    log.error("channel send failed: %s", result.payload)
                    return
                await asyncio.sleep(0.2)


# ---------------------------------------------------------------------------
# Text splitting
# ---------------------------------------------------------------------------


def split_lines(lines: list[str], *limits: int) -> list[str]:
    """Pack lines into chunks, each within a UTF-8 byte limit.

    *limits* gives an optional byte limit for each chunk in order (first,
    second, ...). If fewer limits are given than chunks produced, remaining
    chunks use the last given limit. With no limits, chunks are unbounded.
    """
    default_limit = limits[-1] if limits else 0
    chunks: list[str] = []
    current = ""
    chunk_idx = 0

    def limit_for(i: int) -> int:
        if i < len(limits):
            return limits[i]
        return default_limit

    for line in lines:
        candidate = f"{current}\n{line}" if current else line
        limit = limit_for(chunk_idx)
        if limit == 0 or len(candidate.encode()) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
                chunk_idx += 1
            current = line
    if current:
        chunks.append(current)
    return chunks or [""]


def _hops_str(path_len: Any) -> str:
    """Format a hop count, handling flood (255) and direct (0)."""
    if not isinstance(path_len, int) or path_len < 0 or path_len >= 255:
        return "flood"
    if path_len == 0:
        return "direct"
    return f"{path_len} hop{'s' if path_len != 1 else ''}"


# ---------------------------------------------------------------------------
# Argument parsing (signature introspection)
# ---------------------------------------------------------------------------


@dataclass
class ParamSpec:
    """Metadata for a single command parameter, derived from its annotation."""

    name: str
    kind: str  # "single" | "variadic"
    optional: bool
    inner_type: type  # str, int, or an Enum subclass

    def parse(self, tokens: list[str]) -> Any:
        """Parse this parameter's value from its token(s).

        For ``single`` kind: consumes exactly 1 token (or 0 if optional).
        For ``variadic`` kind: consumes all remaining tokens.

        Returns the parsed value, or ``None`` if optional and no token.
        """
        if self.kind == "variadic":
            if not tokens:
                return None if self.optional else []
            return tokens
        if not tokens:
            return None
        return self._coerce(tokens[0])

    def token_count(self) -> int:
        """How many tokens this param consumes (1 for single, all for variadic)."""
        return -1 if self.kind == "variadic" else 1

    def _coerce(self, token: str) -> Any:
        if self.inner_type is int:
            return int(token)
        if inspect.isclass(self.inner_type) and issubclass(self.inner_type, Enum):
            return self.inner_type(token)
        return token  # str


def _describe_param(param: inspect.Parameter, hints: dict[str, Any]) -> ParamSpec:
    """Build a :class:`ParamSpec` from a function parameter and its type hint."""
    ann = hints.get(param.name)
    if ann is None:
        raise TypeError(f"parameter {param.name!r} has no type annotation")

    optional = param.default is not inspect.Parameter.empty
    origin = typing.get_origin(ann)
    is_union = origin is types.UnionType
    if is_union:
        non_none = [a for a in typing.get_args(ann) if a is not types.NoneType]
        if len(non_none) != 1:
            raise TypeError(f"parameter {param.name!r}: unsupported union {ann!r}")
        inner = non_none[0]
        optional = True
    else:
        inner = ann

    inner_origin = typing.get_origin(inner)
    if inner_origin is list:
        inner_args = typing.get_args(inner)
        if len(inner_args) != 1 or inner_args[0] is not str:
            raise TypeError(f"parameter {param.name!r}: only list[str] is supported")
        return ParamSpec(param.name, "variadic", optional, str)

    if inner is str or inner is int:
        return ParamSpec(param.name, "single", optional, inner)

    if inspect.isclass(inner) and issubclass(inner, Enum):
        return ParamSpec(param.name, "single", optional, inner)

    raise TypeError(f"parameter {param.name!r}: unsupported type {ann!r}")


def _usage_suffix(specs: list[ParamSpec]) -> str:
    """Build the ``[arg1] [arg2...]`` suffix for a usage line."""
    parts: list[str] = []
    for spec in specs:
        if spec.kind == "variadic":
            label = f"<{spec.name}...>" if not spec.optional else f"[{spec.name}...]"
        elif spec.inner_type is not str and not (
            inspect.isclass(spec.inner_type) and issubclass(spec.inner_type, Enum)
        ):
            label = f"<{spec.name}>" if not spec.optional else f"[{spec.name}]"
        elif inspect.isclass(spec.inner_type) and issubclass(spec.inner_type, Enum):
            choices = "|".join(e.value for e in spec.inner_type)
            label = f"[{choices}]" if spec.optional else f"<{choices}>"
        else:
            label = f"<{spec.name}>" if not spec.optional else f"[{spec.name}]"
        parts.append(label)
    return " ".join(parts)


def _parse_args(
    specs: list[ParamSpec], tokens: list[str]
) -> tuple[list[Any], str | None]:
    """Parse *tokens* against *specs*.

    Returns ``(values, error)``.  On success, *error* is ``None`` and *values*
    has one entry per spec.  On failure, *error* is a short message and
    *values* is empty.
    """
    values: list[Any] = []
    remaining = list(tokens)

    for spec in specs:
        if spec.kind == "variadic":
            values.append(spec.parse(remaining))
            remaining = []
        else:
            if not remaining:
                if spec.optional:
                    values.append(None)
                    continue
                return [], f"missing {spec.name}"
            try:
                values.append(spec._coerce(remaining[0]))
            except ValueError:
                if inspect.isclass(spec.inner_type) and issubclass(
                    spec.inner_type, Enum
                ):
                    choices = "|".join(e.value for e in spec.inner_type)
                    return [], f"invalid {spec.name}: {remaining[0]} (try: {choices})"
                return [], f"invalid {spec.name}: {remaining[0]}"
            remaining = remaining[1:]

    if remaining:
        return [], "too many args"

    return values, None


# ---------------------------------------------------------------------------
# Command and Subcommand dataclasses
# ---------------------------------------------------------------------------


CommandFunc = Callable[..., Awaitable[None]]


@dataclass
class Subcommand:
    name: str
    func: CommandFunc
    specs: list[ParamSpec]
    doc: str

    def usage(self, parent_name: str) -> str:
        suffix = _usage_suffix(self.specs)
        return f"{parent_name} {self.name} {suffix}".strip()


@dataclass
class Command:
    aliases: list[str]
    scope: Scope
    secret: bool
    allowed_everywhere: bool
    # Simple command:
    func: CommandFunc | None = None
    specs: list[ParamSpec] = field(default_factory=list)
    # Subcommand command:
    subcommands: dict[str, Subcommand] | None = None
    default_sub: str | None = None
    # For class-based commands, the class docstring (for help):
    class_doc: str = ""

    @property
    def name(self) -> str:
        return self.aliases[0]

    @property
    def doc(self) -> str:
        if self.func is not None:
            return (self.func.__doc__ or "(no help available)").strip()
        return self.class_doc or "(no help available)"

    def usage(self) -> str:
        """Build the usage line for this command."""
        if self.subcommands is not None:
            names = "|".join(self.subcommands.keys())
            suffix = f"[{names}]" if self.default_sub else f"<{names}>"
            return f"{self.name} {suffix}".strip()
        suffix = _usage_suffix(self.specs)
        return f"{self.name} {suffix}".strip()

    async def call(self, ctx: Context, tokens: list[str]) -> str | None:
        """Parse *tokens* and call the handler. Returns error string or None."""
        if self.subcommands is not None:
            return await self._call_sub(ctx, tokens)
        assert self.func is not None
        values, err = _parse_args(self.specs, tokens)
        if err is not None:
            return err
        await self.func(ctx, *values)  # pyright: ignore[reportCallIssue]
        return None

    async def _call_sub(self, ctx: Context, tokens: list[str]) -> str | None:
        """Dispatch to a subcommand."""
        assert self.subcommands is not None
        if not tokens:
            if self.default_sub is not None:
                sub = self.subcommands[self.default_sub]
                await sub.func(ctx)
                return None
            return f"usage: {self.usage()}"
        sub_name = tokens[0].lower()
        sub = self.subcommands.get(sub_name)
        if sub is None:
            return f"unknown subcommand: {sub_name}\nusage: {self.usage()}"
        values, err = _parse_args(sub.specs, tokens[1:])
        if err is not None:
            return f"{sub_name}: {err}\nusage: {sub.usage(self.name)}"
        await sub.func(ctx, *values)  # pyright: ignore[reportCallIssue]
        return None


# ---------------------------------------------------------------------------
# Registry
# ---------------------------------------------------------------------------

_commands: dict[str, Command] = {}


def command(
    aliases: list[str] | str,
    *,
    secret: bool = False,
    scope: Scope = Scope.MENTION,
    allowed_everywhere: bool = False,
) -> Callable[[type | CommandFunc], type | CommandFunc]:
    """Register a command.

    *aliases* is the canonical name (first entry) plus any aliases.  A bare
    string is treated as a single-element list.  Pass a class with
    ``staticmethod`` async methods for subcommand support.

    Parameter types are introspected from the handler signature to build
    typed argument parsing and auto-generated usage strings.
    """
    alias_list = [aliases] if isinstance(aliases, str) else list(aliases)

    def decorator(obj: type | CommandFunc) -> type | CommandFunc:
        if inspect.isclass(obj):
            cmd = _build_class_command(
                obj, alias_list, scope, secret, allowed_everywhere
            )
        else:
            cmd = _build_func_command(
                obj, alias_list, scope, secret, allowed_everywhere
            )  # pyright: ignore[reportArgumentType]
        for a in alias_list:
            _commands[a.lower()] = cmd
        return obj

    return decorator


def _build_func_command(
    func: CommandFunc,
    aliases: list[str],
    scope: Scope,
    secret: bool,
    allowed_everywhere: bool,
) -> Command:
    """Build a simple command from a function."""
    sig = inspect.signature(func)
    hints = typing.get_type_hints(func)
    specs: list[ParamSpec] = []
    for name, param in sig.parameters.items():
        if name == "ctx":
            continue
        if param.kind not in (
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            inspect.Parameter.POSITIONAL_ONLY,
        ):
            raise TypeError(f"parameter {name!r}: only positional params supported")
        specs.append(_describe_param(param, hints))
    return Command(
        aliases=aliases,
        scope=scope,
        secret=secret,
        allowed_everywhere=allowed_everywhere,
        func=func,
        specs=specs,
    )


def _build_class_command(
    cls: type,
    aliases: list[str],
    scope: Scope,
    secret: bool,
    allowed_everywhere: bool,
) -> Command:
    """Build a subcommand command from a class with staticmethod methods."""
    subcommands: dict[str, Subcommand] = {}
    default_sub: str | None = getattr(cls, "default", None)

    for name, method in inspect.getmembers(cls, predicate=inspect.isfunction):
        if name.startswith("_"):
            continue
        if not inspect.iscoroutinefunction(method):
            raise TypeError(f"subcommand {name!r}: must be async")
        sig = inspect.signature(method)
        hints = typing.get_type_hints(method)
        specs: list[ParamSpec] = []
        for pname, param in sig.parameters.items():
            if pname == "ctx":
                continue
            specs.append(_describe_param(param, hints))
        doc = (method.__doc__ or "").strip()
        subcommands[name] = Subcommand(name, method, specs, doc)

    if not subcommands:
        raise TypeError(f"command class {cls.__name__!r} has no subcommands")

    if default_sub is not None and default_sub not in subcommands:
        raise TypeError(
            f"default subcommand {default_sub!r} not found in {cls.__name__!r}"
        )

    cmd = Command(
        aliases=aliases,
        scope=scope,
        secret=secret,
        allowed_everywhere=allowed_everywhere,
        subcommands=subcommands,
        default_sub=default_sub,
        class_doc=(cls.__doc__ or "").strip(),
    )
    return cmd


def get_command(name: str) -> Command | None:
    return _commands.get(name.lower())


def list_commands(
    is_dm: bool = True, channel_allowed: set[str] | None = None
) -> list[Command]:
    """All registered commands, deduplicated (one entry per canonical name).

    Secret commands are excluded. When *is_dm* is False, DM_ONLY commands
    are also excluded.  When *channel_allowed* is not None (a restricted
    channel), commands that are neither ``allowed_everywhere`` nor in
    *channel_allowed* are also excluded.
    """
    seen: set[str] = set()
    out: list[Command] = []
    for cmd in _commands.values():
        if cmd.name not in seen and not cmd.secret:
            if not is_dm and cmd.scope is Scope.DM_ONLY:
                continue
            if (
                not is_dm
                and channel_allowed is not None
                and not cmd.allowed_everywhere
                and cmd.name not in channel_allowed
            ):
                continue
            seen.add(cmd.name)
            out.append(cmd)
    return out


# ---------------------------------------------------------------------------
# Help generation
# ---------------------------------------------------------------------------


def usage_line(cmd: Command) -> str:
    """Build the usage string for *cmd*."""
    return cmd.usage()


def full_help(is_dm: bool = True, channel_allowed: set[str] | None = None) -> str:
    """The text shown for ``help`` / ``?`` with no arguments."""
    lines = ["mention or DM me:"]
    for cmd in sorted(list_commands(is_dm, channel_allowed), key=lambda c: c.name):
        lines.append(f"  {usage_line(cmd)}")
    lines.append("(?cmd for details)")
    return "\n".join(lines)


def command_help(name: str, sub: str | None = None) -> str | None:
    """The text shown for ``?<name>``: docstring plus aliases.

    If *sub* is given (e.g. ``?record start``), show help for that subcommand.
    """
    cmd = get_command(name)
    if cmd is None:
        return None

    if sub is not None and cmd.subcommands is not None:
        s = cmd.subcommands.get(sub)
        if s is None:
            return f"Unknown subcommand: {sub}"
        lines = [s.doc or "(no help available)"]
        lines.append(f"Usage: {s.usage(cmd.name)}")
        return "\n".join(lines)

    lines = [cmd.doc]
    # Show all aliases except the one the user queried.
    extra = [a for a in cmd.aliases if a.lower() != name.lower()]
    if extra:
        lines.append(f"Aliases: {', '.join(extra)}")
    lines.append(f"Usage: {cmd.usage()}")
    if cmd.subcommands is not None:
        for s in cmd.subcommands.values():
            lines.append(f"  {s.usage(cmd.name)}")
    return "\n".join(lines)
