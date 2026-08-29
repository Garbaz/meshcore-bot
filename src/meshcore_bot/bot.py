"""MeshCore bot: reply to DMs and channel mentions with path stats and more.

Connects to a companion node (TCP, serial, or BLE), fetches contacts and
channels at startup, and dispatches shell-like commands from incoming DMs or
channel messages that @-mention the bot. Use ``--help`` for CLI options.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
import re
from pathlib import Path
from typing import Any

from meshcore import EventType, MeshCore
from meshcore.events import Event

from meshcore_bot.commands import (
    Context,
    command_help,
    full_help,
    get_command,
    parse_command,
    usage_line,
)
from meshcore_bot.registry import REGISTRY_TTL, NodeRegistry

log = logging.getLogger("meshcore_bot")

CACHE_PATH = Path("~/.cache/meshcore-bot/registry.json").expanduser()

# Channel names to listen on (configured at startup from CHANNEL_NAMES).
# Each entry is the channel *name* as stored on the companion (e.g. "#ping").
CHANNEL_NAMES = ["#ping", "#bot", "#test"]


# ---------------------------------------------------------------------------
# Connection (mirrors meshcore-cli flags)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="meshcore-bot",
        description="Reply to MeshCore DMs and channel mentions with path stats.",
        add_help=False,
    )
    g = p.add_argument_group("connection")
    g.add_argument("-t", "--tcp", metavar="HOST", help="connect via TCP/IP to HOST")
    g.add_argument(
        "-p", "--port", type=int, default=5000, help="TCP port (default 5000)"
    )
    g.add_argument("-s", "--serial", metavar="PORT", help="connect via serial/USB port")
    g.add_argument(
        "-b",
        "--baudrate",
        type=int,
        default=115200,
        help="serial baudrate (default 115200)",
    )
    g.add_argument("-a", "--address", metavar="ADDR", help="BLE device address or name")
    g.add_argument(
        "-d", "--device", metavar="NAME", help="filter BLE devices by name/address"
    )
    g.add_argument(
        "-P", "--pair", action="store_true", help="force BLE pairing via the OS"
    )
    g.add_argument(
        "-T",
        "--scan-timeout",
        type=float,
        default=2.0,
        help="BLE scan timeout in seconds (default 2)",
    )

    g2 = p.add_argument_group("logging")
    g2.add_argument("-D", "--debug", action="store_true", help="enable debug logging")
    g2.add_argument("-q", "--quiet", action="store_true", help="only show errors")
    g2.add_argument("-h", "--help", action="help", help="show this help and exit")
    return p


async def connect(args: argparse.Namespace) -> MeshCore | None:
    """Create a MeshCore connection based on CLI args (TCP > serial > BLE)."""
    if args.tcp:
        mc = await MeshCore.create_tcp(
            host=args.tcp, port=args.port, debug=args.debug, only_error=args.quiet
        )
        if mc is None:
            log.error("could not connect to %s:%d", args.tcp, args.port)
        return mc

    if args.serial:
        mc = await MeshCore.create_serial(
            port=args.serial,
            baudrate=args.baudrate,
            debug=args.debug,
            only_error=args.quiet,
        )
        if mc is None:
            log.error("could not connect to serial port %s", args.serial)
        return mc

    # BLE
    try:
        from bleak import BleakScanner
    except ImportError:
        log.error("BLE requires the 'bleak' package; install it or use -t/-s")
        return None

    address: str = str(args.device or args.address or "")
    device: Any = None
    if not address or ":" not in address:
        log.info(
            "scanning BLE for MeshCore device%s",
            f" matching {address}" if address else "",
        )
        devices = await BleakScanner.discover(timeout=args.scan_timeout)
        for d in devices:
            if (
                d.name
                and d.name.startswith("MeshCore-")
                and (not address or address in d.name)
            ):
                address = d.address
                device = d
                log.info("found %s (%s)", d.name, d.address)
                break
        else:
            log.error("no matching BLE device found")
            return None

    mc = await MeshCore.create_ble(
        address=address,
        device=device,
        debug=args.debug,
        only_error=args.quiet,
        pin=True if args.pair else None,  # pyright: ignore[reportArgumentType]
    )
    if mc is None:
        log.error("could not connect to BLE device %s", address)
    return mc


# ---------------------------------------------------------------------------
# Channel helpers
# ---------------------------------------------------------------------------


async def fetch_channels(mc: MeshCore) -> list[dict[str, Any]]:
    """Fetch all channel slots from the companion (like meshcore-cli get_channels)."""
    channels: list[dict[str, Any]] = []
    idx = 0
    while True:
        res = await mc.commands.get_channel(idx)
        if res.is_error():
            break
        channels.append(res.payload)
        idx += 1
    return channels


async def ensure_channels(mc: MeshCore, wanted: list[str]) -> set[int]:
    """Ensure all *wanted* channel names exist on the companion.

    Fetches existing channels; for any wanted name not found, creates it in
    the first free slot. Returns the set of channel indices for all wanted
    names.
    """
    existing = await fetch_channels(mc)
    wanted_lower = {n.lower() for n in wanted}
    found: set[int] = set()
    used_indices: set[int] = set()

    for ch in existing:
        used_indices.add(ch["channel_idx"])
        if str(ch.get("channel_name", "")).lower() in wanted_lower:
            found.add(ch["channel_idx"])

    # Channel 0 is "public" (no name); start searching from 1 for new channels.
    next_idx = 1
    for name in wanted:
        if any(
            str(ch.get("channel_name", "")).lower() == name.lower() for ch in existing
        ):
            continue
        while next_idx in used_indices:
            next_idx += 1
        log.info("creating channel %s at index %d", name, next_idx)
        res = await mc.commands.set_channel(next_idx, name)
        if res.is_error():
            log.error("failed to create channel %s: %s", name, res.payload)
            continue
        found.add(next_idx)
        used_indices.add(next_idx)
        next_idx += 1

    return found


# ---------------------------------------------------------------------------
# Mention parsing
# ---------------------------------------------------------------------------

_MENTION_RE = re.compile(r"@(\[?)([^\]\s@]+)(\]?)")


def strip_mention(text: str, bot_name: str) -> str | None:
    """If *text* @-mentions *bot_name*, return the remaining text after it.

    Supports both ``@Name`` and ``@[Name]`` forms. Returns None if the
    bot is not mentioned.
    """
    name_lower = bot_name.lower()
    for m in _MENTION_RE.finditer(text):
        if m.group(2).lower() == name_lower:
            start = m.start()
            rest = (text[:start] + text[m.end() :]).strip()
            return rest
    return None


def parse_channel_sender(text: str) -> tuple[str, str]:
    """Extract sender name from channel text convention ``Name: rest``.

    Channel messages carry no sender identity in the protocol; meshcore chat
    clients prefix messages with ``SenderName: ``. Returns (sender, remainder).
    If no ``:`` separator is found, sender is "unknown" and the full text is
    returned as remainder.
    """
    if ":" in text:
        name, _, rest = text.partition(":")
        return name.strip(), rest.strip()
    return "unknown", text.strip()


# ---------------------------------------------------------------------------
# Event handling
# ---------------------------------------------------------------------------


async def handle_dm(
    mc: MeshCore,
    registry: NodeRegistry,
    bot_name: str,
    region_scope: str,
    event: Event,
) -> None:
    msg: dict[str, Any] = event.payload or {}
    prefix: str = msg.get("pubkey_prefix", "")
    text = str(msg.get("text", "")).strip()
    log.info("DM from %s: %r", prefix, text)
    if not text:
        return

    # Refresh contacts so the sender's path is visible.
    await mc.ensure_contacts()
    contact = mc.get_contact_by_key_prefix(prefix)
    if contact is None:
        log.warning("sender %s not in contacts, not replying", prefix)
        return

    sender = str(contact.get("adv_name") or prefix)
    hash_mode: Any = msg.get("path_hash_mode", 0)
    ctx = Context(
        mc=mc,
        registry=registry,
        sender=sender,
        bot_name=bot_name,
        region_scope=region_scope,
        path_hash_mode=hash_mode if isinstance(hash_mode, int) else 0,
        is_dm=True,
        contact=contact,
        msg=msg,
    )
    await dispatch(ctx, text, mentioned=True)


async def handle_channel(
    mc: MeshCore,
    registry: NodeRegistry,
    bot_name: str,
    region_scope: str,
    chan_indices: set[int],
    event: Event,
) -> None:
    msg: dict[str, Any] = event.payload or {}
    chan: Any = msg.get("channel_idx")
    text = str(msg.get("text", "")).strip()
    log.info("channel %s msg: %r", chan, text)
    if chan not in chan_indices or not text:
        return

    sender, remainder = parse_channel_sender(text)

    mentioned_text = strip_mention(remainder, bot_name)
    if mentioned_text is not None:
        command_text = mentioned_text
        is_mentioned = True
    else:
        command_text = remainder
        is_mentioned = False

    hash_mode: Any = msg.get("path_hash_mode", 0)
    ctx = Context(
        mc=mc,
        registry=registry,
        sender=sender,
        bot_name=bot_name,
        region_scope=region_scope,
        path_hash_mode=hash_mode if isinstance(hash_mode, int) else 0,
        is_dm=False,
        channel_idx=chan,
        msg=msg,
    )
    await dispatch(ctx, command_text, mentioned=is_mentioned)


async def dispatch(ctx: Context, text: str, *, mentioned: bool) -> None:
    """Parse *text* and run the matching command, respecting require_mention."""
    parsed = parse_command(text)
    if parsed is None:
        if ctx.is_dm or mentioned:
            await ctx.reply("unknown command. try help.")
        return
    verb, args = parsed

    # "?verb" is a help query; bare "?" shows the full help listing
    if verb.startswith("?"):
        name = verb[1:]
        if not name:
            await ctx.reply(full_help())
            return
        h = command_help(name)
        if h is not None:
            await ctx.reply(h)
        else:
            await ctx.reply(f"Unknown command: {name}")
        return

    cmd = get_command(verb)
    if cmd is None:
        if ctx.is_dm or mentioned:
            await ctx.reply(f'unknown command "{verb}". try help.')
        return

    if not ctx.is_dm and cmd.require_mention and not mentioned:
        return

    if len(args) < cmd.min_args:
        await ctx.reply(f"usage: {usage_line(cmd)}")
        return

    log.info("dispatching %s (args=%s, mention=%s)", verb, args, mentioned)
    ctx.verb = verb
    try:
        await cmd.func(ctx, args)
    except Exception:
        log.exception("command %s failed", verb)


# ---------------------------------------------------------------------------
# Main
# ---------------------------------------------------------------------------


async def main(args: argparse.Namespace) -> None:
    logging.basicConfig(
        level=logging.DEBUG
        if args.debug
        else logging.ERROR
        if args.quiet
        else logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
    )

    registry = NodeRegistry(CACHE_PATH)
    log.info("loading node registry (may take a while on first run)...")
    await registry.load(ttl=REGISTRY_TTL)
    log.info("registry ready: %d relay nodes", len(registry.nodes))

    mc = await connect(args)
    if mc is None:
        return
    log.info("connected to %s", mc.self_info.get("name", "?"))
    bot_name = str(mc.self_info.get("name", ""))

    region_scope = ""
    scope_res = await mc.commands.get_default_flood_scope()
    if not scope_res.is_error():
        region_scope = scope_res.payload.get("scope_name", "") or ""

    await mc.ensure_contacts()
    log.info("fetched %d contacts", len(mc.contacts))

    mc.set_decrypt_channel_logs(True)
    chan_indices = await ensure_channels(mc, CHANNEL_NAMES)
    log.info("listening on channels: %s", chan_indices or "(none)")

    async def on_direct(event: Event) -> None:
        await handle_dm(mc, registry, bot_name, region_scope, event)

    async def on_channel(event: Event) -> None:
        await handle_channel(mc, registry, bot_name, region_scope, chan_indices, event)

    mc.subscribe(EventType.CONTACT_MSG_RECV, on_direct)  # pyright: ignore[reportArgumentType]
    mc.subscribe(EventType.CHANNEL_MSG_RECV, on_channel)  # pyright: ignore[reportArgumentType]

    await mc.start_auto_message_fetching()

    async def poll_messages() -> None:
        """Poll for messages as push events may not arrive over USB serial."""
        while True:
            await asyncio.sleep(5)
            try:
                while True:
                    result = await mc.commands.get_msg()
                    if result.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
                        break
                    await asyncio.sleep(0.1)
            except asyncio.CancelledError:
                raise
            except OSError as e:
                log.debug("poll error: %s", e)

    poll_task = asyncio.create_task(poll_messages())

    log.info("listening for messages...")
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
        poll_task.cancel()
    finally:
        await mc.disconnect()
        log.info("disconnected")


def run() -> None:
    args = build_parser().parse_args()
    try:
        asyncio.run(main(args))
    except KeyboardInterrupt:
        print()


if __name__ == "__main__":
    run()
