#!/usr/bin/env bash
# scripts/self_reliant/bring_up.sh — one-time WSL bring-up for Self-Reliant (plan U1).
#
# Clones karpathy/autoresearch into a WSL sibling directory (never inside the
# MindHive repo — the researcher agent mutates train.py there), creates a venv,
# installs a CUDA-enabled torch for the RTX 3050, prepares the data, and runs
# ONE 5-minute-capped training to confirm CUDA is used and a val_bpb is emitted.
#
# GATED BY DESIGN (KTD9): this downloads gigabytes and compiles native wheels.
# Run it yourself, inside WSL Ubuntu, only when you intend to:
#     bash scripts/self_reliant/bring_up.sh
#
# Idempotent: every step checks its postcondition before doing work.
set -euo pipefail

DIR="${MINDHIVE_AUTORESEARCH_DIR:-$HOME/autoresearch}"
REPO="https://github.com/karpathy/autoresearch"

step() { printf '\n== %s ==\n' "$*"; }

step "1/6 clone $REPO -> $DIR"
if [ -d "$DIR/.git" ]; then
    echo "already cloned"
else
    git clone "$REPO" "$DIR"
fi
cd "$DIR"

step "2/6 python venv"
if [ ! -x .venv/bin/python ]; then
    python3 -m venv .venv
fi
. .venv/bin/activate

step "3/6 CUDA torch (RTX 3050 Laptop, 6 GB)"
# cu121 wheels cover the 3050's driver on current WSL2 setups; adjust if
# `nvidia-smi` reports an older driver.
python -c 'import torch' 2>/dev/null || \
    pip install torch --index-url https://download.pytorch.org/whl/cu121
python - <<'PY'
import torch
assert torch.cuda.is_available(), "CUDA not available inside WSL — check nvidia-smi and the driver"
print("CUDA OK:", torch.cuda.get_device_name(0))
PY

step "4/6 project dependencies"
[ -f requirements.txt ] && pip install -r requirements.txt || true

step "5/6 data prep"
# autoresearch's prepare step (name per its README at clone time — adjust if
# the repo layout differs; record what you find in docs/self-reliant-setup.md)
if [ -f prepare.py ]; then
    python prepare.py
else
    echo "NOTE: no prepare.py found — check the repo README for the data-prep entrypoint"
fi

step "6/6 one capped verification run"
# One real training bounded to 5 minutes: expect a val_bpb line in the output
# and GPU utilization in a parallel `nvidia-smi`. This is the U1 acceptance
# check; capture the exact val_bpb log-line format and the preset knobs
# train.py exposes into docs/self-reliant-setup.md and research.SMALL_GPU_PRESET.
timeout 300 python train.py || true
echo
echo "bring-up complete — if a val_bpb line printed above, Self-Reliant's"
echo "preflight will now pass. Record the discovered preset knobs in"
echo "docs/self-reliant-setup.md."
