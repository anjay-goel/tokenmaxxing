# Profile Publishing Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add a clean profile-project workflow that builds, previews, safely deploys, and schedules a privacy-safe static usage profile from Tokenmaxxing's existing accounting data.

**Architecture:** Keep accounting in the existing repository/reporting/pricing layers and add a focused `tokenmaxxing.profile` package for configuration, aggregation, rendering, builds, deployment, scheduling, and profile CLI handlers. A human-edited YAML project is the only profile input; builds are static, deployment executes a previously approved argv list without a shell, and generated state remains ignored. Refactor the saved profile snapshot in `/Users/anjay/Documents/tokenmaxxing-profile-v0` into packaged Jinja templates and static assets instead of embedding a generated page in Python.

**Tech Stack:** Python 3.12+, SQLite, argparse, Rich, PyYAML 6, Jinja 3.1, tzdata, tzlocal, semantic HTML, plain CSS, plain JavaScript, pytest, Ruff, uv.

## Global Constraints

- All headline cards cover today plus the previous 27 local calendar days.
- Only profile fields and aggregate statistics may enter the generated site.
- Never publish prompts, responses, reasoning, tool content, paths, source artifacts, or session, run, turn, and event identifiers.
- Reuse `event_total`, `ReportWindow`, `usage_stats_rows`, and `estimate_api_value_rows`; do not duplicate token arithmetic or pricing.
- An agent is one source-scoped session with direct counted usage or one distinct source-scoped run with counted usage, including child and subagent runs.
- The deploy command is an argv list; never invoke a shell or interpolate anything except `{site_dir}`.
- A failed sync, render, validation, or replacement must preserve or restore the previous successful local site. A failed deployment keeps the new validated local build available for inspection or retry.
- macOS scheduling uses a user LaunchAgent; Linux scheduling uses a user systemd timer when available and otherwise prints an explicit cron recipe; Windows uses an owned current-user Task Scheduler XML definition through `schtasks`.
- Core sync, stats, export, profile config, build, preview, and native-executable deployment must work on macOS, Linux, and Windows; Windows is the lowest-priority supported platform, not an untested POSIX fallback.
- The rendered page has no frontend framework, analytics, third-party requests, or runtime data fetches.
- JavaScript must stay below 15 KB compressed; initial compressed HTML and CSS should stay around 60 KB excluding fonts and images.
- Preview builds always use `noindex, nofollow`.
- Use plain Conventional Commit messages with no attribution trailers.
- Preserve the untracked `tokenmaxxing-export.json` file.

## File structure

Create these focused modules:

