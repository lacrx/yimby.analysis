#!/usr/bin/env python3
"""Score structured meeting records using hybrid deterministic + LLM analysis.

Deterministic code handles ~80% of scoring (clear rubric lookups). LLM-as-judge
resolves ambiguous cases: inclusionary weaponization, CEQA weaponization,
community character dogwhistles. Every score has an audit trail back to source
data and rubric line.

Usage:
    python score_records.py                    # score all unscored records
    python score_records.py --force            # re-score all records
    python score_records.py --meeting 1423119  # score specific meeting
    python score_records.py --stats            # show scoring statistics
    python score_records.py --mode api         # use API instead of claude -p
    python score_records.py --dry-run          # show scores without writing
"""

import argparse
import json
import os
import re
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


# ---------------------------------------------------------------------------
# Rubric tables — deterministic score mappings
# ---------------------------------------------------------------------------

HOUSING_OUTCOME_SCORES = {
    ("zoning", "approved"): +2,
    ("zoning", "denied"): -2,
    ("density", "approved"): +2,
    ("density", "denied"): -2,
    ("permit", "approved"): +2,
    ("permit", "denied"): -2,
    ("affordable", "approved"): +2,
    ("affordable", "denied"): -2,
    ("adu", "approved"): +2,
    ("adu", "denied"): -2,
    ("transit_oriented", "approved"): +2,
    ("transit_oriented", "denied"): -2,
    ("state_compliance", "approved"): +1,
    ("state_compliance", "denied"): -1,
}

RUBRIC_LABELS = {
    +2: "approve housing projects → +2",
    +1: "moderate pro-housing action → +1",
    -1: "anti-housing action → -1",
    -2: "vote AGAINST housing projects → -2",
}

POSITION_ACTION_BASE = {
    "voted yes": +2,
    "voted no": -2,
    "moved": +1,
    "seconded": +1,
    "spoke for": +1,
    "spoke against": None,  # ambiguous — needs context
    "amended": 0,
    "abstained": 0,
    "absent": 0,
}

STANCE_SCORES = {
    "pro_housing": +1,
    "anti_housing": -1,
    "mixed": None,       # ambiguous — needs LLM
    "procedural": 0,
}

HIGH_EXPOSURE_FLAGS = {"HAA", "SB330", "SB35", "SB79", "density_bonus"}

DOGWHISTLE_PATTERNS = [
    r"community\s+character",
    r"neighborhood\s+compatib",
    r"neighborhood\s+character",
    r"preserve\s+the\s+character",
    r"scale\s+and\s+mass",
    r"out\s+of\s+character",
    r"too\s+(tall|dense|big|large|massive)",
    r"doesn'?t\s+fit",
    r"traffic\s+(impact|concern|worsen)",
    r"parking\s+(concern|impact|shortage|problem)",
]

DOGWHISTLE_RE = re.compile("|".join(DOGWHISTLE_PATTERNS), re.IGNORECASE)


# ---------------------------------------------------------------------------
# Skills / LLM support (only used for ambiguity resolution)
# ---------------------------------------------------------------------------

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


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_records(meeting_filter=None):
    """Load meeting records from meetings-combined.jsonl."""
    combined = STRUCTURED_DIR / "meetings-combined.jsonl"
    if not combined.exists():
        print(f"Not found: {combined}")
        return []

    records = []
    for line in combined.open():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            if r.get("procedural_only"):
                continue
            if meeting_filter and str(r.get("meeting_id", "")) not in meeting_filter:
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
                    scores[str(r.get("meeting_id", ""))] = r
                except Exception:
                    continue
    return scores


# ---------------------------------------------------------------------------
# Deterministic scoring
# ---------------------------------------------------------------------------

