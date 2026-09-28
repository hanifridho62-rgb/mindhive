# tests/test_plugin_cache.py
"""Bug 8 (spec section 2.8): Claude Code loads this repository's plugin from its
own cache (`~/.claude/plugins/cache/<marketplace>/<plugin>/<version>/`), not from
the checkout. When the two drift, every session runs instructions the
repository no longer contains.

The drift the spec describes is a version mismatch (repo 0.5.0, cache 0.3.0).
The drift this machine actually has is worse: both say 0.5.0, and the cache
holds a skill the checkout does not -- a version-string compare calls that "in
sync". So `mindhive.plugin_cache` compares a content hash of the plugin's
components (manifest, skills, commands, agents, hooks.json, .mcp.json) as well
as the version, reports exactly which files differ, and syncs through Claude
Code's own `claude plugin update` -- never by writing into the cache itself.

The session-start check (`--hook`) is fail-open (a broken check prints nothing and exits
0) and the automatic sync is opt-in (`MINDHIVE_PLUGIN_AUTOSYNC=1`): the hook
fires in every session on the machine, so by default it reports and names the
fix rather than rewriting the user's plugin install.
"""
import hashlib
import json
import os
import shlex
import signal
import subprocess
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest

from mindhive import __main__ as cli_mod
from mindhive import plugin_cache

REPO_ROOT = Path(__file__).resolve().parents[1]
KEY = "mindhive@mindhive-dev"


# --- fixture builders -------------------------------------------------------

def _write(path, text, newline="\n"):
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(text.replace("\n", newline).encode("utf-8"))
    return path


def _plugin_tree(root, version="0.5.0", skills=("circle", "caveman"), newline="\n"):
    """A plugin checkout (or cache copy) with a manifest, skills and a command."""
    _write(root / ".claude-plugin" / "plugin.json",
           json.dumps({"name": "mindhive", "version": version}), newline)
    for slug in skills:
        _write(root / "skills" / slug / "SKILL.md",
               f"---\nname: {slug}\ndescription: Use for {slug}.\n---\n\n# {slug}\n",
               newline)
    _write(root / "commands" / "circle.md", "# /circle\nRun the loop.\n", newline)
    return root


def _claude(tmp_path, entries=None, marketplaces=None):
    """A Claude config dir whose registry and marketplaces are given."""
    root = tmp_path / "claude"
    _write(root / "plugins" / "installed_plugins.json",
           json.dumps({"version": 2, "plugins": entries or {}}))
    _write(root / "plugins" / "known_marketplaces.json",
           json.dumps(marketplaces or {}))
    return root


def _installed(tmp_path, repo, cache, *, version="0.5.0", key=KEY, scope="user"):
    """Register `cache` as the install of `key`, from a marketplace at `repo`."""
    marketplace = key.rpartition("@")[2]
    return _claude(
        tmp_path,
        entries={key: [{"scope": scope, "installPath": str(cache),
                        "version": version}]},
        marketplaces={marketplace: {"source": {"source": "directory",
                                               "path": str(repo)}}})


def _pair(tmp_path, *, repo_version="0.5.0", cache_version="0.5.0",
          repo_skills=("circle", "caveman"), cache_skills=("circle", "caveman")):
    repo = _plugin_tree(tmp_path / "repo", repo_version, repo_skills)
    cache = _plugin_tree(tmp_path / "cache" / cache_version, cache_version, cache_skills)
    claude = _installed(tmp_path, repo, cache, version=cache_version)
    return repo, cache, claude


def _status(state="diverged", **over):
    base = {"state": state, "plugin": "mindhive", "key": KEY, "scope": "user",
            "repo_version": "0.5.0", "cache_version": "0.5.0",
            "repo_hash": "a" * 64, "cache_hash": "b" * 64,
            "install_path": "C:/cache/0.5.0",
            "only_in_repo": [], "only_in_cache": ["skills/extra/SKILL.md"],
            "changed": [], "diff_counts": {"only_in_repo": 0, "only_in_cache": 1,
                                           "changed": 0},
            "reason": ""}
    base.update(over)
    return base


# --- the tree hash ----------------------------------------------------------

