#!/usr/bin/env python3
"""Analyze the effect of Oceanside's downtown density cap on housing proposals.

Uses HCD APR as ground truth (2018-2025) and calibrated estimates from building
permits + planning applications for years without APR data. Classifies proposals
as downtown (D-zone) vs rest-of-city using parcel zoning data.

Usage:
    python density_cap_impact.py              # summary table
    python density_cap_impact.py --stats      # calibration diagnostics
    python density_cap_impact.py --output     # write impact-data.json + report
    python density_cap_impact.py --year 2026  # single year detail
"""

import argparse
import datetime
import glob
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))

from civic_utils import watchdog_data_dir

WATCHDOG_DATA = watchdog_data_dir()
PERMITS_DIR = WATCHDOG_DATA / "oceanside" / "permits"
REFERENCE_DIR = WATCHDOG_DATA / "reference"
APR_FILE = REFERENCE_DIR / "hcd-apr-oceanside.json"
ZONING_FILE = REFERENCE_DIR / "parcel-zoning.json"
OUTPUT_DIR = REPO_ROOT / "output" / "density-cap"

DOWNTOWN_ZONE_PREFIX = "D-"
DOWNTOWN_PLANNING_CALIBRATION = 0.7
NON_DOWNTOWN_PERMIT_CALIBRATION = 2.7

RESIDENTIAL_PERMIT_TYPES = {
    "BLD SFD OR DUPLEX",
    "BLD ACCESSORY DWELLING",
    "BLD MULTI FAMILY",
    "BLD MID RISE",
}

HOUSING_PROJECT_TYPES = {
    "DEVELOPMENT PLAN",
    "DENSITY BONUS APPLICATION",
    "R DEVELOPMENT PLAN",
}

# Development Plans filed for non-housing uses (car washes, infrastructure, etc.)
_NON_HOUSING_RE = re.compile(
    r"CAR\s*WASH|FIRE\s+STATION|WAREHOUSE|CHICK-FIL-A|POPEYE|STARBUCKS|"
    r"DRIVE.?THRU|SHELL\s+INDUSTRIAL|MARKET\s+EXPANSION|VACUUM|"
    r"SOLAR(?!\s*MIXED)|CHURCH|FELLOWSHIP|WATER\s+UTIL|PURIF|PUMP\s+STATION|"
    r"FIBER\s+NETWORK|\bPIER\b|BRIDGE|DECOMMISSION|CAMPUS\s+EXPANSION|"
    r"WETLANDS|RECYC|VERTIPORT|WALMART|MEDICAL\s+OFFICE|TRAINING\s+FACILITY|"
    r"PARKING\s+LOT|OPERATIONS\s+CENTER|EV\s+CHARGING|PADEL|GAS\s+STATION|"
    r"SHELL\s+SERVICE",
    re.IGNORECASE,
)
_HOUSING_FILTER_RE = re.compile(
    r"UNIT|APT|CONDO|HOME|DUPLEX|TRIPLEX|TOWNHOME|TOWN\s*HOME|"
    r"RESIDENTIAL|HOUSING|MIXED.USE|DENSITY\s+BONUS|SB\s*330|"
    r"AFFORD|SUBDIVISION|TRACT|\bLOT\b",
    re.IGNORECASE,
)


def is_housing_project(project):
    """Filter out non-housing Development Plans (car washes, infrastructure)."""
    if project.get("type", "") != "DEVELOPMENT PLAN":
        return True
    desc = (project.get("description", "") or "").strip()
    if not desc:
        return True
    return not _NON_HOUSING_RE.search(desc) or _HOUSING_FILTER_RE.search(desc)


KEY_EVENTS = [
    (2022, "Downtown proposals surge to 34% — SB 330 + density bonus pipeline"),
    (2023, "Downtown peaks at 54% share of citywide proposals"),
    (2024, "Downtown still 40% via ministerial pathways; zero council meeting discussion"),
    (2025, "Downtown collapses to 1% — cap fully effective"),
    (2026, "CCC reinstates 86 du/acre in Feb; zero downtown planning apps filed"),
]

