---
name: thermo-nuclear-code-quality-review
description: >-
  Run an extremely strict maintainability review for abstraction quality, giant
  files, and spaghetti-condition growth. Use when a "Pending thermo-nuclear
  review" notice appears in STATE.md at session start, when the user asks for a
  deep/strict/maintainability/"thermo-nuclear" review, or before merging a large
  change. Hunts for "code judo" restructurings that delete whole branches rather
  than just tidying them.
---

# Thermo-nuclear code quality review

Adapted for mindhive from Cursor's `thermo-nuclear-code-quality-review`. This is a
**post-hoc, advisory** review — it cannot block a merge (the code is already
written), so it produces ranked findings you choose to act on, with strict
standards. Presumptive-blocker findings are written back into hive memory so they
compound across sessions.

## When this runs automatically

`session_end.ps1` queues the source files touched during a coding session
(`hive review queue`) into a sidecar marker, and `projector` renders a
**"⚠ Pending thermo-nuclear review"** block into `STATE.md`. The next session's
`session_start` injects that block. When you see it:

1. Run `hive review show` to read the marker (files + batches).
2. Review **batch by batch, sequentially** — the marker is pre-split into
   ≤1,500-line batches; never review more than one batch's worth at once. Stop
   early if the user has seen enough.
3. **Re-read each file live** before judging it — do NOT trust the queued
   snapshot; the code may have changed since session end.
4. For each presumptive-blocker finding, record it:
   `hive append caveat "<file>: <one-line finding>" --tags review,thermo-nuclear`
   Lower-severity findings: report them in-session only, don't flood the timeline.
5. When done (or stopped), clear the marker: `hive review clear`.

Scope is already filtered to `src/mindhive/**/*.py` and `tests/**/*.py`;
generated artifacts, docs, and the memory store are excluded.

## The seven non-negotiable standards

1. **Structural simplification** — seek changes where entire branches, helpers,
   conditionals, or layers *vanish*, not just get cleaner.
2. **File-size threshold** — flag any file pushed past **1,000 lines** without a
   compelling structural justification; prefer extraction.
3. **Spaghetti prevention** — reject ad-hoc conditionals and scattered special
   cases bolted onto unrelated flows; demand a dedicated abstraction instead.
4. **Design over acceptance** — prefer a cleaner structure even when the existing
   code technically works.
5. **Direct over magic** — challenge brittle, ad-hoc, or overly generic
   mechanisms; favour explicit, boring implementations.
6. **Type cleanliness** — question unnecessary optionality, casts, `any`/`object`,
   and untyped boundaries where a clearer contract exists.
7. **Canonical layers** — push logic toward its owning module; flag leakage
   across boundaries and duplicated utilities.

## Aggressive flagging triggers

- Complex implementations where a reframing deletes an entire category of
  complexity ("code judo").
- Files crossing 1,000 lines.
- Conditionals bolted onto unrelated paths.
- Feature-specific logic bleeding into shared modules.
- Generic "magic" obscuring a simple structure.
- Unnecessary casts, wrappers, or optional parameters.
- Copy-pasted logic instead of an extracted helper.
- Sequential async/IO work that could cleanly parallelise.

## Preferred remedies

Delete indirection layers · reframe the state model so conditionals disappear ·
extract focused modules or pure functions · replace condition chains with typed
models · move logic to the owning layer · parallelise independent work when it is
*simpler*, not just faster.

## Output format

For each batch:

```
### Batch N/M — <files>
- 🔴 BLOCKER  <file:line> — <finding>. Remedy: <code-judo restructuring>.
- 🟡 SHOULD   <file:line> — <finding>. Remedy: <…>.
- 💡 CONSIDER <file:line> — <finding>.
Verdict: <clean | findings to act on>
```

Only 🔴 BLOCKER findings get written back as `caveat` events. Keep the tone
direct and serious about quality without being unkind — frame maintainability
issues clearly, not as mild suggestions.
