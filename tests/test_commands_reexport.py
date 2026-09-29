# tests/test_commands_reexport.py
"""P9 of the blueprint (PDF section 2.5 Bug 5, section 10.16): `__main__.py` is a
shim over the `mindhive.commands` package.

Before this change `src/mindhive/__main__.py` was 4,227 lines -- every handler
and the whole argparse tree in one file, held in `tests/test_file_size_cap.py`
by a shrink-only ceiling that had been raised four times. The split moves the
handlers to `mindhive.commands.<domain>` and the wiring to
`mindhive.commands.parsers`, and the three properties that make that safe are
what this file pins:

1. **The surface is unchanged.** `SURFACE` is the parser tree -- every
   top-level command and every verb under it -- snapshotted from the pre-split
   `_build_parser()` on 2026-09-14. A command that vanished, or a verb that
   moved groups, fails here by name.
2. **Identity, not copies.** Every name a handler module exports is the SAME
   object on `mindhive.__main__`, and every parser leaf whose handler lives in
   `commands` dispatches to that same object. The repo has been bitten once by
   "a mechanical module split silently breaks monkeypatch addresses"
   (`tests/test_file_size_cap.py`'s `pgtools` note); `inspect.getsource` and
   `monkeypatch.setattr(cli_mod, ...)` through `__main__` keep working only if
   the object is shared, so that is asserted, not assumed. `LEAF_HANDLERS`
   closes the gap identity alone leaves: it pins the handler NAME every path
   bound pre-split, so a leaf that lost its `set_defaults(func=...)` (which
   `_leaves` would skip in silence) or was rewired to a sibling fails by path.
3. **The ratchet turns the other way.** `__main__.py` stays under 90 lines and
   every module under `commands/` stays under 500 -- the blueprint's target,
   pinned as a test so the next handler goes into a domain module and a domain
   that outgrows the target is split rather than allow-listed.
"""
import argparse
import ast
import inspect
import os
import subprocess
import sys
from pathlib import Path

from mindhive import __main__ as cli_mod
from mindhive import commands, review

REPO = Path(__file__).resolve().parents[1]
SRC = REPO / "src" / "mindhive"
COMMANDS_DIR = SRC / "commands"
MAIN_CAP = 90          # blueprint section 10.16: `__main__.py` < 90 lines
MODULE_CAP = 500       # blueprint section 10.16: every module <= 500 lines
SPDX = "# SPDX-License-Identifier: Apache-2.0"

