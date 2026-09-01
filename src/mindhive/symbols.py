# src/mindhive/symbols.py
"""T7/T8 — zero-token structural extraction: a ranked symbol index of the tree.

The premise, from the request: an agent that must understand relations in a
codebase does not have to READ the codebase. A deterministic local parse maps
imports, classes and functions at a cost of zero LLM tokens, and the model
receives a few hundred tokens of map instead of a few hundred thousand tokens of
source -- asking for a specific function body only once it knows one exists.

Stdlib `ast` only. No tree-sitter, no language server, no network, no runtime
dependency: `pyproject.toml`'s ``dependencies = []`` stays empty.

## What ranking is for, measured rather than assumed

136 modules do not fit a 1,024-token budget, so something chooses which ones
appear. Without a ranking the chooser is `sorted()`, and the answer is
alphabetical: `autoresume.py` lands, `__main__.py` -- 4,213 lines, the centre of
everything -- is cut.

The import graph is already in hand, so ranking costs a function, not a
dependency. Two PageRanks run over it, because one direction is provably not
enough. Measured on this tree BEFORE any of this was written:

    authority (rank flows to the IMPORTED)   __main__ #131 of 136, autoresume #49
    hub       (rank flows to the IMPORTER)   __main__ #0,          autoresume #31

Authority alone answers "what does everything depend on" -- `paths`, `events`,
`pgstore` -- and drops every entry point, because `__main__` imports 78 modules
and is imported by none. Hub alone answers "where does the work start" and drops
the utilities everything is built on. A map for an agent needs both, so the
score is their sum, with the hub half weighted higher: a utility that everything
imports is reachable by reading from a hub, and the reverse is not true.

## The seventh injector, off by default

`extend_hook_context` is the seat feature 2 built for it, so the map arrives
with a cat, a verdict and a cost line. It stays SILENT unless
``MINDHIVE_SYMBOLS`` is set, for the reason this project already applies to the
rotation hook: this one spends real tokens on every single prompt, so
registering it and switching it on are two steps. A silence here is not an
outage -- the grey cat and its reason are the operator's answer, which is the
whole point of the row.
"""
from __future__ import annotations

import ast
import json
import os
import time
from pathlib import Path
from typing import NamedTuple

from . import events, injectors, paths

# The default the aider repo map established and the plan adopted: a few hundred
# to a thousand tokens is the band where a map is cheaper than the reading it
# replaces.
BUDGET_TOKENS = 1024

ENV_SWITCH = "MINDHIVE_SYMBOLS"
ENV_ROOT = "MINDHIVE_SYMBOLS_ROOT"

# Per file, per name-list. One module must not crowd out every other entry:
# `__main__.py` alone declares hundreds of functions, and a map of one file is
# not a map. What gets cut is COUNTED and rendered -- see `_line`.
MAX_NAMES = 12

# The two halves of the score. Not equal, and the asymmetry is the decision:
# the map is a place to start reading. See the module docstring for the
# measurement behind it.
HUB_WEIGHT = 1.0
AUTHORITY_WEIGHT = 0.5

# Directories a source map has no business walking into. `.`-prefixed names
# cover `.git`, `.venv`, `.claude` and friends in one rule.
SKIP_DIRS = {"__pycache__", "node_modules", "dist", "build", "site-packages"}


# --- extraction -------------------------------------------------------------

def _name(node) -> str:
    """A dotted name for the small subset of expressions that appear as bases.

    Anything cleverer (a subscript, a call) renders as its source text via
    `ast.unparse`, which is exact and rarely reached."""
    if isinstance(node, ast.Name):
        return node.id
    if isinstance(node, ast.Attribute):
        return f"{_name(node.value)}.{node.attr}"
    try:
        return ast.unparse(node)
    except Exception:                                   # noqa: BLE001
        return "?"


