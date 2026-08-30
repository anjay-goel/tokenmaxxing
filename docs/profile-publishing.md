# Profile publishing

Tokenmaxxing profiles turn local aggregate usage into a small static site. The
CLI owns data collection, rendering, validation, deployment, and scheduling;
the profile project owns the user's public information and presentation
choices.

This document is the implementation contract for the first public version.

## Goals

- One short onboarding flow from installed CLI to a local profile preview.
- A portable static directory that works with any static host.
- Optional daily publishing without daily Git commits.
- Human-editable configuration, assets, and CSS.
- A fast, accessible page whose useful content renders without JavaScript.
- A strict publication boundary: only profile fields and aggregate statistics
  enter the generated site.

Provider APIs, account creation, domain configuration, and secret management
are outside Tokenmaxxing. The deploy runner invokes the configured local command;
it does not reimplement Cloudflare, Netlify, Vercel, or another host.

## Install and first run

Users install the CLI once from the checkout, then create a separate profile
project wherever they want.

```bash
uv tool install .
tokenmaxxing sync
tokenmaxxing profile init ~/my-token-profile
cd ~/my-token-profile
tokenmaxxing profile preview
```

`profile init` asks for a name, bio, optional avatar, optional
LinkedIn/GitHub/website URLs, and canonical URL. The avatar may come from
anywhere on the computer; Tokenmaxxing copies it into the profile project. The
computer's timezone is detected automatically, with UTC as a fallback. Invalid
paths, links, and canonical URLs are explained and prompted again.

It then runs the first sync, builds `dist/`, and serves the page locally while
the user inspects it. Only after preview does it ask for an optional deployment
command. A configured command leads to the first publish, then an optional
daily schedule and publish time. Scheduling is never enabled before a
successful publish. A cancelled first run can be rerun without `--force`.

The first sync can take a while because it scans existing historical records.
Subsequent syncs are incremental and are usually much faster.

## Command surface

The existing `sync`, `stats`, and `export` commands remain unchanged. Profile
features live under one group:

```text
tokenmaxxing
|-- sync
|-- stats
|-- export
`-- profile
    |-- init [DIRECTORY]
    |-- edit
    |-- preview
    |-- build
    |-- publish
    |-- status
    `-- schedule [enable|disable|status]
```

Place `--config PATH` after `profile` and before its command. Without it,
Tokenmaxxing searches the current directory and its parents for
`config.yaml`, then uses the last successfully initialized profile.
`profile init` does not accept `--config`; its directory argument selects and
remembers the new project.

### `profile init`

Creates a project in `DIRECTORY`, or prompts for one with
`./tokenmaxxing-profile` as the default, then runs onboarding.

- `--no-setup` writes a documented starter configuration without prompting.
- `--editable-template` copies the packaged HTML templates into the project.
- `--force` permits initialization in a non-empty directory but only adds
  missing project files. It never overwrites an existing editable file.

### `profile edit`

Opens `config.yaml` with `$VISUAL`, then `$EDITOR`, then the platform
default editor. It validates the file after the editor closes.

- `--publish` publishes after successful validation.

### `profile preview`

Builds into a temporary directory, forces `noindex`, starts a local HTTP server,
and opens the browser.

- `--host HOST` defaults to `127.0.0.1`.
- `--port PORT` defaults to an available local port.
- `--no-open` leaves the browser closed.

### `profile build`

Builds and validates the portable static site without deploying it.

- `--output DIRECTORY` defaults to `dist` inside the profile project.
- `--json` emits a stable machine-readable result.

### `profile publish`

Builds, validates, then runs the configured deploy command.

- `--sync` imports local histories before building.
- `--non-interactive` skips the publish confirmation for scheduled updates.
- `--json` emits a stable machine-readable result.

### `profile status`

Validates configuration and reports the built-site path, configured public URL,
and schedule state.

- `--json` emits the same core status plus structured scheduler job and command
  details for automation.

### `profile schedule`

With no action, shows the current schedule. `enable`, `disable`, and `status`
are deterministic for scripts.

The scheduled operation is equivalent to:

```bash
tokenmaxxing profile publish --sync --non-interactive
```

## Profile project

The complete static package is built into one ignored directory:

```text
my-token-profile/
|-- config.yaml
|-- assets/                    # optional, user-created
|   `-- avatar.webp            # optional
|-- custom.css
|-- template/                  # only with --editable-template
|-- .gitignore
`-- dist/                      # complete deployable static site
    |-- index.html
    |-- profile.json
    `-- assets/
```

The profile project may be committed independently of Tokenmaxxing. Generated
statistics and build output are ignored by default, so a daily local publish
does not create GitHub activity.

## Configuration

`config.yaml` is the source of truth. It contains no credentials and no
private database identifiers.

```yaml
version: 1

