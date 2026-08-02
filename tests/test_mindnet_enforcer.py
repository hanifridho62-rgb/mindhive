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


# --- R1/R2/R3: the shell path ------------------------------------------------------
#
# MN-4 deferred this whole surface, and the deferral's REASON is the shape of
# everything below: "inferring a language from an arbitrary shell command is a
# parsing problem with a false-positive cost paid on every command." That reason
# still stands, so the rule here is NOT "what language does this command imply".
# It is the narrower, decidable question the lock was always asking — does this
# command PUT A FILE ON DISK in a forbidden language — and there are exactly two
# shapes that answer it: a redirect target, and a scaffolding generator.
#
# Everything else allows. The tests are paired deliberately: for every "denies"
# there is a sibling "allows" one token away, because a rule that denies too
# much here is worse than the bypass it closes.

def test_an_interpreter_invocation_is_still_not_language_locked():
    """Was `test_an_execute_tool_is_not_language_locked`. The original asserted
    the whole deferral; this asserts the half of it that is still true and
    always will be.

    Running a program produces no source file, so it is outside what the lock
    is for. `node app.js` under a Python-only context stays allowed — denying it
    is exactly the false positive MN-4 refused to pay for."""
    assert enf.language_violation("Bash", {"command": "node app.js"},
                                  _sc(langs=("python",))) is None


@pytest.mark.parametrize("command", [
    "cat > src/app.ts <<'EOF'\nconst x = 1\nEOF",
    "echo x > src/app.ts",
    "echo x >> src/app.ts",
    "echo x | tee src/app.ts",
    "echo x | tee -a src/app.ts",
])
def test_a_redirect_that_creates_a_forbidden_file_is_denied(command):
    """The bypass MN-4 left open, and the reason this unit exists. A heredoc
    writes TypeScript into a Python-only task and the write-tool filter never
    sees it — same bytes on disk, same task, no refusal."""
    reason = enf.language_violation("Bash", {"command": command},
                                    _sc(langs=("python",)))
    assert reason, command
    assert "Language Compatibility Lock violation" in reason
    assert "python" in reason
    assert "t1" in reason, "the message must still name the task that locked it"


@pytest.mark.parametrize("command", [
    "pytest -q",
    "python -c 'print(1)'",
    "ls -la src/app.ts",
    "grep -n foo src/app.ts",
    "cat src/app.ts",
    "python x.py 2>&1",
    "python x.py 2>/dev/null",
    "echo done >&2",
])
def test_a_command_that_creates_nothing_is_allowed(command):
    """The false-positive half, and the more important one. `2>&1` and `>&2`
    are the trap: they contain `>` and are not paths, and a regex that treats
    `&1` as a filename would deny a stderr redirect on a Python-only task."""
    assert enf.language_violation("Bash", {"command": command},
                                  _sc(langs=("python",))) is None, command


@pytest.mark.parametrize("command", [
    "echo x > notes.txt",
    "echo x > out",
    "echo x > pyproject.toml",
])
def test_an_unmapped_redirect_target_is_not_a_violation(command):
    """Unmapped-never-denies has to hold over the shell path too, or the rule
    starts denying `.txt` and extensionless files."""
    assert enf.language_violation("Bash", {"command": command},
                                  _sc(langs=("python",))) is None, command


def test_a_permitted_redirect_target_is_allowed():
    assert enf.language_violation(
        "Bash", {"command": "echo x > README.md"},
        _sc(langs=("python", "markdown"))) is None


def test_a_quoted_redirect_target_is_still_seen():
    """Quoting the path must not be a bypass — it is the natural spelling for
    any path with a space in it."""
    assert enf.language_violation("Bash", {"command": 'echo x > "src/app.ts"'},
                                  _sc(langs=("python",))) is not None


@pytest.mark.parametrize("command,langs", [
    ("npx create-react-app web", ("python",)),
    ("npm init -y", ("python",)),
    ("cargo new thing", ("python",)),
    ("rails new blog", ("python",)),
    ("go mod init example.com/x", ("python",)),
])
def test_a_scaffolding_command_is_denied(command, langs):
    """R3. These are defined by generating a source tree; the files they create
    never pass through a tool call at all, so a path-based rule cannot see
    them."""
    assert enf.language_violation("Bash", {"command": command},
                                  _sc(langs=langs)) is not None, command


@pytest.mark.parametrize("command", [
    "npm run test",
    "npm install",
    "npx tsc --noEmit",
    "cargo build",
    "cargo test",
    "go mod tidy",
    "go build ./...",
])
def test_a_non_scaffolding_toolchain_command_is_allowed(command):
    """The sibling half. `go mod tidy` shares two tokens with `go mod init` and
    creates no source; matching on the program alone would deny it."""
    assert enf.language_violation("Bash", {"command": command},
                                  _sc(langs=("python",))) is None, command


