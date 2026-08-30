# Generic Profile Awards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Add six generic, automatically earned awards to Tokenmaxxing profiles, displayed below profile details with accessible responsive interactions and restrained motion.

**Architecture:** A focused `profile.awards` module derives durable all-time awards from the same privacy-safe `ProfileUsageRow` snapshot used by profile aggregation. The profile data carries only earned award projections. The existing approved static profile renders those projections as keyboard-operable enamel medallions in the profile rail.

**Tech Stack:** Python 3.13 dataclasses and pytest; semantic HTML, CSS, and small framework-free JavaScript.

## Global Constraints

- Awards are generic for every user; no user names, model names, dates, totals, or source combinations are hardcoded from one profile.
- Award inputs are counted aggregate usage only. Raw prompts, paths, session IDs, run IDs, and event IDs never cross the publication boundary.
- Award history is all-time and does not disappear when the headline window advances.
- Day-based awards preserve the first qualifying local date and that day's qualifying value. Hot Streak preserves the fourteen-day threshold when first earned rather than growing with later activity.
- Only earned awards render. Do not add locked placeholders.
- Preserve unfamiliar concurrent edits and avoid unrelated refactors.
- Motion is decorative, subtle, and disabled by `prefers-reduced-motion`.
- Desktop and mobile controls have at least 44 by 44 CSS-pixel hit areas.
- Do not commit while another worker is active in the shared checkout.

---

### Task 1: Generic all-time award derivation

**Files:**
- Create: `src/tokenmaxxing/profile/awards.py`
- Create: `tests/profile/test_awards.py`
- Modify: `src/tokenmaxxing/profile/data.py`
- Modify: `tests/profile/test_data.py`

**Interfaces:**
- Consumes: `Sequence[ProfileUsageRow]`, a timezone, and `event_total`.
- Produces: `Award(key: str, name: str, description: str, metric_value: int, earned_on: date | None)`.
- Produces: `derive_awards(rows: Sequence[ProfileUsageRow], *, timezone: tzinfo) -> tuple[Award, ...]`.
- Extends: `ProfileData.awards: tuple[Award, ...]`.

- [ ] **Step 1: Write failing boundary tests**

Create literal fixtures that prove each exact threshold and the immediately lower value:

```python
assert award_keys(rows(total_tokens=9_999_999_999)) == ()
assert award_keys(rows(total_tokens=10_000_000_000)) == ("tokenmaxxer",)

assert "billion-day" not in award_keys(rows(daily_tokens=999_999_999))
assert "billion-day" in award_keys(rows(daily_tokens=1_000_000_000))

assert "fleet-commander" not in award_keys(rows(daily_agents=249))
assert "fleet-commander" in award_keys(rows(daily_agents=250))

assert "hot-streak" not in award_keys(rows(consecutive_days=13))
assert "hot-streak" in award_keys(rows(consecutive_days=14))

assert "model-collector" not in award_keys(rows(model_count=9))
assert "model-collector" in award_keys(rows(model_count=10))
```

Prove `all-systems-go` requires positive counted usage from each supported source: `claude`, `codex`, `opencode`, and `pi`. Prove repeated events for one agent count once per local day for Fleet Commander, streaks use local calendar dates, missing timestamps cannot earn day-based awards, zero-token rows cannot earn awards, and output ordering follows the catalog rather than metric magnitude.

- [ ] **Step 2: Run the focused tests and verify RED**

Run: `uv run pytest tests/profile/test_awards.py -q`

Expected: collection fails because `tokenmaxxing.profile.awards` does not exist.

- [ ] **Step 3: Implement the minimal award catalog and aggregation**

Use these six fixed generic definitions:

```python
TOKENMAXXER_TOKENS = 10_000_000_000
BILLION_DAY_TOKENS = 1_000_000_000
FLEET_COMMANDER_AGENTS = 250
HOT_STREAK_DAYS = 14
MODEL_COLLECTOR_MODELS = 10
SUPPORTED_HARNESSES = frozenset({"claude", "codex", "opencode", "pi"})
```

Aggregate in one pass where possible. Daily agent counts use distinct `(local_day, agent_key)` pairs. Daily token totals include all positive counted rows. Models and harnesses count only positive-token usage. Select the first qualifying day for `earned_on`; all-time/model/harness awards use `None`.

- [ ] **Step 4: Run award tests and verify GREEN**

