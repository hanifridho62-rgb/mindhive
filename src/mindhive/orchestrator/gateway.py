# SPDX-License-Identifier: Apache-2.0
"""What a gateway command SAYS (blueprint P1f; MEB ORCH-02).

The grammar half of `interceptor.py`, carved out for the same reason
`breaker.py` -> `quota.py` was: neither file may pass the MEB's 500-line
ceiling, and these two halves answer genuinely different questions.

This one is pure text and has no I/O at all -- no store, no subprocess, no
clock. Given a shell command it answers which of the four gateway acts it runs
(`action_of`), which PR number it names, which ref a `git merge` merges
(`branch_of`), and who a GitHub login is (`merge_actor_of`). `interceptor.py`
is the half that then goes and ASKS git and `gh` what really happened, and
writes the signal.

The dependency runs one way only -- this module imports nothing from
`interceptor` -- so either may be imported first, and `interceptor` re-exports
every name here BY IDENTITY (`interceptor.merge_actor_of is
gateway.merge_actor_of`), the carve-out convention this repo already uses for
`events` -> `contracts` / `core.models`.

## Reading a command is not running a shell

`_segments` is the whole of the compromise. A gateway act is rarely the first
thing on the line, so anchoring at character zero missed every `cd /repo && gh
pr merge 7`; but relaxing the anchor altogether would read `echo gh pr merge`
as a merge. So the line is split into the parts that actually run, each still
anchored at its own start, and separators inside quotes do not separate.
Backslash escapes and `$(...)` are left alone: neither can turn a mention into
a run, and a parser that tried would be a shell.
"""
import re

__all__ = ["ACTION_TYPES", "MERGE_ACTORS", "COMMAND_SCAN_CHARS", "action_of",
           "branch_of", "merge_actor_of"]

# The four git-gateway acts worth a signal. Everything else a Bash tool call
# might be is not a gateway event and costs nothing to ignore.
ACTION_TYPES = ("pr_merge", "pr_close", "pr_create", "git_merge")

# Who landed it. "" and "unknown" are both honest answers -- the first when no
# probe ran, the second when one ran and could not tell -- and neither may be
# quietly rounded to "human_manual", which is a claim about a person.
MERGE_ACTORS = ("claude_code", "ci_bot", "human_manual", "unknown", "")

# Everything that is not a letter or a digit is a word boundary in a login:
# `claude-code[bot]` is three words, `mclaude` is one.
_WORD_SPLIT = re.compile(r"[^a-z0-9]+")

# `(?![\w-])` after the verb, never `\b`. A word boundary separates `git merge
# lane` from `git merged-files` -- the boundary fails between `e` and `d` -- but
# it SUCCEEDS between `e` and `-`, so `\b` also matched `git merge-base`,
# `git merge-tree` and `git merge-file`. All three are read-only queries;
# `_branch_of` would have handed `_is_ancestor` their last token (`HEAD`, which
# is trivially its own ancestor) and the interceptor would have appended
# `merged: true` to an append-only ledger for a command that merged nothing.
# The lookahead excludes the hyphen as well as the word characters.
#
# The optional leading `<path>/` admits `/usr/bin/gh pr merge 7`; the rest of
# the anchoring is `_segments`' job.
_PROGRAM = r"(?:[\w./\\+-]*[/\\])?"
_GH_PR = re.compile(_PROGRAM + r"gh\s+pr\s+(merge|close|create)(?![\w-])",
                    re.IGNORECASE)
_GIT_MERGE = re.compile(_PROGRAM + r"git\s+merge(?![\w-])", re.IGNORECASE)
_NUMBER = re.compile(r"\b(\d+)\b")
# A shell line is not one command. `&&`, `||`, `;` and a newline separate the
# parts that actually RUN, and a leading `env` or `KEY=value` belongs to the
# part that follows it rather than to the line.
_SEPARATORS = ("&&", "||")
_ENV_PREFIX = re.compile(r"^(?:env\s+|[A-Za-z_]\w*=\S*\s+)+")
# The scan is bounded for the same reason `PHASE_SCAN_CHARS` is: this runs on
# EVERY Bash tool call, and a command can be a megabyte-long heredoc. A gateway
# act hiding past two thousand characters of one compound line is not a case
# worth paying for on every call.
COMMAND_SCAN_CHARS = 2000

