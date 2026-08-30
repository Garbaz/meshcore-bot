"""Resolve incoming message transport codes to flood scope keys.

The firmware attaches a per-packet transport code (HMAC-SHA256 of the scope
key with the packet payload) to scoped flood messages. To reply on the same
scope, we reverse this: try each known region's key and see which produces
the matching transport code.

Region scope keys for ``#hashtag`` regions are deterministic:
``SHA256("#" + code)[:16]``. The community catalog at `meshcore-regions`
provides all known region codes, loaded and cached via :class:`NodeRegistry`.
"""

import hashlib
import hmac
import logging
from typing import Any

from meshcore_bot.registry import NodeRegistry

log = logging.getLogger("meshcore_bot.scope")


def _calc_transport_code(scope_key: bytes, payload_type: int, payload: bytes) -> int:
    """Compute the uint16 transport code the firmware would produce.

    Matches ``TransportKey::calcTransportCode`` in TransportKeyStore.cpp:
    HMAC-SHA256 with the 16-byte scope key over ``[payload_type] + payload``,
    taking the first two bytes as a little-endian uint16 (ESP32 is LE).
    """
    h = hmac.new(scope_key, bytes([payload_type]) + payload, hashlib.sha256)
    code = int.from_bytes(h.digest()[:2], "little")
    if code == 0:
        code = 1
    elif code == 0xFFFF:
        code = 0xFFFE
    return code


class ScopeResolver:
    """Resolves transport codes from RF log entries to scope keys.

    The companion's default scope is checked first (fast path for the common
    case), then all region scope keys from the registry.

    Mirrors the firmware's ``chooseReplyScope`` (RoutingPolicy.h):

    - ``REPLY_SCOPE_REQUEST``   incoming scope resolved, reuse it
    - ``REPLY_SCOPE_NONE``      incoming was unscoped, send unscoped
    - ``REPLY_SCOPE_DEFAULT``   scope unknowable, use the fallback
    """

    def __init__(self, registry: NodeRegistry) -> None:
        self._registry = registry
        self._default_name: str = ""
        self._default_key: bytes = b""
        self._fallback_name: str = ""
        self._fallback_key: bytes = b""

    @property
    def default_name(self) -> str:
        return self._default_name

    @property
    def default_key(self) -> bytes:
        return self._default_key

    @property
    def fallback_name(self) -> str:
        return self._fallback_name

    @property
    def fallback_key(self) -> bytes:
        return self._fallback_key

    def set_default(self, name: str, key: bytes) -> None:
        """Set the companion's default flood scope (checked first on resolve)."""
        self._default_name = name
        self._default_key = key

    def set_fallback(self, name: str, key: bytes) -> None:
        """Set the fallback scope used when the incoming scope is unknowable.

        Corresponds to ``REPLY_SCOPE_DEFAULT`` in the firmware's
        ``chooseReplyScope``.  When no fallback is set, an unknowable scope
        results in an unscoped reply (``REPLY_SCOPE_NONE``).
        """
        self._fallback_name = name
        self._fallback_key = key

    def resolve(self, log_entry: dict[str, Any] | None) -> tuple[str, bytes] | None:
        """Resolve the scope from a ``channels_log`` entry.

        Returns ``(scope_name, scope_key)`` on match, ``("", b"")`` for
        unscoped messages (no transport code), or ``None`` if the scope
        can't be determined (no log entry or no key matches).  The caller
        decides whether ``None`` falls back to ``self._fallback_*`` or to
        unscoped.
        """
        if log_entry is None:
            return None

        tc_hex = log_entry.get("transport_code")
        if not tc_hex:
            return ("", b"")  # unscoped (no transport codes on this packet)

        tc_bytes = bytes.fromhex(tc_hex)
        if len(tc_bytes) < 2:
            return ("", b"")

        expected_code = int.from_bytes(tc_bytes[:2], "little")
        if expected_code == 0:
            return ("", b"")  # unscoped

        payload_type = log_entry.get("payload_type")
        pkt_payload: Any = log_entry.get("pkt_payload")
        if payload_type is None or pkt_payload is None:
            return None

        payload = pkt_payload if isinstance(pkt_payload, bytes) else bytes(pkt_payload)

        # Try default scope first (common case: same region as companion).
        if (
            self._default_key
            and _calc_transport_code(self._default_key, payload_type, payload)
            == expected_code
        ):
            return (self._default_name, self._default_key)

        # Try all region scope keys from the registry.
        for key, name in self._registry.scope_keys.items():
            if key == self._default_key:
                continue
            if _calc_transport_code(key, payload_type, payload) == expected_code:
                return (name, key)

        log.debug("could not resolve transport code %s", tc_hex[:8])
        return None
