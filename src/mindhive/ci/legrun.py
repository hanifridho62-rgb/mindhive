# SPDX-License-Identifier: Apache-2.0
# src/mindhive/ci/legrun.py
"""One leg, run the way its ci.yml job runs it (spec 2026-09-30).

**A fresh repository per leg**, not a linked worktree: `git archive` of the
tested commit, extracted, then `git init` + `add` + ONE commit under the
`mindhive-ci` identity. That is what an Actions checkout is -- one commit, no
history, no hooks, no other branch -- and it shares nothing with the
operator's repository, so a test that runs `git` in its root sees the tree
under test and nothing else.

**Line endings follow the runner.** A windows job's checkout is CRLF (git for
Windows ships `core.autocrlf=true`, as the runner image does) and an ubuntu
job's is LF. `git archive` honours `core.autocrlf` (measured), so the
conversion happens on export, and the leg repository keeps the same setting,
so `git status` in it is clean.

**Then the job's own steps**: a venv from the leg's interpreter, `pip install
--upgrade pip`, `pip install -e .[extras]`, the pg tier's cluster check and
`pg migrate`, and the job's pytest command verbatim. The `browser` leg is the exception the
catalog documents: it runs the interpreter running `hive` -- which must
already import playwright -- with `PYTHONPATH` at the leg's `src`, so no
Chromium is downloaded per run.

**The environment is built, not inherited.** `leg_env` drops what would make
a leg test something other than what CI tests -- interpreter and pytest
overrides, every `MINDHIVE_*`, git's own variables -- and anything shaped like
a credential, then sets `MINDHIVE_HOME` inside the leg and puts the venv first
on PATH. A WSL leg does the same inside Linux (`POSIX_LEG_SCRIPT`), where
nothing from Windows arrives at all (`WSLENV=""`).

**Classification is pytest's own**: exit 0 success, 1 failure, anything else
error (2 interrupted, 3 internal error, 4 usage, 5 nothing collected, 124 the
cap). A step before pytest that fails makes the leg an ERROR at that step,
never a failure: the suite said nothing. So does a pytest that exits 0 with a
summary naming no passed test -- a tier whose every test skipped proved
nothing, and must not read as a pass.

Every step appends to one log, kept as a runlog artifact; the leg's
directory is removed afterwards unless `keep`. `LegRunner.run` never raises.
"""
import os
import re
import tarfile
import time
from dataclasses import dataclass, field
from pathlib import Path

from .. import runlog, verify
from . import legs as legs_mod
from . import proc
from .runner import _rmtree_force

__all__ = ["FAILED_CAP", "LEG_TIMEOUT_S", "LegResult", "LegRunner", "MARK",
           "PG_CLUSTER_CHECK", "POSIX_LEG_SCRIPT", "SETUP_FAILED_EXIT", "STATES",
           "autocrlf_for", "classify", "extract", "leg_env", "venv_python"]

GIT = "git"
#: Every line the runner -- or the WSL script -- writes into a leg's log starts
#: with this, so a step can be found in the log and told apart from output.
MARK = "::mindhive-ci::"
STATES = ("success", "failure", "error")
#: pytest's cap per leg: ninety minutes, about four times the slowest suite
#: measured here (a Windows full suite ran 1403 s beside a second one).
LEG_TIMEOUT_S = 5400.0
#: One setup step: a venv, a pip install, a migration.
SETUP_TIMEOUT_S = 1800.0
#: Git plumbing and WSL housekeeping.
STEP_TIMEOUT_S = 600.0
SETUP_FAILED_EXIT = 97
FAILED_CAP = 20
CI_NAME = "mindhive-ci"
CI_EMAIL = "mindhive-ci@localhost.invalid"
COMMIT_MESSAGE = "mindhive-ci leg tree"
_DROP_PREFIXES = ("PYTHON", "PYTEST_", "MINDHIVE_", "MINDNETWORK_", "CLAUDE",
                  "ANTHROPIC_", "GIT_", "VIRTUAL_ENV", "CONDA_", "UV_")
