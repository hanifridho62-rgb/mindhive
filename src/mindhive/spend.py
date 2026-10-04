# SPDX-License-Identifier: Apache-2.0
# src/mindhive/spend.py
"""`hive spend` — read-only token-spend reports over Claude Code transcripts.

Plan item 0.3 of the Terion program. The Fase 0.1 snapshot
(`state/spend_baseline_2026-10-03.json`) was written by a one-off script, so
these reports re-state its definitions in code; a later run then reads against
the snapshot number for number, which is how Fase 0.5 judges the plugin diet.

- **Context tokens** of one API call are `input + cache_creation_input +
  cache_read_input`, counted once per message id (one reply is written as one
  row per content block), with `<synthetic>` replies skipped.
- **A session** is one transcript. It is in the window when its first recorded
  timestamp is -- at or after `--since`, or within the last `--days` -- and
  it counts toward the token reports with at least two calls. `subagent` =
  under a `subagents/` directory, `chore` = its first prompt opens one of
  `CHORE_PREFIXES`, `headless` = one of MindHive's headless children (below),
  `interactive` = the rest. `"Use the "` is the skill invoker's prompt
  (`invoker.py`); it also caught the snapshot's five chores, which were
  headless probes ("Use the Write tool to create a file named probe.txt…"),
  and it would misfile a typed prompt that opens with those words, as the
  snapshot did.
- **A headless session** is one in which MindHive's SessionStart hook left no
  run while another owner's did. `claude_cli` starts its children -- an
  auto-resume delivery, a mindnet mission -- with `MINDHIVE_HEADLESS=1`, on
  which MindHive's hooks exit without a word and no other plugin's do; Claude
  Code's own `entrypoint` cannot tell them apart, because a child inherits its
  parent's. Loop and fabricate iterations (a loop-mode auto-resume too),
  Workspace board agents and rotation successors start with their parent's
  environment, so MindHive's hooks run in them and they count as
  interactive. A SessionStart run whose command was not recorded is left
  out: its owner cannot be read. The split needs a readable MindHive
  SessionStart hook in `settings.json` (`headless_split: "unavailable"`
  otherwise). The snapshot had no such kind: its `interactive` row holds
  them, so `include_headless` (`"folded"`) reads against it like for like.
  Either way the headless kind is then not measured, and `tokens` says
  `None` for it, never a count of 0.
- **A hook's owner** is read off its command: each installed plugin's hook
  map, then `plugins/cache/<marketplace>/<plugin>` in the command, then
  `mindhive`. A `hook_additional_context` piece carries no command; it takes
  the owner of the run whose JSON answer it repeats, else `(no command)`. Such
  pieces are context, not runs, so hook health does not count them.
- **Output too large** is a run whose output Claude Code replaced with its
  `<persisted-output>` preview; a hook that only quotes the phrase is not.
- **A namespace** is where harvest places a transcript folder: by its own
  recorded cwd, with MINDHIVE_PROJECT not honoured.
- **Injected hook text and lookup results** stay in context for every call
  after them until the next `compact_boundary`, at `CHARS_PER_TOKEN`.
- **A row a transcript repeats** under a `uuid` it already holds counts once.
  The snapshot counted each copy, so its hook-run counts include 945 repeated
  rows, all in one transcript; its token figures are unaffected.

Hook health counts every session in the window; the token reports count the
sessions with two or more calls, injection and lookups the interactive ones
(headless ones too when folded).
A transcript that cannot be read through counts as unreadable, never as read.
Aggregates only: no prompt, command, hook output or tool result reaches a
report. Nothing here writes. The reading half lives in `spend_scan.py`.
"""
from __future__ import annotations

import json
import statistics
from collections import Counter
from pathlib import Path

from .spend_scan import (CHORE_PREFIXES, NO_COMMAND, UNATTRIBUTED,  # noqa: F401
                         UNKNOWN_AGENT, Scan, Session, classify_lookup, scan)

SCHEMA = "mindhive.spend/1"
SNAPSHOT_SCHEMA = "mindhive.spend_baseline/1"
CATEGORIES = ("interactive", "headless", "chore", "subagent")
CHARS_PER_TOKEN = 4
BASELINE_FIELDS = ("sessions", "first_call_ctx_median", "first_call_ctx_p90",
                   "ctx_per_call_mean", "fixed_baseline_share")
