from __future__ import annotations

import json
import time
from collections import deque
from concurrent.futures import FIRST_COMPLETED, Future, ThreadPoolExecutor, wait
from pathlib import Path
from urllib.request import Request, urlopen

from crawler.agent import seed_agent_endpoints
from crawler.browser import render_page_html
from crawler.extract import LinkExtractor, canonicalize_url, normalize_link
from crawler.models import CrawlResult
from crawler.policy import CrawlPolicy
from crawler.storage import init_db, load_state_db, save_state_db


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
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        max_workers: int = 4,
        request_budget: int | None = None,
        recrawl_after_seconds: int = 86_400,
        render_js: bool = False,
        max_query_params: int = 15,
        max_path_repeats: int = 4,
        per_host_workers: int = 2,
        agent_discovery: bool = False,
    ) -> None:
        self.start_url = canonicalize_url(start_url)
        self.max_depth = max_depth
        self.max_pages = max_pages
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.retries = retries
        self.retry_backoff = retry_backoff
        self.max_workers = max(1, max_workers)
        self.request_budget = request_budget
        self.recrawl_after_seconds = recrawl_after_seconds
        self.render_js = render_js
        self.agent_discovery = agent_discovery
        self.policy = CrawlPolicy(
            start_url=self.start_url,
            same_domain=same_domain,
            allow_subdomains=allow_subdomains,
            respect_robots=respect_robots,
            crawl_delay=crawl_delay,
            include_sitemap=include_sitemap,
            user_agent=user_agent,
            timeout=timeout,
            max_bytes=max_bytes,
            include_domains=include_domains,
            exclude_domains=exclude_domains,
            max_query_params=max_query_params,
            max_path_repeats=max_path_repeats,
            per_host_workers=per_host_workers,
        )
        self._cache_headers: dict[str, dict[str, str]] = {}
        self.metrics: dict[str, int] = {
            "fetched": 0,
            "errors": 0,
            "blocked_robots": 0,
            "non_html": 0,
            "truncated": 0,
            "cache_conditional_sent": 0,
        }

    def crawl(
        self,
        *,
        initial_queue: list[tuple[str, int]] | None = None,
        initial_visited: set[str] | None = None,
        initial_results: list[CrawlResult] | None = None,
    ) -> tuple[list[CrawlResult], list[tuple[str, int]], set[str]]:
        visited: set[str] = set(initial_visited or set())
        results: list[CrawlResult] = list(initial_results or [])

        if initial_queue:
            queue = deque(initial_queue)
            queued = {url for url, _depth in initial_queue}
        else:
            queue = deque([(self.start_url, 0)])
            queued = {self.start_url}

        self.policy.seed_sitemap_queue(self.start_url, queue, visited, queued)
        if self.agent_discovery:
            for endpoint in seed_agent_endpoints(self.start_url):
                if endpoint not in queued and endpoint not in visited:
                    queue.append((endpoint, 0))
                    queued.add(endpoint)
        budget_left = self.request_budget

        with ThreadPoolExecutor(max_workers=self.max_workers) as executor:
            in_flight: dict[Future[CrawlResult], tuple[str, int]] = {}

            while (queue or in_flight) and len(visited) < self.max_pages:
                while queue and len(in_flight) < self.max_workers and len(visited) < self.max_pages:
                    if budget_left is not None and budget_left <= 0:
                        queue.clear()
                        break
                    url, depth = queue.popleft()
                    if url in visited or depth > self.max_depth:
                        continue
                    visited.add(url)

                    if not self.policy.allowed_by_robots(url):
                        self.metrics["blocked_robots"] += 1
                        results.append(
                            CrawlResult(
                                url=url,
                                status=None,
                                links=[],
                                depth=depth,
                                error="blocked by robots.txt",
                                fetched_at=time.time(),
                                next_crawl_at=time.time() + self.recrawl_after_seconds,
                            )
                        )
                        continue

                    if budget_left is not None:
                        budget_left -= 1

                    fut = executor.submit(self._fetch, url, depth)
                    in_flight[fut] = (url, depth)

                if not in_flight:
                    continue

                done, _ = wait(set(in_flight.keys()), return_when=FIRST_COMPLETED)
                for fut in done:
                    url, depth = in_flight.pop(fut)
                    result = fut.result()
                    result.links = self.policy.filter_links(result.links)
                    results.append(result)
                    self.metrics["fetched"] += 1
                    if result.error:
                        self.metrics["errors"] += 1
                    if result.content_type and "text/html" not in result.content_type:
                        self.metrics["non_html"] += 1
                    if result.error and "truncated" in result.error:
                        self.metrics["truncated"] += 1

                    if depth == self.max_depth:
                        continue
                    for link in result.links:
                        if link not in visited and link not in queued:
                            queue.append((link, depth + 1))
                            queued.add(link)

                self.policy.enqueue_new_sitemap_links(queue, visited, queued)

        return results, list(queue), visited

    @staticmethod
    def load_state(path: str) -> tuple[list[tuple[str, int]], set[str], list[CrawlResult]]:
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)
        queue_payload = payload.get("queue", [])
        visited_payload = payload.get("visited", [])
        results_payload = payload.get("results", [])

        queue = [
            (str(item[0]), int(item[1]))
            for item in queue_payload
            if isinstance(item, list) and len(item) == 2
        ]
        visited = {str(item) for item in visited_payload if isinstance(item, str)}
        results = [
            CrawlResult.from_dict(item)
            for item in results_payload
            if isinstance(item, dict)
        ]
        return queue, visited, results

    @staticmethod
    def save_state(
        path: str,
        queue: list[tuple[str, int]],
        visited: set[str],
        results: list[CrawlResult],
    ) -> None:
        payload = {
            "queue": [[url, depth] for url, depth in queue],
            "visited": sorted(visited),
            "results": [result.to_dict() for result in results],
        }
        out_path = Path(path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        with open(out_path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2, sort_keys=True)

    @staticmethod
    def save_state_sqlite(
        db_path: str,
        queue: list[tuple[str, int]],
        visited: set[str],
        results: list[CrawlResult],
    ) -> None:
        conn = init_db(db_path)
        try:
            save_state_db(conn, queue, visited, results)
        finally:
            conn.close()

    @staticmethod
    def load_state_sqlite(db_path: str) -> tuple[list[tuple[str, int]], set[str], list[CrawlResult]]:
        conn = init_db(db_path)
        try:
            return load_state_db(conn)
        finally:
            conn.close()

    def write_report(self, path: str, results: list[CrawlResult], visited: set[str]) -> None:
        report = {
            "metrics": self.metrics,
            "pages": len(results),
            "visited": len(visited),
            "timestamp": time.time(),
        }
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(report, handle, indent=2, sort_keys=True)

    def _fetch(self, url: str, depth: int) -> CrawlResult:
        attempt = 0
        while True:
            attempt += 1
            try:
                self.policy.acquire_host_slot(url)
                self.policy.respect_delay(url)
                headers = {"User-Agent": self.user_agent}
                prev = self._cache_headers.get(url)
                if prev:
                    if "etag" in prev:
                        headers["If-None-Match"] = prev["etag"]
                    if "last_modified" in prev:
                        headers["If-Modified-Since"] = prev["last_modified"]
                    self.metrics["cache_conditional_sent"] += 1

                request = Request(url, headers=headers)
                started = time.monotonic()

                if self.render_js:
                    body = render_page_html(url, timeout_ms=self.timeout * 1000)
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    extractor = LinkExtractor()
                    extractor.feed(body)
                    links = [normalize_link(url, raw) for raw in extractor.links]
                    result = CrawlResult(
                        url=url,
                        status=200,
                        links=[link for link in links if link],
                        content_type="text/html",
                        elapsed_ms=elapsed_ms,
                        bytes=len(body.encode("utf-8", errors="ignore")),
                        depth=depth,
                        fetched_at=time.time(),
                        next_crawl_at=time.time() + self.recrawl_after_seconds,
                        metadata={"rendered_js": True},
                    )
                    self.policy.release_host_slot(url)
                    return result

                with urlopen(request, timeout=self.timeout) as response:
                    status = response.status
                    content_type = response.headers.get("Content-Type", "")
                    elapsed_ms = int((time.monotonic() - started) * 1000)
                    etag = response.headers.get("ETag")
                    last_mod = response.headers.get("Last-Modified")
                    if etag or last_mod:
                        self._cache_headers[url] = {
                            key: value
                            for key, value in {
                                "etag": etag,
                                "last_modified": last_mod,
                            }.items()
                            if value
                        }

                    if status == 304:
                        result = CrawlResult(
                            url=url,
                            status=status,
                            links=[],
                            content_type=content_type,
                            elapsed_ms=elapsed_ms,
                            depth=depth,
                            fetched_at=time.time(),
                            next_crawl_at=time.time() + self.recrawl_after_seconds,
                            from_cache_hint=True,
                        )
                        self.policy.release_host_slot(url)
                        return result

                    if "json" in content_type.lower():
                        body_bytes = response.read(self.max_bytes)
                        metadata: dict[str, object] = {}
                        try:
                            payload = json.loads(body_bytes.decode("utf-8", errors="replace"))
                            if isinstance(payload, dict) and isinstance(payload.get("data"), list):
                                names = []
                                for item in payload["data"]:
                                    if isinstance(item, dict) and isinstance(item.get("id"), str):
                                        names.append(item["id"])
                                if names:
                                    metadata["models"] = names
                        except Exception:  # noqa: BLE001
                            pass

                        result = CrawlResult(
                            url=url,
                            status=status,
                            links=[],
                            content_type=content_type,
                            elapsed_ms=elapsed_ms,
                            bytes=len(body_bytes),
                            depth=depth,
                            fetched_at=time.time(),
                            next_crawl_at=time.time() + self.recrawl_after_seconds,
                            metadata=metadata,
                        )
                        self.policy.release_host_slot(url)
                        return result

                    if "text/html" not in content_type:
                        result = CrawlResult(
                            url=url,
                            status=status,
                            links=[],
                            content_type=content_type,
                            elapsed_ms=elapsed_ms,
                            depth=depth,
                            fetched_at=time.time(),
                            next_crawl_at=time.time() + self.recrawl_after_seconds,
                        )
                        self.policy.release_host_slot(url)
                        return result

                    body_bytes = response.read(self.max_bytes + 1)
                    truncated = len(body_bytes) > self.max_bytes
                    if truncated:
                        body_bytes = body_bytes[: self.max_bytes]
                    body = body_bytes.decode("utf-8", errors="replace")
            except Exception as exc:  # noqa: BLE001
                self.policy.release_host_slot(url)
                if attempt <= self.retries:
                    time.sleep(self.retry_backoff * attempt)
                    continue
                return CrawlResult(
                    url=url,
                    status=None,
                    links=[],
                    depth=depth,
                    error=str(exc),
                    fetched_at=time.time(),
                    next_crawl_at=time.time() + self.recrawl_after_seconds,
                )

            extractor = LinkExtractor()
            extractor.feed(body)
            links = [normalize_link(url, raw) for raw in extractor.links]
            result = CrawlResult(
                url=url,
                status=status,
                links=[link for link in links if link],
                content_type=content_type,
                elapsed_ms=elapsed_ms,
                bytes=len(body_bytes),
                depth=depth,
                error=(f"response truncated at {self.max_bytes} bytes" if truncated else None),
                fetched_at=time.time(),
                next_crawl_at=time.time() + self.recrawl_after_seconds,
            )
            self.policy.release_host_slot(url)
            return result
