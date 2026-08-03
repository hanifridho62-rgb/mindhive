"""Empirical gate for smart-zone session rotation.

    py scripts/rotation/empirical_gate.py

The unit suite proves the loop's *decisions*. It cannot prove that the real
PowerShell shim runs, that the real `claude` binary launches under inherited
stdio, or that a real hook process and a real supervisor process agree about a
file on disk. Those are separate processes and a separate class of failure, so
they get a separate gate — this one.

Costs nothing: no API calls. `claude --version` exits immediately, and the
"sessions" in gates 5 and 6 are real subprocesses that invoke the real hook
rather than a real model.

Isolated: MINDHIVE_HOME points at a throwaway directory, so the live ledger is
never touched.

What this does NOT prove, and no unattended script can: that the full-screen
interactive TUI renders correctly and accepts keystrokes. That needs a human at
a terminal running `hive session run`.

Exit code is 0 only if every gate passes.
"""
import json
import os
import subprocess
import sys
import tempfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
PY = sys.executable

sys.path.insert(0, str(REPO / "src"))

HOME = Path(tempfile.mkdtemp(prefix="rotation-gate-"))
os.environ["MINDHIVE_HOME"] = str(HOME)
os.environ["MINDHIVE_PROJECT"] = "empirical"
os.environ["PYTHONPATH"] = str(REPO / "src")

from mindhive.rotation import artifact, supervisor      # noqa: E402

results = []


def check(name, ok, detail=""):
    results.append((name, ok, detail))
    print(f"{'PASS' if ok else 'FAIL'}  {name}" + (f"\n        {detail}" if detail else ""))


# A transcript in the real shape, carrying the real measured token split: with
# prompt caching on, input_tokens is 2 and the conversation is in the cache
# fields. Occupancy must come out at 203_820, not 198_994.
transcript = HOME / "transcript.jsonl"
transcript.write_text(json.dumps({
    "type": "assistant", "isSidechain": False,
    "message": {"model": "claude-opus-5", "usage": {
        "input_tokens": 2, "cache_creation_input_tokens": 4826,
        "cache_read_input_tokens": 198992, "output_tokens": 661}},
}) + "\n", encoding="utf-8")

payload = json.dumps({
    "session_id": "empirical-1", "transcript_path": str(transcript),
    "hook_event_name": "Stop", "stop_hook_active": False,
})
SHIM = str(REPO / "hooks" / "stop.ps1")


def run_shim(env_extra, drop=()):
    env = dict(os.environ, **env_extra)
    for key in ("MINDHIVE_HEADLESS", *drop):
        env.pop(key, None)
    return subprocess.run(["powershell", "-NoProfile", "-File", SHIM],
                          input=payload, capture_output=True, text=True,
                          env=env, timeout=120)


# ---------------------------------------------------------------- GATES 1-2
proc = run_shim({"MINDHIVE_ROTATION": "1", "MINDHIVE_ROTATION_CAP": "1000"})
saved = artifact.read()
try:
    printed = json.loads(proc.stdout.strip())
except Exception:                                            # noqa: BLE001
    printed = None

check("real .ps1 shim fires the hook and writes the artifact",
      proc.returncode == 0 and saved is not None
      and saved["occupancy"] == 203820 and saved["threshold"] == 1000
      and printed is not None
      and printed["hookSpecificOutput"]["hookEventName"] == "Stop",
      f"exit={proc.returncode} artifact={saved} stderr={proc.stderr.strip()[:120]!r}")

check("the hook never emits a `decision` (block would mean KEEP GOING)",
      printed is not None and "decision" not in printed,
      f"keys={list(printed) if printed else None}")

# ---------------------------------------------------------------- GATE 3
artifact.clear()
off = run_shim({"MINDHIVE_ROTATION_CAP": "1000"}, drop=("MINDHIVE_ROTATION",))
check("hook is inert when MINDHIVE_ROTATION is unset",
      off.returncode == 0 and off.stdout.strip() == "" and artifact.read() is None,
      f"exit={off.returncode} stdout={off.stdout.strip()[:120]!r}")