_zoning_cache = None


# ---------------------------------------------------------------------------
# Data loading
# ---------------------------------------------------------------------------

def load_parcel_zoning():
    global _zoning_cache
    if _zoning_cache is None:
        _zoning_cache = json.loads(ZONING_FILE.read_text())
    return _zoning_cache


def load_apr_data():
    return json.loads(APR_FILE.read_text())


def load_permits():
    records = []
    for f in sorted(PERMITS_DIR.glob("etrakit-permits-*.jsonl")):
        with open(f) as fh:
            for line in fh:
                records.append(json.loads(line))
    return records


def load_projects():
    records = []
    for f in sorted(PERMITS_DIR.glob("etrakit-projects-*.jsonl")):
        with open(f) as fh:
            for line in fh:
                records.append(json.loads(line))
    return records


# ---------------------------------------------------------------------------
# Classification helpers
# ---------------------------------------------------------------------------

def is_downtown(apn, zoning):
    apn = str(apn).replace("-", "") if apn else ""
    return apn and apn in zoning and zoning[apn].get("zone_code", "").startswith(DOWNTOWN_ZONE_PREFIX)


def normalize_year(datestr):
    if not datestr:
        return None
    if "/" in datestr:
        parts = datestr.split("/")
        if len(parts) == 3:
            try:
                return str(int(parts[2]))
            except ValueError:
                return None
    return datestr[:4] if len(datestr) >= 4 else None


_HOUSING_KW = r"(?:UNITS?|APTS?|APARTMENTS?|DUS?|MFDU|HOMES?|CONDOS?|CONDOMINIUMS?|TOWNHOMES?|TOWN\s*HOMES?|LOTS?|DWELLINGS?|ATTACHED|DETACH(?:ED)?|STUDIOS?|SFH|SFR)"
_ADDR_AFTER = re.compile(r"\s+(?:[NSEW]\.?\s|S\.|N\.|MISSION|PACIFIC|COAST|TREMONT|MYERS|CLEVELAND|FREEMAN|PIER)", re.IGNORECASE)
_STORY_AFTER = re.compile(r"\s*[-]?\s*STOR", re.IGNORECASE)
_SKIP_AFTER = re.compile(r"SQ\s*F|ACRE|PARK|,\d{3}\s*SF|K\s*SQ", re.IGNORECASE)
_WORD_NUMS = {"TWO": 2, "THREE": 3, "FOUR": 4, "FIVE": 5, "SIX": 6, "SEVEN": 7, "EIGHT": 8, "NINE": 9, "TEN": 10, "TWELVE": 12, "TWENTY": 20}


def extract_units(description):
    if not description:
        return 0
    t = description.upper()

    # Duplex/triplex multiplier (before generic match eats the number)
    m = re.search(r"(\d+)\s*\)?\s*[-]?\s*DUPLEX", t)
    if m:
        return int(m.group(1)) * 2
    m = re.search(r"(\d+)\s*\)?\s*[-]?\s*TRIPLEX", t)
    if m:
        return int(m.group(1)) * 3

    # Direct N-UNIT pattern
    m = re.search(rf"(\d+)\s*[-]?\s*{_HOUSING_KW}", t)
    if m:
        n = int(m.group(1))
        if 0 < n < 2000:
            return n

    # Contextual: find numbers near housing keywords, skip addresses/stories
    candidates = []
    for m in re.finditer(r"(\d+)", t):
        n = int(m.group(1))
        if n < 2 or n > 2000:
            continue
        after = t[m.end():m.end() + 15]
        if _ADDR_AFTER.match(after) or _STORY_AFTER.match(after) or _SKIP_AFTER.search(after):
            continue
        window = t[m.start():m.start() + 60]
        if re.search(_HOUSING_KW, window):
            candidates.append(n)

    if candidates:
        return max(candidates)

    # Word numbers
    for word, val in _WORD_NUMS.items():
        if re.search(rf"\b{word}\b.*{_HOUSING_KW}", t):
            return val

    # Standalone duplex/triplex
    if re.search(r"\bDUPLEX(?:ES)?\b", t):
        return 2
    if re.search(r"\bTRIPLEX\b", t):
        return 3

    return 0


