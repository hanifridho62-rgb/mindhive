# src/mindhive/graph.py
from __future__ import annotations
import hashlib
from collections import defaultdict
from . import events as ev_mod, paths


SEMANTIC_TYPES = ev_mod.SEMANTIC_TYPES

# shares_tag guards against a quadratic edge explosion. A tag shared by many
# events — a mega-hub like "auto"/"harvest" that tags nearly everything — would
# emit len(bucket)*(len(bucket)-1)/2 edges (hundreds of thousands on a large
# namespace) that carry NO discriminating signal (co-membership in a ubiquitous
# tag says nothing) and exert ZERO layout force in the renderer. Those edges
# alone ballooned graph.json to ~60 MB and crashed the browser tab (one SVG
# element per edge). Skip pairing inside any bucket past this size; the events
# stay connected through the shared tag node either way.
SHARES_TAG_MAX_BUCKET = 24

# Algorithm/schema version stamped into every persisted graph.json. Bump this
# whenever the graph-building logic changes in a way that should invalidate old
# caches (the shares_tag cap was such a change). load_or_build treats any cache
# whose "version" != this — including a pre-cap cache with no version key — as
# stale and rebuilds it. Without this, a dormant project whose events never
# change would keep serving a graph built by the OLD algorithm forever, because
# the mtime freshness check alone can't see that the code changed.
GRAPH_BUILD_VERSION = 2


def _ev_id(ev) -> str:
    # Include a short hash of the payload so two DISTINCT events that share the
    # same timestamp and type get distinct node ids instead of silently
    # overwriting each other (add_node dedupes on id). Two truly identical
    # events still map to one node, which is the intended behaviour.
    body = ev_mod.canonical_json(ev.payload) if ev.payload else ""
    h = hashlib.sha256(body.encode("utf-8")).hexdigest()[:8]
    return f"ev:{ev.ts}:{ev.type}:{h}"


def _session_id(ev) -> str:
    return f"session:{ev.session_id}"


def _file_id(path: str) -> str:
    return f"file:{path}"


def _tag_id(tag: str) -> str:
    return f"tag:{tag}"


def _file_path_from_payload(payload) -> str | None:
    if isinstance(payload, dict):
        return payload.get("path") or payload.get("text") or None
    return None


def _expand_plugin_catalog(snap, add_node, add_edge) -> None:
    """Expand a plugin_catalog snapshot payload into graph nodes/edges.

    Only free plugins are rendered. Each plugin links to its skills (provides),
    to the topics it covers (covers), and the recommendation node links to the
    top-5 plugins (recommends). Pure and deterministic given the snapshot.
    """
    if not isinstance(snap, dict):
        return

    rec_id = f"recommendation:{snap.get('scanned_at', '')}"
    recs = snap.get("recommendations") or []
    if recs:
        add_node(rec_id, "recommendation",
                 f"top {len(recs)} @ {snap.get('scanned_at', '')}")

    rec_names = {r.get("name") for r in recs}

    for p in snap.get("free_plugins") or []:
        pid = f"plugin:{p.get('marketplace')}/{p.get('name')}"
        add_node(pid, "plugin", p.get("name", "?"),
                 version=p.get("version"),
                 breadth=(p.get("score") or {}).get("breadth_score"))

        for sk in p.get("skills") or []:
            skid = f"skill:{p.get('name')}/{sk.get('name')}"
            add_node(skid, "skill", sk.get("name", "?"), text=sk.get("trigger"))
            add_edge(pid, skid, "provides")

        for topic in p.get("topics") or []:
            tid = f"topic:{topic}"
            add_node(tid, "topic", topic)
            add_edge(pid, tid, "covers")

        if recs and p.get("name") in rec_names:
            add_edge(rec_id, pid, "recommends")


