# SPDX-License-Identifier: Apache-2.0
# src/mindhive/harvest.py
# Deterministic, hook-free auto-capture: turn Claude Code session transcripts
# (~/.claude/projects/<encoded>/<uuid>.jsonl) into MIND HIVE events.
import json
import os
import re
import time
from contextlib import contextmanager
from pathlib import Path
from . import events, hookcapture, paths


def _harvest_lock_path() -> Path:
    return paths.store_file("harvest.lock")


@contextmanager
def harvest_lock(stale_secs: float = 120.0):
    """Cross-process mutex so concurrent harvesters (the dashboard's fast loop,
    the scheduled autosync task, and the CLI doors to the same writer such as
    `hive failures capture`) never process the same transcript lines at once
    and double-write events. Yields True if acquired, False if another
    harvest already holds it. A lock older than ``stale_secs`` is presumed
    orphaned (crashed process) and stolen. Public on purpose: every path that
    walks a transcript directory outside `catch_up` holds it under this name."""
    p = _harvest_lock_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    try:
        if p.exists() and (time.time() - p.stat().st_mtime) > stale_secs:
            p.unlink()
    except OSError:
        pass
    try:
        fd = os.open(str(p), os.O_CREAT | os.O_EXCL | os.O_WRONLY)
    except OSError:               # already held by another harvester
        yield False
        return
    try:
        os.write(fd, str(os.getpid()).encode("utf-8"))
        os.close(fd)
        yield True
    finally:
        try:
            p.unlink()
        except OSError:
            pass

_MARKERS = ("remember:", "memo:", "decision:", "caveat:")
# Hint words only disambiguate the GENERIC markers (remember:/memo:). An explicit
# decision:/caveat: marker is authoritative and never reclassified.
_CAVEAT_HINT = ("bug", "fail", "lesson", "incident", "careful", "don't", "avoid", "broke")
_AMBIGUOUS_MARKERS = ("remember:", "memo:")

_TS_RE = re.compile(r"(\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2})")

# Noise the Claude Code transcript wraps around (or injects beside) a real prompt.
_CMD_ARGS_RE = re.compile(r"<command-args>(.*?)</command-args>", re.DOTALL)
_SYS_REMINDER_RE = re.compile(r"<system-reminder>.*?</system-reminder>", re.DOTALL)
_CMD_TAG_RE = re.compile(r"</?command-(?:message|name|args)>", re.DOTALL)


def _clean_prompt(text: str) -> str:
    """Reduce a user-message body to just what the user actually typed: pull the
    real input out of a slash command's <command-args>, and drop injected
    <system-reminder> blocks and stray command tags."""
    m = _CMD_ARGS_RE.search(text)
    if m:                                 # slash command -> the args ARE the input
        text = m.group(1)
    text = _SYS_REMINDER_RE.sub("", text)
    text = _CMD_TAG_RE.sub("", text)
    return text.strip()


def _is_injected_prompt(text: str) -> bool:
    """Skill bodies arrive as user-role messages but aren't prompts the user typed."""
    return text.lstrip()[:40].lower().startswith(_SKILL_BODY_MARK)


def _is_injected(o, text: str) -> bool:
    """True when this user-ROLE message was not authored by the user.

    Claude Code delivers skill bodies, workflow references and other injected
    documents as user-role messages, and stamps them ``isMeta: True``; a really
    typed prompt carries ``origin``/``permissionMode``/``promptSource`` instead
    and has no ``isMeta``. The envelope is the reliable signal, the text prefix
    is not: only SOME injected bodies open with the skill marker. A reference
    document that merely CONTAINS 'Caveat:' mid-body passed the prefix guard
    and was mined for a memo, minting junk caveats from text the user never
    wrote. The prefix check stays as a fallback for rows without the field."""
    return bool((o or {}).get("isMeta")) or _is_injected_prompt(text)


_CMD_NAME_RE = re.compile(r"<command-name>\s*/?([^<\s]+)\s*</command-name>")
_SKILL_BODY_MARK = "base directory for this skill:"