def test_the_real_checkout_hashes_its_plugin_components_only():
    """Pins the committed plugin, not a fixture."""
    digest = plugin_cache.plugin_tree_digest(REPO_ROOT)
    assert ".claude-plugin/plugin.json" in digest
    assert any(p.startswith("skills/") and p.endswith("/SKILL.md") for p in digest)
    assert not any(p.startswith(("src/", "tests/", "hooks/")) for p in digest)
    whole = plugin_cache.plugin_tree_hash(REPO_ROOT)
    assert len(whole) == 64 and int(whole, 16) >= 0


def test_the_components_are_the_ones_claude_code_loads():
    assert plugin_cache.PLUGIN_COMPONENTS == (
        ".claude-plugin/plugin.json", "skills", "commands", "agents",
        "hooks/hooks.json", ".mcp.json")


def test_line_endings_do_not_count_but_content_does(tmp_path):
    lf = _plugin_tree(tmp_path / "lf")
    crlf = _plugin_tree(tmp_path / "crlf", newline="\r\n")
    assert plugin_cache.plugin_tree_hash(lf) == plugin_cache.plugin_tree_hash(crlf)
    # canary: one changed byte of content is a different tree
    _write(crlf / "skills" / "circle" / "SKILL.md", "---\nname: circle\n---\nchanged\n")
    assert plugin_cache.plugin_tree_hash(lf) != plugin_cache.plugin_tree_hash(crlf)


def test_files_outside_the_components_do_not_count(tmp_path):
    root = _plugin_tree(tmp_path / "p")
    before = plugin_cache.plugin_tree_hash(root)
    _write(root / "README.md", "# readme\n")
    _write(root / "src" / "mindhive" / "x.py", "x = 1\n")
    _write(root / "hooks" / "session_start.sh", "#!/bin/sh\n")
    (root / "skills" / "circle" / "__pycache__").mkdir()
    (root / "skills" / "circle" / "__pycache__" / "a.cpython-314.pyc").write_bytes(b"\0")
    assert plugin_cache.plugin_tree_hash(root) == before
    # canaries: each component that is not in the fixture yet does count
    _write(root / "hooks" / "hooks.json", "{}")
    after_hooks = plugin_cache.plugin_tree_hash(root)
    assert after_hooks != before
    _write(root / "agents" / "reviewer.md", "# reviewer\n")
    assert plugin_cache.plugin_tree_hash(root) != after_hooks


def test_a_missing_tree_hashes_to_empty(tmp_path):
    assert plugin_cache.plugin_tree_hash(tmp_path / "nope") == ""
    assert plugin_cache.plugin_tree_digest(tmp_path / "nope") == {}


def test_the_tree_hash_is_sha256_over_sorted_path_and_file_digests(tmp_path):
    root = _plugin_tree(tmp_path / "p", skills=("circle",))
    digest = plugin_cache.plugin_tree_digest(root)
    body = "".join(f"{rel}\0{digest[rel]}\n" for rel in sorted(digest))
    assert plugin_cache.plugin_tree_hash(root) == \
        hashlib.sha256(body.encode("utf-8")).hexdigest()
    manifest = (root / ".claude-plugin" / "plugin.json").read_bytes()
    assert digest[".claude-plugin/plugin.json"] == hashlib.sha256(manifest).hexdigest()


# --- the status -------------------------------------------------------------

def test_an_identical_cache_is_in_sync(tmp_path):
    repo, cache, claude = _pair(tmp_path)
    st = plugin_cache.cache_status(repo_root=repo, claude_dir=claude)
    assert st["state"] == "in_sync"
    assert st["key"] == KEY and st["scope"] == "user"
    assert st["repo_version"] == st["cache_version"] == "0.5.0"
    assert st["repo_hash"] == st["cache_hash"] != ""
    assert st["only_in_repo"] == st["only_in_cache"] == st["changed"] == []
    assert Path(st["install_path"]) == cache


def test_the_same_version_with_other_content_is_divergence(tmp_path):
    """The case on this machine: both 0.5.0, the cache holds a skill the
    checkout does not."""
    repo, cache, claude = _pair(
        tmp_path, cache_skills=("circle", "caveman", "hive-grilled-planning"))
    _write(cache / "commands" / "circle.md", "# /circle\nAn older loop.\n")
    st = plugin_cache.cache_status(repo_root=repo, claude_dir=claude)
    assert st["state"] == "diverged"
    assert st["repo_version"] == st["cache_version"] == "0.5.0"
    assert st["only_in_cache"] == ["skills/hive-grilled-planning/SKILL.md"]
    assert st["only_in_repo"] == []
    assert st["changed"] == ["commands/circle.md"]
    assert st["diff_counts"] == {"only_in_repo": 0, "only_in_cache": 1, "changed": 1}


