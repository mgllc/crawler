"""Engineering Evidence Watch: bounded observation, durable evidence, no actions.

Invoke once per operator cycle. No daemon, scheduler, model calls or publication.
"""
import argparse
import difflib
import hashlib
import json
import re
import signal
import sqlite3
import sys
import time
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from crawler.watch_extract import EXTRACTOR_VERSION, meaningful_text, strict_json
from crawler.watch_network import FetchError, Response, SafeFetcher, checked_url

PORTFOLIO = frozenset({'GovDOSS/GODSclaw', 'GovDOSS/telmo', 'mgllc/palantir-julia-integration',
                       'mgllc/crawler', 'mgllc/SOA4', 'mgllc/KIS4'})
IDENTIFIER = re.compile(r'[a-z0-9][a-z0-9-]{0,63}')
MAX_SNAPSHOTS = 500
USER_AGENT = 'GovDOSSEvidenceWatch'


def now():
    return datetime.now(UTC).isoformat()


def digest(value):
    return hashlib.sha256(value).hexdigest()


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), allow_nan=False)


def validate_manifest(manifest):
    if not isinstance(manifest, dict) or type(manifest.get('schema')) is not int or manifest['schema'] != 1:
        raise ValueError('schema-1-manifest-required')
    if set(manifest) != {'schema', 'sources', 'max_requests_per_run', 'max_bytes_per_response'}:
        raise ValueError('unknown-or-missing-manifest-field')
    for key, low, high in (('max_requests_per_run', 2, 100), ('max_bytes_per_response', 1024, 1_048_576)):
        if type(manifest.get(key)) is not int or not low <= manifest[key] <= high:
            raise ValueError('invalid-budget')
    sources = manifest.get('sources')
    if not isinstance(sources, list) or not 1 <= len(sources) <= 50:
        raise ValueError('one-to-fifty-sources-required')
    ids, urls = set(), set()
    for source in sources:
        if not isinstance(source, dict) or set(source) != {'id', 'url', 'repositories'}:
            raise ValueError('invalid-source-fields')
        sid = source.get('id')
        if not isinstance(sid, str) or not IDENTIFIER.fullmatch(sid) or sid in ids:
            raise ValueError('duplicate-or-invalid-source-id')
        checked_url(source.get('url'))
        if source['url'] in urls:
            raise ValueError('duplicate-source-url')
        repos = source.get('repositories')
        if (not isinstance(repos, list) or not repos or len(repos) > 6
                or any(not isinstance(r, str) or r not in PORTFOLIO for r in repos)
                or len(set(repos)) != len(repos)):
            raise ValueError('source-outside-final-portfolio')
        ids.add(sid)
        urls.add(source['url'])
    if len(canonical(manifest).encode()) > 65_536:
        raise ValueError('manifest-too-large')
    return manifest


def robots_url(url):
    return 'https://' + urlsplit(url).netloc + '/robots.txt'


