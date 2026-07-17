---
title: "feat: Workspace (MindParallel & MindSwarm) gap closure"
type: feat
date: 2026-07-10
status: ready
origin: user request (LFG pipeline); implementation baseline merged in PR #41
---

# feat: Workspace (MindParallel & MindSwarm) gap closure

## Summary

The Workspace feature — MindParallel (16 split-any-direction agent panes with skill drag-drop and a dispatching kanban), MindSwarm (mission tree + command bar), multi-account profiles, full ledger integration, and the Obsidian-style graph redesign — was implemented and merged to master in PR #41. This plan closes the verified gaps between the merged implementation and the original request, and pins every requirement with page-level tests so regressions surface in CI.

Verified gaps to close:

1. **Command-bar `@`-mention targeting** (MindSwarm): the on-screen hint promises "@ to target an agent", but typed `@Name` prefixes are not parsed — only the dropdown selects a target.
2. **UI consistency**: the request says "the UI should be the same as other sections in Mind Hive," but both Workspace pages use their own color palettes (MindParallel blue-tinted `#11131a`, MindSwarm green-tinted `#0b0f0c`) instead of the dashboard/graph shared GitHub-dark tokens (`#0e1116` background, `#161b22` panels, `#30363d` borders, `#58a6ff` accent).
3. **Requirement pinning**: the two page modules have no dedicated tests; every shipped requirement (16-pane cap, split directions, drag payloads, kanban columns, mission roles, command targets, offline self-containment) should be asserted the way `tests/test_graphview.py` pins the graph page.

---

## Problem Frame

PR #41 delivered the feature's substance. What remains is contract polish: a promised interaction that doesn't work as hinted (`@`-mentions), a stated visual requirement not fully honored (shared palette), and zero test coverage on the two largest new UI modules. Left unfixed, the hint actively misleads users, the Workspace looks like a third-party bolt-on, and future edits to the page templates can silently break shipped behavior.

---

## Requirements

Traced to the original request:

- **R1** — Workspace section containing MindParallel and MindSwarm, reachable from the dashboard nav. *(shipped — pin with tests)*
- **R2** — Runs real Claude Code agents; multiple accounts (profiles → `CLAUDE_CONFIG_DIR`) in parallel. *(shipped — pin with tests)*
- **R3** — All agent input/output feeds the Mind Hive database (ledger events). *(shipped in backend, already tested in `tests/test_workspace.py`)*
- **R4** — UI consistent with other Mind Hive sections. **(gap — U1)**
- **R5** — Up to 16 panes, splittable in any direction, all visible at once. *(shipped — pin with tests)*
- **R6** — Drag a skill onto a pane; that agent executes it. *(shipped — pin with tests)*
- **R7** — Kanban whose cards dispatch agents. *(shipped — pin with tests)*
- **R8** — MindSwarm mission spawns coordinator/builders/scouts/reviewers on a live mission tree. *(shipped — pin with tests)*
- **R9** — Command bar directs one agent or all; `@` targets a specific agent. **(gap — U2)**
- **R10** — Agents read/write the shared memory graph. *(shipped via `hive recall`/`hive append` CLI — see Scope Boundaries for the MCP-transport note)*
- **R11** — Graph display restyled (Obsidian-style). *(shipped in PR #41)*

---

## Key Technical Decisions

- **Palette alignment by token substitution, not redesign (U1).** Both pages keep their layouts and role colors; only the base surface tokens (background, panel, border, text, accent) move to the dashboard's shared values. The graph view is the precedent: it shares the base palette while keeping view-specific accent colors. Rationale: honors R4 with minimal diff and zero behavioral risk.
- **`@`-mention parsing stays client-side (U2).** The command input already posts `{target, text}` to `/api/workspace/command`; parsing a leading `@Name ` prefix into the matching agent id (case-insensitive name match, falling back to the dropdown selection when no match) is pure page JS. The backend `Manager.command` already accepts an agent id or `"all"` — no server change.
- **Page tests are string assertions on rendered HTML** (U3), mirroring `tests/test_graphview.py`. The pages are pure functions (`render_parallel(token)`, `render_swarm(token)`); asserting on the returned string pins tokens, caps, payload kinds, and offline self-containment without a browser.

---

## Implementation Units

### U1. Align Workspace pages with the shared dashboard palette

**Goal:** Both Workspace pages read as native Mind Hive sections (R4).

**Requirements:** R4

**Dependencies:** none

**Files:**
- `src/mindhive/workspace_page.py` (modify)
- `src/mindhive/swarm_page.py` (modify)
- `tests/test_workspace_pages.py` (create — shared with U3)

**Approach:** Replace the base surface tokens in each page's CSS with the dashboard/graph values: page background `#0e1116`, panel/header surfaces `#161b22`, borders `#30363d`, primary text `#e6edf3`, secondary text `#8b949e`, accent `#58a6ff`. Keep per-view identity colors (role colors in MindSwarm, status dots, kanban chrome) untouched. MindSwarm's dotted-stage background keeps its pattern but on the shared background color.

**Patterns to follow:** `src/mindhive/graphview.py` CSS (the section that already shares the dashboard palette); `src/mindhive/dashboard_page.py` styles.

**Test scenarios:**
- `render_parallel(token)` output contains the shared background `#0e1116` and border `#30363d`, and no longer contains the old base tokens (`#11131a`, `#2a2f3d` as border).
- `render_swarm(token)` output contains `#0e1116` and `#30363d`, and no longer contains the old green base (`#0b0f0c`).
- Role colors (`coordinator` orange, `builder` green) still present in the swarm page after the swap.

**Verification:** Rendered pages visually match the dashboard chrome; test assertions pass.

### U2. Command-bar `@`-mention targeting in MindSwarm

**Goal:** Typing `@Name <text>` in the command bar sends `<text>` to that agent, honoring the on-screen hint (R9).

**Requirements:** R9

**Dependencies:** none

**Files:**
- `src/mindhive/swarm_page.py` (modify)
- `tests/test_workspace_pages.py` (create/extend — shared with U3)

**Approach:** In `sendCmd()`, before posting: if the text starts with `@`, take the longest case-insensitive prefix match against current agent names (agent names can contain spaces, e.g. "Builder 1"); on a match, use that agent's id as `target` and strip the mention from the text; `@all` maps to `"all"`. No match → keep the dropdown's target and send the text unchanged (don't guess). Update the dropdown selection to reflect the resolved target so the UI shows where the command went.

**Test scenarios (string-level, on the rendered page JS):**
- The page JS contains the mention-parsing logic keyed off a leading `@` (assert on a distinctive function or marker, e.g. `resolveMention`).
- The command post still targets `/api/workspace/command` with `{target, text}` keys.

Backend behavior (id vs `all` targeting) is already covered by `tests/test_workspace.py::test_command_targets_one_agent_or_whole_swarm`.

**Verification:** Typing `@Builder 1 status` in a live swarm sends only to Builder 1; `@all wrap up` fans out; unknown mention falls back to the dropdown target.

### U3. Requirement-pinning tests for both Workspace pages

**Goal:** Every shipped Workspace requirement is asserted in CI so template edits can't silently drop them (R1, R2, R5–R8).

**Requirements:** R1, R2, R5, R6, R7, R8

**Dependencies:** U1, U2 (assertions target the post-change templates)

**Files:**
- `tests/test_workspace_pages.py` (create)

**Approach:** One test module in the style of `tests/test_graphview.py`: call `render_parallel("tok")` / `render_swarm("tok")` and assert on the returned HTML strings.

**Test scenarios:**
- Both pages embed the token as JSON and are offline-self-contained (no `http://`/`https://` references).
- MindParallel: 16-pane cap present (`>= 16` guard string); split directions `'h'`/`'v'` both wired; skill drag payload `kind:'skill'` and task payload `kind:'task'` present; kanban columns backlog/running/done; profiles endpoints (`profile_add`, `profile_remove`) referenced; skills loaded from `/api/plugins/inventory`.
- MindSwarm: all four roles (coordinator, builder, scout, reviewer) styled; mission launch posts to `/api/workspace/mission`; command bar posts to `/api/workspace/command`; `@all` option present; inspector polls `/api/workspace/output`.
- Token with HTML-special characters (`</script>`) renders without breaking out of the script context (JSON-escaped).

**Verification:** New module passes locally and in CI on all four matrix jobs.

---

## Scope Boundaries

### Deferred to Follow-Up Work

- **MCP transport for agent memory access (R10).** The request says agents read/write the graph "over MCP." The shipped mechanism achieves the same outcome — every swarm agent's system prompt directs it through `hive recall` / `hive append`, and all workspace I/O is ledgered — but the transport is the CLI, not an MCP server. Exposing Mind Hive as an MCP server (recall/append/graph tools over stdio) is a real, separable feature with its own design surface (protocol handshake, tool schemas, auth). Deferred rather than rushed here.
- **Kanban drag-to-"running" semantics.** Dragging a card into the Running column today only changes its status; it does not dispatch an agent (the dispatch button and drop-on-pane do). Either auto-dispatch on that drop or block it — small UX decision, not blocking.

### Out of Scope

- Any change to the workspace backend (`src/mindhive/workspace.py`) — its behavior is complete and covered by 31 tests.
- Graph view changes — R11 shipped in PR #41.
- True terminal emulation (PTY) in panes — the stream-json line view is the accepted design.

---

## Risks & Dependencies

- **Palette swap regressions**: pure-CSS change; risk contained by U1's negative assertions (old tokens absent) and visual check.
- **Name-collision in `@`-mentions**: two agents sharing a name prefix ("Builder 1", "Builder 10") — longest-match rule resolves it; documented in U2.
- The pages are inline-JS templates; tests are string-level and can't execute the JS. Runtime behavior is verified manually (U2 verification note) — accepted, consistent with how the graph page is tested.
