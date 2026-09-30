# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/legs.py
"""The leg catalog: every `.github/workflows/ci.yml` job as something this
machine can run, and what it can run of it (spec 2026-09-30).

A LEG is one ci.yml job, or one cell of the `test` job's OS x Python matrix.
`CATALOG` is data, and `tests/test_ci_legs.py` parses ci.yml against it in both
directions, so a job added there without a leg here -- or a leg left behind
after its job is removed -- fails the suite instead of silently never running.

`Probe` answers what THIS machine can run, and why not when it cannot:

* A windows leg needs the interpreter the Python launcher registers for its
  version. `py -0p` lists every PEP 514 install, uv's `Astral/CPython3.11.16`
  included -- which `py -3.11` does NOT resolve (measured) -- so the listing is
  parsed rather than asked one version at a time. Each hit is then run once, to
  prove it is the version it claims and can make a venv with pip.
* An ubuntu leg runs in WSL on Windows and natively on Linux. WSL is asked
  through a LOGIN shell: uv puts 3.11 and 3.14 in `~/.local/bin`, which only
  the login profile adds to PATH (measured: a plain `bash -c` sees neither).
* `node` needs `node` on PATH; `browser` needs the interpreter running `hive`
  to import `playwright`; `pg` needs `MINDHIVE_CI_PG_DSN`.

A leg the machine cannot run is UNAVAILABLE with a reason, and unavailable is
never success: `premerge` names it in the verdict instead of counting it.
"""
import fnmatch
import os
import re
import shutil
import sys
from dataclasses import dataclass

from . import proc

__all__ = ["CATALOG", "HOST_LOCAL", "HOST_POSIX", "HOST_WINDOWS", "PG_DSN_ENV",
           "TIERS", "TIER_BROWSER", "TIER_DEFAULT", "TIER_NODE", "TIER_PG",
           "Availability", "Leg", "Probe", "parse_py_list", "running_version",
           "select"]

#: Where a leg runs. WINDOWS: only on a Windows machine. POSIX: in WSL on
#: Windows, natively on Linux. LOCAL: on whatever machine `hive` runs on.
HOST_WINDOWS, HOST_POSIX, HOST_LOCAL = "windows", "posix", "local"
TIER_DEFAULT, TIER_NODE, TIER_BROWSER, TIER_PG = "default", "node", "browser", "pg"
TIERS = (TIER_DEFAULT, TIER_NODE, TIER_BROWSER, TIER_PG)
#: The THROWAWAY database the pg leg migrates and tests. Never the operator's
#: `MINDHIVE_PG_DSN_ADMIN`: that is the real mirror, and a migration from an
#: unmerged branch cannot be taken back.
PG_DSN_ENV = "MINDHIVE_CI_PG_DSN"
#: A cold WSL start was measured at 7.8 s; the cap is generous on purpose.
PROBE_TIMEOUT_S = 120.0
#: What the POSIX probe prints before each fact, so a login profile's own
#: banner can never be read as an answer.
_MARK = "MHCI"
POSIX_TOOLS = ("tar", "git", "timeout")
#: Run by each candidate interpreter: its version, and proof it can build a
#: venv with pip (a Debian python without python3-venv has no `ensurepip`).
_CHECK = "import sys, venv, ensurepip; print('%d.%d' % sys.version_info[:2])"
_PY_LINE = re.compile(r"^\s*-(?:V:)?(?P<tag>\S+)\s+(?:\*\s+)?(?P<path>\S.*?)\s*$")
_VERSION = re.compile(r"(\d+\.\d+)")


@dataclass(frozen=True)
class Leg:
    """One ci.yml job (or matrix cell) and how this machine runs it."""

    name: str        # the local name, and the commit-status context suffix
    job: str         # the ci.yml job id
    ci_name: str     # the job's display name on GitHub
    runs_on: str     # the ci.yml runner label: windows-latest | ubuntu-latest
    host: str        # HOST_WINDOWS | HOST_POSIX | HOST_LOCAL
    python: str      # "3.12"; "" means the interpreter running `hive`
    tier: str        # one of TIERS
    extras: str      # the `pip install -e .[...]` extras
    note: str = ""   # how the leg departs from its ci.yml job, when it does

    @property
    def pytest_args(self) -> tuple:
        """ci.yml's test command after `python -m pytest`, verbatim."""
        return ("-q",) if self.tier == TIER_DEFAULT else ("-m", self.tier, "-q")

    @property
    def clean_venv(self) -> bool:
        """A fresh venv with the job's extras -- every leg but `browser`."""
        return bool(self.python)


def _matrix(label: str, host: str, version: str) -> Leg:
    return Leg(name=f"{label}-py{version}", job="test",
               ci_name=f"test ({label}-latest, py{version})",
               runs_on=f"{label}-latest", host=host, python=version,
               tier=TIER_DEFAULT, extras="dev")


