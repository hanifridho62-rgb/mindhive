# src/mindhive/gateguard.py
"""Pure classifiers behind the static policy tier in enforce.py — the in-process
Python port of ECC's GateGuard hook suite.

Ported from three ECC PreToolUse hooks (``gateguard-fact-force.js``,
``block-no-verify.js``, ``config-protection.js``): each ran as its own
``node -e`` spawn costing ~64 ms cold on this machine — ~1.15 s of pure spawn
overhead per tool call across ECC's 18 pre/post registrations. Running the same
checks in-process inside MindHive's single ``pre_tool_use`` dispatcher costs
nothing extra per call.

Three public checks, all pure functions over the tool call:

``is_destructive_command(cmd)``
    Quote-aware destructive-command classifier: recursive-force ``rm``,
    working-tree-destroying git forms, ``find -exec`` deletion, bulk SQL /
    ``dd`` phrases, and ``Remove-Item -Recurse -Force`` (a Windows addition —
    the live PreToolUse matcher includes the PowerShell tool). The adversarial
    shapes ECC fixed under GHSA-4v57-ph3x-gf55 are covered: quoted command
    words, newline separators, ``$( )``/backtick substitution, subshell
    groups, and ``sh -c`` wrappers.

``check_no_verify(cmd)``
    Git hook-bypass detection: ``--no-verify`` on the six subcommands that
    accept it, commit's ``-n`` shorthand, and ``-c core.hooksPath=``
    overrides — with commit's value-consuming options skipped so a commit
    MESSAGE mentioning the flag never false-positives.

``protected_config_file(tool_name, tool_input)``
    Lint/formatter config protection for file tools: agents weaken configs to
    make checks pass instead of fixing code. First-time creation is allowed;
    stat errors other than "not found" fail closed.

Everything returns a human-readable reason string (truthy) or ``None`` (allow);
the enforce layer wraps these fail-open, so nothing here may touch the store.
"""
from __future__ import annotations

import os
import re

# SQL-keyword + dd patterns: stable phrases matched on quote-stripped text so a
# commit message mentioning "drop table" does not trigger (ECC's tradeoff:
# a QUOTED destructive SQL string passed to a client is not flagged either).
# The dd alternative deliberately has no trailing \b: `dd if=/dev/zero` puts a
# non-word char after the `=`, so a boundary there never matches (an upstream
# ECC regex bug this port fixes rather than reproduces).
_DESTRUCTIVE_SQL_DD = re.compile(
    r"\b(?:drop\s+table|delete\s+from|truncate)\b|\bdd\s+if=", re.IGNORECASE)

_SHELL_WRAPPERS = {"sh", "bash", "zsh", "dash", "ksh"}
_GIT_VALUE_SHORT = {"-c", "-C"}
_GIT_VALUE_LONG = {"--git-dir", "--work-tree", "--namespace", "--super-prefix"}

_NO_VERIFY_COMMANDS = ("commit", "push", "merge", "cherry-pick", "rebase", "am")
_HOOKSPATH_PREFIX = "core.hookspath="

# Commit options that consume the NEXT token as a value (so the value is never
# scanned as a flag), and their inline `--opt=value` forms.
_COMMIT_VALUE_OPTS = {
    "-m", "--message", "-F", "--file", "-C", "--reuse-message",
    "-c", "--reedit-message", "--author", "--date", "--template",
    "--fixup", "--squash", "--pathspec-from-file",
}
_COMMIT_INLINE_VALUE_PREFIXES = (
    "--message=", "--file=", "--reuse-message=", "--reedit-message=",
    "--author=", "--date=", "--template=", "--fixup=", "--squash=",
    "--pathspec-from-file=",
)
# Short options that take a value; inside a combined token (-am, -tn) the
# FIRST such char ends flag scanning — the rest is the value.
_COMMIT_SHORT_VALUE_CHARS = set("mFCct")

