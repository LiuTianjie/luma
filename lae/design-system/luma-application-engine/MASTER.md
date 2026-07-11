# LAE Design System — Luma Dashboard

LAE directly inherits the existing Luma Dashboard visual language. The source
of truth is `luma/assets/dashboard/styles.css`; LAE must not introduce a second
brand system.

## Non-negotiable inheritance

- IBM Plex Mono / Berkeley Mono style monospace typography throughout.
- Dark theme tokens: `#201d1d` background/surface, `#302c2c` raised surfaces,
  `#fdfcfc` text, `#9a9898` muted text.
- Light theme tokens: `#fdfcfc` background/surface, `#f1eeee` raised surfaces,
  `#201d1d` text, `#424245` muted text.
- Primary action `#007aff`; status green `#30d158`, amber `#ff9f0a`, red
  `#ff3b30` (with the Dashboard's darker accessible light-theme variants).
- 264px sidebar, 68px top bar, 6–8px panel radius, 3–6px control radius.
- Compact cards, tables, forms and operational density. No oversized serif
  headlines, glassmorphism, decorative lakes, grain, large empty canvases or
  product-specific color palette.
- Same `luma.dashboard.theme` local-storage key and light/dark behavior.

## LAE-specific components

LAE may add deployment phase tracking, source selection, template gallery,
Agent diagnostics, environment forms and deployment route handoff. These must
be composed from the same Dashboard panel, gallery-card, status badge, form,
button and terminal patterns.

Templates use the Dashboard deployment-gallery shape: compact rows/cards with
icon, stack, description and selection state. They do not float or animate
continuously.

## UX priorities

1. Deployment source and next action remain visible without scrolling at common
   desktop sizes.
2. Operational data is never replaced by decoration or fixture content.
3. Guest authentication is not reported as API unavailability.
4. All Compose public HTTP routes are shown after a successful deployment.
5. Async work uses real phases/events, never invented percentages.
6. Dark and light themes, 375px mobile, keyboard navigation and reduced motion
   are release gates.
