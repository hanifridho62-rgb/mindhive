# src/mindhive/sync.py
from pathlib import Path
from . import paths, projector
from .briefing import briefing as build_briefing_file

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


def sync(folder: str, project=None, include_briefing: bool = True,
         home: Path | None = None) -> Path:
    """Write the native MEMORY.md that the desktop app auto-loads.

    Calls projector.project() then (optionally) briefing.briefing() to ensure
    STATE.md and BRIEFING.md are up-to-date. Then writes native MEMORY.md.
    Returns the path written.
    """
    # Ensure projections are up to date
    projector.project(project)

    # Ensure briefing (if needed)
    briefing_path = None
    if include_briefing:
        briefing_path = build_briefing_file(project)

    # Resolve paths
    pdir = paths.project_dir(project)
    state_path = pdir / "STATE.md"

    # Compose native MEMORY.md content
    sections = []
    sections.append(USAGE_NOTE)

    # STATE section
    sections.append("## Current State\n")
    if state_path.exists():
        sections.append("```\n" + state_path.read_text(encoding="utf-8") + "\n```\n")
    else:
        sections.append("*(no STATE.md yet — run: hive project)*\n")

    # BRIEFING section
    if include_briefing and briefing_path and briefing_path.exists():
        sections.append("## Full Briefing\n")
        sections.append("```\n" + briefing_path.read_text(encoding="utf-8") + "\n```\n")

    content = "\n".join(sections)

    # Write to native path (create parent dirs)
    dest = native_memory_path(folder, home=home)
    dest.parent.mkdir(parents=True, exist_ok=True)
    tmp = dest.parent / "MEMORY.md.tmp"
    tmp.write_text(content, encoding="utf-8")
    tmp.replace(dest)

    return dest
