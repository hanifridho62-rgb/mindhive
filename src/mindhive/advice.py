# SPDX-License-Identifier: Apache-2.0
"""SOP authorship: who may WRITE a standing procedure, and how one is LABELLED.

Blueprint phase P1d -- PDF BAB 2.3 and section 10.13.3 (Bug 3, prompt injection
through the SOP ledger), MEB `advice.py`.

## The hole

`hive sop add` writes a procedure that `sopgate` re-asserts inside the operator's
prompt and that `sop.block` / `sop.digest_block` compose into every session's
auto-loaded context. Every spawned agent has `hive` on PATH. The partition that
already exists (`mindnet.advice.is_orchestration_session`, `sop.is_agent_authored`)
reads the row's `session_id`, and `--session` is an ARGUMENT: an agent that
passes `--session s1` is, to that test, a human.

## What this module adds

* **A process-bound token.** `MINDHIVE_ORCHESTRATION_SECRET` is configured on
  the host; `MINDHIVE_PROCESS_TOKEN` is held by the processes allowed to write.
  Neither can be supplied by a CLI argument, and `scrub_agent_env` takes the
  token out of every environment this package hands to an agent or to code an
  agent wrote -- `claude_cli._child_env`, `mindnet.executor._child_env`,
  `workspace.Manager.send` (the board agents), `loop.driver.CLIDriver.run`,
  `orchestrator.resilience._run_capped` (the sandbox), `verify._stream` (a
  lane's tests are the agent's) and `research._popen` (the trainer the research
  agent edits) -- after any caller overlay, the same "forced last" rule the
  headless guard keeps. `tests/test_advice_authorship.py` counts every
  `subprocess` spawn SITE in every module, so neither a new spawning module
  nor a second launch inside a listed one can join unnoticed. What keeps the
  token is the operator's own: `rotation/supervisor.py`, whose child is the
  operator's successor session, and `workspace`'s two `claude auth` calls --
  the CLI's login and status flow, which start no session and no agent. So on
  a host with a secret, a spawned agent's `hive sop add` is refused by
  default, whatever `--session` it claims.
* **A door that can verify nothing refuses.** The dashboard's POST answers an
  HTTP client whose environment the server cannot see, so with a secret
  configured `http_door_refusal` turns a `sop` append away instead of writing
  an unstamped row -- which would read as the OPERATOR's, the strongest label,
  from the one door that checked nothing.
* **The session rule as the fallback only.** With no secret configured the
  host has not opted in, and the existing prefix rule decides -- an honest
  orchestration id is now refused at the write instead of being written and
  then withheld.
* **Three provenance labels** (`format_sop_directive`), so text that reaches a
  prompt says where it came from: a human's procedure, a token-validated one,
  or an unverified agent's tip that is never a mandatory directive.

## What it does not claim

The labels ATTRIBUTE what the record says; they are not a proof. A process that
controls its own environment has three moves nothing running as the same OS
user can stop, and each is a deliberate attack on the host rather than the
default path: it can read the inherited secret and export the token itself (its
row is then stamped, and labelled SYSTEM VALIDATED); it can BLANK the secret
for its own command, which puts that command back on the session rule where
`--session` decides (its row is unstamped, and reads as the operator's); and it
can append around the CLI altogether, because the ledger is files. What is
closed is the default path: the argument spoof the PDF names, from an agent
using the CLI it was given. The second move is pinned by a test
(`..._is_the_documented_limit`) so no document can promise more again.
"""
from __future__ import annotations

import hmac
import os

from .mindnet import advice as mn_advice
# The label-shaped-bracket rule lives with the other composing-surface
# questions, where `sop.safe_title` can reach it without importing this
# module; re-exported because it is this module's labels it protects.
from .provenance import _FORGED_LABEL, defang_labels, defang_title  # noqa: F401

__all__ = [
    "SECRET_ENV", "TOKEN_ENV", "HUMAN_OPERATOR", "ORCHESTRATOR",
    "UNTRUSTED_AGENT", "AUTHOR_TYPES", "LABEL_HUMAN", "LABEL_SYSTEM",
    "LABEL_ADVISORY", "DIRECTIVE_LABELS", "VALIDATED_TAG",
    "is_authorized_sop_author", "is_token_validated", "sop_write_refusal",
    "http_door_refusal", "write_tags", "scrub_agent_env", "classify_author",
    "defang_labels", "defang_title",
    "format_sop_directive", "directive_for", "withheld_summary",
]

SECRET_ENV = "MINDHIVE_ORCHESTRATION_SECRET"
TOKEN_ENV = "MINDHIVE_PROCESS_TOKEN"

HUMAN_OPERATOR = "human_operator"
ORCHESTRATOR = "orchestrator"
UNTRUSTED_AGENT = "untrusted_agent"
AUTHOR_TYPES = (HUMAN_OPERATOR, ORCHESTRATOR, UNTRUSTED_AGENT)

