"""The lenses: one question about the record each.

Split from `replenish.py`, which owns the DECISION layer — the gate, the dedup,
the bounds and the audit rows. A lens owns a question and its evidence, and the
two concerns grow at different rates: the core is bounded by its contract, while
lenses accumulate one per thing the record can prove. Keeping them together took
the module past 1,500 lines, which is the size its own `debt` lens flags.

Every lens here is a plain fold: no model, no subprocess, no network. Each takes
the event list and returns `Candidate`s reporting what it OBSERVED — recurrence,
severity, whether a prevention rule already covers it — never a score. The core
computes that, so no lens can promote its own finds past the gate.

Imported for its side effect: each lens registers itself in `replenish.LENSES` at
the bottom of its section. `replenish.py` imports this module on its own last
line, so `from mindhive import replenish` still yields a populated registry and
no call site had to move.
"""

from .replenish import (LENSES, MAX_EVIDENCE_CHARS, MAX_EVIDENCE_ITEMS,
                        MAX_TITLE_CHARS, Candidate, _count, fingerprint)

# Each lens answers ONE question about the record and returns candidates. They
# are plain folds: no model, no subprocess, no network. A lens that needs a model
# is a later phase and runs through the agent seam, not here.

# How many changed files ride the evidence list. Enough to recognise the
# change-set, bounded so one enormous commit cannot blow the row.
_EVIDENCE_FILES = 6


def _passing_verification_ts(evs) -> str:
    """Timestamp of the newest verification whose SUBPROCESS SUCCEEDED, or "".

    The exit code is the observed fact — `verify.py` is explicit that the
    `verified_by` stamp attaches to it and not to the counts, because an
    unparseable summary records counts absent rather than zero. So a run with a
    zero exit code and no parsed counts is a pass here, and a run with counts but
    a non-zero exit code is not."""
    newest = ""
    for ev in evs or []:
        if getattr(ev, "type", None) != "verification":
            continue
        p = getattr(ev, "payload", None)
        if not isinstance(p, dict):
            continue
        code = p.get("exit_code")
        if isinstance(code, bool) or not isinstance(code, int) or code != 0:
            continue
        ts = ev.ts or ""
        if ts > newest:
            newest = ts
    return newest


def evidence_gap(evs) -> list:
    """Code changed and nothing has verified it since.

    ONE candidate, not one per file. The outstanding fact is singular — "the
    suite has not run against the current tree" — and emitting a step per changed
    file would bury the plan and blow `roadmap.MAX_STEPS` on a busy week.

    The fingerprint keys on the newest covered `code_change` timestamp, which is
    the CONDITION INSTANCE: once the suite runs the condition clears, and a later
    change mints a fresh instance so the lens may fire again. Keying on the bare
    lens name would let it fire exactly once in the life of the project.

    `severity` is set because this project treats it as blocking: CLAUDE.md's
    "any change here must land green" and SOP `change-audit` §3 both say the
    suite passing is a precondition for calling work done, not a nicety. The
    flag is that recorded stance, not this lens's opinion of itself."""
    newest_ts, newest_payload, count = "", None, 0
    verified_ts = _passing_verification_ts(evs)
    for ev in evs or []:
        if getattr(ev, "type", None) != "code_change":
            continue
        p = getattr(ev, "payload", None)
        if not isinstance(p, dict):
            continue
        ts = ev.ts or ""
        if ts > verified_ts:
            count += 1
        if ts > newest_ts:
            newest_ts, newest_payload = ts, p
    if not newest_ts or newest_ts <= verified_ts:
        return []

    files = [str(f) for f in (newest_payload.get("files") or [])
             if isinstance(f, str)]
    plural = "" if count == 1 else "s"
    title = (f"Run the test suite: {count} code change{plural} since the last "
             f"passing verification")
    evidence = [
        f"newest code_change @ {newest_ts}",
        (f"last passing verification @ {verified_ts}" if verified_ts
         else "no passing verification has ever been recorded"),
    ]
    evidence += [f"changed: {f}" for f in files[:_EVIDENCE_FILES]]
    return [Candidate(lens="evidence-gap", title=title[:MAX_TITLE_CHARS],
                      fingerprint=fingerprint("evidence-gap", newest_ts),
                      evidence=tuple(evidence),
                      recurrence=count, severity=True)]


LENSES["evidence-gap"] = evidence_gap


# --- lens: repair -------------------------------------------------------------

