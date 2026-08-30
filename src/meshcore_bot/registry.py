"""Online repeater registries for resolving path-hop key prefixes to names.

Sources are tried in order (official first, community mirrors after). Each
source is fetched in parallel, normalized, and merged into a single local
cache file; first source that knows a node wins. The cache is refreshed in
the background by a periodic task so lookups never block.
"""

import asyncio
import bisect
import hashlib
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx
import orjson

log = logging.getLogger(__name__)

REGISTRY_TTL = 24 * 3600
FETCH_TIMEOUT = 60
USER_AGENT = "meshcore-bot/0.1"


@dataclass(frozen=True)
class Source:
    """A node registry HTTP endpoint plus its response dialect."""

    name: str
    url: str
    dialect: str  # "official" | "netz" | "corescope"


SOURCES = [
    Source("official-map", "https://map.meshcore.dev/api/v1/nodes?short=1", "official"),
    Source("meshcorenetz", "https://analyzer.meshcorenetz.de/api/nodes", "netz"),
    Source("meshcore-analyzer-eu", "https://meshcore-analyzer.eu/api/nodes", "netz"),
    Source("corescope", "https://analyzer.00id.net/api/nodes", "corescope"),
]

REGION_SOURCES = [
    Source(
        "meshcore-regions",
        "https://raw.githubusercontent.com/marcelverdult/meshcore-regions/main/index.json",
        "regions",
    ),
]

RELAY_ROLES = {"repeater", "room"}  # only these forward packets, per MeshCore


def scope_key(code: str) -> bytes:
    """Derive the 16-byte flood scope key for a ``#hashtag`` region."""
    name = code if code.startswith("#") else "#" + code
    return hashlib.sha256(name.encode("utf-8")).digest()[:16]


@dataclass(slots=True)
class Node:
    public_key: str  # 64-char lowercase hex
    name: str
    role: str  # "repeater" | "room" | "other"
    lat: float | None
    lon: float | None
    last_seen: int | None  # unix epoch, best effort


@dataclass(frozen=True, slots=True)
class Region:
    code: str  # e.g. "at-bgld" (hashtag without #)
    name: str  # human-readable, e.g. "bgld"


def _iso_to_epoch(value: Any) -> int | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        from datetime import datetime

        return int(datetime.fromisoformat(value).timestamp())
    except ValueError:
        return None


def _coord(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, int | float) or value == 0:
        return None  # registries report unset coordinates as 0
    return float(value)


_ROLE_BY_INT = {1: "companion", 2: "repeater", 3: "room", 4: "sensor"}


def _norm_role(value: Any) -> str:
    if isinstance(value, bool):
        return "other"
    if isinstance(value, int):
        return _ROLE_BY_INT.get(value, "other")
    s = str(value).lower() if value is not None else ""
    if "repeat" in s:
        return "repeater"
    if "room" in s:
        return "room"
    return "other"


def _parse_official(node: dict[str, Any]) -> Node | None:
    key = node.get("public_key")
    if not isinstance(key, str) or len(key) != 64:
        return None
    return Node(
        public_key=key.lower(),
        name=str(node.get("adv_name") or "").strip(),
        role=_norm_role(node.get("type")),
        lat=_coord(node.get("adv_lat")),
        lon=_coord(node.get("adv_lon")),
        last_seen=_iso_to_epoch(node.get("last_advert")),
    )


def _parse_netz(node: dict[str, Any]) -> Node | None:
    key = node.get("ID")
    if not isinstance(key, str) or len(key) != 64:
        return None
    return Node(
        public_key=key.lower(),
        name=str(node.get("Name") or "").strip(),
        role=_norm_role(node.get("Role")),
        lat=_coord(node.get("Lat")),
        lon=_coord(node.get("Lon")),
        last_seen=_iso_to_epoch(node.get("LastSeen")),
    )


def _parse_corescope(node: dict[str, Any]) -> Node | None:
    key = node.get("public_key")
    if not isinstance(key, str) or len(key) != 64:
        return None
    return Node(
        public_key=key.lower(),
        name=str(node.get("name") or "").strip(),
        role=_norm_role(node.get("role")),
        lat=_coord(node.get("lat")),
        lon=_coord(node.get("lon")),
        last_seen=_iso_to_epoch(node.get("last_seen")),
    )


_PARSERS = {
    "official": _parse_official,
    "netz": _parse_netz,
    "corescope": _parse_corescope,
}