profile:
  name: Anjay Goel
  bio: Software Engineer at Dashtoon
  avatar: assets/avatar.webp
  links:
    - label: LinkedIn
      value: anjay-goel
      url: https://www.linkedin.com/in/anjay-goel
    - label: GitHub
      value: anjay-goel
      url: https://github.com/anjay-goel
    - label: Website
      value: anjay.sh
      url: https://anjay.sh

site:
  title: Anjay Goel | Token Trail
  description: A visual snapshot of AI agent usage.
  canonical_url: https://anjay.sh/tokenmaxxing/
  indexable: true
  timezone: Asia/Kolkata
  theme: auto
  accent: violet

metrics:
  window_days: 28
  show_api_equivalent: true
  show_agents: true
  show_peak_usage: true
  show_longest_streak: true
  show_models: true

deploy:
  command:
    - npx
    - wrangler
    - deploy
    - --assets
    - "{site_dir}"

schedule:
  time: "09:00"
```

Rules are deliberately narrow:

- `version` must be `1`.
- Unknown keys are errors so typos do not silently change a public page.
- `window_days` defaults to `28`; all headline cards use that same local-day
  window.
- Links retain YAML order and accept only `https` or `mailto` URLs.
- File paths are resolved inside the profile project. Escaping the project is
  rejected.
- `theme` is `auto`, `light`, or `dark`.
- `canonical_url` must be an absolute HTTPS URL without credentials, a query,
  or a fragment. A missing trailing slash is added automatically so relative
  assets and sitemap URLs remain under a configured subpath.
- Interactive setup makes the configured public profile indexable.
- `profile init --no-setup` leaves the starter at `indexable: false`; preview
  always remains noindex.
- `deploy.command` is a YAML list of arguments, never a shell string.
- `{site_dir}` expands to the generated `dist/` path when present, but deploy
  commands may rely entirely on provider configuration and omit it.
- Environment variables and credentials remain in the provider's own login,
  keychain, or process environment.

PyYAML handles safe parsing. Initialization writes a readable deterministic
file; later edits do not rewrite it, so user comments and formatting survive.

## Published data

Every displayed metric is derived from counted usage events, preserving the
same accounting rules as `stats` and `export`.

- The headline, API equivalent, agent count, peak day, streak, and model count
  cover today plus the previous 27 local calendar days.
- Tokens use the reporting precedence already documented in
  [Architecture](architecture.md): reported total, derived total, then
  non-overlapping components.
- API equivalent uses the bundled rate card and appears only at the existing
  coverage threshold. If it cannot be shown honestly, model count remains as
  the fourth card.
- An agent is a source-scoped root or child execution with counted usage. A
  session with direct usage is one root agent; every distinct child run is one
  additional agent. Marker rows and aggregate rows that do not represent a
  model call cannot create agents.
- An agent's primary model is the model responsible for the most counted tokens
  in that agent, with deterministic alphabetical tie-breaking.
- Peak usage is the largest local-day token total in the selected window.
- Longest streak is the longest consecutive sequence of local days containing
  at least one agent in the selected window.

The activity grid and recent-usage chart use aggregate day and model totals.
Tooltips format values compactly and may show model composition, grouping
models below five percent into `other models`. No prompt, response, reasoning,
tool content, path, source artifact, session identifier, run identifier, or
event identifier is written to the site. `profile.json` contains the same
public aggregates as the HTML and nothing more.

## Rendering

The packaged profile is the default visual source. It is split into readable
Jinja templates, CSS, small JavaScript modules, and local assets rather than
embedded as one generated HTML file. Jinja autoescaping is mandatory for every
user value. `custom.css` loads last; a project template copied by
`--editable-template` overrides the corresponding packaged template.

The generated site contains:

```text
site/
|-- index.html
|-- profile.json
|-- assets/
|   |-- profile.css
|   |-- profile.js
|   |-- avatar.webp           # only when configured
|   `-- fonts and model icons
|-- robots.txt
`-- sitemap.xml              # only for an indexable canonical URL
```

Visible content is present in semantic HTML. JavaScript is limited to theme,
tooltips, and small progressive interactions. The page performs no analytics,
third-party requests, or runtime data fetches.

Indexable builds include a descriptive title and description, self-canonical
URL, Open Graph and Twitter metadata, `ProfilePage` and `Person` JSON-LD,
`robots.txt`, and `sitemap.xml`. Preview and non-indexable builds include
`noindex, nofollow` and omit the sitemap.

Initial budgets and manual release targets:

- no frontend framework or runtime dependency;
- no render-blocking network request;
- less than 15 KB compressed JavaScript;
- about 60 KB compressed initial HTML and CSS, excluding fonts and images;
- fixed media and chart dimensions with no expected layout shift;
- keyboard-operable tooltips, visible focus, sufficient contrast, and reduced
  motion support;
- a manual production Lighthouse run targeting performance, accessibility,
  best-practices, and SEO scores of 95 or higher;
- good Core Web Vitals targets: LCP at most 2.5 seconds, INP at most 200 ms, and
  CLS at most 0.1.

## Build and deploy safety

Builds render into a sibling temporary directory on the same volume.
Tokenmaxxing validates HTML, required assets, JSON shape, internal paths,
metadata, and privacy allowlists before a rollback-safe replacement of
`dist`. The previous directory is retained as a temporary backup
until the validated site is in place; a failed build leaves it untouched and a
failed replacement restores it. Deployment starts only after a successful
replacement. This is not described as an atomic directory swap because Windows
cannot replace a non-empty directory in one operation.

The deploy runner executes an argument list directly with no shell. Pipes,
redirection, command substitution, glob expansion, and chained commands are
therefore unavailable. It resolves `{site_dir}` to the validated build path.

Interactive publishing displays the exact command, working directory, and
public URL before running it. `--non-interactive` skips that prompt and exists
for the scheduled command generated by Tokenmaxxing.

Onboarding asks for one optional shell-style deployment command and parses it
into the argument list stored in YAML. Tokenmaxxing never creates provider
configuration, stores provider tokens, or calls provider deployment APIs
itself.

## Scheduling

Scheduling uses the operating system, not a resident Tokenmaxxing daemon:

- macOS installs a user LaunchAgent;
- Linux installs a user systemd timer when available;
- Linux without user systemd receives an explicit cron command and setup
  instructions rather than a silently different scheduler;
- Windows installs a current-user Task Scheduler XML definition through
  `schtasks` when that command is available.

Generated jobs use absolute CLI and project paths and run non-interactively.
Linux uses the user journal. Windows status points to Task Scheduler History
because Task Scheduler does not provide safe argv-based file redirection.
Enabling a schedule requires a successful build and a valid configured deploy
command.
Disabling removes only a job whose ownership marker matches that profile
project. Provider credentials remain the provider CLI's responsibility.

The core CLI, profile configuration, rendering, preview, and native-executable
deployment paths support macOS, Linux, and Windows. Windows uses
`%LOCALAPPDATA%\tokenmaxxing` for Tokenmaxxing state unless
`TOKENMAXXING_HOME` is set, while OpenCode retains its own
`%USERPROFILE%\.local\share\opencode` default. The package includes IANA
timezone data so local-day windows remain correct on systems without a system
zoneinfo database.

Windows batch launchers (`.cmd` and `.bat`) are not accepted as deploy
executables because Windows may route them through shell parsing even when
Python requests `shell=False`. A Windows user may configure a native executable
such as `node.exe` with a JavaScript CLI path as separate argv entries.

## Code boundaries

Profile code lives under `src/tokenmaxxing/profile/`:

- `config.py`: strict YAML models, discovery, parsing, and validation.
- `project.py`: initialization, editor launch, and project paths.
- `data.py`: aggregate profile data and privacy-safe serialization.
- `render.py`: Jinja environment and static asset rendering.
- `build.py`: temporary build, validation, and rollback-safe replacement.
- `deploy.py`: deploy argv planning and execution.
- `schedule.py`: LaunchAgent, systemd, and Task Scheduler ownership.
- `cli.py`: profile subparsers and concise terminal presentation.
- `templates/` and `assets/`: the readable default site source.

These modules call existing synchronization, repository, reporting, and pricing
APIs. They do not duplicate token arithmetic or source-specific accounting.
The root CLI only registers the profile command group.

## Acceptance checks

Implementation is complete when:

1. A fresh project can be initialized, edited, previewed, built, and published
   using a fake deploy executable in tests.
2. All headline metrics agree exactly with reporting for empty, tiny, medium,
   heavy, and very heavy 28-day fixtures.
3. Agent identity and primary-model tests cover root usage, child runs,
   subagents, missing models, ties, markers, and aggregate records without
   double counting.
4. Missing timestamps, quiet days, daylight-saving transitions, very large
   values, unknown models, and insufficient price coverage render cleanly.
5. Malicious YAML strings, links, paths, template values, and deploy arguments
   cannot inject HTML or invoke a shell.
6. Failed rendering, validation, synchronization, and replacement preserve the
   previous successful site. Failed deployment keeps the new validated local
   build available for inspection or retry. All failures are actionable.
7. Scheduled publishing is idempotent on macOS, Linux, and Windows and owns
   only its generated job.
8. Automated tests pass schema, link, privacy, and size checks. The release
   checklist records a manual keyboard/screen-reader review and production
   Lighthouse run; these browser checks do not run in CI.
9. The full existing importer and reporting suite remains green.
10. Native Windows tests cover data paths, timezone/DST windows, OpenCode paths
    with spaces and an active WAL, rollback-safe site replacement, editor and
    preview behavior, deploy launcher rejection, and Task Scheduler ownership.

README documentation should stay task-oriented for people: installation,
first sync, profile onboarding, publishing, and common commands. AGENTS.md
should stay implementation-oriented for coding agents: module ownership,
privacy and accounting invariants, deploy safety, generated-file boundaries,
and required focused plus full verification.