def _aliases(tree) -> set:
    """Every local name bound by an import — the roots a dependency can hang off."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, (ast.Import, ast.ImportFrom)):
            for a in node.names:
                out.add((a.asname or a.name).split(".")[0])
    return out


def _imports(tree) -> list:
    """Import statements as WRITTEN, one entry per bound name.

    Relative imports keep their leading dots. The map is meant to be read, and
    `.ledger.record` says something `mindhive.ledger.record` does not: that the
    dependency is a sibling. `graph()` resolves them; this does not."""
    out = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            for a in node.names:
                out.add(a.name)
        elif isinstance(node, ast.ImportFrom):
            dots = "." * (node.level or 0)
            base = f"{dots}{node.module}" if node.module else dots
            for a in node.names:
                out.add(f"{base}.{a.name}" if not base.endswith(".")
                        else f"{base}{a.name}")
    return sorted(out)


def _deps(cls, aliases) -> list:
    """Which imported names this CLASS reaches for, e.g. `stripe.API`.

    The request's own example carries this field, and it is the fact that
    answers "what does this talk to" -- which the module's flat import list
    cannot, because it is shared by everything in the file. Scoped to the
    class's own subtree for exactly that reason: walking the module per class
    would list every attribute use in the file under every class in it."""
    out = set()
    for node in ast.walk(cls):
        if (isinstance(node, ast.Attribute)
                and isinstance(node.value, ast.Name)
                and node.value.id in aliases):
            out.add(f"{node.value.id}.{node.attr}")
    return sorted(out)


_DEFS = (ast.FunctionDef, ast.AsyncFunctionDef)


def _ast_extract(path: Path, rel: str):
    """The `.py` extractor. Total: any unreadable or unparseable file is a skip.

    `errors="replace"` rather than a raise, because a file with one bad byte is
    still a module worth mapping, and blanking it would hide it entirely."""
    try:
        src = path.read_text(encoding="utf-8", errors="replace")
        tree = ast.parse(src)
    except (OSError, SyntaxError, ValueError):
        return None
    aliases = _aliases(tree)
    classes = {}
    for node in tree.body:                              # module level ONLY
        if isinstance(node, ast.ClassDef):
            classes[node.name] = {
                "methods": [b.name for b in node.body if isinstance(b, _DEFS)],
                "bases": [_name(b) for b in node.bases],
                "deps": _deps(node, aliases),
            }
    return {
        "file": rel,
        "imports": _imports(tree),
        "classes": classes,
        # `tree.body`, never `ast.walk`: a closure defined inside another
        # function is not part of the module's surface and must not be listed
        # beside the names that are.
        "functions": [n.name for n in tree.body if isinstance(n, _DEFS)],
    }


# T1's seam. A new language is a module and one entry here; tree-sitter, if it
# is ever needed, arrives as an optional extra beside `pg` / `mcp` / `browser`
# and never as a runtime dependency.
#
# Recorded limit: this dashboard's JavaScript lives inside Python string
# literals (`cat_board._JS`, `dashboard_js_*`), so `ast` sees `script()` and not
# the `paintCats` it emits. This seam is where that would be fixed. It is not
# fixed here, and `tests/test_symbols.py` pins the limit so it cannot rot into a
# surprise.
EXTRACTORS = {".py": _ast_extract}


def extract(path, root=None):
    """One file's surface, or `None` if nothing here can read it."""
    path = Path(path)
    fn = EXTRACTORS.get(path.suffix)
    if fn is None:
        return None
    try:
        rel = path.relative_to(Path(root)).as_posix() if root else path.name
    except ValueError:
        rel = path.name
    return fn(path, rel)


def _module_name(rel: str) -> str:
    name = rel[: -len(Path(rel).suffix)].replace("/", ".")
    return name[: -len(".__init__")] if name.endswith(".__init__") else name


def scan(root) -> list:
    """Every extractable file under `root`, with its module name attached.

    Bounded by construction: `SKIP_DIRS` and dot-prefixed directories are never
    descended into, so a repo with a virtualenv or a `node_modules` in it maps
    the source and not the world."""
    root = Path(root)
    out = []
    for path in sorted(root.rglob("*")):
        if not path.is_file() or path.suffix not in EXTRACTORS:
            continue
        rel = path.relative_to(root)
        if any(p in SKIP_DIRS or p.startswith(".") for p in rel.parts[:-1]):
            continue
        got = extract(path, root=root)
        if got is None:
            continue
        got["module"] = _module_name(got["file"])
        got["is_pkg"] = path.name == "__init__.py"
        got["_root"] = root.name
        out.append(got)
    return out


# --- the import graph -------------------------------------------------------

