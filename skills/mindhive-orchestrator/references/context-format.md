# CONTEXT.md — Glossary of Domain Terms

**Use this template for your Phase 3 output. Copy, fill in as you define terms.**
This is the ULT-IDEA CONTEXT.md template, preserved, with a MindHive note on
logging terms back to the ledger.

---

## Purpose

This document is the canonical glossary of all domain terms used in this project.
Every term appears exactly once with one definition. There is no ambiguity.

**Rules:**
1. One term per section
2. One definition (1-2 sentences)
3. Related terms (what's similar/opposite?)
4. Examples (when is this term used?)

**▸ MindHive note:** before minting a definition, run
`python -m mindhive recall "<term>"` — the project may already have a canonical
definition; reuse it rather than creating a conflicting one. After you resolve a
term, log it so the next run inherits the canon:
`python -m mindhive append decision "Canonical term: <term> = <definition>" --tags orchestrator,context`.

---

## [Term Name]

**Definition:** [1-2 sentence precise definition, no jargon]

**Related Terms:** [What other terms are similar or opposite?]

**Examples:** [1-2 concrete examples of when this term applies]

**When to Use:** [When is this term appropriate?]

**When NOT to Use:** [What term should you use instead?]

---

## Attendance

**Definition:** Record of which students were present in class on a given date.
Marks the moment a student enters class and marks them "present", "absent", or
"excused".

**Related Terms:** Absence (opposite), Tardy (related but different timing),
Enrollment (prerequisite — who can be marked)

**Examples:**
- Ms. Wong marks 27 students present and 1 absent at 8:00am for Period 1
- The counselor sees "Marcus was absent 4 times this month" from attendance records

**When to Use:** Talking about presence/absence records for a specific class period

**When NOT to Use:** Don't use "attendance" to mean "enrollment list" or "student roster"

---

## [Add more terms as you discover them in Phase 3]

---

## Document Metadata

**Project:** [Project Name]
**Last Updated:** [Date]
**Last Updated By:** [Name]
**Version:** 1.0

**Guidelines for updates:**
- New term? Add a section
- Definition changed? Update the definition, note the date
- Deprecated term? Mark as [DEPRECATED] but keep for reference
- Merge terms? Keep both definitions but cross-reference

---

**Notes:**
- Start with 5-10 key terms during Phase 3
- Grows to 15-30 terms as project develops
- Reference this file constantly
- Update after each ambiguity is resolved
- Mirror hard reversals and canonical terms into the MindHive ledger so recall
  surfaces them in future runs
