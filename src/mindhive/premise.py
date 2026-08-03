# src/mindhive/premise.py
"""The premise gate: mechanical checking of the checkable claims a prompt makes.

`directives.OPERATING_SPEC` §0 says never accept a premise you did not verify.
This module is the part of that rule a machine can enforce, and it exists
because the failure is real and repeating rather than hypothetical. The
directive that commissioned this module asserted an interrupted roadmap (none
existed), a file to patch (`src/mindhive/history.py`, present on no branch),
two unbuilt features (both already shipped and merged), and a 3518-test
baseline (the real number was 3580). Every checkable claim was false.

**Why `promptgate` did not catch it.** That gate caps its scan at
`promptgate.PROMPT_SCAN_CAP` (2000 chars) because its target regexes backtrack
catastrophically — a pasted 100KB run of dots measured ~100s unscanned versus
~ms capped. Past the cap, `assess` returns `clear` and `is_code_work` returns
False, so a 20KB handoff directive receives no assessment and no catalog
injection whatsoever. The incentive is exactly inverted: the longer and more
assertion-dense a prompt is, the less scrutiny it gets, and handoff directives,
pasted specs, and generated continuations are all past the cap by construction.

That cap is correct and is not touched here. This module carries its own,
larger budget (`PREMISE_SCAN_CAP`) and pays for it structurally: every pattern
below uses bounded repetition and a start-anchoring lookbehind, so a
pathological input costs a linear scan with a small constant rather than an
exponential one.

Measured on this machine rather than asserted — `extract` medians over 5 runs:

    real 2KB handoff        0.22 ms
    24KB document           3.07 ms
    20KB of dots            3.58 ms
    20KB of slashes         3.04 ms
    60KB (at the cap)      10.07 ms

The hostile inputs cost the same order as the benign ones, which is the whole
claim the lookbehind is making. Against the ~0.36 s `promptgate` already spends
per prompt this is noise. A test also times it, so a later edit that reintroduces
a greedy quantifier fails loudly instead of silently restoring the 100s cliff.

**Shape.** Three stages, each independently testable, mirroring `promptgate`'s
own `assess` / `train` / `render_context` split:

    extract(text)      -> claims     pure, bounded, no I/O
    check(claims, ...) -> verdicts   filesystem + ledger
    render(verdicts)   -> str        pure

`extend_hook_context` is the only entry point that touches the world, and it is
total: any error, unreadable store, or malformed payload yields the context
unchanged. Additive only — it never denies, blocks, or edits a prompt.
`promptgate`'s MN-14 note settles that precedent for this surface, and it binds
harder here: a premise checker WILL have false positives (a path claim about a
file the user is about to create is the obvious one), and a false positive that
adds one advisory line costs a line, while one that blocks a prompt wedges the
session.

**What is checked, and what is not.** Checked: asserted repo-relative file
paths, asserted roadmap ids and interrupted-roadmap existence, asserted test
counts. Deliberately NOT checked, because no queryable evidence source exists
and a check that pretended otherwise would itself be the dishonesty this module
is against:

- Claims about what a previous session did.
- Intent, urgency, authority, and framing — not factual claims at all.
- Claims about code behavior ("the matcher over-matches on stopwords"), which
  would require executing the claim. Nothing on the prompt path may do that.
- Git refs (asserted SHAs, branch names). Cheap in principle, but it would put
  a subprocess on the `UserPromptSubmit` critical path, which today spawns
  nothing.
- Counts of anything other than tests. One evidence source, one claim class.
- **Quotation vs assertion.** The extractor reads text, not intent, so a path
  or count MENTIONED as an example, a quote, or a description of someone
  else's claim is extracted the same as one asserted. Running the CLI over a
  document that DISCUSSES a false baseline will flag that baseline. This is
  inherent to regex extraction and is stated in `LIMITS_LINE` rather than
  papered over; on the hook's real surface — a typed prompt — a stated count
  almost always is an assertion, which is the case the gate is for.

`render` states these limits in the injected block, so the report can never
read as a clean bill of health for the rest of the prompt.
"""
from __future__ import annotations

import os
import re
from pathlib import Path

from . import roadmap, verify

PREMISE_MARK = "[mindhive-premise]"

# A cap on WORK, not a statement that longer prompts stop mattering. It is an
# order of magnitude above promptgate's because this module's whole reason to
# exist is the range that one skips; the patterns below are what make the
# larger budget affordable. Past this, `extract` reports `scanned: False`
# rather than silently returning "no claims found", so a caller can tell
# "nothing asserted" from "not looked at".
PREMISE_SCAN_CAP = 60_000