# Incident statuses that mean the recurrence threshold was crossed. `candidate`
# has not crossed it yet, and `expired` is already excluded by
# `incidents.summaries`. Naming them here rather than testing `!= "candidate"`
# keeps a future status from silently becoming a proposal.
_REPAIR_STATUSES = ("active", "promoted", "demoted")

# Distinct sessions a signature must have survived before it counts as blocking.
# A failure that crosses a session boundary is systemic; one that fires twice
# inside a single session is usually one person retrying one thing.
#
# The obvious alternative — treating the record's `flag` as the severity signal —
# is a DEAD BRANCH, and checking that is why it is not used: `needs_review`
# requires a human rule and `reroute_exhausted` requires an auto rule, so every
# flagged signature is also `rule_covered`, scores zero, and is refused before
# severity is ever read. A severity term that can never apply is worse than none,
# because the scale silently loses a level nobody notices is missing.
REPAIR_SEVERITY_SESSIONS = 2


def repair(evs=None, *, project=None) -> list:
    """A failure signature that keeps happening and has nothing written down.

    The recurrence count, the threshold crossing, and whether a rule exists are
    all already resolved by `incidents.summaries` — the same fold the dashboard's
    Improvements view and `hive rules list` read. Re-deriving them from
    `tool_failed` rows here would give the injector its own opinion of what
    counts as recurring, and the two would disagree the first time the incident
    threshold moved.

    A signature that DOES have a rule still produces a candidate, marked
    `rule_covered`. The gate refuses it and records why, so the round's audit
    shows the lens working and being correctly declined — a silent skip would be
    indistinguishable from the lens finding nothing, which is the one difference
    the dry-round exit reads."""
    from . import incidents as inc_mod
    out = []
    for rec in inc_mod.summaries(project) or []:
        if not isinstance(rec, dict):
            continue
        signature = str(rec.get("signature") or "").strip()
        if not signature or rec.get("status") not in _REPAIR_STATUSES:
            continue
        occurrences = _count(rec.get("occurrences"))
        sessions = _count(rec.get("sessions"))
        covered = bool(str(rec.get("active_rule") or "").strip()
                       or str(rec.get("prevention_rule") or "").strip())
        evidence = [f"signature {signature}",
                    f"{occurrences} recorded occurrence(s) across "
                    f"{sessions} session(s), status {rec.get('status')}"]
        excerpt = str(rec.get("excerpt") or "").strip()
        if excerpt:
            evidence.append(f"raw signal: {excerpt}")
        out.append(Candidate(
            lens="repair",
            title=f"Investigate and patch the root cause of {signature}",
            fingerprint=fingerprint("repair", signature),
            evidence=tuple(evidence),
            recurrence=occurrences,
            severity=sessions >= REPAIR_SEVERITY_SESSIONS,
            rule_covered=covered))
    return out


LENSES["repair"] = repair


# --- lens: caveat -------------------------------------------------------------

# How much of a caveat's meaning a later memo must repeat before it counts as
# closing it. Containment, not Jaccard: a `code_change` that fixes a caveat says
# far more than the caveat did, so symmetric overlap would never fire. The bias
# is deliberate and asymmetric — a false "still open" costs one refused
# candidate, a false "closed" loses a safety-critical note.
CAVEAT_CLOSE_RATIO = 0.8
_CAVEAT_CLOSERS = ("experiment_result", "code_change")


def _memo_text(payload) -> str:
    """Memo text from a payload, tolerating the two shapes the ledger actually
    holds: a dict with a ``text`` key, and a bare string from older writers
    (``roadmap._session_prompt`` reads both the same way).

    Anything else is a malformed row and contributes nothing. `str()` on it
    would turn junk into a plausible-looking caveat — a list payload rendered as
    ``['not', 'a', 'dict']`` and became a proposed piece of work."""
    if isinstance(payload, dict):
        return str(payload.get("text") or "")
    if isinstance(payload, str):
        return payload
    return ""


def _closes(caveat_tokens, closer_tokens) -> bool:
    """True when *closer_tokens* repeats enough of *caveat_tokens* to count as
    addressing it. Empty caveat tokens never close — a memo with nothing but
    stopwords in it would otherwise be closed by every later event."""
    if not caveat_tokens:
        return False
    return (len(caveat_tokens & closer_tokens) / len(caveat_tokens)
            >= CAVEAT_CLOSE_RATIO)