#: Longest first: a pool fed in this order finishes about as early as any order
#: can (a Windows suite ran ~25 min here, a Linux one ~15).
CATALOG = (
    *(_matrix("windows", HOST_WINDOWS, v) for v in ("3.11", "3.12", "3.14")),
    *(_matrix("ubuntu", HOST_POSIX, v) for v in ("3.11", "3.12", "3.14")),
    Leg(name="browser", job="browser-tests", ci_name="browser tier (T12)",
        runs_on="ubuntu-latest", host=HOST_LOCAL, python="",
        tier=TIER_BROWSER, extras="dev,browser",
        note="runs on this machine with the interpreter running hive, which "
             "must import playwright, so no Chromium is downloaded; ci.yml: "
             "the engine is the same one on every leg"),
    Leg(name="node", job="node-tests", ci_name="node tier-1 (paint guard)",
        runs_on="ubuntu-latest", host=HOST_LOCAL, python="3.12",
        tier=TIER_NODE, extras="dev",
        note="runs on this machine; ci.yml: these cases have no OS or "
             "Python dimension"),
    Leg(name="pg", job="pg-tests", ci_name="pg tier (Postgres 17)",
        runs_on="ubuntu-latest", host=HOST_LOCAL, python="3.12",
        tier=TIER_PG, extras="dev,pg",
        note=f"only against the throwaway database {PG_DSN_ENV} names; never "
             f"the operator's admin DSN, never ALTER ROLE"),
)


def select(spec=None, catalog=CATALOG) -> tuple:
    """The legs *spec* names -- comma-separated names or globs, like
    ``"windows-*,node"`` -- in catalog order; every leg when *spec* is empty.
    Raises ValueError naming whatever matched nothing."""
    if not spec:
        return tuple(catalog)
    wanted = [p.strip() for p in str(spec).split(",") if p.strip()]
    unknown = [p for p in wanted
               if not any(fnmatch.fnmatchcase(leg.name, p) for leg in catalog)]
    if unknown:
        raise ValueError(f"no leg matches {', '.join(unknown)}; legs are: "
                         + ", ".join(leg.name for leg in catalog))
    return tuple(leg for leg in catalog
                 if any(fnmatch.fnmatchcase(leg.name, p) for p in wanted))


def parse_py_list(text) -> dict:
    """``{"3.12": path}`` from `py -0p`, the first install listed winning per
    version (the launcher lists its preferred one first). Total: a line that is
    not an install is skipped."""
    found = {}
    for line in str(text or "").splitlines():
        m = _PY_LINE.match(line)
        if not m:
            continue
        v = _VERSION.search(m.group("tag"))
        if v and v.group(1) not in found:
            found[v.group(1)] = m.group("path")
    return found


def running_version() -> str:
    return "%d.%d" % sys.version_info[:2]


def _probe_script(versions) -> str:
    return "\n".join([
        f"for v in {' '.join(versions)}; do",
        f'  p=$(command -v "python$v" 2>/dev/null) && echo "{_MARK} py $v $p"',
        "done",
        *(f'command -v {tool} >/dev/null 2>&1 && echo "{_MARK} tool {tool}"'
          for tool in POSIX_TOOLS),
        f'python3 -m pip --version >/dev/null 2>&1 && echo "{_MARK} tool pip"',
        f'echo "{_MARK} ok"', ""])


@dataclass(frozen=True)
class Availability:
    """Whether this machine can run *leg*, with what, and why not."""

    leg: Leg
    ok: bool
    python: str = ""   # an interpreter path (inside WSL for a `wsl` leg)
    reason: str = ""   # why not, when not ok
    via: str = ""      # "native" | "wsl"