def test_a_version_mismatch_is_divergence(tmp_path):
    """The case the spec names: repo 0.5.0, cache 0.3.0."""
    repo, _cache, claude = _pair(tmp_path, cache_version="0.3.0")
    st = plugin_cache.cache_status(repo_root=repo, claude_dir=claude)
    assert st["state"] == "diverged"
    assert (st["repo_version"], st["cache_version"]) == ("0.5.0", "0.3.0")
    assert st["changed"] == [".claude-plugin/plugin.json"]


def test_the_diff_lists_are_capped_but_the_counts_are_not(tmp_path):
    many = tuple(f"s{i:02d}" for i in range(plugin_cache.DIFF_LIST_CAP + 5))
    repo, _cache, claude = _pair(tmp_path, repo_skills=("circle", "caveman") + many)
    st = plugin_cache.cache_status(repo_root=repo, claude_dir=claude)
    assert len(st["only_in_repo"]) == plugin_cache.DIFF_LIST_CAP
    assert st["diff_counts"]["only_in_repo"] == plugin_cache.DIFF_LIST_CAP + 5


def test_nothing_to_compare_is_a_state_not_an_exception(tmp_path):
    repo = _plugin_tree(tmp_path / "repo")
    empty = _claude(tmp_path)
    assert plugin_cache.cache_status(repo_root=repo, claude_dir=empty)["state"] == \
        "not_installed"
    gone = _installed(tmp_path, repo, tmp_path / "cache" / "deleted")
    st = plugin_cache.cache_status(repo_root=repo, claude_dir=gone)
    assert st["state"] == "missing_on_disk"
    assert st["key"] == KEY
    bare = tmp_path / "not-a-plugin"
    bare.mkdir()
    assert plugin_cache.cache_status(repo_root=bare, claude_dir=gone)["state"] == \
        "no_manifest"
    # a registry that is not JSON reads as "nothing installed", never a raise
    _write(tmp_path / "claude" / "plugins" / "installed_plugins.json", "{broken")
    assert plugin_cache.cache_status(repo_root=repo, claude_dir=tmp_path / "claude")[
        "state"] == "not_installed"


@pytest.mark.parametrize("body", [
    [1], "text", {"version": 2, "plugins": [1]},
    {"version": 2, "plugins": {KEY: 5}}, {"version": 2, "plugins": {KEY: [5, "x"]}},
], ids=["list", "string", "plugins-list", "entry-int", "entries-not-dicts"])
def test_a_registry_of_the_wrong_shape_is_a_state_not_an_exception(tmp_path, body):
    """`{broken` above fails to PARSE. JSON that parses into the wrong type
    is the other half: a registry of `[1]` raised AttributeError out of
    `plugins._read_registry`, through `_pick_install`."""
    repo = _plugin_tree(tmp_path / "repo")
    claude = _claude(tmp_path)
    _write(claude / "plugins" / "installed_plugins.json", json.dumps(body))
    assert plugin_cache.cache_status(repo_root=repo, claude_dir=claude)["state"] == \
        "not_installed"


def test_the_install_from_this_checkouts_marketplace_is_the_one_compared(tmp_path):
    repo = _plugin_tree(tmp_path / "repo")
    ours = _plugin_tree(tmp_path / "cache" / "ours")
    theirs = _plugin_tree(tmp_path / "cache" / "theirs", version="9.9.9")
    claude = _claude(
        tmp_path,
        entries={"mindhive@elsewhere": [{"scope": "user", "installPath": str(theirs),
                                         "version": "9.9.9"}],
                 KEY: [{"scope": "user", "installPath": str(ours),
                        "version": "0.5.0"}]},
        marketplaces={"elsewhere": {"source": {"source": "directory",
                                               "path": str(tmp_path / "other")}},
                      "mindhive-dev": {"source": {"source": "directory",
                                                  "path": str(repo)}}})
    st = plugin_cache.cache_status(repo_root=repo, claude_dir=claude)
    assert st["key"] == KEY
    assert st["state"] == "in_sync"
    # canary: without the marketplace that names this checkout, the newest
    # install of the plugin is the one compared -- and that one differs
    _write(claude / "plugins" / "known_marketplaces.json", "{}")
    st = plugin_cache.cache_status(repo_root=repo, claude_dir=claude)
    assert st["key"] == "mindhive@elsewhere"
    assert st["state"] == "diverged"