# The snapshot's owner labels, as this module spells them.
_SNAPSHOT_OWNERS = {"mindhive(settings.json)": "mindhive",
                    "(tanpa command)": NO_COMMAND, "(tak teratribusi)": UNATTRIBUTED}


class SnapshotError(ValueError):
    """A snapshot that cannot be read, or is not a spend baseline."""


def _pct(values, q):
    """The snapshot's percentile: the value at index int(q * n), clamped."""
    ordered = sorted(values)
    return ordered[min(len(ordered) - 1, int(q * len(ordered)))] if ordered else None


def _share(part, whole):
    return round(part / whole, 4) if whole else None


def eligible(scan_: Scan, kind: str | None = None) -> list:
    return [s for s in scan_.sessions
            if len(s.calls) >= 2 and (kind is None or s.kind == kind)]


def _agg(sessions: list) -> dict:
    if not sessions:
        return {"sessions": 0}
    calls = [c for s in sessions for c in s.calls]
    firsts = [s.calls[0] for s in sessions]
    total = sum(calls)
    return {"sessions": len(sessions), "calls": len(calls), "ctx_tokens": total,
            "output_tokens": sum(s.output_tokens for s in sessions),
            "ctx_per_call_mean": round(total / len(calls)),
            "ctx_per_call_median": statistics.median(calls),
            "first_call_ctx_median": statistics.median(firsts),
            "first_call_ctx_p90": _pct(firsts, 0.9),
            "fixed_baseline_share": _share(sum(s.calls[0] * len(s.calls)
                                               for s in sessions), total),
            "window": [min(s.call_span[0] for s in sessions),
                       max(s.call_span[1] for s in sessions)]}


def _frame(scan_: Scan, report: str, *, split: bool = True) -> dict:
    """The fields every report opens with. `split=False` for a report the
    headless split does not touch."""
    return {"schema": SCHEMA, "report": report, "since": scan_.since,
            **({"headless_split": scan_.headless_split} if split else {}),
            "source": {"root": str(scan_.root), "transcripts_read": scan_.files_read,
                       "transcripts_skipped_as_older": scan_.files_skipped,
                       "transcripts_unreadable": scan_.files_unreadable}}


def _by_namespace(sessions: list) -> list:
    """Sessions per MindHive namespace, read off each transcript folder's
    recorded cwd the way harvest places them. MINDHIVE_PROJECT names the
    namespace this process writes to, not where other folders belong, so it
    is not honoured here."""
    from . import harvest
    placed: dict = {}
    groups: dict = {}
    for s in sessions:
        if s.project_dir not in placed:
            try:
                placed[s.project_dir] = harvest.project_for_transcript_dir(
                    s.project_dir, honor_env=False) or "(unplaced)"
            except Exception:  # noqa: BLE001 - a report never dies on one folder
                placed[s.project_dir] = "(unplaced)"
        row = groups.setdefault(placed[s.project_dir], [0, 0, 0])
        row[0], row[1], row[2] = row[0] + 1, row[1] + len(s.calls), row[2] + sum(s.calls)
    rows = [{"namespace": name, "sessions": n, "calls": c, "ctx_tokens": t}
            for name, (n, c, t) in groups.items()]
    return sorted(rows, key=lambda r: (-r["ctx_tokens"], r["namespace"]))


def baseline(scan_: Scan) -> dict:
    """Context tokens per call by session kind: the snapshot's `tokens`. The
    headless kind is None unless the split was `separate`: not measured,
    which a measured zero would misreport as none found."""
    tokens = {kind: _agg(eligible(scan_, kind)) for kind in CATEGORIES}
    if scan_.headless_split != "separate":
        tokens["headless"] = None
    return {**_frame(scan_, "baseline"), "tokens": tokens,
            "by_namespace": _by_namespace(eligible(scan_))}


