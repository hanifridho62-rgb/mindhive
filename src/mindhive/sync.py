# src/mindhive/sync.py
from pathlib import Path
from . import projector, incidents, events as ev_mod
from .briefing import (briefing as build_briefing_file, headline,
                       _absorbed_keys, _is_absorbed)

# Caps for the compact MEMORY.md digest (KTD8): the file auto-loads into every
# session, so each list section is bounded. Full history stays on disk
# (BRIEFING.md, LEDGER, the event log) for on-demand reads.
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
    """Encode a folder path to match Claude Code's project-dir encoding.

    Replaces each of: : \\ / _ with a hyphen.
    Examples:
        C:\\Users\\hanif   -> C--Users-hanif
        C:\\FABIO_HFT_V18  -> C--FABIO-HFT-V18
    """
    result = []
    for ch in folder:
        if ch in (":", "\\", "/", "_"):
            result.append("-")
        else:
            result.append(ch)
    return "".join(result)


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


def sync(folder: str, project=None, include_briefing: bool = True,
         home: Path | None = None) -> Path:
    """Write the native MEMORY.md that the desktop app auto-loads.

    Calls projector.project() then (optionally) briefing.briefing() to keep
    STATE.md and the on-disk BRIEFING.md up-to-date, then writes MEMORY.md as a
    compact digest (KTD8): usage note + current state (WITHOUT its appended
    rules section) + one PREVENTION RULES block + capped lessons (pinned first)
    + top decisions. BRIEFING.md is no longer embedded — it stays on disk for
    on-demand reads (`hive briefing`, dashboard). Returns the path written.
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
                                              suggestions=suggestions)
                    + "```\n")

    rules_block = incidents.inject(project, evs=evs)
    if rules_block:
        sections.append(rules_block + "\n")

    absorbed = _absorbed_keys(evs)
    lessons = _caveat_lines(evs, absorbed=absorbed)
    if lessons:
        sections.append("## Lessons & caveats\n")
        sections.append("\n".join(lessons) + "\n")

    decisions = _decision_lines(evs, absorbed=absorbed)
    if decisions:
        sections.append("## Key decisions\n")
        sections.append("\n".join(decisions) + "\n")

    content = "\n".join(sections)

    # Write to native path (create parent dirs)
    dest = native_memory_path(folder, home=home)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / "MEMORY.md.tmp"
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(dest)

    return dest