# Linter/formatter configs agents weaken to make checks pass. pyproject.toml is
# deliberately absent: it holds project metadata alongside lint config, and
# blocking it would block legitimate dependency changes (ECC's note, kept).
PROTECTED_CONFIG_FILES = frozenset({
    ".eslintrc", ".eslintrc.js", ".eslintrc.cjs", ".eslintrc.json",
    ".eslintrc.yml", ".eslintrc.yaml",
    "eslint.config.js", "eslint.config.mjs", "eslint.config.cjs",
    "eslint.config.ts", "eslint.config.mts", "eslint.config.cts",
    ".prettierrc", ".prettierrc.js", ".prettierrc.cjs", ".prettierrc.json",
    ".prettierrc.yml", ".prettierrc.yaml",
    "prettier.config.js", "prettier.config.cjs", "prettier.config.mjs",
    "biome.json", "biome.jsonc",
    ".ruff.toml", "ruff.toml",
    ".shellcheckrc", ".stylelintrc", ".stylelintrc.json", ".stylelintrc.yml",
    ".markdownlint.json", ".markdownlint.yaml", ".markdownlintrc",
})

_FILE_TOOLS = ("Edit", "Write", "NotebookEdit")


# --------------------------------------------------------------------------
# shared shell-text helpers
# --------------------------------------------------------------------------

def _strip_quoted(text: str) -> str:
    """Collapse single/double-quoted string CONTENTS so phrases inside a
    message or echoed argument cannot trigger the destructive patterns."""
    text = re.sub(r"'(?:[^'\\]|\\.)*'", "''", text)
    return re.sub(r'"(?:[^"\\]|\\.)*"', '""', text)


def _explode_subshells(text: str) -> str:
    """Promote ``$( )`` and backtick delimiters to segment separators so a
    destructive command inside a substitution is scanned like a top-level one.
    Iterated a few times for one layer of nesting per pass."""
    for _ in range(4):
        before = text
        text = re.sub(r"\$\(([^()`]*)\)", r";\1;", text)
        text = re.sub(r"`([^`]*)`", r";\1;", text)
        if text == before:
            break
    return text


def _split_segments(text: str) -> list[str]:
    """Top-level command segments: quotes stripped, substitutions exploded,
    subshell/brace group delimiters promoted to separators (ECC walks group
    bodies via a substitution library; promoting the delimiters is simpler and
    strictly more aggressive — acceptable for an ask/deny gate), split on
    ``;|&`` runs and newlines, per-segment comments removed."""
    flat = _explode_subshells(_strip_quoted(text))
    flat = re.sub(r"[(){}]", ";", flat)
    out = []
    for seg in re.split(r"[;|&\n\r]+", flat):
        seg = re.sub(r"(^|\s)#.*", r"\1", seg).strip()
        if seg:
            out.append(seg)
    return out


def _basename(token: str) -> str:
    """``/usr/bin/git``, ``git.exe``, ``GIT`` -> ``git``."""
    if not token:
        return ""
    token = re.sub(r"^.*[\\/]", "", token)
    return re.sub(r"\.exe$", "", token, flags=re.IGNORECASE).lower()


def _quote_aware_segments(text: str) -> list[list[str]]:
    """Split on UNQUOTED ``;|&``/newlines into dequoted token lists, so a
    quoted command word (``'rm'``) normalizes to ``rm`` and a newline acts as
    a separator — the two classifier bypasses naive quote-stripping missed."""
    segments: list[list[str]] = []
    words: list[str] = []
    current = ""
    has_word = False
    quote = None
    escaped = False

    def flush_word():
        nonlocal current, has_word
        if has_word:
            words.append(current)
        current, has_word = "", False

    def flush_segment():
        nonlocal words
        flush_word()
        if words:
            segments.append(words)
        words = []

    for ch in str(text or ""):
        if escaped:
            current += ch
            has_word = True
            escaped = False
            continue
        if ch == "\\":
            escaped = True
            has_word = True
            continue
        if quote:
            if ch == quote:
                quote = None
            else:
                current += ch
            has_word = True
            continue
        if ch in "\"'":
            quote = ch
            has_word = True     # entering a quote starts a word even if empty
            continue
        if ch in ";|&\n\r":
            flush_segment()
            continue
        if ch.isspace():
            flush_word()
            continue
        current += ch
        has_word = True
    flush_segment()
    return segments