def _match_position_to_housing(position, housing_items):
    """Try to link a council_position to a housing item by text similarity."""
    on_text = (position.get("on") or "").lower()
    evidence = (position.get("evidence") or "").lower()
    combined = on_text + " " + evidence

    if not combined.strip():
        return None

    best_idx = None
    best_overlap = 0
    for i, h in enumerate(housing_items):
        desc = (h.get("description") or "").lower()
        if not desc:
            continue
        desc_words = set(desc.split())
        combined_words = set(combined.split())
        overlap = len(desc_words & combined_words)
        if overlap > best_overlap and overlap >= 3:
            best_overlap = overlap
            best_idx = i

    return best_idx


def score_deterministic(record):
    """Score a meeting record using deterministic rubric lookup.

    Returns (action_scores, flags) where:
    - action_scores: list of ActionScore dicts with full audit trail
    - flags: list of legal exposure flags
    """
    action_scores = []
    flags = []
    housing_items = record.get("housing_items", [])
    votes = record.get("votes", [])
    positions = record.get("council_positions", [])
    legal_flags = record.get("legal_flags", [])

    # 1. Score housing items by outcome
    for idx, h in enumerate(housing_items):
        htype = h.get("type", "")
        outcome = h.get("outcome", "")
        state_flags = h.get("state_law_flags", [])
        desc = h.get("description", "?")

        score = HOUSING_OUTCOME_SCORES.get((htype, outcome))
        if score is None:
            continue

        action_scores.append({
            "member": None,
            "score": score,
            "method": "deterministic",
            "source_type": "housing_item",
            "source_idx": idx,
            "item_summary": f"[{htype}] {desc[:80]} → {outcome}",
            "rubric_line": RUBRIC_LABELS.get(score, f"housing {outcome} → {score:+d}"),
            "evidence": outcome,
        })

        if outcome == "denied" and set(state_flags) & HIGH_EXPOSURE_FLAGS:
            flags.append(f"State law exposure: {', '.join(state_flags)} — {desc[:60]} denied")

    # 2. Score per-member positions on housing items
    for idx, pos in enumerate(positions):
        member = pos.get("member", "?")
        action = pos.get("action", "")
        stance = pos.get("stance", "")
        evidence_text = pos.get("evidence", "")

        # Try to link to a housing item
        housing_idx = _match_position_to_housing(pos, housing_items)

        # Stance-based scoring (dominant pattern: 97% of positions)
        if stance and not action:
            stance_score = STANCE_SCORES.get(stance)
            if stance_score is None:
                continue  # "mixed" → handled by ambiguity detection
            if stance_score == 0:
                continue  # "procedural" → no score

            h_desc = ""
            if housing_idx is not None:
                h_desc = housing_items[housing_idx].get("description", "?")[:60]
            elif not housing_items:
                continue  # no housing context for this stance

            action_scores.append({
                "member": member,
                "score": stance_score,
                "method": "deterministic",
                "source_type": "position",
                "source_idx": idx,
                "item_summary": f"stance:{stance} — {h_desc or evidence_text[:60]}",
                "rubric_line": f"{stance} stance → {stance_score:+d}",
                "evidence": evidence_text[:100],
            })
            continue

        # Action-based scoring (minority pattern: moved/seconded/voted no/etc)
        if not action:
            continue

        is_housing_action = housing_idx is not None
        if not is_housing_action:
            continue

        h = housing_items[housing_idx]
        h_desc = h.get("description", "?")[:60]
        h_outcome = h.get("outcome", "?")

        base_score = POSITION_ACTION_BASE.get(action)
        if base_score is None:
            continue

        if base_score == 0:
            continue  # abstained/absent/amended — no score

        if action == "voted yes" and h_outcome == "approved":
            score = +2
            rubric = "voted yes on approved housing → +2"
        elif action == "voted no":
            score = -2
            rubric = "voted no on housing project → -2"
        elif action in ("moved", "seconded"):
            score = +1
            rubric = f"{action} housing item → +1"
        elif action == "spoke for":
            score = +1
            rubric = "spoke for housing → +1"
        else:
            score = base_score
            rubric = RUBRIC_LABELS.get(score, f"{action} → {score:+d}")

        action_scores.append({
            "member": member,
            "score": score,
            "method": "deterministic",
            "source_type": "position",
            "source_idx": idx,
            "item_summary": f"{action} on {h_desc}",
            "rubric_line": rubric,
            "evidence": evidence_text[:100],
        })

    # 3. Score voters from vote records cross-referenced with housing items
    for vote in votes:
        vote_item = (vote.get("item") or "").lower()
        result = (vote.get("result") or "").lower()

        matched_housing = None
        for i, h in enumerate(housing_items):
            desc = (h.get("description") or "").lower()
            if not desc:
                continue
            desc_words = set(desc.split())
            vote_words = set(vote_item.split())
            if len(desc_words & vote_words) >= 4:
                matched_housing = i
                break

        if matched_housing is None:
            continue

        h = housing_items[matched_housing]
        h_desc = h.get("description", "?")[:60]

        is_approved = "approved" in result or "passed" in result
        is_denied = "denied" in result or "failed" in result

        if not (is_approved or is_denied):
            continue

        already_scored = {
            a["member"] for a in action_scores
            if a["source_type"] == "position" and a["member"]
        }

        for name in vote.get("yes", []):
            if name in already_scored:
                continue
            score = +2 if is_approved else +2
            action_scores.append({
                "member": name,
                "score": score,
                "method": "deterministic",
                "source_type": "vote",
                "source_idx": matched_housing,
                "item_summary": f"voted yes on {h_desc}",
                "rubric_line": "voted yes on housing → +2",
                "evidence": f"yes vote, result: {result}",
            })

        for name in vote.get("no", []):
            if name in already_scored:
                continue
            score = -2
            action_scores.append({
                "member": name,
                "score": score,
                "method": "deterministic",
                "source_type": "vote",
                "source_idx": matched_housing,
                "item_summary": f"voted no on {h_desc}",
                "rubric_line": "voted no on housing → -2",
                "evidence": f"no vote, result: {result}",
            })

    return action_scores, flags


