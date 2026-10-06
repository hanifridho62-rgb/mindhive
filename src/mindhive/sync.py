# SPDX-License-Identifier: Apache-2.0
# src/mindhive/sync.py
from pathlib import Path
from . import adaptivetail, context, projector, incidents, events as ev_mod
from .briefing import (briefing as build_briefing_file, headline,
                       _absorbed_keys, _is_absorbed)

# Caps for the compact MEMORY.md digest (KTD8): the file auto-loads into every
# session, so each list section is bounded. Full history stays on disk
# (BRIEFING.md, LEDGER, the event log) for on-demand reads.
#
# Both bound an ITEM COUNT, and no item count bounds LINES — ten lessons can be
# ten lines or two hundred. The native loader reads only the first
# `context.MEMORY_LINE_BUDGET` lines and then says so, which is how this digest
# quietly lost its own tail: the two sections written last are the ones these
# caps exist to protect. `hive sync` measures the file it just wrote
# (`context.memory_pressure`) and warns; `hive context --check` gates on it.
# Trimming here is deliberately NOT automatic — dropping a lesson to fit a line
# budget is a decision, and this module's job is to write the digest, not to
# choose what the operator loses. That decision now has a switch (blueprint P1c):
# `hive context --set-adaptive-tail enabled=1` hands the lessons and decisions to
# `adaptivetail`, which sizes them to the composer's safe budget. Off by default.
# On either path, what the digest leaves out is counted by one rollover notice.
LESSONS_CAP = 10
DECISIONS_CAP = 5

USAGE_NOTE = """\
## 3-Habit Usage

1. START  -> memory auto-loads from native MEMORY.md. Do nothing.
2. DURING -> after a real decision/result/lesson, tell Claude "remember this"
             (Claude runs: hive append decision|caveat|experiment_result "<one line>" --tags ...).
3. END    -> run `hive sync` to refresh the auto-loaded briefing for next session.
             If your Opus usage is about to run out, also run `hive export` for a portable handoff.

Recall details on demand:  hive recall "<query>"   (add --semantic for meaning-based)
See connections:           hive graph related "<thing>"
Full current state:        hive state        Full briefing: hive briefing
"""


def encode_folder(folder: str) -> str:
    """Encode a folder path the way Claude Code names its project directory:
    every character outside ``[A-Za-z0-9]`` becomes a hyphen -- the drive
    colon and the separators, and a dot, a space or an underscore just the
    same. ``.claude`` therefore encodes to ``-claude``, which is why a worktree
    under ``<repo>\\.claude\\worktrees\\x`` is ``<repo>--claude-worktrees-x``
    on disk; the older rule, which kept the dot, wrote a ``<repo>-.claude-...``
    folder Claude never reads (chip task_cb518538).
    Examples:
        C:\\Users\\hanif   -> C--Users-hanif
        C:\\FABIO_HFT_V18  -> C--FABIO-HFT-V18
    """
    return "".join(ch if ch.isascii() and ch.isalnum() else "-" for ch in folder)


def native_memory_path(folder: str, home: Path | None = None) -> Path:
    """Return the native MEMORY.md path for the given folder.

    Path: <home>/.claude/projects/<encode_folder(folder)>/memory/MEMORY.md
    """
    home = home or Path.home()
    encoded = encode_folder(folder)
    return home / ".claude" / "projects" / encoded / "memory" / "MEMORY.md"


def _payload_text(payload) -> str:
    if isinstance(payload, dict):
        return payload.get("text", "")
    return str(payload)


def _caveat_lines(evs, cap=LESSONS_CAP, absorbed=None) -> list:
    """Capped lessons/caveats for the digest: events tagged `pinned` always
    come first and survive the cap (safety-critical caveats must not rotate out
    as newer lessons accumulate — KTD8); the remainder fills by recency.
    Consolidation-absorbed events are excluded so the digest and the briefing
    agree on which memos are visible post-consolidation."""
    absorbed = absorbed if absorbed is not None else set()
    caveats = [ev for ev in evs if ev.type == "caveat"
               and not _is_absorbed(ev, absorbed)]
    pinned = [ev for ev in caveats if "pinned" in (ev.tags or [])]
    rest = [ev for ev in caveats if "pinned" not in (ev.tags or [])]
    rest.reverse()                                  # newest first
    chosen = pinned + rest[:max(0, cap - len(pinned))]
    return [f"- {headline(_payload_text(ev.payload))}" for ev in chosen]


def _decision_lines(evs, cap=DECISIONS_CAP, absorbed=None) -> list:
    absorbed = absorbed if absorbed is not None else set()
    decisions = [ev for ev in evs if ev.type == "decision"
                 and not _is_absorbed(ev, absorbed)]
    decisions.reverse()                             # newest first
    return [f"- {headline(_payload_text(ev.payload))}" for ev in decisions[:cap]]


def _count(evs, kind: str, absorbed) -> int:
    return sum(1 for ev in evs if ev.type == kind and not _is_absorbed(ev, absorbed))


def _loader_reads_whole(sections, project) -> bool:
    """True when the digest *sections* join into fits both of this namespace's
    loader cuts -- the ones `context.memory_pressure` judges the file against."""
    content = "\n".join(sections)
    return (adaptivetail.lf_bytes(content) <= context.load_memory_byte_budget(project)
            and adaptivetail.line_count(content) <= context.load_memory_line_budget(project))