def caveat(evs) -> list:
    """A pinned caveat nothing has closed.

    Pinned is the project's own marker for "safety-critical": `sync` keeps pinned
    caveats through the MEMORY.md cap rotation and `consolidate` never folds
    them. An unpinned caveat is a note; a pinned one is a standing constraint,
    which is why every candidate here carries `severity`.

    Re-appends collapse through `dedupe.similar`, the same rule
    `hive consolidate` uses, so a reworded repeat raises the recurrence count
    instead of minting a second candidate. The fingerprint keys on the OLDEST
    member's text, so a later re-append does not re-open a condition already
    proposed."""
    from . import dedupe as dedupe_mod
    groups, closers = [], []
    for ev in evs or []:
        etype = getattr(ev, "type", None)
        if etype in _CAVEAT_CLOSERS:
            closers.append(dedupe_mod.normalize(_memo_text(
                getattr(ev, "payload", None))))
            continue
        if etype != "caveat":
            continue
        if "pinned" not in (getattr(ev, "tags", None) or []):
            continue
        text = _memo_text(getattr(ev, "payload", None)).strip()
        if not text:
            continue
        tokens = dedupe_mod.normalize(text)
        for g in groups:
            if dedupe_mod.similar(g["tokens"], tokens):
                g["count"] += 1
                break
        else:
            groups.append({"text": text, "tokens": tokens, "count": 1,
                           "ts": getattr(ev, "ts", "") or ""})

    out = []
    for g in groups:
        if any(_closes(g["tokens"], c) for c in closers):
            continue
        headline = g["text"].splitlines()[0].strip()
        out.append(Candidate(
            lens="caveat",
            title=f"Resolve pinned architectural caveat: {headline}",
            fingerprint=fingerprint(
                "caveat", dedupe_mod.text_fingerprint(g["text"])),
            evidence=(f"pinned caveat @ {g['ts']}",
                      f"seen {g['count']} time(s), nothing recorded closing it",
                      headline[:MAX_EVIDENCE_CHARS]),
            recurrence=g["count"],
            severity=True))
    return out


LENSES["caveat"] = caveat


# --- the tree the filesystem lenses read --------------------------------------
# The SURFACE's tree, not this module's. A loop run in someone else's project
# must not be handed work mined from the mindhive checkout — it would propose
# refactoring files the run has no business touching, and the evidence would
# point at paths that do not exist where the work is happening.
#
# `paths.worktree_root()` answers the git worktree containing cwd and is total
# (None outside a repo). The module-derived path is the fallback, not the
# default: it is right only when the surface happens to be running in this
# checkout, which is the session case and not the loop case.

def repo_root(cwd=None):
    from pathlib import Path
    from . import paths as paths_mod
    return paths_mod.worktree_root(cwd) or Path(__file__).resolve().parents[2]


# `lens_registry` deliberately does NOT live here. It reads the registry, and a
# `from .replenish import LENSES` binding is a SECOND name for the same dict — a
# test that swaps the registry by reassigning `replenish.LENSES` would leave this
# module still holding the original, and the two would silently disagree about
# which lenses exist. It lives beside the registry it reads, in `replenish.py`.


# Only the two directories `review.in_scope` accepts. Walking the repo root
# would drag in .venv, node_modules and .git for nothing.
_SCAN_DIRS = ("src", "tests")
# Bounds on the walk. A lens that stalls the SessionEnd hook is worse than a
# lens that does not exist, and the tree is not something the injector controls.
SCAN_MAX_FILES = 4000
SCAN_MAX_BYTES = 4 * 1024 * 1024


def _scan_source(root):
    """``(path -> line count, truncated)`` for in-scope source under *root*.

    Total by construction: an unreadable or oversized file is skipped, never
    raised on. Returns paths in the repo-relative POSIX form the evidence rows
    and fingerprints use, so a candidate reads the same on Windows and CI."""
    from pathlib import Path
    from . import review as review_mod
    root = Path(root)
    out, seen, truncated = {}, 0, False
    for sub in _SCAN_DIRS:
        base = root / sub
        if not base.is_dir():
            continue
        for p in sorted(base.rglob("*.py")):
            if seen >= SCAN_MAX_FILES:
                truncated = True
                break
            posix = p.as_posix()
            if not review_mod.in_scope(posix):
                continue
            seen += 1
            try:
                if p.stat().st_size > SCAN_MAX_BYTES:
                    continue
                with p.open(encoding="utf-8", errors="replace") as fh:
                    out[p.relative_to(root).as_posix()] = sum(1 for _ in fh)
            except OSError:
                continue
        if truncated:
            break
    return out, truncated


