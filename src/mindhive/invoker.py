# src/mindhive/invoker.py
"""Invocation channel: make any installed skill/plugin callable from the
dashboard by shelling out to a headless `claude` (the pattern reflect.py uses).

The dashboard (a plain Python process) cannot call the in-agent `Skill` tool, so
"call this skill" is realized as a one-shot headless Claude that is told to use
the skill. Every outcome is written to the ledger:

  * success  -> an ``invoke_result`` event (ok=True)
  * failure  -> a ``tool_failed`` event with signature ``skill:<name>``

The ``tool_failed`` signature is exactly what ``incidents.py`` audits and
``reflect.py`` learns from, so a failed call flows into the Improvements loop
with zero extra plumbing. Status derivation (green/red) reads these two event
types back — see ``plugins.call_status``.
"""
import json
import subprocess

from . import events as ev_mod

MODEL = "claude-sonnet-4-6"


def build_prompt(name: str, plugin: str | None = None) -> str:
    src = f" (from the {plugin} plugin)" if plugin else ""
    return (f"Use the {name} skill{src} now. Follow it to completion. "
            f"If the skill cannot be found or fails, say so plainly.")


def _envelope_ok(stdout: str) -> bool:
    """True only when the `claude --output-format json` envelope parses and does
    not report is_error (a 401/auth failure sets is_error even on exit 0)."""
    try:
        obj = json.loads((stdout or "").strip())
    except (json.JSONDecodeError, ValueError):
        return False
    return isinstance(obj, dict) and not obj.get("is_error")


def _excerpt(stdout: str, limit: int = 300) -> str:
    try:
        obj = json.loads((stdout or "").strip())
        text = obj.get("result", "") if isinstance(obj, dict) else ""
    except (json.JSONDecodeError, ValueError):
        text = stdout or ""
    return (text or "").strip()[:limit]


def _default_runner(prompt: str, timeout_s: int = 180) -> dict:
    argv = ["claude", "--output-format", "json", "--model", MODEL, "-p", prompt]
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, timeout=timeout_s,
                              creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        return {"exit": proc.returncode, "stdout": proc.stdout or ""}
    except FileNotFoundError:
        return {"exit": 127, "stdout": ""}          # claude CLI not installed
    except subprocess.TimeoutExpired:
        return {"exit": 124, "stdout": ""}


def invoke_skill(name, plugin=None, runner=None, project=None,
                 session_id=None) -> dict:
    """Invoke one skill headlessly and record the outcome. Returns
    ``{ok, exit, excerpt}``. ``runner`` is injectable for tests."""
    run = runner or _default_runner
    prompt = build_prompt(name, plugin)
    out = run(prompt)
    exit_code = int(out.get("exit", 1))
    stdout = out.get("stdout", "")
    ok = exit_code == 0 and _envelope_ok(stdout)
    excerpt = _excerpt(stdout)

    if ok:
        ev_mod.append_event(
            "invoke_result",
            {"name": name, "plugin": plugin, "ok": True, "text": excerpt},
            session_id=session_id, tags=["invoke", "skill"], project=project)
    else:
        ev_mod.append_event(
            "tool_failed",
            {"text": excerpt or f"invoke of {name} failed (exit {exit_code})",
             "signature": f"skill:{name}",
             "action": f"invoke skill {name}" + (f" ({plugin})" if plugin else "")},
            session_id=session_id, tags=["invoke", "skill", "failure"],
            project=project)
    return {"ok": ok, "exit": exit_code, "excerpt": excerpt}
