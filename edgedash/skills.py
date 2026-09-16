"""
skills.py — deterministic skill name canonicalisation and alias tooling.

canonical() and canonicalise_list() are pure functions — no LLM, no network.
The alias map in config.yaml is the single source of truth (rule 23).
Nothing in this file modifies that map automatically.

Entry points:
    python -m edgedash.skills --audit            frequency table + current mappings
    python -m edgedash.skills --suggest-aliases  ONE model call → paste-ready YAML
                                                 (read-only: writes nothing to disk)
"""

from __future__ import annotations

import re


# ---------------------------------------------------------------------------
# Normalisation pipeline
# ---------------------------------------------------------------------------

_PARENS_RE     = re.compile(r"\s*\([^)]*\)")       # strip (anything)
_MULTI_SPACE   = re.compile(r"\s+")
_LEADING_PUNCT = re.compile(r"^[^\w]+")
_TRAILING_PUNCT = re.compile(r"[^\w]+$")


def canonical(raw: str, aliases: dict[str, str]) -> str:
    """
    Normalise a raw skill string and apply the alias map.

    Steps (in order):
      1. Lowercase and strip outer whitespace.
      2. Drop parenthetical qualifiers — "kubernetes (eks)" -> "kubernetes".
      3. Collapse internal whitespace runs to a single space.
      4. Strip surrounding non-word punctuation.
      5. Look up the result in aliases; return the mapped value if found,
         otherwise return the normalised string as-is.

    Pure function. Same input always produces the same output.
    """
    if not raw:
        return ""

    s = raw.lower().strip()
    s = _PARENS_RE.sub("", s)
    s = _MULTI_SPACE.sub(" ", s)
    s = _LEADING_PUNCT.sub("", s)
    s = _TRAILING_PUNCT.sub("", s)
    s = s.strip()

    return aliases.get(s, s)


def canonicalise_list(
    skills: list[str],
    aliases: dict[str, str],
) -> list[str]:
    """Apply canonical() to a list and deduplicate while preserving order."""
    seen: set[str] = set()
    result: list[str] = []
    for raw in skills:
        c = canonical(raw, aliases)
        if c and c not in seen:
            seen.add(c)
            result.append(c)
    return result


# ---------------------------------------------------------------------------
# Audit CLI:  python -m edgedash.skills --audit
# ---------------------------------------------------------------------------

def _run_audit(db_path: str, aliases: dict[str, str]) -> None:
    import json
    import sqlite3

    conn = sqlite3.connect(db_path)
    rows = conn.execute(
        "SELECT extracted_json FROM extraction_cache"
    ).fetchall()
    conn.close()

    if not rows:
        print("No cached extractions found. Run a fetch+score cycle first.")
        return

    freq: dict[str, int] = {}
    for row in rows:
        try:
            data = json.loads(row[0])
        except (ValueError, KeyError):
            continue
        for skill in (data.get("required_skills") or []) + (data.get("nice_to_have") or []):
            s = skill.strip()
            if s:
                freq[s] = freq.get(s, 0) + 1

    if not freq:
        print("No skills found in cache.")
        return

    sorted_skills = sorted(freq.items(), key=lambda x: -x[1])
    top40         = sorted_skills[:40]
    singletons    = [s for s, c in sorted_skills if c == 1]

    W = 72
    print(f"\n{'─' * W}")
    print(f"  Skill audit — {len(rows)} cached extractions, {len(freq)} unique raw strings")
    print(f"{'─' * W}")
    print(f"\n  {'RAW SKILL':<38} {'COUNT':>5}  CANONICAL")
    print(f"  {'─'*38} {'─'*5}  {'─'*24}")

    for raw, count in top40:
        canon  = canonical(raw, aliases)
        mapped = f"→ {canon}" if canon != raw.lower().strip() else "(no change)"
        print(f"  {raw:<38} {count:>5}  {mapped}")

    if singletons:
        print(f"\n{'─' * W}")
        print(f"  Singletons ({len(singletons)}) — likely typos, junk, or full sentences:")
        print(f"  Add to skill_aliases in config.yaml if they should map to something.")
        print(f"{'─' * W}")
        for s in singletons[:60]:
            print(f"  • {s}")
        if len(singletons) > 60:
            print(f"  … and {len(singletons) - 60} more")

    print(f"\n{'─' * W}\n")


