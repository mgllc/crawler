from __future__ import annotations

from html.parser import HTMLParser
from urllib.parse import parse_qsl, urldefrag, urlencode, urljoin, urlparse, urlunparse

TRACKING_PARAMS = {
    "utm_source",
    "utm_medium",
    "utm_campaign",
    "utm_term",
    "utm_content",
    "gclid",
    "fbclid",
}


class LinkExtractor(HTMLParser):
    """Extract links from common URL-carrying HTML tags."""

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


def canonicalize_url(url: str) -> str:
    parsed = urlparse(url)
    scheme = parsed.scheme.lower()
    netloc = parsed.netloc.lower()
    path = parsed.path or "/"
    if path != "/" and path.endswith("/"):
        path = path.rstrip("/")

    params = [
        (key, value)
        for key, value in parse_qsl(parsed.query, keep_blank_values=True)
        if key.lower() not in TRACKING_PARAMS
    ]
    params.sort(key=lambda item: (item[0], item[1]))
    query = urlencode(params, doseq=True)
    return urlunparse((scheme, netloc, path, "", query, ""))


def normalize_link(base_url: str, link: str) -> str | None:
    if link.startswith(("mailto:", "javascript:")):
        return None
    absolute = urljoin(base_url, link)
    normalized, _ = urldefrag(absolute)
    parsed = urlparse(normalized)
    if parsed.scheme not in {"http", "https"}:
        return None
    return canonicalize_url(normalized)
