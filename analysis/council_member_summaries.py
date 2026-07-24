#!/usr/bin/env python3
"""Generate per-council-member executive summaries graded on housing advocacy.

Usage:
    python council_member_summaries.py                          # API mode, prose
    python council_member_summaries.py --mode local             # claude -p ($0)
    python council_member_summaries.py --source jsonl           # JSONL input
    python council_member_summaries.py --mode local --source jsonl  # $0, fastest
"""

import argparse
import json
import os
import re
import sys
from collections import defaultdict
from datetime import datetime
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

from civic_utils import claude_local_call, all_meetings_dirs, watchdog_data_dir, load_scored_records, load_analysis_context
sys.path.insert(0, str(REPO_ROOT))
import config

WATCHDOG_DATA = watchdog_data_dir()
STRUCTURED_DIR = WATCHDOG_DATA / "structured"
MERGED_DIR = STRUCTURED_DIR / "meetings"
OUTPUT_DIR = REPO_ROOT / "output" / "council-members"

MODE = "api"
client = None


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

_primary_slug = config.get("identity/primary_city_slug", "")
_council_group = f"{_primary_slug}-council"
_planning_group = f"{_primary_slug}-planning"
_all_figures = config.get("figures/known_figures", {})
COUNCIL_MEMBERS = {
    slug: info for slug, info in _all_figures.items()
    if info.get("agency_group") in (_council_group, _planning_group)
}


def load_meeting_info():
    info = {}
    for meetings_dir in all_meetings_dirs():
        for mdir in meetings_dir.iterdir():
            mf = mdir / "meeting.json"
            if not mf.exists():
                continue
            m = json.loads(mf.read_text())
            date = m.get("date", "")
            body = m.get("body", m.get("EventBodyName", ""))
            dt = None
            for fmt in ["%m/%d/%Y", "%Y-%m-%d"]:
                try:
                    dt = datetime.strptime(
                        date.split("T")[0] if "T" in date else date, fmt
                    )
                    break
                except ValueError:
                    pass
            info[mdir.name] = {"date": date, "body": body, "year": dt.year if dt else None, "dt": dt}
    return info


def extract_relevant_passages(text, aliases):
    """Extract paragraphs/bullet points mentioning a council member."""
    lines = text.split("\n")
    relevant = []
    pattern = re.compile("|".join(re.escape(a) for a in aliases), re.IGNORECASE)

    for i, line in enumerate(lines):
        if pattern.search(line):
            context_start = max(0, i - 1)
            context_end = min(len(lines), i + 2)
            block = "\n".join(lines[context_start:context_end]).strip()
            if block and block not in relevant:
                relevant.append(block)

    return relevant


def collect_member_mentions(member_key, member_info, meeting_info):
    """Collect all meeting summary passages mentioning a council member (prose mode)."""
    aliases = member_info["aliases"]
    by_year = defaultdict(list)

    summary_dirs = [d.parent / "summaries" for d in all_meetings_dirs()
                     if (d.parent / "summaries").exists()]

    for sdir in summary_dirs:
        for sf in sorted(sdir.glob("*-summary.md")):
            content = sf.read_text()
            passages = extract_relevant_passages(content, aliases)
            if not passages:
                continue

            mid = sf.stem.split("-")[0]
            mi = meeting_info.get(mid, {})
            year = mi.get("year")
            date = mi.get("date", "unknown")
            body = mi.get("body", "unknown")
            doc_type = "minutes" if "minutes" in sf.stem else "agenda"

            if year:
                by_year[year].append({
                    "date": date,
                    "body": body,
                    "doc_type": doc_type,
                    "passages": passages,
                })

    return by_year


