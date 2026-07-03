# Recommendation Matrix

Phase 2 of the orchestrator produces one matrix **per candidate skill**. The
matrix makes skill selection explicit and auditable instead of a hidden guess.

## Sourcing candidates (hard rules)

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
| **Probability of success** | Chance of completing without a fatal error — a **model estimate**, expressed as a % and explicitly labeled `(est.)`. Anchor on ledger history for this skill when available. |
| **Risk level** | Worst-case blast radius. `None` (read-only/advisory) / `Local` (one file/area) / `Repo-wide` (many files, refactor) / `Destructive` (data, migrations, external side-effects). |
| **Execution speed** | Expected wall-clock/efficiency. `Fast` / `Moderate` / `Slow`. |
| **Resource cost** | Token / computational weight. `Light` / `Moderate` / `Heavy`. |
| **Final verdict** | The definitive call: `Use` / `Use as follow-up` / `Hold` / `Skip`, plus one clause of why. |

## Rendering

Render as a compact table when comparing multiple candidates (scans fastest):

```markdown
| Skill | Suitability | Compatibility | Success (est.) | Risk | Speed | Cost | Verdict |
|-------|-------------|---------------|----------------|------|-------|------|---------|
| ce-plan | High | High | 90% (est.) | None | Moderate | Moderate | **Use** — built for structured planning |
| ce-work | High | High | 85% (est.) | Local | Moderate | Heavy | **Use** — executes the plan |
| ce-code-review | High | High | 88% (est.) | None | Fast | Moderate | **Use as follow-up** — verify before ship |
```

Then state the recommended selection in one or two sentences and ask the user to
confirm or revise before Phase 3 execution.

## Honesty guardrails

- Every percentage carries `(est.)`. Never present an estimate as a measured
  success rate — there is no per-skill telemetry backend yet.
- If the ledger shows a past failure with a candidate, reflect it in the score
  and say so in the verdict clause — do not bury known history.
- If no candidate scores well, say the idea may need `/ce-brainstorm` or
  `/ce-plan` first rather than forcing a weak recommendation.
