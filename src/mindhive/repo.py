# SPDX-License-Identifier: Apache-2.0
# src/mindhive/repo.py
"""Repository plumbing: what git and ``gh`` can PROVE about a branch or a PR.

MindHive records plans; git and GitHub record the work those plans produced.
This module is the only place the two meet, and it exists to answer exactly one
question honestly:

    has the work behind this plan landed?

The lookup is easy. The honesty is the hard part. A merge probe runs on a
machine that may have no GitHub remote, no ``gh`` on PATH, and no network — and
in every one of those cases the tempting answer is the wrong one. Reporting
"not merged" when the probe never ran turns a missing tool into a claim about
the code, and that claim is what would retire a card that is still open (or
pin one open forever). So this module never returns a boolean. It returns a
**state**, and when the state is ``unknown`` it also returns the specific
reason it could not tell:

===================  ======================================================
``no_work_link``     the plan records no branch, PR, or commit to check
``no_github_remote`` not a git checkout, or no GitHub remote to ask about
``no_gh_cli``        ``gh`` is absent, so a PR could not be looked up
``probe_failed``     git or ``gh`` ran and failed (offline, rate limit, auth)
===================  ======================================================

**``merged`` is only ever returned on proof** — a merged PR record, or a commit
that git confirms is an ancestor of the integration ref. Everything softer is
``open`` or ``unknown``.

Callers get one record back and never an exception: every child process runs
through one runner that converts failure into a return code, and every probe
converts a return code into a state. That is the "define errors out of
existence" bargain — this module absorbs the mess so the roadmap registry above
it can treat "has it landed?" as ordinary data.

The ``runner`` seam is how tests drive all four reasons without a network, a
GitHub account, or a fixture repo.
"""
import json
import shutil
import subprocess

from .paths import NO_WINDOW, worktree_root

# --- the vocabulary -----------------------------------------------------------
# Four states, not a boolean, because "we could not tell" is a distinct and
# frequent outcome that a boolean has nowhere to put.
MERGED = "merged"
CLOSED = "closed"        # the PR was closed WITHOUT merging: work abandoned
OPEN = "open"            # the work exists and demonstrably has not landed
UNKNOWN = "unknown"
MERGE_STATES = (MERGED, CLOSED, OPEN, UNKNOWN)

NO_WORK_LINK = "no_work_link"
NO_GITHUB_REMOTE = "no_github_remote"
NO_GH_CLI = "no_gh_cli"
PROBE_FAILED = "probe_failed"
UNKNOWN_REASONS = (NO_WORK_LINK, NO_GITHUB_REMOTE, NO_GH_CLI, PROBE_FAILED)

# When several probes all answer `unknown`, report the one the user can act on.
# A probe that actually broke outranks a missing tool, which outranks a checkout
# that was never going to be able to answer.
_REASON_RANK = {PROBE_FAILED: 3, NO_GH_CLI: 2, NO_GITHUB_REMOTE: 1,
                NO_WORK_LINK: 0, "": 0}

PROBE_TIMEOUT_SECONDS = 10

# Where a merge lands. `origin/HEAD` is the remote's own default branch — what a
# PR merges into. The two literals are the fallback for a checkout whose
# `origin/HEAD` was never set, which `git clone --branch` and `git worktree add`
# both leave absent.
_INTEGRATION_REFS = ("origin/HEAD", "origin/master", "origin/main")

MAX_DETAIL_CHARS = 200


def _default_runner(argv, cwd, timeout_s):
    """``(returncode, stdout, stderr)`` for one child process. Never raises: a
    missing executable, a timeout and a crash all arrive as a non-zero code
    with the failure text in *stderr*, so every caller has one shape to read."""
    try:
        proc = subprocess.run(argv, cwd=cwd, capture_output=True, text=True,
                              timeout=timeout_s, creationflags=NO_WINDOW)
    except Exception as e:
        return 1, "", str(e)
    return proc.returncode, proc.stdout or "", proc.stderr or ""


def _answer(state, *, reason="", detail="", evidence="") -> dict:
    """One merge-probe answer. *evidence* says what proved a definite state;
    *reason* + *detail* say why an ``unknown`` stayed unknown."""
    return {"state": state, "reason": reason,
            "detail": str(detail)[:MAX_DETAIL_CHARS], "evidence": evidence}


def _first_line(text) -> str:
    for line in str(text or "").splitlines():
        if line.strip():
            return line.strip()
    return ""


# --- the shared preconditions -------------------------------------------------

