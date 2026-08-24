# src/mindhive/injectors.py
"""T1-T4 — the injector registry, and the per-turn record of what reached Claude.

Seven passes compose the context every prompt receives — `promptgate`,
`enrich`, `mindnet`, `premise`, `sopgate`, `symbols`, `style` — and until this
module existed, none of it was recorded. Not the text, not the size, not whether
a pass spoke at all. (`symbols` is T8 and is off unless `MINDHIVE_SYMBOLS` is
set; it still gets a row, because "did not inject" is an answer.)

## Why that mattered enough to build

All six end `except Exception: return context`. The fail-open contract is
correct and load-bearing: a raise here does not degrade a feature, it breaks the
user's prompt. The cost is that **a broken injector and a quiet one leave
byte-identical evidence: none.**

On 2026-08-23 a measurement harness passed the hook payload as a `dict` where a
JSON string was expected. `json.loads` raised inside three injectors, all three
`except` blocks absorbed it, and the run reported 1,319 characters where 4,940
belonged. `stderr` was empty. Nothing raised. The first draft of that
measurement published the wrong numbers as a finding. If the person deliberately
measuring the pipeline could not tell, nobody could.

## Two ways to ask, deliberately kept apart

`measure()` is the HOT path: it diffs the context string before and after a
pass and costs two `len()` calls. `cmd_hook` uses only this, so recording a turn
adds no work to the prompt path.

`probe()` — one per injector, generalised from `sopgate.probe` — is the
DIAGNOSTIC path. A probe re-derives what its pass would deliver, which is real
work, so it is never called per prompt. It exists because an external delta of
zero cannot separate *"nothing to say this turn"* from *"raised and fell open"*,
and those are two different answers an operator needs.

Known limit of this slice, stated rather than papered over: on the hook path
verdicts are `verbatim` or `silent` only. `partial` requires an injector to say
what it WANTED to deliver against what it did, which today only `sopgate` can
answer and only by re-rendering. Consulting it per prompt is a cost decision,
not an oversight, and it is deferred with that reason attached.

## Storage: the sidecar is "now", the ledger is "changed"

One row per turn against a 41,470-event ledger is 73,000 rows a year at 200
prompts a day — 1.76x the entire store, and `chain.compute_chain` folds
`read_all()` with no type filter, so every one of them is re-hashed by every
`hive verify`, forever. Content dedup does not help: it removes bytes, and the
cost is driven by rows.

So a row is appended only when the turn's SIGNATURE changes — its composed text
or any pass's verdict. A run of identical turns writes nothing, and the row that
closes such a run carries `unchanged_turns`, which keeps the history exactly
reconstructible. The sidecar always holds the current turn in full, at zero
ledger cost, and is what the dashboard reads.
"""
from __future__ import annotations

import hashlib
import importlib
import json
import time
from pathlib import Path
from typing import NamedTuple

from . import events, paths

# D4's three states. `sopgate`'s finer `pointers_only` is deliberately NOT a
# fourth member — it survives inside `reason`, so there is one legend for a
# verdict rather than two that can drift apart.
VERDICTS = ("verbatim", "partial", "silent")

# WHO injected. This is a different axis from `telemetry.FACULTIES` (which
# faculty was consulted) and must never be merged into it: `FACULTIES` is pinned
# to the MN-5 decomposition by `tests/test_mindnet_telemetry.py`, and a merge
# would silently retire that pin. A test asserts the two sets are disjoint.
#
INJECTOR_NAMES = ("promptgate", "enrich", "mindnet", "premise",
                  "sopgate", "style", "symbols")

# COMPOSITION order, which is a different fact from the registry above and the
# single most load-bearing one on this path: `style` prepends, so it runs LAST
# in order to survive truncation of everything before it. `symbols` therefore
# goes second-to-last -- a map appended after the style block would sit ahead of
# it in the composed text and take the truncation instead.
PIPELINE_ORDER = ("promptgate", "enrich", "mindnet", "premise",
                  "sopgate", "symbols", "style")

EVENT_TYPE = "context_injected"

# Per-injector stored text, and the whole composed block. Both are caps on what
# is WRITTEN, never on what is measured — see `_row`. The ledger is append-only,
# so an unbounded field would let one pathological prompt write a row that can
# never be trimmed afterwards.
MAX_TEXT_CHARS = 4000
MAX_BLOCK_CHARS = 20000

