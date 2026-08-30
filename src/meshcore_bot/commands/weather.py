"""weather command (alias: w)."""

from __future__ import annotations

import logging
from typing import Any

import httpx

from meshcore_bot.commands.base import Context, command
from meshcore_bot.registry import resolve_path

log = logging.getLogger(__name__)

_COMPASS = [
    "N",
    "NNE",
    "NE",
    "ENE",
    "E",
    "ESE",
    "SE",
    "SSE",
    "S",
    "SSW",
    "SW",
    "WSW",
    "W",
    "WNW",
    "NW",
    "NNW",
]


def _deg_to_compass(deg: float) -> str:
    return _COMPASS[round(deg / 22.5) % 16]


def _path_location(ctx: Context) -> tuple[float, float, str] | None:
    """Find the nearest located repeater on the message path.

    For channel messages, ``msg["path"]`` is the incoming route (sender side
    first). For DMs, the contact's ``out_path`` is the reply route (our side
    first). In both cases we want the hop closest to the sender that has a
    known location.
    """
    path_hex = str(ctx.msg.get("path") or "")
    if not path_hex and ctx.contact is not None:
        path_hex = str(ctx.contact.get("out_path") or "")

    if not path_hex:
        return None

    org = ctx.origin
    hops = resolve_path(ctx.registry, path_hex, ctx.path_hash_width, org)

    is_incoming = bool(ctx.msg.get("path"))
    ordered = hops if is_incoming else list(reversed(hops))

    for hop in ordered:
        if (
            hop.node is not None
            and hop.node.lat is not None
            and hop.node.lon is not None
        ):
            name = hop.node.name or hop.hex
            return (hop.node.lat, hop.node.lon, name)
    return None


def _lpp_gps(lpp: list[dict[str, Any]] | None) -> tuple[float, float] | None:
    """Extract GPS coordinates from a telemetry LPP response."""
    if not lpp:
        return None
    for entry in lpp:
        if entry.get("type") == "gps":
            val = entry.get("value")
            if isinstance(val, dict):
                lat = val.get("latitude")
                lon = val.get("longitude")
                if (
                    isinstance(lat, int | float)
                    and isinstance(lon, int | float)
                    and not (lat == 0 and lon == 0)
                ):
                    return (float(lat), float(lon))
    return None


# Day variants for codes that differ at night.
_WMO_NIGHT: dict[int, str] = {
    0: "\U0001f319 clear",
    1: "\U0001f319 mainly clear",
    2: "\u26c5 partly cloudy",
}

_WMO_CODES: dict[int, str] = {
    0: "\u2600\ufe0f clear",
    1: "\U0001f324\ufe0f mainly clear",
    2: "\u26c5 partly cloudy",
    3: "\u2601\ufe0f overcast",
    45: "\U0001f32b\ufe0f fog",
    48: "\U0001f32b\ufe0f rime fog",
    51: "\U0001f327\ufe0f light drizzle",
    53: "\U0001f327\ufe0f drizzle",
    55: "\U0001f327\ufe0f dense drizzle",
    56: "\U0001f327\ufe0f freezing drizzle",
    57: "\U0001f327\ufe0f freezing drizzle",
    61: "\U0001f327\ufe0f light rain",
    63: "\U0001f327\ufe0f rain",
    65: "\U0001f327\ufe0f heavy rain",
    66: "\U0001f327\ufe0f freezing rain",
    67: "\U0001f327\ufe0f freezing rain",
    71: "\u2744\ufe0f light snow",
    73: "\u2744\ufe0f snow",
    75: "\u2744\ufe0f heavy snow",
    77: "\u2744\ufe0f snow grains",
    80: "\U0001f326\ufe0f light showers",
    81: "\U0001f326\ufe0f showers",
    82: "\U0001f326\ufe0f violent showers",
    85: "\u2744\ufe0f snow showers",
    86: "\u2744\ufe0f snow showers",
    95: "\U0001f329\ufe0f thunderstorm",
    96: "\U0001f329\ufe0f thunderstorm+hail",
    99: "\U0001f329\ufe0f thunderstorm+hail",
}

