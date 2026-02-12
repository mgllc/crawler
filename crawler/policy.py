from __future__ import annotations

import threading
import time
import xml.etree.ElementTree as ET
from collections import deque
from urllib.error import URLError
from urllib.parse import parse_qsl, urlparse
from urllib.request import Request, urlopen
from urllib.robotparser import RobotFileParser

from crawler.extract import normalize_link


class CrawlPolicy:
    def __init__(
        self,
        start_url: str,
        same_domain: bool,
        allow_subdomains: bool,
        respect_robots: bool,
        crawl_delay: float,
        include_sitemap: bool,
        user_agent: str,
        timeout: int,
        max_bytes: int,
        include_domains: list[str] | None = None,
        exclude_domains: list[str] | None = None,
        max_query_params: int = 15,
        max_path_repeats: int = 4,
        per_host_workers: int = 2,
    ) -> None:
        parsed = urlparse(start_url)
        self.origin = parsed.netloc
        self.scheme = parsed.scheme or "https"
        self.same_domain = same_domain
        self.allow_subdomains = allow_subdomains
        self.respect_robots = respect_robots
        self.crawl_delay = crawl_delay
        self.include_sitemap = include_sitemap
        self.user_agent = user_agent
        self.timeout = timeout
        self.max_bytes = max_bytes
        self.include_domains = [domain.lower() for domain in (include_domains or [])]
        self.exclude_domains = [domain.lower() for domain in (exclude_domains or [])]
        self.max_query_params = max_query_params
        self.max_path_repeats = max_path_repeats
        self.per_host_workers = max(1, per_host_workers)

        self._robots_cache: dict[str, RobotFileParser] = {}
        self._last_request: dict[str, float] = {}
        self._sitemap_links: set[str] = set()
        self._host_slots: dict[str, int] = {}
        self._host_lock = threading.Condition()

    @property
    def sitemap_links(self) -> set[str]:
        return self._sitemap_links

    def acquire_host_slot(self, url: str) -> None:
        host = urlparse(url).netloc
        with self._host_lock:
            while self._host_slots.get(host, 0) >= self.per_host_workers:
                self._host_lock.wait(timeout=0.05)
            self._host_slots[host] = self._host_slots.get(host, 0) + 1

    def release_host_slot(self, url: str) -> None:
        host = urlparse(url).netloc
        with self._host_lock:
            current = self._host_slots.get(host, 0)
            if current <= 1:
                self._host_slots.pop(host, None)
            else:
                self._host_slots[host] = current - 1
            self._host_lock.notify_all()

    def _domain_matches(self, netloc: str, domain: str) -> bool:
        candidate = netloc.lower()
        target = domain.lower()
        return candidate == target or candidate.endswith(f".{target}")

    def _looks_like_trap(self, link: str) -> bool:
        parsed = urlparse(link)
        if len(parse_qsl(parsed.query, keep_blank_values=True)) > self.max_query_params:
            return True
        segments = [seg for seg in parsed.path.split("/") if seg]
        if not segments:
            return False
        max_repeat = max(segments.count(seg) for seg in set(segments))
        return max_repeat > self.max_path_repeats

    def _passes_custom_domain_filters(self, link: str) -> bool:
        netloc = urlparse(link).netloc.lower()
        if not netloc:
            return False
        if self.include_domains and not any(
            self._domain_matches(netloc, domain) for domain in self.include_domains
        ):
            return False
        if any(self._domain_matches(netloc, domain) for domain in self.exclude_domains):
            return False
        return True

    def is_allowed_domain(self, link: str) -> bool:
        if self._looks_like_trap(link):
            return False
        if not self._passes_custom_domain_filters(link):
            return False
        if not self.same_domain:
            return True
        parsed = urlparse(link)
        if parsed.netloc == self.origin:
            return True
        if self.allow_subdomains and parsed.netloc.endswith(f".{self.origin}"):
            return True
        return False

    def filter_links(self, links: list[str]) -> list[str]:
        return [link for link in links if self.is_allowed_domain(link)]

    def allowed_by_robots(self, url: str) -> bool:
        if not self.respect_robots:
            return True
        parser = self.robots_parser(url)
        return parser.can_fetch(self.user_agent, url)

    def respect_delay(self, url: str) -> None:
        parsed = urlparse(url)
        netloc = parsed.netloc
        parser = self.robots_parser(url) if self.respect_robots else None
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

    def seed_sitemap_queue(
        self,
        start_url: str,
        queue: deque[tuple[str, int]],
        visited: set[str],
        queued: set[str],
    ) -> None:
        if not self.include_sitemap:
            return
        self.robots_parser(start_url)
        self.enqueue_new_sitemap_links(queue, visited, queued)

    def enqueue_new_sitemap_links(
        self,
        queue: deque[tuple[str, int]],
        visited: set[str],
        queued: set[str],
    ) -> None:
        for link in sorted(self._sitemap_links):
            if link in visited or link in queued:
                continue
            if self.is_allowed_domain(link):
                queue.append((link, 0))
                queued.add(link)

    def robots_parser(self, url: str) -> RobotFileParser:
        parsed = urlparse(url)
        netloc = parsed.netloc
        if netloc in self._robots_cache:
            return self._robots_cache[netloc]

        parser = RobotFileParser()
        parser.set_url(f"{parsed.scheme or self.scheme}://{netloc}/robots.txt")
        try:
            parser.read()
        except (URLError, OSError):
            parser.allow_all = True

        self._robots_cache[netloc] = parser

        if self.include_sitemap:
            for sitemap in parser.site_maps() or []:
                self._read_sitemap(sitemap, base_url=url)

        return parser

    def _read_sitemap(self, sitemap_url: str, base_url: str) -> None:
        try:
            request = Request(sitemap_url, headers={"User-Agent": self.user_agent})
            with urlopen(request, timeout=self.timeout) as response:
                if "xml" not in response.headers.get("Content-Type", ""):
                    return
                body = response.read(self.max_bytes)
        except Exception:
            return

        try:
            root = ET.fromstring(body)
        except ET.ParseError:
            return

        for elem in root.iter():
            if elem.tag.endswith("loc") and elem.text:
                normalized = normalize_link(base_url, elem.text.strip())
                if normalized:
                    self._sitemap_links.add(normalized)
