# Recommendation Matrix

**Phase S (Skill Selection)** of the orchestrator produces one matrix covering the
shortlisted **candidate skills**. The matrix makes skill selection explicit and
auditable instead of a hidden guess — and it is built from **measured MindHive
telemetry** (`skills stats --json`, `skills recommend --json`), not
model-estimated percentages; an `estimated` label appears only for a
never-called skill.

## Sourcing candidates (hard rules)

- **Pull the measured data first — mandatory, before scoring anything:**
  - `python -m mindhive skills stats --json` — per-skill calls, ok/fail,
    completion rate, last outcome, streak.
  - `python -m mindhive skills recommend --json` — the deterministic ranking
    (scored in `src/mindhive/recommend.py`), with `measured|estimated`
    provenance on every entry and measured coverage (`N of M skills measured`).
- **Candidates come from the session's available-skills list, verbatim.** That
  list is the only authoritative runtime registry of what is installed. Never
  present a skill name that is not in it — no remembered names, no guesses, no
  "there's probably a skill for X."
- Before scoring a candidate, run `python -m mindhive recall "<skill name>"`
  (add `--semantic` if literal recall is thin). Prior successes, caveats, or
  failures with that skill directly inform its scores below.
- Shortlist 2–4 genuinely plausible candidates. Do not pad the matrix with
  skills you would never pick just to fill rows.

## The seven parameters

Score each candidate on all seven. Use the anchored scales so scores mean the
same thing across runs.

| Parameter | Scale / anchors |
|-----------|-----------------|
| **Suitability** | How well the skill matches *this* task. `High` (built for exactly this) / `Medium` (adjacent, workable) / `Low` (possible but a stretch). |
| **Compatibility** | Fit with the existing stack and any other skills in the chain. `High` (native fit) / `Medium` (minor glue) / `Low` (friction, conversions, or conflicts). |
| **Completion rate** | The **measured** completion rate from `skills stats --json` (ran without a tool error — never call it "success rate"), with call count, e.g. `4/5 (measured)`. For a **never-called** skill only: a model estimate, always labeled `estimated`. `skills record` self-grades carry `agent-assessed` provenance — usable context, never presented as measured task effectiveness. |
| **Risk level** | Worst-case blast radius. `None` (read-only/advisory) / `Local` (one file/area) / `Repo-wide` (many files, refactor) / `Destructive` (data, migrations, external side-effects). |
| **Execution speed** | Expected wall-clock/efficiency. `Fast` / `Moderate` / `Slow`. |
| **Resource cost** | Token / computational weight. `Light` / `Moderate` / `Heavy`. |
| **Final verdict** | The definitive call: `Use` / `Use as follow-up` / `Hold` / `Skip`, plus one clause of why. |

## Overlap tie-break (when two skills fit the same need)

When two or more candidates overlap for one problem, emit **one**
recommendation and pick the winner by the deterministic chain implemented in
`src/mindhive/recommend.py`:

1. **Measured completion rate** — a measured skill always beats an unmeasured
   one; between measured skills, the higher rate wins.
2. **Description clarity** — an explicit "use when" trigger beats none; still
   tied, the shorter (less ambiguous) description wins.
3. **Failure-free completion streak** — the longer trailing run of clean calls.

List the losers as alternatives in the verdict clause. `skills recommend
--json` already applies this chain (`alternatives` on each entry) — do not
override its winner without saying why.

## Rendering

Render as a compact table when comparing multiple candidates (scans fastest):

```markdown
| Skill | Suitability | Compatibility | Completion rate | Risk | Speed | Cost | Verdict |
|-------|-------------|---------------|-----------------|------|-------|------|---------|
| ce-plan | High | High | 5/5 (measured) | None | Moderate | Moderate | **Use** — built for structured planning |
| ce-work | High | High | 3/4 (measured) | Local | Moderate | Heavy | **Use** — executes the plan |
| ce-code-review | High | High | 80% (estimated — never called) | None | Fast | Moderate | **Use as follow-up** — verify before ship |
```

Then state the recommended selection in one or two sentences and ask the user to
confirm or revise before Phase X execution.

## Honesty guardrails

- Every figure carries its provenance: `measured`, `estimated`, or
  `agent-assessed`. An `estimated` figure is legitimate **only** for a skill
  with zero recorded calls — a called skill always shows its measured record,
  however thin ("1/1, too few calls to trust" beats a made-up percentage).
- Never launder an estimate or an agent-assessed self-grade into a measured
  rate — the completion rate measures "ran without a tool error", not task
  quality.
- If the ledger shows a past failure with a candidate, reflect it in the score
  and say so in the verdict clause — do not bury known history.
- If no candidate scores well, say the idea may need `/ce-brainstorm` or
  `/ce-plan` first rather than forcing a weak recommendation.
