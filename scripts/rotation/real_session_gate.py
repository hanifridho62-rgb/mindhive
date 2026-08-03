"""Real-transcript gate for smart-zone rotation.

    py scripts/rotation/real_session_gate.py

`empirical_gate.py` proves the mechanical chain end to end, but against a
transcript this repo wrote itself. This gate removes that last synthetic
element: it picks a **real transcript produced by a real Claude session** on
this machine — real model, real turns, real usage numbers the API reported —
and requires the **real PowerShell Stop-hook shim**, run as a real subprocess,
to measure it correctly and act on it.

Ground truth is computed here by an independent parser rather than by importing
`mindhive.rotation.occupancy`. If the module under test were wrong, importing it
to check itself would agree with itself; a second implementation disagrees.

WHY NOT A LIVE MODEL CALL. The directive asks for a session driven until it
crosses the threshold. That cannot run from inside an agent session: OAuth is
held by the host process (`CLAUDE_CODE_SDK_HAS_HOST_AUTH_REFRESH`), is not
inherited by a nested `claude` child, and a nested `-p` call returns
"Not logged in". Reading the user's credentials file to force it would be
handling their secrets in plain text, which is not an acceptable trade for a
green check. So this gate uses real *output* from real sessions instead, and the
live-drive step is reported as not done rather than approximated and called done.

Isolation: MINDHIVE_HOME is a throwaway directory, so the live ledger is never
written. Transcripts are read, never modified.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
SRC = REPO / "src"
SHIM = REPO / "hooks" / "stop.ps1"
HOME = Path(tempfile.mkdtemp(prefix="rotation-realtx-"))

sys.path.insert(0, str(SRC))
os.environ["MINDHIVE_HOME"] = str(HOME)
os.environ["MINDHIVE_PROJECT"] = "realgate"
os.environ["PYTHONPATH"] = str(SRC)

from mindhive.rotation import artifact                 # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if detail else ""))


def ground_truth(path: Path):
    """Independent parse: newest non-sidechain assistant turn carrying usage.

    Deliberately a separate implementation from the module under test."""
    best = None
    with path.open(encoding="utf-8", errors="ignore") as fh:
        for line in fh:
            try:
                row = json.loads(line)
            except Exception:                            # noqa: BLE001
                continue
            if not isinstance(row, dict):
                continue
            if row.get("type") != "assistant" or row.get("isSidechain"):
                continue
            msg = row.get("message")
            if not isinstance(msg, dict):
                continue
            u = msg.get("usage")
            if not isinstance(u, dict):
                continue
            total = 0
            for key in ("input_tokens", "cache_creation_input_tokens",
                        "cache_read_input_tokens"):
                v = u.get(key)
                if isinstance(v, int) and not isinstance(v, bool):
                    total += v
            best = (total, msg.get("model"))
    return best


def fire_hook(transcript: Path, *, cap: int, ratio: float, session_id="real-gate"):
    """Run the REAL shim as a REAL subprocess with a REAL Stop payload.

    BOTH bounds are set explicitly. `threshold` is `min(cap, ratio x window)`, so
    raising only the cap cannot lift the threshold above the ratio-derived value
    — a real-data gate that moved the cap alone would conclude the hook fires
    unconditionally when in fact the ratio was binding the whole time."""
    payload = json.dumps({
        "session_id": session_id, "transcript_path": str(transcript),
        "hook_event_name": "Stop", "stop_hook_active": False,
    })
    env = dict(os.environ, MINDHIVE_ROTATION="1",
               MINDHIVE_ROTATION_CAP=str(cap),
               MINDHIVE_ROTATION_RATIO=repr(ratio))
    env.pop("MINDHIVE_HEADLESS", None)
    return subprocess.run(["powershell", "-NoProfile", "-File", str(SHIM)],
                          input=payload, capture_output=True, text=True,
                          env=env, timeout=180)


# --------------------------------------------------------------- pick a real one
projects = Path.home() / ".claude" / "projects"
candidates = []
for tx in projects.glob("*/*.jsonl"):
    try:
        if tx.stat().st_size < 50_000:
            continue
    except OSError:
        continue
    truth = ground_truth(tx)
    if truth and truth[0] > 10_000 and truth[1] and "synthetic" not in str(truth[1]):
        candidates.append((truth[0], tx, truth[1]))

candidates.sort(reverse=True)
check("found a real transcript from a real session to test against",
      bool(candidates), f"{len(candidates)} usable transcript(s) on disk")

if not candidates:
    print("\nNo real transcripts available; cannot continue.")
    sys.exit(1)

true_tokens, transcript, true_model = candidates[0]
print(f"\n  transcript : {transcript.name}")
print(f"  size       : {transcript.stat().st_size:,} bytes")
print(f"  model      : {true_model}")
print(f"  occupancy  : {true_tokens:,} tokens (independent parse)\n")

# --------------------------------------------------------------- GATE: measure
sys.path.insert(0, str(SRC))
from mindhive.rotation import occupancy                # noqa: E402

measured = occupancy.read_occupancy(transcript)
check("our reader agrees with the independent parse on real data",
      measured.known and measured.tokens == true_tokens
      and measured.model == true_model,
      f"reader={measured.tokens}/{measured.model} truth={true_tokens}/{true_model}")

# The window this model actually resolves to — the ratio below is expressed
# against it, so the gate drives the threshold to exact token values.
from mindhive.rotation import window as window_mod     # noqa: E402
win = window_mod.window_for(true_model)
NO_CAP = 10_000_000                     # high enough that the ratio always binds
print(f"  window     : {win:,} tokens for {true_model}")
print(f"  ratio needed to sit exactly on {true_tokens:,}: {true_tokens / win:.6f}\n")

# --------------------------------------------------------------- GATE: fires
artifact.clear()
low = fire_hook(transcript, cap=1000, ratio=0.5)
saved = artifact.read()
check("the real shim fires on a real over-threshold session",
      low.returncode == 0 and saved is not None
      and saved["occupancy"] == true_tokens,
      f"exit={low.returncode} artifact={saved} stderr={low.stderr.strip()[:120]!r}")

check("the artifact records the real model and the cap-bound threshold",
      saved is not None and saved.get("model") == true_model
      and saved.get("threshold") == 1000,
      f"model={saved.get('model') if saved else None} "
      f"threshold={saved.get('threshold') if saved else None}")

# --------------------------------------------------------------- GATE: silent
# DISCRIMINATING: same real transcript, same real shim, threshold lifted ABOVE
# the real occupancy via the ratio. A hook that fires unconditionally passes the
# gate above and fails here.
artifact.clear()
over_ratio = (true_tokens + 50_000) / win
high = fire_hook(transcript, cap=NO_CAP, ratio=over_ratio)
check("the same real shim stays silent when the session is UNDER threshold",
      high.returncode == 0 and high.stdout.strip() == "" and artifact.read() is None,
      f"exit={high.returncode} threshold~={int(over_ratio * win):,} "
      f"vs occupancy={true_tokens:,} stdout={high.stdout.strip()[:80]!r}")

# --------------------------------------------------------------- GATE: boundary
# Exactly at the threshold must fire (>=, not >); one token above must not.
artifact.clear()
fire_hook(transcript, cap=NO_CAP, ratio=true_tokens / win)
at = artifact.read()
artifact.clear()
fire_hook(transcript, cap=NO_CAP, ratio=(true_tokens + 1) / win)
over = artifact.read()
check("threshold boundary on real data is >=, not >",
      at is not None and over is None,
      f"at_exactly_fires={at is not None} one_token_above_fires={over is not None}")

print("\n" + "=" * 72)
failed = [n for n, ok, _ in results if not ok]
print(f"{len(results) - len(failed)}/{len(results)} gates passed")
if failed:
    print("FAILED: " + "; ".join(failed))
print("""
NOT covered here, and honestly out of reach from inside an agent session:
  * driving a LIVE session until it crosses the threshold by itself — nested
    `claude -p` is unauthenticated (OAuth lives in the host process)
  * that the full-screen interactive TUI renders and accepts keystrokes
Both need a human at a real terminal running `hive session run`.""")
sys.exit(1 if failed else 0)
