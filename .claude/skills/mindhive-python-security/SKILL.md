---
name: mindhive-python-security
description: >-
  Security discipline for the MindHive codebase: keeping secrets out of the
  immutable store, dashboard auth-token handling, subprocess hygiene on
  Windows, fail-open vs fail-closed decisions, and worktree path safety. Use
  when a change touches tokens, subprocesses, file writes, or gate logic.
origin: mindhive
pack: mindhive-python
role: security
kind: security
---

# MindHive Python Security

Security rules for a local-first, event-sourced tool: the threats are secret
persistence, process leakage, and gates that fail the wrong way.

## When to Activate

- Writing code that could place user text into the event store
- Touching the dashboard server, its auth token, or any HTTP surface
- Spawning subprocesses (git, schtasks, editors) from src/mindhive code
- Choosing failure behavior for a new gate, guard, or validator
- Reviewing writes into `~/.mindhive`, `~/.claude`, or the repo tree
- Handling paths that may come from a worktree, a config file, or user input
- Adding any capture surface that turns external text into stored events
- Deciding what a new feature may read from, or write into, the environment

## Threat model: local-first is not threat-free

MindHive runs as the user, on the user's machine, with no network listener
except the dashboard bound to localhost. The real risks are therefore not
remote attackers but *permanence and leakage*:

```python
# 1. The store is immutable. Anything written to it is effectively forever.
# 2. Projections auto-load into LLM context. A secret in STATE.md is a secret
#    pasted into every future session transcript.
# 3. Subprocesses inherit the environment. A token in os.environ is a token
#    in every child process's /proc-equivalent.
```

## Secrets never enter the store — because they can never leave

The ledger's write-once discipline (see the sibling patterns file: corrections
are new events, and `hive verify` hash-chains every row) has a security
corollary that outranks convenience:

```python
# UNREDACTABLE: an API key appended as event payload cannot be deleted
# without rewriting the chain — which `hive verify` exists to detect.
# Every append surface must therefore treat secret-shaped strings as toxic.

# BAD — harvesting raw tool output into a memo event:
events.append(type="experiment_result",
              payload={"text": raw_stdout})          # may contain tokens

# GOOD — capture surfaces store the SIGNATURE, not the raw text:
payload = {"text": text[:500], "signature": signature(text),
           "tool": tool, "category": category}
```

Redaction happens at capture time or never. The failure-capture path
(`hookcapture` → `tool_failed` events) keeps payloads machine-shaped
(`{tool, error, category, retryable}`) partly for recall quality and partly
so free-form secret-bearing text has no field to hide in.

## The dashboard auth token

The dashboard binds `127.0.0.1` and still requires a per-run bearer token —
localhost is shared with every other local process, including browsers running
untrusted pages.

```python
# Rules the token handling must keep:
# - Generated per run (secrets.token_urlsafe), never persisted to the store.
# - Compared with hmac.compare_digest, never `==` (timing).
# - Never logged, never placed in an event payload, never in a URL that the
#   browser history records — header or cookie only.
# - The port (54461) is stable; the token is not. Anything that "remembers"
#   the token across restarts has broken the model.
```

When testing dashboard code, the `isolate_store` fixture (see the sibling tdd
file) gives a throwaway namespace — never point a test at the live store to
"check the token works"; assert on the compare function and the header path.

## Subprocess hygiene

src/mindhive spawns `git`, `schtasks`, and editors. Every spawn follows one
shape — `context._memory_folder` is the canonical example:

```python
r = subprocess.run(
    ["git", "rev-parse", "--git-common-dir"],   # LIST argv — never shell=True
    cwd=str(base),
    capture_output=True, text=True,
    timeout=2,                                   # bounded — a hung git must
    creationflags=paths.NO_WINDOW,               # not hang a report
)
if r.returncode == 0 and r.stdout.strip():
    ...                                          # trust exit code, then parse
```

