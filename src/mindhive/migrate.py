# SPDX-License-Identifier: Apache-2.0
# src/mindhive/migrate.py
import re
from pathlib import Path
from . import events

_DATE = re.compile(r"\b(20\d{2}-\d{2}-\d{2})\b")


def _classify(block: str) -> str:
    low = block.lower()
    if any(w in low for w in ("insiden", "lesson", "honest", "caveat", "jujur")):
        return "caveat"
    if any(w in low for w in ("a/b", "result", "ic_", "net ", "pnl", "verdict")):
        return "experiment_result"
    return "decision"


def import_memory_file(path, project=None) -> int:
    """Deterministic header/paragraph split of a legacy memory .md into seed events.

    Splits on blank-line-separated blocks; each block with a YYYY-MM-DD gets that
    date as ts; blocks without a date inherit the previous date (or a default).
    """
    text = Path(path).read_text(encoding="utf-8")
    blocks = [b.strip() for b in re.split(r"\n\s*\n", text) if b.strip()]
    count = 0
    last_date = "2000-01-01"
    for i, block in enumerate(blocks):
        if block.startswith("#"):
            continue
        m = _DATE.search(block)
        if m:
            last_date = m.group(1)
        ts = f"{last_date}T{(i // 3600) % 24:02d}:{(i // 60) % 60:02d}:{i % 60:02d}Z"
        events.append_event(_classify(block), {"text": block},
                            session_id="seed-import", tags=["seed"], ts=ts,
                            project=project)
        count += 1
    return count


def disable_pensyve(config_path) -> Path:
    p = Path(config_path)
    lines = []
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            if line.strip().startswith(("context_loading:", "prompt_enrichment:")):
                continue
            lines.append(line)
    lines.append("context_loading: off")
    lines.append("prompt_enrichment: false")
    p.write_text("\n".join(lines) + "\n", encoding="utf-8")
    return p


def write_memory_pointer(memory_md_path, project: str, one_line_state: str) -> Path:
    p = Path(memory_md_path)
    body = (
        "# Memory Index\n\n"
        f"Project/episodic memory now lives in MIND HIVE (`~/.mindhive/{project}/`).\n"
        "The SessionStart hook loads a brief of `STATE.md` (`hive state` prints all of it); use `hive recall \"<query>\"` for history.\n\n"
        f"**Fallback (if a hook fails):** active project `{project}` — {one_line_state}\n"
    )
    p.write_text(body, encoding="utf-8")
    return p
