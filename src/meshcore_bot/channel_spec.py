"""Parser for ``--channels`` CLI config: ``name[cmds]`` specs with ``~`` modifier.

Syntax::

    name                 listen on channel, all commands allowed
    name[cmd1,cmd2]      only listed commands allowed
    name~                channel-level ~: all commands mention-free
    name~[cmd1,cmd2]     combination of both
    name[~cmd1,cmd2]     cmd1 mention-free, cmd2 requires @mention

Uses the lexeme pattern from parsy's JSON example: a ``_ws`` parser for
optional whitespace, and ``_lexeme`` to wrap parsers that should consume
trailing whitespace.
"""

# pyright: reportInvalidTypeForm=false, reportReturnType=false
# parsy's @generate decorator transforms generator functions into Parser
# objects; pyright doesn't understand this, so the -> Parser annotations
# on @generate functions trigger false positives.

from dataclasses import dataclass

from parsy import ParseError, Parser, eof, generate, peek, regex, string

__all__ = ["ChannelSpec", "parse_channel_specs"]


@dataclass(slots=True)
class ChannelSpec:
    """Parsed ``--channels`` entry for one channel.

    ``allowed`` is the set of permitted command names, or None for all.
    ``open`` (channel-level ``~``) makes every command mention-free.
    ``open_cmds`` (command-level ``~``) lists specific commands that are
    mention-free; ignored when ``open`` is True.
    """

    name: str
    allowed: set[str] | None
    open: bool
    open_cmds: set[str]


_ws = regex(r"\s*")
_lexeme = lambda p: p << _ws

_chan_name = _lexeme(regex(r"[^,\[\]\s]+"))
_lbracket = _lexeme(string("["))
_rbracket = _lexeme(string("]"))
_comma = _lexeme(string(","))


@generate
def _channel_spec() -> Parser:
    """Parse a single ``name[cmds]`` or ``name`` to ChannelSpec."""
    raw = yield _chan_name
    open_chan = raw.endswith("~")
    name = raw[:-1] if open_chan else raw
    if not name.startswith("#"):
        name = "#" + name
    is_bracket = yield peek(string("[")).optional()
    if is_bracket is not None:
        yield _lbracket
        cmds = yield regex(r"[^\]]*")
        yield _rbracket
        allowed: set[str] = set()
        open_cmds: set[str] = set()
        for item in cmds.split(","):
            item = item.strip()
            if not item:
                continue
            if item.startswith("~"):
                item = item[1:].strip()
                if item:
                    allowed.add(item.lower())
                    open_cmds.add(item.lower())
            else:
                allowed.add(item.lower())
        allowed_or_none = allowed or None
    else:
        allowed_or_none = None
        open_cmds = set()
    return ChannelSpec(
        name=name, allowed=allowed_or_none, open=open_chan, open_cmds=open_cmds
    )


def parse_channel_specs(raw: list[str]) -> list[ChannelSpec]:
    """Parse ``--channels`` argument values into :class:`ChannelSpec` list.

    Each entry in *raw* is a comma-separated list of specs.  Tolerates
    whitespace around specs and commas.  Returns an empty list if any spec
    fails to parse (fail-fast).
    """
    specs = _ws >> _channel_spec.sep_by(_comma) << _ws
    out: list[ChannelSpec] = []
    for item in raw:
        try:
            result, _ = (specs << eof).parse_partial(item)
        except ParseError:
            return []
        out.extend(result)
    return out
