# src/mindhive/loop/driver.py
import json
import shutil
import subprocess
from dataclasses import dataclass, field


@dataclass
class Usage:
    input_tokens: int = 0
    output_tokens: int = 0
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    cost_usd: float = 0.0

    @property
    def total_tokens(self) -> int:
        return (self.input_tokens + self.output_tokens
                + self.cache_creation_input_tokens + self.cache_read_input_tokens)

    @property
    def context_occupancy(self) -> int:
        return self.input_tokens + self.cache_read_input_tokens


@dataclass
class DriverResult:
    text: str
    usage: Usage
    session_id: str | None
    num_turns: int
    rate_limited: bool
    raw: dict = field(default_factory=dict)


def preflight(cmd: str = "claude") -> bool:
    """True if the CLI is on PATH."""
    return shutil.which(cmd) is not None


def build_argv(cmd, prompt, loop_context, allowed_tools, output_format,
               session_id, model, effort):
    argv = [cmd, "--output-format", output_format]
    if model:
        argv += ["--model", model]
    if effort:
        argv += ["--effort", effort]
    if allowed_tools:
        argv += ["--allowedTools", allowed_tools]
    if session_id:
        # --resume with an explicit id, never --continue (which can hijack the
        # most-recent session in the directory).
        argv += ["--resume", session_id]
    argv += ["--append-system-prompt", loop_context]
    argv += ["-p", prompt]
    return argv


def parse_result(stdout: str) -> DriverResult:
    stdout = stdout.strip()
    try:
        obj = json.loads(stdout)
    except (json.JSONDecodeError, ValueError):
        return DriverResult(text=stdout, usage=Usage(), session_id=None,
                            num_turns=0, rate_limited=False, raw={})
    u = obj.get("usage", {}) or {}
    usage = Usage(
        input_tokens=u.get("input_tokens", 0),
        output_tokens=u.get("output_tokens", 0),
        cache_creation_input_tokens=u.get("cache_creation_input_tokens", 0),
        cache_read_input_tokens=u.get("cache_read_input_tokens", 0),
        cost_usd=obj.get("total_cost_usd", 0.0),
    )
    rate_limited = (
        "rate_limit_event" in obj
        and isinstance(obj.get("rate_limit_event"), dict)
        and obj["rate_limit_event"].get("status") == "rejected"
    )
    return DriverResult(
        text=obj.get("result", "") or "",
        usage=usage,
        session_id=obj.get("session_id"),
        num_turns=obj.get("num_turns", 0),
        rate_limited=rate_limited,
        raw=obj,
    )


class CLIDriver:
    """Real Claude Code CLI driver. Not exercised in unit tests; the engine
    accepts any object with a matching run() for fakes."""

    def __init__(self, cmd: str = "claude"):
        self.cmd = cmd

    def run(self, prompt, loop_context, *, allowed_tools, session_id,
            model, effort, timeout_s, live=False) -> DriverResult:
        output_format = "stream-json" if live else "json"
        argv = build_argv(self.cmd, prompt, loop_context, allowed_tools,
                          output_format, session_id, model, effort)
        proc = subprocess.run(
            argv, capture_output=True, text=True, timeout=timeout_s,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
        )
        return parse_result(proc.stdout or proc.stderr)
