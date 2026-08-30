# Setup

Tokenmaxxing requires Python 3.12+ and [uv](https://docs.astral.sh/uv/). From a
checkout of the repository, install the command-line tool with:

```bash
uv tool install .
```

After pulling changes, update the installed tool with:

```bash
uv tool install --force .
```

## First sync

Tokenmaxxing finds Codex, Claude Code, Pi, and OpenCode history in their usual
locations. Start with:

```bash
tokenmaxxing sync
tokenmaxxing stats
```

The first sync reads all available history and can take a while. Later syncs
process only new or changed records. Repeating a sync is safe and does not count
the same event again.

Stats use the computer's local timezone and default to the last 28 days. A
different window, grouping, or timezone can be selected when needed:

```bash
tokenmaxxing stats --period all
tokenmaxxing stats --group-by harness
tokenmaxxing stats --timezone Asia/Kolkata
```

Use `--json` with `sync`, `stats`, or `profile` status commands when calling
Tokenmaxxing from a script.

## Local data

Tokenmaxxing stores its private SQLite ledger and workspace-hashing salt in the
platform data directory:

| Platform | Tokenmaxxing database | OpenCode source database |
| --- | --- | --- |
| macOS | `~/Library/Application Support/tokenmaxxing/tokenmaxxing.sqlite3` | `~/.local/share/opencode/opencode.db` |
| Linux | `${XDG_DATA_HOME:-~/.local/share}/tokenmaxxing/tokenmaxxing.sqlite3` | `${XDG_DATA_HOME:-~/.local/share}/opencode/opencode.db` |
| Windows | `%LOCALAPPDATA%\tokenmaxxing\tokenmaxxing.sqlite3` | `%USERPROFILE%\.local\share\opencode\opencode.db` |

The other default source locations are `~/.codex`, `~/.claude/projects`, and
`~/.pi/agent/sessions`. Missing sources are simply skipped.

Set `TOKENMAXXING_HOME` to move Tokenmaxxing's complete data directory, or use
the global `--db PATH` option for a specific ledger:

```bash
TOKENMAXXING_HOME=/path/to/private-data tokenmaxxing sync
tokenmaxxing --db /path/to/usage.sqlite3 stats
```

Source locations can be selected explicitly:

```bash
tokenmaxxing sync \
  --codex-root /path/to/.codex \
  --claude-root /path/to/claude/projects \
  --pi-root /path/to/pi/sessions \
  --opencode-db /path/to/opencode.db
```

Use `--source codex`, `claude`, `pi`, or `opencode` to import only one source:

```bash
tokenmaxxing sync --source codex
```

## Public profile

Create a standalone profile project with:

```bash
tokenmaxxing profile init ~/my-token-profile
```

The setup asks for public profile details and an optional deployment command.
It can then run the first sync, open a local preview, publish, and enable daily
updates. The editable settings live in `config.yaml`; the complete static site
is built under `dist/`.

Tokenmaxxing remembers the last initialized profile, so profile commands can be
run from another directory. Use `--config` to select one explicitly when more
than one profile exists:

```bash
tokenmaxxing profile --config ~/my-token-profile/config.yaml status
tokenmaxxing profile preview
tokenmaxxing profile publish --sync
```

Setting a time in `config.yaml` does not enable the operating-system schedule.
Check or change it explicitly:

```bash
tokenmaxxing profile schedule status
tokenmaxxing profile schedule enable
tokenmaxxing profile schedule disable
```

Scheduling uses launchd on macOS, user systemd on Linux, and Task Scheduler on
Windows. The scheduled job syncs, rebuilds, and runs the configured deployment
command.

## Backups

The ledger runs in SQLite WAL mode. For a consistent backup, either stop active
syncs and copy the complete Tokenmaxxing data directory, or use SQLite's backup
mechanism. Keep the database, any live `-wal` and `-shm` companions, and the
`salt` private; none should be committed or published.

The salt preserves stable workspace fingerprints across future syncs, so it
should travel with a restored database.
