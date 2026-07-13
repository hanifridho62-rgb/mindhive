---
title: "feat: Self-Reliant U1 — real autoresearch bring-up + pin execution-time discoveries"
date: 2026-07-12
type: feat
status: executed 2026-07-13 (U1-U4 complete; acceptance run 72c3c8a9 — agent-edited TOTAL_BATCH_SIZE, val_bpb 1.5858 beat the 1.8587 baseline)
depth: standard
origin: docs/plans/2026-07-12-001-feat-self-reliant-autoresearch-plan.md (U1, manual-gated; U2–U11 shipped as PR #49, merge c62cbade)
---

# feat: Self-Reliant U1 — real autoresearch bring-up + pin execution-time discoveries

## Summary

Everything around the Self-Reliant loop is live on master except the thing it drives: `karpathy/autoresearch` itself. This plan executes the origin plan's gated U1 — install autoresearch inside WSL on the RTX 3050, prove one real capped training run emits `val_bpb` on the GPU — and then pins every execution-time discovery back into the code: the real **uv-based** setup flow, the true training command, the `val_bpb` log format characterized against captured output, the small-GPU preset bounds (with `DEPTH` as the primary knob), and a watchdog-cap correction. The user has explicitly authorized the heavy install (via `/lfg` "install this"), which is the confirmation the origin gate (KTD9) required.

## Problem Frame

The dashboard's Start preflight currently refuses with "autoresearch not found at ~/autoresearch" — correct behavior, but it means zero real experiments have ever run. Three facts discovered from the real repository invalidate assumptions baked into the shipped scaffolding:

1. **autoresearch is a uv project** (`pyproject.toml` + `uv.lock`; README setup is `uv sync && uv run prepare.py && uv run train.py`). The shipped `scripts/self_reliant/bring_up.sh` assumes python venv + pip torch — it would install the wrong environment.
2. **`DEPTH` is the primary complexity knob** in `train.py`; the shipped `SMALL_GPU_PRESET` speaks in generic batch/seq-len terms and must be re-grounded in the knobs that actually exist.
3. **train.py's internal 5-minute budget excludes startup/compilation.** The shipped outer watchdog (`EXPERIMENT_CAP_S=300`) is *equal* to the inner budget, so any real run with nonzero startup/compile time would be killed as `timeout` before its inner budget completes — every healthy experiment would classify as a failure.

## Requirements

- **R1** — `bring_up.sh` performs the real uv-based install (idempotent; still manual-gated by invocation, never auto-run by any loop).
- **R2** — One real training run inside WSL completes under the watchdog, uses the GPU (`nvidia-smi` shows utilization), and emits a parseable `val_bpb`.
- **R3** — The outer watchdog cap strictly exceeds the inner training budget plus a startup/compile margin, and the relationship is documented where both are defined.
- **R4** — `parse_val_bpb` is pinned against a captured sample of real trainer output (characterization test).
- **R5** — `SMALL_GPU_PRESET` states the real knobs (`DEPTH` first) with bounds at which the 3050 does not OOM, discovered by the bring-up run(s).
- **R6** — `wsl_trainer_cmd` / `MINDHIVE_RESEARCH_TRAIN_CMD` default to the real command (`uv run train.py` in the checkout).
- **R7** — `docs/self-reliant-setup.md` records the discovered facts; its U1 checklist boxes get checked off.
- **R8** — End-to-end: dashboard Start preflight passes and one real experiment lands on the leaderboard.

## Key Technical Decisions

- **KTD1 — uv all the way down.** The bring-up installs uv inside WSL and defers every dependency decision (torch build included) to `uv sync` against the repo's committed `uv.lock`. No hand-picked torch wheel: the lockfile is upstream's tested environment. If the locked torch can't see the 3050's CUDA driver, that is a discovered fact to record and resolve (see Risks), not a reason to pre-deviate.
- **KTD2 — outer cap = inner budget + startup margin.** Keep autoresearch's inner 5-minute budget authoritative for *comparability*; the outer `run_experiment` cap exists only to guarantee the schedule against hangs. Set the production default so `outer > inner + startup/compile margin` (initial margin discovered in U2 by timing the verification run; expected shape: cap ≈ 300s budget + measured startup + safety). The 5-min-per-experiment product contract (origin R2) is unchanged — the *training* budget stays 5 minutes; the watchdog stops mislabeling healthy runs.
- **KTD3 — OOM search descends DEPTH.** Preset discovery is a short manual descent: run at upstream default; on OOM, halve `DEPTH` (and only then touch batch/seq-len) until a run completes; record the largest passing config as the preset's hard bounds. No automated sweep in this plan — the autonomous loop is the sweep engine later.
- **KTD4 — capture-then-pin.** The verification run's stdout is saved as a committed fixture snippet (the `val_bpb` line(s) plus a few surrounding lines, trimmed) so the parser pin is a real characterization test, not a guess.

## Implementation Units

### U1. Rewrite bring_up.sh for the real uv flow

- **Goal:** The gated installer matches upstream reality: uv install (idempotent), `git clone` to `$MINDHIVE_AUTORESEARCH_DIR`, `uv sync`, `uv run prepare.py`, then one `timeout`-bounded `uv run train.py` verification with `nvidia-smi` capture.
- **Requirements:** R1; enables R2.
- **Dependencies:** none.
- **Files:** `scripts/self_reliant/bring_up.sh`, `docs/self-reliant-setup.md` (setup section only).
- **Approach:** Replace the venv/pip/torch steps with: install uv if absent (official installer), clone if absent, `uv sync`, `uv run prepare.py` (skip if its outputs exist), CUDA assert via `uv run python -c "import torch; assert torch.cuda.is_available()"`, then the bounded verification run teeing output to a log file the operator (and U3) can read. Keep `set -euo pipefail`, per-step postcondition checks, and the loud "gated by design" header.
- **Test scenarios:** Test expectation: none — shell installer verified by executing it in U2; its acceptance is U2's outcomes.
- **Verification:** `bash -n` parses; a dry read shows every step idempotent; the script never runs from any MindHive code path.

### U2. Execute the bring-up inside WSL (manual, authorized)

- **Goal:** A working `~/autoresearch` on the 3050: deps synced, data prepared, and one training run that finishes under the watchdog with GPU utilization and a `val_bpb` line — plus the captured artifacts U3 pins.
- **Requirements:** R2, R5 (discovery), R3 (startup timing measurement).
- **Dependencies:** U1.
- **Files:** none in-repo (WSL side effects); produces `scripts/self_reliant/fixtures/train_output_sample.txt` (trimmed) and timing/DEPTH notes for U3.
- **Approach:** Run `bash scripts/self_reliant/bring_up.sh` inside WSL Ubuntu. Expect the H100-tuned default to OOM on 6 GB: apply KTD3's DEPTH descent by editing the WSL checkout's `train.py` knob between attempts until a run completes. Record: the passing knob values, wall-clock from launch to first training step (startup/compile margin for KTD2), the exact `val_bpb` line format, and `nvidia-smi` utilization during the run. This unit is operator-driven; each attempt is bounded by `timeout`, and OOM attempts are expected outcomes, not failures of the plan.
- **Execution note:** Heavy download (dataset + wheels). Authorized by the user's explicit "install this"; still never wired into any automated path.
- **Test scenarios:** Test expectation: none — environment execution; acceptance is the recorded artifacts and one passing run.
- **Verification:** One run prints `val_bpb`; `nvidia-smi` showed GPU utilization; sample output + timings + knob values captured for U3.

### U3. Pin the discoveries into research.py, tests, and the runbook

- **Goal:** The code reflects reality: real preset knobs, characterized parser, real trainer command, corrected cap relationship, updated runbook.
- **Requirements:** R3, R4, R5, R6, R7.
- **Dependencies:** U2 (its artifacts).
- **Files:** `src/mindhive/research.py`, `tests/test_research.py`, `scripts/self_reliant/fixtures/train_output_sample.txt`, `docs/self-reliant-setup.md`.
- **Approach:** (a) `SMALL_GPU_PRESET` rewritten with `DEPTH` as the lead knob and the discovered passing bounds, phrased as hard constraints for `compose_program`. (b) `MINDHIVE_RESEARCH_TRAIN_CMD` default becomes `cd $DIR && uv run train.py`. (c) `EXPERIMENT_CAP_S` production default raised per KTD2 (inner budget + measured margin), with the relationship stated in the constant's comment; the env override stays. (d) Characterization test: `parse_val_bpb` against the committed fixture returns the known value. (e) Runbook: uv flow, discovered knobs, cap relationship, and check the three U1 discovery boxes.
- **Execution note:** Characterization-first for the parser — pin against the fixture before touching the regex; only generalize the regex if the fixture demands it.
- **Test scenarios:**
  - Happy path: fixture sample → `parse_val_bpb` returns the captured run's exact final value.
  - Edge: existing synthetic-format tests still pass unchanged (the mock trainer's line stays valid).
  - Happy path: composed program text contains the new DEPTH bound verbatim.
  - Edge: `wsl_trainer_cmd()` honors `MINDHIVE_RESEARCH_TRAIN_CMD` override and defaults to the uv command otherwise.
  - Edge: production cap default exceeds 300s inner budget (assert the constant relationship so a future edit can't silently re-break KTD2).
- **Verification:** Full research test suite green; the new tests fail against pre-change code (red first where applicable).

### U4. End-to-end acceptance on the live dashboard

- **Goal:** The user-visible success criterion: Start preflight passes and one real experiment appears on the leaderboard with a real score.
- **Requirements:** R8.
- **Dependencies:** U2, U3 (merged/served code — dashboard restart handoff applies).
- **Files:** none (verification).
- **Approach:** With the branch served (worktree dashboard on a test port, per the established verification pattern — never disturb 54461), open Self-Reliant, Start with `max_experiments=1` and a minimal goal, watch the researcher edit-turn → real training → leaderboard row. A real `val_bpb` on the board closes the origin plan's last open unit. Record the result as an `experiment_result`/`decision` in the ledger (the loop does this itself; confirm it did).
- **Test scenarios:** Test expectation: none — live acceptance run; the behavior it exercises is covered by the shipped route/controller suites.
- **Verification:** Preflight green; leaderboard shows one scored real experiment; ledger carries its events.

---

## Scope Boundaries

**In scope:** the uv bring-up, one-GPU verification, and pinning (preset, parser fixture, trainer command, cap default, runbook).

### Deferred to Follow-Up Work

- Overnight unbounded runs and skill-guidance experiments (product usage, not bring-up).
- Automated preset sweeps or OOM-adaptive DEPTH tuning inside the loop.
- Torch/CUDA driver remediation beyond recording the fix, if `uv sync`'s locked torch can't reach the 3050 (resolve minimally, document exactly).

## Assumptions

- The user's `/lfg` "install this" is the explicit confirmation KTD9 (origin) required for the heavy install; no further gate is needed for U2.
- WSL Ubuntu has working NVIDIA passthrough (preflight already probed `nvidia-smi` successfully) and enough disk for dataset + wheels.
- The origin plan's iteration contract and controller need no changes — this plan only corrects constants, commands, and docs around them.

## Risks & Dependencies

- **Locked torch vs. 3050 driver.** `uv.lock` targets upstream's H100 box; the wheel may mismatch the local CUDA driver. Mitigation: record the failure verbatim, apply the minimal override (uv's torch index pin), document it in the runbook as a deviation-from-lockfile.
- **6 GB OOM at default DEPTH.** Expected; KTD3's descent bounds the search. Worst case (no config completes) the plan stops after U2 with findings recorded — U3's preset then encodes "known-not-to-fit" guidance instead.
- **Dataset download size/time.** One-time; `prepare.py` outputs are checked before re-running.
- **Startup/compile margin varies.** The measured margin from one run may understate cold-start worst cases; the cap default adds safety headroom and stays env-overridable.

## Sources & Research

- `github.com/karpathy/autoresearch` README (fetched this session): root layout `prepare.py`/`train.py`/`program.md`/`pyproject.toml`/`uv.lock`; setup `uv sync && uv run prepare.py && uv run train.py`; single NVIDIA GPU, Python 3.10+; inner 5-minute budget *excluding startup/compilation*; `val_bpb` as the vocab-independent metric; `DEPTH` as the primary complexity knob.
- Origin plan + shipped implementation: `src/mindhive/research.py` (`EXPERIMENT_CAP_S`, `SMALL_GPU_PRESET`, `wsl_trainer_cmd`, `default_preflight`), `scripts/self_reliant/bring_up.sh`, `docs/self-reliant-setup.md`, `tests/test_research.py`.
- Live probe earlier this session: WSL reachable, GPU visible to preflight, auth healthy — only the autoresearch checkout is missing.
