# tests/test_mindnet_enforcer.py
"""MN-4 — the teeth. The unit that can deny a tool call.

Written TEST-FIRST against
``docs/plans/2026-07-27-001-feat-mindnet-mn3-mn4-mn5-mechanical-enforcement-plan.md``
U6. No line of `enforcer.py` existed when these were written.

## This is the most dangerous unit in the system, and the tests are shaped by that

Every other module here can only add noise to a prompt. This one can make the
repository unwritable. The plan names three independent guards, and each has a
test whose deletion would be the actual outage:

1. **`test_a_pending_task_in_brainstorm_allows_writes`** — the anti-brick guard.
   `pending` is the dataclass default for EVERY declared task (`tasks.py:161`),
   and `inject.active` deliberately includes it so the normal case gets steered.
   Reusing that predicate for DENIAL would mean one task declared months ago and
   never touched denies every write in the repo, forever, with a message
   pointing at a task the operator has forgotten. If this test ever goes red,
   the failure mode is a hard stop with no obvious cause.

2. **`test_a_write_to_the_current_phases_evidence_directory_is_allowed`** — the
   anti-deadlock guard. `brainstorm` is cleared by a file under
   `docs/superpowers/specs/`. A rule that denies writes during `brainstorm`
   without exempting that directory forbids the only action that clears the
   phase, and the state machine can never move again.

3. **`test_an_unmapped_extension_is_not_a_violation`** — unknown is not a
   violation. The language lock works off a finite extension table; treating
   everything outside it as a breach denies `.toml`, `.lock`, extensionless
   files and anything the table has not heard of.

## Fail-open, everywhere

A sidecar that is missing, stale, corrupt, or empty ALLOWS. MindNetwork is not
load-bearing for the user's ability to work, and an enforcer that fails closed
turns a projection bug into an outage. Asserted across the whole junk matrix
rather than at one representative value.

## Purity

Both predicates take an already-read `Sidecar`, so not one test here touches the
disk and the latency requirement (R8) is structural rather than timed.
"""
import pytest

from mindhive.mindnet import enforcer as enf
from mindhive.mindnet import sidecar as s_mod
from mindhive.mindnet import tasks as t_mod
from mindhive.mindnet.controller import PhaseRecord
from mindhive.mindnet.tasks import PHASES

BLOCKED = ("brainstorm", "plan", "prd")
ALLOWED = ("execution", "audit-bug", "audit-code", "audit-system", "pr-green")


# --- helpers -------------------------------------------------------------------

def _sc(phase="brainstorm", status=t_mod.STATUS_IN_PROGRESS, langs=(),
        fresh=True, task_id="t1", records=True):
    task = t_mod.Task(task_id=task_id, title="a task", status=status,
                      language_context=tuple(langs))
    recs = ({task_id: PhaseRecord(task_id=task_id, phase=phase, epoch=0)}
            if records else {})
    return s_mod.Sidecar(state=s_mod.PhaseState(recs, {}), fresh=fresh,
                         reason="" if fresh else "moved", stamp={},
                         tasks={task_id: task}, advice={})


def _write(path="src/x.py"):
    return "Write", {"file_path": path}


# --- R6: the phase block ----------------------------------------------------------

@pytest.mark.parametrize("phase", BLOCKED)
def test_a_write_is_denied_in_a_planning_phase(phase):
    reason = enf.phase_violation(*_write(), _sc(phase=phase))
    assert reason
    assert "ACCESS DENIED" in reason
    assert "MindNetwork State Machine violation" in reason
    assert phase in reason, "the message must name the phase that blocked it"


@pytest.mark.parametrize("phase", ALLOWED)
def test_a_write_is_allowed_in_a_working_phase(phase):
    assert enf.phase_violation(*_write(), _sc(phase=phase)) is None


@pytest.mark.parametrize("tool", sorted(enf.WRITE_TOOLS | enf.EXECUTE_TOOLS))
def test_every_write_and_execute_tool_is_covered(tool):
    """Enumerated from the module's own sets, so a tool added to either without
    thought fails here rather than silently escaping enforcement."""
    assert enf.phase_violation(tool, {"file_path": "src/x.py",
                                      "command": "python x.py"},
                               _sc(phase="brainstorm"))


@pytest.mark.parametrize("tool", ["Read", "Grep", "Glob", "WebFetch", "Task"])
def test_a_read_only_tool_is_never_denied(tool):
    """Research is exactly what `brainstorm` is FOR."""
    assert enf.phase_violation(tool, {"file_path": "src/x.py"},
                               _sc(phase="brainstorm")) is None


