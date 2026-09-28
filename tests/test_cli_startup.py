# tests/test_cli_startup.py
"""MEB K15: CLI startup is measured, not claimed.

The MEB says `hive` starts in 42 ms and that `import mindhive.__main__` pulls
in none of the heavy optional stacks. Nothing enforced either. This file holds
the two halves K15 asks for:

(a) In a CHILD interpreter with this checkout's `src` on `PYTHONPATH`,
    `import mindhive.__main__` loads none of `chromadb`, `numpy`,
    `sentence_transformers`, `torch`, `psycopg`, `psycopg_pool`, `fastapi`,
    `uvicorn`. The child prints the intersection of `sys.modules` with that
    set, and it must be empty. A canary requires the same probe to SEE modules
    that are loaded, and a second canary plants a stand-in under a heavy name
    and requires the probe to report it -- an intersection that is empty
    because it was computed wrongly would otherwise pass forever.
(b) The median import time over a few real children is measured and printed
    (`pytest -s`), and only its existence is asserted: a positive, finite
    number. The 45 ms figure is not a gate here either -- a machine with an
    antivirus scanner or a cold disk is a condition, not a failure. This is
    the ONE real-process run of the harness; `tests/test_bench_scenarios.py`
    drives it through the spawn seam.

If (a) fails, the chain that pulls the module in is found with
`python -X importtime -c "import mindhive.__main__"`, and the fix belongs in
the module that imports at top level what it should import at call time.
"""
import json
import math
import os
import subprocess
import sys
from pathlib import Path

from mindhive import bench_scenarios as bs

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src"

# The MEB's list (K15): none of these may be imported by `import mindhive.__main__`.
HEAVY = ("chromadb", "numpy", "sentence_transformers", "torch", "psycopg",
         "psycopg_pool", "fastapi", "uvicorn")
# Modules the same import DOES load; the probe has to see these or it sees nothing.
CANARY = ("json", "mindhive.cliparse", "mindhive.paths")

PROBE = """
import json, sys
import mindhive.__main__ as m
heavy = %r
canary = %r
print(json.dumps({"heavy": sorted(set(sys.modules) & set(heavy)),
                  "canary": [name for name in canary if name in sys.modules],
                  "file": m.__file__}))
""" % (HEAVY, CANARY)


def _child_env() -> dict:
    """This checkout's `src` first on PYTHONPATH -- the editable install points
    at the main checkout, so without this a child measures the wrong code --
    and PYTHONDONTWRITEBYTECODE=1 (MEB K14) through the house helper."""
    env = bs.child_env()
    env["PYTHONPATH"] = str(SRC) + os.pathsep + env.get("PYTHONPATH", "")
    return env


def _probe(code=PROBE) -> dict:
    proc = subprocess.run([sys.executable, "-c", code], capture_output=True,
                          text=True, env=_child_env(), cwd=str(REPO), timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return json.loads(proc.stdout.strip().splitlines()[-1])


# --- (a) the import loads none of the heavy stacks ----------------------------

def test_importing_the_cli_loads_none_of_the_heavy_modules():
    got = _probe()
    assert got["heavy"] == [], (
        f"`import mindhive.__main__` loaded {got['heavy']}. Find the chain with "
        f"`python -X importtime -c \"import mindhive.__main__\"` and move that "
        f"import to call time in the module that pulls it in at top level.")
    # The probe measured THIS checkout, not the editable install.
    assert Path(got["file"]).resolve() == (SRC / "mindhive" / "__main__.py").resolve()


def test_the_probe_sees_modules_that_are_loaded():
    """CANARY: the probe must report the modules the import DOES load, or the
    empty intersection above proves nothing."""
    assert _probe()["canary"] == list(CANARY)


def test_the_heavy_check_can_fire():
    """CANARY: a stand-in planted under a heavy name in the child must be
    reported -- without importing the real package, which CI does not have."""
    planted = ("import sys, types\n"
               "sys.modules['numpy'] = types.ModuleType('numpy')\n" + PROBE)
    assert _probe(planted)["heavy"] == ["numpy"]


# --- (b) the median import time, measured and printed -------------------------

def test_the_median_startup_time_is_measured_and_printed():
    report = bs.run_cli_startup(reps=3, env=_child_env())
    text = bs.render_cli_startup(report)
    print("\n" + text)
    assert report["failed"] == 0, text
    assert report["conditions"]["pythonpath"].split(os.pathsep)[0] == str(SRC)
    assert report["conditions"]["dont_write_bytecode"] == "1"
    for row in report["rows"]:
        assert (row["ok"], row["total"]) == (3, 3), text
        assert isinstance(row["median_ms"], float), text
        assert row["median_ms"] > 0 and math.isfinite(row["median_ms"]), text
        assert row["min_ms"] <= row["median_ms"] <= row["max_ms"], text
