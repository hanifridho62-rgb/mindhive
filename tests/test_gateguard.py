# tests/test_gateguard.py
"""Tests for the GateGuard port (gateguard.py) — destructive-command
classification, git hook-bypass detection, and lint/format config protection.

Ported from ECC's gateguard-fact-force.js / block-no-verify.js /
config-protection.js. The adversarial cases mirror the classifier bypasses
ECC fixed under GHSA-4v57-ph3x-gf55 (quoted command words, newline
separators, quoted `find -exec`, `sh -c` wrappers) — a port that misses
those is a port of the vulnerable version.
"""
import pytest

from mindhive import gateguard


# --------------------------------------------------------------------------
# destructive commands — should flag
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "rm -rf /tmp/x",
    "rm -fr build",
    "rm -Rf build",
    "rm -r -f build",
    "rm --recursive --force build",
    "/usr/bin/rm -rf x",
    "rm.exe -rf x",
])
def test_destructive_rm_forms(cmd):
    assert gateguard.is_destructive_command(cmd)


@pytest.mark.parametrize("cmd", [
    "git reset --hard",
    "git reset --hard HEAD~1",
    "git -C /repo reset --hard",             # global flag skipped
    "git -c user.name=x reset --hard",       # -c consumes its value
    "git checkout -- .",
    "git checkout .",
    "git checkout -f main",
    "git clean -fd",
    "git clean --force",
    "git push --force",
    "git push -f origin main",
    "git push origin +main",                 # + refspec forces the update
    "git push --force --force-if-includes",  # bare force still in effect
    "git commit --amend",
    "git rm -r src",
    "git rm -rf src",
    "git switch -C feature",
    "git switch --discard-changes",
    "git switch -f main",
])
def test_destructive_git_forms(cmd):
    assert gateguard.is_destructive_command(cmd)


@pytest.mark.parametrize("cmd", [
    "find . -name '*.tmp' -exec rm {} \\;",
    "find /tmp -exec rm -rf {} +",
    "find . -exec rmdir {} \\;",
    "find . -exec unlink {} \\;",
    "find . -exec git reset --hard {} \\;",
])
def test_destructive_find_exec(cmd):
    assert gateguard.is_destructive_command(cmd)


@pytest.mark.parametrize("cmd", [
    "DROP TABLE users",
    "psql -c 'x'; drop   table users",
    "delete from accounts where 1=1",
    "truncate audit_log",
    "dd if=/dev/zero of=/dev/sda",
])
def test_destructive_sql_dd_phrases(cmd):
    assert gateguard.is_destructive_command(cmd)


@pytest.mark.parametrize("cmd", [
    "Remove-Item -Recurse -Force C:/tmp/x",
    "Remove-Item C:/tmp/x -Force -Recurse",
    "ri -Recurse -Force build",
])
def test_destructive_powershell_remove_item(cmd):
    # Hive extension beyond the faithful port: the live PreToolUse matcher
    # includes the PowerShell tool, and this machine IS Windows.
    assert gateguard.is_destructive_command(cmd)


@pytest.mark.parametrize("cmd", [
    "echo done && rm -rf /tmp/x",            # chained
    "echo $(rm -rf /tmp/x)",                 # command substitution
    "echo `rm -rf /tmp/x`",                  # backticks
    "sh -c 'rm -rf /tmp/x'",                 # wrapper recursion
    "bash -c 'git reset --hard'",
    "echo hi\nrm -rf /tmp/x",                # newline separator
    "'rm' -rf /tmp/x",                       # quoted command word
    '"rm" -rf /tmp/x',
    "(rm -rf /tmp/x)",                       # subshell group
    "find . -exec 'rm' {} \\;",              # quoted exec binary
])
def test_destructive_bypass_forms(cmd):
    assert gateguard.is_destructive_command(cmd)


# --------------------------------------------------------------------------
# destructive commands — must NOT flag
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "rm file.txt",
    "rm -r build",                           # recursive but not forced
    "rm -f file.txt",                        # forced but not recursive
    "grep -rf patterns.txt src",             # rm flags on a different binary
    "git status",
    "git push",
    "git push --force-with-lease origin main",
    "git checkout main",                     # plain branch switch
    "git switch main",
    "git clean -n",                          # dry run
    "git reset HEAD~1",                      # soft/mixed reset keeps work
    "echo \"rm -rf /tmp/x\"",                # quoted mention, not a command
    "git commit -m 'docs: explain reset --hard'",
    "echo 'drop table users'",               # SQL phrase inside quotes
    "find . -name '*.py' -exec ls {} \\;",   # non-destructive exec
    "Remove-Item C:/tmp/file.txt",           # single file, not recursive+force
    "python -m pytest tests -q",
])
def test_safe_commands_allowed(cmd):
    assert gateguard.is_destructive_command(cmd) is None


def test_destructive_handles_garbage_input():
    assert gateguard.is_destructive_command("") is None
    assert gateguard.is_destructive_command(None) is None


# --------------------------------------------------------------------------
# --no-verify / hooksPath bypass
# --------------------------------------------------------------------------

@pytest.mark.parametrize("cmd", [
    "git commit --no-verify -m 'x'",
    "git commit -n",
    "git commit -nm 'x'",
    "git push --no-verify",
    "git merge --no-verify topic",
    "git cherry-pick --no-verify abc123",
    "git rebase --no-verify main",
    "git am --no-verify patch.mbox",
    "git -c core.hooksPath=/dev/null commit -m 'x'",
    "git -c core.HOOKSPATH=/tmp/empty commit -m 'x'",   # keys case-insensitive
    "git -ccore.hookspath=/tmp/empty commit -m 'x'",    # inline -c form
    "echo ok && git commit --no-verify -m 'x'",         # later chain segment
])
def test_no_verify_blocked_forms(cmd):
    assert gateguard.check_no_verify(cmd)


@pytest.mark.parametrize("cmd", [
    "git commit -m 'never use --no-verify'",  # flag text inside -m value
    "git commit --message='--no-verify'",     # inline value form
    "git commit -m 'x'",
    "git push origin main",
    "git log --no-verify",                    # log doesn't take the flag
    "echo 'git commit --no-verify'",          # quoted mention only
    "git commit",
    "ls -n",                                  # -n on a non-git command
])
def test_no_verify_allowed_forms(cmd):
    assert gateguard.check_no_verify(cmd) is None


def test_no_verify_handles_garbage_input():
    assert gateguard.check_no_verify("") is None
    assert gateguard.check_no_verify(None) is None


# --------------------------------------------------------------------------
# config protection
# --------------------------------------------------------------------------

def test_config_protection_blocks_existing_config(tmp_path):
    target = tmp_path / ".eslintrc.json"
    target.write_text("{}", encoding="utf-8")
    reason = gateguard.protected_config_file(
        "Edit", {"file_path": str(target)})
    assert reason and ".eslintrc.json" in reason


def test_config_protection_allows_first_creation(tmp_path):
    target = tmp_path / "eslint.config.js"        # does not exist
    assert gateguard.protected_config_file(
        "Write", {"file_path": str(target)}) is None


def test_config_protection_ignores_normal_files(tmp_path):
    target = tmp_path / "README.md"
    target.write_text("x", encoding="utf-8")
    assert gateguard.protected_config_file(
        "Edit", {"file_path": str(target)}) is None


def test_config_protection_ignores_other_tools():
    assert gateguard.protected_config_file(
        "Bash", {"command": "cat .eslintrc.json"}) is None


def test_config_protection_handles_missing_input():
    assert gateguard.protected_config_file("Edit", {}) is None
    assert gateguard.protected_config_file("Edit", None) is None