# --- the notice -------------------------------------------------------------

def test_the_session_notice_names_the_drift_and_the_fix():
    note = plugin_cache.session_notice(_status())
    assert "\n" not in note.strip()
    for needle in ("mindhive", "0.5.0", "skills/extra/SKILL.md",
                   "claude plugin update mindhive@mindhive-dev",
                   plugin_cache.AUTOSYNC_ENV):
        assert needle in note


def test_a_missing_install_gets_a_notice_too():
    note = plugin_cache.session_notice(
        _status("missing_on_disk", install_path="C:/cache/gone"))
    assert "C:/cache/gone" in note
    assert "claude plugin update mindhive@mindhive-dev" in note


def test_nothing_to_report_is_silent():
    for state in ("in_sync", "not_installed", "no_manifest"):
        assert plugin_cache.session_notice(_status(state)) == ""


# --- the sync ---------------------------------------------------------------

def _fake_run(calls, returncode=0, stdout="updated", stderr="", exc=None):
    def run(argv, **kw):
        calls.append((argv, kw))
        if exc is not None:
            raise exc
        return SimpleNamespace(returncode=returncode, stdout=stdout, stderr=stderr)
    return run


def test_the_sync_is_claude_codes_own_update_command():
    assert plugin_cache.sync_command(_status()) == [
        "claude", "plugin", "update", KEY, "--scope", "user"]
    assert plugin_cache.sync_command(_status(scope="project"))[-1] == "project"


def test_sync_runs_the_update_without_a_shell_and_reports_success():
    calls, rechecked = [], []
    res = plugin_cache.sync_cache(
        _status(), run=_fake_run(calls), which=lambda name: "C:/bin/claude.exe",
        recheck=lambda: rechecked.append(1) or _status("in_sync"))
    assert res["ok"] is True
    assert len(calls) == 1
    argv, kw = calls[0]
    assert argv == ["C:/bin/claude.exe", "plugin", "update", KEY, "--scope", "user"]
    assert kw["shell"] is False
    assert kw["timeout"] == plugin_cache.SYNC_TIMEOUT_S
    assert "updated" in res["output"]
    assert rechecked == [1], "a sync was called done without comparing again"


def test_the_sync_timeout_is_45_seconds_inside_the_hooks_60():
    """The number the README and the legend promise, pinned as a number. The
    call-site test above compares the call to the constant, so a constant
    raised past Claude Code's 60 s hook limit would still pass it."""
    assert plugin_cache.SYNC_TIMEOUT_S == 45.0
    assert plugin_cache.SYNC_TIMEOUT_S < 60.0


def test_an_update_that_exits_zero_but_leaves_the_drift_is_not_a_sync(tmp_path):
    """Exit 0 is a proxy. `claude plugin update` updates to the latest
    VERSION, and the drift this machine has is same-version, so whether it
    rewrites the copy is not something the exit code says -- the re-check
    does."""
    repo, cache, claude = _pair(tmp_path)
    _write(cache / "skills" / "stale" / "SKILL.md", "---\nname: stale\n---\n")
    status = plugin_cache.cache_status(repo_root=repo, claude_dir=claude)
    assert status["state"] == "diverged"
    res = plugin_cache.sync_cache(status, run=_fake_run([]), which=lambda n: "claude")
    assert res["ok"] is False and res["exit"] == 0
    assert "still" in res["reason"] and "diverged" in res["reason"], res["reason"]

    # canary: an update that does repair the copy is reported as the sync it was
    def repairing(argv, **kw):
        (cache / "skills" / "stale" / "SKILL.md").unlink()
        return SimpleNamespace(returncode=0, stdout="updated", stderr="")

    res = plugin_cache.sync_cache(status, run=repairing, which=lambda n: "claude")
    assert res["ok"] is True, res