_WEATHER_URL = "https://api.open-meteo.com/v1/forecast"
_GEOCODE_URL = "https://geocoding-api.open-meteo.com/v1/search"
_REVERSE_GEOCODE_URL = "https://api.bigdatacloud.net/data/reverse-geocode-client"


async def _geocode(
    place: str, ref: tuple[float, float] | None = None
) -> tuple[float, float, str] | None:
    """Resolve a place name to (lat, lon, display_name) via Open-Meteo geocoding.

    If *ref* (lat, lon) is given, return the nearest match instead of the
    first result — so ``Sölden`` near Freiburg picks BW, not Austria.
    """
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_GEOCODE_URL, params={"name": place, "count": 10})
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        log.warning("geocode failed for %r", place)
        return None
    results = data.get("results")
    if not results:
        return None
    best = results[0]
    if ref is not None:
        rlat, rlon = ref
        best = min(
            results,
            key=lambda r: (r["latitude"] - rlat) ** 2 + (r["longitude"] - rlon) ** 2,
        )
    name_parts = [best.get("name", ""), best.get("country", "")]
    name = ", ".join(p for p in name_parts if p) or place
    return (best["latitude"], best["longitude"], name)


async def _reverse_geocode(lat: float, lon: float) -> str | None:
    """Resolve coordinates to a place name via BigDataCloud."""
    try:
        async with httpx.AsyncClient(timeout=10, follow_redirects=True) as client:
            resp = await client.get(
                _REVERSE_GEOCODE_URL,
                params={"latitude": lat, "longitude": lon, "localityLanguage": "en"},
            )
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        log.warning("reverse geocode failed for %s,%s", lat, lon)
        return None
    name_parts = [data.get("city") or data.get("locality"), data.get("countryName")]
    return ", ".join(p for p in name_parts if p) or None


async def _fetch_weather(
    lat: float, lon: float
) -> tuple[dict[str, Any], dict[str, Any]] | None:
    """Fetch current weather and rain forecast from Open-Meteo.

    Returns (current_conditions, rain_info) or None. rain_info is a dict with:
    - "status": "raining" | "upcoming" | "none"
    - "amount": current precipitation mm (when raining)
    - "starts_in": minutes until rain starts (when upcoming)
    - "stops_in": minutes until rain stops (when raining, if known)
    """
    params: dict[str, str | float] = {
        "latitude": lat,
        "longitude": lon,
        "current": (
            "temperature_2m,apparent_temperature,weather_code,"
            "wind_speed_10m,wind_direction_10m,relative_humidity_2m,"
            "precipitation,is_day"
        ),
        "minutely_15": "precipitation",
        "forecast_minutely_15": "12",
        "timezone": "auto",
    }
    try:
        async with httpx.AsyncClient(timeout=10) as client:
            resp = await client.get(_WEATHER_URL, params=params)
            resp.raise_for_status()
            data = resp.json()
    except (httpx.HTTPError, ValueError):
        log.warning("weather fetch failed for %s,%s", lat, lon)
        return None
    current = data.get("current")
    if not isinstance(current, dict):
        return None

    rain_info: dict[str, Any] = {"status": "none"}
    minute_data = data.get("minutely_15")
    if isinstance(minute_data, dict):
        precip = minute_data.get("precipitation", [])
        if isinstance(precip, list) and precip:
            now_raining = isinstance(precip[0], (int, float)) and precip[0] > 0
            if now_raining:
                rain_info["status"] = "raining"
                rain_info["amount"] = precip[0]
                for i, val in enumerate(precip[1:], start=1):
                    if not isinstance(val, (int, float)) or val <= 0:
                        rain_info["stops_in"] = i * 15
                        break
            else:
                for i, val in enumerate(precip[1:], start=1):
                    if isinstance(val, (int, float)) and val > 0:
                        rain_info["status"] = "upcoming"
                        rain_info["starts_in"] = i * 15
                        break

    return current, rain_info