# ---------------------------------------------------------------------------
# Ambiguity detection
# ---------------------------------------------------------------------------

def detect_ambiguities(record):
    """Detect cases needing LLM interpretation. Returns list of ambiguity dicts."""
    ambiguities = []
    housing_items = record.get("housing_items", [])
    positions = record.get("council_positions", [])
    quotes = record.get("key_quotes", [])
    legal_flags = record.get("legal_flags", [])
    has_housing = len(housing_items) > 0

    # a) Community character dogwhistles in positions/quotes
    if has_housing:
        for idx, pos in enumerate(positions):
            evidence = pos.get("evidence", "")
            if DOGWHISTLE_RE.search(evidence):
                ambiguities.append({
                    "type": "dogwhistle",
                    "source": "position",
                    "source_idx": idx,
                    "member": pos.get("member", "?"),
                    "text": evidence,
                    "context": f"Action: {pos.get('action', '?')} on: {pos.get('on', '?')}",
                })

        for idx, quote in enumerate(quotes):
            if DOGWHISTLE_RE.search(quote):
                ambiguities.append({
                    "type": "dogwhistle",
                    "source": "quote",
                    "source_idx": idx,
                    "member": None,
                    "text": quote,
                    "context": "key quote from meeting",
                })

    # a2) Mixed stances on housing items — need LLM to interpret
    if has_housing:
        for idx, pos in enumerate(positions):
            stance = pos.get("stance", "")
            if stance == "mixed":
                ambiguities.append({
                    "type": "mixed_stance",
                    "source": "position",
                    "source_idx": idx,
                    "member": pos.get("member", "?"),
                    "text": pos.get("evidence", ""),
                    "context": f"Stance: mixed on: {pos.get('on', '?')}",
                })

    # b) Inclusionary weaponization
    for idx, pos in enumerate(positions):
        action = pos.get("action") or pos.get("stance", "")
        evidence = (pos.get("evidence") or "").lower()
        if action in ("spoke against", "voted no", "anti_housing") and "inclusionary" in evidence:
            ambiguities.append({
                "type": "inclusionary_weaponization",
                "source": "position",
                "source_idx": idx,
                "member": pos.get("member", "?"),
                "text": pos.get("evidence", ""),
                "context": f"Action: {action} on: {pos.get('on', '?')}",
            })

    # c) CEQA weaponization
    ceqa_in_flags = any("ceqa" in f.lower() for f in legal_flags)
    denied_housing = any(h.get("outcome") == "denied" for h in housing_items)
    continued_housing = any(h.get("outcome") == "continued" for h in housing_items)

    if ceqa_in_flags and (denied_housing or continued_housing):
        ambiguities.append({
            "type": "ceqa_weaponization",
            "source": "legal_flags",
            "source_idx": None,
            "member": None,
            "text": "; ".join(f for f in legal_flags if "ceqa" in f.lower()),
            "context": f"Housing denied/continued with CEQA flags present",
        })

    for idx, pos in enumerate(positions):
        evidence = (pos.get("evidence") or "").lower()
        if "ceqa" in evidence and has_housing:
            action = pos.get("action") or pos.get("stance", "")
            if action in ("spoke against", "voted no"):
                ambiguities.append({
                    "type": "ceqa_weaponization",
                    "source": "position",
                    "source_idx": idx,
                    "member": pos.get("member", "?"),
                    "text": pos.get("evidence", ""),
                    "context": f"Action: {action} on: {pos.get('on', '?')}",
                })

    return ambiguities


