# tests/test_rotation_install.py
"""Contract for registering the Stop hook in settings.json — unit U7.

This is the only part of the feature that edits a file outside the repo, and it
is the file the user's entire toolchain reads. Getting rotation wrong costs a
rotation; corrupting this costs Claude Code. Every test here is about the second
risk, not the first.
"""
import json
import os
import shlex
import shutil
from pathlib import Path

import pytest

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


# `CMD` for another checkout: a genuinely different registration, because it
# runs a different Stop shim.
OTHER_CMD = CMD.replace("C:/repo/", "C:/other-repo/")


def test_a_different_command_is_added_alongside(tmp_path):
    """CANARY for the test above: proves the matcher is comparing commands, not
    just checking whether the Stop key exists at all.

    "Different" is another checkout's shim. This used to append `CMD + "
    --other"`, which runs the SAME shim with an extra argument; that stays
    distinct only while the shim comparison never looks at `CMD`'s own target,
    and it is exactly the duplicate the comparison exists to refuse -- see the
    next test."""
    p = _write(tmp_path, REAL_SHAPE)
    install.install(p, CMD)
    install.install(p, OTHER_CMD)
    assert len(json.loads(p.read_text(encoding="utf-8"))["hooks"]["Stop"]) == 2


def test_the_same_shim_with_other_arguments_is_already_registered(tmp_path):
    """The decision, pinned rather than implied. Two Stop entries that run one
    shim run it twice at the end of every agent turn, in every project,
    whatever else is on the command line -- the harm the shim comparison was
    added for. So a respelled or re-argued command for a registered shim is
    `already_registered`, and nothing is written."""
    p = _write(tmp_path, REAL_SHAPE)
    install.install(p, CMD)
    before = p.read_text(encoding="utf-8")
    result = install.install(p, CMD + " --other")
    assert result["reason"] == "already_registered", result
    assert result["matched"] == "shim"
    assert p.read_text(encoding="utf-8") == before


# --- idempotency across releases: the SHIM, not the spelling ----------------
#
# Every test above hands `install` the module constant `CMD`, so each one
# compared a string with itself. The command the CLI actually registers is
# `default_command()`, and this branch changed its spelling: the shim path is
# now QUOTED, so a checkout under `C:/My Repo` stays one argument. Against an
# exact string comparison that was a breaking change to a value already written
# into settings.json files on disk -- the next `install-hook` APPENDED a second
# Stop entry beside the previous release's unquoted one, and stop.ps1 ran twice
# at the end of every agent turn in every project. `install._target` now also
# asks whether an existing command runs the shim the installing command runs.

def _settings_with_stop(tmp_path, command, name="settings.json"):
    obj = json.loads(json.dumps(REAL_SHAPE))
    obj["hooks"]["Stop"] = [{"hooks": [{"type": "command",
                                        "command": command}]}]
    return _write(tmp_path, obj, name=name)


def _stop_groups(p):
    return json.loads(p.read_text(encoding="utf-8"))["hooks"]["Stop"]


def _nt_shim():
    """This checkout's Windows Stop shim, read back out of the command the
    module registers rather than rebuilt here, so the fixture cannot drift from
    the template it stands in for."""
    return shlex.split(install.default_command(os_name="nt"))[-1]


def test_install_with_the_default_command_twice_is_a_noop(tmp_path):
    """The gap the review named: no test called `install` with `command=None`
    twice. That is the path `hive session install-hook` takes, and the only one
    where the registered string is the module's own rather than the test's."""
    p = _write(tmp_path, REAL_SHAPE)
    assert install.install(p, None)["reason"] == "installed"
    second = install.install(p, None)
    assert second["ok"] and second["reason"] == "already_registered", second
    assert second["matched"] == "command"
    groups = _stop_groups(p)
    assert len(groups) == 1
    assert groups[0]["hooks"][0]["command"] == install.default_command()
    # One backup, from the one write. A second would mean the no-op wrote.
    assert [b.name for b in tmp_path.glob("*.bak-*")] == [p.name + ".bak-1"]