def _split_skill_name(raw: str) -> dict:
    """{name, plugin?} from a possibly plugin-namespaced skill reference —
    mirrors hookcapture._skill_payload so both capture paths agree."""
    plugin, _, name = str(raw or "").strip().rpartition(":")
    out = {"name": name or str(raw or "").strip()}
    if plugin:
        out["plugin"] = plugin
    return out


def _skill_from_tool_use(inp: dict) -> dict | None:
    """skill_used payload for a Skill tool_use block, or None. Same field
    fallbacks as the live hook (tool_input.skill / .name / .skill_name),
    including the optional capped ``args`` ask, so both capture paths agree
    on the payload shape.

    This reads the CALL, which says nothing about how it went. ``ok`` here is
    only the no-error-seen default `hookcapture._response_ok` also starts
    from; the real outcome comes from `_tool_result_ok` once the call is
    joined to its tool_result, and `outcome_observed` records whether that
    join happened. Stamping ``ok: True`` and stopping — which this function
    used to do — made every invocation a recorded success, so harvest could
    not tell a completed skill from a failed one."""
    for key in ("skill", "name", "skill_name"):
        v = (inp or {}).get(key)
        if isinstance(v, str) and v.strip():
            out = _split_skill_name(v)
            out.update({"ok": True, "text": "harvested Skill tool call"})
            args = (inp or {}).get("args")
            if isinstance(args, str) and args.strip():
                out["args"] = args.strip()[:hookcapture.SKILL_ARGS_MAX]
            return out
    return None


def _tool_result_ok(block: dict, row: dict) -> bool:
    """Whether a joined Skill ``tool_result`` reports a clean run.

    Both signals are read because each covers what the other misses,
    measured across all 86 transcripts (2026-09-03):

    - ``is_error`` on the result block is the failure marker — TRUE on 502
      results corpus-wide, so it is real and used, though absent on all 151
      Skill results in this sample (no Skill call has failed here yet).
    - the row-level ``toolUseResult`` is what Skill results actually carry:
      a dict with ``success`` on 151/151. Where ``is_error`` is true it is a
      plain error STRING instead (502/502), never a dict — so the two can
      never contradict each other, and a missing ``success`` is not evidence
      of failure.

    Same shape as `hookcapture._response_ok`: false only on an explicit
    error marker, never on absence."""
    if block.get("is_error") or block.get("isError"):
        return False
    tur = row.get("toolUseResult")
    if isinstance(tur, dict) and tur.get("success") is False:
        return False
    return True


def _flush_pending_skills(pending) -> list:
    """Skill calls left unmatched at end of file: the tool_result is not in
    the transcript, so no outcome was ever recorded to read.

    They are still emitted. Dropping them would trade a wrong outcome for a
    missing invocation, and 'never called' is the bug this capture path
    exists to fix. Insertion order is call order, so the items come out in
    the order the skills ran."""
    out = []
    if not isinstance(pending, dict):
        return out
    for held in pending.values():
        payload = dict(held["value"])
        payload["outcome_observed"] = False
        out.append({"kind": "skill_used", "value": payload, "ts": held["ts"]})
    pending.clear()
    return out


