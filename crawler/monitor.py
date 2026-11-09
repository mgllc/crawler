"""On-demand, exact-source change monitoring with durable last-good evidence."""

from __future__ import annotations

import argparse
import hashlib
import html
import json
import os
import re
import sqlite3
import sys
import tempfile
import time
import unicodedata
import uuid
from dataclasses import dataclass
from datetime import UTC, datetime
from email.message import Message
from html.parser import HTMLParser
from pathlib import Path
from typing import ClassVar
from urllib.parse import quote, urlsplit, urlunsplit
from urllib.robotparser import RobotFileParser

from crawler.monitor_http import FetchError, fetch_public, normalize_source_url

USER_AGENT = "GovDOSS-source-monitor/1.0"
NORMALIZATION = "visible-text-v1"
MAX_SOURCES = 25


@dataclass(frozen=True)
class Source:
    id: str
    url: str
    label: str = ""


def _validate_sources(sources: list[Source]) -> list[Source]:
    if not 1 <= len(sources) <= MAX_SOURCES:
        raise ValueError(f"configure between 1 and {MAX_SOURCES} sources")
    ids, urls, validated = set(), set(), []
    for source in sources:
        if not isinstance(source, Source):
            raise ValueError("each source must be a Source")  # noqa: TRY004 -- configuration error
        if not isinstance(source.id, str) or not re.fullmatch(
            r"[a-zA-Z0-9][a-zA-Z0-9_-]{0,63}", source.id
        ):
            raise ValueError(
                "source IDs must be 1-64 letters, digits, underscores or hyphens"
            )
        if not isinstance(source.label, str) or len(source.label) > 200:
            raise ValueError("source labels must be strings of at most 200 characters")
        url = normalize_source_url(source.url)
        if source.id in ids or url in urls:
            raise ValueError("source IDs and normalized URLs must be unique")
        ids.add(source.id)
        urls.add(url)
        validated.append(Source(source.id, url, source.label))
    return validated


def load_sources(path: str | Path) -> list[Source]:
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    if not isinstance(payload, dict) or set(payload) != {"schema_version", "sources"}:
        raise ValueError(
            "source configuration requires only schema_version and sources"
        )
    if type(payload["schema_version"]) is not int or payload["schema_version"] != 1:
        raise ValueError("unsupported source configuration schema_version")
    if not isinstance(payload["sources"], list):
        raise ValueError("sources must be a list")  # noqa: TRY004 -- configuration error
    sources = []
    for item in payload["sources"]:
        if not isinstance(item, dict) or not {"id", "url"} <= set(item):
            raise ValueError("each source requires id and url")
        if set(item) - {"id", "url", "label"}:
            raise ValueError("unknown source configuration field")
        sources.append(Source(item["id"], item["url"], item.get("label", "")))
    return _validate_sources(sources)


def _text(value: str) -> str:
    return " ".join(unicodedata.normalize("NFC", value).split())


class _PageText(HTMLParser):
    ignored: ClassVar[set[str]] = {
        "head",
        "script",
        "style",
        "nav",
        "header",
        "footer",
        "noscript",
        "template",
        "svg",
    }
    void: ClassVar[set[str]] = {
        "area",
        "base",
        "br",
        "col",
        "embed",
        "hr",
        "img",
        "input",
        "link",
        "meta",
        "param",
        "source",
        "track",
        "wbr",
    }
    blocks: ClassVar[set[str]] = {
        "p",
        "div",
        "section",
        "article",
        "main",
        "li",
        "ul",
        "ol",
        "br",
        "hr",
        "h1",
        "h2",
        "h3",
        "h4",
        "h5",
        "h6",
        "tr",
        "td",
        "th",
        "pre",
        "blockquote",
    }

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.parts: list[str] = []
        self.title: list[str] = []
        self.published_at: str | None = None
        self.hidden: list[str] = []
        self.in_title = False

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == "title":
            self.in_title = True
        if tag == "meta" and self.published_at is None:
            name = (
                attrs.get("property")
                or attrs.get("name")
                or attrs.get("itemprop")
                or ""
            ).lower()
            if name in {
                "article:published_time",
                "datepublished",
                "pubdate",
                "dcterms.created",
            }:
                value = attrs.get("content")
                if value:
                    self.published_at = _text(value)[:200]
        if self.hidden:
            if tag not in self.void:
                self.hidden.append(tag)
        elif tag in self.ignored:
            self.hidden.append(tag)
        elif tag in self.blocks:
            self.parts.append(" ")

    def handle_endtag(self, tag):
        if tag == "title":
            self.in_title = False
        if self.hidden:
            if tag in self.hidden:
                index = len(self.hidden) - 1 - self.hidden[::-1].index(tag)
                del self.hidden[index:]
        elif tag in self.blocks:
            self.parts.append(" ")

    def handle_startendtag(self, tag, attrs):
        self.handle_starttag(tag, attrs)
        if tag not in self.void:
            self.handle_endtag(tag)

    def handle_data(self, data):
        if self.in_title:
            self.title.append(data)
        elif not self.hidden:
            self.parts.append(data)


