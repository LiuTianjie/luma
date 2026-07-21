# LAE Console design language

## Direction: Luma Dashboard instrument (product A)

LAE is a **tenant application engine**, separate from the Luma cluster dashboard
(super-admin fleet / credentials / topology). They must not be merged as one app.

Visually, LAE **inherits the Luma Dashboard instrument language** so operators
and agents experience one control-plane craft:

- Warm operational surfaces (`#201d1d` / light `#fdfcfc`), not moss “lake” skins
- Compact panels, 3–8px radii, accent `#007aff`, status green / amber / red
- IBM Plex Mono for code; system UI stack for chrome (see `luma-dashboard.css`)
- No glassmorphism, grain, ambient orbs, floating template buoys, or oversized serif heroes

Product flows stay LAE-specific: source → diagnose → configure → deploy → live,
application lifecycle, account tokens, and Agent-friendly CLI. Worker remains the
only service allowed to call Luma management APIs.

## Token source of truth

`src/app/luma-dashboard.css` owns colors, type, and shell chrome.

`src/app/globals.css` only:

1. Maps legacy Stillwater CSS variables onto dashboard tokens
2. Styles modals / observatory that are unique to LAE markup

Do not invent a second palette in components.

## Spatial system

Desktop: sticky rail (LAE nav only) + top status/account bar + workspace.

Workspace priority:

1. Compact page header (eyebrow + title + one primary action)
2. Deployment workbench and verified templates
3. Application list and operation stream

Templates render as a dense gallery grid (dashboard card pattern), not a
decorative water field.

Below 760px the rail may stack; preserve horizontal integrity
(`scrollWidth === clientWidth`).

## Motion

Operational, not ambient:

- Standard transitions ~120–180ms, ease-out
- Route/section enter may use short opacity/y
- Respect `prefers-reduced-motion`
- Never invent deployment percentages; use real operation phases/events

## Interaction rules

1. Templates always run normal diagnosis; no skip-policy success path.
2. Upload accepts HTML/ZIP static artifacts only; Dockerfile/Compose via Git/template.
3. Private Git uses short-lived task-bound credentials; never imply long-lived browser secrets.
4. Diagnosis shows structured steps, not builder stdout/stderr.
5. Primary deploy only when `ready`; lock destructive switches while `deploying`.
6. Success routes and domains come from API operation results.
7. Selection uses `aria-pressed` / `aria-current`; progress regions use `aria-live`.

## Identity portal & account

Login and account reuse the same dashboard tokens and density. Identity UX stays
LAE (email magic code, one-time deploy token reveal). Do not import Luma admin
navigation into those pages.

## Implementation boundary

Session `/v1/me`, tenant applications, draft/analysis/deployment, and operation
events are API-driven. Unavailable API surfaces honest empty/error states—no
fixture “running apps.” Worker admission and real routes are authoritative.
