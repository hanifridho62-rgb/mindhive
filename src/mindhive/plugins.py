# src/mindhive/plugins.py
"""Scan installed Claude Code skill/plugins, keep the ones usable without API
keys or paying, score them by breadth, and produce a top-5 recommendation.

Deterministic and dependency-free: reads the on-disk plugin cache, parses the
small `plugin.json` + `SKILL.md` frontmatter by hand (no YAML dep), and returns
plain dicts that the event store and graph projection consume.
"""
from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

from . import events as ev_mod, paths

# The sole AI behind this evaluation. The curated notes below are authored by
# Claude Opus 4.8 and the snapshot is stamped with this id for provenance; no
# other model is ever consulted (the scan/score pipeline is fully deterministic).
ENGINE = "claude-opus-4-8"

# Breadth weight: distinct-topic spread is worth this many "extra triggers".
# Kept as a named constant so the ranking is easy to tune and audit.
BREADTH_WEIGHT = 1.0

# Curated topic vocabulary. A skill trigger "covers" a topic if the keyword
# appears (word-ish) in its description. Spread = number of distinct topics hit.
TOPIC_KEYWORDS = {
    "test", "security", "frontend", "backend", "data", "deploy", "debug",
    "docs", "performance", "infra", "api", "mobile", "ml", "review", "git",
    "database", "cloud", "kubernetes", "refactor", "design", "auth", "graph",
}

# Dependencies that imply a paid/keyed external model API.
PAID_SDK_DEPS = {"openai", "anthropic", "cohere", "mistralai", "google-generativeai"}

# Secret-shaped config keys that mean the plugin needs a key/token to work.
_SECRET_RE = re.compile(r"(_API_KEY|_TOKEN|_SECRET|_KEY)$", re.IGNORECASE)

# Curated "Claude's recommendations" — authored at build time, printed at
# runtime with zero API tokens. Keyed by plugin name. Missing -> "".
CURATED_NOTES = {
    "superpowers": "Process backbone — brainstorming, planning, TDD, and "
                   "verification skills that shape how every other task runs.",
    "compound-engineering": "Deep bench of review/debug/plan skills; the "
                            "code-review and debug flows pay for themselves fast.",
    "anthropic-skills": "Broadest general toolkit — docs, formats, web artifacts, "
                        "and engineering patterns covering most everyday work.",
    "python-development": "Focused, high-signal Python skills (style, testing, "
                          "packaging, async) that match this very codebase.",
    "git-pr-workflows": "Streamlines the commit/PR loop you run constantly; "
                        "no external service required.",
    "backend-development": "Strong API/architecture/event-sourcing skills — "
                           "directly relevant to MIND HIVE's own design.",
    "debugging-toolkit": "Systematic debugging and DX skills usable on any stack.",
    "ui-design": "Comprehensive design-system and accessibility skills for any UI work.",
}


def _read_json(path: Path):
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except Exception:
        return None


def _parse_frontmatter(text: str) -> dict:
    """Extract simple `key: value` pairs from a leading --- frontmatter block.

    SKILL.md frontmatter is shallow (name + description), so a hand parser is
    enough and avoids a YAML dependency. Values are single-line.
    """
    if not text.startswith("---"):
        return {}
    end = text.find("\n---", 3)
    if end == -1:
        return {}
    block = text[3:end]
    out = {}
    for line in block.splitlines():
        m = re.match(r"\s*([A-Za-z0-9_-]+)\s*:\s*(.*?)\s*$", line)
        if m:
            out[m.group(1).lower()] = m.group(2).strip().strip('"').strip("'")
    return out


def _strip_frontmatter(text: str) -> str:
    """Return the markdown body with any leading `---` frontmatter block removed."""
    if text.startswith("---"):
        end = text.find("\n---", 3)
        if end != -1:
            nl = text.find("\n", end + 1)
            text = text[nl + 1:] if nl != -1 else ""
    return text.strip()


def _semver_key(version: str):
    parts = re.findall(r"\d+", version or "")
    return tuple(int(p) for p in parts) or (0,)