# How many DISTINCT turns the sidecar's ring keeps for the dashboard row.
#
# The ring exists so the Oversee row is one O(1) file read rather than a ledger
# scan on a route every open page polls. It holds no injected TEXT for the same
# reason it is bounded at all: the sidecar is rewritten on every single prompt,
# and twelve turns of capped text would turn that into a hundreds-of-kilobyte
# atomic write per keystroke-batch. The ring is the INDEX -- names, verdicts,
# sizes and reasons -- and the full text lives on the current turn and in the
# ledger row, which is where a replay of an older turn comes from.
RECENT_CAP = 12

# Where each registered name's `probe()` lives — every name that actually runs
# on the hook path. A name registered in `INJECTOR_NAMES` but absent here is
# reported by `probe_one` as a silence with a reason rather than raising, which
# is what let `symbols` be reserved before it was written.
_MODULES = {
    "promptgate": "mindhive.promptgate",
    "enrich": "mindhive.enrich",
    "mindnet": "mindhive.mindnet.inject",
    "premise": "mindhive.premise",
    "sopgate": "mindhive.sopgate",
    "style": "mindhive.style",
    "symbols": "mindhive.symbols",
}


class Injection(NamedTuple):
    """One pass's contribution to one turn.

    `chars` is what was INJECTED; `text` is what was STORED, and the two differ
    whenever the cap bites. Reporting the truncated length would make the ledger
    disagree with the prompt about what Claude actually received."""
    name: str
    verdict: str
    chars: int
    tokens: int
    ms: int
    reason: str
    text: str


# --- measurement: the hot path ----------------------------------------------

