# SVG Profile Awards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Replace text-faced award circles with cohesive SVG medal emblems and ensure every award tooltip is fully opaque and paints above sibling awards.

**Architecture:** Keep award derivation and the public payload unchanged. Add a local SVG symbol sprite to the renderer assets, select a symbol by the existing award key, and render it decoratively inside the existing accessible button. Fix stacking at the award wrapper, which owns both the button and tooltip.

**Tech Stack:** Python, Jinja2, SVG, CSS, pytest, Node static-profile validation, in-app browser QA.

## Global Constraints

- Preserve the existing 210px desktop profile rail and 3-by-2 award grid.
- Keep 44px minimum targets and 68px compact-layout medals.
- Use only local assets and add no runtime dependency.
- Keep tooltip background fully opaque and above every sibling award.
- Preserve keyboard focus, accessible labels, reduced motion, and current public payload fields.
- Update both the packaged renderer and `/Users/anjay/Documents/tokenmaxxing-profile-v0/index.html`.
- Do not commit during concurrent shared-checkout work.

---

### Task 1: Packaged SVG award emblems

**Files:**
- Create: `src/tokenmaxxing/profile/assets/icons/awards.svg`
- Modify: `src/tokenmaxxing/profile/assets/ASSET_SOURCES.md`
- Modify: `src/tokenmaxxing/profile/templates/partials/header.html.j2`
- Modify: `src/tokenmaxxing/profile/assets/profile.css`
- Modify: `tests/profile/test_render.py`

**Interfaces:**
- Consumes: existing `stats.awards[*].key`, `name`, `description`, `metric`, and `earned_on` fields.
- Produces: decorative `<svg class="award-art"><use href="assets/icons/awards.svg#award-{key}"></use></svg>` markup inside each existing award button.

- [ ] **Step 1: Write failing renderer tests**

Require the rendered award to contain a decorative SVG/use reference, contain no face text such as `>1B<`, and require CSS to include a solid tooltip background plus elevated hover/focus wrappers.

- [ ] **Step 2: Run the focused test and verify RED**

Run: `uv run pytest tests/profile/test_render.py::test_awards_render_a_face_metric_and_optional_earned_date tests/profile/test_render.py::test_award_tooltips_are_opaque_and_stack_above_sibling_awards -q`

Expected: FAIL because the button still renders `1B`, the sprite is missing, and the active wrapper has no elevated stacking rule.

- [ ] **Step 3: Add the SVG symbol sprite**

Create six `symbol` elements with IDs `award-tokenmaxxer`, `award-billion-day`, `award-fleet-commander`, `award-hot-streak`, `award-model-collector`, and `award-all-systems-go`. Use `currentColor`, rounded strokes, and a shared seal-and-ribbon silhouette with a distinct central pictogram for each award.

Record the sprite as original Tokenmaxxing artwork in `ASSET_SOURCES.md`.

- [ ] **Step 4: Render decorative SVG artwork**

Replace `{{ award.face }}` with:

```jinja2
<svg class="award-art" viewBox="0 0 48 48" aria-hidden="true" focusable="false">
  <use href="assets/icons/awards.svg#award-{{ award.key }}"></use>
</svg>
```

Keep the existing button `aria-label` and `aria-describedby` attributes.

- [ ] **Step 5: Fix tooltip painting and SVG styling**

Add `.award-wrap:hover, .award-wrap:focus-within { z-index: 20; }`, retain the solid `background: var(--paper)`, and size `.award-art` without adding an extra circular button border. Keep the existing grid, responsive anchors, motion, and reduced-motion rules.

- [ ] **Step 6: Run the renderer tests and verify GREEN**

Run: `uv run pytest tests/profile/test_render.py tests/profile/test_public_payload.py -q`

Expected: all tests pass.

### Task 2: Live static profile parity and browser verification

**Files:**
- Modify: `/Users/anjay/Documents/tokenmaxxing-profile-v0/index.html`
- Modify: `/Users/anjay/Documents/tokenmaxxing-profile-v0/validate.mjs`

**Interfaces:**
- Consumes: the existing client-side `award.key` and accessible award copy.
- Produces: the same six inline decorative SVG emblems and opaque top-layer tooltip behavior as the packaged renderer.

- [ ] **Step 1: Write failing static-profile assertions**

Require `awardIcon(award.key)` to return SVG markup, award buttons to contain SVG rather than text faces, and CSS to elevate hovered, focused, and expanded medals above siblings while keeping a solid tooltip background.

- [ ] **Step 2: Run static validation and verify RED**

Run: `node /Users/anjay/Documents/tokenmaxxing-profile-v0/validate.mjs`

Expected: FAIL because the current client creates a text `<span>` face and the active medal has no z-index elevation.

- [ ] **Step 3: Implement the six inline SVG emblems**

Add a fixed key-to-SVG-path map and create SVG nodes with `createElementNS`. Unknown keys render a restrained generic star seal. Keep all award names, metrics, and dates in text-only accessible attributes and tooltips.

- [ ] **Step 4: Fix the static stacking context**

Elevate `.award-medal:hover`, `.award-medal:focus-visible`, and `.award-medal[aria-expanded="true"]` because the static tooltip is a child of the transformed medal. Preserve the fully opaque tooltip surface.

- [ ] **Step 5: Run static validation and verify GREEN**

Run: `node /Users/anjay/Documents/tokenmaxxing-profile-v0/validate.mjs`

Expected: all tests pass.

- [ ] **Step 6: Verify visually and interactively**

At 1280px, hover/focus the first award and confirm the entire tooltip covers the second and third awards. At 390px and 320px, inspect first, middle, and edge award tooltips, confirm `scrollWidth === innerWidth`, minimum targets remain at least 44px, and no tooltip content is clipped. Check light and dark themes.

- [ ] **Step 7: Run final verification**

Run:

```bash
uv run pytest
uv run ruff check .
git diff --check
uv build
node /Users/anjay/Documents/tokenmaxxing-profile-v0/validate.mjs
```

Expected: every command exits successfully with no failures.
