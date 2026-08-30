# Tokenmaxxing Simple V1 Design

## Goal

Tokenmaxxing is a local command-line tool that imports historical token usage
from Codex, Claude Code, Pi, and OpenCode, stores normalized metadata in SQLite,
and shows privacy-safe personal statistics.

V1 must be easy to install, inspect, modify, and remove. It keeps the accounting
correctness already covered by the source-specific test suites and removes
infrastructure that does not serve historical import or local statistics.

## Product scope

V1 provides three commands:

```text
tokenmaxxing [--db PATH] [--debug] sync
             [--source codex|claude|pi|opencode|all]
             [--codex-root PATH] [--claude-root PATH] [--pi-root PATH]
             [--opencode-db PATH] [--json]
tokenmaxxing [--db PATH] [--debug] stats [--group-by source|model|day]
             [--timezone ZONE] [--json]
tokenmaxxing [--db PATH] [--debug] export PATH [--timezone ZONE]
```

- `sync` discovers the standard local history for each selected harness and
  imports it incrementally. The default source is `all`.
- `stats` prints totals for counted events. Human-readable output is the
  default; `--json` returns the same aggregate schema for scripts.
- `export` writes aggregate JSON only. It never copies the SQLite database or
  exports raw identifiers.

The default database remains local. `TOKENMAXXING_HOME` overrides its directory.
Without an override, macOS uses
`~/Library/Application Support/tokenmaxxing`; other POSIX systems use
`$XDG_DATA_HOME/tokenmaxxing` or `~/.local/share/tokenmaxxing`.

The Python distribution is named `tokenmaxxing-history` because the
`tokenmaxxing` distribution name is already occupied on PyPI. The import package
and executable remain `tokenmaxxing`.

## Non-goals

V1 does not include:

- an OTLP receiver, daemon, background scheduler, or LaunchAgent;
- automatic edits to Codex, Claude, Pi, or OpenCode configuration;
- a web dashboard or public hosting;
- ChatGPT desktop ingestion;
- live rate-limit polling;
- legacy pre-SQLite OpenCode storage and OpenCode `next`/V2 schemas;
- billing estimates for sources that do not report cost.

These features may be designed later without keeping unused scaffolding in V1.

## Architecture

The runtime flow is deliberately short:

```text
CLI -> source sync -> metadata projection -> SQLite -> aggregate query/output
```

The code has five responsibilities:

1. `cli.py` parses commands and renders text or JSON.
2. `sync.py` resolves source roots and runs selected importers sequentially.
3. `ingest/{codex,claude,pi,opencode}.py` owns source-specific discovery and
   accounting.
4. `db.py` and `repository.py` own schema setup, transactions, and aggregate
   queries.
5. `privacy.py` and `models.py` define the storage boundary and normalized
   records.

The existing source importers stay separate because their deduplication rules
are materially different. V1 does not rewrite the reviewed Codex and Claude
reducers merely to reduce line count. It removes unused abstractions and
splits a source file only when the split creates an enforceable boundary.

OpenCode uses a narrow read-only SQLite adapter rather than the JSONL scanner.
The adapter opens one WAL-aware read transaction with `query_only` enabled and
selects only allowlisted scalar fields. It never copies the source database or
selects credential, account, prompt, transcript, tool, snapshot, patch, or
event-log payloads.

`writer.py` and the generic OTel reconciliation module are removed because no
V1 runtime uses concurrent producers or OTel observations. Receiver-related
configuration and all unused runtime dependencies are removed.

## Historical import behavior

Default roots are:

- Codex: `~/.codex`, including active sessions, archived sessions, and the
  optional state/history SQLite databases;
- Claude Code: `~/.claude/projects`;
- Pi: `~/.pi/agent/sessions`;
- OpenCode: `$XDG_DATA_HOME/opencode/opencode.db` or
  `~/.local/share/opencode/opencode.db`.

Environment-specific roots use `--codex-root`, `--claude-root`, `--pi-root`,
and `--opencode-db`. Missing roots are reported as skipped, not created.

Each JSONL artifact is processed transactionally with its cursor. Complete
newline-terminated records are committed; partial tails remain unread until a
future sync. Truncation, replacement, or header identity changes create a new
artifact generation. Re-running `sync` with unchanged inputs performs no new
inserts and does not change totals.

`sync all` attempts Codex, Claude, Pi, and OpenCode independently. A source
failure does not erase prior data or prevent later sources from running. The
command reports content-free error categories and exits nonzero if any requested
source fails.

## Source accounting retained in V1

Codex retains copied-history ownership, cumulative-counter reset handling,
child-agent attribution, active/archive deduplication, and deterministic
rebuild recovery.

Claude Code retains global message identity, progressive snapshot maxima,
iteration and advisor accounting, residual events, subagent discovery,
replacement recovery, and field-scoped metadata ownership.