_DROP_WORDS = ("TOKEN", "SECRET", "PASSWORD", "PASSWD", "API_KEY", "APIKEY",
               "ACCESS_KEY", "PRIVATE_KEY", "CREDENTIAL")
_PYTEST_STEP = f"{MARK} step pytest"
_SETUP_FAILED = re.compile(r"^::mindhive-ci:: setup-failed (\S+)[ \t]*\r?$", re.M)
_PYTEST_EXIT = re.compile(r"^::mindhive-ci:: pytest-exit (-?\d+)[ \t]*\r?$", re.M)
_COUNT_ORDER = ("failed", "errors", "passed", "skipped", "xfailed", "xpassed",
                "deselected")

#: Run in the pg leg's venv before `pg migrate`. The cluster behind the DSN must
#: hold no database but the one under test and the stock `postgres`: roles are
#: cluster-wide, so a branch's migration on a cluster the real mirror shares
#: would reach the mirror's roles, and that cannot be taken back.
PG_CLUSTER_CHECK = """\
import os, sys
import psycopg
with psycopg.connect(os.environ["MINDHIVE_PG_DSN_ADMIN"]) as conn:
    rows = conn.execute(
        "SELECT datname FROM pg_database WHERE NOT datistemplate"
        " AND datname NOT IN (current_database(), 'postgres') ORDER BY 1").fetchall()
if rows:
    sys.exit("the cluster also holds " + ", ".join(r[0] for r in rows)
             + ": the pg leg needs a Postgres cluster of its own")
"""

#: A WSL leg's whole job, fed to `bash -l -s -- ROOT PY EXTRAS CAP <pytest args>`
#: on stdin. A LOGIN shell, because uv's interpreters live in `~/.local/bin`,
#: which only the login profile puts on PATH (measured). The body is one
#: function called with `</dev/null`: bash reads a stdin script as it runs it,
#: so without that a step that reads stdin -- pip, a test's child -- would eat
#: the rest of the script. Windows paths (`/mnt/...`) come off PATH first, so a
#: Windows `python`, `git` or `pytest` can never answer for Linux.
POSIX_LEG_SCRIPT = r"""
mhci_mark() { printf '%s %s\n' '::mindhive-ci::' "$*"; }
mhci_fail() { mhci_mark "setup-failed $1"; exit 97; }
mhci_leg() {
    ROOT=$1 PY=$2 EXTRAS=$3 CAP=$4
    shift 4
    cd "$ROOT" || mhci_fail cd
    IFS=: read -r -a mhci_dirs <<< "$PATH"
    PATH=
    for d in "${mhci_dirs[@]}"; do
        case "$d" in /mnt/*) ;; *) PATH="${PATH:+$PATH:}$d" ;; esac
    done
    export PATH
    for name in $(compgen -e); do
        case "$name" in
            PYTHON*|PYTEST_*|MINDHIVE_*|MINDNETWORK_*|GIT_*|CLAUDE*|ANTHROPIC_*|VIRTUAL_ENV|CONDA_*|UV_*|*TOKEN*|*SECRET*|*PASSWORD*|*PASSWD*|*API_KEY*|*APIKEY*|*ACCESS_KEY*|*PRIVATE_KEY*|*CREDENTIAL*)
                unset "$name" ;;
        esac
    done
    export MINDHIVE_HOME="$ROOT-home" PIP_DISABLE_PIP_VERSION_CHECK=1 PIP_NO_INPUT=1
    mkdir -p "$MINDHIVE_HOME" || mhci_fail home
    mhci_mark "step git-init"
    git -c init.defaultBranch=master init -q \
        && git config core.autocrlf false \
        && git config core.safecrlf false \
        && git add -A -f \
        && git -c user.name=mindhive-ci -c user.email=mindhive-ci@localhost.invalid \
               -c commit.gpgsign=false -c core.hooksPath=/dev/null \
               commit -q --no-verify -m "mindhive-ci leg tree" \
        || mhci_fail git-init
    VENV="$ROOT-venv"
    mhci_mark "step venv: $PY -m venv $VENV"
    if ! "$PY" -m venv "$VENV"; then
        rm -rf "$VENV"
        { "$PY" -m venv --without-pip "$VENV" \
            && python3 -m pip --python "$VENV/bin/python" install -q pip; } \
            || mhci_fail venv
    fi
    export PATH="$VENV/bin:$PATH"
    mhci_mark "step pip-upgrade"
    "$VENV/bin/python" -m pip install --upgrade pip || mhci_fail pip-upgrade
    mhci_mark "step install"
    "$VENV/bin/python" -m pip install -e ".[$EXTRAS]" || mhci_fail install
    mhci_mark "step pytest: $VENV/bin/python -m pytest $*"
    timeout --kill-after=30 "$CAP" "$VENV/bin/python" -m pytest "$@"
    rc=$?
    mhci_mark "pytest-exit $rc"
    exit "$rc"
}
mhci_leg "$@" </dev/null
"""