@pytest.mark.parametrize("dry_run", [False, True], ids=["write", "dry-run"])
def test_the_previous_releases_unquoted_default_is_already_registered(
        tmp_path, dry_run):
    """DISCRIMINATING, and the exact shape of the operator's own settings.json:
    a Stop stage holding `powershell -NoProfile -File <repo>/hooks/stop.ps1`
    with no quotes. Against the exact comparison this returned `installed` (a
    dry run, `would_install`) and left two Stop groups behind.

    The previous release's command is DERIVED by stripping the quotes from the
    current default, and the first assertion proves that produced a different
    string -- otherwise this would be `test_second_install_is_a_noop` again and
    would pass against the very comparison it exists to refute."""
    current = install.default_command(os_name="nt")
    previous = current.replace('"', "")
    assert previous != current, "canary: the two spellings did not differ"

    p = _settings_with_stop(tmp_path, previous)
    before = p.read_text(encoding="utf-8")
    result = install.install(p, None, dry_run=dry_run, os_name="nt")

    assert result["ok"] and result["reason"] == "already_registered", result
    assert result["matched"] == "shim"
    assert p.read_text(encoding="utf-8") == before
    assert not list(tmp_path.glob("*.bak-*")), "a no-op wrote a backup"


@pytest.mark.parametrize("spell", [
    lambda s: s.replace("/", "\\"),
    lambda s: s.upper(),
    lambda s: '"' + s.replace("/", "\\").lower() + '"',
    lambda s: "'" + s + "'",
], ids=["backslashes", "upper-case", "quoted-backslashed-lower",
        "single-quoted"])
def test_the_same_shim_in_another_spelling_is_already_registered(tmp_path,
                                                                 spell):
    """Windows compares paths case-insensitively and with either separator, and
    a settings.json edited by hand uses whichever its author typed. Each of
    these runs the same stop.ps1, so each is the same registration -- and each
    is a string the exact comparison could never match."""
    existing = "powershell -NoProfile -File " + spell(_nt_shim())
    p = _settings_with_stop(tmp_path, existing)
    before = p.read_text(encoding="utf-8")
    result = install.install(p, None, os_name="nt")
    assert result["reason"] == "already_registered", (existing, result)
    assert result["matched"] == "shim"
    assert p.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("other", [
    lambda s: s.replace("/hooks/stop.ps1", "-other/hooks/stop.ps1"),
    lambda s: s + ".disabled",
    lambda s: s.replace("/stop.ps1", "/session_end.ps1"),
], ids=["another-checkout", "path-extended", "another-stage-shim"])
def test_a_command_for_a_different_shim_is_still_added_alongside(tmp_path,
                                                                 other):
    """CANARY for the two tests above. A comparison that matched on the file
    name, on a substring, or on "any Stop entry at all" would pass both of them
    and silently refuse to register this checkout beside a different one.
    `path-extended` holds this shim's full path as a substring, so it is the
    case a `shim in command` implementation gets wrong."""
    existing = "powershell -NoProfile -File " + other(_nt_shim())
    p = _settings_with_stop(tmp_path, existing)
    result = install.install(p, None, os_name="nt")
    assert result["reason"] == "installed", (existing, result)
    assert "matched" not in result
    commands = [g["hooks"][0]["command"] for g in _stop_groups(p)]
    assert commands == [existing, install.default_command(os_name="nt")]


@pytest.mark.parametrize("value", [42, None, True, {"x": 1},
                                   ["not", "a", "command"]],
                         ids=["int", "null", "bool", "object", "array"])
def test_a_non_string_command_in_settings_is_tolerated(tmp_path, value):
    """`command` in a settings.json can hold anything, and the shim comparison
    runs inside the step that decides whether to write the operator's global
    hook configuration: a raise there is an `install-hook` traceback instead of
    an answer. None of these names this checkout's shim, so the honest result
    is to register it beside them, leaving the odd value exactly as it was."""
    p = _settings_with_stop(tmp_path, value)
    result = install.install(p, None, os_name="nt")
    assert result["ok"] and result["reason"] == "installed", (value, result)
    groups = _stop_groups(p)
    assert groups[0]["hooks"][0]["command"] == value
    assert groups[-1]["hooks"][0]["command"] == \
        install.default_command(os_name="nt")


