# src/mindhive/claude_cli.py
"""Shared headless `claude` invocation — one home for what the invoke channel
(invoker.py) and the reflection engine (reflect.py) both need: resolving the
CLI executable, spawning a one-shot `-p` call, and reading the
`--output-format json` envelope.

The loop driver (loop/driver.py) deliberately does NOT use this: it needs a
richer argv (stream-json, --resume, --permission-mode, --allowedTools), so its
runner stays its own.
"""
import json
import os
import shutil
import subprocess

# Use the CLI's rolling alias, not a dated id: `--model sonnet` always resolves
# to the current Sonnet, so the reflection engine never silently 404s the way a
# pinned "claude-sonnet-4-6" would once that snapshot is retired.
MODEL = "sonnet"


def resolve_exe(cmd: str = "claude") -> str:
    """Full path to the CLI. On Windows `claude` is a .CMD shim that
    subprocess.run(["claude", ...]) (no shell) can't launch by the bare name;
    shutil.which returns the extensioned path there and the binary on POSIX."""
    return shutil.which(cmd) or cmd


def run_headless(prompt: str, model: str = MODEL, timeout_s: int = 180) -> dict:
    """One-shot `claude --output-format json -p` with the prompt fed via stdin.
    Returns ``{"exit": int, "stdout": str}``. Spawn failures map to conventional
    exit codes (127 not-found, 124 timeout) so callers treat them uniformly.

    The prompt must never ride in argv: on Windows the CLI resolves to a .CMD
    shim that cmd.exe hosts, and cmd.exe rejects command lines past ~8k chars
    ("The command line is too long."), killing long composed prompts. stdin has
    no such limit, and `claude -p` reads the prompt from stdin when no
    positional prompt is given."""
    argv = [resolve_exe(), "--output-format", "json", "--model", model, "-p"]
    # Recursion guard (KTD3): every hooks/*.ps1 exits immediately when this
    # variable is set, so a headless session spawned here can never re-fire the
    # hooks (no reflect->session_end->reflect loop, no phantom session events).
    env = dict(os.environ, MINDHIVE_HEADLESS="1")
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s,
                              env=env, input=prompt,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"exit": proc.returncode, "stdout": proc.stdout or ""}
    except FileNotFoundError:
        return {"exit": 127, "stdout": ""}          # claude CLI not installed
    except subprocess.TimeoutExpired:
        return {"exit": 124, "stdout": ""}


def _parse(stdout: str):
    try:
        obj = json.loads((stdout or "").strip())
    except (json.JSONDecodeError, ValueError):
        return None
    return obj if isinstance(obj, dict) else None


def envelope_ok(stdout: str) -> bool:
    """True only when the envelope parses and does not report is_error (a
    401/auth failure sets is_error even on a zero exit)."""
    obj = _parse(stdout)
    return obj is not None and not obj.get("is_error")


def result_text(stdout: str) -> str:
    """The model's result text, or "" when the envelope is missing or is_error."""
    obj = _parse(stdout)
    return "" if obj is None or obj.get("is_error") else (obj.get("result", "") or "")


def error_text(stdout: str) -> str:
    """Why an is_error envelope failed, for health reporting: the api status
    plus the CLI's own message (e.g. "429: You've hit your weekly limit …").
    Empty string when the envelope is missing or not an error — the envelope's
    error message must never be mistaken for model output."""
    obj = _parse(stdout)
    if obj is None or not obj.get("is_error"):
        return ""
    status = obj.get("api_error_status")
    msg = str(obj.get("result") or obj.get("error") or "").strip()
    if status and msg:
        return f"{status}: {msg}"
    return msg or (f"api error {status}" if status else "is_error envelope")
