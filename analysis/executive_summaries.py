#!/usr/bin/env python3
"""Generate executive summaries by year from monthly digest JSONL.

Usage:
    python executive_summaries.py                              # claude -p + monthly digests ($0)
    python executive_summaries.py --mode api                   # Claude API + monthly digests ($)
    python executive_summaries.py --source jsonl               # per-record JSONL (more detail, more tokens)
"""

import argparse
import json
import os
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

from civic_utils import claude_local_call, all_meetings_dirs, watchdog_data_dir, load_scored_records, load_analysis_context, load_etrakit_stats, format_etrakit_for_prompt, load_downtown_apr_stats, format_downtown_apr_for_prompt
sys.path.insert(0, str(REPO_ROOT))
import config

WATCHDOG_DATA = watchdog_data_dir()
SUMMARIES_DIR = WATCHDOG_DATA / "summaries"
STRUCTURED_DIR = WATCHDOG_DATA / "structured"
MERGED_DIR = STRUCTURED_DIR / "meetings"
MONTHLY_DIR = STRUCTURED_DIR / "monthly"
OUTPUT_DIR = REPO_ROOT / "output" / "executive-summaries"

MODE = "api"  # set by main()
SCORED_DATA = {}  # set by main() — pre-computed scores from score_records.py
ETRAKIT_DATA = {}  # set by main() — per-year etrakit filing/permit stats
APR_DATA = {}  # set by main() — downtown vs rest-of-city APR filing stats
CITY_OVERRIDE = None  # set by main() when --agency is used
client = None  # initialized only in API mode


def _city_name():
    return CITY_OVERRIDE or config.get("identity/primary_city", "the jurisdiction")


SKILLS_CONTEXT = load_analysis_context()


def call_claude(prompt, max_tokens=2000, system=None, timeout=600):
    """Route to API or local based on MODE."""
    sys_ctx = system if system is not None else SKILLS_CONTEXT
    if MODE == "local":
        return claude_local_call(prompt, system=sys_ctx, timeout=timeout)
    else:
        from civic_utils import claude_api_call
        response = claude_api_call(
            client,
            model="claude-opus-4-6",
            max_tokens=max_tokens,
            system=sys_ctx,
            messages=[{"role": "user", "content": prompt}],
        )
        return response.content[0].text


def load_summaries_by_year():
    """Load prose summaries grouped by year and body."""
    meeting_info = {}
    for meetings_dir in all_meetings_dirs():
        for mdir in meetings_dir.iterdir():
            mf = mdir / "meeting.json"
            if not mf.exists():
                continue
            m = json.loads(mf.read_text())
            date = m.get("date", "")
            body = m.get("body", m.get("EventBodyName", ""))
            year = None
            for fmt in ["%m/%d/%Y", "%Y-%m-%d"]:
                try:
                    dt = datetime.strptime(
                        date.split("T")[0] if "T" in date else date, fmt
                    )
                    year = dt.year
                    break
                except ValueError:
                    pass
            meeting_info[mdir.name] = {"date": date, "body": body, "year": year}

    by_year = defaultdict(lambda: defaultdict(list))
    for sf in sorted(SUMMARIES_DIR.glob("*-summary.md")):
        mid = sf.stem.split("-")[0]
        info = meeting_info.get(mid, {})
        year = info.get("year")
        body = info.get("body", "Unknown")
        if year:
            by_year[year][body].append(
                {"date": info.get("date", ""), "content": sf.read_text()}
            )
    return by_year


def load_jsonl_by_year(agency_filter=None):
    """Load structured JSONL records grouped by year and body.

    agency_filter: if set, only include records whose agency field contains this string (case-insensitive).
    """
    jsonl_path = STRUCTURED_DIR / "all-records.jsonl"
    if not jsonl_path.exists():
        print(f"JSONL not found: {jsonl_path}")
        print("Run extract_structured.py first, or use --source prose")
        sys.exit(1)

    by_year = defaultdict(lambda: defaultdict(list))
    with open(jsonl_path) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            record = json.loads(line)
            if agency_filter:
                agency = str(record.get("agency", "") or "")
                if agency_filter.lower() not in agency.lower():
                    continue
            date = record.get("date", "")
            body = record.get("body") or "Unknown"
            year = None
            if date:
                try:
                    year = int(date[:4])
                except (ValueError, IndexError):
                    pass
            if year:
                by_year[year][body].append(record)

    return by_year


