# tests/test_vectors_v1.py
"""The v1 vector surface (blueprint P1h; PDF BAB 4.3, 8.2; MEB VEC-01, K20).

`tests/test_vectors.py` covers what `vectors.py` already did -- the LEDGER
collection, the event collection, the pack/rank byte format. This file covers
what P1h added on top and nothing else:

* the CPU thread lock (`THREAD_ENV` / `apply_thread_env`), which has to be in
  place BEFORE the numeric stack is imported or it does nothing at all;
* the full HNSW tuning metadata (K20), and a rebuild that triggers on ANY of
  its keys rather than on `hnsw:space` alone;
* `chroma_lock`, the one `O_EXCL` sidecar that keeps two processes out of the
  same SQLite file;
* `VectorStoreManager` and the batch-embedding surface the watcher drives --
  ids, back-pointer metadata, cooperative yields, progress callbacks.

Everything here runs with the extras absent: the encoder, the client and the
collection are stubs, exactly as in `tests/test_vectors.py`.
"""
import json
import os
import subprocess
import sys
import threading
import time

import pytest

from mindhive import events as events_mod
from mindhive import paths, vectors
from mindhive.core import locks as core_locks
from mindhive.core.models import EventRecord


# --- fixtures ----------------------------------------------------------------

def _rec(session_id="alpha", seg_line=1, event_type="decision",
         payload=None, ts="2026-01-01T00:00:00Z", chain_hash="c0ffee",
         prev_hash="0" * 64, tags=None):
    return EventRecord(event_id=f"{session_id}:{seg_line}",
                       session_id=session_id, seg_line=seg_line,
                       timestamp=ts, event_type=event_type,
                       payload={"text": "chose postgres"} if payload is None
                       else payload,
                       tags=list(tags or []), prev_hash=prev_hash,
                       chain_hash=chain_hash)


class _Col:
    """A chroma collection stand-in that records every write it is given."""

    def __init__(self, metadata=None):
        self.metadata = dict(metadata or vectors.HNSW_METADATA)
        self.batches = []
        self.present = set()
        self.queried = None
        self._result = {"documents": [[]], "ids": [[]], "distances": [[]],
                        "metadatas": [[]]}

    def set_result(self, docs, ids, distances, metadatas=None):
        self._result = {"documents": [docs], "ids": [ids],
                        "distances": [distances],
                        "metadatas": [metadatas or [{}] * len(docs)]}

    def add(self, ids=None, documents=None, embeddings=None, metadatas=None):
        self.batches.append({"ids": list(ids or []),
                             "documents": list(documents or []),
                             "embeddings": list(embeddings or []),
                             "metadatas": list(metadatas or [])})
        self.present.update(ids or [])

    def get(self, ids=None):
        return {"ids": [i for i in (ids or []) if i in self.present]}

    def query(self, query_embeddings=None, n_results=10, where=None):
        self.queried = {"n_results": n_results, "where": where}
        return self._result

    def count(self):
        return len(self.present)


class _Client:
    def __init__(self, existing=None):
        self.existing = existing            # metadata a pre-existing collection has
        self.created = []
        self.deleted = []

    def get_or_create_collection(self, name, metadata=None):
        self.created.append((name, metadata))
        if self.existing is not None:
            col, self.existing = _Col(self.existing), None
            return col
        return _Col(metadata)

    def delete_collection(self, name):
        self.deleted.append(name)


def _stub(monkeypatch, client=None, encoder=None):
    client = client if client is not None else _Client()
    monkeypatch.setattr(vectors, "_require_deps", lambda: None)
    monkeypatch.setattr(vectors, "_encoder",
                        lambda: (encoder or (lambda texts: [[0.1]
                                                            for _ in list(texts)])))
    monkeypatch.setattr(vectors, "_client", lambda project=None: client)
    vectors.VectorStoreManager.reset_instances()
    return client


# --- the CPU thread lock (MEB VEC-01) ----------------------------------------

def test_thread_env_pins_the_six_variables_the_numeric_stack_reads():
    """One over-eager BLAS turns a background embed into a machine-wide
    stall: every one of these defaults to "all cores" and they compound."""
    assert vectors.THREAD_ENV == {
        "OMP_NUM_THREADS": "2",
        "MKL_NUM_THREADS": "2",
        "OPENBLAS_NUM_THREADS": "2",
        "VECLIB_MAXIMUM_THREADS": "2",
        "NUMEXPR_NUM_THREADS": "2",
        "TOKENIZERS_PARALLELISM": "false",
    }


def test_apply_thread_env_fills_only_what_is_missing():
    env = {"OMP_NUM_THREADS": "7"}
    out = vectors.apply_thread_env(env)
    assert out is env, "it configures the mapping it was given, not a copy"
    assert env["OMP_NUM_THREADS"] == "7", "an operator's own value was overwritten"
    assert env["MKL_NUM_THREADS"] == "2"
    assert env["TOKENIZERS_PARALLELISM"] == "false"


def test_apply_thread_env_defaults_to_the_real_environment(monkeypatch):
    monkeypatch.delenv("NUMEXPR_NUM_THREADS", raising=False)
    import os
    vectors.apply_thread_env()
    assert os.environ["NUMEXPR_NUM_THREADS"] == "2"