The rules, each load-bearing:

```python
# shell=False (implicit with list argv): no quoting/injection surface. A path
#   containing spaces or `&` is DATA here, never syntax.
# timeout: every subprocess in a read path gets one. Reports must terminate.
# creationflags=paths.NO_WINDOW: on Windows, a console flash per spawn is a
#   UX bug AND an information leak (window titles show command lines).
# Never pass secrets in argv — Windows exposes command lines to any process
#   with the same integrity level (WMI Win32_Process). Environment or stdin.
```

## Fail-open vs fail-closed is a decision, not an accident

Every guard in this codebase writes its failure direction down. The decision
table this repo actually uses:

```python
# FAIL-OPEN (absence of config must not break the tool):
#   context.load_budget      -> corrupt/missing budget = "no budget set"
#   packs.load_external_corpus -> unset env var = warning, comparison skipped
#   plugin scanning          -> unreadable SKILL.md = that file skipped
#
# FAIL-CLOSED (the gate IS the product; passing on error would be lying):
#   hive packs check         -> unreadable manifest = error finding, exit 1
#   hive context --check     -> check without a budget = exit 2, not exit 0
#   chain verification       -> any hash mismatch = failure, loudly
```

The rule of thumb: *reporting* degrades gracefully; *gating* never silently
passes. When you add a gate, write the direction into its docstring the way
`packs.load_manifest` does — the reviewer should never have to infer it.

## Worktree and path safety

Paths arrive from config files, manifest JSON, event payloads, and CLI args.
Treat them as data with two invariants:

```python
# 1. Resolve before comparing. A gate that allowlists a directory must
#    compare resolved absolute paths, or `..` segments walk out of it:
root = skills_root.resolve()
target = (root / slug / "SKILL.md").resolve()
if root not in target.parents:
    # refuse: the slug escaped the skills tree
    ...

# 2. Never re-derive namespace paths by hand. paths.project_dir() is the one
#    place MINDHIVE_HOME/MINDHIVE_PROJECT are honored; a hand-built
#    ~/.mindhive path in test code is how a test escapes isolation and
#    touches the live ledger.
```

The pack validator constructs skill paths from manifest-declared `pack` and
`role` tokens; both feed a directory name, so the slug grammar check
(`name == <pack>-<role>`) is also an injection guard — a role of `../evil`
fails the structure gate before any path math happens.

## Windows file-semantics caveats

```python
# Atomic writes: os.replace() is atomic on NTFS but fails if the destination
#   is open in another process (the dashboard!). write_text_atomic handles
#   the retry; hand-rolled tmp+rename code usually does not.
# Deletes of open files fail on Windows where POSIX would succeed — cleanup
#   code must tolerate PermissionError and retry or defer, never crash.
# ACLs are not chmod: pack/skill files land user-writable by default; do not
#   assume a 0o600-style guarantee exists for the token or sidecar files.
#   Anything genuinely sensitive stays in memory for the process lifetime.
```

## Recalled text is data, never instructions

The store's contents render into LLM context every session — MEMORY.md
auto-loads, recall results inject, briefings deliver. That makes the ledger
a *prompt-injection surface*: text captured from tool output, web content,
or transcripts today becomes instruction-adjacent context in every future
session that recalls it.

```python
# Defenses, layered:
# 1. Capture-side shaping (again): structured payloads over prose. A field
#    named "category" cannot smuggle an imperative; a free "text" blob can.
# 2. Delivery framing: injected recall arrives labelled as background
#    context, and the operating directives instruct treating it as
#    relevant-not-authoritative. Framing is prose, though — see 3.
# 3. Truncation at delivery: memo headlines (first line, 140 chars) are
#    what projections show. Hostile payloads lose their tail; keep it that
#    way when adding delivery surfaces — deliver summaries, not documents.
# 4. Never round-trip: text that arrived IN a session's context must not be
#    re-captured wholesale as a "lesson" — that is how an injected
#    instruction becomes persistent. Memos are written deliberately, small,
#    and in the operator's voice.
```

