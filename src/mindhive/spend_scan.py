# SPDX-License-Identifier: Apache-2.0
# src/mindhive/spend_scan.py
"""The reading half of `hive spend`: one pass over each Claude Code transcript.

`spend.py` states the definitions and folds what this module collects into the
four reports. Kept apart so each stays a module one can read at a sitting.

A transcript is read once, top to bottom. Until its first prompt is known every
line is parsed; after that only lines that can carry a call, a hook, a tool
result or a compaction are (`_MARKERS`), which is what keeps a scan of two
gigabytes of transcripts tolerable. Nothing here writes, and nothing it keeps
is text: counts, lengths, owners, timestamps.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime, timedelta, timezone
from pathlib import Path, PureWindowsPath

CHORE_PREFIXES = ("You are the Reflection Engine", "You are the Resolution Engine",
                  "You are proposing follow-up", "Break one software task", "Use the ")
NO_COMMAND = "(no command)"
UNATTRIBUTED = "(unattributed)"
UNKNOWN_AGENT = "(unknown)"

_CTX_KEYS = ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
# The owner it captures is printed, so the plugin segment must be a whole name
# that ends at a separator, a quote, a space or the end: a segment running on
# into `;KEY=...` is command text, not a plugin.
_CACHE_RE = re.compile(
    r"plugins[\\/]+cache[\\/]+([^\\/\"' ]+)[\\/]+([A-Za-z0-9._-]+)(?=[\\/\"' ]|$)")
# json.loads raises RecursionError, not ValueError, past its depth limit.
_JSON_ERRORS = (ValueError, RecursionError)
_LOOKUP_RE = re.compile(
    r"(?:\bhive(?:\.exe)?['\"]?|-m\s+mindhive)\s+(recall|pg|state|briefing|sop\s+show"
    r"|graph|memo|roadmap\s+show|premise|search|context)\b")
# Claude Code adds a hook's plain stdout to the model's context for these two
# events only; for the rest it shows the output in transcript mode.
_INJECTING_EVENTS = ("SessionStart", "UserPromptSubmit")
# Rows worth parsing once a transcript's first prompt is known. Key names, not
# `"key":"value"` pairs, so the filter does not depend on JSON spacing.
_MARKERS = ('"usage"', '"attachment"', '"tool_result"', "compact_boundary")


@dataclass
class Session:
    path: Path
    project_dir: Path                                  # <projects>/<folder>
    kind: str                                          # interactive|chore|subagent
    agent_type: str | None
    first_ts: str
    calls: list = field(default_factory=list)          # context tokens per call
    call_span: tuple = ("", "")                        # first and last call ts
    output_tokens: int = 0
    hook_runs: list = field(default_factory=list)      # (owner, event, type, ms, too_large)
    injections: list = field(default_factory=list)     # (event, owner, chars, calls)
    lookup_calls: list = field(default_factory=list)   # lookup kinds, one set per call
    lookup_results: list = field(default_factory=list)  # (chars, calls)


@dataclass
class Scan:
    root: Path
    since: str | None
    sessions: list = field(default_factory=list)
    files_read: int = 0
    files_skipped: int = 0
    files_unreadable: int = 0          # failed to stat, open or read: never "read"


def default_claude_dir() -> Path:
    from . import harvest
    return harvest.claude_projects_root().parent


def _read_json(path: Path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, *_JSON_ERRORS):
        return None


def _commands(node, out: set) -> None:
    if isinstance(node, dict):
        for key, value in node.items():
            if key == "command" and isinstance(value, str):
                out.add(value)
            else:
                _commands(value, out)
    elif isinstance(node, list):
        for value in node:
            _commands(value, out)


def _inside_plugin(ref: str) -> bool:
    """A manifest's hook-file path names a file in the plugin. An absolute
    path, a drive, a UNC share or a `..` step names one elsewhere -- for a
    share, reading it would open a connection -- and is not followed."""
    pure = PureWindowsPath(ref)      # reads `/` and `\` alike, on any platform
    return bool(ref) and not pure.anchor and ".." not in pure.parts


def _plugin_hook_commands(root: Path) -> set:
    """Commands in a plugin's `hooks/hooks.json` and in whatever its
    `plugin.json` `hooks` field names: an inline map, a file, or a list."""
    found: set = set()
    _commands(_read_json(root / "hooks" / "hooks.json"), found)
    manifest = _read_json(root / ".claude-plugin" / "plugin.json")
    refs = manifest.get("hooks") if isinstance(manifest, dict) else None
    for ref in refs if isinstance(refs, list) else [refs]:
        if isinstance(ref, dict):
            _commands(ref, found)
        elif isinstance(ref, str) and _inside_plugin(ref):
            _commands(_read_json(root / ref), found)
    return found


def hook_owners(plugins_dir: Path) -> dict:
    """Every installed plugin's hook command -> its plugin name (`a+b` when two
    plugins ship the same command). Disabled plugins count: old transcripts
    still carry their runs."""
    data = _read_json(Path(plugins_dir) / "installed_plugins.json")
    plugins = data.get("plugins") if isinstance(data, dict) else None
    owners: dict = {}
    for pid, installs in (plugins.items() if isinstance(plugins, dict) else ()):
        for inst in installs if isinstance(installs, list) else ():
            root = inst.get("installPath") if isinstance(inst, dict) else None
            if not (isinstance(root, str) and root):
                continue
            for cmd in _plugin_hook_commands(Path(root)):
                owners.setdefault(cmd, set()).add(str(pid).split("@")[0])
    return {cmd: "+".join(sorted(names)) for cmd, names in owners.items()}


def hook_owner(command: str, owners: dict) -> str:
    if command in owners:
        return owners[command]
    match = _CACHE_RE.search(command)
    if match:
        return match.group(2)
    if "mindhive" in command.lower():
        return "mindhive"
    return UNATTRIBUTED if command else NO_COMMAND


def classify_lookup(block: dict) -> str | None:
    """The memory-lookup kind of one tool_use block, or None."""
    name, inp = block.get("name"), block.get("input") or {}
    if not isinstance(inp, dict):
        return None
    if name in ("Bash", "PowerShell"):
        match = _LOOKUP_RE.search(str(inp.get("command", "")))
        return "hive " + match.group(1).split()[0] if match else None
    if name == "Read":
        path = str(inp.get("file_path", "")).replace("\\", "/").lower()
        if ".mindhive/" in path or "/ledger/" in path:
            return "read store"
        if path.endswith(("state.md", "briefing.md", "memory.md")):
            return "read state/memory md"
    if name in ("Grep", "Glob"):
        path = (str(inp.get("path", "")) + " " + str(inp.get("pattern", ""))
                ).replace("\\", "/").lower()
        if ".mindhive/" in path or "/ledger" in path:
            return "grep store"
    return None


def _agent_type(path: Path) -> str:
    meta = _read_json(path.with_name(path.stem + ".meta.json"))
    for key in ("agentType", "subagent_type", "subagentType"):
        value = meta.get(key) if isinstance(meta, dict) else None
        if isinstance(value, str) and value:
            return value
    return UNKNOWN_AGENT


def _part(item) -> str:
    if isinstance(item, dict):
        return str(item.get("text", ""))
    return item if isinstance(item, str) else str(item)


def _pieces(content) -> list:
    """A row's content as its separate text pieces."""
    if isinstance(content, list):
        return [_part(item) for item in content]
    return [content] if isinstance(content, str) else []