# --- KTD4: the anti-brick guard ------------------------------------------------------

def test_a_pending_task_in_brainstorm_allows_writes():
    """THE guard. `pending` is every declared task's default status; denying on
    it would brick the repository on the strength of a task nobody is working
    on. Do not delete this test."""
    assert enf.phase_violation(
        *_write(), _sc(phase="brainstorm",
                       status=t_mod.STATUS_PENDING)) is None


@pytest.mark.parametrize("status", [t_mod.STATUS_PENDING, t_mod.STATUS_READY,
                                    t_mod.STATUS_BLOCKED,
                                    t_mod.STATUS_COMPLETED,
                                    t_mod.STATUS_ABANDONED])
def test_only_an_in_progress_task_can_deny(status):
    assert enf.phase_violation(*_write(), _sc(phase="brainstorm",
                                              status=status)) is None


def test_an_in_progress_task_is_what_denies():
    """The positive half — without it the test above passes vacuously against an
    enforcer that never denies at all."""
    assert enf.phase_violation(
        *_write(), _sc(phase="brainstorm",
                       status=t_mod.STATUS_IN_PROGRESS)) is not None


# --- KTD6: the anti-deadlock guard ---------------------------------------------------

@pytest.mark.parametrize("phase,path", [
    ("brainstorm", "docs/superpowers/specs/thing.md"),
    ("plan", "docs/plans/thing.md"),
    ("plan", "docs/superpowers/plans/thing.md"),
    ("prd", "docs/prds/thing.md"),
])
def test_a_write_to_the_current_phases_evidence_directory_is_allowed(phase,
                                                                     path):
    """The other guard that must never be deleted. Denying writes to the
    directory that CLEARS the phase makes the phase unclearable and the state
    machine unmovable."""
    assert enf.phase_violation("Write", {"file_path": path},
                               _sc(phase=phase)) is None


def test_the_exemption_is_scoped_to_the_current_phase():
    """A plan document does not clear `brainstorm`. Exempting every evidence
    directory regardless of phase would turn the whole of docs/ into a hole."""
    assert enf.phase_violation("Write", {"file_path": "docs/plans/thing.md"},
                               _sc(phase="brainstorm")) is not None


def test_the_exemption_is_read_from_the_phase_descriptor_not_a_local_list():
    """Invariant 10 — one legend. Asserted by driving the exemption from
    `phases.PHASE_EVIDENCE` itself: add a required_dir there and the enforcer
    must honour it without being edited."""
    from mindhive.mindnet import phases as ph_mod
    for phase in BLOCKED:
        for d in ph_mod.PHASE_EVIDENCE[phase]["required_dir"]:
            assert enf.phase_violation(
                "Write", {"file_path": f"{d}doc.md"},
                _sc(phase=phase)) is None


@pytest.mark.parametrize("path", [
    "docs/superpowers/specs/../../../src/evil.py",
    "docs/superpowers/specs/../../plans/sneaky.md",
])
def test_the_exemption_cannot_be_escaped_by_traversal(path):
    """`phases.resolve_ref` refuses prefix matching for exactly this reason
    (`phases.py`: 'CONTAINMENT, not prefix'). A `startswith` check here would
    reopen the hole one module over."""
    assert enf.phase_violation("Write", {"file_path": path},
                               _sc(phase="brainstorm")) is not None


def test_an_execute_tool_is_not_exempted_by_a_docs_path():
    """The exemption is about PRODUCING the evidence document. A shell command
    is not a document, whatever string it mentions."""
    assert enf.phase_violation(
        "Bash", {"command": "python docs/superpowers/specs/x.md"},
        _sc(phase="brainstorm")) is not None


# --- P0 REGRESSIONS: the three deadlocks that shipped -------------------------------
#
# All three passed review and CI. Each is here in the exact shape that let it
# through, because the original tests were green on inputs the system never
# actually produces.

@pytest.mark.parametrize("path", [
    # THE shape that matters: Write/Edit/MultiEdit REQUIRE an absolute
    # file_path, and this project's rule 12 mandates absolute paths anyway. The
    # original anti-deadlock tests were all relative, so the exemption was
    # never once exercised against a path the tool can actually emit — and it
    # failed on every one of them, denying the very document that clears the
    # phase.
    "C:/Users/hanif/mindhive/docs/superpowers/specs/thing.md",
    "C:\\Users\\hanif\\mindhive\\docs\\superpowers\\specs\\thing.md",
    "/home/u/repo/docs/superpowers/specs/thing.md",
    "docs/superpowers/specs/thing.md",
])
def test_the_evidence_exemption_survives_an_absolute_path(path):
    assert enf.phase_violation("Write", {"file_path": path},
                               _sc(phase="brainstorm")) is None


