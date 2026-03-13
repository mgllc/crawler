"""Tests for GovDOSS KIS⁴/SOA⁴ principles and OODA Loop enforcement."""

from __future__ import annotations

import unittest
from collections import deque
from unittest.mock import MagicMock, patch

from crawler import CrawlResult, Crawler, LinkExtractor


class TestStartUrlValidation(unittest.TestCase):
    """KIS⁴ – Secure: start URL must be validated before crawling begins."""

    def test_valid_https_url(self) -> None:
        crawler = Crawler("https://example.com")
        self.assertEqual(crawler.start_url, "https://example.com")

    def test_valid_http_url(self) -> None:
        crawler = Crawler("http://example.com")
        self.assertEqual(crawler.start_url, "http://example.com")

    def test_invalid_scheme_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Crawler("ftp://example.com")
        self.assertIn("http or https", str(ctx.exception))

    def test_missing_host_raises(self) -> None:
        with self.assertRaises(ValueError) as ctx:
            Crawler("https://")
        self.assertIn("valid host", str(ctx.exception))

    def test_no_scheme_raises(self) -> None:
        with self.assertRaises(ValueError):
            Crawler("example.com/path")


class TestSOA4Authorization(unittest.TestCase):
    """SOA⁴ – Authorization: allow/deny patterns gate URLs before enqueuing."""

    def _crawler(self, **kwargs: object) -> Crawler:
        return Crawler("https://example.com", **kwargs)  # type: ignore[arg-type]

    def test_no_patterns_allows_all(self) -> None:
        c = self._crawler()
        self.assertTrue(c._authorized("https://example.com/page"))

    def test_deny_pattern_blocks_matching_url(self) -> None:
        c = self._crawler(deny_patterns=[r"/private/"])
        self.assertFalse(c._authorized("https://example.com/private/secret"))

    def test_deny_pattern_passes_non_matching_url(self) -> None:
        c = self._crawler(deny_patterns=[r"/private/"])
        self.assertTrue(c._authorized("https://example.com/public/page"))

    def test_allow_pattern_blocks_non_matching_url(self) -> None:
        c = self._crawler(allow_patterns=[r"/docs/"])
        self.assertFalse(c._authorized("https://example.com/blog/post"))

    def test_allow_pattern_passes_matching_url(self) -> None:
        c = self._crawler(allow_patterns=[r"/docs/"])
        self.assertTrue(c._authorized("https://example.com/docs/intro"))

    def test_deny_takes_precedence_over_allow(self) -> None:
        c = self._crawler(deny_patterns=[r"/docs/"], allow_patterns=[r"/docs/"])
        self.assertFalse(c._authorized("https://example.com/docs/intro"))

    def test_multiple_deny_patterns(self) -> None:
        c = self._crawler(deny_patterns=[r"/admin/", r"/private/"])
        self.assertFalse(c._authorized("https://example.com/admin/settings"))
        self.assertFalse(c._authorized("https://example.com/private/data"))
        self.assertTrue(c._authorized("https://example.com/public"))

    def test_invalid_regex_raises_on_construction(self) -> None:
        import re
        with self.assertRaises(re.error):
            self._crawler(deny_patterns=["[invalid"])


class TestOODAOrientPhase(unittest.TestCase):
    """OODA – Orient: normalised links from _observe are passed through."""

    def _crawler(self) -> Crawler:
        return Crawler("https://example.com")

    def test_orient_returns_links_on_success(self) -> None:
        c = self._crawler()
        result = CrawlResult(
            url="https://example.com/",
            status=200,
            links=["https://example.com/a", "https://example.com/b"],
            depth=0,
        )
        self.assertEqual(c._orient(result), result.links)

    def test_orient_returns_empty_on_error_with_no_links(self) -> None:
        c = self._crawler()
        result = CrawlResult(
            url="https://example.com/",
            status=None,
            links=[],
            depth=0,
            error="timeout",
        )
        self.assertEqual(c._orient(result), [])

    def test_orient_returns_links_on_truncation_error(self) -> None:
        """Truncated responses still have links; they should be returned."""
        c = self._crawler()
        result = CrawlResult(
            url="https://example.com/",
            status=200,
            links=["https://example.com/page"],
            depth=0,
            error="response truncated at 2000000 bytes",
        )
        self.assertEqual(c._orient(result), result.links)