def test_a_scaffolding_command_in_its_own_language_is_allowed():
    assert enf.language_violation("Bash", {"command": "npx create-react-app w"},
                                  _sc(langs=("javascript",))) is None


@pytest.mark.parametrize("command", [
    "hive mindnet satisfy t1",
    "hive log update 'wrote src/app.ts'",
    "git commit -m 'add app.ts'",
    "git checkout -- src/app.ts",
    "gh pr view",
])
def test_the_exempt_programs_survive_the_language_lock(command):
    """`EXEMPT_PROGRAMS` exists so the block is escapable from inside a session,
    and every documented remedy runs through Bash. A language rule that denied
    `hive` or `git` would re-create the unrecoverable state the phase block's
    exemption was written to prevent — and `git` writes files constantly."""
    assert enf.language_violation("Bash", {"command": command},
                                  _sc(langs=("python",))) is None, command


def test_a_redirect_into_the_current_phases_evidence_directory_is_allowed():
    """The compose-without-deadlocking property, now over the shell path.

    MN-4 already shipped this bug once in the other direction: the phase gate
    waved an evidence document through and the language lock denied it one
    check later, making all three planning gates unclearable. Writing that
    document with `>` instead of `Write` must not reopen it."""
    assert enf.language_violation(
        "Bash", {"command": "echo x > docs/superpowers/specs/design.md"},
        _sc(phase="brainstorm", langs=("python",))) is None


def test_powershell_cmdlet_file_creation_is_a_known_uncovered_gap():
    """Asserted rather than assumed. `Set-Content` puts bytes on disk and this
    iteration does not see it — PowerShell 5.1's quoting and parameter shapes
    are a separate parsing surface, deferred by the plan.

    This test exists so the gap is a recorded fact with a name. When the
    PowerShell surface is covered, this test flips to `is not None` and the
    deferral is closed on purpose rather than discovered by accident."""
    assert enf.language_violation(
        "PowerShell", {"command": "Set-Content src/app.ts 'x'"},
        _sc(langs=("python",))) is None


@pytest.mark.parametrize("tool_input", [
    None, {}, {"command": None}, {"command": ""}, {"command": 123},
    {"command": "   "}, "not-a-dict", [], 7,
])
def test_the_shell_path_is_total(tool_input):
    """Fail-open over the new surface. A malformed payload must allow, never
    raise — this decides whether a real tool call runs."""
    assert enf.language_violation("Bash", tool_input,
                                  _sc(langs=("python",))) is None


def test_a_stale_sidecar_does_not_deny_over_the_shell_path():
    assert enf.language_violation("Bash", {"command": "echo x > src/app.ts"},
                                  _sc(langs=("python",), fresh=False)) is None


def test_an_empty_language_context_locks_no_shell_command():
    """An unpopulated lock is not a lock — the same rule the write path has."""
    assert enf.language_violation("Bash", {"command": "echo x > src/app.ts"},
                                  _sc(langs=())) is None


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


# --- review fixes: the shell rule was reading `>` far too literally -------------------
#
# The first shipped version of the shell path ran its redirect regex over the RAW
# command with no notion of quoting, heredocs, or command segments. A security
# review executed it and produced a verified allow/deny table; these are the rows
# that came back wrong. Every one of them is a FALSE DENIAL — the exact
# false-positive cost MN-4 deferred this whole surface to avoid — or a chained
# bypass, so this section is the honest boundary of the rule.

@pytest.mark.parametrize("command", [
    'echo "renamed a.py -> b.js"',
    'grep -n "a -> b.md" notes.txt',
    "pytest -q -k 'x>y.js'",
    "echo 'x => y.ts'",
    'git log --format="%s -> %h" > /dev/null',
    'python -c "print(1 > 0)"',
])
def test_an_arrow_or_a_quoted_angle_bracket_is_not_a_redirect(command):
    """`->` is not a redirect and `grep` writes nothing. The first version denied
    both: `\d?>>?` matched the `>` in `->` and read the next token as a
    filename, so a read-only grep whose PATTERN mentioned a `.md` file was
    refused. Quoting is the other half — a `>` inside a quoted string is data,
    not an operator."""
    assert enf.language_violation("Bash", {"command": command},
                                  _sc(langs=("python",))) is None, command


