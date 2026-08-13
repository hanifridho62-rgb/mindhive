# src/mindhive/sopgate.py
"""The per-prompt mandatory-SOP pass: re-assert every MANDATORY SOP on every
human-typed prompt, verbatim.

## Why this exists as a hook and not just a registry entry

`sop.py` already delivers the registry once per session, as one block inside
STATE.md -> MEMORY.md. That is the right delivery for a scope-matched
procedure — you read it when the task lands in its scope. It is the wrong
delivery for a procedure that must hold on *every* task, for the reason
`style.py` states about the caveman style: an instruction read once, at session
start, about behaviour that has to hold at turn 80 is precisely the instruction
that decays, because later context crowds out earlier context. The reply style
solved that with a per-prompt hook. The mandatory SOP tier is that same remedy
applied to the user's own standing procedures.

## Why a separate module from `promptgate`

`promptgate.hook_context` is a CONDITIONAL gate: it fires on a prompt that
looks underspecified, and its silence is a correct answer. A mandatory SOP is
UNCONDITIONAL. Fusing the two would make "the gate found nothing" and "the hook
said nothing" indistinguishable — `style.py` names that trap explicitly, and
`mn_inject`, `premise` and `style` already establish
``extend_hook_context(context, raw)`` as the seam every unconditional pass
rides. This is one more pass on that seam.

## Three properties that are not negotiable

- **Verbatim.** The registry is the source of truth; this module is transport.
  A generated summary of a procedure is a paraphrase, and a paraphrase
  re-derived per prompt drifts from the record it claims to carry. Over budget,
  a body degrades to a POINTER, never to a truncation — half a procedure reads
  like a whole one.
- **Sidecar only, never the ledger.** `events.read_all` measures 0.36 s on the
  live store (26k events) and the whole UserPromptSubmit hook costs about that
  much. Folding here would double the latency of every prompt the user types.
  `sop.read_sidecar` is the cheap materialized view; there is deliberately no
  ledger fallback, because a fallback reintroduces the exact cost the sidecar
  exists to avoid.
- **Fail-open, and never a decision.** Any error, missing sidecar, or malformed
  payload returns the context untouched. This module only ever writes
  ``additionalContext``. A hook that can break a prompt is far worse than one
  that occasionally says nothing.
"""
from __future__ import annotations

import json

from . import sop
from .promptgate import PROMPT_SCAN_CAP

# Marker so an injected SOP block is greppable in a transcript and tellable
# apart from `style`'s "◆" and promptgate's "[mindhive-promptgate]".
SOP_MARK = "▣"

# Total characters of SOP BODIES injected per prompt. A hard constant rather
# than an emergent property: without it, promoting a second SOP silently
# doubles the per-prompt cost of every session on this machine. Bodies beyond
# the budget render as one-line pointers instead.
#
# 3000 is measured, not guessed: the founding mandatory SOP
# (`docs/sops/execution-discipline.md`) is 2722 characters, so this is that
# body plus modest headroom for it to be revised without silently degrading to
# a pointer. About 750 tokens on every human-typed prompt — the declared price
# of the tier. `hive context` measures what the auto-loaded side already costs;
# raising this is a deliberate decision to spend more per prompt, not a knob to
# turn when a new SOP does not fit.
PROMPT_BUDGET = 3000

_HEADER = (
    f"{SOP_MARK} MANDATORY SOP — standing procedure, re-asserted every prompt.\n"
    "These are not scope-matched suggestions. They hold for this task, now, "
    "whether or not the prompt mentions them. Registry & history: "
    "`hive sop list` / `hive sop show <id>`."
)


def _is_injected(text) -> bool:
    """True for text the user did not type: harness tags and injected skill
    bodies.

    Borrowed from `style._is_injected`, NOT from `promptgate.is_machine_prompt`,
    and the one difference is the whole point: that predicate answers True for a
    leading `/`, which is right for grading prompt QUALITY and wrong here. A
    slash command is the user speaking, and the standing procedure still governs
    the work it asks for. `tests/test_sopgate.py` pins this independently of
    `tests/test_style.py` so a future unification of the two predicates fails
    loudly rather than silently dropping the block on every slash-command turn.
    """
    s = str(text or "").lstrip()
    if not s:
        return True
    if s.startswith("<"):                       # harness/system tags
        return True
    return s[:40].lower().startswith("base directory for this skill:")