def _json_answer(stdout) -> str | None:
    """The additionalContext a hook returned as JSON on stdout, or None."""
    if not isinstance(stdout, str) or not stdout.lstrip().startswith("{"):
        return None
    try:
        data = json.loads(stdout)
    except _JSON_ERRORS:
        return None
    hso = data.get("hookSpecificOutput") if isinstance(data, dict) else None
    text = hso.get("additionalContext") if isinstance(hso, dict) else None
    return text if isinstance(text, str) and text else None


def _int(value) -> int:
    try:
        return int(value or 0)
    except (TypeError, ValueError, OverflowError):       # Infinity parses as a float
        return 0


def _key(value) -> str | None:
    """An id as a set or dict key. Claude Code writes ids as strings; a list or
    an object where one belongs cannot be hashed, so it reads as no id."""
    return value if isinstance(value, str) else None


class _Reader:
    """The state of one pass over one transcript."""

    def __init__(self, owners: dict, subagent: bool):
        self.owners, self.subagent = owners, subagent
        self.first_ts = self.first_user = None
        self.calls, self.lookups, self.span, self.out = [], [], ["", ""], 0
        self.index: dict = {}
        self.seen_rows, self.lookup_ids, self.charged = set(), set(), set()
        self.runs, self.answers = [], []
        self.open_inj, self.done_inj, self.open_res, self.done_res = [], [], [], []

    def wants(self, line: str) -> bool:
        if self.first_ts is None or (self.first_user is None and not self.subagent):
            return True
        return any(marker in line for marker in _MARKERS)

    def row(self, row: dict) -> None:
        uid = _key(row.get("uuid"))
        if uid is not None:
            if uid in self.seen_rows:
                return
            self.seen_rows.add(uid)
        ts = row.get("timestamp")
        if self.first_ts is None and isinstance(ts, str) and ts:
            self.first_ts = ts
        kind = row.get("type")
        if kind == "assistant":
            self._assistant(row, ts)
        elif kind == "user":
            self._user(row)
        elif kind == "system" and row.get("subtype") == "compact_boundary":
            self._close()
        elif kind == "attachment" and isinstance(row.get("attachment"), dict):
            self._attachment(row["attachment"])

    def _assistant(self, row: dict, ts) -> None:
        msg = row.get("message")
        if not isinstance(msg, dict) or not isinstance(msg.get("usage"), dict):
            return
        if msg.get("model") == "<synthetic>":
            return
        mid = _key(msg.get("id")) or _key(row.get("requestId"))
        if mid not in self.index:
            usage = msg["usage"]
            self.index[mid] = len(self.calls)
            self.calls.append(sum(_int(usage.get(k)) for k in _CTX_KEYS))
            self.lookups.append(set())
            self.out += _int(usage.get("output_tokens"))
            self.span = [self.span[0] or str(ts or ""), str(ts or "")]
        content = msg.get("content")
        for block in content if isinstance(content, list) else ():
            if isinstance(block, dict) and block.get("type") == "tool_use":
                found = classify_lookup(block)
                if found:
                    self.lookups[self.index[mid]].add(found)
                    if _key(block.get("id")) is not None:
                        self.lookup_ids.add(block["id"])

    def _user(self, row: dict) -> None:
        msg = row.get("message") if isinstance(row.get("message"), dict) else {}
        content = msg.get("content")
        if self.first_user is None:
            texts = [content] if isinstance(content, str) else [
                str(b.get("text", "")) for b in (content if isinstance(content, list) else ())
                if isinstance(b, dict) and b.get("type") == "text"]
            self.first_user = next((t for t in texts if t), "")
        for block in content if isinstance(content, list) else ():
            if not (isinstance(block, dict) and block.get("type") == "tool_result"):
                continue
            tool_id = _key(block.get("tool_use_id"))
            if tool_id in self.lookup_ids and tool_id not in self.charged:
                self.charged.add(tool_id)
                chars = sum(len(p) for p in _pieces(block.get("content")))
                self.open_res.append((len(self.calls), chars))

    def _attachment(self, att: dict) -> None:
        kind = str(att.get("type"))
        if "hookEvent" not in att and not kind.startswith("hook_"):
            return
        event = str(att.get("hookEvent") or str(att.get("hookName") or "").split(":")[0])
        if kind == "hook_additional_context":
            # One row can hold several hooks' context (the harness's worktree
            # note beside a plugin's): each piece goes to its own owner.
            for piece in _pieces(att.get("content")):
                owner = next((o for e, answer, o in reversed(self.answers)
                              if e == event and answer in piece), NO_COMMAND)
                if piece:
                    self.open_inj.append((len(self.calls), event, owner, len(piece)))
            return
        owner = hook_owner(str(att.get("command") or ""), self.owners)
        ms = att.get("durationMs")
        text = "\n".join(_pieces(att.get("content")))
        # Claude Code swaps an oversized output for this preview. A hook that
        # merely quotes the phrase, or an error carrying it, is not too large.
        too_large = (text.lstrip().startswith("<persisted-output>")
                     and "Output too large" in text)
        self.runs.append((owner, event, kind, ms if isinstance(ms, (int, float)) else None,
                          too_large))
        if kind == "hook_success":
            if text and event in _INJECTING_EVENTS:
                self.open_inj.append((len(self.calls), event, owner, len(text)))
            answer = _json_answer(att.get("stdout"))
            if answer:
                self.answers.append((event, answer, owner))

    def _close(self) -> None:
        now = len(self.calls)
        self.done_inj += [(e, o, n, now - at) for at, e, o, n in self.open_inj]
        self.done_res += [(n, now - at) for at, n in self.open_res]
        self.open_inj, self.open_res = [], []

    def session(self, path: Path, project_dir: Path) -> Session | None:
        self._close()
        if self.first_ts is None or not (self.calls or self.runs):
            return None
        if self.subagent:
            kind = "subagent"
        else:
            kind = "chore" if (self.first_user or "").lstrip().startswith(
                CHORE_PREFIXES) else "interactive"
        return Session(path=path, project_dir=project_dir, kind=kind,
                       agent_type=_agent_type(path) if self.subagent else None,
                       first_ts=self.first_ts, calls=self.calls,
                       call_span=tuple(self.span), output_tokens=self.out,
                       hook_runs=self.runs, injections=self.done_inj,
                       lookup_calls=self.lookups, lookup_results=self.done_res)