def _normalize(response) -> tuple[str, str | None, str | None]:
    message = Message()
    message["content-type"] = response.headers.get("content-type", "")
    media_type = (
        response.headers.get("content-type", "").split(";", 1)[0].strip().lower()
    )
    if not response.headers.get("content-type"):
        raise ValueError("missing Content-Type; cannot establish a content baseline")
    encoding = response.headers.get("content-encoding", "identity").lower()
    if encoding not in {"", "identity"}:
        raise ValueError(
            "compressed responses are unsupported; identity encoding was requested"
        )
    body = response.body.decode(
        message.get_content_charset() or "utf-8", errors="strict"
    )
    if media_type in {"text/html", "application/xhtml+xml"}:
        parser = _PageText()
        parser.feed(body)
        parser.close()
        text = _text("".join(parser.parts))
        title = _text("".join(parser.title))[:300] or None
        published_at = parser.published_at
    elif media_type == "application/json" or media_type.endswith("+json"):
        payload = json.loads(body)
        text = json.dumps(
            payload,
            sort_keys=True,
            ensure_ascii=False,
            separators=(",", ":"),
            allow_nan=False,
        )
        title, published_at = None, None
    elif media_type in {"text/plain", "text/markdown"}:
        text, title, published_at = _text(body), None, None
    else:
        raise ValueError(f"unsupported Content-Type: {media_type}")
    if not text:
        raise ValueError("empty normalized content; last-good baseline preserved")
    return text, title, published_at


def _now() -> str:
    return datetime.now(UTC).isoformat(timespec="milliseconds").replace("+00:00", "Z")


def _digest(text: str) -> str:
    return hashlib.sha256(text.encode("utf-8")).hexdigest()


def _evidence(old: str, new: str) -> dict:
    prefix = 0
    while prefix < min(len(old), len(new)) and old[prefix] == new[prefix]:
        prefix += 1
    suffix = 0
    while (
        suffix < min(len(old), len(new)) - prefix
        and old[-suffix - 1] == new[-suffix - 1]
    ):
        suffix += 1
    start = max(0, prefix - 60)
    before = old[start : len(old) - suffix + 60]
    after = new[start : len(new) - suffix + 60]
    return {
        "added": [after[:400]],
        "removed": [before[:400]],
        "truncated": len(before) > 400 or len(after) > 400,
    }


def _open_store(path: str | Path) -> sqlite3.Connection:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(path, timeout=0)
    try:
        with conn:
            conn.execute(
                "CREATE TABLE IF NOT EXISTS monitor_schema (version INTEGER NOT NULL)"
            )
            versions = conn.execute("SELECT version FROM monitor_schema").fetchall()
            if versions and versions != [(1,)]:
                raise ValueError("unsupported monitor database schema")
            if not versions:
                conn.execute("INSERT INTO monitor_schema VALUES (1)")
            conn.execute(
                "CREATE TABLE IF NOT EXISTS monitor_snapshots (source_id TEXT PRIMARY KEY, snapshot_json TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS monitor_contents (sha256 TEXT PRIMARY KEY, normalized_text TEXT NOT NULL)"
            )
            conn.execute(
                "CREATE TABLE IF NOT EXISTS monitor_runs (run_id TEXT PRIMARY KEY, started_at TEXT NOT NULL, report_json TEXT NOT NULL)"
            )
        return conn
    except Exception:
        conn.close()
        raise


