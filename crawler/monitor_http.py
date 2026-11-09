"""Bounded HTTPS reads for the explicit-source monitor.

This transport does not inherit proxy settings, follow redirects, run scripts,
send cookies, or reuse connections. DNS answers are checked before a socket is
opened, and the socket connects to a checked address rather than resolving the
hostname a second time. TLS still authenticates the original hostname.
"""

from __future__ import annotations

import http.client
import ipaddress
import math
import re
import socket
import ssl
import unicodedata
from dataclasses import dataclass
from urllib.parse import quote, urlsplit


@dataclass(frozen=True)
class FetchResponse:
    status: int
    headers: dict[str, str]
    body: bytes


class FetchError(RuntimeError):
    """The source could not be read completely through the public HTTPS boundary."""


_LABEL = re.compile(r"(?!-)[a-z0-9-]{1,63}(?<!-)\Z")
_HEADER_NAME = re.compile(r"[!#$%&'*+.^_`|~0-9A-Za-z-]+\Z")
_FORBIDDEN_HEADERS = {
    "host",
    "cookie",
    "cookie2",
    "authorization",
    "connection",
    "content-length",
    "transfer-encoding",
    "upgrade",
    "te",
    "trailer",
    "expect",
}


def _has_control(value: str) -> bool:
    return any(unicodedata.category(char) in {"Cc", "Cf"} for char in value)


def normalize_source_url(url: str) -> str:
    """Validate an HTTPS/443 URL without sorting its query or removing slashes.

    This is syntax validation only. Public-address checks occur at fetch time, so a
    saved hostname is always checked against its current DNS answers.
    """
    if not isinstance(url, str) or not url:
        raise ValueError("A source URL must be a non-empty string")
    if _has_control(url) or any(char.isspace() for char in url) or "\\" in url:
        raise ValueError(
            "Source URLs cannot contain whitespace, controls, or backslashes"
        )
    if "#" in url:
        raise ValueError("Source URLs cannot contain fragments")
    try:
        parsed = urlsplit(url)
        hostname = parsed.hostname
        port = parsed.port
    except ValueError as exc:
        raise ValueError("Invalid source URL authority") from exc
    if parsed.scheme != "https" or not hostname:
        raise ValueError("Source URLs must use HTTPS and name a host")
    if parsed.username is not None or parsed.password is not None:
        raise ValueError("Source URLs cannot contain credentials")
    if port not in (None, 443) or parsed.netloc.endswith(":"):
        raise ValueError("Source URLs must use port 443")
    if "%" in hostname:
        raise ValueError("Encoded or scoped source hostnames are not supported")

    if ":" in hostname:
        if not re.fullmatch(r"\[[0-9a-fA-F:.]+\](?::[0-9]+)?", parsed.netloc):
            raise ValueError("Invalid IPv6 source authority")
        try:
            hostname = ipaddress.IPv6Address(hostname).compressed
        except ValueError as exc:
            raise ValueError("Invalid IPv6 source host") from exc
        authority = f"[{hostname}]"
    else:
        hostname = hostname.removesuffix(".")
        try:
            hostname = hostname.encode("idna").decode("ascii").lower()
        except UnicodeError as exc:
            raise ValueError("Invalid source hostname") from exc
        if len(hostname) > 253 or not all(
            _LABEL.fullmatch(label) for label in hostname.split(".")
        ):
            raise ValueError("Invalid source hostname")
        authority = hostname

    # Keep existing escapes, duplicate query parameters, their order, and empty
    # query delimiters. Encode only characters that cannot appear literally in
    # the HTTP request target.
    path = quote(parsed.path or "/", safe="/%:@!$&'()*+,;=-._~")
    query = quote(parsed.query, safe="/?%:@!$&'()*+,;=-._~")
    return f"https://{authority}{path}" + (f"?{query}" if "?" in url else "")


def _request_headers(headers: dict[str, str] | None, authority: str) -> dict[str, str]:
    result = {
        "Host": authority,
        "User-Agent": "crawler-source-monitor/1.0",
        "Accept-Encoding": "identity",
        "Connection": "close",
    }
    if headers is None:
        return result
    if not isinstance(headers, dict):
        # Keep the public ValueError contract for invalid request arguments.
        raise ValueError("Request headers must be a string dictionary")  # noqa: TRY004
    for name, value in headers.items():
        if not isinstance(name, str) or not _HEADER_NAME.fullmatch(name):
            raise ValueError("Invalid request header name")
        if not isinstance(value, str) or _has_control(value):
            raise ValueError("Invalid request header value")
        try:
            value.encode("latin-1")
        except UnicodeError as exc:
            raise ValueError(
                "Request header values must be HTTP-compatible text"
            ) from exc
        key = name.lower()
        if key in _FORBIDDEN_HEADERS or key.startswith("proxy-"):
            raise ValueError(f"The source transport controls the {key} header")
        if key == "accept-encoding" and value.strip().lower() != "identity":
            raise ValueError(
                "The source transport accepts only identity content encoding"
            )
        for existing in list(result):
            if existing.lower() == key:
                del result[existing]
        result[name] = value
    return result


