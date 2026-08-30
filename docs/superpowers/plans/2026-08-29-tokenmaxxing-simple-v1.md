# Tokenmaxxing Simple V1 Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Ship a small local CLI that safely imports historical Codex, Claude Code, Pi, and OpenCode usage, reports aggregate personal statistics, and can be published for others to install and modify.

**Architecture:** Preserve the reviewed source-specific disk importers and their SQLite evidence model. Add one sequential sync orchestrator and one argparse CLI, remove unused live-telemetry infrastructure, and expose aggregate-only stats/export queries through the repository. Keep every raw identifier inside the private local database.

**Tech Stack:** Python 3.12+, Python standard library, SQLite, argparse, uv/uv_build, pytest.

## Global Constraints

- The distribution name is `tokenmaxxing-history`; the import package and executable are `tokenmaxxing`.
- V1 commands are `sync`, `stats`, and `export` only.
- V1 imports disk history from Codex, Claude Code, Pi, and stable OpenCode V1 SQLite; it does not ingest ChatGPT desktop history.
- V1 has no OTLP receiver, daemon, scheduler, LaunchAgent, harness configuration editor, or web dashboard.
- Persist metadata only; never persist prompts, assistant/reasoning text, summaries, commands, tool arguments/results, raw errors, attachments, raw API bodies, or raw working-directory paths.
- Raw source identifiers remain local and never appear in exports.
- Codex, Claude, Pi, and OpenCode source-specific accounting behavior covered by the regression suites must remain intact.
- Reasoning tokens are a subset of output tokens and must never be added to total tokens again.
- Missing cost is unavailable, not zero.
- Use Conventional Commit messages with no attribution trailers.
- Keep code comments short and only where the reason is not evident from the code.

---

## File Map

```text
pyproject.toml                         package metadata, executable, no runtime deps
uv.lock                               reproducible development environment
README.md                             install, commands, privacy, data removal
LICENSE                               MIT license
src/tokenmaxxing/
  __init__.py                         package version
  __main__.py                         python -m tokenmaxxing entry point
  cli.py                              argparse and text/JSON rendering
  config.py                           portable local data paths, salt, workspace hash
  db.py                               SQLite connection and migrations
  models.py                           normalized records and public result types
  repository.py                       idempotent writes and aggregate stats queries
  sync.py                             source roots and sequential sync orchestration
  privacy.py                          metadata projection validation
  ingest/{jsonl,codex,claude,pi}.py   incremental JSONL source importers
  ingest/opencode.py                  read-only OpenCode SQLite importer
  migrations/*.sql                   existing private local schema
tests/
  test_cli.py                         installed-interface behavior
  test_config.py                      portable path behavior
  test_reporting.py                   aggregate and export semantics
  test_sync.py                        multi-source orchestration behavior
  test_pi_import.py                   Pi correctness and recovery regressions
  test_opencode_import.py             OpenCode accounting and privacy regressions
  fixtures/opencode/schema.sql        minimal content-free OpenCode V1 schema
```

Remove:

```text
src/tokenmaxxing/reconcile.py
src/tokenmaxxing/writer.py
tests/test_reconcile.py
tests/test_writer.py
```

These files implement future OTel/concurrent-writer behavior and have no V1 production caller.

---

### Task 1: Finish Pi replacement and run-state reconciliation

**Files:**
- Modify: `src/tokenmaxxing/ingest/pi.py`
- Modify: `tests/test_pi_import.py`

**Interfaces:**
- Consumes: latest-generation, non-missing Pi disk observations already stored in SQLite.
- Produces: `sync_pi(repository: Repository, root: Path) -> SyncStats` that repairs replacement state even on an EOF-only retry and reduces duplicate subagent snapshots across all live artifacts.

- [ ] **Step 1: Preserve and run the three focused regression tests**

The worktree already contains three test cases: cross-channel field ownership,
EOF-only recovery after an interrupted repair, and status reduction across all
live artifact generations.

Run:

```bash
uv run pytest tests/test_pi_import.py \
  -k 'preserves_cross_channel or repairs_stale_run_from_eof or global_subagent_status' -q
```

