"""record command: start/stop telemetry recording for the sender."""

from typing import Any

import numpy as np
import orjson
from filterpy.kalman import KalmanFilter
from geopy.distance import geodesic

from meshcore_bot.commands.base import command
from meshcore_bot.commands.context import Context

_DEFAULT_PERIOD = 5  # minutes
_MIN_PERIOD = 1  # minute

# Kalman filter tuning: constant-position model operating on (lat, lon)
# degree measurements. R reflects GPS accuracy plus the ~8-11m quantization
# we observe at 0.0001 degree resolution; Q allows small real movement
# between samples while rejecting jitter.
_GPS_MEAS_NOISE_M = 10.0
_GPS_PROC_NOISE_M = 3.0


def _pubkey(ctx: Context) -> str:
    """Extract the sender's full public key."""
    assert ctx.contact is not None  # DM_ONLY: contact always set
    return str(ctx.contact["public_key"])


def _filtered_distance_m(gps_pts: list[tuple[float, float]]) -> float:
    """Estimate total path length through *gps_pts* using a Kalman filter.

    A 2-state (lat, lon) constant-position Kalman filter smooths GPS jitter
    in degree space; geopy then sums geodesic distances between the smoothed
    estimates. R and Q are anisotropic because one degree of longitude is
    shorter than one of latitude — they are scaled by the meters-per-degree
    factors at the track's reference latitude so covariance stays in meters.
    """
    if len(gps_pts) < 2:
        return 0.0

    ref_lat = gps_pts[0][0]
    m_per_deg_lat = geodesic((0.0, 0.0), (1.0, 0.0)).meters
    m_per_deg_lon = geodesic((ref_lat, 0.0), (ref_lat, 1.0)).meters

    kf = KalmanFilter(dim_x=2, dim_z=2)
    kf.x = np.array([gps_pts[0][0], gps_pts[0][1]])
    kf.F = np.eye(2)
    kf.H = np.eye(2)
    kf.R = np.diag(
        [
            (_GPS_MEAS_NOISE_M / m_per_deg_lat) ** 2,
            (_GPS_MEAS_NOISE_M / m_per_deg_lon) ** 2,
        ]
    )
    kf.Q = np.diag(
        [
            (_GPS_PROC_NOISE_M / m_per_deg_lat) ** 2,
            (_GPS_PROC_NOISE_M / m_per_deg_lon) ** 2,
        ]
    )
    kf.P = kf.R.copy()

    smoothed: list[tuple[float, float]] = [(float(kf.x[0]), float(kf.x[1]))]
    for lat, lon in gps_pts[1:]:
        kf.predict()
        kf.update(np.array([lat, lon]))
        smoothed.append((float(kf.x[0]), float(kf.x[1])))

    return sum(
        geodesic(smoothed[i - 1], smoothed[i]).meters for i in range(1, len(smoothed))
    )


def _summarize(records: list[dict[str, Any]]) -> dict[str, Any]:
    """Compute session aggregates from a list of telemetry records."""
    gps_pts: list[tuple[float, float]] = []
    temps: list[float] = []
    illum: list[float] = []
    volts: list[float] = []

    for rec in records:
        for entry in rec.get("data") or []:
            t = entry.get("type")
            v = entry.get("value")
            if t == "gps" and isinstance(v, dict):
                lat = v.get("latitude")
                lon = v.get("longitude")
                if (
                    isinstance(lat, int | float)
                    and isinstance(lon, int | float)
                    and not (lat == 0 and lon == 0)
                ):
                    gps_pts.append((float(lat), float(lon)))
            elif t == "temperature" and isinstance(v, int | float):
                temps.append(float(v))
            elif t == "illuminance" and isinstance(v, int | float):
                illum.append(float(v))
            elif t == "voltage" and isinstance(v, int | float):
                volts.append(float(v))

    distance = _filtered_distance_m(gps_pts)

    return {
        "distance_m": distance,
        "temp_min": min(temps) if temps else None,
        "temp_max": max(temps) if temps else None,
        "illum_min": min(illum) if illum else None,
        "illum_max": max(illum) if illum else None,
        "volt_first": volts[0] if volts else None,
        "volt_last": volts[-1] if volts else None,
    }