def _skill_from_command(obj, prev) -> dict | None:
    """skill_used payload for a SLASH-COMMAND skill invocation, or None.
    Slash invocations never fire the Skill tool, so without this the dominant
    real-world invocation path records nothing and every slash-invoked skill
    stays 'never called'.

    The invocation spans TWO adjacent user rows, never one: the operator's
    message carries <command-name>, and the harness answers it with an
    injected ``isMeta`` body row whose ``parentUuid`` is the command row's
    ``uuid``. ``obj`` is that body row, ``prev`` the line before it. An
    earlier version demanded both signals inside a SINGLE message and so
    never fired at all — measured over 25 real transcripts (2026-09-03):
    30 command rows, 64 body rows, 0 rows carrying both.

    Every clause below is load-bearing against a shape seen in that sample:

    - body is ``isMeta`` — a built-in command (/model) answers with an
      ordinary row and injects no skill, so it must not be counted as one.
    - ``parentUuid`` links the pair — adjacency alone would join rows from
      different branches of the conversation tree.
    - the parent carries <command-name> — 43 of the 64 body rows answer a
      Skill TOOL call instead, and `_skill_from_tool_use` already records
      those; minting here too would double-count them.
    - the parent is a ``user`` row — a defensive guard, since the parent of a
      Skill-tool body is an ``attachment``/``assistant`` row. Measured over
      every transcript (86 files) it rejects nothing the <command-name>
      clause had not already rejected, so it is belt-and-braces, not load
      bearing.

    Note what is deliberately NOT required:

    - the body need not open with ``_SKILL_BODY_MARK`` — real skill bodies
      exist that do not (measured: one, full-stack-feature, a 31KB body).
    - the COMMAND row may itself be ``isMeta``. An earlier version of this
      function rejected those on the theory that a machine-written command
      row is the harness announcing a Skill-tool load, already recorded
      elsewhere. That was false: across all 86 transcripts the clause
      rejected exactly 5 rows, every one of them ``workflow-authoring``,
      and ``workflow-authoring`` appears as a Skill tool_use ZERO times —
      so it recorded nothing and the clause left a genuinely-invoked skill
      reading 'never called', which is the bug this function exists to fix.
    """
    if not isinstance(obj, dict) or not isinstance(prev, dict):
        return None
    if not obj.get("isMeta"):
        return None
    parent = obj.get("parentUuid")
    if not parent or parent != prev.get("uuid"):
        return None
    if prev.get("type") != "user":
        return None
    m = _CMD_NAME_RE.search(_user_text((prev.get("message") or {}).get("content")))
    if not m:
        return None
    out = _split_skill_name(m.group(1))
    out.update({"ok": True, "text": "harvested slash-command invocation",
                # This row proves the command was typed and the harness
                # expanded the body. It proves NOTHING about the outcome: no
                # tool ran, so no response was ever read and nothing can ever
                # mark it failed. Without this flag `ok: True` would read as a
                # recorded success and plugins.skill_stats would report a
                # COMPLETION rate built out of typed commands.
                "outcome_observed": False,
                # Per-invocation identity. skill_used otherwise dedups on the
                # skill name alone, which would collapse two real slash
                # invocations of one skill in a session into one event — see
                # _event_key.
                "row": obj.get("uuid")})
    return out


_MAX_WORDS = 5000


def _cap_words(text: str, limit: int = _MAX_WORDS) -> str:
    """Bound a single captured memory (one bubble) to ``limit`` words so a giant
    paste can't bloat the store/UI, while still allowing very long entries."""
    words = text.split()
    if len(words) <= limit:
        return text
    return " ".join(words[:limit]) + " …[truncated]"


def _normalize_ts(raw) -> str | None:
    """Reduce a transcript ISO-8601 timestamp (e.g. '2026-06-20T09:15:30.500Z')
    to the canonical second-resolution UTC form the store uses, or None when it
    can't be parsed."""
    if not isinstance(raw, str):
        return None
    m = _TS_RE.match(raw.strip())
    return (m.group(1) + "Z") if m else None


def _user_text(content) -> str:
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        return " ".join(
            b.get("text", "") for b in content
            if isinstance(b, dict) and b.get("type") == "text"
        )
    return ""