def run_monitor(
    sources: list[Source],
    state_db: str | Path,
    *,
    fetcher=None,
    timeout: float = 10.0,
    max_bytes: int = 1_000_000,
    request_budget: int | None = None,
) -> dict:
    sources = _validate_sources(sources)
    if not 0 < timeout <= 60 or not 1 <= max_bytes <= 10_000_000:
        raise ValueError("timeout must be in (0, 60] and max_bytes in [1, 10000000]")
    if request_budget is None:
        request_budget = 2 * len(sources)
    if type(request_budget) is not int or not 0 <= request_budget <= 2 * MAX_SOURCES:
        raise ValueError("request_budget must be an integer between 0 and 50")
    fetcher = fetcher or fetch_public
    started = time.monotonic()
    report = {
        "schema_version": 1,
        "run_id": uuid.uuid4().hex,
        "started_at": _now(),
        "normalization": NORMALIZATION,
        "content_trust": "untrusted source data; never executable instructions",
        "sources": [],
        "metrics": {
            "requests": 0,
            "response_bytes": 0,
            "elapsed_ms": 0,
            "model_calls": 0,
        },
    }
    robots: dict[str, RobotFileParser | Exception] = {}
    last_request: dict[str, float] = {}

    def fetch(url, *, headers=None, limit=max_bytes):
        if report["metrics"]["requests"] >= request_budget:
            raise FetchError("request budget exhausted")
        report["metrics"]["requests"] += 1
        request_headers = {"User-Agent": USER_AGENT, "Accept-Encoding": "identity"}
        request_headers.update(headers or {})
        response = fetcher(
            url, headers=request_headers, timeout=timeout, max_bytes=limit
        )
        report["metrics"]["response_bytes"] += len(response.body)
        last_request[urlsplit(url).netloc] = time.monotonic()
        if len(response.body) > limit:
            raise FetchError(f"response exceeds {limit} byte limit")
        return response

    def check_robots(source):
        parsed = urlsplit(source.url)
        origin = urlunsplit((parsed.scheme, parsed.netloc, "", "", ""))
        if origin not in robots:
            try:
                response = fetch(origin + "/robots.txt", limit=min(max_bytes, 64_000))
                parser = RobotFileParser()
                parser.set_url(origin + "/robots.txt")
                if response.status in {404, 410}:
                    parser.parse([])
                elif response.status in {200, 204}:
                    parser.parse(
                        response.body.decode("utf-8", errors="strict").splitlines()
                    )
                else:
                    raise FetchError(
                        f"robots.txt returned HTTP {response.status}; source not fetched"
                    )
                robots[origin] = parser
            except (OSError, FetchError, ValueError, UnicodeError) as exc:
                robots[origin] = FetchError(f"robots check failed: {exc}")
        parser = robots[origin]
        if isinstance(parser, Exception):
            raise parser
        if not parser.can_fetch(USER_AGENT, source.url):
            raise FetchError("source blocked by robots.txt")
        delay = parser.crawl_delay(USER_AGENT) or 0
        if delay > 10:
            raise FetchError(
                "robots crawl delay exceeds the monitor's 10-second wait limit"
            )
        wait = delay - (time.monotonic() - last_request.get(parsed.netloc, 0))
        if wait > 0:
            time.sleep(wait)

    conn = _open_store(state_db)
    try:
        # Serialize complete runs to avoid comparing against a stale concurrent baseline.
        with conn:
            conn.execute("BEGIN IMMEDIATE")
            for source in sources:
                saved = conn.execute(
                    "SELECT snapshot_json FROM monitor_snapshots WHERE source_id = ?",
                    (source.id,),
                ).fetchone()
                previous = json.loads(saved[0]) if saved else None
                if previous and (
                    previous["url"] != source.url
                    or previous["normalization"] != NORMALIZATION
                ):
                    previous = None
                row = {
                    "id": source.id,
                    "label": source.label or source.id,
                    "url": source.url,
                    "checked_at": _now(),
                    "status_code": None,
                    "state": "error",
                    "error": None,
                }
                if previous:
                    row["previous_sha256"] = previous["sha256"]
                    row["last_success_at"] = previous["last_success_at"]
                try:
                    check_robots(source)
                    conditional = {}
                    if previous:
                        if previous.get("etag"):
                            conditional["If-None-Match"] = previous["etag"]
                        if previous.get("last_modified"):
                            conditional["If-Modified-Since"] = previous["last_modified"]
                    response = fetch(source.url, headers=conditional)
                    row["status_code"] = response.status
                    row["checked_at"] = _now()
                    if response.status == 304:
                        if not previous or not conditional:
                            raise ValueError(
                                "HTTP 304 without a conditional last-good baseline"
                            )
                        snapshot = dict(previous)
                        row["state"] = "unchanged"
                    elif response.status == 200:
                        text, title, published_at = _normalize(response)
                        sha = _digest(text)
                        snapshot = {
                            "url": source.url,
                            "normalization": NORMALIZATION,
                            "sha256": sha,
                            "raw_sha256": hashlib.sha256(response.body).hexdigest(),
                            "title": title,
                            "published_at": published_at,
                            "etag": response.headers.get("etag"),
                            "last_modified": response.headers.get("last-modified"),
                        }
                        row["state"] = (
                            "baseline"
                            if previous is None
                            else "unchanged"
                            if previous["sha256"] == sha
                            else "changed"
                        )
                        if row["state"] == "changed":
                            old = conn.execute(
                                "SELECT normalized_text FROM monitor_contents WHERE sha256 = ?",
                                (previous["sha256"],),
                            ).fetchone()
                            if old is None:
                                raise ValueError(
                                    "last-good content evidence is missing from the database"
                                )
                            row["evidence"] = _evidence(old[0], text)
                        conn.execute(
                            "INSERT OR IGNORE INTO monitor_contents VALUES (?, ?)",
                            (sha, text),
                        )
                    else:
                        raise ValueError(
                            f"source returned HTTP {response.status}; redirects are not followed"
                        )
                    snapshot["last_success_at"] = row["checked_at"]
                    conn.execute(
                        "INSERT INTO monitor_snapshots VALUES (?, ?) ON CONFLICT(source_id) DO UPDATE SET snapshot_json = excluded.snapshot_json",
                        (source.id, json.dumps(snapshot, ensure_ascii=False)),
                    )
                    row.update(
                        {
                            key: snapshot.get(key)
                            for key in (
                                "sha256",
                                "raw_sha256",
                                "title",
                                "published_at",
                                "last_success_at",
                            )
                        }
                    )
                except (
                    OSError,
                    FetchError,
                    ValueError,
                    UnicodeError,
                    LookupError,
                ) as exc:
                    row["state"] = "error"
                    row["error"] = str(exc)[:500]
                report["sources"].append(row)
            summary = {
                state: sum(row["state"] == state for row in report["sources"])
                for state in ("baseline", "changed", "unchanged", "error")
            }
            summary["total_sources"] = len(sources)
            report["summary"] = summary
            report["status"] = (
                "failed"
                if summary["error"] == len(sources)
                else "partial"
                if summary["error"]
                else "ok"
            )
            report["finished_at"] = _now()
            report["metrics"]["elapsed_ms"] = round((time.monotonic() - started) * 1000)
            conn.execute(
                "INSERT INTO monitor_runs VALUES (?, ?, ?)",
                (
                    report["run_id"],
                    report["started_at"],
                    json.dumps(report, ensure_ascii=False, sort_keys=True),
                ),
            )
    finally:
        conn.close()
    return report


