"""Online repeater registries for resolving path-hop key prefixes to names.

Sources are tried in order (official first, community mirrors after). Each
source is fetched once, normalized, and merged into a single local cache file;
first source that knows a node wins. The cache is refreshed when older than
REGISTRY_TTL (stale data is kept if a refresh fails).
"""

from __future__ import annotations

import bisect
import json
import logging
import time
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

import httpx

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

RELAY_ROLES = {"repeater", "room"}  # only these forward packets, per MeshCore


@dataclass
class Node:
    public_key: str  # 64-char lowercase hex
    name: str
    role: str  # "repeater" | "room" | "other"
    lat: float | None
    lon: float | None
    last_seen: int | None  # unix epoch, best effort


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
    """Merged, cached view of all sources; prefix lookups via binary search."""

    def __init__(self, cache_path: Path, sources: list[Source] | None = None):
        self._cache_path = cache_path
        self._sources = sources or SOURCES
        self._nodes: list[Node] = []
        self._keys: list[str] = []  # sorted public keys, parallel to _nodes
        self._by_key: dict[str, Node] = {}

    @property
    def nodes(self) -> list[Node]:
        return self._nodes

    async def load(self, ttl: int = REGISTRY_TTL, force: bool = False) -> None:
        cache = self._read_cache()
        now = int(time.time())
        merged: dict[str, tuple[Node, str]] = {}

        def absorb(nodes: list[Node], source_name: str) -> None:
            for node in nodes:
                merged.setdefault(node.public_key, (node, source_name))

        for source in self._sources:
            entry = cache.get(source.name)
            fresh = entry is not None and now - entry["fetched_at"] < ttl
            if entry is not None and fresh and not force:
                absorb(entry["nodes"], source.name)
                continue
            nodes = await self._fetch(source)
            if nodes is None:
                if entry is not None:
                    log.warning("%s: fetch failed, using stale cache", source.name)
                    absorb(entry["nodes"], source.name)
                continue
            cache[source.name] = {"fetched_at": now, "nodes": nodes}
            absorb(nodes, source.name)
        self._write_cache(cache)
        relays = [n for n, _ in merged.values() if n.role in RELAY_ROLES]
        self._nodes = sorted(relays, key=lambda n: n.public_key)
        self._keys = [n.public_key for n in self._nodes]
        self._by_key = {n.public_key: n for n in self._nodes}
        log.info(
            "registry loaded: %d relay nodes from %d sources",
            len(self._nodes),
            len(self._sources),
        )

    async def _fetch(self, source: Source) -> list[Node] | None:
        try:
            async with httpx.AsyncClient(
                headers={"User-Agent": USER_AGENT},
                timeout=FETCH_TIMEOUT,
                follow_redirects=True,
            ) as client:
                resp = await client.get(source.url)
                resp.raise_for_status()
                payload = resp.json()
        except (httpx.HTTPError, ValueError) as ex:
            log.warning("%s: fetch failed: %s", source.name, ex)
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

    def _read_cache(self) -> dict[str, dict[str, Any]]:
        try:
            data = json.loads(self._cache_path.read_text())
            out = {}
            for name, entry in data.items():
                out[name] = {
                    "fetched_at": int(entry["fetched_at"]),
                    "nodes": [Node(**n) for n in entry["nodes"]],
                }
            return out
        except (OSError, ValueError, KeyError, TypeError):
            return {}

    def _write_cache(self, cache: dict[str, dict[str, Any]]) -> None:
        self._cache_path.parent.mkdir(parents=True, exist_ok=True)
        data = {
            name: {
                "fetched_at": entry["fetched_at"],
                "nodes": [asdict(n) for n in entry["nodes"]],
            }
            for name, entry in cache.items()
        }
        tmp = self._cache_path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data))
        tmp.replace(self._cache_path)

    def lookup_prefix(self, prefix: str) -> list[Node]:
        """All relay nodes whose public key starts with the hex prefix."""
        prefix = prefix.lower()
        lo = bisect.bisect_left(self._keys, prefix)
        hi = lo
        while hi < len(self._keys) and self._keys[hi].startswith(prefix):
            hi += 1
        return [self._by_key[self._keys[i]] for i in range(lo, hi)]


@dataclass
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
