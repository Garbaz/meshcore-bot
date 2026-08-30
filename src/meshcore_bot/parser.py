"""Parser for incoming messages: mention extraction and command tokenisation.

Uses parsy parser combinators for explicit, strict parsing. Messages that
don't match the grammar exactly are rejected (return ``None``) rather than
silently misinterpreted.

Grammar
-------

Channel message::

    SenderName: [@mention] verb [args...]

DM message::

    verb [args...]

The firmware prepends the sender's advertised name and a ``: `` separator to
channel messages (e.g. ``🐸 DE.BW.FRog: @bot ping``).  We split on the first
``": "`` to extract the sender and body.  If no ``": "`` is present, the
entire text is parsed as the body (for compatibility).

Mention forms (matched case-insensitively, non-ASCII chars stripped)::

    @NAME          bare, no spaces
    @[NAME]        bracketed, may contain spaces
    @[NAME]:       bracketed with trailing colon

If the body starts with ``@`` but the name doesn't match the bot, parsing
fails (the message is addressed to someone else).
"""

# pyright: reportInvalidTypeForm=false, reportReturnType=false
# parsy's @generate decorator transforms generator functions into Parser
# objects; pyright doesn't understand this, so the -> Parser annotations
# on @generate functions trigger false positives.

from __future__ import annotations

from dataclasses import dataclass

from parsy import (
    ParseError,
    Parser,
    eof,
    fail,
    generate,
    peek,
    regex,
    string,
    whitespace,
)

__all__ = [
    "ParsedMessage",
    "parse_channel_message",
    "parse_channel_specs",
    "parse_dm_message",
]


@dataclass
class ParsedMessage:
    """Result of parsing an incoming message."""

    sender: str | None  # channel sender display name; None for DMs
    mentioned: bool  # whether the bot was @-mentioned (always True for DMs)
    verb: str  # command verb (first token); empty if mention-only
    args: list[str]  # remaining whitespace-separated tokens


# ---------------------------------------------------------------------------
# Name normalisation
# ---------------------------------------------------------------------------


def _normalize_name(name: str) -> str:
    """Strip non-ASCII characters, trim whitespace, lowercase."""
    return "".join(c for c in name if ord(c) < 128).strip().lower()


# ---------------------------------------------------------------------------
# Command parser: verb + whitespace-separated args
# ---------------------------------------------------------------------------

_nonws = regex(r"\S+")


@generate
def _command() -> Parser:
    """Parse ``verb [ws args...]`` → ``(verb, args)``."""
    verb = yield _nonws
    args = yield (whitespace >> _nonws).many()
    return (verb, args)


# ---------------------------------------------------------------------------
# Mention parser
# ---------------------------------------------------------------------------


def _make_mention(normalized: str) -> Parser:
    """Parser that matches a ``@mention`` of the bot, producing ``True``.

    Two forms, decided by peeking after ``@``:

    - ``@[name]:`` or ``@[name]`` — bracketed; name may contain spaces
    - ``@name``                  — bare; name ends at whitespace

    The extracted name is normalised (non-ASCII stripped, trimmed, lowercased)
    and compared to *normalized*.  If it doesn't match the parser fails.
    """

    @generate
    def mention() -> Parser:
        yield string("@")
        is_bracket = yield peek(string("[")).optional()
        if is_bracket is not None:
            yield string("[")
            name = yield regex(r"[^\]]+")
            yield string("]")
            yield string(":").optional()
        else:
            name = yield regex(r"[^\s@\[]+")
        if _normalize_name(name) == normalized:
            return True
        return (yield fail("bot name"))

    return mention


# ---------------------------------------------------------------------------
# Body parser: optional mention + command
# ---------------------------------------------------------------------------


def _make_body(normalized: str) -> Parser:
    """Parse the message body (after ``Sender:``) into ``(mentioned, verb, args)``.

    If the body starts with ``@`` it *must* be a mention of our bot — a
    mismatching name causes the whole parse to fail.  Without ``@`` the body
    is parsed as a bare command.
    """
    mention = _make_mention(normalized)

    @generate
    def body() -> Parser:
        starts_with_at = yield peek(string("@")).optional()
        if starts_with_at is not None:
            yield mention
            yield whitespace.optional()
            cmd = yield _command.optional()
            if cmd is None:
                return (True, "", [])
            return (True, cmd[0], cmd[1])
        else:
            cmd = yield _command
            return (False, cmd[0], cmd[1])

    return body << eof