def _adaptive_sections(evs, project, head, rules_block, absorbed, config) -> tuple:
    """``(sections, notice)``: the rollover notice comes back apart, for `sync`
    to append under the one rule both paths share.

    The adaptive digest after the state block (blueprint P1c, BAB 6): SOUL and
    the prevention rules (Tier 1) and the active work session (Tier 2) as fixed
    bytes, then the tail `adaptivetail.compose_adaptive_tail` sizes to what the
    composer's safe budget leaves -- inside the configured caps, pinned lessons
    never cut, what is left out counted by one rollover notice. *head* is the
    digest composed so far (usage note and state block)."""
    soul = incidents.soul_block(project)
    directives = "\n".join(block + "\n" for block in (soul, rules_block) if block)
    active = adaptivetail.active_work_session_block(evs, project)
    header = "\n".join(head)
    lessons = [adaptivetail.TailItem("lesson", ev.ts, headline(_payload_text(ev.payload)),
                                     "pinned" in (ev.tags or []))
               for ev in evs if ev.type == "caveat" and not _is_absorbed(ev, absorbed)]
    decisions = [adaptivetail.TailItem("decision", ev.ts,
                                       headline(_payload_text(ev.payload)))
                 for ev in evs if ev.type == "decision" and not _is_absorbed(ev, absorbed)]
    # `calculate_budget` is sized to the documented loader. A namespace whose
    # operator set a LOWER cut (`memory_bytes` / `memory_lines`) is judged against
    # that cut by `hive context --check`, so the tail gives up the difference.
    safe_bytes, safe_lines = adaptivetail.safe_budgets(
        context.load_memory_byte_budget(project), context.load_memory_line_budget(project))
    room = max(0, adaptivetail.calculate_budget(header, directives, active)
               - (adaptivetail.MAX_LOADER_BYTES - safe_bytes))
    lines = max(0, adaptivetail.calculate_line_budget(header, directives, active)
                - (adaptivetail.MAX_LOADER_LINES - safe_lines))
    body, notice, _ = adaptivetail.compose_adaptive_parts(
        decisions, lessons, room, available_lines=lines,
        lessons_cap=config["lessons"], decisions_cap=config["decisions"])
    return [part for part in ((soul + "\n") if soul else "",
                              (rules_block + "\n") if rules_block else "",
                              active, body) if part], notice


def sync(folder: str, project=None, include_briefing: bool = True,
         home: Path | None = None) -> Path:
    """Write the native MEMORY.md that the desktop app auto-loads.

    Calls projector.project() then (optionally) briefing.briefing() to keep
    STATE.md and the on-disk BRIEFING.md up-to-date, then writes MEMORY.md as a
    compact digest (KTD8): usage note + current state (WITHOUT its appended
    rules section) + one PREVENTION RULES block + capped lessons (pinned first)
    + top decisions + a rollover notice counting what the caps left out. With
    the adaptive tail enabled the part after the state block is
    `_adaptive_sections`. BRIEFING.md is no longer embedded — it stays on disk
    for on-demand reads (`hive briefing`, dashboard). Returns the path written.
    """
    # Read the event log ONCE and reuse it for both projections — avoids the
    # extra full read_all() each of projector.project() and briefing() used to
    # do on every sync. The suggested-skills block is likewise computed ONCE
    # and passed to both compose_state calls (STATE.md + the embedded digest),
    # so the recommend chain runs a single time per sync.
    evs = ev_mod.read_all(project)
    suggestions = projector.suggested_skills_block(project, evs=evs)
    projector.project(project, evs=evs, suggestions=suggestions)

    # Keep the full briefing fresh on disk (on-demand reads), not embedded.
    if include_briefing:
        build_briefing_file(project, evs=evs)

    # Compose native MEMORY.md content
    sections = [USAGE_NOTE]

    # Current state, embedded WITHOUT its appended rules section so prevention
    # rules appear exactly once in MEMORY.md (single block below).
    sections.append("## Current State\n")
    sections.append("```\n"
                    + projector.compose_state(evs, project, include_rules=False,
                                              suggestions=suggestions,
                                              digest=True)
                    + "```\n")

    rules_block = incidents.inject(project, evs=evs)
    absorbed = _absorbed_keys(evs)
    adaptive = context.load_adaptive_tail(project)
    if adaptive["enabled"]:
        tail_sections, notice = _adaptive_sections(evs, project, sections,
                                                   rules_block, absorbed, adaptive)
        sections += tail_sections
    else:
        if rules_block:
            sections.append(rules_block + "\n")

        lessons = _caveat_lines(evs, absorbed=absorbed)
        if lessons:
            sections.append("## Lessons & caveats\n")
            sections.append("\n".join(lessons) + "\n")

        decisions = _decision_lines(evs, absorbed=absorbed)
        if decisions:
            sections.append("## Key decisions\n")
            sections.append("\n".join(decisions) + "\n")

        # The caps still decide what shows; what they leave out is counted and
        # pointed at the semantic index (blueprint P1c: this path too).
        left_out = (_count(evs, "caveat", absorbed) - len(lessons)
                    + _count(evs, "decision", absorbed) - len(decisions))
        notice = adaptivetail.rollover_notice(left_out)

    # One exception, on BOTH paths: a digest that fits the loader WITHOUT the
    # notice and not with it goes out without it -- the notice must never be the
    # thing that gets a digest cut. (The adaptive path once appended it
    # regardless: a fixed part within a notice's length of the cut became an
    # unmanaged truncation, and `hive context --check` exit 1, from it alone.)
    if notice and (_loader_reads_whole(sections + [notice], project)
                   or not _loader_reads_whole(sections, project)):
        sections.append(notice)

    content = "\n".join(sections)

    # Write to native path (create parent dirs)
    from . import paths
    dest = native_memory_path(folder, home=home)
    # Shared writer: `MEMORY.md.tmp` was a fixed name a concurrent sync could
    # clobber, and Claude Code reads MEMORY.md, so the publish is retried.
    paths.write_text_atomic(dest, content)

    return dest
