---
name: mindhive-python-tdd
description: >-
  Test-first practice for the MindHive codebase: the autouse isolated store,
  injectable collector roots, fixture-builder helpers, CLI exit-code tests,
  discriminating tests, canary tests against vacuous negatives, and the
  worktree PYTHONPATH rule. Use when writing or reviewing tests/ code.
origin: mindhive
pack: mindhive-python
role: tdd
kind: tdd
---

# MindHive Python TDD

Red first, then the module. The suite's job is to encode contracts — the
frozen-dataclass pipelines, exit codes, and total-read behavior the sibling
patterns file describes — so drift breaks tests, not users.

## When to Activate

- Writing a new test file under `tests/` or extending an existing one
- Building a new analyzer feature test-first (the `test_packs.py` playbook)
- Testing CLI handlers, exit codes, or subcommand registration
- A test needs filesystem fixtures, fake stores, or fake skill trees
- A test passed suspiciously on the first try and needs a canary
- Running the suite from a linked worktree (the import trap)

## The isolated store: every test, automatically

`tests/conftest.py` isolates state for the entire suite with one autouse
fixture — no test file opts in, none can forget:

```python
@pytest.fixture(autouse=True)
def isolate_store(tmp_path, monkeypatch):
    monkeypatch.setenv("MINDHIVE_HOME", str(tmp_path))
    monkeypatch.setenv("MINDHIVE_PROJECT", "test")
    # Never register a REAL Windows scheduled task from inside the suite.
    monkeypatch.setenv("MINDHIVE_AUTORESUME_TIMER", "off")
```

Because `paths.*` reads these env vars at call time (not import time), setting
them per-test is sufficient — `paths.project_dir("test")` lands in the
throwaway `tmp_path` and the live `~/.mindhive` ledger is untouchable. Two
consequences:

```python
# 1. A test that hand-builds a "~/.mindhive" path has ESCAPED isolation —
#    that is the exact hazard the sibling security file flags; always go
#    through paths.project_dir().
# 2. Tests that need a variant layout set their own env in-body; monkeypatch
#    applies after the fixture, so in-body values win by design.
```

## Injectable roots: collectors take their filesystem as a parameter

Stage-1 collectors (the only I/O stage in the analyzer pipeline) expose every
root they walk:

```python
# context.collect_claude(home=..., cache_dir=...)   — tests build a fake home
# packs.collect_pack(skills_root, decl)             — tests build a fake tree
# packs.analyze(manifest_path, skills_root=..., env=...)  — even os.environ
#                                                     is injectable
```

Testing a collector therefore never monkeypatches `Path.home` or the real
environment — it passes a `tmp_path` root. When you add a collector, the
injectable-root parameter is part of the definition of done; a collector that
can only walk the real machine is untestable by construction.

## Fixture builders: valid-by-default, corrupted-on-purpose

`tests/test_packs.py` is the house exemplar. It centralizes fixture creation
in tiny builders that produce *valid* inputs, so each test corrupts exactly
one thing:

```python
def make_manifest(tmp_path, **overrides) -> Path:
    data = {...complete valid manifest...}
    data.update(overrides)                    # each test breaks ONE field
    return _write(tmp_path / "packs.json", json.dumps(data))

def make_skill(root, pack, role, *, body=None, front=None) -> Path:
    ...valid frontmatter + template-conformant body by default...

def make_tree(tmp_path, **manifest_overrides):
    ...manifest + a fully valid two-pack skill tree...
```

Two properties worth copying:

```python
# Uniqueness by construction: generated filler embeds pack+role in every
#   line, so fixture packs can never trip the similarity gate by accident —
#   fixtures must not manufacture the defect they exist to detect.
# One corruption per test: `dict(manifest, min_lines=500)` or a single
#   replaced heading. When the assertion fires, the cause is unambiguous.
```

The first test in a suite pins the REAL artifact, not a fixture:
`test_load_real_ws1_manifest` loads the committed `docs/packs/packs.json` and
asserts its actual thresholds — so the manifest and the loader can never
drift apart silently.

## CLI tests: call main(), assert the integer

