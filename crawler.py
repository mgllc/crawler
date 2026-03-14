#!/usr/bin/env python3
"""Simple website crawler with depth and domain limits."""

from __future__ import annotations

import argparse
import json
import sys
import time
from collections import deque
from dataclasses import dataclass
from html.parser import HTMLParser
from typing import Iterable
from urllib.error import HTTPError, URLError
from urllib.parse import urldefrag, urljoin, urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from self_healing import AdaptiveRetryPolicy, CircuitBreaker, HealthMonitor


class LinkExtractor(HTMLParser):
    def __init__(self) -> None:
        super().__init__()
        self.links: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        tag_lower = tag.lower()
        for key, value in attrs:
            if not value:
                continue
            key_lower = key.lower()
            if tag_lower in {"a", "link"} and key_lower == "href":
                self.links.append(value)
            if tag_lower in {"script", "img", "iframe"} and key_lower == "src":
                self.links.append(value)


@dataclass
class CrawlResult:
    url: str
    status: int | None
    links: list[str]
    content_type: str | None = None
    elapsed_ms: int | None = None
    bytes: int | None = None
    depth: int | None = None
    error: str | None = None


class Crawler:
    def __init__(
        self,
        start_url: str,
        max_depth: int = 2,
        max_pages: int = 100,
        same_domain: bool = True,
        allow_subdomains: bool = False,
        user_agent: str = "crawler/1.0",
        timeout: int = 10,
        max_bytes: int = 2_000_000,
        respect_robots: bool = True,
        crawl_delay: float = 0.0,
        include_sitemap: bool = False,
        retries: int = 1,
        retry_backoff: float = 0.5,
        health_monitor: HealthMonitor | None = None,
        circuit_breaker: CircuitBreaker | None = None,
        retry_policy: AdaptiveRetryPolicy | None = None,
    ) -> None:
        self.start_url = start_url
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.same_domain = same_domain
        self.allow_subdomains = allow_subdomains
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.respect_robots = respect_robots
        self.crawl_delay = crawl_delay
        self.include_sitemap = include_sitemap
        self.retries = retries
        self.retry_backoff = retry_backoff
        self._health_monitor = health_monitor
        self._circuit_breaker = circuit_breaker
        self._retry_policy = retry_policy
        parsed = urlparse(start_url)
        self.origin = parsed.netloc
        self.scheme = parsed.scheme or "https"
        self._robots_cache: dict[str, RobotFileParser] = {}
        self._last_request: dict[str, float] = {}
        self._sitemap_links: set[str] = set()

    def crawl(self) -> list[CrawlResult]:
        visited: set[str] = set()
        results: list[CrawlResult] = []
        queued: set[str] = {self.start_url}
        queue: deque[tuple[str, int]] = deque([(self.start_url, 0)])
        if self.include_sitemap:
            self._robots_parser(self.start_url)
            self._enqueue_new_sitemap_links(queue, visited, queued)

        while queue and len(visited) < self.max_pages:
            url, depth = queue.popleft()
            if url in visited or depth > self.max_depth:
                continue
            visited.add(url)
            sitemap_count = len(self._sitemap_links)
            if self.respect_robots and not self._allowed_by_robots(url):
                if self.include_sitemap and len(self._sitemap_links) > sitemap_count:
                    self._enqueue_new_sitemap_links(queue, visited, queued)
                results.append(
                    CrawlResult(
                        url=url,
                        status=None,
                        links=[],
                        depth=depth,
                        error="blocked by robots.txt",
                    )
                )
                continue

            if self.include_sitemap and len(self._sitemap_links) > sitemap_count:
                self._enqueue_new_sitemap_links(queue, visited, queued)

            result = self._fetch(url, depth)
            results.append(result)

            if depth == self.max_depth:
                continue
            for link in self._filter_links(result.links):
                if link not in visited and link not in queued:
                    queue.append((link, depth + 1))
                    queued.add(link)

        return results

    def _fetch(self, url: str, depth: int) -> CrawlResult:
        domain = urlparse(url).netloc

        if self._circuit_breaker is not None and self._circuit_breaker.is_open(domain):
            return CrawlResult(
                url=url,
                status=None,
                links=[],
                depth=depth,
                error="circuit breaker open",
            )

        attempt = 0
        while True:
            attempt += 1
            try:
                self._respect_crawl_delay(url)
                request = Request(url, headers={"User-Agent": self.user_agent})
                started = time.monotonic()
                with urlopen(request, timeout=self.timeout) as response:
                    status = response.status
                    content_type = response.headers.get("Content-Type", "")
                    if "text/html" not in content_type:
                        elapsed_ms = int((time.monotonic() - started) * 1000)
                        if self._health_monitor is not None:
                            self._health_monitor.record_request(
                                domain,
                                success=True,
                                status=status,
                                elapsed_ms=elapsed_ms,
                            )
                        if self._circuit_breaker is not None:
                            self._circuit_breaker.record_success(domain)
                        return CrawlResult(
                            url=url,
                            status=status,
                            links=[],
                            content_type=content_type,
                            elapsed_ms=elapsed_ms,
                            bytes=None,
                            depth=depth,
                        )
                    body_bytes = response.read(self.max_bytes + 1)
                    truncated = len(body_bytes) > self.max_bytes
                    if truncated:
                        body_bytes = body_bytes[: self.max_bytes]
                    body = body_bytes.decode("utf-8", errors="replace")
                    elapsed_ms = int((time.monotonic() - started) * 1000)
            except Exception as exc:  # noqa: BLE001 - show capture error
                status_code = exc.code if isinstance(exc, HTTPError) else None  # type: ignore[union-attr]
                should_retry = (
                    self._retry_policy.should_retry(attempt, status_code, exc)
                    if self._retry_policy is not None
                    else attempt <= self.retries
                )
                if should_retry:
                    backoff = (
                        self._retry_policy.get_backoff(attempt, status_code)
                        if self._retry_policy is not None
                        else self.retry_backoff * attempt
                    )
                    if self._health_monitor is not None:
                        self._health_monitor.record_request(domain, success=False)
                    if self._circuit_breaker is not None:
                        self._circuit_breaker.record_failure(domain)
                    time.sleep(backoff)
                    continue
                if self._health_monitor is not None:
                    self._health_monitor.record_request(domain, success=False)
                if self._circuit_breaker is not None:
                    self._circuit_breaker.record_failure(domain)
                return CrawlResult(
                    url=url,
                    status=None,
                    links=[],
                    depth=depth,
                    error=str(exc),
                )

            if self._health_monitor is not None:
                self._health_monitor.record_request(
                    domain,
                    success=True,
                    status=status,
                    elapsed_ms=elapsed_ms,
                    bytes_read=len(body_bytes),
                )
            if self._circuit_breaker is not None:
                self._circuit_breaker.record_success(domain)

            extractor = LinkExtractor()
            extractor.feed(body)
            links = [self._normalize_link(url, link) for link in extractor.links]
            if truncated:
                error = f"response truncated at {self.max_bytes} bytes"
            else:
                error = None
            return CrawlResult(
                url=url,
                status=status,
                links=[link for link in links if link],
                content_type=content_type,
                elapsed_ms=elapsed_ms,
                bytes=len(body_bytes),
                depth=depth,
                error=error,
            )

    def _normalize_link(self, base_url: str, link: str) -> str | None:
        if link.startswith("mailto:") or link.startswith("javascript:"):
            return None
        absolute = urljoin(base_url, link)
        normalized, _ = urldefrag(absolute)
        parsed = urlparse(normalized)
        if parsed.scheme not in {"http", "https"}:
            return None
        return normalized

    def _filter_links(self, links: Iterable[str]) -> Iterable[str]:
        for link in links:
            if self.same_domain:
                if self._is_same_domain(link):
                    yield link
                continue
            yield link

    def _is_same_domain(self, link: str) -> bool:
        parsed = urlparse(link)
        if parsed.netloc == self.origin:
            return True
        if self.allow_subdomains and parsed.netloc.endswith(f".{self.origin}"):
            return True
        return False

    def _robots_parser(self, url: str) -> RobotFileParser:
        parsed = urlparse(url)
        netloc = parsed.netloc
        if netloc in self._robots_cache:
            return self._robots_cache[netloc]
        robots_url = f"{parsed.scheme or self.scheme}://{netloc}/robots.txt"
        parser = RobotFileParser()
        parser.set_url(robots_url)
        try:
            parser.read()
        except (URLError, OSError):
            parser.allow_all = True
        self._robots_cache[netloc] = parser
        if self.include_sitemap:
            for sitemap in parser.site_maps() or []:
                self._enqueue_sitemap(sitemap)
        return parser

    def _enqueue_sitemap(self, sitemap_url: str) -> None:
        try:
            request = Request(sitemap_url, headers={"User-Agent": self.user_agent})
            with urlopen(request, timeout=self.timeout) as response:
                if "xml" not in response.headers.get("Content-Type", ""):
                    return
                body = response.read(self.max_bytes).decode("utf-8", errors="replace")
        except Exception:
            return
        for line in body.splitlines():
            line = line.strip()
            if line.startswith("<loc>") and line.endswith("</loc>"):
                url = line.removeprefix("<loc>").removesuffix("</loc>").strip()
                normalized = self._normalize_link(self.start_url, url)
                if normalized:
                    self._sitemap_links.add(normalized)

    def _enqueue_new_sitemap_links(
        self,
        queue: deque[tuple[str, int]],
        visited: set[str],
        queued: set[str],
    ) -> None:
        for link in self._filter_links(sorted(self._sitemap_links)):
            if link not in visited and link not in queued:
                queue.append((link, 0))
                queued.add(link)

    def _allowed_by_robots(self, url: str) -> bool:
        parser = self._robots_parser(url)
        return parser.can_fetch(self.user_agent, url)

    def _respect_crawl_delay(self, url: str) -> None:
        parsed = urlparse(url)
        netloc = parsed.netloc
        parser = self._robots_parser(url) if self.respect_robots else None
        delay = self.crawl_delay
        if parser:
            robots_delay = parser.crawl_delay(self.user_agent)
            if robots_delay:
                delay = max(delay, robots_delay)
        if delay <= 0:
            return
        last = self._last_request.get(netloc)
        if last is not None:
            elapsed = time.monotonic() - last
            if elapsed < delay:
                time.sleep(delay - elapsed)
        self._last_request[netloc] = time.monotonic()


