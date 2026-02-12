from __future__ import annotations

from dataclasses import asdict, dataclass, field


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
    fetched_at: float | None = None
    next_crawl_at: float | None = None
    from_cache_hint: bool = False
    metadata: dict[str, object] = field(default_factory=dict)

    def to_dict(self) -> dict[str, object | None]:
        return asdict(self)

    @classmethod
    def from_dict(cls, payload: dict[str, object]) -> "CrawlResult":
        return cls(
            url=str(payload.get("url", "")),
            status=payload.get("status") if isinstance(payload.get("status"), int) else None,
            links=[str(link) for link in payload.get("links", []) if isinstance(link, str)],
            content_type=(
                str(payload.get("content_type")) if isinstance(payload.get("content_type"), str) else None
            ),
            elapsed_ms=(
                int(payload.get("elapsed_ms")) if isinstance(payload.get("elapsed_ms"), int) else None
            ),
            bytes=int(payload.get("bytes")) if isinstance(payload.get("bytes"), int) else None,
            depth=int(payload.get("depth")) if isinstance(payload.get("depth"), int) else None,
            error=str(payload.get("error")) if isinstance(payload.get("error"), str) else None,
            fetched_at=(
                float(payload.get("fetched_at"))
                if isinstance(payload.get("fetched_at"), (int, float))
                else None
            ),
            next_crawl_at=(
                float(payload.get("next_crawl_at"))
                if isinstance(payload.get("next_crawl_at"), (int, float))
                else None
            ),
            from_cache_hint=bool(payload.get("from_cache_hint", False)),
            metadata=(
                payload.get("metadata") if isinstance(payload.get("metadata"), dict) else {}
            ),
        )