def test_an_absolute_path_outside_the_evidence_dir_is_still_denied():
    """The suffix match must not become a blanket allow."""
    assert enf.phase_violation(
        "Write", {"file_path": "C:/Users/hanif/mindhive/src/widget.py"},
        _sc(phase="brainstorm")) is not None


def test_the_phase_and_language_rules_compose_without_deadlocking():
    """P0: two individually-sound guards composing into the exact deadlock both
    were written to prevent. Every planning phase is cleared by a markdown
    document; `.md` maps to `markdown`; so any language_context not literally
    listing `markdown` made all three planning gates unclearable — the phase
    rule waved the document through and the language rule denied it one check
    later."""
    sc = _sc(phase="brainstorm", langs=("python",))
    path = "C:/Users/hanif/mindhive/docs/superpowers/specs/spec.md"
    assert enf.phase_violation("Write", {"file_path": path}, sc) is None
    assert enf.language_violation("Write", {"file_path": path}, sc) is None


@pytest.mark.parametrize("cmd", [
    "hive mindnet satisfy t1 --ref docs/superpowers/specs/spec.md",
    "hive mindnet enter t1 plan",
    "hive rules enforce --disable policy:gate:mindnet-phase",
    "hive log update \"recorded the change\"",
    "git status",
    "gh pr list",
])
def test_the_documented_escape_routes_are_reachable(cmd):
    """P0: every remedy the system itself prescribes runs through Bash, and the
    block denied Bash unconditionally — so a blocked phase could not be cleared,
    the rule could not be disabled, and the mandatory `hive log` entry could not
    be written. The block was unrecoverable without leaving the session."""
    assert enf.phase_violation("Bash", {"command": cmd},
                               _sc(phase="brainstorm")) is None


@pytest.mark.parametrize("cmd", ["python build.py", "pytest -q",
                                 "node app.js", "./configure && make"])
def test_executing_code_is_still_denied(cmd):
    """The exemption is an escape hatch, not an off switch — running code
    during a planning phase is exactly what the rule exists to stop."""
    assert enf.phase_violation("Bash", {"command": cmd},
                               _sc(phase="brainstorm")) is not None


def test_the_refusal_names_the_task_and_the_way_out():
    """A refusal an operator cannot act on is an outage. With several tasks in
    the graph, naming only the phase does not even say which task froze the
    repository."""
    reason = enf.phase_violation(*_write(), _sc(phase="brainstorm"))
    assert "'t1'" in reason
    assert "docs/superpowers/specs/" in reason
    assert "hive rules enforce --disable" in reason


@pytest.mark.parametrize("status", [t_mod.STATUS_COMPLETED,
                                    t_mod.STATUS_ABANDONED,
                                    t_mod.STATUS_BLOCKED])
def test_a_settled_task_no_longer_holds_the_language_lock(status):
    """P1: `locked_task` consulted status not at all, and the shared ordering
    prefers the task furthest along — which a COMPLETED task always is. So a
    task finished last month out-competed the live one and enforced its
    language_context on every write in the repo, forever. That is the brick the
    `in_progress` rule prevents for the phase block, applied to only one rule."""
    assert enf.language_violation("Write", {"file_path": "app.js"},
                                  _sc(status=status,
                                      langs=("python",))) is None


def test_a_live_task_still_holds_the_language_lock():
    """The canary — without it the test above passes against a lock that never
    fires at all."""
    for status in (t_mod.STATUS_PENDING, t_mod.STATUS_READY,
                   t_mod.STATUS_IN_PROGRESS):
        assert enf.language_violation("Write", {"file_path": "app.js"},
                                      _sc(status=status,
                                          langs=("python",))) is not None


@pytest.mark.parametrize("path", ["src/app.ts.", "src/app.ts  ",
                                  "src/app.ts::$DATA"])
def test_windows_path_suffixes_cannot_slip_past_the_language_lock(path):
    """Win32 strips trailing dots/spaces and treats `::` as an alternate data
    stream, so all of these create `app.ts` — but each parsed to an extension
    the table does not know, and 'unmapped never denies' waved them through."""
    assert enf.language_violation("Write", {"file_path": path},
                                  _sc(langs=("python",))) is not None