# --- lens: debt ---------------------------------------------------------------

# The master prompt's number. The bucket is what turns "past the threshold" into
# a ranking, and 250 is chosen so the BALANCED gate (25) first bites at 1500
# lines — `review.BATCH_LINE_LIMIT`, the size this project already treats as one
# reviewable unit. Below that a file is large; at that point it stops fitting in
# a single review pass, which is the concrete cost.
DEBT_LINE_THRESHOLD = 1000
DEBT_BUCKET_LINES = 250


def _debt_bucket(lines) -> int:
    """How many buckets past the threshold — 1 the moment it is crossed, so a
    file exactly at the threshold still ranks above one that is not."""
    return (lines - DEBT_LINE_THRESHOLD) // DEBT_BUCKET_LINES + 1


def debt(evs=None, *, root=None, project=None) -> list:
    """A file too big to hold in one review pass, or one the review queue is
    already blocking on.

    ONE candidate per file, even when both conditions hold — the outstanding
    fact is "this file needs reducing", and two candidates would spend two of
    the round's slots on it and mint two fingerprints for one condition.

    The fingerprint keys on the path plus the size bucket, not the raw line
    count. A file that keeps growing inside one bucket is the SAME outstanding
    condition and must not be re-proposed every round; crossing into a worse
    bucket genuinely is a new one."""
    from . import review as review_mod
    sizes, truncated = _scan_source(root or repo_root())
    # The queue records ABSOLUTE paths (review.queue writes whatever
    # `file_touched` carried); the scan yields repo-relative ones. Match by
    # suffix rather than normalising either side into the other's shape — the
    # marker may have been written from a different worktree of the same repo.
    marker = review_mod.load_pending(project)
    queued = [f.replace("\\", "/") for f in
              ((marker.get("files") or []) if isinstance(marker, dict) else [])
              if isinstance(f, str)]
    scan_note = (f"scan stopped at {SCAN_MAX_FILES} files — this list is not "
                 "the whole tree") if truncated else ""

    out = []
    for rel, lines in sorted(sizes.items()):
        in_queue = any(q.endswith("/" + rel) or q == rel for q in queued)
        over = lines >= DEBT_LINE_THRESHOLD
        if not over and not in_queue:
            continue
        bucket = _debt_bucket(lines) if over else 0
        reasons = []
        if over:
            reasons.append(f"{lines} lines, past the {DEBT_LINE_THRESHOLD}-line "
                           "threshold")
        if in_queue:
            reasons.append("queued for thermo-nuclear review")
        evidence = [f"{rel}: " + "; ".join(reasons)]
        if scan_note:
            evidence.append(scan_note)
        out.append(Candidate(
            lens="debt",
            title=f"Refactor and reduce line count/complexity in {rel}",
            fingerprint=fingerprint("debt", f"{rel}:{bucket}"),
            evidence=tuple(evidence),
            recurrence=max(0, bucket),
            # The review queue is the project's own blocking notice: STATE.md
            # renders it as a gate, not a suggestion (SOP `change-audit` §4).
            severity=in_queue))
    return out


LENSES["debt"] = debt


# --- lens: coverage -----------------------------------------------------------

# Modules with no test counterpart by design, not by omission: a package marker
# and the CLI entry point, whose behaviour is covered through the subcommand
# suites rather than a file named after it.
_COVERAGE_SKIP = ("__init__.py", "__main__.py")


def _test_names(rel: str):
    """The test file names that would count as covering *rel*.

    Both conventions are live in this repo: `src/mindhive/loop/task.py` is
    covered by `tests/test_loop_task.py`, and `src/mindhive/mindnet/agent.py` by
    `tests/test_mindnet_agent.py` — but several package modules are covered by a
    bare `tests/test_<name>.py`. Accepting both is what keeps the lens from
    proposing work for modules that are, in fact, tested."""
    parts = rel.split("/")
    if len(parts) < 3:                       # not under src/mindhive/
        return ()
    stem = parts[-1][:-3]
    pkg = parts[2:-1]                        # src / mindhive / <pkg...> / name
    names = [f"tests/test_{stem}.py"]
    if pkg:
        names.insert(0, "tests/test_" + "_".join(pkg + [stem]) + ".py")
    return tuple(names)