def _skills_for(plugin_dir: Path) -> list[dict]:
    skills = []
    sdir = plugin_dir / "skills"
    if not sdir.is_dir():
        return skills
    for skill_md in sorted(sdir.glob("*/SKILL.md")):
        fm = _parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore"))
        name = fm.get("name") or skill_md.parent.name
        trigger = fm.get("description", "")
        skills.append({"name": name, "trigger": trigger})
    return skills


def scan(cache_dir=None) -> list[dict]:
    """Walk the plugin cache and return one dict per plugin (newest version)."""
    root = Path(cache_dir) if cache_dir else (Path.home() / ".claude" / "plugins" / "cache")
    if not root.is_dir():
        return []

    # marketplace/plugin -> (semver_key, plugin dict)
    best: dict[tuple[str, str], tuple] = {}

    for manifest in root.glob("*/*/*/.claude-plugin/plugin.json"):
        version_dir = manifest.parent.parent          # <cache>/<mkt>/<plugin>/<ver>
        plugin_slug = version_dir.parent.name
        marketplace = version_dir.parent.parent.name
        if marketplace.startswith("temp_local_"):
            continue
        data = _read_json(manifest)
        if not isinstance(data, dict):
            continue

        version = str(data.get("version") or version_dir.name)
        plugin = {
            "marketplace": marketplace,
            "name": data.get("name") or plugin_slug,
            "version": version,
            "description": data.get("description", ""),
            "_config_required": (data.get("configuration") or {}).get("required") or {},
            "_mcp_servers": data.get("mcpServers") or {},
            "_dependencies": data.get("dependencies") or [],
            "skills": _skills_for(version_dir),
        }
        key = (marketplace, plugin["name"])
        sk = _semver_key(version)
        if key not in best or sk > best[key][0]:
            best[key] = (sk, plugin)

    return [p for _sk, p in sorted(best.values(), key=lambda t: (t[1]["marketplace"], t[1]["name"]))]


def is_free(plugin: dict) -> tuple[bool, str]:
    """Return (free, reason). Free = no API key, no auth-gated MCP, no paid SDK."""
    for k in (plugin.get("_config_required") or {}):
        if _SECRET_RE.search(k):
            return False, f"requires secret config {k}"
    if plugin.get("_mcp_servers"):
        servers = ", ".join(sorted(plugin["_mcp_servers"])) or "MCP server"
        return False, f"auth-gated MCP server ({servers})"
    deps = plugin.get("_dependencies") or []
    for d in deps:
        base = re.split(r"[<>=!~ \[]", str(d), maxsplit=1)[0].strip().lower()
        if base in PAID_SDK_DEPS:
            return False, f"depends on paid-API SDK {base}"
    return True, "no API key or account required"


def _topics(plugin: dict) -> set[str]:
    blob = " ".join(s.get("trigger", "") for s in plugin.get("skills", [])).lower()
    return {kw for kw in TOPIC_KEYWORDS if kw in blob}


def score(plugin: dict) -> dict:
    trigger_count = len(plugin.get("skills", []))
    topic_spread = len(_topics(plugin))
    breadth = trigger_count + topic_spread * BREADTH_WEIGHT
    return {
        "trigger_count": trigger_count,
        "topic_spread": topic_spread,
        "breadth_score": breadth,
    }


def _public(plugin: dict, free: bool, reason: str) -> dict:
    """Strip private (_-prefixed) scan fields; attach free flag + score."""
    out = {
        "marketplace": plugin["marketplace"],
        "name": plugin["name"],
        "version": plugin["version"],
        "description": plugin["description"],
        "free": free,
        "free_reason": reason,
        "skills": plugin.get("skills", []),
        "topics": sorted(_topics(plugin)),
        "score": score(plugin),
    }
    return out