def open_state(path):
    target = Path(path)
    if target.is_symlink():
        raise ValueError('state-symlink-rejected')
    target.parent.mkdir(parents=True, exist_ok=True)
    conn = sqlite3.connect(target, timeout=1)
    try:
        tables = {r[0] for r in conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
        expected = {'watch_meta', 'watch_cycles', 'watch_tasks', 'watch_snapshots'}
        if tables and tables != expected:
            raise ValueError('state-database-not-owned-by-watch')
        conn.executescript('''
            CREATE TABLE IF NOT EXISTS watch_meta (schema_version INTEGER NOT NULL);
            CREATE TABLE IF NOT EXISTS watch_cycles (
                cycle TEXT PRIMARY KEY, manifest_hash TEXT NOT NULL, mode TEXT NOT NULL);
            CREATE TABLE IF NOT EXISTS watch_tasks (
                cycle TEXT NOT NULL, source_id TEXT NOT NULL, result TEXT,
                PRIMARY KEY (cycle, source_id));
            CREATE TABLE IF NOT EXISTS watch_snapshots (
                id INTEGER PRIMARY KEY, cycle TEXT NOT NULL, source_id TEXT NOT NULL,
                scope_hash TEXT NOT NULL, mode TEXT NOT NULL, extractor TEXT NOT NULL,
                collected_at TEXT NOT NULL, raw_sha256 TEXT NOT NULL, text_sha256 TEXT NOT NULL,
                raw BLOB NOT NULL, extracted TEXT NOT NULL,
                UNIQUE(cycle, source_id));
        ''')
        if not tables:
            conn.execute('INSERT INTO watch_meta VALUES (1)')
        if conn.execute('SELECT schema_version FROM watch_meta').fetchall() != [(1,)]:
            raise ValueError('unsupported-state-schema')
        conn.commit()
        return conn
    except BaseException:
        conn.close()
        raise


def _source_response(source, fetch, limit):
    robot = fetch(robots_url(source['url']), min(limit, 65_536))
    if robot.status == 404:
        delay = 1
    elif robot.status == 200:
        if robot.content_type.split(';')[0].strip().lower() != 'text/plain':
            raise FetchError('robots-invalid-content')
        parser = RobotFileParser()
        parser.parse(robot.body.decode('utf-8-sig').splitlines())
        if not parser.can_fetch(USER_AGENT, source['url']):
            raise FetchError('robots-denied')
        delay = max(1, parser.crawl_delay(USER_AGENT) or 0)
        rate = parser.request_rate(USER_AGENT)
        if rate:
            delay = max(delay, rate.seconds / rate.requests if rate.requests else 31)
        if delay > 30:
            raise FetchError('robots-delay-exceeds-budget')
    else:
        raise FetchError('robots-unavailable')
    return delay


def run_watch(manifest, state_path, cycle, fetcher):
    """One bounded pass. Inject transport only for trusted host code/tests.

    Each source's snapshot and task receipt commit atomically. A single SQLite
    writer lock covers collection to prevent concurrent duplicate receipts.
    An interrupted GET may repeat; uncommitted observations never become evidence.
    """
    validate_manifest(manifest)
    if not isinstance(cycle, str) or not IDENTIFIER.fullmatch(cycle):
        raise ValueError('invalid-cycle-id')
    mode = getattr(fetcher, 'mode', None)
    if mode not in ('fixture', 'live'):
        raise ValueError('explicit-collection-mode-required')
    binding = digest(canonical(manifest).encode())
    limit = manifest['max_bytes_per_response']
    budget = manifest['max_requests_per_run']
    used = 0
    allowed = {s['url'] for s in manifest['sources']} | {robots_url(s['url']) for s in manifest['sources']}
    last_request = {}
    host_delays = {}

    def fetch(url, byte_limit):
        nonlocal used
        if url not in allowed or used >= budget:
            raise FetchError('request-budget-exhausted')
        host = urlsplit(url).hostname
        # Each invocation may resume immediately after a killed worker. All
        # accepted robots delays are <=30s, so this also bounds restart bursts.
        if mode == 'live' and used == 0:
            time.sleep(30)
        if mode == 'live' and host in last_request:
            time.sleep(max(0, host_delays.get(host, 1) - (time.monotonic() - last_request[host])))
        used += 1
        last_request[host] = time.monotonic()
        response = fetcher(url, byte_limit)
        if not isinstance(response, Response) or type(response.status) is not int or not isinstance(response.body, bytes):
            raise FetchError('invalid-transport-response')
        if len(response.body) > byte_limit:
            raise FetchError('response-too-large')
        return response

    conn = open_state(state_path)
    try:
        conn.execute('BEGIN IMMEDIATE')
        prior = conn.execute('SELECT manifest_hash, mode FROM watch_cycles WHERE cycle=?', (cycle,)).fetchone()
        if prior and prior != (binding, mode):
            raise ValueError('cycle-manifest-or-mode-changed')
        if not prior:
            if conn.execute('SELECT count(*) FROM watch_cycles').fetchone()[0] >= 1000:
                raise ValueError('cycle-storage-capacity')
            conn.execute('INSERT INTO watch_cycles VALUES (?, ?, ?)', (cycle, binding, mode))
            conn.executemany('INSERT INTO watch_tasks VALUES (?, ?, NULL)',
                             [(cycle, s['id']) for s in manifest['sources']])
        conn.commit()
        for source in manifest['sources']:
            # Reserve both robots and source requests; never discard the queue.
            if budget - used < 2:
                break
            conn.execute('BEGIN IMMEDIATE')
            done = conn.execute('SELECT result FROM watch_tasks WHERE cycle=? AND source_id=?',
                                (cycle, source['id'])).fetchone()[0]
            if done is not None:
                conn.commit()
                continue
            if conn.execute('SELECT count(*) FROM watch_snapshots').fetchone()[0] >= MAX_SNAPSHOTS:
                raise ValueError('snapshot-storage-capacity')
            item = {'source_id': source['id'], 'repositories': source['repositories'],
                    'requested_url': source['url'], 'outcome': 'failed', 'stage': 'robots'}
            try:
                delay = _source_response(source, fetch, limit)
                host_delays[urlsplit(source['url']).hostname] = delay
                item['stage'] = 'source'
                response = fetch(source['url'], limit)
                item['status'] = response.status
                if response.status != 200:
                    raise FetchError('source-http-error')
                item['stage'] = 'extraction'
                text = meaningful_text(response.body, response.content_type)
                scope = digest(canonical(source).encode())
                previous = conn.execute('''SELECT raw_sha256, text_sha256, extracted FROM watch_snapshots
                    WHERE scope_hash=? AND mode=? AND extractor=? ORDER BY id DESC LIMIT 1''',
                    (scope, mode, EXTRACTOR_VERSION)).fetchone()
                raw_hash, text_hash = digest(response.body), digest(text.encode())
                outcome = ('baseline' if previous is None else
                           'unchanged' if previous[1] == text_hash else 'changed')
                diff = (''.join(difflib.unified_diff([line + '\n' for line in previous[2].splitlines()],
                                                   [line + '\n' for line in text.splitlines()],
                                                   fromfile='previous', tofile='current'))
                        if outcome == 'changed' else '')
                collected = now()
                cur = conn.execute('''INSERT INTO watch_snapshots
                    (cycle, source_id, scope_hash, mode, extractor, collected_at, raw_sha256,
                     text_sha256, raw, extracted) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)''',
                    (cycle, source['id'], scope, mode, EXTRACTOR_VERSION, collected,
                     raw_hash, text_hash, response.body, text))
                item.update(outcome=outcome, stage='recorded', final_url=source['url'], collected_at=collected,
                            status=response.status, content_type=response.content_type,
                            bytes=len(response.body), snapshot_id=cur.lastrowid,
                            extractor_version=EXTRACTOR_VERSION, raw_sha256=raw_hash,
                            text_sha256=text_hash, previous_raw_sha256=previous[0] if previous else None,
                            diff=diff[:20_000], diff_truncated=len(diff) > 20_000)
            except (ValueError, UnicodeError, RecursionError) as error:
                safe_codes = {'robots-denied', 'robots-unavailable', 'robots-invalid-content',
                              'robots-delay-exceeds-budget', 'source-http-error', 'redirect-blocked',
                              'response-too-large', 'unsupported-content-type', 'unsupported-charset',
                              'empty-extracted-content', 'compressed-response-rejected', 'transport-failed',
                              'non-public-destination', 'dns-no-public-address', 'dns-failed',
                              'url-not-approved', 'invalid-transport-response'}
                safe_codes.add('incomplete-response')
                item.update(error=str(error) if str(error) in safe_codes else 'invalid-source-content',
                            collected_at=now())
            conn.execute('UPDATE watch_tasks SET result=? WHERE cycle=? AND source_id=?',
                         (canonical(item), cycle, source['id']))
            conn.commit()
        rows = {sid: json.loads(result) if result else None for sid, result in conn.execute(
            'SELECT source_id, result FROM watch_tasks WHERE cycle=?', (cycle,))}
        items = [rows[s['id']] or {'source_id': s['id'], 'requested_url': s['url'],
                                  'repositories': s['repositories'], 'outcome': 'pending'}
                 for s in manifest['sources']]
        status = ('pending' if any(x['outcome'] == 'pending' for x in items) else
                  'failed' if any(x['outcome'] == 'failed' for x in items) else 'complete')
        return {'schema': 1, 'application': 'engineering-evidence-watch', 'cycle': cycle,
                'manifest_sha256': binding, 'collection_mode': mode, 'observed_at': now(),
                'status': status, 'requests_used': used, 'sources': items,
                'authority_granted': False, 'live_collection_verified': False,
                'production_ready': False,
                'limits': 'Collection evidence only; source content is untrusted. No task execution, approval or publication.'}
    finally:
        conn.close()


class FixtureFetcher:
    mode = 'fixture'

    def __init__(self, fixture):
        self.fixture = fixture

    def __call__(self, url, limit):
        entry = self.fixture.get(url)
        if not isinstance(entry, dict) or set(entry) != {'status', 'content_type', 'body'}:
            raise FetchError('invalid-transport-response')
        if not isinstance(entry['body'], str) or not isinstance(entry['content_type'], str):
            raise FetchError('invalid-transport-response')
        return Response(entry['status'], entry['content_type'], entry['body'].encode())


def read_json(path, limit):
    with open(path, 'rb') as handle:
        data = handle.read(limit + 1)
    if len(data) > limit:
        raise ValueError('input-too-large')
    return strict_json(data.decode('utf-8'))


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('manifest')
    parser.add_argument('--state', required=True)
    parser.add_argument('--cycle', required=True)
    mode = parser.add_mutually_exclusive_group(required=True)
    mode.add_argument('--fixtures', help='Local fixture JSON; no networking')
    mode.add_argument('--collect', action='store_true', help='Explicit live HTTPS collection')
    args = parser.parse_args()
    old_handler = None
    try:
        if args.collect and not hasattr(signal, 'SIGALRM'):
            raise ValueError('live-mode-requires-posix-deadline')
        if hasattr(signal, 'SIGALRM'):
            def expired(signum, frame):
                raise TimeoutError('cycle-deadline')
            old_handler = signal.signal(signal.SIGALRM, expired)
            signal.alarm(900)
        manifest = validate_manifest(read_json(args.manifest, 65_536))
        if args.fixtures:
            fixtures = read_json(args.fixtures, 2_000_000)
            if not isinstance(fixtures, dict):
                raise ValueError('invalid-fixtures')
            fetcher = FixtureFetcher(fixtures)
        else:
            fetcher = SafeFetcher({s['url'] for s in manifest['sources']} |
                                  {robots_url(s['url']) for s in manifest['sources']})
        report = run_watch(manifest, args.state, args.cycle, fetcher)
        print(json.dumps(report, sort_keys=True, allow_nan=False))
        return 0 if report['status'] == 'complete' else 1
    except (OSError, ValueError, sqlite3.Error, RecursionError):
        print(json.dumps({'error': 'watch-run-failed', 'guidance': 'Check bounded inputs and state; resume the same cycle for unfinished work.'}))
        return 2
    finally:
        if old_handler is not None:
            signal.alarm(0)
            signal.signal(signal.SIGALRM, old_handler)


if __name__ == '__main__':
    sys.exit(main())
