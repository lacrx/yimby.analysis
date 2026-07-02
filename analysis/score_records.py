#!/usr/bin/env python3
"""Score structured meeting records using policy-informed analysis.

Reads objective extraction data from watchdog, applies the ACTIONS OVER WORDS
scoring rubric with CA housing law context, and produces scored records.

Usage:
    python score_records.py                    # score all unscored records
    python score_records.py --force            # re-score all records
    python score_records.py --meeting 1423119  # score specific meeting
    python score_records.py --stats            # show scoring statistics
    python score_records.py --mode api         # use API instead of claude -p
"""

import argparse
import json
import os
import sys
from collections import defaultdict
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

from civic_utils import claude_local_call, claude_api_call, watchdog_data_dir

WATCHDOG_DATA = watchdog_data_dir()
STRUCTURED_DIR = WATCHDOG_DATA / "structured"
OUTPUT_DIR = REPO_ROOT / "output" / "scored"
SCORED_JSONL = OUTPUT_DIR / "scored-records.jsonl"
LOG_DIR = OUTPUT_DIR / "scoring-log"

SKILLS_DIR = WATCHDOG_DATA.parent / ".claude" / "skills"
SKILL_NAMES = ["ca-housing-law"]

MODE = "local"
client = None


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

SCORING_RUBRIC = """## Scoring Rubric: ACTIONS OVER WORDS

Score each meeting record based on what HAPPENED, not what was said.

### Advocacy Score (record-level)
- **green**: Actions align with state housing law compliance — approving compliant projects, upzoning, granting density bonus, complying with SB 79/SB 35
- **yellow**: Mixed signals — some pro-housing and some restrictive actions, or procedural items with housing implications
- **red**: Actions create legal exposure — denying compliant projects, downzoning, adding non-objective standards, deferring state mandates, weaponizing CEQA against housing
- **neutral**: No housing-relevant actions in this record

### Key Legal Tests
- SB 79 deferral/exemption: RED if deferring on sites that clearly qualify under the statute
- Density bonus exclusion from streamlining: RED — density bonus law (Gov. Code § 65915) is mandatory
- Raising inclusionary requirements: context-dependent. Raising citywide rates as policy = YELLOW (reasonable policy debate). Demanding a specific project exceed existing requirements to deny it = RED (inclusionary weaponization)
- Approving compliant housing projects: GREEN
- Consent calendar with routine items: NEUTRAL

### Per-Member Action Scoring
- **Strong Pro-Housing (+2)**: remove/raise density caps, eliminate parking minimums, approve housing projects, support state preemption (SB 9/10/35)
- **Moderate Pro-Housing (+1)**: inclusionary zoning requirements, tenant protections, streamline approvals, transit-oriented density
- **Anti-Housing (-1)**: maintain parking requirements, cite "community character" against density, oppose state mandates
- **Strong Anti-Housing (-2)**: add/maintain density caps, vote AGAINST housing projects, weaponize inclusionary as poison pill, weaponize CEQA
- **Neutral (0)**: procedural actions, non-housing items

### Critical Distinctions
- Tenant protections are genuinely pro-housing (+1). Never penalize tenant protection votes.
- Both market-rate and affordable housing add supply. Blocking either is anti-housing.
- Inclusionary weaponization test: raising citywide inclusionary rates = +1. Citing inclusionary shortfall to DENY a compliant project = -2.

### Output Format
Return ONLY valid JSON:
{
  "advocacy_score": "green | yellow | red | neutral",
  "advocacy_reason": "one sentence citing the specific action and applicable law",
  "position_scores": [
    {"member": "name", "action_summary": "what they did", "score": +2|+1|0|-1|-2, "rubric_cite": "which rubric line applies"}
  ]
}
"""


def call_claude(prompt, max_tokens=2000):
    if MODE == "local":
        return claude_local_call(prompt, system=SKILLS_CONTEXT)
    else:
        import anthropic
        global client
        if client is None:
            client = anthropic.Anthropic()
        resp = claude_api_call(
            client,
            model="claude-sonnet-4-6",
            max_tokens=max_tokens,
            system=SKILLS_CONTEXT,
            messages=[{"role": "user", "content": prompt}],
        )
        return resp.content[0].text


def format_record_for_scoring(record):
    """Format a meeting record as compact text for the scoring prompt."""
    lines = [f"Meeting: {record.get('body', '?')} — {record.get('date', '?')} — {record.get('agency', '?')}"]

    for v in record.get("votes", []):
        vote_line = f"VOTE: {v['item']} → {v['result']}"
        if v.get("yes"):
            vote_line += f" (yes: {', '.join(v['yes'])})"
        if v.get("no"):
            vote_line += f" (no: {', '.join(v['no'])})"
        lines.append(vote_line)

    for h in record.get("housing_items", []):
        h_line = f"HOUSING: [{h.get('type', '?')}] {h.get('description', '?')}"
        if h.get("outcome"):
            h_line += f" → {h['outcome']}"
        if h.get("state_law_flags"):
            h_line += f" [flags: {', '.join(h['state_law_flags'])}]"
        lines.append(h_line)

    for cp in record.get("council_positions", []):
        label = cp.get("action") or cp.get("stance", "unknown")
        lines.append(f"POSITION: {cp.get('member', '?')} — {label}: {cp.get('evidence', '')}")
        if cp.get("on"):
            lines[-1] += f" (on: {cp['on']})"

    for flag in record.get("legal_flags", []):
        lines.append(f"LEGAL: {flag}")

    for q in record.get("key_quotes", []):
        lines.append(f"QUOTE: {q}")

    return "\n".join(lines)