def resolve_imports(entries):
    """`(graph, externals)` — every import placed, in one pass.

    The graph is `{module: {imported module in this tree, ...}}`. The externals
    are `{module: {top-level third-party name, ...}}`.

    Both fall out of the same resolution, and both are needed: the graph is what
    rank runs on, and the externals are what makes a rendered line say `stripe`.
    Third-party names are deliberately NOT graph nodes -- `requests` belongs in
    the map, knowing a module talks to it is the point, but it is not a file
    here, so making it a node would hand rank to something the index can never
    show."""
    nodes = {e["module"] for e in entries}
    pkg_root = entries[0].get("_root") if entries else None
    by_name = {e["module"]: e for e in entries}
    out = {m: set() for m in nodes}

    def resolve(name, owner):
        # Relative: count the dots, walk up from the owner's package.
        if name.startswith("."):
            level = len(name) - len(name.lstrip("."))
            rest = name[level:]
            e = by_name[owner]
            base = owner if e["is_pkg"] else owner.rpartition(".")[0]
            for _ in range(level - 1):
                base = base.rpartition(".")[0]
            cand = f"{base}.{rest}" if base and rest else (base or rest)
        else:
            cand = name
            # An absolute import of this package's own tree: `mindhive.paths`
            # where the scan is rooted AT `mindhive`, so its modules are named
            # without that prefix. Only stripped when the first segment is the
            # root directory's own name, so an unrelated `os.path` cannot be
            # mistaken for a local module.
            if pkg_root and cand.split(".")[0] == pkg_root:
                cand = cand.partition(".")[2]
        # `from pkg import c` arrives here as `pkg.c`, which may be a module or
        # may be a name inside `pkg`. Try the whole thing, then its parent.
        for c in (cand, cand.rpartition(".")[0]):
            if c and c in nodes:
                return c
        return None

    ext = {m: set() for m in nodes}
    for e in entries:
        owner = e["module"]
        for name in e["imports"]:
            target = resolve(name, owner)
            if target and target != owner:
                out[owner].add(target)
            elif not name.startswith("."):
                # Unresolved and absolute: a third-party or stdlib import. The
                # TOP segment is the fact -- `dataclasses.asdict` and
                # `dataclasses.field` are one dependency, not two.
                ext[owner].add(name.split(".")[0])
    return out, ext


def graph(entries) -> dict:
    """Just the intra-tree edges. What `rank` runs on."""
    return resolve_imports(entries)[0]


# --- ranking ----------------------------------------------------------------

def _pagerank(adj, damping=0.85, iters=60) -> dict:
    """Plain PageRank over `{node: {node, ...}}`. ~20 lines, no dependency.

    Dangling nodes redistribute evenly rather than leaking, so the scores stay a
    distribution and two trees of different shapes remain comparable."""
    nodes = sorted(adj)
    n = len(nodes)
    if not n:
        return {}
    out_deg = {k: len(adj[k]) for k in nodes}
    incoming = {k: [] for k in nodes}
    for a in nodes:
        for b in sorted(adj[a]):
            if b in incoming:
                incoming[b].append(a)
    r = {k: 1.0 / n for k in nodes}
    for _ in range(iters):
        dangling = sum(r[k] for k in nodes if out_deg[k] == 0)
        r = {k: (1 - damping) / n
                + damping * (sum(r[a] / out_deg[a] for a in incoming[k])
                             + dangling / n)
             for k in nodes}
    return r


def _normalised(scores) -> dict:
    top = max(scores.values()) if scores else 0.0
    return {k: (v / top if top else 0.0) for k, v in scores.items()}


def rank(g) -> dict:
    """Combined hub+authority PageRank. See the module docstring for the why."""
    if not g:
        return {}
    rev = {k: set() for k in g}
    for a, outs in g.items():
        for b in outs:
            if b in rev:
                rev[b].add(a)
    auth = _normalised(_pagerank(g))
    hub = _normalised(_pagerank(rev))
    return {k: HUB_WEIGHT * hub[k] + AUTHORITY_WEIGHT * auth[k] for k in g}


# --- the index --------------------------------------------------------------

def _cap(names):
    """Return the kept names and how many were dropped. Never silently."""
    return list(names[:MAX_NAMES]), max(0, len(names) - MAX_NAMES)


