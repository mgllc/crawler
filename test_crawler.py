"""Comprehensive tests for crawler.py and self_healing.py.

Tests cover:
- LinkExtractor HTML parsing
- CrawlResult dataclass
- Crawler core logic (depth, domain filtering, retries, robots.txt)
- Self-healing: CircuitBreaker, AdaptiveRetryPolicy, HealthMonitor, HealthCheck
- Integration: Crawler wired to self-healing components
"""

from __future__ import annotations

import time
import unittest
from unittest.mock import MagicMock, call, patch
from urllib.error import HTTPError, URLError

from crawler import CrawlResult, Crawler, LinkExtractor
from self_healing import (
    AdaptiveRetryPolicy,
    CircuitBreaker,
    CircuitState,
    HealthCheck,
    HealthMonitor,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _mock_response(
    body: bytes = b"<html><body></body></html>",
    status: int = 200,
    content_type: str = "text/html",
) -> MagicMock:
    """Return a mock HTTP response usable as a context manager."""
    mock = MagicMock()
    mock.status = status
    mock.headers = MagicMock()
    mock.headers.get = lambda key, default="": {
        "Content-Type": content_type
    }.get(key, default)
    mock.read.return_value = body
    mock.__enter__ = lambda s: s
    mock.__exit__ = MagicMock(return_value=False)
    return mock


def _html(links: list[str]) -> bytes:
    anchors = "".join(f'<a href="{h}">x</a>' for h in links)
    return f"<html><body>{anchors}</body></html>".encode()


# ---------------------------------------------------------------------------
# LinkExtractor
# ---------------------------------------------------------------------------

class TestLinkExtractor(unittest.TestCase):

    def test_extracts_anchor_href(self):
        e = LinkExtractor()
        e.feed('<a href="/page1">text</a>')
        self.assertIn("/page1", e.links)

    def test_extracts_link_href(self):
        e = LinkExtractor()
        e.feed('<link rel="stylesheet" href="/style.css">')
        self.assertIn("/style.css", e.links)

    def test_extracts_script_src(self):
        e = LinkExtractor()
        e.feed('<script src="/app.js"></script>')
        self.assertIn("/app.js", e.links)

    def test_extracts_img_src(self):
        e = LinkExtractor()
        e.feed('<img src="/logo.png">')
        self.assertIn("/logo.png", e.links)

    def test_extracts_iframe_src(self):
        e = LinkExtractor()
        e.feed('<iframe src="/embed"></iframe>')
        self.assertIn("/embed", e.links)

    def test_ignores_tags_without_value(self):
        e = LinkExtractor()
        e.feed('<a href="">empty</a>')
        self.assertEqual(e.links, [])

    def test_multiple_links(self):
        e = LinkExtractor()
        e.feed('<a href="/a">1</a><a href="/b">2</a>')
        self.assertEqual(e.links, ["/a", "/b"])

    def test_case_insensitive_tag(self):
        e = LinkExtractor()
        e.feed('<A HREF="/upper">x</A>')
        self.assertIn("/upper", e.links)


# ---------------------------------------------------------------------------
# CrawlResult
# ---------------------------------------------------------------------------

class TestCrawlResult(unittest.TestCase):

    def test_default_fields(self):
        r = CrawlResult(url="http://x.com", status=200, links=[])
        self.assertIsNone(r.content_type)
        self.assertIsNone(r.elapsed_ms)
        self.assertIsNone(r.bytes)
        self.assertIsNone(r.depth)
        self.assertIsNone(r.error)

    def test_error_result(self):
        r = CrawlResult(url="http://x.com", status=None, links=[], error="timeout")
        self.assertIsNone(r.status)
        self.assertEqual(r.error, "timeout")

    def test_dict_conversion(self):
        r = CrawlResult(url="http://x.com", status=200, links=["/a"])
        d = r.__dict__
        self.assertEqual(d["url"], "http://x.com")
        self.assertEqual(d["links"], ["/a"])


# ---------------------------------------------------------------------------
# Crawler – basic crawl behaviour
# ---------------------------------------------------------------------------

class TestCrawlerBasic(unittest.TestCase):

    def _crawler(self, **kw) -> Crawler:
        defaults = dict(
            start_url="http://example.com",
            max_depth=1,
            max_pages=10,
            respect_robots=False,
            retries=0,
        )
        defaults.update(kw)
        return Crawler(**defaults)

    @patch("crawler.urlopen")
    def test_single_page_crawl(self, mock_open):
        mock_open.return_value = _mock_response(body=_html([]))
        results = self._crawler().crawl()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].url, "http://example.com")
        self.assertEqual(results[0].status, 200)

    @patch("crawler.urlopen")
    def test_follows_links_within_depth(self, mock_open):
        mock_open.side_effect = [
            _mock_response(body=_html(["/page1"])),
            _mock_response(body=_html([])),
        ]
        results = self._crawler(max_depth=1).crawl()
        urls = [r.url for r in results]
        self.assertIn("http://example.com", urls)
        self.assertIn("http://example.com/page1", urls)

    @patch("crawler.urlopen")
    def test_respects_max_depth(self, mock_open):
        # depth-0 page links to /p1; depth-1 page links to /p2 (should NOT be fetched)
        mock_open.side_effect = [
            _mock_response(body=_html(["/p1"])),
            _mock_response(body=_html(["/p2"])),
        ]
        results = self._crawler(max_depth=1).crawl()
        urls = [r.url for r in results]
        self.assertNotIn("http://example.com/p2", urls)

    @patch("crawler.urlopen")
    def test_respects_max_pages(self, mock_open):
        mock_open.side_effect = [
            _mock_response(body=_html(["/p1", "/p2", "/p3"])),
            _mock_response(body=_html([])),
            _mock_response(body=_html([])),
        ]
        results = self._crawler(max_pages=2, max_depth=1).crawl()
        self.assertLessEqual(len(results), 2)

    @patch("crawler.urlopen")
    def test_non_html_content_type(self, mock_open):
        mock_open.return_value = _mock_response(
            body=b"data", content_type="application/json"
        )
        results = self._crawler().crawl()
        self.assertEqual(results[0].links, [])
        self.assertEqual(results[0].content_type, "application/json")

    @patch("crawler.urlopen")
    def test_skips_mailto_and_javascript_links(self, mock_open):
        body = b'<a href="mailto:x@y.com">m</a><a href="javascript:void(0)">j</a>'
        mock_open.return_value = _mock_response(body=body)
        results = self._crawler().crawl()
        self.assertEqual(results[0].links, [])

    @patch("crawler.urlopen")
    def test_deduplicates_visited_urls(self, mock_open):
        # Both pages link to each other – should only visit each once.
        mock_open.side_effect = [
            _mock_response(body=_html(["/page1"])),
            _mock_response(body=_html(["http://example.com"])),
        ]
        results = self._crawler(max_depth=2, max_pages=10).crawl()
        urls = [r.url for r in results]
        self.assertEqual(len(urls), len(set(urls)))

    @patch("crawler.urlopen")
    def test_truncated_response_recorded_as_error(self, mock_open):
        body = b"x" * 10
        mock_open.return_value = _mock_response(body=body)
        results = self._crawler(max_bytes=5).crawl()
        self.assertIsNotNone(results[0].error)
        self.assertIn("truncated", results[0].error)