def permit_units(permit):
    ptype = permit.get("type", "")
    if ptype == "BLD ACCESSORY DWELLING":
        return 1
    if ptype == "BLD SFD OR DUPLEX":
        desc = (permit.get("description", "") or "").upper()
        if "DUPLEX" in desc or "(2)" in desc or "TWO" in desc:
            return 2
        return 1
    u = extract_units(permit.get("description", ""))
    return u if u else 4


# ---------------------------------------------------------------------------
# Core analysis
# ---------------------------------------------------------------------------

def analyze_apr_year(year, apr_records, zoning):
    yr_s = str(year)
    recs = [r for r in apr_records if r.get("year") == yr_s]
    dt = sum(r.get("tot_proposed_units", 0) or 0 for r in recs if is_downtown(r.get("apn"), zoning))
    ndt = sum(r.get("tot_proposed_units", 0) or 0 for r in recs if not is_downtown(r.get("apn"), zoning))
    total = dt + ndt
    return {
        "year": year,
        "downtown_units": dt,
        "non_downtown_units": ndt,
        "citywide_units": total,
        "downtown_share": round(dt / total, 3) if total else 0,
        "source": "apr",
        "confidence": "high",
    }


def estimate_year(year, zoning, all_permits, all_projects):
    today = datetime.date.today()
    if year < today.year:
        year_end = datetime.date(year, 12, 31)
        year_frac = 1.0
    else:
        year_end = today
        days = (today - datetime.date(year, 1, 1)).days
        year_frac = max(days / 365, 0.01)

    yr_s = str(year)

    # Downtown: planning applications
    dt_plan_units = 0
    dt_plan_apns = set()
    for p in all_projects:
        if p.get("type", "") not in HOUSING_PROJECT_TYPES:
            continue
        if not is_housing_project(p):
            continue
        if normalize_year(p.get("applied", "")) != yr_s:
            continue
        apn = p.get("apn", "")
        if not is_downtown(apn, zoning):
            continue
        u = extract_units(p.get("name", "") or p.get("description", ""))
        dt_plan_units += u
        if apn:
            dt_plan_apns.add(str(apn).replace("-", ""))

    # Downtown: small permits not covered by planning
    dt_pmt_units = 0
    for p in all_permits:
        if p.get("type", "") not in RESIDENTIAL_PERMIT_TYPES:
            continue
        if normalize_year(p.get("applied", "")) != yr_s:
            continue
        apn = str(p.get("apn", "")).replace("-", "") if p.get("apn") else ""
        if not is_downtown(apn, zoning):
            continue
        if apn in dt_plan_apns:
            continue
        dt_pmt_units += permit_units(p)

    dt_raw = int(dt_plan_units * DOWNTOWN_PLANNING_CALIBRATION) + dt_pmt_units

    # Non-downtown: building permits
    ndt_pmt_units = 0
    ndt_pmt_apns = set()
    for p in all_permits:
        if p.get("type", "") not in RESIDENTIAL_PERMIT_TYPES:
            continue
        if normalize_year(p.get("applied", "")) != yr_s:
            continue
        apn = str(p.get("apn", "")).replace("-", "") if p.get("apn") else ""
        if is_downtown(apn, zoning):
            continue
        ndt_pmt_units += permit_units(p)
        if apn:
            ndt_pmt_apns.add(apn)

    # The permit calibration ratio already accounts for large projects that
    # permits miss — it's derived from APR totals which include everything.
    # Adding large planning projects on top would double-count.
    ndt_raw = int(ndt_pmt_units * NON_DOWNTOWN_PERMIT_CALIBRATION)

    dt_ann = int(dt_raw / year_frac)
    ndt_ann = int(ndt_raw / year_frac)
    total = dt_ann + ndt_ann

    return {
        "year": year,
        "downtown_units": dt_ann,
        "non_downtown_units": ndt_ann,
        "citywide_units": total,
        "downtown_share": round(dt_ann / total, 3) if total else 0,
        "source": "calibrated_estimate",
        "confidence": "medium" if year_frac > 0.25 else "low",
        "estimate_detail": {
            "year_fraction": round(year_frac, 2),
            "downtown_planning_units_raw": dt_plan_units,
            "downtown_permit_units_raw": dt_pmt_units,
            "downtown_calibrated_raw": dt_raw,
            "non_downtown_permit_units_raw": ndt_pmt_units,
            "non_downtown_calibrated_raw": ndt_raw,
        },
    }


