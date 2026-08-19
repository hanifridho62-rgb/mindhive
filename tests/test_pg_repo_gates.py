# tests/test_pg_repo_gates.py
"""Two repo-wide gates the Postgres mirror must never quietly break.

**No hook-reachable module may acquire a socket dependency.** `PreToolUse` fires
on every tool call. `UserPromptSubmit` fires on every prompt. The Stop hook fires
in **every Claude Code session on this machine, in every project**. A connect
attempt on any of those paths is not a slow feature — it is an outage, and it is
an outage inside the tool you would use to fix it. `enforce.py`, `sopgate.py`,
`rotation/hook.py` and `mindnet/enforcer.py` therefore reach neither `pgstore`
nor `pgtools`, directly or through anything they import at module scope.

**No credential literal in `src/` or `tests/`.** The recommended DSN is
`service=mindhive_ro`, resolved through libpq's `.pg_service.conf` with the
password in `PGPASSFILE`, so neither the repo nor the environment holds a secret.
A full connection URI with an inline password also works and is documented in
prose — but a "just an example" match inside the scanned tree is exactly how a
gate learns to be ignored, so the scanned tree carries none. That constraint
binds this file too: the canary's specimens are ASSEMBLED from fragments at run
time rather than written out, so the gate can scan its own definition without
finding itself. Exempting this file instead would have been simpler and wrong —
a real credential pasted here would then be the one place nothing looks.

Both are asserted over sources rather than over behaviour, because the offending
line is normally on a path no test takes: an import that costs 40 ms at hook
time, and a credential that only matters the day the repo becomes public.

`tests/test_pgstore.py` already carries a narrower version of each (one module,
three files). These are the repo-wide ones, and they cover `pgtools`,
`mcp_server` and `pgmirror` as well — modules that did not exist when those were
written.
"""
import ast
import re
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "mindhive"

# Every module a session hook can reach. Named individually rather than derived,
# because the derivation would itself be the thing that breaks silently.
HOOK_REACHABLE = ("enforce.py", "gateguard.py", "sopgate.py",
                  "rotation/hook.py", "mindnet/enforcer.py")

# The cluster none of them may touch. `pgmirror` is included even though it holds
# only the ADMIN credential — especially because of that.
#
# `pgcli` is in this list, and it was added because a canary went GREEN when it
# should not have: a hook module importing `pgcli` reaches `pgstore` at call
# time, but every one of `pgcli`'s own imports is deferred inside a handler, so
# a module-level graph walk sees `pgcli` importing nothing but `json` and
# reports the path clean. Naming `pgcli` itself closes that, because the walk
# then stops at a member of the cluster rather than looking through it. The
# general lesson is worth keeping in view: a transitive scan over module-level
# imports cannot see past a module that defers, so every member of a forbidden
# cluster has to be named, not merely its leaves.
PG_CLUSTER = ("pgstore", "pgtools", "pgmirror", "mcp_server", "pgcli")


def _module_name(path: Path) -> str:
    return str(path.relative_to(SRC).with_suffix("")).replace("\\", "/")


def _imports(src: str, module_level_only: bool):
    """First-party module names imported by `src`.

    `module_level_only` is the distinction that makes the transitive scan
    correct rather than merely strict: a deferred import inside a function body
    costs nothing at import time, and `events.py`'s mirror seam is exactly that
    — guarded, function-local, and free at the default backend. Counting it
    would flag every module that imports `events`, which is most of them, and a
    gate that fires on the correct design is a gate somebody deletes."""
    tree = ast.parse(src)
    nodes = tree.body if module_level_only else list(ast.walk(tree))
    out = set()
    for node in nodes:
        if isinstance(node, ast.ImportFrom):
            if node.level and node.module is None:
                out.update(a.name for a in node.names)
            elif node.level:
                out.add(node.module)
                out.update(f"{node.module}.{a.name}" for a in node.names)
            elif (node.module or "").startswith("mindhive"):
                out.add(node.module.split("mindhive.", 1)[-1])
        elif isinstance(node, ast.Import):
            for a in node.names:
                if a.name.startswith("mindhive."):
                    out.add(a.name.split("mindhive.", 1)[-1])
    return out