# --- the shim comparison's two false positives ------------------------------
#
# Review of the shim fallback above found two cases where `install` answered
# `already_registered` for a registration the file did not hold. Both were
# carried as strict xfails until the shim comparison fixed them; they are the
# regression guards now.

def test_an_explicit_command_for_another_checkout_is_not_swallowed(tmp_path):
    """`install`'s docstring promises "an explicit `command` still overrides
    both", and `test_cli_install_hook_honours_an_explicit_command` exists
    because a worktree must be able to register the MAIN checkout's shim --
    its own copy disappears with the worktree. That test starts from a file
    with no Stop entry, so it cannot see this: once this checkout's shim is
    registered, the explicit registration is refused as `matched: shim` and
    nothing is written."""
    p = _settings_with_stop(tmp_path, install.default_command(os_name="nt"))
    explicit = 'powershell -NoProfile -File "C:/main-checkout/hooks/stop.ps1"'
    result = install.install(p, explicit, os_name="nt")
    assert result["reason"] == "installed", result
    assert [g["hooks"][0]["command"] for g in _stop_groups(p)][-1] == explicit


def test_an_explicit_command_for_the_same_shim_is_still_already_registered(
        tmp_path):
    """CANARY for the test above. Comparing against the installing command's
    OWN target must not throw away the idempotency the shim fallback exists
    for: an explicit command that runs this checkout's shim, respelled, is the
    registration already in the file."""
    p = _settings_with_stop(tmp_path, install.default_command(os_name="nt"))
    before = p.read_text(encoding="utf-8")
    explicit = ("powershell -NoProfile -ExecutionPolicy Bypass -File "
                + _nt_shim().replace("/", "\\"))
    result = install.install(p, explicit, os_name="nt")
    assert result["reason"] == "already_registered", result
    assert result["matched"] == "shim"
    assert p.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("wrap", [
    lambda s: ["powershell", "-NoProfile", "-File", s],
    lambda s: {"file": s},
], ids=["array", "object"])
def test_a_non_string_value_holding_the_shim_path_is_not_a_registration(
        tmp_path, wrap):
    """A non-string `command` runs nothing. It used to be `str()`ed and
    tokenised: the repr of a list quotes each element, the token pattern strips
    the quotes, and the path matched -- so a malformed entry Claude Code cannot
    run blocked the registration of one it can. The non-string test above only
    uses values that never contain the path, which is why it was green over
    this."""
    p = _settings_with_stop(tmp_path, wrap(_nt_shim()))
    result = install.install(p, None, os_name="nt")
    assert result["reason"] == "installed", result


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
    install.install(p, OTHER_CMD)
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

def test_default_command_matches_the_platform():
    """It used to register the PowerShell form unconditionally, so on macOS or
    Linux it wrote a command the machine cannot run into the user's
    settings.json. `os_name` is injected in both directions so BOTH answers are
    reachable from either CI runner — a branch only one runner can test is a
    branch nobody tests."""
    win = install.default_command(os_name="nt")
    assert win.startswith("powershell -NoProfile -ExecutionPolicy Bypass -File ")
    assert shlex.split(win)[-1].endswith("hooks/stop.ps1")

    posix = install.default_command(os_name="posix")
    assert posix.startswith("sh ")
    assert shlex.split(posix)[-1].endswith("hooks/stop.sh")


def test_the_windows_default_bypasses_the_execution_policy(tmp_path):
    """A Windows client's default execution policy is `Restricted`, under which
    `powershell -File` refuses every script with a SecurityError and exits 1 --
    on every hook fire, before the shim's fail-open code runs. Measured on 5.1:
    `-ExecutionPolicy Restricted -File hooks/stop.ps1` printed `cannot be loaded
    because running scripts is disabled` and exited 1; `Bypass` exited 0. The
    flag scopes to that one process. The previous default without it still
    EXECUTES this shim, so it is the same registration, not a second one -- and
    the canary proves the two spellings really differ."""
    win = install.default_command(os_name="nt")
    words = shlex.split(win)
    assert words[:5] == ["powershell", "-NoProfile", "-ExecutionPolicy",
                         "Bypass", "-File"], words

    previous = win.replace(" -ExecutionPolicy Bypass", "")
    assert previous != win, "canary: the two spellings did not differ"
    p = _settings_with_stop(tmp_path, previous)
    before = p.read_text(encoding="utf-8")
    result = install.install(p, None, os_name="nt")
    assert result["reason"] == "already_registered", result
    assert p.read_text(encoding="utf-8") == before


