# Source-change monitor

## Intent

Produce an auditable brief of content changes across a small, declared set of
public sources. Each run records an outcome for every source, preserves the last
successful observation, and provides evidence for reviewing a change.

The monitor is an opt-in command in the existing crawler repository. It uses the
Python standard library and requires no model API, account, or subscription.

## Run a brief

From the repository root:

```bash
python3 -m crawler.monitor \
  --sources examples/source-monitor.json \
  --state-db artifacts/source-monitor/state.sqlite \
  --report artifacts/source-monitor/brief.json \
  --markdown artifacts/source-monitor/brief.md
```

The example watches three HTML pages about AI-assisted software development.
The first successful observation establishes a baseline. Run the same command
later to compare against the last successful observation. Unchanged content does
not produce another change event.

This command runs on demand. It does not install a service or create a schedule.
The existing link-mapping CLI and its crawl-resume state remain separate.

The `Source monitor live verification` GitHub workflow runs on manual dispatch
and on pull requests that change the monitor or its example configuration. It
fetches two observations with an ephemeral database and retains the JSON and
Markdown evidence for 30 days. A retrieval failure fails that live check; it does
not disappear behind the isolated unit tests. The workflow does not preserve a
baseline across workflow runs or install a recurring monitor.

## Configure sources

The source file has this structure:

```json
{
  "schema_version": 1,
  "sources": [
    {
      "id": "public-document",
      "url": "https://example.com/document",
      "label": "Public document"
    }
  ]
}
```

Use stable, unique IDs and distinct URLs. Labels are optional. The limit is
25 sources. The monitor validates the configuration before retrieval.
IDs use 1–64 letters, digits, underscores, or hyphens and begin with a letter or
digit. Unknown configuration fields are rejected.

URLs must use HTTPS on port 443. Credentials and fragments are not allowed.
Resolved addresses must be public. The transport connects to a validated address
and verifies TLS for the original hostname. Redirects become visible errors;
they do not expand the permitted fetch destinations. If a publisher moves a
document, review the destination and update the source file. Changing the URL
for an existing ID establishes a new baseline.

The monitor also requests each source origin's `robots.txt` through the same
bounded transport. A missing robots file (HTTP 404 or 410) permits retrieval.
Disallow rules, HTTP 403, redirects, and failed robots retrievals prevent the
source fetch and appear in the report.
Recognized `Crawl-delay` values are honored up to 10 seconds. A larger delay
produces an error instead of exceeding that wait limit.

By default, a run uses a 10-second request timeout, a 1,000,000-byte response
limit, and a request budget of twice the number of sources. Requests are
sequential. Robots requests count toward the budget. The timeout is a request
setting, not a promise that the entire run will finish within 10 seconds.

## Read the result

The JSON report is the machine-readable record. The Markdown brief provides a
readable view of the same run. Both identify the run and its observation times.

| Source state | Meaning |
| --- | --- |
| `baseline` | First successful content observation for this source URL. |
| `changed` | Normalized content differs from the last successful observation. |
| `unchanged` | Normalized content is equal, or a valid conditional response confirms the existing baseline. |
| `error` | Retrieval, policy, or content processing did not produce a usable observation. |

Each source row includes its ID, label, URL, check time, and HTTP status when
available. Successful observations include a content digest. Changes include the
previous digest and bounded added/removed text evidence. An evidence truncation
flag means the displayed difference is only an excerpt. A reported publication
date, when present, comes from source metadata and is not independently verified.

The `sha256` digest represents normalized content, rather than the original
response bytes. HTML normalization excludes the head, title, scripts, styles,
navigation, headers, and footers. HTML and plain text use Unicode NFC
normalization and collapse whitespace. JSON is compared in canonical form while
preserving its values. Changes confined to excluded title/date metadata do not
create a content-change classification. When a response body is fetched,
`raw_sha256` also records its digest; a valid HTTP 304 reuses the last successful
body's evidence and requires that a conditional validator was sent.

This detects static text changes, not their business meaning. Visual changes,
link-target changes, or content hidden by client-side behavior are not fully
represented. Review the source and the evidence before deciding what action a
change warrants.

