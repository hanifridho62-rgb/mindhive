# SPDX-License-Identifier: Apache-2.0
# src/mindhive/spendcli.py
"""`hive spend baseline|hooks|lookups|agents` — the CLI over `spend.py`.

Self-registering, the trade `clusterscli` and `bench` make: the group's parser
and handlers live here and `commands/parsers/__init__.py` spends one named call
on it. `spend` is imported at call time, so `import mindhive.__main__` stays as
light as `tests/test_cli_startup.py` holds it.

Exit codes: 0 a report printed (an empty window is a report too); 2 the
arguments stopped it -- `--days` that is not a positive number, a `--since`
that is not a date or an ISO time, a `--claude-dir` with no `projects`
directory under it, or an `--against` snapshot that cannot be read (an empty
one included). A default snapshot that cannot be read is warned about on
stderr and the report prints without it. `--help` states the same, and what
the JSON fields of the headless split mean, for a caller that reads no README.
"""
from __future__ import annotations

import argparse
import json
import math
import sys
from datetime import datetime
from pathlib import Path

_READ_ONLY = "This command is read-only and changed nothing."
_EPILOG = """\
exit codes:
  0    the report printed; an empty window is a report too
  2    the arguments stopped it: --days not a positive number, --since not a
       date or an ISO time, --claude-dir without a projects directory, or an
       --against snapshot that cannot be read

--json: baseline, hooks and lookups carry headless_split -- separate
(MindHive's headless children counted apart), folded (--include-headless:
counted as interactive) or unavailable (settings.json holds no readable
MindHive SessionStart hook). A session kind that was not measured is null
under tokens, never 0. A baseline read against a snapshot that predates the
split carries against.note. Aggregates only; nothing is written."""
# How a report counted MindHive's headless children, said under its head.
_SPLIT = {"separate": "MindHive's headless children counted apart",
          "folded": "MindHive's headless children counted as interactive "
                    "(--include-headless)",
          "unavailable": "MindHive's headless children not told apart: settings.json "
                         "holds no readable MindHive SessionStart hook"}


def _is_num(value) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _n(value) -> str:
    """A count with thousands separators, never in exponent form; `-` for
    anything that is not a number."""
    if not _is_num(value):
        return "-"
    if isinstance(value, float) and not value.is_integer():
        return f"{value:,.1f}"
    return f"{int(value):,}"


def _p(share) -> str:
    return f"{100 * share:.2f}%" if _is_num(share) else "-"


def _compared(name: str, row: dict) -> str:
    """One field of the snapshot comparison. A share is a fraction, so it
    reads as a percentage and moves in percentage points."""
    delta = row.get("delta")
    if name.endswith("_share"):
        moved = f" ({100 * delta:+.2f} pp)" if _is_num(delta) else ""
        return f"{name} {_p(row.get('snapshot'))} -> {_p(row.get('now'))}{moved}"
    moved = ""
    if _is_num(delta):
        moved = f" ({'+' if delta >= 0 else '-'}{_n(abs(delta))})"
    return f"{name} {_n(row.get('snapshot'))} -> {_n(row.get('now'))}{moved}"


def _head(report: dict) -> str:
    src = report["source"]
    since = report.get("since")
    window = f"sessions started since {since}" if since else "every session on disk"
    unreadable = src.get("transcripts_unreadable")
    head = (f"hive spend {report['report']} -- {window}; {src['transcripts_read']} "
            f"transcript(s) read, {src['transcripts_skipped_as_older']} older skipped"
            + (f", {unreadable} unreadable" if unreadable else ""))
    split = _SPLIT.get(report.get("headless_split"))
    return f"{head}\n{split}" if split else head


def _footer(report: dict) -> list:
    """The close the repo's read-only reports share (`context.py`,
    `packs.py`); the `~` figures are characters divided by a constant."""
    if "chars_per_token" not in report:
        return ["", _READ_ONLY]
    return ["", f"Figures marked ~ are estimates (~{report['chars_per_token']} "
                f"chars/token). {_READ_ONLY}"]


def _render_baseline(report: dict) -> list:
    lines = [_head(report), f"{'kind':<12}{'sessions':>9}{'calls':>10}{'ctx tokens':>17}"
             f"{'ctx/call':>10}  {'first call med / p90':>21}  fixed share"]
    for kind, t in report["tokens"].items():
        if t is None:                 # not measured; the head says why
            continue
        if not t.get("sessions"):
            lines.append(f"{kind:<12}{0:>9}")
            continue
        first = f"{_n(t['first_call_ctx_median'])} / {_n(t['first_call_ctx_p90'])}"
        lines.append(f"{kind:<12}{t['sessions']:>9,}{t['calls']:>10,}{t['ctx_tokens']:>17,}"
                     f"{t['ctx_per_call_mean']:>10,}  {first:>21}  "
                     f"{_p(t['fixed_baseline_share'])}")
    if report["by_namespace"]:
        lines.append("by namespace (ctx tokens, sessions): " + "; ".join(
            f"{r['namespace']} {_n(r['ctx_tokens'])} ({r['sessions']})"
            for r in report["by_namespace"][:10]))
    against = report.get("against")
    if against:
        lines.append(f"against {against['path']} (created {against.get('created')}):")
        for kind, rows in against["deltas"].items():
            if rows:
                lines.append(f"  {kind}: " + "; ".join(
                    _compared(name, r) for name, r in rows.items()))
        if against.get("note"):
            lines.append(f"  note: {against['note']}")
    return lines