def _items_from_obj(o, prev=None, pending=None) -> list:
    """Parse one transcript row into capture items.

    Two arguments exist because two kinds of skill invocation span more than
    one row, in opposite directions:

    - ``prev`` is the row immediately before this one, for the SLASH join,
      where the body row points back at the command row (see
      `_skill_from_command`).
    - ``pending`` is a caller-owned dict of in-flight Skill TOOL calls, keyed
      by tool_use id. A Skill call is held there and emitted only when its
      ``tool_result`` arrives — which is 1 to 5+ rows later and reachable
      only by id, so no fixed-size lookahead would do. The caller must flush
      what is left with `_flush_pending_skills` at end of file. Pass None to
      opt out of the join, in which case a call is emitted immediately and
      declares no observed outcome, because none was read.

    Every other item is derived from ``o`` alone."""
    items = []
    t = o.get("type")
    msg = o.get("message") or {}
    if t == "assistant":
        content = msg.get("content")
        if isinstance(content, list):
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_use":
                    continue
                name = b.get("name")
                inp = b.get("input") or {}
                if name in ("Edit", "Write", "NotebookEdit"):
                    fp = inp.get("file_path")
                    if fp:
                        plus, minus = hookcapture.line_stats(name, inp)
                        item = {"kind": "file_touched", "value": fp, "tool": name}
                        if plus is not None:
                            if plus == 0 and minus == 0:
                                continue      # +0 −0 is not a change (locked rule)
                            item["plus"], item["minus"] = plus, minus
                        items.append(item)
                elif name in ("Bash", "PowerShell"):
                    cmd = inp.get("command")
                    if cmd:
                        item = {"kind": "command_run",
                                "value": cmd[:hookcapture.CMD_MAX]}
                        desc = inp.get("description")
                        if isinstance(desc, str) and desc.strip():
                            item["desc"] = desc.strip()[:hookcapture.DESC_MAX]
                        items.append(item)
                elif name == "Skill":
                    skill = _skill_from_tool_use(inp)
                    if skill:
                        tid = b.get("id")
                        if isinstance(pending, dict) and tid:
                            # Hold it: the outcome lives in the tool_result,
                            # and emitting here is what made every call a
                            # recorded success. Stamped at the CALL row so
                            # `last_used` says when the skill ran, not when
                            # its output landed (measured gap: median 6.6s,
                            # max 76.7s).
                            pending[tid] = {"value": skill,
                                            "ts": o.get("timestamp")}
                        else:
                            # No id, or a caller that opted out of the join —
                            # nothing can reach the result, so say so rather
                            # than claiming the call went well.
                            skill["outcome_observed"] = False
                            items.append({"kind": "skill_used", "value": skill})
        # Capture Claude's prose response too, so the OUTPUT enters the hive
        # alongside the prompt. Tool-only turns have no text and are skipped.
        resp = _user_text(content).strip()
        if resp:
            items.append({"kind": "assistant_response", "value": _cap_words(resp)})
    elif t == "user":
        # Close out any Skill call this row answers. Every real Skill result
        # is carried by a `user` row (151/151), keyed `tool_use_id`.
        content = msg.get("content")
        if isinstance(pending, dict) and pending and isinstance(content, list):
            for b in content:
                if not isinstance(b, dict) or b.get("type") != "tool_result":
                    continue
                tid = b.get("tool_use_id") or b.get("toolUseId")
                held = pending.pop(tid, None) if tid else None
                if held is None:
                    continue           # someone else's result, or already closed
                payload = dict(held["value"])
                payload["ok"] = _tool_result_ok(b, o)
                payload["outcome_observed"] = True
                items.append({"kind": "skill_used", "value": payload,
                              "ts": held["ts"]})
        text = _user_text(msg.get("content")).strip()
        low = text.lower()
        injected = _is_injected(o, text)
        # Pick the marker that appears FIRST in the text (smallest position),
        # not whichever happens to come first in _MARKERS.
        best = None  # (position, marker)
        for m in _MARKERS:
            i = low.find(m)
            if i != -1 and (best is None or i < best[0]):
                best = (i, m)
        # A memo is an assertion ATTRIBUTED to the user, so only user-authored
        # text may mint one. skill_used below is deliberately left ungated: it
        # is a structural fact about the session, and a slash invocation is
        # recorded ONLY on the injected body it arrives with, so gating it
        # would lose the capture entirely.
        if best is not None and not injected:
            pos, m = best
            val = text[pos + len(m):].strip()
            if val:
                if m == "caveat:":
                    mtype = "caveat"
                elif m == "decision:":
                    mtype = "decision"            # explicit marker is authoritative
                else:                              # remember:/memo: -> infer from hints
                    mtype = "caveat" if any(h in low for h in _CAVEAT_HINT) else "decision"
                # Store the full memo — no truncation. Redundant re-phrasings
                # are collapsed by meaning at display time (see dedupe.py),
                # so the ledger keeps every memory verbatim and complete.
                items.append({"kind": "memo", "value": _cap_words(val), "memo_type": mtype})
        # Capture the typed prompt itself so EVERY prompt enters the hive — not
        # only marker-bearing ones. Skip harness-injected rows — `_is_injected`
        # reads the ``isMeta`` envelope first and falls back to the body mark.
        # The envelope half is load-bearing here because `_skill_from_command`
        # accepts bodies that do NOT carry the mark: without it the very row
        # just recognised as a machine-written skill body is also filed as
        # something the operator typed, and oversee.skill_trails then shows the
        # body's own text as the ask that led to the skill.
        if text and not injected:
            prompt = _clean_prompt(text)
            if prompt:
                items.append({"kind": "user_prompt", "value": _cap_words(prompt)})
        # Slash-command skill invocations live in user messages, not tool_use
        # — and span this row plus the one before it.
        skill = _skill_from_command(o, prev)
        if skill:
            items.append({"kind": "skill_used", "value": skill})
    return items


