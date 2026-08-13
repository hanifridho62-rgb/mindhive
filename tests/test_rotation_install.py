# tests/test_rotation_install.py
"""Contract for registering the Stop hook in settings.json — unit U7.

This is the only part of the feature that edits a file outside the repo, and it
is the file the user's entire toolchain reads. Getting rotation wrong costs a
rotation; corrupting this costs Claude Code. Every test here is about the second
risk, not the first.
"""
import json
import os

from mindhive.rotation import install

# The shape the real file uses: `hooks` is an object keyed by stage, each stage a
# list of matcher-groups, each group holding a list of command entries.
REAL_SHAPE = {
    "model": "opus",
    "hooks": {
        "SessionStart": [{"hooks": [{"type": "command", "command": "ss.ps1"}]}],
        "PreToolUse": [{"matcher": "Grep|Bash",
                        "hooks": [{"type": "command", "command": "ptu.ps1"}]}],
    },
    "enableWorkflows": True,
}

CMD = "powershell -NoProfile -File C:/repo/hooks/stop.ps1"


def _write(tmp_path, obj, name="settings.json"):
    p = tmp_path / name
    p.write_text(json.dumps(obj, indent=2) if not isinstance(obj, str) else obj,
                 encoding="utf-8")
    return p


# --- the happy paths -------------------------------------------------------

def test_adds_stop_to_a_file_that_has_none(tmp_path):
    p = _write(tmp_path, REAL_SHAPE)
    result = install.install(p, CMD)
    assert result["ok"] and result["reason"] == "installed"

    after = json.loads(p.read_text(encoding="utf-8"))
    entry = after["hooks"]["Stop"][0]["hooks"][0]
    assert entry == {"type": "command", "command": CMD}


def test_leaves_every_existing_stage_untouched(tmp_path):
    p = _write(tmp_path, REAL_SHAPE)
    install.install(p, CMD)
    after = json.loads(p.read_text(encoding="utf-8"))
    assert after["hooks"]["SessionStart"] == REAL_SHAPE["hooks"]["SessionStart"]
    assert after["hooks"]["PreToolUse"] == REAL_SHAPE["hooks"]["PreToolUse"]
    # Unrelated top-level keys survive too — this file holds plugins, marketplaces,
    # and theme settings that have nothing to do with hooks.
    assert after["model"] == "opus"
    assert after["enableWorkflows"] is True


def test_creates_the_hooks_object_when_absent(tmp_path):
    p = _write(tmp_path, {"model": "opus"})
    assert install.install(p, CMD)["reason"] == "installed"
    after = json.loads(p.read_text(encoding="utf-8"))
    assert after["hooks"]["Stop"][0]["hooks"][0]["command"] == CMD
    assert after["model"] == "opus"


def test_emits_no_matcher_key(tmp_path):
    """Stop fires on every occurrence and does not accept a matcher."""
    p = _write(tmp_path, REAL_SHAPE)
    install.install(p, CMD)
    group = json.loads(p.read_text(encoding="utf-8"))["hooks"]["Stop"][0]
    assert "matcher" not in group


# --- idempotency -----------------------------------------------------------

def test_second_install_is_a_noop(tmp_path):
    """DISCRIMINATING: an implementation that appends unconditionally passes
    every test above and fails here, having quietly doubled the hook."""
    p = _write(tmp_path, REAL_SHAPE)
    install.install(p, CMD)
    second = install.install(p, CMD)
    assert second["ok"] and second["reason"] == "already_registered"
    assert len(json.loads(p.read_text(encoding="utf-8"))["hooks"]["Stop"]) == 1


def test_a_different_command_is_added_alongside(tmp_path):
    """CANARY for the test above: proves the matcher is comparing commands, not
    just checking whether the Stop key exists at all."""
    p = _write(tmp_path, REAL_SHAPE)
    install.install(p, CMD)
    install.install(p, CMD + " --other")
    assert len(json.loads(p.read_text(encoding="utf-8"))["hooks"]["Stop"]) == 2


# --- refusal ---------------------------------------------------------------

def test_malformed_json_is_refused_and_nothing_is_written(tmp_path):
    """The important assertion is the second one: reporting failure while having
    already written is the worst possible outcome, and only checking the return
    value would not catch it."""
    raw = '{"hooks": {"SessionStart": [ ... broken'
    p = _write(tmp_path, raw)
    result = install.install(p, CMD)
    assert result["ok"] is False and result["reason"] == "malformed"
    assert p.read_text(encoding="utf-8") == raw


def test_top_level_array_is_refused(tmp_path):
    p = _write(tmp_path, [1, 2, 3])
    assert install.install(p, CMD)["reason"] == "malformed"


def test_hooks_of_the_wrong_type_is_refused_not_coerced(tmp_path):
    """Coercing would silently discard whatever the user had there."""
    p = _write(tmp_path, {"hooks": ["not", "a", "dict"]})
    result = install.install(p, CMD)
    assert result["ok"] is False and result["reason"] == "bad_hooks"
    assert json.loads(p.read_text(encoding="utf-8"))["hooks"] == ["not", "a", "dict"]


def test_stop_of_the_wrong_type_is_refused(tmp_path):
    p = _write(tmp_path, {"hooks": {"Stop": "nope"}})
    assert install.install(p, CMD)["reason"] == "bad_hooks"


