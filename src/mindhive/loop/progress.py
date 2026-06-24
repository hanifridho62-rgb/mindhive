# src/mindhive/loop/progress.py
from pathlib import Path

_IGNORE = {".git", ".hive-loop", "__pycache__", ".venv", "node_modules"}


class MtimeTracker:
    """Cheap, dependency-free progress detector: snapshots the (path, mtime,
    size) of project files and reports whether anything changed since."""

    def __init__(self, root: Path):
        self.root = Path(root)
        self._prev = {}

    def _scan(self):
        state = {}
        for p in self.root.rglob("*"):
            if any(part in _IGNORE for part in p.parts):
                continue
            if p.is_file():
                st = p.stat()
                state[str(p)] = (st.st_mtime, st.st_size)
        return state

    def snapshot(self):
        self._prev = self._scan()

    def changed(self) -> bool:
        current = self._scan()
        changed = current != self._prev
        self._prev = current
        return changed