def iter_transcript_events(path) -> list:
    out = []
    try:
        lines = Path(path).read_text(encoding="utf-8", errors="ignore").splitlines()
    except Exception:
        return out
    prev = None
    pending: dict = {}
    for line in lines:
        line = line.strip()
        if not line:
            continue
        try:
            o = json.loads(line)
        except Exception:
            continue
        out.extend(_items_from_obj(o, prev, pending))
        prev = o
    out.extend(_flush_pending_skills(pending))
    return out


def _hook_captured_sessions(project) -> set:
    """session_ids that already carry a NON-harvest event in this project's
    store — i.e. the live hooks recorded them. For these we dedup at the
    event level (see _existing_event_keys) rather than re-adding actions the
    hooks already wrote. This is what makes harvest safe to run alongside the
    live hooks."""
    out = set()
    for ev in events.read_all(project):
        if "harvest" not in (ev.tags or []):
            out.add(ev.session_id)
    return out


def _event_key(session_id, etype, payload) -> tuple:
    """Dedup identity for one event. skill_used keys on the SKILL NAME — its
    text field differs between the live hook (response excerpt) and harvest
    (fixed marker), so a text key would double-count the same invocation.

    A slash capture additionally carries ``row``, the transcript uuid of the
    body row it was read from: unique per invocation and stable across
    re-reads. Keying on it keeps a re-harvest idempotent while letting one
    session hold several real invocations of the same skill, which the
    name-only key silently discarded. The Skill-TOOL path has no such id and
    keeps the name-only key on purpose — there the risk runs the other way,
    since the live hook and harvest observe the SAME tool_use and must
    collapse to one event."""
    p = payload if isinstance(payload, dict) else {}
    if etype == "skill_used":
        return (session_id, etype, p.get("name"), p.get("row"))
    return (session_id, etype, p.get("text"))


def _existing_event_keys(project) -> set:
    """One _event_key per event already in the store.

    Used to skip re-adding an event the live hooks already captured WITHOUT
    skipping the whole session — so a session the hooks only partially captured
    (e.g. the hook crashed mid-session) still gets its missed events backfilled
    instead of being fast-forwarded past forever."""
    keys = set()
    for ev in events.read_all(project):
        keys.add(_event_key(ev.session_id, ev.type, ev.payload))
    return keys


def _event_for_item(item):
    """Map a parsed transcript item to (event_type, payload, tags)."""
    kind = item["kind"]
    if kind == "file_touched":
        payload = {"text": item["value"]}
        for k in ("plus", "minus", "tool"):
            if k in item:
                payload[k] = item[k]
        return "file_touched", payload, ["harvest", "auto"]
    if kind == "command_run":
        payload = {"text": item["value"]}
        if "desc" in item:
            payload["desc"] = item["desc"]
        return "command_run", payload, ["harvest", "auto"]
    if kind == "memo":
        return item["memo_type"], {"text": item["value"]}, ["harvest", "memo"]
    if kind == "user_prompt":
        return "user_prompt", {"text": item["value"]}, ["harvest", "prompt"]
    if kind == "assistant_response":
        return "assistant_response", {"text": item["value"]}, ["harvest", "response"]
    if kind == "skill_used":
        return "skill_used", dict(item["value"]), ["harvest", "auto", "skill"]
    return None


