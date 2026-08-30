# Tokenmaxxing

Tokenmaxxing imports local Codex, Claude Code, Pi, and OpenCode history into a
private SQLite ledger, then turns it into readable 28-day stats and an optional
publishable profile. Prompts, responses, reasoning, tool content, and raw
workspace paths stay out of both the database and the static site.

## Install

Tokenmaxxing requires Python 3.12+.

```bash
uv tool install .
```

## CLI

```bash
tokenmaxxing sync
tokenmaxxing stats
tokenmaxxing export
tokenmaxxing profile init ~/my-token-profile
```

The first sync scans historical records and can take a while. Later syncs are
incremental and usually much faster.

```text
tokenmaxxing [--db PATH] [--debug] <command>
|-- sync [--source all|codex|claude|pi|opencode] [--json]
|-- stats [--period 7d|28d|all] [--group-by model|harness|day] [--json]
|-- export [PATH] [--timezone ZONE]
`-- profile [--config PATH] <command>
    |-- init [DIRECTORY] [--no-setup] [--editable-template] [--force]
    |-- edit [--publish]
    |-- preview [--host HOST] [--port PORT] [--no-open]
    |-- build [--output DIRECTORY] [--json]
    |-- publish [--sync] [--non-interactive] [--json]
    |-- status [--json]
    `-- schedule [enable|disable|status]
```

`stats` defaults to a 28-day model view with compact token totals and an
approximate public API-equivalent value. `export` writes aggregate JSON to
`./tokenmaxxing-export.json` by default. Run any command with `--help` for its
full options.

## Local SQLite ledger

| Platform | Tokenmaxxing data | OpenCode default |
| --- | --- | --- |
| macOS | `~/Library/Application Support/tokenmaxxing` | `~/.local/share/opencode/opencode.db` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/tokenmaxxing` | `${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db` |
| Windows | `%LOCALAPPDATA%\tokenmaxxing` | `%USERPROFILE%\.local\share\opencode\opencode.db` |

The other source defaults are `~/.codex`, `~/.claude/projects`, and
`~/.pi/agent/sessions`. Set `TOKENMAXXING_HOME` to move Tokenmaxxing state, use
global `--db PATH` for a specific database, or see `tokenmaxxing sync --help`
for source overrides.

Sync is idempotent. Source-scoped identities reconcile copied and progressive
histories without merging unrelated events. Totals prefer a source-reported
total, then a derived total, then non-overlapping components. The SQLite file,
its `-wal` and `-shm` companions, and `salt` are private and should not be
committed. See [Architecture](docs/architecture.md) for the accounting rules.

## Publishable profile

```bash
tokenmaxxing profile init ~/my-token-profile
```

Onboarding asks for the public profile details, syncs and builds `dist/`, then
opens the generated page in the browser. After the preview it asks for an
optional deployment command, offers to publish, and can enable a daily update
at the chosen local time. Leave the deployment command blank for a local-only
profile.

```text
Deployment command (optional; {site_dir} is available when needed): npx wrangler deploy
```

The command is stored as an argument list in editable `config.yaml`.
Use `{site_dir}` only when the deployment tool needs the generated `dist/` path.
Provider login and credentials stay with the provider CLI. Scheduled updates
use launchd on macOS, a user systemd timer on Linux, or Task Scheduler on
Windows; Linux without user systemd gets a cron recipe.

`schedule.time` is the preferred daily time, not an enabled flag. `profile
status` reads the operating-system scheduler and shows how to enable it when
the job is absent.

The complete portable site—HTML, CSS, JavaScript, assets, and aggregate JSON—
lives in the ignored project `dist/` directory. Relative assets work when the
profile is hosted at a domain root or subpath. Interactive onboarding makes the
configured public URL indexable and emits canonical metadata, structured data,
`robots.txt`, and a sitemap. `profile preview` and `profile init --no-setup`
remain noindex until explicitly configured otherwise.

Without `--config`, profile commands search the current directory and its
parents, then use the last successfully initialized project. To select one
explicitly, place the option before the profile subcommand:

```bash
tokenmaxxing profile --config ./config.yaml preview
```

See [Profile publishing](docs/profile-publishing.md) for configuration,
deployment, scheduling, and platform details.

## Privacy and estimates

The static site contains only configured public fields and aggregate stats.
API-equivalent value is an estimate, not a bill: source costs take precedence,
otherwise dated public rates apply only when billable token components fully
reconcile. Unsupported models remain unpriced rather than being treated as
free. Subscriptions, tools, media, and cache storage time are excluded.

## Development

```bash
uv sync --locked
uv run pytest
uv run ruff check .
git diff --check
uv build
```

Read [AGENTS.md](AGENTS.md) before changing accounting, privacy, profile, or
deployment behavior. Tokenmaxxing is licensed under the [MIT License](LICENSE).