def build(project=None, semantic: bool = False, _evs=None) -> dict:
    """Build and persist knowledge graph as graph.json; return the dict."""
    evs = _evs if _evs is not None else ev_mod.read_all(project)

    nodes: dict[str, dict] = {}
    edges: list[dict] = []

    def add_node(nid: str, ntype: str, label: str, **extra):
        if nid not in nodes:
            node = {"id": nid, "type": ntype, "label": label}
            # Only attach extra fields that carry a value (keeps nodes lean).
            node.update({k: v for k, v in extra.items() if v})
            nodes[nid] = node

    def add_edge(src: str, dst: str, rel: str):
        edges.append({"src": src, "dst": dst, "rel": rel})

    # Build nodes and deterministic edges
    for ev in evs:
        # plugin_catalog snapshots are expanded separately into plugin/skill
        # nodes below; user_prompt / assistant_response are the raw conversation
        # log (Memory view only) and would explode the graph. A generic event
        # node for any of these is just noise.
        if ev.type in ("plugin_catalog", "user_prompt", "assistant_response"):
            continue
        eid = _ev_id(ev)
        sid = _session_id(ev)
        # kind = event subtype (decision/caveat/...); text = payload prose, if any.
        # These enrich the node so the graph view can label and color by meaning.
        text = ev.payload.get("text") if isinstance(ev.payload, dict) else None
        add_node(eid, "event", f"{ev.type} @ {ev.ts}", kind=ev.type, text=text)
        add_node(sid, "session", ev.session_id)

        # every event -> session
        add_edge(eid, sid, "in_session")

        # file_touched -> file node
        if ev.type == "file_touched":
            fp = _file_path_from_payload(ev.payload)
            if fp:
                fid = _file_id(fp)
                add_node(fid, "file", fp)
                add_edge(eid, fid, "touched")

        # tags
        for tag in (ev.tags or []):
            tid = _tag_id(tag)
            add_node(tid, "tag", tag)
            add_edge(eid, tid, "tagged")

    # Temporal chain: consecutive semantic events -> next
    sem_evs = [ev for ev in evs if ev.type in SEMANTIC_TYPES]
    for i in range(len(sem_evs) - 1):
        add_edge(_ev_id(sem_evs[i]), _ev_id(sem_evs[i + 1]), "next")

    # shares_tag: pairs of semantic events sharing >=1 tag, once per pair.
    # Build an inverted tag -> [event indices] map and only pair within each
    # tag bucket. This replaces the O(n^2)-over-all-events scan with work
    # proportional to the actual co-tagging, which is typically far smaller.
    tag_to_idx: dict[str, list[int]] = defaultdict(list)
    for idx, ev in enumerate(sem_evs):
        for tag in (ev.tags or []):
            tag_to_idx[tag].append(idx)  # appended in ascending idx order
    seen_tag_pairs: set[tuple[int, int]] = set()
    for idxs in tag_to_idx.values():
        if len(idxs) > SHARES_TAG_MAX_BUCKET:
            continue  # mega-hub tag: skip the O(n^2) pairing (see constant above)
        for x in range(len(idxs)):
            for y in range(x + 1, len(idxs)):
                i, j = idxs[x], idxs[y]  # i < j (idxs ascending)
                if (i, j) in seen_tag_pairs:  # dedupe pairs sharing multiple tags
                    continue
                seen_tag_pairs.add((i, j))
                add_edge(_ev_id(sem_evs[i]), _ev_id(sem_evs[j]), "shares_tag")

    # Expand the latest plugin_catalog snapshot (a sidecar file, not a ledger
    # event) into plugin/skill/topic nodes.
    from . import plugins
    _expand_plugin_catalog(plugins.latest_catalog(project), add_node, add_edge)

    # Optional semantic edges (graceful degradation if deps/embeddings absent)
    if semantic:
        try:
            from . import vectors
            from .projector import build_ledger_entries

            ledger = build_ledger_entries(evs)  # [(fname, content, ev), ...]
            fname_to_evid = {fn: _ev_id(ev) for fn, _c, ev in ledger}
            seen_sem_pairs: set[tuple[str, str]] = set()
            for fn, _c, ev in ledger:
                src_id = _ev_id(ev)
                body = ev.payload.get("text", "") if isinstance(ev.payload, dict) else str(ev.payload)
                if not body:
                    continue
                for hit in vectors.semantic_recall(body, project=project, limit=4):
                    tgt_id = fname_to_evid.get(hit["file"])
                    if not tgt_id or tgt_id == src_id:
                        continue
                    pair = tuple(sorted((src_id, tgt_id)))
                    if pair in seen_sem_pairs:
                        continue
                    seen_sem_pairs.add(pair)
                    add_edge(pair[0], pair[1], "semantic_near")
        except Exception:
            pass  # deps missing / LIVE_LOCK / not embedded -> deterministic edges only

    result = {
        "version": GRAPH_BUILD_VERSION,
        "nodes": sorted(nodes.values(), key=lambda n: n["id"]),
        "edges": edges,
    }

    # Persist
    pdir = paths.project_dir(project)
    pdir.mkdir(parents=True, exist_ok=True)
    from . import events as ev_mod2  # canonical_json lives here
    gfile = pdir / "graph.json"
    tmp = pdir / "graph.json.tmp"
    tmp.write_text(ev_mod2.canonical_json(result), encoding="utf-8")
    tmp.replace(gfile)

    return result