def test_importing_vectors_applies_the_lock_before_anything_numeric():
    """The lock has to be in place at IMPORT time: OpenMP reads
    `OMP_NUM_THREADS` when its runtime loads, and a value set afterwards is
    a value nothing ever reads. Proven in a child, because this process
    imported `vectors` long ago."""
    import os
    env = dict(os.environ)
    env["PYTHONPATH"] = str(paths.__file__.rsplit("mindhive", 1)[0].rstrip("\\/"))
    env["OMP_NUM_THREADS"] = "7"
    env.pop("TOKENIZERS_PARALLELISM", None)
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run(
        [sys.executable, "-c",
         "import os, mindhive.vectors; "
         "print(os.environ['OMP_NUM_THREADS'], "
         "os.environ['TOKENIZERS_PARALLELISM'], "
         "os.environ['MKL_NUM_THREADS'])"],
        capture_output=True, text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["7", "false", "2"], out.stdout


def test_the_caps_this_process_inserted_are_not_handed_to_a_child(monkeypatch):
    """The cap is for THIS process's numeric stack. `os.environ` is what every
    child inherits, so the six defaults `apply_thread_env` inserted rode along
    into every `claude`, `pytest` and `python -m mindhive` this process
    spawned -- a two-thread BLAS cap nothing there asked for. What it
    inserts is registered as process-only (`paths.PROCESS_ONLY_ENV`) and
    `paths.no_bytecode_env`, the one builder of child environments, strips
    it while it still holds the inserted value. A value the operator set --
    before the insert, or over it afterwards -- is not ours to strip: it
    propagates the way it would without mindhive."""
    import os
    monkeypatch.delenv("OMP_NUM_THREADS", raising=False)
    monkeypatch.setenv("MKL_NUM_THREADS", "7")               # over the import-time insert
    monkeypatch.delenv("OPENBLAS_NUM_THREADS", raising=False)
    vectors.apply_thread_env()
    monkeypatch.setenv("OPENBLAS_NUM_THREADS", "5")          # over this call's insert
    assert os.environ["OMP_NUM_THREADS"] == "2"               # this process keeps its cap
    child = paths.no_bytecode_env()
    assert "OMP_NUM_THREADS" not in child
    assert child["MKL_NUM_THREADS"] == "7"
    assert child["OPENBLAS_NUM_THREADS"] == "5"
    assert child["PYTHONDONTWRITEBYTECODE"] == "1"             # the builder's own job, still done
    # A caller-built mapping goes through the same strip.
    assert "OMP_NUM_THREADS" not in paths.no_bytecode_env(dict(os.environ))
    # A mapping handed in explicitly is configured, not registered: the
    # registry describes the real environment only.
    other = {}
    vectors.apply_thread_env(other)
    assert other["OMP_NUM_THREADS"] == "2"


def test_a_child_that_imports_vectors_does_not_pass_the_cap_on():
    """End to end, in a child: import `vectors`, build a child environment the
    way every spawner here does, and read what a grandchild sees. The child
    itself is capped; the grandchild is not."""
    import os
    env = dict(os.environ)
    for name in vectors.THREAD_ENV:
        env.pop(name, None)
    env["PYTHONPATH"] = str(paths.__file__.rsplit("mindhive", 1)[0].rstrip("\\/"))
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    code = (
        "import os, subprocess, sys, mindhive.vectors\n"
        "from mindhive import paths\n"
        "grand = subprocess.run([sys.executable, '-c', "
        "\"import os; print(os.environ.get('OMP_NUM_THREADS'), "
        "os.environ.get('TOKENIZERS_PARALLELISM'))\"], "
        "env=paths.no_bytecode_env(), capture_output=True, text=True)\n"
        "print(os.environ['OMP_NUM_THREADS'], grand.stdout.strip())")
    out = subprocess.run([sys.executable, "-c", code], capture_output=True,
                         text=True, env=env, timeout=120)
    assert out.returncode == 0, out.stderr
    assert out.stdout.split() == ["2", "None", "None"], out.stdout


# --- HNSW tuning (K20, MEB section 8) ----------------------------------------

def test_hnsw_metadata_carries_every_tuning_key_the_blueprint_names():
    assert vectors.HNSW_METADATA == {"hnsw:space": "cosine", "hnsw:M": 16,
                                     "hnsw:construction_ef": 100,
                                     "hnsw:search_ef": 32}


def test_a_new_collection_is_asked_for_the_whole_tuning_set(monkeypatch):
    client = _stub(monkeypatch)
    vectors._open(None, "events")
    assert client.created == [("events", dict(vectors.HNSW_METADATA))]
    assert client.deleted == []


@pytest.mark.parametrize("drifted", [
    {"hnsw:space": "l2", "hnsw:M": 16, "hnsw:construction_ef": 100,
     "hnsw:search_ef": 32},
    {"hnsw:space": "cosine", "hnsw:M": 8, "hnsw:construction_ef": 100,
     "hnsw:search_ef": 32},
    {"hnsw:space": "cosine", "hnsw:M": 16, "hnsw:construction_ef": 16,
     "hnsw:search_ef": 32},
    {"hnsw:space": "cosine", "hnsw:M": 16, "hnsw:construction_ef": 100,
     "hnsw:search_ef": 10},
    {"hnsw:space": "cosine"},
])
def test_any_drifted_tuning_key_rebuilds_a_refillable_collection(monkeypatch,
                                                                 drifted):
    """chroma ignores the metadata argument for a collection that exists, so
    a collection built under the old one-key form keeps M=whatever-the-
    default-was forever. Watching `hnsw:space` alone would have caught the
    first row of this table and silently accepted the other four."""
    client = _stub(monkeypatch, _Client(existing=drifted))
    vectors._open(None, "events", refill=True)
    assert client.deleted == ["events"]


def test_a_matching_collection_is_left_alone(monkeypatch):
    client = _stub(monkeypatch, _Client(existing=dict(vectors.HNSW_METADATA)))
    vectors._open(None, "events", refill=True)
    assert client.deleted == []


def test_a_query_path_never_destroys_the_index(monkeypatch):
    """Wrong scores beat no results: only a caller about to refill may drop."""
    client = _stub(monkeypatch, _Client(existing={"hnsw:space": "l2"}))
    vectors._open(None, "events", refill=False)
    assert client.deleted == []


def test_rebuild_drops_a_collection_that_needs_no_repair(monkeypatch):
    """`--rebuild` is the operator saying "start over" -- it must fire even
    when the metadata already agrees, which is exactly when the drift check
    would not."""
    client = _stub(monkeypatch, _Client(existing=dict(vectors.HNSW_METADATA)))
    vectors._open(None, "events", refill=True, rebuild=True)
    assert client.deleted == ["events"]


# --- collection naming (PDF 8.2) ---------------------------------------------

def test_a_slug_becomes_a_legal_prefixed_collection_name():
    assert vectors.collection_name("My Slug!") == "mindhive_my_slug"


def test_a_collection_name_is_capped_and_still_legal():
    name = vectors.collection_name("x" * 300)
    assert len(name) <= vectors.MAX_COLLECTION_CHARS
    assert name.startswith(vectors.COLLECTION_PREFIX)


@pytest.mark.parametrize("slug", ["My Slug!", "---", "a" * 300, "  ", "7",
                                  "proyek/rahasia", "..", "z" * 44])
def test_every_collection_name_satisfies_chroma(slug):
    """chroma refuses a name that does not start AND end alphanumeric, is
    shorter than 3, or longer than 63. The PDF's own expression
    (`re.sub(r'[^a-zA-Z0-9_-]', '_', slug)[:45]`) leaves `My Slug!` as
    `mindhive_My_Slug_` -- a trailing underscore, which chroma rejects, so
    the name is normalised here rather than copied."""
    import re
    name = vectors.collection_name(slug)
    assert re.fullmatch(r"[a-z0-9][a-z0-9._-]*[a-z0-9]", name), name
    assert 3 <= len(name) <= 63, name


def test_two_different_slugs_do_not_collide_after_truncation():
    a = vectors.collection_name("alpha-" + "x" * 80)
    b = vectors.collection_name("beta-" + "x" * 80)
    assert a != b


# --- the chroma lock (VEC-01) ------------------------------------------------

def test_the_chroma_lock_lives_under_the_project_not_the_store_root():
    with vectors.chroma_lock() as held:
        assert held
        p = paths.project_dir() / vectors.CHROMA_LOCK_NAME
        assert p.exists()
    assert not p.exists(), "the lock outlived its block"


def test_the_chroma_lock_policy_is_the_one_the_plan_fixed():
    pol = vectors.chroma_lock_policy()
    assert (pol.timeout_s, pol.stale_s) == (30.0, 600.0)
    assert pol.fail_open is False


def test_a_second_holder_waits_and_then_gets_in():
    got = []
    started = threading.Event()

    def hold():
        with vectors.chroma_lock():
            started.set()
            time.sleep(0.15)
        got.append("released")

    t = threading.Thread(target=hold)
    t.start()
    assert started.wait(5)
    with vectors.chroma_lock():
        got.append("second")
    t.join(5)
    assert got == ["released", "second"]


def test_a_held_lock_refuses_a_caller_that_will_not_wait():
    with vectors.chroma_lock():
        with pytest.raises(core_locks.LockTimeoutError, match="chroma_lock"):
            with vectors.chroma_lock(timeout_s=0.01):
                pass


def test_the_lock_is_released_even_when_the_block_raises():
    p = paths.project_dir() / vectors.CHROMA_LOCK_NAME
    with pytest.raises(ValueError):
        with vectors.chroma_lock():
            raise ValueError("boom")
    assert not p.exists()


def test_embed_writes_under_the_lock(tmp_path, monkeypatch):
    """Two processes writing one chroma SQLite file is `database is locked`;
    the write has to be inside the lock, not merely near it."""
    _stub(monkeypatch)
    seen = []
    col = _Col()
    monkeypatch.setattr(vectors, "_collection", lambda project=None, **kw: col)
    monkeypatch.setattr(col, "add",
                        lambda **kw: seen.append(
                            (paths.project_dir() / vectors.CHROMA_LOCK_NAME).exists()))
    ldir = paths.project_dir() / "LEDGER"
    ldir.mkdir(parents=True, exist_ok=True)
    (ldir / "a.md").write_text("alpha", encoding="utf-8")
    vectors.embed()
    assert seen == [True], "embed() wrote outside the chroma lock"


def test_embed_events_writes_under_the_lock(monkeypatch):
    col = _Col()
    _stub(monkeypatch)
    monkeypatch.setattr(vectors, "_events_collection",
                        lambda project=None, **kw: col)
    monkeypatch.setattr(events_mod, "read_all",
                        lambda project=None: [events_mod.Event(
                            ts="2026-01-01T00:00:00Z", session_id="a",
                            type="decision", payload={"text": "x"}, tags=[])])
    seen = []
    monkeypatch.setattr(col, "add",
                        lambda **kw: seen.append(
                            (paths.project_dir() / vectors.CHROMA_LOCK_NAME).exists()))
    vectors.embed_events()
    assert seen == [True], "embed_events() wrote outside the chroma lock"


# --- VectorStoreManager ------------------------------------------------------

def test_one_manager_per_project(monkeypatch):
    _stub(monkeypatch)
    assert (vectors.VectorStoreManager.get_instance(None)
            is vectors.VectorStoreManager.get_instance(None))


def test_two_projects_get_two_managers(monkeypatch):
    _stub(monkeypatch)
    a = vectors.VectorStoreManager.get_instance("alpha")
    b = vectors.VectorStoreManager.get_instance("beta")
    assert a is not b


def test_the_manager_opens_its_client_once(monkeypatch):
    calls = []

    class _Counting(_Client):
        def get_or_create_collection(self, name, metadata=None):
            calls.append(name)
            return super().get_or_create_collection(name, metadata)

    _stub(monkeypatch, _Counting())
    mgr = vectors.VectorStoreManager.get_instance(None)
    mgr.client()
    mgr.client()
    assert mgr.client() is mgr.client()


# --- the embedding document (PDF 4.3.2) --------------------------------------

def test_the_document_follows_the_blueprint_template():
    rec = _rec(payload={"summary": "pg over sqlite",
                        "text": "chose postgres for the mirror"})
    assert vectors.build_embedding_document(rec) == (
        "[decision] 2026-01-01T00:00:00Z pg over sqlite "
        "chose postgres for the mirror")


def test_a_document_with_no_summary_has_no_double_space():
    """`" ".join(raw.split())` is what collapses the gap the empty summary
    leaves -- a template that emitted two spaces would embed a different
    string than the same record with a summary."""
    assert vectors.build_embedding_document(_rec()) == (
        "[decision] 2026-01-01T00:00:00Z chose postgres")


def test_a_payload_without_text_is_serialised_rather_than_dropped():
    doc = vectors.build_embedding_document(
        _rec(payload={"key": "next_step", "value": "land it"}))
    assert '"key": "next_step"' in doc and '"value": "land it"' in doc


def test_a_document_is_capped_at_four_thousand_characters():
    assert len(vectors.build_embedding_document(
        _rec(payload={"text": "x" * 9000}))) == 4000


# --- batch embedding (PDF 4.3.3, 8.2) ----------------------------------------

#: The client `vectors._reading` hands a read path in these tests: a stand-in
#: for the one opened on a private copy of the index.
_READ_CLIENT = object()


def _mgr(monkeypatch, col=None, asked=None):
    """A manager whose collection is *col*. *asked* collects the `client`
    each `get_collection` call was given -- `_READ_CLIENT` for a read."""
    from contextlib import contextmanager
    col = col if col is not None else _Col()
    _stub(monkeypatch)
    mgr = vectors.VectorStoreManager.get_instance(None)

    def _get(slug, **kw):
        if asked is not None:
            asked.append(kw.get("client"))
        return col

    @contextmanager
    def _reading(project=None, name=None):
        yield _READ_CLIENT

    monkeypatch.setattr(mgr, "get_collection", _get)
    monkeypatch.setattr(vectors, "_reading", _reading)
    return mgr, col


def test_a_vector_id_is_the_session_and_the_physical_line(monkeypatch):
    mgr, col = _mgr(monkeypatch)
    mgr.embed_batch("proj", [_rec(session_id="alpha", seg_line=7)])
    assert col.batches[0]["ids"] == ["alpha:7"]


def test_every_vector_carries_its_back_pointers(monkeypatch):
    mgr, col = _mgr(monkeypatch)
    mgr.embed_batch("proj", [_rec(session_id="alpha", seg_line=7,
                                  chain_hash="deadbeef")])
    assert col.batches[0]["metadatas"] == [{
        "session_id": "alpha", "seg_line": 7, "chain_hash": "deadbeef",
        "event_type": "decision", "timestamp": "2026-01-01T00:00:00Z",
        "provenance": "events/alpha.jsonl"}]


def test_provenance_names_the_segment_the_row_is_actually_in():
    """The PDF spells provenance `f"events/{session_id}.jsonl"`. For a
    quarantined id that path does not exist and never did -- `../x` lands in
    `unsafe-<sha256[:16]>.jsonl` -- so a back-pointer built from the raw id
    would send a reader to the exact path the quarantine closed."""
    assert vectors.record_provenance(_rec(session_id="../x")).startswith(
        "events/unsafe-")
    # `CON` is a device, and so quarantined, on Windows alone; POSIX keeps it
    # as the ordinary name it is there (`paths.is_reserved_component`).
    con = vectors.record_provenance(_rec(session_id="CON"))
    if os.name == "nt":
        assert con.startswith("events/unsafe-")
    else:
        assert con == "events/CON.jsonl"
    assert vectors.record_provenance(_rec(session_id="alpha")) == (
        "events/alpha.jsonl")


def test_metadata_values_are_all_chroma_scalars(monkeypatch):
    """chroma refuses a list or a dict in metadata; a record whose payload is
    nested must still be indexable."""
    mgr, col = _mgr(monkeypatch)
    mgr.embed_batch("proj", [_rec(payload={"a": {"b": [1, 2]}}, tags=["x"])])
    for v in col.batches[0]["metadatas"][0].values():
        assert isinstance(v, (str, int, float, bool)), v


def test_records_are_added_in_batches_of_thirty_two(monkeypatch):
    mgr, col = _mgr(monkeypatch)
    recs = [_rec(seg_line=i) for i in range(1, 71)]
    assert mgr.embed_batch("proj", recs) == 70
    assert [len(b["ids"]) for b in col.batches] == [32, 32, 6]


def test_the_cpu_is_yielded_between_batches_and_not_after_the_last(monkeypatch):
    """A tight 70-record loop on one core is what makes the dashboard stutter
    while the watcher runs; a sleep AFTER the final batch is latency nobody
    is waiting out."""
    slept = []
    mgr, _col = _mgr(monkeypatch)
    mgr.embed_batch("proj", [_rec(seg_line=i) for i in range(1, 71)],
                    sleep=slept.append)
    assert slept == [vectors.COOPERATIVE_YIELD_SECS] * 2


def test_one_batch_yields_nothing(monkeypatch):
    slept = []
    mgr, _col = _mgr(monkeypatch)
    mgr.embed_batch("proj", [_rec()], sleep=slept.append)
    assert slept == []


def test_progress_is_reported_as_done_over_total(monkeypatch):
    seen = []
    mgr, _col = _mgr(monkeypatch)
    mgr.embed_batch("proj", [_rec(seg_line=i) for i in range(1, 71)],
                    progress_callback=lambda d, t: seen.append((d, t)),
                    sleep=lambda _s: None)
    assert seen == [(32, 70), (64, 70), (70, 70)]


def test_embedding_nothing_is_zero_and_touches_no_collection(monkeypatch):
    mgr, col = _mgr(monkeypatch)
    assert mgr.embed_batch("proj", []) == 0
    assert col.batches == []


def test_a_batch_write_happens_under_the_chroma_lock(monkeypatch):
    seen = []
    col = _Col()
    mgr, _c = _mgr(monkeypatch, col)
    monkeypatch.setattr(col, "add",
                        lambda **kw: seen.append(
                            (paths.project_dir() / vectors.CHROMA_LOCK_NAME).exists()))
    mgr.embed_batch("proj", [_rec()])
    assert seen == [True]


def test_the_constants_are_the_ones_the_blueprint_fixed():
    assert vectors.EMBED_BATCH_SIZE == 32
    assert vectors.COOPERATIVE_YIELD_SECS == 0.050


# --- cooperative encoding ----------------------------------------------------

def test_encode_batch_cooperative_chunks_and_yields():
    seen, slept = [], []
    svc = vectors.LocalEmbeddingService(
        encoder=lambda texts: (seen.append(len(list(texts)))
                               or [[0.0] for _ in texts]),
        sleep=slept.append)
    out = svc.encode_batch_cooperative(["t"] * 70)
    assert len(out) == 70
    assert seen == [32, 32, 6]
    assert slept == [vectors.COOPERATIVE_YIELD_SECS] * 2


def test_encode_batch_cooperative_reports_progress():
    seen = []
    svc = vectors.LocalEmbeddingService(
        encoder=lambda texts: [[0.0] for _ in texts], sleep=lambda _s: None)
    svc.encode_batch_cooperative(["t"] * 40, progress_callback=lambda d, t:
                                 seen.append((d, t)))
    assert seen == [(32, 40), (40, 40)]


def test_encode_batch_cooperative_of_nothing_never_loads_the_model():
    def _boom(_texts):
        raise AssertionError("the model was loaded for an empty batch")

    assert vectors.LocalEmbeddingService(encoder=_boom).encode_batch_cooperative(
        []) == []


def test_the_service_loads_the_real_encoder_lazily(monkeypatch):
    loaded = []
    monkeypatch.setattr(vectors, "_encoder",
                        lambda: (loaded.append(1) or (lambda t: [[0.0] for _ in t])))
    svc = vectors.LocalEmbeddingService()
    assert loaded == [], "the model loaded at construction"
    svc.encode_batch_cooperative(["a"])
    svc.encode_batch_cooperative(["b"])
    assert loaded == [1], "the model was loaded more than once"


# --- semantic_query ----------------------------------------------------------

def test_a_query_score_is_one_minus_distance_to_four_places(monkeypatch):
    col = _Col()
    col.set_result(docs=["[decision] x"], ids=["alpha:7"],
                   distances=[0.256789],
                   metadatas=[{"session_id": "alpha", "seg_line": 7}])
    mgr, _c = _mgr(monkeypatch, col)
    hits = mgr.semantic_query("proj", "why postgres", top_k=5)
    assert hits == [{"id": "alpha:7", "document": "[decision] x",
                     "metadata": {"session_id": "alpha", "seg_line": 7},
                     "score": 0.7432}]
    assert col.queried["n_results"] == 5


def test_a_query_without_distances_reports_no_score(monkeypatch):
    col = _Col()
    col.set_result(docs=["d"], ids=["a:1"], distances=[])
    mgr, _c = _mgr(monkeypatch, col)
    assert mgr.semantic_query("proj", "q")[0]["score"] is None


def test_an_empty_index_answers_with_no_hits(monkeypatch):
    mgr, _col = _mgr(monkeypatch)
    assert mgr.semantic_query("proj", "q") == []


def test_a_manager_query_reads_the_copy_and_a_write_the_live_index(
        monkeypatch):
    """A read is handed the snapshot's client; the watcher's writes keep the
    manager's own live one (no `client` given, so `_open` uses `_client`)."""
    asked = []
    mgr, _col = _mgr(monkeypatch, asked=asked)
    mgr.semantic_query("proj", "q")
    mgr.embed_batch("proj", [_rec()], sleep=lambda _s: None)
    assert asked == [_READ_CLIENT, None]


def test_a_failing_manager_query_is_an_error_not_an_empty_answer(monkeypatch):
    col = _Col()
    boom = RuntimeError("Error loading hnsw index")

    def _raise(**_kw):
        raise boom

    monkeypatch.setattr(col, "query", _raise)
    mgr, _c = _mgr(monkeypatch, col)
    with pytest.raises(vectors.SemanticQueryError) as err:
        mgr.semantic_query("proj", "q")
    assert err.value.stage == "query"
    assert err.value.collection == vectors.collection_name("proj")
    assert err.value.__cause__ is boom
    # `hive embed --rebuild` drops `ledger` and `events`; the watcher's own
    # collection is refilled only by new appends, and the advice says so.
    assert "watcher" in str(err.value)


def test_the_watchers_id_probe_reads_the_copy(monkeypatch):
    """`already_indexed` is a read, so it goes where every read goes: the
    collection is looked up on the snapshot's client, never on the live
    index, where `get_or_create_collection` would mint one."""
    asked = []
    col = _Col()
    col.present = {"alpha:1"}
    mgr, _c = _mgr(monkeypatch, col, asked=asked)
    assert mgr.already_indexed("proj", ["alpha:1", "alpha:2"]) == {"alpha:1"}
    assert asked == [_READ_CLIENT]


def test_a_failed_id_probe_is_empty_and_says_why(monkeypatch, caplog):
    """Empty, because over-embedding is idempotent where refusing is loss --
    but logged as the typed failure, not swallowed."""
    col = _Col()

    def _raise(**_kw):
        raise RuntimeError("Error loading hnsw index")

    monkeypatch.setattr(col, "get", _raise)
    mgr, _c = _mgr(monkeypatch, col)
    with caplog.at_level("WARNING", logger="mindhive.vectors"):
        assert mgr.already_indexed("proj", ["alpha:1"]) == set()
    assert "semantic query failed" in caplog.text and "hnsw" in caplog.text


def test_a_panic_in_the_id_probe_is_empty_and_says_why(monkeypatch, caplog):
    """chromadb's Rust core panics on a malformed page, and pyo3 raises it as
    `pyo3_runtime.PanicException`, a BaseException: it went past the probe's
    `except Exception` and out of `already_indexed`, whose answer to any
    failure is the empty set -- the dashboard's boot recovery included."""
    col = _Col()
    panic = type("PanicException", (BaseException,),
                 {"__module__": "pyo3_runtime"})

    def _raise(**_kw):
        raise panic("Expect it to be fetched")

    monkeypatch.setattr(col, "get", _raise)
    mgr, _c = _mgr(monkeypatch, col)
    with caplog.at_level("WARNING", logger="mindhive.vectors"):
        assert mgr.already_indexed("proj", ["alpha:1"]) == set()
    assert "semantic query failed" in caplog.text
    assert "PanicException" in caplog.text


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_an_interrupt_in_the_id_probe_is_raised_not_taken_for_empty(
        monkeypatch, interrupt):
    """The probe answers any failure with the empty set, which re-embeds
    every record. Wrapped as a failed read, Ctrl-C during the watcher's boot
    recovery was logged as one and the whole claim re-encoded."""
    col = _Col()
    stop = interrupt()

    def _raise(**_kw):
        raise stop

    monkeypatch.setattr(col, "get", _raise)
    mgr, _c = _mgr(monkeypatch, col)
    with pytest.raises(interrupt) as got:
        mgr.already_indexed("proj", ["alpha:1"])
    assert got.value is stop


def test_a_watcher_query_that_answers_nothing_from_its_rows_failed(
        monkeypatch):
    """A collection holding rows that answers a query with none of them --
    measured, a recorded dimension of NULL does this -- has failed. The
    watcher's collection is the delete's to repair: `hive embed --rebuild`
    never writes it."""
    from mindhive import vector_errors
    col = _Col()
    mgr, _c = _mgr(monkeypatch, col)
    assert mgr.semantic_query("proj", "q") == [], "an empty one has not"
    col.present = {"alpha:1", "alpha:2"}
    with pytest.raises(vectors.SemanticQueryError) as err:
        mgr.semantic_query("proj", "q")
    msg = str(err.value)
    assert (err.value.stage, err.value.collection) == (
        "query", vectors.collection_name("proj"))
    assert isinstance(err.value.__cause__, vector_errors.UnansweredQuery)
    assert "--rebuild" not in msg and "watcher's collection" in msg


def test_the_id_probe_asks_in_batches_sqlite_can_bind(monkeypatch):
    """chromadb binds one SQL variable per id, and SQLite refuses a statement
    with too many: a stale claim of 40k rows made one `get` raise
    InternalError 'too many SQL variables' -- the class a damaged index
    raises, so the judge kept a HEALTHY index's client for good and refused
    its reads for 600 s. The probe asks `ID_PROBE_BATCH` ids at a time."""
    col = _Col()
    col.present = {f"alpha:{i}" for i in range(0, 1000, 2)}
    sizes, real_get = [], col.get

    def _get(ids=None):
        sizes.append(len(ids))
        return real_get(ids=ids)

    monkeypatch.setattr(col, "get", _get)
    mgr, _c = _mgr(monkeypatch, col)
    ids = [f"alpha:{i}" for i in range(1000)]
    assert mgr.already_indexed("proj", ids) == col.present
    assert sum(sizes) == 1000 and max(sizes) == vectors.ID_PROBE_BATCH
    assert 1 < vectors.ID_PROBE_BATCH <= 999, "SQLite's oldest variable limit"


def test_a_repeated_id_is_probed_once(monkeypatch):
    """A claim recovered twice queues every row twice, and chromadb refuses a
    `get` naming an id twice -- `DuplicateIDError`, in Python, before the
    index is read -- which the judge took for damage: a HEALTHY index's
    client kept for good and its reads refused. Each id is asked once."""
    col = _Col()
    col.present = {"s1:4"}
    asked, real_get = [], col.get

    def _get(ids=None):
        assert len(set(ids)) == len(ids), f"an id asked twice: {ids}"
        asked.extend(ids)
        return real_get(ids=ids)

    monkeypatch.setattr(col, "get", _get)
    mgr, _c = _mgr(monkeypatch, col)
    assert mgr.already_indexed(
        "proj", ["s1:4", "s1:5", "s1:4", "s1:5"]) == {"s1:4"}
    assert asked == ["s1:4", "s1:5"]


def test_a_manager_query_for_no_hits_asks_the_index_nothing(monkeypatch):
    """A top_k below 1 is `[]`, as `PgVectorIndex.query` answers it -- not a
    chromadb refusal reported as a broken index."""
    col = _Col()

    def _raise(**_kw):                                   # pragma: no cover
        raise AssertionError("a top_k of 0 reached the index")

    monkeypatch.setattr(col, "query", _raise)
    mgr, _c = _mgr(monkeypatch, col)
    assert mgr.semantic_query("proj", "q", top_k=0) == []


def test_a_read_only_open_looks_up_and_never_creates(monkeypatch):
    """`_open(..., client=...)` is the read form: a collection the copy does
    not hold is None, and nothing is created, dropped or re-tuned."""
    import types

    class _Copy:
        def __init__(self):
            self.touched = []

        def list_collections(self):
            return [types.SimpleNamespace(name="ledger")]

        def get_collection(self, name):
            self.touched.append(name)
            return _Col({"hnsw:space": "l2"})

        def get_or_create_collection(self, *a, **k):     # pragma: no cover
            raise AssertionError("a read created a collection")

        def delete_collection(self, *a, **k):            # pragma: no cover
            raise AssertionError("a read dropped a collection")

    copy = _Copy()
    assert vectors._open(None, "events", client=copy) is None
    got = vectors._open(None, "ledger", refill=True, client=copy)
    assert got.metadata == {"hnsw:space": "l2"}, "a read re-tuned the index"
    assert copy.touched == ["ledger"]


# --- `hive embed --rebuild` --------------------------------------------------

def _embed_args(force=False, rebuild=False, project=None):
    import argparse
    return argparse.Namespace(force=force, rebuild=rebuild, project=project)


def test_hive_embed_rebuild_drops_both_collections(monkeypatch, capsys):
    from mindhive.commands import maintenance as maint
    client = _stub(monkeypatch)
    monkeypatch.setattr(events_mod, "read_all", lambda project=None: [])
    ldir = paths.project_dir() / "LEDGER"
    ldir.mkdir(parents=True, exist_ok=True)
    (ldir / "a.md").write_text("alpha", encoding="utf-8")
    assert maint.cmd_embed(_embed_args(rebuild=True)) is None
    assert client.deleted == ["ledger"], client.deleted
    out = json.loads(capsys.readouterr().out)
    assert out["files"]["embedded"] == 1


def test_hive_embed_without_rebuild_drops_nothing_that_matches(monkeypatch,
                                                               capsys):
    from mindhive.commands import maintenance as maint
    client = _stub(monkeypatch)
    monkeypatch.setattr(events_mod, "read_all", lambda project=None: [])
    ldir = paths.project_dir() / "LEDGER"
    ldir.mkdir(parents=True, exist_ok=True)
    (ldir / "a.md").write_text("alpha", encoding="utf-8")
    maint.cmd_embed(_embed_args())
    capsys.readouterr()
    assert client.deleted == []


def test_force_and_rebuild_compose(monkeypatch, capsys):
    """`--force` is the LIVE_LOCK override and `--rebuild` is the index
    decision; neither may quietly disable the other."""
    from mindhive.commands import maintenance as maint
    client = _stub(monkeypatch)
    paths.store_home().mkdir(parents=True, exist_ok=True)
    (paths.store_home() / "LIVE_LOCK").write_text("x", encoding="utf-8")
    monkeypatch.setattr(events_mod, "read_all", lambda project=None: [])
    ldir = paths.project_dir() / "LEDGER"
    ldir.mkdir(parents=True, exist_ok=True)
    (ldir / "a.md").write_text("alpha", encoding="utf-8")
    assert maint.cmd_embed(_embed_args(force=True, rebuild=True)) is None
    capsys.readouterr()
    assert client.deleted == ["ledger"]


def test_hive_embed_under_live_lock_is_a_refusal_with_exit_two(monkeypatch,
                                                                capsys):
    """`hive recall --semantic` answers LIVE_LOCK with one line and exit 2;
    `hive embed` let the RuntimeError out as a traceback."""
    from mindhive.commands import maintenance as maint
    _stub(monkeypatch)
    paths.store_home().mkdir(parents=True, exist_ok=True)
    (paths.store_home() / "LIVE_LOCK").write_text("x", encoding="utf-8")
    assert maint.cmd_embed(_embed_args()) == 2
    captured = capsys.readouterr()
    assert captured.err.startswith("error: ") and "LIVE_LOCK" in captured.err
    assert captured.out == ""


def test_hive_embed_without_the_extras_is_a_refusal_with_exit_two(
        monkeypatch, capsys):
    from mindhive.commands import maintenance as maint

    def _missing():
        raise RuntimeError("semantic layer needs extras: pip install -e .[vectors]")

    monkeypatch.setattr(vectors, "_require_deps", _missing)
    assert maint.cmd_embed(_embed_args()) == 2
    assert "needs extras" in capsys.readouterr().err


def test_the_embed_parser_offers_rebuild():
    from mindhive.commands.parsers import _build_parser
    args = _build_parser().parse_args(["embed", "--rebuild"])
    assert args.rebuild is True and args.force is False
    assert args.project is None


def test_hive_embed_project_embeds_exactly_that_namespace(monkeypatch, capsys):
    """The repair a failed semantic read prints is `hive embed --project
    <ns>`: both collections are built for that namespace, whatever the
    working directory or MINDHIVE_PROJECT would select."""
    from mindhive.commands import maintenance as maint
    seen = []
    monkeypatch.setattr(vectors, "_require_deps", lambda: None)
    monkeypatch.setattr(vectors, "embed", lambda project=None, force=False,
                        rebuild=False: seen.append(("files", project, rebuild))
                        or {"embedded": 0})
    monkeypatch.setattr(vectors, "embed_events", lambda project=None,
                        force=False, rebuild=False: seen.append(
                            ("events", project, rebuild)) or {"embedded": 0})
    assert maint.cmd_embed(_embed_args(rebuild=True, project="my_proj")) is None
    capsys.readouterr()
    assert seen == [("files", "my_proj", True), ("events", "my_proj", True)]


def test_the_embed_parser_refuses_a_namespace_no_directory_can_be(capsys):
    from mindhive.commands.parsers import _build_parser
    with pytest.raises(SystemExit) as exc:
        _build_parser().parse_args(["embed", "--project", "../escape"])
    assert exc.value.code == 2 and "invalid project" in capsys.readouterr().err


def _embed_failing(monkeypatch, error, at="events"):
    """`hive embed` whose *at* pass raises *error*; the passes it asked."""
    asked = []

    def _pass(which):
        def run(project=None, force=False, rebuild=False):
            asked.append(which)
            if which == at:
                raise error
            return {"embedded": 1}
        return run

    monkeypatch.setattr(vectors, "_require_deps", lambda: None)
    monkeypatch.setattr(vectors, "embed", _pass("files"))
    monkeypatch.setattr(vectors, "embed_events", _pass("events"))
    return asked


@pytest.mark.parametrize("rebuild", [True, False])
def test_an_embed_chromadb_fails_is_one_error_line_and_the_repair(
        monkeypatch, capsys, rebuild):
    """Measured on an `events` collection whose configuration chromadb could
    not deserialize: `hive embed --rebuild` rebuilt `ledger`, then raised
    InternalError out of `hive` as a traceback. It is one `error:` line and
    exit 1, saying what was written before the failure and naming the repair:
    the rebuild when the embed was not one, the delete when it was."""
    errors = pytest.importorskip("chromadb.errors")
    from mindhive.commands import maintenance as maint
    cause = errors.InternalError(
        "Could not fetch collections: Could not deserialize configuration")
    asked = _embed_failing(monkeypatch, cause)
    assert maint.cmd_embed(_embed_args(rebuild=rebuild,
                                       project="my_proj")) == 1
    captured = capsys.readouterr()
    err = captured.err
    assert captured.out == "" and asked == ["files", "events"]
    assert err.startswith("error: ") and err.count("\n") == 1, err
    assert "'ledger'" in err and "'events'" in err and "InternalError" in err
    assert "Could not deserialize configuration" in err
    assert f"delete {paths.project_dir('my_proj') / 'chroma'}" in err
    assert "hive embed --project my_proj" in err
    assert ("hive embed --rebuild --project my_proj" in err) is not rebuild


def test_an_embed_behind_another_writer_says_to_retry(monkeypatch, capsys):
    from mindhive.commands import maintenance as maint
    _embed_failing(monkeypatch, core_locks.LockTimeoutError(
        "chroma_lock: held for 30.0s"), at="files")
    assert maint.cmd_embed(_embed_args()) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "retry" in err
    assert "delete" not in err and "--rebuild" not in err
    assert "'ledger'" in err


def test_an_embed_that_failed_elsewhere_names_no_repair(monkeypatch, capsys):
    """A model that is not in the local cache is no index to rebuild."""
    from mindhive.commands import maintenance as maint
    _embed_failing(monkeypatch, OSError(
        "all-MiniLM-L6-v2 is not in the local cache"), at="files")
    assert maint.cmd_embed(_embed_args(rebuild=True)) == 1
    err = capsys.readouterr().err
    assert err.startswith("error: ") and "OSError" in err
    assert "delete" not in err and "--rebuild" not in err


@pytest.mark.parametrize("interrupt", [KeyboardInterrupt, SystemExit])
def test_an_interrupted_embed_is_raised(monkeypatch, capsys, interrupt):
    from mindhive.commands import maintenance as maint
    stop = interrupt()
    _embed_failing(monkeypatch, stop)
    with pytest.raises(interrupt) as got:
        maint.cmd_embed(_embed_args())
    assert got.value is stop and capsys.readouterr().err == ""