class TestOODADecidePhase(unittest.TestCase):
    """OODA – Decide: depth limit and authorization are enforced."""

    def _crawler(self, **kwargs: object) -> Crawler:
        return Crawler("https://example.com", max_depth=2, **kwargs)  # type: ignore[arg-type]

    def test_decide_stops_at_max_depth(self) -> None:
        c = self._crawler()
        links = ["https://example.com/deep"]
        self.assertEqual(list(c._decide(links, depth=2)), [])

    def test_decide_allows_links_below_max_depth(self) -> None:
        c = self._crawler()
        links = ["https://example.com/page"]
        self.assertEqual(list(c._decide(links, depth=1)), links)

    def test_decide_applies_deny_pattern(self) -> None:
        c = self._crawler(deny_patterns=[r"/admin/"])
        links = ["https://example.com/admin/panel", "https://example.com/home"]
        result = list(c._decide(links, depth=0))
        self.assertNotIn("https://example.com/admin/panel", result)
        self.assertIn("https://example.com/home", result)

    def test_decide_applies_allow_pattern(self) -> None:
        c = self._crawler(allow_patterns=[r"/docs/"])
        links = ["https://example.com/docs/guide", "https://example.com/blog"]
        result = list(c._decide(links, depth=0))
        self.assertIn("https://example.com/docs/guide", result)
        self.assertNotIn("https://example.com/blog", result)


class TestOODAActPhase(unittest.TestCase):
    """OODA – Act: approved links are enqueued with incremented depth."""

    def _crawler(self) -> Crawler:
        return Crawler("https://example.com")

    def test_act_enqueues_new_links(self) -> None:
        c = self._crawler()
        queue: deque[tuple[str, int]] = deque()
        visited: set[str] = set()
        queued: set[str] = set()
        links = ["https://example.com/a", "https://example.com/b"]
        c._act(links, depth=0, queue=queue, visited=visited, queued=queued)
        self.assertEqual(len(queue), 2)
        self.assertIn(("https://example.com/a", 1), queue)
        self.assertIn(("https://example.com/b", 1), queue)

    def test_act_skips_already_visited(self) -> None:
        c = self._crawler()
        queue: deque[tuple[str, int]] = deque()
        visited = {"https://example.com/a"}
        queued: set[str] = set()
        c._act(["https://example.com/a"], depth=0, queue=queue, visited=visited, queued=queued)
        self.assertEqual(len(queue), 0)

    def test_act_skips_already_queued(self) -> None:
        c = self._crawler()
        queue: deque[tuple[str, int]] = deque()
        visited: set[str] = set()
        queued = {"https://example.com/a"}
        c._act(["https://example.com/a"], depth=0, queue=queue, visited=visited, queued=queued)
        self.assertEqual(len(queue), 0)


class TestLinkExtractor(unittest.TestCase):
    """Ensure link extraction covers all relevant HTML elements."""

    def test_extracts_anchor_href(self) -> None:
        extractor = LinkExtractor()
        extractor.feed('<a href="/page">link</a>')
        self.assertIn("/page", extractor.links)

    def test_extracts_script_src(self) -> None:
        extractor = LinkExtractor()
        extractor.feed('<script src="/app.js"></script>')
        self.assertIn("/app.js", extractor.links)

    def test_extracts_img_src(self) -> None:
        extractor = LinkExtractor()
        extractor.feed('<img src="/logo.png">')
        self.assertIn("/logo.png", extractor.links)

    def test_ignores_empty_attributes(self) -> None:
        extractor = LinkExtractor()
        extractor.feed('<a href="">empty</a>')
        self.assertEqual(extractor.links, [])


class TestCrawlIntegration(unittest.TestCase):
    """Integration test: full crawl using mocked HTTP responses."""

    def _make_response(self, body: str, status: int = 200) -> MagicMock:
        response = MagicMock()
        response.status = status
        response.headers.get = MagicMock(return_value="text/html; charset=utf-8")
        response.read = MagicMock(return_value=body.encode())
        response.__enter__ = MagicMock(return_value=response)
        response.__exit__ = MagicMock(return_value=False)
        return response

    @patch("crawler.urlopen")
    @patch("crawler.RobotFileParser")
    def test_crawl_respects_deny_pattern(self, mock_rfp_class: MagicMock, mock_urlopen: MagicMock) -> None:
        """SOA⁴ deny pattern blocks matching URLs from being crawled."""
        mock_rfp = MagicMock()
        mock_rfp.can_fetch.return_value = True
        mock_rfp.crawl_delay.return_value = None
        mock_rfp.site_maps.return_value = []
        mock_rfp_class.return_value = mock_rfp

        html = '<html><body><a href="/admin/panel">admin</a><a href="/home">home</a></body></html>'
        mock_urlopen.return_value = self._make_response(html)

        crawler = Crawler(
            "https://example.com",
            max_depth=1,
            max_pages=10,
            deny_patterns=[r"/admin/"],
        )
        results = crawler.crawl()
        crawled_urls = [r.url for r in results]
        self.assertIn("https://example.com", crawled_urls)
        self.assertNotIn("https://example.com/admin/panel", crawled_urls)

    @patch("crawler.urlopen")
    @patch("crawler.RobotFileParser")
    def test_crawl_invalid_start_url(self, mock_rfp_class: MagicMock, mock_urlopen: MagicMock) -> None:
        """KIS⁴ – Secure: invalid start URL raises ValueError before crawling."""
        with self.assertRaises(ValueError):
            Crawler("ftp://example.com")
        mock_urlopen.assert_not_called()


if __name__ == "__main__":
    unittest.main()
