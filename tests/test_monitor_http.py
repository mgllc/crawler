from __future__ import annotations

import io
import socket
import ssl
import unittest
from unittest.mock import patch

from crawler.monitor_http import FetchError, fetch_public, normalize_source_url


def address(ip: str, family: int = socket.AF_INET) -> tuple:
    endpoint = (ip, 443) if family == socket.AF_INET else (ip, 443, 0, 0)
    return family, socket.SOCK_STREAM, socket.IPPROTO_TCP, "", endpoint


class FakeSocket:
    """A socket boundary; the production HTTP request/response parser stays real."""

    def __init__(self, response: bytes) -> None:
        self.response = response
        self.sent = bytearray()
        self.connected_to = None
        self.timeout = None
        self.closed = False

    def settimeout(self, value: float) -> None:
        self.timeout = value

    def connect(self, endpoint: tuple) -> None:
        self.connected_to = endpoint

    def sendall(self, data: bytes) -> None:
        self.sent.extend(data)

    def makefile(self, mode: str):
        return io.BytesIO(self.response)

    def close(self) -> None:
        self.closed = True


class TestNormalizeSourceUrl(unittest.TestCase):
    def test_normalizes_origin_without_rewriting_resource_identity(self):
        self.assertEqual(
            normalize_source_url(
                "HTTPS://EXAMPLE.COM:443/Path//?b=2&a=1&a=0&x=%2f+%20"
            ),
            "https://example.com/Path//?b=2&a=1&a=0&x=%2f+%20",
        )
        self.assertEqual(
            normalize_source_url("https://EXAMPLE.COM?"), "https://example.com/?"
        )
        self.assertEqual(
            normalize_source_url("https://example.com/dir/"), "https://example.com/dir/"
        )

    def test_normalizes_idn_and_encodes_unicode_path(self):
        self.assertEqual(
            normalize_source_url("https://BÜCHER.example/π?q=λ"),
            "https://xn--bcher-kva.example/%CF%80?q=%CE%BB",
        )

    def test_normalizes_ipv6_authority(self):
        self.assertEqual(
            normalize_source_url(
                "https://[2606:4700:4700:0000:0000:0000:0000:1111]:443/a"
            ),
            "https://[2606:4700:4700::1111]/a",
        )

    def test_rejects_invalid_or_unsafe_url_forms(self):
        invalid = (
            "http://example.com/",
            "ftp://example.com/",
            "https://",
            "https://user:secret@example.com/",
            "https://@example.com/",
            "https://example.com:8443/",
            "https://example.com:0/",
            "https://example.com:/",
            "https://example.com:abc/",
            "https://example.com/#",
            "https://example.com/#fragment",
            "https://example.com/\n",
            "https://example.com/\tpath",
            "https://example.com/\x7f",
            "https://example.com/\x85",
            "https://example.com/a b",
            "https://example.com\\@other.com/",
            "https://example..com/",
            "https://[::1",
            "https://[fe80::1%25eth0]/",
        )
        for url in invalid:
            with self.subTest(url=url), self.assertRaises(ValueError):
                normalize_source_url(url)