def _trim(entry, deps=None) -> dict:
    """One file's line-worth of map, capped, with what was cut counted.

    `deps` is the RESOLVED dependency set -- modules, not bound names. Rendering
    `entry["imports"]` directly was the first draft and it was measurably wrong:
    `dashboard.py` spent twelve of its twelve slots on twelve names from ONE
    module (`.dashboard_data.ALL_TOKENS`, `.dashboard_data.HIVEMIND_LABEL`, ...),
    which is a dozen tokens saying what one token says. Collapsing to modules is
    also what the request's own example shows -- `"imports": ["requests",
    "stripe"]` -- and local names here match a `file` line elsewhere in the map,
    so an agent can follow one to the other."""
    fns, dropped = _cap(entry["functions"])
    classes = {}
    for name, body in list(entry["classes"].items())[:MAX_NAMES]:
        methods, d = _cap(body["methods"])
        dropped += d
        classes[name] = {"methods": methods, "bases": body["bases"],
                         "deps": body["deps"][:MAX_NAMES]}
    dropped += max(0, len(entry["classes"]) - MAX_NAMES)
    imports, d = _cap(sorted(deps) if deps is not None else entry["imports"])
    dropped += d
    return {"file": entry["file"], "imports": imports, "classes": classes,
            "functions": fns, "trimmed": dropped}


def _line(f) -> str:
    parts = [f["file"]]
    if f["imports"]:
        parts.append("imp " + ",".join(f["imports"]))
    for name, body in f["classes"].items():
        bases = "(" + ",".join(body["bases"]) + ")" if body["bases"] else ""
        head = f"cls {name}{bases}"
        if body["methods"]:
            head += ":" + ",".join(body["methods"])
        parts.append(head)
    if f["functions"]:
        parts.append("fn " + ",".join(f["functions"]))
    if f["trimmed"]:
        parts.append(f"+{f['trimmed']} more")
    return " | ".join(parts)


def _header(included, total) -> str:
    # The omission count rides the TEXT, not just the dict: an agent reads the
    # map, and a map of 12 modules and a map of 136 cut to 12 are the same
    # bytes unless one of them says so.
    return (f"# symbol index: {included} of {total} modules, "
            f"{total - included} omitted, ranked by import graph")


def render(files, total) -> str:
    return "\n".join([_header(len(files), total)] + [_line(f) for f in files])


def default_root() -> Path:
    return Path(os.environ.get(ENV_ROOT) or Path.cwd())


def index(root=None, budget=BUDGET_TOKENS) -> dict:
    """A ranked, budget-capped symbol index of `root`.

    Greedy in rank order: a file is added only while the WHOLE rendered artifact
    -- header and all -- still fits. The header is always present, so an index
    that could fit nothing still says how much it left out."""
    root = Path(root) if root is not None else default_root()
    t0 = time.perf_counter()
    entries = scan(root)
    g, ext = resolve_imports(entries)
    scores = rank(g)
    ordered = sorted(entries,
                     key=lambda e: (-scores.get(e["module"], 0.0), e["file"]))
    kept = []
    for entry in ordered:
        deps = g.get(entry["module"], set()) | ext.get(entry["module"], set())
        trial = kept + [_trim(entry, deps)]
        if injectors._tokens(len(render(trial, len(entries)))) > budget:
            break
        kept = trial
    text = render(kept, len(entries))
    return {"root": str(root), "modules": len(entries), "included": len(kept),
            "omitted": len(entries) - len(kept), "budget": budget,
            "tokens": injectors._tokens(len(text)), "chars": len(text),
            "ms": int((time.perf_counter() - t0) * 1000),
            "files": kept, "text": text}


# --- one unit's neighbourhood -----------------------------------------------

class Map(NamedTuple):
    """One parse of a tree, kept so several slices can share it.

    `index` scans, resolves and ranks on every call, which is right for the
    per-prompt hook: one caller, one artifact. A mission wants N artifacts from
    the SAME tree — one per unit — and re-parsing 335 modules per unit would
    cost more than the slices are worth. So the expensive half is separated from
    the rendering half, and the caller holds on to it."""

    root: str
    entries: tuple
    graph: dict
    externals: dict
    scores: dict
    importers: dict
    by_file: dict
    by_module: dict

    @property
    def total(self) -> int:
        return len(self.entries)


def build_map(root=None) -> Map:
    """Scan, resolve and rank *root* once — `index`'s expensive half, reusable.

    `importers` is the reverse of `graph` and is built here rather than derived
    per call: a unit's neighbourhood is what it imports AND what imports it, and
    the second half is not reachable from `graph` without a full pass."""
    root = Path(root) if root is not None else default_root()
    # RESOLVED, and the slice depends on it: `focus` is handed the paths
    # `lintester._files_for` already resolved, and `Path.relative_to` between a
    # resolved path and a relative root raises rather than matching — so a map
    # built from a relative root answered an empty slice for every unit, with no
    # error anywhere to say why.
    try:
        root = root.resolve()
    except OSError:                                     # pragma: no cover - odd fs
        pass
    entries = scan(root)
    g, ext = resolve_imports(entries)
    importers: dict = {}
    for owner, targets in g.items():
        for target in targets:
            importers.setdefault(target, set()).add(owner)
    return Map(root=str(root), entries=tuple(entries), graph=g, externals=ext,
               scores=rank(g), importers=importers,
               by_file={e["file"]: e for e in entries},
               by_module={e["module"]: e for e in entries})


