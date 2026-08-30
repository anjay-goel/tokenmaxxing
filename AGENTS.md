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
- `src/tokenmaxxing/profile/config.py`: strict YAML configuration and discovery.
- `src/tokenmaxxing/profile/project.py`: project initialization and paths.
- `src/tokenmaxxing/profile/data.py`: privacy-safe profile aggregation.
- `src/tokenmaxxing/profile/awards.py`: deterministic aggregate award rules.
- `src/tokenmaxxing/profile/render.py`: Jinja rendering and public payload creation.
- `src/tokenmaxxing/profile/build.py`: build validation and rollback-safe replacement.
- `src/tokenmaxxing/profile/deploy.py`: deploy argv planning and execution.
- `src/tokenmaxxing/profile/schedule.py`: owned OS scheduler integrations.
- `src/tokenmaxxing/profile/cli.py`: profile workflow and terminal output.
- `src/tokenmaxxing/profile/templates/` and `assets/`: packaged site source.
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
- Profile aggregation must reuse reporting and pricing arithmetic.
- Internal agent keys may group stored rows but must never enter a public
  payload. Only configured profile fields and aggregate statistics are public.
- Render user content through Jinja autoescaping. Keep the serialized profile
  payload allowlisted and run the site validator before replacement or deploy.
- Build beside the destination, validate, then replace with rollback. Render,
  validation, or replacement failures preserve the previous site; deploy
  failures keep the new validated local build.
- Deploy commands are argv lists executed with `shell=False`. Interactive
  publishes confirm the exact argv; scheduled publishes use the YAML command.
- Interactive onboarding accepts one command string, forces a sync before its
  first build or publish, and offers scheduling only after publishing succeeds.
- Scheduler changes may touch only the deterministic job owned by the current
  profile. Enable scheduling only after a validated build and deploy plan.
- Packaged templates, starters, and assets are source. The complete generated
  static package lives under the profile project's ignored `dist/` directory.
- Windows batch launchers are not equivalent to shell-free native argv
  execution and must remain rejected.

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

For profile work, run the smallest relevant file first. The usual focused
gates are:

```bash
uv run pytest tests/profile
uv run pytest tests/test_cli.py tests/profile/test_cli.py
uv run pytest tests/profile/test_package.py tests/profile/test_end_to_end.py
```

## Commits

Use plain Conventional Commit messages. Never add `Co-Authored-By: Codex`,
`Codex-Session:`, `Generated with Codex`, or similar attribution.

## Verification

Run `uv run pytest`, `uv run ruff check .`, and `git diff --check` before
claiming completion.