# ---------------------------------------------------------------------------
# Suggest-aliases CLI:  python -m edgedash.skills --suggest-aliases
# ---------------------------------------------------------------------------

_SUGGEST_SCHEMA = {
    "type": "array",
    "items": {
        "type": "object",
        "required": ["canonical", "variants", "confidence"],
        "properties": {
            "canonical":  {"type": "string"},
            "variants":   {"type": "array", "items": {"type": "string"}},
            "confidence": {"type": "string"},
        },
    },
}

_SUGGEST_PROMPT_TEMPLATE = """\
You are a technical vocabulary assistant. Below is a list of skill strings
extracted from job listings. Some of them refer to the same underlying skill
but are spelled or abbreviated differently.

Your task: identify groups of strings that clearly refer to the SAME skill.
Propose a canonical name for each group and list the variants.

Rules you MUST follow:
- Only group strings that are unambiguously the same skill.
- Do NOT group skills that are related but distinct (e.g. "spark" and
  "hadoop" are different; "react" and "javascript" are different).
- Do NOT group skills just because they co-occur or are in the same domain.
- If you are unsure whether two strings refer to the same thing, use
  confidence "low". High confidence means you are certain.
- Do not invent canonical names. Use the most common industry spelling.
- Strings that are already clearly distinct should not appear in any group.

Skill strings (with occurrence count):
{skill_list}

Return a JSON array. Each element:
  {{
    "canonical": "the canonical name",
    "variants":  ["variant1", "variant2", ...],
    "confidence": "high" or "low"
  }}
Only include groups with 2 or more variants. Omit singletons entirely.
"""


def _collect_unmapped_skills(
    db_path: str,
    aliases: dict[str, str],
) -> dict[str, int]:
    """
    Read extraction cache and return canonical skill strings that are NOT
    already keys in the alias map, with their occurrence counts.
    """
    import json
    import sqlite3

    conn = sqlite3.connect(db_path)
    rows = conn.execute("SELECT extracted_json FROM extraction_cache").fetchall()
    conn.close()

    raw_freq: dict[str, int] = {}
    for row in rows:
        try:
            data = json.loads(row[0])
        except (ValueError, KeyError):
            continue
        for skill in (data.get("required_skills") or []) + (data.get("nice_to_have") or []):
            s = skill.strip().lower()
            if s:
                raw_freq[s] = raw_freq.get(s, 0) + 1

    # Keep only strings not already handled by the alias map
    alias_keys = set(aliases.keys())
    return {s: c for s, c in raw_freq.items() if s not in alias_keys}


def _detect_conflicts(
    proposals: list[dict],
    aliases: dict[str, str],
) -> list[str]:
    """
    Return conflict messages for proposals that group strings the alias map
    already keeps separate (i.e. map to different canonical values).
    """
    conflicts = []
    for prop in proposals:
        # Collect what the current alias map says about each variant
        mapped: dict[str, str] = {}
        for v in prop.get("variants", []):
            if v in aliases:
                mapped[v] = aliases[v]
        # Conflict if two variants map to DIFFERENT canonical values
        unique_targets = set(mapped.values())
        if len(unique_targets) > 1:
            detail = ", ".join(f'"{v}" → "{t}"' for v, t in mapped.items())
            conflicts.append(
                f'  ⚠  CONFLICT: proposal "{prop["canonical"]}" groups strings '
                f"your alias map already separates: {detail}"
            )
    return conflicts


def _format_yaml_block(proposals: list[dict], conflicts: set[str]) -> str:
    """Render proposals as paste-ready config.yaml YAML lines."""
    lines: list[str] = []
    for prop in proposals:
        canon   = prop.get("canonical", "")
        variants = prop.get("variants", [])
        conf    = prop.get("confidence", "low")
        is_conflict = prop.get("canonical", "") in conflicts

        conf_marker = "  # HIGH confidence" if conf == "high" else "  # low confidence — review carefully"
        if is_conflict:
            conf_marker += "  # ⚠ CONFLICTS WITH YOUR EXISTING MAP"

        for v in variants:
            if v != canon:
                lines.append(f'  "{v}": "{canon}"{conf_marker}')
                conf_marker = ""  # only print comment on first line of the group

        lines.append("")  # blank line between groups

    return "\n".join(lines)