def coverage(evs=None, *, root=None) -> list:
    """A source module with no test file named after it.

    `recurrence` is how many `code_change` entries have named the module, which
    is the discriminating fact: an untested module nobody touches is a different
    (and much cheaper) problem from an untested module under active change. It
    is also what keeps the lens off the bottom of the scale — a bare candidate
    scores the base and would never clear the balanced threshold, so a coverage
    proposal has to earn its place with recorded churn."""
    sizes, truncated = _scan_source(root or repo_root())
    existing = {rel for rel in sizes if rel.startswith("tests/")}
    churn = {}
    for ev in evs or []:
        if getattr(ev, "type", None) != "code_change":
            continue
        p = getattr(ev, "payload", None)
        if not isinstance(p, dict):
            continue
        for f in p.get("files") or []:
            if isinstance(f, str):
                key = f.replace("\\", "/")
                churn[key] = churn.get(key, 0) + 1

    scan_note = (f"scan stopped at {SCAN_MAX_FILES} files — this list is not "
                 "the whole tree") if truncated else ""
    out = []
    for rel in sorted(sizes):
        if not rel.startswith("src/mindhive/"):
            continue
        if rel.rsplit("/", 1)[-1] in _COVERAGE_SKIP:
            continue
        wanted = _test_names(rel)
        if not wanted or any(n in existing for n in wanted):
            continue
        changes = sum(n for path, n in churn.items() if path.endswith(rel))
        evidence = [f"{rel} has no test counterpart",
                    "looked for: " + ", ".join(wanted),
                    f"{changes} change-log entr(ies) name it"]
        if scan_note:
            evidence.append(scan_note)
        out.append(Candidate(
            lens="coverage",
            title=f"Bootstrap unit test suite for {rel}",
            fingerprint=fingerprint("coverage", rel),
            evidence=tuple(evidence),
            recurrence=changes))
    return out


LENSES["coverage"] = coverage


# --- lens: audit --------------------------------------------------------------

def audit(evs=None, *, project=None, root=None, changed_fn=None) -> list:
    """Source edited with no change-log entry covering it.

    Both halves come from `changelog`, deliberately: the pending marker
    SessionEnd writes (cumulative, and the thing STATE.md renders as a blocking
    notice) and the git working-tree scan `hive log` already uses. Re-deriving
    either here would let the injector and the blocking notice disagree about
    which files are unlogged, and the operator would have no way to tell which
    was right.

    ONE candidate for the whole change-set. The outstanding fact is singular —
    "this change-set has no entry" — and a candidate per file would spend the
    round's whole cap on a busy branch.

    Git being unavailable yields no candidates and NO lens error. "I could not
    look" is not "there is nothing", but it is also not a defect worth minting a
    `tool_failed` row for on every round of a non-git checkout; the round's own
    dry accounting already says nothing was found."""
    from . import changelog as cl_mod
    marker = cl_mod.load_pending(project)
    files, seen = [], set()
    for f in ((marker.get("files") or []) if isinstance(marker, dict) else []):
        if isinstance(f, str) and f.strip():
            key = f.replace("\\", "/")
            if key not in seen:
                seen.add(key)
                files.append(key)

    covered = cl_mod._covered_map(evs or [])
    if changed_fn is None:
        # Asked of the SURFACE's checkout. Run in the process's own directory,
        # a loop in another project would report this repo's changes as that
        # run's unlogged work.
        def changed_fn():
            return cl_mod._git_changed_files(cwd=root)
    for f in changed_fn() or []:
        if not isinstance(f, str) or not f.strip():
            continue
        key = f.replace("\\", "/")
        if key in seen or key in covered:
            continue
        seen.add(key)
        files.append(key)

    if not files:
        return []
    files.sort()
    plural = "" if len(files) == 1 else "s"
    evidence = [f"{len(files)} source file{plural} changed with no covering "
                "code_change entry"]
    evidence += [f"unlogged: {f}" for f in files[:_EVIDENCE_FILES]]
    if len(files) > _EVIDENCE_FILES:
        evidence.append(f"… and {len(files) - _EVIDENCE_FILES} more")
    return [Candidate(
        lens="audit",
        title=("Audit changes and record change-log entries for "
               f"{len(files)} modified source file{plural}"),
        # Keyed on the whole set: while the same files are unlogged this is one
        # condition, and a file joining the set is a new one.
        fingerprint=fingerprint("audit", "\n".join(files)),
        evidence=tuple(evidence),
        recurrence=len(files),
        # CLAUDE.md makes logging mandatory and STATE.md renders the notice as
        # blocking. The flag is that recorded stance, not this lens's opinion.
        severity=True)]


LENSES["audit"] = audit