def _state_path(project):
    return paths.project_dir(project) / "harvest_state.json"


def _load_state(project) -> dict:
    p = _state_path(project)
    if p.exists():
        try:
            return json.loads(paths.read_text_retry(p))
        except Exception:
            return {}
    return {}


def _save_state(project, state):
    paths.project_dir(project).mkdir(parents=True, exist_ok=True)
    # Atomic: `_load_state` answers {} for an unparseable file, so a reader
    # that catches this mid-write does not see an error -- it sees a cursor
    # reset to the beginning, which is the same shape as a first run.
    paths.write_text_atomic(_state_path(project), events.canonical_json(state))


def harvest(transcript_dir, project, now=None, skip_captured=False) -> dict:
    """Append events for NEW transcript lines only (cursor in harvest_state.json).

    With ``skip_captured=True`` any event the live hooks already recorded is
    deduped at the event level, so harvest is safe to run alongside the hooks
    without double-counting — while still backfilling events in a captured
    session that the hooks missed (e.g. after a mid-session hook crash).

    ``unclosed_skill_calls`` counts Skill invocations whose ``tool_result``
    was not in the transcript, so their outcome could not be read; they are
    recorded with ``outcome_observed: False``. It is reported rather than
    swallowed because a silent zero and a silent hundred look identical."""
    d = Path(transcript_dir)
    state = _load_state(project)
    captured = _hook_captured_sessions(project) if skip_captured else set()
    # (session, type, text) keys already in the store — only needed to dedup
    # against hook-captured sessions; grows as we write so in-transcript repeats
    # aren't lost.
    seen_keys = _existing_event_keys(project) if captured else set()
    new_events = 0
    files = 0
    skipped = 0
    unclosed = 0
    if d.exists():
        for f in sorted(d.glob("*.jsonl")):
            files += 1
            try:
                all_lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            cursor = int(state.get(f.name, 0))
            if len(all_lines) <= cursor:
                continue
            sid = f.stem
            is_captured = sid in captured
            wrote = 0
            # Seed with the row BEFORE the cursor. A slash-command skill
            # invocation spans two rows and a harvest tick can land between
            # them; without the seed that pair is split and never joined.
            # Walk BACK to the nearest parseable row rather than looking at
            # one line, so the seed means the same thing as the in-loop
            # `prev` — which also survives blank/unparseable lines via the
            # `continue` paths below. Looking at exactly one line would make
            # the capture depend on where the tick happened to land, the very
            # thing the seed removes.
            prev = None
            for back in range(cursor - 1, -1, -1):
                raw = all_lines[back].strip()
                if not raw:
                    continue
                try:
                    prev = json.loads(raw)
                except Exception:
                    continue
                break
            # In-flight Skill tool calls, keyed by tool_use id (see
            # `_items_from_obj`). Deliberately NOT seeded from before the
            # cursor: a tick landing inside the call/result window cannot see
            # a result the file does not hold yet, and the call was already
            # emitted-as-unobserved on that earlier tick. Seeding would mint a
            # SECOND event for the same call, since `_event_key` cannot tell
            # the two apart. So the outcome of a call split by a tick stays
            # unread — the invocation is never lost, only its result.
            pending: dict = {}
            for line in all_lines[cursor:]:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                # Preserve the real time the action happened (from the transcript
                # line), falling back to `now` only when the line has no timestamp.
                line_ts = _normalize_ts(o.get("timestamp")) or now
                items = _items_from_obj(o, prev, pending)
                prev = o
                for item in items:
                    mapped = _event_for_item(item)
                    if mapped is None:
                        continue
                    etype, payload, tags = mapped
                    if is_captured:
                        # Skip what the hooks already wrote for this session, but
                        # let through anything they missed.
                        key = _event_key(sid, etype, payload)
                        if key in seen_keys:
                            continue
                        seen_keys.add(key)
                    # A joined Skill call is stamped at its CALL row, which is
                    # an earlier line than the one being read.
                    ts = _normalize_ts(item.get("ts")) or line_ts
                    events.append_event(etype, payload, session_id=sid,
                                        tags=tags, ts=ts, project=project)
                    new_events += 1
                    wrote += 1
            for item in _flush_pending_skills(pending):
                mapped = _event_for_item(item)
                if mapped is None:
                    continue
                etype, payload, tags = mapped
                if is_captured:
                    key = _event_key(sid, etype, payload)
                    if key in seen_keys:
                        continue
                    seen_keys.add(key)
                events.append_event(etype, payload, session_id=sid, tags=tags,
                                    ts=_normalize_ts(item.get("ts")) or now,
                                    project=project)
                new_events += 1
                wrote += 1
                unclosed += 1
            state[f.name] = len(all_lines)
            if is_captured and wrote == 0:
                skipped += 1
    _save_state(project, state)
    return {"processed_files": files, "new_events": new_events,
            "skipped_captured": skipped, "unclosed_skill_calls": unclosed}