Expected before the complete fix: the cross-channel case passes with the partial worktree fix; the interruption and global-reduction cases fail.

- [ ] **Step 2: Persist enough sanitized subagent state in each observation**

Keep direct and subagent metadata ownership separate:

```python
_PI_DIRECT_DISK_METADATA_FIELDS = frozenset({
    "provider", "api", "model", "response_model", "effort", "stop_reason",
    "error_category", "completed_at_ns", "success",
})
_PI_SUBAGENT_DISK_METADATA_FIELDS = frozenset({
    "provider", "model", "effort", "error_category", "started_at_ns",
    "completed_at_ns", "success",
})
```

Store only numeric status/timing data under the allowlisted `usage` projection:

```python
usage["_pi"] = {
    "status": _SUBAGENT_STATUS_CODES[record.status],
    "startedAt": record.started_at_ns // 1_000_000 if record.started_at_ns else 0,
}
```

Add `finishedAt` only when present. Do not store role text, agent prompts, errors, paths, or tool content.

- [ ] **Step 3: Rebuild subagent state from all live observations**

Replace the sync-local `authoritative_events` decision with a repository-backed reducer. Query every `subagent_snapshot` whose artifact is non-missing and at its latest generation, group by semantic `source_turn_id`, decode the numeric `_pi` metadata, and choose:

- maximum token/cost components across live snapshots;
- a terminal snapshot over a non-terminal snapshot while any live terminal snapshot exists;
- otherwise the latest non-terminal snapshot by observation time;
- earliest non-null start and the chosen terminal finish;
- metadata only from `_PI_SUBAGENT_DISK_METADATA_FIELDS`.

Call this reducer on every `sync_pi`, including an EOF-only second run. This makes the interrupted-repair test recover without rereading lines.

- [ ] **Step 4: Run the focused tests until green**

Run:

```bash
uv run pytest tests/test_pi_import.py \
  -k 'preserves_cross_channel or repairs_stale_run_from_eof or global_subagent_status' -q
```

Expected: `3 passed`.

- [ ] **Step 5: Run the complete Pi suite**

Run: `uv run pytest tests/test_pi_import.py -q`

Expected: every Pi test passes with no warning or error output.

- [ ] **Step 6: Commit the Pi repair**

```bash
git add src/tokenmaxxing/ingest/pi.py tests/test_pi_import.py
git commit -m "fix: complete pi history reconciliation"
```

---

### Task 2: Import stable OpenCode V1 SQLite usage

**Files:**
- Modify: `src/tokenmaxxing/models.py`
- Modify: `src/tokenmaxxing/repository.py`
- Create: `src/tokenmaxxing/ingest/opencode.py`
- Create: `tests/fixtures/opencode/schema.sql`
- Create: `tests/test_opencode_import.py`

**Interfaces:**
- Extends: `Source = Literal["codex", "claude", "pi", "opencode"]`.
- Produces: `OpenCodeRoots.from_data_dir(data_dir: Path) -> OpenCodeRoots`.
- Produces: `sync_opencode(repository: Repository, roots: OpenCodeRoots) -> SyncStats`.
- Supports: populated stable V1 `session`, `message`, and `part` tables only; legacy flat files and OpenCode `next`/V2 are explicit non-goals.

- [ ] **Step 1: Create a content-free SQLite fixture and failing mapping tests**

The fixture schema contains only the relevant columns from OpenCode V1:

```sql
CREATE TABLE session (
  id TEXT PRIMARY KEY,
  parent_id TEXT,
  version TEXT NOT NULL,
  agent TEXT,
  time_created INTEGER NOT NULL,
  time_updated INTEGER NOT NULL,
  cost REAL NOT NULL DEFAULT 0,
  tokens_input INTEGER NOT NULL DEFAULT 0,
  tokens_output INTEGER NOT NULL DEFAULT 0,
  tokens_reasoning INTEGER NOT NULL DEFAULT 0,
  tokens_cache_read INTEGER NOT NULL DEFAULT 0,
  tokens_cache_write INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE message (
  id TEXT PRIMARY KEY,
  session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL,
  time_updated INTEGER NOT NULL,
  data TEXT NOT NULL
);
CREATE TABLE part (
  id TEXT PRIMARY KEY,
  message_id TEXT NOT NULL,
  session_id TEXT NOT NULL,
  time_created INTEGER NOT NULL,
  time_updated INTEGER NOT NULL,
  data TEXT NOT NULL
);
```