def hooks(scan_: Scan) -> dict:
    """Hook runs by (owner, event), and the context their text costs."""
    runs: dict = {}
    for s in scan_.sessions:
        for owner, event, kind, ms, too_large in s.hook_runs:
            r = runs.setdefault((owner, event), {"runs": 0, "errors": 0, "cancelled": 0,
                                                 "ms": [], "too_large": 0, "paths": set()})
            r["runs"] += 1
            r["errors"] += kind in ("hook_non_blocking_error", "hook_blocking_error")
            r["cancelled"] += kind == "hook_cancelled"
            r["too_large"] += too_large
            r["paths"].add(s.path)
            if ms is not None:
                r["ms"].append(ms)
    health = [{"owner": o, "event": e, "runs": r["runs"], "errors": r["errors"],
               "cancelled": r["cancelled"],
               "ms_median": statistics.median(r["ms"]) if r["ms"] else None,
               "ms_p90": _pct(r["ms"], 0.9), "output_too_large": r["too_large"],
               "sessions": len(r["paths"])}
              for (o, e), r in runs.items()]
    health.sort(key=lambda r: (-r["runs"], r["owner"], r["event"]))
    interactive = eligible(scan_, "interactive")
    ctx = sum(sum(s.calls) for s in interactive)
    cost: dict = {}
    for s in interactive:
        for event, owner, chars, calls in s.injections:
            c = cost.setdefault((event, owner), {"chars": [], "tokens": 0.0})
            c["chars"].append(chars)
            c["tokens"] += chars / CHARS_PER_TOKEN * calls
    rows = [{"event": e, "owner": o, "fires": len(c["chars"]),
             "chars_median": statistics.median(c["chars"]), "tokens": round(c["tokens"]),
             "share": _share(c["tokens"], ctx)} for (e, o), c in cost.items()]
    rows.sort(key=lambda r: (-r["tokens"], r["event"], r["owner"]))
    total = sum(c["tokens"] for c in cost.values())
    return {**_frame(scan_, "hooks"), "chars_per_token": CHARS_PER_TOKEN, "health": health,
            "injection": {"sessions": len(interactive), "ctx_tokens": ctx, "rows": rows,
                          "total_tokens": round(total), "total_share": _share(total, ctx)}}


def lookups(scan_: Scan) -> dict:
    """Memory lookups in interactive sessions: the calls that issue one, and
    the context their results take up."""
    interactive = eligible(scan_, "interactive")
    calls = sum(len(s.calls) for s in interactive)
    ctx = sum(sum(s.calls) for s in interactive)
    kinds: Counter = Counter()
    per_session, lookup_ctx, results = [], 0, 0.0
    for s in interactive:
        n = 0
        for tokens, found in zip(s.calls, s.lookup_calls):
            if found:
                n, lookup_ctx = n + 1, lookup_ctx + tokens
                kinds.update(found)
        per_session.append(n)
        results += sum(chars / CHARS_PER_TOKEN * kept for chars, kept in s.lookup_results)
    looked = sum(per_session)
    return {**_frame(scan_, "lookups"), "chars_per_token": CHARS_PER_TOKEN,
            "sessions": len(interactive), "calls": calls,
            "ctx_tokens": ctx, "lookup_calls": looked,
            "lookup_call_pct": round(100 * looked / calls, 2) if calls else None,
            "lookup_ctx_share": _share(lookup_ctx, ctx),
            "result_tokens": round(results), "result_share": _share(results, ctx),
            "per_session_median": statistics.median(per_session) if per_session else None,
            "per_session_p90": _pct(per_session, 0.9),
            "by_kind": dict(sorted(kinds.items(), key=lambda kv: (-kv[1], kv[0])))}


def agents(scan_: Scan) -> dict:
    """Subagent spend grouped by the agent type each one's meta.json records."""
    subs = eligible(scan_, "subagent")
    total = sum(sum(s.calls) for s in subs)
    groups: dict = {}
    for s in subs:
        groups.setdefault(s.agent_type or UNKNOWN_AGENT, []).append(s)
    rows = []
    for name, sessions in groups.items():
        a = _agg(sessions)
        rows.append({"agent_type": name, **{k: a[k] for k in (
            "sessions", "calls", "ctx_tokens", "output_tokens", "ctx_per_call_mean",
            "first_call_ctx_median")}, "share": _share(a["ctx_tokens"], total)})
    rows.sort(key=lambda r: (-r["ctx_tokens"], r["agent_type"]))
    return {**_frame(scan_, "agents", split=False), "rows": rows,
            "total": {"sessions": len(subs), "calls": sum(len(s.calls) for s in subs),
                      "ctx_tokens": total}}