def evaluate(cache_dir=None, top_n: int = 5) -> dict:
    """Full pipeline: scan -> classify -> score -> recommend. Returns the
    snapshot payload (also what gets stored as the plugin_catalog event)."""
    raw = scan(cache_dir)
    free_plugins, paid_plugins = [], []
    skills_total = 0
    for p in raw:
        free, reason = is_free(p)
        skills_total += len(p.get("skills", []))
        pub = _public(p, free, reason)
        if free:
            free_plugins.append(pub)
        else:
            paid_plugins.append({"name": pub["name"], "version": pub["version"],
                                 "free_reason": reason})

    ranked = sorted(
        free_plugins,
        key=lambda p: (-p["score"]["breadth_score"], p["name"]),
    )
    recommendations = [
        {
            "name": p["name"],
            "breadth_score": p["score"]["breadth_score"],
            "note": CURATED_NOTES.get(p["name"], ""),
        }
        for p in ranked[:top_n]
    ]

    return {
        "scanned_at": ev_mod.now_iso(),
        "engine": ENGINE,
        "cache_dir": str(Path(cache_dir) if cache_dir
                         else Path.home() / ".claude" / "plugins" / "cache"),
        "counts": {
            "plugins_total": len(raw),
            "free": len(free_plugins),
            "paid": len(paid_plugins),
            "skills_total": skills_total,
        },
        "free_plugins": ranked,
        "paid_plugins": sorted(paid_plugins, key=lambda p: p["name"]),
        "recommendations": recommendations,
    }


# --------------------------------------------------------------------------
# Live inventory — registry-driven view of what is installed RIGHT NOW.
# The cache-walking scan() above keeps powering the free/paid evaluation, but
# the cache can lag reality (uninstalled plugins leave folders behind). The
# inventory instead trusts installed_plugins.json — the file Claude itself
# rewrites on every install/uninstall (including local-panel uploads) — plus
# the standalone ~/.claude/skills directory.
# --------------------------------------------------------------------------

def _claude_dir(claude_dir=None) -> Path:
    return Path(claude_dir) if claude_dir else (Path.home() / ".claude")


def _read_registry(claude_dir=None) -> dict:
    data = _read_json(_claude_dir(claude_dir) / "plugins" / "installed_plugins.json")
    plugs = (data or {}).get("plugins")
    return plugs if isinstance(plugs, dict) else {}


def inventory(claude_dir=None) -> dict:
    """Snapshot of every installed plugin (with its skills) + local skills."""
    plugins_out = []
    for key, entries in sorted(_read_registry(claude_dir).items()):
        name, _, marketplace = key.rpartition("@")
        if not name:                       # malformed key like "justaname"
            name, marketplace = marketplace, ""
        # several scopes may hold the same plugin — newest version wins
        entry = max((e for e in entries if isinstance(e, dict)),
                    key=lambda e: _semver_key(str(e.get("version", ""))),
                    default=None)
        if entry is None:
            continue
        install = Path(str(entry.get("installPath", "")))
        present = install.is_dir()
        manifest = _read_json(install / ".claude-plugin" / "plugin.json") or {}
        plugins_out.append({
            "marketplace": marketplace,
            "name": name,
            "version": str(entry.get("version", "")),
            "scope": entry.get("scope", ""),
            "description": manifest.get("description", ""),
            "present": present,
            "skills": _skills_for(install) if present else [],
        })
    plugins_out.sort(key=lambda p: (p["marketplace"], p["name"]))
    # ~/.claude/skills/*/SKILL.md has the same layout as a plugin's skills dir,
    # so the one canonical reader covers standalone local skills too
    local = _skills_for(_claude_dir(claude_dir))
    plugin_skills = sum(len(p["skills"]) for p in plugins_out)
    return {
        "generated_at": ev_mod.now_iso(),
        "plugins": plugins_out,
        "local_skills": local,
        "counts": {
            "plugins": len(plugins_out),
            "plugin_skills": plugin_skills,
            "local_skills": len(local),
            "skills_total": plugin_skills + len(local),
        },
    }


def inventory_signature(claude_dir=None) -> str:
    """Cheap fingerprint of everything the inventory depends on. Any install,
    uninstall, or skill add/delete changes it; identical disk state yields an
    identical signature, so pollers can skip the full rescan."""
    root = _claude_dir(claude_dir)
    parts = []

    def stat_of(p: Path):
        try:
            st = p.stat()
            parts.append(f"{p}|{st.st_mtime_ns}|{st.st_size}")
        except OSError:
            parts.append(f"{p}|missing")

    stat_of(root / "plugins" / "installed_plugins.json")
    for skill_md in sorted((root / "skills").glob("*/SKILL.md")):
        stat_of(skill_md)
    # the set of SKILL.md paths inside each installed plugin catches a skill
    # folder deleted from an install without any registry change
    for entries in _read_registry(claude_dir).values():
        for e in entries:
            if isinstance(e, dict) and e.get("installPath"):
                sdir = Path(str(e["installPath"])) / "skills"
                parts.extend(str(p) for p in sorted(sdir.glob("*/SKILL.md")))
    return hashlib.sha256("\n".join(parts).encode("utf-8")).hexdigest()