Build rows in test helpers with `json.dumps`; do not commit a binary database. Test one assistant message with two `step-finish` parts and assert two canonical events, no assistant/session duplicate, and:

```python
assert event.tokens.output == source_output + source_reasoning
assert event.tokens.reasoning == source_reasoning
assert event.tokens.reported_total == source_total
assert event.cost.source == "opencode_reported_estimate"
assert event.cost.estimated is True
```

Run: `uv run pytest tests/test_opencode_import.py -q`

Expected: collection fails because `tokenmaxxing.ingest.opencode` does not exist.

- [ ] **Step 2: Add no-finish, overlap, and privacy tests**

Cover one no-finish message fallback, fallback suppression when a completed step
exists, exclusion of session/event-log copies, forbidden-content sentinels, and
a content-free unsupported-schema error.

The privacy test scans target SQLite, WAL, and SHM bytes. It never prints the sentinel-bearing source JSON.

- [ ] **Step 3: Implement the narrow read-only importer**

Open the source database with:

```python
uri = f"file:{quote(str(path))}?mode=ro"
source = sqlite3.connect(uri, uri=True, isolation_level=None)
source.execute("PRAGMA query_only = ON")
source.execute("BEGIN")
```

Do not use `immutable=1`; it can miss active WAL data. Validate the required tables and columns before querying.

Select scalar allowlist paths with SQLite `json_extract`; do not select complete `data` values into the importer. Canonical completed-step identity is `opencode:part:{part_id}`. A message with no `step-finish` uses `opencode:message:{message_id}`. Join message metadata for provider, model, agent, session, and completion time.

Normalize OpenCode V1 tokens as:

```python
normalized_output = visible_output + reasoning
derived_total = input_tokens + normalized_output + cache_read + cache_write
```

Keep `reasoning` separately and never add it again in reporting. Convert finite cost decimal text to nanodollars with `source="opencode_reported_estimate"` and `estimated=True`.

- [ ] **Step 4: Make full rescans corrective and idempotent**

Use a stable observation key containing the source row ID plus a hash of only the allowlisted scalar projection. Add `replace_usage: bool = False` to `UsageEventDraft`; when true, repository upsert replaces token/cost columns exactly while `replace_metadata_fields` remains scoped to OpenCode-owned metadata.

Before applying an unchanged projection, check whether its stable observation and canonical event already exist and are countable. A same-input second sync must report zero inserted or updated events. Mark OpenCode events absent from the current source transaction as excluded; restore a reappearing row to canonical.

- [ ] **Step 5: Test correction, deletion, reappearance, hierarchy, and active WAL**

Add tests for a true no-op repeat sync, exact replacement after a corrected
step, exclusion and restoration after deletion/reappearance, single counting
of child-session usage, and visibility of committed rows in an active WAL.

Run: `uv run pytest tests/test_opencode_import.py -q`

Expected: all OpenCode tests pass.

- [ ] **Step 6: Commit the OpenCode importer**

```bash
git add src/tokenmaxxing/models.py src/tokenmaxxing/repository.py \
  src/tokenmaxxing/ingest/opencode.py tests/fixtures/opencode/schema.sql \
  tests/test_opencode_import.py
git commit -m "feat: import opencode usage"
```

---

### Task 3: Remove future-only runtime infrastructure and simplify paths

**Files:**
- Modify: `src/tokenmaxxing/config.py`
- Modify: `tests/test_config.py`
- Delete: `src/tokenmaxxing/reconcile.py`
- Delete: `src/tokenmaxxing/writer.py`
- Delete: `tests/test_reconcile.py`
- Delete: `tests/test_writer.py`
- Modify: `src/tokenmaxxing/models.py`
- Modify: `src/tokenmaxxing/migrations/0001_initial.sql`