def collect_member_mentions_jsonl(member_key, member_info):
    """Collect structured records mentioning a council member (JSONL mode)."""
    # Prefer merged per-meeting records; fall back to individual
    merged_jsonl = STRUCTURED_DIR / "meetings-combined.jsonl"
    jsonl_path = merged_jsonl if merged_jsonl.exists() else STRUCTURED_DIR / "all-records.jsonl"
    if not jsonl_path.exists():
        return {}

    last_name = member_key.lower()
    aliases = [a.lower() for a in member_info["aliases"]]
    by_year = defaultdict(list)

    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)

            mentioned = False
            for cp in record.get("council_positions", []):
                if cp.get("member", "").lower() in aliases or last_name in cp.get("member", "").lower():
                    mentioned = True
                    break

            if not mentioned:
                for v in record.get("votes", []):
                    all_voters = v.get("yes", []) + v.get("no", []) + v.get("abstain", [])
                    for voter in all_voters:
                        if voter.lower() in aliases or last_name in voter.lower():
                            mentioned = True
                            break
                    if mentioned:
                        break

            if not mentioned:
                record_str = json.dumps(record).lower()
                if any(a in record_str for a in aliases):
                    mentioned = True

            if not mentioned:
                continue

            date = record.get("date", "")
            year = None
            if date:
                try:
                    year = int(date[:4])
                except (ValueError, IndexError):
                    pass

            if year:
                by_year[year].append(record)

    return by_year


def format_member_records(records_by_year, member_info, scored_data=None):
    """Format JSONL records into text for the member profile prompt.

    When scored_data is provided, annotates records with pre-computed scores
    so the LLM can synthesize narrative without re-deriving scores.
    """
    parts = []
    total = 0
    member_name = member_info["full_name"]
    last_name = member_info["full_name"].split()[-1].lower()
    net_score = 0
    action_count = 0

    for year in sorted(records_by_year.keys()):
        records = records_by_year[year]
        year_text = f"\n### {year}\n"
        for r in records:
            mid = str(r.get("meeting_id", ""))
            scored = scored_data.get(mid) if scored_data else None

            lines = [f"**{r.get('body', '?')} — {r.get('date', '?')}**"]

            if scored:
                lines.append(f"RECORD SCORE: {scored.get('advocacy_score', '?')} — {scored.get('advocacy_reason', '')}")
                for ps in scored.get("position_scores", []):
                    ps_name = ps.get("member", "").lower()
                    if last_name in ps_name or ps_name in [a.lower() for a in member_info.get("aliases", [])]:
                        lines.append(f"MEMBER SCORE: {ps['member']} net {ps.get('score', 0):+d} ({ps.get('action_summary', '')})")
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


