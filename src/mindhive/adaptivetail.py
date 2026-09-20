# SPDX-License-Identifier: Apache-2.0
# src/mindhive/adaptivetail.py
"""Adaptive Tail Extension (blueprint BAB 6 and section 8.5; MEB CTX-01/CTX-02).

The native loader reads MEMORY.md up to 25 000 LF-normalised bytes and 200
lines. Those are the LOADER's numbers and stay in `context`
(`MEMORY_BYTE_BUDGET`, `MEMORY_LINE_BUDGET`), where `hive context --check`
judges a digest against them. This module is the COMPOSER's side of the same
file: a safe budget under that ceiling (`MAX_LOADER_BYTES` 24 500,
`MAX_LOADER_LINES` 195), what is left of it once the fixed part of the digest
is measured (`calculate_budget`, `calculate_line_budget`), and a tail composed
to fit what is left (`compose_adaptive_tail`).

Tiered retention (BAB 6.3):

* Tier 1 -- pinned lessons, the mandatory SOPs, the SOUL block -- is never
  trimmed. Mandatory SOPs and SOUL ride the FIXED part of the digest (the state
  block and `incidents.soul_block`), so they are charged before the tail is
  sized; pinned lessons open the tail's lessons section whatever the budget.
* Tier 2 -- the active work session (`active_work_session_block`) -- is fixed
  bytes too, carried while its session is still active.
* Tier 3 -- decisions and ordinary lessons -- fills by recency, newest first.
  The Dreaming Pass runs as the fill does: an item at least
  `DREAMING_THRESHOLD` similar to one already shown is not shown again. The
  first item that does not fit ends the fill, so an older memo is never shown
  in place of a newer one.

Everything Tier 3 does not show is counted by ONE notice, `ROLLOVER_TEMPLATE`
(MEB verbatim), which points at the semantic index the ledger already feeds
(`hive embed`, `hive recall --semantic`). Without the `vectors` extras the
pointer says what to install, and names the Postgres tool when the event
backend is ``dual`` or ``pg`` (plan review I.3).

The budget has no floor (plan review KRITIK 3): section 8.5's ``max(1500, ...)``
forced a tail past the loader's cut whenever the fixed part was large. 1 500
survives as `MIN_TAIL_BYTES`, a THRESHOLD: below it no Tier-3 item is shown at
all, only the notice, and the 800-byte safety margin is what holds the notice.

Opt-in: `sync` composes this tail only when ``adaptive_tail.enabled`` is set in
``context_budget.json`` (`load_adaptive_tail`, ``hive context
--set-adaptive-tail``); the default digest keeps its item caps and only gains
the notice when those caps leave items out.
"""
from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass

from . import paths
from .core.dreaming import DREAMING_THRESHOLD, dreaming_pass, jaccard_similarity, tokens

MAX_LOADER_BYTES = 24500
MAX_LOADER_LINES = 195
MIN_TAIL_BYTES = 1500
SAFETY_MARGIN_BYTES = 800
# The joins between the fixed sections and the tail (four at most) plus the
# notice's three lines when no Tier-3 item fits: the line-side 800 bytes.
SAFETY_MARGIN_LINES = 8
# What the safe budget keeps under the documented loader cut (25 000 bytes, 200
# lines); `safe_budgets` keeps the same distance under a lower, operator-set cut.
LOADER_MARGIN_BYTES = 500
LOADER_MARGIN_LINES = 5

ROLLOVER_TEMPLATE = ("\n... {n} keputusan dan pelajaran dialihkan ke basis data vektor "
                     "sekunder (ChromaDB).\nPencarian semantik lengkap: "
                     "`hive recall --semantic \"\"`\n")
