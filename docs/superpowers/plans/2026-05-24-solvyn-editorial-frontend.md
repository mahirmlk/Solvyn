# Solvyn Editorial Frontend Implementation Plan

> **For agentic workers:** REQUIRED SUB-SKILL: Use superpowers:subagent-driven-development (recommended) or superpowers:executing-plans to implement this plan task-by-task. Steps use checkbox (`- [ ]`) syntax for tracking.

**Goal:** Rebuild Solvyn's four-route Next.js frontend into a pearl-and-midnight editorial operations console inspired by the supplied image while preserving its API-driven behavior.

**Architecture:** Keep the existing App Router pages and `api` client intact. Replace the sidebar-led SaaS layout with a centered application frame and top navigation, centralize the visual system in `globals.css`, and restyle page/component markup around split surfaces and compact operational modules.

**Tech Stack:** Next.js 15 App Router, React 19, TypeScript, Tailwind utility classes plus global CSS, local browser verification.

---

## File Structure

- Modify `frontend/src/app/globals.css`: palette, atmosphere, editorial frame primitives, states, layouts, responsive rules.
- Modify `frontend/src/app/layout.tsx`: root frame composition and metadata.
- Modify `frontend/src/components/Sidebar.tsx`: shared top navigation/header shell, retaining route-awareness through `usePathname`.
- Modify `frontend/src/app/page.tsx`: split dashboard hero, metric/status modules, action strip, agent section.
- Modify `frontend/src/components/AgentCard.tsx`: editorial agent row/card presentation and unified violet/state styling.
- Modify `frontend/src/app/workflows/page.tsx`: split workflow index/detail presentation while retaining selection and execution functions.
- Modify `frontend/src/components/ExecutionGraph.tsx`: numbered rail presentation for workflow output.
- Modify `frontend/src/app/sessions/page.tsx`: ledger layout with responsive row treatment.
- Modify `frontend/src/app/analytics/page.tsx`: editorial metrics and observability feeds.
- Preserve `frontend/src/lib/api.ts`: endpoints and response contracts are unchanged.

Automated component tests are not added because this application has no JavaScript test runner or DOM-test dependencies and the change is presentational. The verification contract is a successful production build plus browser checks on all affected routes.

### Task 1: Establish The Editorial Shell And Tokens

**Files:**
- Modify: `frontend/src/app/globals.css`
- Modify: `frontend/src/app/layout.tsx`
- Modify: `frontend/src/components/Sidebar.tsx`

- [ ] **Step 1: Run the existing automated baseline**

Run: `npm run build`

Expected: the existing Next.js frontend builds before visual changes, or any pre-existing build failure is documented before implementation.

- [ ] **Step 2: Replace the token set and frame primitives**

Implement CSS tokens and layout classes equivalent to:

```css
:root {
  --color-atmosphere: #cbc4d6;
  --color-pearl: #efedeb;
  --color-midnight: #202235;
  --color-violet: #775499;
  --color-ink: #191b26;
  --color-light: #f4f1ec;
  --line-light: rgba(25, 27, 38, 0.12);
  --line-dark: rgba(244, 241, 236, 0.14);
}

.app-frame {
  width: min(1320px, calc(100vw - 56px));
  margin: 32px auto;
  border-radius: 18px;
  overflow: hidden;
  background: var(--color-pearl);
}
```

Add reusable classes for `app-frame`, `topbar`, `editorial-panel`, `dark-panel`, `eyebrow`, `status-pill`, `action-button`, loading skeletons, focus states, and responsive stacking.

- [ ] **Step 3: Change the root shell and navigation**

Render the shared navigation above page content:

```tsx
<div className="app-frame">
  <Sidebar />
  <main className="app-content">{children}</main>
</div>
```

Update `Sidebar` to output a top header with the `Solvyn` identity, links for `/`, `/workflows`, `/sessions`, and `/analytics`, and a compact right-side system/profile group. Preserve accessible `Link` navigation and active route styles.

- [ ] **Step 4: Rebuild after the shell change**

Run: `npm run build`

Expected: PASS with all four routes statically generated or compiled as before.

### Task 2: Build The Dashboard Signature View

**Files:**
- Modify: `frontend/src/app/page.tsx`
- Modify: `frontend/src/components/AgentCard.tsx`

- [ ] **Step 1: Keep the data contract unchanged**

Retain this request behavior:

```tsx
Promise.all([
  api.health().catch(() => ({ status: "error", service: "solvyn" } as HealthStatus)),
  api.listAgents().then((r) => r.agents).catch(() => [] as AgentInfo[]),
]).then(([h, a]) => {
  setHealth(h as HealthStatus);
  setAgents(a as AgentInfo[]);
  setLoaded(true);
});
```

- [ ] **Step 2: Compose the split editorial hero**

Replace the old stat-card header with a split hero containing:

```tsx
<section className="dashboard-hero">
  <div className="hero-pearl">{/* purpose, product label, actions */}</div>
  <div className="hero-midnight">{/* API status, workflow readiness, operating readouts */}</div>
  <h1 className="hero-wordmark">SOLVYN</h1>
</section>
```

