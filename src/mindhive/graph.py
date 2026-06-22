# src/mindhive/graph.py
from __future__ import annotations
from collections import defaultdict
from . import events as ev_mod, paths


SEMANTIC_TYPES = ev_mod.SEMANTIC_TYPES


def _ev_id(ev) -> str:
    return f"ev:{ev.ts}:{ev.type}"


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


def _expand_plugin_catalog(evs, add_node, add_edge) -> None:
    """Expand the most recent plugin_catalog snapshot into graph nodes/edges.

    Only free plugins are rendered. Each plugin links to its skills (provides),
    to the topics it covers (covers), and the recommendation node links to the
    top-5 plugins (recommends). Pure and deterministic given the snapshot.
    """
    snap = None
    for ev in evs:
        if ev.type == "plugin_catalog" and isinstance(ev.payload, dict):
            snap = ev.payload  # keep the last (latest) one
    if not snap:
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
        # nodes below; a generic event node for them would just be noise.
        if ev.type == "plugin_catalog":
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

    # shares_tag: pairs of semantic events sharing >=1 tag
    # emit once per unordered pair
    for i in range(len(sem_evs)):
        for j in range(i + 1, len(sem_evs)):
            a, b = sem_evs[i], sem_evs[j]
            if set(a.tags or []) & set(b.tags or []):
                add_edge(_ev_id(a), _ev_id(b), "shares_tag")

    # Expand the latest plugin_catalog snapshot into plugin/skill/topic nodes.
    _expand_plugin_catalog(evs, add_node, add_edge)

    # Optional semantic edges (graceful degradation if deps/embeddings absent)
    if semantic:
        try:
            from . import vectors
            from .projector import build_ledger_entries

            ledger = build_ledger_entries(evs)  # [(fname, content, ev), ...]
            fname_to_evid = {fn: _ev_id(ev) for fn, _c, ev in ledger}
            seen_pairs: set[tuple[str, str]] = set()
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
                    if pair in seen_pairs:
                        continue
                    seen_pairs.add(pair)
                    add_edge(pair[0], pair[1], "semantic_near")
        except Exception:
            pass  # deps missing / LIVE_LOCK / not embedded -> deterministic edges only

    result = {
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