def summarize_member(member_key, member_info, mentions_by_year, source="prose", scored_data=None):
    """Generate housing advocacy summary for one council member."""
    if source == "jsonl":
        all_text, total_mentions, net_score, action_count = format_member_records(
            mentions_by_year, member_info, scored_data=scored_data
        )
    else:
        combined_parts = []
        total_mentions = 0
        for year in sorted(mentions_by_year.keys()):
            entries = mentions_by_year[year]
            year_text = f"\n### {year}\n"
            for entry in entries:
                year_text += f"\n**{entry['body']} — {entry['date']} ({entry['doc_type']})**\n"
                for p in entry["passages"]:
                    year_text += f"{p}\n"
                total_mentions += 1
            combined_parts.append(year_text)
        all_text = "\n".join(combined_parts)

    if len(all_text) > 180000:
        all_text = all_text[:180000] + "\n[...truncated...]"

    score_header = ""
    if source == "jsonl" and scored_data and action_count > 0:
        grade = "A" if net_score >= 10 else "B" if net_score >= 5 else "C" if net_score >= 0 else "D" if net_score >= -9 else "F"
        score_header = f"\n**Pre-computed net score:** {net_score:+d} ({action_count} scored actions) → Grade: {grade}\nScores are pre-computed using the ACTIONS OVER WORDS rubric. Use these scores as the basis for your grade — do NOT re-derive scores. Focus your analysis on narrative synthesis: vote patterns, alliances, evolution over time, and strategic implications.\n"

    _city = config.get("identity/primary_city", "the jurisdiction")
    _state = config.get("identity/state", "")
    _advocate_role = config.get("advocacy/advocate_role", "housing advocate")
    prompt = f"""You are analyzing the record of a {_city}, {_state} elected official from the perspective of a {_advocate_role}.

**Official:** {member_info['full_name']}
**Title:** {member_info['title']}
**Terms:** {member_info['terms']}
**Total meeting references found:** {total_mentions}{score_header}

Below are all passages from City Council and commission meeting summaries (agendas and minutes) that mention this official. These span multiple years.

Your task:
1. **Housing Advocacy Grade** (A through F): Grade their record using the ACTIONS-BASED scorecard below. Focus exclusively on VOTES AND ACTIONS, not rhetoric. A member's words count for nothing if their votes contradict them — and in fact, rhetoric-vote gaps should count AGAINST them (supply skepticism disguised as progressivism confuses advocates and wastes organizing energy).

   ## Scoring Framework (grounded in housing economics research)

   ### Strong Pro-Housing (+2 each):
   - Vote to REMOVE or RAISE density caps (supply constraint removal — National Zoning Atlas)
   - Vote to ELIMINATE parking minimums or reduce to 0-0.5/unit ($30-40K/unit cost impact — Streetsblog/Colorado studies)
   - Vote to LEGALIZE missing middle housing by-right (ADUs, duplexes, triplexes, fourplexes)
   - Vote FOR state preemption of local restrictive zoning (SB 9, SB 10, RHNA compliance)
   - Vote to STREAMLINE environmental/CEQA review for housing projects
   - Vote to APPROVE housing projects (market-rate OR affordable — both add supply)

   ### Moderate Pro-Housing (+1 each):
   - Support for inclusionary zoning requirements (genuinely pro-housing policy)
   - Tenant protections: relocation assistance, just-cause eviction, rent stabilization — these are GOOD VOTES on their own merits (+1 each). Do NOT penalize tenant protection votes for lacking a "supply-side pair." Every tenant protection vote stands alone as pro-housing.
   - Streamlining discretionary approvals for qualifying projects
   - Support for transit-oriented density increases

   ### Anti-Housing (-1 each):
   - Vote to MAINTAIN parking requirements at 1+ space/unit
   - Vote to MAINTAIN single-family-only zoning
   - Citing "community character" or "neighborhood preservation" to oppose density
   - Opposing state housing mandates under "local control" framing (local control historically enables exclusionary zoning)

   ### Strong Anti-Housing (-2 each):
   - Vote to ADD or MAINTAIN density caps (supply restriction)
   - Vote to DOWNZONE (reduce allowed density)
   - Vote AGAINST housing projects (blocking supply — the cardinal sin)
   - Weaponizing CEQA/environmental review against housing
   - Using inclusionary rates as a POISON PILL to kill a SPECIFIC PROJECT: e.g., arguing a project should be denied because its inclusionary rate isn't high enough, even when it meets or exceeds the city's own requirements. The test is concrete: did the member cite inclusionary shortfall as grounds to DENY or OPPOSE a specific project that complied with existing rules? That is weaponization. Voting to raise the citywide inclusionary rate as policy is fine (+1). Demanding an individual project exceed it as a condition for approval is a supply-blocking tactic.
   - Example: arguing the OTC project should be denied because it doesn't have a high enough affordable percentage — when the project exceeds the city's inclusionary rate and would replace a parking lot with housing — is strong anti-housing.

   ### IMPORTANT: Tenant protections and inclusionary policy are genuinely pro-housing.
   Do NOT penalize a member for voting for tenant protections, rent stabilization, or inclusionary requirements. These are good votes. Score them positive (+1).

   The supply skepticism question is a PATTERN-LEVEL assessment, not a per-vote penalty:
   - If a member supports tenant protections AND also votes to approve housing projects, raise density, and support supply — that's a genuinely pro-housing record. Grade accordingly.
   - If a member ONLY supports tenant protections and NEVER votes to approve projects, raise density, or support supply — note the gap as a pattern, but do not subtract points from individual tenant protection votes. Instead, note that their record is incomplete: pro-tenant but silent or hostile on supply.
   - If a member uses pro-housing language (inclusionary, affordability, tenant protection) as a WEAPON to oppose specific housing projects — e.g., "this project doesn't have enough affordable units" as grounds to deny a compliant project — THAT is anti-housing (-2) and should be called out explicitly.

   The distinction: genuine advocacy vs. cynical weaponization. A member who votes for tenant protections AND votes against housing projects is not penalized for the tenant protection votes. They are penalized for the anti-project votes. The pattern is noted in the narrative.

   ### Key analytical principle:
   ACTIONS OVER WORDS. Speeches, resolutions, and stated priorities are noise. Votes on actual projects, zoning changes, and density are signal. Grade the votes. But grade tenant protection and inclusionary votes as the good votes they are.

2. **Executive Summary** (600-1000 words): Write a narrative profile covering:
   - Their VOTING RECORD on housing — every project vote, zoning vote, density vote you can find
   - Whether they use pro-housing rhetoric to cover anti-housing votes (supply skepticism pattern)
   - Specific projects they voted for or against, with unit counts and outcomes
   - Key dissents and what they signal about actual priorities (not stated priorities)
   - Alliances and voting blocs on housing-specific items
   - Evolution over time — are they getting better or worse?

3. **Key Votes Table**: List their 5-10 most significant housing-related votes with date, item, their vote, and outcome.

Be analytical and specific. Name projects, dollar amounts, vote splits. This is for an advocate who needs to understand who is an ally, who is an obstacle, and who is persuadable. ONLY GRADE ON ACTIONS.

GRADING SCALE — use the NET SCORE to assign the letter grade. Lower net scores MUST get lower grades:
- A: net score +10 or higher
- B: net score +5 to +9
- C: net score 0 to +4
- D: net score -1 to -9
- F: net score -10 or lower
Add +/- within each band as appropriate. A member with a -27 net score MUST get a lower grade than one with -13. Do not let occasional positive votes pull the letter grade above what the net score dictates.

CRITICAL: Only reference votes and actions that appear in the meeting passages below. NEVER invent votes, meetings, or actions not present in the source data. If the record is thin, say so — do not fill gaps with guesses.

Meeting references:
{all_text}"""

    return call_claude(prompt, max_tokens=4000)


