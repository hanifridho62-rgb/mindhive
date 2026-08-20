# Participatory-intelligence gateway — blueprint (PART 5, honoured in this stack)

Status: **§2.1 and §2.2 are implemented; §2.3 is not.** The fold, its two append
contracts, and the `hive consensus` group landed as `src/mindhive/consensus/`
(38 tests in `tests/test_consensus.py`). The HTTP routes of §4, the job of §5,
and the dashboard section of §6 are still plan. Every row marked **EXISTS** names
code you can open today; **NEW** is proposed and not claimed; **LANDED** is this
plan's own new work, already in the tree.

Two claims in the first draft of this document were wrong and are corrected
below rather than quietly deleted, because a blueprint that edits away its own
mistakes teaches the next reader nothing: §1 and §3 asserted that this codebase
had no single validation seam. It has one — `events._PAYLOAD_CONTRACTS` plus
`events.validate_typed_payload` — and the consensus contracts extend it instead
of inventing a parallel mechanism.

## 0. Why this is not a Laravel application

The standing directive asks for Laravel plus a static frontend. This repository
is Python: `pyproject.toml`, a `src/mindhive/` package, pytest, argparse, an
append-only event ledger, and a stdlib `BaseHTTPRequestHandler` server. There is
no `composer.json`, no `artisan`, no PHP runtime, and no node application. The
MANDATORY SOP `execution-discipline` §6 records the decision this document
follows: *"The standing ask names Laravel; this is Python with no PHP, so the
intent is honoured and the framework is this one."*

So the deliverable is not a translation of Laravel's file layout. It is the
answer to what Laravel was being asked *for* — a request surface, typed
validation, background work, a query side separate from the write side, and a
rendered dashboard — expressed in the seams this codebase already has. Where a
Laravel concept has no counterpart worth building, this document says so rather
than inventing one.

## 1. Concept map — Laravel's ask → this stack's seam

| PART 5 asks for | The seam here | State |
|---|---|---|
| Controllers | `GetRoutes` / `PostRoutes` mixins composed onto the handler by `dashboard.make_handler`, each owning its half of the `_GET_ROUTES` / `_POST_ROUTES` table | **EXISTS** — `src/mindhive/dashboard_get.py`, `dashboard_post.py` |
| FormRequests (strictly typed input validation) | `events._PAYLOAD_CONTRACTS` maps an event type to a module whose `validate_payload` raises `ValueError`; `events.validate_typed_payload` is the one gate both `hive append` and `/api/append` pass through | **EXISTS** — `src/mindhive/events.py`; extended by `consensus/contributions.py`, `consensus/records.py` |
| Eloquent models / repositories | the write side is `events.append_event` into `events/<session>.jsonl`; there are no mutable rows to model | **EXISTS** — `src/mindhive/events.py` |
| Read models / queries | the Postgres read mirror and its eight bounded tools | **EXISTS** — `pgmirror.py`, `pgstore.py`, `pgtools.py`, `pgshape.py`, `pgsearch.py` (branch `feat/pg-event-mirror-mcp`, PR #112) |
| Job queues + event listeners | `jobs.register_kind` / `jobs.start`, `OutputRing` read by absolute byte offset, and the `job_started` / `job_finished` / `job_abandoned` rows | **EXISTS** — `src/mindhive/jobs.py` |
| API gateway to a Python microservice | there is no second service to bridge to; the agent-facing gateway that does exist is MCP over stdio | **EXISTS** — `hive pg mcp`, `src/mindhive/mcp_server.py` |
| `backend_unavailable` → a friendly response | the outcome vocabulary (`did_work`, `nothing_to_do`, `degraded`, `failed`, `abandoned`) and the structural error envelope | **EXISTS** — `outcomes.py`, `pgshape.error_envelope` |
| Static frontend (Vue / React / Livewire) | one `<script>` assembled from six `dashboard_js_*.py` fragments, one stylesheet, one palette source | **EXISTS** — `dashboard_page.py`, `dashboard_css.py`, `theme.py` |
| Auditability ("decide in daylight") | append-only JSONL, hash-chained, provable against the mirror by `hive pg parity` | **EXISTS** — `events/`, `chain.json`, `pgcli.py parity` |

Two rows deserve emphasis because they change what has to be built.

**CQRS is already the shape.** PART 5 imagines Laravel in front of a Python
compute service. What exists is stronger and simpler: an append-only write side
whose only mutation is "append one canonical event", and a derived read side
(the Postgres mirror) that is rebuildable from it and provable against its hash
chain. A gateway process in front of that would add a hop, a serialization
boundary, and a second place for the schema to drift — while removing nothing.
So the gateway is a **module boundary inside this process**, not a service.

**The frontend has no build step, and that is a constraint to keep.** The panel
is served by the same process on `127.0.0.1:54461` behind a session token, as
one document with one inline `<script>`. Introducing Vue or React means a
bundler, a `node_modules`, a second deploy artifact, and a CSS system competing
with `theme.py` — which `tests/test_dashboard_theme.py` currently enforces as
one source of truth for four documents. The existing fragments already give the
separation a component framework is wanted for (`dashboard_js_core.py` holds the
render/paint choke points; the section modules hold one area each). This plan
therefore proposes **no** frontend framework, and treats "static framework
integration" as satisfied by the fragment layout plus the palette contract.

## 2. What is genuinely missing

Three capabilities in PART 4 have no code today. They are the whole of the new
work, and they are listed smallest-first because each is usable without the next.

1. **Contribution ingestion.** ~~One route that accepts~~ — **LANDED** as
   `consensus.contribute` behind `contributions.validate_payload`, reachable as
   `hive consensus contribute` and as a plain `hive append contribution`, because
   registering the contract in `_PAYLOAD_CONTRACTS` gates BOTH surfaces at once.
   The HTTP route (§4) is still outstanding; the ingestion it would call is not.
2. **Weighted consensus.** **LANDED** as `consensus.fold_window` — a pure fold
   over a list of events, no store — plus `fold_and_record` and
   `hive consensus fold`. The rule is named and versioned
   (`subject-share-weighted-median-v1`): per-subject weight normalisation so one
   flooder gets one share, then a weighted MEDIAN, which is how it refuses the
   naive averaging PART 4 forbids. Wrapping it in a background job (§5) is still
   outstanding; the fold it would run is not.
3. **A reading surface.** A dashboard section that shows contributions arriving
   and consensus records forming, and — because PART 4 forbids black boxes —
   lets a reader walk from any consensus number back to the events it folded.

## 3. Validation: the FormRequest counterpart — **already existed**

The first draft of this section proposed a new module, `contribution_schema.py`,
on the premise that validation here was inline per handler. That premise was
false. `events._PAYLOAD_CONTRACTS` maps an event type to `(module, hint)`, and
`events.validate_typed_payload` calls that module's `validate_payload`, raising
`ValueError` carrying a corrective hint. Every typed append already passes
through it — `hive append`, `/api/append`, and any module that calls
`append_event` for a registered type.

So the consensus work registered two contracts in that table instead of building
a parallel mechanism:

* `consensus/contributions.py` — refuses a contribution with no frame, kind,
  subject, or value, a boolean value, a confidence outside `[0, 1]`, and — the
  load-bearing one — a **self-claimed `weight`**. A sender may not set its own
  influence; the rule assigns it and the record says so.
* `consensus/records.py` — refuses a record whose inputs do not each carry a
  `ref`, an `occurrence`, a positive `weight`, and a `reason`. This is the
  anti-black-box rule as a gate rather than a convention: an untraceable record
  in an append-only store is permanent, and reads as evidence forever.

The properties the proposed module was reaching for were the right ones, and the
existing seam has them:

- **It refuses, it does not raise.** `pgshape.error_envelope` exists precisely
  so a caller on a failing path gets a structured answer rather than an
  exception, and the AST gate in `tests/test_pgshape.py` keeps that module
  importable when the backend is gone. A validator that raised would push every
  route back into the try/except shape PART 3 rules out.
- **Refusal names the field and the rule.** A bare `400` teaches the sender
  nothing; the refusal carries `field`, `rule`, and the value's *shape* (never
  the value — a biometric sample is personal data and must not appear in a log
  line or a URL).
