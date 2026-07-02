#!/usr/bin/env python3
"""Generate leadership profiles graded on housing advocacy.

Auto-discovers named figures from meeting data. Hardcoded entries for
Oceanside council (rich alias resolution); auto-discovered for regional
agencies above mention threshold.

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

from civic_utils import claude_local_call, watchdog_data_dir

WATCHDOG_DATA = watchdog_data_dir()
STRUCTURED_DIR = WATCHDOG_DATA / "structured"
MERGED_DIR = STRUCTURED_DIR / "meetings"
OUTPUT_DIR = REPO_ROOT / "output" / "leadership-profiles"
STATE_FILE = OUTPUT_DIR / "_state.json"

SKILLS_DIR = WATCHDOG_DATA.parent / ".claude" / "skills"
SKILL_NAMES = ["ca-housing-law"]

MODE = "local"
client = None

# Minimum mentions to auto-generate a profile
AUTO_THRESHOLD = 50

# ── Known figures with alias resolution ──
# key: slug used for filename
# agency_group: used for per-agency comparative analysis

KNOWN_FIGURES = {
    # ── Oceanside City Council ──
    "sanchez": {
        "full_name": "Esther Sanchez",
        "title": "Mayor",
        "terms": "Council Member 2012–2020, Mayor 2020–present",
        "agency_group": "oceanside-council",
        "aliases": ["sanchez", "mayor sanchez", "council member sanchez",
                    "councilmember sanchez", "mayor esther sanchez", "esther sanchez"],
    },
    "weiss": {
        "full_name": "Ryan Weiss",
        "title": "Council Member",
        "terms": "2018–present",
        "agency_group": "oceanside-council",
        "aliases": ["weiss", "council member weiss", "councilmember weiss",
                    "ryan weiss", "peter weiss"],
    },
    "joyce": {
        "full_name": "Eric Joyce",
        "title": "Council Member / Deputy Mayor",
        "terms": "2022–present",
        "agency_group": "oceanside-council",
        "aliases": ["joyce", "council member joyce", "councilmember joyce",
                    "deputy mayor joyce", "board member joyce", "eric joyce"],
    },
    "robinson": {
        "full_name": "Rick Robinson",
        "title": "Council Member",
        "terms": "2024–present",
        "agency_group": "oceanside-council",
        "aliases": ["robinson", "council member robinson", "councilmember robinson",
                    "rick robinson"],
    },
    "figueroa": {
        "full_name": "Jaime \"Jimmy\" Figueroa",
        "title": "Council Member, District 3",
        "terms": "2024–present",
        "agency_group": "oceanside-council",
        "aliases": ["figueroa", "council member figueroa", "councilmember figueroa",
                    "jimmy figueroa", "jaime figueroa"],
    },
    "rodriguez": {
        "full_name": "Christopher Rodriguez",
        "title": "Former Council Member",
        "terms": "2020–2024",
        "agency_group": "oceanside-council",
        "aliases": ["rodriguez", "council member rodriguez", "councilmember rodriguez",
                    "christopher rodriguez"],
    },
    "keim": {
        "full_name": "Peter Keim",
        "title": "Former Council Member / Mayor",
        "terms": "Mayor 2012–2016, Council Member various terms",
        "agency_group": "oceanside-council",
        "aliases": ["keim", "council member keim", "councilmember keim", "mayor keim",
                    "peter keim", "ryan keim"],
    },
    "feller": {
        "full_name": "Jack Feller",
        "title": "Former Council Member",
        "terms": "2014–2022",
        "agency_group": "oceanside-council",
        "aliases": ["feller", "council member feller", "councilmember feller",
                    "mayor feller", "deputy mayor feller", "jack feller"],
    },
    "tyson": {
        "full_name": "Kori Tyson",
        "title": "Former Council Member",
        "terms": "2020–2022",
        "agency_group": "oceanside-council",
        "aliases": ["tyson", "council member tyson", "councilmember tyson",
                    "kori tyson"],
    },
    "jensen": {
        "full_name": "Jensen",
        "title": "Former Council Member",
        "terms": "Appears in 2020–2022 era records",
        "agency_group": "oceanside-council",
        "aliases": ["jensen", "council member jensen", "councilmember jensen",
                    "kori jensen"],
    },
    "egonzalez": {
        "full_name": "Emily Gonzalez",
        "title": "Planning Commissioner",
        "terms": "Appointed 2025, term through April 15, 2029",
        "agency_group": "oceanside-planning",
        "aliases": ["emily gonzalez", "commissioner gonzalez"],
    },
    # ── Oceanside Planning Commission ──
    "raetz": {
        "full_name": "Patricia Raetz",
        "title": "Planning Commissioner",
        "terms": "Active in records",
        "agency_group": "oceanside-planning",
        "aliases": ["raetz", "patricia raetz", "pat raetz", "commissioner raetz"],
    },
    "vey": {
        "full_name": "Darin Vey",
        "title": "Planning Commissioner",
        "terms": "Active in records",
        "agency_group": "oceanside-planning",
        "aliases": ["vey", "darin vey", "commissioner vey"],
    },
    "balma": {
        "full_name": "Louise Balma",
        "title": "Planning Commissioner",
        "terms": "Active in records",
        "agency_group": "oceanside-planning",
        "aliases": ["balma", "louise balma", "commissioner balma"],
    },
    # ── SD County Board of Supervisors ──
    "desmond": {
        "full_name": "Jim Desmond",
        "title": "County Supervisor, District 5",
        "terms": "2019–present",
        "agency_group": "sd-county",
        "aliases": ["desmond", "jim desmond", "supervisor desmond",
                    "chair desmond", "chairman desmond"],
    },
    "lawson-remer": {
        "full_name": "Terra Lawson-Remer",
        "title": "County Supervisor, District 3",
        "terms": "2021–present",
        "agency_group": "sd-county",
        "aliases": ["lawson-remer", "terra lawson-remer", "supervisor lawson-remer"],
    },
    "anderson": {
        "full_name": "Joel Anderson",
        "title": "County Supervisor, District 2",
        "terms": "2021–present",
        "agency_group": "sd-county",
        "aliases": ["anderson", "joel anderson", "supervisor anderson"],
    },
    "vargas": {
        "full_name": "Nora Vargas",
        "title": "County Supervisor, District 1",
        "terms": "2021–present",
        "agency_group": "sd-county",
        "aliases": ["vargas", "nora vargas", "supervisor vargas",
                    "chair vargas", "chairwoman vargas"],
    },
    "montgomery-steppe": {
        "full_name": "Monica Montgomery Steppe",
        "title": "County Supervisor, District 4",
        "terms": "2023–present",
        "agency_group": "sd-county",
        "aliases": ["montgomery steppe", "monica montgomery steppe",
                    "supervisor montgomery steppe"],
    },
    "fletcher": {
        "full_name": "Nathan Fletcher",
        "title": "Former County Supervisor, District 4",
        "terms": "2019–2023",
        "agency_group": "sd-county",
        "aliases": ["fletcher", "nathan fletcher", "supervisor fletcher",
                    "chair fletcher", "chairman fletcher"],
    },
}

AGENCY_GROUP_LABELS = {
    "oceanside-council": "Oceanside City Council",
    "oceanside-planning": "Oceanside Planning Commission",
    "sd-county": "San Diego County Board of Supervisors",
}


def load_skills():
    parts = []
    for name in SKILL_NAMES:
        path = SKILLS_DIR / name / "SKILL.md"
        if path.exists():
            parts.append(path.read_text())
        supplement = SKILLS_DIR / name / "recent-developments.md"
        if supplement.exists():
            parts.append(supplement.read_text())
    return "\n\n---\n\n".join(parts)


SKILLS_CONTEXT = load_skills()


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


def format_records_for_prompt(records, member_info):
    """Format meeting records into text for profile prompt.

    For figures with many records, filter to housing-relevant ones
    to keep the prompt within token budget.
    """
    # For large record sets, filter to housing-relevant records only
    if len(records) > 60:
        filtered = [r for r in records if is_housing_relevant(r)]
        if len(filtered) < 10:
            filtered = records[:60]
        records = filtered
        # If still too many, keep the most recent (most relevant to current advocacy)
        if len(records) > 80:
            records = sorted(records, key=lambda r: r.get("date", ""), reverse=True)[:80]

    by_year = defaultdict(list)
    for r in records:
        date = r.get("date", "")
        year = int(date[:4]) if date and len(date) >= 4 else 0
        if year:
            by_year[year].append(r)

    parts = []
    total = 0
    for year in sorted(by_year.keys()):
        year_text = f"\n### {year}\n"
        for r in by_year[year]:
            if r.get("procedural_only"):
                continue
            lines = [f"**{r.get('body', '?')} — {r.get('date', '?')} — {r.get('agency', '?')}**"]
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

    return "\n".join(parts), total


# ── Profile generation ──

def generate_profile(slug, info, records):
    """Generate a housing advocacy profile for one figure."""
    text, total = format_records_for_prompt(records, info)

    if total == 0:
        return None

    if len(text) > 180000:
        text = text[:180000] + "\n[...truncated...]"

    role_context = ""
    group = info.get("agency_group", "")
    if "county" in group:
        role_context = """This official serves on the San Diego County Board of Supervisors.
County-level housing actions include: regional housing mandates (RHNA allocation),
unincorporated area zoning, affordable housing trust fund allocations, farmworker housing,
homelessness programs, and votes on state housing law compliance. Grade using the same
housing advocacy framework but at the county/regional scale.

IMPORTANT: Weigh actions by how directly they affect housing outcomes in Oceanside and
North County. Votes on RHNA allocations, regional transit, and county housing programs
that flow to Oceanside matter more than actions on South County or East County issues."""
    elif "planning" in group:
        role_context = """This official serves on the Oceanside Planning Commission.
Planning commissioners make recommendations on project approvals, zoning changes,
specific plans, and environmental review. Their votes directly shape which housing
projects advance to council. Grade using the same housing advocacy framework.

These votes have maximum direct impact on Oceanside housing outcomes."""

    prompt = f"""You are analyzing the record of a local government official from the perspective of a housing advocate in Oceanside/North San Diego County, CA.

**Official:** {info['full_name']}
**Title:** {info['title']}
**Terms:** {info['terms']}
**Total substantive meeting references:** {total}

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

    prompt = f"""You are writing a comparative analysis of {label} members for a housing advocate in Oceanside/North San Diego County, CA.

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

        summary = generate_profile(slug, info, records)
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
