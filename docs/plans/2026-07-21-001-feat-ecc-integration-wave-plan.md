# ECC integration wave — implementation plan

> Approved scope (user, 2026-07-21): four items from the ECC×MindHive synthesis
> (`C:/Users/hanif/ECC/.ecc-review/03-mindhive-integration.md`). All code lands
> test-first; the full suite must be green; every change-set gets a `hive log`.

## 1. GateGuard port → static policy tier in `enforce.py`

New module `src/mindhive/gateguard.py` — pure, dependency-free Python ports of
three ECC PreToolUse checks (sources: `gateguard-fact-force.js`,
`block-no-verify.js`, `config-protection.js` in the ECC clone):

- `is_destructive_command(cmd)` — quote-aware destructive classifier:
  `rm -rf` (combined/split/long flags), destructive git (`reset --hard`,
  `checkout --`/`.`/`-f`, `clean -f*`, `push --force`/`+refspec` without
  `--force-with-lease`, `commit --amend`, `rm -r`, `switch -C/--discard-changes/-f`),
  `find … -exec rm|rmdir|unlink|git reset --hard`, SQL/dd phrases
  (`drop table`, `delete from`, `truncate`, `dd if=`) on quote-stripped text,
  subshell/backtick/`sh -c` recursion, newline separators, quoted command words.
- `check_no_verify(cmd)` — git hook-bypass detection: `--no-verify` on
  commit/push/merge/cherry-pick/rebase/am (with commit's value-consuming
  options skipped so `-m "use --no-verify"` never false-positives; `-n`
  shorthand for commit), plus `-c core.hooksPath=` overrides (case-insensitive).
- `protected_config_file(tool_name, tool_input)` — linter/formatter config
  protection for Edit/Write/NotebookEdit (ECC's basename set; first-time
  creation allowed; non-ENOENT stat errors fail closed).

`enforce.py` gains a POLICY tier consulted by `hook_decision` **before** the
learned matchers: `POLICY_CHECKS = (id, decision, fn)` with decisions
`ask` (destructive commands — escalate to the human, don't hard-block) and
`deny` (no-verify, config-protection). Default-enabled — the user's explicit
adoption is the opt-in; per-id override in `rules/enforce.json` under
`policy:<id>`. Fail-open, `ENFORCED_MARK` on every reason.

Zero new hook registrations: rides the existing `pre_tool_use` dispatcher
(matcher `Grep|Bash|PowerShell`). Config-protection activates for direct
Edit/Write only if the matcher is later widened (opt-in, noted below).

## 2. Structured failure capture → `post_tool_use_failure` stage

- `hookcapture.categorize_failure(text)` → `{category, retryable}`
  (permission_denied, not_found, timeout, network, syntax, rejected, error).
- `hookcapture.record_post_tool_use_failure(raw)` — parses a
  PostToolUseFailure payload defensively, filters infra noise via
  `failures._is_infra_noise`, maps rejections to `judgment:rejected:*`
  signatures (KTD4, same as transcript capture), and appends `tool_failed`
  with the machine-readable payload `{text, signature, action, tool,
  category, retryable}` — fixing the 260-unparseable-events defect class
  (SOP 2.1). Idempotent vs transcript back-harvest: `failures.capture_dir`
  dedups on (session, signature, action), which the live event pre-seeds.
- `hive hook post_tool_use_failure` stage + `hooks/post_tool_use_failure.ps1`
  shim. Live registration in `~/.claude/settings.json` happens only after this
  branch is merged into the checkout the shim path points at (follow-up note).

## 3. Context budget enforcement

- `context.load_budget/save_budget` — persisted per-namespace budget sidecar
  (`context_budget.json`, same operator-config pattern as `rules/enforce.json`).
- `collect_claude` additionally measures `~/.claude/skills/*/SKILL.md` trigger
  descriptions (the exact path adopted skills arrive through — previously
  invisible to the analyzer).
- CLI: `hive context --set-budget N` persists; plain `hive context` uses the
  persisted budget automatically; `hive context --check` exits 1 when over
  budget (the enforcement hook for scripts/CI/adoption flows). Read-only
  reporting semantics otherwise unchanged.

## 4. Adopt 15 ECC skills

Prerequisite: `plugins._parse_frontmatter` learns YAML block scalars
(`>`, `>-`, `|`, `|-`) so folded descriptions parse (16/278 ECC skills).

Then copy these 15 from `C:/Users/hanif/ECC/skills/` into `~/.claude/skills/`
with an `ecc-` name prefix (uniform provenance, zero collision risk — bare-name
telemetry keying + verified `security-review`/`deep-research` collisions):
loop-design-check, agent-harness-construction, agent-architecture-audit,
agent-introspection-debugging, continuous-agent-loop, eval-harness, benchmark,
agent-eval, benchmark-optimization-loop, agent-self-evaluation,
token-budget-advisor, cost-aware-llm-pipeline, skill-scout, skill-stocktake,
iterative-retrieval. Frontmatter `name:` rewritten to the prefixed name.

Verification: measure `hive context` before, set budget = baseline + 10%,
adopt, `hive context --check` must pass; full pytest suite green; `hive log`
entries recorded.

## Follow-ups deliberately out of scope (registration touches live config)

- Adding `PostToolUseFailure` to `~/.claude/settings.json` (after merge).
- Widening the PreToolUse matcher to include `Edit|Write` for config
  protection (~0.5 s/edit hook-latency cost — user's call).
