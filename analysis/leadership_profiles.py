#!/usr/bin/env python3
"""Generate leadership profiles graded on housing advocacy.

Known figures loaded from config (SSM or config.local.yaml);
auto-discovered for regional agencies above mention threshold.

Change detection via content-hash — only regenerates profiles when
underlying meeting data changes.

Usage:
    python leadership_profiles.py                     # local mode, change detection
    python leadership_profiles.py --force             # rebuild all profiles
    python leadership_profiles.py --mode api          # Claude API ($)
    python leadership_profiles.py --list              # show discovered figures
    python leadership_profiles.py --stats             # show profile freshness
"""

import argparse
import hashlib
import json
import os
import re
import sys
from collections import Counter, defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
ENV_FILE = REPO_ROOT / ".env"
if ENV_FILE.exists():
    for line in ENV_FILE.read_text().splitlines():
        line = line.strip()
        if line and not line.startswith("#") and "=" in line:
            k, v = line.split("=", 1)
            os.environ.setdefault(k.strip(), v.strip())

from civic_utils import claude_local_call, watchdog_data_dir, load_scored_records, load_analysis_context
sys.path.insert(0, str(REPO_ROOT))
import config

WATCHDOG_DATA = watchdog_data_dir()
STRUCTURED_DIR = WATCHDOG_DATA / "structured"
MERGED_DIR = STRUCTURED_DIR / "meetings"
OUTPUT_DIR = REPO_ROOT / "output" / "leadership-profiles"
STATE_FILE = OUTPUT_DIR / "_state.json"

MODE = "local"
client = None

# Minimum mentions to auto-generate a profile
AUTO_THRESHOLD = 50

KNOWN_FIGURES = config.get("figures/known_figures", {})
AGENCY_GROUP_LABELS = config.get("figures/agency_group_labels", {})


SKILLS_CONTEXT = load_analysis_context()


