# Engineering Evidence Watch

This is a bounded, read-only collection component for GovDOSS Engineering
Operations. It produces traceable observations and changes for a reviewer. It
does not open issues/PRs, invoke models, execute fetched instructions, change
repository policy, schedule itself, approve releases or publish a website.

## Offline demonstration

From the repository root, using Python 3.11 or newer:

```sh
python -m crawler.watch examples/watch/sources.json --state /tmp/govdoss-watch-demo.sqlite --cycle demo-1 --fixtures examples/watch/baseline.json
python -m crawler.watch examples/watch/sources.json --state /tmp/govdoss-watch-demo.sqlite --cycle demo-2 --fixtures examples/watch/noise-only.json
python -m crawler.watch examples/watch/sources.json --state /tmp/govdoss-watch-demo.sqlite --cycle demo-3 --fixtures examples/watch/changed.json
```

These synthetic example.com records demonstrate `baseline`, `unchanged` despite
navigation/formatting changes, then `changed` with a diff and repository mapping.
The example URLs are not a live source enrollment. Fixture mode performs no
network requests. Fixture and live observations can never share a baseline.

Each stdout record is JSON. Exit 0 means the cycle finished with successful
observations; exit 1 means pending work or source failures; exit 2 means an input,
state, capacity, deadline or infrastructure error. None establishes release
authority, evidence truth, business effectiveness or production certification.

## Source contract and collection boundary

The schema 1 manifest contains exactly `schema`, `max_requests_per_run`,
`max_bytes_per_response` and `sources`. Each source has a unique lowercase `id`,
an exact `url` and one or more `repositories` from the final six-project portfolio.
Limits: 1–50 sources, 2–100 HTTP requests per invocation, and 1 KiB–1 MiB per
response. A source requires up to two requests: robots.txt, then the source.
Both count toward the request budget, including failures. The complete manifest
is limited to 64 KiB. Credentials and customer information do not belong in it.

Live collection is explicit:

```sh
python -m crawler.watch reviewed-sources.json --state engineering-watch.sqlite --cycle operator-cycle-id --collect
```

Use a reviewed manifest of authorized, static public sources. The JSON is an
operator-supplied allowlist, not authentication or proof of permission. Do not
copy example URLs into a production collection schedule. Before adding a source,
review its suitability, permitted access, published access guidance, relevant
repository and extraction behavior against representative content.

The restricted transport accepts only exact HTTPS URLs on port 443, ASCII DNS
hostnames, and paths without query strings, fragments, userinfo or IP literals.
It rejects private, shared, multicast, reserved and mixed public/private DNS
answers. The initial profile is IPv4-only. It resolves once, connects to that
validated address and verifies TLS using the original hostname. Environment
proxies, custom authorization headers, cookies and credentials are not used.

Every redirect is rejected; there is no inherited redirect trust. The only
derived request is `/robots.txt` on the exact source origin. Robots denial or
unavailability fails closed, except 404 means no robots rules. Accepted robots
delays/request rates are honored within a run; delays over 30 seconds are rejected
for explicit follow-up. A 30-second startup pause bounds immediate restart
bursts. Requests are sequential. The transport applies a 10-second socket timeout
and the POSIX CLI enforces a 15-minute wall-clock deadline, including DNS and
extraction. Hosts embedding `run_watch` must supply their own process deadline.

No sitemap discovery, linked-page fetching, browser rendering, subrequests,
external model endpoints or retries are included in this profile. Compressed,
oversized, truncated, non-UTF-8 and unsupported responses are rejected rather
than accepted as complete evidence. The legacy crawler CLI remains separate;
these controls do not retrofit its transport or establish its security posture.

## Evidence, extraction and change review

For each successful observation the SQLite state retains exact response bytes,
raw SHA256, extracted text and SHA256, collection time, extractor version, source
scope hash, collection mode and cycle. The JSON receipt links that snapshot to
the requested/final URL, status, content type, size and affected repositories.
Final URL equals the requested URL because redirects are never followed.

HTML extraction prefers main content, then article content, then the available
document text. It excludes navigation, footer, aside, head, script, style,
template, SVG, noscript and explicit hidden/aria-hidden content. It collapses
ordinary layout whitespace while preserving preformatted code indentation and
link targets. Plain text preserves indentation. JSON normalizes key order and
formatting; duplicate keys and non-finite numbers are invalid.

This is structural normalization, not semantic understanding. CSS-only hiding,
malformed HTML and unusual page structure can affect extraction. Relevant changes
outside the selected main/article content can be omitted from the text diff.
Raw bytes are retained for inspection; a reviewer should calibrate each source.
`unchanged` means unchanged under the stated extractor, not proof that nothing
on the site changed. Diffs are capped at 20,000 characters and explicitly mark
truncation. Downstream renderers must escape source text and never execute it.

Reports distinguish baseline, changed, unchanged, pending and failed sources.
Failures include stage and a stable error code without copying sensitive error
details. The last successful baseline survives a later failed fetch. A changed
source mapping, collection mode or extractor version starts a new baseline.

## Recovery and operating ownership

One cycle ID binds to one manifest hash and collection mode. Reusing a cycle
resumes pending sources; completed sources are not fetched again. Reusing it
with different input is rejected. Start a new cycle to retry a recorded source
failure or observe later content. A transaction commits each snapshot and task
receipt together. Hard process exit rolls back the unfinished source; earlier
committed work remains. A GET interrupted before commit may repeat on resume.

The SQLite writer lock serializes workers. An overlapping worker reports a state
error without fetching; it must be rescheduled by the existing operator. Keep
this state on a durable local filesystem, with one accountable operating owner.
Do not use a shared network filesystem or treat this as a multi-tenant service.
An unrelated SQLite database or symlink is rejected. Database capacity is bounded
to 500 snapshots and 1,000 cycles; exhaustion stops collection before a request.
The owner must archive the database and begin a new one after checking retention
requirements. There is no automatic evidence deletion or backup/restore service.

The existing workspace operator owns observation timing and source review. This
patch adds no automation. When an approved scheduled cycle is introduced, use a
new cycle ID, record exit status, preserve unfinished work, and notify only on
meaningful changes or collection failures. Never attach this command to CI in
live mode. CI runs offline fixture and transport-boundary tests only.

## Production promotion prerequisites

This change is a review candidate until merged and checked on its exact GitHub
head. The two-active-maintenance-PR cap still applies. Before internal live use,
review the actual source list, verify permitted network access and TLS behavior
in the deployment environment, designate runtime/state ownership, test backup
and restore, define retention and monitoring, and authorize that deployment
target. No live source was collected as part of the offline acceptance suite.

## Verification

```sh
python -m unittest test_crawler tests.test_crawler tests.test_watch -v
python -m compileall -q crawler crawler.py self_healing.py
ruff check .
```

Contracts cover meaningful changes versus navigation noise, provenance hashes,
code indentation, JSON normalization, scope validation, robots denial, request
and byte budgets, live/fixture separation, hard process termination and resume,
database ownership/capacity, concurrent writer rejection, pinned destination/TLS
hostname behavior, redirects, mixed/private DNS and unapproved URL rejection.
Transport boundary tests use doubles; they are not live production network tests.