# Per-class ceiling. A prompt naming 500 files gets 20 checked and a recorded
# truncation — runlog's rule that a bounded view must announce its bound.
MAX_CLAIMS_PER_CLASS = 20

KIND_PATH = "path"
KIND_ROADMAP_ID = "roadmap-id"
KIND_ROADMAP_INTERRUPTED = "roadmap-interrupted"
KIND_TEST_COUNT = "test-count"

CONFIRMED = "confirmed"
CONTRADICTED = "contradicted"
UNKNOWN = "unknown"

# Extensions that make a dotted token a *code or config path claim*. A fixed
# list, unlike promptgate's `[A-Za-z]{1,4}`, which is why this module needs no
# `e.g.`/`i.e.`/`etc.`/`vs.` lookahead: none of those trailing fragments is in
# the list, so prose abbreviations cannot match in the first place.
_PATH_EXTS = (
    "py", "pyi", "md", "json", "jsonl", "toml", "yaml", "yml", "txt", "cfg",
    "ini", "ps1", "sh", "bat", "js", "ts", "tsx", "jsx", "rs", "go", "rb",
    "java", "c", "h", "cpp", "html", "css", "sql",
)

# The start-anchoring lookbehind is the performance guarantee, not a nicety.
# Without it, a 20000-character run of dots gives the engine 20000 start
# positions each trying up to 120 lazy expansions. With it, every position
# after the first is preceded by a path character and refused outright, so the
# hostile input collapses to a single bounded attempt. The mandatory
# `[A-Za-z0-9_-]` immediately before the extension dot does the same job from
# the other side: a run of pure separators can never reach the alternation.
_PATH_RE = re.compile(
    r"(?<![A-Za-z0-9_.:~/\\-])"
    r"((?:[A-Za-z0-9_.:~/\\-]){0,120}?[A-Za-z0-9_-]\.(?:"
    + "|".join(_PATH_EXTS) + r"))"
    r"(?![A-Za-z0-9])"
)

# URLs are matched FIRST and their spans excluded from path claims. A link to
# `https://example.com/pkg/setup.py` is not an assertion about this repo, and
# resolving it against the repo root would manufacture a contradiction out of a
# citation.
_URL_RE = re.compile(r"\bhttps?://[^\s<>\"'`\)\]]{0,300}")

# Only a *command position* or an explicit field yields a concrete id. Loose
# proximity matching ("some id near the word roadmap") reads the metavariable
# in `hive roadmap resume {roadmap_id_or_error}` as an asserted plan id, which
# would have the gate accusing a prompt of naming a plan it never named. The
# character class excludes `{`, so that placeholder simply does not match.
_ROADMAP_CMD_RE = re.compile(
    r"\bhive\s+roadmap\s+(?:resume|interrupt|complete|show|step)\s+"
    r"([A-Za-z0-9][A-Za-z0-9_-]{1,63})(?![A-Za-z0-9_-])")
_ROADMAP_FIELD_RE = re.compile(
    r"\broadmap[_ -]?id\b\s*[:=]\s*[\"'`]?"
    r"([A-Za-z0-9][A-Za-z0-9_-]{1,63})(?![A-Za-z0-9_-])")

# The existence claim, which is what the real directive actually made: not
# "resume plan X" but "an interrupted plan is waiting for you". Checkable
# against `roadmap.interrupted`, and the false premise that started all this.
#
# Deliberately NOT triggered by the bare string `hive roadmap resume`. That
# fires on any text that merely MENTIONS the command — this repo's own
# DATA_MODEL.md and plan files among them — and mentioning a command is not
# asserting that an interrupted roadmap exists. A prompt that really does name
# one to resume supplies an id, which `_ROADMAP_CMD_RE` catches and checks
# far more precisely.
_ROADMAP_INTERRUPTED_RES = (
    re.compile(r"\binterrupted\s+(?:plan|roadmap)\b"),
    re.compile(r"\broadmap\s+resumption\b"),
)

# The number must be ADJACENT to test vocabulary: "3518 passed", "3518-test",
# "3401 tests". A looser window around "suite"/"baseline" was tried and cut —
# it read "the suite finished in 228s" as a claim of 228 tests, and a
# contradiction manufactured out of a runtime is exactly the false positive
# that teaches an operator to stop believing this gate. Recall lost is small:
# every real phrasing seen in the wild puts the count next to the word.
_TEST_COUNT_RES = (
    re.compile(r"\b(\d{2,7})\s*-?\s*(?:tests?|passed|passing)\b"),
)


def _claim(kind, value, raw):
    return {"kind": kind, "value": value, "raw": raw}