def test_default_command_defaults_to_this_machine():
    assert install.default_command() == install.default_command(os_name=os.name)


def test_an_unknown_platform_is_treated_as_posix():
    """`os.name` is `nt` or `posix` in every CPython build that matters, but a
    resolver that answered nothing for a third value would register an empty
    command rather than a wrong one, which is worse."""
    assert install.default_command(os_name="java") == \
        install.default_command(os_name="posix")


# --- the warning now asks the filesystem, not the calendar -----------------
#
# This block used to assert "you are not on Windows, so rotation cannot fire".
# That was true when every shim in hooks/ was PowerShell and stopped being true
# the day `hooks/stop.sh` landed — the warning had become a statement about the
# past. It now means "the shim for your platform is missing", which is
# actionable and stays correct as platforms are added.

def test_neither_platform_warns_while_both_shims_exist(tmp_path):
    """The repo ships both families, so a healthy install says nothing on either
    platform. This is the assertion that goes red if one family is deleted."""
    p = _write(tmp_path, REAL_SHAPE)
    for os_name in ("nt", "posix"):
        result = install.install(_write(tmp_path, REAL_SHAPE,
                                        name=f"s-{os_name}.json"),
                                 CMD, os_name=os_name)
        assert result["ok"], (os_name, result)
        assert "platform_warning" not in result, (os_name, result)
    assert p.is_file()


def test_a_missing_shim_is_reported(tmp_path):
    """DISCRIMINATING, and the canary for the test above: without this, a
    `platform_warning` that had been hard-wired to "" would pass every
    assertion in this file. `repo` points the resolver at an empty tree, which
    is what a checkout missing its shims actually looks like."""
    warning = install.platform_warning(os_name="posix", repo=str(tmp_path))
    assert "missing" in warning
    assert "stop.sh" in warning
    warning = install.platform_warning(os_name="nt", repo=str(tmp_path))
    assert "stop.ps1" in warning


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


def test_the_registered_command_names_a_shim_that_exists():
    """END TO END: the command string is only useful if it points at a real
    file. Asserted on disk rather than on the string, because a template typo
    (`hooks/stop.sh ` with a space, say) passes any string comparison.

    Parsed with `shlex` rather than `str.split`, which is not pedantry: the
    earlier version of this test split on the first space and so could only
    ever pass on a checkout whose path had none. It agreed with the bug."""
    cmd = install.default_command(os_name="posix")
    assert Path(shlex.split(cmd)[-1]).is_file(), cmd
    cmd = install.default_command(os_name="nt")
    assert Path(shlex.split(cmd)[-1]).is_file(), cmd


def test_a_repo_path_containing_a_space_stays_one_argument():
    """DISCRIMINATING: this is the whole reason the template quotes `{shim}`.
    Unquoted, `shlex` sees four words after `-File` instead of one, which is
    exactly what PowerShell would see -- it would read `-File C:/My` and fail
    at the end of every agent turn.

    `repo` is injected rather than the test moving a real checkout, so the
    assertion runs identically on a CI runner whose own path is boring."""
    weird = "C:/My Repo (v2)/mind hive"
    for os_name, tail in (("nt", "hooks/stop.ps1"), ("posix", "hooks/stop.sh")):
        cmd = install.default_command(os_name=os_name, repo=weird)
        parts = shlex.split(cmd)
        assert parts[-1] == weird + "/" + tail, (os_name, cmd, parts)
        assert " " in parts[-1]          # the space really is still in there
        # nt: powershell -NoProfile -ExecutionPolicy Bypass -File <shim>
        assert len(parts) == (6 if os_name == "nt" else 2), (os_name, parts)


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


# --- what a command EXECUTES, not what it mentions (round-5 review) ----------