# ---------------------------------------------------------------------------
# LLM-as-judge for ambiguities
# ---------------------------------------------------------------------------

AMBIGUITY_PROMPT = """You are scoring municipal meeting actions using the ACTIONS OVER WORDS rubric.

Score each ambiguous item below. For each, determine the correct score and cite the rubric line.

### Scoring Rules
- Community character/neighborhood compatibility cited AGAINST density or housing: -1
- Community character in general plan discussion (not opposing specific housing): 0
- Inclusionary weaponization (demanding a specific project exceed existing requirements to deny it): -2
- Raising citywide inclusionary rates as policy: +1
- CEQA used as pretext to block/delay housing: -2
- Legitimate CEQA environmental concern (not targeting housing specifically): 0
- Tenant protections: always +1, never penalize

Return ONLY valid JSON — an array of objects:
[
  {"idx": 0, "score": -1, "rubric_cite": "community character against density → -1", "reasoning": "one sentence"}
]

### Items to score:
"""


def resolve_ambiguities(record, ambiguities):
    """Batch-resolve ambiguous items via single LLM call.

    Returns list of ActionScore dicts for each resolved ambiguity.
    """
    if not ambiguities:
        return []

    items_text = []
    for i, amb in enumerate(ambiguities):
        items_text.append(
            f"[{i}] Type: {amb['type']}\n"
            f"    Member: {amb.get('member', 'unknown')}\n"
            f"    Text: {amb['text']}\n"
            f"    Context: {amb['context']}"
        )

    meeting_ctx = (
        f"Meeting: {record.get('body', '?')} — {record.get('date', '?')} — "
        f"{record.get('agency', '?')}"
    )

    prompt = AMBIGUITY_PROMPT + meeting_ctx + "\n\n" + "\n\n".join(items_text)

    response = call_claude(prompt, max_tokens=1000)
    if not response:
        return []

    try:
        text = response.strip()
        if text.startswith("```"):
            text = text.split("\n", 1)[1].rsplit("```", 1)[0]
        judgments = json.loads(text)
    except (json.JSONDecodeError, IndexError):
        print(f"  Failed to parse ambiguity response")
        return []

    resolved = []
    for j in judgments:
        idx = j.get("idx", 0)
        if idx >= len(ambiguities):
            continue
        amb = ambiguities[idx]
        resolved.append({
            "member": amb.get("member"),
            "score": j.get("score", 0),
            "method": "llm_judge",
            "source_type": amb["source"],
            "source_idx": amb["source_idx"],
            "item_summary": f"{amb['type']}: {amb['text'][:60]}",
            "rubric_line": j.get("rubric_cite", "LLM judgment"),
            "evidence": j.get("reasoning", ""),
        })

    return resolved