def _tree_snapshot(root):
    """Every file under `root` -- components or not -- with its bytes' hash and
    its mtime, so a rewrite of identical bytes is seen as well."""
    root = Path(root)
    return {f.relative_to(root).as_posix(): (hashlib.sha256(f.read_bytes()).hexdigest(),
                                             f.stat().st_mtime_ns)
            for f in sorted(root.rglob("*")) if f.is_file()}


def test_no_path_through_the_check_writes_into_the_cache(tmp_path, capsys, monkeypatch):
    """Repair goes through `claude plugin update`, never by writing into the
    cache: the registry and the copy are Claude Code's to keep in step. Every
    path -- the status, the hook with the sync opted in, `--sync` -- runs the
    REAL `sync_cache` against a drifted copy, with a stand-in CLI that changes
    nothing; the copy and the registry must be identical afterwards."""
    monkeypatch.setenv(plugin_cache.AUTOSYNC_ENV, "1")
    calls = []
    monkeypatch.setattr(plugin_cache, "_run_bounded", _fake_run(calls))
    monkeypatch.setattr(plugin_cache.shutil, "which", lambda name: "C:/fake/claude.exe")
    repo, cache, claude = _pair(tmp_path)
    _write(cache / "skills" / "stale" / "SKILL.md", "---\nname: stale\n---\n")
    _write(cache / "src" / "not_a_component.py", "x = 1\n")
    before = (_tree_snapshot(cache), _tree_snapshot(claude))
    plugin_cache.cache_status(repo_root=repo, claude_dir=claude)
    assert _check(repo, claude, "--hook") == 0
    assert _check(repo, claude, "--sync") == 1          # the update changed nothing
    assert len(calls) == 2, "the sync was not reached on both paths"
    assert (_tree_snapshot(cache), _tree_snapshot(claude)) == before
    # canary: the snapshot does see a write into the cache
    _write(cache / "skills" / "circle" / "SKILL.md", "rewritten\n")
    assert _tree_snapshot(cache) != before[0]


# --- the timeout holds for the process TREE -----------------------------------
#
# Measured on this machine before the fix: `claude` resolves to `claude.CMD`,
# whose body runs the real CLI as a grandchild of cmd.exe. `subprocess.run`'s
# timeout killed cmd.exe alone and then drained the pipes the grandchild still
# held, with no bound -- a 2 s timeout came back after 30.27 s (`sync_cache`) and
# 30.12 s (`environment._probe`), the grandchild's own lifetime. These stubs
# have that shape, and the tests drive the REAL runner, no `run` seam.

STUB_SLEEP_S = 30
# How far past its timeout a call may come back: the tree kill plus
# `driver.reap`'s bounded close. Generous for a loaded machine, and still far
# below the stub's 30 s, which is what the unfixed path waited for.
TREE_SLACK_S = 6.0


def sleeping_tree_stub(tmp_path):
    """A `claude` stand-in whose work runs as a GRANDCHILD that holds the pipes
    and writes its pid to a file. Answers ``(stub, pidfile)``; skips only where
    the stub cannot be built."""
    bindir = tmp_path / "bin"
    bindir.mkdir()
    pidfile = tmp_path / "grandchild.pid"
    code = ("import os, pathlib, sys, time; "
            "pathlib.Path(sys.argv[1]).write_text(str(os.getpid())); "
            f"time.sleep({STUB_SLEEP_S})")
    if sys.platform == "win32":
        if not os.environ.get("COMSPEC"):
            pytest.skip("no cmd.exe here to run a .cmd stub")
        stub = bindir / "claude.cmd"
        stub.write_bytes((f'@echo off\r\n"{sys.executable}" -c "{code}" '
                          f'"{pidfile}"\r\n').encode("utf-8"))
    else:
        if not Path("/bin/sh").exists():
            pytest.skip("no /bin/sh here to run a shell stub")
        stub = bindir / "claude"
        stub.write_text("#!/bin/sh\n"
                        f"{shlex.quote(sys.executable)} -c {shlex.quote(code)} "
                        f"{shlex.quote(str(pidfile))} &\nwait\n", encoding="utf-8")
        stub.chmod(0o755)
    return stub, pidfile