def _posix_shim():
    """The POSIX twin of `_nt_shim`, read back out of the registered template."""
    return shlex.split(install.default_command(os_name="posix"))[-1]


@pytest.mark.parametrize("mention", [
    lambda s: f"echo {s}",
    lambda s: f"rem {s}",
    lambda s: f'cmd /c type "{s}"',
    lambda s: f'powershell -NoProfile -Command Write-Output "{s}"',
    lambda s: f"powershell -NoProfile Write-Output -File {s}",
    lambda s: f"powershell -Co x -File {s}",
    lambda s: f'sh -c cat "{s}"',
], ids=["echo", "rem", "cmd-type", "powershell-command", "powershell-positional",
        "powershell-ambiguous-parameter", "sh-c"])
def test_a_command_that_only_mentions_the_shim_is_not_a_registration(tmp_path,
                                                                     mention):
    """DISCRIMINATING. The rule was whether ANY word named a Stop shim, so each
    of these -- none of which runs stop.ps1 -- answered `already_registered`,
    and `install-hook` reported a hook registered that never runs. The shim
    path is in every one of them, as its own word."""
    existing = mention(_nt_shim())
    p = _settings_with_stop(tmp_path, existing)
    result = install.install(p, None, os_name="nt")
    assert result["reason"] == "installed", (existing, result)
    assert len(_stop_groups(p)) == 2


@pytest.mark.parametrize("spell", [
    lambda s: f"powershell -nop -ep Bypass -f {s}",
    lambda s: f'pwsh -WindowStyle Hidden -File "{s}"',
    lambda s: f'"C:/Program Files/PowerShell/7/pwsh.exe" -NonInteractive -File {s}',
], ids=["short-names", "valued-parameter", "full-host-path"])
def test_the_shim_behind_known_parameters_is_already_registered(tmp_path, spell):
    """CANARY for the test above: parsing the parameters must keep the
    spellings that DO run the shim. `-ep Bypass` takes a value and `-nop` does
    not -- the distinction the positional case above depends on."""
    existing = spell(_nt_shim())
    p = _settings_with_stop(tmp_path, existing)
    before = p.read_text(encoding="utf-8")
    result = install.install(p, None, os_name="nt")
    assert result["reason"] == "already_registered", (existing, result)
    assert result["matched"] == "shim"
    assert p.read_text(encoding="utf-8") == before


def test_a_prompt_hook_carrying_the_command_text_is_not_a_registration(tmp_path):
    """DISCRIMINATING. Only a `command` hook runs a command. A `prompt` entry
    holding the very string `install` registers, under `command`, runs nothing,
    and matching it reported the Stop shim registered."""
    obj = json.loads(json.dumps(REAL_SHAPE))
    obj["hooks"]["Stop"] = [{"hooks": [{
        "type": "prompt", "command": install.default_command(os_name="nt")}]}]
    p = _write(tmp_path, obj)
    result = install.install(p, None, os_name="nt")
    assert result["reason"] == "installed", result
    assert len(_stop_groups(p)) == 2


@pytest.mark.parametrize("spell", [
    lambda s: f'sh "{s}"',
    lambda s: f"sh -e {s}",
    lambda s: f"bash --noprofile -o pipefail {s}",
    lambda s: f"/bin/sh -- {s}",
    lambda s: s,
], ids=["sh-quoted", "sh-option", "bash-long-option-and-valued-o",
        "bin-sh-double-dash", "direct"])
def test_the_posix_shim_in_another_spelling_is_already_registered(tmp_path,
                                                                  spell):
    """The POSIX half of the executed-script rule: interpreter options before the
    script are skipped, `-o` with its value, and `--` ends them."""
    existing = spell(_posix_shim())
    p = _settings_with_stop(tmp_path, existing)
    before = p.read_text(encoding="utf-8")
    result = install.install(p, None, os_name="posix")
    assert result["reason"] == "already_registered", (existing, result)
    assert p.read_text(encoding="utf-8") == before


@pytest.mark.parametrize("spell", [lambda s: s.upper(),
                                   lambda s: s.replace("/", "\\")],
                         ids=["upper-case", "backslashes"])