def _url_spans(text):
    return [m.span() for m in _URL_RE.finditer(text)]


def _inside(span, spans):
    return any(a <= span[0] and span[1] <= b for a, b in spans)


def extract(text) -> dict:
    """Structured claims a prompt asserts, as
    ``{claims: [...], truncated: {kind: n}, scanned: bool}``.

    Pure and total: no disk, no ledger, no raise. Non-string input and input
    past `PREMISE_SCAN_CAP` both yield no claims, distinguished by `scanned`."""
    if not isinstance(text, str):
        return {"claims": [], "truncated": {}, "scanned": False}
    if len(text) > PREMISE_SCAN_CAP:
        return {"claims": [], "truncated": {}, "scanned": False}

    by_kind: dict[str, list] = {}
    truncated: dict[str, int] = {}

    def add(kind, value, raw):
        bucket = by_kind.setdefault(kind, [])
        if any(c["value"] == value for c in bucket):
            return                              # same assertion twice is one claim
        if len(bucket) >= MAX_CLAIMS_PER_CLASS:
            truncated[kind] = truncated.get(kind, 0) + 1
            return
        bucket.append(_claim(kind, value, raw))

    urls = _url_spans(text)
    for m in _PATH_RE.finditer(text):
        if _inside(m.span(), urls):
            continue
        add(KIND_PATH, m.group(1), m.group(0))

    for rx in (_ROADMAP_CMD_RE, _ROADMAP_FIELD_RE):
        for m in rx.finditer(text):
            add(KIND_ROADMAP_ID, m.group(1), m.group(0))

    for rx in _ROADMAP_INTERRUPTED_RES:
        m = rx.search(text)
        if m:
            add(KIND_ROADMAP_INTERRUPTED, True, m.group(0))
            break

    for rx in _TEST_COUNT_RES:
        for m in rx.finditer(text):
            try:
                add(KIND_TEST_COUNT, int(m.group(1)), m.group(0))
            except (TypeError, ValueError):     # pragma: no cover - defensive
                continue

    claims = [c for kind in (KIND_PATH, KIND_ROADMAP_ID,
                             KIND_ROADMAP_INTERRUPTED, KIND_TEST_COUNT)
              for c in by_kind.get(kind, [])]
    return {"claims": claims, "truncated": truncated, "scanned": True}


# --- checking: resolve each claim against a real evidence source --------------

def _verdict(claim, outcome, ground):
    return {"claim": claim, "outcome": outcome, "ground": ground}


def _path_exists(p: Path) -> bool:
    """Its own function so a test can prove the unsafe-path branch never
    reaches it."""
    try:
        return p.exists()
    except OSError:
        return False


def repo_root(root=None):
    """The checkout this prompt is about, or None.

    Walks up for a `.git` entry rather than calling `paths.worktree_root`,
    which shells out to `git rev-parse`. That would put a process spawn on the
    UserPromptSubmit critical path, which today spawns nothing — and this gate
    runs on every prompt. A test pins the no-subprocess property.

    `.git` is checked with `exists`, not `is_dir`: in a linked worktree it is a
    FILE pointing at the real git dir, and requiring a directory would make the
    gate blind in exactly the worktrees this repo works in."""
    if root is not None:
        return Path(root)
    try:
        here = Path.cwd().resolve()
    except OSError:
        return None
    for cand in (here, *here.parents):
        try:
            if (cand / ".git").exists():
                return cand
        except OSError:
            continue
    return None


def _safe_repo_path(value: str, root: Path):
    """The claim resolved inside *root*, or None when it must not be followed.

    Refusal is not a filesystem question, so it is answered before any stat.
    Absolute paths, drive letters, `~`, URLs, and anything escaping the root
    are all refused: the gate's job is to check assertions about THIS repo, and
    a checker that will happily walk to `../../../../etc/shadow` to answer one
    has become a file-existence oracle for the whole disk."""
    if "://" in value:
        return None
    if value.startswith(("/", "\\", "~")):
        return None
    if re.match(r"^[A-Za-z]:", value):
        return None
    norm = value.replace("\\", "/")
    if any(seg == ".." for seg in norm.split("/")):
        return None
    base = os.path.normpath(str(root))
    joined = os.path.normpath(os.path.join(base, norm))
    if joined != base and not joined.startswith(base + os.sep):
        return None
    return Path(joined)


def _check_path(claim, root):
    if root is None:
        return _verdict(claim, UNKNOWN,
                        "no repo root resolved — nothing to check against")
    target = _safe_repo_path(claim["value"], root)
    if target is None:
        return _verdict(claim, UNKNOWN,
                        "refused: not a repo-relative path, so not checked")
    if _path_exists(target):
        return _verdict(claim, CONFIRMED, "exists in this checkout")
    return _verdict(claim, CONTRADICTED,
                    "no such file in this checkout")


