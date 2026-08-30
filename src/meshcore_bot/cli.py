"""CLI argument parser and companion connection (mirrors meshcore-cli flags)."""

import argparse
import logging
from pathlib import Path
from typing import Any

from meshcore import MeshCore

log = logging.getLogger("meshcore_bot")


_CHANNEL_SPEC_EPILOG = """\
channel spec syntax (--channels):

  name                 listen on channel, all commands allowed
  name[cmd1,cmd2]      only listed commands allowed
  name~                all commands mention-free (no @bot needed)
  name~[cmd1,cmd2]     combination of both
  name[~cmd1,cmd2]     cmd1 mention-free, cmd2 still needs @bot

Separate channel specs with commas, and/or repeat --channels argument.
Example: --channels "ping[~ping], test[~ping, path], weather~[weather], bot"
"""


class _HelpFormatter(
    argparse.ArgumentDefaultsHelpFormatter, argparse.RawDescriptionHelpFormatter
):
    """Show defaults inline and preserve epilog formatting."""


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="meshcore-bot",
        description="Reply to MeshCore DMs and channel mentions with path stats.",
        formatter_class=_HelpFormatter,
        add_help=False,
        epilog=_CHANNEL_SPEC_EPILOG,
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
        help="channel spec (see syntax below)",
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
