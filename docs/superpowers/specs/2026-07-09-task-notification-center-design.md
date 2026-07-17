# Sessions notification center — design

Date: 2026-07-09
Status: approved (user, 2026-07-09)

## Goal

Turn the dashboard **Sessions** page into a permanent, auditable notification
center. Every session (completed or ongoing) renders as a collapsible
notification card mirroring Claude Code's own task-notification UI:

- **L1 (collapsed):** status badge + one-line summary chip —
  `● ongoing · edited 8 files (+120 −44) · ran 3 commands · 1 note`
- **L2 (card expanded):** the ordered activity rows —
  `[caveat] plan FIRST, then work…`, `touched sync.py +25 −14`,
  `ran full test suite`, `✓ iter 3 · fixed breaker reset`
- **L3 (row expanded):** granular detail — the unified diff with +/− line
  coloring for edits, full command + captured output for commands, the loop
  iteration's progress/breaker/token/cost table, full memo text for notes.

## Locked decisions (user)

1. **Plan shape:** one implementation plan of five tasks containing the
   complete code — data tracking → backend module → new API → UI cards →
   tests/docs. (`docs/superpowers/plans/2026-07-09-task-notification-center.md`)
2. **Zero-change filter:** a file event is only logged when lines actually
   changed. When the computed stats are `+0 −0`, the event is skipped at
   capture time. When stats cannot be derived (old events, exotic payloads)
   the event is still logged — unknown is not zero.
3. **History cap:** the detailed row list (L2/L3) is capped to the **last 100
   entries** per session. The ledger itself keeps everything; the cap bounds
   the API payload and UI, and the card says how many older entries exist.
4. **No delete:** the Sessions page deliberately has **no delete button**.
   The history is an auditable track record; the UI states this in a legend
   line. (The ledger is append-only anyway — this locks the UI affordance.)

## Concept mapping

- **Task = session episode.** A session whose newest event is younger than
  `ACTIVE_WINDOW_SECS` (600 s) renders **ongoing** (pulsing amber); otherwise
  **completed** (green check). No dependency on `session_end`, which nothing
  currently emits.
- **Loop iterations are first-class rows.** `loop_iteration` events already
  carry `summary`, `progress`, `breaker_state`, `cost_usd`, `total_tokens`,
  `context_fill_ratio` (`loop/engine.py:88-97`) but had no UI. Row title =
  summary; glyph = `✓` progressed / `○` no progress / `⚠` breaker OPEN;
  click → full detail table. This closes the "data exists, UI missing" gap.

## Data flow (hybrid: enrich + lazy fetch)

**Capture enrichment (cheap, stored in the ledger).** Both capture paths —
the live PostToolUse hook (`hookcapture.py`) and transcript back-harvest
(`harvest.py`) — gain identical enrichment via a shared `line_stats()` helper:

- Edit/Write/NotebookEdit: `plus`/`minus` line counts. Exact counts come from
  the tool response's `structuredPatch` when present (live hook only);
  fallback is `new_string`/`old_string` line counts, then Write `content`
  line count. `+0 −0` → event skipped (locked rule 2).
- Bash/PowerShell: additionally store the tool call's `description` field
  (`desc`, ≤120 chars) — this is what gives human labels like *"Ran full test
  suite"* instead of raw command text.
- **Dedup invariant:** the payload `text` field stays byte-identical to
  today's values (path / command[:180]) because harvest's dedup key is
  `(session_id, type, text)`. New fields ride alongside; old events without
  them degrade gracefully.

**Lazy granular detail (heavy, fetched on expand).** A new endpoint parses the
session's Claude Code transcript (`~/.claude/projects/*/<session_id>.jsonl`),
pairs `tool_use` blocks with their `tool_result` by id, and returns capped
details (diff ≤400 lines, output ≤10 000 chars, list capped to the last 100).
This works retroactively for any past session whose transcript survives; when
it's gone the row shows an inline "detail unavailable" notice. The session id
is validated as a strict token before becoming a filename (path-traversal
guard).

## Components

| Unit | Responsibility |
|---|---|
| `taskview.py` (new) | All backend assembly: `status_of`, `tally` (L1), `items` (L2, 100-cap), `detail` (L3 transcript parse), plus the cards' CSS/JS fragments. |
| `hookcapture.py` | `line_stats()` shared helper; enriched `extract_events` payloads; zero-change skip. |
| `harvest.py` | Same enrichment for back-harvest; `_event_for_item` passes extra keys through. |
| `dashboard.py` | Thin wiring: `/api/task`, `/api/task_detail` routes; `/api/sessions` gains `activity` + `status`; injects the fragments into the page. Stays under the 1000-line cap. |
| `dashboard_page.py` | Gains `__TASKS_CSS__`/`__TASKS_JS__` placeholders; `renderSessions` delegates to the injected `renderTaskCards`. Stays sibling-free. |

## Live-refresh safety

The panel silently re-renders every 2.5 s. All interaction state — open
cards, open rows, fetched L2/L3 data — lives in module-level JS
(`TASK_OPEN`, `TASK_ROW_OPEN`, `TASK_ITEMS`, `TASK_DETAIL`), applying the
recorded dashboard lesson. Ongoing sessions refetch their rows each tick;
completed ones keep their cache. "Click a session → scope Memory" survives as
an explicit *open in Memory →* link inside the expanded card.

## Error handling

- Transcript missing/unreadable → `{"error": "transcript_unavailable"}` with
  HTTP 200; per-row inline notice, never an error banner.
- Malformed transcript JSON lines are skipped (same tolerance as harvest).
- Invalid session id → no transcript lookup (guard returns None).
- HIVE MIND (`project=*`): each card carries its own project; `/api/task`
  requires `session_project` in that mode and 400s without it.

## Considered and rejected

- Capturing `Read` events — would flood the append-only store; reads appear
  in L3 transcript detail anyway.
- Storing full diffs/output in the ledger — unbounded growth.
- A separate "Tasks" nav view — Sessions *is* the task list.
- A delete/prune affordance — rejected by design (locked rule 4).

## Follow-up work (separate documents)

- **Skill-effectiveness ranking** — activate the dormant green/red pipeline
  and rank by measured effectiveness, not breadth:
  `docs/superpowers/plans/2026-07-09-skill-effectiveness-ranking.md`
- **Parallel multi-agent orchestration** — design brief only; needs its own
  brainstorm → spec → plan cycle:
  `docs/superpowers/specs/2026-07-09-parallel-orchestration-brief.md`

## Testing

Unit: `line_stats` (patch / fallback / unknown), zero-change skip, harvest
parity + dedup-key stability, `status_of` boundary, `tally` sums, `items`
100-cap + truncated count, transcript detail parsing (fixture jsonl with
tool_use/tool_result pairing), traversal-id rejection, missing-transcript
degradation. Endpoint: `/api/sessions` new fields, `/api/task`,
`/api/task_detail` via the existing `dashboard_server` fixture. Full suite
stays green.