def create_comparative_summary(member_summaries):
    """Generate a comparative overview across all council members."""
    combined = "\n\n---\n\n".join(
        f"## {name}\n{summary}" for name, summary in member_summaries.items()
    )

    if len(combined) > 180000:
        combined = combined[:180000] + "\n[...truncated...]"

    _city = config.get("identity/primary_city", "the jurisdiction")
    _state = config.get("identity/state", "")
    _advocate_role = config.get("advocacy/advocate_role", "housing advocate")
    _group_labels = config.get("figures/agency_group_labels", {})
    _council_label = _group_labels.get(f"{config.get('identity/primary_city_slug', '')}-council", f"{_city} City Council")
    prompt = f"""You are writing a comparative analysis of {_council_label} members for a {_advocate_role}.

Below are individual profiles of each council member graded on housing advocacy using an ACTIONS-BASED framework grounded in housing economics research (National Zoning Atlas, UCLA Housing Voice, NYU Furman Center supply skepticism research).

The grading framework weighs VOTES over rhetoric. Key principles:
- Removing density caps, parking minimums, and single-family zoning = strong pro-housing
- Blocking projects (market-rate or affordable) = strong anti-housing
- Using high inclusionary rates as poison pills to kill projects = anti-housing disguised as equity
- Pro-housing rhetoric + anti-housing votes = supply skepticism penalty (WORSE than honest NIMBYism)
- Tenant protections are genuinely pro-housing on their own. The supply skepticism flag applies at the PATTERN level — a member who ONLY does tenant protections and NEVER supports supply is noted, but tenant votes themselves are positive.

GRADING CONSISTENCY: Use the net scores from the individual profiles. A member with a worse (more negative) net score MUST get a lower letter grade. Do not let rhetorical analysis override the numerical scoring.

Synthesize into a single comparative document:

1. **Power Map**: Who are reliable housing allies (based on VOTES, not speeches)? Obstacles? Swing votes?
2. **Voting Blocs**: What coalitions form on housing votes? How stable?
3. **Grade Summary Table**: All members with grade and one-line rationale based on vote record
4. **Strategic Assessment**: Where should advocacy energy go? Who is persuadable and on what? What framings work?
5. **Supply Skepticism Watch**: Which members use pro-housing language to cover anti-housing votes? How should advocates handle this?
6. **Historical Arc**: How has the council's housing posture evolved as membership changed?

Be direct and strategic. This is an advocacy tool, not a neutral report. ACTIONS OVER WORDS.

Target length: 1000-1500 words.

Individual profiles:
{combined}"""

    return call_claude(prompt, max_tokens=4000)