def read_transcript(path: Path, owners: dict, *, subagent: bool,
                    project_dir: Path | None = None) -> tuple[Session | None, bool]:
    """`(session, readable)` for one transcript. `session` is None when the
    file holds no call and no hook run. `readable` is False when opening or
    reading it failed, part-way included: what was read is then dropped, not
    reported as the whole file. Malformed lines are skipped; nothing raises."""
    reader = _Reader(owners, subagent)
    try:
        with open(path, encoding="utf-8", errors="replace") as fh:
            for line in fh:
                if not reader.wants(line):
                    continue
                try:
                    row = json.loads(line)
                except _JSON_ERRORS:
                    continue
                if isinstance(row, dict):
                    reader.row(row)
    except OSError:
        return None, False
    return reader.session(Path(path), Path(project_dir or Path(path).parent)), True


def _iso(moment: datetime) -> str:
    return moment.strftime("%Y-%m-%dT%H:%M:%S.") + f"{moment.microsecond // 1000:03d}Z"


def scan(claude_dir=None, *, days: float | None = 7, now: datetime | None = None) -> Scan:
    """Every transcript under `<claude_dir>/projects` whose session started in
    the last `days` (None: all of them). A file last written before the cutoff
    cannot hold a call inside the window, so it is skipped unread; one that
    cannot be stat'd, opened or read through is counted unreadable."""
    claude_dir = Path(claude_dir) if claude_dir else default_claude_dir()
    root = claude_dir / "projects"
    now = now or datetime.now(timezone.utc)
    since = cutoff = None
    if days is not None:
        try:
            moment = (now - timedelta(days=days)).astimezone(timezone.utc)
            since, cutoff = _iso(moment), moment.timestamp()
        except (OverflowError, ValueError):
            since = cutoff = None
    result = Scan(root=root, since=since)
    if not root.is_dir():
        return result
    owners = hook_owners(claude_dir / "plugins")
    for path in sorted(root.rglob("*.jsonl")):
        try:
            if cutoff is not None and path.stat().st_mtime < cutoff:
                result.files_skipped += 1
                continue
        except OSError:
            result.files_unreadable += 1
            continue
        parts = path.relative_to(root).parts
        session, readable = read_transcript(path, owners,
                                            subagent="subagents" in parts[:-1],
                                            project_dir=root / parts[0])
        if not readable:
            result.files_unreadable += 1
            continue
        result.files_read += 1
        if session is not None and (since is None or session.first_ts >= since):
            result.sessions.append(session)
    return result