def call_claude(prompt, max_tokens=4000):
    if MODE == "local":
        return claude_local_call(prompt, system=SKILLS_CONTEXT, timeout=600)
    else:
        from civic_utils import claude_api_call
        response = claude_api_call(
            client,
            model="claude-opus-4-6",
            max_tokens=max_tokens,
            system=SKILLS_CONTEXT,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text


def content_hash(data):
    return hashlib.sha256(json.dumps(data, sort_keys=True, default=str).encode()).hexdigest()[:16]


def load_state():
    if STATE_FILE.exists():
        return json.loads(STATE_FILE.read_text())
    return {}


def save_state(state):
    STATE_FILE.write_text(json.dumps(state, indent=2, default=str))


# ── Data collection ──

def collect_all_mentions():
    """Scan all merged meetings. Return {slug: [records]} for known figures."""
    merged_jsonl = STRUCTURED_DIR / "meetings-combined.jsonl"
    if not merged_jsonl.exists():
        print(f"No merged JSONL at {merged_jsonl}. Run meeting_merge.py first.")
        sys.exit(1)

    # Build alias → slug lookup
    alias_to_slug = {}
    for slug, info in KNOWN_FIGURES.items():
        for alias in info["aliases"]:
            alias_to_slug[alias.lower()] = slug

    by_slug = defaultdict(list)

    with open(merged_jsonl) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            matched_slugs = set()

            # Check council_positions
            for cp in record.get("council_positions", []):
                member = cp.get("member", "").lower()
                slug = alias_to_slug.get(member)
                if slug:
                    matched_slugs.add(slug)
                else:
                    for alias, s in alias_to_slug.items():
                        if alias in member or member in alias:
                            matched_slugs.add(s)
                            break

            # Check vote records
            for v in record.get("votes", []):
                for voter in v.get("yes", []) + v.get("no", []) + v.get("abstain", []):
                    vl = voter.lower()
                    slug = alias_to_slug.get(vl)
                    if slug:
                        matched_slugs.add(slug)
                    else:
                        for alias, s in alias_to_slug.items():
                            if alias in vl or vl in alias:
                                matched_slugs.add(s)
                                break

            # Check key_quotes for name mentions
            record_text = json.dumps(record).lower()
            for alias, slug in alias_to_slug.items():
                if len(alias) > 5 and alias in record_text:
                    matched_slugs.add(slug)

            for slug in matched_slugs:
                by_slug[slug].append(record)

    return dict(by_slug)


HOUSING_KEYWORDS = {
    "housing", "density", "zoning", "affordable", "inclusionary",
    "rhna", "tenant", "rent stabiliz", "eviction", "duplex",
    "accessory dwelling", "mixed-use", "mixed use", "transit-oriented",
    "sb 9", "sb 10", "sb 35", "sb 79", "parking minimum",
    "density bonus", "workforce housing", "apartment", "subdivision",
    "rezone", "upzone", "downzone", "specific plan",
}


def is_housing_relevant(record):
    """Check if a record has housing/land use substance (not just keywords in metadata)."""
    if record.get("housing_items"):
        return True
    # Check content fields only, not full JSON (avoids matching field names)
    content_parts = []
    for v in record.get("votes", []):
        content_parts.append(v.get("item", ""))
    for cp in record.get("council_positions", []):
        content_parts.append(cp.get("evidence", ""))
    content_parts.extend(record.get("legal_flags", []))
    content_parts.extend(record.get("key_quotes", []))
    text = " ".join(content_parts).lower()
    return any(kw in text for kw in HOUSING_KEYWORDS)


def format_records_for_prompt(records, member_info, scored_data=None):
    """Format meeting records into text for profile prompt.

    For figures with many records, filter to housing-relevant ones
    to keep the prompt within token budget. When scored_data is provided,
    annotates with pre-computed scores.
    """
    if len(records) > 60:
        filtered = [r for r in records if is_housing_relevant(r)]
        if len(filtered) < 10:
            filtered = records[:60]
        records = filtered
        if len(records) > 80:
            records = sorted(records, key=lambda r: r.get("date", ""), reverse=True)[:80]

    last_name = member_info.get("full_name", "").split()[-1].lower() if member_info.get("full_name") else ""
    aliases = [a.lower() for a in member_info.get("aliases", [])]

    by_year = defaultdict(list)
    for r in records:
        date = r.get("date", "")
        year = int(date[:4]) if date and len(date) >= 4 else 0
        if year:
            by_year[year].append(r)

    parts = []
    total = 0
    net_score = 0
    action_count = 0
    for year in sorted(by_year.keys()):
        year_text = f"\n### {year}\n"
        for r in by_year[year]:
            if r.get("procedural_only"):
                continue

            mid = str(r.get("meeting_id", ""))
            scored = scored_data.get(mid) if scored_data else None

            lines = [f"**{r.get('body', '?')} — {r.get('date', '?')} — {r.get('agency', '?')}**"]

            if scored:
                lines.append(f"SCORED: {scored.get('advocacy_score', '?')} — {scored.get('advocacy_reason', '')}")
                for ps in scored.get("position_scores", []):
                    ps_name = ps.get("member", "").lower()
                    if last_name in ps_name or ps_name in aliases:
                        lines.append(f"MEMBER SCORE: {ps['member']} {ps.get('score', 0):+d} ({ps.get('action_summary', '')[:60]})")
                        net_score += ps.get("score", 0)
                        action_count += 1

            for v in r.get("votes", []):
                vote_line = f"VOTE: {v['item']} → {v['result']}"
                if v.get("yes"):
                    vote_line += f" (yes: {', '.join(v['yes'])})"
                if v.get("no"):
                    vote_line += f" (no: {', '.join(v['no'])})"
                lines.append(vote_line)
            for cp in r.get("council_positions", []):
                label = cp.get('action') or cp.get('stance', 'unknown')
                lines.append(f"POSITION: {cp['member']} — {label}: {cp.get('evidence', '')}")
            for h in r.get("housing_items", []):
                h_line = f"HOUSING: [{h.get('type', '?')}] {h['description']}"
                if h.get("outcome"):
                    h_line += f" → {h['outcome']}"
                if h.get("state_law_flags"):
                    h_line += f" ⚠ {', '.join(h['state_law_flags'])}"
                lines.append(h_line)
            for flag in r.get("legal_flags", []):
                lines.append(f"LEGAL: {flag}")
            for q in r.get("key_quotes", []):
                lines.append(f"QUOTE: {q}")
            year_text += "\n".join(lines) + "\n"
            total += 1
        parts.append(year_text)

    return "\n".join(parts), total, net_score, action_count


# ── Profile generation ──

def generate_profile(slug, info, records, scored_data=None):
    """Generate a housing advocacy profile for one figure."""
    text, total, net_score, action_count = format_records_for_prompt(records, info, scored_data=scored_data)

    if total == 0:
        return None

    if len(text) > 180000:
        text = text[:180000] + "\n[...truncated...]"

    _city = config.get("identity/primary_city", "the jurisdiction")
    _region = config.get("identity/region_label", "the region")

    role_context = ""
    group = info.get("agency_group", "")
    group_label = AGENCY_GROUP_LABELS.get(group, group)
    if "county" in group:
        role_context = f"""This official serves on the {group_label}.
County-level housing actions include: regional housing mandates (RHNA allocation),
unincorporated area zoning, affordable housing trust fund allocations, farmworker housing,
homelessness programs, and votes on state housing law compliance. Grade using the same
housing advocacy framework but at the county/regional scale.

IMPORTANT: Weigh actions by how directly they affect housing outcomes in {_city} and
the surrounding region. Votes on RHNA allocations, regional transit, and county housing programs
that flow to {_city} matter more than actions in distant parts of the county."""
    elif "planning" in group:
        role_context = f"""This official serves on the {group_label}.
Planning commissioners make recommendations on project approvals, zoning changes,
specific plans, and environmental review. Their votes directly shape which housing
projects advance to council. Grade using the same housing advocacy framework.

These votes have maximum direct impact on {_city} housing outcomes."""

    score_header = ""
    if scored_data and action_count > 0:
        grade_thresholds = [(10, "A"), (5, "B"), (0, "C"), (-1, "D")]
        grade = "F"
        for threshold, letter in grade_thresholds:
            if net_score >= threshold:
                grade = letter
                break
        score_header = f"""
**PRE-COMPUTED SCORE:** Net score {net_score:+d} across {action_count} scored actions → Grade {grade}
These scores were computed deterministically from voting records using the rubric below.
Use these scores as your baseline. Do NOT re-derive scores from scratch — validate against
the MEMBER SCORE annotations in the meeting data and adjust only if you find clear errors."""

    _advocate_role = config.get("advocacy/advocate_role", "housing advocate")
    _lens = config.get("advocacy/lens", "")
    prompt = f"""You are analyzing the record of a local government official from the perspective of a {_advocate_role} in {_city}/{_region}.

**Official:** {info['full_name']}
**Title:** {info['title']}
**Terms:** {info['terms']}
**Total substantive meeting references:** {total}
{score_header}

{role_context}

Your task:
1. **Housing Advocacy Grade** (A through F): Grade their record using ACTIONS, not rhetoric.

   ## Scoring Framework

   ### Strong Pro-Housing (+2 each):
   - Vote to REMOVE or RAISE density caps
   - Vote to ELIMINATE or REDUCE parking minimums
   - Vote to LEGALIZE missing middle housing by-right
   - Vote FOR state preemption of restrictive zoning (SB 9, SB 10, RHNA compliance)
   - Vote to APPROVE housing projects (market-rate OR affordable)
   - Vote FOR regional housing funding or transit-oriented development

   ### Moderate Pro-Housing (+1 each):
   - Support for inclusionary zoning requirements
   - Tenant protections: relocation assistance, just-cause eviction, rent stabilization
   - Streamlining discretionary approvals
   - Support for transit-oriented density increases
   - Votes for homelessness services and affordable housing finance

   ### Anti-Housing (-1 each):
   - Vote to MAINTAIN parking requirements at 1+ space/unit
   - Citing "community character" to oppose density
   - Opposing state housing mandates under "local control" framing

   ### Strong Anti-Housing (-2 each):
   - Vote to ADD or MAINTAIN density caps
   - Vote AGAINST housing projects
   - Using inclusionary rates as a POISON PILL to kill specific compliant projects
   - Weaponizing CEQA against housing
   - Opposing regional housing allocations (RHNA)

   GRADING SCALE by net score:
   - A: +10 or higher
   - B: +5 to +9
   - C: 0 to +4
   - D: -1 to -9
   - F: -10 or lower
   Add +/- within bands.

2. **Executive Summary** (400-800 words):
   - Their VOTING RECORD on housing — every project vote, zoning vote, density vote
   - Whether they use pro-housing rhetoric to cover anti-housing votes
   - Specific projects and outcomes
   - Alliances and voting blocs
   - Evolution over time

3. **Key Votes Table**: 5-10 most significant housing-related votes.

Be analytical and specific. ACTIONS OVER WORDS. This is an advocacy tool.

CRITICAL: Only reference votes and actions in the source data below. NEVER invent votes, meetings, or actions. If the record is thin, say so.

Meeting references:
{text}"""

    return call_claude(prompt, max_tokens=4000)


def generate_comparative(agency_group, label, profiles):
    """Generate comparative analysis for one agency group."""
    combined = "\n\n---\n\n".join(
        f"## {name}\n{summary}" for name, summary in profiles.items()
    )

    if len(combined) > 180000:
        combined = combined[:180000] + "\n[...truncated...]"

    _city = config.get("identity/primary_city", "the jurisdiction")
    _region = config.get("identity/region_label", "the region")
    _advocate_role = config.get("advocacy/advocate_role", "housing advocate")
    prompt = f"""You are writing a comparative analysis of {label} members for a {_advocate_role} in {_city}/{_region}.

Below are individual profiles graded on housing advocacy using an ACTIONS-BASED framework.

Synthesize into a comparative document:

1. **Power Map**: Reliable housing allies (by VOTES)? Obstacles? Swing votes?
2. **Voting Blocs**: What coalitions form on housing votes? How stable?
3. **Grade Summary Table**: All members with grade and one-line rationale
4. **Strategic Assessment**: Where should advocacy energy go? Who is persuadable?
5. **Supply Skepticism Watch**: Who uses pro-housing language to cover anti-housing votes?
6. **Historical Arc**: How has the body's housing posture evolved?

Be direct and strategic. ACTIONS OVER WORDS. Target: 800-1200 words.

Individual profiles:
{combined}"""

    return call_claude(prompt, max_tokens=4000)


# ── Commands ──

def cmd_build(args):
    """Generate/update leadership profiles with change detection."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    state = load_state()

    scored_data = load_scored_records()
    if scored_data:
        print(f"Loaded {len(scored_data)} pre-scored records")

    print("Scanning meeting data for named figures...")
    all_mentions = collect_all_mentions()

    # Filter to figures with enough data
    figures_to_process = {}
    for slug, info in KNOWN_FIGURES.items():
        records = all_mentions.get(slug, [])
        if len(records) < 3 and not args.force:
            continue
        figures_to_process[slug] = (info, records)

    print(f"Found {len(figures_to_process)} figures with sufficient data")

    # Check which need rebuild
    to_build = []
    skipped = 0
    for slug, (info, records) in figures_to_process.items():
        record_hash = content_hash([
            (r.get("meeting_id"), r.get("source_count")) for r in records
        ])
        prev_hash = state.get(slug, {}).get("record_hash")
        out_path = OUTPUT_DIR / f"{slug}.md"

        if args.force or not out_path.exists() or record_hash != prev_hash:
            to_build.append((slug, info, records, record_hash))
        else:
            skipped += 1

    if not to_build and not args.force:
        print(f"All {len(figures_to_process)} profiles up to date ({skipped} skipped).")
        return

    print(f"Building {len(to_build)} profiles ({skipped} up to date)")

    built_by_group = defaultdict(dict)
    all_by_group = defaultdict(dict)

    for slug, info, records, record_hash in to_build:
        group = info.get("agency_group", "other")
        print(f"\n  {info['full_name']} ({info['title']}, {len(records)} records)...")

        summary = generate_profile(slug, info, records, scored_data=scored_data)
        if summary is None:
            print(f"    No substantive records, skipping.")
            continue

        out_path = OUTPUT_DIR / f"{slug}.md"
        out_path.write_text(
            f"# {info['full_name']} — Housing Advocacy Profile\n\n"
            f"**Title:** {info['title']}  \n"
            f"**Terms:** {info['terms']}  \n\n"
            f"{summary}\n"
        )

        state[slug] = {
            "record_hash": record_hash,
            "record_count": len(records),
            "agency_group": group,
        }
        save_state(state)

        built_by_group[group][info["full_name"]] = summary
        print(f"    Saved: {out_path}")

    # Load existing profiles for complete comparative analyses
    for slug, (info, records) in figures_to_process.items():
        group = info.get("agency_group", "other")
        if info["full_name"] not in built_by_group.get(group, {}):
            out_path = OUTPUT_DIR / f"{slug}.md"
            if out_path.exists():
                text = out_path.read_text()
                # Strip the header
                lines = text.split("\n")
                body_start = next((i for i, l in enumerate(lines) if l.startswith("#") and "Housing Advocacy" in l), 0)
                header_end = next((i for i in range(body_start + 1, len(lines)) if lines[i].strip() and not lines[i].startswith("**")), body_start + 1)
                body = "\n".join(lines[header_end:]).strip()
                all_by_group[group][info["full_name"]] = body

    # Merge built profiles into all_by_group
    for group, profiles in built_by_group.items():
        all_by_group[group].update(profiles)

    # Generate per-agency comparative analyses
    for group, profiles in all_by_group.items():
        if len(profiles) < 2:
            continue
        label = AGENCY_GROUP_LABELS.get(group, group)
        print(f"\n  Comparative analysis: {label} ({len(profiles)} members)...")
        comparative = generate_comparative(group, label, profiles)
        if comparative:
            out_path = OUTPUT_DIR / f"{group}-comparative.md"
            out_path.write_text(
                f"# {label} — Housing Advocacy Comparative Analysis\n\n{comparative}\n"
            )
            print(f"    Saved: {out_path}")

    save_state(state)
    total_built = sum(len(p) for p in built_by_group.values())
    print(f"\nBuilt {total_built} profiles, skipped {skipped}")


def cmd_list(args):
    """Show discovered figures and their mention counts."""
    all_mentions = collect_all_mentions()

    by_group = defaultdict(list)
    for slug, info in KNOWN_FIGURES.items():
        records = all_mentions.get(slug, [])
        group = info.get("agency_group", "other")
        has_profile = (OUTPUT_DIR / f"{slug}.md").exists()
        by_group[group].append((slug, info, len(records), has_profile))

    for group in sorted(by_group.keys()):
        label = AGENCY_GROUP_LABELS.get(group, group)
        print(f"\n{label}:")
        entries = sorted(by_group[group], key=lambda x: x[2], reverse=True)
        for slug, info, count, has_profile in entries:
            status = "✓" if has_profile else "·"
            print(f"  {status} {info['full_name']:30s} {count:5d} records  ({slug})")


def cmd_stats(args):
    """Show profile freshness stats."""
    state = load_state()
    if not state:
        print("No profiles built yet. Run leadership_profiles.py first.")
        return

    by_group = defaultdict(list)
    for slug, info in state.items():
        group = info.get("agency_group", "other")
        out_path = OUTPUT_DIR / f"{slug}.md"
        exists = out_path.exists()
        by_group[group].append((slug, info, exists))

    for group in sorted(by_group.keys()):
        label = AGENCY_GROUP_LABELS.get(group, group)
        print(f"\n{label}:")
        for slug, info, exists in by_group[group]:
            status = "✓" if exists else "MISSING"
            print(f"  {status} {slug:25s} ({info.get('record_count', '?')} records)")


def main():
    global MODE, client

    parser = argparse.ArgumentParser(description="Generate leadership profiles")
    parser.add_argument("--mode", choices=["api", "local"], default="local",
                        help="api=Claude API ($), local=claude -p (subscription, $0)")
    parser.add_argument("--force", action="store_true", help="Rebuild all profiles")
    parser.add_argument("--list", action="store_true", help="Show discovered figures")
    parser.add_argument("--stats", action="store_true", help="Show profile freshness")
    args = parser.parse_args()

    MODE = args.mode

    if MODE == "api":
        import anthropic
        client = anthropic.Anthropic()

    if args.list:
        cmd_list(args)
    elif args.stats:
        cmd_stats(args)
    else:
        cmd_build(args)


if __name__ == "__main__":
    main()
