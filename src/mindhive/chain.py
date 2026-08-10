# src/mindhive/chain.py
import hashlib
import json
from . import paths, events


def compute_chain(evs, checkpoint=None) -> dict:
    """Fold events into a SHA-256 hash chain -> {count, head}. When ``checkpoint``
    is an int, also return ``checkpoint_head``: the head after exactly that many
    events (i.e. the value write_chain would have stored at that count), or None
    when the log is shorter than the checkpoint claims. This lets verify() run a
    single-pass prefix-consistency check instead of a second fold over a slice."""
    prev = "0" * 64
    head = prev
    cp_head = prev if checkpoint == 0 else None
    for i, ev in enumerate(evs):
        material = prev + events.canonical_json(events.to_dict(ev))
        head = hashlib.sha256(material.encode("utf-8")).hexdigest()
        prev = head
        if i + 1 == checkpoint:
            cp_head = head
    out = {"count": len(evs), "head": head}
    if checkpoint is not None:
        out["checkpoint_head"] = cp_head
    return out


def sealable(evs) -> list:
    """The prefix a checkpoint may attest to: every event strictly before the
    newest timestamp on record.

    `read_all` orders by ``(ts, segment_name, line_index)``. That order is total
    and deterministic for a given set of files, but it is **not
    append-monotonic**: ledger timestamps are second-resolution and the
    tie-break is the segment's filename, so an event appended later with a ts
    equal to already-sealed rows can sort BEFORE them. A checkpoint whose
    boundary sits inside such a group is invalidated by the next arrival in that
    same second — measured on the live store, that kept `hive verify` red
    permanently, and an alarm that is always red attests to nothing.

    Ending the prefix at a second boundary fixes that: no later event can sort
    into a second that is already complete.

    The cost is real and bounded. Events in the newest second are unattested
    until a later projection seals them — the `ahead` window widens by at most
    one second's worth of events. A store written entirely within one second
    therefore seals nothing, which is the correct answer rather than a case to
    special-case around."""
    if not evs:
        return []
    newest = evs[-1].ts or ""
    end = len(evs)
    while end > 0 and (evs[end - 1].ts or "") == newest:
        end -= 1
    return evs[:end]


def write_chain(project=None, evs=None) -> dict:
    # Accept pre-parsed events to avoid a second full read of the event log when
    # the caller (e.g. projector.project) already has them in hand.
    rows = events.read_all(project) if evs is None else evs
    c = compute_chain(sealable(rows))
    pdir = paths.project_dir(project)
    pdir.mkdir(parents=True, exist_ok=True)
    (pdir / "chain.json").write_text(events.canonical_json(c), encoding="utf-8")
    return c


def verify(project=None, evs=None) -> dict:
    # Accept pre-read events so a caller that already has them (e.g. the dashboard
    # overview poll) need not read the whole event log a second time.
    try:
        rows = events.read_all(project) if evs is None else evs
    except json.JSONDecodeError as e:
        return {"ok": False, "reason": "corrupt_event_log", "detail": str(e)}
    except Exception as e:
        return {"ok": False, "reason": "read_error", "detail": str(e)}
    p = paths.project_dir(project) / "chain.json"
    if not p.exists():
        return {"ok": False, "reason": "no chain.json (run: hive project)",
                "actual": compute_chain(rows)}
    try:
        stored = json.loads(p.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError) as e:
        # A corrupt/truncated chain.json must not crash verify() — callers like
        # the dashboard overview poll rely on it always returning a verdict.
        return {"ok": False, "reason": "corrupt_chain_file", "detail": str(e),
                "actual": compute_chain(rows)}
    # chain.json is a CHECKPOINT refreshed at projection time — events are
    # routinely appended after it (the steady state). So the right integrity
    # test is prefix-consistency, not full equality: recompute the head after
    # the first stored["count"] events; it must reproduce the stored head or
    # already-recorded history was rewritten/truncated. Extra events past the
    # checkpoint are legitimate new appends, reported via "ahead" — NOT a
    # mismatch. (Full equality flagged every normal append as tampering, so the
    # integrity light was chronically red and a real tamper would hide in it.)
    scount = stored.get("count")
    computed = compute_chain(rows, checkpoint=scount)
    cp_head = computed.pop("checkpoint_head", None)
    actual = computed                     # {count, head} — unchanged shape for callers
    if not isinstance(scount, int) or "head" not in stored:
        return {"ok": False, "reason": "corrupt_chain_file",
                "stored": stored, "actual": actual}
    # cp_head is None when the log is shorter than the checkpoint (history
    # truncated); a diverging head means checkpointed history was rewritten.
    if cp_head is None or cp_head != stored["head"]:
        return {"ok": False, "reason": "hash_mismatch", "stored": stored, "actual": actual}
    result = {"ok": True, "stored": stored, "actual": actual}
    ahead = actual["count"] - scount
    if ahead:
        result["ahead"] = ahead
    return result