def _tokens(chars: int) -> int:
    """The project's standing 4-chars-per-token estimate, as `context.py` uses.

    An estimate is honest here in a way a tokenizer would not be: no tokenizer
    ships in `dependencies = []`, and a per-prompt tokenizer call would cost more
    than the number is worth."""
    return max(0, int(chars) // 4)


def _delta_text(before: str, after: str) -> str:
    """The text *after* gained over *before*.

    Handles BOTH directions because the pipeline does both: `promptgate` and
    `mindnet` append, while `sopgate` and `style` PREPEND — `style` runs last
    precisely so its prepend survives truncation of anything earlier. Assuming a
    suffix would silently report every prepending pass as contributing nothing,
    which is the exact blindness this module exists to end."""
    if not before:
        return after
    if after.startswith(before):
        return after[len(before):]
    if after.endswith(before):
        return after[:len(after) - len(before)]
    # Neither: the pass rewrote rather than extended. The size is still true,
    # so report the whole thing rather than claiming an isolated fragment.
    return after


def measure(name: str, before, after, ms: int = 0, reason: str = "") -> Injection:
    """One pass's `Injection`, from the context string either side of it.

    Two `len()` calls and a slice. This is what the hook path uses, and it is
    why recording a turn adds no measurable work to the prompt."""
    b, a = str(before or ""), str(after or "")
    text = _delta_text(b, a)
    # max(0, ...) is not defensive noise: a pass that SHRANK the context is a
    # contract violation, and a negative size would render as a cat that
    # injected minus-forty characters. Clamp, and let the verdict carry it.
    chars = max(0, len(a) - len(b))
    if chars == 0:
        return Injection(name, "silent", 0, 0, ms,
                         reason or "no contribution this turn", "")
    return Injection(name, "verbatim", chars, _tokens(chars), ms,
                     reason or "delivered", text)


# --- probes: the diagnostic path --------------------------------------------

def module_for(name: str):
    """The module owning *name*'s `probe()`. Raises `KeyError` for a name with
    no module — `probe_one` is the total wrapper, this is the sharp edge."""
    return importlib.import_module(_MODULES[name])


# `sopgate` reports on a four-value scale; this module publishes three. The
# finer word is not lost, it moves into `reason`.
_VERDICT_ALIASES = {"pointers_only": "partial"}


def _coerce(name: str, raw, ms: int) -> Injection:
    """A probe's return, whatever shape it chose, as an `Injection`."""
    if isinstance(raw, Injection):
        return raw._replace(name=name)
    if not isinstance(raw, dict):
        return Injection(name, "silent", 0, 0, ms,
                         f"probe returned {type(raw).__name__}, not a report", "")
    verdict = str(raw.get("verdict") or "silent")
    reason = str(raw.get("reason") or "")
    if verdict in _VERDICT_ALIASES:
        reason = reason or f"{verdict}: every body degraded to a pointer"
        verdict = _VERDICT_ALIASES[verdict]
    if verdict not in VERDICTS:
        verdict = "silent"
    chars = int(raw.get("chars") or raw.get("block_chars") or 0)
    return Injection(name, verdict, chars, _tokens(chars), ms,
                     reason or f"{verdict}", str(raw.get("text") or ""))


def probe_one(name: str, **kw) -> Injection:
    """Ask *name* what it would deliver. Total: never raises.

    A probe that can fail is one more thing to check — the same contract
    `sopgate.probe` already states about itself."""
    t0 = time.perf_counter()
    try:
        mod = module_for(name)
        raw = mod.probe(**kw) if kw else mod.probe()
        return _coerce(name, raw, int((time.perf_counter() - t0) * 1000))
    except Exception as exc:                                # noqa: BLE001
        return Injection(name, "silent", 0, 0,
                         int((time.perf_counter() - t0) * 1000),
                         f"probe failed: {type(exc).__name__}: {exc}", "")


def probe_all() -> list:
    """Every live injector's current report, in pipeline order."""
    return [probe_one(n) for n in INJECTOR_NAMES if n in _MODULES]


# --- the sidecar ------------------------------------------------------------

def sidecar_path(project=None) -> Path:
    """``<project>/injection/last.json`` — the current turn, always."""
    return paths.project_dir(project) / "injection" / "last.json"


def read_last(project=None) -> dict:
    """The last recorded turn, or ``{}``.

    Total. An unreadable or corrupt sidecar degrades to "no previous turn",
    which costs one redundant ledger row and nothing else; raising here would
    put a JSON parse error on the per-prompt hot path."""
    try:
        raw = sidecar_path(project).read_text(encoding="utf-8")
        doc = json.loads(raw)
        return doc if isinstance(doc, dict) else {}
    except Exception:                                       # noqa: BLE001
        return {}


def _write_sidecar(doc: dict, project=None) -> Path:
    """Publish the injection sidecar through the ONE shared atomic writer.

    This used to hand-roll its own `pid.thread.tmp` name and `os.replace`,
    with a comment saying `paths.write_text_atomic` could not be used because it
    derived its temp name from the target alone — two writers would share one
    `.tmp` and publish a torn hybrid. That was true once and is not any more:
    `write_text_atomic`'s temp name is `<name>.<pid>.<counter>.tmp`, unique per
    process AND per call, so it is now safe for the dashboard's threads. Keeping
    the bespoke copy meant keeping a WORKAROUND for a bug that had been fixed
    centrally, and its stale comment actively told the next reader the opposite.

    Routing through the shared writer also fixes a real loss this copy carried:
    it re-raised WinError 5 on a concurrent read, and this module's caller is
    fail-open, so that raise silently dropped the turn's sidecar record. The
    shared writer retries that sharing violation instead."""
    p = sidecar_path(project)
    paths.write_text_atomic(
        p, json.dumps(doc, ensure_ascii=False, sort_keys=True))
    return p


# --- the record ------------------------------------------------------------

def turn_signature(injections) -> str:
    """What makes two turns "the same turn".

    Covers the composed text AND every verdict. Text alone would miss the case
    this feature exists for: `sopgate` degrading from verbatim to pointers ships
    the SAME bytes with a different meaning, and a content-only hash would
    record that transition as nothing at all."""
    head = "\x1f".join(f"{i.name}|{i.verdict}|{i.chars}" for i in injections)
    body = "".join(str(i.text or "") for i in injections)
    return hashlib.sha256(f"{head}\x1e{body}".encode("utf-8", "replace")).hexdigest()


def _row(i: Injection) -> dict:
    """One injection as a stored row. `chars` stays true; `text` gets capped."""
    return {"name": i.name, "verdict": i.verdict, "chars": int(i.chars),
            "tokens": int(i.tokens), "ms": int(i.ms), "reason": str(i.reason),
            "text": str(i.text or "")[:MAX_TEXT_CHARS]}


def record_turn(injections, session_id=None, project=None) -> dict:
    """Record one turn. Appends a ledger row only when the turn CHANGED.

    Returns a report rather than raising — every caller of this is on the prompt
    path, where an exception is a broken prompt. `error` carries the reason a
    row was not written, because a write that fails silently is the failure mode
    this whole module was built to end."""
    out = {"wrote_row": False, "error": "", "signature": "", "unchanged_turns": 0}
    try:
        injections = list(injections or [])
        sig = turn_signature(injections)
        prev = read_last(project=project)
        pending = int(prev.get("unchanged_turns") or 0)
        changed = sig != str(prev.get("signature") or "")
        out["signature"] = sig

        doc = {
            "session_id": str(session_id or ""),
            "signature": sig,
            "injectors": [_row(i) for i in injections],
            "total_chars": sum(int(i.chars) for i in injections),
            "total_tokens": sum(int(i.tokens) for i in injections),
            "block": "".join(str(i.text or "") for i in injections)[:MAX_BLOCK_CHARS],
            "unchanged_turns": 0 if changed else pending + 1,
        }

        # The ring follows the SAME distinctness rule as the ledger, and for the
        # same reason: twelve identical rows are noise, not signal. An unchanged
        # turn bumps a counter on the head entry, so the operator sees "this
        # injection, three times" rather than three rows they must compare by eye.
        #
        # `or not ring` is the case the REAL hook found, not a test: a steady
        # state that began before the ring existed -- or after any sidecar reset
        # -- has no head entry to bump, and inserting on change alone would leave
        # the row reading "nothing injected yet" while an injection was in fact
        # happening on every prompt. That is the exact false silence this feature
        # exists to end. `repeats` then comes from `unchanged_turns`, which has
        # been counting those turns the whole time, so the seeded entry claims a
        # number it can support.
        ring = [r for r in (prev.get("recent") or []) if isinstance(r, dict)]
        if changed or not ring:
            ring.insert(0, {
                "ts": events.now_iso(),
                "session_id": str(session_id or ""),
                "signature": sig,
                "total_chars": doc["total_chars"],
                "total_tokens": doc["total_tokens"],
                "repeats": 1 if changed else doc["unchanged_turns"] + 1,
                "injectors": [{k: r[k] for k in
                               ("name", "verdict", "chars", "tokens", "ms", "reason")}
                              for r in doc["injectors"]],
            })
            del ring[RECENT_CAP:]
        else:
            ring[0] = dict(ring[0], repeats=int(ring[0].get("repeats") or 1) + 1)
        doc["recent"] = ring

        if changed:
            try:
                # The row that closes a run of identical turns carries that
                # run's length, so the turns which bought no row are still
                # exactly accounted for.
                events.append_event(EVENT_TYPE, dict(doc, unchanged_turns=pending),
                                    session_id=session_id, project=project)
            except Exception as exc:                        # noqa: BLE001
                # Leave the sidecar untouched: the signature must not advance
                # past a turn that never reached the ledger, or the next
                # identical turn would consider itself already recorded.
                out["error"] = f"{type(exc).__name__}: {exc}"
                return out
            out["wrote_row"] = True

        out["unchanged_turns"] = doc["unchanged_turns"]
        try:
            _write_sidecar(doc, project=project)
        except Exception as exc:                            # noqa: BLE001
            out["error"] = f"sidecar: {type(exc).__name__}: {exc}"
        return out
    except Exception as exc:                                # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
        return out


# --- the seam cmd_hook uses -------------------------------------------------

def context_text(ctx) -> str:
    """The `additionalContext` string inside a hook context, or ``""``.

    Total, and deliberately so: this is called twice per injector on the prompt
    path, and a shape it did not expect must read as "no context" rather than as
    an exception thrown into somebody's prompt."""
    try:
        hso = (ctx or {}).get("hookSpecificOutput") or {}
        return str(hso.get("additionalContext") or "")
    except Exception:                                       # noqa: BLE001
        return ""


def merge_hook_context(context, text, *, sep="\n\n", prepend=False):
    """Fold *text* into a UserPromptSubmit hook context — the ONE writer.

    The inverse of `context_text`, and the single construction of the house
    payload shape. Five passes (`premise`, `sopgate`, `style`, `symbols`,
    `mindnet`) hand-rolled this identical dict, differing only in two axes this
    signature names: `prepend` (append is the default; `sopgate`/`style` put
    their block FIRST so it survives truncation) and `sep` (`\\n\\n` for all but
    `mindnet`, which joins with a single `\\n`). Collapsing them here is not
    tidiness for its own sake: the duplication is exactly where `symbols` forgot
    the `is_machine_prompt` gate the other four carried.

    Total and defensive like the callers it replaces: *context* may be ``None``
    or a non-dict (a fresh context is BUILT, because `style`/`mindnet` speak on a
    clear prompt too), existing `additionalContext` is preserved, `hookEventName`
    is always stamped, and every other context and `hookSpecificOutput` key
    survives. The input is never mutated — the returned dicts are fresh."""
    base = context if isinstance(context, dict) else {}
    hso = dict(base.get("hookSpecificOutput") or {})
    existing = str(hso.get("additionalContext") or "")
    if not existing:
        merged = text
    elif prepend:
        merged = f"{text}{sep}{existing}"
    else:
        merged = f"{existing}{sep}{text}"
    hso["hookEventName"] = "UserPromptSubmit"
    hso["additionalContext"] = merged
    return dict(base, hookSpecificOutput=hso)


def now() -> float:
    """A monotonic start stamp. Wrapped so the hook path names one clock."""
    return time.perf_counter()


def since(t0) -> int:
    """Whole milliseconds since *t0*. Never raises, never negative."""
    try:
        return max(0, int((time.perf_counter() - float(t0)) * 1000))
    except Exception:                                       # noqa: BLE001
        return 0


# --- the pipeline -----------------------------------------------------------

def run_pipeline(raw, spine=None):
    """Compose the UserPromptSubmit context, measuring each pass as it runs.

    Returns ``(context, rows)``: the hook output to print, and one `Injection`
    per pass for `record_turn`.

    **The ORDER is the contract, and this function is its one home.** It used to
    exist only as the sequence of statements inside `__main__.cmd_hook`, which
    meant the single most load-bearing fact about the injection path -- that
    `style` runs LAST so its prepend survives truncation of anything earlier --
    was documented nowhere a reader would look. `docs/session-legend.md` names
    all six modules as six unrelated table rows.

    Measurement is two `len()` calls and a slice per pass. No pass is re-run, no
    block is re-rendered, and no sidecar is read twice: the spine is read once by
    the caller and threaded in (MN-17), because `record_direction` and
    `announce_faculties` need that same object afterwards.

    Every pass is additive and fail-open; each returns *context* untouched
    (possibly falsy) when it has nothing to say or when anything goes wrong. That
    contract is the reason this function does not need a `try` of its own, and
    the reason the caller still prints only when something actually spoke."""
    from . import enrich, premise, promptgate, sopgate, style, symbols
    from .mindnet import inject as mn_inject

    rows = []

    def step(name, ctx, fn):
        prev, t = context_text(ctx), now()
        out = fn(ctx)
        rows.append(measure(name, prev, context_text(out), since(t)))
        return out

    # The prompt-quality gate: skill-trigger directives when the typed prompt is
    # underspecified. A clear prompt yields nothing at all, which is why every
    # later pass must tolerate a falsy context.
    context = step("promptgate", None, lambda _c: promptgate.hook_context(raw))

    # Enrichment EXTENDS the gate's context and never creates one -- the
    # clarifying questions only make sense attached to a gate that fired.
    context = step("enrich", context,
                   lambda c: enrich.extend_hook_context(c, raw) if c else c)

    # MN-1 U9: the phase spine steers EVERY turn, including a clear prompt. That
    # unconditional reach is the whole point, so it sits outside the `if` above
    # rather than riding the gate's decision.
    context = step("mindnet", context,
                   lambda c: mn_inject.extend_hook_context(c, raw, sc=spine))

    # Section 0's mechanical half: check the checkable factual claims this prompt
    # makes against the filesystem, the roadmap fold and the last recorded test
    # run. Unconditional, and with a sharper edge than the gate -- `promptgate`
    # stops scanning at PROMPT_SCAN_CAP, so the long, assertion-dense prompts
    # most likely to carry a fabricated premise are exactly the ones it never sees.
    context = step("premise", context,
                   lambda c: premise.extend_hook_context(c, raw))

    # The MANDATORY SOP pass: the user's standing procedures, verbatim, on every
    # prompt. A procedure declared to hold on every task but delivered only at
    # session start is exactly the instruction that decays by turn 80. Reads the
    # materialized sidecar, never the ledger -- `read_all` is 0.36 s on the live
    # store and this sits on the per-prompt hot path.
    context = step("sopgate", context,
                   lambda c: sopgate.extend_hook_context(c, raw))

    # T8: the zero-token structural map. Second-to-last, and OFF unless
    # `MINDHIVE_SYMBOLS` is set -- it is the one pass that spends a four-figure
    # token count on every prompt, so it is registered and switched on
    # separately, exactly as the rotation hook is. Switched off it still
    # produces a row, because "did not inject" is an answer the operator asked
    # to be able to see.
    context = step("symbols", context,
                   lambda c: symbols.extend_hook_context(c, raw))

    # The standing reply-style pass. LAST, and that placement is the contract:
    # it prepends, so running last is what makes the style survive truncation of
    # everything before it. It is also the one pass that creates the context from
    # nothing, which is why a bare greeting still costs ~1,235 tokens.
    context = step("style", context,
                   lambda c: style.extend_hook_context(c, raw))

    return context, rows


def recent_turns(project=None) -> list:
    """The bounded ring of recent DISTINCT turns, newest first.

    What the dashboard's injection row draws. One O(1) file read -- no ledger
    scan on a route every open page polls, which is the mistake
    `/api/mindnet/cats` had to be gated after making.

    Total: a missing or corrupt sidecar is an empty board, never an exception on
    a request handler."""
    ring = read_last(project=project).get("recent")
    return [r for r in ring if isinstance(r, dict)] if isinstance(ring, list) else []
