# tests/test_symbols.py
"""T7/T8 — zero-token structural extraction, and its seat as the seventh injector.

Encodes §11.6 of
`docs/plans/2026-08-22-001-feat-injection-visibility-and-symbol-index-plan.md`.

The claim under test is a COST claim: that a few hundred tokens of symbol index
can stand in for the parts of a 125,241-line tree an agent would otherwise read.
Two things have to be true for that to hold, and neither is obvious:

1. **The index has to fit.** A flat listing of 136 modules does not fit any
   budget, so something chooses. Without ranking the chooser is `sorted()`, and
   `autoresume.py` lands while `__main__.py` -- 4,213 lines, the centre of
   everything -- is cut. That is the failure this file's ranking cases exist for.
2. **What it leaves out has to be SAID.** A map that silently drops 124 modules
   reads exactly like a map of a 12-module project. Every truncation here is
   counted and reported, and there is a test for it.

The extraction itself is stdlib `ast` and nothing else: no tree-sitter, no LSP,
no runtime dependency, and `pyproject.toml`'s `dependencies = []` stays empty.
"""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from mindhive import injectors, symbols

REPO = Path(__file__).resolve().parent.parent
REAL_SRC = REPO / "src" / "mindhive"


# --- fixture builders: valid by default, one thing varied per test ----------

PAYMENT = '''\
"""A gateway."""
import requests
import stripe
from .ledger import record
from ..shared import util


class GatewayPembayaran(Base, Mixin):
    """Docstring."""

    def proses_kartu(self, n):
        return stripe.API.charge(n)

    def refund_dana(self, n):
        return requests.post(util.URL, json={"n": n})


def validasi_format_kartu(n):
    return bool(n)


CONST = 3
'''


def _write(tmp_path, rel, text):
    p = tmp_path / rel
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(text, encoding="utf-8")
    return p


# --- extract: the shape the ask names ---------------------------------------

def test_extract_returns_the_shape_the_request_asked_for(tmp_path):
    """The ask supplied a target JSON shape for `pembayaran.py`. This is that
    shape, against the same file contents, so the contract is checked against
    what was asked rather than against what was convenient to build."""
    p = _write(tmp_path, "pkg/pembayaran.py", PAYMENT)
    got = symbols.extract(p, root=tmp_path)

    assert got["file"] == "pkg/pembayaran.py"
    assert got["imports"] == ["..shared.util", ".ledger.record", "requests", "stripe"]
    assert list(got["classes"]) == ["GatewayPembayaran"]
    assert got["classes"]["GatewayPembayaran"]["methods"] == [
        "proses_kartu", "refund_dana"]
    assert got["classes"]["GatewayPembayaran"]["bases"] == ["Base", "Mixin"]
    assert got["functions"] == ["validasi_format_kartu"]


def test_a_class_records_the_imported_names_it_actually_uses(tmp_path):
    """The ask's example carries `"dependencies": ["stripe.API"]` INSIDE the
    class. That is not the module's import list -- it is which imports this
    class reaches for, which is the fact that answers "what does this talk to".
    `ast` can see it exactly, so it is extracted rather than approximated."""
    p = _write(tmp_path, "pkg/pembayaran.py", PAYMENT)
    cls = symbols.extract(p, root=tmp_path)["classes"]["GatewayPembayaran"]
    assert cls["deps"] == ["requests.post", "stripe.API", "util.URL"]


def test_a_module_level_name_is_not_mistaken_for_a_class_dependency(tmp_path):
    """The discriminating half. `CONST = 3` and `validasi_format_kartu` sit
    OUTSIDE the class; an implementation that walked the whole module for each
    class -- the easy mistake -- would list the module's every attribute use
    under every class it contains."""
    src = "import stripe\n\n\nclass A:\n    pass\n\n\nX = stripe.API\n"
    p = _write(tmp_path, "m.py", src)
    assert symbols.extract(p, root=tmp_path)["classes"]["A"]["deps"] == []


