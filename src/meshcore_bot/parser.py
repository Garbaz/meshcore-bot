"""Parser for incoming messages: mention extraction and command tokenisation.

The parser enforces only syntax, not values. A mention of any name parses
successfully; the caller decides whether the mention was directed at the bot.

Grammar
-------

Channel message::

    SenderName: [@mention] verb [args...]

DM message::

    verb [args...]

The firmware prepends the sender's advertised name and a ``: `` separator to
channel messages. We split on the first ``": "`` to extract the sender and
body. If no ``": "`` is present, the entire text is parsed as the body.

Mention forms::

    @NAME          bare, no spaces
    @[NAME]        bracketed, may contain spaces
    @[NAME]:       bracketed with trailing colon
"""

# pyright: reportInvalidTypeForm=false, reportReturnType=false
# parsy's @generate decorator transforms generator functions into Parser
# objects; pyright doesn't understand this, so the -> Parser annotations
# on @generate functions trigger false positives.

from dataclasses import dataclass

from parsy import ParseError, Parser, eof, generate, peek, regex, string, whitespace

__all__ = ["ParsedMessage", "parse_channel_message", "parse_dm_message"]


@dataclass(frozen=True, slots=True)
class ParsedMessage:
    """Result of parsing an incoming message."""

    sender: str | None  # channel sender display name; None for DMs
    mentioned: bool  # whether the bot was @-mentioned (always True for DMs)
    verb: str  # command verb (first token); empty if mention-only
    args: list[str]  # remaining whitespace-separated tokens


def _normalize_name(name: str) -> str:
    """Strip non-ASCII, trim, lowercase for case-insensitive comparison."""
    return "".join(c for c in name if ord(c) < 128).strip().lower()


_nonws = regex(r"\S+")


@generate
def _command() -> Parser:
    """Parse ``verb [ws args...]`` to ``(verb, args)``."""
    verb = yield _nonws
    args = yield (whitespace >> _nonws).many()
    return (verb, args)


@generate
def _mention() -> Parser:
    """Parse ``@mention`` syntax, returning the raw name string."""
    yield string("@")
    is_bracket = yield peek(string("[")).optional()
    if is_bracket is not None:
        yield string("[")
        name = yield regex(r"[^\]]+")
        yield string("]")
        yield string(":").optional()
    else:
        name = yield regex(r"[^\s@\[]+")
    return name


@generate
def _body() -> Parser:
    """Parse body into ``(mention_name, verb, args)``.

    If the body starts with ``@``, parse the mention then an optional
    command. Without ``@``, parse the entire body as a bare command.
    """
    starts_with_at = yield peek(string("@")).optional()
    if starts_with_at is not None:
        mention_name: str | None = yield _mention
        yield whitespace.optional()
        cmd = yield _command.optional()
        if cmd is None:
            return (mention_name, "", [])
        return (mention_name, cmd[0], cmd[1])
    cmd = yield _command
    return (None, cmd[0], cmd[1])


_channel_body = _body << eof
_dm_body = _command << eof


def parse_channel_message(text: str, bot_name: str) -> ParsedMessage | None:
    """Parse a channel message to ParsedMessage or None.

    Returns None for empty or syntactically unparseable text. The
    ``mentioned`` flag is True only when the parsed mention name matches
    ``bot_name`` (case-insensitive, non-ASCII stripped).
    """
    if not text or not text.strip():
        return None

    stripped = text.strip()
    sender: str | None = None
    body_text = stripped

    sep = stripped.find(": ")
    if sep >= 0:
        sender = stripped[:sep].strip()
        body_text = stripped[sep + 2 :].strip()
    elif stripped.endswith(":"):
        return None

    if not body_text:
        return None

    try:
        result, _ = _channel_body.parse_partial(body_text)
    except ParseError:
        return None

    mention_name, verb, args = result
    mentioned = mention_name is not None and _normalize_name(
        mention_name
    ) == _normalize_name(bot_name)
    return ParsedMessage(sender=sender, mentioned=mentioned, verb=verb, args=args)


def parse_dm_message(text: str) -> ParsedMessage | None:
    """Parse ``"verb args"`` to ParsedMessage or None.

    DMs are always considered mentioned.
    """
    text = text.strip()
    if not text:
        return None
    try:
        result, _ = _dm_body.parse_partial(text)
    except ParseError:
        return None

    verb, args = result
    return ParsedMessage(sender=None, mentioned=True, verb=verb, args=args)