# per-claude-dir cache: signature of the last build -> its inventory payload
_INV_CACHE: dict[str, tuple[str, dict]] = {}


def live_inventory(claude_dir=None) -> dict:
    """Signature-gated inventory: rebuild only when the disk actually changed.
    Cheap enough for the dashboard to poll every couple of seconds."""
    key = str(_claude_dir(claude_dir))
    sig = inventory_signature(claude_dir)
    cached = _INV_CACHE.get(key)
    if cached and cached[0] == sig:
        return {"changed": False, "signature": sig, "inventory": cached[1]}
    payload = inventory(claude_dir)
    _INV_CACHE[key] = (sig, payload)
    return {"changed": True, "signature": sig, "inventory": payload}


def _newest_entry(entries: list) -> dict | None:
    return max((e for e in entries if isinstance(e, dict)),
               key=lambda e: _semver_key(str(e.get("version", ""))), default=None)


def _find_skill_md(sdir: Path, name: str) -> Path | None:
    """The SKILL.md under sdir whose frontmatter name (or folder) matches name."""
    for skill_md in sorted(sdir.glob("*/SKILL.md")):
        fm = _parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore"))
        if name in ((fm.get("name") or ""), skill_md.parent.name):
            return skill_md
    return None


def _mtime_iso(path: Path) -> str:
    import time
    try:
        return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(path.stat().st_mtime))
    except OSError:
        return ""


def skill_detail(name: str, plugin: str | None = None, claude_dir=None) -> dict | None:
    """Full click-through detail for one skill: intended use (its description),
    when it was added, and the full SKILL.md body. Returns None if not found.

    A plugin skill dates from the registry's ``installedAt``; a standalone local
    skill dates from its SKILL.md mtime (there is no registry row for it)."""
    if plugin:
        for key, entries in _read_registry(claude_dir).items():
            pname, _, _mkt = key.rpartition("@")
            if (pname or _mkt) != plugin:
                continue
            entry = _newest_entry(entries)
            if not entry:
                return None
            install = Path(str(entry.get("installPath", "")))
            skill_md = _find_skill_md(install / "skills", name)
            if not skill_md:
                return None
            fm = _parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore"))
            return {
                "name": fm.get("name") or skill_md.parent.name,
                "intended_use": fm.get("description", ""),
                "added_at": str(entry.get("installedAt", "")),
                "body": _strip_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore")),
                "source": plugin,
            }
        return None

    skill_md = _find_skill_md(_claude_dir(claude_dir) / "skills", name)
    if not skill_md:
        return None
    fm = _parse_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore"))
    return {
        "name": fm.get("name") or skill_md.parent.name,
        "intended_use": fm.get("description", ""),
        "added_at": _mtime_iso(skill_md),
        "body": _strip_frontmatter(skill_md.read_text(encoding="utf-8", errors="ignore")),
        "source": "local",
    }


def call_status(project=None) -> dict:
    """Last-outcome per skill from the ledger: ``{name: "ok"|"fail"}``.

    Reads ``invoke_result`` (ok) and ``tool_failed`` with a ``skill:<name>``
    signature (fail). ``read_all`` is time-then-append ordered, so iterating and
    overwriting leaves each skill at its most recent call — the green/red the
    dashboard paints. Skills never called are simply absent (rendered grey)."""
    status: dict[str, str] = {}
    for e in ev_mod.read_all(project):
        if e.type == "invoke_result":
            name = e.payload.get("name")
            if name:
                status[name] = "ok"
        elif e.type == "tool_failed":
            sig = e.payload.get("signature", "")
            if isinstance(sig, str) and sig.startswith("skill:"):
                status[sig[len("skill:"):]] = "fail"
    return status


