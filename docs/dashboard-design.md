# Luma console design

Reference reviewed on 2026-09-12: the Cloudflare routes screenshot provided by the user. Apply its information hierarchy across the console, including application, delivery, infrastructure, observability and settings workspaces.

## Sources and component decision

- [Kumo](https://github.com/cloudflare/kumo) is Cloudflare's public React component library, built on Base UI. Its [resource-list layout](https://github.com/cloudflare/kumo/blob/main/packages/kumo/src/blocks/resource-list/resource-list.tsx) constrains page width and standardizes title, description and resource content.
- [Flow](https://kumo-ui.com/components/flow/) provides directed relationships, parallel branches and custom nodes. This is a verified available component; the screenshot alone does not establish which component that particular dashboard page uses.
- [cf-ui](https://github.com/cloudflare/cf-ui) was archived in 2021 and is not a suitable new dependency.

Retain Luma's existing React 19, Base UI, Tailwind 4 and shadcn components. Adopt the reference's visual rules through shared semantic tokens and components. Retain Cytoscape and Dagre for the existing graph interactions; do not introduce a second component or graph framework just for appearance.

## Shared rules

- Neutral page background, white content surfaces, thin borders, 8px base radius. Blue is reserved for primary actions, links and selection. Semantic health colors require evidence; exposure or route configuration does not imply health.
- Persistent topbar with current workspace and sync status. Preferences and sign-out live only in the sidebar; there is no global refresh button. Desktop expand/collapse lives in the sidebar header and remains visible in the collapsed rail; mobile retains a navigation opener because its sidebar is off-canvas. Sidebar keeps existing workspace ownership and nested routes. Preserve Luma branding and cluster identity.
- Main content width at most 1400px, responsive horizontal gutters, 24px section rhythm. Terminal workspaces retain their full available size.
- Use one page heading with actions aligned right on desktop. Observability subpages use compact 22px titles without repeated workspace descriptions. Application detail breadcrumbs live in the topbar; the content has a compact object title and tabs. Metrics are compact labeled counts below the heading. They are informational, not fake filter controls.
- Tables use subtle header/alternate-row surfaces, consistent cell padding and horizontal containment. Forms and overlays continue using the existing semantic components, focus handling and validation.
- Network views separate routes from node topology. Route search and ingress type filter apply to both diagram and table. Selecting a route focuses its current path; clearing selection restores the filtered overview. Certificate retry remains conditional on backend capability.
- Preserve light/dark themes, keyboard access and narrow-screen navigation. Avoid adding API diagnostics or unsupported health claims to visually fill empty states.

## Validation boundary

Use local development fixtures for layout and interaction checks. These checks do not establish production health or change any live route, certificate or deployment.

## Public website

The GitHub Pages site uses the same neutral surfaces, 8px cards, thin borders and blue actions. Its marketing layout has larger editorial headings while the embedded illustrative console stays compact. English and Chinese landing pages carry equivalent content. Product illustrations use example domains and explicit sample-data labels, never a production-health claim.

Documentation is built from repository Markdown into standalone pages with navigation, tables and a table of contents. See [Website maintenance](website.md) for the build and publishing workflow.