The monitor processes static HTML, JSON, and plain text. It does not render
JavaScript, follow discovered links, load images, execute scripts, or interpret
page instructions as commands. PDFs and other unsupported formats produce an
error. Content available only after client-side rendering may be absent from an
otherwise successful HTML observation.

Fetched text remains untrusted data even when included in a report. Markdown
output escapes source-provided content. If a brief is later given to an AI
assistant, keep its excerpts separate from trusted instructions and permission
decisions.

### Run status and exit codes

| Result | Run status | Exit code |
| --- | --- | ---: |
| Every source has a successful observation | `ok` | 0 |
| Some sources fail | `partial` | 2 |
| Every source fails | `failed` | 2 |
| Invalid configuration, storage failure, or export failure | No successful command completion | 1 |

Malformed command-line usage follows the argument parser's exit code 2. The
configuration error above refers to a source file or a validated run setting.

A source failure does not erase its successful baseline. Successful sources may
advance during a partial run. On recovery, a failed source is compared with its
last successful content rather than with an error page or an empty placeholder.

## Recover or export an earlier run

The SQLite database stores source baselines and each run's report in one
transaction. `monitor_snapshots` holds each source's latest successful metadata;
`monitor_contents` retains full normalized historical text keyed by its digest;
`monitor_runs` retains immutable report records. Original response bodies are
not stored. A failed transaction does not partially advance the baselines.

An immediate write transaction protects the comparison and update across a run.
A competing writer to the same database is rejected with a storage error rather
than waiting or overwriting a baseline based on stale state. Retry after the
active run finishes. JSON and Markdown files are derived exports, each written
atomically.

If a file export fails after the database commit, recover the recorded report
without retrieving the sources again:

```bash
python3 -m crawler.monitor \
  --state-db artifacts/source-monitor/state.sqlite \
  --export-run latest \
  --report artifacts/source-monitor/brief.json \
  --markdown artifacts/source-monitor/brief.md
```

Replace `latest` with the report's run ID to export that exact observation. Keep
the run IDs together when comparing JSON and Markdown: the two output files are
not a single cross-file transaction. Re-exporting the selected run repairs an
interrupted or mismatched pair.

Preserve the database when retrying failed sources. Removing it discards the
comparison history and makes the next successful retrieval a new baseline.

### Backup, retention, and rollback

- Back up the database while no monitor process is writing to it, or use the
  SQLite backup API for an online backup. Preserve the database with its required
  SQLite sidecar files when applicable.
- Baselines and reports remain until the operator removes or archives the
  database. There is no automatic retention policy in this pilot. Check storage
  growth and archive completed histories according to the use case.
- Keep generated databases and briefs outside version control. The documented
  `artifacts/source-monitor/` directory is ignored by Git.
- To stop monitoring, stop invoking the command. To roll back the feature, revert
  its code change after preserving any reports needed for review. Keep the
  database backup so observations remain recoverable with a compatible version.

## Responsibility and acceptance

The source owner chooses the URLs and decides which changes matter. The
implementation performs the declared retrieval and comparison. The reviewer
checks behavior, evidence, and failure handling. The operator owns the state
database and recovery procedure. Existing authorization determines who can
change configuration or use the output.

The pilot is ready for operational evaluation when these checks pass:

- Every configured source has a visible outcome and traceable observation.
- An unchanged rerun emits no new change classification.
- A real content change includes before/after digests and useful evidence.
- Failed, incomplete, or unsupported responses preserve the last good baseline.
- Source content cannot add destinations, execute commands, or alter policy.
- A saved run can be exported again after an export failure without network I/O.
- Existing crawl behavior and tests still pass.

Run the checks documented in `AGENTS.md`. Isolated tests establish local
behavior; live retrieval separately establishes which configured sources are
accessible in the operating environment.

Each run records request count, returned response-body bytes, elapsed
milliseconds, and model calls. The byte measure excludes transport headers and
data discarded inside a failed transport, such as an oversized response. Model
calls are zero for this implementation. These measures do not include operator
review time, hosting charges, or other organizational costs. Record review
effort and whether the brief informed a real decision before expanding the
source set or adding automation.
