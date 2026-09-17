# SPDX-License-Identifier: Apache-2.0
# src/mindhive/core/models.py
"""The ledger's domain primitives, moved here VERBATIM from `events.py`
(blueprint P0b, decision K6): the genesis hash, the one chain step and the
frozen `EventRecord` a reader builds per physical row. `events` re-exports
all three by identity -- `events.EventRecord is core.models.EventRecord` --
so every existing spelling still resolves to the same object and no chain
head moves.

A stdlib leaf: `hashlib` and `dataclasses` only. It must never import
`events` (which imports it) nor anything that touches a filesystem.
"""
import hashlib
from dataclasses import dataclass

# --- the hash chain's primitives, exported ------------------------------------
# `chain.py` has always folded the ledger into a SHA-256 chain, but the fold
# lived inline in `chain.compute_chain` and `chain._fold` with nothing exporting
# the formula. The background watcher builds an `EventRecord` per physical row
# and stores `prev_hash` / `chain_hash` in vector metadata; a formula re-derived
# there by hand could drift from `chain.py` by one `.encode()` argument, and the
# two digests for one row would disagree without anything noticing. So the
# primitive lives here. `chain._ZERO` is not rewritten to import this constant
# -- `chain.py` is untouched -- and `tests/test_events_chain_helpers.py` pins
# the two equal and the fold identical in both of `chain.py`'s coordinates.
GENESIS_PREV_HASH = "0" * 64


def compute_chain_hash(prev_hash: str, canonical: str) -> str:
    """One chain step, byte for byte what `chain.compute_chain` and
    `chain._fold` run: SHA-256 over the UTF-8 bytes of `prev_hash + canonical`,
    where *canonical* is the str `canonical_json(to_dict(ev))` returns. NOT
    the PDF's `bytes` parameter with an ASCII-encoded prefix -- a second scheme
    would make the watcher's digest and the checkpoint's two different numbers
    for one row (plan decision AMBIG 3)."""
    return hashlib.sha256((prev_hash + canonical).encode("utf-8")).hexdigest()


@dataclass(frozen=True)
class EventRecord:
    """One physical row as a READER sees it, with its chain coordinates.

    A runtime wrapper, never the row (AMBIG 3): the segment keeps exactly
    `Event`'s five keys, because every checkpoint on disk was folded over
    those bytes and a `prev_hash` written into the row would move every head
    on the first append. Readers build this from a row -- `event_id` is
    `f"{session_id}:{seg_line}"` with `seg_line` the 1-BASED physical line --
    and it is frozen so a record handed to a vector store cannot be edited
    after its `chain_hash` was computed over it."""
    event_id: str
    session_id: str
    seg_line: int
    timestamp: str
    event_type: str
    payload: dict
    tags: list
    prev_hash: str
    chain_hash: str

__all__ = ["GENESIS_PREV_HASH", "compute_chain_hash", "EventRecord"]