def test_a_quoted_redirect_target_is_still_seen_after_masking():
    """The sibling that keeps the fix above from being 'ignore all quotes'. The
    OPERATOR must be outside quotes; the TARGET may be quoted, and it has to be,
    for any path containing a space."""
    assert enf.language_violation("Bash", {"command": 'echo x > "src/app.ts"'},
                                  _sc(langs=("python",))) is not None


def test_a_heredoc_body_is_not_scanned_for_redirects():
    """THE anti-deadlock regression, verified reachable by review.

    `_redirect_targets` returned every hit in the whole string, so a heredoc
    BODY that merely mentioned `echo x > src/app.ts` was collected alongside the
    real target. The real target here is this phase's evidence document and is
    correctly exempt — but `any(...)` then denied on the body hit anyway. That is
    precisely the composition the module comment promises cannot reopen, and
    this repo's own planning documents contain example text of that shape."""
    command = ("cat > docs/superpowers/specs/design.md <<'EOF'\n"
               "Example: echo x > src/app.ts\n"
               "EOF")
    assert enf.language_violation("Bash", {"command": command},
                                  _sc(phase="brainstorm",
                                      langs=("python",))) is None


def test_a_heredocs_own_target_is_still_seen():
    """The sibling. Stripping bodies must not strip the command line itself."""
    command = "cat > src/app.ts <<'EOF'\nconst x = 1\nEOF"
    assert enf.language_violation("Bash", {"command": command},
                                  _sc(langs=("python",))) is not None


@pytest.mark.parametrize("command", [
    "git status; echo x > src/app.ts",
    "hive log update ok && echo x > src/app.ts",
    "gh pr view | echo x > src/app.ts",
    "git status && npx create-react-app web",
])
def test_an_exempt_program_does_not_exempt_the_rest_of_the_chain(command):
    """`EXEMPT_PROGRAMS` was applied to the first token of the WHOLE command, so
    prefixing anything with `git status;` turned the entire rule off. The
    exemption exists so the documented remedies stay reachable — that argues for
    exempting the invocation, not everything chained after it."""
    assert enf.language_violation("Bash", {"command": command},
                                  _sc(langs=("python",))) is not None, command


@pytest.mark.parametrize("command", [
    "hive mindnet satisfy t1",
    "hive log update 'wrote src/app.ts'",
    "git commit -m 'add app.ts'",
    "git status 2>&1",
    "hive rules enforce --disable policy:gate:mindnet-language",
])
def test_the_escape_hatch_survives_segment_scoping(command):
    """The guard the fix above must not break. Every documented way out of an
    enforcement state runs through Bash, so narrowing the exemption to a segment
    must still leave a bare remedy exempt."""
    assert enf.language_violation("Bash", {"command": command},
                                  _sc(langs=("python",))) is None, command


@pytest.mark.parametrize("command", [
    "echo x >| src/app.ts",
    "echo x | tee --append src/app.ts",
    "echo x | tee -a -- src/app.ts",
])
def test_the_other_spellings_of_the_shapes_already_claimed(command):
    """Not new shapes — the SAME two shapes spelled the way a shell actually
    allows. `>|` is the clobber-override redirect and was matched by nothing;
    `tee --append` had `--append` captured AS the path, which then went to the
    extension table, came back unmapped, and allowed. A rule that claims to
    cover redirects and `tee` should cover their real spellings."""
    assert enf.language_violation("Bash", {"command": command},
                                  _sc(langs=("python",))) is not None, command


def test_redirecting_program_output_to_a_forbidden_extension_still_denies():
    """A judgment call, pinned so it is a decision rather than an accident.

    The lock asks what lands on disk, not who authored it. `python gen.py >
    report.md` creates a markdown file, and a `Write` of `report.md` under a
    python-only context is refused — denying one and allowing the other would
    make the rule depend on which tool happened to be used, which is the exact
    inconsistency this whole unit exists to remove."""
    assert enf.language_violation(
        "Bash", {"command": "python gen.py > report.md"},
        _sc(langs=("python",))) is not None
    assert enf.language_violation(
        "Bash", {"command": "python gen.py > report.md"},
        _sc(langs=("python", "markdown"))) is None


# --- MN-8a U1: the AST syntax check ------------------------------------------------
#
# The extension rule above answers "what language is this FILE NAMED". This one
# answers "is this CONTENT actually Python". They are different questions and a
# `.py` file full of JavaScript passes the first and fails the second.

VALID_PYTHON = [
    "def add(a, b): return a + b",
    "x = 1\n",
    "",                                   # an intentional empty file
    "   \n\t\n",                          # whitespace only
    "match x:\n    case 1:\n        pass\n",   # 3.11+ syntax must not be alien
    "# just a comment\n",
]

