from __future__ import annotations

import argparse
import json

from crawler.agent import build_agent_inventory, build_service_graph, write_json
from crawler.core import Crawler


def parse_args() -> argparse.Namespace:
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
        "--all-domains",
        action="store_true",
        help="Alias for --allow-external to crawl across all discovered domains.",
    )
    parser.add_argument(
        "--allow-subdomains",
        action="store_true",
        help="Allow subdomains when same-domain restriction is enabled.",
    )
    parser.add_argument(
        "--include-domain",
        action="append",
        default=[],
        help="Optional domain allowlist (repeatable). Includes subdomains.",
    )
    parser.add_argument(
        "--exclude-domain",
        action="append",
        default=[],
        help="Optional domain denylist (repeatable). Excludes subdomains.",
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
    parser.add_argument("--state-in", help="Resume crawl state from this JSON file.")
    parser.add_argument("--state-out", help="Write crawl state for resume to this JSON file.")
    parser.add_argument("--state-db", help="SQLite state database path for resume/persist.")
    parser.add_argument("--report", help="Write crawl report JSON to this path.")
    parser.add_argument("--max-workers", type=int, default=4, help="Global fetch concurrency.")
    parser.add_argument("--per-host-workers", type=int, default=2, help="Per-host concurrency limit.")
    parser.add_argument("--request-budget", type=int, help="Max number of fetches this run.")
    parser.add_argument(
        "--recrawl-after-seconds",
        type=int,
        default=86_400,
        help="Suggested recrawl interval stored in result metadata.",
    )
    parser.add_argument(
        "--max-query-params",
        type=int,
        default=15,
        help="Trap protection: max allowed query params per URL.",
    )
    parser.add_argument(
        "--max-path-repeats",
        type=int,
        default=4,
        help="Trap protection: max repeated path segment occurrences.",
    )
    parser.add_argument(
        "--render-js",
        action="store_true",
        help="Render pages via Playwright before extracting links.",
    )
    parser.add_argument(
        "--agent-discovery",
        action="store_true",
        help="Seed and classify AI/agent/server endpoints (well-known, schemas, models, health).",
    )
    parser.add_argument("--agent-inventory-out", help="Write agent inventory JSON path.")
    parser.add_argument("--service-graph-out", help="Write service graph JSON path.")
    parser.add_argument("--output", default="crawl.json", help="Output JSON path.")
    return parser.parse_args()


def main() -> int:
    args = parse_args()
    same_domain = not (args.allow_external or args.all_domains)
    crawler = Crawler(
        start_url=args.start_url,
        max_depth=args.max_depth,
        max_pages=args.max_pages,
        same_domain=same_domain,
        allow_subdomains=args.allow_subdomains,
        user_agent=args.user_agent,
        timeout=args.timeout,
        max_bytes=args.max_bytes,
        respect_robots=args.respect_robots,
        crawl_delay=args.crawl_delay,
        include_sitemap=args.include_sitemap,
        retries=args.retries,
        retry_backoff=args.retry_backoff,
        include_domains=args.include_domain,
        exclude_domains=args.exclude_domain,
        max_workers=args.max_workers,
        per_host_workers=args.per_host_workers,
        request_budget=args.request_budget,
        recrawl_after_seconds=args.recrawl_after_seconds,
        render_js=args.render_js,
        agent_discovery=args.agent_discovery,
        max_query_params=args.max_query_params,
        max_path_repeats=args.max_path_repeats,
    )

    initial_queue = None
    initial_visited = None
    initial_results = None
    if args.state_db:
        initial_queue, initial_visited, initial_results = Crawler.load_state_sqlite(args.state_db)
    elif args.state_in:
        initial_queue, initial_visited, initial_results = Crawler.load_state(args.state_in)

    results, queue, visited = crawler.crawl(
        initial_queue=initial_queue,
        initial_visited=initial_visited,
        initial_results=initial_results,
    )

    if args.state_db:
        Crawler.save_state_sqlite(args.state_db, queue, visited, results)
    if args.state_out:
        Crawler.save_state(args.state_out, queue, visited, results)
    if args.report:
        crawler.write_report(args.report, results, visited)
    if args.agent_inventory_out:
        write_json(args.agent_inventory_out, build_agent_inventory(results))
    if args.service_graph_out:
        write_json(args.service_graph_out, build_service_graph(results))

    payload = [result.to_dict() for result in results]
    with open(args.output, "w", encoding="utf-8") as handle:
        json.dump(payload, handle, indent=2, sort_keys=True)
    print(f"Wrote {len(results)} pages to {args.output}")
    return 0
