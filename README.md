# crawler

A minimal, dependency-free website crawler that maps links with depth and page
limits, structured around the **OODA Loop** and **GovDOSS KIS⁴/SOA⁴** principles.

## Architecture

### OODA Loop (Observe → Orient → Decide → Act)

Every URL in the crawl queue passes through four explicit phases:

| Phase   | Method        | Responsibility |
|---------|---------------|----------------|
| Observe | `_observe()`  | Fetch the URL; collect status, headers, body, and raw links. |
| Orient  | `_orient()`   | Analyse the observation; return normalised link candidates. |
| Decide  | `_decide()`   | Apply depth, domain, and SOA⁴ authorization rules to links. |
| Act     | `_act()`      | Enqueue approved URLs for the next crawl iteration. |

### GovDOSS KIS⁴

| Pillar       | Enforcement |
|--------------|-------------|
| **Simple**   | One responsibility per method; clear phase boundaries. |
| **Secure**   | Start URL validated on construction; robots.txt honoured; URL deny/allow patterns via `--deny-pattern` / `--allow-pattern`. |
| **Sustainable** | Structured logging at every OODA phase (`--log-level`); consistent error reporting in `CrawlResult`. |
| **Scalable** | Configurable depth, page, byte, timeout, and retry limits. |

### GovDOSS SOA⁴

| Element          | Mapping |
|------------------|---------|
| **Subject**      | The crawler, identified by `--user-agent`. |
| **Object**       | Each URL in the crawl queue. |
| **Authorization**| `--deny-pattern` / `--allow-pattern` regex filters evaluated in the Decide phase before any URL is enqueued. |
| **Approval**     | robots.txt compliance checked before every fetch. |
| **Action**       | Every fetch and enqueue logged with structured key=value fields. |

## Features

- Breadth-first crawl with configurable depth and page limits.
- Optional same-domain restriction with subdomain support.
- Robots.txt compliance with optional crawl delay.
- Link discovery for common HTML assets (links, scripts, images, iframes).
- Optional sitemap seeding via robots.txt.
- JSON output with status codes, response metadata, and discovered links.
- SOA⁴ URL authorization via regex allow/deny patterns.
- Structured audit logging at configurable verbosity.

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

### SOA⁴ Authorization patterns

Block URLs matching a pattern (evaluated first):
```bash
python3 crawler.py https://example.com --deny-pattern "/admin/" --deny-pattern "/private/"
```

Restrict crawl to only URLs matching a pattern:
```bash
python3 crawler.py https://example.com --allow-pattern "/docs/"
```

### Logging (OODA observability)

Enable INFO-level logging to see each OODA phase:
```bash
python3 crawler.py https://example.com --log-level INFO
```

Enable DEBUG-level logging for full OODA + SOA⁴ authorization traces:
```bash
python3 crawler.py https://example.com --log-level DEBUG
```

The output JSON is a list of pages with their status, response metadata, extracted links,
and any error details.

## Tests

```bash
python3 -m unittest discover -s tests -v
```
