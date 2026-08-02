# src/mindhive/workspace.py
"""Workspace backend: parallel Claude Code agents feeding the Mind Hive ledger.

One Manager owns up to MAX_AGENTS agents. Each agent is a headless `claude`
subprocess (`-p --output-format stream-json --verbose`) whose stdout is read on
a daemon thread into an in-memory line buffer the dashboard polls. Follow-up
prompts resume the same CLI session via --resume, so a pane behaves like a
persistent terminal without needing a PTY or websockets.

Multiple Claude Code accounts: a profile maps a name to a CLAUDE_CONFIG_DIR;
each agent runs under its profile's config dir, so differently-authenticated
CLIs run side by side.

Every exchange lands in the ledger: the prompt as a ``user_prompt`` event, the
final result as ``assistant_response``, and a failed run as ``tool_failed``
with signature ``workspace:<role>`` — so workspace activity flows into Memory,
the graph, and the Improvements loop with zero extra plumbing.

Output durability: the line buffer is a live VIEW that forgets its head at
MAX_LINES and dies with the process, so every run also streams into a durable
per-run artifact (runlog.py). All three exit paths — clean, failed, user-stopped
— finalise that artifact and record a bounded excerpt plus its path, so no run
can finish (or die) with its output written nowhere.

MindSwarm reuses the same agents: a mission spawns a coordinator plus builders,
scouts, and reviewers, each with a role lens appended to its system prompt that
also tells it to read/write the shared Mind Hive memory (`hive recall` /
`hive append`) — what one agent learns, the next one starts with.

A running mission also lights MN-5's faculty cat board, under `SWARM_FACULTY`.
That producer is edge-triggered on the fleet's AGGREGATE state — one row when
the swarm starts working and one when it stops, never one per agent — and
`Manager._sync_faculty` carries the reasoning for why the distinction is the
requirement rather than a tidiness preference.
"""
from __future__ import annotations

import json
import os
import re
import secrets
import subprocess
import threading
import time

from . import claude_cli, events as ev_mod, paths, runlog

# Profile names end up interpolated into a Windows `cmd /k` string by
# profile_login, so restrict them to an identifier-safe charset — no shell
# metacharacters (& | < > " etc.) can reach the console. Enforced at save time
# and re-checked before any login launch.
_SAFE_PROFILE = re.compile(r"^[A-Za-z0-9 _.\-]{1,64}$")

MAX_AGENTS = 16
MAX_LINES = 4000                 # per-agent scrollback cap (oldest dropped)

# The MN-5 faculty a running swarm lights on the cat board.
#
# `task-graph`, not a new name. `telemetry.FACULTIES` is the one legend
# (invariant 10) and it is pinned three ways — an independent literal in
# `test_mindnet_telemetry.py`, `events.py`'s registration comment, and the
# architecture decomposition spec — so growing it to accommodate a UI fix would
# mean editing a founding document. `orchestrator._WAVE_FACULTY` already settled
# the same question for the same shape: an orchestrated wave of parallel
# execution IS walking the graph, and a mission — a coordinator decomposing work
# across builders, scouts and reviewers — is that shape.
#
# Membership in the legend is asserted by the suite rather than trusted here. A
# name outside it is refused by `telemetry._payload` SILENTLY: no row lands and
# the cat simply never appears, which R16 reads as a bug in the faculty rather
# than a typo in the caller.
SWARM_FACULTY = "task-graph"
RUN_TIMEOUT_S = int(os.environ.get("MINDHIVE_WS_TIMEOUT", "1800"))

# Per-pane model picker: the ids the headless CLI's --model flag accepts.
# "" = account default (no flag). The UI also allows a free-typed id, so new
# models work without a code change.
MODEL_CHOICES = [
    {"id": "", "label": "account default"},
    {"id": "claude-fable-5", "label": "Fable 5"},
    {"id": "claude-opus-4-8", "label": "Opus 4.8"},
    {"id": "claude-opus-4-7", "label": "Opus 4.7"},
    {"id": "claude-opus-4-6", "label": "Opus 4.6"},
    {"id": "claude-sonnet-5", "label": "Sonnet 5"},
    {"id": "claude-haiku-4-5-20251001", "label": "Haiku 4.5"},
]

# Role lenses for MindSwarm agents. Every lens ends with the shared-memory
# contract so each agent reads and writes the same Mind Hive graph.
_SHARED_MEMORY = (
    "You share one persistent memory with every other agent in this swarm: "
    "the Mind Hive ledger. Before starting, run `python -m mindhive recall "
    "\"<key terms>\"` to inherit what other agents already learned. When you "
    "decide, discover, or finish something worth keeping, commit it like "
    "code: `python -m mindhive append decision|experiment_result|caveat "
    "\"<one line>\" --tags swarm`."
)
ROLE_LENSES = {
    "coordinator": ("You are the COORDINATOR of a MindSwarm mission. Break the "
                    "mission into concrete tasks, decide the order, and record "
                    "the plan and each dispatch decision in Mind Hive so the "
                    "builders, scouts, and reviewers can pick them up. "),
    "builder": ("You are a BUILDER in a MindSwarm mission. Implement the "
                "concrete piece of the mission assigned to you; prefer small "
                "verifiable steps. "),
    "scout": ("You are a SCOUT in a MindSwarm mission. Explore, research, and "
              "gather the facts the builders need; report findings, not "
              "opinions. "),
    "reviewer": ("You are a REVIEWER in a MindSwarm mission. Inspect the work "
                 "the builders produced, hunt for defects and gaps, and report "
                 "each finding precisely. "),
    "agent": "",                     # plain MindParallel pane — no lens
}


def _now_iso() -> str:
    return time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"


# --- profiles: named CLAUDE_CONFIG_DIR overrides (multiple accounts) ------

def _profiles_path():
    return paths.store_home() / "workspace_profiles.json"


def load_profiles() -> list:
    """[{name, config_dir}] — always includes the implicit 'default' profile
    (no override: whatever account the plain `claude` CLI is logged into)."""
    entries = [{"name": "default", "config_dir": ""}]
    try:
        raw = json.loads(_profiles_path().read_text(encoding="utf-8"))
        for e in raw:
            if isinstance(e, dict) and e.get("name") and e["name"] != "default":
                entries.append({"name": str(e["name"]),
                                "config_dir": str(e.get("config_dir") or "")})
    except (OSError, ValueError):
        pass
    return entries


