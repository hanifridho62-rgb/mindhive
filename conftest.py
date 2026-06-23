# conftest.py — ensure tests import the mindhive package from THIS checkout's
# src/, not a globally pip-installed editable copy that may point at a different
# worktree/clone. Inserting at the front of sys.path makes the local source win
# regardless of any .pth-based editable install.
import os
import sys

_SRC = os.path.join(os.path.dirname(os.path.abspath(__file__)), "src")
if _SRC not in sys.path:
    sys.path.insert(0, _SRC)