def call_history(name, project=None, limit=20) -> list[dict]:
    """Past invoke outcomes for one skill, newest first: a list of
    ``{ts, ok, text, session}``. Reads the same ``invoke_result`` / ``tool_failed``
    events ``call_status`` derives from, but keeps every occurrence so the
    dashboard can show a skill's call log. ``read_all`` is chronological, so
    reversing yields newest-first with append order preserved on ties."""
    sig = f"skill:{name}"
    out = []
    for e in ev_mod.read_all(project):
        if e.type == "invoke_result" and e.payload.get("name") == name:
            out.append({"ts": e.ts, "ok": True,
                        "text": e.payload.get("text", ""), "session": e.session_id})
        elif e.type == "tool_failed" and e.payload.get("signature") == sig:
            out.append({"ts": e.ts, "ok": False,
                        "text": e.payload.get("text", ""), "session": e.session_id})
    out.reverse()
    return out[:max(0, limit)]


# A passive `skill_used` capture and an explicit record (invoke_result or a
# `skills record` tool_failed) can describe the SAME execution — hookcapture
# sees the Skill tool fire while the agent/orchestrator writes its own verdict
# moments later. Within this window (same skill, same session) the explicit
# record wins and the pair counts once.
DEDUP_WINDOW_SECS = 120