def _format_distance(m: float) -> str:
    """Format a distance, switching to km for 1km+ values."""
    if m < 1000:
        return f"{m:.0f}m"
    return f"{m / 1000:.2f}km"


def _format_status(period: int, readings: int, summary: dict[str, Any]) -> str:
    """Format a multiline recording status, mirroring the weather style."""
    mins = period // 60
    lines = [f"\u23f1\ufe0f {mins}min interval", f"\U0001f4ca {readings} readings"]

    dist = summary["distance_m"]
    lines.append(f"\U0001f5fa\ufe0f {_format_distance(dist)}")

    tmin = summary["temp_min"]
    tmax = summary["temp_max"]
    if tmin is not None and tmax is not None:
        lines.append(f"\U0001f321\ufe0f {tmin:.1f}~{tmax:.1f}C")

    imin = summary["illum_min"]
    imax = summary["illum_max"]
    if imin is not None and imax is not None:
        lines.append(f"\U0001f4a1 {imin:.0f}~{imax:.0f}lx")

    vfirst = summary["volt_first"]
    vlast = summary["volt_last"]
    if vfirst is not None and vlast is not None:
        delta = vlast - vfirst
        sign = "+" if delta >= 0 else ""
        lines.append(f"\U0001f50c {sign}{delta:.2f}V")

    return "\n".join(lines)


def _read_session(path: Any) -> list[dict[str, Any]]:
    """Read a telemetry session JSONL file into a list of records."""
    records: list[dict[str, Any]] = []
    try:
        with path.open() as f:
            for line in f:
                line = line.strip()
                if line:
                    records.append(orjson.loads(line))
    except OSError:
        pass
    return records


@command(["record", "log", "r"], dm_only=True, secret=True)
class _:
    """start, stop, or check telemetry recording"""

    default = "status"

    @staticmethod
    async def start(ctx: Context, period: int | None = None) -> None:
        """Start recording (N=min interval, default 5)."""
        assert ctx.telemetry is not None  # always set in main()
        pubkey = _pubkey(ctx)
        mins = max(period or _DEFAULT_PERIOD, _MIN_PERIOD)

        info = ctx.telemetry.get_status(pubkey)
        if info is not None:
            path = ctx.telemetry.session_path(pubkey)
            records = _read_session(path) if path is not None else []
            summary = _summarize(records)
            await ctx.reply(
                "Already recording:\n"
                + _format_status(info["period"], len(records), summary)
            )
            return

        ctx.telemetry.start_logging(pubkey, mins * 60)
        await ctx.reply(f"Started recording: {mins}min interval")

    @staticmethod
    async def stop(ctx: Context) -> None:
        """Stop recording."""
        assert ctx.telemetry is not None
        pubkey = _pubkey(ctx)

        path = ctx.telemetry.session_path(pubkey)
        records = _read_session(path) if path is not None else []
        summary = _summarize(records)
        info = ctx.telemetry.stop_logging(pubkey)
        if info is None:
            await ctx.reply("Not recording")
            return

        await ctx.reply(
            "Stopped recording:\n"
            + _format_status(info["period"], len(records), summary)
        )

    @staticmethod
    async def status(ctx: Context) -> None:
        """Show recording status."""
        assert ctx.telemetry is not None
        pubkey = _pubkey(ctx)

        info = ctx.telemetry.get_status(pubkey)
        if info is None:
            await ctx.reply("Not recording")
            return

        path = ctx.telemetry.session_path(pubkey)
        records = _read_session(path) if path is not None else []
        summary = _summarize(records)
        await ctx.reply(
            "Recording:\n" + _format_status(info["period"], len(records), summary)
        )