def test_a_drive_relative_prefix_is_not_invented_into_evidence():
    """`text[1] == ':'` matched far more than a drive letter, and amputating an
    arbitrary `xx:` prefix could turn an unrelated path into something that
    looked like this phase's evidence."""
    assert enf._norm("https://x/docs/plans/y.md").startswith("https:")


# --- R7: the language lock -------------------------------------------------------

def test_a_javascript_write_is_denied_under_a_python_lock():
    reason = enf.language_violation("Write", {"file_path": "app.js"},
                                    _sc(langs=("python",)))
    assert reason
    assert "ACCESS DENIED" in reason
    assert "Language Compatibility Lock violation" in reason
    assert "python" in reason, "the message must name the permitted set"


@pytest.mark.parametrize("path", ["mod.py", "notes.md"])
def test_a_permitted_extension_is_allowed(path):
    assert enf.language_violation("Write", {"file_path": path},
                                  _sc(langs=("python", "markdown"))) is None


@pytest.mark.parametrize("path", ["app.js", "main.go", "lib.cpp", "app.rb"])
def test_every_forbidden_extension_is_denied(path):
    assert enf.language_violation("Write", {"file_path": path},
                                  _sc(langs=("python",))) is not None


def test_an_empty_language_context_locks_nothing():
    """An unpopulated lock is not a lock — the overwhelmingly common case."""
    assert enf.language_violation("Write", {"file_path": "app.js"},
                                  _sc(langs=())) is None


def test_extension_matching_is_case_insensitive():
    assert enf.language_violation("Write", {"file_path": "APP.JS"},
                                  _sc(langs=("python",))) is not None


def test_an_unmapped_extension_is_not_a_violation():
    """Unknown is not a violation. The table is finite; the file types a repo
    contains are not."""
    for path in ("pyproject.toml", "uv.lock", "Makefile", "data.parquet"):
        assert enf.language_violation("Write", {"file_path": path},
                                      _sc(langs=("python",))) is None


def test_an_extensionless_path_is_not_a_violation():
    assert enf.language_violation("Write", {"file_path": "LICENSE"},
                                  _sc(langs=("python",))) is None


def test_the_language_lock_is_case_insensitive_about_the_context_itself():
    assert enf.language_violation("Write", {"file_path": "app.js"},
                                  _sc(langs=("Python",))) is not None


def test_an_execute_tool_is_not_language_locked():
    """Deferred by the plan: inferring a language from an arbitrary shell
    command is a parsing problem with a false-positive cost paid on every
    command."""
    assert enf.language_violation("Bash", {"command": "node app.js"},
                                  _sc(langs=("python",))) is None


def test_the_language_lock_ignores_task_status():
    """Unlike the phase block, the lock is a property of the TASK's declared
    scope, not of whether someone is actively working it. A task restricted to
    Python does not become polyglot by being parked."""
    assert enf.language_violation("Write", {"file_path": "app.js"},
                                  _sc(langs=("python",),
                                      status=t_mod.STATUS_PENDING)) is not None


# --- R9: fail-open across the whole junk matrix -----------------------------------

def test_a_stale_sidecar_allows():
    """Same argument as `inject`'s stale branch: a projection that cannot prove
    it is current must not be the basis for a refusal."""
    assert enf.phase_violation(*_write(), _sc(fresh=False)) is None
    assert enf.language_violation("Write", {"file_path": "app.js"},
                                  _sc(langs=("python",),
                                      fresh=False)) is None


def test_a_sidecar_with_no_records_allows():
    assert enf.phase_violation(*_write(), _sc(records=False)) is None


def test_an_empty_sidecar_allows():
    empty = s_mod.Sidecar(s_mod.empty_state(), True, "", {}, {}, {})
    assert enf.phase_violation(*_write(), empty) is None
    assert enf.language_violation("Write", {"file_path": "app.js"},
                                  empty) is None


@pytest.mark.parametrize("sc", [None, "string", 42, object()])
def test_a_junk_sidecar_allows(sc):
    assert enf.phase_violation(*_write(), sc) is None
    assert enf.language_violation("Write", {"file_path": "a.js"}, sc) is None


@pytest.mark.parametrize("tool_input", [None, "string", 42, {},
                                        {"file_path": None},
                                        {"file_path": 42},
                                        {"other": "x"}])
def test_a_junk_tool_input_does_not_raise(tool_input):
    assert enf.language_violation("Write", tool_input,
                                  _sc(langs=("python",))) is None