def score_record(record):
    """Score a single meeting record using the policy-informed rubric."""
    formatted = format_record_for_scoring(record)
    if len(formatted.strip().split("\n")) <= 1:
        return {"advocacy_score": "neutral", "advocacy_reason": "No substantive content", "position_scores": []}

    prompt = f"{SCORING_RUBRIC}\n\n---\n\nScore this meeting record:\n\n{formatted}"

    response = call_claude(prompt)
    if not response:
        return None

    try:
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        return json.loads(text)
    except (json.JSONDecodeError, IndexError):
        print(f"  Failed to parse scoring response")
        return None


def load_records(meeting_filter=None):
    """Load meeting records from watchdog structured data."""
    records = []
    merged_dir = STRUCTURED_DIR / "meetings"
    if not merged_dir.exists():
        print("No merged meetings directory found.")
        return records

    for jf in sorted(merged_dir.glob("*.json")):
        if meeting_filter and jf.stem not in meeting_filter:
            continue
        try:
            r = json.loads(jf.read_text())
            if r.get("procedural_only"):
                continue
            records.append(r)
        except Exception:
            continue

    return records


def load_existing_scores():
    """Load previously scored records."""
    scores = {}
    if SCORED_JSONL.exists():
        for line in SCORED_JSONL.read_text().splitlines():
            if line.strip():
                try:
                    r = json.loads(line)
                    scores[r.get("meeting_id", "")] = r
                except Exception:
                    continue
    return scores


def cmd_score(args):
    """Score meeting records."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    meeting_filter = set(args.meeting) if args.meeting else None
    records = load_records(meeting_filter)

    if not records:
        print("No records to score.")
        return

    existing = {} if args.force else load_existing_scores()
    to_score = [r for r in records if r.get("meeting_id") not in existing]

    if not to_score:
        print(f"All {len(records)} records already scored. Use --force to re-score.")
        return

    print(f"Scoring {len(to_score)} records ({len(records)} total, {len(existing)} already scored)")

    scored = dict(existing)
    success = 0
    failed = 0

    for i, record in enumerate(to_score):
        mid = record.get("meeting_id", "?")
        body = record.get("body", "?")
        date = record.get("date", "?")
        print(f"  [{i+1}/{len(to_score)}] {mid}: {body} — {date}...", end="", flush=True)

        result = score_record(record)
        if result:
            scored_record = {
                "meeting_id": mid,
                "date": date,
                "body": body,
                "agency": record.get("agency", ""),
                "advocacy_score": result.get("advocacy_score", "neutral"),
                "advocacy_reason": result.get("advocacy_reason", ""),
                "position_scores": result.get("position_scores", []),
                "votes": record.get("votes", []),
                "housing_items": record.get("housing_items", []),
            }
            scored[mid] = scored_record
            success += 1

            log_path = LOG_DIR / f"{mid}.json"
            log_path.write_text(json.dumps({
                "input": format_record_for_scoring(record),
                "output": result,
            }, indent=2))

            print(f" {result.get('advocacy_score', '?')}")
        else:
            failed += 1
            print(f" FAILED")

    with open(SCORED_JSONL, "w") as f:
        for r in sorted(scored.values(), key=lambda x: x.get("date", "")):
            f.write(json.dumps(r, default=str) + "\n")

    print(f"\nDone. {success} scored, {failed} failed. Total: {len(scored)} → {SCORED_JSONL}")


def cmd_stats(args):
    """Show scoring statistics."""
    if not SCORED_JSONL.exists():
        print("No scored records. Run score_records.py first.")
        return

    scores = defaultdict(int)
    total = 0
    member_scores = defaultdict(list)

    for line in SCORED_JSONL.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            total += 1
            scores[r.get("advocacy_score", "neutral")] += 1
            for ps in r.get("position_scores", []):
                member_scores[ps["member"]].append(ps.get("score", 0))
        except Exception:
            continue

    print(f"Scored records: {total}")
    print(f"\nAdvocacy scores:")
    for score in ["green", "yellow", "red", "neutral"]:
        bar = "█" * (scores[score] // 2) if scores[score] > 0 else ""
        print(f"  {score:8s}: {scores[score]:4d} {bar}")

    if member_scores:
        print(f"\nMember net scores (top 10):")
        ranked = sorted(member_scores.items(), key=lambda x: sum(x[1]), reverse=True)
        for member, action_scores in ranked[:10]:
            net = sum(action_scores)
            count = len(action_scores)
            print(f"  {member:25s}: net {net:+4d} ({count} actions)")


def main():
    parser = argparse.ArgumentParser(description="Score meeting records with policy-informed analysis")
    parser.add_argument("--force", action="store_true", help="Re-score all records")
    parser.add_argument("--meeting", nargs="+", help="Score specific meeting(s) by ID")
    parser.add_argument("--stats", action="store_true", help="Show scoring statistics")
    parser.add_argument("--mode", choices=["local", "api"], default="local", help="LLM mode")

    args = parser.parse_args()

    global MODE
    MODE = args.mode
    if MODE == "api":
        os.environ.get("ANTHROPIC_API_KEY") or sys.exit("ANTHROPIC_API_KEY required for API mode")

    if args.stats:
        cmd_stats(args)
    else:
        cmd_score(args)


if __name__ == "__main__":
    main()