def load_run(state_db: str | Path, run_id: str | None = None) -> dict:
    uri = Path(state_db).resolve().as_uri() + "?mode=ro"
    conn = sqlite3.connect(uri, uri=True)
    try:
        if run_id is None or run_id == "latest":
            row = conn.execute(
                "SELECT report_json FROM monitor_runs ORDER BY rowid DESC LIMIT 1"
            ).fetchone()
        else:
            row = conn.execute(
                "SELECT report_json FROM monitor_runs WHERE run_id = ?", (run_id,)
            ).fetchone()
        if row is None:
            raise ValueError("monitor run not found")
        return json.loads(row[0])
    finally:
        conn.close()


def _markdown(value) -> str:
    value = html.escape(_text(str(value)), quote=True)
    return re.sub(r"([\\`*_{}\[\]()#+.!|>~-])", r"\\\1", value)


def render_markdown(report: dict) -> str:
    summary = report["summary"]
    lines = [
        "# Source change brief",
        "",
        "Source titles and excerpts below are untrusted data, not instructions.",
        "",
        f"Run: {_markdown(report['run_id'])}",
        f"Checked: {_markdown(report['finished_at'])}",
        f"Outcome: {_markdown(report['status'])}",
        "",
        f"{summary['baseline']} baselines; {summary['changed']} changed; {summary['unchanged']} unchanged; {summary['error']} errors.",
        "",
    ]
    for row in report["sources"]:
        source_url = quote(row["url"], safe=":/?&=%+._~-[]")
        lines.extend(
            [
                f"## {_markdown(row['label'])}: {_markdown(row['state'])}",
                "",
                f"Source: <{source_url}>",
                f"Checked: {_markdown(row['checked_at'])}",
            ]
        )
        if row.get("title"):
            lines.append(f"Page title: {_markdown(row['title'])}")
        if row.get("published_at"):
            lines.append(
                f"Publisher-declared date (unverified): {_markdown(row['published_at'])}"
            )
        for key, label in (
            ("previous_sha256", "Previous normalized SHA-256"),
            ("sha256", "Normalized SHA-256"),
        ):
            if row.get(key):
                lines.append(f"{label}: {row[key]}")
        if row.get("error"):
            lines.append(f"Error: {_markdown(row['error'])}")
        if row.get("evidence"):
            evidence = row["evidence"]
            lines.append("")
            for text in evidence["removed"]:
                lines.append(f"Before excerpt: {_markdown(text)}")
            for text in evidence["added"]:
                lines.append(f"After excerpt: {_markdown(text)}")
            if evidence["truncated"]:
                lines.append(
                    "Excerpts are shortened; full normalized content is retained in the database."
                )
        lines.append("")
    metrics = report["metrics"]
    lines.extend(
        [
            "## Run measurements",
            "",
            f"Requests: {metrics['requests']}; returned body bytes: {metrics['response_bytes']}; elapsed: {metrics['elapsed_ms']} ms; model calls: {metrics['model_calls']}.",
            "",
            "Changes reflect normalized static text, not a judgment of business significance. Human review time and hosting cost are not measured.",
            "",
        ]
    )
    return "\n".join(lines)


