# src/mindhive/research.py
"""Self-Reliant backend — autonomous autoresearch experiments for MindHive.

Phase 1 (this module's first slice) is the deterministic core the autonomous
loop is built on, and it is deliberately GPU-free at the seams so it can be
tested without WSL or a GPU:

  * ``run_experiment`` runs ONE experiment: it launches a trainer command in its
    own process group, hard-kills the whole tree at a wall-clock cap (the plan's
    5-minute watchdog, KTD2), captures stdout, and classifies the outcome as
    ``ok | timeout | oom | crash``. The trainer command is injected, so tests
    drive it with ``scripts/self_reliant/mock_trainer.py`` and production drives
    it with a ``wsl … python train.py`` invocation.
  * ``parse_val_bpb`` / ``diff_summary`` read the score and summarise what the
    agent changed in ``train.py`` for the leaderboard and the event write-back.
  * ``Leaderboard`` persists experiments atomically and answers "ranked" and
    "best so far".

The autonomous controller, program.md composition, dashboard section, and event
write-back land in later units and build on these primitives.
"""
from __future__ import annotations

import difflib
import json
import os
import re
import secrets
import signal
import subprocess
import threading
import time
from pathlib import Path

from . import paths

# The plan's hard contract: one training run is capped at 5 minutes wall-clock
# regardless of hardware (KTD2, R2). Overridable via env for tuning/tests.
EXPERIMENT_CAP_S = int(os.environ.get("MINDHIVE_RESEARCH_CAP_S", "300"))

_IS_WIN = os.name == "nt"

# Substrings that mark an out-of-memory failure in trainer output. On a 6 GB
# card OOM is an expected early outcome, so it is a *classified failure*, never a
# crash or a stall (R10).
_OOM_MARKERS = (
    "out of memory",
    "cuda oom",
    "outofmemoryerror",
    "cublas_status_alloc_failed",
    "cudnn_status_alloc_failed",
)

# ``val_bpb`` (lower is better) is autoresearch's comparison metric. Tolerant on
# spacing/punctuation ("val_bpb 1.23", "val bpb: 1.23", "val_bpb=1.23"); only a
# real decimal/integer token counts, so "val_bpb nan" / "inf" yield no match.
_VAL_BPB_RE = re.compile(r"val[_ ]?bpb\s*[:=]?\s*([0-9]+(?:\.[0-9]+)?)", re.IGNORECASE)


# --- one experiment: launch, watchdog, classify ----------------------------

def _popen(argv, cwd=None):
    """Launch the trainer in its OWN process group so the watchdog can kill the
    whole tree (a hung/OOMing run can spawn children). Windows: a new process
    group + no console window; POSIX: a new session (setsid)."""
    kwargs = dict(
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, stdin=subprocess.DEVNULL,
        text=True, encoding="utf-8", errors="replace", cwd=cwd or None)
    if _IS_WIN:
        kwargs["creationflags"] = (getattr(subprocess, "CREATE_NEW_PROCESS_GROUP", 0)
                                   | getattr(subprocess, "CREATE_NO_WINDOW", 0))
    else:
        kwargs["start_new_session"] = True
    return subprocess.Popen(argv, **kwargs)


