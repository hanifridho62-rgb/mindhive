---
title: "feat: MindParallel stage-then-run, 16-tab view, orphan fix; MindSwarm cat nodes"
type: feat
date: 2026-07-11
status: ready
origin: /mindhive orchestrator interview (2026-07-11) — all four scope decisions user-approved
---

# feat: MindParallel stage-then-run, 16-tab view, orphan fix; MindSwarm cat nodes

## Summary

The Workspace (MindParallel + MindSwarm) shipped in PR #41/#43 with instant-run
skill drops, a split-tree layout, and text chips on the MindSwarm mission tree.
This plan layers four user-approved changes on top: (1) skill drops **stage**
into a per-pane recipe and only an explicit **Run** button launches Claude;
(2) a **16-tab** view (one full-size agent per tab, per-tab account profile)
toggleable with the existing splits; (3) **orphaned server agents are adopted**
into the UI on load, plus a **Clear idle** button — fixing the "9/16 agents
with empty panes" drift; (4) MindSwarm nodes render as **6 pixel-art SVG cats**
chosen deterministically from the agent id, and the node inspector shows
**skills used + run info** (turns, cost).

---

## Problem Frame

- Dropping a skill fires a Claude run immediately — there is no way to compose
  an agent (several skills + a command) before running. The user's request:
  "only run when I customize the agent I want."
- 16 agents in a split tree get unusably small; the user wants full-size tabs,
  one Claude Code account per tab.
- Pane layout lives in browser localStorage while agents live in the
  server-side `Manager` singleton — so agents orphan, the header count climbs
  ("9/16") while panes render empty, and nothing reconciles them.
- MindSwarm chips are text-only and the inspector shows the raw stream but not
  *which skills* an agent used or its run economics.

---

## Requirements

- **R1** — A skill dropped on a MindParallel pane stages into a visible recipe
  (removable chips + command input); nothing executes until the pane's Run
  button is pressed. Kanban card dispatch stays immediate.
- **R2** — A tab-strip view shows up to 16 agents, one full-size per tab, each
  tab spawnable with its own account profile; toggleable with the split view;
  the choice persists.
- **R3** — On load/poll, server agents not shown anywhere are adopted into the
  UI (splits: filled into empty/new leaves; tabs: listed by server truth), so
  the header count always matches what is visible. A "Clear idle" button
  removes all non-running, non-swarm agents in one click.
- **R4** — MindSwarm nodes render as one of 6 inline pixel-art SVG cats
  (white, calico, orange, grey tabby, siamese, black), picked
  deterministically from the agent id (stable across refreshes).
- **R5** — Tapping a MindSwarm node shows the skills the agent has used and
  how it is running (role, profile, status, session, turns, cost).
- **R6** — Pages stay offline-self-contained (no external URLs) and
  palette-aligned per docs/plans/2026-07-10-001-feat-workspace-gap-closure-plan.md;
  all existing pinned tests keep passing.

---

## Key Technical Decisions

- **Staging is client-side state; Run composes one prompt.** The backend's
  one-run-per-send model is untouched. Staged skills live in a localStorage
  map keyed by agent id (pane id before spawn); Run joins them into a single
  prompt ("Use the X skill (from the Y plugin)… <command>") and posts to the
  existing `/api/workspace/send`. Rationale: zero backend risk, recipe
  survives refresh, and the composed prompt is visible in the pane scrollback
  (`❯ …`) so the run is auditable.
- **Tabs are server-truth; splits stay layout-truth.** The tab strip renders
  directly from `/api/workspace/agents` (plus a "+" tab), which makes orphaned
  agents impossible in tab view by construction. The split tree keeps its
  localStorage layout but gains an adoption pass. Both views share the same
  pane body markup and wiring.
- **Orphan adoption targets non-swarm agents only.** MindParallel adopts
  role-`agent`, non-swarm orphans; swarm agents belong to the MindSwarm view
  and its `mission_stop` lifecycle. `Clear idle` mirrors this: it removes
  agents with `status != "running"` and `swarm == False`, server-side, so any
  stale browser state cannot resurrect them.