- **Stdlib only, no store access.** The same reason `pgshape` has that rule: the
  answer is wanted on the paths that run when everything downstream is down.

One correction to the third bullet, learned in the build: a validator must not
resolve a `ref` either. Checking that a 64-hex address exists in the ledger means
reading the whole ledger, which an append-surface gate cannot do. So
`records.validate_payload` checks ref SHAPE, and RESOLVABILITY is
`consensus.trace`'s job — `hive consensus trace` exits non-zero when an input
resolves to nothing, the same gate semantics `hive pg parity` uses.

The one genuinely new mechanism the build needed was not validation but identity:
a ref is a content address (SHA-256 over the same canonical bytes the hash chain
covers), so two byte-identical contributions — same subject, same value, same
second — share one. Three input rows pointing at one ref named "one of these"
three times, and a ref-only walk reported all three as found even against a
ledger holding one. Every input row therefore also carries `occurrence`, its
1-based rank among the events under its ref, and `trace` resolves the nth. This
defect was found by running the real CLI and reading its output, not by the
tests, which is the argument for §8's rule that a surface is not done until its
own output has been read.

## 4. Route surface

Registered in the existing tables, following the naming already there
(`_get_*` / `_post_*`, one method per route, thin over a fold):

| Method | Path | Handler | Answers |
|---|---|---|---|
| POST | `/api/contribute` | `_post_contribute` | 200 with the appended event's id, or 200 with a structural refusal — never a bare 400 |
| GET | `/api/contributions` | `_get_contributions` | a bounded page of contributions for the current namespace |
| POST | `/api/consensus` | `_post_consensus` | **202** plus a job id; the fold is long |
| GET | `/api/consensus` | `_get_consensus` | consensus records, newest first |
| GET | `/api/consensus/trace` | `_get_consensus_trace` | for one consensus id, the events it folded |

Two rules the existing server makes non-negotiable, not stylistic:

- **Every response carries an exact `Content-Length`, and a route that raises
  after writing must not frame a second response.** The connection is
  keep-alive (`protocol_version = "HTTP/1.1"`); `_fail` / `_responded` exist for
  this. A new route that returns early on refusal has to return *through* the
  same `_json` path.
- **Long work answers 202 with a job id and is polled at
  `GET /api/job?id=&offset=`.** Instant validation still answers synchronously.
  The consensus fold is a `jobs.register_kind` kind with its own lock, held for
  the life of the job.

`_get_consensus_trace` is the "decide in daylight" requirement made mechanical.
It is not a logging convention that can rot; it is a route whose absence is a
test failure.

## 5. Consensus: what the job may and may not do

PART 4 forbids naive averaging. The design consequence is that the job writes
**two** things, not one: the consensus record, and the weight it assigned to
each input with the reason. A number whose weights are not recorded cannot be
walked backwards, which is exactly the black box the PRD rules out.

Proposed event types (semantic, so `hive pg embed` indexes them and
`search_events_by_meaning` can reach them):

- `contribution` — one framed input. Payload carries a `kind` discriminant, the
  measurement, and the framing id. **One event type for every demographic and
  every instrument**, which is PART 4's general-purpose requirement taken
  literally: a youth anxiety sample and an executive's strategic input differ in
  the `kind` field and in nothing else. A second event type per population would
  put the demographic in the schema, where it becomes a fork in every reader.
- `consensus_record` — the fold's output, carrying the input event ids, the
  weight assigned to each, the rule that produced each weight, and the window
  boundaries. Re-runnable: given the same inputs and the same rule version, the
  same record.

The weighting rule is versioned in the payload (`rule_version`). Changing the
rule does not rewrite history; it produces new records that say which rule made
them. This is the same discipline the migrations follow — an applied file is
immutable, a change is a new number.

## 6. Storage

Nothing new. Contributions and consensus records are events in
`events/<session_id>.jsonl`, so they inherit the hash chain, the parity proof,
the Postgres mirror, and the semantic index for free. No new table, no new
sidecar, no migration.

One caveat worth stating before it is discovered: **all git worktrees of this
repo collapse onto the main repo's namespace** (`paths.detect_project()` resolves
`git rev-parse --git-common-dir`). Contributions written from a worktree land in
the same namespace as the main checkout's. That is correct for this data and is
the reason `paths.worktree_root()` is a deliberately separate function — do not
conflate them.

## 7. Scale

PART 4 asks that the interface not change between dozens of analysts and tens of
thousands of stakeholders. What actually breaks first, in order, and what it
costs:

1. **The consensus fold reads the whole namespace.** Same failure `/api/oversee`
   has today (872 ms per poll, still unfixed and recorded as deferred). Fix
   before it bites: fold a window, keyed by the window boundary, memoized behind
   the stat-only ledger fingerprint (`dashboard_memo._ledger_fingerprint`).
2. **The memo bound.** A new memo in front of a whole-namespace walk **sizes its
   own bound above the namespace count** — `_STATS_MEMO_MAX` (512) and
   `_OVERVIEW_MEMO_MAX` (128) both reached that conclusion from the same cause,
   and one shared 8-slot bound cost a memo its entire hit rate, not merely some
   of it. Borrowing `_MEMO_MAX` here would repeat a measured mistake.
3. **The single-process server.** `BaseHTTPRequestHandler` with keep-alive is
   right for a localhost panel and wrong for public ingestion. That is the real
   boundary at which a different front end is justified — and the honest trigger
   for revisiting §1's "no gateway process" conclusion. It is not justified by
   the interface being unfashionable.

## 8. Test plan

Written before the code, per PART 3:

- **LANDED** `tests/test_consensus.py`, 38 tests, written before the module.
  The two contracts against a table of refusals; the fold against fixtures where
  a weighted mean, an unnormalised count, or an ignored confidence each produce a
  DIFFERENT number, so the tests discriminate rather than describe; a canary
  proving the weight guard can fire on a record the real fold produced; and the
  duplicate-ref case, whose discriminating fixture is a record folded over three
  identical contributions checked against a ledger holding one — provable only
  as `[True, False, False]`.
- Routes — one test per route asserting the **shape** of both the accepted and
  the refused answer, and one asserting the refusal never contains the submitted
  value.
- The job — registered in `jobs`, answers 202, holds its lock, and writes
  `job_finished` before it reads as finished.
- `_get_consensus_trace` — for a fabricated consensus record, every input id
  resolves to a real event; and a mutant that drops the weights makes it red.
- `outcomes` — the consensus surface registers a probe and appears in
  `tests/test_outcomes.py::EXPECTED_SURFACES`, which is where a sixth surface
  gets named.

## 9. Explicitly out of scope

- Any PHP, `composer.json`, or Laravel directory.
- Vue, React, Livewire, Alpine, or a bundler (§1).
- A second HTTP process or a network hop between the panel and the fold (§1).
- Authentication beyond the existing session token — public ingestion is §7.3's
  boundary and needs its own decision, not a default.
- pgvector. The vector path is capability-probed and fails toward `BYTEA`; see
  `src/mindhive/pgvec.py` on `feat/pg-event-mirror-mcp`.