# ---------------------------------------------------------------------------
# Channel message parser: Sender: body
# ---------------------------------------------------------------------------


def parse_channel_message(text: str, bot_name: str) -> ParsedMessage | None:
    """Parse a channel message → :class:`ParsedMessage` or ``None``.

    Channel messages arrive as ``SenderName: body`` (the firmware prepends
    the sender's advertised name).  We split on the first ``": "`` to
    separate sender from body, then parse the body.

    If no ``": "`` is found, the entire text is parsed as body (for
    compatibility with clients that don't prepend a sender).

    Returns ``None`` when the body is addressed to a different bot
    (``@wrong …``) or doesn't contain a parseable command.
    """
    if not text or not text.strip():
        return None

    normalized = _normalize_name(bot_name)
    body_parser = _make_body(normalized)

    stripped = text.strip()
    sender: str | None = None
    body_text = stripped

    # Split on first ": " (colon-space) to separate sender from body.
    sep = stripped.find(": ")
    if sep >= 0:
        sender = stripped[:sep].strip()
        body_text = stripped[sep + 2 :].strip()
    elif stripped.endswith(":"):
        # "Sender:" with no body — nothing to parse.
        return None

    if not body_text:
        return None

    try:
        result, _ = body_parser.parse_partial(body_text)
    except ParseError:
        return None

    mentioned, verb, args = result
    return ParsedMessage(sender=sender, mentioned=mentioned, verb=verb, args=args)


# ---------------------------------------------------------------------------
# DM message parser
# ---------------------------------------------------------------------------


def parse_dm_message(text: str) -> ParsedMessage | None:
    """Parse ``"verb args"`` → :class:`ParsedMessage` or ``None``.

    DMs are always considered mentioned.  Returns ``None`` for empty or
    unparseable text.
    """
    text = text.strip()
    if not text:
        return None
    try:
        result, _ = (_command << eof).parse_partial(text)
    except ParseError:
        return None

    verb, args = result
    return ParsedMessage(sender=None, mentioned=True, verb=verb, args=args)


# ---------------------------------------------------------------------------
# Channel spec parser: name[cmd1,cmd2,...] or just name
#
# Uses the lexeme pattern from parsy's JSON example: a ``_ws`` parser for
# optional whitespace, and ``_lexeme`` to wrap parsers that should consume
# trailing whitespace.  This keeps whitespace handling in one place instead
# of scattering ``whitespace.optional()`` calls.
# ---------------------------------------------------------------------------

_ws = regex(r"\s*")
_lexeme = lambda p: p << _ws

_chan_name = _lexeme(regex(r"[^,\[\]\s]+"))
_lbracket = _lexeme(string("["))
_rbracket = _lexeme(string("]"))
_comma = _lexeme(string(","))


@generate
def _channel_spec() -> Parser:
    """Parse a single ``name[cmds]`` or ``name`` → ``(name, allowed)``."""
    name = yield _chan_name
    if not name.startswith("#"):
        name = "#" + name
    is_bracket = yield peek(string("[")).optional()
    if is_bracket is not None:
        yield _lbracket
        cmds = yield regex(r"[^\]]*")
        yield _rbracket
        allowed: set[str] | None = {
            c.strip().lower() for c in cmds.split(",") if c.strip()
        } or None
    else:
        allowed = None
    return (name, allowed)


def parse_channel_specs(raw: list[str]) -> list[tuple[str, set[str] | None]]:
    """Parse ``--channels`` argument values into ``(name, allowed)`` tuples.

    Each entry in *raw* is a comma-separated list of specs.  Tolerates
    whitespace around specs and commas.  Returns an empty list if any spec
    fails to parse (fail-fast).
    """
    specs = _ws >> _channel_spec.sep_by(_comma) << _ws
    out: list[tuple[str, set[str] | None]] = []
    for item in raw:
        try:
            result, _ = (specs << eof).parse_partial(item)
        except ParseError:
            return []
        out.extend(result)
    return out