def test_a_nested_function_is_not_reported_as_a_module_function(tmp_path):
    """`ast.walk` finds every FunctionDef at any depth. The map is of the
    module's surface, so a closure inside another function must not appear
    beside it as though it were importable."""
    src = "def outer():\n    def inner():\n        pass\n    return inner\n"
    p = _write(tmp_path, "m.py", src)
    assert symbols.extract(p, root=tmp_path)["functions"] == ["outer"]


def test_an_async_definition_is_still_a_definition(tmp_path):
    src = "async def fetch():\n    pass\n\n\nclass A:\n    async def go(self):\n        pass\n"
    p = _write(tmp_path, "m.py", src)
    got = symbols.extract(p, root=tmp_path)
    assert got["functions"] == ["fetch"]
    assert got["classes"]["A"]["methods"] == ["go"]


# --- extract: totality ------------------------------------------------------

def test_a_file_that_does_not_parse_is_skipped_not_raised(tmp_path):
    """Totality. One unparseable file in a 136-module tree must cost that file,
    not the index -- and certainly not the prompt, since this runs on the hook
    path."""
    p = _write(tmp_path, "broken.py", "def (:\n")
    assert symbols.extract(p, root=tmp_path) is None


def test_a_missing_file_is_skipped_not_raised(tmp_path):
    assert symbols.extract(tmp_path / "nope.py", root=tmp_path) is None


def test_invalid_utf8_is_still_measurable(tmp_path):
    """`errors="replace"` keeps a file with a bad byte readable enough to index.
    Blanking it would hide a real module; raising would take out the caller.

    The bad byte sits in a COMMENT, which is where one realistically survives in
    a committed file. A first draft put it in the middle of `import os`, which
    `replace` turns into genuinely invalid Python -- that file is unparseable
    rather than merely oddly encoded, and the totality case above covers it."""
    p = tmp_path / "m.py"
    p.write_bytes(b"# caf\xe9\nimport os\n\n\ndef f():\n    pass\n")
    got = symbols.extract(p, root=tmp_path)
    assert got is not None and got["functions"] == ["f"]


# --- the pluggable extractor seam (T1) --------------------------------------

def test_the_extractor_registry_is_keyed_by_suffix(tmp_path):
    """T1's seam. tree-sitter is NOT in this slice; the place it would plug in
    is, and it is one dict entry rather than a rewrite."""
    assert symbols.EXTRACTORS[".py"] is not None
    assert list(symbols.EXTRACTORS) == [".py"], (
        "a second extractor arrived — give it its own tests before this list "
        "grows")


def test_a_suffix_with_no_extractor_is_skipped_rather_than_guessed(tmp_path):
    p = _write(tmp_path, "app.ts", "export function f() {}\n")
    assert symbols.extract(p, root=tmp_path) is None


def test_a_registered_suffix_routes_to_its_own_extractor(tmp_path, monkeypatch):
    """The seam, exercised rather than merely declared: a fake extractor
    registered for `.ts` must actually be the thing that runs."""
    p = _write(tmp_path, "app.ts", "whatever")
    monkeypatch.setitem(symbols.EXTRACTORS, ".ts",
                        lambda path, rel: {"file": rel, "imports": [],
                                           "classes": {}, "functions": ["fake"]})
    assert symbols.extract(p, root=tmp_path)["functions"] == ["fake"]


def test_the_javascript_inside_python_string_literals_is_a_recorded_limit():
    """A limit stated in the plan and asserted here so it cannot rot into a
    surprise: this dashboard's JS lives inside Python string literals
    (`cat_board._JS`), so `ast` sees the module's Python surface and none of the
    functions the browser actually runs. The seam above is where that gets
    fixed; this slice does not fix it."""
    got = symbols.extract(REAL_SRC / "cat_board.py", root=REAL_SRC)
    assert "script" in got["functions"]          # the Python producer IS seen
    assert "paintCats" not in got["functions"]   # the JS it emits is NOT


# --- graph ------------------------------------------------------------------