Handlers return `int | None` and `main()` maps `None` to 0 (the contract from
the sibling patterns file). Tests exercise the real argv path:

```python
from mindhive import __main__ as cli_mod

def test_cli_check_exit_codes(tmp_path):
    ...lay fixture out in repo shape...
    assert cli_mod.main(["packs", "check", "--manifest", str(mp)]) == 0
    (repo / ".claude" / "skills" / "beta-patterns" / "SKILL.md").unlink()
    assert cli_mod.main(["packs", "check", "--manifest", str(mp)]) == 1
```

Rules: assert the exact integer (0, 1, 2 all mean different documented
things); parse `--json` output with `json.loads` inside the test (a test that
only checks "something printed" tests nothing); and when the CLI path derives
locations from an input file (manifest → skills root), the fixture must
mirror the real repo shape (`docs/packs/packs.json` beside `.claude/skills/`)
— that layout IS the contract under test.

## Discriminating tests: make the wrong implementation fail

A test discriminates only if a plausible wrong implementation fails it. The
recorded lesson: a median test built on a 9-element (odd) list passed for both
the correct median and a wrong upper-median formula — both agree on odd
lengths. The fix was an 8-element list where true median is 30 but
upper-median is 50.

```python
# Before trusting a test, name the wrong implementation it kills:
# - even-length list        -> kills upper-median
# - 100% vs 0% overlap pair -> kills inverted similarity comparisons
# - threshold-boundary case -> kills off-by-one on > vs >=
# If you cannot name one, the test is documentation, not verification.
```

## Canary tests: a negative assertion must be provably armed

The recorded CI lesson: `assert not any(dead_path_findings)` passed vacuously
on Linux — the path-regex matched nothing there, so the rule *couldn't* fire
and the "negative" test proved only that. The canary pattern arms every
negative:

```python
def test_similarity_internal_excludes_shared_preamble_lines(tmp_path):
    ...two files whose ONLY shared content is preamble truth...
    # The negative: with exclusion, the pair passes.
    assert by_rule(gate(files, {}, manifest), "similarity-internal") == []
    # The CANARY: with exclusion disabled and the threshold floored, the
    # same pair MUST trip — proving the gate can fire on this input at all.
    stripped = dict(manifest, shared_preamble_markers=[],
                    similarity_internal_same_role_max_pct=1.0)
    assert errors(gate(files, {}, stripped))
```

Every "the gate stays silent here" test gets a sibling proving the gate is
capable of noise on that input. Silence is only meaningful from something
that can speak.

## Testing totality

The total-read contract (malformed input → Finding or skip, never a raise) is
asserted directly, one test per failure shape:

```python
def test_load_manifest_missing_is_a_finding_not_a_raise(tmp_path): ...
def test_load_manifest_corrupt_is_a_finding_not_a_raise(tmp_path): ...
# and for collectors: a directory where a file should be, an unreadable
# file, invalid UTF-8 (errors="replace" keeps it measurable).
```

The assertion style matters: check the returned Finding's `rule` and
`severity`, not just "no exception" — a function that swallows errors into
nothing also raises no exception, and that is a worse bug.

## The worktree PYTHONPATH rule

