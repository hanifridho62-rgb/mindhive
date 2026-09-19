# SPDX-License-Identifier: Apache-2.0
# src/mindhive/core/__init__.py
"""`mindhive.core`: the stdlib-only domain layer of the hexagonal split
(MEB §1.3). Deliberately EMPTY of imports: `mindhive.core.models` is on
the append path of every hook, and a package `__init__` that loaded the
locks or the dreaming pass would drag them onto that path. Import the
submodule you need by name.

One name is served lazily (blueprint K6): `WorkSessionStep`, which blueprint
section 8.4 defines in `worksessions.py`. PEP 562 `__getattr__` resolves it on
first access, so `import mindhive.core` still loads nothing but the package.
"""


def __getattr__(name):
    if name == "WorkSessionStep":
        from ..worksessions import WorkSessionStep
        return WorkSessionStep
    raise AttributeError(f"module 'mindhive.core' has no attribute {name!r}")