def pid_of(pidfile):
    try:
        return int(Path(pidfile).read_text(encoding="utf-8").strip())
    except (OSError, ValueError):
        return None


def _alive(pid) -> bool:
    """Whether `pid` is still running. Never signals it: on Windows
    `os.kill(pid, 0)` is CTRL_C_EVENT, not a probe."""
    if sys.platform == "win32":
        import ctypes
        k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        k32.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
        k32.OpenProcess.restype = ctypes.c_void_p
        k32.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
        k32.WaitForSingleObject.restype = ctypes.c_uint32
        k32.CloseHandle.argtypes = [ctypes.c_void_p]
        handle = k32.OpenProcess(0x00100000 | 0x1000, False, pid)  # SYNCHRONIZE|QUERY
        if not handle:
            return False
        try:
            return k32.WaitForSingleObject(handle, 0) != 0          # 0: it has exited
        finally:
            k32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    try:                                        # a zombie still answers signal 0
        return Path(f"/proc/{pid}/stat").read_text().rsplit(")", 1)[1].split()[0] != "Z"
    except (OSError, IndexError):
        return True


def process_gone(pid, within_s=5.0) -> bool:
    deadline = time.monotonic() + within_s
    while _alive(pid):
        if time.monotonic() >= deadline:
            return False
        time.sleep(0.1)
    return True


def kill_leftover(pid) -> None:
    """Test hygiene only: a grandchild the code under test failed to end."""
    if pid is not None and _alive(pid):
        try:
            os.kill(pid, signal.SIGTERM)
        except OSError:
            pass


def test_the_sync_timeout_holds_for_the_whole_process_tree(tmp_path):
    stub, pidfile = sleeping_tree_stub(tmp_path)
    timeout = 2.0
    started = time.monotonic()
    res = plugin_cache.sync_cache(_status(), which=lambda name: str(stub),
                                  timeout_s=timeout)
    elapsed = time.monotonic() - started
    pid = pid_of(pidfile)
    try:
        assert elapsed < timeout + TREE_SLACK_S, (
            f"a {timeout} s sync came back after {elapsed:.2f} s")
        assert res["ok"] is False and "TimeoutExpired" in res["reason"], res
        assert pid is not None, "the stub's grandchild never started"
        assert process_gone(pid), "the grandchild outlived the sync's timeout"
    finally:
        kill_leftover(pid)


def test_a_failed_update_is_reported_not_raised():
    calls = []
    res = plugin_cache.sync_cache(
        _status(), run=_fake_run(calls, returncode=1, stderr="no such plugin"),
        which=lambda name: "claude")
    assert res["ok"] is False and res["exit"] == 1
    assert "no such plugin" in res["output"]
    for exc in (subprocess.TimeoutExpired(["claude"], 1), OSError("denied")):
        res = plugin_cache.sync_cache(_status(), run=_fake_run([], exc=exc),
                                      which=lambda name: "claude")
        assert res["ok"] is False
        assert type(exc).__name__ in res["reason"]


def test_no_cli_on_path_means_no_spawn():
    calls = []
    res = plugin_cache.sync_cache(_status(), run=_fake_run(calls),
                                  which=lambda name: None)
    assert res["ok"] is False
    assert calls == []
    assert "claude" in res["reason"] and "PATH" in res["reason"]


def test_there_is_nothing_to_sync_unless_the_cache_drifted():
    for state in ("in_sync", "not_installed", "no_manifest"):
        calls = []
        res = plugin_cache.sync_cache(_status(state), run=_fake_run(calls),
                                      which=lambda name: "claude")
        assert calls == []
        assert res["ok"] is (state == "in_sync")
    # canary: a missing install is something the update can repair
    calls = []
    plugin_cache.sync_cache(_status("missing_on_disk"), run=_fake_run(calls),
                            which=lambda name: "claude",
                            recheck=lambda: _status("in_sync"))
    assert len(calls) == 1


def test_the_automatic_sync_is_opt_in():
    assert plugin_cache.AUTOSYNC_ENV == "MINDHIVE_PLUGIN_AUTOSYNC"
    assert plugin_cache.autosync_enabled({}) is False
    for off in ("", "0", "false", "no", "off"):
        assert plugin_cache.autosync_enabled({plugin_cache.AUTOSYNC_ENV: off}) is False
    for on in ("1", "true", "yes", "on", "TRUE"):
        assert plugin_cache.autosync_enabled({plugin_cache.AUTOSYNC_ENV: on}) is True