**Interfaces:**
- Produces: `default_paths(home: Path | None = None, environ: Mapping[str, str] | None = None, platform: str | None = None) -> AppPaths`.
- Preserves: `load_or_create_salt` and `hash_workspace` used by Pi.

- [ ] **Step 1: Replace receiver configuration tests with portable path tests**

Add tests that assert:

```python
assert default_paths(tmp_path, {}, "darwin").data_dir == (
    tmp_path / "Library" / "Application Support" / "tokenmaxxing"
)
assert default_paths(tmp_path, {"XDG_DATA_HOME": "/data"}, "linux").data_dir == (
    Path("/data/tokenmaxxing")
)
assert default_paths(tmp_path, {"TOKENMAXXING_HOME": "/custom"}, "linux").data_dir == (
    Path("/custom")
)
```

Run: `uv run pytest tests/test_config.py -q`

Expected: fail because the current function is macOS-only and `AppConfig` still models the removed receiver.

- [ ] **Step 2: Implement the small path model**

Keep only:

```python
@dataclass(frozen=True, slots=True)
class AppPaths:
    data_dir: Path
    db_path: Path
    salt_path: Path
```

Resolve `TOKENMAXXING_HOME` first, then macOS Application Support, then `XDG_DATA_HOME`, then `~/.local/share`. Remove `AppConfig`, `ipaddress`, receiver host/port, interval, and `ownership_path`.

- [ ] **Step 3: Remove unused live-telemetry modules**

Delete the generic `Reconciler`, serialized `SQLiteWriter`, their tests, and `ReconcileStats`. Remove the unused `reconciled_at_ns` column/index from the initial migration because no released database contract exists and no V1 code reads it. Keep the observation channel field because it is part of the stable local identity and existing fixtures exercise it.

- [ ] **Step 4: Verify simplification**

Run:

```bash
uv run pytest tests/test_config.py tests/test_db.py tests/test_models.py \
  tests/test_repository.py -q
rg -n 'AppConfig|receiver_port|SQLiteWriter|Reconciler|ReconcileStats' src tests
```

Expected: tests pass; `rg` returns no matches.

- [ ] **Step 5: Commit the simplification**

```bash
git add src/tokenmaxxing/config.py src/tokenmaxxing/models.py \
  src/tokenmaxxing/migrations/0001_initial.sql tests/test_config.py
git add -u src/tokenmaxxing/reconcile.py src/tokenmaxxing/writer.py \
  tests/test_reconcile.py tests/test_writer.py
git commit -m "refactor: remove unused telemetry infrastructure"
```

---

### Task 4: Add sequential multi-source sync orchestration

**Files:**
- Create: `src/tokenmaxxing/sync.py`
- Create: `tests/test_sync.py`

**Interfaces:**
- Produces: `SourceRoots.defaults(home: Path | None = None) -> SourceRoots`.
- Produces: `sync_sources(repository: Repository, roots: SourceRoots, sources: tuple[Source, ...]) -> tuple[SourceSyncResult, ...]`.
- `SourceSyncResult` contains `source`, `status` (`ok`, `skipped`, or `error`), `SyncStats`, and an optional content-free `error_category`.

- [ ] **Step 1: Write orchestration tests**

Cover source order `codex, claude, pi, opencode`, missing-root skip behavior,
continuation after one source fails, and exception-class-only error output with
temporary roots and monkeypatched importer functions.

Run: `uv run pytest tests/test_sync.py -q`

Expected: collection fails because `tokenmaxxing.sync` does not exist.

- [ ] **Step 2: Implement source roots and orchestration**

Use:

```python
@dataclass(frozen=True, slots=True)
class SourceRoots:
    codex: Path
    claude: Path
    pi: Path
    opencode_db: Path

@dataclass(frozen=True, slots=True)
class SourceSyncResult:
    source: Source
    status: Literal["ok", "skipped", "error"]
    stats: SyncStats = SyncStats()
    error_category: str | None = None
```

