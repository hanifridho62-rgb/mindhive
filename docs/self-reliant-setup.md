# Self-Reliant — bring-up & preset runbook

Self-Reliant wraps [karpathy/autoresearch](https://github.com/karpathy/autoresearch)
as an autonomous experiment loop inside the MindHive dashboard. A researcher
agent edits `train.py`; deterministic Python runs each training capped at
**5 minutes wall-clock**, parses `val_bpb` (lower is better), keeps a
leaderboard, and writes every result back into MindHive memory.

## One-time bring-up (manual, gated)

The training environment lives **inside WSL2 Ubuntu**, in a sibling directory
outside this repo (default `~/autoresearch`, override with
`MINDHIVE_AUTORESEARCH_DIR`). The install downloads gigabytes — it never runs
automatically:

```bash
# inside WSL Ubuntu
bash scripts/self_reliant/bring_up.sh
```

The script is idempotent: uv install → clone → `uv sync` (locked deps, CUDA
torch included) → `uv run prepare.py` (dataset + tokenizer, one-time) → one
bounded `uv run train.py` verification. Success = a `val_bpb` line in the
output and GPU utilization in `nvidia-smi`.

autoresearch is a **uv project** (`pyproject.toml` + `uv.lock`) — never install
deps by hand; `uv sync` reproduces upstream's tested environment exactly.

**Cap vs. budget:** `train.py`'s internal 5-minute budget *excludes*
startup/compilation — measured 432 s of startup/compile on this machine, and it
recurs on every `train.py` edit. MindHive's outer watchdog default is therefore
`MINDHIVE_RESEARCH_CAP_S=900`; the outer cap only guards against hangs, while
the inner budget is what keeps experiments comparable. Real throughput is
~4–5 experiments/hour on this laptop (12/hour assumed H100-class startup).

## Preflight

The dashboard's Start button refuses to launch a run until preflight passes:

1. WSL reachable (`wsl bash -lc true`)
2. `$MINDHIVE_AUTORESEARCH_DIR/train.py` exists
3. a GPU is visible inside WSL (`nvidia-smi -L`)
4. the researcher profile's Claude auth is healthy

Each failure names its fix; the bring-up script clears 1–3.

## Small-GPU preset (RTX 3050 Laptop, 6 GB VRAM)

The 5-minute cap makes experiments comparable *on this machine*; "best model"
means the best `val_bpb` a 3050 reaches in 5 minutes. OOM above the bounds is
an expected, logged failure — the loop continues (R10).

Current bounds live in `research.SMALL_GPU_PRESET` and are rendered verbatim
into the researcher's program. **Pinned by the real bring-up (2026-07-12):**

- [x] knobs `train.py` exposes: `DEPTH` (8), `DEVICE_BATCH_SIZE` (128 → 16 here),
  `TOTAL_BATCH_SIZE` (2**19, held by gradient accumulation), `ASPECT_RATIO` (64),
  `HEAD_DIM` (128), `WINDOW_PATTERN`, per-group LRs — all module constants,
  "edit these directly, no CLI flags needed"
- [x] `val_bpb` format: final summary block `val_bpb:          1.858687`
  (colon + aligned whitespace) — characterized in
  `scripts/self_reliant/fixtures/train_output_sample.txt`
- [x] largest non-OOM config: `DEPTH=8` + `DEVICE_BATCH_SIZE=16` → val_bpb
  1.8587, peak VRAM 6150 MB (the ceiling), ~27K tok/sec, 27 steps in the 300 s
  budget; startup/compile ≈ 432 s (recurs on every train.py edit — the compile
  cache invalidates)

Prerequisite discovered the hard way: **triton needs a host C compiler**
(`sudo apt-get install -y gcc`) — a bare WSL Ubuntu ships without one and
training dies after ~75 s with "Failed to find C compiler".

## Environment knobs

| variable | default | meaning |
|---|---|---|
| `MINDHIVE_AUTORESEARCH_DIR` | `~/autoresearch` | WSL checkout the agent mutates |
| `MINDHIVE_RESEARCH_TRAIN_CMD` | `cd $DIR && uv run train.py` | inner WSL training command |
| `MINDHIVE_RESEARCH_CAP_S` | `900` | outer watchdog (300s budget + ~450s measured startup/compile + slack) |
| `MINDHIVE_RESEARCH_TURN_TIMEOUT_S` | `600` | bound on one researcher edit-turn |
| `MINDHIVE_BRINGUP_TIMEOUT` | `900` | bound on bring_up.sh's verification run |

## Operating the section

Dashboard → **Self-Reliant**. Type a research goal, optionally drag skills in
as strategy guidance (they never execute in the training loop), optionally set
max-experiments / time-budget caps and account failover, then **Start**. The
run is unbounded by default and ends on Stop, usage exhaustion, or a cap.
Every experiment becomes an `experiment_result` event tagged `self-reliant`
(best-of-run: a `decision`), so results flow into Memory, the graph, recall
priming, and the Skills/Improvements surfaces automatically.