# ---------------------------------------------------------------------------
# Crawler – domain filtering
# ---------------------------------------------------------------------------

class TestCrawlerDomainFilter(unittest.TestCase):

    def _crawler(self, **kw) -> Crawler:
        defaults = dict(
            start_url="http://example.com",
            max_depth=1,
            max_pages=10,
            same_domain=True,
            respect_robots=False,
            retries=0,
        )
        defaults.update(kw)
        return Crawler(**defaults)

    @patch("crawler.urlopen")
    def test_blocks_external_links(self, mock_open):
        mock_open.return_value = _mock_response(
            body=_html(["http://other.com/page"])
        )
        results = self._crawler().crawl()
        self.assertEqual(len(results), 1)  # only the start URL

    @patch("crawler.urlopen")
    def test_allows_external_with_flag(self, mock_open):
        mock_open.side_effect = [
            _mock_response(body=_html(["http://other.com/page"])),
            _mock_response(body=_html([])),
        ]
        results = self._crawler(same_domain=False).crawl()
        self.assertEqual(len(results), 2)

    @patch("crawler.urlopen")
    def test_blocks_subdomains_by_default(self, mock_open):
        mock_open.return_value = _mock_response(
            body=_html(["http://sub.example.com/page"])
        )
        results = self._crawler().crawl()
        self.assertEqual(len(results), 1)

    @patch("crawler.urlopen")
    def test_allows_subdomains_with_flag(self, mock_open):
        mock_open.side_effect = [
            _mock_response(body=_html(["http://sub.example.com/page"])),
            _mock_response(body=_html([])),
        ]
        results = self._crawler(allow_subdomains=True).crawl()
        self.assertEqual(len(results), 2)