def _module_level_graph() -> dict:
    return {_module_name(p): _imports(p.read_text(encoding="utf-8"), True)
            for p in SRC.rglob("*.py")}


def _reaches(start: str, graph: dict) -> set:
    """Every first-party module reachable from `start` at import time."""
    seen, stack = set(), [start]
    while stack:
        current = stack.pop()
        for name in graph.get(current, ()):
            for candidate in (name, name.rsplit(".", 1)[0]):
                if candidate in graph and candidate not in seen:
                    seen.add(candidate)
                    stack.append(candidate)
    return seen


# --------------------------------------------------------------------------
# the forbidden import edges
# --------------------------------------------------------------------------

@pytest.mark.parametrize("name", HOOK_REACHABLE)
def test_a_hook_reachable_module_names_no_part_of_the_pg_cluster(name):
    """The direct edge, at ANY nesting depth — a deferred import here would
    still be paid the first time the guarded branch is taken, on a path where
    that cost is measured in every session on the machine."""
    path = SRC / name
    assert path.is_file(), f"{name} moved; this guard now covers nothing"
    imported = _imports(path.read_text(encoding="utf-8"), False)
    hits = sorted(i for i in imported
                  if any(i == m or i.endswith(f".{m}") for m in PG_CLUSTER))
    assert hits == [], f"{name} imports {hits}"


@pytest.mark.parametrize("name", HOOK_REACHABLE)
def test_no_hook_reachable_module_reaches_the_pg_cluster_transitively(name):
    """The edge the direct check cannot see: module A is clean, imports B at
    module scope, and B imports `pgstore`. The hook pays for it just the same,
    and nobody reading A would know."""
    graph = _module_level_graph()
    start = _module_name(SRC / name)
    reached = _reaches(start, graph)
    hits = sorted(m for m in PG_CLUSTER if m in reached)
    assert hits == [], f"{name} reaches {hits} at import time"


def test_the_transitive_scan_would_notice_a_two_hop_edge():
    """CANARY. The assertion above is an emptiness claim over a graph walk, and
    a walk that returned nothing — a broken `_module_name`, a changed layout —
    would satisfy it forever."""
    graph = {"enforce": {"helper"}, "helper": {"pgstore"}, "pgstore": set()}
    assert "pgstore" in _reaches("enforce", graph)
    assert _reaches("pgstore", graph) == set()


def test_the_scan_reads_a_real_and_populated_graph():
    """The other half of the canary: the graph must actually describe this
    repo. An empty or one-node graph makes every reachability answer `set()`."""
    graph = _module_level_graph()
    assert len(graph) > 40, f"only {len(graph)} modules in the graph"
    assert "pgtools" in graph and "pgstore" in graph
    assert "pgstore" in _reaches("pgtools", graph), \
        "pgtools imports pgstore at module scope; if this is false the scan is broken"


def test_the_admin_module_is_reachable_only_from_the_cli():
    """The privilege split, structurally. `pgmirror` holds the owner DSN; the
    AI path (`pgtools`, `mcp_server`) must not reach it, or the credential and
    the model would share a process by design rather than by accident."""
    graph = _module_level_graph()
    for ai_module in ("pgtools", "mcp_server", "pgstore"):
        assert "pgmirror" not in _reaches(ai_module, graph), \
            f"{ai_module} reaches the admin module"


# --------------------------------------------------------------------------
# no credential literal
# --------------------------------------------------------------------------

CREDENTIAL_PATTERNS = (
    # A connection URI carrying inline userinfo with a password.
    re.compile(r"postgres(?:ql)?://[^\s\"'/]*:[^\s\"'/]*@"),
    # A libpq keyword/value password, or any assignment that looks like one.
    re.compile(r"password\s*=\s*[\"'][^\"']+[\"']"),
    re.compile(r"PGPASSWORD\s*=\s*[\"'][^\"']+[\"']"),
)


