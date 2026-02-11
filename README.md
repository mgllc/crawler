# crawler

A minimal, dependency-free website crawler that maps links with depth and page limits.

## Features
- Breadth-first crawl with configurable depth and page limits.
- Optional same-domain restriction with subdomain support.
- Robots.txt compliance with optional crawl delay.
- Link discovery for common HTML assets (links, scripts, images, iframes).
- Optional sitemap seeding via robots.txt.
- JSON output with status codes, response metadata, and discovered links.

## Usage
```bash
python3 crawler.py https://example.com --max-depth 2 --max-pages 50 --output crawl.json
```

Allow external domains:
```bash
python3 crawler.py https://example.com --allow-external
```

Respect robots.txt and crawl with a custom user agent:
```bash
python3 crawler.py https://example.com --user-agent "crawler/2.0" --respect-robots
```

Seed the crawl from sitemap URLs referenced in robots.txt:
```bash
python3 crawler.py https://example.com --include-sitemap
```

The output JSON is a list of pages with their status, response metadata, extracted links,
and any error details.
