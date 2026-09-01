# src/mindhive/orchestrator/metrics.py
"""Quantitative workload weighting — how heavy is one file, really?

Three terms, fused into one comparable number:

* **complexity** — a real cyclomatic count from the AST. `clusters.file_complexity`
  is a regex-and-indent heuristic whose own docstring calls itself "a cheap
  cyclomatic proxy"; it is kept, it is still right for its caller, and it is not
  what this is. The difference shows on a file that is long and flat versus one
  that is short and branchy: the proxy ranks by size, this ranks by decisions.
* **churn** — lines added and deleted across the file's history, from
  `git log --numstat`. `clusters.git_churn` counts COMMITS, which says how often
  a file was touched and nothing about how much moved. A file with 200 one-line
  fixups and a file rewritten twice score identically there and very differently
  here.
* **usage** — recorded agent turns against the key, from `mindnet.telemetry`.

Every term is TOTAL. A syntax error, a missing file, an absent git, an
unreachable ledger: that term contributes 0 and the others still count. The only
caller is a scheduler deciding how to pack lanes, and a weighting function that
raises takes the whole mission down to improve an estimate.
"""
from __future__ import annotations

import ast
import subprocess
from dataclasses import dataclass
from pathlib import Path

from .. import paths

# Relative importance of the three terms. Deliberately small integers: the score
# only has to ORDER files correctly, and a float here would invite the belief
# that the absolute number means something.
W_COMPLEXITY = 3
W_CHURN = 1
W_USAGE = 2

_CHURN_TIMEOUT_S = 5

# A file this large is not weighted by parsing it — `ast.parse` on a multi-megabyte
# generated module is slow enough to matter when a mission weighs a whole tree.
MAX_PARSE_BYTES = 2_000_000


class ComplexityVisitor(ast.NodeVisitor):
    """Counts decision points: every place control flow can branch.

    `generic_visit` is called from each handler, so the walk is recursive and a
    branch nested ten deep counts the same as one at module level. That is the
    intent — nesting is picked up by the DECISIONS it takes, not by measuring
    indentation, which is what the incumbent proxy does.

    `BoolOp` contributes `len(values) - 1`, because `a and b and c and d` is three
    short-circuit branches in one expression, not one. A comprehension
    contributes one for the loop plus one per `if` clause, for the same reason a
    `for` with an `if` inside it does.
    """

    def __init__(self):
        self.decisions = 0

    def _count(self, node, n: int = 1):
        if n > 0:
            self.decisions += n
        self.generic_visit(node)

    def visit_If(self, node):            # noqa: N802 - ast's own naming
        self._count(node)

    def visit_IfExp(self, node):         # noqa: N802
        self._count(node)

    def visit_For(self, node):           # noqa: N802
        self._count(node)

    def visit_AsyncFor(self, node):      # noqa: N802
        self._count(node)

    def visit_While(self, node):         # noqa: N802
        self._count(node)

    def visit_ExceptHandler(self, node):  # noqa: N802
        self._count(node)

    def visit_With(self, node):          # noqa: N802
        self._count(node)

    def visit_AsyncWith(self, node):     # noqa: N802
        self._count(node)

    def visit_Assert(self, node):        # noqa: N802
        self._count(node)

    def visit_BoolOp(self, node):        # noqa: N802
        self._count(node, max(0, len(getattr(node, "values", ())) - 1))

    def visit_comprehension(self, node):
        self._count(node, 1 + len(getattr(node, "ifs", ())))

    def visit_Match(self, node):         # noqa: N802
        # 3.10+. One branch per case; the walk still descends into the bodies.
        self._count(node, len(getattr(node, "cases", ())))


def cyclomatic(text) -> int:
    """Decision points in *text*, or 0 when it does not parse.

    Total. A file that fails to parse is a file this cannot weigh, and the honest
    answer to "how complex" is then "this term contributed nothing" — not an
    exception thrown at a scheduler."""
    try:
        source = str(text or "")
    except (ValueError, TypeError):
        return 0
    if not source.strip() or len(source) > MAX_PARSE_BYTES:
        return 0
    try:
        tree = ast.parse(source)
    except (SyntaxError, ValueError, TypeError, MemoryError, RecursionError):
        return 0
    visitor = ComplexityVisitor()
    try:
        visitor.visit(tree)
    except RecursionError:
        return 0
    return int(visitor.decisions)