# ---------------------------------------------------------------------------
# Crawler – retry behaviour
# ---------------------------------------------------------------------------

class TestCrawlerRetry(unittest.TestCase):

    @patch("crawler.time.sleep")
    @patch("crawler.urlopen")
    def test_retries_on_network_error(self, mock_open, mock_sleep):
        mock_open.side_effect = [
            URLError("connection refused"),
            _mock_response(body=_html([])),
        ]
        c = Crawler(
            "http://example.com",
            max_depth=0,
            respect_robots=False,
            retries=1,
            retry_backoff=0.0,
        )
        results = c.crawl()
        self.assertEqual(results[0].status, 200)
        self.assertIsNone(results[0].error)

    @patch("crawler.time.sleep")
    @patch("crawler.urlopen")
    def test_returns_error_after_all_retries_exhausted(self, mock_open, mock_sleep):
        mock_open.side_effect = URLError("timeout")
        c = Crawler(
            "http://example.com",
            max_depth=0,
            respect_robots=False,
            retries=2,
            retry_backoff=0.0,
        )
        results = c.crawl()
        self.assertIsNone(results[0].status)
        self.assertIsNotNone(results[0].error)
        self.assertEqual(mock_open.call_count, 3)  # 1 initial + 2 retries

    @patch("crawler.time.sleep")
    @patch("crawler.urlopen")
    def test_zero_retries_does_not_retry(self, mock_open, mock_sleep):
        mock_open.side_effect = URLError("refused")
        c = Crawler(
            "http://example.com",
            max_depth=0,
            respect_robots=False,
            retries=0,
        )
        results = c.crawl()
        self.assertEqual(mock_open.call_count, 1)
        self.assertIsNotNone(results[0].error)


# ---------------------------------------------------------------------------
# Crawler – robots.txt compliance
# ---------------------------------------------------------------------------

class TestCrawlerRobots(unittest.TestCase):

    def _disallow_parser(self) -> MagicMock:
        parser = MagicMock()
        parser.can_fetch.return_value = False
        parser.crawl_delay.return_value = None
        parser.site_maps.return_value = []
        return parser

    def _allow_parser(self) -> MagicMock:
        parser = MagicMock()
        parser.can_fetch.return_value = True
        parser.crawl_delay.return_value = None
        parser.site_maps.return_value = []
        return parser

    def test_blocked_by_robots_returns_error_result(self):
        c = Crawler(
            "http://example.com",
            max_depth=0,
            respect_robots=True,
            retries=0,
        )
        with patch.object(c, "_robots_parser", return_value=self._disallow_parser()):
            results = c.crawl()
        self.assertEqual(results[0].error, "blocked by robots.txt")
        self.assertIsNone(results[0].status)

    @patch("crawler.urlopen")
    def test_allowed_by_robots_proceeds(self, mock_open):
        mock_open.return_value = _mock_response(body=_html([]))
        c = Crawler(
            "http://example.com",
            max_depth=0,
            respect_robots=True,
            retries=0,
        )
        with patch.object(c, "_robots_parser", return_value=self._allow_parser()):
            results = c.crawl()
        self.assertEqual(results[0].status, 200)


# ---------------------------------------------------------------------------
# CircuitBreaker
# ---------------------------------------------------------------------------