def save_profile(name: str, config_dir: str) -> dict:
    name = (name or "").strip()
    if not name or name == "default":
        return {"ok": False, "error": "profile needs a unique name (not 'default')"}
    if not _SAFE_PROFILE.match(name):
        return {"ok": False, "error": "profile name may use only letters, "
                "digits, space, dot, dash, underscore"}
    others = [e for e in load_profiles()
              if e["name"] not in ("default", name)]
    others.append({"name": name, "config_dir": (config_dir or "").strip()})
    p = _profiles_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(others, ensure_ascii=False, indent=1), encoding="utf-8")
    return {"ok": True, "profiles": load_profiles()}


def remove_profile(name: str) -> dict:
    kept = [e for e in load_profiles() if e["name"] not in ("default", name)]
    _profiles_path().write_text(json.dumps(kept, ensure_ascii=False, indent=1),
                                encoding="utf-8")
    return {"ok": True, "profiles": load_profiles()}


def _profile_env(profile: str | None) -> dict:
    env = dict(os.environ)
    for e in load_profiles():
        if e["name"] == profile and e["config_dir"]:
            env["CLAUDE_CONFIG_DIR"] = e["config_dir"]
    return env


def _profile_config_dir(profile: str | None) -> str:
    """The profile's CLAUDE_CONFIG_DIR override, or "" for the system default."""
    for e in load_profiles():
        if e["name"] == profile and e["config_dir"]:
            return e["config_dir"]
    return ""


def _profile_paths(profile: str | None):
    """(credentials.json, .claude.json) for a profile. With no override the CLI
    keeps credentials in ~/.claude/ but its state file at ~/.claude.json; with
    CLAUDE_CONFIG_DIR set, both live inside that directory."""
    from pathlib import Path
    cfg = _profile_config_dir(profile)
    if cfg:
        base = Path(cfg)
        return base / ".credentials.json", base / ".claude.json"
    return Path.home() / ".claude" / ".credentials.json", Path.home() / ".claude.json"


def profile_auth_status(profile: str = "default") -> dict:
    """File-based auth health for one profile — a heuristic that never spends
    tokens: read the OAuth expiry from the profile's credential store. An
    expired token is exactly what a headless run's 401 looks like."""
    creds_path, state_path = _profile_paths(profile)
    out = {"profile": profile, "state": "none",
           "detail": "no credentials — Login to connect an account"}
    try:
        oauth = json.loads(creds_path.read_text(encoding="utf-8")) \
            .get("claudeAiOauth") or {}
        exp = float(oauth.get("expiresAt") or 0) / 1000.0
        sub = oauth.get("subscriptionType") or "account"
        if exp > time.time():
            out.update(state="ok", detail=f"logged in ({sub})")
        else:
            out.update(state="expired",
                       detail="OAuth token expired — headless runs will 401; "
                              "Login to refresh")
    except (OSError, ValueError):
        pass
    if out["state"] == "none":
        try:
            st = json.loads(state_path.read_text(encoding="utf-8"))
            if st.get("oauthAccount") or st.get("primaryApiKey"):
                out.update(state="unknown",
                           detail="account configured but token store unreadable")
        except (OSError, ValueError):
            pass
    if out["state"] == "none" and os.environ.get("ANTHROPIC_API_KEY"):
        out.update(state="ok", detail="ANTHROPIC_API_KEY set")
    return out


def auth_status_all() -> dict:
    return {"ok": True,
            "auth": [profile_auth_status(e["name"]) for e in load_profiles()]}


# --- authoritative auth: ask the CLI itself (no tokens spent) ---------------
# `claude auth status --json` answers from the CLI's own credential machinery,
# so "ok" here means a headless run will actually authenticate — including the
# case the file heuristic can't see: an expired access token the CLI refreshes
# transparently on use. It costs a subprocess (~1-2s), so verify_auth() caches
# it and the cheap file heuristic stays the polling path.

AUTH_VERIFY_TIMEOUT_S = 20
_AUTH_CACHE: dict = {}               # profile -> (checked_epoch, status dict)
_AUTH_CACHE_TTL_S = 60.0
_AUTH_CACHE_LOCK = threading.Lock()


def cli_auth_status(profile: str = "default",
                    timeout_s: float = AUTH_VERIFY_TIMEOUT_S) -> dict:
    """Ground-truth auth for one profile via ``claude auth status --json``
    under its CLAUDE_CONFIG_DIR. States: ``ok`` (logged in, with the account
    email + plan), ``none`` (not logged in), ``error`` (CLI missing/timed
    out/unparseable — the caller falls back to the file heuristic)."""
    env = _profile_env(profile)
    env["MINDHIVE_HEADLESS"] = "1"    # hooks must never fire off a status probe
    try:
        proc = subprocess.run(
            [claude_cli.resolve_exe(), "auth", "status", "--json"],
            capture_output=True, text=True, timeout=timeout_s, env=env,
            creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))
        st = json.loads((proc.stdout or "").strip())
    except (OSError, ValueError, subprocess.SubprocessError) as e:
        return {"profile": profile, "state": "error", "source": "cli",
                "detail": f"could not verify with the claude CLI: {e}"}
    if not isinstance(st, dict):
        return {"profile": profile, "state": "error", "source": "cli",
                "detail": "claude auth status returned no JSON object"}
    if st.get("loggedIn"):
        email = str(st.get("email") or "")
        sub = str(st.get("subscriptionType") or "account")
        return {"profile": profile, "state": "ok", "source": "cli",
                "email": email, "subscription": sub,
                "detail": f"connected as {email or 'account'} ({sub})"}
    return {"profile": profile, "state": "none", "source": "cli",
            "detail": "not logged in — press Connect to sign in via "
                      "your browser"}


def verify_auth(profile: str = "default", force: bool = False) -> dict:
    """Authoritative, cached verification: the CLI itself always answers (it
    can refresh a refreshable token and it knows the account identity, so
    "connected as <email>" renders in every state — a file-heuristic fast path
    would drop the email whenever the credential file happened to look fresh).
    The file heuristic is only the fallback when the CLI is unavailable. The
    verdict is cached for ``_AUTH_CACHE_TTL_S`` so the dashboard can call this
    freely; ``force`` skips the cache (the re-check after a Connect click)."""
    with _AUTH_CACHE_LOCK:
        hit = _AUTH_CACHE.get(profile)
        if hit and not force and time.time() - hit[0] < _AUTH_CACHE_TTL_S:
            return dict(hit[1])
    out = cli_auth_status(profile)
    if out["state"] == "error":
        # CLI unavailable — the file heuristic is the best remaining answer,
        # with the CLI failure noted so the UI can say why identity is missing
        out = dict(profile_auth_status(profile), source="file",
                   cli_error=out["detail"])
    with _AUTH_CACHE_LOCK:
        _AUTH_CACHE[profile] = (time.time(), dict(out))
    return dict(out)


