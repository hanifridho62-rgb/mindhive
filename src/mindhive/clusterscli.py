# SPDX-License-Identifier: Apache-2.0
# src/mindhive/clusterscli.py
"""`hive clusters` — the parallel-orchestration layer the skill pipeline reads.

## Why this lives outside the parser module

Third module to take the self-registering trade, after `bench.register` and
`briefcli.register`, and for the reason those two record: `__main__.py` sat at a
shrink-only ratchet (`tests/test_file_size_cap.py`) with no headroom when this
was written, and the argparse wiring plus three `cmd_*` bodies is ~55 lines.
Since the P9 split the wiring is `commands/parsers/__init__.py`, capped at 500
lines by `tests/test_commands_reexport.py`; here the group costs that file three.

Nothing about the command changed — same `hive clusters build|plan|show`, same
flags, same output. `/mindhive-orchestrator` reads `hive clusters plan`'s lane
assignment (`commands/mindhive-orchestrator.md:42`,
`references/parallel-orchestration.md:17-47`, pinned by
`tests/test_mindhive_orchestrator_skill.py`), so the command NAME is a contract
even though its implementation address is not.
"""
from __future__ import annotations


def cmd_clusters_build(args) -> int | None:
    """Build the cluster DB (parallel-orchestration layer) and store the sidecar."""
    from . import clusters as clusters_mod
    payload = clusters_mod.build_and_store(root=args.root, lanes=args.lanes,
                                           use_git=args.git)
    c = payload["counts"]
    print(f"clustered {c['files']} files into {c['clusters']} clusters "
          f"-> {c['lanes']} lane(s); stored cluster_db.json")
    return None


def cmd_clusters_show(args) -> int | None:
    from . import clusters as clusters_mod
    payload = clusters_mod.latest()
    if not payload:
        print("(no cluster_db yet - run: hive clusters build)")
        return None
    c = payload["counts"]
    print(f"cluster DB from {payload.get('generated_at', '?')} "
          f"({c['files']} files, {c['clusters']} clusters)")
    for key, info in payload.get("clusters", {}).items():
        print(f"  {key}  [weight {info['weight']}, {len(info['files'])} files]")
    return None


def cmd_clusters_plan(args) -> int | None:
    """Print the file-ownership-safe shard plan the orchestrator fans out to
    parallel sub-agents. Computes fresh (does not require a stored DB)."""
    from . import clusters as clusters_mod
    payload = clusters_mod.build(root=args.root, lanes=args.lanes, use_git=args.git)
    plan = payload["shard_plan"]
    print(f"{len(plan)} parallel lane(s) for {payload['counts']['files']} files:")
    for lane in plan:
        print(f"  lane {lane['lane']}: {len(lane['files'])} files across "
              f"{len(lane['clusters'])} cluster(s) "
              f"[est_complexity {lane['est_complexity']}]")
        for key in lane["clusters"]:
            print(f"      - {key}")
    return None


def register(subparsers) -> None:
    """Wire the `clusters` group onto the root subparser set built by
    `commands.parsers._build_parser`."""
    # --- clusters: parallel-orchestration layer for /mindhive-orchestrator ---
    cl = subparsers.add_parser("clusters",
                        help="cluster the repo, classify file complexity, plan parallel lanes")
    clsub = cl.add_subparsers(dest="clusters_cmd", required=True)
    for _name, _help, _fn in (
        ("build", "cluster the repo + store cluster_db.json", cmd_clusters_build),
        ("plan", "print the file-ownership-safe parallel shard plan", cmd_clusters_plan),
    ):
        _p = clsub.add_parser(_name, help=_help)
        _p.add_argument("--root", default=None, help="repo root to scan (default: cwd)")
        _p.add_argument("--lanes", type=int, default=None,
                        help="number of parallel lanes (default: one per cluster; clamped to [1, n])")
        _p.add_argument("--git", action="store_true",
                        help="weight complexity by git churn (opt-in; no-op if git is absent)")
        _p.set_defaults(func=_fn)
    clsub.add_parser("show", help="print the latest stored cluster DB").set_defaults(func=cmd_clusters_show)