# The parser tree as it was BEFORE the split (2026-09-14, master @ bac198a):
# {command: {verb: {sub-verb: {}}}}. Names only; the handlers are checked by
# identity below, not by name.
SURFACE = {'append': {},
 'symbols': {},
 'hook': {},
 'session': {'run': {}, 'install-hook': {}},
 'project': {},
 'state': {},
 'projects': {},
 'directives': {},
 'context': {},
 'repo': {'stale': {}},
 'packs': {'check': {}, 'report': {}},
 # blueprint P8 (MEB K15, spec section 4.7): `cli-startup` and `recall-semantic`
 # register beside `dashboard`, from `bench_scenarios.register` via `bench.register`.
 'bench': {'dashboard': {}, 'cli-startup': {}, 'recall-semantic': {}},
 'inject': {'show': {}, 'table': {}},
 'pg': {'status': {},
        'migrate': {},
        'rebuild': {},
        'refresh': {},
        'embed': {},
        'parity': {},
        'tool': {},
        'mcp': {}},
 'consensus': {'contribute': {}, 'fold': {}, 'trace': {}},
 'verify': {},
 'selftest': {},
 'premise': {'check': {}},
 'recall': {},
 'consolidate': {},
 'gc': {},
 'export': {},
 'embed': {},
 'briefing': {},
 'review': {'queue': {}, 'clear': {}, 'show': {}},
 'log': {'update': {},
         'minor_change': {},
         'deletion': {},
         'reduction': {},
         'major_change': {},
         'replacement': {},
         'queue': {},
         'show': {},
         'pending': {}},
 'replenish': {'run': {}, 'explain': {}, 'status': {}},
 'sop': {'add': {},
         'update': {},
         'delete': {},
         'list': {},
         'show': {},
         'preflight': {},
         'injection': {}},
 'roadmap': {'set': {},
             'step': {},
             'interrupt': {},
             'resume': {},
             'complete': {},
             'archive': {},
             'link': {},
             'merge-state': {},
             'retire': {},
             'autoclose': {},
             'list': {},
             'show': {}},
 'mindnet': {'declare': {},
             'enter': {},
             'satisfy': {},
             'fall-back': {},
             'validate': {},
             'restat': {},
             'show': {},
             'list': {},
             'rebuild': {},
             'decompose': {},
             'depend': {},
             'orchestrate': {},
             'brief': {},
             'ab': {},
             'enforce': {}},
 'autoresume': {'schedule': {}, 'list': {}, 'cancel': {}, 'fire': {}, 'wait': {}},
 # blueprint P10 (spec Bug 9): the PRD assembly line, ported from branch
 # claude/prd-assembly-line-40e466 (3a45aea); registered after `autoresume`,
 # where that branch's `__main__.py` had it.
 'fabricate': {'compile': {}, 'dispatch': {}, 'run': {}, 'list': {}},
 'graph': {'build': {}, 'related': {}, 'export': {}},
 # blueprint P4b (spec section 2.8, Bug 8): `cache-check` compares the plugin
 # Claude Code loads with this checkout; registered after `autoscan`.
 'plugins': {'list': {}, 'scan': {}, 'autoscan': {}, 'cache-check': {},
             'recommend': {}},
 'skills': {'list': {},
            'invoke': {},
            'status': {},
            'stats': {},
            'record': {},
            'recommend': {},
            'trail': {}},
 'clusters': {'build': {}, 'plan': {}, 'show': {}},
 'lintester': {},
 'sync': {},
 'dashboard': {},
 'harvest': {},
 'autosync': {'register': {}},
 'loop': {},
 'flag': {},
 'failures': {'capture': {}, 'list': {}},
 'rules': {'list': {},
           'summaries': {},
           'show': {},
           'reflect': {},
           'resolve': {},
           'audit': {},
           'inject': {},
           'promote': {},
           'demote': {},
           'reconcile': {},
           'enforce': {}},
 'triggers': {'train': {}, 'list': {}, 'check': {}},
 'enrich': {'status': {}, 'on': {}, 'off': {}, 'preview': {}},
 'worksession': {'record': {}, 'status': {}, 'initiative': {}, 'list': {},
                 'migrate': {}},
 # blueprint P1f: QUARANTINED is reachable and `manual_reset` is its only exit,
 # so the breaker verbs shipped WITH the state rather than waiting for P1g.
 # P1g added the gate itself: `run`, `status` and `hook-install`.
 'ci': {'run': {}, 'status': {}, 'hook-install': {},
        'breaker': {'status': {}, 'reset': {}}},
 'watcher': {'run': {}, 'status': {}, 'recover': {}},
 # blueprint P1i: the DATA half of `hive pg migrate`'s SCHEMA. Registered
 # last, so it sits at the tail of this list as well as of `hive --help`.
 'db': {'migrate': {}, 'status': {}},
 # blueprint P1k: `hive dashboard`'s page with a heartbeat, on the same port.
 # Also registered last, so it takes the new tail.
 'ui': {}}