# ---------------------------------------------------------------- GATE 3b
# The shim is registered globally the moment you run `install-hook`, but the
# backend it calls only gains the `stop` stage when this branch merges. Claude
# Code surfaces hook failures to the user, so an unguarded shim would print a
# failure line at the end of EVERY turn in EVERY session until then. Point it at
# a PYTHONPATH with no mindhive to reproduce exactly that, and require silence.
artifact.clear()
blind = subprocess.run(
    ["powershell", "-NoProfile", "-File", SHIM],
    input=payload, capture_output=True, text=True, timeout=120,
    env={**os.environ, "MINDHIVE_ROTATION": "1", "MINDHIVE_ROTATION_CAP": "1000",
         "PYTHONPATH": str(HOME / "no-mindhive-here")},
)
check("shim stays silent when the backend cannot answer (pre-merge state)",
      blind.returncode == 0 and blind.stdout.strip() == ""
      and blind.stderr.strip() == "",
      f"exit={blind.returncode} stdout={blind.stdout.strip()[:80]!r} "
      f"stderr={blind.stderr.strip()[:80]!r}")

# ---------------------------------------------------------------- GATE 4
# resolve_exe + Popen with no pipes + wait, against the actual binary.
try:
    rc = supervisor.default_spawn(None, extra=["--version"])
    spawn_ok, spawn_detail = rc == 0, f"claude --version exit={rc}"
except Exception as e:                                       # noqa: BLE001
    spawn_ok, spawn_detail = False, f"{type(e).__name__}: {e}"
check("default_spawn launches the real claude binary, stdio inherited",
      spawn_ok, spawn_detail)

# ---------------------------------------------------------------- GATES 5-6
artifact.clear()
child = HOME / "fake_session.py"
child.write_text(
    "import json,os,subprocess,sys\n"
    f"payload = {payload!r}\n"
    "n = int(os.environ.get('ROT_N','0'))\n"
    "if n < 2:\n"
    f"    subprocess.run([{PY!r},'-m','mindhive','hook','stop'], input=payload,\n"
    "                   text=True, env=dict(os.environ, MINDHIVE_ROTATION='1',\n"
    "                                       MINDHIVE_ROTATION_CAP='1000'))\n"
    "sys.exit(0)\n", encoding="utf-8")

spawned, seeds = [], []


def real_spawn(handoff):
    seeds.append(handoff)
    env = dict(os.environ, ROT_N=str(len(spawned)))
    spawned.append(handoff)
    return subprocess.run([PY, str(child)], env=env, timeout=180).returncode


result = supervisor.run(spawn=real_spawn,
                        reseed=lambda p: f"seeded-from-{p['session_id']}",
                        now=lambda: len(spawned) * 3600.0)
check("hook process -> artifact -> supervisor -> reseeded relaunch",
      len(spawned) == 3 and result.rotations == 2
      and result.reason == "user_quit" and seeds[0] is None
      and seeds[1] == "seeded-from-empirical-1",
      f"spawns={len(spawned)} rotations={result.rotations} "
      f"reason={result.reason} seeds={seeds}")

artifact.clear()
spun = []


def always_rotate(handoff):
    spun.append(handoff)
    return subprocess.run([PY, str(child)], env=dict(os.environ, ROT_N="0"),
                          timeout=180).returncode


guard = supervisor.run(spawn=always_rotate, reseed=lambda p: "x",
                       now=lambda: len(spun) * 10.0, max_rotations=3, window_s=180)
check("spin guard trips on a real rotation spiral and reports its numbers",
      guard.reason == "spin_guard" and guard.exit_code != 0 and len(spun) == 3,
      f"reason={guard.reason} exit={guard.exit_code} detail={guard.detail}")

print("\n" + "=" * 70)
failed = [n for n, ok, _ in results if not ok]
print(f"{len(results) - len(failed)}/{len(results)} gates passed")
if failed:
    print("FAILED: " + "; ".join(failed))
print("\nNOT covered here: that the interactive TUI renders and takes keystrokes.")
print("That needs a human running `hive session run` at a real terminal.")
sys.exit(1 if failed else 0)