def test_relative_imports_resolve_to_modules_in_the_tree(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/a.py", "from .b import thing\nfrom ..top import other\n")
    _write(tmp_path, "pkg/b.py", "")
    _write(tmp_path, "top.py", "")
    g = symbols.graph(symbols.scan(tmp_path))
    assert g["pkg.a"] == {"pkg.b", "top"}


def test_an_absolute_self_package_import_resolves_too(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/a.py", "import pkg.b\nfrom pkg import c\n")
    _write(tmp_path, "pkg/b.py", "")
    _write(tmp_path, "pkg/c.py", "")
    g = symbols.graph(symbols.scan(tmp_path))
    assert g["pkg.a"] == {"pkg.b", "pkg.c"}


def test_third_party_imports_are_listed_but_are_not_nodes(tmp_path):
    """`requests` belongs in the map -- knowing a module talks to stripe is the
    point -- but it is not a file in this tree, so it must not become a graph
    node that rank then distributes score to."""
    _write(tmp_path, "a.py", "import requests\n")
    entries = symbols.scan(tmp_path)
    assert entries[0]["imports"] == ["requests"]
    assert symbols.graph(entries)["a"] == set()


def test_a_module_never_imports_itself(tmp_path):
    _write(tmp_path, "pkg/__init__.py", "from .a import x\n")
    _write(tmp_path, "pkg/a.py", "from . import a\n")
    g = symbols.graph(symbols.scan(tmp_path))
    assert "pkg.a" not in g["pkg.a"]


# --- rank -------------------------------------------------------------------

def _star(tmp_path):
    """A hub that imports five leaves, one core every leaf imports, and one
    module nobody touches. Every ranking case below reads from this one shape."""
    _write(tmp_path, "core.py", "")
    for n in range(5):
        _write(tmp_path, f"leaf{n}.py", "import core\n")
    _write(tmp_path, "hub.py", "".join(f"import leaf{n}\n" for n in range(5)))
    _write(tmp_path, "lonely.py", "")
    return symbols.rank(symbols.graph(symbols.scan(tmp_path)))


def test_a_widely_imported_module_outranks_one_nobody_imports(tmp_path):
    """The authority half: `core` is imported five times, `lonely` never."""
    r = _star(tmp_path)
    assert r["core"] > r["lonely"]


def test_a_module_that_pulls_the_system_together_outranks_a_leaf(tmp_path):
    """The HUB half, and the discriminating case for the whole design.

    Measured on the real tree before this was built: plain authority PageRank
    puts `__main__` at position 131 of 136 and `autoresume` at 49, because
    `__main__` imports 78 modules and is imported by none. An index built on
    authority alone would cut the entry point -- the single most useful file to
    hand an agent -- and keep a leaf. This asserts the ranking has a hub term at
    all; an authority-only implementation fails it."""
    r = _star(tmp_path)
    assert r["hub"] > r["leaf0"]


def test_rank_is_deterministic(tmp_path):
    """Same tree, same numbers. An index that reshuffles between runs makes the
    injected context differ every turn, which would defeat the write-on-change
    policy the recorder depends on."""
    assert _star(tmp_path) == _star(tmp_path)


def test_rank_survives_a_tree_with_no_edges_at_all(tmp_path):
    _write(tmp_path, "a.py", "")
    _write(tmp_path, "b.py", "")
    r = symbols.rank(symbols.graph(symbols.scan(tmp_path)))
    assert set(r) == {"a", "b"}
    assert all(v > 0 for v in r.values())


def test_rank_of_an_empty_tree_is_empty_not_a_division_by_zero(tmp_path):
    assert symbols.rank({}) == {}


# --- index and its budget ---------------------------------------------------

def test_the_index_fits_the_budget_it_was_given(tmp_path):
    for n in range(40):
        _write(tmp_path, f"m{n}.py",
               "import os\n\n\nclass C%d:\n    def m(self):\n        pass\n" % n)
    idx = symbols.index(tmp_path, budget=60)
    assert idx["tokens"] <= 60
    assert injectors._tokens(len(idx["text"])) <= 60


def test_the_index_says_what_it_left_out(tmp_path):
    """No silent caps. A map of 12 modules and a map of 136 truncated to 12 look
    identical unless the second one says so -- and the whole value of this
    artifact is that an agent can trust it as a description of the tree."""
    for n in range(40):
        _write(tmp_path, f"m{n}.py", "def f():\n    pass\n")
    idx = symbols.index(tmp_path, budget=40)
    assert idx["omitted"] > 0
    assert idx["included"] + idx["omitted"] == idx["modules"] == 40
    assert str(idx["omitted"]) in idx["text"], (
        "the rendered map must carry the omission count — an agent reads the "
        "text, not this dict")


def test_a_generous_budget_omits_nothing_and_says_that_too(tmp_path):
    _write(tmp_path, "a.py", "def f():\n    pass\n")
    idx = symbols.index(tmp_path, budget=4096)
    assert idx["omitted"] == 0
    assert idx["included"] == 1


def test_the_index_is_ranked_not_alphabetical(tmp_path):
    """The failure §11.6 names by name. `aaa` sorts first and matters least;
    `zzz` is the hub. A `sorted()` chooser keeps the wrong one."""
    _write(tmp_path, "aaa.py", "")
    _write(tmp_path, "mid.py", "import aaa\n")
    _write(tmp_path, "zzz.py", "import aaa\nimport mid\n")
    idx = symbols.index(tmp_path, budget=4096)
    order = [f["file"] for f in idx["files"]]
    assert order.index("zzz.py") < order.index("aaa.py")


def test_one_enormous_module_cannot_eat_the_whole_budget(tmp_path):
    """A per-file cap, so the highest-ranked module cannot crowd out every other
    entry. `__main__.py` alone declares hundreds of functions."""
    _write(tmp_path, "big.py",
           "".join("def f%d():\n    pass\n\n\n" % n for n in range(400)))
    _write(tmp_path, "small.py", "def g():\n    pass\n")
    idx = symbols.index(tmp_path, budget=1024)
    assert idx["included"] == 2
    big = [f for f in idx["files"] if f["file"] == "big.py"][0]
    assert len(big["functions"]) < 400
    assert big["trimmed"] > 0


def test_the_rendered_map_lists_modules_not_every_bound_name(tmp_path):
    """Measured, not theorised. Rendering the raw import list spent all twelve
    of `dashboard.py`'s slots on twelve names from ONE module -- a dozen tokens
    saying what one token says -- and the map fitted 11 of 137 modules because
    of it. The request's own example shows modules too: `["requests","stripe"]`.

    Discriminating: a bound-name renderer produces three entries here, and a
    module renderer produces one."""
    _write(tmp_path, "core.py", "A = 1\nB = 2\nC = 3\n")
    _write(tmp_path, "user.py",
           "from .core import A, B, C\nfrom dataclasses import asdict, field\n")
    idx = symbols.index(tmp_path, budget=4096)
    user = [f for f in idx["files"] if f["file"] == "user.py"][0]
    assert user["imports"] == ["core", "dataclasses"]


def test_a_local_dependency_is_named_so_it_can_be_followed(tmp_path):
    """A local name in an `imp` list must match a `file` line elsewhere in the
    same map -- that is what makes the map navigable rather than a word list."""
    _write(tmp_path, "pkg/__init__.py", "")
    _write(tmp_path, "pkg/core.py", "")
    _write(tmp_path, "pkg/user.py", "from .core import thing\n")
    idx = symbols.index(tmp_path, budget=4096)
    user = [f for f in idx["files"] if f["file"] == "pkg/user.py"][0]
    assert user["imports"] == ["pkg.core"]
    assert any(f["file"] == "pkg/core.py" for f in idx["files"])


def test_the_index_is_json_serialisable(tmp_path):
    _write(tmp_path, "a.py", PAYMENT)
    json.dumps(symbols.index(tmp_path, budget=1024))


# --- the real tree: the plan's verify line ----------------------------------

def test_the_real_index_fits_the_default_budget():
    """§11.6's benchmark, against the committed tree rather than a fixture --
    the same reason `test_packs.py` opens by loading the real manifest."""
    idx = symbols.index(REAL_SRC, budget=symbols.BUDGET_TOKENS)
    assert idx["tokens"] <= symbols.BUDGET_TOKENS
    assert idx["modules"] > 100, "the scan found almost nothing — check the walk"


def test_the_real_index_ranks_the_entry_point_above_a_leaf():
    """The plan's verify line, verbatim: `__main__.py` above `autoresume.py`.

    Not a tautology -- measured before implementation, authority-only PageRank
    puts them at 131 and 49 respectively, the wrong way round."""
    r = symbols.rank(symbols.graph(symbols.scan(REAL_SRC)))
    assert r["__main__"] > r["autoresume"]


def test_the_real_index_keeps_the_entry_point_and_drops_something():
    """The consequence of the line above, at the budget. If everything fit,
    ranking would be decoration."""
    idx = symbols.index(REAL_SRC, budget=symbols.BUDGET_TOKENS)
    kept = [f["file"] for f in idx["files"]]
    assert "__main__.py" in kept
    assert idx["omitted"] > 0


# --- T8: the seventh injector ----------------------------------------------

def test_symbols_is_a_registered_injector_name():
    assert "symbols" in injectors.INJECTOR_NAMES


def test_the_probe_reports_itself_as_symbols():
    inj = symbols.probe("any prompt")
    assert isinstance(inj, injectors.Injection)
    assert inj.name == "symbols"
    assert inj.verdict in injectors.VERDICTS


def test_the_injector_stays_silent_until_it_is_switched_on(monkeypatch):
    """Opt-in, the same shape the rotation hook uses and for the same reason:
    this one costs real tokens on EVERY prompt, so registering it and switching
    it on are separate steps. A silence with a reason is not an outage -- the
    grey cat plus this sentence is the operator's answer."""
    monkeypatch.delenv(symbols.ENV_SWITCH, raising=False)
    inj = symbols.probe("any prompt")
    assert inj.verdict == "silent"
    assert inj.chars == 0
    assert symbols.ENV_SWITCH in inj.reason, (
        "a silent injector must name the switch that would make it speak")


def test_switched_on_the_injector_delivers_a_real_map(monkeypatch, tmp_path):
    monkeypatch.setenv(symbols.ENV_SWITCH, "1")
    monkeypatch.setenv(symbols.ENV_ROOT, str(REAL_SRC))
    inj = symbols.probe("any prompt")
    assert inj.verdict == "verbatim"
    assert inj.chars > 0
    assert inj.tokens <= symbols.BUDGET_TOKENS
    assert "symbol index" in inj.text


def _ctx(text):
    """A UserPromptSubmit context in the shape the pipeline actually passes.

    This helper exists because its absence shipped a bug. The first draft of the
    three tests below asserted against a BARE STRING, `symbols.extend_hook_context`
    was written to match, and it therefore returned a `str` into a pipeline whose
    every other pass speaks this dict -- which silently cost `style` its block.
    Same class of mistake as the `dict`-where-a-JSON-string-belongs incident in
    `injectors.py`'s docstring, in the opposite direction."""
    return {"hookSpecificOutput": {"hookEventName": "UserPromptSubmit",
                                   "additionalContext": text}}


def test_extend_hook_context_is_additive(monkeypatch):
    monkeypatch.setenv(symbols.ENV_SWITCH, "1")
    monkeypatch.setenv(symbols.ENV_ROOT, str(REAL_SRC))
    out = symbols.extend_hook_context(_ctx("EXISTING"), "prompt")
    got = injectors.context_text(out)
    assert got.startswith("EXISTING"), "an injector may add, never replace"
    assert len(got) > len("EXISTING")


def test_extend_hook_context_speaks_the_shape_the_next_pass_reads(monkeypatch):
    """The discriminating test for the bug above. A bare-string return passes
    every other case in this file and fails only this one."""
    monkeypatch.setenv(symbols.ENV_SWITCH, "1")
    monkeypatch.setenv(symbols.ENV_ROOT, str(REAL_SRC))
    for start in (None, _ctx(""), _ctx("EXISTING")):
        out = symbols.extend_hook_context(start, "prompt")
        assert isinstance(out, dict), f"returned {type(out).__name__} for {start!r}"
        assert out["hookSpecificOutput"]["hookEventName"] == "UserPromptSubmit"
        assert injectors.context_text(out), "the map did not reach the payload"


def test_extend_hook_context_returns_the_context_untouched_when_off(monkeypatch):
    monkeypatch.delenv(symbols.ENV_SWITCH, raising=False)
    assert symbols.extend_hook_context(_ctx("EXISTING"), "p") == _ctx("EXISTING")
    assert symbols.extend_hook_context(None, "prompt") is None


def test_extend_hook_context_is_fail_open(monkeypatch):
    """The contract every injector on this path holds: a raise here breaks the
    user's prompt, not a feature. The canary runs FIRST so this cannot pass by
    the switch simply being off."""
    monkeypatch.setenv(symbols.ENV_SWITCH, "1")
    monkeypatch.setenv(symbols.ENV_ROOT, str(REAL_SRC))
    assert symbols.extend_hook_context(_ctx("E"), "p") != _ctx("E")    # CANARY

    def boom(*a, **k):
        raise RuntimeError("index blew up")

    monkeypatch.setattr(symbols, "cached_text", boom)
    assert symbols.extend_hook_context(_ctx("E"), "p") == _ctx("E")


def test_extend_hook_context_stays_silent_on_a_machine_prompt(monkeypatch):
    """The pipeline-wide contract every speaking pass holds: a machine-generated
    prompt -- a task notification, a system reminder, an injected skill body --
    receives NO injection, because it is not the user asking for anything. The
    other passes suppress via `promptgate.is_machine_prompt`; this one must too,
    or the ~1,000-token repo map lands on EVERY machine turn the moment the
    switch is on. That is exactly what reddened the "machine text stays silent"
    tests in `test_sopgate.py` and `test_style.py` when `MINDHIVE_SYMBOLS=1`."""
    monkeypatch.setenv(symbols.ENV_SWITCH, "1")
    monkeypatch.setenv(symbols.ENV_ROOT, str(REAL_SRC))
    machine = json.dumps({"prompt": "<system-reminder/>"})
    assert symbols.extend_hook_context(_ctx("EXISTING"), machine) == _ctx("EXISTING")
    # CANARY: the SAME switched-on pass DOES inject for a real typed question on
    # this exact raw shape -- so the silence above is the machine-prompt gate,
    # not a dead injector nor an unparsed raw quietly failing open to nothing.
    human = json.dumps({"prompt": "how does the refund flow work?"})
    assert symbols.extend_hook_context(_ctx("EXISTING"), human) != _ctx("EXISTING")


def test_the_probe_reports_silent_on_a_forced_fault(monkeypatch):
    monkeypatch.setenv(symbols.ENV_SWITCH, "1")
    monkeypatch.setenv(symbols.ENV_ROOT, str(REAL_SRC))
    assert symbols.probe("p").verdict == "verbatim"                    # CANARY

    def boom(*a, **k):
        raise RuntimeError("index blew up")

    monkeypatch.setattr(symbols, "cached_text", boom)
    inj = symbols.probe("p")
    assert inj.verdict == "silent"
    assert "blew up" in inj.reason


def test_the_pipeline_names_symbols_last(monkeypatch):
    """Order is the contract. `style` prepends and must survive truncation of
    everything before it, so a seventh pass appended after it would sit ahead of
    the style block in the composed text. The map goes BEFORE style."""
    order = [n for n in injectors.PIPELINE_ORDER]
    assert order[-1] == "style"
    assert order[-2] == "symbols"


def test_the_symbols_pass_does_not_silence_the_pass_after_it(monkeypatch):
    """THE regression test for the shape bug, at the level it actually bit.

    `style` runs last, prepends, and is the one pass that creates a context from
    nothing -- so it is never silent, and a run where it reports zero is a run
    where something upstream broke the payload. That is precisely what a
    bare-string return did: the map arrived, `style`'s fail-open swallowed the
    type mismatch, and the recorder showed all seven passes at zero while the
    hook printed the map. Only a whole-pipeline assertion can see it; every
    single-function test still passed."""
    monkeypatch.setenv(symbols.ENV_SWITCH, "1")
    monkeypatch.setenv(symbols.ENV_ROOT, str(REAL_SRC))
    _ctx_out, rows = injectors.run_pipeline(json.dumps({"prompt": "hello"}))
    by_name = {r.name: r for r in rows}
    assert by_name["symbols"].chars > 0, "the map did not get injected at all"
    assert by_name["style"].chars > 0, (
        "style went silent behind the symbols pass — the payload shape broke")


def test_a_switched_off_symbols_pass_still_gets_a_row(monkeypatch):
    """E2 again, on the recorder rather than the CSS: the row exists whether or
    not it spoke, because "did not inject" is the answer the operator asked to
    be able to see."""
    monkeypatch.delenv(symbols.ENV_SWITCH, raising=False)
    _ctx, rows = injectors.run_pipeline(json.dumps({"prompt": "hello"}))
    names = [r.name for r in rows]
    assert "symbols" in names
    row = [r for r in rows if r.name == "symbols"][0]
    assert row.verdict == "silent" and row.chars == 0


# --- the CLI surface (§11.6: `hive symbols [PATH] [--budget N] [--json]`) ----

def test_the_cli_maps_a_tree_and_exits_zero(capsys):
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["symbols", str(REAL_SRC), "--budget", "240"]) == 0
    out = capsys.readouterr().out
    assert "symbol index" in out
    assert "omitted" in out, "a truncated map must say so on the CLI too"


def test_the_cli_json_is_parseable_on_stdout(capsys):
    """Parsed, not merely non-empty: a test that only checks something printed
    tests nothing."""
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["symbols", str(REAL_SRC), "--budget", "240",
                         "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert doc["included"] + doc["omitted"] == doc["modules"]
    assert doc["tokens"] <= 240


def test_the_cli_budget_defaults_rather_than_requiring_a_flag(capsys):
    """Every parameter has a default — the DX checklist line this surface was
    scored against."""
    from mindhive import __main__ as cli_mod
    assert cli_mod.main(["symbols", str(REAL_SRC), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["budget"] == symbols.BUDGET_TOKENS


# --- the per-unit slice -------------------------------------------------------
#
# WB1.4's open half. The wave-scoped slice is one artifact resolved once and
# handed to every task in the wave, which is right for the incumbent — a wave is
# a set of related tasks — and wrong for a lintester mission, whose units own
# DIFFERENT directories by construction. Measured before this existed: the same
# 3,896-character block, byte for byte, in every unit's brief.


def _three_module_tree(tmp_path):
    """`core` <- `web`, and `lonely`, which no module in the tree touches."""
    _write(tmp_path, "core.py",
           "import json\n\n\ndef fold(rows):\n    return json.dumps(rows)\n")
    _write(tmp_path, "web.py",
           "from core import fold\n\n\ndef serve(rows):\n    return fold(rows)\n")
    _write(tmp_path, "lonely.py",
           "def unrelated():\n    return 1\n")
    return symbols.build_map(tmp_path)


def test_a_slice_carries_the_module_the_unit_owns(tmp_path):
    smap = _three_module_tree(tmp_path)
    got = symbols.focus(smap, [tmp_path / "core.py"])

    assert "core.py" in got
    assert "fold" in got, "the unit's own functions are the point of the slice"


def test_two_units_in_one_repository_are_given_different_slices(tmp_path):
    """The whole of WB1.4. Before this, `wave_context` resolved one repo-wide
    block and `_compose` gave every unit the same bytes, so a per-task slice was
    reported as delivered while every task read the same map."""
    smap = _three_module_tree(tmp_path)

    owned = symbols.focus(smap, [tmp_path / "core.py"])
    other = symbols.focus(smap, [tmp_path / "lonely.py"])

    assert owned and other
    assert owned != other
    assert "lonely.py" not in owned, (
        "a module nothing in the unit's neighbourhood touches is not its "
        "business, and carrying it is the repo-wide slice again")


def test_the_neighbourhood_reaches_back_along_the_import_edge(tmp_path):
    """`web` imports `core`, so a unit that owns `core` must be shown `web`.

    The reverse edge is the half a naive implementation drops: `graph` answers
    what a module imports, and the question an agent editing `core.fold` needs
    answered is who CALLS it."""
    smap = _three_module_tree(tmp_path)
    got = symbols.focus(smap, [tmp_path / "core.py"])

    assert "web.py" in got, got


def test_a_unit_that_owns_no_module_gets_no_slice_rather_than_an_empty_one(
        tmp_path):
    """`""` is the signal to fall back to the wave slice. An empty rendered map
    — a header saying zero of three — would brief the agent with a statement
    that its repository has no code in it."""
    smap = _three_module_tree(tmp_path)
    _write(tmp_path, "notes.md", "# not python\n")

    assert symbols.focus(smap, [tmp_path / "notes.md"]) == ""
    assert symbols.focus(smap, []) == ""
    assert symbols.focus(smap, [tmp_path.parent / "outside.py"]) == ""


def test_a_map_built_from_a_relative_root_still_matches_a_resolved_path(
        tmp_path, monkeypatch):
    """The defect this fixes, pinned. `lintester._files_for` answers RESOLVED
    absolute paths; `Path.relative_to` between one of those and a relative root
    raises rather than matching, so a map built from `src/mindhive` answered an
    empty slice for every unit and nothing anywhere reported a fault."""
    _write(tmp_path, "pkg/core.py", "def f():\n    return 1\n")
    monkeypatch.chdir(tmp_path)

    smap = symbols.build_map("pkg")
    got = symbols.focus(smap, [(tmp_path / "pkg" / "core.py").resolve()])

    assert "core.py" in got


def test_the_slice_says_how_many_modules_it_left_out(tmp_path):
    """Same rule the repo-wide index is held to: a map that drops modules and
    does not say so reads as a map of a smaller repository."""
    smap = _three_module_tree(tmp_path)
    got = symbols.focus(smap, [tmp_path / "core.py"])

    assert got.startswith("# symbol index:")
    assert f"of {smap.total} modules" in got
    assert "this unit owns" in got, (
        "the header must not claim the repo-wide ranking it did not do")


def test_the_budget_cuts_the_neighbour_before_it_cuts_the_owner(tmp_path):
    """Greedy in the order `focus` builds: owned modules first, neighbours
    after. A budget that dropped the unit's OWN module while keeping a module it
    merely imports would be a map of everything except the thing being worked
    on."""
    smap = _three_module_tree(tmp_path)
    tight = symbols.focus(smap, [tmp_path / "core.py"], budget=32)
    wide = symbols.focus(smap, [tmp_path / "core.py"], budget=4096)

    assert injectors._tokens(len(tight)) <= 32
    assert "core.py" in tight and "web.py" not in tight
    assert "core.py" in wide and "web.py" in wide
    assert "1 of 3 modules" in tight, "a cut map must still say what it cut"


def test_a_budget_that_fits_nothing_answers_no_slice_at_all(tmp_path):
    """Not a header on its own. `""` already means "fall back to the wave
    slice", and a lone header saying one of three would be a map with no map in
    it — worse than the repo-wide block it displaced."""
    smap = _three_module_tree(tmp_path)
    assert symbols.focus(smap, [tmp_path / "core.py"], budget=8) == ""


def test_one_map_serves_many_units_without_re_parsing(tmp_path, monkeypatch):
    """`build_map` is separated from `focus` for a cost reason, and a change
    that quietly re-scanned per unit would restore the cost it was split to
    avoid — silently, since every slice would still be correct."""
    smap = _three_module_tree(tmp_path)
    calls = []
    monkeypatch.setattr(symbols, "scan",
                        lambda root: calls.append(root) or [])

    for rel in ("core.py", "web.py", "lonely.py"):
        symbols.focus(smap, [tmp_path / rel])

    assert calls == [], "focus parsed the tree again"