def _render_hooks(report: dict) -> list:
    lines = [_head(report), "hook runs, every session in the window:",
             f"{'runs':>8}{'errors':>8}{'cancel':>8}{'ms med / p90':>18}{'too large':>11}"
             f"{'sessions':>10}  owner | event"]
    for r in report["health"]:
        ms = f"{_n(r['ms_median'])} / {_n(r['ms_p90'])}"
        lines.append(f"{r['runs']:>8,}{r['errors']:>8,}{r['cancelled']:>8,}{ms:>18}"
                     f"{r['output_too_large']:>11,}{r['sessions']:>10,}  "
                     f"{r['owner']} | {r['event']}")
    inj = report["injection"]
    lines.append(f"injected context, {inj['sessions']} interactive session(s), "
                 f"{_n(inj['ctx_tokens'])} ctx tokens:")
    for r in inj["rows"]:
        lines.append(f"{r['fires']:>8,} fires  {_n(r['chars_median']):>8} chars median  "
                     f"~{_n(r['tokens'])} tokens  {_p(r['share'])}  {r['event']} | {r['owner']}")
    lines.append(f"  all hooks: ~{_n(inj['total_tokens'])} tokens, {_p(inj['total_share'])}")
    against = report.get("against")
    if against:
        lines.append(f"against {against['path']} (created {against.get('created')}):")
        for r in against["rows"]:
            lines.append(
                f"  {r['owner']} | {r['event']}: runs {_n(r['runs']['snapshot'])} -> "
                f"{_n(r['runs']['now'])}; errors {_p(r['error_rate']['snapshot'])} -> "
                f"{_p(r['error_rate']['now'])}; too large "
                f"{_p(r['too_large_rate']['snapshot'])} -> {_p(r['too_large_rate']['now'])}")
    return lines


def _render_lookups(report: dict) -> list:
    r = report
    return [_head(r),
            f"{r['sessions']} interactive session(s), {_n(r['calls'])} calls, "
            f"{_n(r['ctx_tokens'])} ctx tokens",
            f"calls issuing a memory lookup: {_n(r['lookup_calls'])} "
            f"({'-' if r['lookup_call_pct'] is None else r['lookup_call_pct']}% of calls), "
            f"{_p(r['lookup_ctx_share'])} of context",
            f"lookup results kept in context: ~{_n(r['result_tokens'])} tokens, "
            f"{_p(r['result_share'])}",
            f"lookup calls per session: median {_n(r['per_session_median'])}, "
            f"p90 {_n(r['per_session_p90'])}",
            "by kind: " + (", ".join(f"{k} {v}" for k, v in r["by_kind"].items()) or "-")]


def _render_agents(report: dict) -> list:
    lines = [_head(report), f"{'sessions':>9}{'calls':>9}{'ctx tokens':>16}{'ctx/call':>10}"
             f"{'first call':>12}{'share':>9}  agent type"]
    for r in report["rows"]:
        lines.append(f"{r['sessions']:>9,}{r['calls']:>9,}{r['ctx_tokens']:>16,}"
                     f"{r['ctx_per_call_mean']:>10,}{_n(r['first_call_ctx_median']):>12}"
                     f"{_p(r['share']):>9}  {r['agent_type']}")
    t = report["total"]
    lines.append(f"total: {t['sessions']} subagent session(s), {_n(t['calls'])} calls, "
                 f"{_n(t['ctx_tokens'])} ctx tokens")
    return lines


def _snapshot(args):
    """(snapshot, path), or (None, None) for no comparison. Raises
    SnapshotError only for a snapshot the caller named."""
    from . import spend
    if getattr(args, "no_compare", True):
        return None, None
    if args.against == "":
        raise spend.SnapshotError("--against needs a snapshot path, not an empty string")
    path = args.against if args.against is not None else spend.find_snapshot()
    if path is None:
        return None, None
    try:
        return spend.load_snapshot(path), str(path)
    except spend.SnapshotError as exc:
        if args.against is not None:
            raise
        print(f"hive spend: warning: {exc}; printing without a comparison",
              file=sys.stderr)
        return None, None


