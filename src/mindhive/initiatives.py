# SPDX-License-Identifier: Apache-2.0
"""Autonomous initiatives (blueprint BAB 5.3.2): the ``initiative_declared``
event type, and the one listing of every initiative a namespace holds.

BAB 5.3.2 forbids an autonomous step going in silently. An initiative reaches
the ledger in one of two ways, and `initiatives` lists both, in ledger order:

* a ``work_session`` step whose ``initiative_tag`` is ``[INITIATIVE: …]`` -- the
  step IS its declaration. That covers ``hive worksession record --initiative``
  and the §8.4 API (`worksessions.create_work_session_step` with
  ``is_initiative=True``) alike, so a sub-agent writing through the API gets the
  same badge the CLI does;
* an ``initiative_declared`` row, ``{reason, ref, tag}``, for a decision that is
  not one step (``hive worksession initiative``, `declare_initiative`).

The type has its own contract module, as `consensus.contributions` and
`consensus.records` do, because `contracts.validate_typed_payload` hands a
module only the payload: when one module served both work-session types by
guessing from the payload's shape, each type accepted the other's rows and a
step-status payload stored as ``initiative_declared`` listed as an initiative
nobody declared. `validate_payload` refuses a work-session key outright,
requires ``reason`` and ``ref`` in the cleaned form the tag carries (so neither
can smuggle a ``| Ref:`` or a bracket in), and requires ``tag`` to equal
`worksessions.make_initiative_tag(reason, ref)`.
"""
from __future__ import annotations

from . import events
from . import worksessions as ws

_STEP_KEYS = ("action", "session_id", "step_id", "phase", "ws_tag", "initiative_tag",
              "usage_tag", "status", "timestamp")


def declare_initiative(session_id: str, reason: str, ref: str = "",
                       project: str | None = None) -> str:
    """Append ``initiative_declared`` and return its tag. The reason and the ref
    are stored cleaned (`worksessions.clean_tag_text`), exactly as the tag
    carries them, so the row always satisfies its own contract."""
    clean_reason = ws.clean_tag_text(reason if isinstance(reason, str) else "",
                                     ws.TAG_TEXT_MAX_CHARS)
    if not clean_reason:
        raise ValueError("initiative_declared: kunci 'reason' wajib berupa teks non-kosong")
    clean_ref = ws.clean_tag_text(ref if isinstance(ref, str) else "", ws.TAG_TEXT_MAX_CHARS)
    tag = ws.make_initiative_tag(clean_reason, clean_ref)
    events.append_event("initiative_declared",
                        {"reason": clean_reason, "ref": clean_ref, "tag": tag},
                        session_id=session_id, tags=["initiative"], project=project)
    return tag


def validate_payload(payload) -> None:
    """The ``initiative_declared`` contract (see the module docstring)."""
    if not isinstance(payload, dict):
        raise ValueError("initiative_declared: payload harus berupa objek JSON")
    stray = sorted(k for k in payload if k in _STEP_KEYS)
    if stray:
        raise ValueError(f"initiative_declared: kunci {stray} milik work_session "
                         "(hive worksession record)")
    reason = payload.get("reason")
    if not isinstance(reason, str) or not reason.strip():
        wrapped = " (tanpa --json, `hive append` mengirim teks sebagai kunci 'text')" \
            if set(payload) == {"text"} else ""
        raise ValueError("initiative_declared: kunci 'reason' wajib berupa teks "
                         f"non-kosong{wrapped}")
    ref = payload.get("ref", "")
    if not isinstance(ref, str):
        raise ValueError("initiative_declared: kunci 'ref' harus berupa teks")
    for key, value in (("reason", reason), ("ref", ref)):
        if value != ws.clean_tag_text(value, ws.TAG_TEXT_MAX_CHARS):
            raise ValueError(f"initiative_declared: kunci '{key}' tidak boleh memuat '|', "
                             "'[', ']', baris baru atau spasi berlebih (maks "
                             f"{ws.TAG_TEXT_MAX_CHARS} karakter)")
    if payload.get("tag") != ws.make_initiative_tag(reason, ref):
        raise ValueError("initiative_declared: kunci 'tag' harus sama dengan "
                         "make_initiative_tag(reason, ref)")


def ledger_body(payload) -> str:
    """The LEDGER entry for an ``initiative_declared`` row."""
    p = payload if isinstance(payload, dict) else {}
    tag = p.get("tag") or ws.make_initiative_tag(str(p.get("reason") or ""),
                                                 str(p.get("ref") or ""))
    return f"[initiative] {tag}"


def initiatives(evs) -> list:
    """Every initiative in *evs*, oldest first: ``{session_id, reason, ref, tag,
    ts, step_id}``, where ``step_id`` names the step that declared it and is ""
    for a declaration row. Total: a declaration without a reason, or a step the
    work-session fold would not keep (`worksessions.step_append_payload`, first
    row per step id), is skipped."""
    out, seen = [], set()
    for ev in evs:
        if ev.type == "initiative_declared" and isinstance(ev.payload, dict):
            p = ev.payload
            reason = p.get("reason")
            if not isinstance(reason, str) or not reason.strip():
                continue
            ref = p.get("ref") if isinstance(p.get("ref"), str) else ""
            tag = p.get("tag") if isinstance(p.get("tag"), str) and p.get("tag") \
                else ws.make_initiative_tag(reason, ref)
            out.append({"session_id": ev.session_id, "reason": reason, "ref": ref,
                        "tag": tag, "ts": ev.ts, "step_id": ""})
            continue
        p = ws.step_append_payload(ev)
        if p is None or (ev.session_id, p["step_id"]) in seen:
            continue
        seen.add((ev.session_id, p["step_id"]))
        parsed = ws.parse_initiative_tag(p.get("initiative_tag"))
        if parsed is not None:
            out.append({"session_id": ev.session_id, "reason": parsed[0], "ref": parsed[1],
                        "tag": p["initiative_tag"], "ts": ev.ts, "step_id": p["step_id"]})
    return out


__all__ = ["declare_initiative", "validate_payload", "ledger_body", "initiatives"]
