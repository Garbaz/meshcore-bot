"""MeshCore bot: reply to DMs and channel mentions with path stats and more.

Connects to a companion node (TCP, serial, or BLE), fetches contacts and
channels at startup, and dispatches commands from incoming DMs or channel
messages that @-mention the bot. Use ``--help`` for CLI options.
"""

import argparse
import asyncio
import logging
from hashlib import sha256
from pathlib import Path
from typing import Any

from meshcore import EventType, MeshCore
from meshcore.events import Event

from meshcore_bot.budget import MessageBudget
from meshcore_bot.channel_spec import ChannelSpec, parse_channel_specs
from meshcore_bot.cli import build_parser, connect
from meshcore_bot.commands import (
    Context,
    command_help,
    full_help,
    get_command,
)
from meshcore_bot.dedup import MessageDedup
from meshcore_bot.parser import (
    ParsedMessage,
    parse_channel_message,
    parse_dm_message,
)
from meshcore_bot.registry import NodeRegistry
from meshcore_bot.scope import ScopeResolver
from meshcore_bot.telemetry import TelemetryLogger

log = logging.getLogger("meshcore_bot")


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
    mc: MeshCore, wanted: list[ChannelSpec]
) -> dict[int, ChannelSpec]:
    """Ensure all *wanted* channel names exist on the companion.

    Returns a mapping of channel index to ChannelSpec for all
    wanted channels that were found or created.
    """
    existing = await fetch_channels(mc)
    used_indices: set[int] = set()
    existing_by_name: dict[str, int] = {}
    configs: dict[int, ChannelSpec] = {}

    for ch in existing:
        idx = ch["channel_idx"]
        name = str(ch.get("channel_name", "")).lower()
        if name:
            used_indices.add(idx)
            existing_by_name[name] = idx
        # Empty-named slots are available for reuse.

    next_idx = 1  # Channel 0 is "public" (no name); start from 1.
    for spec in wanted:
        lname = spec.name.lower()
        if lname in existing_by_name:
            idx = existing_by_name[lname]
            configs[idx] = spec
            continue
        while next_idx in used_indices:
            next_idx += 1
        log.info("creating channel %s at index %d", lname, next_idx)
        res = await mc.commands.set_channel(next_idx, lname)
        if res.is_error():
            log.error("failed to create channel %s: %s", lname, res.payload)
            continue
        configs[next_idx] = spec
        used_indices.add(next_idx)
        next_idx += 1

    return configs


async def _resolve_reply_scope(
    scope_resolver: ScopeResolver, log_entry: dict[str, Any] | None
) -> tuple[bytes | None, str]:
    """Resolve the flood scope for a reply.

    Mirrors chooseReplyScope() in RoutingPolicy.h:
    REQUEST > NONE > DEFAULT > NONE.

    Returns (scope_key, scope_name). scope_key is None for unscoped.
    """
    resolved = scope_resolver.resolve(log_entry)
    if resolved is None:
        # Scope unknowable: use fallback if set, else unscoped.
        fb = scope_resolver.fallback_key
        if fb:
            log.debug(
                "scope unresolved, using fallback: %s", scope_resolver.fallback_name
            )
            return fb, scope_resolver.fallback_name
        log.debug("scope unresolved, sending unscoped")
        return None, ""
    if resolved[1] == b"":
        return None, ""  # unscoped: force unscoped
    log.debug("resolved scope: %s", resolved[0])
    return resolved[1], resolved[0]


