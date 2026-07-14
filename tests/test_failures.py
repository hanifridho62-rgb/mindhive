from mindhive import failures


def _tool_result_line(is_error, stderr="", interrupted=False, content_text=""):
    return {
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu_1",
             "is_error": is_error, "content": content_text},
        ]},
        "toolUseResult": {"stdout": "", "stderr": stderr, "interrupted": interrupted},
    }


def test_extracts_failure_with_signature_from_stderr():
    line = _tool_result_line(True, stderr="ModuleNotFoundError: No module named 'mindhive'")
    out = failures.extract_failures(line)
    assert len(out) == 1
    assert out[0]["tool_use_id"] == "tu_1"
    assert out[0]["signature"] == "pymodule:mindhive"
    assert "mindhive" in out[0]["error_excerpt"]


def test_success_and_interrupted_are_not_failures():
    assert failures.extract_failures(_tool_result_line(False, stderr="")) == []
    assert failures.extract_failures(
        _tool_result_line(True, stderr="boom", interrupted=True)) == []


def test_uses_block_content_when_stderr_empty():
    line = _tool_result_line(True, stderr="",
                             content_text="ModuleNotFoundError: No module named 'pypdf'")
    out = failures.extract_failures(line)
    assert out[0]["signature"] == "pymodule:pypdf"


def test_non_tool_result_lines_yield_nothing():
    assert failures.extract_failures({"type": "assistant", "message": {"content": []}}) == []
    assert failures.extract_failures({}) == []


import json
from mindhive import events


def _assistant_tool_use(tool_use_id, name, cmd=None, file_path=None):
    inp = {}
    if cmd is not None:
        inp["command"] = cmd
    if file_path is not None:
        inp["file_path"] = file_path
    return {"type": "assistant", "message": {"content": [
        {"type": "tool_use", "id": tool_use_id, "name": name, "input": inp}]}}


def test_capture_writes_tool_failed_event_with_action(tmp_path):
    tdir = tmp_path / "tx"
    tdir.mkdir()
    lines = [
        _assistant_tool_use("tu_1", "Bash", cmd="python mindhive.py state"),
        _tool_result_line(True, stderr="ModuleNotFoundError: No module named 'mindhive'"),
    ]
    lines[1]["message"]["content"][0]["tool_use_id"] = "tu_1"
    (tdir / "sess.jsonl").write_text(
        "\n".join(json.dumps(o) for o in lines), encoding="utf-8")

    res = failures.capture_dir(str(tdir), project="test")
    assert res["new_failures"] == 1
    evs = [e for e in events.read_all("test") if e.type == "tool_failed"]
    assert len(evs) == 1
    assert evs[0].payload["signature"] == "pymodule:mindhive"
    assert evs[0].payload["action"] == "python mindhive.py state"
    assert "failure" in evs[0].tags


def test_capture_is_idempotent(tmp_path):
    tdir = tmp_path / "tx"
    tdir.mkdir()
    lines = [_assistant_tool_use("tu_1", "Bash", cmd="x"),
             _tool_result_line(True, stderr="No module named 'tools'")]
    lines[1]["message"]["content"][0]["tool_use_id"] = "tu_1"
    (tdir / "sess.jsonl").write_text(
        "\n".join(json.dumps(o) for o in lines), encoding="utf-8")
    failures.capture_dir(str(tdir), project="test")
    res2 = failures.capture_dir(str(tdir), project="test")
    assert res2["new_failures"] == 0
    evs = [e for e in events.read_all("test") if e.type == "tool_failed"]
    assert len(evs) == 1


def test_capture_preserves_transcript_timestamp(tmp_path):
    tdir = tmp_path / "tx"
    tdir.mkdir()
    line = {
        "timestamp": "2026-06-30T08:15:30.500Z",
        "type": "user",
        "message": {"content": [
            {"type": "tool_result", "tool_use_id": "tu1", "is_error": True, "content": ""}]},
        "toolUseResult": {"stderr": "No module named 'zzz'", "interrupted": False},
    }
    (tdir / "s.jsonl").write_text(json.dumps(line), encoding="utf-8")
    failures.capture_dir(str(tdir), project="test")
    evs = [e for e in events.read_all("test") if e.type == "tool_failed"]
    assert len(evs) == 1
    assert evs[0].ts == "2026-06-30T08:15:30Z"     # real transcript time, not wall-clock


def test_model_unavailable_is_not_a_failure():
    line = _tool_result_line(
        True, stderr="claude-opus-4-8 is temporarily unavailable, so auto mode "
                     "cannot determine the safety of Bash right now. Wait briefly")
    assert failures.extract_failures(line) == []


def test_real_failure_still_captured_after_noise_filter():
    line = _tool_result_line(True, stderr="No module named 'mindhive'")
    assert len(failures.extract_failures(line)) == 1


