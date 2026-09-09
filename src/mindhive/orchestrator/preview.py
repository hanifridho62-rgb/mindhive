# src/mindhive/orchestrator/preview.py
"""The dry run's local decomposer, and the row shape every decomposer answers.

Carved out of `lintester.py` unchanged when that module reached the 1000-line
cap `tests/test_file_size_cap.py` enforces; nothing here is new. `lintester`
re-imports every name below, so `lintester._unit` and
`lintester.preview_units` still resolve for whatever reached them that way.

`preview_units` is the one decomposer that needs no agent, and what keeps it
honest is that it splits on nothing but a fact the intent already carries —
see its docstring. `MAX_TASKS` lives here rather than beside the mission that
enforces it because this module cannot import the module that imports it;
`lintester` reads the constant from here and applies it to every other split.
"""
from __future__ import annotations

# Hard ceiling on units in one mission, matching `decompose.MAX_PROPOSED`'s
# spirit. A decomposition that answers fifty subtasks did not decompose, it
# enumerated, and fanning fifty children at one repo is how a machine falls over.
MAX_TASKS = 24


def _unit(item) -> tuple:
    """``(task_id, title, description, files_or_None)`` from a decomposer row.

    A decomposer — injected, or `decompose.run` — answers three fields and lets
    `_files_for` derive the rest from the description. The local preview answers
    four, because its whole point is that each unit owns ONE directory:
    re-deriving a preview unit's files from a description that still carries the
    entire intent would hand every unit every file, and two lanes claiming the
    same file is the exact thing `clusters.shard_plan` exists to prevent."""
    row = tuple(item)
    files = row[3] if len(row) > 3 else None
    return (str(row[0]), str(row[1]), str(row[2]),
            None if files is None else tuple(files))


# The blank line between a preview unit's intent and its scope note. A named
# constant rather than an inline escape: the description is composed inside a
# nested f-string expression, where an escape is both hard to read and easy to
# mangle into a syntax error.
_PARA = """

"""


def _slug(text) -> str:
    """A task-id-safe form of a cluster key. Ids are read by people and used in
    paths, so `src/mindhive/orchestrator` may not travel as itself."""
    out = "".join(c if (c.isalnum() or c == "-") else "-"
                  for c in str(text).lower()).strip("-")
    while "--" in out:
        out = out.replace("--", "-")
    return out or "cluster"


def preview_units(root_id, title, intent, files, root=None) -> list:
    """One unit per directory the *intent*'s own file paths fall in.

    Honest by construction, and that constraint is the whole design. A real run
    asks `decompose.run`, which needs an agent; inventing subtasks locally and
    letting them stand in for that agent's answer would make a dry run show a
    plan the real run would never produce. So this splits on nothing but a fact
    the intent already carries — the repo files it names — grouped by the same
    `clusters.cluster_key` the lane packer packs by. The caller labels the
    result `PREVIEW`, and `Mission.decomposition` carries that label out, so no
    reader downstream can mistake it for what an agent proposed.

    Fewer than two directories is not a split, and the caller falls back to the
    root task alone rather than dressing a single unit up as a decomposition."""
    from pathlib import Path

    from .. import clusters as clusters_mod

    base = Path(root) if root else Path.cwd()
    try:
        anchor = base.resolve()
    except OSError:                                     # pragma: no cover - odd fs
        return []
    groups: dict = {}
    for path in files or ():
        try:
            full = Path(path).resolve()
            rel = full.relative_to(anchor).as_posix()
        except (OSError, ValueError):
            continue
        groups.setdefault(clusters_mod.cluster_key(rel), {})[rel] = full.as_posix()
    if len(groups) < 2:
        return []
    out = []
    for key in sorted(groups):
        members = groups[key]
        # The description shows repo-relative paths because a person reads it;
        # the file tuple carries the resolved ones because `_weigh` opens them.
        out.append((f"{root_id}-{_slug(key)}", f"{title} [{key}]",
                    _PARA.join((intent, f"This unit owns {key} only: "
                                + ", ".join(sorted(members)))),
                    tuple(members[k] for k in sorted(members))))
    return out[:MAX_TASKS]