- **Skills-used and run stats are parsed server-side in the reader thread.**
  `Manager._reader` already iterates every stream-json event; it additionally
  records `tool_use` blocks named `Skill` (input's skill name) into
  `Agent.skills_used` and the `result` event's `num_turns` /
  `total_cost_usd` into `Agent.last_turns` / `Agent.last_cost`, all exposed
  via `snapshot()`. Rationale: testable in `tests/test_workspace.py` with the
  existing fake-spawn harness, and both pages get the data for free from the
  agents poll rather than re-parsing scrollback text client-side.
- **Cats are inline SVG pixel grids, deterministic by id.** Six palettes
  (white, calico, orange, grey tabby, siamese, black) render one shared
  pixel-cat shape as SVG `<rect>` cells; index = `parseInt(id.slice(0,4), 16) % 6`
  (agent ids are hex). No image files, no network — keeps the pinned
  offline-self-containment test green.

---

## Implementation Units

### U1. Stage-then-run recipes in MindParallel

**Goal:** Skill drops stage; explicit Run launches the composed Claude run (R1).

**Requirements:** R1, R6

**Dependencies:** none

**Files:**
- `src/mindhive/workspace_page.py` (modify)
- `tests/test_workspace_pages.py` (extend)

**Approach:** Add a `STAGED` map (localStorage-persisted) keyed by agent id, or
pane id until the pane has an agent (key migrates on spawn). `onDrop` with
`kind:'skill'` appends to the pane's staged list instead of posting; the pane
renders a recipe strip of chips, each with a remove ×. The pane input keeps
its role as the command; a new **▶ Run** control composes
`Use the <name> skill (from the <plugin> plugin). …` lines plus the command
(fallback instruction when the command is empty) and posts one
`/api/workspace/send`. Run on an empty pane spawns first (existing spawn path,
pane's name/profile inputs), then sends. Plain Enter in the input with no
staged skills keeps today's direct-prompt behavior. Kanban `kind:'task'`
drops keep dispatching immediately.

**Patterns to follow:** existing `onDrop` / `wirePane` structure; the
"seed AGENTS before repaint" and "pin agentId before await" regression
patterns already pinned by tests.

**Test scenarios:**
- Rendered page contains the staging marker (`STAGED`), a chip-remove control,
  and a Run control; the skill-drop handler no longer posts
  `/api/workspace/send` directly from the drop path (assert the drop branch
  stages — e.g. drop code references `STAGED`, and the run-composition string
  `Use the ` appears in the Run path, not the drop path).
- Task-card drop still posts `/api/workspace/board_dispatch` (existing pin
  stays green).
- Existing pins (spawn seeding, pinned agentId, two-miss reset, 16-cap,
  payload kinds) all still pass.

**Verification:** In a live dashboard, dropping two skills shows two chips and
starts nothing; pressing Run starts exactly one run whose `❯` echo contains
both skill instructions and the typed command; removing a chip before Run
excludes it.

### U2. 16-tab view toggleable with splits

**Goal:** Full-size per-agent tabs, one account profile per tab, up to the
16-agent cap (R2).

**Requirements:** R2, R6

**Dependencies:** U1 (shares the pane body + staging markup)

**Files:**
- `src/mindhive/workspace_page.py` (modify)
- `tests/test_workspace_pages.py` (extend)

**Approach:** Header gains a view toggle (Tabs ⇄ Splits) persisted in
localStorage (`mh_ws_view`). Tab mode replaces the grid with a tab strip
rendered from the live agents list (non-swarm agents), plus a **+** tab
carrying the name/profile spawn form; the body is a single full-size pane
bound to the active tab's agent, reusing `paneHTML`/`wirePane` and U1 staging.
Active tab id persists; a closed/vanished agent falls back to the first tab.
Tab labels show name, profile, and status dot. The 16 cap is the server cap —
the + tab surfaces the server's error toast when full.

**Patterns to follow:** `buildGrid`/`paintAgents` poll-repaint structure;
profile `<select>` population from `PROFILES`.

**Test scenarios:**
- Page contains the view toggle and the `mh_ws_view` persistence key.
- Tab strip markup and + tab spawn path present; tab spawn posts
  `/api/workspace/spawn` with a profile (assert the strip references
  `PROFILES` / profile select).
- Splits assertions (split directions, 16-pane cap) still pass — toggle must
  not delete the split code path.

**Verification:** Toggling to Tabs shows every live agent as a full-size tab;
spawning from + with a second profile creates a tab running under that
profile; toggling back to Splits restores the saved split layout.

### U3. Orphan adoption + Clear idle

**Goal:** Header count always matches visible agents; one-click cull of idle
leftovers (R3).

**Requirements:** R3

**Dependencies:** U2 (tab view is the server-truth half of the fix)

**Files:**
- `src/mindhive/workspace.py` (modify — `Manager.clear_idle`, API action)
- `src/mindhive/dashboard.py` (modify — register `clear_idle` POST route)
- `src/mindhive/workspace_page.py` (modify — adoption pass, Clear idle button)
- `tests/test_workspace.py` (extend)
- `tests/test_workspace_pages.py` (extend)

**Approach:** Frontend: after each poll in splits mode, any non-swarm agent
not referenced by a layout leaf is adopted — assigned to the first empty leaf,
else a new leaf is split in (respecting the 16-pane cap); the layout is saved
so adoption is sticky. Header gains **Clear idle** with a `confirm()`, posting
`/api/workspace/clear_idle`. Backend: `Manager.clear_idle()` removes (via the
existing `remove`) every agent with `status != "running"` and `swarm` false,
returns `{ok, removed}`; wire into `api_post` and the dashboard's POST route
list. The existing two-miss vanish logic then clears their panes.

**Patterns to follow:** `Manager.remove`/`stop` locking style; dashboard route
registration loop at the `_POST_ROUTES` workspace block.

**Test scenarios:**
- Backend: with three spawned agents (one running via fake process, one done,
  one idle) plus one swarm agent, `clear_idle` removes exactly the done+idle
  non-swarm agents, returns `removed == 2`, and the running and swarm agents
  survive.
- Backend: `api_post("clear_idle", …)` dispatches to the manager (route
  smoke).
- Page: contains the adoption marker (e.g. an `adoptOrphans` function), the
  Clear idle control, and `/api/workspace/clear_idle`.

**Verification:** Reproduce the drift (spawn agents, clear localStorage,
reload): panes repopulate and the count matches; Clear idle drops idle agents
and the count falls accordingly.

### U4. MindSwarm cat nodes + skills/run inspector (and pane run stats)

**Goal:** Nodes are pixel cats; tapping one shows skills used and run info
(R4, R5).

**Requirements:** R4, R5, R6

**Dependencies:** none (backend half); U1–U3 only share files, not behavior

**Files:**
- `src/mindhive/workspace.py` (modify — `skills_used`, `last_turns`,
  `last_cost` on `Agent` + `snapshot`, parsed in `_reader`)
- `src/mindhive/swarm_page.py` (modify — cat sprites, inspector fields)
- `src/mindhive/workspace_page.py` (modify — surface turns/cost in the pane
  header meta so Claude run data is reflected in MindParallel too)
- `tests/test_workspace.py` (extend)
- `tests/test_workspace_pages.py` (extend)

**Approach:** Backend: in `_reader`, for `assistant` events, scan content
blocks for `tool_use` with `name == "Skill"` and record the input's skill
name (deduped, ordered) on `agent.skills_used`; on the `result` event record
`num_turns` and `total_cost_usd`. All three fields join `snapshot()`.
Swarm page: define one pixel-cat shape rendered as SVG rects with 6 palette
variants; `catFor(id)` = `parseInt(id.slice(0,4),16) % 6`; chips render the
cat sprite above the name chip (role color stays as the border/label accent).
Inspector adds a "skills used" line (from the snapshot, em-dash when empty)
and extends the meta line with turns and cost. Parallel page: pane header
shows `· N turns · $C` when present.

**Patterns to follow:** `render_stream_event`'s defensive block parsing;
`ROLE_STYLE`/`ROLE_ICON` map style for the cat palette map; fake-spawn
stream-json fixtures in `tests/test_workspace.py`.

**Test scenarios:**
- Backend happy path: a fake run whose stream contains two `Skill` tool_use
  blocks (one repeated) and a result with `num_turns`/`total_cost_usd` yields
  `skills_used == [first, second]` (deduped), `last_turns`, `last_cost` set,
  and all three present in `snapshot()`.
- Backend edge: malformed tool_use input (no skill key) and non-Skill tools
  are ignored without error; agents that never ran expose empty/None fields.
- Page: swarm HTML contains 6 cat palette entries and the deterministic
  index expression (pin `% 6`); no `http(s)://` (existing offline pin covers
  it); inspector references `skills_used`.
- Page: parallel HTML references turns/cost fields.

**Verification:** Launch a small mission: every chip shows a cat, the same
agent keeps the same cat across reloads, and clicking a node that invoked a
skill lists it with turns/cost in the inspector.

---

## Scope Boundaries

### Deferred to Follow-Up Work

- Reusable named recipes (save/load a staged skill set across panes) — U1
  stores per-pane recipes only.
- Kanban drag-to-"running" auto-dispatch (carried from the 2026-07-10 plan).
- Cat sprites in MindParallel tab labels — cats are a MindSwarm identity
  element for now.

### Out of Scope

- PTY/terminal emulation in panes; websockets (polling stays).
- Changes to mission/role mechanics or the shared-memory lenses.
- Using the user's original cat image file (recreated as SVG per approved
  decision).

---

## Risks & Dependencies

- **Prompt-composition drift (U1):** a staged-skill prompt is still one
  plain-text prompt; a skill name containing quotes must be escaped in the
  composed string — covered by the existing `esc`/JSON-encode conventions.
- **Adoption vs. the two-miss vanish logic (U3):** adoption and vanish both
  mutate leaves on poll; adoption must run after the miss-tracking pass and
  never adopt an agent already claimed by another leaf, or the two features
  fight. Test the ordering in the page markers and verify live.
- **Template tests are string-level:** runtime behavior (drag, tabs, adoption)
  is verified manually per unit — accepted, consistent with the graph page
  and the 2026-07-10 plan.