def _scanned_files():
    for root in (REPO / "src", REPO / "tests"):
        for path in sorted(root.rglob("*.py")):
            yield path
    for path in sorted((REPO / "src").rglob("*.sql")):
        yield path


def test_no_credential_literal_anywhere_in_src_or_tests():
    """§9.10's repo gate, at repo scope rather than scoped to three files."""
    offences = []
    for path in _scanned_files():
        text = path.read_text(encoding="utf-8", errors="replace")
        for pattern in CREDENTIAL_PATTERNS:
            for match in pattern.finditer(text):
                line = text[:match.start()].count("\n") + 1
                offences.append(f"{path.relative_to(REPO)}:{line} "
                                f"{match.group(0)[:40]}")
    assert offences == [], offences


def test_the_credential_scan_recognises_every_shape_it_claims_to():
    """CANARY. Three patterns, and a scanner that only matched one would pass
    the assertion above while covering a third of it. Each planted string is a
    real spelling of the same mistake."""
    secret = "hunter" + "2"
    at = chr(64)
    planted = [
        f"dsn = 'postgresql://mindhive_reader:{secret}{at}127.0.0.1:5432/mindhive'",
        f"dsn = 'postgres://u:p{at}localhost/db'",
        f'conn = connect(host="h", pass' + f'word="{secret}")',
        "PG" + f'PASSWORD="{secret}"',
    ]
    for sample in planted:
        assert any(p.search(sample) for p in CREDENTIAL_PATTERNS), sample


def test_the_credential_scan_does_not_fire_on_the_recommended_form():
    """The scan must stay usable: `service=mindhive_ro` and a passwordless URI
    are the shapes this repo deliberately does use, and a gate that flags them
    is a gate that gets suppressed."""
    for benign in ("dsn = 'service=mindhive_ro'",
                   "'postgresql://owner@localhost/mindhive'",
                   "PGPASSFILE points at the password"):
        assert not any(p.search(benign) for p in CREDENTIAL_PATTERNS), benign


def test_the_scan_actually_reaches_the_postgres_modules():
    """An emptiness claim over a file walk that found no files would be green
    forever."""
    scanned = {p.name for p in _scanned_files()}
    for expected in ("pgstore.py", "pgtools.py", "pgmirror.py", "mcp_server.py",
                     "pgcli.py", "001_schema.sql", "003_roles_and_views.sql"):
        assert expected in scanned, expected


# --------------------------------------------------------------------------
# the backend switch is not flipped by anything in src/
# --------------------------------------------------------------------------

def test_nothing_in_src_writes_the_event_backend_variable():
    """R1, the highest-ranked risk. Flipping `MINDHIVE_EVENT_BACKEND` away from
    `file` also disables the dashboard's overview memo — a measured 726 ms ->
    6.6 ms on `/api/overview` — so it is a separate decision with a known price.
    Nothing in this change takes it, and that is enforced rather than intended."""
    offences = []
    for path in sorted((REPO / "src").rglob("*.py")):
        text = path.read_text(encoding="utf-8", errors="replace")
        for match in re.finditer(
                r"environ\s*\[\s*[\"']MINDHIVE_EVENT_BACKEND[\"']\s*\]\s*=|"
                r"setenv\s*\(\s*[\"']MINDHIVE_EVENT_BACKEND[\"']|"
                r"environ\.setdefault\s*\(\s*[\"']MINDHIVE_EVENT_BACKEND[\"']",
                text):
            offences.append(f"{path.relative_to(REPO)}: {match.group(0)}")
    assert offences == [], offences


def test_the_backend_write_scan_would_catch_one():
    """CANARY for the scan above."""
    planted = 'os.environ["MINDHIVE_EVENT_BACKEND"] = "pg"'
    assert re.search(r"environ\s*\[\s*[\"']MINDHIVE_EVENT_BACKEND[\"']\s*\]\s*=",
                     planted)


