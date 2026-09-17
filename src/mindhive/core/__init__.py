# SPDX-License-Identifier: Apache-2.0
# src/mindhive/core/__init__.py
"""`mindhive.core`: the stdlib-only domain layer of the hexagonal split
(MEB §1.3). Deliberately EMPTY of imports: `mindhive.core.models` is on
the append path of every hook, and a package `__init__` that loaded the
locks or the dreaming pass would drag them onto that path. Import the
submodule you need by name.
"""
