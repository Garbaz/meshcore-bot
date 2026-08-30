"""MeshCore bot: reply to DMs and channel mentions with path stats and more.

Connects to a companion node (TCP, serial, or BLE), fetches contacts and
channels at startup, and dispatches commands from incoming DMs or channel
messages that @-mention the bot. Use ``--help`` for CLI options.
"""

from __future__ import annotations

import argparse
import asyncio
import logging
from pathlib import Path
from typing import Any

from meshcore import EventType, MeshCore
from meshcore.events import Event

from meshcore_bot.budget import MessageBudget
from meshcore_bot.commands import (
    Context,
    Scope,
    command_help,
    full_help,
    get_command,
    usage_line,
)
from meshcore_bot.parser import (
    ParsedMessage,
    parse_channel_message,
    parse_channel_specs,
    parse_dm_message,
)
from meshcore_bot.registry import NodeRegistry, scope_key
from meshcore_bot.scope import ScopeResolver
from meshcore_bot.telemetry import TelemetryLogger

log = logging.getLogger("meshcore_bot")


# ---------------------------------------------------------------------------
# Connection (mirrors meshcore-cli flags)
# ---------------------------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="meshcore-bot",
        description="Reply to MeshCore DMs and channel mentions with path stats.",
        formatter_class=argparse.ArgumentDefaultsHelpFormatter,
        add_help=False,
    )
    g = p.add_argument_group("connection")
    g.add_argument("-t", "--tcp", metavar="HOST", help="connect via TCP/IP to HOST")
    g.add_argument("-p", "--port", type=int, default=5000, help="TCP port")
    g.add_argument("-s", "--serial", metavar="PORT", help="connect via serial/USB port")
    g.add_argument(
        "-b",
        "--baudrate",
        type=int,
        default=115200,
        help="serial baudrate",
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
        help="BLE scan timeout in seconds",
    )

    g2 = p.add_argument_group("logging")
    g2.add_argument("-D", "--debug", action="store_true", help="enable debug logging")
    g2.add_argument("-q", "--quiet", action="store_true", help="only show errors")
    g2.add_argument("-h", "--help", action="help", help="show this help and exit")

    g3 = p.add_argument_group("bot")
    g3.add_argument(
        "--location",
        default=None,
        help="bot location shown in ping/path/weather fallback",
    )
    g3.add_argument(
        "--channels",
        action="append",
        default=[],
        metavar="SPEC",
        help=(
            "channel spec: name[allowed_cmds] or just name. "
            "e.g. 'ping[ping,path],bot,weather[weather]'"
        ),
    )
    g3.add_argument(
        "--cache-path",
        default=str(Path("~/.cache/meshcore-bot/registry.json").expanduser()),
        help="registry cache path",
    )
    g3.add_argument(
        "--registry-ttl",
        type=float,
        default=24.0,
        metavar="HOURS",
        help="registry refresh interval in hours",
    )
    g3.add_argument(
        "--default-scope",
        default=None,
        metavar="CODE",
        help=(
            "region code (without #) to use when the incoming scope can't be "
            "resolved (e.g. 'de-bw'). If unset, replies are sent unscoped."
        ),
    )
    g3.add_argument(
        "--channel-rate",
        type=int,
        default=10,
        metavar="N",
        help=(
            "max replies per channel per hour (default: 10). "
            "Budget refills continuously up to the burst cap."
        ),
    )
    g3.add_argument(
        "--channel-burst",
        type=int,
        default=5,
        metavar="N",
        help=(
            "max burst of replies per channel before the budget refills (default: 5)."
        ),
    )
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


async def ensure_channels(
    mc: MeshCore, wanted: list[tuple[str, set[str] | None]]
) -> tuple[set[int], dict[int, str], dict[int, set[str] | None]]:
    """Ensure all *wanted* channel names exist on the companion.

    Returns (indices, name_map, allowed_map) where name_map is idx → lowercase
    channel name and allowed_map is idx → allowed command names (or None).
    """
    existing = await fetch_channels(mc)
    found: set[int] = set()
    used_indices: set[int] = set()
    existing_by_name: dict[str, int] = {}
    name_map: dict[int, str] = {}
    allowed_map: dict[int, set[str] | None] = {}

    wanted_names = {name.lower() for name, _ in wanted}

    for ch in existing:
        idx = ch["channel_idx"]
        used_indices.add(idx)
        name = str(ch.get("channel_name", "")).lower()
        if name:
            existing_by_name[name] = idx
            name_map[idx] = name
        if name in wanted_names:
            found.add(idx)

    # Channel 0 is "public" (no name); start searching from 1 for new channels.
    next_idx = 1
    for name, allowed in wanted:
        lname = name.lower()
        if lname in existing_by_name:
            idx = existing_by_name[lname]
            allowed_map[idx] = allowed
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
        name_map[next_idx] = lname
        allowed_map[next_idx] = allowed
        next_idx += 1

    return found, name_map, allowed_map


