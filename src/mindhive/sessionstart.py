# SPDX-License-Identifier: Apache-2.0
# src/mindhive/sessionstart.py
"""What the session-start hook prints into every new session, and the budget
it has to fit in.

`hooks/session_start.ps1` and `.sh` print three things: the operating
directives, the state, and -- only when the plugin Claude Code loads has
drifted from this checkout -- one plugin-cache line. Claude Code delivers hook
output whole only while it is small. Measured 2026-10-03 over the last 400
transcripts: output of at most 5,973 characters always arrived whole
(n = 21,600), and output of 10,979 or more was always persisted to a file the
session sees a 2 KB preview of (n = 243). The full directives plus the full
STATE.md were 38 KB, so 336 of 336 session starts delivered the first 2 KB of
the directives and none of the state.

So the hook prints brief forms, and each part has a share of
`SESSION_START_CAP` that something enforces:

    directives   `directives.BRIEF`, a constant     <= DIRECTIVES_BRIEF_CAP (test)
    state        STATE_BRIEF.md, by the projector    <= STATE_BRIEF_CAP, cut on
                                                        write and again on print
    cache line   `plugin_cache.session_notice`        <= CACHE_NOTICE_CHARS

`tests/test_sessionstart.py` locks the sum of the shares under the cap, and
`hive context --check` fails when what is on disk would not keep to them. The
full texts stay one command away: `hive directives`, `hive state`.

Sizes are DELIVERED characters (`delivered_chars`): what `print` emits, with
every newline counted twice, because the PowerShell shim may re-emit each line
with CRLF and the cap is the owner's ceiling, not an estimate."""
from . import directives, paths

SESSION_START_CAP = 6000
DIRECTIVES_BRIEF_CAP = 2900
STATE_BRIEF_CAP = 2300
# A length, not a delivered size: the notice is one line. Its delivered size,
# `delivered_chars` of a line this long, is what the budget reserves for it.
CACHE_NOTICE_CHARS = 700

STATE_BRIEF_FILE = "STATE_BRIEF.md"
CUT_MARKER = "… cut to fit the session-start budget — full text: `hive state`"
# What `hive state` has always printed with nothing projected yet.
NO_STATE = "(no STATE.md — run: hive project)"

_NAMES = {"directives": "directives brief", "state": STATE_BRIEF_FILE,
          "cache_notice": "plugin-cache line"}


def delivered_chars(text: str) -> int:
    """Characters *text* costs when the hook prints it: `print` adds one
    newline, and each newline may arrive as CRLF."""
    printed = f"{text}\n"
    return len(printed) + printed.count("\n")


def cut(text: str, cap: int, marker: str = CUT_MARKER) -> str:
    """*text* unchanged when it fits *cap* delivered characters. Otherwise the
    longest prefix of whole lines that fits with *marker* as its last line --
    the top of every text cut here is the part that matters most -- and a hard
    cut only when the first line alone is over."""
    if delivered_chars(text) <= cap:
        return text
    room = cap - delivered_chars(marker)
    kept, used = [], 0
    for line in text.split("\n"):
        cost = len(line) + 2                    # the line, its \n, a \r
        if used + cost > room:
            break
        kept.append(line)
        used += cost
    if not kept:
        kept = [text.split("\n", 1)[0][:max(0, room - 2)]]
    return "\n".join(kept + [marker])


def _read(path):
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def state_brief(project=None) -> str:
    """What `hive state --brief` prints: STATE_BRIEF.md as the projector wrote
    it, else -- before the first rebuild that writes one -- the head of
    STATE.md. Cut to STATE_BRIEF_CAP here as well, so a file an older build or
    a hand wrote cannot carry the hook past its cap."""
    pdir = paths.project_dir(project)
    text = _read(pdir / STATE_BRIEF_FILE)
    if text is None:
        text = _read(pdir / "STATE.md")
    if text is None:
        return NO_STATE
    return cut(text.rstrip("\n"), STATE_BRIEF_CAP)


def measure(project=None) -> dict:
    """The hook's output, part by part, in delivered characters, beside each
    part's share. The state part is STATE_BRIEF.md as WRITTEN, before
    `state_brief` cuts it, because what this answers is whether the writer kept
    to its share; the cache line is counted at its share, since it prints only
    on drift and never past it."""
    raw = _read(paths.project_dir(project) / STATE_BRIEF_FILE)
    state = raw.rstrip("\n") if raw is not None else state_brief(project)
    parts = {"directives": delivered_chars(directives.directives(brief=True)),
             "state": delivered_chars(state),
             "cache_notice": delivered_chars("x" * CACHE_NOTICE_CHARS)}
    shares = {"directives": DIRECTIVES_BRIEF_CAP, "state": STATE_BRIEF_CAP,
              "cache_notice": parts["cache_notice"]}
    total = sum(parts.values())
    return {"cap": SESSION_START_CAP, "parts": parts, "shares": shares,
            "total": total, "over": max(0, total - SESSION_START_CAP),
            "over_share": [k for k in parts if parts[k] > shares[k]]}


def findings(project=None) -> list:
    """`hive context`'s rule for this budget, as the fields `context.Finding`
    takes: one HIGH finding when a part is over its share, else none. High,
    like `memory_truncated`, because the loss is real either way -- a part over
    its share loses its tail at print, and output over the cap reaches the
    session as a 2 KB preview -- and `hive context --check` fails on it."""
    m = measure(project)
    if not m["over_share"]:
        return []
    over = "; ".join(f"{_NAMES[k]} {m['parts'][k]:,} (share {m['shares'][k]:,})"
                     for k in m["over_share"])
    return [{
        "rule": "session_start_over_cap", "severity": "high",
        "subject": "session start",
        "message": (f"the session-start hook output would be {m['total']:,} "
                    f"delivered characters against a cap of {m['cap']:,}; over "
                    f"its share: {over}. A part over its share loses its tail "
                    f"at print, and output over the cap reaches the session as "
                    f"a 2 KB preview of a file. Rebuild with `hive project`; a "
                    f"text that is over needs trimming, or the shares in "
                    f"`mindhive.sessionstart` rebalancing."),
    }]
