# Tokenmaxxing contributor guide

Tokenmaxxing imports local historical token usage from Codex, Claude Code, Pi,
and OpenCode. It stores privacy-safe metadata in SQLite and reports aggregate
personal statistics.

## Project map

- `src/tokenmaxxing/ingest/`: source-specific importers and JSONL scanning.
- `src/tokenmaxxing/models.py`: shared immutable drafts and usage values.
- `src/tokenmaxxing/repository.py`: SQLite projection and reconciliation writes.
- `src/tokenmaxxing/reporting.py`: aggregate totals and export payloads.
- `src/tokenmaxxing/pricing.py`: API-equivalent estimation and rate-card validation.
- `src/tokenmaxxing/data/rate-card.json`: dated public token prices.
- `src/tokenmaxxing/sync.py`: four-source orchestration.
- `tests/fixtures/`: minimal source-shaped fixtures without private content.
- `docs/architecture.md`: identities, data flow, and accounting semantics.

Each large harness is a package with a small public facade. Parsing turns raw
records into privacy-safe projections. Reconciliation derives canonical events
from stored observations. Importer modules own discovery and lifecycle order.

## Invariants

- Event identity is source-scoped and deterministic. Never merge by timestamp.
- Repeated syncs must be idempotent.
- Copied, replaced, missing, and reappearing artifacts must reconcile without
  counting the same semantic event twice.
- Only canonical and provisional events appear in reports. Conflicted and
  excluded events remain stored but uncounted.
- Reporting prefers a source-reported total, then a derived total, then the
  non-overlapping input/output/cache components. Reasoning is never added again
  when the source already includes it in output or total.
- Pi assistant, non-subagent tool-result, compaction, and branch-summary
  records with usage are direct events. Subagent batch markers do not create
  additional usage.
- Missing cost is distinct from a known zero cost.
- Catalog pricing requires complete components that reconcile to the canonical
  event total. Unsupported prices and modifiers remain unpriced.
- Source text, prompts, reasoning, tool content, and raw workspace paths must
  never cross the projection boundary.
- Check the SQLite database, WAL, and SHM when changing privacy behavior.

## Style

- Prefer direct functions, small data classes, and explicit source logic.
- Do not add importer frameworks, registries, inheritance, or runtime
  dependencies to remove a small amount of duplication.
- Keep SQL beside the reconciliation rule it implements.
- Avoid comments when names and structure are sufficient.
- Preserve public imports from `tokenmaxxing.ingest.<source>`.

## Development

```bash
uv sync --locked
uv run pytest
uv run ruff check .
git diff --check
uv build
```

Run the focused harness test after changing an importer, then run the complete
suite before claiming completion.

## Commits

Use plain Conventional Commit messages. Never add `Co-Authored-By: Codex`,
`Codex-Session:`, `Generated with Codex`, or similar attribution.

## Verification

Run `uv run pytest`, `uv run ruff check .`, and `git diff --check` before
claiming completion.