def test_a_write_with_no_path_still_hits_the_phase_block():
    """The phase rule is about the ACT of writing, not about the target. A
    missing path must not become an escape hatch."""
    assert enf.phase_violation("Write", {}, _sc(phase="brainstorm")) is not None


# --- selection: which task decides ---------------------------------------------------

def test_no_in_progress_task_anywhere_allows():
    sc = _sc(phase="brainstorm", status=t_mod.STATUS_PENDING)
    assert enf.phase_violation(*_write(), sc) is None


def test_selection_does_not_depend_on_dict_insertion_order():
    """A denial an operator cannot reproduce is a denial they cannot clear.

    Built as TWO sidecars whose dicts are populated in opposite order, not as
    five calls against one object: `json.loads` preserves document order and
    `sidecar._decode` preserves it again, so the risk is that WRITE order
    decides the answer. Repeating a call on a single already-constructed dict
    can never surface that — Python dict iteration is stable within one object,
    so the earlier version of this test passed unconditionally."""
    def build(order):
        tasks, recs = {}, {}
        for tid, phase in order:
            tasks[tid] = t_mod.Task(task_id=tid, title=tid.upper(),
                                    status=t_mod.STATUS_IN_PROGRESS)
            recs[tid] = PhaseRecord(task_id=tid, phase=phase, epoch=0)
        return s_mod.Sidecar(s_mod.PhaseState(recs, {}), True, "", {}, tasks,
                             {})

    forward = build([("a", "brainstorm"), ("b", "prd")])
    reverse = build([("b", "prd"), ("a", "brainstorm")])
    assert enf.phase_violation(*_write(), forward) \
        == enf.phase_violation(*_write(), reverse)


def test_a_second_task_further_along_does_not_switch_the_block_off():
    """REGRESSION. This test previously asserted the OPPOSITE, and the opposite
    was the bug: routing the block through `active_task` (which returns the
    single task FURTHEST along) meant advancing any unrelated task to
    `execution` silently disabled the gate repo-wide, and finishing that task
    silently re-enabled it. The block is a property of the repository — if a
    task you picked up is still at a planning gate, producing code is out of
    order."""
    tasks = {
        "a": t_mod.Task(task_id="a", title="A",
                        status=t_mod.STATUS_IN_PROGRESS),
        "b": t_mod.Task(task_id="b", title="B",
                        status=t_mod.STATUS_IN_PROGRESS),
    }
    recs = {"a": PhaseRecord(task_id="a", phase="brainstorm", epoch=0),
            "b": PhaseRecord(task_id="b", phase="execution", epoch=0)}
    sc = s_mod.Sidecar(s_mod.PhaseState(recs, {}), True, "", {}, tasks, {})
    reason = enf.phase_violation(*_write(), sc)
    assert reason is not None
    assert "brainstorm" in reason
    assert "'a'" in reason, "the refusal must name the task that is blocking"


# --- R8: the latency contract is structural ------------------------------------------

def test_the_module_does_not_import_events_at_all():
    """R8 as a property rather than a timing bound — the same shape
    `test_the_hook_path_never_calls_read_all` uses. `PreToolUse` fires on every
    tool call; a ~264 ms ledger read there is not a slow feature, it is an
    unusable session."""
    import ast
    import inspect
    tree = ast.parse(inspect.getsource(enf))
    imported = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported |= {a.name.split(".")[0] for a in node.names}
        elif isinstance(node, ast.ImportFrom):
            imported |= {(node.module or "").split(".")[0]}
            imported |= {a.name for a in node.names}
    assert "events" not in imported, (
        "PreToolUse fires on every tool call; a ~264 ms ledger read here is "
        "not a slow feature, it is an unusable session")
    # Parsed, not grepped: the module docstring explains WHY it stays off the
    # ledger and names `read_all` doing so, and a substring check over the
    # source would fail on its own rationale.
    called = {n.attr for n in ast.walk(tree) if isinstance(n, ast.Attribute)}
    called |= {n.id for n in ast.walk(tree) if isinstance(n, ast.Name)}
    assert "read_all" not in called


@pytest.mark.parametrize("phase", PHASES)
def test_every_phase_is_classified(phase):
    """No phase may fall through unclassified — that is how a gap becomes a
    silent allow nobody notices."""
    sc = _sc(phase=phase)
    result = enf.phase_violation(*_write(), sc)
    assert (result is None) == (phase not in BLOCKED)