def github_remote(runner, root, timeout_s) -> str:
    """Name of the first remote pointing at github.com, or "" when there is
    none. This is checked BEFORE any `gh` call so "this checkout has no GitHub
    remote" reports as its own reason instead of arriving as a generic `gh`
    failure the user cannot act on."""
    rc, out, _ = runner(["git", "remote", "-v"], root, timeout_s)
    if rc != 0:
        return ""
    for line in out.splitlines():
        parts = line.split()
        if len(parts) >= 2 and "github.com" in parts[1]:
            return parts[0]
    return ""


def _integration_ref(runner, root, timeout_s) -> str:
    """The ref a merge lands on, or "" when none of the candidates resolve."""
    for ref in _INTEGRATION_REFS:
        rc, _, _ = runner(["git", "rev-parse", "--verify", "--quiet", ref],
                          root, timeout_s)
        if rc == 0:
            return ref
    return ""


def _current_branch(runner, root, timeout_s) -> str:
    """The checked-out branch, or "" on a detached HEAD — where git answers the
    literal string ``HEAD``, which is a state, not a branch name."""
    rc, out, _ = runner(["git", "rev-parse", "--abbrev-ref", "HEAD"], root,
                        timeout_s)
    name = out.strip() if rc == 0 else ""
    return "" if name == "HEAD" else name


def _upstream_ref(runner, root, timeout_s) -> str:
    """The ref `git pull` would pull from, or "" when this branch tracks
    nothing.

    Deliberately NOT `_integration_ref`. That one answers a different question —
    *where does a merge land* — and it resolves `origin/HEAD` on any ordinary
    clone, including one whose own branch tracks nothing at all. Reading it as
    an upstream is exactly how `hive repo stale` came to report `current — 0
    behind / 0 ahead` for a `master` on which `git pull` was failing outright
    with "There is no tracking information for the current branch"."""
    rc, out, _ = runner(["git", "rev-parse", "--abbrev-ref",
                         "--symbolic-full-name", "@{upstream}"], root, timeout_s)
    return out.strip() if rc == 0 else ""


def tracking(*, root=None, runner=None, timeout_s=PROBE_TIMEOUT_SECONDS) -> dict:
    """Can this checkout pull, and from where? ``{branch, upstream,
    upstream_missing}``. Never raises, never fetches.

    Split out of `staleness` because it answers on its own and costs two git
    calls against that one's five — measured 2026-08-14 on this repo, min of 5:
    133 ms here against 249 ms for the full probe (an earlier note said ~85ms,
    which no longer holds). Cheap enough to run once per session on the orient
    path, where a checkout that cannot pull is worth saying out loud. Nothing else reports it: `git status` is silent about
    a missing upstream, and the drift counts are measured against a ref that
    resolves whether or not this branch tracks anything."""
    runner = runner or _default_runner
    if root is None:
        root = worktree_root()
    if root is None:
        return {"branch": "", "upstream": "", "upstream_missing": True}
    root = str(root)
    upstream = _upstream_ref(runner, root, timeout_s)
    return {"branch": _current_branch(runner, root, timeout_s),
            "upstream": upstream, "upstream_missing": not upstream}


# --- the three probes ---------------------------------------------------------
# Each takes the link fragment it understands and returns an `_answer`. A probe
# that cannot tell says UNKNOWN with its own reason; it never guesses.

def _probe_pr(pr, runner, root, timeout_s) -> dict:
    """The PR record itself — the most authoritative answer available, and the
    only one that survives a squash merge (which leaves no ancestor commit)."""
    gh = shutil.which("gh")
    if not gh:
        return _answer(UNKNOWN, reason=NO_GH_CLI,
                       detail="gh is not on PATH; cannot read PR #%s" % pr)
    if not github_remote(runner, root, timeout_s):
        return _answer(UNKNOWN, reason=NO_GITHUB_REMOTE,
                       detail="no github.com remote in this checkout")
    # `mergedBy` and `author` are free here -- one `gh pr view` either way --
    # and are what `orchestrator/interceptor.py` needs to name the actor. The
    # ANSWER below is unchanged: this probe still reports only the state, its
    # evidence line and its reason, so `roadmap.retire_landed` reads exactly
    # what it read before.
    rc, out, err = runner([gh, "pr", "view", str(pr), "--json",
                           "state,mergedAt,url,mergedBy,author"],
                          root, timeout_s)
    if rc != 0:
        return _answer(UNKNOWN, reason=PROBE_FAILED,
                       detail=_first_line(err) or f"gh pr view exited {rc}")
    try:
        record = json.loads(out)
        state = str(record.get("state") or "").upper()
        url = str(record.get("url") or f"PR #{pr}")
    except Exception as e:
        return _answer(UNKNOWN, reason=PROBE_FAILED,
                       detail=f"unreadable gh output: {e}")
    if state == "MERGED":
        return _answer(MERGED, evidence=f"{url} is merged "
                                        f"({record.get('mergedAt') or '?'})")
    if state == "CLOSED":
        return _answer(CLOSED, evidence=f"{url} was closed without merging")
    if state == "OPEN":
        return _answer(OPEN, evidence=f"{url} is still open")
    return _answer(UNKNOWN, reason=PROBE_FAILED,
                   detail=f"gh reported an unknown PR state: {state!r}")