def load_monthly_by_year():
    """Load monthly digests grouped by year → list of digests (not per-body)."""
    monthly_jsonl = STRUCTURED_DIR / "monthly-digests.jsonl"
    if not monthly_jsonl.exists():
        print(f"Monthly digests not found: {monthly_jsonl}")
        print("Run monthly_rollup.py first, or use --source jsonl")
        sys.exit(1)

    by_year = defaultdict(list)
    with open(monthly_jsonl) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            digest = json.loads(line)
            month = digest.get("month", "")
            if len(month) < 4:
                continue
            year = int(month[:4])
            by_year[year].append(digest)

    return by_year


_DEFAULT_RELEVANCE_KEYWORDS = {
    "housing", "affordable", "cdbg", "home program", "inclusionary",
    "density", "zoning", "rezoning", "upzone", "land use", "general plan",
    "development", "permit", "entitlement", "subdivision",
    "infrastructure", "capital improvement", "cip", "sewer capacity", "water capacity",
    "transit", "bus", "active transport",
    "sidewalk", "bicycle", "pedestrian", "bike lane", "complete street",
    "parking", "impact fee", "development fee",
    "bond", "measure", "tax increment", "tif",
    "tenant", "rent", "eviction", "relocation", "displacement",
    "rhna",
    "vacancy", "blight", "code enforcement",
}
_config_keywords = config.get("analysis/relevance_keywords", [])
RELEVANCE_KEYWORDS = set(_config_keywords) if _config_keywords else _DEFAULT_RELEVANCE_KEYWORDS


def _is_relevant(text):
    """Check if text contains relevant policy keywords."""
    lower = text.lower()
    return any(kw in lower for kw in RELEVANCE_KEYWORDS)


def _filter_vote(vote):
    """Keep votes about housing/development/infrastructure topics."""
    item = vote.get("item", "")
    return _is_relevant(item) or vote.get("no") or vote.get("result", "").lower() in ("failed", "denied")


def _filter_fiscal(fiscal):
    """Keep fiscal items relevant to housing, infrastructure, or development capacity."""
    return _is_relevant(fiscal.get("description", ""))


def format_records_for_prompt(records, scored_data=None):
    """Format JSONL records as compact text for Claude, filtered to policy relevance.

    When scored_data is provided, annotates records with pre-computed advocacy scores.
    """
    parts = []
    for r in records:
        if r.get("procedural_only"):
            continue
        lines = [f"### {r.get('date', r.get('month', '?'))} — {r.get('doc_type', 'monthly digest')}"]

        mid = str(r.get("meeting_id", ""))
        scored = scored_data.get(mid) if scored_data else None
        if scored:
            lines.append(f"SCORED: {scored.get('advocacy_score', '?')} — {scored.get('advocacy_reason', '')}")
            for ps in scored.get("position_scores", []):
                if ps.get("score", 0) != 0:
                    lines.append(f"  MEMBER: {ps['member']} {ps.get('score', 0):+d} ({ps.get('action_summary', '')[:60]})")

        if r.get("activity_summary"):
            act = r["activity_summary"]
            lines.append(f"Activity: {act.get('total_votes', 0)} votes, {act.get('total_housing_items', 0)} housing items, {act.get('total_legal_flags', 0)} legal flags")
        for v in r.get("votes", []):
            if not _filter_vote(v):
                continue
            vote_line = f"VOTE: {v['item']} → {v['result']}"
            if v.get("no"):
                vote_line += f" (dissent: {', '.join(v['no'])})"
            lines.append(vote_line)
        for h in r.get("housing_items", []):
            h_line = f"HOUSING: [{h.get('type', '?')}] {h['description']}"
            if h.get("units"):
                h_line += f" ({h['units']} units)"
            if h.get("outcome"):
                h_line += f" → {h['outcome']}"
            if h.get("state_law_flags"):
                h_line += f" ⚠ {', '.join(h['state_law_flags'])}"
            lines.append(h_line)
        for f in r.get("fiscal_items", []):
            if not _filter_fiscal(f):
                continue
            f_line = f"FISCAL: {f['description']}"
            if f.get("amount"):
                f_line += f" (${f['amount']:,.0f})"
            lines.append(f_line)
        for flag in r.get("legal_flags", []):
            lines.append(f"LEGAL: {flag}")
        for cp in r.get("council_positions", []):
            label = cp.get('action') or cp.get('stance', 'unknown')
            lines.append(f"POSITION: {cp['member']} — {label}: {cp.get('evidence', '')}")
        for q in r.get("key_quotes", []):
            lines.append(f"QUOTE: {q}")

        if r.get("planning_projects"):
            pp = r["planning_projects"]
            lines.append(f"FILINGS: {pp.get('total', 0)} discretionary projects filed")
            if pp.get("by_type"):
                lines.append(f"  Types: {', '.join(f'{v} {k}' for k, v in pp['by_type'].items())}")
            for proj in pp.get("projects", []):
                p_line = f"  FILED: {proj.get('project_no', '?')} ({proj.get('type', '?')}): {proj.get('description', '')}"
                if proj.get("address"):
                    p_line += f" @ {proj['address']}"
                lines.append(p_line)

        if r.get("permits"):
            pm = r["permits"]
            lines.append(f"PERMITS: {pm.get('total', 0)} housing permits ({pm.get('estimated_units', '?')} est. units)")
            if pm.get("by_type"):
                lines.append(f"  Types: {', '.join(f'{v} {k}' for k, v in pm['by_type'].items())}")
            for proj in pm.get("projects", []):
                if proj.get("estimated_units", 0) > 1:
                    lines.append(f"  PROJECT: {proj.get('project_name', '?')} ({proj.get('estimated_units', '?')} units, {proj.get('permits_this_month', 0)} permits)")

        if r.get("permits_issued"):
            pi = r["permits_issued"]
            lines.append(f"ISSUED: {pi.get('total', 0)} permits issued ({pi.get('estimated_units', '?')} est. units)")

        if len(lines) > 1:
            parts.append("\n".join(lines))
    return "\n\n".join(parts)


