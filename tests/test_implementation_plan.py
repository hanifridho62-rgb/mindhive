# tests/test_implementation_plan.py — blueprint P11: the closing document.
"""`IMPLEMENTATION_PLAN.md` closes the blueprint (RP P11; MEB §3, §9): the
traceability matrix of v1.0 and v1.1, an audit table whose statuses come from
a pytest run that was actually read, the MEB forecast-vs-actual LOC table, the
measured ISO/IEC 25010 matrix and the two benches.

A document like this goes stale in two ways: quietly (a path it cites is
renamed, a status word is invented) and loudly (the whole file is forgotten).
These tests hold it to the tree for the quiet ways:

* the file sits at the repository root, README points at it, and it names the
  commit its numbers were measured at -- a commit this repository has;
* the audit table carries the prescribed header, four cells per row, and a
  status drawn from a closed vocabulary, so "verified" cannot be paraphrased
  into something the pytest output never said;
* every `tests/`, `src/mindhive/`, `hooks/`, `scripts/` and `docs/` path the
  audit table cites exists;
* the LOC table copies the MEB rows `scripts/loc_report.py` carries -- label,
  baseline and target, in the MEB's order -- and its measured column is a
  number, not a claim;
* the ISO matrix and both benches are present with measured milliseconds.

The measured numbers themselves are not pinned: they are what the run gave,
and the commit line says which run.
"""
import importlib.util
import re
import subprocess
from pathlib import Path

import pytest

REPO = Path(__file__).resolve().parents[1]
PLAN = REPO / "IMPLEMENTATION_PLAN.md"
SCRIPT = REPO / "scripts" / "loc_report.py"

AUDIT_HEADER = ("| Bab / Fitur PDF | Modul / Berkas Kode Sumber | "
                "Berkas Pengujian (Tests) | Status Verifikasi |")
STATUS_WORDS = ("LULUS", "GAGAL", "DILEWATI", "TANPA TES", "DIUKUR",
                "DEVIASI", "MANUAL")
PATH_RE = re.compile(
    r"`((?:tests|src/mindhive|hooks|scripts|docs)/[\w./\-]+"
    r"|pyproject\.toml|README\.md|TODOS\.md|CHANGELOG\.md|CONTRIBUTING\.md"
    r"|LICENSE|circle\.ps1|\.github/workflows/[\w.\-]+)`")


@pytest.fixture(scope="module")
def text() -> str:
    assert PLAN.is_file(), "IMPLEMENTATION_PLAN.md is missing from the repo root"
    return PLAN.read_text(encoding="utf-8")


def _table_after(text: str, header: str) -> list:
    """The rows of the markdown table whose header line is `header`."""
    lines = text.splitlines()
    assert header in lines, f"table header not found: {header}"
    start = lines.index(header) + 2          # header, then the |---| rule
    rows = []
    for line in lines[start:]:
        if not line.startswith("|"):
            break
        rows.append([c.strip() for c in line.strip().strip("|").split("|")])
    return rows


# --- the file, its anchor and its provenance -------------------------------

def test_the_plan_sits_at_the_root_and_the_readme_points_at_it(text):
    readme = (REPO / "README.md").read_text(encoding="utf-8")
    assert "IMPLEMENTATION_PLAN.md" in readme
    assert text.startswith("# ")


def test_the_plan_names_a_commit_this_repository_has(text):
    m = re.search(r"Diukur pada commit `([0-9a-f]{7,40})`", text)
    assert m, "the plan does not say which commit its numbers were measured at"
    probe = subprocess.run(
        ["git", "-C", str(REPO), "cat-file", "-e", m.group(1) + "^{commit}"],
        capture_output=True, text=True)
    if "not a git repository" in probe.stderr:
        pytest.skip("not a git checkout")
    assert probe.returncode == 0, f"unknown commit {m.group(1)}: {probe.stderr}"


# --- the audit table --------------------------------------------------------

def test_the_audit_table_has_the_prescribed_shape_and_a_closed_vocabulary(text):
    rows = _table_after(text, AUDIT_HEADER)
    assert len(rows) >= 90, f"only {len(rows)} audit rows"
    for row in rows:
        assert len(row) == 4, f"a row with {len(row)} cells: {row[:1]}"
        assert row[3].startswith(STATUS_WORDS), \
            f"status outside the vocabulary: {row[3]!r} (row {row[0]!r})"


def test_every_path_the_audit_table_cites_exists(text):
    rows = _table_after(text, AUDIT_HEADER)
    cited = {p for row in rows for cell in row[1:3] for p in PATH_RE.findall(cell)}
    assert len(cited) >= 150, f"only {len(cited)} distinct paths cited"
    missing = sorted(p for p in cited if not (REPO / p).exists())
    assert not missing, f"cited but absent: {missing}"


def test_the_path_pattern_sees_what_the_table_writes():
    cell = ("`src/mindhive/events.py`, `tests/test_events_sec.py` (14 tes), "
            "`hooks/lib.sh`, `.github/workflows/ci.yml`, `pyproject.toml`")
    assert PATH_RE.findall(cell) == [
        "src/mindhive/events.py", "tests/test_events_sec.py", "hooks/lib.sh",
        ".github/workflows/ci.yml", "pyproject.toml"]


# --- the LOC table, the ISO matrix and the benches ---------------------------

def _loc_report():
    spec = importlib.util.spec_from_file_location("loc_report_for_the_plan", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


def test_the_loc_table_copies_the_mebs_rows_and_measures_the_rest(text):
    lr = _loc_report()
    header = ("| MEB row | MEB baseline | MEB target | Measured | vs target | "
              "Modules (MEB) | Verdict |")
    rows = _table_after(text, header)
    want = [(r["label"], str(r["baseline"]),
             "< %d" % r["target"] if r.get("target_is_max") else str(r["target"]))
            for r in lr.MEB_ROWS]
    assert [tuple(r[:3]) for r in rows] == want
    for row in rows:
        assert re.fullmatch(r"\d+", row[3]), f"measured is not a count: {row}"
        assert re.fullmatch(r"[+-]\d+", row[4]), f"delta is not signed: {row}"


def test_the_iso_matrix_and_both_benches_report_milliseconds(text):
    assert "ISO/IEC 25010" in text
    for bench in ("cli-startup", "recall-semantic"):
        i = text.find(f"hive bench {bench}")
        assert i >= 0, f"bench {bench} is not in the plan"
        window = text[i:i + 1500]
        assert re.search(r"\d+(?:[.,]\d+)?\s*ms", window), \
            f"no measured milliseconds near `hive bench {bench}`"