# --------------------------------------------------------------------------
# destructive-command classifier
# --------------------------------------------------------------------------

def _is_destructive_rm(tokens: list[str]) -> bool:
    """``rm`` with BOTH recursive and force, in combined/split/long forms."""
    if not tokens or _basename(tokens[0]) != "rm":
        return False
    has_r = has_f = False
    for t in tokens[1:]:
        if t == "--recursive":
            has_r = True
            continue
        if t == "--force":
            has_f = True
            continue
        if not t.startswith("-") or t.startswith("--"):
            continue
        body = t[1:]
        if re.search(r"[rR]", body):
            has_r = True
        if "f" in body:
            has_f = True
    return has_r and has_f


def _find_git_subcommand(tokens: list[str]):
    """(subcommand, rest) after skipping git's value-consuming global options,
    or None when the tokens are not a git invocation."""
    if not tokens or _basename(tokens[0]) != "git":
        return None
    i = 1
    while i < len(tokens):
        t = tokens[i]
        if t in _GIT_VALUE_SHORT or t in _GIT_VALUE_LONG:
            i += 2
            continue
        if any(t.startswith(opt + "=") for opt in _GIT_VALUE_LONG):
            i += 1
            continue
        if t.startswith("-"):
            i += 1              # unknown global option, no value consumed
            continue
        return t.lower(), tokens[i + 1:]
    return None


def _short_flag_has(token: str, chars: str) -> bool:
    """True when a combined short-option token (``-fd``) contains any of
    ``chars``. Long options and non-flags never match."""
    if not token.startswith("-") or token.startswith("--"):
        return False
    return any(c in token[1:] for c in chars)


def _is_destructive_git(tokens: list[str]) -> str | None:
    sub = _find_git_subcommand(tokens)
    if not sub:
        return None
    command, rest = sub

    if command == "reset":
        if "--hard" in rest:
            return "`git reset --hard` discards uncommitted work"
    elif command == "checkout":
        for t in rest:
            if t in ("--", ".", "--force") or _short_flag_has(t, "f"):
                return "`git checkout` in a form that overwrites working-tree changes"
    elif command == "clean":
        for t in rest:
            if t == "--force" or _short_flag_has(t, "f"):
                return "`git clean -f` deletes untracked files"
    elif command == "push":
        with_lease = bare_force = plus_refspec = False
        for t in rest:
            if t == "--force-with-lease" or t.startswith("--force-with-lease="):
                with_lease = True
            elif t == "--force" or t.startswith("--force="):
                bare_force = True
            elif _short_flag_has(t, "f"):
                bare_force = True
            elif (t.startswith("+") and len(t) > 1
                  and re.match(r"^\+(?:[a-zA-Z_/.:]|HEAD)", t)):
                plus_refspec = True
        if bare_force or (plus_refspec and not with_lease):
            return "`git push --force`/`+refspec` rewrites remote history"
    elif command == "commit":
        if "--amend" in rest:
            return "`git commit --amend` rewrites the last commit"
    elif command == "rm":
        for t in rest:
            if _short_flag_has(t, "rR"):
                return "`git rm -r` removes tracked trees"
    elif command == "switch":
        for t in rest:
            if t in ("--discard-changes", "--force") or _short_flag_has(t, "fC"):
                return "`git switch` in a form that discards local changes"
    return None