Use `healthy`, `agents.length`, and the existing three workflow/action destinations to populate the hero without creating new API behavior.

- [ ] **Step 3: Restyle agents and action modules**

Render agent content beneath the hero in editorial modules:

```tsx
<AgentCard
  key={agent.type}
  name={agent.name || agent.type}
  description={agent.description}
  status={(agent.status || "idle") as "idle" | "running" | "error" | "success"}
  capabilities={agent.capabilities || []}
/>
```

Update `AgentCard` to use a restrained violet marker, single-color tags, and compact status pills rather than multicolor gradient icons.

- [ ] **Step 4: Rebuild after dashboard composition**

Run: `npm run build`

Expected: PASS; `/` compiles with existing loading and empty-agent paths.

### Task 3: Redesign Workflow Execution Presentation

**Files:**
- Modify: `frontend/src/app/workflows/page.tsx`
- Modify: `frontend/src/components/ExecutionGraph.tsx`

- [ ] **Step 1: Preserve workflow interaction functions**

Do not change these behavioral entry points:

```tsx
async function selectWorkflow(name: string) {
  setSelected(name);
  setResult(null);
  const d = await api.getWorkflow(name);
  setDetail(d);
  setInputs(d.default_inputs);
}

async function runWorkflow() {
  if (!selected) return;
  setRunning(true);
  try {
    const r = await api.runWorkflow(selected, inputs);
    setResult(r);
  } finally {
    setRunning(false);
  }
}
```

- [ ] **Step 2: Replace generic cards with a split workbench**

Use:

```tsx
<div className="page-split workflow-workbench">
  <aside className="pearl-pane workflow-index">{/* workflow buttons */}</aside>
  <section className="midnight-pane workflow-detail">{/* inputs, run button, output */}</section>
</div>
```

Use violet selected state, fine-rule labels, dark input treatment, and retained loading/empty/detail conditions.

- [ ] **Step 3: Style execution as an editorial rail**

Update `ExecutionGraph` to number nodes, show a vertical rail, retain pending/running/success/error status meaning, and render the current spinner only for active steps.

- [ ] **Step 4: Rebuild after workflow changes**

Run: `npm run build`

Expected: PASS; `/workflows` compiles with selectable workflow and execution-result UI.

### Task 4: Redesign Sessions And Analytics

**Files:**
- Modify: `frontend/src/app/sessions/page.tsx`
- Modify: `frontend/src/app/analytics/page.tsx`

- [ ] **Step 1: Convert sessions into a ledger**

Keep `api.listSessions()` behavior. Build a pearl ledger with an editorial title block, compact columns, status pills, and mobile-responsive row presentation:

```tsx
<section className="ledger-panel">
  <div className="ledger-head">{/* ID, Title, Status, Created */}</div>
  <div className="ledger-body">{/* loading, empty, or session rows */}</div>
</section>
```

- [ ] **Step 2: Convert analytics into a split observability surface**

Keep the current `Promise.all` trace/audit retrieval and display:

```tsx
<div className="analytics-layout">
  <section className="metric-strip">{/* four existing metric identifiers */}</section>
  <section className="analytics-split">
    <div className="midnight-pane">{/* traces */}</div>
    <div className="pearl-pane">{/* audit entries */}</div>
  </section>
</div>
```

All accent styling uses the violet system except semantic error/success feedback.

- [ ] **Step 3: Run production build for the complete rewrite**

Run: `npm run build`

Expected: PASS for `/`, `/workflows`, `/sessions`, and `/analytics`.

### Task 5: Browser Verification And Responsive Polish

**Files:**
- Modify, only if verification reveals defects: files listed in Tasks 1-4.

- [ ] **Step 1: Start the local frontend**

Run: `npm run dev`

Expected: the Next.js development server starts on an available localhost port.

- [ ] **Step 2: Verify every desktop route in the Browser app**

Open `/`, `/workflows`, `/sessions`, and `/analytics`. For each route check:

```text
- Top navigation and active route are visible.
- Pearl/midnight/violet visual system is consistent.
- No content overflows its frame.
- Loading or API-unavailable states render intentionally.
- No browser console errors caused by the rewrite.
```

Capture route screenshots for completion evidence.

- [ ] **Step 3: Verify the dashboard at narrow width**

Check a mobile or narrow viewport and confirm:

```text
- Header remains navigable.
- Split hero stacks without covering the SOLVYN title or actions.
- Agent/action panels remain readable.
- No horizontal clipping of critical controls.
```

- [ ] **Step 4: Fix visual defects and repeat verification**

Apply only defect-driven changes, rerun `npm run build`, reload affected browser routes, and capture updated screenshots.

## Self-Review

- Spec coverage: shared shell, all four routes, component styling, preserved API behavior, accessibility/responsiveness, build and browser verification are each represented above.
- Placeholder scan: no deferred implementation markers remain.
- Type consistency: plan uses current exported API types and existing state functions; no new backend interfaces are introduced.
- Constraint: Git commits and isolated worktrees are not possible because no Git repository exists at the workspace root.