# --------------------------------------------------------------------------
# D4: no write statement reachable from the reader credential
# --------------------------------------------------------------------------
#
# The gate that replaces a deleted function. `pgstore.mirror_one` used to insert
# one row per `append_event` through the READER DSN, and it could never have
# worked: `003_roles_and_views.sql` creates `mindhive_reader` with
# `default_transaction_read_only = on` set **on the role**, so the insert was
# refused by the server on every call and the `False` it returned went nowhere.
# It was deleted rather than repaired.
#
# A deletion is not a guarantee, though — the next person to want a write seam
# will put it back in the module that already has a connection. This asserts the
# shape instead: every write statement in `src/` lives in the ONE module that
# holds the owner DSN.

_WRITE_SQL = re.compile(
    r"\b(INSERT\s+INTO|UPDATE|DELETE\s+FROM|TRUNCATE(\s+TABLE)?|COPY|"
    r"CREATE\s+(TABLE|INDEX|VIEW)|ALTER\s+TABLE|DROP\s+(TABLE|INDEX|VIEW))"
    r"\s+(IF\s+(NOT\s+)?EXISTS\s+)?mindhive\.\w+",
    re.IGNORECASE)

# `pgmirror` holds `MINDHIVE_PG_DSN_ADMIN` and is the only module that may write.
# `pgcli` is NOT on this list: it prints what `pgmirror` returns and owns no SQL.
WRITERS = {"pgmirror.py"}


def _sql_writes(path: Path) -> list[str]:
    """Every write statement in a module's string constants.

    Read off the AST's string constants rather than the raw text, so a docstring
    that DESCRIBES the rule ("never `TRUNCATE`, which cannot be scoped") is not
    itself an offence — that sentence is in `pgmirror`'s docstring and would make
    the gate unusable if prose counted. Comments are invisible to the AST, which
    is the same protection by a different route."""
    try:
        tree = ast.parse(path.read_text(encoding="utf-8"))
    except SyntaxError:                         # pragma: no cover - not our file
        return []
    out = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Constant) and isinstance(node.value, str):
            if ast.get_docstring(tree) == node.value:
                continue
            for match in _WRITE_SQL.finditer(node.value):
                out.append(match.group(0))
    return out


def test_no_module_but_the_admin_one_carries_a_write_statement():
    """D4. The reader path — `pgstore`, `pgtools`, `mcp_server`, `pgcli` — must
    hold no statement that could mutate the mirror, so a future write seam has
    nowhere to live that already has a connection."""
    offences = {}
    for path in sorted(SRC.rglob("*.py")):
        rel = str(path.relative_to(SRC)).replace("\\", "/")
        if rel in WRITERS:
            continue
        writes = _sql_writes(path)
        if writes:
            offences[rel] = sorted(set(writes))
    assert offences == {}, \
        (f"write SQL outside the admin module: {offences}. The reader role is "
         f"read-only at the ROLE level, so this cannot work — it can only fail "
         f"at runtime, quietly.")


def test_the_write_scan_sees_the_admin_module_it_exempts():
    """CANARY. An emptiness claim over a scan that finds nothing anywhere would
    hold forever. `pgmirror` really does carry `DELETE` and `COPY`, and if this
    stops being true the scan has broken rather than the code having improved."""
    found = " ".join(_sql_writes(SRC / "pgmirror.py")).upper()
    assert "DELETE FROM" in found, "the scan cannot see pgmirror's own DELETE"
    assert "COPY" in found, "the scan cannot see pgmirror's own COPY"


def test_the_write_scan_would_catch_a_reintroduced_insert():
    """CANARY, on the exact shape that was deleted. Written to a temp file rather
    than asserted against a regex in isolation, so the canary exercises the same
    AST path the gate does."""
    import tempfile
    with tempfile.TemporaryDirectory() as d:
        planted = Path(d) / "pgstore_like.py"
        planted.write_text(
            '_INSERT_ONE_SQL = ("INSERT INTO mindhive.events (project, ts) '
            'VALUES (%s, %s)")\n', encoding="utf-8")
        assert _sql_writes(planted) != [], \
            "the gate would not notice a reintroduced single-row insert"
