# Architecture Decision Record — [ADR-NNNN: Brief Title]

The ULT-IDEA ADR template, preserved, with a MindHive note on mirroring the
decision into the ledger. Write an ADR only when the Phase 3.5 three-part test
passes (hard to reverse, surprising without context, result of a real trade-off).

**Status:** Proposed | Accepted | Superseded

**Date:** [YYYY-MM-DD]

**Decision Maker(s):** [Names]

> **▸ MindHive note:** when you accept an ADR, mirror its headline into the ledger
> so recall surfaces it even before anyone opens the ADR file:
> `python -m mindhive append decision "<ADR headline>" --tags orchestrator,adr`.

---

## Context

[2-3 paragraphs: What situation forced this decision? Why now? What are the options?]

Example:
> We need to decide how agents interact with the file system. Currently, we're considering two approaches:
> 1. Agent uses primitive file tools (`read_file`, `write_file`, `list_files`) directly
> 2. Agent uses domain-specific tools (`read_document`, `write_summary`) that abstract file operations
>
> This decision affects whether new features are deployed as prompts (approach 1) or require code changes (approach 2).

---

## Decision

[1-2 sentences: The decision, stated clearly and unambiguously]

Example:
> We will use primitive file tools as the agent's primary interface, adding domain-specific tools only after confirming recurring patterns of tool composition.

---

## Rationale

[Why this decision over alternatives? What trade-offs are we accepting?]

**Why this approach:**
- Agents can handle new use cases without waiting for code deployment
- Behavior changes are prompt-based, not code-based
- Testing is simpler (fewer code paths to verify)

**Trade-offs we accept:**
- Agents may take longer to solve problems (more tool composition steps)
- Initial development is slightly slower (we're not pre-building domain tools)

**Trade-offs we avoid:**
- Tool bloat (pre-built tools that are rarely used)
- Lock-in (behavior locked in code, not available to prompts)

---

## Alternatives Considered

### Alternative 1: [Name]
[Brief description]

**Pros:** [2-3 advantages]
**Cons:** [2-3 disadvantages]

### Alternative 2: [Name]
[Brief description]

**Pros:** [2-3 advantages]
**Cons:** [2-3 disadvantages]

**Why we didn't choose this:** [Clear reason]

---

## Consequences

### Positive Outcomes
- [Outcome 1]
- [Outcome 2]
- [Outcome 3]

### Challenges & Mitigations
- **Challenge:** Agents may be slow initially (multiple tool calls per task)
  - **Mitigation:** Monitor usage patterns. Graduate to domain tools once patterns are clear.

- **Challenge:** Requires strong system prompt guidance
  - **Mitigation:** Investment in prompt engineering upfront.

---

## Related Decisions

- ADR-0001: [Related decision]
- ADR-0002: [Related decision]

(Reference other ADRs that depend on or relate to this decision. Cross-reference
the ledger too: `python -m mindhive graph related "<entity>"` surfaces connected
decisions the ADR set may not list.)

---

## Timeline

- **Proposed:** [Date]
- **Accepted:** [Date]
- **Superseded:** [Date] (if applicable)

---

## References

[Links to supporting documents, issues, discussions]
- See: `from-primitives-to-domain-tools.md`
- See: `mcp-tool-design.md`

---

## Implementation Notes

[If accepted: Any notes for implementation team]

- Tool list: [`read_file`, `write_file`, `list_files`, `store_item`, `get_item`, `delete_item`]
- System prompt emphasis: "You have primitive tools. Compose them to solve problems."
- Monitoring: Log all tool sequences to identify patterns for Phase 1 domain tool graduation

---

## Questions & Answers

**Q: Why not just use domain tools from the start?**
A: Because we don't know which domain tools we'll need. Building 5 tools speculatively is wasteful. Better to see patterns first, then build deliberately.

**Q: When will we graduate to domain tools?**
A: After 50-100 agent sessions, we analyze tool usage. If a pattern appears 10+ times, we consider a domain tool.

**Q: What if agents are too slow?**
A: We have a checkpoint to decide: wait for patterns to emerge, or invest in domain tools speculatively.

---

**Document Version:** 1.0
**Last Modified:** [Date]
