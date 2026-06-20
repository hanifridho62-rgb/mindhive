# src/mindhive/export.py
from pathlib import Path
from . import paths


def export(project=None, recent=20, out=None) -> Path:
    pdir = paths.project_dir(project)
    parts = ["# MIND HIVE HANDOFF", ""]
    state = pdir / "STATE.md"
    parts.append(state.read_text(encoding="utf-8") if state.exists() else "(no STATE.md)")
    parts.append("\n## Recent ledger entries\n")
    ldir = pdir / "LEDGER"
    files = sorted(ldir.glob("*.md")) if ldir.exists() else []
    for f in files[-recent:]:
        parts.append(f"### {f.name}\n")
        parts.append(f.read_text(encoding="utf-8"))
        parts.append("")
    out_path = Path(out) if out else (pdir / "handoff.md")
    out_path.write_text("\n".join(parts), encoding="utf-8")
    return out_path