def _month_cache_dir():
    return OUTPUT_DIR / "month-cache"


def chunk_records(records, max_chars=120000):
    """Group records into chunks that fit under max_chars when formatted.

    Works for both monthly digests and individual JSONL records.
    """
    chunks = []
    current = []
    current_size = 0
    sort_key = lambda x: x.get("month", x.get("date", ""))
    for r in sorted(records, key=sort_key):
        text = format_records_for_prompt([r])
        size = len(text)
        if size > max_chars:
            if current:
                chunks.append(current)
                current, current_size = [], 0
            chunks.append([r])
            continue
        if current_size + size > max_chars:
            chunks.append(current)
            current, current_size = [], 0
        current.append(r)
        current_size += size
    if current:
        chunks.append(current)
    return chunks


def chunk_months(digests, max_chars=120000):
    """Group monthly digests into chunks that fit under max_chars when formatted."""
    return chunk_records(digests, max_chars)


def summarize_month_chunk(chunk, year):
    """Summarize a chunk of monthly digests (1+ months)."""
    months = [d.get("month", "?") for d in chunk]
    bodies = sorted(set(b for d in chunk for b in d.get("bodies", [])))
    text = format_records_for_prompt(chunk, scored_data=SCORED_DATA)
    count = len([r for r in chunk if not r.get("procedural_only")])

    skip_system = len(text) > 100000
    if len(text) > 150000:
        text = text[:150000] + "\n[...truncated...]"

    label = months[0] if len(months) == 1 else f"{months[0]} to {months[-1]}"

    _city = _city_name()
    _state = config.get("identity/state", "")
    _advocate_role = config.get("advocacy/advocate_role", "housing advocate")
    prompt = f"""You are analyzing local government meeting records from {_city}, {_state} for a {_advocate_role}.

Period: {label}
Bodies active: {', '.join(bodies)}
Substantive records: {count}

Provide a concise executive summary of government activity for this period. Focus on:
- Housing project votes: approvals and denials, with unit counts, vote splits, and who dissented
- Zoning and density actions: any changes to density caps, parking requirements, single-family zoning, or land use designations
- Affordable housing: bond financing, inclusionary policy changes, CDBG/HOME allocations
- Tenant protections: rent stabilization, relocation assistance, just-cause eviction measures
- State housing mandate responses: RHNA compliance, SB 9/SB 10 implementation, "local control" resolutions
- Budget and infrastructure decisions relevant to development capacity
- Controversies or significant public opposition on housing/development items
- Individual council member votes on contested items — name who voted which way

Analytical framework for housing assessment:
- Approving housing projects (market-rate or affordable) = pro-housing. Blocking them = anti-housing.
- Removing or raising density caps = pro-housing. Adding or maintaining caps = anti-housing.
- Tenant protections (relocation assistance, just-cause eviction, rent stabilization) are genuinely pro-housing.
- Using high inclusionary rates to block a SPECIFIC compliant project is anti-housing (poison pill). Raising citywide inclusionary rates as policy is pro-housing.
- Judge by VOTES, not rhetoric. Note when stated positions contradict voting records.

Be specific — name projects, vote counts, dollar amounts, who dissented. Skip procedural items.
Keep it to 400-600 words.

CRITICAL: Only name individuals who appear BY NAME in the source data below. NEVER invent, guess, or fill in names. Hallucinating names is worse than leaving a gap.

Meeting data:
{text}"""

    return call_claude(prompt, max_tokens=2000, system="" if skip_system else None)


