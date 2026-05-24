# Solvyn Editorial Frontend Redesign

## Objective

Rebuild the existing Solvyn Next.js frontend visual system and all current routes so the product feels like an editorial, premium operations console inspired by the supplied reference image, while preserving all existing route behavior and API contracts.

## Scope

The redesign covers:

- Shared application frame and primary navigation.
- Dashboard route at `/`.
- Workflows route at `/workflows`.
- Sessions route at `/sessions`.
- Analytics route at `/analytics`.
- Shared agent and execution graph presentation.
- Responsive behavior for desktop and smaller screens.

The redesign does not change:

- Backend endpoints or request payloads.
- Existing page purposes or interactive workflow execution behavior.
- Authentication, data modeling, or infrastructure.
- The supplied reference image as an application asset.

## Reference Translation

The inspiration image establishes a centered, rounded canvas within a soft lavender environment. Its defining features are:

- A near-even split between cool pearl and midnight navy surfaces.
- Muted violet as the only strong accent.
- Sparse, highly tracked uppercase display typography.
- Very fine outlines, compact labels, minimal controls, and generous negative space.
- A central hero focus rather than a traditional admin dashboard grid.

Solvyn will adopt these design principles without copying the pictured product, brand name, watch photograph, or oversized decorative `S`. The central focus will instead be Solvyn's operational context: platform identity, live status, workflow readiness, and agent execution information.

## Visual System

### Color Palette

- Atmosphere/background: desaturated lavender mist, approximately `#cbc3d7` with subtle darker edge gradients.
- Pearl surface: `#eeecea` to `#f4f2ef`.
- Midnight surface: `#202235` to `#25273b`.
- Violet accent: `#765198`, with softened translucent states.
- Ink on pearl: `#171a26`.
- Text on midnight: `#f2efe8`.
- Muted supporting text: cool gray-violet tones.
- Borders: low-contrast hairlines derived from the panel background.
- Status colors remain semantically recognizable but are desaturated to fit the palette.

### Typography

- Use a geometric or editorial sans display family loaded through `next/font/google`, chosen for clean uppercase letterforms and generous tracking.
- Use a restrained sans family for interface text and operational data.
- Hero/display labels use uppercase and wide tracking.
- Data values and controls remain compact and readable; readability takes priority over mimicking the poster typography.

### Geometry And Effects

- The desktop application is a centered rounded frame with a subtle lavender shadow.
- Surfaces use large clean blocks, fine rules, small bordered pills, and minimal elevation.
- Violet is used for selected states, emphasized actions, live indicators, and focal geometry only.
- The dashboard hero may include CSS/SVG linework or restrained geometric shapes, but no large letterform centerpiece and no unrelated product photography.

## Application Frame

The existing left sidebar will be replaced with a reference-inspired application frame:

- A compact top header spans the frame with the Solvyn wordmark, primary routes, system/profile affordances, and active-route indication.
- Main content sits inside the frame rather than on an open full-screen admin canvas.
- Desktop layouts can divide pearl and midnight panels to maintain the signature contrast.
- A small contextual/status strip may sit at the bottom of the dashboard hero or within page headers.
- On narrow screens, navigation becomes horizontally scrollable or wraps compactly, while content stacks vertically and maintains the same palette and border language.

## Page Designs

### Dashboard

The dashboard becomes the signature editorial entry point:

- A large split hero dominates the first viewport.
- `SOLVYN` appears as the central tracked product title across the split composition.
- Operational text communicates that it is an AI engineering command center.
- Live API state, number of active agents, and workflow readiness are embedded as restrained labels and status modules rather than generic SaaS summary cards.
- Primary actions guide users to run workflows, inspect sessions, and review telemetry.
- Below the hero, registered agents are presented as compact editorial modules with capability labels and status treatments.

### Workflows

- Use a split workspace layout: workflow collection/navigation on one side and configuration/execution detail on the other.
- Selected workflow and run action use violet emphasis.
- Inputs, step sequencing, and execution results remain functionally unchanged.
- Execution state is presented with fine rails, numbered steps, and status labels matching the editorial visual language.

### Sessions

- Render history as a refined ledger rather than a generic table card.
- Preserve ID, title, status, and creation time.
- Use clear row separation, compact status labels, and responsive stacking or horizontal handling on smaller screens.

### Analytics

- Present observability as a dark/light control surface with refined metric blocks, trace feed, audit feed, and metrics endpoint reference.
- Preserve current data retrieval behavior and empty/loading states.
- Use violet selectively for activity and operational emphasis; avoid multiple unrelated accent hues.

## Components And Boundaries

The redesign should retain the existing page and data-fetching boundaries unless a small shared presentational component materially reduces duplication:

- `src/app/layout.tsx`: frame placement, fonts, global shell.
- `src/app/globals.css`: palette tokens, typography, layout primitives, responsive rules, animation and state styling.
- `src/components/Sidebar.tsx`: replace implementation with the new header/navigation shell while keeping a shared navigation boundary; renaming is optional only if clarity materially improves.
- `src/components/AgentCard.tsx`: editorial agent module styling.
- `src/components/ExecutionGraph.tsx`: editorial step/rail styling.
- Route page files: content composition and route-specific presentation while keeping current API calls.

No new backend-facing abstractions are required.

## Data And Error Behavior

- Current `api` client usage and endpoint paths remain unchanged.
- Existing loading and empty states remain available and will be visually restyled.
- Existing caught request failures continue to degrade into status/empty-state UI as currently implemented.
- Workflow run behavior continues to disable the action while execution is in progress and renders results after completion.

## Accessibility And Responsiveness

- Ensure sufficient contrast on both pearl and midnight surfaces.
- Preserve keyboard-accessible links, buttons, and input focus indication.
- Keep body text readable despite tracked display typography.
- Desktop target: full editorial frame and split composition.
- Narrow target: stacked panels, navigable header, no clipped critical actions or data rows.

## Verification

Implementation is complete only when:

- The frontend builds successfully with `npm run build` from `frontend`.
- Dashboard, Workflows, Sessions, and Analytics render without client-side visual/layout breakage.
- Browser verification captures the desktop dashboard and each secondary route.
- A narrow-width dashboard check confirms the shell and primary content remain usable.
- Existing route and API-facing behavior has not been intentionally changed.

## Known Constraint

The workspace does not contain a Git repository at `C:\Users\DELL\Documents\AI Agents\Solvyn`, so the design document cannot be committed as prescribed by the design workflow unless repository metadata is later supplied.