def _run_suggest_aliases(db_path: str, aliases: dict[str, str]) -> None:
    from edgedash.llm import LLMError, complete_json
    from edgedash.config import load_config

    W = 72

    unmapped = _collect_unmapped_skills(db_path, aliases)
    if not unmapped:
        print("\n  No unmapped skills found in the database.")
        print("  Either run a cycle first, or all strings are already in your alias map.\n")
        return

    # Sort by frequency desc, cap at 80 to keep prompt size reasonable
    top_skills = sorted(unmapped.items(), key=lambda x: -x[1])[:80]
    skill_list = "\n".join(f"  {s}  (×{c})" for s, c in top_skills)

    print(f"\n{'─' * W}")
    print(f"  Alias Suggestions  —  {len(top_skills)} unmapped skill strings sent to model")
    print(f"{'─' * W}")
    print()
    print("  ⚠  WARNING: These are SUGGESTIONS that require your review.")
    print("  ⚠  Merging distinct skills is worse than leaving them separate.")
    print("  ⚠  The model may be wrong. You are the final authority (rule 23).")
    print("  ⚠  Nothing is written to any file — copy what you want manually.")
    print()

    print("  Calling model … ", end="", flush=True)
    try:
        cfg = load_config()
        proposals: list[dict] = complete_json(
            _SUGGEST_PROMPT_TEMPLATE.format(skill_list=skill_list),
            _SUGGEST_SCHEMA,
            config=cfg,
        )
    except LLMError as exc:
        print(f"FAILED\n\n  LLM error: {exc}\n")
        return

    if not isinstance(proposals, list):
        print(f"FAILED\n\n  Unexpected response shape: {type(proposals)}\n")
        return

    print(f"OK  ({len(proposals)} groupings proposed)")
    print()

    # Detect conflicts with existing alias map
    conflicts_msgs = _detect_conflicts(proposals, aliases)
    conflict_canonicals: set[str] = set()
    if conflicts_msgs:
        print(f"{'─' * W}")
        print("  CONFLICTS WITH YOUR EXISTING ALIAS MAP:")
        for msg in conflicts_msgs:
            print(msg)
            # Extract canonical name from the message for flagging
            try:
                conflict_canonicals.add(msg.split('"')[1])
            except IndexError:
                pass
        print(f"{'─' * W}")
        print()

    # Print paste-ready YAML
    print(f"{'─' * W}")
    print("  Paste the lines you agree with into the skill_aliases section")
    print("  of config.yaml. Leave out anything you disagree with.")
    print(f"{'─' * W}")
    print()
    print("  # --- SUGGESTED ALIASES (review before adding) ---")

    for prop in proposals:
        canon    = prop.get("canonical", "")
        variants = prop.get("variants", [])
        conf     = prop.get("confidence", "low")
        flag     = "  # ⚠ CONFLICTS WITH YOUR MAP" if canon in conflict_canonicals else ""
        conf_tag = "HIGH" if conf == "high" else "low"

        print(f"\n  # {canon}  [{conf_tag} confidence]{flag}")
        for v in variants:
            if v and v != canon:
                print(f'  "{v}": "{canon}"')

    print(f"\n{'─' * W}\n")
    import argparse
    from edgedash.config import load_config

    parser = argparse.ArgumentParser(description="Skill canonicalisation audit tool.")
    parser.add_argument("--audit", action="store_true",
                        help="Print skill frequency + canonical mappings from the DB.")
    parser.add_argument("--suggest-aliases", action="store_true",
                        help="Ask the LLM to propose alias groupings (read-only, one call).")
    args = parser.parse_args()

    cfg = load_config()

    if args.audit:
        _run_audit(cfg.db_path, cfg.skill_aliases)
    elif args.suggest_aliases:
        _run_suggest_aliases(cfg.db_path, cfg.skill_aliases)
    else:
        parser.print_help()
