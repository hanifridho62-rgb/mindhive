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


def _child_env(extra):
    """``os.environ`` plus *extra*, with the recursion guard forced LAST.

    Merged, never replaced: a bare replacement drops ``PATH`` and — on Windows —
    ``SYSTEMROOT``, and a child with no ``SYSTEMROOT`` does not start at all
    (`mindnet/executor._child_env` learned the same thing). A non-mapping argument
    is ignored rather than raised.

    ``MINDHIVE_HEADLESS`` is applied after the overlay so **no caller can turn the
    guard off**, deliberately or by accident. Every ``hooks/*.ps1`` exits
    immediately when it is set; a child that re-fires the hooks re-enters this
    spawn path, and the failure mode of that is an unbounded spawn loop rather
    than a wrong answer."""
    merged = dict(os.environ)
    if isinstance(extra, dict):
        for key, value in extra.items():
            if value is None:
                merged.pop(str(key), None)
            else:
                merged[str(key)] = str(value)
    merged["MINDHIVE_HEADLESS"] = "1"
    # The SOP process token never reaches a headless child either (blueprint
    # P1d): withheld after the overlay, like the guard above. Imported here so
    # this module stays a stdlib-only leaf at import time.
    from . import advice as authorship
    return authorship.scrub_agent_env(merged)


def _envelope_extras(stdout: str) -> dict:
    """``{"usage": dict|None, "session_id": str|None}`` read off the envelope.

    Both are ``None`` unless the envelope parses AND reports success. An
    ``is_error`` envelope means the CLI ran and the model never worked, so
    there is no honest usage figure to report — and zeros presented as fact
    would read as "this session is carrying no context", suppressing a
    rotation that may well be due."""
    obj = _parse(stdout)
    if obj is None or obj.get("is_error"):
        return {"usage": None, "session_id": None}
    usage = obj.get("usage")
    sid = obj.get("session_id")
    return {"usage": usage if isinstance(usage, dict) else None,
            "session_id": str(sid) if sid else None}


def run_headless(prompt: str, model: str | None = MODEL, timeout_s: int = 180,
                 permission_mode: str | None = None,
                 cwd: str | None = None, env: dict | None = None,
                 session_id: str | None = None, spawn=None) -> dict:
    """One-shot `claude --output-format json -p` with the prompt fed via stdin.

    Returns ``{"exit": int, "stdout": str, "usage": dict|None,
    "session_id": str|None}``. Spawn failures map to conventional exit codes
    (127 not-found, 124 timeout) so callers treat them uniformly, and carry the
    same four keys so no caller needs a branch for which failure it was.

    ``usage`` and ``session_id`` exist for the smart-zone gate: without the
    first, nothing could see how much context a run was carrying, and without
    the second there was no session to rotate FROM. ``usage`` is handed back as
    the raw envelope mapping rather than a typed shape, deliberately — the loop
    driver already owns a ``Usage`` dataclass for its own richer argv, and a
    second copy here is how the two begin to disagree. Callers compute
    occupancy with `loop.context.occupancy_from`.

    ``session_id`` resumes an existing conversation with ``--resume <id>``,
    always by explicit id and never ``--continue``: continue can hijack the
    most-recent session in the directory, which in a fleet of concurrent
    worktree agents means one task silently resuming another's conversation.
    A blank or whitespace-only id is treated as absent.

    ``model=None`` omits the flag so the CLI's own configured default decides
    (the auto-resume delivery wants the user's model, not the reflection
    alias); ``permission_mode`` lets a delivery run actually act (headless has
    no interactive approval), and ``cwd`` pins where that work happens.

    ``env`` is an overlay on the child's environment — MN-9's orchestrator uses it
    to give each concurrent agent its own ``MINDHIVE_SESSION``, so each agent's
    rows carry its own id in its own ``<session_id>.jsonl``. That is for
    attribution: appends are serialised by `events._append_lock`, so a shared
    segment would not interleave. See
    `_child_env` for the merge rules and why the recursion guard is applied last.

    ``spawn`` replaces `subprocess.run` for this one call, and exists so a
    caller can put a real resource ceiling around the child without this module
    learning what a ceiling is. `orchestrator.fence.Fence.spawn` is the only
    implementation: it holds the child in a Windows job object (or POSIX
    rlimits) and, on a timeout, kills the whole tree rather than the child
    alone. Before it, a lintester mission could report `enforcement=kernel`
    while this line ran a completely unfenced `claude`.

    The default resolves `subprocess.run` HERE, at call time, rather than
    binding it as a parameter default at import time. That is not a style
    choice: a dozen tests in `tests/test_claude_cli.py` and elsewhere patch the
    module attribute (`claude_cli.subprocess.run`, which is `subprocess.run`),
    and an import-time default would have frozen the real function into the
    signature where no patch could reach it — the whole suite would keep
    passing while proving nothing.

    Anything with `subprocess.run`'s call shape works; the exceptions below are
    caught from it exactly as they are from the stdlib, so a fenced spawn maps
    its own faults to the same 127/124 every caller already handles.

    The prompt must never ride in argv: on Windows the CLI resolves to a .CMD
    shim that cmd.exe hosts, and cmd.exe rejects command lines past ~8k chars
    ("The command line is too long."), killing long composed prompts. stdin has
    no such limit, and `claude -p` reads the prompt from stdin when no
    positional prompt is given."""
    argv = [resolve_exe(), "--output-format", "json"]
    if model:
        argv += ["--model", model]
    if permission_mode:
        argv += ["--permission-mode", permission_mode]
    if session_id and str(session_id).strip():
        argv += ["--resume", str(session_id).strip()]
    argv += ["-p"]
    # Recursion guard (KTD3): every hooks/*.ps1 exits immediately when this
    # variable is set, so a headless session spawned here can never re-fire the
    # hooks (no reflect->session_end->reflect loop, no phantom session events).
    # `_child_env` forces it on AFTER any caller overlay, so it cannot be unset.
    child_env = _child_env(env)
    try:
        # `is None`, not `or`: every other seam in this chain tests absence that
        # way, and a callable object that defines a falsy `__bool__` would be
        # silently discarded by `or` — the caller having asked for a ceiling and
        # got none, which is the one outcome this parameter exists to prevent.
        run = subprocess.run if spawn is None else spawn
        proc = run(argv, capture_output=True, text=True, timeout=timeout_s,
                   env=child_env, input=prompt, cwd=cwd or None,
                   creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        out = proc.stdout or ""
        return {"exit": proc.returncode, "stdout": out, **_envelope_extras(out)}
    except FileNotFoundError:
        # claude CLI not installed
        return {"exit": 127, "stdout": "", "usage": None, "session_id": None}
    except subprocess.TimeoutExpired:
        return {"exit": 124, "stdout": "", "usage": None, "session_id": None}


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


def turns(stdout: str) -> int:
    """How many turns the child took, 0 when the envelope does not say.

    Deliberately reads an is_error envelope too, unlike `result_text` and
    `_envelope_extras`. Those two refuse an error envelope because there is no
    honest MODEL OUTPUT in one -- but there is an honest turn count, and it is
    the only evidence that separates a limit hit at the first request from one
    hit after the child spent twenty turns editing a tree."""
    obj = _parse(stdout)
    if obj is None:
        return 0
    try:
        return max(0, int(obj.get("num_turns") or 0))
    except (TypeError, ValueError):
        return 0


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
