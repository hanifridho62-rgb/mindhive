# SPDX-License-Identifier: Apache-2.0
"""Context-assembly handlers: `state`, `directives`, `context`, `packs`,
`premise check`, `briefing`, `symbols` and `repo stale` -- the commands
that read projections and answer "what does the session load".
"""
import json
import sys
from .. import paths

__all__ = ["cmd_symbols", "cmd_state", "cmd_directives", "cmd_context",
    "_packs_manifest_path", "cmd_packs_check", "cmd_packs_report",
    "cmd_premise_check", "cmd_briefing", "_repo_default_max_age",
    "cmd_repo_stale"]


# --- command handlers -------------------------------------------------------
# Each handler takes the parsed argparse.Namespace and returns an exit code, or
# None to mean 0. They are bound to their subparser via set_defaults(func=...)
# in _build_parser(), so dispatch is a single args.func(args) call instead of a
# long if/elif ladder. Per-command heavy imports stay local to the handler that
# needs them (keeps `hive` startup cheap and import side effects scoped).

def cmd_symbols(args) -> int | None:
    from .. import symbols as symbols_mod
    return symbols_mod.cli(args)

def cmd_state(args) -> int | None:
    project = getattr(args, "project", None)
    # Warn (stderr, non-fatal) when a bare `hive state` is about to resolve to a
    # brand-new namespace inferred from cwd — the "wrong project from the wrong
    # directory" failure. An explicit --project or MINDHIVE_PROJECT silences it.
    if project is None and paths.is_freshly_minted():
        print(f"warning: no memory for detected project "
              f"'{paths.detect_project()}' — run from the project dir, set "
              f"MINDHIVE_PROJECT, or pass --project. See `hive projects`.",
              file=sys.stderr)
    # Self-evident rather than something to remember to check. A branch that
    # tracks nothing is invisible locally: `git status` says nothing about it,
    # and `hive repo stale` answers `current` because its counts come off the
    # integration ref, which resolves either way. So the checkout stops being
    # able to notice it has fallen behind — and this repo's `master` sat that
    # way long enough to drift 64 commits. This is the one command the
    # session-start hook runs, it costs two git calls and no network, and it
    # says nothing at all when the branch tracks something. Silent on any
    # failure and outside a checkout: printing STATE.md is this command's job.
    try:
        from .. import repo as repo_mod
        track = repo_mod.tracking()
        if track["upstream_missing"] and track["branch"]:
            name = track["branch"]
            print(f"warning: branch '{name}' tracks nothing — `git pull` here "
                  f"fails, so this checkout cannot notice it has fallen "
                  f"behind. Fix: git branch --set-upstream-to=origin/{name} "
                  f"{name}  (then `hive repo stale`)", file=sys.stderr)
    except Exception:                                       # noqa: BLE001
        pass
    # MN-18 U4: refresh the MindNetwork projection here, stale-gated. The session-start
    # hook runs `hive state`, and this is the only command in that hook belonging to this
    # repo. Without it the projection never returns to fresh and per-task phase direction
    # never appears. Silent on failure: printing STATE.md is this command's job and a
    # projection rebuild must not be able to stop it.
    #
    # Measured 2026-08-14 on the live store (35,202 events), min of 5: this refresh 82 ms
    # when the gate finds it fresh and 643 ms when it actually rebuilds; `repo.tracking()`
    # above 122 ms; `sopgate.probe()` (via the projection, not this path) 0.1 ms. The whole
    # command is 699 ms end to end, worst of 5 1468 ms.
    #
    # That is OVER the "~0.4 s" both copies of this note used to claim, and saying so is
    # the point of the correction: the number was a real target, the path exceeds it, and
    # nothing here has been relabelled to make the target look like it was merely wrong.
    # Choosing a replacement budget needs a decision about what session start is allowed
    # to cost, which is bigger than the change that measured it — recorded as the open
    # question in docs/plans/2026-08-14-001-feat-close-five-gaps-plan.md.
    try:
        from ..mindnet import autodeclare as mn_auto
        mn_auto.refresh_projection(project)
    except Exception:                                       # noqa: BLE001
        pass
    # Self-heal the mandatory-SOP sidecars (missing, corrupt, or written before
    # `agent_authored`): see `sop.heal_sidecar`, which is total. Session start is
    # the right place: this is the one command in that hook that is this repo's.
    from .. import sop as _sop
    _sop.heal_sidecar(project)
    s = paths.project_dir(project) / "STATE.md"
    print(s.read_text(encoding="utf-8") if s.exists()
          else "(no STATE.md — run: hive project)")
    return None