def test_missing_file_is_reported_not_created(tmp_path):
    target = tmp_path / "nothing-here.json"
    result = install.install(target, CMD)
    assert result["ok"] is False and result["reason"] == "missing"
    assert not target.exists()


# --- backups ---------------------------------------------------------------

def test_a_backup_is_written_before_mutating(tmp_path):
    p = _write(tmp_path, REAL_SHAPE)
    before = p.read_text(encoding="utf-8")
    result = install.install(p, CMD)
    backup = tmp_path / (p.name + ".bak-1")
    assert backup.is_file()
    assert backup.read_text(encoding="utf-8") == before
    assert result["backup"] == str(backup)


def test_a_second_install_does_not_clobber_the_first_backup(tmp_path):
    p = _write(tmp_path, REAL_SHAPE)
    pristine = p.read_text(encoding="utf-8")
    install.install(p, CMD)
    install.install(p, CMD + " --other")
    assert (tmp_path / (p.name + ".bak-1")).read_text(encoding="utf-8") == pristine
    assert (tmp_path / (p.name + ".bak-2")).is_file()


# --- dry run ---------------------------------------------------------------

def test_dry_run_reports_without_writing(tmp_path):
    p = _write(tmp_path, REAL_SHAPE)
    before = p.read_text(encoding="utf-8")
    result = install.install(p, CMD, dry_run=True)
    assert result["ok"] and result["reason"] == "would_install"
    assert p.read_text(encoding="utf-8") == before
    assert not (tmp_path / (p.name + ".bak-1")).exists()


# --- the default command ---------------------------------------------------

def test_default_command_points_at_the_repo_shim():
    cmd = install.default_command()
    assert cmd.startswith("powershell -NoProfile -File ")
    assert cmd.endswith("hooks/stop.ps1")


# --- the platform the shim cannot run on -----------------------------------
#
# Every shim in hooks/ is PowerShell — nine .ps1 files, no POSIX equivalent — so
# on macOS or Linux this registration succeeds and the hook can then never fire.
# `os_name` is injected in both directions so BOTH answers are reachable from
# either CI runner; a caveat only one runner can test is a caveat nobody tests.

def test_a_posix_install_says_the_shim_cannot_run_there(tmp_path):
    """DISCRIMINATING: the JSON edit works everywhere, which is exactly why the
    failure is silent. Rotation would simply never happen, with nothing said."""
    p = _write(tmp_path, REAL_SHAPE)
    result = install.install(p, CMD, os_name="posix")
    assert result["ok"] and result["reason"] == "installed"
    warning = result["platform_warning"]
    assert "PowerShell" in warning and "Windows-only" in warning
    assert "posix" in warning                   # names the platform it found


def test_a_windows_install_carries_no_platform_warning(tmp_path):
    """CANARY for the test above: the key is absent when the shim can actually
    run, so the warning is a finding rather than boilerplate on every result."""
    p = _write(tmp_path, REAL_SHAPE)
    result = install.install(p, CMD, os_name="nt")
    assert result["ok"] and "platform_warning" not in result


def test_a_refusal_is_not_diluted_by_the_platform_warning(tmp_path):
    """A malformed settings.json is the whole answer. Appending a second caveat
    to it buries the one the user has to act on."""
    p = _write(tmp_path, "{broken", name="bad.json")
    result = install.install(p, CMD, os_name="posix")
    assert result["reason"] == "malformed"
    assert "platform_warning" not in result


def test_platform_warning_defaults_to_this_machine():
    """Unqualified, it answers for the interpreter that is running — which is
    what the CLI path relies on, since it passes no os_name."""
    assert install.platform_warning() == install.platform_warning(os.name)


# --- CLI wiring ------------------------------------------------------------

def test_cli_install_hook_exit_codes(tmp_path, capsys):
    from mindhive import __main__ as cli_mod
    good = _write(tmp_path, REAL_SHAPE)
    assert cli_mod.main(["session", "install-hook", str(good)]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "installed"

    bad = _write(tmp_path, "{broken", name="bad.json")
    assert cli_mod.main(["session", "install-hook", str(bad)]) == 2
    assert json.loads(capsys.readouterr().out)["reason"] == "malformed"


def test_cli_install_hook_honours_an_explicit_command(tmp_path, capsys):
    """A worktree's default_command() resolves to the WORKTREE's own copy of the
    shim, which disappears when the worktree does. Registering from one has to be
    able to name the main checkout's path instead."""
    from mindhive import __main__ as cli_mod
    p = _write(tmp_path, REAL_SHAPE)
    explicit = "powershell -NoProfile -File C:/main/hooks/stop.ps1"
    assert cli_mod.main(["session", "install-hook", str(p),
                         "--command", explicit]) == 0
    capsys.readouterr()
    after = json.loads(p.read_text(encoding="utf-8"))
    assert after["hooks"]["Stop"][0]["hooks"][0]["command"] == explicit


def test_cli_install_hook_dry_run(tmp_path, capsys):
    from mindhive import __main__ as cli_mod
    p = _write(tmp_path, REAL_SHAPE)
    before = p.read_text(encoding="utf-8")
    assert cli_mod.main(["session", "install-hook", str(p), "--dry-run"]) == 0
    assert json.loads(capsys.readouterr().out)["reason"] == "would_install"
    assert p.read_text(encoding="utf-8") == before