async def handle_dm(
    mc: MeshCore,
    registry: NodeRegistry,
    bot_name: str,
    location: str | None,
    telemetry: TelemetryLogger,
    dedup: MessageDedup,
    startup_time: int,
    scope_resolver: ScopeResolver,
    event: Event,
) -> None:
    msg: dict[str, Any] = event.payload or {}
    prefix: str = msg.get("pubkey_prefix", "")
    text = str(msg.get("text", "")).strip()
    if not text:
        return

    # Dedup: same DM can arrive multiple times (retransmits, multiple paths).
    ts: Any = msg.get("sender_timestamp", 0)
    dedup_key = f"dm:{prefix}:{ts}:{text}"
    if not dedup.check(dedup_key):
        return

    # Ignore messages from before the bot started (companion clock).
    if isinstance(ts, int) and ts < startup_time:
        log.debug("ignoring pre-startup DM from %s (ts=%d)", prefix, ts)
        return

    log.info("DM from %s: %r", prefix, text)

    # Refresh contacts so the sender's path is visible.
    await mc.ensure_contacts()
    contact = mc.get_contact_by_key_prefix(prefix)
    if contact is None:
        log.warning("sender %s not in contacts, not replying", prefix)
        return

    sender = str(contact.get("adv_name") or prefix)
    hash_mode: Any = msg.get("path_hash_mode", 0)

    # DMs aren't stored in channels_log, so we can't look up the RF log
    # entry. The resolver returns None, falling back to the companion's
    # default scope — the same logic as channel messages.
    flood_scope, flood_scope_name = await _resolve_reply_scope(scope_resolver, None)

    ctx = Context(
        mc=mc,
        registry=registry,
        sender=sender,
        bot_name=bot_name,
        path_hash_mode=hash_mode if isinstance(hash_mode, int) else 0,
        is_dm=True,
        flood_scope=flood_scope,
        flood_scope_name=flood_scope_name,
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
    chan_configs: dict[int, ChannelSpec],
    rate_chan: MessageBudget,
    dedup: MessageDedup,
    startup_time: int,
    event: Event,
) -> None:
    msg: dict[str, Any] = event.payload or {}
    chan: Any = msg.get("channel_idx")
    text = str(msg.get("text", "")).strip()
    if not text:
        return

    # Dedup: same flood packet arrives via multiple paths.
    ts: Any = msg.get("sender_timestamp", 0)
    txt_hash: Any = msg.get("txt_hash", 0)
    dedup_key = f"ch:{chan}:{ts}:{txt_hash or text}"
    if not dedup.check(dedup_key):
        return

    # Ignore messages from before the bot started (companion clock).
    if isinstance(ts, int) and ts < startup_time:
        log.debug("ignoring pre-startup channel msg on %s (ts=%d)", chan, ts)
        return

    spec = chan_configs.get(chan)
    if spec is None:
        log.debug("channel %s (not listened) msg: %r", chan, text)
        return

    log.info("channel %s msg: %r", spec.name, text)

    parsed = parse_channel_message(text, bot_name)
    if parsed is None:
        log.info("unparseable channel message on %s: %r", spec.name, text)
        return

    # Resolve the flood scope from the RF log entry so the reply uses the
    # same scope as the incoming message.
    log_entry = await _find_chan_log_entry(mc, msg)
    flood_scope, flood_scope_name = await _resolve_reply_scope(
        scope_resolver, log_entry
    )

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
        channel_name=spec.name,
        channel_allowed=spec.allowed,
        channel_open=spec.open,
        open_cmds=spec.open_cmds,
        msg=msg,
        location=location,
        telemetry=telemetry,
        budget_check=lambda: rate_chan.check(f"chan:{chan}"),
    )
    await dispatch(ctx, parsed)