def backfill_skills(transcript_dir, project, now=None) -> dict:
    """One-shot skill_used backfill over WHOLE transcripts, ignoring the
    harvest cursor — skill invocations before this capture path existed live
    behind cursors the incremental harvest will never revisit. Idempotent on
    `_event_key`, the same identity the incremental harvest dedups on, so
    re-runs and hook overlap never double count while genuine repeat slash
    invocations of one skill in a session still each get an event.
    Returns {"new_skill_used": n, "files": m}."""
    seen: set = set()
    for ev in events.read_all(project):
        if ev.type == "skill_used" and isinstance(ev.payload, dict) \
                and ev.payload.get("name"):
            seen.add(_event_key(ev.session_id, "skill_used", ev.payload))
    d = Path(transcript_dir)
    new = files = 0
    if d.exists():
        for f in sorted(d.glob("*.jsonl")):
            files += 1
            sid = f.stem
            try:
                lines = f.read_text(encoding="utf-8", errors="ignore").splitlines()
            except Exception:
                continue
            prev = None
            pending: dict = {}
            emit = []
            for line in lines:
                line = line.strip()
                if not line:
                    continue
                try:
                    o = json.loads(line)
                except Exception:
                    continue
                line_ts = _normalize_ts(o.get("timestamp")) or now
                for item in _items_from_obj(o, prev, pending):
                    emit.append((item, line_ts))
                prev = o
            # Whole-file pass, so an unmatched call really has no result in
            # the transcript rather than one this tick could not see yet.
            for item in _flush_pending_skills(pending):
                emit.append((item, now))
            for item, line_ts in emit:
                if item["kind"] != "skill_used":
                    continue
                payload = dict(item["value"])
                key = _event_key(sid, "skill_used", payload)
                if key in seen:
                    continue
                seen.add(key)
                events.append_event("skill_used", payload, session_id=sid,
                                    tags=["harvest", "auto", "skill"],
                                    ts=_normalize_ts(item.get("ts")) or line_ts,
                                    project=project)
                new += 1
    return {"new_skill_used": new, "files": files}


def claude_projects_root() -> Path:
    """Where Claude Code stores per-folder session transcripts."""
    return Path.home() / ".claude" / "projects"


def project_for_transcript_dir(d) -> str | None:
    """Infer the MIND HIVE project namespace for a Claude transcript directory
    by reading the first ``cwd`` recorded in any transcript and applying the
    normal project detection. None when no cwd is found (can't be placed)."""
    for f in sorted(Path(d).glob("*.jsonl")):
        try:
            # Stream and stop at the first cwd (it's near the top) instead of
            # loading the whole transcript — these files can be multi-MB.
            with open(f, encoding="utf-8", errors="ignore") as fh:
                for line in fh:
                    if '"cwd"' not in line:
                        continue
                    try:
                        cwd = (json.loads(line) or {}).get("cwd")
                    except (json.JSONDecodeError, ValueError):
                        # A malformed line (e.g. a transcript caught mid-write)
                        # must skip to the next LINE, not abandon the whole file
                        # — otherwise the session is mis-detected as None and
                        # dropped from catch_up entirely.
                        continue
                    if cwd:
                        return paths.detect_project(Path(cwd))
        except OSError:
            continue
    return None


