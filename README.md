# crawler

A modular crawler with breadth-first traversal, policies, persistence, and optional rendering.

## Implemented roadmap phases

### Phase A (Scale)
- Concurrent fetchers with configurable global workers (`--max-workers`).
- Request budget per run (`--request-budget`) to cap total network fetches.

### Phase B (Freshness)
- Conditional request headers (`If-None-Match`, `If-Modified-Since`) based on previous response headers.
- Scheduling metadata in results (`fetched_at`, `next_crawl_at`, `from_cache_hint`).

### Phase C (Quality)
- URL canonicalization (host/scheme normalization and tracking query stripping).
- Trap detection controls (`--max-query-params`, `--max-path-repeats`).
- XML sitemap parsing with namespace-agnostic `<loc>` extraction.

### Phase D (Ops)
- Crawl metrics and report export (`--report`).
- DB-backed state/frontier persistence via SQLite (`--state-db`).

### Phase E (Advanced)
- Optional JS rendering pipeline (`--render-js`) via Playwright if installed.
- Agent/server discovery mode with endpoint seeding, inventory, and service graph outputs.

## Reliability

`Crawler` optionally accepts a `health_monitor`, `circuit_breaker`, and
`retry_policy` (see `self_healing.py`) for production-grade fault handling:
per-domain circuit breaking after repeated failures, HTTP-status-aware
adaptive retry/backoff, and real-time health/anomaly tracking. These are
library-level options (construct the objects and pass them to `Crawler(...)`)
rather than CLI flags — see `test_crawler.py`'s `TestCrawlerWithSelfHealing`
for usage examples.

## Core features
- Same-domain restriction, optional subdomain support, or full external crawling (`--all-domains`).
- Domain include/exclude filters for controlled multi-domain scans.
- robots.txt compliance with optional crawl delay.
- Optional sitemap seeding via robots.txt.
- Retries with linear backoff and per-response byte limits.
- Link discovery for common URL-bearing tags (`a`, `link`, `script`, `img`, `iframe`).
- Resume support with crawl-state import/export (`--state-in`, `--state-out`, `--state-db`).

## Architecture
- `crawler/core.py`: crawl loop, concurrency, fetch logic, state/report helpers.
- `crawler/policy.py`: robots, politeness, domain filtering, trap detection, sitemap handling.
- `crawler/extract.py`: HTML extraction + URL normalization/canonicalization.
- `crawler/models.py`: result dataclass + serialization.
- `crawler/storage.py`: SQLite persistence for frontier/state/results.
- `crawler/browser.py`: optional Playwright page rendering.
- `crawler/cli.py`: command-line interface.

## Usage
```bash
python3 crawler.py https://example.com --max-depth 2 --max-pages 200 --max-workers 8
```

Cross-domain crawl with controls:
```bash
python3 crawler.py https://example.com --all-domains \
  --include-domain example.com --exclude-domain ads.example.com
```

Run with state DB and report output:
```bash
python3 crawler.py https://example.com --state-db crawl.db --report report.json
```

Enable JS rendering (requires Playwright installed):
```bash
python3 crawler.py https://example.com --render-js
```

Discover AI agent/server endpoints and export inventories:
```bash
python3 crawler.py https://example.com --agent-discovery \
  --agent-inventory-out agent_inventory.json \
  --service-graph-out service_graph.json
```

## Testing
Two suites cover this project: `test_crawler.py` at the repo root (core
crawl loop, self-healing components) and `tests/` (agent/server discovery,
inventory, and service-graph generation).
```bash
python3 -m pytest test_crawler.py -v
python3 -m unittest discover -s tests -v
```