EXTRAS_SUFFIX = " (memerlukan instalasi extras: pip install -e .[vectors])"
PG_SUFFIX = " atau: hive pg tool search_events_by_type"
PG_BACKENDS = frozenset({"dual", "pg"})
# The count is bounded: `int` refuses a numeral past 4 300 digits, and the
# pressure gate that reads this must never raise on a hand-edited file.
_NOTICE = re.compile(
    r"^\.\.\. (?P<n>\d{1,18}) keputusan dan pelajaran dialihkan ke basis data vektor "
    r"sekunder \(ChromaDB\)\.$(?:\n^Pencarian semantik lengkap: [^\n]*$)?", re.M)
_FENCE = re.compile(r"^```", re.M)

LESSONS_HEADING = "## Lessons & caveats"
DECISIONS_HEADING = "## Key decisions"
ACTIVE_SESSION_HEADING = "## Active work session"

ADAPTIVE_TAIL_KEY = "adaptive_tail"
DEFAULT_LESSONS_CAP = 10        # == sync.LESSONS_CAP (pinned by the tests)
DEFAULT_DECISIONS_CAP = 5       # == sync.DECISIONS_CAP
_SETTINGS = ("enabled", "lessons", "decisions")
_TRUE = frozenset({"1", "true", "yes", "on"})
_FALSE = frozenset({"0", "false", "no", "off"})
_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


# --- measuring the fixed part ---------------------------------------------------------

def lf_text(text) -> str:
    """*text* as the loader reads it: CRLF folded to LF."""
    return str(text).replace("\r\n", "\n")


def lf_bytes(text) -> int:
    """UTF-8 bytes of *text* after CRLF -> LF (``"a\\r\\nb"`` is 3)."""
    return len(lf_text(text).encode("utf-8"))


def line_count(text) -> int:
    """Lines of *text* the way `context.memory_pressure` counts them."""
    return len(lf_text(text).splitlines())


def _amount(value, measure, what: str) -> int:
    if value is None:
        return 0
    if isinstance(value, str):
        return measure(value)
    if isinstance(value, bool) or not isinstance(value, int):
        raise TypeError(f"{what} must be a byte/line count or the text itself, "
                        f"got {type(value).__name__}")
    if value < 0:
        raise ValueError(f"{what} must be zero or more, got {value}")
    return value


def calculate_budget(header_bytes, directives_bytes, active_session_bytes) -> int:
    """Bytes left for the tail: ``max(0, MAX_LOADER_BYTES - fixed - 800)``.

    Each part is a byte count or the text itself, measured LF-normalised. No
    floor: a large fixed part leaves 0, never a tail that overruns the loader."""
    fixed_bytes = (_amount(header_bytes, lf_bytes, "header_bytes")
                   + _amount(directives_bytes, lf_bytes, "directives_bytes")
                   + _amount(active_session_bytes, lf_bytes, "active_session_bytes"))
    # Normalisasi LF murni, sisakan margin keselamatan 800 byte
    available = MAX_LOADER_BYTES - fixed_bytes - SAFETY_MARGIN_BYTES
    return max(0, available)


def calculate_line_budget(header_lines, directives_lines, active_session_lines) -> int:
    """Lines left for the tail: ``max(0, MAX_LOADER_LINES - fixed - 8)``; each
    part is a line count or the text itself."""
    fixed = (_amount(header_lines, line_count, "header_lines")
             + _amount(directives_lines, line_count, "directives_lines")
             + _amount(active_session_lines, line_count, "active_session_lines"))
    return max(0, MAX_LOADER_LINES - fixed - SAFETY_MARGIN_LINES)


def safe_budgets(loader_bytes: int | None = None,
                 loader_lines: int | None = None) -> tuple[int, int]:
    """The composer's safe ``(bytes, lines)`` under a loader cutting at
    *loader_bytes* / *loader_lines*: `MAX_LOADER_BYTES` / `MAX_LOADER_LINES` for
    the documented loader or a looser one, and the same margin under a cut an
    operator set LOWER (``memory_bytes`` / ``memory_lines``), so the digest that
    `hive context --check` judges against that cut still ends before it."""
    nbytes = MAX_LOADER_BYTES if loader_bytes is None else min(
        MAX_LOADER_BYTES, max(0, int(loader_bytes) - LOADER_MARGIN_BYTES))
    nlines = MAX_LOADER_LINES if loader_lines is None else min(
        MAX_LOADER_LINES, max(0, int(loader_lines) - LOADER_MARGIN_LINES))
    return nbytes, nlines