def cmd_directives(args) -> int | None:
    from .. import directives as directives_mod
    print(directives_mod.directives())
    return None

def cmd_context(args) -> int | None:
    """Report what auto-loads into every session — ranked by estimated token
    cost, with trim suggestions. Reporting never mutates anything; the write
    paths are --set-budget (and the memory budgets beside it), which persists
    the namespace's token budget, and --set-adaptive-tail, which persists the
    digest's adaptive-tail opt-in. --check turns the report into a gate: exit 1
    when over the budget (the hook adoption flows and scripts rely on) or when
    MEMORY.md is truncated with no rollover notice arriving before the cut,
    exit 2 when asked to check with no budget configured at all. A managed
    truncation, or a digest past the composer's safe budget, is a WARNING."""
    from .. import context as context_mod
    project = getattr(args, "project", None)
    origin = getattr(args, "origin", "all")

    set_memory_lines = getattr(args, "set_memory_lines", None)
    set_memory_bytes = getattr(args, "set_memory_bytes", None)
    adaptive = getattr(args, "set_adaptive_tail", None)
    writes_budget = (getattr(args, "set_budget", None) is not None
                     or set_memory_lines is not None or set_memory_bytes is not None)
    if writes_budget:
        # One sidecar, one write. A memory override alone must not drop the
        # token budget already stored there, so the current value is read back
        # and rewritten when only a memory budget is being changed. The
        # carry-forward between the two memory keys is save_budget's job.
        budget_value = getattr(args, "set_budget", None)
        if budget_value is None:
            budget_value = context_mod.load_budget(project)
        if budget_value is None:
            flag = ("--set-memory-lines" if set_memory_lines is not None
                    else "--set-memory-bytes")
            print(f"error: {flag} needs a token budget too — pass "
                  "--set-budget N (the sidecar holds both)", file=sys.stderr)
            return 2
    if adaptive is not None:
        # After the refusal above, never before it: a command that exits 2 has
        # stored nothing, the adaptive-tail opt-in included.
        node = context_mod.save_adaptive_tail(project, **adaptive)
        print(f"adaptive tail {'enabled' if node['enabled'] else 'disabled'}: "
              f"lessons cap {node['lessons']}, decisions cap {node['decisions']} "
              f"(stored in {context_mod.BUDGET_FILE}; `hive sync` applies it)")
    if writes_budget:
        data = context_mod.save_budget(budget_value, project=project,
                                       memory_lines=set_memory_lines,
                                       memory_bytes=set_memory_bytes)
        print(f"context budget set to {data['budget']:,} est. tokens"
              + (f", MEMORY.md line budget {data['memory_lines']}"
                 if "memory_lines" in data else "")
              + (f", MEMORY.md byte budget {data['memory_bytes']}"
                 if "memory_bytes" in data else "")
              + f" (stored in {context_mod.BUDGET_FILE})")
    if adaptive is not None or writes_budget:
        return None

    segments = []
    if origin in ("all", "hive"):
        segments += context_mod.collect_hive(project=project)
    if origin in ("all", "claude"):
        segments += context_mod.collect_claude()
    # T9: the per-turn cost. Last, so the two session-start origins keep the
    # order a reader is used to and the new one reads as an addition.
    if origin in ("all", "hook"):
        segments += context_mod.collect_hook(project=project)

    budget = getattr(args, "budget", None)
    if budget is None:
        budget = context_mod.load_budget(project)

    report = context_mod.analyze(segments, budget=budget)
    found = context_mod.findings(segments, project=project)

    if getattr(args, "json", False):
        print(context_mod.to_json(report, found))
    else:
        print(context_mod.render_text(report, found, top=getattr(args, "top", None)))

    if getattr(args, "check", False):
        # Two independent failures, checked in this order because they are not
        # the same kind of problem. The token budget is about COST — context
        # spent. A truncated MEMORY.md is about LOSS — context paid for and never
        # delivered, which is the worse of the two and needs no configured
        # budget to be a fact, so it is reported even when the token check
        # cannot run for want of one.
        truncated = [f for f in found if f.rule == "memory_truncated"]
        for f in truncated:
            print(f"TRUNCATED: {f.message}", file=sys.stderr)
        # Warned, never failed (blueprint P1c): the rollover notice tells the
        # session what was left out, or the loader still reads the whole file.
        for f in found:
            if f.rule in ("memory_rollover_truncated", "memory_over_safe_budget"):
                print(f"WARNING: {f.message}", file=sys.stderr)
        if budget is None:
            if truncated:
                return 1
            print("error: --check needs a budget — set one with "
                  "`hive context --set-budget N` (or pass --budget N)",
                  file=sys.stderr)
            return 2
        over = report.get("over_budget") or 0
        if over > 0:
            print(f"OVER BUDGET by ~{over:,} est. tokens — trim before adding "
                  f"anything new (see suggestions above)", file=sys.stderr)
            return 1
        if truncated:
            return 1
    return None