class TestCircuitBreaker(unittest.TestCase):

    def test_starts_closed(self):
        cb = CircuitBreaker()
        self.assertFalse(cb.is_open("example.com"))
        self.assertEqual(cb.get_state("example.com"), CircuitState.CLOSED)

    def test_opens_after_threshold_failures(self):
        cb = CircuitBreaker(failure_threshold=3)
        for _ in range(3):
            cb.record_failure("example.com")
        self.assertTrue(cb.is_open("example.com"))
        self.assertEqual(cb.get_state("example.com"), CircuitState.OPEN)

    def test_does_not_open_before_threshold(self):
        cb = CircuitBreaker(failure_threshold=5)
        for _ in range(4):
            cb.record_failure("example.com")
        self.assertFalse(cb.is_open("example.com"))

    def test_transitions_to_half_open_after_timeout(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.05)
        cb.record_failure("example.com")
        self.assertTrue(cb.is_open("example.com"))
        time.sleep(0.1)
        self.assertFalse(cb.is_open("example.com"))  # transitions to HALF_OPEN
        self.assertEqual(cb.get_state("example.com"), CircuitState.HALF_OPEN)

    def test_success_closes_circuit(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure("example.com")
        cb.record_failure("example.com")
        self.assertTrue(cb.is_open("example.com"))
        # simulate recovery timeout
        cb._opened_at["example.com"] = time.monotonic() - cb.recovery_timeout - 1
        cb.is_open("example.com")  # triggers HALF_OPEN transition
        cb.record_success("example.com")
        self.assertFalse(cb.is_open("example.com"))
        self.assertEqual(cb.get_state("example.com"), CircuitState.CLOSED)

    def test_failure_in_half_open_reopens_circuit(self):
        cb = CircuitBreaker(failure_threshold=1, recovery_timeout=0.0)
        cb.record_failure("example.com")
        cb._opened_at["example.com"] = 0.0
        cb.is_open("example.com")  # → HALF_OPEN
        cb.record_failure("example.com")
        self.assertEqual(cb.get_state("example.com"), CircuitState.OPEN)

    def test_independent_circuits_per_domain(self):
        cb = CircuitBreaker(failure_threshold=2)
        cb.record_failure("a.com")
        cb.record_failure("a.com")
        self.assertTrue(cb.is_open("a.com"))
        self.assertFalse(cb.is_open("b.com"))

    def test_thread_safety(self):
        """Concurrent failures must not corrupt internal state."""
        import threading
        cb = CircuitBreaker(failure_threshold=100)
        errors: list[Exception] = []

        def hammer():
            try:
                for _ in range(20):
                    cb.record_failure("shared.com")
                    cb.is_open("shared.com")
            except Exception as exc:
                errors.append(exc)

        threads = [threading.Thread(target=hammer) for _ in range(5)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        self.assertEqual(errors, [])


# ---------------------------------------------------------------------------
# AdaptiveRetryPolicy
# ---------------------------------------------------------------------------

class TestAdaptiveRetryPolicy(unittest.TestCase):

    def test_should_retry_network_error(self):
        policy = AdaptiveRetryPolicy(max_retries=3)
        self.assertTrue(policy.should_retry(1, None, URLError("err")))

    def test_should_not_retry_beyond_max(self):
        policy = AdaptiveRetryPolicy(max_retries=3)
        self.assertFalse(policy.should_retry(4, None, URLError("err")))

    def test_retries_429(self):
        policy = AdaptiveRetryPolicy(max_retries=3)
        self.assertTrue(policy.should_retry(1, 429, None))

    def test_retries_5xx(self):
        policy = AdaptiveRetryPolicy(max_retries=3)
        for code in (500, 502, 503, 504):
            with self.subTest(code=code):
                self.assertTrue(policy.should_retry(1, code, None))

    def test_does_not_retry_404(self):
        policy = AdaptiveRetryPolicy(max_retries=3)
        self.assertFalse(policy.should_retry(1, 404, None))

    def test_does_not_retry_non_retryable_4xx(self):
        policy = AdaptiveRetryPolicy(max_retries=3)
        for code in (400, 401, 403, 410):
            with self.subTest(code=code):
                self.assertFalse(policy.should_retry(1, code, None))

    def test_backoff_increases_with_attempt(self):
        policy = AdaptiveRetryPolicy(max_retries=5, base_backoff=1.0, jitter=False)
        b1 = policy.get_backoff(1, None)
        b2 = policy.get_backoff(2, None)
        self.assertGreater(b2, b1)

    def test_rate_limit_backoff_higher_than_base(self):
        policy = AdaptiveRetryPolicy(
            base_backoff=1.0, rate_limit_backoff=5.0, jitter=False
        )
        base = policy.get_backoff(1, None)
        rate_limit = policy.get_backoff(1, 429)
        self.assertGreater(rate_limit, base)

    def test_backoff_capped_at_max(self):
        policy = AdaptiveRetryPolicy(
            base_backoff=1.0, max_backoff=10.0, jitter=False
        )
        self.assertLessEqual(policy.get_backoff(100, None), 10.0)

    def test_jitter_adds_randomness(self):
        """With jitter enabled, two calls should rarely return identical values."""
        policy = AdaptiveRetryPolicy(base_backoff=10.0, jitter=True)
        vals = {policy.get_backoff(1, None) for _ in range(20)}
        self.assertGreater(len(vals), 1)


# ---------------------------------------------------------------------------
# HealthMonitor
# ---------------------------------------------------------------------------

class TestHealthMonitor(unittest.TestCase):

    def test_records_successful_request(self):
        m = HealthMonitor()
        m.record_request("example.com", success=True, status=200, elapsed_ms=50)
        status = m.get_health_status()
        self.assertEqual(status["total_requests"], 1)
        self.assertEqual(status["total_failures"], 0)
        self.assertTrue(status["healthy"])

    def test_records_failed_request(self):
        m = HealthMonitor()
        m.record_request("example.com", success=False)
        status = m.get_health_status()
        self.assertEqual(status["total_failures"], 1)

    def test_high_failure_rate_triggers_alert(self):
        m = HealthMonitor(failure_rate_threshold=0.5, window_size=10)
        for _ in range(10):
            m.record_request("example.com", success=False)
        status = m.get_health_status()
        self.assertFalse(status["healthy"])
        self.assertTrue(len(status["alerts"]) > 0)

    def test_consecutive_failures_trigger_alert(self):
        m = HealthMonitor()
        for _ in range(3):
            m.record_request("bad.com", success=False)
        status = m.get_health_status()
        alerts = status["alerts"]
        self.assertTrue(any(a.startswith("Domain bad.com") for a in alerts))

    def test_clear_alerts_resets_state(self):
        m = HealthMonitor()
        for _ in range(3):
            m.record_request("bad.com", success=False)
        m.clear_alerts()
        status = m.get_health_status()
        self.assertEqual(status["alerts"], [])

    def test_domain_stats_tracked_separately(self):
        m = HealthMonitor()
        m.record_request("a.com", success=True)
        m.record_request("b.com", success=False)
        status = m.get_health_status()
        self.assertEqual(status["domain_stats"]["a.com"]["requests"], 1)
        self.assertEqual(status["domain_stats"]["b.com"]["failures"], 1)

    def test_uptime_is_positive(self):
        m = HealthMonitor()
        status = m.get_health_status()
        self.assertGreaterEqual(status["uptime_seconds"], 0)

    def test_healthy_after_only_successes(self):
        m = HealthMonitor(failure_rate_threshold=0.5, window_size=5)
        for _ in range(5):
            m.record_request("ok.com", success=True)
        self.assertTrue(m.get_health_status()["healthy"])


# ---------------------------------------------------------------------------
# HealthCheck
# ---------------------------------------------------------------------------

class TestHealthCheck(unittest.TestCase):

    def test_healthy_status(self):
        monitor = HealthMonitor()
        cb = CircuitBreaker()
        monitor.record_request("example.com", success=True)
        hc = HealthCheck(monitor, cb)
        result = hc.run()
        self.assertEqual(result["status"], "healthy")
        self.assertIn("details", result)

    def test_degraded_status_on_failures(self):
        monitor = HealthMonitor(failure_rate_threshold=0.5, window_size=4)
        cb = CircuitBreaker()
        for _ in range(4):
            monitor.record_request("bad.com", success=False)
        hc = HealthCheck(monitor, cb)
        result = hc.run()
        self.assertEqual(result["status"], "degraded")

    def test_details_contain_expected_keys(self):
        monitor = HealthMonitor()
        hc = HealthCheck(monitor, CircuitBreaker())
        result = hc.run()
        details = result["details"]
        for key in ("healthy", "total_requests", "total_failures", "alerts"):
            self.assertIn(key, details)


# ---------------------------------------------------------------------------
# Integration: Crawler + Self-Healing
# ---------------------------------------------------------------------------

class TestCrawlerWithSelfHealing(unittest.TestCase):

    def _make_crawler(self, **kw) -> tuple[Crawler, HealthMonitor, CircuitBreaker]:
        monitor = HealthMonitor()
        cb = CircuitBreaker(failure_threshold=5)
        policy = AdaptiveRetryPolicy(max_retries=0, jitter=False)
        crawler = Crawler(
            start_url="http://example.com",
            max_depth=0,
            respect_robots=False,
            retries=0,
            health_monitor=monitor,
            circuit_breaker=cb,
            retry_policy=policy,
            **kw,
        )
        return crawler, monitor, cb

    @patch("crawler.urlopen")
    def test_successful_crawl_records_in_monitor(self, mock_open):
        mock_open.return_value = _mock_response(body=_html([]))
        crawler, monitor, _ = self._make_crawler()
        crawler.crawl()
        status = monitor.get_health_status()
        self.assertEqual(status["total_requests"], 1)
        self.assertEqual(status["total_failures"], 0)

    @patch("crawler.time.sleep")
    @patch("crawler.urlopen")
    def test_failed_crawl_records_failure_in_monitor(self, mock_open, mock_sleep):
        mock_open.side_effect = URLError("refused")
        crawler, monitor, _ = self._make_crawler()
        crawler.crawl()
        status = monitor.get_health_status()
        self.assertEqual(status["total_failures"], 1)

    @patch("crawler.urlopen")
    def test_circuit_breaker_blocks_open_domain(self, mock_open):
        _, _, cb = self._make_crawler()
        # Open the circuit manually.
        for _ in range(5):
            cb.record_failure("example.com")
        crawler = Crawler(
            start_url="http://example.com",
            max_depth=0,
            respect_robots=False,
            circuit_breaker=cb,
        )
        results = crawler.crawl()
        mock_open.assert_not_called()
        self.assertEqual(results[0].error, "circuit breaker open")

    @patch("crawler.time.sleep")
    @patch("crawler.urlopen")
    def test_adaptive_policy_skips_retry_on_404(self, mock_open, mock_sleep):
        """AdaptiveRetryPolicy should NOT retry a 404 error."""
        http_error = HTTPError(
            url="http://example.com", code=404, msg="Not Found", hdrs=MagicMock(), fp=None
        )
        mock_open.side_effect = http_error
        policy = AdaptiveRetryPolicy(max_retries=3, jitter=False)
        crawler = Crawler(
            start_url="http://example.com",
            max_depth=0,
            respect_robots=False,
            retry_policy=policy,
        )
        results = crawler.crawl()
        # Should only have attempted once (no retry for 404).
        self.assertEqual(mock_open.call_count, 1)
        self.assertIsNotNone(results[0].error)

    @patch("crawler.time.sleep")
    @patch("crawler.urlopen")
    def test_adaptive_policy_retries_on_503(self, mock_open, mock_sleep):
        """AdaptiveRetryPolicy should retry 503 errors."""
        http_error = HTTPError(
            url="http://example.com", code=503, msg="Service Unavailable",
            hdrs=MagicMock(), fp=None,
        )
        mock_open.side_effect = [
            http_error,
            _mock_response(body=_html([])),
        ]
        policy = AdaptiveRetryPolicy(max_retries=2, base_backoff=0.0, jitter=False)
        crawler = Crawler(
            start_url="http://example.com",
            max_depth=0,
            respect_robots=False,
            retry_policy=policy,
        )
        results = crawler.crawl()
        self.assertEqual(mock_open.call_count, 2)
        self.assertEqual(results[0].status, 200)

    @patch("crawler.urlopen")
    def test_health_check_integration(self, mock_open):
        mock_open.return_value = _mock_response(body=_html([]))
        monitor = HealthMonitor()
        cb = CircuitBreaker()
        crawler = Crawler(
            start_url="http://example.com",
            max_depth=0,
            respect_robots=False,
            health_monitor=monitor,
            circuit_breaker=cb,
        )
        crawler.crawl()
        hc = HealthCheck(monitor, cb)
        result = hc.run()
        self.assertEqual(result["status"], "healthy")
        self.assertEqual(result["details"]["total_requests"], 1)


# ---------------------------------------------------------------------------
# Scalability / edge-case scenarios
# ---------------------------------------------------------------------------

class TestCrawlerScalability(unittest.TestCase):

    @patch("crawler.urlopen")
    def test_many_pages_within_limit(self, mock_open):
        """Crawler should handle a large page limit without errors."""
        pages = 50
        responses = [_mock_response(body=_html([]))] * pages
        mock_open.side_effect = responses
        c = Crawler(
            "http://example.com",
            max_depth=0,
            max_pages=pages,
            respect_robots=False,
            retries=0,
        )
        results = c.crawl()
        self.assertEqual(len(results), 1)  # only start URL (depth 0)

    @patch("crawler.urlopen")
    def test_normalizes_relative_and_absolute_links(self, mock_open):
        body = b'<a href="/rel">r</a><a href="http://example.com/abs">a</a>'
        mock_open.side_effect = [
            _mock_response(body=body),
            _mock_response(body=_html([])),
            _mock_response(body=_html([])),
        ]
        c = Crawler(
            "http://example.com",
            max_depth=1,
            max_pages=10,
            respect_robots=False,
            retries=0,
        )
        results = c.crawl()
        urls = [r.url for r in results]
        self.assertIn("http://example.com/rel", urls)
        self.assertIn("http://example.com/abs", urls)


if __name__ == "__main__":
    unittest.main()