def _rel_of(root: Path, path) -> str:
    """*path* as a repo-relative posix path, or ``""`` when it is outside."""
    try:
        return Path(path).resolve().relative_to(root).as_posix()
    except (OSError, ValueError):
        return ""


def owned_modules(smap, files) -> list:
    """The modules of *files* that *smap* actually holds, in rank order.

    A path the map does not hold — a text file, a path outside the root, a
    module the scan skipped — is dropped rather than guessed at."""
    root = Path(smap.root)
    out = []
    for path in files or ():
        rel = _rel_of(root, path)
        entry = smap.by_file.get(rel)
        if entry is not None:
            out.append(entry["module"])
    return sorted(dict.fromkeys(out),
                  key=lambda m: (-smap.scores.get(m, 0.0), m))


def _focus_header(included, owned, total) -> str:
    # Deliberately NOT `_header`'s wording, which ends "ranked by import graph".
    # That is true of the whole-repo index and false here: this slice is chosen
    # by which modules the unit owns, and rank only orders what survived the
    # choice. Borrowing the other header would overstate what the slice is.
    return (f"# symbol index: {included} of {total} modules, scoped to the "
            f"{owned} this unit owns and their direct neighbours")


def _focus_render(files, owned, total) -> str:
    return "\n".join([_focus_header(len(files), owned, total)]
                     + [_line(f) for f in files])


def focus(smap, files, budget=BUDGET_TOKENS) -> str:
    """The slice of *smap* one unit can actually reach.

    Its own modules first, then their direct neighbours in the import graph —
    what they import, and what imports them. Same budget rule as `index`: a
    module is added only while the WHOLE rendered artifact still fits, so the
    header is always present and always says how much was left out.

    Answers ``""`` when the unit owns no module this map holds. That is the
    signal to fall back to the repo-wide slice rather than brief an agent with
    an empty map — a unit that touches no Python is better served by the
    repository's shape than by a header saying it has none."""
    owned = owned_modules(smap, files)
    if not owned:
        return ""
    near: set = set()
    for module in owned:
        near |= set(smap.graph.get(module, ()) or ())
        near |= set(smap.importers.get(module, ()) or ())
    ordered = owned + sorted(near - set(owned),
                             key=lambda m: (-smap.scores.get(m, 0.0), m))
    kept: list = []
    for module in ordered:
        entry = smap.by_module.get(module)
        if entry is None:                               # pragma: no cover
            continue
        deps = (set(smap.graph.get(module, ()) or ())
                | set(smap.externals.get(module, ()) or ()))
        trial = kept + [_trim(entry, deps)]
        if injectors._tokens(len(_focus_render(trial, len(owned),
                                               smap.total))) > budget:
            break
        kept = trial
    if not kept:
        return ""
    return _focus_render(kept, len(owned), smap.total)


# --- the cache, and the seventh injector ------------------------------------

def cache_path(project=None) -> Path:
    return paths.project_dir(project) / "symbols" / "index.json"


def _fingerprint(root: Path, budget: int) -> str:
    """Cheap and exact enough: how many source files there are and the newest
    mtime among them. A rebuild costs a full parse of the tree, so the check
    must not."""
    count, newest = 0, 0
    for path in root.rglob("*"):
        if path.suffix not in EXTRACTORS or not path.is_file():
            continue
        rel = path.relative_to(root)
        if any(p in SKIP_DIRS or p.startswith(".") for p in rel.parts[:-1]):
            continue
        count += 1
        newest = max(newest, path.stat().st_mtime_ns)
    return f"{count}:{newest}:{budget}"