ALIEN_SYNTAX = [
    "function add(a, b) { return a + b; }",        # JavaScript
    "const x = 1;\nlet y = 2;",                    # JavaScript
    "for f in *.py; do echo $f; done",             # Bash
    "def broken(:\n",                              # genuinely malformed Python
]


@pytest.mark.parametrize("src", VALID_PYTHON)
def test_valid_python_is_not_a_syntax_violation(src):
    assert enf.python_syntax_violation(src) is False


@pytest.mark.parametrize("src", ALIEN_SYNTAX)
def test_alien_syntax_is_a_violation(src):
    """The canary for every allow-assertion above: without this the helper could
    return False unconditionally and the whole VALID_PYTHON block would still
    pass."""
    assert enf.python_syntax_violation(src) is True


def test_a_json_object_is_not_a_violation_because_it_is_valid_python():
    """The honest limit of this check, pinned so it is a decision rather than a
    gap someone discovers later.

    `ast.parse` answers "is this valid Python", NOT "was this intended as
    Python". A JSON object literal is a perfectly good Python dict expression,
    so it parses. Asserting a denial here would be asserting a capability the
    implementation does not have and cannot get from `ast` alone."""
    assert enf.python_syntax_violation('{"a": 1, "b": 2}') is False


@pytest.mark.parametrize("junk", [None, 0, 1, [], {}, object(), b"x = 1"])
def test_a_non_string_payload_is_never_a_violation(junk):
    """Total on junk, like every other predicate in this module. A payload whose
    `content` is missing or the wrong type is not evidence of alien syntax, and
    an enforcer that raises here would take down the hook."""
    assert enf.python_syntax_violation(junk) is False


def test_a_null_byte_is_a_violation():
    """Deliberate, and NOT the KTD4 fail-open case the plan first guessed at.

    Python raises `SyntaxError` — not `ValueError` — for source containing a
    null byte, and it is right to: such a file cannot be imported. So this is a
    true positive rather than a checker fault, and it denies."""
    assert enf.python_syntax_violation("x = 1\x00") is True


def test_an_indented_fragment_is_a_violation_which_is_why_edit_is_excluded():
    """The load-bearing reason `Edit` payloads are never AST-checked (KTD1).

    A `new_string` of ``    return a + b`` is a correct edit to the middle of a
    function and raises `IndentationError` — a `SyntaxError` subclass — when
    parsed standalone. If `Edit` were routed through this helper, ordinary
    edits would be denied and a live session would wedge. This test states the
    hazard at the helper so the exclusion in `language_violation` reads as a
    consequence rather than an oversight."""
    assert enf.python_syntax_violation("    return a + b") is True


def test_the_syntax_denial_is_the_mandated_message_verbatim():
    assert enf.SYNTAX_DENIAL == (
        "ACCESS DENIED: Language Compatibility Lock Violation. The provided "
        "code contains alien syntax or spaghetti code that is not valid "
        "Python. You are strictly forbidden from writing non-compilable "
        "syntax.")


def test_the_two_denials_are_distinct_messages():
    """They answer different questions and tell the operator to do different
    things — widening `language_context` does not make invalid code compile.
    A tidy-up that collapses them into one string would give wrong advice for
    one of the two failures."""
    assert enf.SYNTAX_DENIAL != enf.LANGUAGE_DENIAL
    assert "language_context" in enf.LANGUAGE_DENIAL
    assert "language_context" not in enf.SYNTAX_DENIAL


# --- MN-8a U2: the content check inside the real predicate --------------------------

JS_BODY = "function add(a, b) { return a + b; }"
PY_BODY = "def add(a, b):\n    return a + b\n"
FRAGMENT = "    return a + b"          # a correct mid-function edit


def test_a_python_file_full_of_javascript_is_denied():
    """The hole MN-8a closes. Every check before this one passes: `.py` maps to
    `python`, `python` is permitted, the path is not evidence. Only the content
    is wrong."""
    reason = enf.language_violation(
        "Write", {"file_path": "src/x.py", "content": JS_BODY},
        _sc(langs=("python",)))
    assert reason == enf.SYNTAX_DENIAL


def test_a_python_file_full_of_python_is_allowed():
    """The other half. Without it the test above passes against a rule that
    denies every `.py` write."""
    assert enf.language_violation(
        "Write", {"file_path": "src/x.py", "content": PY_BODY},
        _sc(langs=("python",))) is None