def _is_destructive_find_exec(segment: str) -> bool:
    """``find … -exec rm|rmdir|unlink|git reset --hard`` up to the ``;``/``+``
    terminator. ``rm`` with ANY flags (or none) inside -exec is destructive."""
    tokens = str(segment or "").split()
    if not tokens or _basename(tokens[0]) != "find":
        return False
    try:
        idx = tokens.index("-exec")
    except ValueError:
        return False
    exec_tokens = []
    for t in tokens[idx + 1:]:
        if t in (";", "\\;", "+"):
            break
        exec_tokens.append(t)
    if not exec_tokens:
        return False
    base = _basename(exec_tokens[0])
    if base in ("rm", "rmdir", "unlink"):
        return True
    if base == "git":
        sub = _find_git_subcommand(exec_tokens)
        return bool(sub and sub[0] == "reset" and "--hard" in sub[1])
    return False


def _is_destructive_ps_remove(tokens: list[str]) -> bool:
    """``Remove-Item``/``ri`` with -Recurse AND -Force (any order). A Windows
    addition to the faithful port — the live PreToolUse matcher includes the
    PowerShell tool and this is rm -rf's native spelling here."""
    if not tokens or _basename(tokens[0]) not in ("remove-item", "ri"):
        return False
    low = [t.lower() for t in tokens[1:]]
    has_r = any(t.startswith("-recurse") for t in low)
    has_f = any(t.startswith("-force") for t in low)
    return has_r and has_f


def _destructive_in_tokens(tokens: list[str]) -> str | None:
    if _is_destructive_rm(tokens):
        return "`rm` with recursive force flags deletes trees irrecoverably"
    reason = _is_destructive_git(tokens)
    if reason:
        return reason
    if _is_destructive_ps_remove(tokens):
        return "`Remove-Item -Recurse -Force` deletes trees irrecoverably"
    return None


def _destructive_quote_aware(raw: str, depth: int = 0) -> str | None:
    """Quote-aware pass: closes the quoted-command-word, newline-separator,
    quoted-find-exec, and ``sh -c`` wrapper bypasses."""
    if depth > 4:
        return None
    for tokens in _quote_aware_segments(raw):
        if not tokens:
            continue
        reason = _destructive_in_tokens(tokens)
        if reason:
            return reason
        if _is_destructive_find_exec(" ".join(tokens)):
            return "`find -exec` deleting files"
        if _basename(tokens[0]) in _SHELL_WRAPPERS and "-c" in tokens:
            ci = tokens.index("-c")
            if ci + 1 < len(tokens):
                reason = _destructive_quote_aware(tokens[ci + 1], depth + 1)
                if reason:
                    return reason
    return None


def is_destructive_command(cmd) -> str | None:
    """Reason string when ``cmd`` contains a destructive action, else None."""
    raw = str(cmd or "")
    if not raw.strip():
        return None
    flat = _explode_subshells(_strip_quoted(raw))
    if _DESTRUCTIVE_SQL_DD.search(flat):
        return ("a bulk-destructive data phrase "
                "(DROP TABLE / DELETE FROM / TRUNCATE / dd if=)")
    # find -exec on raw text: quote-stripping would erase a quoted binary name
    # (`-exec 'rm'` -> `-exec ''`), so raw segments are checked first.
    for raw_seg in re.split(r"[;|&\n\r]+", raw):
        if _is_destructive_find_exec(raw_seg.strip()):
            return "`find -exec` deleting files"
    for seg in _split_segments(raw):
        reason = _destructive_in_tokens(seg.split())
        if reason:
            return reason
    return _destructive_quote_aware(raw)


# --------------------------------------------------------------------------
# --no-verify / hooksPath bypass
# --------------------------------------------------------------------------