Catch `Exception`, not `BaseException`. Store `type(error).__name__`, never `str(error)`. Call `CodexRoots.from_path` only for the Codex root and `OpenCodeRoots.from_data_dir` only after resolving the OpenCode database parent.

- [ ] **Step 3: Run orchestration and importer tests**

Run:

```bash
uv run pytest tests/test_sync.py tests/test_codex_import.py \
  tests/test_claude_import.py tests/test_pi_import.py \
  tests/test_opencode_import.py -q
```

Expected: all pass.

- [ ] **Step 4: Commit orchestration**

```bash
git add src/tokenmaxxing/sync.py tests/test_sync.py
git commit -m "feat: sync local agent histories"
```

---

### Task 5: Add aggregate statistics and privacy-safe export

**Files:**
- Modify: `src/tokenmaxxing/models.py`
- Modify: `src/tokenmaxxing/repository.py`
- Create: `src/tokenmaxxing/reporting.py`
- Create: `tests/test_reporting.py`

**Interfaces:**
- Produces: `usage_stats(repository: Repository, group_by: Literal["source", "model", "day"], timezone: tzinfo) -> tuple[UsageStat, ...]`.
- Produces: `export_payload(repository: Repository, timezone: tzinfo, generated_at: datetime) -> dict[str, JsonValue]`.
- `UsageStat` contains group, event count, token components, total tokens, cost nanodollars, and cost-covered event count.

- [ ] **Step 1: Write aggregate tests**

Create canonical, provisional, excluded, and conflicted events and assert:

```python
assert source_stat.event_count == 2
assert source_stat.total_tokens == reported_total_plus_fallback_total
assert source_stat.reasoning_tokens == reasoning_subset
assert source_stat.cost_covered_events == 1
assert source_stat.cost_nanos == reported_cost
```

Also assert response model wins over model, unknown models group as `(unknown)`, day grouping honors an explicit `ZoneInfo("Asia/Kolkata")`, and missing costs remain `None`.

Run: `uv run pytest tests/test_reporting.py -q`

Expected: fail because reporting interfaces do not exist.

- [ ] **Step 2: Implement the repository read query**

Add one query that selects counted events with:

```sql
COALESCE(e.response_model, e.model, r.model,
         s.current_model, s.initial_model, '(unknown)') AS resolved_model
```

and the first non-null event/run/session timestamp. Return private rows only to `reporting.py`; do not expose identifiers.

- [ ] **Step 3: Implement deterministic aggregation**

For each event choose total tokens in this order:

```python
event_total = (
    reported_total
    if reported_total is not None
    else derived_total
    if derived_total is not None
    else sum(value or 0 for value in (input, output, cache_read, cache_write))
)
```

Do not add reasoning. Sum cost only across rows where `total_cost_nanos` is non-null; return `None` when coverage is zero. Sort groups lexically, with days in chronological ISO order.

- [ ] **Step 4: Implement aggregate-only export**

Return exactly:

```python
{
    "schema_version": 1,
    "generated_at": "2026-08-29T00:00:00+00:00",
    "timezone": "Asia/Kolkata",
    "overall": {"group": "all", "event_count": 2, "total_tokens": 17},
    "by_source": [{"group": "claude", "event_count": 2, "total_tokens": 17}],
    "by_model": [{"group": "sonnet", "event_count": 2, "total_tokens": 17}],
    "by_day": [{"group": "2026-08-29", "event_count": 2, "total_tokens": 17}],
}
```

Verify recursively that keys such as `session`, `request`, `response`, `artifact`, `path`, and `workspace` never appear.

- [ ] **Step 5: Run reporting and privacy tests**

Run:

```bash
uv run pytest tests/test_reporting.py tests/test_repository.py tests/test_privacy.py -q
```

Expected: all pass.

- [ ] **Step 6: Commit reporting**

```bash
git add src/tokenmaxxing/models.py src/tokenmaxxing/repository.py \
  src/tokenmaxxing/reporting.py tests/test_reporting.py
git commit -m "feat: report aggregate agent usage"
```

---

### Task 6: Add the command-line interface and package entry points