def main():
    global MODE, client

    parser = argparse.ArgumentParser(description="Generate council member profiles")
    parser.add_argument("--mode", choices=["api", "local"], default="api",
                        help="api=Claude API ($), local=claude -p (subscription, $0)")
    parser.add_argument("--source", choices=["jsonl", "prose"], default="jsonl",
                        help="jsonl=structured records (default, scans individual records for member mentions), prose=summaries (deprecated)")
    args = parser.parse_args()

    MODE = args.mode
    source = args.source

    if MODE == "api":
        import anthropic
        client = anthropic.Anthropic()

    print(f"Mode: {MODE} | Source: {source}")

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    meeting_info = load_meeting_info()
    scored_data = load_scored_records() if source == "jsonl" else {}
    if scored_data:
        print(f"Loaded {len(scored_data)} pre-scored records")

    member_summaries = {}

    for member_key in sorted(COUNCIL_MEMBERS.keys()):
        member = COUNCIL_MEMBERS[member_key]
        print(f"\n{'='*60}")
        print(f"Processing {member['full_name']} ({member['title']})")
        print(f"{'='*60}")

        if source == "jsonl":
            mentions = collect_member_mentions_jsonl(member_key, member)
        else:
            mentions = collect_member_mentions(member_key, member, meeting_info)

        total = sum(len(entries) for entries in mentions.values())

        if total == 0:
            print(f"  No mentions found, skipping.")
            continue

        years = sorted(mentions.keys())
        print(f"  Found {total} meeting references across {years[0]}–{years[-1]}")
        print(f"  Generating housing advocacy summary...")

        summary = summarize_member(member_key, member, mentions, source=source, scored_data=scored_data)
        member_summaries[member["full_name"]] = summary

        outfile = OUTPUT_DIR / f"{member_key.lower()}.md"
        outfile.write_text(
            f"# {member['full_name']} — Housing Advocacy Profile\n\n"
            f"**Title:** {member['title']}  \n"
            f"**Terms:** {member['terms']}  \n\n"
            f"{summary}\n"
        )
        print(f"  Saved: {outfile}")

    if len(member_summaries) >= 2:
        print(f"\n{'='*60}")
        print("Creating comparative analysis...")
        print(f"{'='*60}")
        comparative = create_comparative_summary(member_summaries)
        outfile = OUTPUT_DIR / "comparative-analysis.md"
        _group_labels = config.get("figures/agency_group_labels", {})
        _council_label = _group_labels.get(f"{config.get('identity/primary_city_slug', '')}-council", "City Council")
        outfile.write_text(
            f"# {_council_label} — Housing Advocacy Comparative Analysis\n\n{comparative}\n"
        )
        print(f"Saved: {outfile}")

    print("\nDone.")


if __name__ == "__main__":
    main()
