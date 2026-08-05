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
                # nanosecond mtime avoids float-seconds rounding; size catches
                # changes the coarse OS last-write-time tick might otherwise miss.
                state[str(p)] = (st.st_mtime_ns, st.st_size)
        return state

    def snapshot(self):
        self._prev = self._scan()

    def changed(self) -> bool:
        current = self._scan()
        changed = current != self._prev
        self._prev = current
        return changed

    def changed_paths(self) -> list:
        """Which files differ from the snapshot, without consuming it.

        `changed()` answers a yes/no question and ADVANCES the snapshot, which is
        exactly right for the engine's progress detection and useless to anything
        that wants to know what moved. Kept separate rather than widening
        `changed()`'s return, because the engine's contract is a bool and a caller
        that only needs the bool must not start paying for a list.

        Non-destructive on purpose: the engine owns when the snapshot advances, and
        a reader that silently consumed it would make the next `changed()` report
        no progress on a run that had made some."""
        current = self._scan()
        moved = [path for path, stamp in current.items()
                 if self._prev.get(path) != stamp]
        moved += [path for path in self._prev if path not in current]
        return sorted(moved)