**Files:**
- Create: `src/tokenmaxxing/cli.py`
- Create: `src/tokenmaxxing/__main__.py`
- Create: `tests/test_cli.py`
- Modify: `pyproject.toml`
- Modify: `src/tokenmaxxing/__init__.py`

**Interfaces:**
- Produces: `main(argv: Sequence[str] | None = None) -> int`.
- Produces console script: `tokenmaxxing = "tokenmaxxing.cli:main"`.

- [ ] **Step 1: Write CLI tests**

Test through `main(argv)` with temporary roots/database. Cover four-source JSON
sync output, stable stats JSON, aggregate-only export, nonzero source-error exit
without a traceback, and traceback propagation in debug mode.

Run: `uv run pytest tests/test_cli.py -q`

Expected: collection fails because `tokenmaxxing.cli` does not exist.

- [ ] **Step 2: Implement argparse commands**

Global options are `--db PATH` and `--debug`. Implement the exact approved command options from the design. Human `stats` output uses a compact aligned table; all machine output uses `json.dumps(payload, sort_keys=True)`.

Return `0` for successful or skipped-only syncs, `1` when a source errors, and `2` through argparse for invalid input. Write operational errors to stderr. In debug mode re-raise the caught source exception from orchestration or run the selected importer without the catch wrapper.

- [ ] **Step 3: Add module and console entry points**

`__main__.py` contains:

```python
from tokenmaxxing.cli import main

raise SystemExit(main())
```

Set `name = "tokenmaxxing-history"`, keep version `0.1.0`, remove all runtime dependencies, retain only `pytest>=8.3` as a development dependency, and add:

```toml
[project.scripts]
tokenmaxxing = "tokenmaxxing.cli:main"
```

- [ ] **Step 4: Run CLI tests and smoke help**

Run:

```bash
uv run pytest tests/test_cli.py -q
uv run tokenmaxxing --help
uv run python -m tokenmaxxing --help
```

Expected: tests pass and both help commands list `sync`, `stats`, and `export`.

- [ ] **Step 5: Commit CLI and packaging**

```bash
git add src/tokenmaxxing/cli.py src/tokenmaxxing/__main__.py \
  src/tokenmaxxing/__init__.py tests/test_cli.py pyproject.toml
git commit -m "feat: add tokenmaxxing cli"
```

---

### Task 7: Publishable documentation, license, ignores, and lockfile

**Files:**
- Create: `README.md`
- Create: `LICENSE`
- Modify: `.gitignore`
- Modify: `pyproject.toml`
- Add: `uv.lock`
- Delete: `docs/plans/2026-08-28-tokenmaxxing-design.md`
- Delete: `docs/superpowers/plans/2026-08-28-tokenmaxxing-implementation.md`

**Interfaces:**
- README installation uses `uv tool install tokenmaxxing-history` and source installation.
- MIT license grants permission to use, copy, modify, publish, and distribute.

- [ ] **Step 1: Write the concise public README**

Include:

- what the tool counts and the four supported harnesses;
- Python 3.12+ installation and the three commands;
- default source and database paths plus overrides;
- an explicit statement that ChatGPT desktop and live rate limits are unsupported;
- cost-coverage caveat: Pi reports source cost, OpenCode reports an estimate, and Codex/Claude missing cost is unavailable;
- privacy boundary and warning never to publish the database, salt, WAL, or SHM;
- deletion instructions for the local data directory;
- development commands `uv sync --locked` and `uv run pytest`.

- [ ] **Step 2: Add MIT license and complete package metadata**

Add `readme`, `license`, authors, project URLs, classifiers, and keywords. Keep the distribution name distinct from the existing unrelated PyPI package.

- [ ] **Step 3: Harden ignores**

Ignore:

```gitignore
*.sqlite
*.sqlite3
*.db
*-journal
*-wal
*-shm
salt
ownership.json
dist/
```

Do not ignore source migrations or test fixtures.

- [ ] **Step 4: Remove obsolete internal plans and lock dependencies**

Delete the superseded broad OTLP design and implementation plan. Preserve the approved simple-v1 spec and this implementation plan. Run `uv lock` and commit the resulting lockfile.

