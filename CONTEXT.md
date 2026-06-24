# MIND HIVE — Context Glossary

This file is the canonical glossary of domain terms for MIND HIVE. It is **not**
a spec or a scratchpad — only resolved terms and their precise meanings live
here. Implementation decisions belong in `docs/adr/`.

## Terms

### Hive mind
The shared, cross-session memory: the union of every Claude session and project
that reads from and writes to **one** MIND HIVE event store. "The hive mind sees
X" means *any* future session, in any project namespace, can retrieve X. It does
**not** mean multiple independent agents coordinating in real time.

### Memory graph
The existing `graph.json` projection of the event store (built by
`src/mindhive/graph.py`). Nodes: `event`, `session`, `file`, `tag`, `plugin`,
`skill`, `topic`, `recommendation`. This is the **intent layer** — what the hive
decided, learned, and questioned.

### Code graph
*(new — being designed)* A graph of code entities (functions, classes, modules)
and their structural relationships (calls, imports, defines), extracted from the
project's source. Distinct from the memory graph, which is about intent.

### Merge (memory ⨝ code)
The goal of the current work: linking memory-graph nodes to the code-graph
entities they concern, so the hive mind retrieves a decision/caveat *attached to
the specific function or module it was about* — not just "some file was touched."

### Edge provenance (EXTRACTED / INFERRED / AMBIGUOUS)
A tag on every graph edge recording how it was derived. **EXTRACTED** =
deterministic (tree-sitter AST facts; event-derived links) — trustworthy as
ground truth. **INFERRED** = produced by Opus reasoning (conceptual links) —
soft. **AMBIGUOUS** = Opus flagged it uncertain. Retrieval can filter to
EXTRACTED-only for deterministic structural answers. See `docs/adr/0002`.

### Structural spine vs conceptual overlay
The **structural spine** is the EXTRACTED subgraph (AST + event facts) — the
deterministic, verifiable core. The **conceptual overlay** is the INFERRED
edges Opus adds on top. "Trust the spine, weigh the overlay."