# --- judgment:rejected capture (KTD4): user rejections are signal, not noise ---

_REJECT_TEXT = ("The user doesn't want to proceed with this tool use. "
                "The tool use was rejected")


def test_user_rejection_yields_judgment_marker_not_noise():
    out = failures.extract_failures(_tool_result_line(True, stderr=_REJECT_TEXT))
    assert len(out) == 1
    assert out[0]["rejected"] is True
    assert out[0]["signature"] is None            # finalized by capture_dir
    assert "rejected" in out[0]["error_excerpt"]


def test_rejection_wins_over_interrupted_flag():
    # a rejection sometimes rides an interrupted result — rejection semantics win
    out = failures.extract_failures(
        _tool_result_line(True, stderr=_REJECT_TEXT, interrupted=True))
    assert len(out) == 1 and out[0]["rejected"] is True


def test_technical_failure_carries_rejected_false():
    out = failures.extract_failures(
        _tool_result_line(True, stderr="No module named 'mindhive'"))
    assert out[0]["rejected"] is False


def _rejection_session(tdir, name, tu, cmd):
    lines = [_assistant_tool_use(tu, "Bash", cmd=cmd),
             _tool_result_line(True, stderr=_REJECT_TEXT)]
    lines[1]["message"]["content"][0]["tool_use_id"] = tu
    (tdir / f"{name}.jsonl").write_text(
        "\n".join(json.dumps(o) for o in lines), encoding="utf-8")


def test_rejection_capture_candidate_at_one_activates_at_two(tmp_path):
    from mindhive import incidents
    tdir = tmp_path / "tx"
    tdir.mkdir()
    _rejection_session(tdir, "s1", "tu_1", "git push --force")
    failures.capture_dir(str(tdir), project="test")
    evs = [e for e in events.read_all("test") if e.type == "tool_failed"]
    assert len(evs) == 1
    signature = evs[0].payload["signature"]
    # readable stem prefix + content hash (sig.slug_id) — same action recurs to
    # the same signature (deterministic hash), so candidate->active still holds.
    assert signature.startswith("judgment:rejected:bash:git-push-force-")
    assert "judgment" in evs[0].tags
    incidents.audit(project="test")
    incidents.project(project="test")
    assert incidents.load(signature, project="test")["status"] == "candidate"

    _rejection_session(tdir, "s2", "tu_2", "git push --force")
    failures.capture_dir(str(tdir), project="test")
    incidents.audit(project="test")
    incidents.project(project="test")
    rec = incidents.load(signature, project="test")
    assert rec["status"] == "active"
    assert rec["occurrences"] == 2


def test_unrelated_rejections_do_not_share_a_signature(tmp_path):
    tdir = tmp_path / "tx"
    tdir.mkdir()
    _rejection_session(tdir, "s1", "tu_1", "git push --force")
    _rejection_session(tdir, "s2", "tu_2", "rm -rf build")
    failures.capture_dir(str(tdir), project="test")
    sigs = {e.payload["signature"] for e in events.read_all("test")
            if e.type == "tool_failed"}
    assert len(sigs) == 2                        # distinct actions stay distinct
    prefixes = {s.rsplit("-", 1)[0] for s in sigs}   # strip the content hash
    assert prefixes == {"judgment:rejected:bash:git-push-force",
                        "judgment:rejected:bash:rm-rf-build"}


def test_transient_infra_error_still_produces_nothing(tmp_path):
    tdir = tmp_path / "tx"
    tdir.mkdir()
    lines = [_assistant_tool_use("tu_1", "Bash", cmd="x"),
             _tool_result_line(
                 True, stderr="claude-opus-4-8 is temporarily unavailable")]
    lines[1]["message"]["content"][0]["tool_use_id"] = "tu_1"
    (tdir / "s1.jsonl").write_text(
        "\n".join(json.dumps(o) for o in lines), encoding="utf-8")
    res = failures.capture_dir(str(tdir), project="test")
    assert res["new_failures"] == 0
    assert [e for e in events.read_all("test") if e.type == "tool_failed"] == []


def test_enforcement_denial_is_not_a_failure():
    """The PreToolUse enforcement hook's own deny text must not be re-captured
    as a new incident — that is the learning loop eating its own output (a
    grep-unscoped deny once became raw:bc40af13ca)."""
    from mindhive import enforce
    line = _tool_result_line(
        True, content_text=f"{enforce.ENFORCED_MARK} This Grep is unscoped — "
                           "re-run it bounded.")
    assert failures.extract_failures(line) == []


def test_permission_gate_prompt_is_not_a_failure():
    line = _tool_result_line(
        True, content_text="Claude requested permissions to read from "
                           "C:/somewhere, but you haven't granted it yet.")
    assert failures.extract_failures(line) == []
