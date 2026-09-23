"""Client IP for rate-limiting, with a trust gate on forwarding headers.

Behind a reverse proxy, uvicorn sees the proxy as `request.client.host`, so every
caller would share one rate-limit bucket if we keyed on the peer alone.
Honoring `X-Forwarded-For` / `X-Real-IP` without a gate lets any client
spoof an IP and pick a fresh bucket.

Rule: only trust forwarding headers when the immediate TCP peer is listed
in TRUSTED_PROXIES (comma-separated IPs). Empty/unset = peer IP only —
the safe default for local TestClient and any deployment that has not
explicitly named its proxy.

Traefik v3 (for example) sets `X-Real-IP` to the connecting client (overwriting a
client-supplied value) and appends that client to `X-Forwarded-For`.
We prefer `X-Real-IP` when it is itself not a trusted proxy. If it is
absent (or is a trusted hop) we take the *right-most untrusted*
X-Forwarded-For hop — not X-Forwarded-For[0], which a client can prepend.
If every hop is trusted or unparseable, fall back to the TCP peer.
"""
from __future__ import annotations

import ipaddress
import logging
import os

from starlette.requests import Request

logger = logging.getLogger(__name__)


def _canonical_ip(value: str) -> str | None:
    """Return a canonical IP string, or None if `value` is not an IP.

    Strips an optional :port on IPv4 and bracketed IPv6 (`[::1]:port`).
    """
    value = (value or "").strip()
    if not value:
        return None
    if value.startswith("[") and "]" in value:
        value = value[1:value.index("]")]
    elif value.count(":") == 1:
        host, _, port = value.partition(":")
        if port.isdigit():
            value = host
    try:
        return str(ipaddress.ip_address(value))
    except ValueError:
        return None


def _token(value: str) -> str:
    """Canonical IP if parseable, otherwise the stripped string (e.g. 'testclient')."""
    value = (value or "").strip()
    return _canonical_ip(value) or value


def trusted_proxies() -> set[str]:
    """Parse TRUSTED_PROXIES on every call so tests can monkeypatch the env."""
    raw = os.getenv("TRUSTED_PROXIES", "") or ""
    return {_token(part) for part in raw.split(",") if part.strip()}


def non_ip_trusted_proxy_entries() -> list[str]:
    """TRUSTED_PROXIES tokens that are not IP addresses (e.g. compose hostnames)."""
    raw = os.getenv("TRUSTED_PROXIES", "") or ""
    bad = []
    for part in raw.split(","):
        part = part.strip()
        if part and _canonical_ip(part) is None:
            bad.append(part)
    return bad


def warn_if_non_ip_trusted_proxies() -> None:
    """Log when TRUSTED_PROXIES cannot match a TCP peer IP (silent no-op)."""
    bad = non_ip_trusted_proxy_entries()
    if not bad:
        return
    logger.warning(
        "TRUSTED_PROXIES entries are not IP addresses (%s); header trust "
        "will never match a TCP peer. Use the proxy's docker-network IP, "
        "not a compose service name.",
        ", ".join(bad),
    )


def _xff_client_ip(xff: str, trusted: set[str]) -> str | None:
    """Right-most hop that is not in `trusted`.

    X-Forwarded-For is `client, proxy1, proxy2` — the left-most entry is
    attacker-controlled (spoof risk). Walking from the right skips proxies
    we already trust until we hit the first untrusted hop, which is the
    client the last trusted proxy actually saw.
    """
    hops = [h.strip() for h in xff.split(",") if h.strip()]
    if not hops:
        return None
    for hop in reversed(hops):
        ip = _canonical_ip(hop)
        if ip is None:
            continue
        if ip not in trusted:
            return ip
    return None


def get_client_ip(request: Request) -> str:
    """Immediate peer, or Traefik's forwarded client if that peer is trusted.

    Never logs headers. Falls back to 'unknown' when the ASGI scope has no
    client (matches the previous `request.client.host` call sites).
    """
    peer = request.client.host if request.client else None
    if not peer:
        return "unknown"

    trusted = trusted_proxies()
    if not trusted or _token(peer) not in trusted:
        return peer

    real_ip = _canonical_ip(request.headers.get("x-real-ip", ""))
    if real_ip and real_ip not in trusted:
        return real_ip

    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        forwarded = _xff_client_ip(xff, trusted)
        if forwarded:
            return forwarded

    return peer