class Probe:
    """What this machine can run of the catalog. Each question -- `py -0p`,
    each interpreter, WSL -- is asked at most once per probe."""

    def __init__(self, run=proc.run, platform=None, which=shutil.which,
                 environ=None, executable=None, wsl_distro=None):
        self._run = run
        self.platform = platform or sys.platform
        self._which = which
        self.environ = dict(os.environ if environ is None else environ)
        self.executable = executable or sys.executable
        self.wsl_distro = wsl_distro or ""
        self._native = None
        self._native_bad = {}
        self._posix = None
        self._playwright = None

    @property
    def windows(self) -> bool:
        return self.platform == "win32"

    def wsl_prefix(self) -> list:
        """The argv prefix that runs a program in WSL."""
        prefix = ["wsl.exe"]
        if self.wsl_distro:
            prefix += ["-d", self.wsl_distro]
        return prefix + ["--exec"]

    def wsl_env(self) -> dict:
        """What `wsl.exe` itself runs with: its own messages in UTF-8 rather
        than UTF-16, and nothing from this side forwarded (`WSLENV`)."""
        return {**self.environ, "WSL_UTF8": "1", "WSLENV": ""}

    # --- the answers ------------------------------------------------------------------

    def check(self, leg) -> Availability:
        need = self._needs(leg)
        if need:
            return Availability(leg, False, reason=need)
        if leg.host == HOST_WINDOWS and not self.windows:
            return Availability(leg, False, reason="a windows leg needs a Windows host")
        if leg.host == HOST_POSIX:
            if self.windows:
                return self._wsl_leg(leg)
            if not self.platform.startswith("linux"):
                return Availability(leg, False,
                                    reason="an ubuntu leg needs Linux, or WSL on Windows")
        if not leg.python:
            if not self._imports_playwright():
                return Availability(leg, False, reason=(
                    f"the interpreter running hive ({self.executable}) cannot "
                    f"import playwright: pip install -e .[browser]"))
            return Availability(leg, True, python=self.executable, via="native")
        path = self._natives().get(leg.python)
        if not path:
            return Availability(leg, False, reason=self._native_missing(leg.python))
        return Availability(leg, True, python=path, via="native")

    def _needs(self, leg) -> str:
        if leg.tier == TIER_PG and not str(self.environ.get(PG_DSN_ENV) or "").strip():
            return (f"no {PG_DSN_ENV}: the pg tier needs a throwaway "
                    f"Postgres 17 database")
        if leg.tier == TIER_NODE and not self._which("node",
                                                     path=self.environ.get("PATH")):
            return "`node` is not on PATH"
        return ""

    def _imports_playwright(self) -> bool:
        if self._playwright is None:
            rc, _, _ = self._run([self.executable, "-c", "import playwright"],
                                 timeout_s=PROBE_TIMEOUT_S)
            self._playwright = rc == 0
        return self._playwright

    # --- this machine's interpreters --------------------------------------------------

    def _natives(self) -> dict:
        if self._native is None:
            self._native = self._find_natives()
        return self._native

    def _find_natives(self) -> dict:
        versions = sorted({leg.python for leg in CATALOG if leg.python})
        if self.windows:
            rc, out, _ = self._run(["py", "-0p"], timeout_s=PROBE_TIMEOUT_S)
            found = parse_py_list(out) if rc == 0 else {}
            if not found:
                found = {running_version(): self.executable}
        else:
            found = {}
            for v in versions:
                path = self._which(f"python{v}", path=self.environ.get("PATH"))
                if path:
                    found[v] = path
        good = {}
        for v in versions:
            path = found.get(v)
            if not path:
                continue
            rc, out, err = self._run([path, "-c", _CHECK], timeout_s=PROBE_TIMEOUT_S)
            answer = (out or "").strip()
            if rc == 0 and answer == v:
                good[v] = path
            elif rc == 0:
                self._native_bad[v] = f"{path} is Python {answer}, not {v}"
            else:
                why = (err or out or f"exit {rc}").strip().splitlines()[-1:]
                self._native_bad[v] = (f"{path} cannot make a venv with pip: "
                                       f"{why[0] if why else rc}")
        return good

    def _native_missing(self, version) -> str:
        if version in self._native_bad:
            return self._native_bad[version]
        if self.windows:
            return f"no Python {version} found by `py -0p`"
        return f"no python{version} on PATH"

    # --- WSL --------------------------------------------------------------------------

    def _posix_answer(self) -> tuple:
        """``(pythons, tools, error)`` as WSL's login shell reports them."""
        if self._posix is None:
            versions = sorted({leg.python for leg in CATALOG if leg.host == HOST_POSIX})
            rc, out, err = self._run(self.wsl_prefix() + ["bash", "-l", "-s"],
                                     stdin=_probe_script(versions),
                                     env=self.wsl_env(), timeout_s=PROBE_TIMEOUT_S)
            pythons, tools, done = {}, set(), False
            for line in (out or "").splitlines():
                parts = line.strip().split(" ", 3)
                if len(parts) == 4 and parts[:2] == [_MARK, "py"]:
                    pythons[parts[2]] = parts[3]
                elif len(parts) == 3 and parts[:2] == [_MARK, "tool"]:
                    tools.add(parts[2])
                elif parts == [_MARK, "ok"]:
                    done = True
            error = "" if rc == 0 and done else (
                (err or "").strip() or (out or "").strip() or f"exit {rc}")[-300:]
            self._posix = (pythons, tools, error)
        return self._posix

    def _wsl_leg(self, leg) -> Availability:
        pythons, tools, error = self._posix_answer()
        if error:
            return Availability(leg, False, reason=f"WSL could not answer: {error}")
        missing = [tool for tool in POSIX_TOOLS if tool not in tools]
        if missing:
            return Availability(leg, False, reason="WSL lacks " + ", ".join(missing))
        path = pythons.get(leg.python)
        if not path:
            return Availability(leg, False,
                                reason=f"no python{leg.python} on WSL's login PATH")
        return Availability(leg, True, python=path, via="wsl")
