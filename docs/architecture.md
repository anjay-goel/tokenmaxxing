# Architecture

Tokenmaxxing is a local, metadata-only accounting pipeline. Each supported
harness has its own source parser, while storage and reporting use one shared
SQLite model.

## Ingestion lifecycle

Every importer follows the same six stages:

1. Discover source artifacts.
2. Read complete, valid records.
3. Project only privacy-safe metadata.
4. Identify changed semantic events.
5. Reconcile observations into counted or uncounted events.
6. Return sync statistics.

JSONL importers store byte offsets and process only newline-terminated records.
Truncation, replacement, inode changes, and header changes create a new artifact
generation. Reconciliation includes retained observations from older generations
and missing files. Artifact availability controls scanning, not historical
retention. OpenCode reads a stable V1 SQLite snapshot in query-only mode,
including committed WAL data, and retains imported events after source deletion.

## Source identities

Identity is source-scoped. A timestamp alone never identifies an event.

| Source | Counted identity | Duplicate handling |
| --- | --- | --- |
| Codex | owner and fingerprint of timestamp plus full counter usage, with repeated-call occurrence | Physical counter streams are reconciled before copied observations collapse |
| Claude | global message identity and iteration index | Progressive and copied snapshots reduce component-wise |
| Pi direct | lineage root, entry identity, and usage slot | Copied observations reduce to one event |
| Pi subagent | batch-qualified or lineage-qualified run identity | Progressive snapshots reduce to one run aggregate |
| OpenCode | completed part identity, or assistant message fallback | Retained message and part observations select one coherent representation |

Child Codex and OpenCode activity remains one model call per source identity.
Root session attribution groups the activity without creating another usage
event, while a private child run retains the physical execution boundary.

## Observations and events

An observation is a privacy-safe fact read from a source artifact. Multiple
observations may describe one semantic usage event. Links record the exact
relationship between them.

Usage events have four counting states:

- `canonical`: reconciled and counted.
- `provisional`: safely countable pending reconciliation recovery.
- `excluded`: known non-usage, zero delta, or a superseded accounting representation.
- `conflicted`: contradictory observations with no safe winner.

Reports include canonical and provisional events. Excluded and conflicted rows
remain available for deterministic recovery but do not affect totals.
Merges retain both event rows and their original observation links. A
`merged_into_id` records supersession, and a database trigger keeps absorbed
events excluded if later reconciliation tries to reactivate them. Import and
repair paths do not delete ledger rows.

## Harness semantics

### Codex

Codex writes cumulative and last-call token counters. The importer uses
cumulative deltas while counters increase and uses the last-call value after a
reset. Deltas are computed in physical order within each artifact; concurrent
streams sharing an owner never share a counter baseline. Active, archived, and
retained copies share semantic identity. Original physical context wins when
later compacted copies omit intervening counters; disagreements are recorded as
issues. Repeated headers do not restart event identity. Rebuilding older ledgers
replaces colliding ordinal identities with fingerprint identities and excludes
the old aliases without counting both.

Codex state and thread-history databases enrich session, run, and turn
metadata. They never create token usage.

### Claude

Claude may write progressive snapshots for one message. The importer takes the
maximum observed value of each component. When iteration data exists, normal
iterations replace the outer aggregate and any positive unexplained residual is
counted once. Advisor iterations are separate calls. A negative residual is an
internally contradictory decomposition. If an earlier coherent decomposition
was collected, it remains counted alongside independently observed advisor
calls; the contradiction is still recorded as an issue.

### Pi

Assistant, non-subagent tool-result, compaction, and branch-summary records
with usage are direct events. Subagent batch markers do not create additional
usage. Progressive subagent snapshots reduce to one run aggregate using
terminal-state preference plus component maxima. This keeps direct calls and
aggregate subagent calls separate without counting batch markers again.

Pi source token and cost values are authoritative for local reporting.

### OpenCode

Completed `step-finish` parts are model calls. An assistant message supplies a
fallback until the collected parts cover its recorded usage and cost. A later
revision replaces a snapshot only when its recorded usage components do not
decrease. Both representations remain stored, but only one is counted. Known
dates and metadata survive source pruning. Session aggregate columns are not
counted again. Child-session calls attach to the root session for grouping and
to one child run for execution accounting. Normal sync repairs legacy child
events that have the root session but no child run without changing their event
identity or token values.

OpenCode reported totals remain reported totals; when absent, a derived total is
stored separately. Its source cost is labeled as an estimate, including known
zero values.

## Totals and costs

For each counted event, reporting uses this precedence:

1. Source-reported total.
2. Derived total.
3. Input + output + cache read + cache write.

Reasoning may be a subset of output or already included in the source total, so
it is displayed but never added again by reporting. Cached input can likewise
overlap source input in some harnesses; component columns are not a substitute
for the authoritative total.

Cost coverage is explicit. A known zero is covered. A missing value is not
zero, and grouped cost is unavailable unless every event in that group has cost
coverage.

## Profile aggregation

Profile statistics are derived from one counted-event snapshot. The configured
window uses half-open local-day boundaries, and the activity calendar always
contains the 364 local days ending today. Missing-timestamp events contribute
only to the all-time token total.

An internal execution key groups direct calls by session and genuine child
calls by run. Only positive-token rows with an execution key count as agents.
Repeated calls in one execution count once, and each agent's primary model is
selected from window token totals with an alphabetical tie break. Daily agent
stacks count distinct day-and-execution pairs using that stable primary model.
Unidentified usage still contributes to token, model, cost-equivalent, peak,
and activity totals, but not agent counts or streaks.

## Privacy boundary

Parsers may inspect private source records, but projections may retain only
allowlisted accounting and execution metadata. Prompts, responses, reasoning,
tool content, and arbitrary source strings are rejected or dropped. Workspace
paths are salted hashes.

The local SQLite database contains stable identifiers required for incremental
accounting and must not be published. Internal profile execution keys remain in
repository-only wrappers and never enter general reporting or serialization.
Aggregate JSON export is the publication boundary.
