# tests/test_cli_premise.py
"""`hive premise check` — the same engine the hook uses, callable directly.

This is what makes §0's "verify the premise" instruction actionable rather than
aspirational: the directive can name a command that exists, and an agent handed
a handoff document can check it before acting on it.
"""
from mindhive import __main__ as cli_mod

from tests.test_premise import REAL_DIRECTIVE


def _repo(tmp_path, monkeypatch):
    (tmp_path / ".git").mkdir()
    (tmp_path / "src" / "mindhive").mkdir(parents=True)
    (tmp_path / "src" / "mindhive" / "advice.py").write_text("x", encoding="utf-8")
    monkeypatch.chdir(tmp_path)
    return tmp_path


def _write(tmp_path, text, name="doc.md"):
    p = tmp_path / name
    p.write_text(text, encoding="utf-8")
    return str(p)


def test_contradiction_exits_nonzero_and_names_the_claim(tmp_path, monkeypatch,
                                                         capsys):
    """The dogfood case: the real directive, checked."""
    root = _repo(tmp_path, monkeypatch)
    ret = cli_mod.main(["premise", "check", "--file",
                        _write(root, REAL_DIRECTIVE)])
    out = capsys.readouterr().out
    assert ret != 0, "a contradicted claim must fail the command"
    assert "src/mindhive/history.py" in out
    assert "CONTRADICTED" in out


def test_all_true_claims_exit_zero(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, monkeypatch)
    ret = cli_mod.main(["premise", "check", "--file",
                        _write(root, "edit src/mindhive/advice.py")])
    assert ret in (0, None)
    assert "CONFIRMED" in capsys.readouterr().out


def test_unknowns_exit_zero_but_are_still_printed(tmp_path, monkeypatch, capsys):
    """The operator should see what could NOT be checked. Silence there is the
    failure mode this whole module is against."""
    root = _repo(tmp_path, monkeypatch)
    ret = cli_mod.main(["premise", "check", "--file",
                        _write(root, "protecting a 3518-test baseline")])
    out = capsys.readouterr().out
    assert ret in (0, None)
    assert "UNKNOWN" in out
    assert "3518" in out


def test_empty_input_says_so_rather_than_going_silent(tmp_path, monkeypatch,
                                                      capsys):
    root = _repo(tmp_path, monkeypatch)
    ret = cli_mod.main(["premise", "check", "--file", _write(root, "hello there")])
    out = capsys.readouterr().out
    assert ret in (0, None)
    assert "no checkable claims" in out.lower()


def test_missing_file_is_a_real_error_not_a_traceback(tmp_path, monkeypatch,
                                                      capsys):
    _repo(tmp_path, monkeypatch)
    ret = cli_mod.main(["premise", "check", "--file",
                        str(tmp_path / "nope.md")])
    assert ret != 0
    assert "cannot read" in capsys.readouterr().out.lower()


def test_reads_stdin_when_no_file_given(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, monkeypatch)
    monkeypatch.setattr("sys.stdin", _Stdin(REAL_DIRECTIVE))
    ret = cli_mod.main(["premise", "check"])
    assert ret != 0
    assert "src/mindhive/history.py" in capsys.readouterr().out
    assert root.exists()


def test_output_names_the_gates_limits(tmp_path, monkeypatch, capsys):
    root = _repo(tmp_path, monkeypatch)
    cli_mod.main(["premise", "check", "--file", _write(root, REAL_DIRECTIVE)])
    assert "not a clean bill of health" in capsys.readouterr().out.lower()


class _Stdin:
    def __init__(self, text):
        self._text = text

    def read(self):
        return self._text

    def isatty(self):
        return False