@pytest.mark.parametrize("tool,payload", [
    ("Edit", {"file_path": "src/x.py", "old_string": "pass",
              "new_string": FRAGMENT}),
    ("MultiEdit", {"file_path": "src/x.py",
                   "edits": [{"old_string": "pass", "new_string": FRAGMENT}]}),
    ("NotebookEdit", {"notebook_path": "src/x.py", "new_source": FRAGMENT}),
])
def test_a_fragment_bearing_tool_is_never_ast_checked(tool, payload):
    """THE anti-wedge guard for MN-8a, and the reason the rule is `Write`-only.

    ``    return a + b`` is a correct edit to the middle of a function and
    raises `IndentationError` parsed standalone. Routing these payloads through
    the syntax check would deny ordinary edits, and — because the escape hatches
    (`hive mindnet enforce --disable`, `hive log`) are themselves edits and
    commands — a session could not necessarily edit its way back out. This
    module has already shipped one deadlock built from two individually-correct
    guards; do not delete this test."""
    assert enf.language_violation(tool, payload, _sc(langs=("python",))) is None


def test_the_fragment_exclusion_is_not_vacuous():
    """Canary. The parametrised test above would pass against a syntax check
    that never fires at all, so pin that this exact fragment IS refused by the
    helper — the exclusion is a routing decision, not an inert code path."""
    assert enf.python_syntax_violation(FRAGMENT) is True


def test_a_heredoc_body_is_not_ast_checked():
    """KTD2: the shell rule still refuses by TARGET PATH, and extracting the
    heredoc BODY stays out of scope — same false-positive economics MN-4 already
    declined for command language inference. `src/x.py` is a permitted target
    under a python lock, so this allows."""
    cmd = "cat > src/x.py <<'EOF'\n" + JS_BODY + "\nEOF"
    assert enf.language_violation("Bash", {"command": cmd},
                                  _sc(langs=("python",))) is None


def test_the_syntax_check_is_python_only():
    """A task locked to typescript must not be handed Python parse semantics —
    `.ts` content that is invalid TypeScript is not this rule's business."""
    assert enf.language_violation(
        "Write", {"file_path": "src/x.ts", "content": "const x: = ;"},
        _sc(langs=("typescript",))) is None


def test_the_extension_rule_still_wins_for_a_forbidden_extension():
    """Ordering, pinned. A `.py` write under a typescript-only lock is refused
    for being the wrong FILE TYPE — the operator's remedy is to widen the
    context, so they must get `LANGUAGE_DENIAL`, not a message about
    non-compilable syntax."""
    reason = enf.language_violation(
        "Write", {"file_path": "src/x.py", "content": JS_BODY},
        _sc(langs=("typescript",)))
    assert reason == enf.LANGUAGE_DENIAL.format(languages="typescript",
                                                task_id="t1")


def test_phase_evidence_survives_the_syntax_check():
    """R5. The evidence exemption is the anti-deadlock guard and must hold
    across BOTH rules — the same composition failure the `is_phase_evidence`
    comment records."""
    sc = _sc(phase="brainstorm", langs=("python",))
    path = "C:/Users/hanif/mindhive/docs/superpowers/specs/spec.py"
    assert enf.language_violation(
        "Write", {"file_path": path, "content": JS_BODY}, sc) is None


def test_no_language_context_means_no_syntax_check():
    """The lock is opt-in per task. A task that declared no languages does not
    acquire a syntax gate for free."""
    assert enf.language_violation(
        "Write", {"file_path": "src/x.py", "content": JS_BODY},
        _sc(langs=())) is None


def test_no_locked_task_means_no_syntax_check():
    assert enf.language_violation(
        "Write", {"file_path": "src/x.py", "content": JS_BODY},
        _sc(langs=("python",), records=False)) is None


@pytest.mark.parametrize("payload", [
    {"file_path": "src/x.py"},                       # no content key at all
    {"file_path": "src/x.py", "content": None},
    {"file_path": "src/x.py", "content": 17},
    {"file_path": "src/x.py", "content": b"x = 1"},
])
def test_a_missing_or_non_string_content_allows(payload):
    """Fail-open on a payload shape this rule cannot read. Absent content is not
    evidence of alien syntax."""
    assert enf.language_violation("Write", payload,
                                  _sc(langs=("python",))) is None


@pytest.mark.parametrize("path", ["src/x.py.", "src/x.py ", "src/x.py::$DATA"])
def test_windows_path_suffixes_cannot_smuggle_alien_syntax_past(path):
    """Win32 strips these when it creates the file, so all three write `x.py`.
    The syntax check must parse the name Windows will USE — the same correction
    the extension rule already carries."""
    assert enf.language_violation(
        "Write", {"file_path": path, "content": JS_BODY},
        _sc(langs=("python",))) == enf.SYNTAX_DENIAL
