"""Client IP for rate-limiting, with a trust gate on forwarding headers.

Behind a reverse proxy, uvicorn sees the proxy as `request.client.host`, so every
caller would share one rate-limit bucket if we keyed on the peer alone.
Honoring `X-Forwarded-For` / `X-Real-IP` without a gate lets any client
spoof an IP and pick a fresh bucket.

Rule: only trust forwarding headers when the immediate TCP peer is listed
in TRUSTED_PROXIES (comma-separated IPs, CIDRs, or hostnames). Empty/unset
= peer IP only — the safe default for local TestClient and any deployment
that has not explicitly named its proxy.

CIDRs cover a compose network without discovering a container IP by hand
(e.g. `172.18.0.0/16`). Hostnames are resolved at parse time (startup and
whenever the env value changes).

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
import socket

from starlette.requests import Request

logger = logging.getLogger(__name__)

# Cache keyed on the raw env string so tests can monkeypatch TRUSTED_PROXIES
# and get a fresh parse, without a DNS lookup on every request.
_trusted_cache_key: str | None = None
_trusted_cache: tuple[set[str], list] | None = None


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


def _resolve_hostname(host: str) -> list[str]:
    ips: list[str] = []
    try:
        infos = socket.getaddrinfo(host, None)
    except OSError:
        return ips
    for info in infos:
        sockaddr = info[4]
        if not sockaddr:
            continue
        ip = _canonical_ip(sockaddr[0])
        if ip:
            ips.append(ip)
    return ips


def _parse_trusted_proxies(raw: str) -> tuple[set[str], list]:
    literals: set[str] = set()
    networks: list[ipaddress._BaseNetwork] = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            try:
                networks.append(ipaddress.ip_network(part, strict=False))
                continue
            except ValueError:
                logger.warning("TRUSTED_PROXIES entry is not a valid CIDR: %s", part)
                continue
        ip = _canonical_ip(part)
        if ip:
            literals.add(ip)
            continue
        # Hostname (or TestClient peer name): keep the token so an exact
        # peer match still works if DNS fails, and add resolved A/AAAA.
        literals.add(_token(part))
        literals.update(_resolve_hostname(part))
    return literals, networks


def _trusted_sets() -> tuple[set[str], list]:
    """Parse TRUSTED_PROXIES, cached while the env value is unchanged."""
    global _trusted_cache_key, _trusted_cache
    raw = os.getenv("TRUSTED_PROXIES", "") or ""
    if _trusted_cache is not None and raw == _trusted_cache_key:
        return _trusted_cache
    parsed = _parse_trusted_proxies(raw)
    _trusted_cache_key = raw
    _trusted_cache = parsed
    return parsed


def reset_trusted_proxies_cache() -> None:
    """Drop the parse/DNS cache. Tests that mock DNS should call this."""
    global _trusted_cache_key, _trusted_cache
    _trusted_cache_key = None
    _trusted_cache = None


def trusted_proxies() -> set[str]:
    """Literal IPs and hostname tokens. Kept for callers/tests that expect a set."""
    literals, _networks = _trusted_sets()
    return set(literals)


def _is_trusted(value: str | None) -> bool:
    if not value:
        return False
    literals, networks = _trusted_sets()
    token = _token(value)
    if token in literals:
        return True
    ip = _canonical_ip(value)
    if ip is None:
        return False
    if ip in literals:
        return True
    try:
        addr = ipaddress.ip_address(ip)
    except ValueError:
        return False
    return any(addr in network for network in networks)


def non_ip_trusted_proxy_entries() -> list[str]:
    """TRUSTED_PROXIES tokens that are not IPs or CIDRs (hostnames)."""
    raw = os.getenv("TRUSTED_PROXIES", "") or ""
    bad = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            try:
                ipaddress.ip_network(part, strict=False)
                continue
            except ValueError:
                bad.append(part)
                continue
        if _canonical_ip(part) is None:
            bad.append(part)
    return bad


def warn_if_non_ip_trusted_proxies() -> None:
    """Resolve hostnames and log any TRUSTED_PROXIES entry that cannot match."""
    reset_trusted_proxies_cache()
    raw = os.getenv("TRUSTED_PROXIES", "") or ""
    if not raw.strip():
        return
    unresolved = []
    for part in raw.split(","):
        part = part.strip()
        if not part:
            continue
        if "/" in part:
            try:
                ipaddress.ip_network(part, strict=False)
            except ValueError:
                unresolved.append(part)
            continue
        if _canonical_ip(part):
            continue
        if not _resolve_hostname(part):
            unresolved.append(part)
    if not unresolved:
        return
    logger.warning(
        "TRUSTED_PROXIES hostnames did not resolve (%s); they will only "
        "match a TCP peer with that exact name. Prefer a CIDR such as "
        "172.18.0.0/16 for the compose network.",
        ", ".join(unresolved),
    )


def _xff_client_ip(xff: str) -> str | None:
    """Right-most hop that is not trusted.

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
        if not _is_trusted(ip):
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

    literals, networks = _trusted_sets()
    if not literals and not networks:
        return peer
    if not _is_trusted(peer):
        return peer

    real_ip = _canonical_ip(request.headers.get("x-real-ip", ""))
    if real_ip and not _is_trusted(real_ip):
        return real_ip

    xff = request.headers.get("x-forwarded-for", "")
    if xff:
        forwarded = _xff_client_ip(xff)
        if forwarded:
            return forwarded

    return peer
