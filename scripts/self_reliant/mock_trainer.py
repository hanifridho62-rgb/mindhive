#!/usr/bin/env python3
"""No-GPU synthetic trainer — the Self-Reliant test seam (plan U2/U3/U4, R12).

Emits a fake ``val_bpb`` (or a failure) after a short delay so the experiment
runner, watchdog, parser, and leaderboard can be exercised without WSL or a GPU.
The real path runs autoresearch's ``train.py`` inside WSL; this stands in for it
so the whole loop is testable on any machine.

Modes:
  ok          print a val_bpb line and exit 0
  oom         print a CUDA-out-of-memory line and exit 1
  crash       print a traceback-ish line and exit 2
  noscore     exit 0 but print no score line (garbled/incomplete run)
  hang        sleep effectively forever (forces the watchdog to kill)
  grandchild  spawn a long-lived grandchild, record its pid, then hang
              (proves the runner kills the whole process tree, not just us)
"""
import argparse
import subprocess
import sys
import time

_HANG_S = 600.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", default="ok",
                    choices=["ok", "oom", "crash", "noscore", "hang", "grandchild"])
    ap.add_argument("--seconds", type=float, default=0.3)
    ap.add_argument("--val-bpb", type=float, default=1.2345)
    ap.add_argument("--pidfile", default="")
    args = ap.parse_args()

    if args.mode == "grandchild":
        # A long-lived grandchild that must die when the runner kills the tree.
        # Detach its stdio so it never holds the runner's stdout pipe open.
        gc = subprocess.Popen(
            [sys.executable, "-c", "import time; time.sleep(600)"],
            stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
            stdin=subprocess.DEVNULL)
        if args.pidfile:
            with open(args.pidfile, "w", encoding="utf-8") as fh:
                fh.write(str(gc.pid))
        time.sleep(_HANG_S)                 # parent hangs -> watchdog must kill
        return 0

    if args.mode == "hang":
        time.sleep(_HANG_S)
        return 0

    time.sleep(args.seconds)

    if args.mode == "oom":
        print("RuntimeError: CUDA out of memory. Tried to allocate 512.00 MiB",
              flush=True)
        return 1
    if args.mode == "crash":
        print("Traceback (most recent call last): RuntimeError: boom", flush=True)
        return 2
    if args.mode == "noscore":
        print("step 100/100 | training complete (no score line)", flush=True)
        return 0

    print(f"step 100/100 | val_loss 1.10 | val_bpb {args.val_bpb:.4f}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