# --- the rollover notice -----------------------------------------------------------------

def rollover_notice(count: int, *, vectors_available: bool | None = None,
                    backend: str | None = None) -> str:
    """`ROLLOVER_TEMPLATE` for *count* items, "" for none. Unavailable vectors
    (``None`` asks `vectors.has_vectors_support`) suffix the second line with
    the install hint, and with the Postgres tool when *backend* (default
    ``MINDHIVE_EVENT_BACKEND``) is ``dual`` or ``pg``."""
    count = int(count)
    if count <= 0:
        return ""
    if vectors_available is None:
        from . import vectors
        vectors_available = vectors.has_vectors_support()
    text = ROLLOVER_TEMPLATE.format(n=count)
    if vectors_available:
        return text
    suffix = EXTRAS_SUFFIX
    if backend is None:
        backend = os.environ.get("MINDHIVE_EVENT_BACKEND", "")
    if str(backend).strip().lower() in PG_BACKENDS:
        suffix += PG_SUFFIX
    return text[:-1] + suffix + "\n"


def find_rollover(text) -> dict | None:
    """The LAST rollover notice of the composer's own in *text*: ``{count,
    start, line_end, end, bytes}`` as character offsets (``line_end`` closes the
    "... N" line, ``end`` the pointer line when present), or None.

    A notice between the FIRST and the LAST ``` line of the digest is a QUOTE --
    that span is the fenced state block, which carries SOP bodies raw -- and is
    passed over. The span, not the parity of the fences before the match: an SOP
    body with one ``` line it never closes made the count odd for the rest of
    the file, and the composer's own notice, written after the block, went
    unseen. A single fence line closes nothing, so everything after it is
    treated as quoted: the reading that reports a loss rather than hides one."""
    text = lf_text(text or "")
    fences = [m.start() for m in _FENCE.finditer(text)]
    lo = fences[0] if fences else len(text)
    hi = fences[-1] if len(fences) > 1 else len(text)
    found = None
    for m in _NOTICE.finditer(text):
        if not lo <= m.start() <= hi:
            found = m
    if found is None:
        return None
    first = found.group(0).split("\n", 1)[0]
    return {"count": int(found["n"]), "start": found.start(),
            "line_end": found.start() + len(first), "end": found.end(),
            "bytes": len(found.group(0).encode("utf-8"))}


# --- the items -------------------------------------------------------------------------

@dataclass(frozen=True)
class TailItem:
    """One Tier-3 candidate (or a pinned Tier-1 lesson)."""
    kind: str       # "lesson" | "decision"
    ts: str
    text: str
    pinned: bool = False


def tail_item(obj, kind: str) -> TailItem | None:
    """A `TailItem` from a `TailItem`, an event (``.ts``, ``.payload``, ``.tags``),
    a dict (``text``/``active_rule``, ``ts``/``last_seen``, ``pinned`` or a
    ``pinned`` tag) or a plain string. Whitespace collapses to one line; an item
    with no text is None."""
    if isinstance(obj, TailItem):
        ts, text, pinned = obj.ts, obj.text, obj.pinned
    elif isinstance(obj, str):
        ts, text, pinned = "", obj, False
    elif isinstance(obj, dict):
        ts = obj.get("ts") or obj.get("last_seen") or ""
        text = obj.get("text") or obj.get("active_rule") or ""
        pinned = bool(obj.get("pinned")) or "pinned" in (obj.get("tags") or ())
    else:
        payload = getattr(obj, "payload", None)
        ts = getattr(obj, "ts", "") or ""
        text = (payload.get("text") or "") if isinstance(payload, dict) else (payload or "")
        pinned = "pinned" in (getattr(obj, "tags", None) or ())
    text = " ".join(str(text).split())
    return TailItem(kind=kind, ts=str(ts), text=text, pinned=pinned) if text else None


