# ADR 0008 — Complete the Pensyve→MindHive cutover by disabling the Pensyve plugin

Date: 2026-07-05
Status: accepted — step 2 applied; **step 1 pending manual user action** (the
agent's permission classifier correctly blocked automated editing of
`settings.json` as self-modification; apply the one-key change by hand)

## Context

Pensyve ran as a safety net alongside MindHive during the staged memory cutover
(see the user memory index note of 2026-06-21). Measured cost of keeping it on:
its `hooks.json` `cat`s full markdown specs into context on **every**
UserPromptSubmit and SessionStart — tens of thousands of tokens per session —
because the hook payload *is* the spec text; `prompt_enrichment: false` only
changes what the agent does after those tokens are already spent. MindHive has
since booted cleanly across real sessions, satisfying the cutover condition.

## Decision

1. `C:\Users\hanif\.claude\settings.json` → `enabledPlugins`:
   `"pensyve@claude-code-workflows": false` (single-key edit; the same file
   carries the MindHive hooks and was not otherwise modified).
2. `pensyve-plugin.local.md` (plugin cache dir) → `prompt_enrichment: false`,
   `context_loading: "off"` — belt-and-braces so a future re-enable does not
   resume per-turn spec flooding.

The plugin is **not** uninstalled and its memory store is untouched.

## Rollback

Set `"pensyve@claude-code-workflows": true` in `enabledPlugins`. One boolean.

## Consequences

- New sessions no longer receive Pensyve SessionStart/UserPromptSubmit/Stop
  spec injections; the measured per-turn overhead drops to zero.
- Pensyve MCP memories remain on disk as the documented rollback safety net.
- MindHive is now the sole session-memory layer (hooks in `hooks/*.ps1`).