def _events_mtime(project=None) -> float:
    """Newest modification time across the project's event segments (0.0 when
    there are none). A cheap freshness signal — one stat per segment, never a
    read — used to decide whether the persisted graph.json is still current."""
    d = paths.events_dir(project)
    if not d.exists():
        return 0.0
    try:
        return max((seg.stat().st_mtime for seg in d.glob("*.jsonl")), default=0.0)
    except OSError:
        return 0.0


def load_or_build(project=None, *, semantic: bool = False,
                  source_mtime: float | None = None, build_evs=None) -> dict:
    """Read path for graph viewers: return the persisted graph.json when it is
    at least as fresh as the event source, otherwise (re)build it.

    autosync already rebuilds graph.json whenever a project gains events, so a
    warm cache is the common case — serving it costs one stat + one read instead
    of a full rebuild + 60 MB serialize + disk write on every open.

    - ``semantic`` graphs are never cached: the persisted graph is the
      deterministic one autosync maintains (no embedding-derived edges), so a
      semantic view is always built on demand.
    - ``source_mtime`` overrides the freshness signal for callers whose events
      span multiple namespaces (the HIVE MIND aggregate has no single event
      log); otherwise the project's own segments are used.
    - ``build_evs`` is a zero-arg factory for the events list, invoked ONLY on a
      cache miss so a large aggregate read is skipped whenever the cache hits.
    """
    if not semantic:
        import json
        gfile = paths.project_dir(project) / "graph.json"
        if gfile.exists():
            fresh_since = _events_mtime(project) if source_mtime is None else source_mtime
            try:
                if gfile.stat().st_mtime >= fresh_since:
                    cached = json.loads(gfile.read_text(encoding="utf-8"))
                    # Serve only a cache built by the CURRENT algorithm. A missing
                    # or older "version" means the file was built before a
                    # cache-invalidating change (e.g. pre-cap), so rebuild even
                    # though its mtime looks fresh.
                    if cached.get("version") == GRAPH_BUILD_VERSION:
                        return cached
            except (OSError, ValueError):
                pass  # missing/corrupt cache -> fall through and rebuild
    evs = build_evs() if build_evs is not None else None
    return build(project=project, semantic=semantic, _evs=evs)


def related(query: str, project=None, depth: int = 1) -> list[dict]:
    """Load graph.json (build if missing), return neighbor nodes within depth hops."""
    import json
    pdir = paths.project_dir(project)
    gfile = pdir / "graph.json"
    if not gfile.exists():
        build(project=project)

    g = json.loads(gfile.read_text(encoding="utf-8"))
    nodes_by_id: dict[str, dict] = {n["id"]: n for n in g["nodes"]}
    edges = g["edges"]

    q = query.lower()

    # Find seed nodes matching query
    seeds = {nid for nid, n in nodes_by_id.items()
             if q in nid.lower() or q in n["label"].lower()}

    # BFS up to depth hops
    visited: set[str] = set(seeds)
    frontier = set(seeds)
    for _ in range(depth):
        next_frontier: set[str] = set()
        for e in edges:
            if e["src"] in frontier and e["dst"] not in visited:
                next_frontier.add(e["dst"])
            if e["dst"] in frontier and e["src"] not in visited:
                next_frontier.add(e["src"])
        frontier = next_frontier
        visited |= frontier

    # Include all visited nodes (seeds + neighbors) so callers see both the
    # matched node and its connected nodes within depth hops.
    neighbor_ids = visited

    result = []
    for nid in neighbor_ids:
        if nid not in nodes_by_id:
            continue
        n = nodes_by_id[nid]
        # Find which rel connected it
        via = ""
        for e in edges:
            if (e["src"] in seeds and e["dst"] == nid) or \
               (e["dst"] in seeds and e["src"] == nid):
                via = e["rel"]
                break
        item = {"id": n["id"], "type": n["type"], "label": n["label"], "via": via}
        if n.get("text"):
            item["text"] = n["text"]
        result.append(item)

    return sorted(result, key=lambda x: x["id"])