def summarize_year_monthly(year, digests):
    """Summarize a year using cached per-chunk monthly summaries."""
    _month_cache_dir().mkdir(parents=True, exist_ok=True)
    chunks = chunk_months(digests)

    chunk_summaries = []
    for i, chunk in enumerate(chunks):
        months = [d.get("month", "?") for d in chunk]
        label = months[0] if len(months) == 1 else f"{months[0]}_to_{months[-1]}"
        cache_file = _month_cache_dir() / f"{year}-{label}.md"

        if cache_file.exists() and cache_file.stat().st_size > 100:
            print(f"  Cached: {label}")
            chunk_summaries.append(cache_file.read_text())
            continue

        print(f"  Summarizing {label} ({len(chunk)} digests)...")
        result = summarize_month_chunk(chunk, year)
        if result is None:
            print(f"  WARNING: claude -p returned None for {label}, skipping.")
            continue
        cache_file.write_text(result)
        chunk_summaries.append(result)

    return chunk_summaries


def _body_cache_dir():
    return OUTPUT_DIR / "body-cache"


def _body_summary_prompt(body, year, text, count, source="jsonl"):
    _city = _city_name()
    _state = config.get("identity/state", "")
    _advocate_role = config.get("advocacy/advocate_role", "housing advocate")
    return f"""You are analyzing local government meeting {'records' if source == 'jsonl' else 'summaries'} from {_city}, {_state} for a {_advocate_role}.

Body: {body}
Year: {year}
Number of {'substantive records' if source == 'jsonl' else 'meetings'}: {count}

Provide a concise executive summary of this body's activity for the year. Focus on:
- Housing project votes: approvals and denials, with unit counts, vote splits, and who dissented
- Zoning and density actions: any changes to density caps, parking requirements, single-family zoning, or land use designations
- Affordable housing: bond financing, inclusionary policy changes, CDBG/HOME allocations
- Tenant protections: rent stabilization, relocation assistance, just-cause eviction measures
- State housing mandate responses: RHNA compliance, SB 9/SB 10 implementation, "local control" resolutions
- Budget and infrastructure decisions relevant to development capacity
- Controversies or significant public opposition on housing/development items
- Individual council member votes on contested items — name who voted which way

Analytical framework for housing assessment:
- Approving housing projects (market-rate or affordable) = pro-housing. Blocking them = anti-housing.
- Removing or raising density caps = pro-housing. Adding or maintaining caps = anti-housing.
- Tenant protections (relocation assistance, just-cause eviction, rent stabilization) are genuinely pro-housing.
- Using high inclusionary rates to block a SPECIFIC compliant project is anti-housing (poison pill). Raising citywide inclusionary rates as policy is pro-housing.
- Judge by VOTES, not rhetoric. Note when stated positions contradict voting records.

Be specific — name projects, vote counts, dollar amounts, who dissented. Skip procedural items.
Keep it to 400-600 words.

CRITICAL: Only name individuals who appear BY NAME in the source data below. NEVER invent, guess, or fill in names. Hallucinating names is worse than leaving a gap.

Meeting data:
{text}"""


