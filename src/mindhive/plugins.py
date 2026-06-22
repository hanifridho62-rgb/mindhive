# src/mindhive/plugins.py
"""Scan installed Claude Code skill/plugins, keep the ones usable without API
keys or paying, score them by breadth, and produce a top-5 recommendation.

Deterministic and dependency-free: reads the on-disk plugin cache, parses the
small `plugin.json` + `SKILL.md` frontmatter by hand (no YAML dep), and returns
plain dicts that the event store and graph projection consume.
"""
from __future__ import annotations

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


def scan_and_store(cache_dir=None, top_n: int = 5, project=None) -> dict:
    """Run the evaluation and append a single plugin_catalog snapshot event."""
    payload = evaluate(cache_dir, top_n=top_n)
    ev_mod.append_event("plugin_catalog", payload, project=project)
    return payload


def latest_catalog(project=None) -> dict | None:
    """Return the most recent plugin_catalog snapshot payload, or None."""
    latest = None
    for ev in ev_mod.read_all(project):
        if ev.type == "plugin_catalog":
            latest = ev
    return latest.payload if latest else None