def cached_text(root=None, budget=BUDGET_TOKENS, project=None):
    """`(text, reason)` for the hook path, rebuilding only when the tree moved.

    A full parse of 136 modules is real work and this sits on the per-prompt
    path, so the artifact is cached beside the project's other sidecars and the
    fingerprint check is a stat walk rather than a parse."""
    root = Path(root) if root is not None else default_root()
    fp = _fingerprint(root, budget)
    path = cache_path(project)
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
        if doc.get("fingerprint") == fp and doc.get("text"):
            return doc["text"], f"cached {doc.get('modules', '?')} modules"
    except Exception:                                   # noqa: BLE001
        pass
    idx = index(root, budget=budget)
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        paths.write_text_atomic(path, json.dumps(
            {"fingerprint": fp, "text": idx["text"], "modules": idx["modules"],
             "included": idx["included"], "omitted": idx["omitted"],
             "built": events.now_iso()}, indent=2))
    except Exception:                                   # noqa: BLE001
        pass                                            # a cache miss, not a failure
    return idx["text"], (f"built {idx['included']} of {idx['modules']} modules "
                         f"in {idx['ms']} ms")


def enabled() -> bool:
    return bool(os.environ.get(ENV_SWITCH))


def _is_machine(raw) -> bool:
    """True only when *raw* positively parses to a machine-generated prompt.

    The same gate `premise`/`sopgate`/`style` apply: a task notification, a
    system reminder or an injected skill body is not the user asking for
    anything, so it must not draw the repo map. Deliberately one-directional:
    an unparseable or unexpected *raw* returns False and the pass still speaks
    -- the hook path always hands us the JSON envelope, and the unit tests pass
    a bare token, so `False` there means "proceed", never "break the prompt"."""
    try:
        from . import promptgate
        payload = json.loads(raw or "{}")
        if not isinstance(payload, dict):
            return False
        return promptgate.is_machine_prompt(payload.get("prompt", ""))
    except Exception:                                   # noqa: BLE001
        return False


def extend_hook_context(context, raw):
    """The seventh injector. Additive and fail-open, like the other six.

    The context is a HOOK PAYLOAD -- ``{"hookSpecificOutput":
    {"additionalContext": ...}}`` -- not a bare string, and this function's
    first version treated it as one. Caught by driving the real hook, not by a
    test: the map arrived, but `run_pipeline` then reported all seven passes
    silent, INCLUDING `style`, which is never silent. Returning a `str` where
    the next pass expected a dict meant `style`'s own fail-open swallowed the
    mismatch and dropped its block entirely -- a broken injector and a quiet one
    leaving identical evidence, which is the exact failure `injectors.py` exists
    to end, produced by the newest injector itself.

    Two lessons are baked in here rather than left in a commit message. The
    shape is the house one, copied from `premise.extend_hook_context` so there
    is one construction and not two. And the tests that missed it asserted
    against a bare string -- the same `dict`-where-a-JSON-string-belongs mistake
    recorded in `injectors.py`'s docstring, made a second time in the opposite
    direction."""
    try:
        if not enabled():
            return context
        if _is_machine(raw):
            return context
        text, _reason = cached_text()
        if not text:
            return context
        return injectors.merge_hook_context(context, text)
    except Exception:                                   # noqa: BLE001
        return context


def probe(raw=None):
    """What this pass WOULD deliver, and why, without running the pipeline."""
    if not enabled():
        return injectors.Injection(
            name="symbols", verdict="silent", chars=0, tokens=0, ms=0,
            reason=f"off — set {ENV_SWITCH}=1 to inject the repo map",
            text="")
    t0 = time.perf_counter()
    try:
        text, reason = cached_text()
    except Exception as exc:                            # noqa: BLE001
        return injectors.Injection(
            name="symbols", verdict="silent", chars=0, tokens=0,
            ms=int((time.perf_counter() - t0) * 1000),
            reason=f"failed: {exc}", text="")
    return injectors.measure("symbols", "", text,
                             ms=int((time.perf_counter() - t0) * 1000),
                             reason=reason)


def cli(args) -> int:
    """`hive symbols [PATH] [--budget N] [--json]`.

    Lives here rather than in `__main__` deliberately: that file sits 19 lines
    under its size ratchet, and `tests/test_file_size_cap.py` is explicit that
    the allowlist is a ceiling and not a licence. The CLI surface there stays
    argparse wiring; the behaviour is here, beside the thing it renders."""
    budget = getattr(args, "budget", None) or BUDGET_TOKENS
    idx = index(getattr(args, "path", None), budget=budget)
    if getattr(args, "json", False):
        print(json.dumps(idx, indent=2))
        return 0
    print(idx["text"])
    print("\n%d of %d modules, ~%d of %d tokens, built in %d ms"
          % (idx["included"], idx["modules"], idx["tokens"], budget, idx["ms"]))
    return 0