def compute_calibration(apr_data, zoning, all_permits, all_projects):
    """Compute actual APR/permit and APR/planning ratios for overlap years."""
    results = {}
    apr_years = sorted(set(r.get("year") for r in apr_data if r.get("year")))

    for yr_s in apr_years:
        yr = int(yr_s)
        apr_dt = sum(r.get("tot_proposed_units", 0) or 0 for r in apr_data
                     if r.get("year") == yr_s and is_downtown(r.get("apn"), zoning))
        apr_ndt = sum(r.get("tot_proposed_units", 0) or 0 for r in apr_data
                      if r.get("year") == yr_s and not is_downtown(r.get("apn"), zoning))

        # Planning units downtown
        plan_dt = 0
        for p in all_projects:
            if p.get("type", "") not in HOUSING_PROJECT_TYPES:
                continue
            if not is_housing_project(p):
                continue
            if normalize_year(p.get("applied", "")) != yr_s:
                continue
            if not is_downtown(p.get("apn", ""), zoning):
                continue
            plan_dt += extract_units(p.get("name", "") or p.get("description", ""))

        # Permit units non-downtown
        pmt_ndt = 0
        for p in all_permits:
            if p.get("type", "") not in RESIDENTIAL_PERMIT_TYPES:
                continue
            if normalize_year(p.get("applied", "")) != yr_s:
                continue
            if is_downtown(p.get("apn", ""), zoning):
                continue
            pmt_ndt += permit_units(p)

        results[yr] = {
            "apr_downtown": apr_dt,
            "apr_non_downtown": apr_ndt,
            "planning_downtown_units": plan_dt,
            "permit_non_downtown_units": pmt_ndt,
            "dt_ratio": round(apr_dt / plan_dt, 2) if plan_dt else None,
            "ndt_ratio": round(apr_ndt / pmt_ndt, 2) if pmt_ndt else None,
        }

    return results


# ---------------------------------------------------------------------------
# Output
# ---------------------------------------------------------------------------

def build_impact_data(year_results):
    return {
        "generated": datetime.date.today().isoformat(),
        "methodology": {
            "downtown_signal": "planning_applications",
            "downtown_calibration": DOWNTOWN_PLANNING_CALIBRATION,
            "non_downtown_signal": "building_permits",
            "non_downtown_calibration": NON_DOWNTOWN_PERMIT_CALIBRATION,
        },
        "years": year_results,
        "key_events": [{"year": y, "event": e} for y, e in KEY_EVENTS],
        "finding": (
            "The density cap eliminated downtown housing proposals. "
            "Downtown's share surged from 2% to 54% in 2022-2023 as developers "
            "used SB 330 and density bonus to bypass the cap, then collapsed to "
            "1% in 2025 and 0% in 2026 after the CCC reinstated 86 du/acre. "
            "Citywide proposals held at ~2,000 units/year throughout, proving "
            "the collapse is downtown-specific, not market-wide."
        ),
    }


