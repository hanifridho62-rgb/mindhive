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


def write_chain(project=None, evs=None) -> dict:
    # Accept pre-parsed events to avoid a second full read of the event log when
    # the caller (e.g. projector.project) already has them in hand.
    c = compute_chain(events.read_all(project) if evs is None else evs)
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