def _check_roadmap_id(claim, evs):
    from .sop import ID_RE
    plan_id = claim["value"]
    if not ID_RE.match(plan_id):
        return _verdict(claim, UNKNOWN,
                        "not a well-formed kebab-case plan id")
    if roadmap.get(evs, plan_id) is not None:
        return _verdict(claim, CONFIRMED, "a roadmap with this id exists")
    return _verdict(claim, CONTRADICTED,
                    "no roadmap with this id exists in the ledger")


def _check_roadmap_interrupted(claim, evs):
    rows = roadmap.interrupted(evs)
    if rows:
        ids = ", ".join(r["plan_id"] for r in rows[:3])
        return _verdict(claim, CONFIRMED, f"interrupted roadmap(s): {ids}")
    # An empty roadmap store is authoritative about its own contents, so this
    # is the one place an absent source contradicts rather than abstaining.
    # `hive roadmap list` answering "(no roadmaps)" is evidence, not silence.
    return _verdict(claim, CONTRADICTED,
                    "no interrupted roadmap exists — `hive roadmap list` is "
                    "empty of them")


def _check_test_count(claim, evs):
    last = verify.last_run(evs)
    if last is None:
        return _verdict(claim, UNKNOWN,
                        "no recorded test run to check against — run "
                        "`hive selftest` to create one")
    code = last.get("exit_code")
    if code != 0:
        return _verdict(claim, CONTRADICTED,
                        f"the last recorded run exited {code} — a failing run "
                        f"is not a green suite, whatever it counted")
    passed = (last.get("counts") or {}).get("passed")
    if passed is None:
        return _verdict(claim, UNKNOWN,
                        "the last recorded run's counts were not parseable")
    if int(passed) == int(claim["value"]):
        return _verdict(claim, CONFIRMED,
                        f"the last recorded run: {passed} passed")
    return _verdict(claim, CONTRADICTED,
                    f"claimed {claim['value']}, but the last recorded run "
                    f"was {passed} passed")


def check(claims, evs=None, root=None) -> list:
    """A verdict per claim: CONFIRMED, CONTRADICTED, or UNKNOWN.

    UNKNOWN is first-class and never collapses into either neighbour.
    Folding it into CONFIRMED re-creates the failure this module exists to fix
    — silence reading as assent. Folding it into CONTRADICTED is worse: it
    manufactures accusations against true statements, and the first false one
    teaches the operator to ignore the block permanently.

    Total. Any evidence source that raises degrades that claim to UNKNOWN."""
    resolved = repo_root(root)
    out = []
    for claim in (claims or []):
        kind = claim.get("kind")
        try:
            if kind == KIND_PATH:
                out.append(_check_path(claim, resolved))
            elif kind == KIND_ROADMAP_ID:
                out.append(_check_roadmap_id(claim, evs))
            elif kind == KIND_ROADMAP_INTERRUPTED:
                out.append(_check_roadmap_interrupted(claim, evs))
            elif kind == KIND_TEST_COUNT:
                out.append(_check_test_count(claim, evs))
            else:                                       # pragma: no cover
                out.append(_verdict(claim, UNKNOWN, "no checker for this kind"))
        except Exception as e:                          # noqa: BLE001
            out.append(_verdict(claim, UNKNOWN,
                                f"could not check: {type(e).__name__}"))
    return out


# --- rendering: pure ----------------------------------------------------------

# The one sentence that keeps the block honest about itself. A report listing
# three contradictions reads, to a model skimming context, like a completed
# audit — so it has to say out loud that it is not one.
LIMITS_LINE = (
    "- Only file paths, roadmap ids, and test counts are checked. Everything "
    "else this prompt asserts — what a previous session did, what the code "
    "does, why it is urgent — is unverified. A path or number MENTIONED as an "
    "example, a quote, or a description of someone else's claim is read as an "
    "assertion too, so a contradiction here can be about the text rather than "
    "about the intent. This is not a clean bill of health.")


def _label(claim) -> str:
    kind = claim.get("kind")
    value = claim.get("value")
    if kind == KIND_PATH:
        return f"`{value}`"
    if kind == KIND_ROADMAP_ID:
        return f"roadmap id `{value}`"
    if kind == KIND_ROADMAP_INTERRUPTED:
        return "an interrupted roadmap is waiting to be resumed"
    if kind == KIND_TEST_COUNT:
        return f"a test count of {value}"
    return str(value)                                   # pragma: no cover


