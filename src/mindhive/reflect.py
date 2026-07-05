# src/mindhive/reflect.py
"""Reflection pass: turn one incident into a structured prevention rule.

Pure helpers (build_prompt/parse_reflection) are unit-tested. reflect_incident
calls an injectable runner; the default shells out to the shared headless
`claude` helper (claude_cli) and is NOT unit-tested.
"""
import json

from . import claude_cli

_SYSTEM = (
    "You are the Reflection Engine. Analyze ONE execution failure and output ONLY "
    "a JSON object — no prose, no code fences — with keys: root_cause, lesson, "
    "prevention_rule, task_category. task_category is one of: architecture, syntax, "
    "git_merge, engine_logic, dependency, tool_use. prevention_rule is a single "
    "imperative sentence a future agent can follow to avoid this failure."
)


def build_prompt(signature, error_excerpt, actions) -> str:
    acts = "\n".join(f"- {a}" for a in (actions or [])) or "- (unknown)"
    return (f"{_SYSTEM}\n\nSIGNATURE: {signature}\nFAILING ACTIONS:\n{acts}\n"
            f"ERROR:\n{error_excerpt}\n\nReturn the JSON now.")


def parse_reflection(text: str) -> dict:
    """Extract the JSON verdict from a model response, tolerating fences/prose."""
    if not text:
        return {}
    s, e = text.find("{"), text.rfind("}")
    if s == -1 or e == -1 or e <= s:
        return {}
    try:
        obj = json.loads(text[s:e + 1])
    except (json.JSONDecodeError, ValueError):
        return {}
    return obj if isinstance(obj, dict) else {}


def _claude_runner(prompt: str, timeout_s: int = 120) -> str:
    """Default runner: one-shot headless `claude` call, returns the result text
    (empty on any spawn failure or an is_error envelope)."""
    return claude_cli.result_text(
        claude_cli.run_headless(prompt, timeout_s=timeout_s)["stdout"])


from . import incidents


def _auto_section(verdict: dict) -> str:
    return (f"- Root cause: {verdict.get('root_cause', '').strip()}\n"
            f"- Lesson: {verdict.get('lesson', '').strip()}\n"
            f"- Prevention rule: {verdict.get('prevention_rule', '').strip()}\n"
            f"- Category: {verdict.get('task_category', '').strip()}")


def reflect_incident(signature, project=None, runner=_claude_runner) -> dict:
    """Reflect one incident: build a prompt from its Raw Signal, run the model,
    write the Auto Evaluation section, and re-derive the Active rule."""
    d = incidents._incident_dir(project)
    path = d / f"{incidents._safe_name(signature)}.md"
    if not path.exists():
        return {"reflected": False, "reason": "no such incident"}
    secs = incidents._split_sections(path.read_text(encoding="utf-8"))
    raw = secs.get("Raw Signal", "")
    verdict = parse_reflection(runner(build_prompt(signature, raw, [])))
    if not verdict.get("prevention_rule"):
        return {"reflected": False, "reason": "empty verdict"}
    # Hold the incident write guard across re-read → replace → write → project so
    # no other writer — another thread OR another process (a `hive rules reflect`
    # CLI run, a second dashboard) — can clobber this verdict mid-flight. The
    # model call above is deliberately OUTSIDE the guard — it can take 120s.
    with incidents._write_guard(project):
        txt = incidents._replace_section(path.read_text(encoding="utf-8"),
                                         "Auto Evaluation", _auto_section(verdict))
        incidents._atomic_write(path, txt)
        incidents.project(project)      # re-derive Active + rule_source, preserve sections
    return {"reflected": True}
