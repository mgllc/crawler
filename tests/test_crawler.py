from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crawler.agent import (
    build_agent_inventory,
    build_service_graph,
    seed_agent_endpoints,
)
from crawler.core import Crawler
from crawler.extract import canonicalize_url, normalize_link


class FakeHeaders(dict):
    def get(self, key: str, default=None):
        return super().get(key, default)


class FakeResponse:
    def __init__(self, status: int, content_type: str, body: bytes, extra_headers: dict[str, str] | None = None):
        self.status = status
        self.headers = FakeHeaders({"Content-Type": content_type, **(extra_headers or {})})
        self._body = body

    def read(self, limit: int | None = None) -> bytes:
        if limit is None:
            return self._body
        return self._body[:limit]

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        return False


class CrawlerTests(unittest.TestCase):
    def test_normalize_link_filters_non_http(self) -> None:
        self.assertIsNone(normalize_link("https://example.com", "mailto:a@b.com"))
        self.assertIsNone(normalize_link("https://example.com", "ftp://example.com/file"))
        self.assertEqual(
            normalize_link("https://EXAMPLE.com/a", "/b/?utm_source=x#x"),
            "https://example.com/b",
        )

    def test_canonicalization(self) -> None:
        self.assertEqual(
            canonicalize_url("https://Example.com/a/?b=2&utm_source=x&a=1"),
            "https://example.com/a?a=1&b=2",
        )

    def test_crawl_extracts_and_filters_links(self) -> None:
        html = b"<a href='/next'>x</a><a href='https://other.com'>y</a>"

        with patch("crawler.core.urlopen", return_value=FakeResponse(200, "text/html", html)):
            crawler = Crawler(
                "https://example.com",
                max_depth=1,
                max_pages=10,
                same_domain=True,
                respect_robots=False,
            )
            results, _queue, _visited = crawler.crawl()

        self.assertEqual(len(results), 2)
        self.assertEqual(results[0].url, "https://example.com/")
        self.assertIn("https://example.com/next", results[0].links)
        self.assertEqual(
            sorted(result.url for result in results),
            ["https://example.com/", "https://example.com/next"],
        )

    def test_domain_include_exclude_filters(self) -> None:
        html = b"<a href='https://a.example.com/x'></a><a href='https://b.example.com/y'></a>"
        with patch("crawler.core.urlopen", return_value=FakeResponse(200, "text/html", html)):
            crawler = Crawler(
                "https://seed.example.com",
                max_depth=1,
                same_domain=False,
                include_domains=["example.com"],
                exclude_domains=["b.example.com"],
                respect_robots=False,
            )
            results, _queue, _visited = crawler.crawl()

        self.assertIn("https://a.example.com/x", results[0].links)
        self.assertNotIn("https://b.example.com/y", results[0].links)

    def test_trap_detection(self) -> None:
        html = b"<a href='/x?a=1&b=2&c=3&d=4'></a>"
        with patch("crawler.core.urlopen", return_value=FakeResponse(200, "text/html", html)):
            crawler = Crawler(
                "https://example.com",
                same_domain=True,
                max_query_params=2,
                respect_robots=False,
            )
            results, _queue, _visited = crawler.crawl()
        self.assertEqual(results[0].links, [])

    def test_robots_blocked_result(self) -> None:
        with patch("crawler.core.urlopen", return_value=FakeResponse(200, "text/html", b"<a href='/x'>")):
            crawler = Crawler("https://example.com", respect_robots=True)
            with patch.object(crawler.policy, "allowed_by_robots", return_value=False):
                results, _queue, _visited = crawler.crawl()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].error, "blocked by robots.txt")

    def test_retry_then_success(self) -> None:
        html = b"<a href='/ok'>ok</a>"

        calls = {"n": 0}

        def flaky(*args, **kwargs):
            calls["n"] += 1
            if calls["n"] == 1:
                raise OSError("boom")
            return FakeResponse(200, "text/html", html)

        with patch("crawler.core.urlopen", side_effect=flaky):
            crawler = Crawler(
                "https://example.com",
                max_depth=0,
                retries=1,
                retry_backoff=0,
                respect_robots=False,
            )
            results, _queue, _visited = crawler.crawl()

        self.assertEqual(len(results), 1)
        self.assertEqual(results[0].status, 200)
        self.assertEqual(calls["n"], 2)

    def test_conditional_headers(self) -> None:
        seen_headers = []

        def fake_urlopen(request, timeout=10):
            seen_headers.append(dict(request.header_items()))
            return FakeResponse(
                200,
                "text/html",
                b"<a href='/next'></a>",
                extra_headers={"ETag": "\"abc\"", "Last-Modified": "Mon, 01 Jan 2024 00:00:00 GMT"},
            )

        with patch("crawler.core.urlopen", side_effect=fake_urlopen):
            crawler = Crawler("https://example.com", max_depth=0, same_domain=True, respect_robots=False)
            crawler._fetch("https://example.com/", 0)
            crawler._fetch("https://example.com/", 0)

        flattened = {k.lower(): v for k, v in seen_headers[-1].items()}
        self.assertIn("if-none-match", flattened)

    def test_request_budget(self) -> None:
        with patch("crawler.core.urlopen", return_value=FakeResponse(200, "text/html", b"<a href='/a'></a><a href='/b'></a>")):
            crawler = Crawler("https://example.com", request_budget=1, max_depth=2, respect_robots=False)
            results, queue, _visited = crawler.crawl()
        self.assertEqual(len(results), 1)
        self.assertEqual(len(queue), 0)

    def test_state_save_and_load_json_and_sqlite(self) -> None:
        with tempfile.TemporaryDirectory() as tmp_dir:
            state_path = Path(tmp_dir) / "state.json"
            db_path = Path(tmp_dir) / "state.db"
            queue = [("https://example.com/next", 1)]
            visited = {"https://example.com"}
            crawler = Crawler("https://example.com", respect_robots=False)
            with patch("crawler.core.urlopen", return_value=FakeResponse(200, "text/html", b"<a href='/x'></a>")):
                results, _q, _v = crawler.crawl(initial_queue=queue, initial_visited=visited)
            Crawler.save_state(str(state_path), queue, visited, results)
            loaded_queue, loaded_visited, loaded_results = Crawler.load_state(str(state_path))
            Crawler.save_state_sqlite(str(db_path), queue, visited, results)
            db_queue, db_visited, db_results = Crawler.load_state_sqlite(str(db_path))

        self.assertEqual(loaded_queue, queue)
        self.assertEqual(loaded_visited, visited)
        self.assertEqual(len(loaded_results), len(results))
        self.assertEqual(db_queue, queue)
        self.assertEqual(db_visited, visited)
        self.assertEqual(len(db_results), len(results))

    def test_agent_discovery_seed_and_outputs(self) -> None:
        seeds = seed_agent_endpoints("https://example.com")
        self.assertIn("https://example.com/v1/models", seeds)

        with patch(
            "crawler.core.urlopen",
            return_value=FakeResponse(
                200,
                "application/json",
                b'{"data":[{"id":"gpt-4.1"},{"id":"o4-mini"}]}',
            ),
        ):
            crawler = Crawler(
                "https://example.com",
                max_depth=0,
                agent_discovery=True,
                respect_robots=False,
            )
            results, _queue, _visited = crawler.crawl()

        inventory = build_agent_inventory(results)
        graph = build_service_graph(results)
        self.assertGreaterEqual(inventory["total_endpoints"], 1)
        self.assertIn("gpt-4.1", inventory["model_names"])
        self.assertTrue(any(edge["relation"] == "exposes" for edge in graph["edges"]))


if __name__ == "__main__":
    unittest.main()
