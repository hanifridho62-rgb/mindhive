# tests/test_loc_targets.py — blueprint P11: the MEB's LOC forecast, measured.
"""`scripts/loc_report.py` sets the MEB's section 3.1 forecast beside the tree.

The MEB forecast lengths; it never measured them. These tests hold the report
to three things and nothing more:

* **One answer to "how long".** The script counts with `review._line_count`'s
  rule (re-implemented so it runs without the package), and every module in
  `src/mindhive` gets the same number from both -- the number
  `tests/test_file_size_cap.py` enforces.
* **The MEB copied, not paraphrased.** Its fifteen rows, baselines, targets
  and stated totals are pinned literally; the report recomputes the totals
  from the rows (they do not add up) instead of repeating them.
* **Only the HARD ceilings fail.** `--check` exits 1 for `__main__.py` at 90
  or more, `orchestrator/lintester.py` above 997 (zero-edit), a shrink-only
  module above its recorded count, or any other module above 1000 -- and those
  ceilings are held equal to the ones the repo already enforces, so there is
  one ceiling, not two. A row over its FORECAST is reported as `over` and
  never fails a run: the forecasts are the MEB's guesses, and the measured
  deviations are recorded in `IMPLEMENTATION_PLAN.md` rather than hidden.

Every emptiness claim ("no breach", "nothing missing") has a canary on a fake
tree that proves the check can fire.
"""
import importlib.util
import json
from pathlib import Path

import pytest

from mindhive import replenish, review

REPO = Path(__file__).resolve().parents[1]
SCRIPT = REPO / "scripts" / "loc_report.py"
SRC = REPO / "src" / "mindhive"


def _load(path: Path, name: str):
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


@pytest.fixture(scope="module")
def lr():
    return _load(SCRIPT, "loc_report_under_test")


def _tree(root: Path, files: dict) -> Path:
    """A fake checkout: `files` maps a path under src/mindhive to a line count."""
    src = root / "src" / "mindhive"
    for name, n in files.items():
        path = src / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text("x = 1\n" * n, encoding="utf-8")
    return root


# --- one counter -----------------------------------------------------------

def test_every_module_gets_the_size_caps_count(lr):
    modules = sorted(SRC.rglob("*.py"))
    assert len(modules) > 100, "canary: the walk must see the real package"
    differ = {p.relative_to(SRC).as_posix(): (lr.line_count(p), review._line_count(str(p)))
              for p in modules if lr.line_count(p) != review._line_count(str(p))}
    assert not differ


@pytest.mark.parametrize("body, want", [
    (b"a\nb\n", 2), (b"a\nb", 2), (b"a\r\nb\r\n", 2), (b"", 0), (b"\n\n", 2)])
def test_the_counter_agrees_on_the_edge_spellings(lr, tmp_path, body, want):
    path = tmp_path / "m.py"
    path.write_bytes(body)
    assert lr.line_count(path) == review._line_count(str(path)) == want


def test_an_unreadable_path_counts_zero_like_the_cap_does(lr, tmp_path):
    missing = tmp_path / "gone.py"
    assert lr.line_count(missing) == review._line_count(str(missing)) == 0


# --- one ceiling -----------------------------------------------------------

def test_the_ceilings_are_the_ones_the_repo_already_enforces(lr):
    size_cap = _load(REPO / "tests" / "test_file_size_cap.py", "size_cap_for_loc")
    reexport = _load(REPO / "tests" / "test_commands_reexport.py", "reexport_for_loc")
    assert lr.SHRINK_ONLY == size_cap.ALLOWED
    assert lr.CAP == replenish.LINE_THRESHOLD == size_cap.CAP
    assert lr.MAIN_CAP == reexport.MAIN_CAP == 90
    assert lr.LINTESTER_CEILING == 997


def test_the_real_tree_breaks_no_hard_ceiling(lr, capsys):
    assert lr.hard_breaches(REPO) == []
    assert lr.main(["--root", str(REPO), "--check"]) == 0
    assert "BREACH" not in capsys.readouterr().out


@pytest.mark.parametrize("name, ok, bad", [
    ("__main__.py", 89, 90),
    ("orchestrator/lintester.py", 997, 998),
    ("replenish.py", 2781, 2782),
    ("mindnet/controller.py", 1016, 1017),
    ("some_new_module.py", 1000, 1001),
    ("commands/parsers/deep.py", 1000, 1001),
])
def test_each_hard_ceiling_fires_one_line_over_and_not_at_it(lr, tmp_path, capsys,
                                                             name, ok, bad):
    at = _tree(tmp_path / "at", {name: ok})
    assert lr.hard_breaches(at) == []
    assert lr.main(["--root", str(at), "--check"]) == 0
    over = _tree(tmp_path / "over", {name: bad})
    breaches = lr.hard_breaches(over)
    assert [(b[0], b[1]) for b in breaches] == [(name, bad)]
    capsys.readouterr()
    assert lr.main(["--root", str(over), "--check"]) == 1
    assert f"BREACH {name} {bad}" in capsys.readouterr().out


def test_a_shrink_only_module_is_not_also_held_to_the_plain_cap(lr, tmp_path):
    assert lr.hard_breaches(_tree(tmp_path, {"workspace.py": 1462})) == []


def test_without_check_a_breach_is_printed_but_never_fails(lr, tmp_path, capsys):
    root = _tree(tmp_path, {"some_new_module.py": 1500})
    assert lr.main(["--root", str(root)]) == 0
    assert "BREACH some_new_module.py 1500" in capsys.readouterr().out


