# SPDX-License-Identifier: Apache-2.0
"""What a failed semantic read is, and the repair it names.

A read of the semantic index that FAILED says so (DATA_MODEL.md 4.19). Each
step that can break -- taking the copy, opening it, counting, encoding the
query, querying -- raises `SemanticQueryError` naming the step, instead of the
`[]` or `0` that made a broken index look exactly like one with nothing
relevant in it. The message names the namespace and the repair, which depends
on what broke: a busy writer is waited out, a full temp dir is freed, and an
argument chromadb refused is nobody's index to repair. Damaged files are
rebuilt or deleted by how far a read got: `hive embed --rebuild` opens the
live index the way a read does and then drops the collection without reading
it, so a collection that opened and then failed -- or a cut segment of
`ledger` or `events` -- is rebuilt first, while files chromadb cannot open are
deleted, and so is the watcher's collection, which the rebuild never writes.

`as_failure` is how a read turns what a step raised into that error, and it
takes chromadb's panics with the rest: its Rust core panics on a malformed
SQLite page, and pyo3 raises the panic as `pyo3_runtime.PanicException` -- a
BaseException. Measured on damaged scratch indexes, it went past every
`except Exception` the read path had: a raw traceback from `hive recall
--semantic`, every other namespace's hits lost from a HIVE MIND search, and
the client that panicked left in service. `judge` tells `vector_snapshot`
which failures were the chroma client's own, so that client is never closed.

Split out of `vectors`, which re-exports `SemanticQueryError`: `vectors` is
where a read meets chromadb, this module what the read says when that fails.
"""
from . import paths

__all__ = ["SemanticQueryError", "MissingDocument", "CLIENT_STAGES",
           "as_failure", "judge", "namespace", "refused_arguments", "remedy",
           "text_of"]

#: The stages that ran inside the chroma client.
CLIENT_STAGES = ("open", "count", "query")


class SemanticQueryError(RuntimeError):
    """A semantic read that failed, as opposed to one that found nothing.

    `stage` is the step that broke -- `snapshot`, `open`, `count`, `encode` or
    `query` -- and `collection` the collection being read; the original
    exception is `__cause__`. A `RuntimeError` so that callers which already
    catch the LIVE_LOCK and missing-extras refusals keep catching it."""

    def __init__(self, stage, collection, cause, project=None, note=None):
        self.stage = stage
        self.collection = collection
        ns, where = namespace(project)
        msg = (f"semantic {stage} failed on the {collection!r} index of project "
               f"{ns!r}: {type(cause).__name__}: {cause} "
               f"({remedy(stage, cause, ns, where, collection)})")
        super().__init__(msg + (f" [{note}]" if note else ""))


class MissingDocument(RuntimeError):
    """A hit chromadb returned without its document, the text `hive embed`
    stores with every row of `ledger` and `events`: chromadb answers None
    for a stored document it cannot decode (measured: invalid UTF-8), and a
    row another writer added without text has none either. The client
    answered, so it is sound; the rows are not, and a rebuild writes them
    again."""


def text_of(doc, hit, collection, project=None) -> str:
    """*doc*, the document chromadb returned for *hit*, or a failed `query`
    when it came back without one -- the snippet built from it raised
    AttributeError, past every caller that catches `SemanticQueryError`.
    Asked once the client is handed back, so the copy is never poisoned for
    a failure its client did not have."""
    if isinstance(doc, str):
        return doc
    cause = MissingDocument(f"hit {hit!r} came back without its document")
    raise SemanticQueryError("query", collection, cause, project) from cause


def _panicked(exc) -> bool:
    """Whether *exc* is pyo3's `PanicException`. Every pyo3 extension builds
    its own class, so it is known by its module and name, never imported."""
    return any((c.__module__, c.__name__) == ("pyo3_runtime", "PanicException")
               for c in type(exc).__mro__)


def as_failure(stage, collection, exc, project=None):
    """The `SemanticQueryError` to raise in place of *exc*, which a step of a
    read at *stage* raised -- a panic in chromadb's Rust core included. Any
    other BaseException, KeyboardInterrupt or SystemExit, is no failed read:
    it is raised again, as it is."""
    if not (isinstance(exc, Exception) or _panicked(exc)):
        raise exc
    return SemanticQueryError(stage, collection, exc, project)


