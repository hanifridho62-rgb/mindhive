# src/mindhive/loop/scaffold.py
from pathlib import Path

_PROMPT = "# Loop goal\n\nDescribe what the loop should accomplish, end to end.\n"
_FIX_PLAN = ("# Fix plan\n\n- [ ] First concrete step\n- [ ] Second step\n\n"
             "## Future / Optional\n- [ ] Nice-to-haves that must NOT block exit\n")
_AGENT = "# Build / test / run\n\nrun tests: <command>\nbuild: <command>\n"
_TOML = """\
[model]
# Fallback window, used only when the running model's id is not recognised.
context_window = 1000000
smart_zone_ratio = 0.5
# Smart-zone ceiling in absolute tokens. The effective ceiling is the smaller
# of this and smart_zone_ratio x the model's real window. 0 disables this bound.
absolute_ceiling = 500000

[budget]
window_hours = 5
window_token_budget = 0
window_cost_budget_usd = 0
on_cap = "pause"

[loop]
max_iterations = 50
max_hours = 8
allowed_tools = "Write,Read,Edit,Bash(git *),Bash(npm *)"
iteration_timeout_minutes = 15
# Headless permission posture: "acceptEdits" auto-approves file edits (safe
# autonomy), "bypassPermissions" approves any tool, "" relies on allowed_tools.
permission_mode = "acceptEdits"

[breaker]
no_progress_threshold = 3
same_error_threshold = 5
cooldown_minutes = 30

[replenish]
# Adaptive work injection: when this surface would otherwise stop, mine the
# ledger for work the record still proves outstanding and land it as required
# work. Every candidate must point at a real ledger row, file or signature.
enabled = true
# How bold the injector is. One dial, because the threshold, the cap and the
# active lens set are not independent:
#   conservative - score >= 40, at most 3 per round, repair + evidence-gap only
#   balanced     - score >= 25, at most 10 per round, all deterministic lenses
#   ambitious    - score >= 15, at most 25 per round, plus the model-driven lens
ambition = "balanced"
"""

_FILES = {"PROMPT.md": _PROMPT, "fix_plan.md": _FIX_PLAN,
          "AGENT.md": _AGENT, "loop.toml": _TOML}


def scaffold(loop_dir: Path) -> list[str]:
    """Create .hive-loop/ template files that don't already exist. Returns the
    list of created file names."""
    loop_dir = Path(loop_dir)
    loop_dir.mkdir(parents=True, exist_ok=True)
    created = []
    for name, body in _FILES.items():
        p = loop_dir / name
        if not p.exists():
            p.write_text(body, encoding="utf-8")
            created.append(name)
    return created
