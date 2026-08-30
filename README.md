# Tokenmaxxing

Tokenmaxxing counts historical token usage from local Codex, Claude Code, Pi,
and OpenCode histories. It stores only the metadata needed for incremental
accounting and reports aggregate personal statistics.

## Install

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required.

```bash
uv tool install tokenmaxxing-history
```

To install from a source checkout instead:

```bash
uv tool install .
```

## Use

```bash
tokenmaxxing sync
tokenmaxxing stats
tokenmaxxing export usage.json
```

`sync` imports the four supported local histories. `stats` prints aggregate
usage, and `export` writes aggregate JSON only.

By default, Tokenmaxxing reads:

| Harness | Default location |
| --- | --- |
| Codex | `~/.codex` |
| Claude Code | `~/.claude/projects` |
| Pi | `~/.pi/agent/sessions` |
| OpenCode | `$XDG_DATA_HOME/opencode/opencode.db`, or `~/.local/share/opencode/opencode.db` |

The local database is `~/Library/Application Support/tokenmaxxing/tokenmaxxing.sqlite3`
on macOS. On other POSIX systems it is
`$XDG_DATA_HOME/tokenmaxxing/tokenmaxxing.sqlite3`, or
`~/.local/share/tokenmaxxing/tokenmaxxing.sqlite3`.

Set `TOKENMAXXING_HOME` to move the local data directory, use `--db PATH` to
choose one database, and use `sync --codex-root`, `--claude-root`, `--pi-root`,
or `--opencode-db` to choose source locations. Run `tokenmaxxing --help` for
all options.

ChatGPT desktop history and live rate limits are not supported.

## Costs and privacy

Pi reports source-provided cost. OpenCode cost is an upstream estimate. When
Codex or Claude histories do not report cost, the result is unavailable rather
than zero.

Tokenmaxxing does not store prompts, model text, reasoning text, tool content,
or raw working-directory paths. Its database still contains identifiers used
for deterministic accounting. Never publish the database, `salt`, or SQLite
`-wal` and `-shm` files.

To remove all local Tokenmaxxing data, delete the selected data directory:

```bash
# macOS
rm -rf "$HOME/Library/Application Support/tokenmaxxing"

# Other POSIX systems, unless TOKENMAXXING_HOME is set
rm -rf "${XDG_DATA_HOME:-$HOME/.local/share}/tokenmaxxing"
```

If `TOKENMAXXING_HOME` is set, delete that directory instead.

## Development

```bash
uv sync --locked
uv run pytest
```

## License

[MIT](LICENSE)