def _kill_tree(proc) -> None:
    """Kill the process and every descendant. Windows: ``taskkill /T`` walks the
    tree; POSIX: signal the whole process group. Best-effort and idempotent."""
    if proc.poll() is not None:
        return
    try:
        if _IS_WIN:
            subprocess.run(
                ["taskkill", "/F", "/T", "/PID", str(proc.pid)],
                stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        else:
            os.killpg(os.getpgid(proc.pid), signal.SIGKILL)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    try:
        proc.kill()
    except OSError:
        pass


def _classify(timed_out: bool, exit_code, output: str, val_bpb) -> str:
    if timed_out:
        return "timeout"
    low = (output or "").lower()
    if any(m in low for m in _OOM_MARKERS):
        return "oom"
    if exit_code == 0 and val_bpb is not None:
        return "ok"
    return "crash"


def run_experiment(trainer_cmd, cap_s: int = EXPERIMENT_CAP_S, cwd=None,
                   _clock=time.monotonic) -> dict:
    """Run exactly one experiment and return a structured result.

    ``trainer_cmd`` is an argv list (injected so tests use the mock trainer and
    production uses ``wsl …``). The run is hard-capped at ``cap_s`` seconds; on
    expiry the whole process tree is killed and the result is ``timeout``. A
    spawn failure or nonzero exit without a score is ``crash``; an OOM signature
    is ``oom``; a clean exit with a parsed score is ``ok``.
    """
    start = _clock()
    try:
        proc = _popen(list(trainer_cmd), cwd=cwd)
    except (OSError, ValueError) as e:
        return {"status": "crash", "val_bpb": None, "exit_code": 127,
                "duration_s": 0.0, "output": f"spawn failed: {e}"}

    timed_out = False
    try:
        output, _ = proc.communicate(timeout=cap_s)
    except subprocess.TimeoutExpired:
        timed_out = True
        _kill_tree(proc)
        try:
            output, _ = proc.communicate(timeout=10)
        except subprocess.TimeoutExpired:
            output = ""

    duration = _clock() - start
    output = output or ""
    val_bpb = parse_val_bpb(output)
    return {
        "status": _classify(timed_out, proc.returncode, output, val_bpb),
        "val_bpb": val_bpb,
        "exit_code": proc.returncode,
        "duration_s": round(duration, 3),
        "output": output[-4000:],       # tail is enough for the log stream
    }


# --- U3: score parsing + train.py change summary ---------------------------

def parse_val_bpb(text):
    """Last ``val_bpb`` value in ``text`` as a float, or ``None`` when absent or
    non-numeric. Never raises — malformed trainer output is a failed experiment,
    not a crash of the loop."""
    if not text:
        return None
    last = None
    for m in _VAL_BPB_RE.finditer(text):
        try:
            v = float(m.group(1))
        except (TypeError, ValueError):
            continue
        if v != v or v in (float("inf"), float("-inf")):   # NaN / inf guard
            continue
        last = v
    return last


def snapshot_text(path) -> str:
    """Read ``train.py`` (or any file) for a pre/post experiment snapshot.
    Missing/unreadable file reads as empty rather than raising."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return ""


def diff_summary(before: str, after: str, max_lines: int = 8) -> str:
    """A concise one-line summary of the change from ``before`` to ``after``
    (added/removed lines only), for the leaderboard and the event write-back.
    ``"no change"`` when the texts are identical."""
    before_l = (before or "").splitlines()
    after_l = (after or "").splitlines()
    changed = [ln for ln in difflib.unified_diff(before_l, after_l, lineterm="")
               if ln[:1] in "+-" and not ln.startswith(("+++", "---"))]
    if not changed:
        return "no change"
    summary = "; ".join(ln.strip() for ln in changed[:max_lines])
    if len(changed) > max_lines:
        summary += f"  (+{len(changed) - max_lines} more)"
    return summary[:400]


# --- U4: leaderboard -------------------------------------------------------

# One module-level lock guards every leaderboard write, so concurrent appends
# from different Leaderboard instances on the same file can't lose an update
# (mirrors workspace.py's _BOARD_LOCK).
_LB_LOCK = threading.Lock()


def leaderboard_path(run_id: str | None = None) -> Path:
    name = f"research_run_{run_id}.json" if run_id else "research_runs.json"
    return paths.store_home() / name


def _is_scored(exp: dict) -> bool:
    return exp.get("status") == "ok" and isinstance(exp.get("val_bpb"), (int, float))


class Leaderboard:
    """Append-only experiment store for one run, ranked by ``val_bpb`` (asc).
    Failures sort last and never become best-so-far. Writes are atomic
    (temp + ``os.replace``) so a concurrent reader never sees a half-written
    file."""

    def __init__(self, path=None):
        self._path = Path(path) if path else leaderboard_path()

    def _load(self) -> list:
        try:
            data = json.loads(self._path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            return []
        exps = data.get("experiments") if isinstance(data, dict) else None
        return exps if isinstance(exps, list) else []

    def _write(self, experiments: list) -> None:
        self._path.parent.mkdir(parents=True, exist_ok=True)
        tmp = self._path.with_suffix(".json.tmp")
        tmp.write_text(json.dumps({"experiments": experiments},
                                  ensure_ascii=False, indent=1), encoding="utf-8")
        os.replace(tmp, self._path)

    def add(self, record: dict) -> dict:
        rec = dict(record)
        rec.setdefault("id", secrets.token_hex(4))
        with _LB_LOCK:
            exps = self._load()
            exps.append(rec)
            self._write(exps)
        return rec

    def all(self) -> list:
        return self._load()

    def ranked(self) -> list:
        # (0, score) for scored experiments sorts them ascending ahead of every
        # (1, 0) failure; failures keep insertion order among themselves.
        return sorted(self._load(),
                      key=lambda e: (0, e["val_bpb"]) if _is_scored(e) else (1, 0))

    def best(self) -> dict | None:
        scored = [e for e in self._load() if _is_scored(e)]
        return min(scored, key=lambda e: e["val_bpb"]) if scored else None