def _parse_since(text: str) -> datetime | None:
    """A date (midnight UTC) or an ISO time (UTC unless it names an offset),
    in UTC; None for anything else, a moment UTC cannot hold included
    (9999-12-31T23:00-05:00 is the year 10000). Transcripts are stamped in UTC."""
    from .spend_scan import to_utc
    try:
        return to_utc(datetime.fromisoformat(text.strip()))
    except (ValueError, OverflowError):
        return None


def _run(args, build, render, compare=None) -> int | None:
    from . import spend
    since = None
    if args.since is not None:
        since = _parse_since(args.since)
        if since is None:
            print(f"hive spend: --since must be a date (YYYY-MM-DD) or an ISO time, "
                  f"not {args.since!r}", file=sys.stderr)
            return 2
    elif not args.all and not (math.isfinite(args.days) and args.days > 0):
        print(f"hive spend: --days must be a positive number of days, not {args.days:g}",
              file=sys.stderr)
        return 2
    claude_dir = None
    if args.claude_dir is not None:
        # Named, it must hold transcripts: a typo would otherwise print an
        # all-zero report, and an empty value would read the default home.
        claude_dir = Path(args.claude_dir).expanduser()
        if not args.claude_dir or not (claude_dir / "projects").is_dir():
            where = claude_dir / "projects" if args.claude_dir else "an empty --claude-dir"
            print(f"hive spend: no Claude Code transcripts under {where}", file=sys.stderr)
            return 2
    try:
        snapshot, path = _snapshot(args) if compare else (None, None)
    except spend.SnapshotError as exc:
        print(f"hive spend: {exc}", file=sys.stderr)
        return 2
    report = build(spend.scan(claude_dir,
                              days=None if args.all or since is not None else args.days,
                              since=since,
                              include_headless=getattr(args, "include_headless", False)))
    if snapshot is not None:
        report["against"] = {"path": path, "created": snapshot.get("created"),
                             **compare(report, snapshot)}
    print(json.dumps(report, indent=1) if args.json
          else "\n".join(render(report) + _footer(report)))
    return None


def cmd_spend_baseline(args) -> int | None:
    from . import spend

    def compare(rep, snap):
        note = spend.baseline_note(rep, snap)
        return {"deltas": spend.compare_baseline(rep, snap), **({"note": note} if note else {})}
    return _run(args, spend.baseline, _render_baseline, compare)


def cmd_spend_hooks(args) -> int | None:
    from . import spend
    return _run(args, spend.hooks, _render_hooks,
                lambda rep, snap: {"rows": spend.compare_hooks(rep, snap)})


def cmd_spend_lookups(args) -> int | None:
    from . import spend
    return _run(args, spend.lookups, _render_lookups)


def cmd_spend_agents(args) -> int | None:
    from . import spend
    return _run(args, spend.agents, _render_agents)


def register(subparsers) -> None:
    """Wire the `spend` group onto the root subparser set built by
    `commands.parsers._build_parser`."""
    sp = subparsers.add_parser(
        "spend", help="read-only token-spend reports over Claude Code transcripts")
    ssub = sp.add_subparsers(dest="spend_cmd", required=True)
    for name, text, fn, compares in (
        ("baseline", "context tokens per call by session kind, against the snapshot",
         cmd_spend_baseline, True),
        ("hooks", "hook runs, errors, timings and injected context by owner",
         cmd_spend_hooks, True),
        ("lookups", "memory lookups and the context their results take",
         cmd_spend_lookups, False),
        ("agents", "subagent spend by agent type", cmd_spend_agents, False),
    ):
        p = ssub.add_parser(name, help=text, description=f"hive spend {name}: {text}.",
                            epilog=_EPILOG,
                            formatter_class=argparse.RawDescriptionHelpFormatter)
        window = p.add_mutually_exclusive_group()
        window.add_argument("--days", type=float, default=7.0,
                            help="sessions that started in the last N days (default 7)")
        window.add_argument("--since", default=None, metavar="DATE",
                            help="sessions that started at or after DATE: YYYY-MM-DD "
                                 "(midnight UTC) or an ISO time (UTC unless it names "
                                 "an offset)")
        window.add_argument("--all", action="store_true",
                            help="every transcript on disk")
        p.add_argument("--claude-dir", default=None,
                       help="Claude Code's home directory (default: ~/.claude)")
        p.add_argument("--json", action="store_true", help="print the report as JSON")
        if name != "agents":
            p.add_argument("--include-headless", action="store_true", help=(
                "count MindHive's own headless sessions as interactive, as the "
                "snapshot did"))
        if compares:
            snap = p.add_mutually_exclusive_group()
            snap.add_argument("--against", default=None, help=(
                "snapshot to read against (default: the newest "
                "state/spend_baseline_*.json of this namespace)"))
            snap.add_argument("--no-compare", action="store_true",
                              help="print the report without a snapshot")
        p.set_defaults(func=fn)