def _commit_short_value_opt(token: str):
    """For a combined short token: (consumes_next, has_inline) per the FIRST
    value-taking char — git treats the rest of the token as that option's
    value, so scanning must stop there."""
    if not token.startswith("-") or token.startswith("--") or token == "-":
        return None
    body = token[1:]
    for i, c in enumerate(body):
        if c in _COMMIT_SHORT_VALUE_CHARS:
            return (i == len(body) - 1, i < len(body) - 1)
    return None


def _is_commit_no_verify_short(token: str) -> bool:
    return token == "-n" or bool(re.match(r"^-n[a-zA-Z]", token))


def _segment_no_verify(tokens: list[str]) -> str | None:
    """One dequoted segment: find git, skip global options (flagging a
    ``core.hooksPath`` override on the way), then scan the subcommand's own
    flags for ``--no-verify`` / commit's ``-n``."""
    gi = next((i for i, t in enumerate(tokens) if _basename(t) == "git"), None)
    if gi is None:
        return None

    hookspath = False
    i = gi + 1
    command = None
    while i < len(tokens):
        t = tokens[i]
        if t == "-c":
            nxt = tokens[i + 1] if i + 1 < len(tokens) else ""
            if nxt.lower().startswith(_HOOKSPATH_PREFIX):
                hookspath = True
            i += 2
            continue
        if t.lower().startswith("-c" + _HOOKSPATH_PREFIX):
            hookspath = True
            i += 1
            continue
        if t == "-C" or t in _GIT_VALUE_LONG:
            i += 2
            continue
        if any(t.startswith(opt + "=") for opt in _GIT_VALUE_LONG):
            i += 1
            continue
        if t.startswith("-"):
            i += 1
            continue
        command = t.lower()
        break

    if command not in _NO_VERIFY_COMMANDS:
        return None
    if hookspath:
        return (f"overriding core.hooksPath with `git {command}` bypasses the "
                f"repo's git hooks")

    rest = tokens[i + 1:]
    skip_next = False
    for t in rest:
        if skip_next:
            skip_next = False
            continue
        if t == "--":
            break
        if command == "commit" and not _is_commit_no_verify_short(t):
            if t in _COMMIT_VALUE_OPTS:
                skip_next = True
                continue
            if t.startswith(_COMMIT_INLINE_VALUE_PREFIXES):
                continue
            short = _commit_short_value_opt(t)
            if short:
                consumes_next, _has_inline = short
                if consumes_next:
                    skip_next = True
                continue
        if t == "--no-verify":
            return f"`--no-verify` on `git {command}` skips the repo's git hooks"
        if command == "commit" and _is_commit_no_verify_short(t):
            return "`-n` on `git commit` skips the repo's git hooks (--no-verify)"
    return None


def check_no_verify(cmd) -> str | None:
    """Reason string when ``cmd`` bypasses git hooks, else None."""
    raw = str(cmd or "")
    if not raw.strip():
        return None
    for tokens in _quote_aware_segments(raw):
        reason = _segment_no_verify(tokens)
        if reason:
            return reason
    return None


# --------------------------------------------------------------------------
# config protection
# --------------------------------------------------------------------------

def protected_config_file(tool_name, tool_input) -> str | None:
    """Reason string when a file tool targets an EXISTING protected lint or
    formatter config, else None. First-time creation is a legitimate bootstrap
    and allowed; a stat error other than not-found leaves the guard in place
    (fail closed — a permissions error must not silently weaken it)."""
    if tool_name not in _FILE_TOOLS:
        return None
    path = (tool_input or {}).get("file_path") or ""
    if not isinstance(path, str) or not path:
        return None
    base = re.split(r"[\\/]", path)[-1]
    if base not in PROTECTED_CONFIG_FILES:
        return None
    try:
        os.lstat(path)
    except FileNotFoundError:
        return None
    except OSError:
        pass                     # fail closed: treat as existing
    return (f"modifying {base} weakens the lint/format gate — fix the source "
            f"to satisfy the rules instead. If the config change itself is "
            f"the task, disable policy:gate:config-protection in "
            f"rules/enforce.json first")
