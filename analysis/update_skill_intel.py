#!/usr/bin/env python3
"""
Generate a recent developments supplement for the ca-housing-law skill
from intel feed hits. Runs after intel_feed.py in the pipeline.

Reads: data/intel/intel-*.json (all daily intel files)
Writes: ~/.claude/skills/ca-housing-law/recent-developments.md

The supplement is loaded alongside SKILL.md, keeping the core skill
hand-curated while auto-incorporating new enforcement actions,
precedents, and policy developments.
"""

import json
from collections import defaultdict
from datetime import datetime, timedelta
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
DATA_DIR = REPO_ROOT / "data"
INTEL_DIR = DATA_DIR / "intel"
SKILL_DIR = REPO_ROOT / ".claude" / "skills" / "ca-housing-law"
OUTPUT_PATH = SKILL_DIR / "recent-developments.md"

MAX_AGE_DAYS = 90


def load_all_hits():
    """Load all intel hits from all daily files."""
    hits = []
    for f in sorted(INTEL_DIR.glob("intel-*.json")):
        try:
            items = json.loads(f.read_text())
            for item in items:
                item["_file_date"] = f.stem.replace("intel-", "")
                hits.append(item)
        except (json.JSONDecodeError, Exception):
            continue
    return hits


def categorize_hit(hit):
    """Categorize a hit by type based on source and keywords."""
    source = hit.get("source", "").lower()
    title = hit.get("title", "").lower()
    keywords = [k.lower() for k in hit.get("matched_keywords", [])]
    reason = hit.get("relevance_reason", "").lower()

    if any(k in keywords for k in ["haa violation", "haa penalty", "attorney general", "ag housing"]):
        return "enforcement"
    if any(k in keywords for k in ["builder's remedy", "builders remedy", "appeal bond"]):
        return "case_law"
    if any(k in keywords for k in ["sb 79", "sb79", "tier classification", "tod stop", "dedicated bus lane"]):
        return "sb79"
    if any(k in keywords for k in ["coastal act", "lcp amendment", "density bonus coastal"]):
        return "coastal"
    if any(k in keywords for k in ["rhna", "housing element", "prohousing"]):
        return "compliance"
    if "calhdf" in source or "yimby" in source:
        return "enforcement"
    if hit.get("detection") == "direct":
        return "oceanside"
    return "other"


def generate_supplement(hits):
    """Generate the recent developments markdown."""
    cutoff = datetime.now() - timedelta(days=MAX_AGE_DAYS)

    recent = []
    for h in hits:
        file_date = h.get("_file_date", "")
        if file_date:
            try:
                dt = datetime.strptime(file_date, "%Y-%m-%d")
                if dt < cutoff:
                    continue
            except ValueError:
                pass
        recent.append(h)

    if not recent:
        return None

    categorized = defaultdict(list)
    for h in recent:
        cat = categorize_hit(h)
        categorized[cat].append(h)

    lines = [
        "# CA Housing Law — Recent Developments",
        f"*Auto-generated from intel feed. {len(recent)} relevant items in last {MAX_AGE_DAYS} days. Updated {datetime.now().strftime('%Y-%m-%d')}.*",
        "",
    ]

    section_names = {
        "oceanside": "Oceanside Direct Mentions",
        "enforcement": "Enforcement Actions & Litigation",
        "case_law": "New Case Law & Precedents",
        "sb79": "SB 79 / Transit Density Developments",
        "coastal": "Coastal Act & Housing",
        "compliance": "RHNA & Housing Element Compliance",
        "other": "Other Relevant Developments",
    }

    for cat, label in section_names.items():
        items = categorized.get(cat, [])
        if not items:
            continue

        lines.append(f"## {label}")
        lines.append("")

        for h in items:
            date = h.get("_file_date", "?")
            source = h.get("source", "?")
            title = h.get("title", "?")
            url = h.get("url", "")
            reason = h.get("relevance_reason", "")
            actions = h.get("action_items", [])
            score = h.get("relevance_score", "?")
            detection = h.get("detection", "?")

            lines.append(f"**[{date}] {title}**")
            lines.append(f"Source: {source} | Detection: {detection} | Relevance: {score}/10")
            if url:
                lines.append(f"URL: {url}")
            if reason:
                lines.append(f"Why it matters: {reason}")
            if actions:
                for a in actions:
                    lines.append(f"- {a}")
            lines.append("")

    return "\n".join(lines)


def main():
    hits = load_all_hits()
    if not hits:
        print("No intel hits found.")
        return

    supplement = generate_supplement(hits)
    if not supplement:
        print("No recent hits within window.")
        return

    SKILL_DIR.mkdir(parents=True, exist_ok=True)
    OUTPUT_PATH.write_text(supplement)
    print(f"Updated {OUTPUT_PATH} ({len(hits)} hits)")


if __name__ == "__main__":
    main()