def _probe_commits(commits, runner, root, timeout_s) -> dict:
    """Pure git ancestry: are all the recorded commits in the integration ref?
    Needs no network and no ``gh``, so it answers on a machine where the PR
    probe cannot — but it only sees merges that PRESERVE the commits."""
    ref = _integration_ref(runner, root, timeout_s)
    if not ref:
        return _answer(UNKNOWN, reason=PROBE_FAILED,
                       detail="no integration ref resolved (tried "
                              + ", ".join(_INTEGRATION_REFS) + ")")
    for sha in commits:
        rc, _, err = runner(["git", "merge-base", "--is-ancestor", sha, ref],
                            root, timeout_s)
        if rc == 0:
            continue
        # git distinguishes the two failures, and so must we: 1 is a real
        # answer ("not an ancestor"), anything else means the question was
        # never asked (unknown object, corrupt repo, git missing).
        if rc == 1:
            return _answer(OPEN, evidence=f"{sha[:12]} is not in {ref}")
        return _answer(UNKNOWN, reason=PROBE_FAILED,
                       detail=_first_line(err)
                       or f"git merge-base exited {rc} for {sha[:12]}")
    return _answer(MERGED, evidence=f"{len(commits)} recorded commit(s) are "
                                    f"ancestors of {ref}")


def _probe_branch(branch, runner, root, timeout_s) -> dict:
    """The branch name — the weakest link, because a merged branch is usually
    deleted and a squash merge leaves none of its commits behind. So this probe
    is **prove-only**: git ancestry can establish ``merged``, and ``gh`` can
    establish the rest. With neither able to answer, the reason is ``no_gh_cli``
    — naming the one tool that would have settled it."""
    ref = _integration_ref(runner, root, timeout_s)
    if ref:
        rc, _, _ = runner(["git", "merge-base", "--is-ancestor",
                           f"origin/{branch}", ref], root, timeout_s)
        if rc == 0:
            return _answer(MERGED,
                           evidence=f"origin/{branch} is an ancestor of {ref}")
    gh = shutil.which("gh")
    if not gh:
        return _answer(UNKNOWN, reason=NO_GH_CLI,
                       detail=f"gh is not on PATH; origin/{branch} is not an "
                              f"ancestor of {ref or 'any integration ref'}, "
                              f"which a squash merge also looks like")
    if not github_remote(runner, root, timeout_s):
        return _answer(UNKNOWN, reason=NO_GITHUB_REMOTE,
                       detail="no github.com remote in this checkout")
    rc, out, err = runner([gh, "pr", "list", "--head", branch, "--state",
                           "all", "--limit", "10", "--json",
                           "number,state,url"], root, timeout_s)
    if rc != 0:
        return _answer(UNKNOWN, reason=PROBE_FAILED,
                       detail=_first_line(err) or f"gh pr list exited {rc}")
    try:
        prs = json.loads(out)
        if not isinstance(prs, list):
            raise ValueError("gh pr list did not return a list")
    except Exception as e:
        return _answer(UNKNOWN, reason=PROBE_FAILED,
                       detail=f"unreadable gh output: {e}")
    states = {str(p.get("state") or "").upper(): p for p in prs
              if isinstance(p, dict)}
    if "MERGED" in states:
        pr = states["MERGED"]
        return _answer(MERGED, evidence=f"{pr.get('url') or branch} is merged")
    if "OPEN" in states:
        pr = states["OPEN"]
        return _answer(OPEN, evidence=f"{pr.get('url') or branch} is still open")
    if "CLOSED" in states:
        pr = states["CLOSED"]
        return _answer(CLOSED, evidence=f"{pr.get('url') or branch} was closed "
                                        f"without merging")
    return _answer(OPEN, evidence=f"no PR exists for {branch} and it is not in "
                                  f"{ref or 'any integration ref'}")


# --- the one public entry point ----------------------------------------------

