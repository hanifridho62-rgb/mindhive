# Contributing to MIND HIVE

Thanks for working on MIND HIVE. This file is the short version of the rules the
test suite already enforces, so a first pull request does not have to discover
them one red test at a time.

## License of contributions

MIND HIVE is licensed under the [Apache License, Version 2.0](LICENSE)
(`Apache-2.0`). Unless you state otherwise, any contribution you intentionally
submit for inclusion is licensed under the same terms, without any additional
terms or conditions (Apache-2.0, section 5).

Third-party code keeps its own license. `skills/caveman/` is vendored from
JuliusBrussee/caveman under the MIT License — see
[skills/caveman/LICENSE](skills/caveman/LICENSE). Anything else you vendor must
arrive with its license file beside it and a line in the README's License
section.

## Every source file starts with the SPDX identifier

The first line of every source file is the license identifier:

```python
# SPDX-License-Identifier: Apache-2.0
```

- Python, shell and PowerShell use `# SPDX-License-Identifier: Apache-2.0`;
  JavaScript uses `// SPDX-License-Identifier: Apache-2.0`; CSS uses
  `/* SPDX-License-Identifier: Apache-2.0 */`.
- A shebang (`#!/bin/sh`) stays on line 1 and the identifier goes on line 2;
  a Python encoding declaration keeps its line the same way.
- In `src/mindhive/`, a new module follows the identifier with its docstring
  and then `__all__`.

`tests/test_license.py` walks `src/mindhive/`, `hooks/`, `scripts/`, `build/`
and the root `*.ps1` files and fails on any file without it. Two kinds of file
are left alone on purpose:

- **`src/mindhive/pgmigrations/*.sql`.** `apply_migrations` records the sha256
  of every applied migration and refuses to continue when one changes, so a
  header added after the fact stops every mirror that already ran the file. A
  schema change is always a new migration file.
- **Markdown skills and commands, and JSON.** Their first line belongs to YAML
  frontmatter or to a format with no comments.

## Setting up

Python 3.11 or newer (the project is developed on 3.14; the release matrix runs
3.11, 3.12 and 3.14 on Windows and Ubuntu).

```sh
python -m venv .venv
python -m pip install -e .[dev]
```

Runtime `dependencies` is empty on purpose — `hive`, the dashboard, the hooks
and the default test tier work with the standard library alone. Optional
features sit behind extras (`vectors`, `pg`, `mcp`, `tui`, `ui`, `browser`,
`build`); each is explained in `pyproject.toml`.

## Tests come first

Write the failing test, watch it fail for the reason you expect, then write the
code. A change that lands without a test that could have caught it is not done.

```sh
python -m pytest -q
```

- The default run deselects the `node`, `browser` and `pg` markers; run those
  tiers explicitly with `-m node`, `-m browser` or `-m pg` when you touch them.
- Every test runs against a throwaway store: the autouse `isolate_store`
  fixture in `tests/conftest.py` points `MINDHIVE_HOME` at a temporary
  directory. Never build a path to the real `~/.mindhive` in a test.
- From a linked git worktree, set `PYTHONPATH=<worktree>/src` so subprocesses
  import the tree under test rather than the editable install.
- `hive ci run` is the daily gate: the repo's own guards, the suite, the `pg`
  tier when a server is configured, and the hash chain, in a worktree of its
  own. `hive ci hook-install` puts it on `pre-push`.

## Record every code change

Any change under `src/mindhive/` or `tests/` gets a change-log entry before it
is called done:

```sh
hive log <category> "<what changed and why>"
```

The category is one of `update`, `minor_change`, `deletion`, `reduction`,
`major_change` or `replacement`. The entries are `code_change` events in the
ledger and appear in the dashboard's **Log** section; `session_end` flags
unlogged source files until the entry exists.

## Guards the suite enforces

- **File size.** No module under `src/mindhive/` may exceed 1000 lines
  (`tests/test_file_size_cap.py`). The few modules already over it are listed
  with their exact counts, and a listed module may not grow — raising a
  recorded count needs the arithmetic written beside it.
- **Process spawns.** Every `subprocess` call site in `src/` is counted in
  `tests/test_advice_authorship.py`. A new one is registered there with the
  reason it exists.
- **CLI wiring.** Every `--session` / `--project` option carries its validator
  (`tests/test_cli_arg_wiring.py`), and every top-level command appears in the
  README as `hive <command>` (`tests/test_docs_parity.py`).
- **Hooks fail open and stay opt-in.** A hook runs in every Claude Code session
  on the machine it is registered on. Any failure inside one must end in
  silence and exit 0, and anything that changes behaviour is switched on by an
  environment variable, never by default.
- **The ledger is append-only.** Never edit `events/*.jsonl` by hand;
  corrections are new events written through `hive append` or
  `events.append_event`.

## Line endings and shells

The repository is developed on Windows with `core.autocrlf=true`.
`.gitattributes` pins `*.sh` to LF so the POSIX hook shims keep a working
shebang on every checkout. PowerShell hooks target Windows PowerShell 5.1,
which has no `&&`, `||` or ternary operator and mangles inline JSON arguments —
pass JSON through stdin instead.

## Commits and pull requests

- Commit subjects follow `type(scope): summary` — for example
  `feat(orchestrator): …`, `fix(dashboard): …`, `docs(todos): …`.
- Open a pull request against `master`, keep it to one concern, and say in the
  description how you verified it — the command you ran and what it printed.
- Notable changes go in [CHANGELOG.md](CHANGELOG.md) under `[Unreleased]`.