LABEL_HUMAN = "[MANDATORY SOP - HUMAN VERIFIED]"
LABEL_SYSTEM = "[SYSTEM SOP - SYSTEM VALIDATED]"
LABEL_ADVISORY = "[ADVISORY TIP - UNVERIFIED AGENT]"
DIRECTIVE_LABELS = {HUMAN_OPERATOR: LABEL_HUMAN, ORCHESTRATOR: LABEL_SYSTEM,
                    UNTRUSTED_AGENT: LABEL_ADVISORY}

# Stamped on a `sop` event by the write path when the process token matched the
# host secret. It can only LOWER a label, never raise one: a row without it that
# is not an agent's already reads as the operator's, so forging the tag through
# a raw append buys nothing the `--session` spoof had not already bought.
VALIDATED_TAG = "sop-validated"


def is_authorized_sop_author(session_id: str) -> bool:
    """Only the main orchestrator process or the human operator may write an
    active SOP (PDF 10.13.3, with review correction II.1: an unset secret must
    never reach `compare_digest("", "")`, which is True)."""
    secret = os.environ.get("MINDHIVE_ORCHESTRATION_SECRET", "")
    token = os.environ.get("MINDHIVE_PROCESS_TOKEN", "")
    if secret:
        return bool(token) and hmac.compare_digest(secret, token)
    # Fallback berbasis sesi hanya bila secret memang belum dikonfigurasi di host
    return not mn_advice.is_orchestration_session(session_id)


def is_token_validated() -> bool:
    """Whether THIS process holds the token the host secret asks for -- the
    cryptographic branch of `is_authorized_sop_author`, without the fallback.
    What the write path stamps `VALIDATED_TAG` on. Never raises."""
    secret = os.environ.get(SECRET_ENV, "")
    token = os.environ.get(TOKEN_ENV, "")
    try:
        return bool(secret) and bool(token) and hmac.compare_digest(secret, token)
    except TypeError:                   # `compare_digest` refuses non-ASCII str
        return False


def sop_write_refusal(session_id, action="add") -> str:
    """``""`` when this process may write an SOP, else the one refusal line.

    The line carries `ditolak` and names which rule refused, because the two
    have different remedies: a token mismatch is fixed on the host, an
    orchestration session is not meant to be fixed at all. Fails CLOSED -- a
    write gate that errors open is not a gate -- and never raises: a secret
    `compare_digest` cannot compare (non-ASCII text) is a refusal that says so.
    The session id is shown through `repr`, so a control character in an
    unvalidated `MINDHIVE_SESSION` cannot forge a second line."""
    try:
        if is_authorized_sop_author(session_id):
            return ""
    except TypeError:
        return (f"penulisan SOP ({action}) ditolak: {SECRET_ENV} dan "
                f"{TOKEN_ENV} harus berupa teks ASCII agar dapat dibandingkan.")
    if os.environ.get(SECRET_ENV, ""):
        return (f"penulisan SOP ({action}) ditolak: {TOKEN_ENV} tidak cocok "
                f"dengan {SECRET_ENV} -- hanya proses orkestrator utama atau "
                f"operator manusia yang memegang token boleh menulis SOP aktif.")
    shown = repr(str(session_id or ""))
    return (f"penulisan SOP ({action}) ditolak: sesi {shown} adalah sesi agen "
            f"orkestrasi -- hanya operator manusia atau orkestrator utama yang "
            f"boleh menulis SOP aktif.")


def http_door_refusal() -> str:
    """``""`` when an HTTP door may append a `sop`, else the one refusal line.

    The dashboard's POST cannot see its caller's environment, so on a host with
    a secret it can verify nothing -- whether or not the SERVER holds the token
    -- and a row it let through would land unstamped, which `classify_author`
    reads as the operator's. With no secret configured nothing changes. Never
    raises, and never carries the secret."""
    if not os.environ.get(SECRET_ENV, ""):
        return ""
    return (f"penulisan SOP lewat pintu HTTP ditolak: {SECRET_ENV} terpasang dan "
            f"permintaan HTTP tidak membawa {TOKEN_ENV} yang dapat diverifikasi "
            f"-- tulis lewat `hive sop add` dari proses yang memegang token.")


def write_tags(tags, stamp=True) -> list:
    """*tags* as a `sop` write path records them.

    A caller-supplied `VALIDATED_TAG` is dropped -- it is a claim nobody checked
    -- and the real one is appended when *stamp* is set and this process holds
    the token. ``stamp=False`` is for a surface whose caller is not this
    process: the dashboard's POST answers an HTTP client, and the server holding
    a token says nothing about who sent the request."""
    kept = [str(t) for t in (tags or ()) if t and str(t) != VALIDATED_TAG]
    if stamp and is_token_validated():
        kept.append(VALIDATED_TAG)
    return kept