def _public_address(hostname: str) -> tuple[int, tuple]:
    try:
        answers = socket.getaddrinfo(
            hostname, 443, type=socket.SOCK_STREAM, proto=socket.IPPROTO_TCP
        )
    except OSError as exc:
        raise FetchError(f"DNS lookup failed: {exc}") from exc
    if not answers:
        raise FetchError("DNS lookup returned no addresses")
    checked: list[tuple[int, tuple]] = []
    for family, _kind, _protocol, _canonical, endpoint in answers:
        if family not in (socket.AF_INET, socket.AF_INET6):
            raise FetchError("DNS lookup returned an unsupported address family")
        try:
            ip = ipaddress.ip_address(endpoint[0])
        except ValueError as exc:
            raise FetchError("DNS lookup returned an invalid address") from exc
        embedded = []
        if isinstance(ip, ipaddress.IPv6Address):
            embedded = [
                part for part in (ip.ipv4_mapped, ip.sixtofour) if part is not None
            ]
            if ip.teredo is not None:
                embedded.extend(ip.teredo)
        if (
            not ip.is_global
            or ip.is_multicast
            or ip.is_reserved
            or ip.is_loopback
            or ip.is_link_local
            or ip.is_unspecified
            or getattr(ip, "is_site_local", False)
            or "%" in str(ip)
            or any(
                not part.is_global or part.is_multicast or part.is_reserved
                for part in embedded
            )
        ):
            raise FetchError("Source host resolves to a non-public address")
        if (family == socket.AF_INET) != (ip.version == 4):
            raise FetchError("DNS lookup returned an inconsistent address family")
        numeric = (str(ip), 443) if ip.version == 4 else (str(ip), 443, 0, 0)
        checked.append((family, numeric))
    # One connection attempt keeps a fetch bounded; prefer IPv4 where available.
    return next((item for item in checked if item[0] == socket.AF_INET), checked[0])


class _PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(
        self, hostname: str, family: int, endpoint: tuple, timeout: float
    ) -> None:
        super().__init__(
            hostname, port=443, timeout=timeout, context=ssl.create_default_context()
        )
        self._family = family
        self._endpoint = endpoint

    def connect(self) -> None:
        raw = socket.socket(self._family, socket.SOCK_STREAM, socket.IPPROTO_TCP)
        try:
            raw.settimeout(self.timeout)
            raw.connect(self._endpoint)
            self.sock = self._context.wrap_socket(raw, server_hostname=self.host)
        except BaseException:
            raw.close()
            raise


def fetch_public(
    url: str,
    *,
    headers: dict[str, str] | None = None,
    timeout: float = 10.0,
    max_bytes: int = 1_000_000,
) -> FetchResponse:
    """Read one public HTTPS resource, returning redirects without following them.

    ``timeout`` bounds socket operations, including the TLS handshake; DNS follows
    the operating system resolver's timeouts. Bodies are not decompressed. Invalid
    arguments raise ValueError; retrieval and response failures raise FetchError.
    """
    normalized = normalize_source_url(url)
    if (
        isinstance(timeout, bool)
        or not isinstance(timeout, (float, int))
        or not math.isfinite(timeout)
        or timeout <= 0
    ):
        raise ValueError("timeout must be a positive finite number")
    if isinstance(max_bytes, bool) or not isinstance(max_bytes, int) or max_bytes <= 0:
        raise ValueError("max_bytes must be a positive integer")
    parsed = urlsplit(normalized)
    request_headers = _request_headers(headers, parsed.netloc)
    family, endpoint = _public_address(parsed.hostname or "")
    target = parsed.path + (f"?{parsed.query}" if "?" in normalized else "")
    connection: _PinnedHTTPSConnection | None = None
    try:
        connection = _PinnedHTTPSConnection(
            parsed.hostname or "", family, endpoint, float(timeout)
        )
        connection.request("GET", target, headers=request_headers)
        with connection.getresponse() as response:
            response_headers = {
                name.lower(): value for name, value in response.getheaders()
            }
            if response.status in (204, 304):
                return FetchResponse(response.status, response_headers, b"")
            if any(
                len(response.headers.get_all(name, [])) > 1
                for name in ("Content-Encoding", "Transfer-Encoding")
            ):
                raise FetchError("Response has ambiguous encoding headers")
            if response_headers.get(
                "content-encoding", "identity"
            ).strip().lower() not in ("", "identity"):
                raise FetchError(
                    "Unsupported response content encoding; expected identity"
                )

            lengths = response.headers.get_all("Content-Length", [])
            expected_length = None
            if lengths:
                if (
                    len(lengths) != 1
                    or not lengths[0].isascii()
                    or not lengths[0].isdigit()
                ):
                    raise FetchError("Invalid or ambiguous response Content-Length")
                try:
                    expected_length = int(lengths[0])
                except ValueError as exc:
                    raise FetchError("Invalid response Content-Length") from exc
                if expected_length > max_bytes:
                    raise FetchError(f"Response exceeds the {max_bytes}-byte limit")
            transfer = response_headers.get("transfer-encoding", "").strip().lower()
            if transfer:
                if transfer != "chunked":
                    raise FetchError("Unsupported response transfer encoding")
                if expected_length is not None:
                    raise FetchError("Response has ambiguous transfer framing")
            body = response.read(max_bytes + 1)
            if len(body) > max_bytes:
                raise FetchError(f"Response exceeds the {max_bytes}-byte limit")
            if expected_length is not None and len(body) != expected_length:
                raise FetchError("Incomplete response body")
            return FetchResponse(response.status, response_headers, body)
    except (OSError, http.client.HTTPException) as exc:
        raise FetchError(f"HTTPS retrieval failed: {exc}") from exc
    finally:
        if connection is not None:
            connection.close()