def summarize_body_year(body, year, summaries, source="prose"):
    """Summarize a body's year of activity, chunking large record sets."""
    if source in ("jsonl", "monthly"):
        text = format_records_for_prompt(summaries, scored_data=SCORED_DATA)
        count = len([r for r in summaries if not r.get("procedural_only")])
    else:
        combined = []
        for s in summaries:
            combined.append(f"### {s['date']}\n{s['content']}\n")
        text = "\n".join(combined)
        count = len(summaries)

    if len(text) <= 100000:
        prompt = _body_summary_prompt(body, year, text, count, source)
        return call_claude(prompt, max_tokens=2000)

    if source not in ("jsonl", "monthly"):
        text = text[:150000] + "\n[...truncated...]"
        prompt = _body_summary_prompt(body, year, text, count, source)
        return call_claude(prompt, max_tokens=2000)

    _body_cache_dir().mkdir(parents=True, exist_ok=True)
    chunks = chunk_records(summaries)
    body_slug = body.lower().replace(" ", "-").replace("/", "-")[:60]
    print(f"    Chunking {body} into {len(chunks)} pieces ({len(text):,} chars total)")

    chunk_summaries = []
    for i, chunk in enumerate(chunks):
        dates = sorted(set(r.get("date", "?") for r in chunk))
        label = f"{dates[0]}_{dates[-1]}" if len(dates) > 1 else dates[0]
        cache_file = _body_cache_dir() / f"{year}-{body_slug}-chunk{i}-{label}.md"

        if cache_file.exists() and cache_file.stat().st_size > 100:
            print(f"    Cached: chunk {i+1}/{len(chunks)}")
            chunk_summaries.append(cache_file.read_text())
            continue

        chunk_text = format_records_for_prompt(chunk, scored_data=SCORED_DATA)
        chunk_count = len([r for r in chunk if not r.get("procedural_only")])
        print(f"    Summarizing chunk {i+1}/{len(chunks)} ({chunk_count} records, {len(chunk_text):,} chars)...")
        prompt = _body_summary_prompt(body, year, chunk_text, chunk_count, source)
        result = call_claude(prompt, max_tokens=2000)
        if result is None:
            print(f"    WARNING: chunk {i+1} returned None, skipping.")
            continue
        cache_file.write_text(result)
        chunk_summaries.append(result)

    if not chunk_summaries:
        return None
    if len(chunk_summaries) == 1:
        return chunk_summaries[0]

    combined_chunks = "\n\n".join(
        f"### Part {i+1}\n{s}" for i, s in enumerate(chunk_summaries)
    )
    _city = _city_name()
    _state = config.get("identity/state", "")
    _advocate_role = config.get("advocacy/advocate_role", "housing advocate")
    merge_prompt = f"""You are synthesizing chunked summaries of {body} activity in {_city}, {_state} for {year}, for a {_advocate_role}.

Below are {len(chunk_summaries)} partial summaries covering different periods of the year. Merge into one cohesive summary of this body's full year.

Preserve all specific details: project names, unit counts, vote splits, individual votes, dollar amounts. Do not generalize away specifics.
Keep it to 400-600 words.

CRITICAL: Only name individuals who appear BY NAME below. NEVER invent names.

Partial summaries:
{combined_chunks}"""
    return call_claude(merge_prompt, max_tokens=2000, system="")