Pi retains lineage and clone handling, direct and subagent usage, retained-tail
exclusion, progressive snapshot collapse, overlapping aggregate exclusion,
reasoning tokens, and reported costs.

Pi is not considered trustworthy until all three existing regression cases are
green:

1. Replacement preserves metadata owned by another channel.
2. An interrupted replacement repair is completed by the next EOF sync.
3. Run status and timing reduce across every live current-generation artifact.

OpenCode counts one completed provider step per `part.id` where
`part.data.type` is `step-finish`. The part is joined to its assistant message
for session, provider, model, and agent metadata. Assistant-message usage and
session aggregates are not counted again because they overlap step usage.

An assistant message with no `step-finish` produces one message-keyed fallback
event so an interrupted or failed call remains visible. The fallback is used
only when that message has no completed step, so it cannot overlap a part event.

OpenCode V1 stores visible output and reasoning as separate components.
Tokenmaxxing normalizes these to its inclusive-output invariant:

```text
output = source output + source reasoning
reasoning = source reasoning
```

The source-reported total is retained when present. Otherwise the importer
derives total as non-cached input plus normalized output plus cache read and
cache write. Reporting never adds reasoning again.

OpenCode event identities are stable part or message IDs. Every sync performs a
full allowlisted metadata scan, replaces OpenCode-owned fields from the current
row, excludes disappeared events, and restores events that reappear. This is
preferred over a cursor because rows can be corrected after initial insertion
and the scan avoids reading content-bearing event snapshots.

OpenCode cost is labeled as an OpenCode-reported estimate. A reported zero has
cost coverage but is not proof of provider billing. Session aggregate token and
cost columns are used only as a checksum against imported step/fallback events.

## Statistics

Totals include only canonical or provisional counted events from the existing
`counted_usage_events` view. Excluded and conflicted events remain out of the
default result.

The aggregate schema contains:

- group value and counted event count;
- input, output, cache-read, cache-write, and reasoning tokens;
- total tokens;
- reported cost in USD and the number of events with cost coverage.

Total tokens use the source-reported total when present, then the normalized
derived total, then the sum of non-overlapping input/output/cache components.
Reasoning tokens are a subset of output tokens and are never added again.

Unknown model values are labeled `(unknown)`. Day grouping uses the local
timezone by default and accepts an explicit IANA timezone. Missing cost is
displayed as unavailable, never `$0`.

## Privacy

Tokenmaxxing stores usage and execution metadata only. It never stores prompts,
assistant text, reasoning text, summaries, commands, tool arguments, tool
results, raw errors, attachments, raw API bodies, or raw working-directory
paths.

The local database may contain source session, run, turn, request, and response
identifiers needed for deterministic deduplication. Therefore the database,
salt, journal, WAL, and SHM files are private local state and are never valid
export artifacts.

An OpenCode database may also contain access tokens, refresh tokens,
credentials, prompts, reasoning, tool payloads, file paths, and snapshots.
Tokenmaxxing queries only the `session`, `message`, and `part` columns required
for allowlisted metadata and never copies or publishes the OpenCode database.

`export` emits only aggregate rows plus an export schema version and generation
timestamp. It omits raw identifiers, artifact metadata, issues, paths, and
workspace hashes.

## Errors and observability

CLI errors are short and actionable. Human output goes to stderr; JSON mode
returns a stable object with per-source status, counts, and a content-free error
category. Tracebacks are shown only with `--debug`.

Malformed source rows create sanitized local issues and do not expose their raw
content. Unsupported schema shapes increment issue counts while other artifacts
continue importing.

## Publishing

The repository ships under the MIT License with:

- a concise README containing installation, commands, support status, privacy,
  data location, and deletion instructions;
- Python 3.12+ package metadata and a `tokenmaxxing` console entry point;
- no runtime dependencies unless the final implementation proves one is
  required;
- a committed `uv.lock` for reproducible development;
- no private paths, databases, salts, histories, or generated exports.

## Verification

Implementation is accepted only after:

1. The three Pi regressions fail before their fixes and pass afterward.
2. The focused importer suites and full test suite pass.
3. A fresh real-history import succeeds for Codex, Claude Code, Pi, and
   OpenCode.
4. A fresh OpenCode import matches session-level token/cost checksums without
   counting assistant, session, part, or event-log copies twice.
5. An OpenCode fixture proves multi-step messages, no-finish fallback, active
   WAL reads, correction, deletion, and reappearance behavior.
6. A second import against unchanged history inserts no new observations or
   events and leaves totals unchanged.
7. SQLite, journal, WAL, and SHM files contain none of the privacy sentinels.
8. `sync`, `stats`, and `export` work from an installed wheel.
9. The wheel and source distribution contain migrations, README, and license,
   and exclude local/private artifacts.
10. `git diff --check` and a secrets scan pass.