The review question for any new capture or delivery path: *could a string
an external tool produced end up rendered, un-truncated, into a future
session?* If yes, the path needs shaping or truncation before it ships.

## The event-type registry is an allowlist

Append surfaces reject unregistered event types — the registry in events.py
doubles as a security control, not just a schema nicety:

```python
# Why it matters here:
# - No generic "write anything" surface exists: every append names a
#   registered type whose payload contract a specific module owns.
# - A compromised or buggy caller cannot invent "system_instruction"-style
#   event types that downstream renderers would deliver — the append
#   surface refuses at the boundary, loudly.
# - Tags are drawn from an existing taxonomy; new tags are deliberate
#   decisions because they become graph edges and recall filters — an
#   attacker-controlled tag would steer future retrieval.
```

When reviewing a diff that adds an append call, check three things: the
type is registered with a why-comment, the payload is built from named
fields (never `**untrusted_dict`), and tags come from literals in the code,
not from input.

## Environment variables: config, not secrets, not trust

`MINDHIVE_HOME`, `MINDHIVE_PROJECT`, `MINDHIVE_SESSION`, corpus path vars —
the codebase reads environment for *configuration*, and the trust rules are
explicit:

```python
# Env vars REDIRECT behavior; they must never ESCALATE it.
#   MINDHIVE_HOME points the store somewhere else — acceptable, the caller
#   already controls the filesystem. It must never disable a gate: no
#   MINDHIVE_SKIP_CHECKS style variables, ever. Gates are disabled by
#   editing config sidecars, which are visible, diffable decisions.
# Paths from env are still paths: resolve() them and treat them with the
#   same containment discipline as any input (below).
# Never put a secret IN an env var this code then logs, embeds in an event
#   payload, or passes to argv. Environment inheritance means every child
#   process sees it; the subprocess rules above exist for exactly this.
# session_id comes from MINDHIVE_SESSION or falls back to "manual" —
#   fabricating ids would corrupt the store's join key, so the fallback is
#   a constant, never a guess.
```

## Reviewing a diff for security

The checklist this pack's rules compress into, applied to any
`src/mindhive/` diff in review order:

```text
1. New append call?      type registered · payload from named fields ·
                         literal tags · no raw tool/transcript text
2. New subprocess?       list argv · timeout · NO_WINDOW · no secrets in
                         argv · exit code checked, not output-parsed blindly
3. New file write?       write_text_atomic for sidecars · encoding named ·
                         inside a resolved, contained root
4. New gate or guard?    failure direction stated in the docstring ·
                         fail-closed if it gates, fail-open if it reports
5. New HTTP surface?     localhost bind · token compared constant-time ·
                         token absent from URLs, logs, and events
6. New env var?          redirects, never escalates · documented · path
                         values resolved and contained
7. New delivery surface? truncation/caps applied · recalled text framed as
                         data · measured with hive context before shipping
```

A diff that touches none of these seven surfaces has no security review to
fail; a diff that touches one and skips its row is the finding.

## The trust boundary map

Every rule in this file guards one of five boundaries; naming the boundary
is how a review decides which rules apply to a diff:

```text
CLI args        -> handlers        argparse shapes it; handlers bound it
                                   (positive numbers, choices, min length)
environment     -> configuration   redirects behavior, never escalates;
                                   paths resolved and contained
tool output     -> the store       shaped payloads only; signatures over
                                   prose; registered types; literal tags
the store       -> session context caps + truncation + framing; recalled
                                   text is data, never instructions
localhost       -> dashboard       per-run token, constant-time compare,
                                   header-only transport
src/mindhive    -> the OS          list-argv subprocess, timeout, no
                                   secrets in argv, exit-code truth
```