class TestPublicHttpsFetch(unittest.TestCase):
    def run_transport(
        self,
        raw: bytes,
        *,
        url="https://EXAMPLE.com:443/a?z=2&z=1",
        resolved=None,
        **kwargs,
    ):
        sock = FakeSocket(raw)
        tls = {}

        def wrap(context, raw_socket, *, server_hostname):
            tls.update(
                host=server_hostname,
                verifies=context.verify_mode,
                checks_hostname=context.check_hostname,
            )
            return raw_socket

        with (
            patch(
                "crawler.monitor_http.socket.getaddrinfo",
                return_value=resolved or [address("93.184.216.34")],
            ) as dns,
            patch(
                "crawler.monitor_http.socket.socket", return_value=sock
            ) as create_socket,
            patch.object(ssl.SSLContext, "wrap_socket", new=wrap),
            patch.dict(
                "os.environ",
                {
                    "HTTPS_PROXY": "http://127.0.0.1:9999",
                    "ALL_PROXY": "http://127.0.0.1:9999",
                },
            ),
        ):
            result = fetch_public(url, **kwargs)
        self.assertEqual(dns.call_count, 1)
        self.assertEqual(create_socket.call_count, 1)
        self.assertTrue(sock.closed)
        return result, sock, tls

    def test_https_uses_pinned_address_and_original_host_with_certificate_checks(self):
        result, sock, tls = self.run_transport(
            b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\nX-MiXeD: yes\r\n\r\nhello",
            headers={"If-None-Match": '"old"'},
            timeout=3.0,
        )
        self.assertEqual((result.status, result.body), (200, b"hello"))
        self.assertEqual(result.headers["x-mixed"], "yes")
        self.assertEqual(sock.connected_to, ("93.184.216.34", 443))
        self.assertEqual(sock.timeout, 3.0)
        self.assertEqual(
            tls,
            {
                "host": "example.com",
                "verifies": ssl.CERT_REQUIRED,
                "checks_hostname": True,
            },
        )
        self.assertIn(b"GET /a?z=2&z=1 HTTP/1.1\r\n", sock.sent)
        self.assertIn(b"Host: example.com\r\n", sock.sent)
        self.assertIn(b'if-none-match: "old"\r\n', sock.sent.lower())
        self.assertNotIn(b"cookie:", sock.sent.lower())
        self.assertNotIn(b"proxy", sock.sent.lower())

    def test_public_ipv6_connection_preserves_the_original_dns_hostname(self):
        result, sock, tls = self.run_transport(
            b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok",
            resolved=[address("2606:4700:4700::1111", socket.AF_INET6)],
        )
        self.assertEqual(result.body, b"ok")
        self.assertEqual(sock.connected_to, ("2606:4700:4700::1111", 443, 0, 0))
        self.assertEqual(tls["host"], "example.com")
        self.assertIn(b"Host: example.com\r\n", sock.sent)

    def test_redirect_is_returned_without_following_it(self):
        result, _, _ = self.run_transport(
            b"HTTP/1.1 302 Found\r\nLocation: http://127.0.0.1/admin\r\nContent-Length: 0\r\n\r\n"
        )
        self.assertEqual(result.status, 302)
        self.assertEqual(result.headers["location"], "http://127.0.0.1/admin")

    def test_304_does_not_read_or_reject_representation_content_length(self):
        result, _, _ = self.run_transport(
            b"HTTP/1.1 304 Not Modified\r\nContent-Length: 99999999\r\nETag: abc\r\n\r\n",
            max_bytes=4,
        )
        self.assertEqual((result.status, result.body), (304, b""))
        self.assertEqual(result.headers["etag"], "abc")

    def test_exact_byte_limit_is_accepted(self):
        result, _, _ = self.run_transport(
            b"HTTP/1.1 200 OK\r\nContent-Length: 4\r\n\r\ndata", max_bytes=4
        )
        self.assertEqual(result.body, b"data")

    def test_unknown_length_body_is_still_capped(self):
        with self.assertRaisesRegex(FetchError, "exceed|limit|large"):
            self.run_transport(
                b"HTTP/1.1 200 OK\r\nConnection: close\r\n\r\n12345", max_bytes=4
            )

    def test_declared_oversize_response_is_rejected(self):
        with self.assertRaisesRegex(FetchError, "exceed|limit|large"):
            self.run_transport(
                b"HTTP/1.1 200 OK\r\nContent-Length: 500\r\n\r\n", max_bytes=4
            )

    def test_short_response_is_not_a_successful_snapshot(self):
        with self.assertRaisesRegex(FetchError, "[Ii]ncomplete|[Tt]runcat"):
            self.run_transport(b"HTTP/1.1 200 OK\r\nContent-Length: 5\r\n\r\nab")

    def test_chunked_response_is_decoded_and_bounded(self):
        raw = b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n3\r\nabc\r\n2\r\nde\r\n0\r\n\r\n"
        result, _, _ = self.run_transport(raw, max_bytes=5)
        self.assertEqual(result.body, b"abcde")
        with self.assertRaises(FetchError):
            self.run_transport(raw, max_bytes=4)

    def test_compressed_response_is_rejected_without_decompression(self):
        with self.assertRaises(FetchError):
            self.run_transport(
                b"HTTP/1.1 200 OK\r\nContent-Encoding: gzip\r\nContent-Length: 2\r\n\r\nxx"
            )

    def test_unsupported_transfer_encoding_is_rejected(self):
        with self.assertRaises(FetchError):
            self.run_transport(
                b"HTTP/1.1 200 OK\r\nTransfer-Encoding: gzip\r\nConnection: close\r\n\r\nxx"
            )

    def test_ambiguous_framing_is_rejected(self):
        invalid = (
            b"Content-Length: 2\r\nContent-Length: 3\r\n",
            b"Content-Length: 2\r\nTransfer-Encoding: chunked\r\n",
            b"Transfer-Encoding: gzip\r\nTransfer-Encoding: chunked\r\n",
            b"Content-Encoding: gzip\r\nContent-Encoding: identity\r\n",
        )
        for framing in invalid:
            with self.subTest(framing=framing), self.assertRaises(FetchError):
                self.run_transport(b"HTTP/1.1 200 OK\r\n" + framing + b"\r\nxx")

    def test_extreme_content_length_is_a_fetch_error(self):
        with self.assertRaises(FetchError):
            self.run_transport(
                b"HTTP/1.1 200 OK\r\nContent-Length: " + b"9" * 5000 + b"\r\n\r\n"
            )

    def test_every_dns_answer_is_checked_before_connecting(self):
        with (
            patch(
                "crawler.monitor_http.socket.getaddrinfo",
                return_value=[address("93.184.216.34"), address("127.0.0.1")],
            ),
            patch("crawler.monitor_http.socket.socket") as create_socket,
        ):
            with self.assertRaises(FetchError):
                fetch_public("https://example.com/")
            create_socket.assert_not_called()

    def test_non_public_dns_addresses_are_rejected(self):
        blocked = (
            ("127.0.0.1", socket.AF_INET),
            ("10.0.0.1", socket.AF_INET),
            ("169.254.169.254", socket.AF_INET),
            ("100.64.0.1", socket.AF_INET),
            ("224.0.0.1", socket.AF_INET),
            ("0.0.0.0", socket.AF_INET),
            ("203.0.113.1", socket.AF_INET),
            ("::1", socket.AF_INET6),
            ("fc00::1", socket.AF_INET6),
            ("fe80::1", socket.AF_INET6),
            ("ff02::1", socket.AF_INET6),
            ("::ffff:127.0.0.1", socket.AF_INET6),
            ("2002:7f00:1::", socket.AF_INET6),
        )
        for ip, family in blocked:
            with (
                self.subTest(ip=ip),
                patch(
                    "crawler.monitor_http.socket.getaddrinfo",
                    return_value=[address(ip, family)],
                ),
                patch("crawler.monitor_http.socket.socket") as create_socket,
            ):
                with self.assertRaises(FetchError):
                    fetch_public("https://example.com/")
                create_socket.assert_not_called()

    def test_dns_failure_and_no_answers_are_fetch_errors(self):
        with (
            patch(
                "crawler.monitor_http.socket.getaddrinfo",
                side_effect=socket.gaierror("lookup failed"),
            ),
            self.assertRaises(FetchError),
        ):
            fetch_public("https://example.com/")
        with (
            patch("crawler.monitor_http.socket.getaddrinfo", return_value=[]),
            self.assertRaises(FetchError),
        ):
            fetch_public("https://example.com/")

    def test_tls_failure_closes_the_socket(self):
        sock = FakeSocket(b"")
        with (
            patch(
                "crawler.monitor_http.socket.getaddrinfo",
                return_value=[address("93.184.216.34")],
            ),
            patch("crawler.monitor_http.socket.socket", return_value=sock),
            patch.object(
                ssl.SSLContext,
                "wrap_socket",
                side_effect=ssl.SSLCertVerificationError("bad certificate"),
            ),
            self.assertRaises(FetchError),
        ):
            fetch_public("https://example.com/")
        self.assertTrue(sock.closed)

    def test_tls_configuration_failure_is_a_fetch_error(self):
        with (
            patch(
                "crawler.monitor_http.socket.getaddrinfo",
                return_value=[address("93.184.216.34")],
            ),
            patch(
                "crawler.monitor_http.ssl.create_default_context",
                side_effect=ssl.SSLError("trust store unavailable"),
            ),
            self.assertRaises(FetchError),
        ):
            fetch_public("https://example.com/")

    def test_connection_timeout_is_a_fetch_error(self):
        sock = FakeSocket(b"")
        with (
            patch(
                "crawler.monitor_http.socket.getaddrinfo",
                return_value=[address("93.184.216.34")],
            ),
            patch("crawler.monitor_http.socket.socket", return_value=sock),
            patch.object(sock, "connect", side_effect=TimeoutError("timed out")),
            self.assertRaises(FetchError),
        ):
            fetch_public("https://example.com/")
        self.assertTrue(sock.closed)

    def test_headers_cannot_replace_host_or_add_credentials_or_inject_lines(self):
        invalid = (
            {"Host": "internal"},
            {"Cookie": "secret"},
            {"Authorization": "secret"},
            {"Proxy-Authorization": "secret"},
            {"X-Test": "ok\r\nInjected: yes"},
            {"Content-Length": "10"},
            {"Accept-Encoding": "gzip"},
        )
        for headers in invalid:
            with (
                self.subTest(headers=list(headers)),
                patch("crawler.monitor_http.socket.getaddrinfo") as dns,
            ):
                with self.assertRaises(ValueError):
                    fetch_public("https://example.com/", headers=headers)
                dns.assert_not_called()

    def test_invalid_limits_fail_before_network_io(self):
        for kwargs in (
            {"timeout": 0},
            {"timeout": float("nan")},
            {"timeout": float("inf")},
            {"max_bytes": 0},
            {"max_bytes": -1},
        ):
            with (
                self.subTest(kwargs=kwargs),
                patch("crawler.monitor_http.socket.getaddrinfo") as dns,
            ):
                with self.assertRaises(ValueError):
                    fetch_public("https://example.com/", **kwargs)
                dns.assert_not_called()


if __name__ == "__main__":
    unittest.main()
