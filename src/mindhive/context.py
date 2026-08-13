# src/mindhive/context.py
"""Context-budget analyzer for the always-on session surface.

Mind Hive's operating rule is that nothing unbounded may ship into auto-loaded
context, and the delivery caps exist to enforce it — ``LESSONS_CAP`` (sync),
``DECISIONS_CAP`` (sync), ``_TIMELINE_CAP`` (briefing), ``SUGGEST_CAP``
(projector). Every one of those bounds an *item count*. None bounds a *size*.
Ten lessons can be 500 bytes or 50 KB and the caps cannot tell the difference,
so the artifacts they nominally protect grew unmeasured: at the time this module
was written STATE.md was 37 KB and the native MEMORY.md 23 KB, together roughly
16,000 estimated tokens paid at the start of every single session.

This module measures that surface. It never trims or rewrites an artifact, and
it never changes what the sync and briefing paths deliver. It reports what the
cost is and which contributors are worth trimming; deciding to act on that
stays with the operator. The one thing it persists is the operator-set token
**budget** (``context_budget.json`` beside the namespace artifacts — the same
operator-config sidecar pattern as ``rules/enforce.json``): ``hive context
--check`` compares the measured total against it and exits non-zero when over,
which is the enforcement hook adoption flows and scripts gate on. Without a
persisted budget, ``over_budget`` was computed and thrown away every run — a
silent regression path for every new plugin or adopted skill.

Two origins are measured:

``hive``    the artifacts this tool itself maintains — STATE.md, BRIEFING.md,
            SOUL.md in the namespace directory, plus the native MEMORY.md that
            the desktop app auto-loads (``sync.native_memory_path``).
``claude``  the wider always-on load — the user's CLAUDE.md and the trigger
            description of every installed plugin skill, which is resident in
            context before any work begins.

The pipeline is four stages over one currency, the ``Segment``: collect (the
only stage that touches the filesystem), ``analyze`` (totals, ranking, share,
budget delta), ``findings`` (rule-based trim suggestions), and render. Analysis,
findings and rendering are origin-agnostic — they only ever see ``Segment`` — so
a new context source needs a collector plus its CLI wiring, and nothing in the
three downstream stages changes.

Reads stay total. This walks user configuration written by many tools over a
long time and will meet truncated frontmatter, invalid UTF-8, and directories
that no longer exist. Every collector skips what it cannot parse and returns
what it could — a malformed file must never take down a report.

Token figures are **estimates**. The package declares no third-party
dependencies, so an exact tokenizer is out of scope for a reporting tool;
``estimate_tokens`` is a documented character-ratio heuristic and is the single
place to swap in an exact backend if that ever becomes worth a dependency."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, asdict
from pathlib import Path

from . import outcomes, paths

# Average characters per token for English prose and markdown. Real tokenizers
# land near this for ordinary text; dense punctuation or CJK diverge. Used only
# to rank contributors against each other, where a consistent ratio matters far
# more than absolute precision.
CHARS_PER_TOKEN = 4

# Findings thresholds. Deliberately generous: a finding should mean "this is
# worth your attention", not "this is 2% over a line I invented".
DOMINANT_SHARE = 0.30        # one artifact eating >30% of the whole budget
OVERSIZED_BYTES = 20_000     # a single auto-loaded artifact past ~5k tokens
DUPLICATE_MIN_LEN = 24       # ignore short repeated lines (headers, "---", "")
DUPLICATE_MIN_COUNT = 3      # a line must repeat at least this often to matter
OUTLIER_FACTOR = 4           # skill description this many times the median
MAX_PATHS_PROBED = 200       # bound the stat() calls the dead-path rule makes

# POSIX roots worth probing. `tmp` and `var` are included deliberately: those
# trees are ephemeral, so a reference into them is exactly the kind of stale
# pointer this rule exists to catch — and their absence made the rule silently
# untestable on Linux/macOS, where pytest's tmp_path lives under /tmp or
# /var/folders. The list stays an allow-list rather than "any absolute path" so
# URL-ish and API-route strings in prose cannot be mistaken for filesystem refs.
_ABS_PATH_RE = re.compile(
    r"(?:[A-Za-z]:\\[^\s\"'`|,;:*?<>]+"
    r"|/(?:home|Users|mnt|opt|tmp|var|srv|data)/[^\s\"'`|,;:*?<>]+)"
)
_URL_RE = re.compile(r"https?://\S+")
# Only candidates that look like a FILE reference are probed. The tokenizer
# stops at whitespace, so a path containing a space ("My Documents") truncates
# to a stub that does not exist — reporting that stub as a dead reference is a
# false positive on a very common path shape. Requiring a trailing extension
# discards those stubs (they rarely end in one) while keeping the real target
# of this rule: stale references to specific files in deleted worktrees.
_FILE_EXT_RE = re.compile(r"\.[A-Za-z0-9]{1,8}$")
_TRAILING_PUNCT = ".,;:!?)]}'\""


@dataclass(frozen=True)
class Segment:
    """One measured contributor to auto-loaded context."""
    name: str
    origin: str          # "hive" | "claude"
    path: str            # "" when the segment is not a single file
    nbytes: int
    tokens: int
    kind: str            # "artifact" | "skill" | "instructions"
    detail: str = ""


@dataclass(frozen=True)
class Finding:
    """A rule-based trim suggestion. Advisory only — nothing acts on these."""
    rule: str
    severity: str        # "high" | "medium" | "low"
    subject: str
    message: str


# Operator-config sidecar holding the persisted token budget for this
# namespace. Config, not a projection: it is not rebuildable from the ledger,
# exactly like rules/enforce.json.
BUDGET_FILE = "context_budget.json"

# Lines of MEMORY.md the native loader will actually read. Past this it loads
# PART of the file and says so — "WARNING: MEMORY.md is 207 lines (limit: 200)"
# — which means the sections at the END of the digest silently stop arriving.
# `sync` writes that file with caps on ITEM COUNTS only (LESSONS_CAP,
# DECISIONS_CAP), and no item count bounds lines, so the digest grew past the
# limit and lost its own tail: the lessons and decisions the caps were protecting
# are the last two sections written.
#
# 200 is an external constant — the loader's, not ours — so it is named here
# once and overridable per namespace (`memory_lines` in BUDGET_FILE) rather than
# hardcoded at each surface that judges the file. Two surfaces judge it: `hive
# sync`, right after writing it, and `hive context --check`, which gates on it.
MEMORY_LINE_BUDGET = 200

# Sections of the digest, in the order `sync` writes them. Used to name WHICH
# sections fall past the loader's cut — "over by 7 lines" is a number, "Key
# decisions never arrives" is the consequence.
_MEMORY_SECTION_RE = re.compile(r"^##+\s+(.+?)\s*$", re.MULTILINE)


def load_budget(project=None) -> int | None:
    """The persisted token budget for this namespace, or None when unset.
    Fail-open: a missing, corrupt, or non-positive value reads as no budget."""
    try:
        path = paths.project_dir(project) / BUDGET_FILE
        obj = json.loads(path.read_text(encoding="utf-8"))
        budget = obj.get("budget") if isinstance(obj, dict) else None
        if isinstance(budget, bool) or not isinstance(budget, (int, float)):
            return None
        return int(budget) if budget > 0 else None
    except Exception:
        return None


def save_budget(budget: int, project=None, memory_lines: int | None = None) -> dict:
    """Persist the namespace's token budget. Returns the stored record.

    A `memory_lines` override is written only when given: absent, the file keeps
    exactly the shape it has always had, so an older sidecar and a newer one both
    read correctly and the default stays a constant in code rather than a value
    silently baked into every namespace that ever ran this."""
    import time
    data = {"budget": int(budget),
            "set_at": time.strftime("%Y-%m-%dT%H:%M:%S", time.gmtime()) + "Z"}
    if memory_lines is None:
        # Merge, never replace: setting the token budget must not silently
        # discard a line budget the operator set earlier. `load_` cannot answer
        # "was it stored?" — it returns the default for absent and for corrupt
        # alike — so the raw file is what decides whether to carry one forward.
        try:
            prior = json.loads((paths.project_dir(project) / BUDGET_FILE)
                               .read_text(encoding="utf-8"))
            stored = prior.get("memory_lines") if isinstance(prior, dict) else None
            if not isinstance(stored, bool) and isinstance(stored, (int, float)) \
                    and stored > 0:
                data["memory_lines"] = int(stored)
        except Exception:
            pass
    else:
        data["memory_lines"] = int(memory_lines)
    paths.write_text_atomic(paths.project_dir(project) / BUDGET_FILE,
                            json.dumps(data, indent=2, sort_keys=True))
    return data


def load_memory_line_budget(project=None) -> int:
    """This namespace's MEMORY.md line budget — the sidecar's `memory_lines`, or
    `MEMORY_LINE_BUDGET`. Total: a corrupt or nonsensical value reads as the
    default, because the wrong answer here is silence about a truncated file."""
    try:
        path = paths.project_dir(project) / BUDGET_FILE
        obj = json.loads(path.read_text(encoding="utf-8"))
        lines = obj.get("memory_lines") if isinstance(obj, dict) else None
        if isinstance(lines, bool) or not isinstance(lines, (int, float)):
            return MEMORY_LINE_BUDGET
        return int(lines) if lines > 0 else MEMORY_LINE_BUDGET
    except Exception:
        return MEMORY_LINE_BUDGET


def memory_pressure(path, budget: int | None = None, project=None) -> dict:
    """Does the native MEMORY.md fit the loader, and what is lost if not?

    Returns ``{lines, budget, over, dropped, readable}``. ``dropped`` names the
    digest sections that begin past the loader's cut — the sections a session
    therefore never sees. Never raises: an unreadable or absent file reports
    ``readable: False`` and zero pressure, because "we could not measure" must
    not read as "it is fine"... which is why `over` is 0 there and `readable`
    is the field a caller has to look at."""
    budget = int(budget if budget is not None else load_memory_line_budget(project))
    text = _read_text(Path(path)) if path else None
    if text is None:
        return {"lines": 0, "budget": budget, "over": 0, "dropped": [],
                "readable": False, "path": str(path or "")}
    lines = text.splitlines()
    dropped = [m.group(1) for m in _MEMORY_SECTION_RE.finditer(text)
               if text[:m.start()].count("\n") >= budget]
    return {"lines": len(lines), "budget": budget,
            "over": max(0, len(lines) - budget), "dropped": dropped,
            "readable": True, "path": str(path)}


def estimate_tokens(text: str) -> int:
    """Estimate the token cost of ``text``.

    A character-ratio heuristic (see ``CHARS_PER_TOKEN``), not an exact count.
    Monotonic by construction: appending text can never lower the estimate,
    which is what makes the ranking trustworthy even though the absolute
    numbers are approximate. Empty text costs nothing; any non-empty text costs
    at least one token."""
    if not text:
        return 0
    return max(1, round(len(text) / CHARS_PER_TOKEN))


def _read_text(path: Path) -> str | None:
    """Read a file as text, or return None if it cannot be read at all.

    ``errors="replace"`` keeps invalid UTF-8 measurable rather than fatal — a
    corrupt byte should cost its bytes in the report, not abort the run."""
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except Exception:
        return None


def _memory_folder(cwd: Path | None = None) -> str:
    """The folder key under which the native MEMORY.md is stored.

    MEMORY.md is keyed by the *main repo* folder, not the current directory. Run
    from inside a linked worktree, ``Path.cwd()`` encodes to the worktree path
    and the lookup misses the real file entirely — which silently understates
    the report by the size of MEMORY.md. Resolve the main repo the same way
    ``paths.detect_project`` does (``--git-common-dir`` points at the main
    repo's .git even from a linked worktree), and fall back to cwd off-repo."""
    import subprocess
    base = Path(cwd) if cwd is not None else Path.cwd()
    try:
        r = subprocess.run(
            ["git", "rev-parse", "--git-common-dir"],
            cwd=str(base), capture_output=True, text=True, timeout=2,
            creationflags=paths.NO_WINDOW,
        )
        if r.returncode == 0 and r.stdout.strip():
            gcd = Path(r.stdout.strip())
            if not gcd.is_absolute():
                gcd = base / gcd
            gcd = gcd.resolve()
            if gcd.name == ".git":
                return str(gcd.parent)
    except Exception:
        pass
    return str(base)


def _file_segment(path: Path, name: str, origin: str, kind: str,
                  detail: str = "") -> Segment | None:
    """Measure one file, or return None when it is absent/unreadable."""
    text = _read_text(path)
    if text is None:
        return None
    try:
        nbytes = path.stat().st_size
    except Exception:
        nbytes = len(text.encode("utf-8", errors="replace"))
    return Segment(name=name, origin=origin, path=str(path), nbytes=nbytes,
                   tokens=estimate_tokens(text), kind=kind, detail=detail)


# --------------------------------------------------------------------------
# collectors
# --------------------------------------------------------------------------

def collect_hive(project: str | None = None, memory_path: Path | None = None,
                 folder: str | None = None) -> list[Segment]:
    """Measure the artifacts Mind Hive maintains and auto-loads.

    The namespace artifacts resolve through ``paths.project_dir`` so this always
    reports the same namespace the rest of the tool is operating on — with many
    namespaces on disk, re-deriving the path here would risk measuring somebody
    else's project. MEMORY.md lives outside the store, at the native per-project
    path the desktop app reads; ``memory_path`` overrides it for tests."""
    out: list[Segment] = []
    pdir = paths.project_dir(project)

    for filename, kind in (("STATE.md", "artifact"),
                           ("BRIEFING.md", "artifact"),
                           ("SOUL.md", "artifact")):
        seg = _file_segment(pdir / filename, filename, "hive", kind)
        if seg is not None:
            out.append(seg)

    if memory_path is None:
        from . import sync
        try:
            memory_path = sync.native_memory_path(folder or _memory_folder())
        except Exception:
            memory_path = None
    if memory_path is not None:
        seg = _file_segment(Path(memory_path), "MEMORY.md", "hive", "artifact",
                            detail="native auto-loaded memory digest")
        if seg is not None:
            out.append(seg)

    return out


def collect_claude(home: Path | None = None,
                   cache_dir: Path | None = None) -> list[Segment]:
    """Measure the wider always-on Claude Code load.

    Covers the user's CLAUDE.md instructions and the trigger description of
    every installed plugin skill. Only the description is charged, not the skill
    body: the body loads on invocation, the description is resident in context
    from the start, and charging the body would overstate the standing cost by
    orders of magnitude.

    Plugin discovery reuses ``plugins.scan``, which already handles version
    dedup and cache layout. Both roots are injectable so tests never read the
    real ``~/.claude``."""
    home = Path(home) if home is not None else Path.home()
    out: list[Segment] = []

    for rel, name in ((Path(".claude") / "CLAUDE.md", "CLAUDE.md"),
                      (Path(".claude") / "settings.json", "settings.json")):
        seg = _file_segment(home / rel, name, "claude", "instructions")
        if seg is not None:
            out.append(seg)

    from . import plugins
    try:
        found = plugins.scan(cache_dir) if cache_dir is not None else plugins.scan()
    except Exception:
        found = []

    for plugin in found or []:
        if not isinstance(plugin, dict):
            continue
        pname = plugin.get("name") or "?"
        for skill in plugin.get("skills") or []:
            if not isinstance(skill, dict):
                continue
            trigger = skill.get("trigger") or skill.get("description") or ""
            if not trigger:
                continue
            sname = skill.get("name") or "?"
            out.append(Segment(
                name=f"{pname}:{sname}", origin="claude", path="",
                nbytes=len(trigger.encode("utf-8", errors="replace")),
                tokens=estimate_tokens(trigger), kind="skill",
                detail="skill trigger description",
            ))

    # Local (unpackaged) skills: ~/.claude/skills/<name>/SKILL.md — the path
    # adopted skills arrive through. Previously invisible to this analyzer,
    # which meant the budget could not see the one growth channel an adoption
    # flow actually uses. _skills_for's parent-dir contract (<dir>/skills/*)
    # makes home/.claude the right root here.
    try:
        for skill in plugins._skills_for(home / ".claude") or []:
            trigger = skill.get("trigger") or ""
            if not trigger:
                continue
            out.append(Segment(
                name=f"local:{skill.get('name') or '?'}", origin="claude",
                path="", nbytes=len(trigger.encode("utf-8", errors="replace")),
                tokens=estimate_tokens(trigger), kind="skill",
                detail="local skill trigger description",
            ))
    except Exception:
        pass

    return out


# --------------------------------------------------------------------------
# analysis
# --------------------------------------------------------------------------

def analyze(segments: list[Segment], budget: int | None = None) -> dict:
    """Total, rank, and (optionally) compare against a token budget.

    Ranking is descending by tokens with the name as a tiebreak, so repeated
    runs over unchanged inputs produce byte-identical output — a report that
    reshuffles itself is useless for spotting real change."""
    segs = list(segments or [])
    total_tokens = sum(s.tokens for s in segs)
    total_bytes = sum(s.nbytes for s in segs)

    ranked = sorted(segs, key=lambda s: (-s.tokens, s.name))
    rows = [{
        "name": s.name, "origin": s.origin, "kind": s.kind, "path": s.path,
        "bytes": s.nbytes, "tokens": s.tokens, "detail": s.detail,
        "share": (s.tokens / total_tokens) if total_tokens else 0.0,
    } for s in ranked]

    by_origin: dict[str, dict] = {}
    for s in segs:
        slot = by_origin.setdefault(s.origin, {"tokens": 0, "bytes": 0, "count": 0})
        slot["tokens"] += s.tokens
        slot["bytes"] += s.nbytes
        slot["count"] += 1

    report = {
        "total_tokens": total_tokens,
        "total_bytes": total_bytes,
        "count": len(segs),
        "by_origin": by_origin,
        "segments": rows,
        "budget": budget,
        "over_budget": (total_tokens - budget) if budget is not None else None,
        "estimated": True,
        "chars_per_token": CHARS_PER_TOKEN,
    }
    return report


# --------------------------------------------------------------------------
# findings
# --------------------------------------------------------------------------

def _finding_dominant(segments, total) -> list[Finding]:
    out = []
    if not total:
        return out
    for s in segments:
        share = s.tokens / total
        if share > DOMINANT_SHARE:
            out.append(Finding(
                rule="dominant_artifact", severity="high", subject=s.name,
                message=(f"{s.name} is {share:.0%} of the whole auto-loaded budget "
                         f"(~{s.tokens} est. tokens). Trimming this one file moves "
                         f"the total more than anything else."),
            ))
    return out


def _finding_oversized(segments) -> list[Finding]:
    return [
        Finding(rule="oversized_artifact", severity="medium", subject=s.name,
                message=(f"{s.name} is {s.nbytes:,} bytes (~{s.tokens} est. tokens), "
                         f"past the {OVERSIZED_BYTES:,}-byte guideline for a single "
                         f"auto-loaded artifact."))
        for s in segments
        if s.kind == "artifact" and s.nbytes > OVERSIZED_BYTES
    ]


def _finding_duplicate_lines(segments, texts: dict[str, str]) -> list[Finding]:
    out = []
    for s in segments:
        text = texts.get(s.path) if s.path else None
        if text is None:
            continue
        counts: dict[str, int] = {}
        for line in text.splitlines():
            stripped = line.strip()
            if len(stripped) >= DUPLICATE_MIN_LEN:
                counts[stripped] = counts.get(stripped, 0) + 1
        repeats = {k: v for k, v in counts.items() if v >= DUPLICATE_MIN_COUNT}
        if not repeats:
            continue
        wasted = sum(len(k) * (v - 1) for k, v in repeats.items())
        worst = max(repeats.items(), key=lambda kv: len(kv[0]) * (kv[1] - 1))
        # Derive the token cost arithmetically. Materializing a filler string of
        # `wasted` characters just to measure its length would allocate megabytes
        # on a pathologically duplicated artifact.
        wasted_tokens = max(1, round(wasted / CHARS_PER_TOKEN)) if wasted else 0
        out.append(Finding(
            rule="duplicate_lines", severity="medium", subject=s.name,
            message=(f"{s.name} repeats {len(repeats)} line(s) {DUPLICATE_MIN_COUNT}+ "
                     f"times, wasting ~{wasted:,} chars (~{wasted_tokens} "
                     f"est. tokens). Worst repeats {worst[1]}x: {worst[0][:60]!r}"),
        ))
    return out


def _is_illustrative(candidate: str) -> bool:
    """True for paths that are documentation examples, not real references.

    Instruction files legitimately contain example paths — "never use absolute
    paths like /Users/name/Code/project/src/file.ts". Those will never exist on
    disk, so without this the dead-path rule fires on every doc that teaches
    path hygiene, and a rule that cries wolf gets ignored.

    The tokens here are deliberately unambiguous placeholders. Bare words like
    "user" or "name" are NOT matched: `user` is a real account name on Docker
    images, WSL and CI runners, so suppressing every `/home/user/...` would hide
    genuinely stale references — the opposite failure."""
    if "..." in candidate or "${" in candidate:
        return True
    lowered = candidate.lower()
    return any(tok in lowered for tok in
               ("/path/to/", "/name/", "yourname", "your-name", "username-here",
                "<", ">", "example.com"))


def _path_candidates(text: str) -> list[str]:
    """Extract probe-worthy absolute file references from arbitrary text.

    URLs are stripped first: an address like ``http://host/mnt/backup/x`` would
    otherwise yield ``/mnt/backup/x``, which was never a filesystem reference at
    all. Trailing sentence punctuation is trimmed — prose ends paths with a
    period, and on POSIX ``file.md.`` is a genuinely different (non-existent)
    name from ``file.md``."""
    cleaned = _URL_RE.sub(" ", text)
    out = []
    for raw in dict.fromkeys(_ABS_PATH_RE.findall(cleaned)):
        cand = raw.rstrip(_TRAILING_PUNCT)
        if not cand or _is_illustrative(cand):
            continue
        if not _FILE_EXT_RE.search(cand):
            continue
        out.append(cand)
    return out


def _finding_dead_paths(segments, texts: dict[str, str]) -> list[Finding]:
    out = []
    for s in segments:
        text = texts.get(s.path) if s.path else None
        if text is None:
            continue
        candidates = _path_candidates(text)[:MAX_PATHS_PROBED]
        dead = []
        for cand in candidates:
            try:
                if not Path(cand).exists():
                    dead.append(cand)
            except Exception:
                continue
        if dead:
            out.append(Finding(
                rule="dead_path", severity="low", subject=s.name,
                message=(f"{s.name} references {len(dead)} absolute path(s) that no "
                         f"longer exist (e.g. {dead[0]}). Stale paths cost tokens every "
                         f"session and mislead on read."),
            ))
    return out


def _finding_memory_truncated(segments, project=None) -> list[Finding]:
    """MEMORY.md past the native loader's line budget.

    Distinct from `oversized_artifact`, which is about COST — this one is about
    LOSS. An oversized artifact is paid for and delivered; a truncated one is
    paid for and half-delivered, and the half that vanishes is the tail, where
    `sync` writes the lessons and decisions. High severity for that reason: the
    other findings suggest trimming, this one reports something already gone."""
    out = []
    for s in segments:
        if s.name != "MEMORY.md" or not s.path:
            continue
        pressure = memory_pressure(s.path, project=project)
        if not pressure["readable"] or not pressure["over"]:
            continue
        lost = (", ".join(pressure["dropped"]) if pressure["dropped"]
                else "the tail of the file")
        out.append(Finding(
            rule="memory_truncated", severity="high", subject=s.name,
            message=(f"MEMORY.md is {pressure['lines']} lines against a "
                     f"{pressure['budget']}-line loader budget (over by "
                     f"{pressure['over']}). The loader reads part of the file, so "
                     f"these never reach a session: {lost}. Trim the digest or "
                     f"raise `memory_lines` in {BUDGET_FILE} deliberately."),
        ))
    return out


def _finding_outlier_skills(segments) -> list[Finding]:
    skills = [s for s in segments if s.kind == "skill"]
    if len(skills) < 4:
        return []
    import statistics
    median = statistics.median(s.tokens for s in skills) or 1
    return [
        Finding(rule="outlier_description", severity="low", subject=s.name,
                message=(f"{s.name} has a {s.tokens}-token trigger description, "
                         f"{s.tokens / median:.1f}x the median of {median}. Long "
                         f"descriptions are resident in context for every session."))
        for s in skills
        if s.tokens > median * OUTLIER_FACTOR
    ]


def findings(segments: list[Segment], project=None) -> list[Finding]:
    """Rule-based trim suggestions over measured segments.

    Each rule is independent and returns zero or more findings, so a rule that
    finds nothing is silent rather than noisy. Nothing here mutates its input or
    touches the artifacts it reports on."""
    segs = list(segments or [])
    total = sum(s.tokens for s in segs)

    # Read each file-backed segment exactly once and share the text across the
    # rules that need it. Reading per-rule would re-read every artifact for
    # every content rule — the same redundant-read trap this tool exists to
    # surface, and it would grow with each rule added.
    texts: dict[str, str] = {}
    for s in segs:
        if s.path and s.path not in texts:
            text = _read_text(Path(s.path))
            if text is not None:
                texts[s.path] = text

    out: list[Finding] = []
    out += _finding_dominant(segs, total)
    out += _finding_oversized(segs)
    out += _finding_memory_truncated(segs, project)
    out += _finding_duplicate_lines(segs, texts)
    out += _finding_dead_paths(segs, texts)
    out += _finding_outlier_skills(segs)
    order = {"high": 0, "medium": 1, "low": 2}
    return sorted(out, key=lambda f: (order.get(f.severity, 3), f.rule, f.subject))


# --------------------------------------------------------------------------
# rendering
# --------------------------------------------------------------------------

def render_text(report: dict, found: list[Finding], top: int | None = None) -> str:
    """Human-readable report. Figures are labelled as estimates throughout."""
    rows = report.get("segments") or []
    if not rows:
        return ("No context contributors found. Nothing is auto-loading from this "
                "namespace, or the store has not been synced yet.")

    shown = rows[:top] if top else rows
    lines = []
    total_t = report["total_tokens"]
    lines.append(f"Auto-loaded context: ~{total_t:,} est. tokens "
                 f"({report['total_bytes']:,} bytes across {report['count']} contributors)")

    for origin, slot in sorted(report.get("by_origin", {}).items()):
        share = (slot["tokens"] / total_t) if total_t else 0.0
        lines.append(f"  {origin:<7} ~{slot['tokens']:>7,} est. tokens  "
                     f"{share:>5.0%}  ({slot['count']} item(s))")

    budget = report.get("budget")
    if budget is not None:
        over = report.get("over_budget") or 0
        verdict = f"OVER by ~{over:,}" if over > 0 else f"under by ~{abs(over):,}"
        lines.append(f"  budget  {budget:,} est. tokens -> {verdict}")

    lines.append("")
    lines.append(f"{'tokens':>8}  {'share':>6}  {'origin':<7} name")
    for r in shown:
        lines.append(f"{r['tokens']:>8,}  {r['share']:>5.0%}  {r['origin']:<7} {r['name']}")
    if top and len(rows) > top:
        lines.append(f"{'...':>8}  {len(rows) - top} more (use --top to widen)")

    if found:
        lines.append("")
        lines.append(f"Trim suggestions ({len(found)}):")
        for f in found:
            lines.append(f"  [{f.severity}] {f.rule}: {f.message}")

    lines.append("")
    lines.append(f"Token figures are estimates (~{report['chars_per_token']} chars/token). "
                 "This command is read-only and changed nothing.")
    return "\n".join(lines)


def to_json(report: dict, found: list[Finding]) -> str:
    """Machine-readable report. Stable keys so downstream tooling can rely on it."""
    payload = dict(report)
    payload["findings"] = [asdict(f) for f in found]
    return json.dumps(payload, indent=2, sort_keys=True)


# --- the outcomes-registry probe ----------------------------------------------

def _probe(project=None, path=None) -> outcomes.Report:
    """Report on the always-on digest's own feedback loop: does everything `sync`
    wrote actually reach a session?

    Three states this has to keep apart, because the file arriving at all made
    every one of them look like success:

    - No file yet is NOTHING_TO_DO. Nothing is delivered, so nothing is lost — an
      unwritten digest is not a truncated one.
    - A file present but unreadable is DEGRADED, never "fine". `memory_pressure`
      reports `readable: False` for absent AND unreadable, so the existence check
      here is what separates the two.
    - Over the loader's budget is DEGRADED and names the sections that fall past
      the cut, because those are the ones no session ever sees.

    The folder comes from `_memory_folder`, not from `Path.cwd()`, for the reason
    that function documents: from inside a linked worktree the cwd encoding misses
    the real file, and this probe would then answer "no MEMORY.md yet" about a
    digest that is in fact being truncated — the exact failure it exists to catch.
    Measured live: cwd gave `nothing_to_do`, the main repo gave the truncation.

    Cheap: one `git rev-parse`, one `stat`, and one read of a file whose whole
    purpose is to be small.

    `path` overrides the resolution so a test can point at a file it wrote, the
    way `memory_pressure` already takes its path from the caller.
    """
    if path is None:
        from . import sync as sync_mod      # lazy: sync pulls in the projections,
        path = sync_mod.native_memory_path(_memory_folder())   # nothing else here

    pressure = memory_pressure(path, project=project)
    if not pressure["readable"]:
        if not Path(path).exists():
            return outcomes.Report(
                outcomes.NOTHING_TO_DO,
                f"no MEMORY.md for this folder yet ({path}) — `hive sync` writes "
                f"it; nothing is delivered, so nothing is being lost", pressure)
        return outcomes.Report(
            outcomes.DEGRADED,
            f"MEMORY.md at {path} exists but could not be read — the pressure is "
            f"unmeasured, which is not the same as fine", pressure)
    if pressure["over"]:
        lost = ", ".join(pressure["dropped"]) or "the tail of the file"
        return outcomes.Report(
            outcomes.DEGRADED,
            f"MEMORY.md is {pressure['lines']} lines against a "
            f"{pressure['budget']}-line loader budget (over by "
            f"{pressure['over']}) — these never reach a session: {lost}",
            pressure)
    return outcomes.Report(
        outcomes.DID_WORK,
        f"MEMORY.md is {pressure['lines']} of {pressure['budget']} loader lines "
        f"— every section reaches a session", pressure)


outcomes.register("memory-budget",
                  "the always-on digest reports the sections that fall past the "
                  "loader's cut instead of losing them quietly",
                  _probe)
