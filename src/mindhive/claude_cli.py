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
import shutil
import subprocess

MODEL = "claude-sonnet-4-6"


def resolve_exe(cmd: str = "claude") -> str:
    """Full path to the CLI. On Windows `claude` is a .CMD shim that
    subprocess.run(["claude", ...]) (no shell) can't launch by the bare name;
    shutil.which returns the extensioned path there and the binary on POSIX."""
    return shutil.which(cmd) or cmd


def run_headless(prompt: str, model: str = MODEL, timeout_s: int = 180) -> dict:
    """One-shot `claude --output-format json -p <prompt>`. Returns
    ``{"exit": int, "stdout": str}``. Spawn failures map to conventional exit
    codes (127 not-found, 124 timeout) so callers treat them uniformly."""
    argv = [resolve_exe(), "--output-format", "json", "--model", model, "-p", prompt]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s,
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