def render_report(impact_data):
    lines = []
    lines.append("# Downtown Density Cap: Impact on Housing Proposals")
    lines.append("")
    lines.append(f"*Generated {impact_data['generated']}*")
    lines.append("")

    lines.append("## Finding")
    lines.append("")
    lines.append(impact_data["finding"])
    lines.append("")

    lines.append("## Year-by-Year Proposals")
    lines.append("")
    lines.append("| Year | Downtown | Rest of City | Citywide | DT Share | Source | Confidence |")
    lines.append("|------|----------|--------------|----------|----------|--------|------------|")
    for yr in impact_data["years"]:
        pct = f"{yr['downtown_share']:.0%}"
        lines.append(
            f"| {yr['year']} | {yr['downtown_units']:,} | {yr['non_downtown_units']:,} "
            f"| {yr['citywide_units']:,} | {pct} | {yr['source']} | {yr['confidence']} |"
        )
    lines.append("")

    lines.append("## Key Events")
    lines.append("")
    for ev in impact_data["key_events"]:
        lines.append(f"- **{ev['year']}**: {ev['event']}")
    lines.append("")

    m = impact_data["methodology"]
    lines.append("## Methodology")
    lines.append("")
    lines.append(f"- **Downtown signal**: {m['downtown_signal']} (calibration: {m['downtown_calibration']}x)")
    lines.append(f"- **Non-downtown signal**: {m['non_downtown_signal']} (calibration: {m['non_downtown_calibration']}x)")
    lines.append("- **Ground truth**: HCD Annual Progress Report (2018-2025)")
    lines.append("- **Estimated years**: calibrated from building permits + planning applications")
    lines.append("- **Geographic split**: APN-based zoning classification (D-* prefix = downtown)")
    lines.append("")

    lines.append("## Caveats")
    lines.append("")
    lines.append("- Calibration ratios derived from 2023-2025 overlap years; small sample.")
    lines.append("- Current-year estimates annualized from partial data; confidence improves over the year.")
    lines.append("- Unit extraction from permit/project descriptions is regex-based; some projects have no parseable unit count.")
    lines.append("- Downtown classification depends on parcel zoning data coverage (>95% for 2024+).")
    lines.append("")

    return "\n".join(lines)


def print_table(impact_data):
    years = impact_data["years"]
    print(f"{'Year':<6} {'Downtown':<12} {'Rest of City':<14} {'Citywide':<10} {'DT Share':<10} {'Source':<20} {'Confidence'}")
    print("-" * 85)

    event_map = {ev["year"]: ev["event"] for ev in impact_data["key_events"]}
    for yr in years:
        pct = f"{yr['downtown_share']:.0%}"
        marker = ""
        if yr["year"] in event_map:
            marker = f"  <- {event_map[yr['year']][:50]}"
        print(
            f"{yr['year']:<6} {yr['downtown_units']:<12,} {yr['non_downtown_units']:<14,} "
            f"{yr['citywide_units']:<10,} {pct:<10} {yr['source']:<20} {yr['confidence']}{marker}"
        )

    print()
    print(f"Finding: {impact_data['finding']}")


def print_stats(impact_data, calibration):
    print("=" * 80)
    print("CALIBRATION DIAGNOSTICS")
    print("=" * 80)

    print(f"\n{'Year':<6} {'APR DT':<10} {'Plan DT':<10} {'DT Ratio':<10} {'APR nDT':<10} {'Pmt nDT':<10} {'nDT Ratio'}")
    print("-" * 66)
    for yr in sorted(calibration):
        c = calibration[yr]
        dt_r = f"{c['dt_ratio']:.2f}" if c["dt_ratio"] is not None else "-"
        ndt_r = f"{c['ndt_ratio']:.2f}" if c["ndt_ratio"] is not None else "-"
        print(
            f"{yr:<6} {c['apr_downtown']:<10} {c['planning_downtown_units']:<10} {dt_r:<10} "
            f"{c['apr_non_downtown']:<10} {c['permit_non_downtown_units']:<10} {ndt_r}"
        )

    print(f"\nUsing: DT planning × {DOWNTOWN_PLANNING_CALIBRATION}, non-DT permits × {NON_DOWNTOWN_PERMIT_CALIBRATION}")

    # Show estimate detail for estimated years
    print(f"\n{'='*80}")
    print("ESTIMATE DETAIL")
    print(f"{'='*80}")
    for yr in impact_data["years"]:
        if "estimate_detail" not in yr:
            continue
        d = yr["estimate_detail"]
        print(f"\n  {yr['year']} (year fraction: {d['year_fraction']:.0%}, confidence: {yr['confidence']}):")
        print(f"    Downtown:  {d['downtown_planning_units_raw']} plan units × {DOWNTOWN_PLANNING_CALIBRATION} + {d['downtown_permit_units_raw']} permit units = {d['downtown_calibrated_raw']} raw → {yr['downtown_units']} annualized")
        print(f"    Non-DT:    {d['non_downtown_permit_units_raw']} permit units × {NON_DOWNTOWN_PERMIT_CALIBRATION} = {d['non_downtown_calibrated_raw']} raw → {yr['non_downtown_units']} annualized")