The editable `mindhive` install resolves `import mindhive` to the MAIN
checkout. Run from a linked worktree, the in-process suite is safe (the root
`conftest.py` puts this checkout's `src` first), but any SUBPROCESS — pytest
re-invocations, `python -m mindhive` calls, hook shims — imports the wrong
code unless told otherwise:

```bash
# Git Bash, from the worktree — the shape every WS used:
PYTHONPATH="<worktree>/src" py -m pytest tests/test_packs.py -q
PYTHONPATH="<worktree>/src" py -m mindhive packs check
```

Symptom of forgetting: a brand-new module "doesn't exist" (`ImportError` from
the subprocess) or a just-fixed bug "still reproduces" — you are testing the
main checkout. Check `Path(module.__file__)` in the failing process before
debugging anything else.

## Case study: the validator's red-first landing

The pack validator (packs.py) is the most recent full test-first landing,
and its sequence is the template:

```text
1. RED    tests/test_packs.py written complete — 34 tests encoding the
          spec — before packs.py existed. First run: collection error
          (`from mindhive import packs` ImportError). Collection failure
          IS a valid red: it proves the suite demands the module.
2. GREEN  packs.py implemented against the failing suite. First full run:
          33 passed, 1 skipped — zero test edits during implementation.
          When a test fails at this stage, suspect the test exactly as
          hard as the code (the median lesson below came from a test bug).
3. HONEST SKIP  the 34th test exercises the real repo manifest and skips
          cleanly while content is unauthored — a skip DESIGNED to flip to
          active later, named in every suite-tail read.
4. GATE   the feature validates itself: `hive packs check` red on the real
          repo (8 missing skills) was the CORRECT result and got reported
          as such. Green-at-any-cost is the anti-pattern; the loop's word
          for a truthful red is honest-red.
```

The transferable rules: write the whole suite first (partial suites bend to
match the implementation); let the first test pin a real committed artifact;
design skips that activate themselves; and never edit a red test into green
without naming the bug — in the test or the code — that made it red.

## Testing folds and projections

Projection code (STATE/briefing builders, stats aggregation) tests as pure
folds over a golden event list — no store, no I/O:

```python
GOLDEN = [
    {"ts": "2026-01-01T00:00:00Z", "session_id": "s1",
     "type": "caveat", "payload": {"text": "lesson one"}, "tags": []},
    {"ts": "2026-01-02T00:00:00Z", "session_id": "s1",
     "type": "caveat", "payload": {"text": "lesson two"}, "tags": ["pinned"]},
    {"type": "not-a-registered-type", "payload": None},      # must be skipped
    "not even a dict",                                       # must be skipped
]

def test_fold_caps_and_survives_junk():
    state = project_state(iter(GOLDEN))
    assert state["lessons"][-1] == "lesson two"     # order preserved
    # malformed rows vanish silently — totality is part of the contract
```

Three cases every fold test file carries: the cap boundary (CAP and CAP+1
items — off-by-one in slicing is the classic), the pinned-survives-rotation
case, and the junk-row case above. Golden lists live in the test file, not
fixtures on disk — a fold's input contract is data, and inlining it keeps
the test readable as a specification.

## Testing subprocess-calling code

Code that shells out (git resolution, task scheduling) is tested through
behavior, not mocks-of-subprocess:

```python
def test_memory_folder_falls_back_off_repo(tmp_path):
    # No .git anywhere under tmp_path: the git call fails, and the
    # documented fallback (cwd) must engage — the code path where
    # subprocess.run returns nonzero is exercised for real.
    assert context._memory_folder(cwd=tmp_path) == str(tmp_path)

def test_memory_folder_resolves_main_repo(tmp_path):
    # A REAL git repo in the sandbox: cheap, honest, no mocking.
    subprocess.run(["git", "init", "-q", str(tmp_path / "repo")], check=True)
    ...
```

Rules: prefer building a tiny real environment (git init in tmp_path costs
milliseconds) over patching `subprocess.run` — a patched run test encodes
your guess about argv, not the contract. Reserve monkeypatching for the
unbuildable (a hung binary, a permission error), and when you do patch,
patch at the module seam (`context.subprocess.run`) so the test names the
importing module it constrains. Timeouts are part of the contract: a test
may assert the call site passes one without asserting its exact value.

## What not to test

Suite time is a budget; these are the recorded non-goals:

```python
# argparse mechanics       — choices=, required=; stdlib's contract, not ours.
#   Test the HANDLER's exit codes, not that argparse rejects a bad flag.
# stdlib behavior          — json round-trips, pathlib semantics.
# print formatting details — assert presence of load-bearing substrings
#   ("NOT OK", the numbers), never full rendered output; renders change.
# private helpers directly — through the public gate/collector, or the
#   refactor that inlines the helper breaks tests that shouldn't care.
# the live store           — never; isolation is not optional (autouse).
```

The smell that one of these is being tested anyway: a refactor with zero
behavior change goes red. That test was coupled to implementation, and the
fix is deleting or re-aiming it, not preserving it.

## Coverage expectations by module kind

```python
# Gate/validator modules (packs, enforce-style): every rule gets its
#   fire-and-stay-silent pair + a canary; exit codes exact.  ~95%+
# Collectors: happy path + each failure shape (missing/unreadable/corrupt)
#   + injectable-root proof.                                  ~90%
# Projections/folds: golden-input fold + cap boundary + malformed-row skip.
# CLI wiring: one exit-code test per documented code; parsing itself is
#   argparse's problem, not ours.
```

Ratchet, don't chase: the suite (1,600+ tests) grows with every feature; a
new module that lands below its kind's bar needs a written reason in the PR.

## The practical invocations

The pytest forms this repo actually runs, and when each is the right one:

```bash
PYTHONPATH="$PWD/src" py -m pytest -q                     # THE gate: whole
                                                          # suite, quiet tail
PYTHONPATH="$PWD/src" py -m pytest tests/test_packs.py -x -q
                                                          # iteration: one
                                                          # file, stop at
                                                          # first failure
PYTHONPATH="$PWD/src" py -m pytest --lf -q                # triage: rerun
                                                          # last failures only
PYTHONPATH="$PWD/src" py -m pytest -k "similarity" -q     # slice by name
                                                          # across files
PYTHONPATH="$PWD/src" py -m pytest \
    "tests/test_packs.py::test_cli_check_exit_codes" -q   # exactly one test
```

Rules of use: `-x` and `--lf` are development tools, never gate evidence;
`-k` slices are for locating, not for proving (a green slice says nothing
about the rest); and the quiet `-q` tail is what gets pasted into
verification reports — counts, skips, duration, verbatim.

## Naming and layout conventions

The suite's readability contract, as practiced across its 70+ files:

```python
# File mapping: tests/test_<module>.py mirrors src/mindhive/<module>.py.
#   Cross-module integration tests earn their own file with a name that
#   says which seam they cover, not a grab-bag "test_misc.py".

# Test names read as specifications, three segments:
def test_load_manifest_missing_is_a_finding_not_a_raise(): ...
#        ^unit          ^scenario  ^expected outcome
#   The name should let a failure report be understood WITHOUT opening
#   the file — "test_edge_case_2" fails that bar by construction.

# Module-level docstring states what contract family the file encodes and
#   points at the spec/PRD it enforces — the packs suite opens by naming
#   docs/packs/SPEC.md as the thing it encodes.

# Helpers before tests: _write(), builders, and constants (REPO,
#   REAL_MANIFEST) sit at the top under a "fixture builders" banner;
#   a reader meets the vocabulary before the sentences.

# Arrange-act-assert separated by blank lines inside each test; comments
#   only where the arrangement itself needs justifying (why 8 elements,
#   why this exact threshold) — the discriminating-test rationale lives
#   NEXT TO the magic value it explains.
```

One convention with teeth: a test asserting on a Finding always matches on
`rule` (the stable id), never on message prose — messages are for humans
and may be reworded; rules are contract.

## Quick Reference

| Mechanic | Use |
|---|---|
| `isolate_store` (autouse) | every test runs in a throwaway namespace; live ledger untouchable |
| injectable roots (`home=`, `skills_root=`, `env=`) | collectors take their filesystem as parameters |
| `make_manifest` / `make_skill` / `make_tree` | valid-by-default builders; corrupt exactly one thing per test |
| first-test-pins-real-artifact | the committed manifest and its loader cannot drift apart |
| `cli_mod.main([...]) == N` | exact exit-code assertions through the real argv path |
| even-length median / boundary cases | discriminating tests that kill the plausible wrong implementation |
| canary beside every negative | prove the gate CAN fire before trusting that it didn't |
| totality tests assert the Finding | swallowed-into-nothing is worse than raised |
| `PYTHONPATH=<worktree>/src` | subprocesses import THIS checkout, not the editable main install |
| assert on `rule`, not message prose | messages reword freely; rule ids are the stable contract |
| golden event lists inline | a fold's input contract reads as data inside its own test |
| real `git init` over patched run | build the tiny world; mocks encode guesses about argv |