def classify(rc) -> str:
    """pytest's exit code as a leg state."""
    return "success" if rc == 0 else "failure" if rc == 1 else "error"


def autocrlf_for(leg) -> str:
    """`core.autocrlf` for *leg*'s checkout: the runner's, not this machine's."""
    return "true" if str(leg.runs_on).startswith("windows") else "false"


def venv_python(venv, windows=None) -> Path:
    windows = (os.name == "nt") if windows is None else bool(windows)
    return Path(venv) / ("Scripts/python.exe" if windows else "bin/python")


def leg_env(environ, leg_dir, bin_dir=None) -> dict:
    """The environment a leg's children get: built from *environ*, never
    inherited whole. *environ* itself is not changed."""
    env = {}
    for name, value in dict(environ).items():
        upper = str(name).upper()
        if upper.startswith(_DROP_PREFIXES) or any(w in upper for w in _DROP_WORDS):
            continue
        env[name] = value
    env["MINDHIVE_HOME"] = str(Path(leg_dir) / "home")
    env["PIP_DISABLE_PIP_VERSION_CHECK"] = "1"
    env["PIP_NO_INPUT"] = "1"
    if bin_dir is not None:
        key = next((k for k in env if str(k).upper() == "PATH"), "PATH")
        prior = env.get(key, "")
        env[key] = str(bin_dir) + (os.pathsep + prior if prior else "")
    return env


def extract(tar_path, dest) -> None:
    """Unpack a `git archive` tar into *dest*, refusing any member that would
    land outside it -- the stdlib `data` filter where the interpreter has it."""
    dest = Path(dest)
    dest.mkdir(parents=True, exist_ok=True)
    with tarfile.open(tar_path) as archive:
        if hasattr(tarfile, "data_filter"):
            archive.extractall(dest, filter="data")
            return
        root = dest.resolve()
        for member in archive.getmembers():
            target = (dest / member.name).resolve()
            if target != root and root not in target.parents:
                raise ValueError(f"archive member outside the leg: {member.name}")
            if member.issym() or member.islnk():
                raise ValueError(f"archive link refused: {member.name}")
        archive.extractall(dest)


def _read(path) -> str:
    try:
        return Path(path).read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _append(path, text) -> None:
    with open(path, "ab") as fh:
        fh.write(str(text).encode("utf-8"))


def _last_line(text) -> str:
    """The last line of *text* that is output, not one of the runner's marks."""
    for line in reversed(str(text or "").splitlines()):
        line = line.strip()
        if line and not line.startswith(MARK):
            return line[:300]
    return ""


def _took(seconds) -> str:
    seconds = max(0.0, float(seconds or 0.0))
    if seconds < 60:
        return f"{seconds:.0f}s"
    return f"{int(seconds // 60)}m{int(seconds % 60):02d}s"


def _count_text(counts) -> str:
    parts = []
    for key in _COUNT_ORDER:
        n = (counts or {}).get(key)
        if isinstance(n, int) and not isinstance(n, bool) and n > 0:
            parts.append(f"{n} {'error' if key == 'errors' and n == 1 else key}")
    return ", ".join(parts)