def test_a_posix_respelling_names_another_file(tmp_path, spell):
    """DISCRIMINATING. POSIX paths are case-sensitive and a backslash is not a
    separator there, so `/REPO/HOOKS/STOP.SH` is another file or none -- yet the
    Windows folding was applied on every platform, and it read as this
    checkout's registered shim."""
    respelled = spell(_posix_shim())
    if os.name != "nt" and os.path.exists(respelled):
        pytest.skip("this volume folds case, so the respelled path IS the shim")
    p = _settings_with_stop(tmp_path, f'sh "{respelled}"')
    result = install.install(p, None, os_name="posix")
    assert result["reason"] == "installed", (respelled, result)


@pytest.mark.parametrize("value", [5, True, 1.5, None, "stop.ps1",
                                   {"type": "command", "command": CMD}],
                         ids=["int", "bool", "float", "null", "string", "object"])
def test_a_group_whose_hooks_is_not_a_list_is_refused(tmp_path, value):
    """DISCRIMINATING. `group.get("hooks") or []` iterated whatever was there: an
    int or a bool raised TypeError out of `install-hook`, and null, a string or
    an object was passed over and the Stop hook appended beside a group Claude
    Code cannot read. Refused like a Stop stage that is not a list: nothing
    written, no backup."""
    obj = json.loads(json.dumps(REAL_SHAPE))
    obj["hooks"]["Stop"] = [{"hooks": value}]
    p = _write(tmp_path, obj)
    before = p.read_text(encoding="utf-8")
    result = install.install(p, None, os_name="nt")
    assert result == {"ok": False, "reason": "bad_hooks", "path": str(p)}, result
    assert p.read_text(encoding="utf-8") == before
    assert not list(tmp_path.glob("*.bak-*"))


def test_canary_a_group_without_a_hooks_key_is_still_installed_beside(tmp_path):
    obj = json.loads(json.dumps(REAL_SHAPE))
    obj["hooks"]["Stop"] = [{"matcher": ""}]
    p = _write(tmp_path, obj)
    assert install.install(p, None, os_name="nt")["reason"] == "installed"


def test_a_relative_shim_path_is_not_this_checkouts_registration(tmp_path,
                                                                 monkeypatch):
    """DISCRIMINATING against the file comparison below. A relative path runs
    whatever `hooks/stop.ps1` is under the directory Claude Code runs the hook
    from -- every project on the machine -- so it is not this checkout's shim,
    even when `install-hook` itself runs from the checkout root."""
    monkeypatch.chdir(install._repo_root())
    existing = ("powershell -NoProfile -File hooks/stop.ps1" if os.name == "nt"
                else "sh hooks/stop.sh")
    p = _settings_with_stop(tmp_path, existing)
    result = install.install(p, None)
    assert result["reason"] == "installed", result


def _this_platforms_shim():
    return Path(_nt_shim() if os.name == "nt" else _posix_shim())


def _command_for(path):
    return (f'powershell -NoProfile -File "{path}"' if os.name == "nt"
            else f'sh "{path}"')


def test_a_hard_link_to_the_shim_is_the_same_registration(tmp_path):
    """Another path to the same FILE runs the same shim, so it is the
    registration already in the file."""
    shim = _this_platforms_shim()
    link = tmp_path / "hooks" / shim.name
    link.parent.mkdir()
    try:
        os.link(shim, link)
    except OSError as exc:
        pytest.skip(f"cannot hard-link the shim here: {exc}")
    p = _settings_with_stop(tmp_path, _command_for(link))
    before = p.read_text(encoding="utf-8")
    result = install.install(p, None)
    assert result["reason"] == "already_registered", result
    assert result["matched"] == "shim"
    assert p.read_text(encoding="utf-8") == before


def test_canary_a_copy_of_the_shim_is_another_file(tmp_path):
    """CANARY for the test above: the same bytes under another path are another
    file, and a comparison that matched on the file name would pass the link
    test while wrongly refusing this."""
    shim = _this_platforms_shim()
    copy = tmp_path / "hooks" / shim.name
    copy.parent.mkdir()
    shutil.copyfile(shim, copy)
    p = _settings_with_stop(tmp_path, _command_for(copy))
    result = install.install(p, None)
    assert result["reason"] == "installed", result