# The handler NAME every parser bound BEFORE the split (the `__func__` entries
# of the same 2026-09-14 snapshot). Self-registering groups included: this is
# the whole surface's dispatch, not only the part that moved.
LEAF_HANDLERS = {'append': 'cmd_append',
 'autoresume cancel': 'cmd_autoresume_cancel',
 'autoresume fire': 'cmd_autoresume_fire',
 'autoresume list': 'cmd_autoresume_list',
 'autoresume schedule': 'cmd_autoresume_schedule',
 'autoresume wait': 'cmd_autoresume_wait',
 'autosync': 'cmd_autosync_run',
 'autosync register': 'cmd_autosync_register',
 'bench cli-startup': 'cmd_bench_cli_startup',
 'bench dashboard': 'cmd_bench_dashboard',
 'bench recall-semantic': 'cmd_bench_recall_semantic',
 'briefing': 'cmd_briefing',
 'clusters build': 'cmd_clusters_build',
 'clusters plan': 'cmd_clusters_plan',
 'clusters show': 'cmd_clusters_show',
 'consensus contribute': 'cmd_contribute',
 'consensus fold': 'cmd_fold',
 'consensus trace': 'cmd_trace',
 'consolidate': 'cmd_consolidate',
 'context': 'cmd_context',
 'dashboard': 'cmd_dashboard',
 'db migrate': 'cmd_db_migrate',
 'db status': 'cmd_db_status',
 'directives': 'cmd_directives',
 'embed': 'cmd_embed',
 'enrich off': 'cmd_enrich',
 'enrich on': 'cmd_enrich',
 'enrich preview': 'cmd_enrich',
 'enrich status': 'cmd_enrich',
 'export': 'cmd_export',
 'fabricate compile': 'cmd_fabricate_compile',
 'fabricate dispatch': 'cmd_fabricate_dispatch',
 'fabricate list': 'cmd_fabricate_list',
 'fabricate run': 'cmd_fabricate_run',
 'failures capture': 'cmd_failures_capture',
 'failures list': 'cmd_failures_list',
 'flag': 'cmd_flag',
 'gc': 'cmd_gc',
 'graph build': 'cmd_graph_build',
 'graph export': 'cmd_graph_export',
 'graph related': 'cmd_graph_related',
 'harvest': 'cmd_harvest',
 'hook': 'cmd_hook',
 'inject show': 'cli',
 'inject table': 'cli_table',
 'lintester': 'cmd_lintester',
 'log deletion': 'cmd_log_add',
 'log major_change': 'cmd_log_add',
 'log minor_change': 'cmd_log_add',
 'log pending': 'cmd_log_pending',
 'log queue': 'cmd_log_queue',
 'log reduction': 'cmd_log_add',
 'log replacement': 'cmd_log_add',
 'log show': 'cmd_log_show',
 'log update': 'cmd_log_add',
 'loop': 'cmd_loop',
 'mindnet ab': 'cmd_ab',
 'mindnet brief': 'cmd_brief',
 'mindnet declare': 'cmd_mindnet_declare',
 'mindnet decompose': 'cmd_mindnet_decompose',
 'mindnet depend': 'cmd_mindnet_depend',
 'mindnet enforce': 'cmd_mindnet_enforce',
 'mindnet enter': 'cmd_mindnet_enter',
 'mindnet fall-back': 'cmd_mindnet_fall_back',
 'mindnet list': 'cmd_mindnet_list',
 'mindnet orchestrate': 'cmd_mindnet_orchestrate',
 'mindnet rebuild': 'cmd_mindnet_rebuild',
 'mindnet restat': 'cmd_mindnet_restat',
 'mindnet satisfy': 'cmd_mindnet_satisfy',
 'mindnet show': 'cmd_mindnet_show',
 'mindnet validate': 'cmd_mindnet_validate',
 'packs check': 'cmd_packs_check',
 'packs report': 'cmd_packs_report',
 'pg embed': 'cmd_embed',
 'pg mcp': 'cmd_mcp',
 'pg migrate': 'cmd_migrate',
 'pg parity': 'cmd_parity',
 'pg rebuild': 'cmd_rebuild',
 'pg refresh': 'cmd_refresh',
 'pg status': 'cmd_status',
 'pg tool': 'cmd_tool',
 'plugins autoscan': 'cmd_plugins_autoscan',
 'plugins list': 'cmd_plugins_list',
 'plugins recommend': 'cmd_plugins_recommend',
 'plugins scan': 'cmd_plugins_scan',
 'premise check': 'cmd_premise_check',
 'project': 'cmd_project',
 'projects': 'cmd_projects',
 'recall': 'cmd_recall',
 'replenish explain': 'cmd_replenish_explain',
 'replenish run': 'cmd_replenish_run',
 'replenish status': 'cmd_replenish_status',
 'repo stale': 'cmd_repo_stale',
 'review clear': 'cmd_review_clear',
 'review queue': 'cmd_review_queue',
 'review show': 'cmd_review_show',
 'roadmap archive': 'cmd_roadmap_archive',
 'roadmap autoclose': 'cmd_roadmap_autoclose',
 'roadmap complete': 'cmd_roadmap_complete',
 'roadmap interrupt': 'cmd_roadmap_interrupt',
 'roadmap link': 'cmd_roadmap_link',
 'roadmap list': 'cmd_roadmap_list',
 'roadmap merge-state': 'cmd_roadmap_merge_state',
 'roadmap resume': 'cmd_roadmap_resume',
 'roadmap retire': 'cmd_roadmap_retire',
 'roadmap set': 'cmd_roadmap_set',
 'roadmap show': 'cmd_roadmap_show',
 'roadmap step': 'cmd_roadmap_step',
 'rules audit': 'cmd_rules_audit',
 'rules demote': 'cmd_rules_demote',
 'rules enforce': 'cmd_rules_enforce',
 'rules inject': 'cmd_rules_inject',
 'rules list': 'cmd_rules_list',
 'rules promote': 'cmd_rules_promote',
 'rules reconcile': 'cmd_rules_reconcile',
 'rules reflect': 'cmd_rules_reflect',
 'rules resolve': 'cmd_rules_resolve',
 'rules show': 'cmd_rules_show',
 'rules summaries': 'cmd_rules_summaries',
 'selftest': 'cmd_selftest',
 'session install-hook': 'cmd_session_install_hook',
 'session run': 'cmd_session_run',
 'skills invoke': 'cmd_skills_invoke',
 'skills list': 'cmd_skills_list',
 'skills recommend': 'cmd_skills_recommend',
 'skills record': 'cmd_skills_record',
 'skills stats': 'cmd_skills_stats',
 'skills status': 'cmd_skills_status',
 'skills trail': 'cmd_skills_trail',
 'sop add': 'cmd_sop_add',
 'sop delete': 'cmd_sop_delete',
 'sop injection': 'cmd_sop_injection',
 'sop list': 'cmd_sop_list',
 'sop preflight': 'cmd_sop_preflight',
 'sop show': 'cmd_sop_show',
 'sop update': 'cmd_sop_update',
 'state': 'cmd_state',
 'symbols': 'cmd_symbols',
 'sync': 'cmd_sync',
 'triggers check': 'cmd_triggers_check',
 'triggers list': 'cmd_triggers_list',
 'triggers train': 'cmd_triggers_train',
 'verify': 'cmd_verify',
 'worksession initiative': 'cmd_worksession',
 'worksession list': 'cmd_worksession',
 'worksession migrate': 'cmd_worksession',
 'worksession record': 'cmd_worksession',
 'worksession status': 'cmd_worksession',
 'ci breaker reset': 'cmd_ci_breaker_reset',
 'ci breaker status': 'cmd_ci_breaker_status',
 'ci hook-install': 'cmd_ci_hook_install',
 'ci run': 'cmd_ci_run',
 'ci status': 'cmd_ci_status',
 'watcher recover': 'cmd_watcher_recover',
 'watcher run': 'cmd_watcher_run',
 'watcher status': 'cmd_watcher_status',
 'ui': 'cmd_ui',
 'plugins cache-check': 'cmd_plugins_cache_check'}