def _safe_title(value) -> str:
    """Whitespace-collapsed, length-capped title.

    Titles come from the registry and flow verbatim into agent-steering
    context, so a newline in one must not be able to forge a second directive
    line. Same hardening, and the same reason, as `promptgate._safe_text`."""
    return " ".join(str(value or "").split())[:120]


def plan(rows) -> list[dict]:
    """Per-SOP delivery decision for one prompt — the ONE place the budget is
    spent. Junk rows and rows missing an id or a body are dropped here, so
    everything downstream sees only deliverable records.

    Extracted from `render` rather than re-derived by `probe`, and that is the
    whole point of it existing: a probe carrying its own copy of the fit rule can
    report "the procedure is injected verbatim" while the hook path ships
    pointers — which is precisely the failure a probe exists to catch. `render`
    renders this list and `probe` counts it, so the two cannot disagree.

    Each record: `sop_id`, `title`, `chars`, `delivered` ("body" | "pointer"),
    and `text` (the body itself, for `render`; `probe` drops it)."""
    out, spent = [], 0
    for r in rows or []:
        if not isinstance(r, dict):
            continue                    # a junk row must not sink the block
        sop_id = _safe_title(r.get("sop_id"))
        title = _safe_title(r.get("title"))
        text = str(r.get("text") or "").strip()
        if not sop_id or not text:
            continue
        fits = spent + len(text) <= PROMPT_BUDGET
        if fits:
            spent += len(text)
        out.append({"sop_id": sop_id, "title": title, "chars": len(text),
                    "delivered": "body" if fits else "pointer", "text": text})
    return out


def render(rows) -> str:
    """The injected block for *rows* (sidecar records), or "" when there is
    nothing to say. Pure and deterministic: same rows in, same bytes out.

    Bodies ride verbatim while the running total fits `PROMPT_BUDGET`; the rest
    become `hive sop show <id>` pointers. Order is the sidecar's, which is
    `sop.mandatory`'s `(created, sop_id)` — so promoting a new SOP never
    reshuffles the ones already in force."""
    delivered = plan(rows)
    lines = []
    for row in delivered:
        lines.append(f"— {row['title']} `[{row['sop_id']}]`")
        if row["delivered"] == "body":
            lines.append(row["text"])
        else:
            lines.append(f"  (body over the per-prompt budget — read it now: "
                         f"`hive sop show {row['sop_id']}`)")
    if not lines:
        return ""
    if not any(row["delivered"] == "body" for row in delivered):
        # Every body was over budget. The pointers still carry the obligation,
        # so the block is worth sending — but say plainly that the text is not
        # here, rather than letting a list of titles read as the procedure.
        lines.append("  None of the bodies fit this prompt's budget — read "
                     "them before acting.")
    return "\n".join([_HEADER] + lines)


def block_for(text, project=None) -> str:
    """The mandatory-SOP directive for one prompt, or "" when silent.

    Machine-injected text gets nothing: a task notification is not the user
    asking for anything, and spending context on one every time would be pure
    waste. Never raises."""
    try:
        if _is_injected(text):
            return ""
        # GLOBAL, not per-project. "Mandatory" was asked for as *every
        # session*; `paths.detect_project` gives each repo its own namespace,
        # so reading the per-project sidecar would silently mean "every session
        # in the repo where you happened to register it" — the hook is
        # registered globally and fires everywhere, so the data has to be
        # global too or the reach is a fiction.
        data = sop.read_global_sidecar()
        return render(data.get("sops") if isinstance(data, dict) else None)
    except Exception:                                       # noqa: BLE001
        return ""


# What the injection is currently delivering, worst first. `pointers_only` is
# the state that matters: the block still arrives, so nothing looks broken, and
# the procedure it claims to re-assert is not in it.
VERDICTS = ("silent", "pointers_only", "partial", "verbatim")