# --- hive plugins cache-check -------------------------------------------------

def _check(repo, claude, *extra):
    return cli_mod.main(["plugins", "cache-check", "--repo-root", str(repo),
                         "--claude-dir", str(claude), *extra])


def test_cache_check_exit_codes(tmp_path, capsys):
    repo, cache, claude = _pair(tmp_path)
    assert _check(repo, claude) == 0
    assert "in sync" in capsys.readouterr().out
    _write(cache / "skills" / "stale" / "SKILL.md", "---\nname: stale\n---\n")
    assert _check(repo, claude) == 1
    assert "skills/stale/SKILL.md" in capsys.readouterr().out


def test_cache_check_json_is_the_status(tmp_path, capsys):
    repo, _cache, claude = _pair(tmp_path, cache_version="0.3.0")
    assert _check(repo, claude, "--json") == 1
    st = json.loads(capsys.readouterr().out)
    assert st["state"] == "diverged" and st["cache_version"] == "0.3.0"


def test_hook_mode_reports_and_never_fails(tmp_path, capsys, monkeypatch):
    monkeypatch.delenv(plugin_cache.AUTOSYNC_ENV, raising=False)
    synced = []
    monkeypatch.setattr(plugin_cache, "sync_cache",
                        lambda status, **kw: synced.append(status) or {"ok": True})
    repo, cache, claude = _pair(tmp_path)
    assert _check(repo, claude, "--hook") == 0
    assert capsys.readouterr().out == ""                     # in sync: silent
    _write(cache / "skills" / "stale" / "SKILL.md", "---\nname: stale\n---\n")
    assert _check(repo, claude, "--hook") == 0
    out = capsys.readouterr().out
    assert "claude plugin update" in out and "skills/stale/SKILL.md" in out
    assert synced == []                                      # opt-in: not set


def test_hook_mode_syncs_when_opted_in(tmp_path, capsys, monkeypatch):
    monkeypatch.setenv(plugin_cache.AUTOSYNC_ENV, "1")
    synced = []
    monkeypatch.setattr(plugin_cache, "sync_cache",
                        lambda status, **kw: synced.append(status["state"])
                        or {"ok": False, "reason": "boom", "output": ""})
    repo, cache, claude = _pair(tmp_path)
    assert _check(repo, claude, "--hook") == 0
    assert synced == []                                      # in sync: no sync
    _write(cache / "skills" / "stale" / "SKILL.md", "---\nname: stale\n---\n")
    assert _check(repo, claude, "--hook") == 0            # a failed sync still exits 0
    assert synced == ["diverged"]
    assert "boom" in capsys.readouterr().out


def test_hook_mode_is_fail_open(tmp_path, capsys, monkeypatch):
    def broken(**kw):
        raise RuntimeError("the check itself broke")
    monkeypatch.setattr(plugin_cache, "cache_status", broken)
    assert _check(tmp_path, tmp_path, "--hook") == 0
    assert capsys.readouterr().out == ""
    # canary: outside hook mode the same fault is not swallowed
    with pytest.raises(RuntimeError):
        _check(tmp_path, tmp_path)


def test_cache_check_sync_flag_runs_the_update_only_on_drift(tmp_path, capsys,
                                                            monkeypatch):
    synced = []
    monkeypatch.setattr(plugin_cache, "sync_cache",
                        lambda status, **kw: synced.append(status["state"])
                        or {"ok": True, "argv": ["claude"], "output": "done",
                            "reason": ""})
    repo, cache, claude = _pair(tmp_path)
    assert _check(repo, claude, "--sync") == 0
    assert synced == []
    _write(cache / "skills" / "stale" / "SKILL.md", "---\nname: stale\n---\n")
    assert _check(repo, claude, "--sync") == 0
    assert synced == ["diverged"]
    monkeypatch.setattr(plugin_cache, "sync_cache",
                        lambda status, **kw: {"ok": False, "argv": ["claude"],
                                              "output": "", "reason": "nope"})
    assert _check(repo, claude, "--sync") == 1
