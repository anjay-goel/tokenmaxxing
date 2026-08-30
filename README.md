# Tokenmaxxing

Tokenmaxxing imports local Codex, Claude Code, Pi, and OpenCode history, then
turns it into readable 28-day stats and an optional static profile. Prompts,
responses, reasoning, tool content, and raw workspace paths stay out of its
database and published output.

## Install

Tokenmaxxing requires Python 3.12+. Install the CLI once; profile projects are
ordinary folders that remain yours.

```bash
# From this checkout
uv tool install .

# After publication to PyPI
uv tool install tokenmaxxing-history
```

## First five minutes

```bash
tokenmaxxing sync
tokenmaxxing stats
tokenmaxxing profile init ~/my-token-profile
cd ~/my-token-profile
tokenmaxxing profile preview
tokenmaxxing profile publish
tokenmaxxing profile schedule enable
```

`profile init` creates editable YAML and CSS plus an ignore file for generated
state. Add an avatar or editable templates only when you need them. Generated
statistics and the built site live under the ignored `.tokenmaxxing/`
directory. Keep the profile project in any repository you like, but never
commit `.tokenmaxxing/` or the local Tokenmaxxing database. Enable the daily
schedule only after an interactive publish has shown and approved the exact
deploy command.

See [Profile publishing](docs/profile-publishing.md) for configuration,
hosting, approval, scheduler, and platform details.

## Commands

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

Place `--config` between `profile` and its command:

```bash
tokenmaxxing --db ./usage.sqlite3 profile --config ./tokenmaxxing.yaml build
```

Without `--config`, profile commands search the current directory and its
parents for `tokenmaxxing.yaml`. `profile init` does not accept `--config`;
its directory argument selects the new project. `profile schedule` with no
action is the same as `profile schedule status`.

Canonical profile URLs must be absolute HTTPS URLs without credentials, a
query, or a fragment. Tokenmaxxing adds a trailing slash so subpath assets and
the sitemap resolve beneath the configured profile URL. New profiles remain
`noindex` until `site.indexable` is explicitly changed to `true`.

`sync` is incremental and idempotent. `stats` defaults to a 28-day model view
with compact token totals and an approximate public API-equivalent value.
`export` writes aggregate JSON to `./tokenmaxxing-export.json` by default; a
`.json` path or existing directory chooses another destination.

## Publish without Git activity

Deployment is a local argv list in `tokenmaxxing.yaml`, not a shell command:

```yaml
deploy:
  command:
    - npx
    - wrangler
    - deploy
    - --assets
    - "{site_dir}"
```

Login remains with the provider's CLI. Tokenmaxxing stores no provider token.
It validates and installs the new local build before starting deployment, so a
failed deploy leaves that new validated build available to inspect or retry.
Render, validation, or replacement failure preserves the previous successful
local site. Any deploy-command change requires fresh interactive approval.

Daily updates use the operating system rather than a resident daemon: a user
LaunchAgent on macOS, a user systemd timer on Linux, or a current-user Task
Scheduler job on Windows. Linux without user systemd prints a cron recipe.
Windows deploy commands must start with a native executable; `.cmd` and `.bat`
launchers are rejected. For example, use `node.exe` and the provider CLI's
JavaScript entry point as separate arguments.

## Local data

| Platform | Tokenmaxxing data | OpenCode default |
| --- | --- | --- |
| macOS | `~/Library/Application Support/tokenmaxxing` | `~/.local/share/opencode/opencode.db` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/tokenmaxxing` | `${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db` |
| Windows | `%LOCALAPPDATA%\tokenmaxxing` | `%USERPROFILE%\.local\share\opencode\opencode.db` |

The other imported histories default to `~/.codex`, `~/.claude/projects`, and
`~/.pi/agent/sessions`. Set `TOKENMAXXING_HOME` to move Tokenmaxxing state,
use global `--db PATH` for one database, or pass the source-path options shown
by `tokenmaxxing sync --help`.

ChatGPT desktop history and live rate limits are not supported.

## Accuracy, estimates, and privacy

Event identity is source-scoped; timestamps never merge unrelated events.
Copied and progressive histories reconcile to one semantic event. Only
canonical and safely provisional events count. Totals prefer a source-reported
total, then a derived total, then non-overlapping components, so reasoning and
cached input are not blindly added twice.

Pi source cost and OpenCode's upstream estimate take precedence, including a
known zero. Other events use the dated public
[rate card](src/tokenmaxxing/data/rate-card.json) only when billable components
fully reconcile with the canonical total. Human stats show an estimate only at
95% token coverage. Unsupported models and modifiers remain unpriced, not
zero. This is an approximate token API equivalent, not a bill; subscriptions,
tools, cache storage time, images, audio, and video are excluded.

The SQLite database contains stable identifiers needed for reconciliation even
though it excludes source text. Treat the database, its `-wal` and `-shm`
companions, and `salt` as private. The static profile contains only configured
public fields and aggregate statistics.

See [Architecture](docs/architecture.md) for exact source identities and
accounting rules.

## Remove local data

```bash
# macOS
rm -rf "$HOME/Library/Application Support/tokenmaxxing"

# Linux, unless TOKENMAXXING_HOME is set
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/tokenmaxxing"

# PowerShell, unless TOKENMAXXING_HOME is set
Remove-Item -Recurse -Force "$env:LOCALAPPDATA\tokenmaxxing"
```

If `TOKENMAXXING_HOME` is set, remove that directory instead.

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
