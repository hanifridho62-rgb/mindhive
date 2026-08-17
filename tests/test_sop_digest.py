# tests/test_sop_digest.py — the MEMORY.md rendering of the SOP registry.
"""STATE.md and MEMORY.md are not the same delivery channel, and until now they
shared one renderer.

Measured on the live store at 404a7c0: `sop.block()` was 15,463 chars and
occupied lines 32..151 of a 202-line MEMORY.md — 58.7% of the one file every
session auto-loads, against a native loader that stops around 25,000 LF bytes.
`BLOCK_FULL_BUDGET = 16000` never bit once (ordinary bodies totalled 11,425), so
the section that pushed `## Key decisions` past the cut was nominally "budgeted"
the whole time. A ceiling set above the delivery channel is not a budget.

STATE.md has no such cut and is the full-fidelity read, so `block()` keeps
rendering bodies there, byte for byte. `digest_block()` is the MEMORY.md
rendering: ordinary SOPs become one pointer line each, and a MANDATORY body is
elided ONLY when `sopgate` is verifiably re-asserting that same body on every
prompt. That condition is the whole safety argument — `sopgate.probe` can report
`pointers_only`/`partial`, and eliding a body in that state would put it in front
of no reader at all.

Fail-safe polarity, asserted below: no measurement (missing or corrupt sidecar →
verdict `silent` → empty id set) means bodies STAY.
"""
from mindhive import context, events, paths, projector, sop
from mindhive.sync import sync


def _add(sop_id, title, text, ts, mandatory=False):
    events.append_event("sop", {"action": "add", "sop_id": sop_id,
                                "title": title, "text": text,
                                "mandatory": mandatory},
                        session_id="s", ts=ts, tags=["sop"])


def _registry(n_ordinary=2, body="ORDINARY-BODY-" + "o" * 60, mandatory_body=None):
    if mandatory_body is not None:
        _add("execution-discipline", "Execution discipline", mandatory_body,
             "2026-07-01T09:00:00Z", mandatory=True)
    for i in range(n_ordinary):
        _add(f"proc-{i:02d}", f"Procedure {i}", f"{body}-{i}",
             f"2026-07-0{(i % 8) + 1}T1{i % 10}:00:00Z")
    return events.read_all()


# --------------------------------------------------------------------------
# the renderer
# --------------------------------------------------------------------------

def test_ordinary_sops_render_as_pointers_not_bodies():
    evs = _registry(n_ordinary=3)
    blk = sop.digest_block(evs)
    assert "STANDARD OPERATING PROCEDURES" in blk        # still greppable
    for i in range(3):
        assert f"proc-{i:02d}" in blk                    # every id is named
        assert f"Procedure {i}" in blk                   # and its title
    assert "ORDINARY-BODY" not in blk                    # but no body rides along
    assert "hive sop show" in blk and "hive sop list" in blk


def test_a_mandatory_body_stays_when_the_prompt_pass_is_not_delivering_it():
    """Fail-safe direction. An empty id set is what a missing or corrupt sidecar
    produces, and in that state the digest is the only channel left."""
    evs = _registry(n_ordinary=1, mandatory_body="MANDATORY-BODY-" + "m" * 80)
    blk = sop.digest_block(evs, injected_ids=())
    assert "MANDATORY-BODY" in blk


def test_a_mandatory_body_is_elided_only_when_it_is_injected_verbatim():
    evs = _registry(n_ordinary=1, mandatory_body="MANDATORY-BODY-" + "m" * 80)
    blk = sop.digest_block(evs, injected_ids={"execution-discipline"})
    assert "MANDATORY-BODY" not in blk
    assert "execution-discipline" in blk                 # named, with a way back
    assert "hive sop show execution-discipline" in blk


def test_the_digest_caps_the_list_and_says_how_many_it_dropped(monkeypatch):
    monkeypatch.setattr(sop, "DIGEST_ITEM_CAP", 3)
    evs = _registry(n_ordinary=6)
    blk = sop.digest_block(evs)
    listed = sum(1 for i in range(6) if f"proc-{i:02d}" in blk)
    assert listed == 3
    assert "3 more" in blk
    assert "hive sop list" in blk