- [ ] **Step 5: Run documentation/package checks**

Run:

```bash
uv sync --locked
uv run pytest -q
uv build
unzip -l dist/*.whl
tar -tzf dist/*.tar.gz
```

Expected: tests pass; build succeeds; the artifacts include README, license metadata, package code, and both SQL migrations, with no local database or generated export.

- [ ] **Step 6: Commit release preparation**

```bash
git add README.md LICENSE .gitignore pyproject.toml uv.lock
git add -u docs/plans/2026-08-28-tokenmaxxing-design.md \
  docs/superpowers/plans/2026-08-28-tokenmaxxing-implementation.md
git commit -m "docs: prepare tokenmaxxing for public use"
```

---

### Task 8: Real-history import, privacy audit, personal stats, and branch consolidation

**Files:**
- Modify only if a regression is proven by the acceptance commands above.
- Local artifacts: `/private/tmp/tokenmaxxing-v1-*/tokenmaxxing.sqlite3` and aggregate JSON; never commit them.

**Interfaces:**
- Uses installed CLI behavior from Tasks 4-7.
- Produces verified aggregate personal statistics and one clean `main` branch.

- [ ] **Step 1: Run the complete verification suite from a clean feature commit**

Run:

```bash
uv run pytest -q
git diff --check
git status --short
```

Expected: all tests pass, diff check is clean, and only deliberately generated ignored build/local artifacts may exist.

- [ ] **Step 2: Import real history into a fresh private staging directory**

Create a unique directory with `mktemp -d /private/tmp/tokenmaxxing-v1.XXXXXX`, then run:

```bash
uv run tokenmaxxing --db "$STAGING_DB" sync --source all --json
uv run tokenmaxxing --db "$STAGING_DB" stats --group-by source --json
uv run tokenmaxxing --db "$STAGING_DB" stats --group-by model --json
uv run tokenmaxxing --db "$STAGING_DB" stats --group-by day \
  --timezone Asia/Kolkata --json
```

Expected: all four source statuses are `ok`, stats contain no raw identifiers, OpenCode matches its source checksum, and missing costs are null.

- [ ] **Step 3: Prove idempotency**

Record counts and totals, run the same sync a second time, then compare. Expected: zero new observations/events on pass two and byte-for-byte equal aggregate stats apart from generated timestamps in exports.

- [ ] **Step 4: Audit privacy and database health**

Run `PRAGMA integrity_check`, count unresolved error issues and provisional/conflicted events, and scan the SQLite database plus any `-wal`, `-shm`, and `-journal` files for known synthetic privacy sentinels and representative real prompt fragments selected locally without printing them. Expected: integrity is `ok`, no unexpected error/conflict rows, and no forbidden content matches.

- [ ] **Step 5: Install the built wheel in a clean temporary environment**

Create a temporary uv environment, install the wheel without the repository on `PYTHONPATH`, and run:

```bash
tokenmaxxing --help
tokenmaxxing --db /private/tmp/tokenmaxxing-wheel-smoke.sqlite3 stats --json
```

Expected: the entry point works and opens/migrates a fresh database.

- [ ] **Step 6: Merge and remove the redundant branch/worktree**

After all agents are stopped and the feature worktree is clean:

```bash
git -C /Users/anjay/Documents/tokenmaxxing merge --ff-only feat/tokenmaxxing-v1
git -C /Users/anjay/Documents/tokenmaxxing worktree remove \
  /Users/anjay/Documents/tokenmaxxing/.worktrees/tokenmaxxing-v1
git -C /Users/anjay/Documents/tokenmaxxing branch -d feat/tokenmaxxing-v1
```

Expected: `main` points to the tested implementation commit, the redundant worktree and feature branch are gone, and no merge commit is created.

- [ ] **Step 7: Report personal stats and validation limits**

Report overall and per-source totals, top models, active days/date range, and Pi/OpenCode cost coverage. State explicitly that OpenCode cost is an upstream estimate, Codex and Claude cost is unavailable when their histories do not report it, ChatGPT desktop is not included, and native-provider billing totals were not independently reconciled.