# ---------------------------------------------------------------------------
# Event handling
# ---------------------------------------------------------------------------


async def handle_dm(
    mc: MeshCore,
    registry: NodeRegistry,
    bot_name: str,
    location: str | None,
    telemetry: TelemetryLogger,
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
        path_hash_mode=hash_mode if isinstance(hash_mode, int) else 0,
        is_dm=True,
        contact=contact,
        msg=msg,
        location=location,
        telemetry=telemetry,
    )

    parsed = parse_dm_message(text)
    if parsed is None:
        await ctx.reply("unknown command. try help.")
        return
    await dispatch(ctx, parsed)


async def _find_chan_log_entry(
    mc: MeshCore, msg: dict[str, Any]
) -> dict[str, Any] | None:
    """Find the RF log entry matching a CHANNEL_MSG_RECV event.

    Uses ``txt_hash`` (present in V3 events) or computes it from
    ``sender_timestamp`` + ``text`` to look up the entry in
    ``channels_log``, which carries ``transport_code`` and ``pkt_payload``.
    """
    from hashlib import sha256

    parser = mc._reader.packet_parser  # type: ignore[attr-defined]
    txt_hash: int | None = msg.get("txt_hash")
    if txt_hash is None:
        ts = msg.get("sender_timestamp")
        text = str(msg.get("text", "")).encode("utf-8", "ignore")
        if ts is None:
            return None
        txt_hash = int.from_bytes(
            sha256(ts.to_bytes(4, "little", signed=False) + text).digest()[:4],
            "little",
            signed=False,
        )
    return await parser.findLogChannelMsg(txt_hash)  # type: ignore[attr-defined]


async def handle_channel(
    mc: MeshCore,
    registry: NodeRegistry,
    bot_name: str,
    scope_resolver: ScopeResolver,
    location: str | None,
    telemetry: TelemetryLogger,
    chan_indices: set[int],
    chan_names: dict[int, str],
    chan_allowed: dict[int, set[str] | None],
    rate_chan: MessageBudget,
    event: Event,
) -> None:
    msg: dict[str, Any] = event.payload or {}
    chan: Any = msg.get("channel_idx")
    text = str(msg.get("text", "")).strip()
    log.info("channel %s msg: %r", chan, text)
    if not text:
        return
    if chan not in chan_indices:
        return

    parsed = parse_channel_message(text, bot_name)
    if parsed is None:
        log.info("unparseable channel message on %s: %r", chan, text)
        return

    chan_key = f"chan:{chan}"

    # Resolve the flood scope from the RF log entry so the reply uses the
    # same scope as the incoming message.  Mirrors chooseReplyScope()
    # in RoutingPolicy.h: REQUEST > NONE > DEFAULT > NONE.
    log_entry = await _find_chan_log_entry(mc, msg)
    resolved = scope_resolver.resolve(log_entry)
    flood_scope_name = ""
    if resolved is None:
        # Scope unknowable: use fallback if set, else unscoped.
        fb = scope_resolver.fallback_key
        if fb:
            flood_scope = fb
            flood_scope_name = scope_resolver.fallback_name
            log.debug("scope unresolved, using fallback: %s", flood_scope_name)
        else:
            flood_scope = None
            log.debug("scope unresolved, sending unscoped")
    elif resolved[1] == b"":
        flood_scope = None  # unscoped → force unscoped
    else:
        flood_scope = resolved[1]
        flood_scope_name = resolved[0]
        log.debug("resolved scope: %s", flood_scope_name)

    hash_mode: Any = msg.get("path_hash_mode", 0)
    ctx = Context(
        mc=mc,
        registry=registry,
        sender=parsed.sender or "unknown",
        bot_name=bot_name,
        path_hash_mode=hash_mode if isinstance(hash_mode, int) else 0,
        is_dm=False,
        flood_scope=flood_scope,
        flood_scope_name=flood_scope_name,
        channel_idx=chan,
        channel_name=chan_names.get(chan, ""),
        channel_allowed=chan_allowed.get(chan),
        msg=msg,
        location=location,
        telemetry=telemetry,
        budget_check=lambda: rate_chan.check(chan_key),
    )
    await dispatch(ctx, parsed)