def scrub_agent_env(env=None, os_name=None):
    """The environment a SPAWNED AGENT may have: *env* without the process token.

    ``None`` means "inherit": it stays ``None`` while `os.environ` holds no
    token, so every existing caller that passes no environment keeps spawning
    exactly as it did, and becomes a scrubbed copy of `os.environ` the moment
    there is a token to withhold. The name is matched case-blind on Windows,
    where the environment block is, so an overlay cannot hand the token down
    under another spelling. The secret is left in place on purpose: a child
    that sees a secret and holds no token is REFUSED, while a child that sees
    neither would fall back to the session rule the token exists to replace.
    By default, that is: a child that blanks the variable for its own command is
    on the session rule again -- the same deliberate-environment limit as
    exporting the token, stated in this module's docstring.
    *os_name* is the seam for that platform rule (default `os.name`)."""
    if env is None:
        if not any(_is_token(k, os_name) for k in os.environ):
            return None
        env = os.environ
    return {k: v for k, v in dict(env).items() if not _is_token(k, os_name)}


def _is_token(name, os_name=None) -> bool:
    text = str(name)
    windows = (os.name if os_name is None else os_name) == "nt"
    return (text.upper() if windows else text) == TOKEN_ENV


def _tags(record) -> tuple:
    tags = record.get("tags")
    return tuple(tags) if isinstance(tags, (list, tuple)) else ()


def classify_author(sop_record) -> str:
    """One of `AUTHOR_TYPES` for a folded SOP row or a sidecar row.

    Read from what the fold ACCUMULATED, never from a payload key: `sop._fold`
    builds rows from a fixed key list, so an `author_type` written into a raw
    event does not reach here. An agent wins over everything (the accumulated
    `agent_authored` flag, then the session prefix -- `sopgate.is_agent_authored`'s
    order and reason); a row ANY applied write of which was token-validated is
    the orchestrator's (`sop._fold` accumulates `validated` for the reason it
    accumulates the agent flag: the fold keeps the latest editor's tags, so one
    unstamped title-only edit would otherwise relabel a system-composed body as
    HUMAN VERIFIED; the tag is honoured beside the key as well, even a key that
    says False -- only a hand-built row can hold both, and the tag can only
    LOWER a label); the rest,
    unrecorded authorship included, is the operator's, which is the partition's
    own standing rule that absent authorship is unknown, not hostile. Anything
    that is not a row at all is the lowest trust there is."""
    if not isinstance(sop_record, dict):
        return UNTRUSTED_AGENT
    if sop_record.get("agent_authored") is True:
        return UNTRUSTED_AGENT
    try:
        if mn_advice.is_orchestration_session(sop_record.get("session_id")):
            return UNTRUSTED_AGENT
    except Exception:                                       # noqa: BLE001
        return UNTRUSTED_AGENT
    if sop_record.get("validated") is True or VALIDATED_TAG in _tags(sop_record):
        return ORCHESTRATOR
    return HUMAN_OPERATOR


def format_sop_directive(sop_record: dict) -> str:
    """Render one SOP with its provenance label (PDF 10.13.3).

    `author_type` absent or unknown is `untrusted_agent`: the caller states the
    provenance (`directive_for` derives it), and a record that states none gets
    the label that is never a mandatory directive. `content` is the PDF's key;
    `text` is this registry's, accepted so a folded row needs no re-keying."""
    record = sop_record if isinstance(sop_record, dict) else {}
    author_type = record.get("author_type", UNTRUSTED_AGENT)
    content = defang_labels(record.get("content") or record.get("text")).strip()
    if author_type == HUMAN_OPERATOR:
        return f"{LABEL_HUMAN}: {content}"
    elif author_type == ORCHESTRATOR:
        return f"{LABEL_SYSTEM}: {content}"
    # Karantina saran dari sub-agen; tidak diinjeksi sebagai direktif wajib
    return f"{LABEL_ADVISORY}: {content}"


def directive_for(row) -> str:
    """`format_sop_directive` for a folded or sidecar *row*, provenance derived
    by `classify_author` -- for a caller holding a row it has not classified.
    `sopgate.render` is not one: `plan` classifies each row once and `probe`
    reports that same record, so the fence and its probe cannot disagree."""
    body = row.get("text") if isinstance(row, dict) else ""
    return format_sop_directive({"author_type": classify_author(row),
                                 "content": body})


def withheld_summary(n) -> str:
    """The one line admitting *n* quarantined agent rows, or ``""`` for none.

    A COUNT and nothing else: this line is composed beside the operator's
    mandatory procedures, so it must not carry a byte an agent chose -- not a
    title, not an id. `sop.withheld_footer` names them on the surfaces where an
    id is safe to show; `hive sop list` is where the operator reads them."""
    try:
        count = int(n)
    except (TypeError, ValueError):
        return ""
    if count <= 0:
        return ""
    return (f"{LABEL_ADVISORY}: {count} agent-written procedure(s) withheld "
            f"from this block -- quarantined, not injected as a mandatory "
            f"directive. Read them with `hive sop list`.")
