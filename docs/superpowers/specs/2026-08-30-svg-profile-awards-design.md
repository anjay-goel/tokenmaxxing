# SVG Profile Awards Design

## Goal

Make earned awards read as deliberate, collectible emblems while keeping the existing quiet editorial profile layout intact.

## Visual direction

Use one cohesive engraved-medal family rather than text inside circles. Each award uses a local, theme-aware SVG symbol with a small ribbon-and-seal silhouette and a distinct pictogram:

- Tokenmaxxer: stacked tokens with a spark.
- Billion Day: a radiant sunburst.
- Fleet Commander: a three-ship formation.
- Hot Streak: a flame.
- Model Collector: a connected constellation.
- All Systems Go: four linked systems with a check.

The six emblems retain the existing muted award colors. They remain 49px in the desktop profile rail and expand to 68px touch targets below 760px. No text or number is drawn on the face; the award name, earned metric, and date remain in the tooltip and accessible label.

## Tooltip behavior

Award tooltips use the same fully opaque surface as the other profile tooltips: solid `var(--paper)` background, hairline border, and shadow. The active award wrapper is elevated above every sibling on hover and keyboard focus so its tooltip cannot be covered by later awards. Mobile tooltips remain anchored inside the viewport.

## Accessibility and motion

SVG artwork is decorative and hidden from assistive technology. Each award button keeps a complete accessible label and tooltip relationship. Existing 44px minimum targets, keyboard focus, 240ms entrance stagger, 2px hover lift, and reduced-motion behavior remain.

## Scope

Update both the packaged renderer and the live static profile prototype. Add no runtime dependency and publish no additional profile data.

## Verification

- Rendering tests require six SVG symbols and decorative SVG markup instead of text faces.
- CSS tests require opaque tooltip surfaces and elevated active wrappers.
- Browser checks cover the first award tooltip at desktop width and edge awards at 390px and 320px.
- Run the profile suite, full suite, Ruff, `git diff --check`, and `uv build`.