def churn(rel_posix, root=None) -> int:
    """Lines added + deleted across the file's history.

    `git log --numstat --format=` emits one `added<TAB>deleted<TAB>path` row per
    commit that touched the path; a binary file writes `-` in both numeric cells
    and contributes 0.

    Guarded by `shutil.which("git")` and never raising, for the reason
    `clusters.git_churn` gives: a weight term that can take down a run is worse
    than a weight term that is sometimes 0."""
    import shutil

    if not rel_posix or not shutil.which("git"):
        return 0
    try:
        proc = subprocess.run(
            ["git", "log", "--numstat", "--format=", "--", str(rel_posix)],
            cwd=str(root or Path.cwd()), capture_output=True, text=True,
            shell=False, timeout=_CHURN_TIMEOUT_S, creationflags=paths.NO_WINDOW)
        if proc.returncode != 0:
            return 0
        out = proc.stdout or ""
    except (OSError, subprocess.SubprocessError, ValueError):
        return 0
    total = 0
    for line in out.splitlines():
        cells = line.split("\t")
        if len(cells) < 3:
            continue
        for cell in cells[:2]:
            try:
                total += int(cell)
            except ValueError:
                continue                    # "-" — a binary revision
    return total


def _field(row, name, default=None):
    """One field off an event row, whether it decoded as a dict or an object."""
    if isinstance(row, dict):
        return row.get(name, default)
    return getattr(row, name, default)


def usage(key, evs=None, project=None) -> int:
    """Recorded agent turns against *key*, from `mindnet.telemetry` rows.

    *evs* is the caller's already-read event list — same bargain
    `advice.resolve` documents. Passing ``None`` reads the narrowed type slice,
    which is correct for a standalone caller and wrong on a hot path."""
    if not key:
        return 0
    try:
        from ..mindnet import telemetry as tel_mod
        want_type = tel_mod.EVENT_TYPE
    except Exception:                                   # noqa: BLE001
        return 0
    rows = evs
    if rows is None:
        try:
            from .. import events as ev_mod
            rows = list(ev_mod.read_types((want_type,), project))
        except Exception:                               # noqa: BLE001
            return 0
    want = str(key)
    seen = 0
    for row in rows or ():
        try:
            if str(_field(row, "type", "") or "") != want_type:
                continue
            payload = _field(row, "payload", None) or {}
            if not isinstance(payload, dict):
                continue
            if str(payload.get("task_id") or "") == want:
                seen += 1
        except Exception:                               # noqa: BLE001
            continue
    return seen


@dataclass(frozen=True)
class Weight:
    """One file's workload estimate, with the terms it was fused from.

    The terms are kept rather than collapsed into `total` because a lane plan
    that looks wrong is diagnosed by asking WHICH term dominated, and a bare
    integer cannot answer that."""

    path: str = ""
    complexity: int = 0
    churn: int = 0
    usage: int = 0
    total: int = 0
    reason: str = ""            # why a term is 0, when it is worth saying


def _read_text(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        return ""


def calculate_workload_weight(file_path, root=None, evs=None,
                              usage_key=None) -> Weight:
    """Fuse the three terms for one file.

    *root* is the repo the churn term is measured in; without it the file's own
    parent chain is used, which is right for an absolute path and gives 0 for a
    relative one rather than measuring some unrelated repository.

    *usage_key* is the telemetry key — a task id, since telemetry records agent
    turns per task and not per file. Omitted, the usage term is 0 and says so."""
    try:
        target = Path(str(file_path))
    except (TypeError, ValueError):
        return Weight(reason="unusable path")
    if not target.exists():
        return Weight(path=str(target), reason="no such file")

    complexity = cyclomatic(_read_text(target)) if target.suffix == ".py" else 0

    repo = Path(root) if root else target.parent
    try:
        rel = target.resolve().relative_to(Path(repo).resolve()).as_posix()
    except (ValueError, OSError):
        rel = target.name
    churned = churn(rel, root=repo)

    used = usage(usage_key, evs=evs) if usage_key else 0

    total = (W_COMPLEXITY * complexity + W_CHURN * churned + W_USAGE * used)
    reason = "" if (complexity or churned or used) else "every term resolved 0"
    return Weight(path=str(target), complexity=complexity, churn=churned,
                  usage=used, total=int(total), reason=reason)


def weigh_files(files, root=None, evs=None) -> dict:
    """`{rel_posix: total}` for *files* — the shape `clusters.shard_plan` wants.

    Total per file: one unweighable file contributes 0 and the rest still weigh."""
    out: dict = {}
    for item in files or ():
        try:
            rel = str(item)
        except (TypeError, ValueError):
            continue
        candidate = Path(root) / rel if root else Path(rel)
        out[rel] = calculate_workload_weight(candidate, root=root, evs=evs).total
    return out