A diff crossing zero boundaries needs no security review; a diff crossing
one gets that boundary's row applied in full; a diff that ADDS a boundary
(a new listener, a new capture source) needs this file amended before it
lands — an unmapped boundary is an unreviewed one.

## Input at the CLI boundary

argparse handles shape; handlers own semantics. Numbers get bounds
(`--top`, budgets must be positive — `load_budget` rejects non-positive),
choices get `choices=[...]` (`--origin {hive,claude,all}`), and free text that
will be *stored* (memo text, change-log explanations) gets a minimum-length
check at the append surface — garbage in the immutable store is garbage in
every future briefing.

Never `eval`, never `pickle` across a trust boundary, and JSON parsing of
user-editable files (manifest, sidecars) always lands in the total-read
pattern: parse failure becomes a Finding or a None, never a traceback.

## What this model deliberately excludes

Honest scope keeps the rules credible. The threats this codebase does NOT
defend against, and why that is a decision rather than an oversight:

```text
A local attacker with the user's own account
  -> out of scope. The store, sidecars, and skills are user-writable by
     design; OS account separation is the boundary, not this tool.
Encryption at rest
  -> out of scope, stdlib-only and single-user. The mitigation is the
     capture-side rule: nothing worth encrypting is allowed IN.
A malicious MindHive module
  -> out of scope. src/ is the trusted computing base; review and the
     change-log discipline are the control, not runtime sandboxing.
Multi-user concurrency
  -> out of scope. One user, one machine; the locks that exist guard
     against concurrent SESSIONS, not adversarial writers.
```

If a future feature crosses one of these lines (a sync server, a shared
store), this file's model is invalid for it and must be rewritten first —
scope drift in a threat model is itself the vulnerability.

## Incident response: a secret landed in the store

It will happen eventually; the response order matters more than the tooling:

```text
1. ROTATE THE SECRET FIRST. The copy in the ledger is now a fact of
   history; treat it as disclosed from the moment of discovery. Every
   minute spent on store surgery before rotation is exposure time.
2. Contain delivery, not history: regenerate projections after confirming
   the memo/payload is excluded or truncated out (consolidation and caps
   act on delivery — the auto-loaded surfaces are what future sessions
   actually see).
3. Only then weigh store surgery. Namespace-level pruning exists (opt-in,
   guarded) but is a chain-visible operation on an audit structure —
   deleting history is itself an auditable event and usually not worth it
   once the secret is dead.
4. Close the capture hole: the incident's root cause is a path that let
   raw text in. Fix THAT (shaping, truncation) and add the test, or the
   next secret follows the same route.
```

The postmortem question is never "who pasted the key" — it is "which
capture surface accepted prose it should have shaped".

## Quick Reference

| Mechanic | Use |
|---|---|
| signature-not-raw-text payloads | capture surfaces store machine fields; secrets have no field to hide in |
| write-once corollary | anything appended is unredactable — filter at capture time |
| `secrets.token_urlsafe` + `hmac.compare_digest` | dashboard token: per-run, constant-time compare, header-only |
| list-argv `subprocess.run` + `timeout` + `NO_WINDOW` | every spawn; no shell, no hang, no console flash |
| no secrets in argv | Windows exposes command lines process-wide; use env or stdin |
| fail-open reporting / fail-closed gating | direction written in the docstring, per the repo decision table |
| `resolve()` + `parents` containment | path allowlists survive `..`; slug grammar doubles as injection guard |
| `write_text_atomic` retry semantics | NTFS rename-over-open-file behavior handled once, centrally |
| bounded CLI inputs | positive budgets, `choices=`, min-length stored text |
| five-boundary map | route a diff's review by which trust boundary it crosses |
| rotate-first incident order | the leaked secret dies before any store surgery starts |
| scope honesty | out-of-scope threats named, so scope drift is detectable |
| delivery truncation as defense | headlines and caps strip hostile tails before context |
| no gate-disabling env vars | environment redirects behavior, never escalates it |