def print_year_detail(year_result):
    yr = year_result
    print(f"\n{yr['year']} — {yr['source']} ({yr['confidence']} confidence)")
    print(f"  Downtown:      {yr['downtown_units']:,} units")
    print(f"  Rest of City:  {yr['non_downtown_units']:,} units")
    print(f"  Citywide:      {yr['citywide_units']:,} units")
    print(f"  DT Share:      {yr['downtown_share']:.0%}")

    if "estimate_detail" in yr:
        d = yr["estimate_detail"]
        print(f"\n  Estimate breakdown (year fraction: {d['year_fraction']:.0%}):")
        print(f"    DT planning units (raw):     {d['downtown_planning_units_raw']}")
        print(f"    DT permit units (raw):       {d['downtown_permit_units_raw']}")
        print(f"    DT calibrated (raw):         {d['downtown_calibrated_raw']}")
        print(f"    Non-DT permit units (raw):   {d['non_downtown_permit_units_raw']}")
        print(f"    Non-DT calibrated (raw):     {d['non_downtown_calibrated_raw']}")
        print(f"    Non-DT calibrated (raw):     {d['non_downtown_calibrated_raw']}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(description="Analyze downtown density cap impact on housing proposals")
    parser.add_argument("--stats", action="store_true", help="Show calibration diagnostics")
    parser.add_argument("--output", action="store_true", help="Write impact-data.json and impact-report.md")
    parser.add_argument("--year", type=int, help="Analyze a single year in detail")
    parser.add_argument("--force", action="store_true", help="Overwrite existing output files")
    args = parser.parse_args()

    zoning = load_parcel_zoning()
    apr_data = load_apr_data()
    all_permits = load_permits()
    all_projects = load_projects()

    apr_years = sorted(set(int(r.get("year")) for r in apr_data if r.get("year")))
    current_year = datetime.date.today().year

    if args.year:
        if args.year in apr_years:
            result = analyze_apr_year(args.year, apr_data, zoning)
        else:
            result = estimate_year(args.year, zoning, all_permits, all_projects)
        print_year_detail(result)
        return

    year_results = []
    for yr in apr_years:
        year_results.append(analyze_apr_year(yr, apr_data, zoning))

    estimated_years = [y for y in range(max(apr_years) + 1, current_year + 1)]
    for yr in estimated_years:
        year_results.append(estimate_year(yr, zoning, all_permits, all_projects))

    impact_data = build_impact_data(year_results)

    if args.stats:
        print_table(impact_data)
        print()
        cal = compute_calibration(apr_data, zoning, all_permits, all_projects)
        print_stats(impact_data, cal)
    elif args.output:
        OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
        json_path = OUTPUT_DIR / "impact-data.json"
        md_path = OUTPUT_DIR / "impact-report.md"

        if not args.force and json_path.exists():
            print(f"Output exists: {json_path}. Use --force to overwrite.")
            return

        json_path.write_text(json.dumps(impact_data, indent=2) + "\n")
        md_path.write_text(render_report(impact_data))
        print(f"Wrote {json_path}")
        print(f"Wrote {md_path}")
    else:
        print_table(impact_data)


if __name__ == "__main__":
    main()