Run: `uv run pytest tests/profile/test_awards.py -q`

Expected: every award boundary, de-duplication, timezone, and ordering test passes.

- [ ] **Step 5: Add awards to `ProfileData` through TDD**

First add assertions to `tests/profile/test_data.py` showing `build_profile_data` returns the same tuple as `derive_awards` for the fixture snapshot and returns no awards for an empty snapshot. Run the focused test and verify it fails because `ProfileData.awards` is absent. Then add `awards: tuple[Award, ...]` to `ProfileData` and call `derive_awards(snapshot, timezone=timezone)`.

- [ ] **Step 6: Run the profile aggregation tests**

Run: `uv run pytest tests/profile/test_awards.py tests/profile/test_data.py -q`

Expected: all tests pass without altering existing profile totals, agent counts, or daily model stacks.

### Task 2: Existing profile awards UI

**Files:**
- Modify: `/Users/anjay/Documents/tokenmaxxing-profile-v0/index.html`
- Modify: `/Users/anjay/Documents/tokenmaxxing-profile-v0/validate.mjs`
- Modify renderer templates/assets and renderer tests under `src/tokenmaxxing/profile/` and `tests/profile/` when the concurrent renderer task has created them.

**Interfaces:**
- Consumes: public `stats.awards`, each with `key`, `name`, `description`, `metric_value`, and optional `earned_on`.
- Produces: an optional `<section class="profile-awards" aria-labelledby="awards-title">` below `.profile-lines`.

- [ ] **Step 1: Write failing static-profile behavior tests**

Extend the real static-page validation runtime so its fixture contains `awards`. Assert that:

```javascript
assert.equal(elements.get("awards-list").children.length, 6);
assert.equal(elements.get("awards-title").textContent, "Awards");
assert.match(elements.get("awards-list").children[0].getAttribute("aria-label"), /Tokenmaxxer/);
```

Also assert that an empty award list hides the section, names/descriptions are inserted with text nodes rather than HTML, all award buttons remain keyboard reachable, and the stylesheet contains mobile layout plus reduced-motion behavior.

- [ ] **Step 2: Run static validation and verify RED**

Run: `node /Users/anjay/Documents/tokenmaxxing-profile-v0/validate.mjs`

Expected: the new award assertions fail because the section and renderer do not exist.

- [ ] **Step 3: Add semantic award markup and rendering**

Place the section directly below `.profile-lines`. Render only earned awards in catalog order. Each medallion is a `<button type="button">` with a short face (`10B`, `1B`, `250`, `14`, `10+`, or `ALL`) and an associated visible-on-hover/focus/tap detail containing the generic award name, description, metric, and date when present. Use the award `key` only as a known CSS modifier; never interpolate it into raw HTML.

- [ ] **Step 4: Add responsive styling and restrained motion**

Desktop uses a three-column medallion grid inside the 210px sticky rail. Mobile places the section across the full profile width with the same three columns. Medal buttons use 44px minimum hit areas, visible focus, and no hover-only information. Under `prefers-reduced-motion: no-preference`, stagger the initial reveal by no more than 180ms total and use a two-pixel lift with a one-degree rotation on hover/focus. Under `prefers-reduced-motion: reduce`, remove animation and transforms.

- [ ] **Step 5: Run static validation and verify GREEN**

Run: `node /Users/anjay/Documents/tokenmaxxing-profile-v0/validate.mjs`

Expected: all existing and new behavior tests pass.

- [ ] **Step 6: Port the approved UI into the packaged renderer**

Once the renderer files exist, add the same semantic partial, public-payload allowlist, stylesheet, and tests. The generated HTML must contain earned awards without JavaScript; JavaScript may only enhance tap behavior.

- [ ] **Step 7: Verify desktop, mobile, keyboard, and reduced motion**

Build or serve the profile, inspect 1280px, 760px, 390px, and 320px widths, traverse every award by keyboard, test touch-style activation, inspect both themes, and emulate reduced motion. Fix overlap, clipping, unreadable tooltips, or scroll traps before completion.

### Task 3: Final verification

- [ ] Run `uv run pytest tests/profile -q`.
- [ ] Run `uv run pytest`.
- [ ] Run `uv run ruff check .`.
- [ ] Run `git diff --check`.
- [ ] Run `uv build`.
- [ ] Review `git diff` and confirm unrelated concurrent changes remain intact.