def test_a_root_without_the_package_is_refused(lr, tmp_path, capsys):
    assert lr.main(["--root", str(tmp_path)]) == 2
    assert "no src/mindhive" in capsys.readouterr().err


# --- the MEB, verbatim -----------------------------------------------------

MEB_TABLE = [
    ("src/mindhive/__main__.py", 4237, 90),
    ("src/mindhive/lintester.py", 997, 997),
    ("src/mindhive/replenish.py", 2781, 2781),
    ("src/mindhive/workspace.py", 1462, 1462),
    ("src/mindhive/research.py", 1353, 1353),
    ("src/mindhive/mindnet/controller.py", 1016, 1016),
    ("src/mindhive/events.py", 684, 745),
    ("src/mindhive/paths.py", 118, 135),
    ("src/mindhive/cliparse.py", 95, 108),
    ("src/mindhive/vectors.py", 380, 475),
    ("src/mindhive/tasktool.py", 462, 498),
    ("src/mindhive/recommend.py", 285, 340),
    ("Modul-Modul Baru (14 Modul)", 0, 2885),
    ("Subpaket commands/ (8 Modul)", 0, 2450),
    ("Subpaket commands/parsers/ (6 Modul)", 0, 1180),
]


def test_the_meb_table_is_copied_not_paraphrased(lr):
    assert [(r["label"], r["baseline"], r["target"]) for r in lr.MEB_ROWS] == MEB_TABLE
    assert lr.MEB_STATED_TOTALS == {"baseline": 14771, "target": 16126}
    bounds = {r["label"]: (r.get("module_count"), r.get("module_bound"))
              for r in lr.MEB_ROWS if r.get("module_count")}
    assert bounds == {"Modul-Modul Baru (14 Modul)": (14, 999),
                      "Subpaket commands/ (8 Modul)": (8, 400),
                      "Subpaket commands/parsers/ (6 Modul)": (6, 250)}


def test_the_meb_totals_are_recomputed_from_its_own_rows(lr):
    totals = lr.meb_totals(lr.report_rows(REPO))
    assert totals["computed_baseline"] == 13870 != totals["stated_baseline"]
    assert totals["computed_target"] == 16515 != totals["stated_target"]
    text = lr.render_markdown(lr.report_rows(REPO), totals)
    assert "stated 14771 -> 16126; its rows sum to 13870 -> 16515" in text


def test_every_file_a_row_names_exists_in_this_tree(lr):
    rows = lr.report_rows(REPO)
    assert [r["label"] for r in rows] == [label for label, _, _ in MEB_TABLE]
    assert {r["label"]: r["missing"] for r in rows if r["missing"]} == {}
    assert all(r["modules"] >= 1 for r in rows)


def test_a_renamed_file_shows_as_missing_not_as_zero(lr, tmp_path):
    rows = {r["label"]: r for r in lr.report_rows(_tree(tmp_path, {"events.py": 10}))}
    assert rows["src/mindhive/paths.py"]["verdict"] == "missing"
    assert rows["src/mindhive/paths.py"]["missing"] == ["paths.py"]
    assert rows["src/mindhive/events.py"]["verdict"] == "met"


# --- verdicts ----------------------------------------------------------------

@pytest.mark.parametrize("files, label, verdict", [
    ({"events.py": 745}, "src/mindhive/events.py", "met"),
    ({"events.py": 746}, "src/mindhive/events.py", "over"),
    ({"__main__.py": 89}, "src/mindhive/__main__.py", "met"),
    ({"__main__.py": 90}, "src/mindhive/__main__.py", "over"),
    ({"research.py": 1300}, "src/mindhive/research.py", "shrank"),
    ({"research.py": 1353}, "src/mindhive/research.py", "met"),
    ({"events.py": 10}, "src/mindhive/events.py", "met"),
])
def test_a_row_is_met_over_or_shrank_by_its_own_target(lr, tmp_path, files, label, verdict):
    rows = {r["label"]: r for r in lr.report_rows(_tree(tmp_path, files))}
    assert rows[label]["verdict"] == verdict


def test_a_group_names_each_module_over_the_mebs_per_module_bound(lr, tmp_path):
    root = _tree(tmp_path, {"commands/parsers/a.py": 250, "commands/parsers/b.py": 251,
                            "commands/c.py": 401})
    rows = {r["label"]: r for r in lr.report_rows(root)}
    parsers = rows["Subpaket commands/parsers/ (6 Modul)"]
    assert parsers["over_bound"] == {"commands/parsers/b.py": 251}
    assert parsers["verdict"] == "over-bound"
    commands = rows["Subpaket commands/ (8 Modul)"]
    assert commands["over_bound"] == {"commands/c.py": 401}
    text = lr.render_markdown(list(rows.values()), lr.meb_totals(list(rows.values())))
    assert "over 250: commands/parsers/b.py 251" in text


def test_json_carries_rows_totals_and_breaches(lr, tmp_path, capsys):
    root = _tree(tmp_path, {"events.py": 800, "big.py": 1001})
    assert lr.main(["--root", str(root), "--json"]) == 0
    doc = json.loads(capsys.readouterr().out)
    assert list(doc) == ["rows", "totals", "breaches"]
    events = next(r for r in doc["rows"] if r["label"] == "src/mindhive/events.py")
    assert (events["actual"], events["delta_vs_target"], events["verdict"]) == (800, 55, "over")
    assert doc["breaches"] == [["big.py", 1001, "over the 1000-line cap"]]
