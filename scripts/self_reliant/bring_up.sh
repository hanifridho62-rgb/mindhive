#!/usr/bin/env bash
# SPDX-License-Identifier: Apache-2.0
# scripts/self_reliant/bring_up.sh — one-time WSL bring-up for Self-Reliant (plan U1).
#
# Clones karpathy/autoresearch into a WSL sibling directory (never inside the
# MindHive repo — the researcher agent mutates train.py there). autoresearch is
# a uv project (pyproject.toml + uv.lock): every dependency decision, torch
# included, is deferred to `uv sync` against the committed lockfile — upstream's
# tested environment, not a hand-picked wheel. Then prepares the data and runs
# ONE bounded training to confirm CUDA is used and a val_bpb is emitted.
#
# GATED BY DESIGN (KTD9): this downloads gigabytes (wheels + dataset).
# Run it yourself, inside WSL Ubuntu, only when you intend to:
#     bash scripts/self_reliant/bring_up.sh
#
# Idempotent: every step checks its postcondition before doing work.
set -euo pipefail

DIR="${MINDHIVE_AUTORESEARCH_DIR:-$HOME/autoresearch}"
REPO="https://github.com/karpathy/autoresearch"
# Outer bound for the verification run: train.py's own 5-minute budget EXCLUDES
# startup/compilation, so the bound must leave headroom past 300s (plan KTD2).
VERIFY_TIMEOUT="${MINDHIVE_BRINGUP_TIMEOUT:-900}"
LOG="$HOME/autoresearch_bringup_train.log"

step() { printf '\n== %s ==\n' "$*"; }

step "1/6 uv (the project manager autoresearch uses)"
if command -v uv >/dev/null 2>&1; then
    echo "uv already installed: $(uv --version)"
else
    curl -LsSf https://astral.sh/uv/install.sh | sh
    # the installer drops uv in ~/.local/bin; make it visible to THIS shell
    export PATH="$HOME/.local/bin:$PATH"
    command -v uv >/dev/null || { echo "uv install failed"; exit 1; }
fi

step "1b/6 C compiler (triton JIT-compiles its CUDA utils at runtime)"
# Discovered in the first real bring-up: torch.compile -> triton needs a host C
# compiler; a bare WSL Ubuntu ships without one and training dies with
# "Failed to find C compiler" after ~75s of startup.
if command -v gcc >/dev/null 2>&1 || command -v cc >/dev/null 2>&1; then
    echo "compiler present: $(command -v gcc || command -v cc)"
else
    echo "no C compiler — install one first:  sudo apt-get install -y gcc"
    echo "(or from Windows:  wsl -u root bash -lc 'apt-get update && apt-get install -y gcc')"
    exit 1
fi

step "2/6 clone $REPO -> $DIR"
if [ -d "$DIR/.git" ]; then
    echo "already cloned"
else
    git clone "$REPO" "$DIR"
fi
cd "$DIR"

step "3/6 uv sync (locked deps, torch included)"
uv sync

step "4/6 CUDA visible to the locked torch"
uv run python - <<'PY'
import torch
assert torch.cuda.is_available(), \
    "CUDA not available inside WSL — check `nvidia-smi` and the Windows NVIDIA driver"
print("CUDA OK:", torch.cuda.get_device_name(0))
PY

step "5/6 data prep (one-time; skipped when outputs exist)"
# prepare.py downloads training data and trains the tokenizer. It is expensive,
# so probe cheaply first: if a previous run left artifacts, don't repeat it.
if [ -n "$(find . -maxdepth 2 -name '*.bin' -o -name 'tokenizer*' 2>/dev/null | head -1)" ]; then
    echo "prepare outputs detected — skipping (delete them to force a re-prep)"
else
    uv run prepare.py
fi

step "6/6 bounded verification run (log: $LOG)"
# One real training. Expect the H100-tuned default to OOM on a 6 GB card: that
# is a RESULT, not a failure of this script. On OOM, lower DEPTH in $DIR/train.py
# (the primary complexity knob) and re-run this step until a val_bpb prints,
# then record the passing knobs in docs/self-reliant-setup.md and
# research.SMALL_GPU_PRESET (plan KTD3).
START_TS=$(date +%s)
timeout "$VERIFY_TIMEOUT" uv run train.py 2>&1 | tee "$LOG" || true
END_TS=$(date +%s)
echo
echo "run wall-clock: $((END_TS - START_TS))s (budget excludes startup/compile — record this for the watchdog margin, plan KTD2)"
if grep -qi "val_bpb" "$LOG"; then
    echo "SUCCESS: val_bpb emitted — bring-up complete. Copy the tail of $LOG into"
    echo "scripts/self_reliant/fixtures/train_output_sample.txt and pin the knobs."
else
    echo "NO val_bpb in output — check $LOG (OOM? lower DEPTH in $DIR/train.py and re-run step 6)"
fi