async def dispatch(ctx: Context, parsed: ParsedMessage) -> None:
    """Run the matching command, respecting scope and channel restrictions."""
    verb = parsed.verb

    # Empty verb means mention-only (no command): ignore.
    if not verb:
        return

    # "?verb" is a help query; bare "?" shows the full help listing.
    # "?record start" queries a subcommand.
    if verb.startswith("?"):
        name = verb[1:]
        if not name:
            await ctx.reply(full_help(ctx.is_dm, ctx.channel_allowed))
            return
        sub = parsed.args[0] if parsed.args else None
        h = command_help(name, sub)
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

    # dm_only commands are silently ignored in channels.
    if cmd.dm_only and not ctx.is_dm:
        log.info("%s is dm_only, ignoring in channel %s", verb, ctx.channel_name)
        return
    # Mention required in channels unless relaxed by ~ config.
    if (
        not ctx.is_dm
        and not parsed.mentioned
        and not ctx.channel_open
        and cmd.name not in ctx.open_cmds
    ):
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

    # Budget check: only for channel messages, only when we're about to
    # actually reply.  This avoids wasting budget on commands that are
    # silently dropped by scope or channel restrictions.
    if ctx.budget_check is not None and not ctx.budget_check():
        log.info("channel %s budget exhausted, skipping reply", ctx.channel_name)
        return

    ctx.verb = verb
    log.info(
        "dispatching %s (args=%s, mention=%s, dm=%s, chan=%s)",
        verb,
        parsed.args,
        parsed.mentioned,
        ctx.is_dm,
        ctx.channel_name or "-",
    )
    try:
        err = await cmd.call(ctx, parsed.args)
        if err is not None:
            await ctx.reply(err)
    except Exception:
        log.exception("command %s failed", verb)


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

    # Read the companion's default flood scope and use it as both the
    # primary match (checked first on resolve) and the fallback (used
    # when the incoming scope can't be determined). The bot never calls
    # set_default_flood_scope, so the companion's setting is respected.
    scope_resolver = ScopeResolver(registry)
    scope_res = await mc.commands.get_default_flood_scope()
    if not scope_res.is_error():
        scope_name = scope_res.payload.get("scope_name", "") or ""
        scope_key_hex = scope_res.payload.get("scope_key", "")
        if scope_key_hex:
            scope_key_bytes = bytes.fromhex(scope_key_hex)
            scope_resolver.set_default(scope_name, scope_key_bytes)
            scope_resolver.set_fallback(scope_name, scope_key_bytes)
    log.info("default flood scope: %s", scope_resolver.default_name or "(none)")

    await mc.ensure_contacts()
    log.info("fetched %d contacts", len(mc.contacts))

    mc.set_decrypt_channel_logs(True)
    channel_specs = parse_channel_specs(args.channels)
    chan_configs = await ensure_channels(mc, channel_specs)
    log.info(
        "listening on channels: %s",
        ", ".join(s.name for s in chan_configs.values()) or "(none)",
    )

    telemetry = TelemetryLogger(mc, Path(args.cache_path).parent / "telemetry")
    telemetry.start()

    rate_chan = MessageBudget(
        max_budget=args.channel_burst,
        refill_per_sec=args.channel_rate / 3600.0,
    )

    dedup = MessageDedup()

    # Drain any backlog of messages from the companion before subscribing
    # our handlers, so we don't reply to messages sent before the bot started.
    log.info("draining message backlog...")
    while True:
        res = await mc.commands.get_msg()
        if res.type in (EventType.NO_MORE_MSGS, EventType.ERROR):
            break
    log.info("backlog drained")

    # Record the companion's current time as the startup cutoff. Messages
    # with sender_timestamp before this are ignored (second line of defense
    # against backlog, in case draining missed any).
    startup_time = mc.time
    log.info("companion time: %d", startup_time)

    async def on_direct(event: Event) -> None:
        await handle_dm(
            mc,
            registry,
            bot_name,
            args.location,
            telemetry,
            dedup,
            startup_time,
            scope_resolver,
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
            chan_configs,
            rate_chan,
            dedup,
            startup_time,
            event,
        )

    mc.subscribe(EventType.CONTACT_MSG_RECV, on_direct)  # pyright: ignore[reportArgumentType]
    mc.subscribe(EventType.CHANNEL_MSG_RECV, on_channel)  # pyright: ignore[reportArgumentType]

    await mc.start_auto_message_fetching()

    log.info("listening for messages...")
    try:
        while True:
            await asyncio.sleep(3600)
    except asyncio.CancelledError:
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