```text
src/tokenmaxxing/
|-- presentation.py                 # shared number formatting and usage quips
`-- profile/
    |-- __init__.py                 # stable public profile imports
    |-- config.py                   # YAML dataclasses, parsing, discovery
    |-- project.py                  # init, editor, project/generated paths
    |-- data.py                     # 28-day and activity aggregates
    |-- render.py                   # Jinja rendering and public JSON
    |-- build.py                    # temp build, validation, rollback-safe replace
    |-- deploy.py                   # argv expansion, approval, execution
    |-- schedule.py                 # LaunchAgent/systemd/Task Scheduler ownership
    |-- cli.py                      # profile argparse and terminal handlers
    |-- starters/                   # init and host starter files
    |-- templates/
    |   |-- index.html.j2
    |   `-- partials/
    |       |-- activity.html.j2
    |       |-- agents.html.j2
    |       |-- header.html.j2
    |       `-- models.html.j2
    `-- assets/
        |-- profile.css
        |-- profile.js
        |-- icons/*.svg
        `-- fonts/*
```

Add one test module per responsibility under `tests/profile/`. Keep
`src/tokenmaxxing/cli.py` responsible only for global commands and registration;
profile command behavior belongs in `profile/cli.py`.

---

### Task 1: Shared presentation helpers

**Files:**
- Create: `src/tokenmaxxing/presentation.py`
- Modify: `src/tokenmaxxing/cli.py`
- Create: `tests/test_presentation.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: `ApiValueEstimate` from `tokenmaxxing.pricing`.
- Produces: `compact_tokens(int) -> str`, `compact_usd(int) -> str`, `api_value_text(ApiValueEstimate) -> str | None`, and `usage_quip(int) -> str`.

- [ ] **Step 1: Write failing helper tests**

```python
from tokenmaxxing.presentation import (
    api_value_text,
    compact_tokens,
    compact_usd,
    usage_quip,
)
from tokenmaxxing.pricing import ApiValueEstimate


def test_compact_values_keep_one_useful_decimal() -> None:
    assert compact_tokens(13_343_876_259) == "13.3B"
    assert compact_tokens(999_999) == "1M"
    assert compact_usd(8_059_030_000_000) == "$8.1K"


def test_usage_quip_is_shared_for_the_same_window_total() -> None:
    assert usage_quip(13_343_876_259) == "The tokens have unionized."


def test_api_value_text_requires_ninety_five_percent_coverage() -> None:
    estimate = ApiValueEstimate(
        cost_nanos=8_059_030_000_000,
        priced_tokens=95,
        total_tokens=100,
        priced_events=1,
        total_events=1,
        by_provider=(),
    )
    assert api_value_text(estimate) == "$8.1K"
```

- [ ] **Step 2: Run the focused test and verify the import fails**

Run: `uv run pytest tests/test_presentation.py -q`

Expected: collection fails because `tokenmaxxing.presentation` does not exist.

- [ ] **Step 3: Move the exact presentation behavior into the shared module**

```python
def compact_tokens(tokens: int) -> str:
    scales = ((1_000_000_000, "B"), (1_000_000, "M"), (1_000, "K"))
    for index, (threshold, suffix) in enumerate(scales):
        if tokens >= threshold:
            scaled = f"{tokens / threshold:.1f}"
            if scaled == "1000.0" and index:
                threshold, suffix = scales[index - 1]
                scaled = f"{tokens / threshold:.1f}"
            return f"{scaled.rstrip('0').rstrip('.')}{suffix}"
    return str(tokens)


def compact_usd(cost_nanos: int) -> str:
    if cost_nanos == 0:
        return "$0"
    dollars = Decimal(cost_nanos) / Decimal(1_000_000_000)
    if dollars < Decimal("0.01"):
        return "<$0.01"
    if dollars < 10:
        value = dollars.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
        return f"${format(value, 'f').rstrip('0').rstrip('.')}"
    if dollars < 100:
        return f"${dollars.quantize(Decimal('1'), rounding=ROUND_HALF_UP)}"
    scales = (
        (Decimal(1_000_000_000), "B"),
        (Decimal(1_000_000), "M"),
        (Decimal(1_000), "K"),
        (Decimal(1), ""),
    )
    scale_index, threshold, suffix = next(
        (index, threshold, suffix)
        for index, (threshold, suffix) in enumerate(scales)
        if dollars >= threshold
    )
    value = (dollars / threshold).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    if value == 1000 and scale_index:
        threshold, suffix = scales[scale_index - 1]
        value = (dollars / threshold).quantize(Decimal("0.1"), rounding=ROUND_HALF_UP)
    return f"${format(value, 'f').rstrip('0').rstrip('.')}{suffix}"


def api_value_text(estimate: ApiValueEstimate) -> str | None:
    if (
        estimate.total_tokens
        and estimate.priced_tokens * 100 < estimate.total_tokens * 95
    ):
        return None
    return compact_usd(estimate.cost_nanos)


def usage_quip(tokens: int) -> str:
    for upper_bound, copy in _QUIPS:
        if tokens < upper_bound:
            return copy
    return "The tokens have unionized."
```

Copy the current thresholds and wording unchanged. Update `cli.py` to import
these helpers. Preserve the CLI's `API equivalent:` prefix and `≈` display in
its own terminal-only wrapper while the profile receives the plain formatted
amount.

- [ ] **Step 4: Run presentation and existing CLI tests**

Run: `uv run pytest tests/test_presentation.py tests/test_cli.py -q`

Expected: all tests pass and the 28-day CLI quip remains byte-for-byte the same.

- [ ] **Step 5: Commit the extraction**

```bash
git add src/tokenmaxxing/presentation.py src/tokenmaxxing/cli.py tests/test_presentation.py tests/test_cli.py
git commit -m "refactor: share usage presentation helpers"
```

### Task 2: Strict profile configuration and project initialization

**Files:**
- Modify: `pyproject.toml`
- Modify: `uv.lock`
- Modify: `src/tokenmaxxing/config.py`
- Modify: `src/tokenmaxxing/sync.py`
- Modify: `src/tokenmaxxing/ingest/opencode.py`
- Modify: `src/tokenmaxxing/cli.py`
- Create: `src/tokenmaxxing/profile/__init__.py`
- Create: `src/tokenmaxxing/profile/config.py`
- Create: `src/tokenmaxxing/profile/project.py`
- Create: `src/tokenmaxxing/profile/starters/config.yaml`
- Create: `src/tokenmaxxing/profile/starters/custom.css`
- Create: `src/tokenmaxxing/profile/starters/gitignore`
- Create: `src/tokenmaxxing/profile/starters/wrangler.jsonc`
- Create: `tests/profile/conftest.py`
- Create: `tests/profile/test_config.py`
- Create: `tests/profile/test_project.py`
- Modify: `tests/test_config.py`
- Modify: `tests/test_sync.py`
- Modify: `tests/test_opencode_import.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Produces: `ProfileConfig`, `ProfileInfo`, `ProfileLink`, `SiteConfig`, `MetricsConfig`, `DeployConfig`, `ScheduleConfig`, `ProfilePaths` frozen dataclasses.
- Produces: `discover_config(start: Path) -> Path`, `load_config(path: Path) -> ProfileConfig`, `write_initial_config(path: Path, config: ProfileConfig) -> None`, `profile_paths(config_path: Path) -> ProfilePaths`, `initialize_project(directory: Path, *, editable_template: bool, force: bool) -> Path`, and `open_editor(config_path: Path, environ: Mapping[str, str]) -> int`.

- [ ] **Step 0: Add failing cross-platform foundation tests**

Add table-driven tests before implementation:

```python
def test_windows_data_dir_uses_local_app_data(tmp_path: Path) -> None:
    paths = default_paths(
        tmp_path,
        {"LOCALAPPDATA": r"C:\Users\Ada\AppData\Local"},
        "win32",
    )
    assert paths.data_dir == Path(r"C:\Users\Ada\AppData\Local") / "tokenmaxxing"


def test_windows_opencode_default_remains_under_user_profile(tmp_path: Path) -> None:
    roots = SourceRoots.defaults(home=tmp_path, environ={}, platform="win32")
    assert roots.opencode_db == tmp_path / ".local" / "share" / "opencode" / "opencode.db"
```

Add an OpenCode read-only import fixture whose database path contains spaces
and whose committed rows are present in `-wal`; assert native `Path.as_uri()`
opens the snapshot on every platform. Add local-timezone tests that monkeypatch
`tzlocal.get_localzone()` to a DST-observing `ZoneInfo` and retain the existing
POSIX `/etc/localtime` cases. Verify `chmod` failures on Windows do not make a
valid salt unreadable.

- [ ] **Step 0a: Implement portable core paths and timezones**

Add `tzdata>=2025.2` and `tzlocal>=5,<6`. `default_paths` uses
`%LOCALAPPDATA%\tokenmaxxing`, falling back to
`home / "AppData" / "Local" / "tokenmaxxing"`, after the existing
`TOKENMAXXING_HOME` override. `SourceRoots.defaults` accepts injected `environ`
and `platform` while preserving OpenCode's Windows `~/.local/share` location.
Use `path.resolve().as_uri() + "?mode=ro"` for the OpenCode SQLite URI. Make
`chmod(0o600)` best-effort on Windows because ACLs, not POSIX mode bits, are
authoritative. Use `tzlocal.get_localzone()` before the fixed-offset fallback.

- [ ] **Step 1: Add failing configuration tests**

Create the shared test configuration in `tests/profile/conftest.py`:

```python
MINIMAL_CONFIG = """\
version: 1
profile:
  name: Ada Lovelace
  role: Programmer
  avatar: avatar.webp
  links: []
site:
  title: Ada's token trail
  description: Aggregate local AI agent usage.
  canonical_url: https://example.com/tokens/
  indexable: true
  timezone: UTC
  theme: auto
  accent: violet
metrics:
  window_days: 28
deploy:
  command: [fake-deploy, "{site_dir}"]
schedule:
  time: "09:00"
"""


@pytest.fixture
def profile_config_path(tmp_path: Path) -> Path:
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_CONFIG, encoding="utf-8")
    (tmp_path / "avatar.webp").write_bytes(b"avatar")
    return path
```

```python
def test_load_config_rejects_unknown_keys(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text("version: 1\nprofile:\n  name: Ada\n  typo: true\n")
    with pytest.raises(ValueError, match="profile.typo"):
        load_config(path)


def test_load_config_rejects_assets_outside_project(tmp_path: Path) -> None:
    path = tmp_path / "config.yaml"
    path.write_text(MINIMAL_CONFIG.replace("avatar: avatar.webp", "avatar: ../avatar.webp"))
    with pytest.raises(ValueError, match="inside the profile project"):
        load_config(path)


def test_discover_config_walks_parents(tmp_path: Path) -> None:
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG)
    child = tmp_path / "one" / "two"
    child.mkdir(parents=True)
    assert discover_config(child) == config
```

Include table-driven cases for version, `https`/`mailto` link schemes, timezone,
theme, positive `window_days`, `HH:MM` schedule time, empty deploy argv, unknown
placeholders, scalar deploy commands, and symlinks escaping the project.

- [ ] **Step 2: Run the configuration tests and verify failure**

Run: `uv run pytest tests/profile/test_config.py -q`

Expected: collection fails because `tokenmaxxing.profile.config` does not exist.

- [ ] **Step 3: Add PyYAML and Jinja dependencies and implement typed parsing**

Add these runtime dependencies:

```toml
dependencies = [
    "jinja2>=3.1,<4",
    "pyyaml>=6,<7",
    "rich>=15,<16",
    "tzdata>=2025.2",
    "tzlocal>=5,<6",
]
```

Define immutable defaults exactly:

```python
@dataclass(frozen=True, slots=True)
class ProfileLink:
    label: str
    value: str
    url: str


@dataclass(frozen=True, slots=True)
class ProfileInfo:
    name: str
    role: str = ""
    bio: str = ""
    avatar: Path | None = None
    links: tuple[ProfileLink, ...] = ()


@dataclass(frozen=True, slots=True)
class SiteConfig:
    title: str
    description: str
    canonical_url: str
    indexable: bool
    timezone: ZoneInfo
    theme: Literal["auto", "light", "dark"] = "auto"
    accent: str = "violet"


@dataclass(frozen=True, slots=True)
class MetricsConfig:
    window_days: int = 28
    show_api_equivalent: bool = True
    show_agents: bool = True
    show_peak_usage: bool = True
    show_longest_streak: bool = True
    show_models: bool = True


@dataclass(frozen=True, slots=True)
class DeployConfig:
    command: tuple[str, ...] = ()


@dataclass(frozen=True, slots=True)
class ScheduleConfig:
    time: time = time(hour=9)


@dataclass(frozen=True, slots=True)
class ProfileConfig:
    version: Literal[1]
    profile: ProfileInfo
    site: SiteConfig
    metrics: MetricsConfig = MetricsConfig()
    deploy: DeployConfig = DeployConfig()
    schedule: ScheduleConfig = ScheduleConfig()
```

Use `yaml.safe_load`, explicit `_expect_mapping`, `_expect_keys`, `_string`,
`_boolean`, and `_path_inside_project` helpers, and `ZoneInfo` validation. Never
construct dataclasses with `**raw_mapping`. Return errors with dotted YAML paths.
`write_initial_config` converts these dataclasses to an explicit ordered mapping
of strings, booleans, integers, lists, and mappings before `yaml.safe_dump`; it
is used only by initialization, never by `profile edit`.

- [ ] **Step 4: Add failing initialization tests**

```python
def test_initialize_project_creates_only_editable_starters(tmp_path: Path) -> None:
    project = tmp_path / "profile"
    config = initialize_project(project, editable_template=False, force=False)
    assert config == project / "config.yaml"
    assert (project / "custom.css").read_text() == ""
    assert ".tokenmaxxing/" in (project / ".gitignore").read_text()
    assert not (project / "template").exists()


def test_force_never_overwrites_an_editable_file(tmp_path: Path) -> None:
    project = tmp_path / "profile"
    project.mkdir()
    css = project / "custom.css"
    css.write_text("body { color: hotpink; }\n")
    initialize_project(project, editable_template=False, force=True)
    assert css.read_text() == "body { color: hotpink; }\n"
```

- [ ] **Step 5: Implement project paths, safe initialization, and editor launch**

```python
@dataclass(frozen=True, slots=True)
class ProfilePaths:
    root: Path
    config: Path
    generated: Path
    site: Path
    build_state: Path
    deploy_approval: Path
    logs: Path


def profile_paths(config_path: Path) -> ProfilePaths:
    root = config_path.resolve().parent
    generated = root / ".tokenmaxxing"
    return ProfilePaths(
        root=root,
        config=config_path.resolve(),
        generated=generated,
        site=generated / "site",
        build_state=generated / "build.json",
        deploy_approval=generated / "deploy-approval.json",
        logs=generated / "logs",
    )
```

`initialize_project` refuses a non-empty directory without `force`, uses
`importlib.resources` to copy packaged starters, and writes files with UTF-8.
`open_editor` tokenizes `$VISUAL` or `$EDITOR` with POSIX rules on macOS/Linux
and Windows command-line rules on Windows, falling back to `notepad.exe` there.
It calls `subprocess.run([*editor, str(config_path)], shell=False, check=False)`
and waits for the editor. Tests cover a quoted editor path under `Program Files`.

- [ ] **Step 6: Run configuration and project tests**

Run: `uv run pytest tests/profile/test_config.py tests/profile/test_project.py -q`

Expected: all tests pass.

- [ ] **Step 7: Lock dependencies and commit**

```bash
uv lock
git add pyproject.toml uv.lock src/tokenmaxxing/profile tests/profile/conftest.py tests/profile/test_config.py tests/profile/test_project.py
git commit -m "feat: add profile project configuration"
```

### Task 3: Profile aggregation and exact agent accounting

**Files:**
- Modify: `src/tokenmaxxing/models.py`
- Modify: `src/tokenmaxxing/repository.py`
- Modify: `src/tokenmaxxing/reporting.py`
- Modify: `src/tokenmaxxing/ingest/opencode.py`
- Create: `src/tokenmaxxing/profile/data.py`
- Modify: `tests/test_repository.py`
- Modify: `tests/test_reporting.py`
- Modify: `tests/test_opencode_import.py`
- Create: `tests/profile/test_data.py`
- Modify: `docs/architecture.md`

**Interfaces:**
- Produces: private `ProfileUsageRow(usage: ReportingRow, agent_key: str | None)`; `agent_key` never enters general reporting or serialization.
- Produces: `Repository.profile_reporting_rows() -> list[ProfileUsageRow]` from one counted-event snapshot.
- Produces: `ModelTotal`, `HarnessTotal`, `DailyTotal`, `AgentModelTotal`, `DailyAgentTotal`, and `ProfileData` frozen dataclasses.
- Produces: `build_profile_data(rows: Sequence[ProfileUsageRow], *, timezone: tzinfo, now: datetime, window_days: int) -> ProfileData`.

- [ ] **Step 1: Write failing OpenCode child-execution tests**

Extend the existing OpenCode root/child fixture and assert:

```python
root_event = event_for("root-part")
child_events = events_for_session("child")
child_run = run_for_source_session("child")

assert root_event["session_id"] == root_database_id
assert root_event["run_id"] is None
assert {event["session_id"] for event in child_events} == {root_database_id}
assert {event["run_id"] for event in child_events} == {child_run["id"]}
assert child_run["parent_run_id"] == "root"
```

Add root + child + grandchild, child fallback-message, repeated sync, and legacy
repair cases. A repair clears a child event's `run_id`, resyncs, and asserts the
same event key/tokens/root session with the child run restored. Counted event
totals must not change.

- [ ] **Step 2: Run the OpenCode tests and verify child runs are missing**

Run: `uv run pytest tests/test_opencode_import.py -k "child or legacy" -q`

Expected: child events have no `run_id` and no child `runs` row.

- [ ] **Step 3: Materialize OpenCode child runs without changing token events**

Keep root session attribution. Create one `RunDraft` for every physical
OpenCode session with `parent_session_id`:

```python
RunDraft(
    source="opencode",
    source_session_id=child.source_session_id,
    source_run_id=child.source_session_id,
    parent_run_id=child.parent_session_id,
    started_at_ns=child.started_at_ns,
)
```

Set `UsageEventDraft.session_id` to the existing root database session and
`run_id` to the physical child run. Root events keep `run_id=None`. Extend the
current-event comparison to include both IDs so a normal sync repairs legacy
root-only events. Do not change event keys, observation keys, token/cost fields,
or create an additional usage event. No migration is needed.

- [ ] **Step 4: Add failing private profile-row tests**

Create canonical fixtures containing repeated root calls, two events in one
child run, a nested child, a positive event without session/run identity, a
zero-token counted event, and an excluded marker. Assert:

```python
rows = repository.profile_reporting_rows()
assert [row.agent_key for row in rows] == [
    "codex:session:1",
    "codex:session:1",
    "codex:run:2",
    "codex:run:2",
    "codex:run:3",
    None,
    "codex:session:4",
]
assert all(not hasattr(row.usage, "agent_key") for row in rows)
```

The zero-token row may have a key at the repository layer but cannot become an
agent during aggregation. Excluded/conflicted rows do not appear.

- [ ] **Step 5: Add the private profile reporting wrapper**

Define:

```python
@dataclass(frozen=True, slots=True)
class ProfileUsageRow:
    usage: ReportingRow
    agent_key: str | None
```

`Repository.profile_reporting_rows()` reuses the same counted-event SELECT and
model/timestamp precedence as `reporting_rows()` and adds this private grouping:

```sql
CASE
    WHEN e.run_id IS NOT NULL AND r.parent_run_id IS NOT NULL
        THEN e.source || ':run:' || e.run_id
    WHEN COALESCE(e.session_id, r.session_id) IS NOT NULL
        THEN e.source || ':session:' || COALESCE(e.session_id, r.session_id)
    ELSE NULL
END AS agent_key
```

This makes session-sentinel/root runs one root agent and genuine child runs one
additional agent across Codex, Claude, Pi, and OpenCode. General
`Repository.reporting_rows()`, stats, and exports remain unchanged. Never
serialize `ProfileUsageRow` or `agent_key`.

- [ ] **Step 6: Add failing aggregation tests**

Use `ProfileUsageRow` fixtures for an explicit
`2026-08-30T12:00:00+05:30` clock. Cover repeated calls in one root, a root plus
child run, Claude iterations/advisor calls within one execution, Pi direct plus
reconciled subagent and batch marker, OpenCode root/child, a positive event with
no identity, a zero-token row, window boundaries, missing timestamps, DST,
primary-model ties, empty data, one agent active across days, and values above
one trillion. Core assertions:

```python
data = build_profile_data(
    profile_rows,
    timezone=ZoneInfo("Asia/Kolkata"),
    now=datetime(2026, 8, 30, 12, tzinfo=ZoneInfo("Asia/Kolkata")),
    window_days=28,
)

assert data.window_start.isoformat() == "2026-08-03"
assert data.window_end.isoformat() == "2026-08-30"
assert data.total_tokens == sum(event_total(row) for row in recent_rows)
assert data.agent_count == 3
assert data.agent_models[0] == AgentModelTotal(model="gpt-5.6-sol", agents=2)
assert data.peak_usage == max(day.total_tokens for day in data.activity_days[-28:])
assert data.longest_streak == 2
assert len(data.recent_days) == 28
assert len(data.activity_days) == 364
```

- [ ] **Step 7: Implement deterministic aggregation from one row snapshot**

Use these exact shapes:

```python
@dataclass(frozen=True, slots=True)
class ModelTotal:
    model: str
    total_tokens: int
    provider: str | None = None


@dataclass(frozen=True, slots=True)
class HarnessTotal:
    harness: str
    total_tokens: int


@dataclass(frozen=True, slots=True)
class DailyTotal:
    day: date
    total_tokens: int
    models: tuple[ModelTotal, ...]


@dataclass(frozen=True, slots=True)
class AgentModelTotal:
    model: str
    agents: int


@dataclass(frozen=True, slots=True)
class DailyAgentTotal:
    day: date
    agents: int
    models: tuple[AgentModelTotal, ...]


@dataclass(frozen=True, slots=True)
class ProfileData:
    generated_at: datetime
    window_start: date
    window_end: date
    total_tokens: int
    all_time_tokens: int
    api_equivalent: ApiValueEstimate
    agent_count: int
    peak_usage: int
    longest_streak: int
    model_count: int
    models: tuple[ModelTotal, ...]
    harnesses: tuple[HarnessTotal, ...]
    agent_models: tuple[AgentModelTotal, ...]
    recent_days: tuple[DailyAgentTotal, ...]
    activity_days: tuple[DailyTotal, ...]
    first_tracked_day: date | None
    quip: str
```

Filter the 28-day window with a `ReportWindow` created from `window_days`; add a
`ReportWindow.from_days(day_count, timezone, now)` constructor and keep
`from_period` delegating to it. Price the same filtered rows with
`estimate_api_value_rows(row.usage for row in recent_rows)`. Aggregate 364 days ending on the current local day
for exactly 52 seven-day columns. Sort models by descending tokens then name.
For each non-null `agent_key` on a row where `event_total(row.usage) > 0`,
select the primary model by descending summed tokens then alphabetical name and
count the key once. Daily agent stacks use that stable 28-day primary model and
distinct `(day, agent_key)` pairs. `DailyAgentTotal.agents` and each nested
`AgentModelTotal.agents` are agent counts; `DailyTotal.total_tokens` and each
nested `ModelTotal.total_tokens` remain token counts exclusively. Compute
streak only from recent days containing a positive-token agent; unidentified usage still contributes to
tokens, models, API equivalent, activity, and peak usage. Missing-timestamp rows
contribute only to all-time tokens.

The saved snapshot's `3,196` Agents value is a legacy all-time/static number,
not a 28-day acceptance target. The current ignored database yielded `1,764`
under the audited 28-day definition before the OpenCode repair; regenerate from
the implemented semantics rather than hard-coding either value.

- [ ] **Step 8: Run importer, reporting, repository, pricing, and profile tests**

Run: `uv run pytest tests/test_opencode_import.py tests/test_reporting.py tests/test_repository.py tests/test_pricing.py tests/profile/test_data.py -q`

Expected: all tests pass and existing stats/export payloads are unchanged.

- [ ] **Step 9: Commit aggregation**

```bash
git add src/tokenmaxxing/models.py src/tokenmaxxing/repository.py src/tokenmaxxing/reporting.py src/tokenmaxxing/ingest/opencode.py src/tokenmaxxing/profile/data.py tests/test_repository.py tests/test_reporting.py tests/test_opencode_import.py tests/profile/test_data.py docs/architecture.md
git commit -m "feat: aggregate profile usage data"
```

### Task 4: Readable static profile renderer

**Files:**
- Create: `src/tokenmaxxing/profile/render.py`
- Create: `src/tokenmaxxing/profile/templates/index.html.j2`
- Create: `src/tokenmaxxing/profile/templates/partials/header.html.j2`
- Create: `src/tokenmaxxing/profile/templates/partials/activity.html.j2`
- Create: `src/tokenmaxxing/profile/templates/partials/agents.html.j2`
- Create: `src/tokenmaxxing/profile/templates/partials/models.html.j2`
- Create: `src/tokenmaxxing/profile/assets/profile.css`
- Create: `src/tokenmaxxing/profile/assets/profile.js`
- Create: `src/tokenmaxxing/profile/assets/icons/openai.svg`
- Create: `src/tokenmaxxing/profile/assets/icons/claude.svg`
- Create: `src/tokenmaxxing/profile/assets/icons/opencode.svg`
- Create: `src/tokenmaxxing/profile/assets/icons/google.svg`
- Create: `src/tokenmaxxing/profile/assets/icons/deepseek.svg`
- Create: `src/tokenmaxxing/profile/assets/icons/zai.svg`
- Create: `src/tokenmaxxing/profile/assets/icons/moonshot.svg`
- Create: `src/tokenmaxxing/profile/assets/icons/xai.svg`
- Create: `src/tokenmaxxing/profile/assets/icons/mistral.svg`
- Create: `src/tokenmaxxing/profile/assets/icons/qwen.svg`
- Create: `src/tokenmaxxing/profile/assets/ASSET_SOURCES.md`
- Create: `src/tokenmaxxing/profile/assets/licenses/Lobe-Icons-MIT.txt`
- Create: `src/tokenmaxxing/profile/assets/fonts/inter-latin.woff2`
- Create: `src/tokenmaxxing/profile/assets/fonts/jetbrains-mono-latin.woff2`
- Create: `src/tokenmaxxing/profile/assets/fonts/newsreader-latin.woff2`
- Create: `src/tokenmaxxing/profile/assets/fonts/newsreader-latin-italic.woff2`
- Create: `src/tokenmaxxing/profile/assets/fonts/OFL.txt`
- Create: `src/tokenmaxxing/profile/assets/fonts/NOTICE.md`
- Modify: `src/tokenmaxxing/profile/data.py`
- Create: `tests/profile/test_render.py`
- Create: `tests/profile/test_public_payload.py`
- Modify: `tests/profile/test_data.py`

**Interfaces:**
- Consumes: `ProfileConfig`, `ProfileData`, `ProfilePaths`.
- Produces: `RenderedSite` frozen dataclass.
- Produces: `public_payload(config: ProfileConfig, data: ProfileData) -> dict[str, JsonValue]` and `render_site(config: ProfileConfig, data: ProfileData, paths: ProfilePaths, destination: Path, *, noindex: bool) -> RenderedSite`.

- [ ] **Step 1: Write failing public-payload and escaping tests**

Extend `tests/profile/conftest.py` with deterministic object fixtures:

```python
@pytest.fixture
def profile_config(profile_config_path: Path) -> ProfileConfig:
    return load_config(profile_config_path)


@pytest.fixture
def profile_data() -> ProfileData:
    timezone = ZoneInfo("UTC")
    return build_profile_data(
        (),
        timezone=timezone,
        now=datetime(2026, 8, 30, 12, tzinfo=timezone),
        window_days=28,
    )
```

```python
def test_public_payload_has_only_allowlisted_aggregate_fields(profile_config, profile_data) -> None:
    payload = public_payload(profile_config, profile_data)
    encoded = json.dumps(payload, sort_keys=True)
    assert set(payload) == {"schema_version", "generated_at", "profile", "site", "stats"}
    for forbidden in ("session_id", "run_id", "event_id", "path", "prompt", "reasoning"):
        assert forbidden not in encoded


def test_render_escapes_profile_values(tmp_path, profile_config_path, profile_config, profile_data) -> None:
    hostile = replace(profile_config, profile=replace(profile_config.profile, name='<script>alert(1)</script>'))
    render_site(hostile, profile_data, profile_paths(profile_config_path), tmp_path, noindex=True)
    html = (tmp_path / "index.html").read_text()
    assert "<script>alert(1)</script>" not in html
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in html
    assert '<meta name="robots" content="noindex, nofollow">' in html
```

Also assert visible headline text exists in HTML without executing JavaScript.
At 95% priced-token coverage the four cards are API equivalent, Agents, Peak
usage, and Longest streak in that order; below the threshold they are Agents,
Peak usage, Longest streak, and Models. Empty stats render without broken
charts, model tooltips group shares below five percent into `other models`, and
`unknown models` stays lowercase.

- [ ] **Step 2: Run renderer tests and verify failure**

Run: `uv run pytest tests/profile/test_render.py tests/profile/test_public_payload.py -q`

Expected: collection fails because `tokenmaxxing.profile.render` does not exist.

- [ ] **Step 3: Implement the public schema and Jinja environment**

```python
def _environment(template_root: Path | None) -> SandboxedEnvironment:
    loaders = []
    if template_root is not None:
        loaders.append(FileSystemLoader(template_root))
    loaders.append(PackageLoader("tokenmaxxing.profile", "templates"))
    return SandboxedEnvironment(
        loader=ChoiceLoader(loaders),
        autoescape=select_autoescape(("html", "xml", "j2")),
        undefined=StrictUndefined,
        trim_blocks=True,
        lstrip_blocks=True,
    )
```

Register filters that call `compact_tokens`, `compact_usd`, and an HTML-safe
date formatter. Build the payload from new dicts; never call `asdict` on
database-facing types. Write JSON with `ensure_ascii=False`, `sort_keys=True`,
compact separators, and a trailing newline.
The public profile avatar field is only the generated relative asset URL or
null; it never contains the absolute configured source path.

```python
@dataclass(frozen=True, slots=True)
class RenderedSite:
    destination: Path
    files: tuple[Path, ...]


def public_payload(config: ProfileConfig, data: ProfileData) -> dict[str, JsonValue]:
    return {
        "schema_version": 1,
        "generated_at": data.generated_at.isoformat(),
        "profile": _public_profile(config.profile),
        "site": _public_site(config.site),
        "stats": _public_stats(data),
    }
```

Extend `ModelTotal` with `provider: str | None` and choose the provider that
contributes the most tokens to that resolved model in the same window, then
alphabetically on ties. This provider is public aggregate categorization, not a
private identifier. Canonicalize known model creators before selecting the
token-majority provider. Treat hosts and harnesses as neutral, then use
conservative, boundary-anchored model-family matching only when creator metadata
is absent. Keep explicit unknown models neutral. Select harness icons separately.

- [ ] **Step 4: Refactor the saved snapshot into templates and assets**

Use `/Users/anjay/Documents/tokenmaxxing-profile-v0/index.html` and
`.superpowers/sdd/2026-08-30-profile-publishing/task-4-design.md` as the visual
contract. Preserve its approved compact 210px left profile rail, right theme
switch, baseline-aligned 28-day headline, quip spacing, fixed activity grid,
28-day agent histogram, 228px scrollable model/harness list, tooltips,
light/dark appearance, and responsive behavior. Replace every embedded
statistic and profile string with a Jinja variable.

The base template must contain this semantic skeleton:

```html
<main class="profile-shell">
  {% include "partials/header.html.j2" %}
  <section class="usage" aria-labelledby="usage-title">
    <h1 id="usage-title"><strong>{{ stats.total_tokens|tokens }}</strong> Tokens</h1>
    <p class="quip">{{ stats.quip }}</p>
    <p class="window-note">Last 28 Days · {{ stats.all_time_tokens|tokens }} all time</p>
  </section>
  {% include "partials/activity.html.j2" %}
  {% include "partials/agents.html.j2" %}
  {% include "partials/models.html.j2" %}
</main>
```

Use `<button>` for the theme switch and Models/Harnesses tabs,
`aria-describedby` for keyboard-focusable chart cells, inline SVG icons with
`aria-hidden="true"`, fixed `aspect-ratio` for the avatar, and
`prefers-reduced-motion`. Activity uses exactly 52 columns x 7 rows with no
horizontal scroll. Empty pre-tracking cells have no border or fill; quiet
tracked days remain visually quieter than usage. Use four deterministic
positive-usage quantiles. `profile.js` handles only early theme persistence,
tab selection/arrow keys, roving chart focus, and viewport-safe tooltip
positioning. It performs no fetch or data calculation.

Copy the configured avatar and `custom.css` into generated assets. A configured
but missing avatar is an error; an omitted avatar renders an initial-based
fallback. Load project templates from `paths.root / "template"` before packaged
templates without exposing that path publicly.

Vendor monochrome icons from pinned Lobe Icons 1.94.0 and retain the three
existing snapshot icons when hashes/provenance match. Record every source URL,
version, retrieval date, and checksum in `ASSET_SOURCES.md`, include the MIT
license, and add OFL/notice files for Inter, JetBrains Mono, and Newsreader.
The built page uses only local assets and no CDN.

- [ ] **Step 5: Render canonical and preview metadata**

Indexable builds include canonical, description, Open Graph, Twitter card,
`ProfilePage`/`Person` JSON-LD, robots, and sitemap. `noindex=True` omits
canonical discovery files, writes `robots.txt` with `Disallow: /`, and adds the
robots meta tag. Every local font, icon, CSS, JS, and image path must be relative
to the generated directory.

- [ ] **Step 6: Run renderer tests and inspect both themes locally**

Run: `uv run pytest tests/profile/test_render.py tests/profile/test_public_payload.py -q`

Tests also assert 364 activity cells, 28 agent bars, daily agent/model
conservation, Models/Harnesses tab semantics, keyboard roving focus hooks,
local-only asset URLs, provider icon/fallback mapping, license notices, and
compressed JavaScript below 15 KB.

Preserve one renderer-test destination under pytest's reported temporary path
for inspection. Inspect desktop and mobile widths, keyboard traversal, reduced
motion, light theme, and dark theme. Record screenshot-only differences as
template or stylesheet fixes, not inline CSS patches in Python. The complete
browser matrix runs again in Task 9 after CLI integration.

- [ ] **Step 7: Commit renderer**

```bash
git add src/tokenmaxxing/profile/render.py src/tokenmaxxing/profile/templates src/tokenmaxxing/profile/assets tests/profile/test_render.py tests/profile/test_public_payload.py
git commit -m "feat: render static usage profiles"
```

### Task 5: Rollback-safe build and validation pipeline

**Files:**
- Create: `src/tokenmaxxing/profile/build.py`
- Create: `tests/profile/test_build.py`

**Interfaces:**
- Consumes: `ProfileConfig`, `ProfileData`, `ProfilePaths`, `render_site`.
- Produces: `BuildResult(site_dir: Path, generated_at: datetime, file_count: int, total_bytes: int)`.
- Produces: `build_profile(config_path: Path, *, db_path: Path, output: Path | None = None, noindex: bool = False, now: datetime | None = None) -> BuildResult` and `validate_site(site_dir: Path, *, noindex: bool) -> None`.

- [ ] **Step 1: Write failing atomicity tests**

Define the local test helpers in `test_build.py`:

```python
def prepared_project(tmp_path: Path) -> ProfilePaths:
    config = tmp_path / "config.yaml"
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    (tmp_path / "avatar.webp").write_bytes(b"avatar")
    return profile_paths(config)


def raising_renderer(*args, **kwargs):
    raise RuntimeError("render failed")


def leaking_renderer(config, data, destination, *, noindex):
    destination.mkdir(parents=True)
    (destination / "index.html").write_text("<html data-total='0'></html>")
    (destination / "profile.json").write_text('{"session_id":"private"}\n')
    (destination / "robots.txt").write_text("User-agent: *\nDisallow: /\n")
```

```python
def test_failed_render_preserves_previous_site(tmp_path, monkeypatch) -> None:
    paths = prepared_project(tmp_path)
    paths.site.mkdir(parents=True)
    (paths.site / "index.html").write_text("previous")
    monkeypatch.setattr("tokenmaxxing.profile.build.render_site", raising_renderer)
    with pytest.raises(RuntimeError, match="render failed"):
        build_profile(paths.config, db_path=tmp_path / "usage.sqlite3")
    assert (paths.site / "index.html").read_text() == "previous"


def test_build_rejects_public_identifier_leak(tmp_path, monkeypatch) -> None:
    monkeypatch.setattr("tokenmaxxing.profile.build.render_site", leaking_renderer)
    with pytest.raises(ValueError, match="forbidden public field"):
        build_profile(prepared_project(tmp_path).config, db_path=tmp_path / "usage.sqlite3")
```

Cover missing assets, absolute URLs to third-party assets, invalid JSON,
HTML references escaping the site, missing robots/canonical metadata, oversized
compressed JS, a successful replacement, Windows `PermissionError` from an open
handle, failure of the second rename, and failure while restoring the backup.

- [ ] **Step 2: Run build tests and verify failure**

Run: `uv run pytest tests/profile/test_build.py -q`

Expected: collection fails because `tokenmaxxing.profile.build` does not exist.

- [ ] **Step 3: Implement one-snapshot build orchestration**

```python
@dataclass(frozen=True, slots=True)
class BuildResult:
    site_dir: Path
    generated_at: datetime
    file_count: int
    total_bytes: int
```

```python
def build_profile(
    config_path: Path,
    *,
    db_path: Path,
    output: Path | None = None,
    noindex: bool = False,
    now: datetime | None = None,
) -> BuildResult:
    config = load_config(config_path)
    paths = profile_paths(config_path)
    destination = output.resolve() if output else paths.site
    database = Database.open(db_path)
    try:
        database.connection.execute("BEGIN")
        rows = Repository(database).reporting_rows()
        database.connection.commit()
    except BaseException:
        database.connection.rollback()
        raise
    finally:
        database.close()
    generated_at = now or datetime.now(config.site.timezone)
    data = build_profile_data(
        rows,
        timezone=config.site.timezone,
        now=generated_at,
        window_days=config.metrics.window_days,
    )
    effective_noindex = noindex or not config.site.indexable
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = Path(tempfile.mkdtemp(prefix=f".{destination.name}-", dir=destination.parent))
    try:
        render_site(config, data, temporary, noindex=effective_noindex)
        validate_site(temporary, noindex=effective_noindex)
        _replace_directory(temporary, destination)
    except BaseException:
        shutil.rmtree(temporary, ignore_errors=True)
        raise
    return _build_result(destination, generated_at)
```

Create the temporary directory beside the destination on the same volume, fsync
written files where practical, rename the old destination to a uniquely named
backup, rename the validated temporary directory into place, then remove the
backup. Do not call this sequence an atomic directory swap: Windows cannot
replace a non-empty target directory in one operation. On rename failure,
restore the backup. If restoration also fails, retain the backup and raise an
error containing its exact recovery path. `_replace_directory` must first
assert that both paths share the same resolved parent and that neither is the
parent itself. Never delete an unresolved broad path.

- [ ] **Step 4: Implement structural and privacy validation**

Parse `profile.json`, walk its key paths against an allowlist, reject keys
containing `session`, `run_id`, `event`, `prompt`, `reasoning`, `path`, or
`artifact`, and verify that its totals match visible HTML data attributes.
Parse HTML references with `html.parser.HTMLParser`; allow only relative local
assets plus configured profile links/canonical metadata. Gzip CSS/JS in memory
to enforce the JavaScript and combined HTML/CSS budgets. Validate required
files and sitemap/robots mode.

- [ ] **Step 5: Run build and upstream accounting tests**

Run: `uv run pytest tests/profile/test_build.py tests/test_reporting.py tests/test_pricing.py -q`

Expected: all tests pass and failure cases preserve the prior site.

- [ ] **Step 6: Commit build pipeline**

```bash
git add src/tokenmaxxing/profile/build.py tests/profile/test_build.py
git commit -m "feat: build profiles atomically"
```

### Task 6: Safe generic deploy runner

**Files:**
- Create: `src/tokenmaxxing/profile/deploy.py`
- Create: `tests/profile/test_deploy.py`

**Interfaces:**
- Produces: `DeployPlan(argv: tuple[str, ...], cwd: Path, canonical_url: str, fingerprint: str)` and `DeployResult(returncode: int, stdout: str, stderr: str)`.
- Produces: `make_deploy_plan(config: ProfileConfig, paths: ProfilePaths) -> DeployPlan`, `is_approved(plan: DeployPlan, approval_path: Path) -> bool`, `approve(plan: DeployPlan, approval_path: Path) -> None`, and `run_deploy(plan: DeployPlan, *, approval_path: Path, non_interactive: bool, confirm: Callable[[DeployPlan], bool]) -> DeployResult`.

- [ ] **Step 1: Write failing deploy safety tests**

```python
def test_deploy_executes_argv_without_a_shell(tmp_path, monkeypatch) -> None:
    plan = DeployPlan(
        argv=("fake-deploy", "--site", str(tmp_path / "site; touch PWNED")),
        cwd=tmp_path,
        canonical_url="https://example.com/profile/",
        fingerprint="abc",
    )
    seen = {}
    monkeypatch.setattr(
        subprocess,
        "run",
        lambda argv, **kwargs: seen.update(argv=argv, kwargs=kwargs)
        or subprocess.CompletedProcess(argv, 0, "published\n", ""),
    )
    run_deploy(
        plan,
        approval_path=tmp_path / "approval.json",
        non_interactive=False,
        confirm=lambda _: True,
    )
    assert seen["argv"] == list(plan.argv)
    assert seen["kwargs"]["shell"] is False
    assert not (tmp_path / "PWNED").exists()


def test_changed_command_invalidates_approval(tmp_path) -> None:
    approve(plan("one"), tmp_path / "approval.json")
    assert is_approved(plan("one"), tmp_path / "approval.json")
    assert not is_approved(plan("two"), tmp_path / "approval.json")
```

Also cover absent command, unknown `{name}`, more than one placeholder per
argument, NUL bytes, nonexistent site directory, corrupt approval JSON,
non-interactive refusal, cancellation, nonzero exit, and bounded captured
stdout/stderr. On Windows, reject `.cmd` and `.bat` executables because the OS
may route them through shell parsing despite `shell=False`; cover mixed-case
suffixes. Permit native `.exe` commands and `node.exe` plus a JavaScript CLI
path as separate argv entries.

- [ ] **Step 2: Run deploy tests and verify failure**

Run: `uv run pytest tests/profile/test_deploy.py -q`

Expected: collection fails because `tokenmaxxing.profile.deploy` does not exist.

- [ ] **Step 3: Implement canonical planning and approval**

```python
@dataclass(frozen=True, slots=True)
class DeployPlan:
    argv: tuple[str, ...]
    cwd: Path
    canonical_url: str
    fingerprint: str


@dataclass(frozen=True, slots=True)
class DeployResult:
    returncode: int
    stdout: str
    stderr: str
```

```python
def _fingerprint(command: tuple[str, ...], cwd: Path, canonical_url: str) -> str:
    document = json.dumps(
        {"command": command, "cwd": str(cwd), "canonical_url": canonical_url},
        sort_keys=True,
        separators=(",", ":"),
    )
    return hashlib.sha256(document.encode()).hexdigest()


def make_deploy_plan(config: ProfileConfig, paths: ProfilePaths) -> DeployPlan:
    argv = tuple(argument.replace("{site_dir}", str(paths.site)) for argument in config.deploy.command)
    return DeployPlan(
        argv=argv,
        cwd=paths.root,
        canonical_url=config.site.canonical_url,
        fingerprint=_fingerprint(config.deploy.command, paths.root, config.site.canonical_url),
    )
```

Write approval atomically with mode `0o600`, schema version `1`, fingerprint,
canonical command template, cwd, canonical URL, and approval time. Approval is
local generated state, not profile YAML.

- [ ] **Step 4: Implement execution with explicit confirmation**

Call:

```python
completed = subprocess.run(
    list(plan.argv),
    cwd=plan.cwd,
    shell=False,
    check=False,
    text=True,
    capture_output=True,
    timeout=900,
)
```

Limit retained output to the final 64 KiB per stream. Raise a typed
`DeployError` on timeout or nonzero return. Interactive confirmation occurs
before `approve(plan, approval_path)`; non-interactive mode never calls
`confirm` and requires a matching approval file.

The Cloudflare onboarding preset is available on Windows only when it can emit
a native executable argv, such as `node.exe` followed by Wrangler's JavaScript
entry point. It must not silently approve `npx.cmd`; otherwise onboarding leaves
deployment unset and explains how to configure a native command.

- [ ] **Step 5: Run deploy tests**

Run: `uv run pytest tests/profile/test_deploy.py -q`

Expected: all tests pass.

- [ ] **Step 6: Commit deploy runner**

```bash
git add src/tokenmaxxing/profile/deploy.py tests/profile/test_deploy.py
git commit -m "feat: add approved profile deployment"
```

### Task 7: Owned operating-system schedules

**Files:**
- Create: `src/tokenmaxxing/profile/schedule.py`
- Create: `tests/profile/test_schedule.py`

**Interfaces:**
- Produces: `ScheduleStatus(enabled: bool, backend: str, job_path: Path | None, command: tuple[str, ...], next_step: str | None)`.
- Produces: `schedule_status(paths: ProfilePaths, *, platform: str, environ: Mapping[str, str]) -> ScheduleStatus`, `enable_schedule(paths: ProfilePaths, config: ProfileConfig, *, executable: Path, db_path: Path, platform: str, environ: Mapping[str, str]) -> ScheduleStatus`, and `disable_schedule(paths: ProfilePaths, *, platform: str, environ: Mapping[str, str]) -> ScheduleStatus` for LaunchAgent, systemd, and Windows Task Scheduler backends.

- [ ] **Step 1: Write failing schedule ownership tests**

Define deterministic helpers in `test_schedule.py`:

```python
def config_at(config_path: Path, value: str) -> ProfileConfig:
    text = MINIMAL_CONFIG.replace('time: "09:00"', f'time: "{value}"')
    config_path.write_text(text, encoding="utf-8")
    (config_path.parent / "avatar.webp").write_bytes(b"avatar")
    return load_config(config_path)


def launch_agent_path(paths: ProfilePaths, home: Path) -> Path:
    identifier = schedule_identifier(paths.root)
    return home / "Library" / "LaunchAgents" / f"{identifier}.plist"
```

```python
def test_launch_agent_uses_absolute_noninteractive_command(tmp_path) -> None:
    status = enable_schedule(
        paths=profile_paths(tmp_path / "config.yaml"),
        config=config_at(tmp_path / "config.yaml", "09:00"),
        executable=tmp_path / "bin" / "tokenmaxxing",
        db_path=tmp_path / "usage.sqlite3",
        platform="darwin",
        environ={"HOME": str(tmp_path)},
    )
    plist = plistlib.loads(status.job_path.read_bytes())
    assert plist["ProgramArguments"][-4:] == ["profile", "publish", "--sync", "--non-interactive"]
    assert all(Path(value).is_absolute() for value in plist["ProgramArguments"] if "/" in value)


def test_disable_refuses_a_foreign_job(tmp_path) -> None:
    paths = profile_paths(tmp_path / "config.yaml")
    job = launch_agent_path(paths, tmp_path)
    job.parent.mkdir(parents=True)
    job.write_text("foreign")
    with pytest.raises(ValueError, match="not owned"):
        disable_schedule(paths, platform="darwin", environ={"HOME": str(tmp_path)})
```

Cover deterministic project-specific job names, idempotent enable/disable,
systemd unit and timer contents, missing user systemd, cron recipe output,
log paths, and spaces in project paths. Add Windows Task Scheduler XML tests for
an absolute executable, structured Windows-quoted arguments, daily local start
time, `InteractiveToken`, ownership marker, idempotent query/create/delete, and
refusal to delete a foreign task.

- [ ] **Step 2: Run schedule tests and verify failure**

Run: `uv run pytest tests/profile/test_schedule.py -q`

Expected: collection fails because `tokenmaxxing.profile.schedule` does not exist.

- [ ] **Step 3: Implement deterministic schedule documents**

```python
@dataclass(frozen=True, slots=True)
class ScheduleStatus:
    enabled: bool
    backend: str
    job_path: Path | None
    command: tuple[str, ...]
    next_step: str | None = None
```

Derive the identifier as `tokenmaxxing.profile.<first-12-sha256-of-root>`. The
scheduled argv is:

```python
(
    str(executable.resolve()),
    "--db",
    str(db_path.resolve()),
    "profile",
    "--config",
    str(paths.config),
    "publish",
    "--sync",
    "--non-interactive",
)
```

Pass the selected database path into `enable_schedule`; do not infer it from a
profile project. LaunchAgent files live in `~/Library/LaunchAgents`; systemd
files live in `${XDG_CONFIG_HOME:-~/.config}/systemd/user`. Include an ownership
marker containing the resolved project root. Write via temporary file and
atomic rename. Call `launchctl bootstrap/bootout` or `systemctl --user` with
`daemon-reload`, `enable --now`, or `disable --now` arguments and `shell=False`.
systemd uses the user journal instead of custom append-only files.

On Windows, generate a Task Scheduler XML file with deterministic task name
`\Tokenmaxxing\<root-hash>`, the project ownership fingerprint in its
description, a daily local `StartBoundary`, `InteractiveToken`, the absolute
Tokenmaxxing executable in `Exec/Command`, and the remaining argv encoded with
`subprocess.list2cmdline` in `Exec/Arguments`. Register it with
`schtasks /create /tn <name> /xml <temporary-xml> /f`; query and delete with
separate argv calls. Never concatenate an argv list into `/TR`. Windows status
points to Task Scheduler History rather than promising redirected log files.

- [ ] **Step 4: Gate enablement on a valid site and deploy approval**

`enable_schedule` calls `validate_site(paths.site, noindex=not
config.site.indexable)` and requires
`is_approved(make_deploy_plan(config, paths), paths.deploy_approval)`.
Return the cron line as `next_step` on Linux without user systemd; do not edit a
crontab.

- [ ] **Step 5: Run schedule tests**

Run: `uv run pytest tests/profile/test_schedule.py -q`

Expected: all tests pass without changing the real user's LaunchAgents,
systemd units, crontab, or Windows tasks.

- [ ] **Step 6: Commit scheduling**

```bash
git add src/tokenmaxxing/profile/schedule.py tests/profile/test_schedule.py
git commit -m "feat: schedule profile publishing"
```

### Task 8: Profile CLI, preview, publishing, and status

**Files:**
- Create: `src/tokenmaxxing/profile/cli.py`
- Modify: `src/tokenmaxxing/cli.py`
- Create: `tests/profile/test_cli.py`
- Modify: `tests/test_cli.py`

**Interfaces:**
- Consumes: every public profile function from Tasks 2-7 and existing `sync_sources`.
- Produces: `add_profile_parser(commands: argparse._SubParsersAction) -> None` and `run_profile(arguments: argparse.Namespace) -> int`.

- [ ] **Step 1: Write failing parser and happy-path CLI tests**

Define test factories in `test_cli.py` so command-order assertions do not touch
real sources, browsers, deploy tools, or schedulers:

```python
def initialized_profile(tmp_path: Path) -> Path:
    config = tmp_path / "profile" / "config.yaml"
    config.parent.mkdir()
    config.write_text(MINIMAL_CONFIG, encoding="utf-8")
    (config.parent / "avatar.webp").write_bytes(b"avatar")
    return config


def build_result(tmp_path: Path) -> BuildResult:
    site = tmp_path / ".tokenmaxxing" / "site"
    site.mkdir(parents=True, exist_ok=True)
    return BuildResult(site, datetime(2026, 8, 30, tzinfo=UTC), 4, 1024)


def deploy_result() -> DeployResult:
    return DeployResult(returncode=0, stdout="published\n", stderr="")


def publish_arguments(tmp_path: Path, *, sync: bool) -> argparse.Namespace:
    config = initialized_profile(tmp_path)
    return argparse.Namespace(
        command="profile",
        profile_command="publish",
        config=config,
        db=tmp_path / "usage.sqlite3",
        sync=sync,
        non_interactive=False,
        json=False,
        debug=False,
    )
```

```python
@pytest.mark.parametrize("command", ["init", "edit", "preview", "build", "publish", "status", "schedule"])
def test_profile_command_is_registered(command: str) -> None:
    parser = build_parser()
    arguments = parser.parse_args(["profile", command])
    assert arguments.command == "profile"
    assert arguments.profile_command == command


def test_profile_build_json_is_stable(tmp_path, capsys) -> None:
    project = initialized_profile(tmp_path)
    assert main(["--db", str(tmp_path / "usage.sqlite3"), "profile", "--config", str(project), "build", "--json"]) == 0
    payload = json.loads(capsys.readouterr().out)
    assert set(payload) == {"file_count", "generated_at", "site_dir", "total_bytes"}


def test_publish_syncs_before_building_and_deploying(tmp_path, monkeypatch) -> None:
    order = []
    monkeypatch.setattr(profile_cli, "sync_sources", lambda *args, **kwargs: order.append("sync") or ())
    monkeypatch.setattr(profile_cli, "build_profile", lambda *args, **kwargs: order.append("build") or build_result(tmp_path))
    monkeypatch.setattr(profile_cli, "run_deploy", lambda *args, **kwargs: order.append("deploy") or deploy_result())
    assert run_profile(publish_arguments(tmp_path, sync=True)) == 0
    assert order == ["sync", "build", "deploy"]
```

Add tests for config discovery, `edit --publish`, preview forced noindex,
OS-assigned port selection, `--no-open`, a non-fatal `webbrowser.open(False)`,
status with stale approval, interactive
publish rejection, non-interactive refusal, schedule actions, `--debug`, and
human-readable errors that include useful messages without exposing paths from
source histories.

- [ ] **Step 2: Run profile CLI tests and verify failure**

Run: `uv run pytest tests/profile/test_cli.py -q`

Expected: profile is not a registered command.

- [ ] **Step 3: Register the exact compact command surface**

```python
profile = commands.add_parser("profile")
profile.add_argument("--config", type=Path)
profile_commands = profile.add_subparsers(dest="profile_command", required=True)

init = profile_commands.add_parser("init")
init.add_argument("directory", nargs="?", type=Path, default=Path("tokenmaxxing-profile"))
init.add_argument("--no-setup", action="store_true")
init.add_argument("--editable-template", action="store_true")
init.add_argument("--force", action="store_true")

edit = profile_commands.add_parser("edit")
edit.add_argument("--publish", action="store_true")

preview = profile_commands.add_parser("preview")
preview.add_argument("--host", default="127.0.0.1")
preview.add_argument("--port", type=int)
preview.add_argument("--no-open", action="store_true")

build = profile_commands.add_parser("build")
build.add_argument("--output", type=Path)
build.add_argument("--json", action="store_true")

publish = profile_commands.add_parser("publish")
publish.add_argument("--sync", action="store_true")
publish.add_argument("--non-interactive", action="store_true")
publish.add_argument("--json", action="store_true")

status = profile_commands.add_parser("status")
status.add_argument("--json", action="store_true")

schedule = profile_commands.add_parser("schedule")
schedule.add_argument("action", nargs="?", choices=("enable", "disable", "status"))
```

Add a root `--version` using `importlib.metadata.version("tokenmaxxing")`.

- [ ] **Step 4: Implement concise handlers and local preview**

Use `ThreadingHTTPServer` with
`SimpleHTTPRequestHandler(directory=str(site_dir))`, bind
only the configured host and port `0` when no port is supplied, then read
`server.server_port`; do not probe a free port and bind it later. Start the
server, open `webbrowser.open(url)` unless disabled, print the URL even when the
browser call returns `False`, and stop cleanly on `KeyboardInterrupt`. Build
preview into a `TemporaryDirectory` with `noindex=True`.

Interactive `init` uses Rich `Prompt.ask` for name, role, optional avatar,
ordered links, canonical URL, timezone, and deployment mode. The deployment
choices are `cloudflare`, `custom`, and `none`. `cloudflare` writes this command
and a small `wrangler.jsonc` on macOS/Linux; `custom` accepts one argument per prompt until a
blank answer; `none` leaves the argv empty:

```yaml
deploy:
  command:
    - npx
    - wrangler
    - deploy
    - --assets
    - "{site_dir}"
```

On Windows, the preset writes a native `node.exe` plus Wrangler JavaScript path
only when both resolve; it never writes `npx.cmd`. Otherwise it selects `none`
with a concise manual-native-command hint.

Validate the generated config by loading it before reporting success. The
wizard records schedule time but tells the user to run `profile schedule
enable` after the first approved publish; it does not mutate OS scheduling
during initialization. `--no-setup` skips every prompt and leaves a documented
starter with `indexable: false` and an empty deploy command.

`publish` runs sync only with `--sync`, then build, plan, approval, and deploy.
The confirmation prompt prints exact argv, working directory, and canonical
URL. Human output is short; JSON writes only deterministic command results to
stdout and sends progress/errors to stderr. `status` reports config, site,
deploy approval, URL, and scheduler state without mutation.

- [ ] **Step 5: Run all CLI tests**

Run: `uv run pytest tests/test_cli.py tests/profile/test_cli.py -q`

Expected: all tests pass and existing sync/stats/export behavior is unchanged.

- [ ] **Step 6: Commit CLI integration**

```bash
git add src/tokenmaxxing/cli.py src/tokenmaxxing/profile/cli.py tests/test_cli.py tests/profile/test_cli.py
git commit -m "feat: add profile CLI workflow"
```

### Task 9: Human and agent documentation, package checks, and end-to-end verification

**Files:**
- Modify: `README.md`
- Modify: `AGENTS.md`
- Verify: `CLAUDE.md` remains a symlink to `AGENTS.md`
- Modify: `.gitignore` only if generated profile state is not already covered
- Create: `.github/workflows/tests.yml`
- Create: `tests/profile/test_package.py`

**Interfaces:**
- Consumes: the complete public CLI and profile project contract.
- Produces: task-oriented human onboarding and implementation-oriented agent guidance.

- [ ] **Step 1: Add failing package-content tests**

```python
def build_wheel(tmp_path: Path) -> Path:
    subprocess.run(
        ["uv", "build", "--wheel", "--out-dir", str(tmp_path)],
        check=True,
        shell=False,
        capture_output=True,
        text=True,
    )
    return next(tmp_path.glob("*.whl"))


def test_built_wheel_contains_profile_site_sources(tmp_path: Path) -> None:
    wheel = build_wheel(tmp_path)
    with ZipFile(wheel) as archive:
        names = set(archive.namelist())
    assert "tokenmaxxing/profile/templates/index.html.j2" in names
    assert "tokenmaxxing/profile/assets/profile.css" in names
    assert "tokenmaxxing/profile/assets/profile.js" in names
    assert "tokenmaxxing/data/rate-card.json" in names
```

Also install the wheel into an isolated uv environment and assert
`tokenmaxxing profile init --no-setup`, build, and preview-assets validation work
without access to the source checkout.

- [ ] **Step 2: Run package test and verify any missing resource configuration**

Run: `uv run pytest tests/profile/test_package.py -q`

Expected before packaging fixes: the wheel test identifies any missing
templates, assets, fonts, or icons.

- [ ] **Step 3: Rewrite README profile onboarding for humans**

Place the shortest successful flow immediately after installation:

```bash
tokenmaxxing sync
tokenmaxxing stats
tokenmaxxing profile init ~/my-token-profile
cd ~/my-token-profile
tokenmaxxing profile preview
tokenmaxxing profile publish
tokenmaxxing profile schedule enable
```

Explain the separation between the installed CLI and editable profile project,
show the complete compact command tree, link `docs/profile-publishing.md`, show
the safe argv YAML form, explain provider login stays external, and state that
generated output and the local database must not be committed. Keep accounting,
cost, and privacy sections concise and retain every existing accuracy caveat.
Document macOS, Linux, and Windows data locations, OpenCode's Windows
`%USERPROFILE%\.local\share` path, PowerShell cleanup, Task Scheduler behavior,
and the Windows native-executable deploy limitation.

- [ ] **Step 4: Update AGENTS.md for coding agents**

Add the `src/tokenmaxxing/profile/` module map and these invariants:

```text
- Profile aggregation must reuse reporting and pricing arithmetic.
- Internal agent keys may group rows but must never cross the public payload boundary.
- Render user content only through Jinja autoescaping.
- Deploy with argv and shell=False; command changes invalidate local approval.
- Build into a sibling temporary directory, validate, then replace with rollback.
- Scheduler changes may touch only the deterministic job owned by the current profile.
- Packaged templates and assets are source files; generated .tokenmaxxing/site is not.
- Windows batch launchers are not equivalent to shell-free native argv execution.
```

Add focused profile tests to the verification section. Keep `CLAUDE.md` as the
existing symlink so both tools read one authoritative file.

- [ ] **Step 4a: Add cross-platform CI**

Create one workflow that runs Python 3.12 on `ubuntu-latest`, `macos-latest`,
and `windows-latest`:

```yaml
strategy:
  fail-fast: false
  matrix:
    os: [ubuntu-latest, macos-latest, windows-latest]
steps:
  - uses: actions/checkout@v4
  - uses: astral-sh/setup-uv@v6
  - run: uv sync --locked
  - run: uv run pytest
  - run: uv run ruff check .
  - run: uv build
```

Run Windows-specific tests in the normal suite. Skip only symlink/junction
creation cases when Windows returns the documented privilege error; do not skip
path, timezone, SQLite/WAL, replacement, preview, deploy, or scheduler tests.

- [ ] **Step 5: Run the complete automated verification**

```bash
uv run pytest
uv run ruff check .
git diff --check
uv build
uv run pytest tests/profile/test_package.py -q
```

Expected: every command exits zero. Inspect the wheel and sdist contents and
verify no database, salt, WAL, SHM, generated site, local profile YAML, or
`tokenmaxxing-export.json` is packaged.

- [ ] **Step 6: Run a real local end-to-end build against the ignored database**

```bash
uv run tokenmaxxing --db data/tokenmaxxing.sqlite3 profile --config /Users/anjay/Documents/tokenmaxxing-profile-v0/config.yaml build
uv run tokenmaxxing --db data/tokenmaxxing.sqlite3 profile --config /Users/anjay/Documents/tokenmaxxing-profile-v0/config.yaml status
```

If the saved project predates YAML, initialize its YAML and assets without
overwriting the saved `index.html` until the generated page has been visually
compared. Confirm the real 28-day headline, API equivalent, agent total, peak
day, streak, models, activity tooltips, and quip against direct reporting
queries. Do not deploy or enable a real schedule during this check.

- [ ] **Step 7: Validate browser quality and responsive edge cases**

Serve the real build locally and check 320, 768, 1280, and 1600 CSS-pixel widths
in light and dark themes. Test empty, light, medium, heavy, and extreme fixture
pages for number formatting, fixed activity columns, quiet trailing days,
tooltips, model overflow, keyboard focus, reduced motion, and no horizontal
scroll. Run Lighthouse production-mode checks and record actual scores and
compressed sizes in the completion report.

- [ ] **Step 8: Commit docs and verification**

```bash
git add README.md AGENTS.md pyproject.toml tests/profile/test_package.py tests/profile/test_end_to_end.py .github/workflows/tests.yml
git commit -m "docs: add profile publishing guide"
```

Do not add `tokenmaxxing-export.json`, the local database, saved profile build,
or any generated `.tokenmaxxing` directory.