def _sub(parser):
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action
    return None


def _tree(parser) -> dict:
    sub = _sub(parser)
    if sub is None:
        return {}
    return {name: _tree(child) for name, child in sub.choices.items()}


def _leaves(parser, path=()):
    """Yield (path, func) for every parser that binds a handler."""
    func = parser.get_default("func")
    if func is not None:
        yield path, func
    sub = _sub(parser)
    if sub is not None:
        for name, child in sub.choices.items():
            yield from _leaves(child, path + (name,))


# --- 1. the surface --------------------------------------------------------------

def test_the_snapshot_is_not_empty():
    """CANARY for every comparison against SURFACE."""
    assert len(SURFACE) >= 40
    assert "mindnet" in SURFACE and "orchestrate" in SURFACE["mindnet"]


def test_the_parser_tree_is_the_pre_split_snapshot():
    assert _tree(cli_mod._build_parser()) == SURFACE


def test_the_top_level_order_is_the_pre_split_order():
    """`hive --help` lists commands in registration order; the split must
    register them in the same order, not merely the same set."""
    live = list(_sub(cli_mod._build_parser()).choices)
    assert live == list(SURFACE)


# --- 2. identity -----------------------------------------------------------------

def test_every_handler_module_exports_by_identity():
    seen = 0
    for name in commands.HANDLER_MODULES:
        mod = __import__(f"mindhive.commands.{name}", fromlist=["__all__"])
        assert mod.__all__, f"{name} declares no __all__"
        for attr in mod.__all__:
            assert getattr(cli_mod, attr) is getattr(mod, attr), (
                f"mindhive.__main__.{attr} is not the object "
                f"mindhive.commands.{name}.{attr}")
            seen += 1
    assert seen >= 100, "the re-export walk saw almost nothing"