def _line(item: TailItem) -> str:
    day = item.ts[:10]
    return f"- [{day}] {item.text}" if _DATE.fullmatch(day) else f"- {item.text}"


def _render(lesson_lines, decision_lines, notice: str) -> str:
    """The sections `sync` has always written, then the notice: parts joined
    with a newline, the join `sync` uses for the whole digest."""
    parts = []
    if lesson_lines:
        parts += [LESSONS_HEADING + "\n", "\n".join(lesson_lines) + "\n"]
    if decision_lines:
        parts += [DECISIONS_HEADING + "\n", "\n".join(decision_lines) + "\n"]
    if notice:
        parts.append(notice)
    return "\n".join(parts)


# --- composing the tail -----------------------------------------------------------------

def compose_adaptive_tail(decisions, lessons, available_bytes: int, *,
                          available_lines: int | None = None,
                          lessons_cap: int | None = None,
                          decisions_cap: int | None = None,
                          similarity=None, threshold: float = DREAMING_THRESHOLD,
                          vectors_available: bool | None = None) -> tuple[str, int]:
    """``(markdown, rollover_count)`` for the digest's tail: the two pieces of
    `compose_adaptive_parts` joined the way `sync` joins its sections."""
    body, notice, rolled = compose_adaptive_parts(
        decisions, lessons, available_bytes, available_lines=available_lines,
        lessons_cap=lessons_cap, decisions_cap=decisions_cap,
        similarity=similarity, threshold=threshold,
        vectors_available=vectors_available)
    return "\n".join(part for part in (body, notice) if part), rolled