def test_the_digest_rendering_is_pure():
    """Determinism is test-guarded across this whole projection path."""
    evs = _registry(n_ordinary=3, mandatory_body="body " * 10)
    a = sop.digest_block(evs, injected_ids={"execution-discipline"})
    b = sop.digest_block(evs, injected_ids=frozenset(["execution-discipline"]))
    c = sop.digest_block(evs, injected_ids=["execution-discipline"])
    assert a == b == c
    assert sop.digest_block(evs) == sop.digest_block(evs)


def test_the_digest_never_emits_the_injection_notice_wording():
    """CANARY for a real collision: tests/test_sync.py:295 asserts the string
    'SOP injection' is ABSENT from a healthy digest, and healthy is exactly when
    the mandatory pointer renders. Wording the pointer with that phrase would
    turn a passing suite red from the far side of the repo."""
    evs = _registry(n_ordinary=1, mandatory_body="body " * 10)
    assert "SOP injection" not in sop.digest_block(
        evs, injected_ids={"execution-discipline"})


# --------------------------------------------------------------------------
# the wiring — a renderer the digest never reaches is the failure mode
# tests/test_sync.py:252-255 already warns about
# --------------------------------------------------------------------------

def _digest(tmp_path):
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    events.append_event("state_set", {"task": "T", "verified": "v",
                                      "next_step": "n", "open_questions": [],
                                      "deferred": []},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    return sync(r"C:\TEMP\demo", project=None, home=fake_home).read_text(
        encoding="utf-8")


def test_the_digest_sync_actually_writes_uses_the_pointer_rendering(tmp_path):
    _registry(n_ordinary=3)
    content = _digest(tmp_path)
    assert "STANDARD OPERATING PROCEDURES" in content
    assert content.count("STANDARD OPERATING PROCEDURES") == 1   # not both renderings
    assert "proc-00" in content
    assert "ORDINARY-BODY" not in content


def test_state_md_still_carries_the_bodies_in_full(tmp_path):
    """CANARY: the STATE.md rendering must not move. It is the full-fidelity read
    that every pointer in the digest points AT."""
    _registry(n_ordinary=3)
    _digest(tmp_path)
    state = (paths.project_dir() / "STATE.md").read_text(encoding="utf-8")
    assert "ORDINARY-BODY" in state
    assert "SOP 1: Procedure 0" in state


def test_compose_state_default_variant_is_unchanged():
    """CANARY for the flag's polarity: digest must default False, or STATE.md
    silently loses its bodies."""
    evs = _registry(n_ordinary=2)
    assert "ORDINARY-BODY" in projector.compose_state(evs, suggestions="")
    assert "ORDINARY-BODY" in projector.compose_state(
        evs, include_rules=False, suggestions="")


# --------------------------------------------------------------------------
# the outcome
# --------------------------------------------------------------------------

def test_a_full_registry_no_longer_pushes_the_digest_past_the_loader_budget(tmp_path):
    """FLAGSHIP. The live shape that broke: 1 mandatory + 10 ordinary SOPs with
    real bodies. Before this change that digest ran 202 lines against a 200-line
    loader budget and lost '## Key decisions' entirely."""
    _registry(n_ordinary=10, mandatory_body="M" * 1200)
    for i in range(5):
        events.append_event("decision", {"text": f"decision number {i}"},
                            session_id="s", ts=f"2026-07-10T1{i}:00:00Z")
    fake_home = tmp_path / "home"
    fake_home.mkdir(exist_ok=True)
    events.append_event("state_set", {"task": "T", "verified": "v",
                                      "next_step": "n", "open_questions": [],
                                      "deferred": []},
                        session_id="s", ts="2026-06-20T09:00:00Z")
    path = sync(r"C:\TEMP\demo", project=None, home=fake_home)
    pressure = context.memory_pressure(path)
    assert pressure["readable"]
    assert pressure["over"] == 0, (
        f"digest is {pressure['lines']} lines against {pressure['budget']}; "
        f"dropped={pressure['dropped']}")
    content = path.read_text(encoding="utf-8")
    assert "## Key decisions" in content
    assert "decision number 4" in content