def _packs_manifest_path(arg) -> "Path":
    """Resolve the packs manifest: --manifest wins; otherwise
    <git toplevel>/docs/packs/packs.json. git rev-parse is used instead of
    bare cwd so the command works from any subdirectory of the checkout
    (inside a linked worktree, --show-toplevel is the worktree root — the
    checkout being validated, which is exactly right)."""
    from pathlib import Path
    if arg:
        return Path(arg)
    import subprocess
    from .. import paths as paths_mod
    try:
        r = subprocess.run(["git", "rev-parse", "--show-toplevel"],
                           capture_output=True, text=True, timeout=2,
                           creationflags=paths_mod.NO_WINDOW)
        if r.returncode == 0 and r.stdout.strip():
            return Path(r.stdout.strip()) / "docs" / "packs" / "packs.json"
    except Exception:
        pass
    return Path("docs") / "packs" / "packs.json"

def cmd_packs_check(args) -> int | None:
    """Run every pack quality gate; exit 1 on any error-severity finding —
    the same gate contract as `hive context --check`. Read-only."""
    from .. import packs as packs_mod
    report = packs_mod.analyze(_packs_manifest_path(getattr(args, "manifest", None)))
    if getattr(args, "json", False):
        print(packs_mod.to_json(report))
    else:
        print(packs_mod.render_text(report))
    return None if report.get("ok") else 1

def cmd_packs_report(args) -> int | None:
    """Full pack stats (depth, similarity matrices, findings). Informational:
    always exits 0 — gating belongs to `hive packs check`."""
    from .. import packs as packs_mod
    report = packs_mod.analyze(_packs_manifest_path(getattr(args, "manifest", None)))
    only = getattr(args, "pack", None)
    if only:
        stats = report.get("stats") or {}
        packs_stats = stats.get("packs") or {}
        stats["packs"] = {k: v for k, v in packs_stats.items() if k == only}
        report["findings"] = [f for f in report.get("findings") or []
                              if only in f.get("subject", "")]
    if getattr(args, "json", False):
        print(packs_mod.to_json(report))
    else:
        print(packs_mod.render_text(report))
    return None