def _parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Crawl and map a website.")
    parser.add_argument("start_url", help="Starting URL to crawl.")
    parser.add_argument("--max-depth", type=int, default=2, help="Max crawl depth.")
    parser.add_argument("--max-pages", type=int, default=100, help="Max pages to crawl.")
    parser.add_argument(
        "--allow-external",
        action="store_true",
        help="Allow crawling external domains.",
    )
    parser.add_argument(
        "--allow-subdomains",
        action="store_true",
        help="Allow subdomains when same-domain restriction is enabled.",
    )
    parser.add_argument("--user-agent", default="crawler/1.0", help="HTTP user agent.")
    parser.add_argument("--timeout", type=int, default=10, help="Request timeout.")
    parser.add_argument(
        "--max-bytes",
        type=int,
        default=2_000_000,
        help="Max bytes to read per HTML response.",
    )
    parser.add_argument(
        "--respect-robots",
        action=argparse.BooleanOptionalAction,
        default=True,
        help="Respect robots.txt rules.",
    )
    parser.add_argument(
        "--crawl-delay",
        type=float,
        default=0.0,
        help="Minimum delay between requests per host (seconds).",
    )
    parser.add_argument(
        "--include-sitemap",
        action="store_true",
        help="Seed crawl from sitemap URLs referenced in robots.txt.",
    )
    parser.add_argument("--retries", type=int, default=1, help="Retry failed requests.")
    parser.add_argument(
        "--retry-backoff",
        type=float,
        default=0.5,
        help="Seconds to back off between retries (multiplied by attempt).",
    )
    parser.add_argument("--output", default="crawl.json", help="Output JSON path.")
    return parser.parse_args()


def main() -> int:
    args = _parse_args()
    crawler = Crawler(
        start_url=args.start_url,
        max_depth=args.max_depth,
        max_pages=args.max_pages,
        same_domain=not args.allow_external,
        allow_subdomains=args.allow_subdomains,
        user_agent=args.user_agent,
        timeout=args.timeout,
        max_bytes=args.max_bytes,
        respect_robots=args.respect_robots,
        crawl_delay=args.crawl_delay,
        include_sitemap=args.include_sitemap,
        retries=args.retries,
        retry_backoff=args.retry_backoff,
    )
    results = crawler.crawl()
    payload = [result.__dict__ for result in results]
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    print(f"Wrote {len(results)} pages to {args.output}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