def _atomic_write(path: str | Path, content: str) -> None:
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = None
    try:
        with tempfile.NamedTemporaryFile(
            "w", encoding="utf-8", dir=path.parent, prefix=".monitor-", delete=False
        ) as handle:
            temporary = handle.name
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        os.replace(temporary, path)
    finally:
        if temporary and os.path.exists(temporary):
            os.unlink(temporary)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument("--sources", help="Versioned source configuration JSON.")
    mode.add_argument(
        "--export-run", help="Export a stored run ID, or latest, without fetching."
    )
    parser.add_argument("--state-db", required=True)
    parser.add_argument("--report", required=True, help="Atomic JSON report output.")
    parser.add_argument("--markdown", help="Optional readable Markdown brief.")
    parser.add_argument("--timeout", type=float, default=10.0)
    parser.add_argument("--max-bytes", type=int, default=1_000_000)
    parser.add_argument("--request-budget", type=int)
    args = parser.parse_args(argv)
    report = None
    try:
        paths = [
            Path(value).resolve()
            for value in (args.sources, args.state_db, args.report, args.markdown)
            if value
        ]
        for index, path in enumerate(paths):
            for other in paths[index + 1 :]:
                if path == other or (
                    path.exists() and other.exists() and os.path.samefile(path, other)
                ):
                    raise ValueError(
                        "configuration, state database and output paths must be distinct"
                    )
        if args.export_run:
            report = load_run(args.state_db, args.export_run)
        else:
            sources = load_sources(args.sources)
            report = run_monitor(
                sources,
                args.state_db,
                timeout=args.timeout,
                max_bytes=args.max_bytes,
                request_budget=args.request_budget,
            )
        _atomic_write(
            args.report,
            json.dumps(report, indent=2, ensure_ascii=False, sort_keys=True) + "\n",
        )
        if args.markdown:
            _atomic_write(args.markdown, render_markdown(report))
    except (OSError, ValueError, sqlite3.Error) as exc:
        print(f"Source monitor failed: {exc}", file=sys.stderr)
        if report:
            print(
                f"Run {report['run_id']} is stored; retry with --export-run {report['run_id']}.",
                file=sys.stderr,
            )
        return 1
    print(f"Run {report['run_id']}: {report['status']}; wrote {args.report}")
    return 2 if report["summary"]["error"] else 0


if __name__ == "__main__":
    raise SystemExit(main())