@dataclass
class LegResult:
    """One leg's verdict. Every field is present on construction."""

    name: str
    job: str = ""
    ci_name: str = ""
    tier: str = ""
    state: str = "error"
    exit_code: int = 0
    counts: dict = field(default_factory=dict)
    failed_tests: tuple = ()
    duration_s: float = 0.0
    step: str = ""
    reason: str = ""
    argv: tuple = ()
    python: str = ""
    via: str = ""
    artifact: str = ""

    def summary(self) -> str:
        """One line, the shape a commit status description takes."""
        if self.state in ("success", "failure"):
            return f"{_count_text(self.counts) or self.state} in {_took(self.duration_s)}"
        return f"{self.step or 'leg'}: {self.reason or f'exit {self.exit_code}'}"

    def as_dict(self) -> dict:
        return {"name": self.name, "job": self.job, "ci_name": self.ci_name,
                "tier": self.tier, "state": self.state,
                "exit_code": int(self.exit_code), "counts": dict(self.counts),
                "failed_tests": list(self.failed_tests),
                "duration_s": round(self.duration_s, 3), "step": self.step,
                "reason": self.reason, "argv": list(self.argv),
                "python": self.python, "via": self.via,
                "artifact": self.artifact, "summary": self.summary()}


class LegRunner:
    """Runs the legs of one pre-merge run. Each `run` is independent of every
    other -- its own directory, log and artifact -- so a pool may call it from
    several threads at once."""

    def __init__(self, repo_root, work_root, *, run_id, run=proc.run, environ=None,
                 project=None, timeout_s=LEG_TIMEOUT_S,
                 wsl_prefix=("wsl.exe", "--exec"), wsl_env=None, keep=False,
                 clock=time.monotonic, windows=None):
        self.repo_root = Path(repo_root)
        self.work_root = Path(work_root)
        self.run_id = str(run_id)
        self._run = run
        self.environ = dict(os.environ if environ is None else environ)
        self.project = project
        self.timeout_s = float(timeout_s)
        self.wsl_prefix = list(wsl_prefix)
        self.wsl_env = dict(self.environ if wsl_env is None else wsl_env)
        self.keep = bool(keep)
        self._clock = clock
        self.windows = (os.name == "nt") if windows is None else bool(windows)

    def run(self, leg, availability, commit) -> LegResult:
        """Run *leg* against *commit* with what *availability* found. Never raises."""
        started = self._clock()
        res = LegResult(name=leg.name, job=leg.job, ci_name=leg.ci_name,
                        tier=leg.tier, python=availability.python,
                        via=availability.via)
        leg_dir = self.work_root / leg.name
        log = leg_dir / "leg.log"
        try:
            leg_dir.mkdir(parents=True, exist_ok=True)
            if availability.via == "wsl":
                self._wsl(leg, availability, str(commit), leg_dir, log, res)
            else:
                self._native(leg, availability, str(commit), leg_dir, log, res)
        except Exception as exc:                        # noqa: BLE001
            res.state, res.step = "error", res.step or "runner"
            res.reason = f"{type(exc).__name__}: {exc}"
        res.duration_s = self._clock() - started
        self._conclude(res, _read(log))
        if not self.keep:
            _rmtree_force(leg_dir)
        return res

    # --- steps ----------------------------------------------------------------------

    def _step(self, res, name, argv, *, log, cwd=None, env=None,
              timeout_s=STEP_TIMEOUT_S, stdin=None) -> bool:
        """One setup step. False -- with *res* an error AT *name* -- when it failed."""
        _append(log, f"{MARK} step {name}: {' '.join(str(a) for a in argv)}\n")
        rc, _, err = self._run(argv, cwd=cwd, env=env, timeout_s=timeout_s,
                               stdin=stdin, log=log)
        if err:
            _append(log, str(err).rstrip() + "\n")
        if rc == 0:
            return True
        res.state, res.step, res.exit_code = "error", name, int(rc)
        res.reason = _last_line(err) or _last_line(_read(log)) or f"exit {rc}"
        return False

    def _pytest(self, res, argv, *, log, cwd, env) -> None:
        res.argv = tuple(str(a) for a in argv)
        _append(log, f"{_PYTEST_STEP}: {' '.join(res.argv)}\n")
        rc, _, err = self._run(argv, cwd=cwd, env=env, timeout_s=self.timeout_s,
                               log=log)
        if err:
            _append(log, str(err).rstrip() + "\n")
        res.step, res.exit_code, res.state = "pytest", int(rc), classify(rc)
        if res.state == "error":
            res.reason = _last_line(err) or f"pytest exit {rc}"

    def _init_repo(self, res, repo, crlf, leg_dir, env, log) -> bool:
        hooks = leg_dir / "no-hooks"
        hooks.mkdir(exist_ok=True)
        steps = (
            [GIT, "-c", "init.defaultBranch=master", "init", "-q"],
            [GIT, "config", "core.autocrlf", crlf],
            [GIT, "config", "core.safecrlf", "false"],
            # -f: every file here was TRACKED; a .gitignore in the tree must
            # not drop one that was force-added upstream.
            [GIT, "add", "-A", "-f"],
            [GIT, "-c", f"user.name={CI_NAME}", "-c", f"user.email={CI_EMAIL}",
             "-c", "commit.gpgsign=false", "-c", f"core.hooksPath={hooks}",
             "commit", "-q", "--no-verify", "-m", COMMIT_MESSAGE],
        )
        return all(self._step(res, "git-init", argv, log=log, cwd=repo, env=env)
                   for argv in steps)

    # --- a leg on this machine --------------------------------------------------------

    def _native(self, leg, avail, commit, leg_dir, log, res) -> None:
        crlf = autocrlf_for(leg)
        repo, tar = leg_dir / "repo", leg_dir / "tree.tar"
        (leg_dir / "home").mkdir(exist_ok=True)
        env = leg_env(self.environ, leg_dir)
        if not self._step(res, "archive",
                          [GIT, "-c", f"core.autocrlf={crlf}", "archive",
                           "--format=tar", "-o", str(tar), commit],
                          log=log, cwd=self.repo_root, env=env):
            return
        res.step = "extract"
        extract(tar, repo)
        tar.unlink()
        res.step = ""
        if not self._init_repo(res, repo, crlf, leg_dir, env, log):
            return
        if leg.clean_venv:
            venv = leg_dir / "venv"
            if not self._step(res, "venv", [avail.python, "-m", "venv", str(venv)],
                              log=log, cwd=leg_dir, env=env,
                              timeout_s=SETUP_TIMEOUT_S):
                return
            python = str(venv_python(venv, self.windows))
            env = leg_env(self.environ, leg_dir, bin_dir=Path(python).parent)
            for name, args in (("pip-upgrade", ["install", "--upgrade", "pip"]),
                               ("install", ["install", "-e", f".[{leg.extras}]"])):
                if not self._step(res, name, [python, "-m", "pip", *args],
                                  log=log, cwd=repo, env=env,
                                  timeout_s=SETUP_TIMEOUT_S):
                    return
        else:
            # Spelled as the probe found it: Path() would re-spell the
            # separators, and the record names the interpreter that answered.
            python = str(avail.python)
            env = leg_env(self.environ, leg_dir, bin_dir=Path(python).parent)
            env["PYTHONPATH"] = str(repo / "src")
            env["HF_HUB_OFFLINE"] = "1"
        if leg.tier == legs_mod.TIER_PG:
            env["MINDHIVE_PG_DSN_ADMIN"] = str(self.environ.get(legs_mod.PG_DSN_ENV) or "")
            env["MINDHIVE_PG_DSN_RO"] = str(self.environ.get(legs_mod.PG_RO_DSN_ENV) or "")
            if not self._step(res, "cluster-check", [python, "-c", PG_CLUSTER_CHECK],
                              log=log, cwd=repo, env=env):
                return
            if not self._step(res, "migrate",
                              [python, "-m", "mindhive", "pg", "migrate"],
                              log=log, cwd=repo, env=env, timeout_s=SETUP_TIMEOUT_S):
                return
        self._pytest(res, [python, "-m", "pytest", *leg.pytest_args],
                     log=log, cwd=repo, env=env)

    # --- a leg in WSL -----------------------------------------------------------------

    def _wsl(self, leg, avail, commit, leg_dir, log, res) -> None:
        tar = leg_dir / "tree.tar"
        env = leg_env(self.environ, leg_dir)
        if not self._step(res, "archive",
                          [GIT, "-c", "core.autocrlf=false", "archive",
                           "--format=tar", "-o", str(tar), commit],
                          log=log, cwd=self.repo_root, env=env):
            return
        data = tar.read_bytes()
        tar.unlink()
        rc, out, err = self._run(self.wsl_prefix + ["mktemp", "-d", "/tmp/mindhive-ci.XXXXXX"],
                                 env=self.wsl_env, timeout_s=STEP_TIMEOUT_S)
        root = (out or "").strip()
        if rc != 0 or not root.startswith("/tmp/mindhive-ci."):
            res.state, res.step, res.exit_code = "error", "mktemp", int(rc)
            res.reason = _last_line(err) or _last_line(out) or f"exit {rc}"
            return
        try:
            if not self._step(res, "extract", self.wsl_prefix + ["tar", "-x", "-C", root],
                              log=log, env=self.wsl_env, stdin=data):
                return
            res.argv = (avail.python, "-m", "pytest", *leg.pytest_args)
            argv = self.wsl_prefix + ["bash", "-l", "-s", "--", root, avail.python,
                                      leg.extras, str(int(self.timeout_s)),
                                      *leg.pytest_args]
            rc, _, err = self._run(argv, env=self.wsl_env, stdin=POSIX_LEG_SCRIPT,
                                   log=log, timeout_s=self.timeout_s + SETUP_TIMEOUT_S)
            if err:
                _append(log, str(err).rstrip() + "\n")
            self._judge_posix(res, rc, err, _read(log))
        finally:
            self._run(self.wsl_prefix + ["rm", "-rf", "--", root, f"{root}-venv",
                                         f"{root}-home"],
                      env=self.wsl_env, timeout_s=STEP_TIMEOUT_S)

    def _judge_posix(self, res, rc, err, text) -> None:
        failed = list(_SETUP_FAILED.finditer(text))
        exits = _PYTEST_EXIT.findall(text)
        if failed:
            last = failed[-1]
            res.state, res.step, res.exit_code = "error", last.group(1), int(rc)
            res.reason = _last_line(text[:last.start()]) or f"exit {rc}"
        elif exits:
            code = int(exits[-1])
            res.step, res.exit_code, res.state = "pytest", code, classify(code)
            if res.state == "error":
                res.reason = f"pytest exit {code}" + (
                    " -- killed at the cap" if code in (124, 137) else "")
        else:
            res.state, res.exit_code = "error", int(rc)
            res.step = "pytest" if _PYTEST_STEP in text else "wsl"
            res.reason = _last_line(err) or _last_line(text) or f"wsl.exe exit {rc}"

    # --- the record -------------------------------------------------------------------

    def _conclude(self, res, text) -> None:
        part = text.rsplit(_PYTEST_STEP, 1)[1] if _PYTEST_STEP in text else ""
        if part:
            res.counts = verify.parse_summary(part)
            res.failed_tests = tuple(
                line.strip() for line in part.splitlines()
                if line.startswith(("FAILED ", "ERROR ")))[:FAILED_CAP]
        if res.state == "success" and res.counts and not res.counts.get("passed"):
            # A summary was read and it names no passed test. An unreadable one
            # keeps pytest's exit code: absent counts are not zero counts.
            res.state = "error"
            res.reason = f"no test passed ({_count_text(res.counts) or 'nothing ran'})"
        if text.strip():
            try:
                res.artifact = runlog.write_once(f"premerge-{self.run_id}-{res.name}",
                                                 text, project=self.project) or ""
            except Exception:                           # noqa: BLE001
                res.artifact = ""       # the evidence is lost; the verdict is not
