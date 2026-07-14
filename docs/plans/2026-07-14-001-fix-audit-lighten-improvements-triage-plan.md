# Audit fixes + lighter pipeline + Improvements triage marks

Date: 2026-07-14 · Branch: claude/audit-lighten-improvements-marking

## Findings (audit against ledger intent)

1. **O(signatures × ledger) rescans** — `incidents.project()` takes ~50s and
   `incidents.audit()` ~19s on the live store (6.4k events, 73 signatures),
   because `_status_and_flag`/`leaks`/`_has_lifecycle`/`_latest_expiry_ts`
   each re-read the whole ledger per signature. Both run inside
   `harvest.catch_up` (the dashboard's 3s auto-harvest loop fires it on every
   transcript change) and inside every dismiss/reflect/resolve request — the
   dismiss button blocks ~50s per click. Contradicts the evs-reuse intent the
   helpers already carry (`evs=` params exist but are never threaded).
2. **The learning loop eats its own output** — PreToolUse enforcement denials
   (enforce.py reasons) come back as `is_error` tool_results and are
   re-captured as new `raw:*` incidents (e.g. `raw:bc40af13ca` = the
   grep-unscoped deny text). Permission-gate prompts ("Claude requested
   permissions…") are captured too. 50 opaque `raw:*` records flood the
   Improvements view.
3. **Reflection health misdiagnosis** — the live failure is CLI quota
   (`api_error_status: 429`, "weekly limit"), but `result_text()` discards
   is_error envelopes so the marker records only "empty verdict" and the
   banner says "check auth".

## Changes

- **P1 lighter incidents**: build a one-pass context (events list, failures
  by signature, lifecycle index) in `project()`/`audit()` and thread it
  through `_status_and_flag`/`leaks`/`reroute_count`/helpers. Behavior
  identical; target <1s for a full projection rebuild.
- **P2 capture filters**: mark every enforcement deny reason with a stable
  `[mindhive-rule]` prefix at the `hook_decision` choke point and treat it as
  non-failure in `failures.py`; classify "requested permissions" gate texts
  as infra noise.
- **P3 honest reflect errors**: `claude_cli.error_text()` surfaces the
  envelope's error (status + message); `_claude_runner` raises it;
  `reflect_incident` records it, so STATE.md shows "429 … weekly limit"
  instead of "empty verdict".
- **P4 Improvements triage marks (feature)**: deterministic
  `incidents.classify_triage(resolution, rule_source)` →
  `for Claude` (unresolved: no rule yet, or rule leaking — pending agent
  work) vs `dismissible` (watching/resolved — no agent work; safe to clear
  manually). Surfaced as a badge on every lesson card, a legend quick-filter,
  and a copy-prompt button on for-Claude cards; parity in
  `hive rules summaries --json`.
- **P5 gzip**: compress dashboard HTML/JSON responses when the client
  accepts it (72KB page → ~22KB).

## Verification

Full pytest suite green + timed re-run of `incidents.project()` on the live
store + dashboard smoke on a scratch port.