def profile_login(profile: str = "default") -> dict:
    """Zero-typing sign-in: open a console running ``claude auth login`` under
    this profile's CLAUDE_CONFIG_DIR. The CLI drives the OAuth flow itself —
    the browser opens on the claude.ai account the user is already signed into
    and one Approve click connects it. Nothing to type, no /login. Logging in
    stays the user's own act; MindHive only starts the flow."""
    profile = (profile or "default").strip()
    # Only launch for a real, name-validated profile: the name is interpolated
    # into the cmd string below, so an unchecked value (e.g. 'x & calc') would
    # be shell injection. 'default' plus save_profile's charset guard mean any
    # name reaching here is already safe, but re-check rather than trust it.
    if profile != "default" and not any(
            e["name"] == profile for e in load_profiles()):
        return {"ok": False, "error": f"unknown profile: {profile}"}
    if not _SAFE_PROFILE.match(profile):
        return {"ok": False, "error": "profile name has unsafe characters"}
    new_console = getattr(subprocess, "CREATE_NEW_CONSOLE", 0)
    if not new_console:
        return {"ok": False, "error":
                "browser sign-in console is Windows-only — run `claude auth "
                "login` with CLAUDE_CONFIG_DIR set instead"}
    exe = claude_cli.resolve_exe()
    # Pass CLAUDE_CONFIG_DIR through the child environment, never as a shell
    # `set` command — a config_dir with shell metacharacters can't reach cmd.
    env = _profile_env(profile)
    cmdline = (f'echo [MindHive] connecting account "{profile}" - your browser '
               f'will open; approve the sign-in there (nothing to type here) '
               f'&& "{exe}" auth login')
    try:
        subprocess.Popen(["cmd", "/k", cmdline], env=env,
                         creationflags=new_console)
    except OSError as e:
        return {"ok": False, "error": str(e)}
    # a fresh login invalidates whatever verify_auth remembered
    with _AUTH_CACHE_LOCK:
        _AUTH_CACHE.pop(profile, None)
    return {"ok": True, "detail":
            f"browser sign-in started for {profile} — approve it in the "
            "browser, nothing to type"}


_TRUST_LOCK = threading.Lock()


