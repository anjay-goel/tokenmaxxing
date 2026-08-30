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
generation. OpenCode reads a stable V1 SQLite snapshot in query-only mode,
including committed WAL data.

## Source identities

Identity is source-scoped. Timestamps are metadata, never identity.

| Source | Counted identity | Duplicate handling |
| --- | --- | --- |
| Codex | session owner and counter ordinal | Equal copied counters collapse; divergent copies conflict |
| Claude | global message identity and iteration index | Progressive and copied snapshots reduce component-wise |
| Pi direct | lineage root, entry identity, and usage slot | Copied observations reduce to one event |
| Pi subagent | batch-qualified or lineage-qualified run identity | Progressive snapshots reduce to one run aggregate |
| OpenCode | completed part identity, or assistant message fallback | A message fallback exists only without a completed part |

Child Codex and OpenCode activity remains one model call per source identity.
Root attribution groups the activity without creating another usage event.

## Observations and events

An observation is a privacy-safe fact read from a source artifact. Multiple
observations may describe one semantic usage event. Links record the exact
relationship between them.

Usage events have four counting states:

- `canonical`: reconciled and counted.
- `provisional`: safely countable pending reconciliation recovery.
- `excluded`: known non-usage, zero delta, stale generation, or superseded event.
- `conflicted`: contradictory observations with no safe winner.

Reports include canonical and provisional events. Excluded and conflicted rows
remain available for deterministic recovery but do not affect totals.

## Harness semantics

### Codex

Codex writes cumulative and last-call token counters. The importer uses
cumulative deltas while counters increase and uses the last-call value after a
reset. Active and archived copies share semantic identity. A divergent copied
counter is quarantined and also resets the delta baseline so later calls cannot
inherit an ambiguous cumulative value.

Codex state and thread-history databases enrich session, run, and turn
metadata. They never create token usage.

### Claude

Claude may write progressive snapshots for one message. The importer takes the
maximum observed value of each component. When iteration data exists, normal
iterations replace the outer aggregate and any positive unexplained residual is
counted once. Advisor iterations are separate calls. A negative residual is an
internally contradictory decomposition and remains conflicted.

### Pi

Assistant, non-subagent tool-result, compaction, and branch-summary records
with usage are direct events. Subagent batch markers do not create additional
usage. Progressive subagent snapshots reduce to one run aggregate using
terminal-state preference plus component maxima. This keeps direct calls and
aggregate subagent calls separate without counting batch markers again.

Pi source token and cost values are authoritative for local reporting.

### OpenCode

Completed `step-finish` parts are model calls. An assistant message supplies a
fallback only when no completed part exists. Session aggregate columns are not
counted again. Child-session calls attach to the root session for grouping.

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

## Privacy boundary

Parsers may inspect private source records, but projections may retain only
allowlisted accounting and execution metadata. Prompts, responses, reasoning,
tool content, and arbitrary source strings are rejected or dropped. Workspace
paths are salted hashes.

The local SQLite database contains stable identifiers required for incremental
accounting and must not be published. Aggregate JSON export is the publication
boundary.