async def dispatch(ctx: Context, parsed: ParsedMessage) -> None:
    """Run the matching command, respecting scope and channel restrictions."""
    verb = parsed.verb

    # Empty verb means mention-only (no command) — ignore.
    if not verb:
        return

    # "?verb" is a help query; bare "?" shows the full help listing
    if verb.startswith("?"):
        name = verb[1:]
        if not name:
            await ctx.reply(full_help(ctx.is_dm, ctx.channel_allowed))
            return
        h = command_help(name)
        if h is not None:
            await ctx.reply(h)
        else:
            await ctx.reply(f"Unknown command: {name}")
        return

    cmd = get_command(verb)
    if cmd is None:
        if ctx.is_dm or parsed.mentioned:
            await ctx.reply(f'unknown command "{verb}". try help.')
        return

    # Scope check
    if cmd.scope is Scope.DM_ONLY and not ctx.is_dm:
        log.info("%s is DM_ONLY, ignoring in channel %s", verb, ctx.channel_name)
        return
    if cmd.scope is Scope.MENTION and not ctx.is_dm and not parsed.mentioned:
        return

    # Channel restriction: some channels only allow specific commands.
    # Commands with allowed_everywhere bypass this check.
    if (
        not ctx.is_dm
        and ctx.channel_allowed is not None
        and not cmd.allowed_everywhere
        and cmd.name not in ctx.channel_allowed
    ):
        log.info("%s not allowed on %s, ignoring", verb, ctx.channel_name)
        return

    if len(parsed.args) < cmd.min_args:
        await ctx.reply(f"usage: {usage_line(cmd)}")
        return

    # Budget check: only for channel messages, only when we're about to
    # actually reply.  This avoids wasting budget on commands that are
    # silently dropped by scope or channel restrictions.
    if ctx.budget_check is not None and not ctx.budget_check():
        log.info("channel %s budget exhausted, skipping reply", ctx.channel_name)
        return

    log.info(
        "dispatching %s (args=%s, mention=%s, dm=%s, chan=%s)",
        verb,
        parsed.args,
        parsed.mentioned,
        ctx.is_dm,
        ctx.channel_name or "-",
    )
    ctx.verb = verb
    try:
        await cmd.func(ctx, parsed.args)
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

    registry = NodeRegistry(Path(args.cache_path))
    log.info("loading node registry (may take a while on first run)...")
    await registry.load(ttl=int(args.registry_ttl * 3600))
    log.info(
        "registry ready: %d relay nodes, %d regions",
        len(registry.nodes),
        len(registry.regions),
    )

    mc = await connect(args)
    if mc is None:
        return
    log.info("connected to %s", mc.self_info.get("name", "?"))
    bot_name = str(mc.self_info.get("name", ""))

    # Read the companion's default flood scope and build a scope resolver.
    scope_resolver = ScopeResolver(registry)
    scope_res = await mc.commands.get_default_flood_scope()
    if not scope_res.is_error():
        scope_name = scope_res.payload.get("scope_name", "") or ""
        scope_key_hex = scope_res.payload.get("scope_key", "")
        if scope_key_hex:
            scope_resolver.set_default(scope_name, bytes.fromhex(scope_key_hex))
    log.info("default flood scope: %s", scope_resolver.default_name or "(none)")

    # Set the fallback scope for when the incoming scope can't be resolved.
    if args.default_scope:
        fb_key = scope_key(args.default_scope)
        scope_resolver.set_fallback("#" + args.default_scope, fb_key)
        log.info("fallback flood scope: %s", scope_resolver.fallback_name)

    await mc.ensure_contacts()
    log.info("fetched %d contacts", len(mc.contacts))

    mc.set_decrypt_channel_logs(True)
    channel_specs = parse_channel_specs(args.channels)
    chan_indices, chan_names, chan_allowed = await ensure_channels(mc, channel_specs)
    log.info("listening on channels: %s", chan_names or "(none)")

    telemetry = TelemetryLogger(mc, Path(args.cache_path).parent / "telemetry")
    telemetry.start()

    rate_chan = MessageBudget(
        max_budget=args.channel_burst,
        refill_per_sec=args.channel_rate / 3600.0,
    )

    async def on_direct(event: Event) -> None:
        await handle_dm(
            mc,
            registry,
            bot_name,
            args.location,
            telemetry,
            event,
        )

    async def on_channel(event: Event) -> None:
        await handle_channel(
            mc,
            registry,
            bot_name,
            scope_resolver,
            args.location,
            telemetry,
            chan_indices,
            chan_names,
            chan_allowed,
            rate_chan,
            event,
        )

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
        telemetry.stop()
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
