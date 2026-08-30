# Tokenmaxxing

Tokenmaxxing counts historical token usage from local Codex, Claude Code, Pi,
and OpenCode histories. It stores only the metadata needed for incremental
accounting and reports aggregate personal statistics.

## Install

Python 3.12+ and [uv](https://docs.astral.sh/uv/) are required.

From a source checkout:

```bash
uv tool install .
```

After publication to PyPI:

```bash
uv tool install tokenmaxxing-history
```

## Use

```bash
tokenmaxxing sync
tokenmaxxing stats
tokenmaxxing stats --period 7d
tokenmaxxing stats --period all --group-by harness
tokenmaxxing stats --group-by day --json
tokenmaxxing export
tokenmaxxing export usage.json
```

`sync` imports the four supported local histories, prints one concise status
per harness, and suggests the next stats command. `stats` defaults to a
28-day model leaderboard with compact token totals and an approximate public
API-equivalent value. Use `--period 7d`,
`--period 28d`, or `--period all` to choose a local-day window, and
`--group-by harness` or `--group-by day` for the secondary views. Human output
does not show percentages; `--json` keeps raw model names and adds the selected
period and view as deterministic metadata. `export` writes aggregate JSON only.
With no path it creates `./tokenmaxxing-export.json`; pass a `.json` file or an
existing directory to choose another destination.

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

## Architecture

Each harness has a small source-specific importer. Parsers produce privacy-safe
observations, reconciliation collapses copies and progressive snapshots into
semantic usage events, and shared reporting reads only counted events. See
[Architecture](docs/architecture.md) for identities and source-specific rules.

## Accounting semantics

Repeated imports are idempotent. Canonical and safely provisional events are
counted; stale, superseded, contradictory, and non-usage records are retained
but excluded from totals. Reporting prefers a source-reported total, then a
derived total, then non-overlapping token components. Reasoning and cached input
may overlap other source components and are not blindly added again.

## Costs and privacy

Pi reports source-provided cost and OpenCode cost is an upstream estimate.
Those values take precedence, including known zero-cost usage. Other events use
the dated public prices in
[`src/tokenmaxxing/data/rate-card.json`](src/tokenmaxxing/data/rate-card.json)
when every non-zero token component can be priced. The bundled card covers
popular OpenAI, Anthropic, Gemini, DeepSeek, GLM, Kimi, Grok, Mistral, and Qwen
text models and can be extended without changing the estimator.

Catalog estimates also require the billable components to reconcile with the
canonical event total. Human stats show the estimate only when at least 95% of
the window's tokens are priced; otherwise the value is omitted instead of
showing a misleading partial total. JSON always includes the raw coverage.

This is an approximate token API equivalent, not an actual bill. It excludes
subscriptions and unrecorded charges such as tools, cache storage time, images,
audio, and video. Unknown models or missing component rates are reported as
unpriced rather than zero; JSON stats include exact nanodollars and coverage
counts.

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
uv run ruff check .
git diff --check
```

## Contributing

Read [AGENTS.md](AGENTS.md) for the module map, accounting invariants, privacy
boundary, style, and verification requirements. Keep source-specific behavior
explicit and avoid adding abstraction that makes a small importer harder to
follow.

## License

[MIT](LICENSE)