def test_every_parser_leaf_dispatches_to_the_exported_handler():
    table = commands.dispatch_table()
    assert len(table) >= 100
    checked = 0
    for path, func in _leaves(cli_mod._build_parser()):
        if not func.__module__.startswith("mindhive.commands."):
            continue          # bench/pg/consensus/... register their own handlers
        assert table.get(func.__name__) is func, (
            f"hive {' '.join(path)} is wired to a copy of {func.__name__}")
        assert getattr(cli_mod, func.__name__) is func
        checked += 1
    assert checked >= 100, "almost no leaf is wired through commands/"


def test_every_leaf_binds_a_handler():
    """A parser with no verbs under it and no `func` default is a leaf `hive`
    reaches and then dies on (`AttributeError: func` in `_run`). `_leaves`
    skips such a parser in silence, so the identity walks cannot see one and
    their `>= 100` floors absorb a single loss; this walk names it."""
    def leafless(parser, path=()):
        sub = _sub(parser)
        if sub is None:
            if parser.get_default("func") is None:
                yield " ".join(path)
        else:
            for name, child in sub.choices.items():
                yield from leafless(child, path + (name,))
    assert list(leafless(cli_mod._build_parser())) == []


def test_every_path_binds_the_pre_split_handler_by_name():
    """Identity says each leaf reaches THE object of its name; this says the
    name is the one the pre-split tree bound to that path. A leaf rewired to
    a sibling, or one whose `set_defaults` was dropped, fails here by path."""
    assert len(LEAF_HANDLERS) >= 130, "the handler snapshot went thin"
    live = {" ".join(path): func.__name__
            for path, func in _leaves(cli_mod._build_parser())}
    assert live == LEAF_HANDLERS


def test_the_names_tests_reach_through_main_still_resolve():
    """The names other test files read off `mindhive.__main__` today."""
    from mindhive.commands import mindnet_orchestrate, mindnet_tasks, ledger, skills
    from mindhive.commands import parsers
    assert cli_mod.MINDNET_ORCHESTRATE_SIGNPOST is mindnet_orchestrate.MINDNET_ORCHESTRATE_SIGNPOST
    assert cli_mod.MN_REFUSED is mindnet_tasks.MN_REFUSED
    assert cli_mod.cmd_hook is ledger.cmd_hook
    assert cli_mod.cmd_mindnet_orchestrate is mindnet_orchestrate.cmd_mindnet_orchestrate
    assert cli_mod.cmd_skills_trail is skills.cmd_skills_trail
    assert cli_mod._build_parser is parsers._build_parser
    assert cli_mod._closest_command is parsers._closest_command
    assert cli_mod.build_cli_parser is parsers._build_parser


def test_getsource_through_main_reads_the_real_body():
    """`tests/test_faculty_beat.py` reads `cmd_hook` through `__main__`."""
    src = inspect.getsource(cli_mod.cmd_hook)
    assert 'if args.stage == "post_tool_use"' in src
    assert Path(inspect.getsourcefile(cli_mod.cmd_hook)).parent == COMMANDS_DIR


def test_closest_command_still_works_over_the_live_surface():
    assert cli_mod._closest_command("stat") == "state"
    assert cli_mod._closest_command("lintestr") == "lintester"
    assert cli_mod._closest_command("zzzzzzz") is None


# --- 3. the ratchet, the other way ---------------------------------------------------