async def _find_coords(ctx: Context) -> tuple[float, float, str] | None:
    """Find the sender's area coordinates and a display name.

    Priority: nearest repeater on path → sender's telemetry GPS →
    bot's companion GPS → geocode ctx.location.
    """
    # 1. Nearest located repeater on message path (live, from current message)
    coords = _path_location(ctx)
    if coords is not None:
        return coords

    # 2. Sender's telemetry GPS (async request to their device, DMs only)
    if ctx.contact is not None:
        try:
            lpp = await ctx.mc.commands.req_telemetry_sync(ctx.contact, timeout=10)
            tc = _lpp_gps(lpp)
            if tc is not None:
                name = await _reverse_geocode(*tc)
                return (tc[0], tc[1], name or str(ctx.contact.get("adv_name", "")))
        except Exception:
            log.debug("telemetry request failed", exc_info=True)

    # 3. Bot's companion GPS
    org = ctx.origin
    if org is not None:
        name = await _reverse_geocode(*org)
        if name:
            return (org[0], org[1], name)

    # 4. Geocode ctx.location
    if ctx.location:
        geocoded = await _geocode(ctx.location)
        if geocoded is not None:
            return geocoded

    return None


async def _resolve_location(
    ctx: Context, args: list[str]
) -> tuple[float, float, str] | str | None:
    """Resolve the weather target location.

    Returns one of:
    - (lat, lon, name) on success
    - str (error message) if a location was given but not found
    - None if no location is available at all
    """
    found = await _find_coords(ctx)
    ref = (found[0], found[1]) if found is not None else None

    if args:
        place = " ".join(args)
        coords = await _geocode(place, ref)
        if coords is None:
            return f"Could not find: {place}"
        return coords

    return found


@command(["weather", "w"], usage="[place]")
async def _(ctx: Context, args: list[str]) -> None:
    """Show current weather."""
    result = await _resolve_location(ctx, args)
    if result is None:
        await ctx.reply("No location available. Try: weather <place>")
        return
    if isinstance(result, str):
        await ctx.reply(result)
        return
    lat, lon, name = result

    weather_result = await _fetch_weather(lat, lon)
    if weather_result is None:
        await ctx.reply("Weather data unavailable.")
        return
    weather, rain = weather_result

    temp = weather["temperature_2m"]
    feels = weather.get("apparent_temperature")
    code = weather.get("weather_code", 0)
    is_day = weather.get("is_day", 1)
    cond_table = _WMO_NIGHT if is_day == 0 else _WMO_CODES
    cond = cond_table.get(code, _WMO_CODES.get(code, "unknown"))
    wind = weather.get("wind_speed_10m")
    wind_dir = weather.get("wind_direction_10m")
    humidity = weather.get("relative_humidity_2m")

    lines = [f"{name}:", cond]
    lines.append(
        "\U0001f321\ufe0f "
        + f"{temp:.0f}C"
        + (f" ({feels:.0f}C)" if feels is not None and abs(feels - temp) >= 2 else "")
    )
    if wind is not None:
        wind_str = f"\U0001f32c\ufe0f {wind:.0f}kmh"
        if wind_dir is not None:
            wind_str += f" ({_deg_to_compass(float(wind_dir))})"
        lines.append(wind_str)
    if humidity is not None:
        if humidity < 20:
            emoji = "\U0001f335"
        elif humidity > 80:
            emoji = "\U0001fae0"
        else:
            emoji = "\U0001f4a7"
        lines.append(f"{emoji} " + f"{humidity:.0f}%")

    status = rain.get("status", "none")
    if status == "raining":
        amount = rain.get("amount", 0)
        stops = rain.get("stops_in")
        if stops is not None:
            lines.append(f"\u2614\ufe0f {amount:.1f}mm (stops in {stops}min)")
        else:
            lines.append(f"\u2614\ufe0f {amount:.1f}mm")
    elif status == "upcoming":
        starts = rain.get("starts_in")
        lines.append(f"\u2614\ufe0f in {starts}min")
    else:
        lines.append("\U0001f302 no rain")
    await ctx.reply("\n".join(lines))