def probe(rows=None, project=None) -> dict:
    """Measure what the per-prompt injection ACTUALLY delivers right now.

    The tier's promise is that every mandatory procedure rides verbatim on every
    human prompt. Whether it does is a fact about two numbers — the bodies'
    length and `PROMPT_BUDGET` — and until this existed, nothing reported it: a
    body that outgrew the budget degraded to a pointer silently, per prompt,
    forever, and the block still arrived so nothing looked wrong. That is the
    live state of this repo (one 3.4k-char mandatory SOP against a 3k budget).

    Reads the same global sidecar the hook path reads, and counts the same `plan`
    the hook path renders. Never raises: a probe that can fail is one more thing
    to check."""
    if rows is None:
        try:
            data = sop.read_global_sidecar()
            rows = data.get("sops") if isinstance(data, dict) else None
        except Exception:                                   # noqa: BLE001
            rows = None
    try:
        delivered = plan(rows)
        block = render(rows)
    except Exception:                                       # noqa: BLE001
        delivered, block = [], ""
    bodies = [r for r in delivered if r["delivered"] == "body"]
    pointers = [r for r in delivered if r["delivered"] == "pointer"]
    if not delivered:
        verdict = "silent"
    elif not bodies:
        verdict = "pointers_only"
    elif pointers:
        verdict = "partial"
    else:
        verdict = "verbatim"
    return {
        "budget": PROMPT_BUDGET,
        "verdict": verdict,
        "mandatory": len(delivered),
        "bodies": len(bodies),
        "pointers": len(pointers),
        "body_chars": sum(r["chars"] for r in bodies),
        "largest_chars": max((r["chars"] for r in delivered), default=0),
        "block_chars": len(block),
        # Bodies are deliberately NOT carried out of here: this is a size report,
        # and a report that embeds the text it measures cannot be printed.
        "sops": [{k: r[k] for k in ("sop_id", "title", "chars", "delivered")}
                 for r in delivered],
    }


_PROBE_VERDICTS = {
    "silent": "nothing is injected — no mandatory SOP in the global sidecar",
    "pointers_only": "NO body fits — every prompt gets titles and a pointer",
    "partial": "some bodies fit, the rest degraded to pointers",
    "verbatim": "every mandatory body rides in full",
}


def render_probe(result: dict) -> str:
    """Human-readable probe report. Says what a session RECEIVES, not what the
    tier intends to send — the gap between those two is the whole finding."""
    result = result or {}
    verdict = str(result.get("verdict") or "silent")
    lines = [f"Per-prompt SOP injection: {verdict} — "
             f"{_PROBE_VERDICTS.get(verdict, 'unknown state')}"]
    for row in result.get("sops") or []:
        mark = "body" if row.get("delivered") == "body" else "POINTER"
        lines.append(f"  {mark:<7} {row.get('chars', 0):>6,} chars  "
                     f"{row.get('sop_id')}  ({row.get('title')})")
    lines.append(f"  bodies {result.get('bodies', 0)}/{result.get('mandatory', 0)} · "
                 f"body chars {result.get('body_chars', 0):,}/"
                 f"{result.get('budget', 0):,} · "
                 f"largest {result.get('largest_chars', 0):,} · "
                 f"injected block {result.get('block_chars', 0):,} chars")
    if verdict in ("pointers_only", "partial"):
        lines.append("  Raising PROMPT_BUDGET is a deliberate decision to spend "
                     "more on every prompt; shortening the body is the other "
                     "lever. A pointer is honest, but it is not the procedure.")
    return "\n".join(lines)


def extend_hook_context(context, raw, project=None) -> dict:
    """Prepend the mandatory-SOP block to a UserPromptSubmit context.

    CREATES the context when every other pass stayed silent — a clear,
    well-phrased prompt is the common case, and the standing procedure applies
    to it exactly as much as to a vague one. Prepended so it survives any later
    truncation of a long injected block; `style` runs after this one and
    prepends again, leaving the reply style first and the procedure second.

    Fail-open in the project's house style: any error returns *context*
    unchanged."""
    try:
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            return context
        prompt = payload.get("prompt", "")
        # Bail on a pasted wall of text without normalizing it, matching
        # `style.is_continuation`: past the cap nothing about the block
        # changes, and the leading-slice check is all `_is_injected` needs.
        block = block_for(str(prompt)[:PROMPT_SCAN_CAP], project)
        if not block:
            return context
        hso = (context or {}).get("hookSpecificOutput") or {}
        existing = str(hso.get("additionalContext") or "")
        merged = f"{block}\n\n{existing}" if existing else block
        return dict(context or {}, hookSpecificOutput=dict(
            hso, hookEventName="UserPromptSubmit", additionalContext=merged))
    except Exception:                                       # noqa: BLE001
        return context
