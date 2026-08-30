# Responsive Profile Controls and Awards Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Keep the established desktop profile intact while fixing the small-screen theme and link layout and replacing abstract ribbon badges with understandable circular award coins and visible titles.

**Architecture:** The packaged Jinja renderer remains the source of truth, with the checked-in live prototype updated in parallel for immediate visual review. Theme state stays in the existing JavaScript controller; markup exposes two SVG states without replacing button contents. Awards remain derived generically from public aggregate data and use a local SVG symbol sprite.

**Tech Stack:** Jinja2 templates, CSS, vanilla JavaScript, local SVG symbols, pytest, Node validation.

## Global Constraints

- Preserve the current desktop layout and visual tone.
- Use an icon-only theme control with a visible 34px button and an accessible 44px hit area.
- Keep every award face circular with no ribbon tails.
- Show each award name and metric without requiring a tooltip.
- Keep the full award description in an opaque accessible tooltip.
- Keep the three profile links together from 341px through 760px; stack all three at 340px and below.
- Respect reduced-motion preferences and avoid new runtime dependencies.
- Do not alter award eligibility, names, or data derivation.

---

### Task 1: Lock the rendered responsive and award contracts

**Files:**
- Modify: `tests/profile/test_render.py`
- Modify: `/Users/anjay/Documents/tokenmaxxing-profile-v0/validate.mjs`

**Interfaces:**
- Consumes: rendered `index.html`, `profile.css`, and `profile.js`
- Produces: regression coverage for icon-only theme controls, visible award labels, circular SVG art, and non-orphaned mobile links

- [ ] **Step 1: Write failing render assertions**

Add assertions that require two theme SVG states, visible `.award-name` and `.award-summary` content, a circular coin-only award layout, an anchored mobile `.theme-dock`, and a three-item mobile link row above the 340px stack breakpoint.

- [ ] **Step 2: Run focused tests and verify the expected failures**

Run: `uv run pytest tests/profile/test_render.py -q`

Expected: failures for missing icon markup, award labels, and responsive CSS.

- [ ] **Step 3: Add equivalent prototype validator assertions**

Require the live prototype to create an award row containing the SVG art, visible title, and visible metric, and to retain accessible labels and descriptions.

- [ ] **Step 4: Run the prototype validator and verify the expected failures**

Run: `node /Users/anjay/Documents/tokenmaxxing-profile-v0/validate.mjs`

Expected: failures for the pre-change award DOM and responsive CSS.

### Task 2: Implement the packaged renderer treatment

**Files:**
- Modify: `src/tokenmaxxing/profile/templates/index.html.j2`
- Modify: `src/tokenmaxxing/profile/templates/partials/header.html.j2`
- Modify: `src/tokenmaxxing/profile/assets/profile.css`
- Modify: `src/tokenmaxxing/profile/assets/profile.js`
- Modify: `src/tokenmaxxing/profile/assets/icons/awards.svg`

**Interfaces:**
- Consumes: existing `Award` view values and `data-theme-toggle`
- Produces: icon-only theme button, circular local award symbols, visible award labels, and responsive link/control layout

- [ ] **Step 1: Add stable theme icon markup and accessible state updates**

Render sun and moon SVGs inside the existing button. Update only `aria-label` and `title` when the theme changes so JavaScript never destroys the SVG children.

- [ ] **Step 2: Render visible award names and metrics**

Keep the button and tooltip relationship, but place the coin and a `.award-copy` block in each row so names and metrics remain readable without hover.

- [ ] **Step 3: Replace the sprite artwork**

Use a shared 48px circular coin frame and literal centered symbols: `10B`, `1B`, fleet nodes, flame, model tiles, and a four-port check. Remove every ribbon path.

- [ ] **Step 4: Implement the responsive layout**

At 760px and below, anchor the theme button to the page top-right and place links in one compact row. At 340px and below, switch all links to one column. Keep packaged desktop positioning unchanged.

- [ ] **Step 5: Run focused tests until green**

Run: `uv run pytest tests/profile/test_render.py -q`

Expected: all focused tests pass.

### Task 3: Mirror and verify the live prototype

**Files:**
- Modify: `/Users/anjay/Documents/tokenmaxxing-profile-v0/index.html`
- Modify: `/Users/anjay/Documents/tokenmaxxing-profile-v0/assets/awards.svg`
- Modify: `/Users/anjay/Documents/tokenmaxxing-profile-v0/validate.mjs`

**Interfaces:**
- Consumes: prototype `profile.json` award payload
- Produces: visually equivalent live prototype at `http://127.0.0.1:4173/`

- [ ] **Step 1: Mirror theme, link, award markup, CSS, and SVG treatment**

Keep prototype-specific dynamic award creation and click behavior while producing the same visible rows and coin art as the packaged renderer.

- [ ] **Step 2: Run the prototype validator until green**

Run: `node /Users/anjay/Documents/tokenmaxxing-profile-v0/validate.mjs`

Expected: all validator checks pass.

- [ ] **Step 3: Inspect desktop, 390px, and 320px browser states**

Verify no horizontal overflow, no 2+1 link orphan, top-right theme placement, readable award labels, circular art, opaque tooltips, keyboard focus, and unchanged desktop composition.

- [ ] **Step 4: Run repository verification**

Run: `uv run pytest && uv run ruff check . && git diff --check && uv build`

Expected: every command exits zero.