def _stat_secs(ts):
    """Epoch seconds for a store ISO timestamp, or None when unparseable."""
    import calendar
    import time as _time
    try:
        return calendar.timegm(_time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ"))
    except (TypeError, ValueError):
        return None


def _skill_calls(evs) -> list[dict]:
    """Normalize the three skill-telemetry event sources into one call list
    (chronological — read_all is already ordered): ``skill_used`` (passive hook
    capture), ``invoke_result`` (ok), and ``tool_failed`` with a ``skill:*``
    signature (fail). Explicit records tagged ``agent-assessed`` keep that
    provenance (KTD5 — self-grades are never presented as measured)."""
    calls = []
    for e in evs:
        p = e.payload if isinstance(e.payload, dict) else {}
        if e.type == "skill_used":
            name = p.get("name")
            if not name:
                continue
            calls.append({"name": name, "ts": e.ts, "ok": bool(p.get("ok", True)),
                          "session": e.session_id, "explicit": False,
                          "provenance": "passive", "source": "skill_used"})
        elif e.type == "invoke_result":
            name = p.get("name")
            if not name:
                continue
            prov = "agent-assessed" if "agent-assessed" in (e.tags or []) else "invoke"
            calls.append({"name": name, "ts": e.ts, "ok": True,
                          "session": e.session_id, "explicit": True,
                          "provenance": prov, "source": "invoke_result"})
        elif e.type == "tool_failed":
            sig = p.get("signature", "")
            if not (isinstance(sig, str) and sig.startswith("skill:")):
                continue
            prov = "agent-assessed" if "agent-assessed" in (e.tags or []) else "invoke"
            calls.append({"name": sig[len("skill:"):], "ts": e.ts, "ok": False,
                          "session": e.session_id, "explicit": True,
                          "provenance": prov, "source": "tool_failed"})
    return calls


def _dedup_calls(calls: list[dict]) -> list[dict]:
    """Drop each passive capture that an explicit record already covers: same
    skill, same session, timestamps within DEDUP_WINDOW_SECS. Greedy 1:1
    pairing — one explicit record retires at most one passive capture, so two
    genuine executions never collapse into one."""
    explicit_pool: dict[tuple, list[dict]] = {}
    for c in calls:
        if c["explicit"]:
            explicit_pool.setdefault((c["name"], c["session"]), []).append(c)
    paired: set[int] = set()
    kept = []
    for c in calls:
        if not c["explicit"]:
            cs = _stat_secs(c["ts"])
            mate = None
            for x in explicit_pool.get((c["name"], c["session"]), []):
                if id(x) in paired:
                    continue
                xs = _stat_secs(x["ts"])
                if cs is not None and xs is not None \
                        and abs(cs - xs) <= DEDUP_WINDOW_SECS:
                    mate = x
                    break
            if mate is not None:
                paired.add(id(mate))
                continue                    # explicit record wins; passive drops
        kept.append(c)
    return kept


def skill_stats(project=None, evs=None) -> dict:
    """Per-skill telemetry counters from the ledger, keyed by skill name:
    ``{calls, ok, fail, completion_rate, last_outcome, last_used, streak,
    provenance, sources}``.

    The rate is a COMPLETION rate (ran without a tool error), never a success
    rate (KTD5); it is None when a skill has zero calls. ``provenance`` breaks
    calls down by capture path (``passive`` hook capture / ``invoke`` channel /
    ``agent-assessed`` `skills record` write-throughs); ``streak`` is the
    trailing run of failure-free calls (the completion record the recommend
    tie-break reads). Pass evs to reuse an events.read_all() result."""
    if evs is None:
        evs = ev_mod.read_all(project)
    out: dict[str, dict] = {}
    for c in _dedup_calls(_skill_calls(evs)):
        s = out.setdefault(c["name"], {
            "calls": 0, "ok": 0, "fail": 0, "completion_rate": None,
            "last_outcome": None, "last_used": None, "streak": 0,
            "provenance": {}, "sources": []})
        s["calls"] += 1
        if c["ok"]:
            s["ok"] += 1
            s["streak"] += 1
        else:
            s["fail"] += 1
            s["streak"] = 0
        s["last_outcome"] = "ok" if c["ok"] else "fail"
        s["last_used"] = c["ts"]
        s["provenance"][c["provenance"]] = s["provenance"].get(c["provenance"], 0) + 1
        if c["source"] not in s["sources"]:
            s["sources"].append(c["source"])
    for s in out.values():
        if s["calls"]:
            s["completion_rate"] = s["ok"] / s["calls"]
        s["sources"].sort()
    return out


def _catalog_path(project=None) -> Path:
    """Sidecar file holding the single latest plugin catalog snapshot."""
    return paths.project_dir(project) / "plugin_catalog.json"


def _write_catalog(payload: dict, project=None) -> None:
    p = _catalog_path(project)
    p.parent.mkdir(parents=True, exist_ok=True)
    tmp = p.with_name(p.name + ".tmp")
    tmp.write_text(ev_mod.canonical_json(payload), encoding="utf-8")
    tmp.replace(p)  # atomic overwrite-in-place — no unbounded growth


def scan_and_store(cache_dir=None, top_n: int = 5, project=None) -> dict:
    """Run the evaluation and overwrite the latest-catalog sidecar.

    The plugin catalog is DERIVED, ephemeral data (a snapshot of what's on disk),
    not a memory — so it lives in a single overwrite-in-place sidecar rather than
    the append-only, hash-chained event ledger. Putting it in the ledger would
    bloat the chain by one snapshot per daily autoscan forever, with no way to
    prune (pruning would break the hash chain). The sidecar sidesteps that
    entirely while keeping the latest snapshot instantly available."""
    payload = evaluate(cache_dir, top_n=top_n)
    _write_catalog(payload, project=project)
    return payload


def latest_catalog(project=None) -> dict | None:
    """Return the latest plugin catalog snapshot payload, or None if never scanned."""
    p = _catalog_path(project)
    if not p.exists():
        return None
    try:
        return json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return None


def _age_hours(ts: str):
    """Hours elapsed since a UTC ts ('YYYY-MM-DDTHH:MM:SSZ'), or None if the
    timestamp can't be parsed (treated as stale by callers)."""
    import calendar
    import time as _time
    try:
        t = _time.strptime(ts, "%Y-%m-%dT%H:%M:%SZ")
        return (_time.time() - calendar.timegm(t)) / 3600.0
    except Exception:
        return None


def ensure_fresh(cache_dir=None, top_n: int = 5, max_age_hours: float = 24.0,
                 force: bool = False, project=None) -> dict:
    """Auto-refresh the catalog only when it's missing or stale — cheap enough
    to call on every session start.

    Rescans (overwriting the sidecar) when there is no snapshot, when the
    snapshot's ``scanned_at`` is older than ``max_age_hours``, or when ``force``
    is set. Otherwise it returns the existing snapshot without scanning.

    Returns ``{"scanned": bool, "age_hours": float|None, "payload": dict}``.
    The shared store means a refresh by one session is immediately seen by every
    other session.
    """
    payload = None if force else latest_catalog(project)
    if payload is not None:
        age = _age_hours(payload.get("scanned_at", ""))
        if age is not None and age < max_age_hours:
            return {"scanned": False, "age_hours": age, "payload": payload}
    payload = scan_and_store(cache_dir=cache_dir, top_n=top_n, project=project)
    return {"scanned": True, "age_hours": 0.0, "payload": payload}