def combine_year_summary(year, body_summaries):
    combined = "\n\n".join(
        f"## {body}\n{summary}" for body, summary in body_summaries.items()
    )

    etrakit_section = ""
    if year in ETRAKIT_DATA and ETRAKIT_DATA[year]["filings"]["total"] > 0:
        prev = ETRAKIT_DATA.get(year - 1)
        etrakit_section = "\n\n" + format_etrakit_for_prompt(ETRAKIT_DATA[year], prev_stats=prev)
    downtown_section = format_downtown_apr_for_prompt(APR_DATA, year)
    if downtown_section:
        etrakit_section += "\n\n" + downtown_section

    _city = _city_name()
    _state = config.get("identity/state", "")
    _advocate_role = config.get("advocacy/advocate_role", "housing advocate")
    prompt = f"""You are writing an executive summary of {_city}, {_state} city governance for {year}, for a {_advocate_role}.

Below are summaries of government activity for the year, followed by development pipeline data (discretionary project filings and building permit activity from etrakit). Synthesize into a single cohesive executive summary.

PRIORITY ORDER — structural decisions before project-level decisions. A density cap shapes every project that follows; an individual project approval is just one outcome within that structure. Lead with what changed the rules, then show what happened under those rules.

1. Opens with 2-3 sentences capturing the year's character for housing production and tenant protection
2. **Zoning and density**: Any changes to density caps, parking minimums, land use redesignations, inclusionary rates, discretionary review authority, fee structures. Note direction (more restrictive or less). These are the HIGHEST IMPACT decisions — they shape the entire regulatory environment for years. Connect policy changes to filing trends where the data supports it.
3. **Development pipeline**: Analyze discretionary filing trends (are developers filing more or fewer projects?), building permit issuance, and construction completions. Filings are a LEADING INDICATOR of council policy — when council imposes restrictions, filings drop. When they streamline, filings rise. A drop in filings after a policy change IS the finding. This section connects cause (structural decisions above) to effect (market response).
4. **Tenant protections**: Rent stabilization, relocation assistance, just-cause eviction — what passed, what failed, who voted which way.
5. **State mandate compliance**: RHNA progress, responses to state housing laws (SB 79, HAA, density bonus), any "local control" resolutions or coalition opposition.
6. **Housing supply table**: List every housing project voted on, with unit counts, vote splits, who dissented, and outcome. Note which projects were shielded by state law (HAA, SB 330 vesting, density bonus) vs. genuinely discretionary approvals. Individual project votes matter, but they are outcomes within the structural framework above — not the story itself.
7. **Affordable housing finance**: Bonds, CDBG/HOME allocations, inclusionary policy.
8. **Infrastructure and budget** relevant to development capacity.
9. **Council member vote patterns**: Who voted for structural changes (density caps, zoning, tenant protections)? Those votes reveal more than project votes, since most project votes are legally compelled. Focus on contested structural votes where members had real discretion.

Analytical framework:
- ACTIONS OVER WORDS. Assess the council's year by what it approved and blocked, not what members said.
- Tenant protections are genuinely pro-housing policy.
- Using high inclusionary rates to kill a specific compliant project is anti-housing, even though inclusionary policy itself is pro-housing.
- Note supply skepticism: members who claim to support housing but vote against projects.
- Development filings measure market confidence in the regulatory environment. Declining filings after restrictive policy = the market responding to council's signal.
- Distinguish state-law-compelled approvals (HAA, density bonus, SB 35) from genuine discretionary approvals. Compliance is not leadership.
- Structural changes (upzoning, cap removal, parking elimination) create future capacity. Structural restrictions (density caps, expanded discretionary review) destroy it.
- Declining development filings after restrictive policy changes are CONFIRMING EVIDENCE of anti-housing impact.

Do NOT assign letter grades or numerical scores to the city or to individual council members. Present the factual record — votes, trends, contradictions — and let it speak for itself.

Be specific. Name projects, dollar amounts, vote outcomes, individual votes on contested items.

CRITICAL: Only name individuals who appear BY NAME in the body summaries below. NEVER invent, guess, or fill in names. If individual votes are not documented, say so. Hallucinating names is worse than leaving a gap.

Target length: 1200-1800 words.

Body summaries:
{combined}{etrakit_section}"""

    return call_claude(prompt, max_tokens=5000, system="", timeout=900)


def create_overall_summary(year_summaries):
    combined = "\n\n---\n\n".join(
        f"# {year}\n{summary}" for year, summary in sorted(year_summaries.items())
    )

    _city = _city_name()
    _state = config.get("identity/state", "")
    _advocate_role = config.get("advocacy/advocate_role", "housing advocate")
    years_covered = sorted(year_summaries.keys())
    prompt = f"""You are writing a comprehensive executive summary covering {_city}, {_state} city governance from {years_covered[0]} through {years_covered[-1]}. This is for a {_advocate_role}.

Below are executive summaries for each year. Synthesize into a single narrative:

1. **The arc of housing production**: How many units were approved vs blocked? What was the net direction — more housing or less? Which projects were the biggest wins and losses?
2. **Council member vote patterns across years**: Who consistently approved housing? Who consistently blocked it? Who used pro-housing rhetoric while voting anti-housing (supply skepticism)? Track individual patterns across years — are members getting better or worse?
3. **Density and zoning trajectory**: Did density caps go up or down? Did parking requirements change? Did the city respond to state mandates or resist them?
4. **Tenant protection trajectory**: What passed, what failed, and who was on which side?
5. **The inclusionary question**: Was inclusionary policy used to genuinely increase affordable production, or weaponized to block specific projects?
6. **Turning points**: Which votes or policy changes most significantly shaped the housing landscape?
7. **Current trajectory**: Is the council moving toward more housing production or less? What does the advocate need to watch?

Analytical framework:
- ACTIONS OVER WORDS. A member's speeches and resolutions are noise. Their votes on projects, density, and zoning are signal.
- Tenant protections (relocation assistance, just-cause eviction, rent stabilization) are genuinely pro-housing.
- Using inclusionary rates to kill specific compliant projects is anti-housing. Raising citywide inclusionary rates is pro-housing.
- Supply skepticism — claiming to support housing while blocking projects — is the most common and most damaging form of anti-housing politics. Name it when the vote record shows it.
- Both market-rate and affordable housing add supply. Blocking either is anti-housing.

Be specific — name projects, policies, vote patterns, dollar amounts. Be analytical, not descriptive. This is an advocacy tool.

CRITICAL: Only name individuals who appear BY NAME in the year summaries below. NEVER invent, guess, or fill in names of council members, commissioners, or other officials. If you are uncertain whether someone served in a given year, do not name them. Hallucinating names is worse than leaving a gap.

Target length: 1500-2500 words.

Year-by-year summaries:
{combined}"""

    return call_claude(prompt, max_tokens=6000, system="")