def merge_state(*, branch="", pr=0, commits=(), root=None, runner=None,
                timeout_s=PROBE_TIMEOUT_SECONDS) -> dict:
    """Has the work described by this link landed? Returns
    ``{state, reason, detail, evidence}`` and never raises.

    The probes run most-authoritative-first (PR, then commits, then branch) and
    **the first definite answer wins**. That ordering is what makes the module
    degrade instead of fail: a machine without ``gh`` still gets a real answer
    from the recorded commits, and only a link that no available probe can read
    comes back ``unknown`` — carrying the reason, so the user knows whether to
    install ``gh``, add a remote, or reconnect.
    """
    runner = runner or _default_runner
    branch = str(branch or "").strip()
    # `isinstance(c, str)` before stripping, because `str(None)` is the truthy
    # string "None" — which would be probed as a commit-ish and answer
    # probe_failed forever.
    commits = [c.strip() for c in (commits or [])
               if isinstance(c, str) and c.strip()]
    # A historical record may hold anything, and two of the wrong things are
    # dangerous rather than merely useless: `True` becomes 1 under int(), and a
    # negative number is truthy — either would send the probe at a real,
    # unrelated pull request. Both normalise to "no PR recorded".
    try:
        pr = 0 if isinstance(pr, bool) else int(pr or 0)
    except (TypeError, ValueError):
        pr = 0
    pr = pr if pr > 0 else 0
    if not (branch or pr or commits):
        return _answer(UNKNOWN, reason=NO_WORK_LINK,
                       detail="the plan records no branch, PR, or commit — "
                              "add one with: hive roadmap set <id> --branch … "
                              "--pr … --commit …")
    if root is None:
        root = worktree_root()
    if root is None:
        return _answer(UNKNOWN, reason=NO_GITHUB_REMOTE,
                       detail="not inside a git checkout, so nothing can be "
                              "checked")
    root = str(root)

    unknowns = []
    for probe in ([lambda: _probe_pr(pr, runner, root, timeout_s)] if pr else []) \
            + ([lambda: _probe_commits(commits, runner, root, timeout_s)]
               if commits else []) \
            + ([lambda: _probe_branch(branch, runner, root, timeout_s)]
               if branch else []):
        answer = probe()
        if answer["state"] != UNKNOWN:
            return answer
        unknowns.append(answer)
    return max(unknowns, key=lambda a: _REASON_RANK.get(a["reason"], 0))


# --- how stale is this checkout? ---------------------------------------------
# Same bargain as `merge_state`, different question. This one exists because a
# checkout drifts silently: nothing in a working tree announces that the ref it
# branched from moved on 64 commits ago, and the answer is invisible precisely
# when it matters — the moment before you build on a stale base.

CURRENT = "current"
BEHIND = "behind"
STALENESS_STATES = (CURRENT, BEHIND, UNKNOWN)

NO_CHECKOUT = "no_checkout"
NO_INTEGRATION_REF = "no_integration_ref"
STALENESS_REASONS = (NO_CHECKOUT, NO_INTEGRATION_REF, PROBE_FAILED)

# How old a fetch may be before the comparison is worth distrusting. A day: long
# enough that ordinary work does not trip it, short enough that "0 behind"
# stops meaning "up to date" before it becomes a lie. Callers override it — the
# `--max-age-hours` idiom this repo already uses for staleness gates.
DEFAULT_MAX_AGE_HOURS = 24.0

_FETCH_MARKER = "FETCH_HEAD"


def _fetch_age_hours(runner, root, timeout_s, now) -> float | None:
    """Hours since this checkout last heard from its remote, or None when that
    cannot be established. Read from `FETCH_HEAD`'s mtime, which git rewrites on
    every fetch — the cheapest honest proxy available without a network call.

    `--git-common-dir`, NOT `--absolute-git-dir`: inside a linked worktree the
    latter points at `.git/worktrees/<name>`, which has no FETCH_HEAD, so every
    probe run from a worktree reported "never fetched" — a false staleness alarm
    in exactly the place this repo does most of its work. The common dir is the
    main repo's `.git`, which is where fetches actually land, and in an ordinary
    checkout the two are the same path anyway."""
    import os
    rc, out, _ = runner(["git", "rev-parse", "--git-common-dir"], root,
                        timeout_s)
    if rc != 0 or not out.strip():
        return None
    git_dir = out.strip()
    if not os.path.isabs(git_dir):      # git answers ".git" in a plain checkout
        git_dir = os.path.join(root, git_dir)
    try:
        stamp = os.path.getmtime(os.path.join(git_dir, _FETCH_MARKER))
    except OSError:
        return None                     # never fetched, or the file is gone
    return max(0.0, (now - stamp) / 3600.0)


