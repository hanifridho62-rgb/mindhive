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

MindSwarm reuses the same agents: a mission spawns a coordinator plus builders,
scouts, and reviewers, each with a role lens appended to its system prompt that
also tells it to read/write the shared Mind Hive memory (`hive recall` /
`hive append`) — what one agent learns, the next one starts with.
"""
from __future__ import annotations

import json
import os
import secrets
import subprocess
import threading
import time

from . import claude_cli, events as ev_mod, paths

MAX_AGENTS = 16
MAX_LINES = 4000                 # per-agent scrollback cap (oldest dropped)
RUN_TIMEOUT_S = int(os.environ.get("MINDHIVE_WS_TIMEOUT", "1800"))

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
    return subprocess.Popen(
        argv, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
        stdin=subprocess.DEVNULL, text=True, encoding="utf-8", errors="replace",
        env=env, cwd=cwd or None,
        creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0))


class Agent:
    """One workspace agent: identity + scrollback + the current run."""

    def __init__(self, name, role="agent", profile="default", project=None,
                 swarm=False):
        self.id = secrets.token_hex(4)
        self.name = name or role.title()
        self.role = role if role in ROLE_LENSES else "agent"
        self.profile = profile or "default"
        self.project = project
        self.swarm = swarm
        self.status = "idle"            # idle | running | done | error | stopped
        self.lines: list = []
        self.session_id = None          # claude session for --resume continuity
        self.task_id = None             # kanban card being executed, if any
        self.created = _now_iso()
        self.proc = None
        self._timer = None

    def snapshot(self) -> dict:
        return {"id": self.id, "name": self.name, "role": self.role,
                "profile": self.profile, "project": self.project,
                "swarm": self.swarm, "status": self.status,
                "session_id": self.session_id, "task_id": self.task_id,
                "created": self.created, "lines": len(self.lines)}

    def emit(self, line: str) -> None:
        self.lines.append(line)
        if len(self.lines) > MAX_LINES:
            del self.lines[:len(self.lines) - MAX_LINES]


class Manager:
    """Owns the agent fleet. Thread-safe; the dashboard holds one instance."""

    def __init__(self, spawn=None, append_event=None):
        self._spawn = spawn or _default_spawn
        self._append = append_event or ev_mod.append_event
        self._lock = threading.Lock()
        self.agents: dict = {}
        self.mission = None             # {"mission", "started", "coordinator"}

    # --- fleet -------------------------------------------------------------
    def list(self) -> dict:
        with self._lock:
            ags = [a.snapshot() for a in self.agents.values()]
        ags.sort(key=lambda a: a["created"])
        return {"agents": ags, "cap": MAX_AGENTS, "mission": self.mission}

    def get(self, agent_id):
        return self.agents.get(agent_id)

    def spawn(self, name=None, role="agent", profile="default", project=None,
              prompt=None, swarm=False) -> dict:
        with self._lock:
            if len(self.agents) >= MAX_AGENTS:
                return {"ok": False, "error": f"agent cap reached ({MAX_AGENTS})"}
            a = Agent(name, role=role, profile=profile, project=project,
                      swarm=swarm)
            self.agents[a.id] = a
        a.emit(f"· {a.name} ready · role {a.role} · profile {a.profile}")
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
        return {"ok": True}

    def output(self, agent_id, since=0) -> dict:
        a = self.get(agent_id)
        if a is None:
            return {"ok": False, "error": "unknown agent"}
        since = max(0, int(since))
        # the buffer trims from the front; clamp so a stale cursor still works
        since = min(since, len(a.lines))
        return {"ok": True, "lines": a.lines[since:], "next": len(a.lines),
                "status": a.status}

    # --- one run = one headless claude call ---------------------------------
    def send(self, agent_id, prompt) -> dict:
        a = self.get(agent_id)
        if a is None:
            return {"ok": False, "error": "unknown agent"}
        if not (prompt or "").strip():
            return {"ok": False, "error": "prompt is empty"}
        if a.status == "running":
            return {"ok": False, "error": f"{a.name} is busy — wait or stop it"}
        argv = [claude_cli.resolve_exe(), "--output-format", "stream-json",
                "--verbose", "--permission-mode", "acceptEdits"]
        if a.session_id:
            argv += ["--resume", a.session_id]
        lens = ROLE_LENSES.get(a.role, "")
        if a.role != "agent":
            argv += ["--append-system-prompt", lens + _SHARED_MEMORY]
        argv += ["-p", prompt]
        env = _profile_env(a.profile)
        cwd = _folder_for_project(a.project)
        try:
            a.proc = self._spawn(argv, env, cwd)
        except OSError as e:
            a.status = "error"
            a.emit(f"✖ could not start claude: {e}")
            return {"ok": False, "error": str(e)}
        a.status = "running"
        a.emit(f"❯ {prompt}")
        self._log(a, "user_prompt", {"text": prompt, "agent": a.name,
                                     "role": a.role})
        t = threading.Thread(target=self._reader, args=(a, a.proc), daemon=True)
        t.start()
        a._timer = threading.Timer(RUN_TIMEOUT_S, self._timeout_kill, args=(a, a.proc))
        a._timer.daemon = True
        a._timer.start()
        return {"ok": True, "agent": a.snapshot()}

    def _timeout_kill(self, a, proc):
        if proc.poll() is None:
            try:
                proc.kill()
            except OSError:
                pass
            a.emit(f"✖ run exceeded {RUN_TIMEOUT_S}s — killed")

    def _reader(self, a, proc):
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
                if obj.get("type") == "result":
                    saw_result = True
                    result_text = obj.get("result") or ""
                    if obj.get("session_id"):
                        a.session_id = obj["session_id"]
                for line in render_stream_event(obj):
                    a.emit(line)
        except (OSError, ValueError):
            pass
        code = proc.wait()
        if a._timer is not None:
            a._timer.cancel()
        if a.status == "stopped":
            pass                                     # user kill already reported
        elif code == 0 and saw_result:
            a.status = "done"
            self._log(a, "assistant_response",
                      {"text": (result_text or "").strip()[:2000] or "(no text)",
                       "agent": a.name, "role": a.role})
        else:
            a.status = "error"
            a.emit(f"✖ run failed (exit {code})")
            self._log(a, "tool_failed",
                      {"text": f"workspace run failed (exit {code})",
                       "signature": f"workspace:{a.role}",
                       "action": f"workspace agent {a.name} run"})
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


# --- kanban board: cards that dispatch agents ------------------------------
_BOARD_LOCK = threading.Lock()
BOARD_COLUMNS = ("backlog", "running", "done")


def _board_path():
    return paths.store_home() / "workspace_board.json"


def load_board() -> dict:
    try:
        b = json.loads(_board_path().read_text(encoding="utf-8"))
        if isinstance(b, dict) and isinstance(b.get("tasks"), list):
            return b
    except (OSError, ValueError):
        pass
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
        return {"ok": True, "profiles": load_profiles()}
    if action == "board":
        return load_board()
    return {"ok": False, "error": "not found"}


def api_post(action, proj=None, body=None) -> dict:
    body = body or {}
    if action == "spawn":
        return MANAGER.spawn(name=body.get("name"),
                             role=body.get("role", "agent"),
                             profile=body.get("profile", "default"),
                             project=proj, prompt=body.get("prompt"))
    if action == "send":
        return MANAGER.send(body.get("id"), body.get("prompt", ""))
    if action == "stop":
        return MANAGER.stop(body.get("id"))
    if action == "remove":
        return MANAGER.remove(body.get("id"))
    if action == "profile_add":
        return save_profile(body.get("name", ""), body.get("config_dir", ""))
    if action == "profile_remove":
        return remove_profile(body.get("name", ""))
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
