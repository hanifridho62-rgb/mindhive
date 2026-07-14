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

On top of those primitives sit ``compose_program`` (the researcher's steering
document, U6), ``ResearchController`` (the autonomous loop with its terminator
matrix, flywheel write-back, skill attribution, and profile failover — U5, U9,
U10, U11), and the production wiring: the WSL trainer command, the Start
preflight gate, the Manager-backed researcher agent, and the dashboard's
``api_get``/``api_post`` dispatch consumed by /api/research/*.
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

# The product contract: one TRAINING run gets a 5-minute budget regardless of
# hardware (origin R2) — that budget lives inside autoresearch's train.py and
# EXCLUDES startup/compilation. The outer watchdog below therefore must exceed
# the inner budget by a startup/compile margin, or every healthy real run would
# be killed as 'timeout' before its budget completed (U1 plan KTD2). Measured
# on the real bring-up (RTX 3050, WSL2): training 313.8s, total 746.3s —
# startup/compile ≈ 432s, and every agent edit to train.py re-triggers compile,
# so the margin never amortizes away. 900s = 300 budget + ~450 startup + slack.
INNER_BUDGET_S = 300
EXPERIMENT_CAP_S = int(os.environ.get("MINDHIVE_RESEARCH_CAP_S", "900"))

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
                   on_spawn=None, _clock=time.monotonic) -> dict:
    """Run exactly one experiment and return a structured result.

    ``trainer_cmd`` is an argv list (injected so tests use the mock trainer and
    production uses ``wsl …``). The run is hard-capped at ``cap_s`` seconds; on
    expiry the whole process tree is killed and the result is ``timeout``. A
    spawn failure or nonzero exit without a score is ``crash``; an OOM signature
    is ``oom``; a clean exit with a parsed score is ``ok``. ``on_spawn`` (if
    given) receives the live Popen right after launch so a caller's Stop can
    kill an in-flight run.
    """
    start = _clock()
    try:
        proc = _popen(list(trainer_cmd), cwd=cwd)
    except (OSError, ValueError) as e:
        return {"status": "crash", "val_bpb": None, "exit_code": 127,
                "duration_s": 0.0, "output": f"spawn failed: {e}"}
    if on_spawn is not None:
        on_spawn(proc)

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


def snapshot_text(path):
    """Read ``train.py`` (or any file) for a pre/post experiment snapshot.
    Missing/unreadable reads as ``None`` — never as empty text, or a run whose
    train.py sits on a flaky ``\\\\wsl$`` mount would diff ""-vs-"" and record
    a false "no change" even when the researcher's edit moved the score."""
    try:
        return Path(path).read_text(encoding="utf-8")
    except OSError:
        return None


def diff_summary(before, after, max_lines: int = 8) -> str:
    """A concise one-line summary of the change from ``before`` to ``after``
    (added/removed lines only), for the leaderboard and the event write-back.
    ``"no change"`` when the texts are identical; an unreadable snapshot
    (``None``) is reported as unknown rather than claimed as no change."""
    if before is None or after is None:
        return "change unknown (train.py snapshot unreadable)"
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


# --- U6: program.md composition ---------------------------------------------

# Small-GPU preset bounds for the RTX 3050 Laptop (6 GB VRAM), pinned against
# autoresearch's real train.py by the U1 bring-up run (2026-07-12): DEPTH=8 +
# DEVICE_BATCH_SIZE=16 reached val_bpb 1.8587 at peak VRAM 6150 MB — the
# ceiling. Knob names are train.py's actual constants. Rendered verbatim into
# the researcher's program as hard constraints; override per-run via `preset=`.
SMALL_GPU_PRESET = {
    "hardware": "RTX 3050 Laptop, 6 GB VRAM — not an H100. Verified baseline: "
                "DEPTH=8 + DEVICE_BATCH_SIZE=16 fits at peak_vram 6150 MB "
                "(the ceiling); upstream's DEVICE_BATCH_SIZE=128 OOMs.",
    "DEVICE_BATCH_SIZE": "8 is the safe baseline at DEPTH 8; 16 fits only with "
                         "nothing else on the GPU (peak 6150/6144 MB — one "
                         "real run at 16 OOM'd from ambient VRAM use). Lower "
                         "THIS first when memory is tight — TOTAL_BATCH_SIZE "
                         "stays fixed via gradient accumulation, so "
                         "comparability is preserved.",
    "DEPTH": "8 fits with zero headroom; raising DEPTH requires lowering "
             "DEVICE_BATCH_SIZE at the same time (activations dominate VRAM).",
    "throughput": "~27K tok/sec, ~20 s/optimizer-step at the baseline — the "
                  "300 s budget is ~27 steps; keep eval cadence cheap.",
    "precision": "bf16 autocast is train.py's default — do not add fp32 paths.",
}

# KTD11 — the iteration contract the researcher agent must follow. One
# experiment is exactly one agent edit-turn plus one Python-owned training run;
# the agent never runs training itself, so the 5-minute watchdog stays
# authoritative.
_ITERATION_CONTRACT = (
    "Iteration contract — follow it exactly: edit train.py ONLY, using your "
    "file-editing tool, then end your turn immediately. Do not run training "
    "or any other command, and do not edit other files. You are headless — "
    "there is NO operator: never ask permission, never propose a script for "
    "someone else to run, never end your turn with a question. MindHive "
    "itself runs the 5-minute capped experiment after your turn and hands "
    "you the score in the next prompt; one small edit per turn is the whole "
    "job."
)

# The learn-mode contract: the agent analyzes and reports, it does not mutate.
# The report is captured and persisted as a durable learning so a human working
# with Claude Code makes the actual improvement. The GitHub clause is stated
# here as well as enforced at spawn (read-only allowlist) — defense in depth.
_ANALYSIS_CONTRACT = (
    "Analysis contract — follow it exactly: you are read-only. Do NOT edit "
    "train.py or any file, and do NOT run training or any other command. You "
    "must NEVER touch GitHub — no git, no gh, no pushes, no commits, no remote "
    "operations of any kind. Read train.py and the experiment history, then "
    "produce a structured learning report with these sections: Observations "
    "(what the current config and history show), What worked / regressed, "
    "Ranked hypotheses (most promising first, with why), and Recommended edits "
    "(concrete changes a human using Claude Code should apply next). You are "
    "headless — there is NO operator: never ask permission and never end your "
    "turn with a question. End your turn when the report is written; MindHive "
    "persists it as a learning for the next prompt."
)


def _clean_skills(skills) -> list:
    """Mirror of workspace._clean_skills, local so research.py stays free of a
    workspace import at module level: keep only named skills, cap lengths."""
    out = []
    for s in skills or []:
        if isinstance(s, dict) and s.get("name"):
            out.append({"name": str(s["name"])[:120],
                        "plugin": (str(s["plugin"])[:120]
                                   if s.get("plugin") else None)})
    return out


def compose_program(goal, skills=None, primed=None, preset=None, prior=None,
                    train_path=None, mode="improve", history=None) -> dict:
    """The researcher agent's steering document (program.md text) plus its
    system-prompt addition — a pure function of its inputs (U6).

    ``skills`` are strategy *guidance*, never executable steps (KTD4);
    ``primed`` is a list of past-learning strings recalled from the ledger;
    ``preset`` is a name->bound mapping stated as hard constraints; ``prior``
    is the previous experiment's record so the agent iterates on feedback.
    ``mode`` is ``improve`` (the agent edits train.py) or ``learn`` (the agent
    analyzes and reports; it never edits). ``history`` is the leaderboard's
    ranked experiment records — supplied in learn mode so the analysis is
    grounded in real past results rather than train.py alone (R2).
    """
    learn = mode == "learn"
    skills = _clean_skills(skills)
    if learn:
        parts = [f"RESEARCH GOAL: {(goal or '').strip()}",
                 "You are the Self-Reliant researcher in analyze-and-learn "
                 "mode. Study train.py and the experiment history and produce "
                 "learnings that a human using Claude Code will act on — the "
                 "objective is the lowest val_bpb (lower is better) on this "
                 "machine, but you do not edit anything yourself."]
    else:
        parts = [f"RESEARCH GOAL: {(goal or '').strip()}",
                 "You are the Self-Reliant researcher. Optimize train.py so a "
                 "5-minute training run reaches the lowest possible val_bpb "
                 "(lower is better) on this machine."]
    if skills:
        lines = ["Strategy guidance from the staged skills, in order — steer "
                 "your edits with them (they are guidance, not commands to "
                 "execute):"]
        for i, s in enumerate(skills, 1):
            src = f" (from the {s['plugin']} plugin)" if s.get("plugin") else ""
            lines.append(f"{i}. {s['name']}{src}")
        parts.append("\n".join(lines))
    if preset:
        bounds = "\n".join(f"- {k}: {v}" for k, v in preset.items())
        parts.append("Hard constraints from the small-GPU preset — configs "
                     "outside these bounds will OOM:\n" + bounds)
    if primed:
        parts.append("Past learnings from MindHive — honor them:\n"
                     + "\n".join(f"- {p}" for p in primed))
    if learn and history:
        lines = ["Experiment history on this machine (best val_bpb first) — "
                 "ground your analysis in these real results, not train.py "
                 "alone:"]
        for h in history:
            score = h.get("val_bpb")
            lines.append(f"- {h.get('status', '?')}"
                         + (f" val_bpb {score}" if score is not None else " (no score)")
                         + (f" — {h['change']}" if h.get("change") else ""))
        parts.append("\n".join(lines))
    if prior:
        score = prior.get("val_bpb")
        parts.append("Previous experiment: status "
                     f"{prior.get('status', '?')}"
                     + (f", val_bpb {score}" if score is not None else ", no score")
                     + (f" — change: {prior['change']}" if prior.get("change") else "")
                     + ". Iterate on this result.")
        # the no-edit nudge is an improve-mode concern only — in learn mode not
        # editing is the whole point, so never scold the agent for it
        if not learn and prior.get("change") == "no change":
            parts.append("Your previous turn made NO edit to train.py — the "
                         "experiment trained an unchanged file, which wastes "
                         "the whole 5-minute budget. This turn you MUST make "
                         "an actual edit before ending your turn.")
    if train_path:
        if learn:
            parts.append(f"train.py location: {train_path} — read exactly this "
                         "file; it lives inside WSL, outside your working "
                         "directory. Do not edit it.")
        else:
            parts.append(f"train.py location: {train_path} — edit exactly this "
                         "file; it lives inside WSL, outside your working "
                         "directory.")
    contract = _ANALYSIS_CONTRACT if learn else _ITERATION_CONTRACT
    parts.append(contract)
    return {"program": "\n\n".join(parts), "system": contract}


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

    @property
    def path(self) -> Path:
        return self._path

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


# --- U5: ResearchController — loop lifecycle, terminators, stop semantics ----

# Bound on ONE researcher edit-turn (thinking time between experiments), so a
# stuck agent can't destroy the ~12/hour throughput (R2). Distinct from the
# training cap: the 5-minute watchdog bounds the training subprocess only.
TURN_TIMEOUT_S = float(os.environ.get("MINDHIVE_RESEARCH_TURN_TIMEOUT_S", "600"))

_MAX_LOG_LINES = 4000                # controller log scrollback (mirrors panes)


class ResearchController:
    """Owns one Self-Reliant run: compose -> agent edit-turn -> Python-owned
    capped training run -> record -> check terminators -> repeat (KTD11).

    Everything nondeterministic is injected: ``trainer_cmd`` (argv list, or a
    callable ``iteration -> argv``), ``agent_factory`` (``profile -> researcher``
    with ``turn(prompt) -> dict`` and ``stop()``), ``preflight`` (``() -> list``
    of failure strings), ``append_event``, ``auth_status``, and ``profiles`` —
    so the full terminator matrix runs against the mock trainer and a stubbed
    agent with no WSL, GPU, or CLI.

    A researcher turn returns ``{"ok": bool}`` plus optionally
    ``usage_exhausted`` (account limit — a graceful terminator, KTD6) or
    ``error``. Unknown terminal errors end the run conservatively (log + stop)
    per the plan's deferred-detection rule.
    """

    def __init__(self, trainer_cmd=None, agent_factory=None, train_path=None,
                 leaderboard=None, append_event=None, preflight=None,
                 cap_s=None, turn_timeout_s=None, profiles=None,
                 auth_status=None, on_kill=None):
        self._trainer_cmd = trainer_cmd
        self._factory = agent_factory
        self._train_path = train_path
        # An injected leaderboard is pinned (tests assert on it); without one,
        # each start() re-points at a per-run store (plan U4), so "best of run"
        # never mixes tonight's experiments with last night's.
        self._lb_injected = leaderboard is not None
        self.leaderboard = leaderboard or Leaderboard()
        self._append = append_event
        self._preflight = preflight if preflight is not None else default_preflight
        self._cap_s = cap_s or EXPERIMENT_CAP_S
        self._turn_timeout_s = turn_timeout_s or TURN_TIMEOUT_S
        self._profiles = list(profiles or [])
        self._auth_status = auth_status
        self._on_kill = on_kill          # extra cleanup after a training kill
        self._lock = threading.Lock()
        self._stop_flag = threading.Event()
        self._thread = None
        self._proc = None                # in-flight training subprocess
        self._researcher = None
        self._state = "idle"             # idle | running | stopped | ended
        self._reason = None              # None | cap | time | usage | error
        self._count = 0
        self.run_id = None
        self._goal = ""
        self._mode = "improve"           # improve (edit) | learn (analyze)
        self._last_report = ""           # last recorded learn report (dedup)
        self.lines: list = []

    # --- observability -------------------------------------------------------
    def emit(self, line: str) -> None:
        self.lines.append(line)
        if len(self.lines) > _MAX_LOG_LINES:
            del self.lines[:len(self.lines) - _MAX_LOG_LINES]

    def output(self, since=0) -> dict:
        since = min(max(0, int(since)), len(self.lines))
        return {"ok": True, "lines": self.lines[since:], "next": len(self.lines),
                "status": self._state}

    def learnings_text(self) -> str:
        """The current run's accumulated learn-mode report artifact, or empty
        when the run is not a learn run / has produced nothing yet."""
        try:
            return self._learnings_path().read_text(encoding="utf-8")
        except (OSError, ValueError):
            return ""

    def status(self) -> dict:
        best = self.leaderboard.best()
        return {"state": self._state, "reason": self._reason,
                "experiments": self._count, "goal": self._goal,
                "run_id": self.run_id, "mode": self._mode,
                "best": best.get("val_bpb") if best else None}

    def wait(self, timeout=None) -> bool:
        t = self._thread
        if t is None:
            return True
        t.join(timeout)
        return not t.is_alive()

    # --- lifecycle -----------------------------------------------------------
    def start(self, goal, skills=None, max_experiments=None, time_budget_s=None,
              failover=False, profile="default", primed=None, preset=None,
              model=None, mode="improve") -> dict:
        goal = (goal or "").strip()
        if not goal:
            return {"ok": False, "error": "research goal is empty"}
        # Parse the caps BEFORE any state transition: a malformed body must be
        # a structured rejection, never a 500 with the state stuck on running.
        try:
            max_exp = int(max_experiments) if max_experiments else None
            budget = float(time_budget_s) if time_budget_s else None
        except (TypeError, ValueError):
            return {"ok": False, "error":
                    "max_experiments and time_budget_s must be numbers"}
        with self._lock:
            if self._state == "running":
                return {"ok": False, "error": "a run is already active — Stop it first"}
            # a just-stopped run's loop thread may still be finishing; let it
            # write its final state before this run overwrites it
            t = self._thread
            if t is not None and t.is_alive():
                t.join(5)
                if t.is_alive():
                    return {"ok": False, "error":
                            "previous run is still winding down — retry in a moment"}
            failures = self._preflight(profile or "default")
            if failures:
                return {"ok": False, "error": "preflight failed: " + "; ".join(failures)}
            self._state = "running"
        self._stop_flag.clear()
        self._reason = None
        self._count = 0
        self.run_id = secrets.token_hex(4)
        if not self._lb_injected:
            self.leaderboard = Leaderboard(leaderboard_path(self.run_id))
        self._goal = goal
        self._skills = _clean_skills(skills)
        self._max = max_exp
        self._deadline = time.monotonic() + budget if budget else None
        self._failover = bool(failover)
        self._profile = profile or "default"
        # "" / whitespace => account default (no --model flag), so the researcher
        # runs on whatever the chosen account defaults to (mirrors MindParallel).
        self._model = (model or "").strip() or None
        self._used_profiles = {self._profile}
        self._primed = list(primed or [])
        self._preset = preset if preset is not None else SMALL_GPU_PRESET
        self._best_score = None          # running best within THIS run (U10)
        self._tally = {}
        self._last_report = ""            # dedup guard for learn-mode reports
        self._mode = "learn" if mode == "learn" else "improve"
        # an uncapped learn run re-analyzes identical state forever; give it a
        # default round cap when the caller set neither cap nor budget
        if (self._mode == "learn" and self._max is None
                and self._deadline is None):
            self._max = _DEFAULT_LEARN_ROUNDS
        try:
            self._researcher = self._factory(self._profile, self._model)
            self._apply_tool_policy(self._researcher)
        except Exception as e:
            self._state = "idle"
            return {"ok": False, "error": f"could not start researcher agent: {e}"}
        self.emit(f"· run {self.run_id} started ({self._mode}) — goal: {goal}"
                  + (f" · cap {self._max}" if self._max else " · unbounded"))
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return {"ok": True, "run_id": self.run_id}

    def stop(self) -> dict:
        """Manual Stop (R4): immediate. Kills the in-flight training process
        tree, stops the researcher agent, preserves the leaderboard."""
        self._stop_flag.set()
        proc = self._proc
        if proc is not None:
            _kill_tree(proc)
            if self._on_kill is not None:
                try:
                    self._on_kill()
                except Exception:
                    pass
        r = self._researcher
        if r is not None:
            try:
                r.stop()
            except Exception:
                pass
        return {"ok": True}

    # --- the loop ------------------------------------------------------------
    def _run_turn(self, prompt) -> dict:
        """One researcher edit-turn, bounded by the turn timeout and
        interruptible by Stop. ``aborted`` marks a timed-out turn."""
        box = {}

        def worker():
            try:
                box["res"] = self._researcher.turn(prompt)
            except Exception as e:               # a raising agent is terminal
                box["res"] = {"ok": False, "error": f"agent turn raised: {e}"}

        t = threading.Thread(target=worker, daemon=True)
        t.start()
        deadline = time.monotonic() + self._turn_timeout_s
        while t.is_alive() and time.monotonic() < deadline \
                and not self._stop_flag.is_set():
            t.join(0.05)
        if t.is_alive():
            # The turn crossed its wall-clock budget, but the underlying agent
            # run is still in flight — abandoning the worker thread alone leaves
            # it running. On the production ManagerResearcher that orphans the
            # workspace agent (status stays "running"); the NEXT turn's send()
            # then rejects with "… is busy", which the loop treats as terminal
            # and the whole run dies. Stop the in-flight run so the next turn
            # starts against a settled agent.
            try:
                self._researcher.stop()
            except Exception:
                pass
            return {"ok": False, "aborted": True}
        res = box.get("res")
        return res if isinstance(res, dict) else \
            {"ok": False, "error": "agent turn returned nothing"}

    def _trainer_argv(self, iteration) -> list:
        cmd = self._trainer_cmd
        return list(cmd(iteration)) if callable(cmd) else list(cmd or [])

    def _apply_tool_policy(self, researcher) -> None:
        """Enforce the no-GitHub boundary on the researcher agent: learn mode is
        read-only (allowlist ⇒ no Bash ⇒ no git/gh); every mode denies git/gh.
        In learn mode the boundary is load-bearing, so if it cannot be set the
        run fails closed (raises) rather than silently proceeding as an
        unrestricted agent — a failure to restrict must never be invisible."""
        allowed = SELF_RELIANT_ALLOWED_TOOLS if self._mode == "learn" else None
        try:
            researcher.allowed_tools = allowed
            researcher.disallowed_tools = GITHUB_DENY
        except Exception as e:
            self.emit(f"✖ could not apply the no-GitHub tool policy: {e}")
            if self._mode == "learn":
                raise

    def _loop(self) -> None:
        prior = None
        reason = "error"
        try:
            while True:
                if self._stop_flag.is_set():
                    reason = "stopped"
                    return
                if self._max is not None and self._count >= self._max:
                    reason = "cap"
                    return
                if self._deadline is not None and time.monotonic() >= self._deadline:
                    reason = "time"
                    return
                composed = compose_program(self._goal, self._skills,
                                           self._primed, self._preset, prior,
                                           train_path=(str(self._train_path)
                                                       if self._train_path else None),
                                           mode=self._mode,
                                           history=(self.leaderboard.ranked()
                                                    if self._mode == "learn" else None))
                # snapshot BEFORE the edit-turn: the turn is what changes
                # train.py, so a post-turn "before" reads the edited file and
                # every agent edit would record as "no change". Learn mode never
                # edits, so it takes no snapshot.
                before = (snapshot_text(self._train_path)
                          if self._train_path and self._mode != "learn" else "")
                turn = self._run_turn(composed["program"])
                for ln in turn.get("lines") or []:
                    self.emit(f"  agent: {ln}")   # why it edited (or didn't)
                if self._stop_flag.is_set():
                    reason = "stopped"
                    return
                if turn.get("aborted"):
                    if self._mode == "learn":
                        self._count += 1     # a round happened; counts to the cap
                        will_retry = self._max is None or self._count < self._max
                        self.emit(f"#{self._count} analysis turn timed out"
                                  + (" — retrying" if will_retry else ""))
                    else:
                        self._record({"status": "aborted", "val_bpb": None,
                                      "change": "", "duration_s": 0.0,
                                      "output": "agent turn exceeded the timeout"})
                    prior = None
                    continue
                if turn.get("usage_exhausted"):
                    nxt = self._next_healthy_profile() if self._failover else None
                    if nxt:
                        self.emit(f"· usage exhausted on {self._profile} — "
                                  f"failing over to {nxt}")
                        self._profile = nxt
                        self._used_profiles.add(nxt)
                        self._researcher = self._factory(nxt, self._model)
                        self._apply_tool_policy(self._researcher)
                        continue
                    reason = "usage"
                    return
                if not turn.get("ok"):
                    self.emit("✖ researcher turn failed: "
                              + str(turn.get("error") or "unknown error"))
                    reason = "error"
                    return
                if self._mode == "learn":
                    # analyze-and-learn: persist the agent's report as a durable
                    # learning; never run training (an unchanged file would just
                    # retrain the baseline and waste the budget). The round
                    # counts toward the cap whether or not the report is kept.
                    self._count += 1
                    self._record_learning(turn.get("report", ""))
                    prior = None
                    continue
                res = run_experiment(self._trainer_argv(self._count),
                                     cap_s=self._cap_s,
                                     on_spawn=self._track_proc)
                self._proc = None
                if self._stop_flag.is_set():
                    reason = "stopped"       # Stop killed this run — don't record
                    return
                after = snapshot_text(self._train_path) if self._train_path else ""
                res["change"] = diff_summary(before, after) if self._train_path else ""
                prior = self._record(res)
        finally:
            self._finish(reason)

    def _track_proc(self, proc) -> None:
        self._proc = proc

    def _next_healthy_profile(self):
        for p in self._profiles:
            if p in self._used_profiles:
                continue
            try:
                if (self._auth_status(p) or {}).get("state") == "ok":
                    return p
            except Exception:
                continue
        return None

    # --- record + flywheel write-back (U9) + attribution (U10) ---------------
    def _record(self, res: dict) -> dict:
        rec = {"status": res.get("status"), "val_bpb": res.get("val_bpb"),
               "change": res.get("change", ""),
               "duration_s": res.get("duration_s", 0.0),
               "skills": [s["name"] for s in self._skills],
               "run_id": self.run_id}
        rec = self.leaderboard.add(rec)
        self._count += 1
        score = rec["val_bpb"]
        self.emit(f"#{self._count} {rec['status']}"
                  + (f" · val_bpb {score}" if score is not None else "")
                  + (f" · {rec['change']}" if rec.get("change") else ""))
        self._log("experiment_result", {
            "text": f"Self-Reliant experiment {self._count}: {rec['status']}"
                    + (f" val_bpb={score}" if score is not None else "")
                    + (f" — {rec['change']}" if rec.get("change") else ""),
            "goal": self._goal, "status": rec["status"], "val_bpb": score,
            "change": rec.get("change", ""), "skills": rec["skills"]})
        improved = (rec["status"] == "ok" and score is not None
                    and (self._best_score is None or score < self._best_score))
        if improved:
            self._best_score = score
            self._credit_skills(score)
        return rec

    # --- learn mode: durable, recallable learnings (R3) ----------------------
    def _learnings_path(self) -> Path:
        # per-run artifact: keying on run_id (not just the leaderboard stem)
        # keeps one run's reports from accumulating onto a prior run's file
        # when the leaderboard path is pinned/shared across runs.
        p = self.leaderboard.path
        rid = self.run_id or "run"
        return p.with_name(f"{p.stem}_{rid}_learnings.md")

    def _record_learning(self, report) -> None:
        """Persist one analysis turn's report so a human with Claude Code can
        recall and act on it: a ledger event tagged self-reliant + learning,
        and an append to the per-run learnings artifact beside the leaderboard.
        Learn mode never touches the leaderboard. The round has already been
        counted by the caller; empty or unchanged reports are skipped so they
        never flood the ledger or poison future _primed_learnings recall."""
        report = (report or "").strip()
        if not report or report == self._last_report:
            self.emit("· analysis round produced "
                      + ("an empty report" if not report else "no new insight")
                      + " — not recorded")
            return
        self._last_report = report
        self.emit(f"#{self._count} learning recorded · {len(report)} chars")
        self._log("learning", {
            "text": f"Self-Reliant learning {self._count} (goal: {self._goal}):"
                    f"\n{report}",
            "goal": self._goal, "report": report},
            extra_tags=["learning"])
        try:
            p = self._learnings_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            with p.open("a", encoding="utf-8") as f:
                f.write(f"\n\n## Learning {self._count} — {self._goal}\n\n"
                        + report + "\n")
        except OSError as e:
            # the ledger event above is the durable record; surface the artifact
            # failure in the run log instead of silently dropping it
            self.emit(f"· learning {self._count} ledgered, but its artifact "
                      f"write failed: {e}")

    def _credit_skills(self, score) -> None:
        """U10 presence-correlation: every staged skill on a new-best experiment
        earns credit, persisted beside the leaderboard and emitted as a signed
        event the reflect/Improvements pipeline can ingest (KTD5, KTD8)."""
        for s in self._skills:
            name = s["name"]
            self._tally[name] = self._tally.get(name, 0) + 1
            self._log("experiment_result", {
                "signature": f"skill:{name}",
                "text": f"skill {name} was staged on a new-best Self-Reliant "
                        f"experiment (val_bpb {score})"},
                extra_tags=["skill-effectiveness", f"skill:{name}"])
        if self._skills:
            self._save_tally()

    def _tally_path(self) -> Path:
        p = self.leaderboard.path
        return p.with_name(p.stem + "_skills.json")

    def _save_tally(self) -> None:
        try:
            merged = self.skill_tally()
            for k, v in self._tally.items():
                merged[k] = v
            p = self._tally_path()
            p.parent.mkdir(parents=True, exist_ok=True)
            tmp = p.with_suffix(".json.tmp")
            tmp.write_text(json.dumps(merged, ensure_ascii=False, indent=1),
                           encoding="utf-8")
            os.replace(tmp, p)
        except OSError:
            pass                          # the tally must never crash the loop

    def skill_tally(self) -> dict:
        try:
            data = json.loads(self._tally_path().read_text(encoding="utf-8"))
            return data if isinstance(data, dict) else {}
        except (OSError, ValueError):
            return {}

    def _finish(self, reason) -> None:
        self._state = "stopped" if reason == "stopped" else "ended"
        self._reason = None if reason == "stopped" else reason
        best = self.leaderboard.best()
        if best is not None:
            self._log("decision", {
                "text": f"Self-Reliant best of run {self.run_id}: "
                        f"val_bpb {best['val_bpb']}"
                        + (f" — {best['change']}" if best.get("change") else "")
                        + f" (goal: {self._goal})"})
        self.emit(f"■ run {self.run_id} {self._state}"
                  + (f" ({self._reason})" if self._reason else "")
                  + (f" · best val_bpb {best['val_bpb']}" if best else ""))

    def _log(self, etype, payload, extra_tags=None) -> None:
        if self._append is None:
            return
        tags = ["self-reliant"]
        # attribution events carry their own skill:<name> tag; plain records
        # are tagged with every staged skill so recall/graph can slice by skill
        tags += list(extra_tags) if extra_tags else \
            [f"skill:{s['name']}" for s in self._skills]
        try:
            self._append(etype, payload,
                         session_id=f"research-{self.run_id}", tags=tags)
        except Exception:
            pass                     # the ledger must never crash the loop


# --- production wiring: WSL trainer, researcher agent, preflight, API --------

# Where autoresearch lives inside WSL — a sibling checkout outside the repo
# (KTD3); the agent mutates train.py there. Pinned for real by U1's bring-up.
AUTORESEARCH_DIR = os.environ.get("MINDHIVE_AUTORESEARCH_DIR", "~/autoresearch")


def wsl_trainer_cmd(iteration=0) -> list:
    """argv for one training run inside WSL. autoresearch is a uv project —
    `uv run train.py` resolves the locked environment (pinned by the U1
    bring-up); override via MINDHIVE_RESEARCH_TRAIN_CMD."""
    inner = os.environ.get(
        "MINDHIVE_RESEARCH_TRAIN_CMD",
        f"cd {AUTORESEARCH_DIR} && uv run train.py")
    return ["wsl", "bash", "-lc", inner]


def kill_wsl_training() -> None:
    """Kill the Linux-side training tree. taskkill on the wsl.exe shim alone
    can orphan the Linux process (plan risk), so Stop also pkills inside WSL."""
    try:
        subprocess.run(["wsl", "pkill", "-f", "train.py"],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                       timeout=15,
                       creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
    except (OSError, subprocess.SubprocessError):
        pass


def wsl_train_py_windows_path():
    """The Windows-visible (\\\\wsl$\\...) path of the WSL checkout's train.py,
    via wslpath — the researcher agent runs on Windows and must be pointed at
    the exact file. None when WSL/the checkout is unavailable (preflight will
    refuse the run anyway)."""
    try:
        p = subprocess.run(
            ["wsl", "bash", "-lc", f"wslpath -w {AUTORESEARCH_DIR}/train.py"],
            capture_output=True, text=True, timeout=20,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = (p.stdout or "").strip()
        return out if p.returncode == 0 and out else None
    except (OSError, subprocess.SubprocessError):
        return None


def _wsl_ok(cmd: str, timeout=20) -> bool:
    try:
        p = subprocess.run(["wsl", "bash", "-lc", cmd], capture_output=True,
                           text=True, timeout=timeout,
                           creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return p.returncode == 0
    except (OSError, subprocess.SubprocessError):
        return False


def default_preflight(profile: str = "default") -> list:
    """The production Start gate (U5): refuse to launch an autonomous run with
    a specific reason instead of failing one experiment at a time."""
    if not _wsl_ok("true"):
        return ["WSL is not reachable — check `wsl --status`"]
    fails = []
    if not _wsl_ok(f"test -f {AUTORESEARCH_DIR}/train.py"):
        fails.append(f"autoresearch not found at {AUTORESEARCH_DIR} — run "
                     "scripts/self_reliant/bring_up.sh first")
    if not _wsl_ok("nvidia-smi -L"):
        fails.append("no GPU visible inside WSL (nvidia-smi failed)")
    try:
        from . import workspace
        st = workspace.profile_auth_status(profile)
        if st.get("state") != "ok":
            fails.append(f"researcher profile '{profile}' auth is "
                         f"{st.get('state')} — {st.get('detail')}")
    except Exception as e:
        fails.append(f"auth check failed: {e}")
    return fails


# Conservative usage-exhaustion signatures in a failed run's output. The
# precise CLI wording is an execution-time discovery (plan: pinned in U5);
# anything not matching stays an unknown terminal error -> log + stop.
_USAGE_MARKERS = ("usage limit", "rate limit", "out of usage",
                  "hit your limit", "session limit", "quota")


# The GitHub boundary. Learn mode spawns read-only: the allowlist below is the
# ENFORCED gate — workspace.send() drops bypassPermissions whenever an allowlist
# is present, so with no prompt channel any tool outside it (no Bash ⇒ no
# git/gh) is auto-denied. The set is deliberately minimal (Read/Grep/Glob only);
# Skill is intentionally excluded because it could shell out and widen a
# "read-only" turn. GITHUB_DENY layers on top as defense-in-depth: for learn it
# is redundant with the allowlist; for improve mode — which keeps
# bypassPermissions so it can Edit train.py — it is a best-effort deny against
# literal git/gh invocations, NOT a hard guarantee (a shell can reach the same
# binaries by other spellings). A hard improve-mode boundary (no-Bash / egress
# controls) is deferred follow-up.
SELF_RELIANT_ALLOWED_TOOLS = ["Read", "Grep", "Glob"]
GITHUB_DENY = ["Bash(git:*)", "Bash(gh:*)", "mcp__github__*"]

# Learn rounds re-analyze the same state (there is no fresh experiment between
# them), so an uncapped learn run would loop and burn account usage forever.
# When the caller sets neither a round cap nor a time budget, fall back to this
# many analysis rounds. Improve runs gather new data each round and stay
# unbounded by default.
_DEFAULT_LEARN_ROUNDS = 5


class ManagerResearcher:
    """The production researcher brain: one headless-claude workspace agent on
    the shared Manager. Manager.send is asynchronous, so a turn = send + poll
    until the agent settles; --resume continuity keeps the whole run one
    conversation. Stop kills the agent's in-flight CLI run.

    ``allowed_tools``/``disallowed_tools`` (set by the controller per mode) are
    forwarded to every send, enforcing the no-GitHub boundary at the CLI."""

    def __init__(self, profile="default", manager=None, model=None,
                 system_extra="", poll_s=1.0, add_dir=None,
                 allowed_tools=None, disallowed_tools=None):
        from . import workspace
        self._mgr = manager or workspace.MANAGER
        self._poll_s = poll_s
        self._system_extra = system_extra
        self.allowed_tools = allowed_tools
        self.disallowed_tools = disallowed_tools
        # the autoresearch checkout lives OUTSIDE the agent's cwd (\\wsl$ …);
        # granted per-turn via --add-dir or its edits are silently blocked
        self._add_dirs = [add_dir] if add_dir else []
        res = self._mgr.spawn(name="Self-Reliant researcher", role="agent",
                              profile=profile, model=model)
        if not res.get("ok"):
            raise RuntimeError(res.get("error") or "could not spawn researcher")
        self.agent_id = res["agent"]["id"]

    def turn(self, prompt) -> dict:
        res = self._mgr.send(self.agent_id, prompt,
                             extra_system=self._system_extra or None,
                             add_dirs=self._add_dirs,
                             allowed_tools=self.allowed_tools,
                             disallowed_tools=self.disallowed_tools)
        if not res.get("ok"):
            return {"ok": False, "error": res.get("error")}
        while True:
            a = self._mgr.get(self.agent_id)
            if a is None:
                return {"ok": False, "error": "researcher agent disappeared"}
            if a.status != "running":
                break
            time.sleep(self._poll_s)
        lines = list(a.lines[-12:])         # reply tail for the run log
        if a.status == "done":
            # report = the agent's final message; learn mode persists it as a
            # learning, improve mode ignores it
            return {"ok": True, "lines": lines,
                    "report": getattr(a, "last_result", "") or ""}
        tail = " ".join(a.lines[-30:]).lower()
        if any(m in tail for m in _USAGE_MARKERS):
            return {"ok": False, "usage_exhausted": True, "lines": lines}
        return {"ok": False, "error": f"researcher run ended '{a.status}'",
                "lines": lines}

    def stop(self) -> None:
        try:
            self._mgr.stop(self.agent_id)
        except Exception:
            pass


def _primed_learnings(goal, project=None, limit=5) -> list:
    """Recall past learnings relevant to the goal to prime the run (R8).
    Best-effort — an empty ledger or broken recall never blocks a Start."""
    out = []
    try:
        from . import recall as recall_mod
        for hit in (recall_mod.recall(goal, project=project, limit=limit)
                    or [])[:limit]:
            snip = (hit.get("snippet") or "").strip()
            if snip:
                out.append(snip[:180])
    except Exception:
        pass
    return out


# The dashboard's singleton controller (mirrors workspace.MANAGER).
CONTROLLER: ResearchController | None = None
_CTL_LOCK = threading.Lock()


def get_controller() -> ResearchController:
    global CONTROLLER
    with _CTL_LOCK:
        if CONTROLLER is None:
            from . import events as ev_mod, workspace
            tp = wsl_train_py_windows_path()
            train_dir = str(Path(tp).parent) if tp else None
            CONTROLLER = ResearchController(
                trainer_cmd=wsl_trainer_cmd,
                agent_factory=lambda profile, model=None: ManagerResearcher(
                    profile=profile, model=model, add_dir=train_dir),
                train_path=tp,
                append_event=ev_mod.append_event,
                preflight=default_preflight,
                profiles=[e["name"] for e in workspace.load_profiles()],
                auth_status=workspace.profile_auth_status,
                on_kill=kill_wsl_training)
        return CONTROLLER


# --- dashboard API dispatch (one entry point per verb, like workspace) ------

def api_get(action, proj=None, qs=None) -> dict:
    qs = qs or {}
    ctl = get_controller()
    if action == "status":
        return {"ok": True, **ctl.status()}
    if action == "leaderboard":
        return {"ok": True, "experiments": ctl.leaderboard.ranked(),
                "best": ctl.leaderboard.best(), "tally": ctl.skill_tally()}
    if action == "output":
        return ctl.output(since=int((qs.get("since") or ["0"])[0]))
    if action == "learnings":
        return {"ok": True, "learnings": ctl.learnings_text()}
    return {"ok": False, "error": "not found"}


def api_post(action, proj=None, body=None) -> dict:
    body = body or {}
    ctl = get_controller()
    if action == "start":
        goal = body.get("goal", "")
        return ctl.start(goal, skills=body.get("skills"),
                         max_experiments=body.get("max_experiments"),
                         time_budget_s=body.get("time_budget_s"),
                         failover=bool(body.get("failover")),
                         profile=body.get("profile", "default"),
                         model=body.get("model"),
                         mode=body.get("mode", "improve"),
                         primed=_primed_learnings(goal, proj))
    if action == "stop":
        return ctl.stop()
    return {"ok": False, "error": "not found"}