def staleness(*, root=None, runner=None, timeout_s=PROBE_TIMEOUT_SECONDS,
              max_age_hours: float = DEFAULT_MAX_AGE_HOURS, now=None) -> dict:
    """How far this checkout has drifted from its integration ref.

    Returns ``{state, reason, detail, behind, ahead, ref, fetch_age_hours,
    fetch_stale, branch, upstream, upstream_missing}`` and never raises.

    **It never fetches.** A probe that reached the network could not run
    anywhere a probe belongs: not on the prompt path, not in a hook, not on a
    plane. So it compares against the ref as this checkout last saw it, and
    reports the age of that snapshot alongside the count — because "0 commits
    behind" and "0 commits behind, last fetched in July" are different facts and
    only one of them means up to date. That pairing is the whole design: the
    count without the age is the misleading half.

    ``upstream`` is the second half of the same bargain, and was added after
    this probe answered ``current`` for a checkout whose `git pull` was broken.
    The counts are measured against the INTEGRATION ref — where a merge lands —
    which resolves on any clone. Whether this branch tracks anything is a
    separate fact, and it is the one that decides if `git pull` works at all, so
    it ships beside the counts instead of being inferred from them.

    ``unknown`` carries its reason, like every other answer in this module:
    ``no_checkout`` (not a git worktree), ``no_integration_ref`` (nothing to
    compare against — no `origin/HEAD`, `origin/master` or `origin/main`, which
    `git clone --branch` and `git worktree add` both leave absent), or
    ``probe_failed``."""
    runner = runner or _default_runner
    if now is None:
        import time
        now = time.time()
    if root is None:
        root = worktree_root()
    if root is None:
        return _stale_answer(UNKNOWN, reason=NO_CHECKOUT,
                             detail="not inside a git checkout")
    root = str(root)

    ref = _integration_ref(runner, root, timeout_s)
    age = _fetch_age_hours(runner, root, timeout_s, now)
    # Asked for every state, including the unknown ones: a checkout that cannot
    # be compared can still be one whose `git pull` is broken, and that is the
    # more actionable of the two facts.
    probe = tracking(root=root, runner=runner, timeout_s=timeout_s)
    tracked = dict(branch=probe["branch"], upstream=probe["upstream"])
    if not ref:
        return _stale_answer(UNKNOWN, reason=NO_INTEGRATION_REF,
                             detail="no integration ref resolved (tried "
                                    + ", ".join(_INTEGRATION_REFS) + ")",
                             fetch_age_hours=age,
                             max_age_hours=max_age_hours, **tracked)
    rc, out, err = runner(["git", "rev-list", "--left-right", "--count",
                           f"HEAD...{ref}"], root, timeout_s)
    if rc != 0:
        return _stale_answer(UNKNOWN, reason=PROBE_FAILED, ref=ref,
                             detail=_first_line(err)
                             or f"git rev-list exited {rc}",
                             fetch_age_hours=age, max_age_hours=max_age_hours,
                             **tracked)
    parts = str(out or "").split()
    try:
        ahead, behind = int(parts[0]), int(parts[1])
    except (IndexError, ValueError):
        return _stale_answer(UNKNOWN, reason=PROBE_FAILED, ref=ref,
                             detail=f"unreadable rev-list output: {out!r}",
                             fetch_age_hours=age, max_age_hours=max_age_hours,
                             **tracked)
    return _stale_answer(BEHIND if behind else CURRENT, ref=ref, ahead=ahead,
                         behind=behind, fetch_age_hours=age,
                         max_age_hours=max_age_hours, **tracked,
                         detail=f"{behind} behind / {ahead} ahead of {ref}")


def _stale_answer(state, *, reason="", detail="", ref="", ahead=0, behind=0,
                  fetch_age_hours=None, max_age_hours=DEFAULT_MAX_AGE_HOURS,
                  branch="", upstream="") -> dict:
    """One staleness answer. ``fetch_stale`` is True when the snapshot this
    comparison used is older than *max_age_hours* — and also when its age could
    not be read at all, because an unknown age is not evidence of a fresh one.

    ``upstream_missing`` follows the same rule from the other side: it is True
    whenever no tracking ref could be read, including outside a checkout, since
    "we could not ask" is not evidence that `git pull` would work."""
    return {"state": state, "reason": reason,
            "detail": str(detail)[:MAX_DETAIL_CHARS], "ref": ref,
            "ahead": ahead, "behind": behind,
            "fetch_age_hours": fetch_age_hours,
            "fetch_stale": (fetch_age_hours is None
                            or fetch_age_hours > max_age_hours),
            "branch": branch, "upstream": upstream,
            "upstream_missing": not upstream}