def namespace(project):
    """`(namespace, chroma dir)` for an error message: the name of the
    namespace's directory, and what to delete."""
    try:
        pdir = paths.project_dir(project)
        return pdir.name, pdir / "chroma"
    except Exception:                              # noqa: BLE001 -- a message only
        return str(project or "?"), "the project's chroma directory"


def remedy(stage, cause, ns, where, collection=None) -> str:
    """The repair for a read of namespace *ns* -- index directory *where* --
    that failed at *stage* with *cause*."""
    from .vector_snapshot import DamagedSegment
    from .vectors import COLLECTION_PREFIX
    if stage == "snapshot":
        return {"copy": "copying the index into the temp directory failed: "
                        "free space there, or retry if a writer held a file",
                "space": "the temp directory cannot hold a copy of the "
                         "index: it is full or not writable",
                }.get(getattr(cause, "reason", "busy"),
                      "a writer is busy; retry when `hive embed` or the "
                      "watcher has finished")
    if stage == "encode":
        return "the embedding model failed to load or run"
    if refused_arguments(cause, stage):
        return ("chromadb refused the read's arguments before touching the "
                "index; the index is not at fault")
    # `--project`, not MINDHIVE_PROJECT: `hive embed` reads the variable
    # through `paths.normalize`, which turns `my_proj` into `my-proj` -- the
    # advice repaired a different namespace. `--project` takes the name
    # verbatim, except a quarantined `unsafe-<hash>` directory's: it refuses
    # that one (exit 2), as it refuses the name stored there, so that
    # namespace is reached the way its project was selected.
    select, how = f" --project {ns}", ""
    if paths.namespace_name(ns) != ns:
        select, how = "", (f"; `--project` refuses the quarantined {ns}, so "
                           f"run `hive embed` the way this project was "
                           f"selected: from its checkout, or with the same "
                           f"MINDHIVE_PROJECT")
    # A cut segment refuses the whole copy, whichever collection was asked
    # for, so it is repaired as the collection it belongs to.
    owner = (getattr(cause, "collection", None)
             if isinstance(cause, DamagedSegment) else None)
    collection = owner or collection
    if str(collection or "").startswith(COLLECTION_PREFIX):
        # The watcher's collection: `hive embed` never writes it, `--rebuild`
        # included, so the delete is the only repair that reaches it -- and
        # nothing brings back the vectors of rows appended before it.
        return (f"{collection!r} is the watcher's collection, which `hive "
                f"embed` never writes: delete {where} and run `hive "
                f"embed{select}` for the ledger and events collections; the "
                f"watcher's refills only with rows appended after that{how}")
    if stage == "open" and not owner:
        # Files chromadb cannot open: `--rebuild` opens them the same way
        # before it drops anything, and fails as this read did.
        return (f"chromadb cannot open the index files, and a rebuild opens "
                f"them too: delete {where} and run `hive embed{select}`{how}")
    # A collection that opened and then failed -- or a cut segment of
    # `ledger` or `events` -- which `--rebuild` drops without reading it.
    return (f"rebuild it with `hive embed --rebuild{select}`; if the read "
            f"still fails, delete {where} and run `hive embed{select}`{how}")


def refused_arguments(cause, stage) -> bool:
    """Whether *cause* is chromadb refusing a query's ARGUMENTS before it
    touched the index, which says nothing about the index: a bad `n_results`
    (ValueError, TypeError; OverflowError past 2**32 - 1), a `where` its
    validation refuses, an id named twice in one request. Only at `query`:
    `open` and `count` are asked nothing a caller chose. Never an
    InvalidArgumentError or InvalidDimensionException: chromadb 1.5.9 raises
    the first for a log it cannot read as well -- "Failed to pull logs from
    the log store", measured on damaged pages at `count` and at `query` --
    and either for a query embedding whose width is not the collection's, a
    collection another model built, which `hive embed --rebuild` repairs."""
    if stage != "query":
        return False
    if isinstance(cause, (ValueError, TypeError, OverflowError)):
        return True
    try:
        from chromadb.errors import DuplicateIDError
    except ImportError:
        return False
    return isinstance(cause, DuplicateIDError)


def judge(exc) -> bool:
    """Whether a failed read was the chroma client failing on its copy -- the
    client `vector_snapshot` then never closes. Arguments chromadb refused
    never reached the index, and claiming them poisoned a healthy one."""
    return (isinstance(exc, SemanticQueryError)
            and exc.stage in CLIENT_STAGES
            and not refused_arguments(exc.__cause__, exc.stage))
