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

from . import claude_cli, events as ev_mod


def build_prompt(name: str, plugin: str | None = None) -> str:
    src = f" (from the {plugin} plugin)" if plugin else ""
    return (f"Use the {name} skill{src} now. Follow it to completion. "
            f"If the skill cannot be found or fails, say so plainly.")


def _excerpt(stdout: str, limit: int = 300) -> str:
    """A short human excerpt for the ledger — the model's result if present,
    else the raw stdout. Unlike claude_cli.result_text this keeps the result
    text even on an is_error envelope (so a 401 message still shows in history)."""
    try:
        obj = json.loads((stdout or "").strip())
        text = obj.get("result", "") if isinstance(obj, dict) else ""
    except (json.JSONDecodeError, ValueError):
        text = stdout or ""
    return (text or "").strip()[:limit]


def invoke_skill(name, plugin=None, runner=None, project=None,
                 session_id=None) -> dict:
    """Invoke one skill headlessly and record the outcome. Returns
    ``{ok, exit, excerpt}``. ``runner`` is injectable for tests."""
    run = runner or claude_cli.run_headless
    prompt = build_prompt(name, plugin)
    out = run(prompt)
    exit_code = int(out.get("exit", 1))
    stdout = out.get("stdout", "")
    ok = exit_code == 0 and claude_cli.envelope_ok(stdout)
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