def render(verdicts) -> str:
    """The injected block, or ``""`` when there is nothing to say.

    Fires ONLY on a contradiction. Confirmations and unknowns alone stay
    silent: a gate that speaks on every prompt is a gate the reader learns to
    skip, and this one needs to be believed on the rare prompt where it
    matters. Unknowns still get counted in the block once it fires, because
    "what could not be checked" is part of an honest report."""
    bad = [v for v in (verdicts or []) if v.get("outcome") == CONTRADICTED]
    if not bad:
        return ""
    unknown = [v for v in verdicts if v.get("outcome") == UNKNOWN]
    lines = [f"{PREMISE_MARK} Premise check — this prompt asserts "
             f"{len(bad)} thing(s) the record contradicts. Verify before "
             f"building on them:"]
    for v in bad:
        lines.append(f"- {_label(v['claim'])} — {v['ground']}.")
    if unknown:
        lines.append(f"- Could not be checked ({len(unknown)}): "
                     + "; ".join(_label(v["claim"]) for v in unknown[:5]) + ".")
    lines.append(LIMITS_LINE)
    return "\n".join(lines)


_ORDER = {CONTRADICTED: 0, UNKNOWN: 1, CONFIRMED: 2}


def report(verdicts) -> str:
    """The operator-facing listing for `hive premise check`: every verdict,
    contradicted first, then unknown, then confirmed.

    Unlike `render`, this prints CONFIRMED and UNKNOWN too. The hook stays
    quiet unless something is wrong because it costs the model context on every
    prompt; a human who typed the command asked to see the whole answer — and
    "what could not be checked" is the half most worth showing them."""
    if not verdicts:
        return "(no checkable claims found in this text)"
    rows = sorted(verdicts, key=lambda v: (_ORDER.get(v["outcome"], 9),
                                           str(v["claim"].get("value"))))
    width = max(len(v["outcome"]) for v in rows)
    lines = [f"{v['outcome'].upper():<{width}}  {_label(v['claim'])} "
             f"— {v['ground']}" for v in rows]
    lines.append("")
    lines.append(LIMITS_LINE.lstrip("- "))
    return "\n".join(lines)


def extend_hook_context(context, raw, project=None) -> dict:
    """Append the premise report to a UserPromptSubmit context.

    Wired unconditionally in the `user_prompt_submit` stage, beside
    `mindnet.inject` and `style` rather than inside the `if context:` branch
    that guards `enrich` — this pass has to be able to speak when every other
    pass stayed silent, which is precisely the case that produced the incident
    it exists for.

    Fail-open in the project's house style: any error returns `context`
    unchanged. A gate that can break a prompt is worse than one that
    occasionally says nothing, and that ordering matters more here than
    anywhere, because this gate's whole job is to be trusted when it does
    speak."""
    try:
        import json

        from . import promptgate
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            return context
        prompt = payload.get("prompt", "")
        if promptgate.is_machine_prompt(prompt):
            return context
        found = extract(prompt)
        if not found["claims"]:
            return context
        block = render(check(found["claims"],
                             evs=_events_for(found["claims"], project),
                             root=None))
        if not block:
            return context
        hso = (context or {}).get("hookSpecificOutput") or {}
        existing = str(hso.get("additionalContext") or "")
        merged = f"{existing}\n\n{block}" if existing else block
        return dict(context or {}, hookSpecificOutput=dict(
            hso, hookEventName="UserPromptSubmit", additionalContext=merged))
    except Exception:                                   # noqa: BLE001
        return context


# Claim kinds whose evidence lives in the ledger. Everything else is answered
# by the filesystem alone.
_LEDGER_KINDS = frozenset({KIND_ROADMAP_ID, KIND_ROADMAP_INTERRUPTED,
                           KIND_TEST_COUNT})


def _events_for(claims, project=None):
    """The ledger, but only when a claim actually needs it.

    This runs on every prompt, and `promptgate`'s own docstring records the
    measurement that makes the guard necessary: the live store is 22.8k events
    and `events.read_all` costs **1.09 s**. Path claims — far and away the
    common case, since ordinary code prompts name files constantly — are
    answered by `Path.exists` and need no ledger at all. Reading it
    unconditionally would put a second on the critical path of most prompts to
    answer a question none of them asked.

    Returns [] when nothing needs the ledger and when it cannot be read; its
    own function so the hook's total contract does not depend on `read_all`
    being total."""
    if not any(c.get("kind") in _LEDGER_KINDS for c in (claims or [])):
        return []
    try:
        from . import events as ev_mod
        return ev_mod.read_all(project)
    except Exception:                                   # noqa: BLE001
        return []