def _segments(command):
    """The runnable parts of *command*, each stripped of its environment prefix.

    A gateway act is rarely the first thing on the line. `cd /repo && gh pr
    merge 7` is how one is actually run from a hook or a script, and anchoring
    the pattern at character zero made every such run invisible: no intent, no
    signal, and the event this module exists to observe simply never happened.

    Each SEGMENT is still anchored at its own start, which is the property the
    original anchor was chosen for and which this keeps: `echo gh pr merge` and
    `git commit -m 'gh pr merge 7'` begin with `echo` and with `git commit`, so
    neither is a run.

    Separators inside QUOTES do not separate. Without that, `echo 'cd /repo &&
    gh pr merge 7'` and `git commit -m 'cd x && gh pr merge 7'` would each
    yield a second segment that reads as a real merge -- and the probe would
    then go and find the PR they merely name, recording a merge that this
    command did not make. Scanning for the quote state is the whole cost of not
    getting that wrong; it is not a shell parser, and it does not try to be:
    backslash escapes and `$(...)` are left alone, because neither can turn a
    mention into a run."""
    text = _text(command)[:COMMAND_SCAN_CHARS]
    parts, buf, quote, i = [], [], "", 0
    while i < len(text):
        ch = text[i]
        if quote:                               # inside '...' or "..."
            buf.append(ch)
            quote = "" if ch == quote else quote
            i += 1
        elif ch in "'\"":
            quote = ch
            buf.append(ch)
            i += 1
        elif text.startswith(_SEPARATORS, i):
            parts.append("".join(buf))
            buf, i = [], i + 2
        elif ch in ";\n":
            parts.append("".join(buf))
            buf, i = [], i + 1
        else:
            buf.append(ch)
            i += 1
    parts.append("".join(buf))
    for part in parts:
        part = _ENV_PREFIX.sub("", part.strip())
        if part:
            yield part

def _text(value) -> str:
    return value if isinstance(value, str) else ""


def _dict(value) -> dict:
    return value if isinstance(value, dict) else {}


def _int(value) -> int:
    """*value* as a non-negative int, or 0 -- `gh` may answer with anything."""
    try:
        return max(0, int(value))
    except (TypeError, ValueError):
        return 0

# --- the actor rule ----------------------------------------------------------------

def merge_actor_of(login) -> str:
    """Who a GitHub login is, to this system.

    ORDER MATTERS. `claude-code[bot]` is both a bot and claude, and it must read
    as claude: telling the agent's own merges from every other automation is the
    whole reason this signal exists.

    Matched on WHOLE WORDS, never on a bare substring. `abbott`, `talbot` and
    `botond` are surnames and `claudel` and `mclaude` are ordinary logins;
    reading any of them as automation writes a claim about the wrong kind of
    actor into an append-only ledger. GitHub's own convention for an App is the
    `[bot]` suffix, so that is what is matched, plus `bot` as a word of its own
    (`ci-bot`, `build_bot`).

    An empty or absent login answers "unknown", NEVER "human_manual". `gh`
    reports `mergedBy: null` for any PR whose merger it will not name -- one
    merged before the field existed, a token without the scope, a merge queue
    -- and `MERGE_ACTORS` above says in as many words that rounding that to a
    person is the one thing this must not do."""
    name = _text(login).strip()
    if not name:
        return "unknown"
    lowered = name.lower()
    words = [w for w in _WORD_SPLIT.split(lowered) if w]
    if "claude" in words:
        return "claude_code"
    if lowered.endswith("[bot]") or "bot" in words:
        return "ci_bot"
    return "human_manual"


# --- the grammar ---------------------------------------------------------------------

def action_of(command) -> tuple:
    """(action_type, pr_number) for a command that RUNS one of the four acts.

    Each segment is anchored at its own start, so a command that merely
    mentions one -- `echo gh pr merge`, `grep -rn 'git merge'` -- is not one,
    while `cd /repo && gh pr merge 7` is.

    A `pr_merge` with no number is not an error: `gh pr merge --squash` merges
    the CURRENT BRANCH's PR, which is the commonest form there is. 0 means
    "none was named", and the interceptor's probe resolves it by asking `gh`
    the same question without one."""
    for segment in _segments(command):
        hit = _GH_PR.match(segment)
        if hit:
            action = f"pr_{hit.group(1)}"
            number = _NUMBER.search(segment[hit.end():])
            return action, int(number.group(1)) if number else 0
        if _GIT_MERGE.match(segment):
            return "git_merge", 0
    return "", 0


def branch_of(command) -> str:
    """The ref a `git merge` names: the last token that is not a flag.

    The LAST, and after the first two tokens. `git merge -m 'msg' lane` names
    `lane`; taking the first non-flag token would name `'msg'`, and keeping
    token 1 would name `merge`. Either way git is asked about a ref that does
    not exist, every `git merge` records as unmerged, and the answer to "who
    landed this" is wrong for the whole non-squash path."""
    for segment in _segments(command):
        if _GIT_MERGE.match(segment):
            refs = [p for p in segment.split()[2:] if not p.startswith("-")]
            return refs[-1] if refs else ""
    return ""