class NodeRegistry:
    """Merged, cached view of all sources; prefix lookups via binary search.

    The registry is loaded once at startup (``load``) and then kept fresh by
    a background task (``start_background_refresh``). Lookups always read
    in-memory indexes that are swapped atomically after a refresh, so they
    never block on network I/O.
    """

    def __init__(
        self,
        cache_path: Path,
        sources: list[Source] | None = None,
        region_sources: list[Source] | None = None,
    ):
        self._cache_path = cache_path
        self._sources = sources or SOURCES
        self._region_sources = region_sources or REGION_SOURCES
        self._nodes: list[Node] = []
        self._keys: list[str] = []  # sorted public keys, parallel to _nodes
        self._by_key: dict[str, Node] = {}
        self._regions: list[Region] = []
        self._scope_keys: dict[bytes, str] = {}  # scope_key -> "#code"
        self._refresh_task: asyncio.Task[None] | None = None

    @property
    def nodes(self) -> list[Node]:
        return self._nodes

    @property
    def regions(self) -> list[Region]:
        return self._regions

    @property
    def scope_keys(self) -> dict[bytes, str]:
        """Mapping of 16-byte scope keys to ``"#code"`` region names."""
        return self._scope_keys

    async def load(self, ttl: int = REGISTRY_TTL) -> None:
        """Initial load: read cache, fetch stale sources in parallel."""
        cache = self._read_cache()
        now = int(time.time())

        node_results = await asyncio.gather(
            *(self._refresh_source(s, cache, now, ttl) for s in self._sources)
        )
        region_results = await asyncio.gather(
            *(
                self._refresh_region_source(s, cache, now, ttl)
                for s in self._region_sources
            )
        )

        if any(node_results) or any(region_results):
            self._write_cache(cache)

        self._build_indexes(cache, now)

    async def refresh(self) -> None:
        """Fetch all sources (ignoring TTL), rebuild, and swap indexes."""
        cache = self._read_cache()
        now = int(time.time())

        await asyncio.gather(
            *(self._refresh_source(s, cache, now, ttl=0) for s in self._sources)
        )
        await asyncio.gather(
            *(
                self._refresh_region_source(s, cache, now, ttl=0)
                for s in self._region_sources
            )
        )

        self._write_cache(cache)
        self._build_indexes(cache, now)

    def start_background_refresh(self, interval: int = REGISTRY_TTL // 2) -> None:
        """Start a periodic background refresh task."""
        if self._refresh_task is None or self._refresh_task.done():
            self._refresh_task = asyncio.create_task(self._refresh_loop(interval))

    def stop_background_refresh(self) -> None:
        """Cancel the background refresh task."""
        if self._refresh_task is not None:
            self._refresh_task.cancel()
            self._refresh_task = None

    async def _refresh_loop(self, interval: int) -> None:
        """Periodically refresh the registry."""
        while True:
            await asyncio.sleep(interval)
            try:
                log.info("registry: background refresh starting")
                await self.refresh()
                log.info("registry: background refresh done")
            except asyncio.CancelledError:
                raise
            except Exception:
                log.warning("registry: background refresh failed", exc_info=True)

    async def _refresh_source(
        self, source: Source, cache: dict[str, Any], now: int, ttl: int
    ) -> bool:
        """Fetch *source* if stale (or ttl=0). Returns True if cache changed."""
        entry = cache.get(source.name)
        if entry is not None and ttl > 0 and now - entry["fetched_at"] < ttl:
            return False

        nodes = await self._fetch(source)
        if nodes is None:
            if entry is not None:
                log.warning("%s: fetch failed, using stale cache", source.name)
            return False

        cache[source.name] = {"fetched_at": now, "nodes": nodes}
        return True

    async def _refresh_region_source(
        self, source: Source, cache: dict[str, Any], now: int, ttl: int
    ) -> bool:
        """Fetch region *source* if stale (or ttl=0). Returns True if changed."""
        entry = cache.get(source.name)
        if entry is not None and ttl > 0 and now - entry["fetched_at"] < ttl:
            return False

        regions = await self._fetch_regions(source)
        if regions is None:
            if entry is not None:
                log.warning("%s: fetch failed, using stale cache", source.name)
            return False

        cache[source.name] = {"fetched_at": now, "regions": regions}
        return True

    def _build_indexes(self, cache: dict[str, Any], now: int) -> None:
        """Merge cached sources into in-memory lookup indexes."""
        merged: dict[str, Node] = {}
        for source in self._sources:
            entry = cache.get(source.name)
            if entry is None:
                continue
            for node in entry["nodes"]:
                merged.setdefault(node.public_key, node)

        merged_regions: dict[str, Region] = {}
        for source in self._region_sources:
            entry = cache.get(source.name)
            if entry is None:
                continue
            for r in entry.get("regions", []):
                merged_regions.setdefault(r.code, r)

        relays = [n for n in merged.values() if n.role in RELAY_ROLES]
        nodes = sorted(relays, key=lambda n: n.public_key)
        keys = [n.public_key for n in nodes]
        by_key = {n.public_key: n for n in nodes}
        regions = sorted(merged_regions.values(), key=lambda r: r.code)
        scope_keys = {scope_key(r.code): "#" + r.code for r in regions}

        # Atomic swap: readers see old or new snapshot, never a mix.
        self._nodes = nodes
        self._keys = keys
        self._by_key = by_key
        self._regions = regions
        self._scope_keys = scope_keys
        log.info(
            "registry loaded: %d relay nodes from %d sources, %d regions",
            len(nodes),
            len(self._sources),
            len(regions),
        )

    async def _http_get(self, source: Source) -> Any:
        """Fetch JSON from *source*, logging a warning on failure."""
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                timeout=FETCH_TIMEOUT,
                follow_redirects=True,
            ) as client:
                resp = await client.get(source.url)
                resp.raise_for_status()
                return orjson.loads(resp.content)
        except (httpx.HTTPError, orjson.JSONDecodeError):
            log.warning("%s: fetch failed", source.name, exc_info=True)
            return None

    async def _fetch(self, source: Source) -> list[Node] | None:
        payload = await self._http_get(source)
        if payload is None:
            return None
        if source.dialect == "corescope":
            payload = payload.get("nodes", []) if isinstance(payload, dict) else payload
        if not isinstance(payload, list):
            log.warning("%s: unexpected payload shape", source.name)
            return None
        nodes = []
        for raw in payload:
            if not isinstance(raw, dict):
                continue
            node = _PARSERS[source.dialect](raw)
            if node is not None:
                nodes.append(node)
        log.info(
            "%s: fetched %d nodes (%d relays)",
            source.name,
            len(nodes),
            sum(1 for n in nodes if n.role in RELAY_ROLES),
        )
        return nodes

    async def _fetch_regions(self, source: Source) -> list[Region] | None:
        payload = await self._http_get(source)
        if payload is None:
            return None
        flat = payload.get("flat", []) if isinstance(payload, dict) else []
        regions: list[Region] = []
        for raw in flat:
            if not isinstance(raw, dict):
                continue
            code = raw.get("path", "")
            name = raw.get("name", "")
            if code:
                regions.append(Region(code=code, name=name))
        log.info("%s: fetched %d regions", source.name, len(regions))
        return regions

    def _read_cache(self) -> dict[str, Any]:
        try:
            data = orjson.loads(self._cache_path.read_bytes())
            if not isinstance(data, dict):
                return {}
            out: dict[str, Any] = {}
            for name, entry in data.items():
                item: dict[str, Any] = {"fetched_at": int(entry["fetched_at"])}
                if "nodes" in entry:
                    item["nodes"] = [Node(**n) for n in entry["nodes"]]
                if "regions" in entry:
                    item["regions"] = [Region(**r) for r in entry["regions"]]
                out[name] = item
            return out
        except (OSError, orjson.JSONDecodeError, KeyError, TypeError):
            return {}

    def _write_cache(self, cache: dict[str, Any]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        data: dict[str, Any] = {}
        for name, entry in cache.items():
            item: dict[str, Any] = {"fetched_at": entry["fetched_at"]}
            if "nodes" in entry:
                item["nodes"] = [asdict(n) for n in entry["nodes"]]
            if "regions" in entry:
                item["regions"] = [asdict(r) for r in entry["regions"]]
            data[name] = item
        tmp = self._cache_path.with_suffix(".tmp")
        tmp.write_bytes(orjson.dumps(data))
        tmp.replace(self._cache_path)

    def lookup_prefix(self, prefix: str) -> list[Node]:
        """All relay nodes whose public key starts with the hex prefix."""
        prefix = prefix.lower()
        lo = bisect.bisect_left(self._keys, prefix)
        hi = lo
        while hi < len(self._keys) and self._keys[hi].startswith(prefix):
            hi += 1
        return [self._by_key[self._keys[i]] for i in range(lo, hi)]


@dataclass(slots=True)
class ResolvedHop:
    hex: str
    node: Node | None
    ambiguous: bool  # multiple candidates and no location to disambiguate


def resolve_path(
    registry: NodeRegistry,
    path_hex: str,
    hash_width: int,
    origin: tuple[float, float] | None,
) -> list[ResolvedHop]:
    """Resolve path hop hashes to registry nodes.

    ``path_hex`` is given in device-travel order (first hop adjacent to us,
    last hop adjacent to the sender). Disambiguation walks outward from
    ``origin`` (our own coordinates), like meshcore-open does.
    """
    width = max(1, hash_width) * 2  # hex chars per hop
    hops = [path_hex[i : i + width] for i in range(0, len(path_hex), width)]
    resolved: list[ResolvedHop | None] = [None] * len(hops)

    ref = origin
    for i, hop_hex in enumerate(hops):
        candidates = registry.lookup_prefix(hop_hex)
        if not candidates:
            resolved[i] = ResolvedHop(hop_hex, None, False)
            continue
        ambiguous = False
        if len(candidates) == 1:
            chosen = candidates[0]
        else:
            located = [c for c in candidates if c.lat is not None and c.lon is not None]
            if ref is not None and located:
                lat, lon = ref
                chosen = min(
                    located,
                    key=lambda c: (c.lat - lat) ** 2 + (c.lon - lon) ** 2,  # type: ignore[operator]
                )
            else:
                ordered = sorted(
                    candidates, key=lambda c: c.last_seen or 0, reverse=True
                )
                chosen = ordered[0]
                ambiguous = True
        resolved[i] = ResolvedHop(hop_hex, chosen, ambiguous)
        if chosen.lat is not None and chosen.lon is not None:
            ref = (chosen.lat, chosen.lon)

    return [r for r in resolved if r is not None]