# ---------------------------------------------------------------------------
# Score aggregation
# ---------------------------------------------------------------------------

def aggregate_scores(action_scores, flags):
    """Derive record-level advocacy_score and per-member position_scores."""

    member_totals = defaultdict(lambda: {"scores": [], "actions": []})
    has_housing_score = False
    has_negative = False
    has_positive = False

    for a in action_scores:
        member = a.get("member")
        score = a.get("score", 0)

        if a["source_type"] in ("housing_item", "vote", "position"):
            has_housing_score = True
            if score > 0:
                has_positive = True
            if score < 0:
                has_negative = True

        if member:
            member_totals[member]["scores"].append(score)
            member_totals[member]["actions"].append(a)

    # Record-level advocacy score
    if flags:
        advocacy_score = "red"
    elif not has_housing_score:
        advocacy_score = "neutral"
    elif has_negative and has_positive:
        advocacy_score = "yellow"
    elif has_negative:
        advocacy_score = "red"
    elif has_positive:
        advocacy_score = "green"
    else:
        advocacy_score = "neutral"

    # Build reason
    if flags:
        advocacy_reason = flags[0]
    elif advocacy_score == "green":
        count = sum(1 for a in action_scores if a.get("score", 0) > 0)
        advocacy_reason = f"{count} pro-housing actions"
    elif advocacy_score == "red":
        count = sum(1 for a in action_scores if a.get("score", 0) < 0)
        advocacy_reason = f"{count} anti-housing actions"
    elif advocacy_score == "yellow":
        advocacy_reason = "mix of pro- and anti-housing actions"
    else:
        advocacy_reason = "no housing-relevant actions"

    # Backward-compatible position_scores
    position_scores = []
    for member, data in sorted(member_totals.items()):
        net = sum(data["scores"])
        summaries = [a["item_summary"] for a in data["actions"][:3]]
        rubrics = [a["rubric_line"] for a in data["actions"][:3]]
        position_scores.append({
            "member": member,
            "action_summary": "; ".join(summaries),
            "score": net,
            "rubric_cite": "; ".join(rubrics),
        })

    return advocacy_score, advocacy_reason, position_scores


# ---------------------------------------------------------------------------
# Main scoring flow
# ---------------------------------------------------------------------------

def score_record(record):
    """Score a single meeting record using hybrid approach."""
    housing_items = record.get("housing_items", [])
    votes = record.get("votes", [])
    positions = record.get("council_positions", [])

    if not housing_items and not votes and not positions:
        return {
            "advocacy_score": "neutral",
            "advocacy_reason": "no substantive content",
            "action_scores": [],
            "position_scores": [],
            "ambiguities_resolved": 0,
            "flags": [],
        }

    action_scores, flags = score_deterministic(record)

    ambiguities = detect_ambiguities(record)
    resolved = []
    if ambiguities:
        resolved = resolve_ambiguities(record, ambiguities)
        action_scores.extend(resolved)

    advocacy_score, advocacy_reason, position_scores = aggregate_scores(action_scores, flags)

    return {
        "advocacy_score": advocacy_score,
        "advocacy_reason": advocacy_reason,
        "action_scores": action_scores,
        "position_scores": position_scores,
        "ambiguities_resolved": len(resolved),
        "flags": flags,
    }


# ---------------------------------------------------------------------------
# CLI commands
# ---------------------------------------------------------------------------