def compose_adaptive_parts(decisions, lessons, available_bytes: int, *,
                           available_lines: int | None = None,
                           lessons_cap: int | None = None,
                           decisions_cap: int | None = None,
                           similarity=None, threshold: float = DREAMING_THRESHOLD,
                           vectors_available: bool | None = None) -> tuple[str, str, int]:
    """``(body, notice, rollover_count)`` for the digest's tail -- the notice
    apart, because `sync` withholds it from a digest it alone would push past
    the loader cut.

    *decisions* and *lessons* are in ledger order (oldest first; see `tail_item`
    for the shapes taken). Pinned lessons always open the lessons section and are
    never rolled over. The rest fill newest first while the tail -- the notice it
    will need included, and none when every item fits without one -- stays
    within *available_bytes* and *available_lines*
    and the per-kind caps (pinned lessons count toward *lessons_cap*); a
    near-duplicate of a shown item (*similarity*, default token Jaccard, at
    *threshold*) is not shown. Below `MIN_TAIL_BYTES` nothing but pinned lessons
    and the notice is emitted.

    The bound, as it is: the BODY exceeds the budget only when the pinned
    lessons alone do. Body and notice together keep it whenever a Tier-3 item is
    shown -- the fill charged the notice for that item. A tail that shows none
    is the pinned lessons and the notice as they are, neither one cut here, so
    it can run over by at most that one notice: what the safety margins in
    `calculate_budget` / `calculate_line_budget` are sized to hold."""
    if not (0.0 <= threshold <= 1.0):
        raise ValueError(f"threshold harus dalam [0, 1]: {threshold!r}")
    budget_bytes = max(0, int(available_bytes))
    budget_lines = None if available_lines is None else max(0, int(available_lines))
    ds = [i for i in (tail_item(o, "decision") for o in decisions or ()) if i]
    ls = [i for i in (tail_item(o, "lesson") for o in lessons or ()) if i]
    pinned = [i for i in ls if i.pinned]
    pool = ds + [i for i in ls if not i.pinned]
    order = sorted(range(len(pool)), key=lambda k: (pool[k].ts, k), reverse=True)
    if vectors_available is None and pool:
        from . import vectors
        vectors_available = vectors.has_vectors_support()
    reserve = rollover_notice(len(pool), vectors_available=vectors_available)
    bags: dict = {}                 # token bags and pair scores, keyed by identity:
    scores: dict = {}               # each is computed once, whichever fill asks

    def bag(item):
        if id(item) not in bags:
            bags[id(item)] = tokens(item.text)
        return bags[id(item)]

    def near(item, kept) -> bool:
        for other in kept:
            pair = (id(item), id(other))
            if pair not in scores:
                scores[pair] = (jaccard_similarity(bag(item), bag(other))
                                if similarity is None else similarity(item, other))
            if scores[pair] >= threshold:
                return True
        return False

    def fill(reserving: bool):
        """``(lesson lines, decision lines, rolled)``. Not *reserving*, the fill
        charges no notice and gives up (None) at the first item it would roll."""
        shown_l = [_line(i) for i in pinned]
        shown_d: list = []
        kept = list(pinned)
        rolled = 0
        filling = budget_bytes >= MIN_TAIL_BYTES
        for pos, k in enumerate(order):
            if rolled and not reserving:
                return None
            item = pool[k]
            lesson = item.kind == "lesson"
            cap = lessons_cap if lesson else decisions_cap
            if not filling or (cap is not None
                               and len(shown_l if lesson else shown_d) >= cap):
                rolled += 1
                continue
            if near(item, kept):
                rolled += 1
                continue
            line = _line(item)
            trial_l = shown_l + [line] if lesson else shown_l
            trial_d = shown_d if lesson else shown_d + [line]
            more = reserving and (rolled > 0 or pos < len(order) - 1)
            md = _render(trial_l, trial_d, reserve if more else "")
            if lf_bytes(md) <= budget_bytes and (budget_lines is None
                                                 or line_count(md) <= budget_lines):
                shown_l, shown_d = trial_l, trial_d
                kept.append(item)
            else:
                filling = False
                rolled += 1
        return None if rolled and not reserving else (shown_l, shown_d, rolled)

    # A tail that shows every item needs no notice, so none is charged for it;
    # only when something must roll is the fill redone with the notice reserved.
    shown_l, shown_d, rolled = fill(False) or fill(True)
    notice = rollover_notice(rolled, vectors_available=vectors_available)
    return _render(shown_l, shown_d, ""), notice, rolled


def active_work_session_block(evs, project: str | None = None) -> str:
    """Tier 2: the newest still-active work session's current step with its four
    tags, or "" when none is active."""
    from . import worksessions
    live = [w for w in worksessions.fold(evs, project).values()
            if w.status == "active" and w.steps]
    if not live:
        return ""
    order = sorted(range(len(live)), key=lambda i: (live[i].steps[-1].timestamp, i))
    w = live[order[-1]]
    step = w.steps[w.active_step_index]
    return "\n".join([
        ACTIVE_SESSION_HEADING, "",
        f"- session {w.session_id} · phase {step.phase_tag} · step "
        f"{w.active_step_index + 1}/{len(w.steps)} {step.step_id} ({step.status})",
        f"- {step.technical_action}", f"- {step.initiative_tag}", f"- {step.usage_tag}",
    ]) + "\n"


# --- the opt-in, in context_budget.json ------------------------------------------------

def _sidecar(project):
    from .context import BUDGET_FILE        # lazy: `context` re-exports this module
    return paths.project_dir(project) / BUDGET_FILE


def load_adaptive_tail(project=None) -> dict:
    """``{enabled, lessons, decisions}`` from ``context_budget.json``'s
    ``adaptive_tail``. Total: a missing or corrupt value reads as the default
    (off; the digest's own caps)."""
    out = {"enabled": False, "lessons": DEFAULT_LESSONS_CAP,
           "decisions": DEFAULT_DECISIONS_CAP}
    try:
        raw = json.loads(_sidecar(project).read_text(encoding="utf-8"))
    except Exception:
        return out
    node = raw.get(ADAPTIVE_TAIL_KEY) if isinstance(raw, dict) else None
    if not isinstance(node, dict):
        return out
    if isinstance(node.get("enabled"), bool):
        out["enabled"] = node["enabled"]
    for key in ("lessons", "decisions"):
        value = node.get(key)
        if isinstance(value, int) and not isinstance(value, bool) and value >= 0:
            out[key] = value
    return out


