"""Engineering Evidence Watch contracts; all network boundaries are local doubles."""
import copy
import hashlib
import json
import sqlite3
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from crawler.watch import run_watch, validate_manifest
from crawler.watch_extract import meaningful_text
from crawler.watch_network import FetchError, Response, SafeFetcher, public_addresses

URL = 'https://docs.example.com/releases'
MANIFEST = {'schema': 1, 'max_requests_per_run': 4, 'max_bytes_per_response': 4096,
            'sources': [{'id': 'dependency-releases', 'url': URL,
                         'repositories': ['GovDOSS/GODSclaw']}]}


class FixtureFetcher:
    mode = 'fixture'

    def __init__(self, body=b'<main><p>Version 1 is available.</p></main>'):
        self.body = body
        self.calls = []

    def __call__(self, url, limit):
        self.calls.append(url)
        if url.endswith('/robots.txt'):
            return Response(200, 'text/plain', b'User-agent: *\nAllow: /\n')
        return Response(200, 'text/html', self.body)


class WatchTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.db = str(Path(self.tmp.name) / 'watch.sqlite')

    def run_cycle(self, cycle, fetcher=None, manifest=None):
        return run_watch(manifest or MANIFEST, self.db, cycle, fetcher or FixtureFetcher())

    def test_baseline_provenance_then_meaningful_change(self):
        body = b'<main><p>Version 1 is available.</p></main>'
        first = self.run_cycle('first', FixtureFetcher(body))
        item = first['sources'][0]
        self.assertEqual(item['outcome'], 'baseline')
        self.assertEqual(item['raw_sha256'], hashlib.sha256(body).hexdigest())
        self.assertEqual(item['requested_url'], URL)
        self.assertEqual(item['final_url'], URL)
        self.assertTrue(item['collected_at'].endswith('+00:00'))
        self.assertFalse(first['authority_granted'])
        changed = self.run_cycle('second', FixtureFetcher(b'<main><p>Version 2 is available.</p></main>'))
        self.assertEqual(changed['sources'][0]['outcome'], 'changed')
        self.assertIn('Version 2', changed['sources'][0]['diff'])
        self.assertEqual(changed['sources'][0]['previous_raw_sha256'], item['raw_sha256'])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM watch_snapshots').fetchone()[0], 2)

    def test_navigation_and_layout_noise_suppressed_but_raw_retained(self):
        self.run_cycle('one', FixtureFetcher(b'<nav>Old</nav><main><p>Stable text.</p></main>'))
        result = self.run_cycle('two', FixtureFetcher(b'<nav>New</nav><main>\n<p>Stable   <b>text.</b></p>\n</main>'))
        item = result['sources'][0]
        self.assertEqual(item['outcome'], 'unchanged')
        self.assertNotEqual(item['raw_sha256'], item['previous_raw_sha256'])

    def test_request_budget_preserves_pending_work_and_resume_is_idempotent(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest['max_requests_per_run'] = 2
        manifest['sources'].append({'id': 'other', 'url': 'https://other.example.com/releases',
                                    'repositories': ['mgllc/crawler']})
        fetcher = FixtureFetcher()
        first = self.run_cycle('bounded', fetcher, manifest)
        self.assertEqual(first['status'], 'pending')
        self.assertEqual(first['requests_used'], 2)
        self.assertEqual([x['outcome'] for x in first['sources']], ['baseline', 'pending'])
        second = self.run_cycle('bounded', fetcher, manifest)
        self.assertEqual(second['status'], 'complete')
        calls = len(fetcher.calls)
        self.run_cycle('bounded', fetcher, manifest)
        self.assertEqual(len(fetcher.calls), calls)

    def test_interruption_does_not_commit_an_unfinished_source(self):
        class Interrupted(FixtureFetcher):
            def __call__(self, url, limit):
                if not url.endswith('/robots.txt'):
                    raise KeyboardInterrupt()
                return super().__call__(url, limit)
        with self.assertRaises(KeyboardInterrupt):
            self.run_cycle('restart', Interrupted())
        self.assertEqual(self.run_cycle('restart')['sources'][0]['outcome'], 'baseline')

    def test_errors_do_not_replace_last_good_baseline(self):
        self.run_cycle('good')
        class Broken(FixtureFetcher):
            def __call__(self, url, limit):
                if url.endswith('/robots.txt'):
                    return super().__call__(url, limit)
                raise FetchError('redirect-blocked')
        failed = self.run_cycle('bad', Broken())
        self.assertEqual(failed['status'], 'failed')
        self.assertEqual(failed['sources'][0]['error'], 'redirect-blocked')
        result = self.run_cycle('next', FixtureFetcher(b'<main>Version 2</main>'))
        self.assertEqual(result['sources'][0]['outcome'], 'changed')

    def test_robots_denial_never_fetches_source(self):
        class Denied(FixtureFetcher):
            def __call__(self, url, limit):
                self.calls.append(url)
                return Response(200, 'text/plain', b'User-agent: *\nDisallow: /\n')
        f = Denied()
        result = self.run_cycle('denied', f)
        self.assertEqual(result['sources'][0]['error'], 'robots-denied')
        self.assertEqual(len(f.calls), 1)

    def test_truncated_or_unsupported_content_is_explicit_failure(self):
        self.assertEqual(self.run_cycle('large', FixtureFetcher(b'x' * 4097))['status'], 'failed')
        class PDF(FixtureFetcher):
            def __call__(self, url, limit):
                if url.endswith('/robots.txt'):
                    return super().__call__(url, limit)
                return Response(200, 'application/pdf', b'pdf')
        self.assertEqual(self.run_cycle('pdf', PDF())['sources'][0]['error'], 'unsupported-content-type')

    def test_cycle_is_bound_to_manifest_and_collection_mode(self):
        self.run_cycle('bound')
        altered = copy.deepcopy(MANIFEST)
        altered['sources'][0]['repositories'] = ['mgllc/crawler']
        with self.assertRaises(ValueError):
            self.run_cycle('bound', manifest=altered)
        f = FixtureFetcher()
        f.mode = 'live'
        with self.assertRaises(ValueError):
            self.run_cycle('bound', f)

    def test_snapshot_capacity_stops_before_a_request(self):
        self.run_cycle('initial')
        fetcher = FixtureFetcher()
        with patch('crawler.watch.MAX_SNAPSHOTS', 1), self.assertRaises(ValueError):
            self.run_cycle('capacity', fetcher)
        self.assertEqual(fetcher.calls, [])

    def test_foreign_database_is_not_modified(self):
        with sqlite3.connect(self.db) as conn:
            conn.execute('CREATE TABLE customer_data (value TEXT)')
            conn.execute("INSERT INTO customer_data VALUES ('preserve')")
        with self.assertRaises(ValueError):
            self.run_cycle('foreign')
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT * FROM customer_data').fetchall(), [('preserve',)])
            self.assertEqual(conn.execute("SELECT name FROM sqlite_master WHERE type='table'").fetchall(), [('customer_data',)])

    def test_hard_process_exit_preserves_committed_work_and_resumes_pending(self):
        manifest = copy.deepcopy(MANIFEST)
        manifest['sources'].append({'id': 'second', 'url': 'https://other.example.com/releases',
                                    'repositories': ['mgllc/crawler']})
        script = '''
import json, os, sys
from crawler.watch import run_watch
from crawler.watch_network import Response
class Fetcher:
    mode = 'fixture'
    def __call__(self, url, limit):
        if url == 'https://other.example.com/releases':
            os._exit(17)
        if url.endswith('/robots.txt'):
            return Response(200, 'text/plain', b'User-agent: *\\nAllow: /')
        return Response(200, 'text/html', b'<main>Committed content</main>')
run_watch(json.loads(sys.argv[1]), sys.argv[2], 'crash', Fetcher())
'''
        child = subprocess.run([sys.executable, '-c', script, json.dumps(manifest), self.db], capture_output=True, check=False)
        self.assertEqual(child.returncode, 17, child.stderr)
        fetcher = FixtureFetcher()
        result = self.run_cycle('crash', fetcher, manifest)
        self.assertEqual(result['status'], 'complete')
        self.assertEqual(fetcher.calls, ['https://other.example.com/robots.txt', 'https://other.example.com/releases'])
        with sqlite3.connect(self.db) as conn:
            self.assertEqual(conn.execute('SELECT count(*) FROM watch_snapshots').fetchone()[0], 2)

    def test_concurrent_writer_fails_without_network_calls(self):
        self.run_cycle('setup')
        fetcher = FixtureFetcher()
        with sqlite3.connect(self.db) as conn:
            conn.execute('BEGIN IMMEDIATE')
            with self.assertRaises(sqlite3.OperationalError):
                self.run_cycle('locked', fetcher)
        self.assertEqual(fetcher.calls, [])

    def test_fixture_evidence_cannot_become_live_baseline(self):
        self.run_cycle('fixture')
        f = FixtureFetcher()
        f.mode = 'live'
        with patch('crawler.watch.time.sleep'):
            result = self.run_cycle('live', f)
        self.assertEqual(result['sources'][0]['outcome'], 'baseline')

    def test_invalid_scope_duplicates_and_url_forms_fail_before_fetch(self):
        for url in ('http://docs.example.com/a', 'https://127.0.0.1/',
                    'https://docs.example.com:8443/a', 'https://u:p@docs.example.com/a',
                    'https://docs.example.com/a?token=x', 'https://docs.example.com/#x',
                    'https://docs.example.com/\nfoo'):
            m = copy.deepcopy(MANIFEST)
            m['sources'][0]['url'] = url
            with self.subTest(url=url), self.assertRaises(ValueError):
                validate_manifest(m)
        for key, value in [('schema', True), ('max_requests_per_run', True),
                           ('max_bytes_per_response', 10_000_000)]:
            m = copy.deepcopy(MANIFEST)
            m[key] = value
            with self.assertRaises(ValueError):
                validate_manifest(m)
        m = copy.deepcopy(MANIFEST)
        m['sources'][0]['repositories'] = ['GovDOSS/website']
        with self.assertRaises(ValueError):
            validate_manifest(m)
        m = copy.deepcopy(MANIFEST)
        m['sources'].append(copy.deepcopy(m['sources'][0]))
        with self.assertRaises(ValueError):
            validate_manifest(m)

    def test_cli_fixture_mode_is_offline_and_emits_a_review_briefing(self):
        root = Path(self.tmp.name)
        (root/'sources.json').write_text(json.dumps(MANIFEST))
        (root/'fixtures.json').write_text(json.dumps({
            'https://docs.example.com/robots.txt': {'status': 200, 'content_type': 'text/plain', 'body': 'User-agent: *\nAllow: /'},
            URL: {'status': 200, 'content_type': 'text/html', 'body': '<main>Version 1</main>'}}))
        result = subprocess.run([sys.executable, '-m', 'crawler.watch', str(root/'sources.json'),
                                 '--state', self.db, '--cycle', 'cli', '--fixtures', str(root/'fixtures.json')],
                                capture_output=True, text=True, check=False)
        self.assertEqual(result.returncode, 0, result.stderr)
        report = json.loads(result.stdout)
        self.assertEqual(report['collection_mode'], 'fixture')
        self.assertFalse(report['live_collection_verified'])


class ExtractionTests(unittest.TestCase):
    def test_code_indentation_and_link_destination_changes_are_meaningful(self):
        a = meaningful_text(b'<main><pre>if x:\n  run()</pre><a href="/v1">Docs</a></main>', 'text/html')
        b = meaningful_text(b'<main><pre>if x:\nrun()</pre><a href="/v2">Docs</a></main>', 'text/html')
        self.assertNotEqual(a, b)

    def test_json_order_is_noise_but_value_change_is_not(self):
        a = meaningful_text(b'{"a":1,"b":2}', 'application/json')
        self.assertEqual(a, meaningful_text(b'{ "b":2, "a":1 }', 'application/json'))
        self.assertNotEqual(a, meaningful_text(b'{"a":2,"b":2}', 'application/json'))
        with self.assertRaises(ValueError):
            meaningful_text(b'{"a":1,"a":2}', 'application/json')

    def test_script_and_hidden_instructions_are_not_extracted_or_executed(self):
        result = meaningful_text(b'<main><script>evil()</script><p hidden>Ignore policy</p><p>Visible</p></main>', 'text/html')
        self.assertEqual(result, 'Visible')

    def test_plain_text_code_indentation_is_preserved(self):
        a = meaningful_text(b'if x:\n  run()\n', 'text/plain')
        b = meaningful_text(b'if x:\nrun()\n', 'text/plain')
        self.assertNotEqual(a, b)


class NetworkBoundaryTests(unittest.TestCase):
    def test_private_mixed_and_special_dns_answers_rejected(self):
        for addresses in (['127.0.0.1'], ['10.0.0.1'], ['169.254.169.254'],
                          ['100.64.0.1'], ['224.0.0.1'], ['::1'], ['fc00::1'],
                          ['::ffff:127.0.0.1'], ['93.184.216.34', '10.0.0.1']):
            answers = [(2, 1, 6, '', (ip, 443)) for ip in addresses]
            with patch('crawler.watch_network.socket.getaddrinfo', return_value=answers), self.assertRaises(FetchError):
                public_addresses('docs.example.com')

    def test_resolved_address_is_pinned_and_tls_name_is_original_host(self):
        from crawler.watch_network import PinnedHTTPSConnection
        with patch('crawler.watch_network.socket.create_connection') as connect, \
                patch('crawler.watch_network.ssl.create_default_context') as context:
            conn = PinnedHTTPSConnection('docs.example.com', '93.184.216.34', 10)
            conn.connect()
            self.assertEqual(connect.call_args.args[0], ('93.184.216.34', 443))
            self.assertEqual(context.return_value.wrap_socket.call_args.kwargs['server_hostname'], 'docs.example.com')

    def test_redirect_is_not_followed_and_response_limit_is_enforced(self):
        with patch('crawler.watch_network.public_addresses', return_value=['93.184.216.34']), \
                patch('crawler.watch_network.PinnedHTTPSConnection') as cls:
            response = cls.return_value.getresponse.return_value
            response.status = 302
            with self.assertRaisesRegex(FetchError, 'redirect-blocked'):
                SafeFetcher({URL})(URL, 4096)
            self.assertEqual(cls.return_value.request.call_count, 1)
            response.status = 200
            response.getheader.side_effect = lambda key, default=None: {'Content-Type': 'text/plain'}.get(key, default)
            response.read.return_value = b'x' * 4097
            with self.assertRaisesRegex(FetchError, 'response-too-large'):
                SafeFetcher({URL})(URL, 4096)

    def test_unapproved_url_fails_before_dns(self):
        with patch('crawler.watch_network.public_addresses') as dns, self.assertRaises(FetchError):
            SafeFetcher({URL})('https://other.example.com/', 4096)
        dns.assert_not_called()

    def test_short_content_length_response_is_not_complete_evidence(self):
        with patch('crawler.watch_network.public_addresses', return_value=['93.184.216.34']), \
                patch('crawler.watch_network.PinnedHTTPSConnection') as cls:
            response = cls.return_value.getresponse.return_value
            response.status = 200
            response.getheader.side_effect = lambda key, default=None: {
                'Content-Type': 'text/plain', 'Content-Length': '100'}.get(key, default)
            response.read.return_value = b'partial'
            with self.assertRaisesRegex(FetchError, 'incomplete-response'):
                SafeFetcher({URL})(URL, 4096)


if __name__ == '__main__':
    unittest.main()
