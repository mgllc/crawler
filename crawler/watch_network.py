"""Restricted static HTTPS transport for reviewed Evidence Watch URLs.

This separate profile does not inherit the general crawler's redirects, browser
rendering, discovery, credentials, environment proxies, or arbitrary URL queue.
"""
import http.client
import ipaddress
import re
import socket
import ssl
from dataclasses import dataclass
from urllib.parse import urlsplit


class FetchError(ValueError):
    """Stable, non-sensitive collection failure code."""


@dataclass(frozen=True)
class Response:
    status: int
    content_type: str
    body: bytes


def checked_url(url):
    if not isinstance(url, str) or not 1 <= len(url) <= 2048:
        raise ValueError('invalid-source-url')
    if any(ord(c) < 33 or ord(c) > 126 for c in url) or '\\' in url:
        raise ValueError('invalid-source-url')
    parts = urlsplit(url)
    host = parts.hostname or ''
    if (parts.scheme != 'https' or parts.netloc != host or '.' not in host
            or parts.query or parts.fragment or '?' in url or '#' in url
            or not re.fullmatch(r'[a-z0-9](?:[a-z0-9.-]*[a-z0-9])?', host)
            or len(host) > 253 or any(not label or len(label) > 63
                                     or label.startswith('-') or label.endswith('-')
                                     for label in host.split('.'))):
        raise ValueError('invalid-source-url')
    try:
        ipaddress.ip_address(host)
    except ValueError:
        return parts
    raise ValueError('literal-ip-not-supported')


def public_addresses(host):
    """Resolve once; this first transport deliberately supports IPv4 only."""
    try:
        answers = socket.getaddrinfo(host, 443, socket.AF_INET, socket.SOCK_STREAM)
        addresses = sorted({answer[4][0] for answer in answers})
        if not addresses:
            raise FetchError('dns-no-public-address')
        for value in addresses:
            ip = ipaddress.ip_address(value)
            if (ip.version != 4 or not ip.is_global or ip.is_multicast
                    or ip.is_reserved or ip in ipaddress.ip_network('192.0.0.0/24')
                    or ip in ipaddress.ip_network('192.88.99.0/24')):
                raise FetchError('non-public-destination')
        return addresses
    except (OSError, ValueError) as error:
        if isinstance(error, FetchError):
            raise
        raise FetchError('dns-failed') from None


class PinnedHTTPSConnection(http.client.HTTPSConnection):
    def __init__(self, host, address, timeout):
        context = ssl.create_default_context()
        context.set_alpn_protocols(['http/1.1'])
        super().__init__(host, port=443, timeout=timeout, context=context)
        self.address = address

    def connect(self):
        sock = socket.create_connection((self.address, 443), self.timeout)
        try:
            self.sock = self._context.wrap_socket(sock, server_hostname=self.host)
        except BaseException:
            sock.close()
            raise


class SafeFetcher:
    mode = 'live'

    def __init__(self, allowed_urls):
        self.allowed_urls = frozenset(allowed_urls)
        for url in self.allowed_urls:
            checked_url(url)

    def __call__(self, url, limit):
        if url not in self.allowed_urls:
            raise FetchError('url-not-approved')
        parts = checked_url(url)
        address = public_addresses(parts.hostname)[0]
        conn = PinnedHTTPSConnection(parts.hostname, address, 10)
        try:
            conn.request('GET', parts.path or '/', headers={
                'User-Agent': 'GovDOSSEvidenceWatch/1.0',
                'Accept': 'text/html, text/plain, application/json',
                'Accept-Encoding': 'identity', 'Connection': 'close'})
            response = conn.getresponse()
            if 300 <= response.status < 400:
                raise FetchError('redirect-blocked')
            if response.getheader('Content-Encoding', 'identity').lower() != 'identity':
                raise FetchError('compressed-response-rejected')
            declared = response.getheader('Content-Length')
            if declared is not None and (not declared.isdecimal() or int(declared) > limit):
                raise FetchError('response-too-large')
            body = response.read(limit + 1)
            if len(body) > limit:
                raise FetchError('response-too-large')
            if declared is not None and len(body) != int(declared):
                raise FetchError('incomplete-response')
            return Response(response.status, response.getheader('Content-Type', ''), body)
        except (OSError, http.client.HTTPException):
            raise FetchError('transport-failed') from None
        finally:
            conn.close()