def save_adaptive_tail(project=None, *, enabled: bool | None = None,
                       lessons: int | None = None, decisions: int | None = None) -> dict:
    """Merge the given settings into ``adaptive_tail`` and return the result;
    every other key of the sidecar is kept as it was."""
    given = {"enabled": enabled, "lessons": lessons, "decisions": decisions}
    for key in ("lessons", "decisions"):
        value = given[key]
        if value is not None and (isinstance(value, bool) or not isinstance(value, int)
                                  or value < 0):
            raise ValueError(f"{key} must be zero or more, got {value!r}")
    if enabled is not None and not isinstance(enabled, bool):
        raise ValueError(f"enabled must be true or false, got {enabled!r}")
    path = _sidecar(project)
    try:
        raw = json.loads(path.read_text(encoding="utf-8"))
        data = raw if isinstance(raw, dict) else {}
    except Exception:
        data = {}
    node = load_adaptive_tail(project)
    node.update({k: v for k, v in given.items() if v is not None})
    data[ADAPTIVE_TAIL_KEY] = node
    paths.write_text_atomic(path, json.dumps(data, indent=2, sort_keys=True))
    return dict(node)


def parse_adaptive_tail_spec(text) -> dict:
    """``"lessons=20,decisions=8,enabled=1"`` -> ``{"lessons": 20, "decisions": 8,
    "enabled": True}``; `ValueError` naming the bad part otherwise."""
    usage = "use lessons=N,decisions=N,enabled=0|1"
    out: dict = {}
    for part in str(text).split(","):
        key, sep, value = (s.strip() for s in part.partition("="))
        key = key.lower()
        if not part.strip():
            continue
        if not sep or key not in _SETTINGS:
            raise ValueError(f"unknown adaptive-tail setting {part.strip()!r} ({usage})")
        if key in out:
            raise ValueError(f"{key} is given twice ({usage})")
        if key == "enabled":
            if value.lower() not in _TRUE | _FALSE:
                raise ValueError(f"enabled must be 0 or 1, got {value!r}")
            out[key] = value.lower() in _TRUE
        elif re.fullmatch(r"\d+", value):
            if len(value) > 6:
                raise ValueError(f"{key} must be at most 999999, got {value!r}")
            out[key] = int(value)
        else:
            raise ValueError(f"{key} must be zero or more, got {value!r}")
    if not out:
        raise ValueError(f"no adaptive-tail setting given ({usage})")
    return out


__all__ = [
    "MAX_LOADER_BYTES", "MAX_LOADER_LINES", "MIN_TAIL_BYTES", "SAFETY_MARGIN_BYTES",
    "SAFETY_MARGIN_LINES", "LOADER_MARGIN_BYTES", "LOADER_MARGIN_LINES",
    "DREAMING_THRESHOLD", "ROLLOVER_TEMPLATE", "EXTRAS_SUFFIX",
    "PG_SUFFIX", "PG_BACKENDS", "LESSONS_HEADING", "DECISIONS_HEADING",
    "ACTIVE_SESSION_HEADING", "ADAPTIVE_TAIL_KEY", "DEFAULT_LESSONS_CAP",
    "DEFAULT_DECISIONS_CAP", "lf_text", "lf_bytes", "line_count", "calculate_budget",
    "calculate_line_budget", "safe_budgets", "rollover_notice", "find_rollover",
    "TailItem", "tail_item",
    "compose_adaptive_parts", "compose_adaptive_tail", "active_work_session_block",
    "dreaming_pass",
    "load_adaptive_tail", "save_adaptive_tail", "parse_adaptive_tail_spec",
]
