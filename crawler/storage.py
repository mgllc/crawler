from __future__ import annotations

import json
import sqlite3
from pathlib import Path

from crawler.models import CrawlResult


def init_db(path: str) -> sqlite3.Connection:
    db_path = Path(path)
    db_path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path)
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawl_state (
            id INTEGER PRIMARY KEY CHECK (id = 1),
            queue_json TEXT NOT NULL,
            visited_json TEXT NOT NULL,
            results_json TEXT NOT NULL
        )
        """
    )
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS crawl_results (
            url TEXT PRIMARY KEY,
            payload_json TEXT NOT NULL
        )
        """
    )
    conn.commit()
    return conn


def save_state_db(
    conn: sqlite3.Connection,
    queue: list[tuple[str, int]],
    visited: set[str],
    results: list[CrawlResult],
) -> None:
    queue_json = json.dumps([[u, d] for u, d in queue], sort_keys=True)
    visited_json = json.dumps(sorted(visited), sort_keys=True)
    results_json = json.dumps([r.to_dict() for r in results], sort_keys=True)
    conn.execute(
        "INSERT INTO crawl_state (id, queue_json, visited_json, results_json) VALUES (1, ?, ?, ?) "
        "ON CONFLICT(id) DO UPDATE SET queue_json=excluded.queue_json, visited_json=excluded.visited_json, results_json=excluded.results_json",
        (queue_json, visited_json, results_json),
    )
    conn.executemany(
        "INSERT OR REPLACE INTO crawl_results (url, payload_json) VALUES (?, ?)",
        [(r.url, json.dumps(r.to_dict(), sort_keys=True)) for r in results],
    )
    conn.commit()


def load_state_db(
    conn: sqlite3.Connection,
) -> tuple[list[tuple[str, int]], set[str], list[CrawlResult]]:
    row = conn.execute(
        "SELECT queue_json, visited_json, results_json FROM crawl_state WHERE id = 1"
    ).fetchone()
    if not row:
        return [], set(), []
    queue_payload = json.loads(row[0])
    visited_payload = json.loads(row[1])
    results_payload = json.loads(row[2])
    queue = [
        (str(item[0]), int(item[1]))
        for item in queue_payload
        if isinstance(item, list) and len(item) == 2
    ]
    visited = {str(item) for item in visited_payload if isinstance(item, str)}
    results = [CrawlResult.from_dict(item) for item in results_payload if isinstance(item, dict)]
    return queue, visited, results
