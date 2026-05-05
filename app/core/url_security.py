import asyncio
import ipaddress
import socket
from urllib.parse import urlparse, urlunparse

from app.core.exceptions import ValidationError

_BLOCKED_HOSTS = {"localhost", "metadata.google.internal"}
_BLOCKED_SUFFIXES = (".localhost",)
_BLOCKED_IPS = {
    ipaddress.ip_address("169.254.169.254"),
}


def normalize_crawl_url(url: str) -> str:
    """Normalize and validate the URL shape before any outbound request."""
    parsed = urlparse(url.strip())
    if parsed.scheme not in {"http", "https"}:
        raise ValidationError("crawl url must use http or https")
    if not parsed.hostname:
        raise ValidationError("crawl url must include a hostname")
    if parsed.username or parsed.password:
        raise ValidationError("crawl url must not include credentials")
    if parsed.fragment:
        parsed = parsed._replace(fragment="")

    host = parsed.hostname.lower()
    if host in _BLOCKED_HOSTS or host.endswith(_BLOCKED_SUFFIXES):
        raise ValidationError("crawl url host is not allowed")

    _reject_blocked_ip_literal(host)

    try:
        port = parsed.port
    except ValueError as exc:
        raise ValidationError("crawl url has an invalid port") from exc

    netloc = host
    if port:
        netloc = f"{host}:{port}"
    return urlunparse(
        (
            parsed.scheme.lower(),
            netloc,
            parsed.path or "/",
            "",
            parsed.query,
            "",
        )
    )


async def validate_crawl_destination(url: str) -> str:
    """Reject destinations that can reach local/private infrastructure."""
    normalized = normalize_crawl_url(url)
    hostname = urlparse(normalized).hostname
    assert hostname is not None

    infos = await asyncio.to_thread(
        socket.getaddrinfo,
        hostname,
        None,
        type=socket.SOCK_STREAM,
    )
    addresses = {info[4][0] for info in infos}
    if not addresses:
        raise ValidationError("crawl url host could not be resolved")

    for address in addresses:
        ip = ipaddress.ip_address(address)
        if _is_blocked_ip(ip):
            raise ValidationError("crawl url resolves to a blocked address")

    return normalized


def _reject_blocked_ip_literal(host: str) -> None:
    try:
        ip = ipaddress.ip_address(host)
    except ValueError:
        return
    if _is_blocked_ip(ip):
        raise ValidationError("crawl url host is not allowed")


def _is_blocked_ip(ip: ipaddress.IPv4Address | ipaddress.IPv6Address) -> bool:
    return (
        ip in _BLOCKED_IPS
        or ip.is_private
        or ip.is_loopback
        or ip.is_link_local
        or ip.is_multicast
        or ip.is_reserved
        or ip.is_unspecified
    )