def year_summary_done(year):
    """Check if a year summary already has real content (not just a stub)."""
    label = f"{year}" if year != 2021 else "2020-2021"
    outfile = OUTPUT_DIR / f"executive-summary-{label}.md"
    return outfile.exists() and outfile.stat().st_size > 200


def main():
    global MODE, client, CITY_OVERRIDE, OUTPUT_DIR

    parser = argparse.ArgumentParser(description="Generate executive summaries")
    parser.add_argument("--mode", choices=["api", "local"], default="local",
                        help="api=Claude API ($), local=claude -p (subscription, $0)")
    parser.add_argument("--source", choices=["monthly", "jsonl"], default="monthly",
                        help="monthly=monthly digests (default, 7x smaller), jsonl=individual records")
    parser.add_argument("--year", type=int,
                        help="Generate summary for a single year only")
    parser.add_argument("--agency", type=str,
                        help="Filter to a specific agency (substring match, e.g. 'Carlsbad')")
    parser.add_argument("--force", action="store_true",
                        help="Regenerate even if cached/completed")
    args = parser.parse_args()

    MODE = args.mode
    source = args.source

    if MODE == "api":
        import anthropic
        client = anthropic.Anthropic()

    global SCORED_DATA, ETRAKIT_DATA, APR_DATA
    SCORED_DATA = load_scored_records()

    agency_filter = args.agency
    # Permit data dirs are slugged with underscores (e.g. "solana_beach"), so a
    # multi-word --agency has to be slugged the same way or the lookup silently
    # finds nothing. Agencies on non-etrakit platforms return zeros, which the
    # prompt builder already skips.
    etrakit_city = (agency_filter.lower().replace(" ", "_").replace("-", "_")
                    if agency_filter else config.get("identity/primary_city_slug", "oceanside"))
    apr_city = etrakit_city

    for yr in range(2020, 2027):
        ETRAKIT_DATA[yr] = load_etrakit_stats(yr, city=etrakit_city)
    APR_DATA = load_downtown_apr_stats(city=apr_city)
    etrakit_years = [yr for yr in ETRAKIT_DATA if ETRAKIT_DATA[yr].get("filings", {}).get("total", 0) > 0]
    apr_years = sorted(APR_DATA.keys()) if APR_DATA else []
    print(f"Mode: {MODE} | Source: {source} | Force: {args.force} | Scored: {len(SCORED_DATA)} records | Etrakit: {sorted(etrakit_years)} | APR downtown: {apr_years}")

    if agency_filter:
        CITY_OVERRIDE = agency_filter.title()
        agency_slug = agency_filter.lower().replace(" ", "-")
        OUTPUT_DIR = REPO_ROOT / "output" / "executive-summaries" / agency_slug
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)

    if agency_filter and source == "monthly":
        print(f"WARNING: --agency filter requires --source jsonl (monthly digests are multi-agency).")
        print(f"Switching to --source jsonl.")
        source = "jsonl"

    if source == "monthly":
        by_year_raw = load_monthly_by_year()
    elif source == "jsonl":
        by_year_raw = load_jsonl_by_year(agency_filter=agency_filter)
    else:
        by_year_raw = load_summaries_by_year()

    year_summaries = {}

    years_to_process = sorted(by_year_raw.keys())

    if args.year:
        if args.year not in by_year_raw:
            print(f"No data for year {args.year}. Available: {sorted(by_year_raw.keys())}")
            return
        years_to_process = [args.year]

    if source == "monthly":
        # Monthly path: digests are per-month (not per-body), merge 2020 into 2021
        if 2020 in by_year_raw and 2021 in by_year_raw:
            by_year_raw[2021] = by_year_raw[2020] + by_year_raw[2021]
            years_to_process = [y for y in years_to_process if y != 2020]

        for year in years_to_process:
            digests = by_year_raw[year]
            label = f"{year}" if year != 2021 else "2020-2021"

            if not args.force and year_summary_done(year):
                outfile = OUTPUT_DIR / f"executive-summary-{label}.md"
                content = outfile.read_text()
                header_end = content.find("\n\n")
                year_summaries[year] = content[header_end+2:] if header_end > 0 else content
                print(f"\n{'='*60}")
                print(f"Skipping {label} (already done, {outfile.stat().st_size:,} bytes)")
                print(f"{'='*60}")
                continue

            print(f"\n{'='*60}")
            print(f"Processing {label} ({len(digests)} monthly digests)")
            print(f"{'='*60}")

            chunk_summaries = summarize_year_monthly(year, digests)
            if not chunk_summaries:
                print(f"  WARNING: No chunk summaries produced for {label}.")
                continue

            combined = "\n\n".join(
                f"## Period {i+1}\n{s}" for i, s in enumerate(chunk_summaries)
            )
            print(f"  Combining {len(chunk_summaries)} chunk summaries into year summary...")
            year_exec = combine_year_summary(year, {"monthly data": combined})
            year_summaries[year] = year_exec

            outfile = OUTPUT_DIR / f"executive-summary-{label}.md"
            outfile.write_text(f"# {_city_name()}, {config.get('identity/state', '')} — Executive Summary {label}\n\n{year_exec}\n")
            print(f"  Saved: {outfile} ({outfile.stat().st_size:,} bytes)")

    else:
        # JSONL/prose path: per-body (original logic)
        by_year = by_year_raw
        if not agency_filter and 2020 in by_year and 2021 in by_year:
            for body, summaries in by_year[2020].items():
                by_year[2021][body] = summaries + by_year[2021].get(body, [])
            years_to_process = [y for y in years_to_process if y != 2020]

        for year in years_to_process:
            label = f"{year}" if (year != 2021 or agency_filter) else "2020-2021"
            if not args.force and year_summary_done(year):
                outfile = OUTPUT_DIR / f"executive-summary-{label}.md"
                content = outfile.read_text()
                header_end = content.find("\n\n")
                year_summaries[year] = content[header_end+2:] if header_end > 0 else content
                print(f"\n{'='*60}")
                print(f"Skipping {label} (already done)")
                print(f"{'='*60}")
                continue

            bodies = by_year[year]
            total = sum(len(s) for s in bodies.values())
            print(f"\n{'='*60}")
            print(f"Processing {year} ({total} records across {len(bodies)} bodies)")
            print(f"{'='*60}")

            body_summaries = {}
            for body in sorted(bodies.keys()):
                summaries = bodies[body]
                print(f"  Summarizing {body} ({len(summaries)} records)...")
                result = summarize_body_year(body, year, summaries, source=source)
                if result is None:
                    print(f"  WARNING: claude -p returned None for {body} {year}, skipping.")
                    continue
                body_summaries[body] = result

            print(f"  Combining into year executive summary...")
            year_exec = combine_year_summary(year, body_summaries)
            year_summaries[year] = year_exec

            outfile = OUTPUT_DIR / f"executive-summary-{label}.md"
            outfile.write_text(f"# {_city_name()}, {config.get('identity/state', '')} — Executive Summary {label}\n\n{year_exec}\n")
            print(f"  Saved: {outfile}")

    # Overall summary (skip for single-year runs)
    if args.year:
        print("\nDone (single-year mode, skipping overall synthesis).")
        return

    overall_file = OUTPUT_DIR / "executive-summary-full.md"
    if not args.force and overall_file.exists() and overall_file.stat().st_size > 500:
        all_years_done = all(year_summary_done(y) for y in years_to_process)
        if all_years_done:
            print(f"\nOverall summary exists ({overall_file.stat().st_size:,} bytes), skipping.")
            print("Use --force to regenerate.")
            print("\nDone.")
            return

    print(f"\n{'='*60}")
    print("Creating overall executive summary...")
    print(f"{'='*60}")
    overall = create_overall_summary(year_summaries)
    overall_file.write_text(
        f"# {_city_name()}, {config.get('identity/state', '')} — Comprehensive Executive Summary\n\n{overall}\n"
    )
    print(f"Saved: {overall_file} ({overall_file.stat().st_size:,} bytes)")
    print("\nDone.")


if __name__ == "__main__":
    main()