def cmd_premise_check(args) -> int | None:
    """Check a text's checkable factual claims against the record.

    Exit code is the product: non-zero when anything is CONTRADICTED, so this
    composes into a script. UNKNOWN exits zero — "could not check" is not a
    finding, and failing on it would train the operator to pass `|| true`."""
    from pathlib import Path

    from .. import events as ev_mod, premise
    if getattr(args, "file", None):
        try:
            text = Path(args.file).read_text(encoding="utf-8", errors="replace")
        except OSError as e:
            print(f"cannot read {args.file}: {e}")
            return 2
    else:
        text = sys.stdin.read() if not sys.stdin.isatty() else ""

    found = premise.extract(text)
    if not found["scanned"] and text:
        # Reported, not silently passed: "too big to scan" and "nothing
        # asserted" are different answers and must not look alike.
        print(f"(not scanned — {len(text)} chars exceeds the "
              f"{premise.PREMISE_SCAN_CAP}-char budget)")
        return 0
    verdicts = premise.check(found["claims"], evs=ev_mod.read_all(args.project))
    print(premise.report(verdicts))
    for kind, dropped in (found.get("truncated") or {}).items():
        print(f"(+{dropped} further {kind} claim(s) not checked — per-class cap)")
    return 1 if any(v["outcome"] == premise.CONTRADICTED for v in verdicts) else 0

def cmd_briefing(args) -> int | None:
    from ..briefing import briefing as build_briefing
    bpath = build_briefing()
    print(str(bpath))
    print(bpath.read_text(encoding="utf-8"))
    return None

def _repo_default_max_age() -> float:
    """The staleness default, read from the module that owns it rather than
    duplicated as an argparse literal — the same reason
    `rotation_supervisor_defaults` exists."""
    from .. import repo
    return repo.DEFAULT_MAX_AGE_HOURS

def cmd_repo_stale(args) -> int | None:
    """How stale is this checkout? Read-only and never fetches — see
    `repo.staleness`. A report, not a gate: it exits zero even when behind,
    because being behind is a normal state mid-branch and a diagnostic that
    fails would be pruned from whatever ran it."""
    from .. import repo
    max_age = getattr(args, "max_age_hours", None)
    result = repo.staleness(max_age_hours=max_age if max_age is not None
                            else repo.DEFAULT_MAX_AGE_HOURS)
    if getattr(args, "json", False):
        print(json.dumps(result, indent=2))
        return None
    age = result["fetch_age_hours"]
    age_text = ("never fetched (or unreadable)" if age is None
                else f"last fetched {age:.1f}h ago")
    if result["state"] == "unknown":
        print(f"checkout staleness: unknown ({result['reason']}) — "
              f"{result['detail']}")
    else:
        print(f"checkout staleness: {result['state']} — {result['behind']} behind "
              f"/ {result['ahead']} ahead of {result['ref']}")
    print(f"  {age_text}"
          + ("  [the comparison is older than the freshness window, so the "
             "counts above may already be wrong — `git fetch` to refresh]"
             if result["fetch_stale"] else ""))
    # The counts above are measured against the integration ref, which resolves
    # on any clone. Whether THIS branch tracks anything is the fact that decides
    # if `git pull` works, and reporting only the first is how a checkout whose
    # pull was broken read as `current`. Outside a checkout the reason already
    # said so, and a second line about upstreams would only bury it.
    if result["upstream_missing"] and result["reason"] != repo.NO_CHECKOUT:
        branch = result["branch"]
        print("  no upstream: `git pull` on this branch fails with \"no "
              "tracking information\", so nothing here refreshes the "
              f"{result['ref'] or 'remote'} the counts were measured against"
              + (f"  [e.g. git branch --set-upstream-to=origin/{branch} "
                 f"{branch}]" if branch
                 else "  [detached HEAD — check out a branch first]"))
    return None