def existing_projects() -> list:
    """Store namespaces that already have an events log — i.e. projects the user
    already tracks in MIND HIVE. Canonical names only (`paths.event_namespaces`):
    a directory no name reaches cannot be harvested into by name either."""
    return paths.event_namespaces()


def catch_up(now=None, skip_captured=True, known_only=True) -> dict:
    """Self-healing fallback: scan every Claude Code transcript directory and
    harvest any session the live hooks didn't capture, into the project its cwd
    maps to. Safe to run repeatedly and alongside the hooks (skip_captured).
    This is what backfills sessions in environments where the hooks never fire
    (e.g. the Agent SDK harness).

    ``known_only`` (default) bounds the blast radius to namespaces the user
    already tracks, so incidental sessions (e.g. run from the home folder) never
    spawn surprise projects. Returns a per-project summary plus any skipped
    namespaces.

    ``unclosed_skill_calls`` (total, and per project) is carried up from
    `harvest`. This is the only harvest path that runs by itself — autosync
    and the dashboard sweep both come through here — so a count that stopped
    at `harvest` would be a count nobody ever reads. `processed_files` and
    `skipped_captured` are dropped as before: neither is a signal anyone
    acts on, while an unclosed call says an invocation went on the record
    with no outcome, so any rate built from it is thinner than it looks."""
    with harvest_lock() as got:
        if not got:               # another harvester is running — skip this tick
            return {"projects": {}, "total_new": 0, "skipped_unknown": [],
                    "unclosed_skill_calls": 0, "locked": True}
        return _catch_up_unlocked(now=now, skip_captured=skip_captured, known_only=known_only)


def _catch_up_unlocked(now=None, skip_captured=True, known_only=True) -> dict:
    # Local import: the Learning-Loop capture stage. Kept local so importing
    # harvest stays cheap and there is no import-time coupling.
    from . import failures, incidents
    root = claude_projects_root()
    known = set(existing_projects()) if known_only else None
    per_project: dict = {}
    skipped_unknown: set = set()
    total_new = 0
    total_unclosed = 0
    if root.exists():
        for d in sorted(root.iterdir()):
            if not d.is_dir():
                continue
            proj = project_for_transcript_dir(d)
            if not proj:
                continue
            if known is not None and proj not in known:
                skipped_unknown.add(proj)
                continue
            r = harvest(d, proj, now=now, skip_captured=skip_captured)
            # Counted for every dir, not only the ones that make the
            # per-project summary, so the total cannot silently undercount.
            total_unclosed += r["unclosed_skill_calls"]
            # Also capture tool FAILURES and advance the incident lifecycle, so
            # the Learning Loop self-feeds from the same automatic sweep instead
            # of requiring a manual `hive failures capture`.
            fr = failures.capture_dir(str(d), project=proj, now=now)
            if fr.get("new_failures"):
                incidents.audit(project=proj)
                incidents.project(project=proj)
                # New failures are when leak counts grow — the moment a nudge
                # can cross the auto-enforcement threshold. Best-effort.
                try:
                    from . import enforce
                    enforce.auto_escalate(project=proj)
                except Exception:
                    pass
            if r["new_events"]:
                # New session activity is exactly what creates fresh rule
                # EXPOSURE, so this is the moment a holding rule can flip to
                # resolved — graduate it to SOUL.md without human input.
                # Best-effort: promotion must never break the capture sweep.
                try:
                    incidents.auto_promote(project=proj)
                except Exception:
                    pass
            if r["new_events"] or fr.get("new_failures"):
                acc = per_project.setdefault(
                    proj, {"new_events": 0, "new_failures": 0, "dirs": 0,
                           "unclosed_skill_calls": 0})
                acc["new_events"] += r["new_events"]
                acc["unclosed_skill_calls"] += r["unclosed_skill_calls"]
                acc["new_failures"] += fr.get("new_failures", 0)
                acc["dirs"] += 1
                total_new += r["new_events"]
    return {"projects": per_project, "total_new": total_new,
            "unclosed_skill_calls": total_unclosed,
            "skipped_unknown": sorted(skipped_unknown)}