def test_main_is_a_shim_under_ninety_lines():
    counted = review._line_count(str(SRC / "__main__.py"))
    assert counted > 0, "the count went vacuous"
    assert counted < MAIN_CAP, f"__main__.py is {counted} lines; the cap is {MAIN_CAP}"


def test_main_defines_no_handler():
    tree = ast.parse((SRC / "__main__.py").read_text(encoding="utf-8"))
    defs = sorted(n.name for n in tree.body
                  if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)))
    assert defs == ["_run", "main"], defs
    # `cmd_x = lambda args: 0` is a handler too, and a `def` walk cannot see
    # it: the only name the shim may bind by assignment is the alias.
    assigned = sorted(t.id for n in tree.body if isinstance(n, ast.Assign)
                      for t in n.targets if isinstance(t, ast.Name))
    assigned += sorted(n.target.id for n in tree.body
                       if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name))
    assert assigned == ["build_cli_parser"], assigned
    # And by identity, whatever the syntax: nothing named `cmd_*` is born here.
    homegrown = [n for n in dir(cli_mod) if n.startswith("cmd_")
                 and getattr(getattr(cli_mod, n), "__module__", None)
                 == "mindhive.__main__"]
    assert homegrown == [], homegrown


def test_every_commands_module_is_under_the_target():
    files = sorted(COMMANDS_DIR.rglob("*.py"))
    assert len(files) >= 12, "the commands package is nearly empty"
    over = {str(p.relative_to(SRC)).replace("\\", "/"): review._line_count(str(p))
            for p in files if review._line_count(str(p)) > MODULE_CAP}
    assert not over, f"over the {MODULE_CAP}-line target: {over}"


def test_every_commands_module_starts_with_spdx_and_a_docstring():
    for p in sorted(COMMANDS_DIR.rglob("*.py")):
        text = p.read_text(encoding="utf-8")
        assert text.splitlines()[0] == SPDX, p
        assert ast.get_docstring(ast.parse(text)), f"{p} has no module docstring"


def test_every_handler_module_lists_all_its_top_level_names():
    """`__all__` is the re-export contract; a handler left out of it is a
    handler `__main__` cannot see and a test cannot monkeypatch."""
    for name in commands.HANDLER_MODULES:
        path = COMMANDS_DIR / f"{name}.py"
        tree = ast.parse(path.read_text(encoding="utf-8"))
        defined = []          # a LIST: a name defined twice shadows the first
        for n in tree.body:
            if isinstance(n, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
                defined.append(n.name)
            elif isinstance(n, ast.Assign):
                for t in n.targets:
                    if isinstance(t, ast.Name) and t.id != "__all__":
                        defined.append(t.id)
        twice = sorted({d for d in defined if defined.count(d) > 1})
        assert not twice, (name, "defined more than once; the last one wins", twice)
        mod = __import__(f"mindhive.commands.{name}", fromlist=["__all__"])
        assert len(mod.__all__) == len(set(mod.__all__)), (name, "duplicate in __all__")
        assert set(mod.__all__) == set(defined), (name, set(mod.__all__) ^ set(defined))


def test_the_parser_lives_in_the_parsers_package():
    where = Path(inspect.getsourcefile(cli_mod._build_parser))
    assert where.parent == COMMANDS_DIR / "parsers"
    assert where.name == "__init__.py"


def test_the_package_imports_cold_without_a_cycle():
    """A cross-module `from .x import y` between two handler modules is a
    cycle waiting for the wrong import order; a cold interpreter with
    warnings-as-errors is the only honest check."""
    env = dict(os.environ)
    env["PYTHONPATH"] = str(REPO / "src") + os.pathsep + env.get("PYTHONPATH", "")
    env["PYTHONDONTWRITEBYTECODE"] = "1"
    out = subprocess.run(
        [sys.executable, "-W", "error", "-c",
         "import mindhive.commands.parsers, mindhive.__main__ as m; "
         "print(m.__file__)"],
        capture_output=True, text=True, env=env, cwd=str(REPO))
    assert out.returncode == 0, out.stderr
    assert Path(out.stdout.strip()).resolve() == (SRC / "__main__.py").resolve()