def profile_trust(profile: str = "default", folder: str = "") -> dict:
    """Pre-accept the workspace-trust dialog for `folder` in the profile's
    .claude.json, so headless runs stop ignoring .claude/settings.local.json
    permissions. Both slash spellings are written — the CLI looks projects up
    by the literal cwd string.

    The whole read-modify-write runs under _TRUST_LOCK: two dashboard requests
    trusting different folders at once would otherwise each load the file, add
    only their own key, and the second write would drop the first's."""
    folder = (folder or "").strip() or os.getcwd()
    _, state_path = _profile_paths(profile)
    with _TRUST_LOCK:
        try:
            st = json.loads(state_path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            st = {}
        if not isinstance(st, dict):
            return {"ok": False, "error": f"unreadable state file: {state_path}"}
        projects = st.setdefault("projects", {})
        for key in {folder, folder.replace("\\", "/"), folder.replace("/", "\\")}:
            entry = projects.get(key)
            if isinstance(entry, dict):
                entry["hasTrustDialogAccepted"] = True
            else:
                projects[key] = {"hasTrustDialogAccepted": True}
        tmp = state_path.with_suffix(".json.tmp")
        try:
            tmp.write_text(json.dumps(st, ensure_ascii=False, indent=2),
                           encoding="utf-8")
            os.replace(tmp, state_path)
        except OSError as e:
            return {"ok": False, "error": str(e)}
    return {"ok": True, "detail": f"trusted {folder} for profile {profile}"}


def _folder_for_project(proj: str | None) -> str:
    """The project's registered folder (autosync registry), else the cwd —
    the working directory agent subprocesses run in."""
    from . import autosync
    for e in autosync.load_registry():
        if e.get("project") == proj and e.get("folder"):
            return e["folder"]
    return os.getcwd()


# --- stream-json -> human lines -------------------------------------------

def _compact(obj, limit=110) -> str:
    try:
        s = json.dumps(obj, ensure_ascii=False)
    except (TypeError, ValueError):
        s = str(obj)
    s = " ".join(s.split())
    return s if len(s) <= limit else s[:limit - 1] + "…"


def render_stream_event(obj: dict) -> list:
    """Human-readable line(s) for one stream-json event (best-effort; unknown
    shapes render as nothing rather than raw JSON noise)."""
    t = obj.get("type")
    if t == "system" and obj.get("subtype") == "init":
        sid = str(obj.get("session_id") or "")[:8]
        return [f"· session {sid} started · model {obj.get('model', '?')}"]
    if t == "assistant":
        lines = []
        for block in (obj.get("message") or {}).get("content") or []:
            if block.get("type") == "text" and block.get("text"):
                lines.extend(str(block["text"]).splitlines())
            elif block.get("type") == "tool_use":
                lines.append("▶ " + str(block.get("name", "tool"))
                             + " " + _compact(block.get("input", {})))
        return lines
    if t == "user":
        for block in (obj.get("message") or {}).get("content") or []:
            if isinstance(block, dict) and block.get("type") == "tool_result" \
                    and block.get("is_error"):
                return ["✖ tool error: " + _compact(block.get("content", ""))]
        return []
    if t == "result":
        cost = obj.get("total_cost_usd") or 0.0
        mark = "■ done" if obj.get("subtype") == "success" else "■ ended (error)"
        return [f"{mark} — {obj.get('num_turns', 0)} turns · ${cost:.4f}"]
    return []


def _default_spawn(argv, env, cwd):
    # stdin is a PIPE because the prompt is fed through it (see _feed_stdin) —
    # never through argv, which cmd.exe (hosting the Windows .CMD shim) caps at
    # ~8k chars ("The command line is too long.").
    return subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.PIPE, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=cwd or None,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


def _feed_stdin(proc, text) -> None:
    """Write the prompt to the run's stdin and close it. Runs on its own daemon
    thread: a prompt larger than the OS pipe buffer would otherwise block
    send() until the CLI drains it — and would block forever if the CLI died
    before reading. Tolerates injected fakes without a stdin and processes that
    exit early (broken pipe)."""
    stdin = getattr(proc, "stdin", None)
    if stdin is None:
        return
    try:
        stdin.write(text)
        stdin.close()
    except (OSError, ValueError):
        pass


class Agent:
    """One workspace agent: identity + scrollback + the current run."""

    def __init__(self, name, role="agent", profile="default", project=None,
                 swarm=False, model=None):
        self.id = secrets.token_hex(4)
        self.name = name or role.title()
        self.role = role if role in ROLE_LENSES else "agent"
        self.profile = profile or "default"
        self.model = (model or "").strip()      # "" = account default
        self.project = project
        self.swarm = swarm
        self.status = "idle"            # idle | running | done | error | stopped
        self.lines: list = []
        self.emitted = 0                # lines EVER emitted (absolute cursor)
        self.run_count = 0              # sends so far — one run, one artifact
        self.artifact = None            # path of the current/last run's artifact
        self._runlog = None             # runlog.RunLog of the in-flight run
        self.session_id = None          # claude session for --resume continuity
        self.task_id = None             # kanban card being executed, if any
        self.skills_used: list = []     # Skill tool invocations, ordered, deduped
        self.last_turns = None          # num_turns of the most recent run
        self.last_cost = None           # total_cost_usd of the most recent run
        self.last_result = ""           # final result text of the most recent run
        self.created = _now_iso()
        self.proc = None
        self._timer = None

    def snapshot(self) -> dict:
        return {"id": self.id, "name": self.name, "role": self.role,
                "profile": self.profile, "project": self.project,
                "model": self.model,
                "swarm": self.swarm, "status": self.status,
                "session_id": self.session_id, "task_id": self.task_id,
                "skills_used": list(self.skills_used),
                "last_turns": self.last_turns, "last_cost": self.last_cost,
                "created": self.created, "lines": len(self.lines),
                "artifact": self.artifact}

    def emit(self, line: str) -> None:
        self.lines.append(line)
        self.emitted += 1
        if len(self.lines) > MAX_LINES:
            del self.lines[:len(self.lines) - MAX_LINES]
        # The ring is a live VIEW and forgets its head; the run artifact is the
        # record and forgets nothing. Mirroring here (rather than at the stream
        # loop) means no emit path can bypass durability.
        rl = self._runlog
        if rl is not None:
            rl.append(line + "\n")


class Manager:
    """Owns the agent fleet. Thread-safe; the dashboard holds one instance."""

    def __init__(self, spawn=None, append_event=None, emit_activity=None):
        self._spawn = spawn or _default_spawn
        self._append = append_event or ev_mod.append_event
        # Resolved lazily in `_emit_faculty` rather than defaulted here: a
        # dashboard for a project that never touched the phase spine must not pay
        # for importing the mindnet package, which is the same rule
        # `dashboard._get_mindnet_cats` keeps.
        self._emit_activity = emit_activity
        self._lock = threading.Lock()
        self.agents: dict = {}
        self.mission = None             # {"mission", "started", "coordinator"}
        # The cat board's edge state. `_faculty_lit` is what the ledger last
        # heard, NOT what the fleet is doing — comparing the two is the whole of
        # the edge rule. The project and session are remembered from the moment
        # the cat was lit so it is put out in the same namespace and segment it
        # was lit in; see `_sync_faculty`.
        self._faculty_lit = False
        self._faculty_project = None
        self._faculty_session = None

    # --- fleet -------------------------------------------------------------
    def list(self) -> dict:
        with self._lock:
            ags = [a.snapshot() for a in self.agents.values()]
        ags.sort(key=lambda a: a["created"])
        return {"agents": ags, "cap": MAX_AGENTS, "mission": self.mission}

    def get(self, agent_id):
        return self.agents.get(agent_id)

    def spawn(self, name=None, role="agent", profile="default", project=None,
              prompt=None, swarm=False, model=None) -> dict:
        with self._lock:
            if len(self.agents) >= MAX_AGENTS:
                return {"ok": False, "error": f"agent cap reached ({MAX_AGENTS})"}
            a = Agent(name, role=role, profile=profile, project=project,
                      swarm=swarm, model=model)
            self.agents[a.id] = a
        a.emit(f"· {a.name} ready · role {a.role} · profile {a.profile}"
               + (f" · model {a.model}" if a.model else ""))
        if prompt:
            res = self.send(a.id, prompt)
            if not res.get("ok"):
                a.emit("✖ " + str(res.get("error")))
        return {"ok": True, "agent": a.snapshot()}

    def remove(self, agent_id) -> dict:
        self.stop(agent_id)
        with self._lock:
            self.agents.pop(agent_id, None)
        return {"ok": True}

    def stop(self, agent_id) -> dict:
        a = self.get(agent_id)
        if a is None:
            return {"ok": False, "error": "unknown agent"}
        proc = a.proc
        if proc is not None and proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            a.status = "stopped"
            a.emit("■ stopped by user")
            self._sync_faculty()       # a user stop is a settle like any other
        return {"ok": True}

    def clear_idle(self) -> dict:
        """Remove every settled MindParallel agent (not running, not swarm) —
        the one-click cull behind the dashboard's Clear idle button. Swarm
        agents are left to the mission_stop lifecycle."""
        with self._lock:
            ids = [a.id for a in self.agents.values()
                   if a.status != "running" and not a.swarm]
        removed = 0
        for i in ids:
            a = self.get(i)
            # re-check right before removal: an agent snapshotted as settled
            # may have started a run since — never kill a live run
            if a is None or a.status == "running":
                continue
            self.remove(i)
            removed += 1
        return {"ok": True, "removed": removed}

    def output(self, agent_id, since=0) -> dict:
        """Lines after ``since``, plus how many this cursor can never see.

        ``next`` is an ABSOLUTE line number (lines ever emitted), so a cursor
        stays meaningful after the ring trims its head. A poller that falls
        behind by more than MAX_LINES used to be clamped and silently resumed at
        the wrong offset; now the gap is reported — a bounded view must announce
        its bound. The untrimmed run lives in the agent's artifact."""
        a = self.get(agent_id)
        if a is None:
            return {"ok": False, "error": "unknown agent"}
        since = max(0, int(since))
        first = a.emitted - len(a.lines)       # absolute index of lines[0]
        dropped = max(0, first - since)
        start = max(0, min(since, a.emitted) - first)
        return {"ok": True, "lines": a.lines[start:], "next": a.emitted,
                "dropped": dropped, "status": a.status,
                "artifact": a.artifact}

    # --- one run = one headless claude call ---------------------------------
    def send(self, agent_id, prompt, extra_system=None, add_dirs=None,
             allowed_tools=None, disallowed_tools=None) -> dict:
        a = self.get(agent_id)
        if a is None:
            return {"ok": False, "error": "unknown agent"}
        if not (prompt or "").strip():
            return {"ok": False, "error": "prompt is empty"}
        if a.status == "running" and not self._settle_orphan(a):
            # Structured and retryable (SOP 2.1) so a caller can wait or reroute.
            # A bare string here was read as terminal by the Self-Reliant loop
            # and killed whole runs; the message text is unchanged for the UI.
            return {"ok": False, "error": f"{a.name} is busy — wait or stop it",
                    "isError": True, "errorCategory": "agent_busy",
                    "isRetryable": True}
        argv = [claude_cli.resolve_exe(), "--output-format", "stream-json",
                "--verbose"]
        # Permission posture. Default is bypassPermissions: headless runs have
        # no prompt channel, so under acceptEdits anything beyond an in-cwd edit
        # was silently auto-denied — agents burned whole turns pleading for
        # approvals nobody could grant (user mandate 2026-07-13: full access for
        # all workspace agents). BUT bypassPermissions approves EVERY tool,
        # which would make an --allowedTools restriction inert. So when a caller
        # passes an allowlist (Self-Reliant's read-only learn mode), we drop
        # bypassPermissions: with the default posture and no prompt channel,
        # tools outside the allowlist are auto-denied — the allowlist becomes
        # the real, enforced boundary (no Bash ⇒ no git/gh). --disallowedTools
        # is layered on as defense-in-depth in both postures.
        if allowed_tools:
            argv += ["--allowedTools", ",".join(str(t) for t in allowed_tools)]
        else:
            argv += ["--permission-mode", "bypassPermissions"]
        if disallowed_tools:
            argv += ["--disallowedTools",
                     ",".join(str(t) for t in disallowed_tools)]
        # extra workspace roots (e.g. Self-Reliant's \\wsl$ autoresearch dir) —
        # without --add-dir the CLI's boundary silently blocks out-of-cwd edits
        for d in add_dirs or []:
            argv += ["--add-dir", str(d)]
        if a.session_id:
            argv += ["--resume", a.session_id]
        if a.model:
            argv += ["--model", a.model]
        lens = ROLE_LENSES.get(a.role, "")
        sys_extra = (lens + _SHARED_MEMORY if a.role != "agent" else "") \
            + (extra_system or "")
        if sys_extra:
            argv += ["--append-system-prompt", sys_extra]
        # `-p` alone: the prompt is fed via stdin (_feed_stdin below). The
        # researcher's composed programs run to tens of KB and blew cmd.exe's
        # ~8k command-line cap when passed as an argv element.
        argv += ["-p"]
        env = _profile_env(a.profile)
        cwd = _folder_for_project(a.project)
        try:
            a.proc = self._spawn(argv, env, cwd)
        except OSError as e:
            a.status = "error"
            a.emit(f"✖ could not start claude: {e}")
            return {"ok": False, "error": str(e)}
        threading.Thread(target=_feed_stdin, args=(a.proc, prompt),
                         daemon=True).start()
        a.status = "running"
        # Light the cat here, BEFORE the reader thread starts. A run short enough
        # to settle first would otherwise have its reader see nobody running, and
        # this call would then find the same — the whole episode passing with the
        # board never having said anything.
        self._sync_faculty()
        # One send = one run = one artifact. Opened BEFORE the first emit so the
        # prompt itself heads the durable record, and handed to the reader
        # explicitly: a later send must never make an earlier reader finalise
        # the wrong run.
        a.run_count += 1
        rl = runlog.RunLog(runlog.workspace_key(a.id, a.run_count),
                           project=a.project)
        a._runlog = rl
        a.artifact = str(rl.path)
        a.emit(f"❯ {prompt}")
        self._log(a, "user_prompt", {"text": prompt, "agent": a.name,
                                     "role": a.role})
        t = threading.Thread(target=self._reader, args=(a, a.proc, rl),
                             daemon=True)
        t.start()
        a._timer = threading.Timer(RUN_TIMEOUT_S, self._timeout_kill, args=(a, a.proc))
        a._timer.daemon = True
        a._timer.start()
        return {"ok": True, "agent": a.snapshot()}

    def settle(self, agent_id) -> dict:
        """Release an agent stuck at ``running`` whose run is actually over.

        An orchestrator calls this between turns when it abandoned a turn at its
        wall-clock budget. Reports whether anything was settled; a genuinely live
        run is never yanked out from under itself."""
        a = self.get(agent_id)
        if a is None:
            return {"ok": False, "error": "unknown agent"}
        settled = a.status == "running" and self._settle_orphan(a)
        return {"ok": True, "settled": settled, "agent": a.snapshot()}

    def _settle_orphan(self, a) -> bool:
        """True when an agent whose process has demonstrably exited but whose
        status was never settled has been released.

        The orphan cascade (documented at research.py:742-754): a turn crossing
        its wall-clock budget leaves the run in flight; abandoning the worker
        thread strands ``status == "running"``; the next ``send()`` then rejects
        with "is busy", which the caller treats as terminal — "and the whole run
        dies." ResearchController worked around it for itself, so the swarm and
        MindParallel paths never inherited the fix. It lives here now.

        The kill path stays single and explicit: this only settles a process
        that has ALREADY exited, so it can never race ``_timeout_kill``."""
        proc = a.proc
        if proc is None or proc.poll() is None:
            return False                     # live run (or a fake): leave it
        if a._timer is not None:
            a._timer.cancel()
        rl, a._runlog = a._runlog, None
        if rl is not None:
            artifact = rl.finalise()         # keep whatever the orphan produced
            if artifact:
                a.artifact = artifact
        a.status = "stopped"
        a.emit("■ orphaned run settled — its process had already exited")
        # A cat left lit by an orphan is a permanent false positive: nothing else
        # will ever report that run finished.
        self._sync_faculty()
        return True

    def _timeout_kill(self, a, proc):
        # Sole owner of the kill path: the reader thread owns settling, and
        # _settle_orphan only touches already-exited processes, so no two
        # mechanisms can fight over one run.
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            a.emit(f"✖ run exceeded {RUN_TIMEOUT_S}s — killed")

    def _reader(self, a, proc, rl=None):
        """Drain one run's stream-json stdout into the agent buffer, then
        record the outcome in the ledger and release any kanban card."""
        result_text, saw_result = "", False
        try:
            for raw in proc.stdout:
                raw = raw.strip()
                if not raw:
                    continue
                try:
                    obj = json.loads(raw)
                except ValueError:
                    a.emit(raw)
                    continue
                if obj.get("type") == "system" and obj.get("session_id"):
                    a.session_id = obj["session_id"]
                if obj.get("type") == "assistant":
                    # record Skill tool invocations so the UI can show which
                    # skills this agent has used (deduped, in first-use order)
                    for block in (obj.get("message") or {}).get("content") or []:
                        if not (isinstance(block, dict)
                                and block.get("type") == "tool_use"
                                and block.get("name") == "Skill"):
                            continue
                        inp = block.get("input")
                        skill = inp.get("skill") if isinstance(inp, dict) else None
                        if skill and str(skill) not in a.skills_used:
                            a.skills_used.append(str(skill))
                if obj.get("type") == "result":
                    saw_result = True
                    result_text = obj.get("result") or ""
                    a.last_result = result_text
                    if obj.get("num_turns") is not None:
                        a.last_turns = obj.get("num_turns")
                    if obj.get("total_cost_usd") is not None:
                        a.last_cost = obj.get("total_cost_usd")
                    if obj.get("session_id"):
                        a.session_id = obj["session_id"]
                for line in render_stream_event(obj):
                    a.emit(line)
        except (OSError, ValueError):
            pass
        code = proc.wait()
        if a._timer is not None:
            a._timer.cancel()
        stopped = a.status == "stopped"
        clean = (code == 0 and saw_result)
        if not stopped and not clean:
            a.emit(f"✖ run failed (exit {code})")    # recorded, then persisted
        # Persist BEFORE any branch decides what to say: no exit path may
        # discard produced output. The event keeps a bounded excerpt plus the
        # pointer; the artifact keeps the whole run.
        artifact = rl.finalise() if rl is not None else None
        excerpt = rl.excerpt() if rl is not None else ""
        if a._runlog is rl:
            a._runlog = None
        if artifact:
            a.artifact = artifact
        text = ((result_text or "").strip() or excerpt)[:2000] or "(no text)"
        if stopped:
            # a user stop recorded nothing at all before MN-0 — interrupted work
            # is still work the operator paid for
            self._log(a, "assistant_response",
                      {"text": text, "agent": a.name, "role": a.role,
                       "artifact": artifact, "partial": True})
        elif clean:
            a.status = "done"
            self._log(a, "assistant_response",
                      {"text": text, "agent": a.name, "role": a.role,
                       "artifact": artifact, "partial": False})
        else:
            a.status = "error"
            # A non-zero exit (or a missing `result` event) used to log ONLY the
            # bare tool_failed below: a run that did real work and then died lost
            # 100% of that work's record. The partial response goes first so the
            # output is durable before the failure is reported.
            self._log(a, "assistant_response",
                      {"text": text, "agent": a.name, "role": a.role,
                       "artifact": artifact, "partial": True})
            self._log(a, "tool_failed",
                      {"text": f"workspace run failed (exit {code})",
                       "signature": f"workspace:{a.role}",
                       "action": f"workspace agent {a.name} run"})
        # After every branch above has settled the status, and once — the three
        # exit paths (stopped, clean, failed) are all the same event to the
        # board: this agent is no longer running.
        self._sync_faculty()
        if a.task_id:
            done = a.status == "done"
            _board_finish(a.task_id, done)
            a.task_id = None

    def _log(self, a, etype, payload) -> None:
        try:
            self._append(etype, payload,
                         session_id=a.session_id or f"ws-{a.id}",
                         tags=["workspace", "swarm" if a.swarm else "parallel",
                               a.role],
                         project=a.project)
        except Exception:
            pass                       # the ledger must never crash a pane

    # --- MN-5 cat board ------------------------------------------------------
    # The swarm is the surface an operator commands work from, and until now it
    # had no producer at all: the faculty strip read "no MindNetwork activity"
    # however many agents were running. These two methods are that producer.

    def _sync_faculty(self) -> None:
        """Light or extinguish `SWARM_FACULTY` when the swarm's aggregate state
        changes — and stay silent when it does not.

        EDGE-triggered, and that is the requirement rather than an optimisation.
        `telemetry.fold` is last-write-wins per faculty, so an `idle` emitted
        when one builder finishes would put the cat out with four siblings still
        working — a false "nothing is running" on the board whose whole job is to
        be trusted about exactly that. So: one `active` when the swarm goes from
        nobody-running to somebody-running, one `idle` when the last one settles,
        and nothing in between.

        The predicate is recomputed from the fleet rather than counted
        incrementally. A counter drifts permanently the first time a status
        changes on a path nobody remembered to decrement, and the drift is
        invisible; recomputing means a redundant call is merely wasted work and a
        missed one self-corrects at the next transition.

        The lock is released BEFORE the emit. Holding the fleet lock across a
        ledger append would put a disk write in front of every `list()` the
        dashboard polls, and `spawn` already calls `send` outside its own lock so
        a re-entrant acquire here would deadlock the whole fleet.

        Never raises, for the same reason `_log` does not: a run that succeeded
        must not be reported as failed because a cat could not be logged."""
        try:
            with self._lock:
                live = next((a for a in self.agents.values()
                             if a.swarm and a.status == "running"), None)
                if (live is not None) == self._faculty_lit:
                    return                      # no edge — the common case
                if live is not None:
                    self._faculty_lit = True
                    self._faculty_project = live.project
                    self._faculty_session = live.session_id or f"ws-{live.id}"
                else:
                    self._faculty_lit = False
                # Read INSIDE the lock: another thread may light the cat again
                # between here and the emit, and this call must report the edge
                # it actually observed.
                active = live is not None
                project, session = self._faculty_project, self._faculty_session
            self._emit_faculty(active, project, session)
        except Exception:                       # noqa: BLE001
            pass

    def _emit_faculty(self, active, project, session_id) -> None:
        """One `mindnet_activity` row. Imported lazily (see `__init__`) and
        injectable, mirroring `spawn` and `append_event` — a test observes the
        real faculty name and the real status vocabulary without a ledger round
        trip, because those names are the part that can silently be wrong."""
        from .mindnet import telemetry as tel_mod
        emit = self._emit_activity or tel_mod.emit
        emit(SWARM_FACULTY,
             tel_mod.STATUS_ACTIVE if active else tel_mod.STATUS_IDLE,
             project=project, session_id=session_id)

    # --- MindSwarm -----------------------------------------------------------
    def start_mission(self, mission, builders=2, scouts=1, reviewers=1,
                      profile="default", project=None) -> dict:
        mission = (mission or "").strip()
        if not mission:
            return {"ok": False, "error": "mission is empty"}
        want = 1 + max(0, int(builders)) + max(0, int(scouts)) + max(0, int(reviewers))
        with self._lock:
            room = MAX_AGENTS - len(self.agents)
        if want > room:
            return {"ok": False, "error":
                    f"mission needs {want} agents but only {room} slots are free"}
        spawned = []
        res = self.spawn(name="Coordinator 1", role="coordinator",
                         profile=profile, project=project, swarm=True,
                         prompt=f"MISSION: {mission}")
        if not res.get("ok"):
            return res
        spawned.append(res["agent"])
        roles = ([("builder", i + 1) for i in range(int(builders))]
                 + [("scout", i + 1) for i in range(int(scouts))]
                 + [("reviewer", i + 1) for i in range(int(reviewers))])
        for role, n in roles:
            r = self.spawn(
                name=f"{role.title()} {n}", role=role, profile=profile,
                project=project, swarm=True,
                prompt=(f"MISSION: {mission}\nWork your role. Check Mind Hive "
                        f"for the coordinator's plan and tasks for your role."))
            if r.get("ok"):
                spawned.append(r["agent"])
        self.mission = {"mission": mission, "started": _now_iso(),
                        "coordinator": spawned[0]["id"]}
        return {"ok": True, "mission": self.mission, "agents": spawned}

    def stop_mission(self) -> dict:
        with self._lock:
            ids = [a.id for a in self.agents.values() if a.swarm]
        for i in ids:
            self.stop(i)
        self.mission = None
        return {"ok": True, "stopped": len(ids)}

    def command(self, target, text) -> dict:
        """Direct one agent (by id) or the whole swarm ('all') from the bar."""
        if not (text or "").strip():
            return {"ok": False, "error": "command is empty"}
        if target and target != "all":
            return self.send(target, text)
        with self._lock:
            ids = [a.id for a in self.agents.values() if a.swarm]
        sent, busy = 0, 0
        for i in ids:
            r = self.send(i, text)
            sent += 1 if r.get("ok") else 0
            busy += 0 if r.get("ok") else 1
        return {"ok": True, "sent": sent, "busy": busy}


# --- recipe runs: staged skills + command -> one enriched pipeline prompt --
# The MindHive capability primer rides along as system prompt so a pane agent
# knows the memory system's surface area without burning turns discovering it.
MINDHIVE_PRIMER = (
    "MindHive integration: this machine runs the MindHive shared-memory "
    "system. Its full function surface is the `python -m mindhive` CLI — "
    "recall \"<terms>\" (query the ledger; add --semantic for meaning-based), "
    "append decision|experiment_result|caveat \"<one line>\" --tags workspace "
    "(commit a lesson), skills list / skills status / skills invoke <name> "
    "(the live skill registry, per-skill green/red health, and the recorded "
    "invoke channel), rules summaries (Improvements: prevention rules learned "
    "from past failures), graph related \"<entity>\", state, and briefing. "
    "Before acting, recall relevant history; when a skill shows a failing "
    "status or a prevention rule, apply the lesson instead of repeating the "
    "mistake; when you finish something durable, append it."
)

_NO_OVERLAP = (
    "Invoke each staged skill via the Skill tool exactly once, in the order "
    "listed. Do not re-run a skill and do not let two skills repeat the same "
    "work: carry each skill's output forward as the next skill's input, and "
    "when two skills would cover the same step, do that step once and reuse "
    "the result. The COMMAND below is the single source of intent — every "
    "skill serves it; when skills disagree, the command wins."
)


def _clean_skills(skills) -> list:
    out = []
    for s in skills or []:
        if isinstance(s, dict) and s.get("name"):
            out.append({"name": str(s["name"])[:120],
                        "plugin": (str(s["plugin"])[:120]
                                   if s.get("plugin") else None)})
    return out


def _recipe_context(skills, command, project=None) -> str:
    """Learned-history block for a recipe: per-skill health + Improvements
    prevention rules, then top ledger recall hits for the command. Every
    source is best-effort — a broken subsystem must never block a run."""
    lines = []
    try:
        from . import plugins as plugins_mod
        status = plugins_mod.call_status(project) or {}
    except Exception:
        status = {}
    try:
        from . import incidents as inc
        recs = {r.get("signature"): r for r in inc.summaries(project)}
    except Exception:
        recs = {}
    for s in skills:
        nm = s["name"]
        st = status.get(nm)
        rec = recs.get(f"skill:{nm}") or {}
        lesson = rec.get("lesson") or rec.get("active_rule") or ""
        if st or lesson:
            lines.append(f"- skill {nm}: last outcome {st or 'unrecorded'}"
                         + (f" — prevention rule: {lesson}" if lesson else ""))
    query = (command or "").strip() or " ".join(s["name"] for s in skills)
    try:
        from . import recall as recall_mod
        for hit in (recall_mod.recall(query, project=project, limit=3) or [])[:3]:
            snip = (hit.get("snippet") or "").strip()
            if snip:
                lines.append("- ledger: " + snip[:180])
    except Exception:
        pass
    return "\n".join(lines)


def compose_recipe_prompt(skills, command, project=None) -> str:
    """One prompt from staged skills + a command, enriched with MindHive's
    learned history. Pure function of its inputs (plus the ledger) — the UI's
    Run button posts the staged chips here instead of composing client-side."""
    skills = _clean_skills(skills)
    command = (command or "").strip()
    parts = []
    if skills:
        parts.append("Execute this staged recipe as ONE coherent pipeline:")
        for i, s in enumerate(skills, 1):
            src = f" (from the {s['plugin']} plugin)" if s.get("plugin") else ""
            parts.append(f"{i}. Use the {s['name']} skill{src}.")
        parts.append(_NO_OVERLAP)
    if command:
        parts.append("COMMAND: " + command)
    elif skills:
        parts.append("Follow the skill(s) to completion.")
    ctx = _recipe_context(skills, command, project)
    if ctx:
        parts.append("Learned history from MindHive — honor it:\n" + ctx)
    return "\n\n".join(parts)


def run_recipe(manager: "Manager", agent_id, skills, command,
               project=None) -> dict:
    """The explicit ▶ Run: compose the enriched pipeline prompt and send it,
    with the MindHive primer appended to the agent's system prompt."""
    skills = _clean_skills(skills)
    if not skills and not (command or "").strip():
        return {"ok": False, "error": "stage a skill or type a command first"}
    a = manager.get(agent_id)
    if a is None:
        return {"ok": False, "error": "unknown agent"}
    prompt = compose_recipe_prompt(skills, command, project=project or a.project)
    return manager.send(agent_id, prompt, extra_system=" " + MINDHIVE_PRIMER)


# --- kanban board: cards that dispatch agents ------------------------------
_BOARD_LOCK = threading.Lock()
BOARD_COLUMNS = ("backlog", "running", "done")


def _board_path():
    return paths.store_home() / "workspace_board.json"


# A read that loses a race with the atomic write retries this many times, this far
# apart. The store's own pinned caveat records why: on Windows `os.replace` raises
# PermissionError (WinError 5) against the DESTINATION, and for that instant a
# concurrent reader gets a sharing violation on a file that exists and is intact.
_BOARD_READ_TRIES = 4
_BOARD_READ_BACKOFF = 0.02


def load_board() -> dict:
    """The board, or an empty one.

    Retries a transient read failure instead of reporting an empty board.
    `_write_board_locked` is already atomic, so the only way a reader sees nothing is by
    landing inside the replace — and answering "no tasks" there is a failure disguised as
    an absence. That disguise is what made
    `test_board_dispatch_runs_card_on_idle_agent` fail on windows/py3.12 while passing on
    py3.11 in the same CI run: the board had a card, the read lost the race, and the
    caller saw an empty list.

    A MISSING file is not retried — absence is a real answer there, and the common
    first-run path must not pay four sleeps for it. Malformed JSON *is* retried, because
    a truncated read of a file mid-replace looks exactly like that."""
    for attempt in range(_BOARD_READ_TRIES):
        try:
            b = json.loads(_board_path().read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {"tasks": []}
        except (OSError, ValueError):
            if attempt + 1 >= _BOARD_READ_TRIES:
                return {"tasks": []}
            time.sleep(_BOARD_READ_BACKOFF)
            continue
        if isinstance(b, dict) and isinstance(b.get("tasks"), list):
            return b
        return {"tasks": []}        # valid JSON, wrong shape — retrying cannot help
    return {"tasks": []}


def _write_board_locked(tasks: list) -> None:
    """Atomic write (temp + replace): a concurrent reader — the dashboard's
    board poll, or another thread's load_board — must never catch the file
    mid-truncate and see an empty board. Caller holds _BOARD_LOCK."""
    p = _board_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_suffix(".json.tmp")
    tmp.write_text(json.dumps({"tasks": tasks}, ensure_ascii=False, indent=1),
                   encoding="utf-8")
    os.replace(tmp, p)


def save_board(board: dict) -> dict:
    tasks = []
    for t in (board or {}).get("tasks", []):
        if not isinstance(t, dict) or not t.get("title"):
            continue
        tasks.append({"id": t.get("id") or secrets.token_hex(4),
                      "title": str(t["title"])[:160],
                      "prompt": str(t.get("prompt") or t["title"])[:4000],
                      "status": t.get("status") if t.get("status") in BOARD_COLUMNS
                      else "backlog",
                      "agent_id": t.get("agent_id")})
    with _BOARD_LOCK:
        _write_board_locked(tasks)
    return {"ok": True, "tasks": tasks}


def _board_finish(task_id, done: bool) -> None:
    # The whole read-modify-write stays under the lock so two runs finishing
    # at once can't interleave and drop each other's card release.
    with _BOARD_LOCK:
        board = load_board()
        for t in board["tasks"]:
            if t.get("id") == task_id:
                t["status"] = "done" if done else "backlog"
                t["agent_id"] = None
        _write_board_locked(board["tasks"])


def board_dispatch(manager: Manager, task_id, agent_id=None) -> dict:
    """Send a card's prompt to an agent (named, or the first idle one) and move
    the card to 'running'; the reader thread moves it on when the run ends."""
    board = load_board()
    task = next((t for t in board["tasks"] if t.get("id") == task_id), None)
    if task is None:
        return {"ok": False, "error": "unknown task"}
    a = manager.get(agent_id) if agent_id else None
    if a is None:
        a = next((x for x in manager.agents.values()
                  if x.status != "running" and not x.swarm), None)
    if a is None:
        return {"ok": False, "error": "no free agent — spawn a pane first"}
    # Link the card and persist BEFORE dispatching: send() starts the reader
    # thread, and a fast run could finish (and try to release the card) before
    # a later task_id assignment or board save — leaving the card stuck in
    # 'running'. On a failed send, roll the card back to the backlog.
    a.task_id = task_id
    task["status"], task["agent_id"] = "running", a.id
    save_board(board)
    res = manager.send(a.id, task["prompt"])
    if not res.get("ok"):
        a.task_id = None
        _board_finish(task_id, done=False)
        return res
    return {"ok": True, "task": task, "agent": a.snapshot()}


# The dashboard's singleton fleet.
MANAGER = Manager()


# --- dashboard API dispatch -------------------------------------------------
# One entry point per verb keeps dashboard.py's routing table tiny.

def api_get(action, proj=None, qs=None) -> dict:
    qs = qs or {}
    if action == "agents":
        return MANAGER.list()
    if action == "output":
        return MANAGER.output((qs.get("id") or [""])[0],
                              since=int((qs.get("since") or ["0"])[0]))
    if action == "profiles":
        return {"ok": True, "profiles": load_profiles(),
                "models": MODEL_CHOICES}
    if action == "auth":
        return auth_status_all()
    if action == "board":
        return load_board()
    return {"ok": False, "error": "not found"}


def api_post(action, proj=None, body=None) -> dict:
    body = body or {}
    if action == "spawn":
        return MANAGER.spawn(name=body.get("name"),
                             role=body.get("role", "agent"),
                             profile=body.get("profile", "default"),
                             project=proj, prompt=body.get("prompt"),
                             model=body.get("model"))
    if action == "send":
        return MANAGER.send(body.get("id"), body.get("prompt", ""))
    if action == "run_recipe":
        return run_recipe(MANAGER, body.get("id"), body.get("skills"),
                          body.get("command", ""), project=proj)
    if action == "set_model":
        a = MANAGER.get(body.get("id"))
        if a is None:
            return {"ok": False, "error": "unknown agent"}
        if a.status == "running":
            return {"ok": False, "error": f"{a.name} is busy — model applies "
                                          "to the next run; stop it first"}
        a.model = (body.get("model") or "").strip()
        a.emit(f"· model → {a.model or 'account default'}")
        return {"ok": True, "agent": a.snapshot()}
    if action == "stop":
        return MANAGER.stop(body.get("id"))
    if action == "remove":
        return MANAGER.remove(body.get("id"))
    if action == "clear_idle":
        return MANAGER.clear_idle()
    if action == "profile_add":
        return save_profile(body.get("name", ""), body.get("config_dir", ""))
    if action == "profile_remove":
        return remove_profile(body.get("name", ""))
    if action == "profile_login":
        return profile_login(body.get("name", "default"))
    if action == "auth_verify":
        # POST (not GET): may spawn one `claude auth status` subprocess. The
        # 60s cache absorbs page-load bursts; force=true is the post-Connect
        # re-check.
        return {"ok": True, **verify_auth(body.get("name", "default"),
                                          force=bool(body.get("force")))}
    if action == "profile_trust":
        return profile_trust(body.get("name", "default"),
                             body.get("folder") or _folder_for_project(proj))
    if action == "board_save":
        return save_board(body.get("board") or {"tasks": body.get("tasks", [])})
    if action == "board_dispatch":
        return board_dispatch(MANAGER, body.get("task_id"),
                              agent_id=body.get("agent_id"))
    if action == "mission":
        return MANAGER.start_mission(
            body.get("mission", ""), builders=body.get("builders", 2),
            scouts=body.get("scouts", 1), reviewers=body.get("reviewers", 1),
            profile=body.get("profile", "default"), project=proj)
    if action == "mission_stop":
        return MANAGER.stop_mission()
    if action == "command":
        return MANAGER.command(body.get("target", "all"), body.get("text", ""))
    return {"ok": False, "error": "not found"}