def cmd_score(args):
    """Score meeting records."""
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    LOG_DIR.mkdir(parents=True, exist_ok=True)

    meeting_filter = set(str(m) for m in args.meeting) if args.meeting else None
    records = load_records(meeting_filter)

    if not records:
        print("No records to score.")
        return

    existing = {} if args.force else load_existing_scores()
    to_score = [r for r in records if str(r.get("meeting_id", "")) not in existing]

    if not to_score:
        print(f"All {len(records)} records already scored. Use --force to re-score.")
        return

    print(f"Scoring {len(to_score)} records ({len(records)} total, {len(existing)} already scored)")

    scored = dict(existing)
    success = 0
    failed = 0
    total_ambiguities = 0

    for i, record in enumerate(to_score):
        mid = str(record.get("meeting_id", "?"))
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
                **result,
                "votes": record.get("votes", []),
                "housing_items": record.get("housing_items", []),
            }
            scored[mid] = scored_record
            success += 1
            total_ambiguities += result.get("ambiguities_resolved", 0)

            log_path = LOG_DIR / f"{mid}.json"
            log_path.write_text(json.dumps(scored_record, indent=2, default=str))

            amb_tag = f" +{result['ambiguities_resolved']}llm" if result["ambiguities_resolved"] else ""
            flag_tag = f" ⚠{len(result['flags'])}" if result.get("flags") else ""
            print(f" {result['advocacy_score']}{amb_tag}{flag_tag}")
        else:
            failed += 1
            print(" FAILED")

        if args.dry_run:
            continue

    if not args.dry_run:
        with open(SCORED_JSONL, "w") as f:
            for r in sorted(scored.values(), key=lambda x: x.get("date", "")):
                f.write(json.dumps(r, default=str) + "\n")

    print(f"\nDone. {success} scored, {failed} failed, {total_ambiguities} LLM-resolved.")
    if not args.dry_run:
        print(f"Total: {len(scored)} → {SCORED_JSONL}")


def cmd_stats(args):
    """Show scoring statistics."""
    if not SCORED_JSONL.exists():
        print("No scored records. Run score_records.py first.")
        return

    scores = defaultdict(int)
    total = 0
    member_scores = defaultdict(list)
    methods = defaultdict(int)

    for line in SCORED_JSONL.read_text().splitlines():
        if not line.strip():
            continue
        try:
            r = json.loads(line)
            total += 1
            scores[r.get("advocacy_score", "neutral")] += 1
            for a in r.get("action_scores", []):
                methods[a.get("method", "unknown")] += 1
            for ps in r.get("position_scores", []):
                member_scores[ps["member"]].append(ps.get("score", 0))
        except Exception:
            continue

    print(f"Scored records: {total}")
    print(f"\nAdvocacy scores:")
    for score in ["green", "yellow", "red", "neutral"]:
        bar = "█" * (scores[score] // 2) if scores[score] > 0 else ""
        print(f"  {score:8s}: {scores[score]:4d} {bar}")

    if methods:
        print(f"\nScoring methods:")
        for method, count in sorted(methods.items()):
            print(f"  {method:15s}: {count}")

    if member_scores:
        print(f"\nMember net scores (top 10):")
        ranked = sorted(member_scores.items(), key=lambda x: sum(x[1]), reverse=True)
        for member, action_scores_list in ranked[:10]:
            net = sum(action_scores_list)
            count = len(action_scores_list)
            grade = "A" if net >= 10 else "B" if net >= 5 else "C" if net >= 0 else "D" if net >= -9 else "F"
            print(f"  {member:25s}: {grade} net {net:+4d} ({count} actions)")


def main():
    parser = argparse.ArgumentParser(description="Score meeting records with hybrid deterministic + LLM analysis")
    parser.add_argument("--force", action="store_true", help="Re-score all records")
    parser.add_argument("--meeting", nargs="+", help="Score specific meeting(s) by ID")
    parser.add_argument("--stats", action="store_true", help="Show scoring statistics")
    parser.add_argument("--mode", choices=["local", "api"], default="local", help="LLM mode")
    parser.add_argument("--dry-run", action="store_true", help="Show scores without writing output")

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