# --- the snapshot -----------------------------------------------------------------

def find_snapshot(project: str | None = None) -> Path | None:
    """The newest `spend_baseline_*.json` in the namespace's `state/`, by the
    date in its name. Looks only: never creates the directory."""
    from . import paths
    found = sorted((paths.project_dir(project) / "state").glob("spend_baseline_*.json"))
    return found[-1] if found else None


def load_snapshot(path) -> dict:
    try:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise SnapshotError(f"{path}: cannot be read as a spend snapshot "
                            f"({exc.__class__.__name__})") from exc
    if not isinstance(data, dict) or data.get("schema") != SNAPSHOT_SCHEMA:
        raise SnapshotError(f"{path}: not a {SNAPSHOT_SCHEMA} snapshot")
    return data


# `load_snapshot` checks the schema name only. What sits under it was written
# by hand, so the comparisons read a field of the wrong shape as absent.

def _num(value):
    """A JSON number, or None for anything else (a bool included)."""
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def compare_baseline(report: dict, snapshot: dict) -> dict:
    """`BASELINE_FIELDS` per session kind: the snapshot's value, today's, and
    the difference. A kind today's report did not measure gets no rows."""
    tokens = _dict(snapshot.get("tokens"))
    out: dict = {}
    for kind in CATEGORIES:
        new = report["tokens"].get(kind)
        if new is None:
            out[kind] = {}
            continue
        old = _dict(tokens.get(kind))
        rows = {}
        for name in BASELINE_FIELDS:
            was, now = _num(old.get(name)), new.get(name)
            if was is None and now is None:
                continue
            rows[name] = {"snapshot": was, "now": now,
                          "delta": None if None in (was, now) else round(now - was, 4)}
        out[kind] = rows
    return out


def baseline_note(report: dict, snapshot: dict) -> str | None:
    """Why a baseline comparison is not like for like, or None when it is: a
    split report against a snapshot with no measured headless row."""
    if report.get("headless_split") == "separate" and not isinstance(
            _dict(snapshot.get("tokens")).get("headless"), dict):
        return ("the snapshot predates the headless split, so its interactive row "
                "also holds headless sessions; --include-headless reads like for like")
    return None


def _rate(row: dict, name: str):
    return _share(_num(row.get(name)) or 0, _num(row.get("runs")) or 0)


def compare_hooks(report: dict, snapshot: dict) -> list:
    """Per (owner, event): runs, error rate, too-large rate and median ms,
    snapshot beside today. Rates, because the windows differ in length. The
    snapshot's `(no command)` rows were context pieces counted as runs; they
    have no counterpart here and are left out."""
    listed = snapshot.get("hooks")
    old = {}
    for r in listed if isinstance(listed, list) else ():
        if not (isinstance(r, dict) and isinstance(r.get("owner"), str)
                and isinstance(r.get("event"), str)):
            continue
        owner = _SNAPSHOT_OWNERS.get(r["owner"], r["owner"])
        if owner != NO_COMMAND:
            old[(owner, r["event"])] = r
    new = {(r["owner"], r["event"]): r for r in report["health"]}
    rows = []
    for key in set(old) | set(new):
        was, now = old.get(key, {}), new.get(key, {})
        rows.append({"owner": key[0], "event": key[1],
                     "runs": {"snapshot": _num(was.get("runs")) or 0,
                              "now": now.get("runs", 0)},
                     "error_rate": {"snapshot": _rate(was, "errors"),
                                    "now": _rate(now, "errors")},
                     "too_large_rate": {"snapshot": _rate(was, "output_too_large"),
                                        "now": _rate(now, "output_too_large")},
                     "ms_median": {"snapshot": _num(was.get("ms_median")),
                                   "now": now.get("ms_median")}})
    rows.sort(key=lambda r: (-max(r["runs"]["snapshot"], r["runs"]["now"]),
                             r["owner"], r["event"]))
    return rows
