#!/usr/bin/env python3
"""Count housing UNITS newly entering the pipeline, by year of application.

"Filings" means units, never permit or project row counts. One permit can be a
199-unit project or a single ADU, so row counts measure paperwork volume rather
than housing. Filings are the least reliable rung of the pipeline but the most
immediate, so they register a council decision before approvals or completions
can.

Counts key on the etrakit `applied` date. Filed, not approved, not issued.

Why this exists instead of transforms/permit_projects.py:estimate_units() in
watchdog: that function reads building numbers as unit counts ("BLDG 30 UNITS
75-78" returns 30 for a 4-plex), counts hotel rooms as dwelling units, repeats
a project total once per permit, and defaults to 1 on phrasings like "20 NEW
RESIDENTIAL UNITS". Over 2020-2026 it is wrong in both directions by hundreds
of units. Fixing it is a separate change with dashboard consequences, since
monthly_rollup.py, housing_projects.py, and export_parquet.py all call it.

Method:
  - ADU permits count 1 unit each, SFD permits 1, duplexes 2.
  - Multifamily and mid-rise rows are resolved individually. Plex and explicit
    unit-range patterns are matched mechanically; every row those patterns
    cannot resolve carries a hand-verified value in HAND_UNITS or sits in
    NO_UNIT_COUNT below. There are under 200 such rows across 2020-2026, so
    reading them is tractable and the alternative is fabrication.
  - Rows whose unit count is absent from the (source-truncated) description are
    reported in a separate column and contribute zero. They are not guessed.
  - Hotels, assisted living, solar, repairs, VOID/WITHDRAWN rows, and duplicate
    permits for an already-counted project are excluded, each with a reason.

Usage:
    python filed_units.py                      # table, 2020 to present
    python filed_units.py --since 2023         # limit start year
    python filed_units.py --year 2026          # single year, itemized
    python filed_units.py --json               # machine-readable
    python filed_units.py --show-unresolved    # list rows with no unit count
"""

import argparse
import json
import re
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
sys.path.insert(0, str(REPO_ROOT))

from civic_utils import watchdog_data_dir
import config

ADU_TYPE = "BLD ACCESSORY DWELLING"
SFD_TYPE = "BLD SFD OR DUPLEX"
MF_TYPES = ("BLD MULTI FAMILY", "BLD MID RISE")
HOUSING_TYPES = (ADU_TYPE, SFD_TYPE) + MF_TYPES

# Hand-verified unit counts for multifamily and mid-rise rows that the plex and
# unit-range patterns cannot resolve. A value of 0 means deliberately excluded,
# with the reason in the comment. Verified 2026-11-09 against full descriptions.
HAND_UNITS = {
    # not net new dwelling units
    "BLDG20-4460": 0,   # Marriott Fairfield, 99 hotel rooms
    "BLDG21-4685": 0,   # Hilton Home2 Suites, 136 hotel rooms
    "BLDG23-2058": 0,   # 86-room hotel, renewal of BLDG19-4559
    "BLDG20-2997": 0,   # Sandpiper Villa, 94-bed assisted living (group quarters)
    "BLDG22-2233": 0,   # underground gas leak repair, re-pipe
    "BLDG23-1238": 0,   # 8.8kW rooftop solar on existing multifamily
    "BLDG21-1629": 0,   # VOID
    "BLDG23-0985": 0,   # WITHDRAWN, blank description
    "BLDG23-1410": 0,   # no applied date, blank description
    "BLDG24-0343": 0,   # description reads "Do not use"
    # duplicate permit for a project already counted
    "BLDG26-0522": 0,   # Olive Park 199 units, counted on BLDG26-0473
    # unit counts stated in prose
    "BLDG20-1615": 8, "BLDG20-2884": 34, "BLDG20-4597": 3,
    "BLDG21-0490": 4, "BLDG21-0651": 5, "BLDG21-0728": 16, "BLDG21-1109": 14,
    "BLDG21-2021": 5, "BLDG21-2127": 3, "BLDG21-2832": 4, "BLDG21-3767": 3,
    "BLDG21-3807": 20,  # EXPIRED, but it was filed, so it counts
    "BLDG21-3814": 3, "BLDG21-4048": 5, "BLDG21-4415": 6, "BLDG21-4648": 4,
    "BLDG22-0443": 3,   # "3-PLX BLD 25 UNTS 115-117", typos defeat both patterns
    "BLDG22-1139": 6, "BLDG22-1158": 60, "BLDG22-1162": 18,
    "BLDG21-0860": 309,  # 5-story mixed use, applied 2022-09-02 under a 2021 number
    "BLDG22-2359": 179, "BLDG22-2423": 54, "BLDG22-2555": 10,
    "BLDG23-0586": 3, "BLDG23-0834": 3, "BLDG23-1414": 21, "BLDG23-1983": 43,
    "BLDG24-0006": 3, "BLDG24-0007": 3, "BLDG24-0435": 3, "BLDG24-2302": 111,
    "BLDG24-2499": 8, "BLDG24-2500": 8, "BLDG24-2501": 8, "BLDG24-2502": 8,
    "BLDG24-2503": 8, "BLDG24-2504": 7,
    "BLDG25-1204": 56,
    "BLDG26-0182": 4, "BLDG26-0473": 199, "BLDG26-0605": 3,
    "BLDG26-1587": 3, "BLDG26-1603": 3, "BLDG26-1677": 20,
}

# Residential rows whose unit count is absent from the source description.
# Etrakit truncates the description field and the counts fell off the end.
# These are real projects of unknown size. They contribute zero rather than a
# guess, so years containing them are undercounted by an unknown margin.
NO_UNIT_COUNT = {
    "BLDG20-1005",  # garage and storage conversion, count not stated
    "BLDG21-0049", "BLDG21-2267", "BLDG21-2828", "BLDG21-2829",
    "BLDG21-3498", "BLDG21-3499",  # 4-story mixed use, buildings 2 and 3
    # 2022 mixed use and residential complex, recorded only as square footage
    "BLDG22-2573", "BLDG22-2574", "BLDG22-2575", "BLDG22-2576", "BLDG22-2577",
    "BLDG23-0899",
    # Modera Melrose, 6 buildings; only building 4 kept its unit count
    "BLDG23-1411", "BLDG23-1412", "BLDG23-1413", "BLDG23-1415", "BLDG23-1416",
    "BLDG25-1032",
}

# Possible duplicate plan-check filings: bare plan types, status RECEIVED, filed
# the same day as ISSUED siblings that may already cover these buildings. If
# they are duplicates, 2022 is overstated by 19 units. Unresolved as of
# 2026-11-09; settling it needs the project grouping, not the description.
SUSPECTED_DUPLICATES_2022 = ("BLDG22-2699", "BLDG22-2708", "BLDG22-2710", "BLDG22-2712")


def plex_units(desc):
    """Units from a plex or explicit unit-range pattern, or None.

    The unit-range check runs first and the plex check second, both ahead of any
    bare "N UNITS" reading, because building and phase numbers in these
    descriptions sit immediately before the word UNITS and would otherwise win.
    """
    text = (desc or "").upper()
    rng = re.search(r"UNITS?\s*#?\s*(\d+)\s*[-–]\s*(\d+)", text)
    if rng:
        low, high = int(rng.group(1)), int(rng.group(2))
        if 0 < high - low < 100:
            return high - low + 1
    plex = re.search(r"(\d+)\s*[-\s]?PLEX", text)
    if plex:
        return int(plex.group(1))
    if "TRIPLEX" in text:
        return 3
    return None


def load_permits(city):
    """Load every etrakit building permit for a city, deduplicated by permit_no."""
    permits_dir = watchdog_data_dir() / city.lower() / "permits"
    if not permits_dir.is_dir():
        raise SystemExit(f"No permits directory for '{city}': {permits_dir}")
    rows = {}
    for path in sorted(permits_dir.glob("etrakit-permits-*.jsonl")):
        with open(path) as fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if rec.get("permit_no"):
                    rows[rec["permit_no"]] = rec
    if not rows:
        raise SystemExit(f"No permit rows found in {permits_dir}")
    return rows


def applied_parts(rec):
    """(year, month, day) from the M/D/YYYY applied field, or (None, None, None)."""
    try:
        month, day, year = [int(p) for p in (rec.get("applied") or "").split("/")]
        return year, month, day
    except (ValueError, AttributeError):
        return None, None, None


def tally(permits, since=2020):
    """Units filed per year, plus the rows that carry no count and the exclusions."""
    years = defaultdict(lambda: {
        "adu": 0, "sfd_duplex": 0, "mf_midrise": 0, "total": 0,
        "rows_without_count": [], "excluded": [], "items": [],
    })
    unresolved = []
    for rec in permits.values():
        ptype = (rec.get("type") or "").strip()
        if ptype not in HOUSING_TYPES:
            continue
        year, _month, _day = applied_parts(rec)
        if year is None or year < since:
            continue
        no = rec.get("permit_no")
        desc = rec.get("description") or ""
        bucket = years[year]

        if ptype == ADU_TYPE:
            units, key = 1, "adu"
        elif ptype == SFD_TYPE:
            units = 2 if "DUPLEX" in desc.upper() else 1
            key = "sfd_duplex"
        else:
            key = "mf_midrise"
            if no in NO_UNIT_COUNT:
                bucket["rows_without_count"].append(no)
                continue
            if no in HAND_UNITS:
                units = HAND_UNITS[no]
                if units == 0:
                    bucket["excluded"].append(no)
                    continue
            else:
                units = plex_units(desc)
                if units is None:
                    unresolved.append((year, no, desc[:70]))
                    bucket["rows_without_count"].append(no)
                    continue

        bucket[key] += units
        bucket["total"] += units
        if key == "mf_midrise":
            y, m, d = applied_parts(rec)
            bucket["items"].append((f"{y:04d}-{m:02d}-{d:02d}", no, units, desc[:60]))
    return years, unresolved


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--city", default=config.get("identity/primary_city_slug", "oceanside"),
                    help="jurisdiction slug under the watchdog data directory")
    ap.add_argument("--since", type=int, default=2020, help="first year to report")
    ap.add_argument("--year", type=int, help="report one year, itemizing multifamily rows")
    ap.add_argument("--json", action="store_true", help="emit JSON instead of a table")
    ap.add_argument("--show-unresolved", action="store_true",
                    help="list residential rows whose unit count is absent from the data")
    args = ap.parse_args()

    permits = load_permits(args.city)
    years, unresolved = tally(permits, since=args.since)
    if args.year:
        years = {args.year: years.get(args.year, {
            "adu": 0, "sfd_duplex": 0, "mf_midrise": 0, "total": 0,
            "rows_without_count": [], "excluded": [], "items": [],
        })}

    cutoff = max(
        (f"{y:04d}-{m:02d}-{d:02d}" for y, m, d in (applied_parts(r) for r in permits.values())
         if y is not None),
        default="unknown",
    )

    if args.json:
        out = {
            "city": args.city,
            "metric": "housing units filed, by year of application (etrakit applied date)",
            "data_cutoff": cutoff,
            "years": {
                str(y): {k: v for k, v in b.items() if k != "items"}
                for y, b in sorted(years.items())
            },
        }
        print(json.dumps(out, indent=2))
        return

    print(f"=== FILED HOUSING UNITS, {args.city}, by year applied ===")
    print(f"    data cutoff {cutoff}; the current year is partial\n")
    head = f"  {'year':<6}{'ADU':>6}{'SFD/dup':>9}{'MF/midrise':>12}{'TOTAL':>8}{'no count':>10}{'excluded':>10}"
    print(head)
    tot = defaultdict(int)
    for y, b in sorted(years.items()):
        print(f"  {y:<6}{b['adu']:>6}{b['sfd_duplex']:>9}{b['mf_midrise']:>12}"
              f"{b['total']:>8}{len(b['rows_without_count']):>10}{len(b['excluded']):>10}")
        for k in ("adu", "sfd_duplex", "mf_midrise", "total"):
            tot[k] += b[k]
        tot["nc"] += len(b["rows_without_count"])
        tot["ex"] += len(b["excluded"])
    if len(years) > 1:
        print(f"  {'all':<6}{tot['adu']:>6}{tot['sfd_duplex']:>9}{tot['mf_midrise']:>12}"
              f"{tot['total']:>8}{tot['nc']:>10}{tot['ex']:>10}")

    if args.year:
        b = next(iter(years.values()))
        if b["items"]:
            print("\n  multifamily and mid-rise rows counted:")
            for applied, no, units, desc in sorted(b["items"]):
                print(f"    {applied:>10} {no:<13} {units:>4}u  {desc}")
        if b["rows_without_count"]:
            print(f"\n  rows with no unit count in the data: {', '.join(sorted(b['rows_without_count']))}")
        if b["excluded"]:
            print(f"  excluded as not net new housing: {', '.join(sorted(b['excluded']))}")

    if args.show_unresolved:
        print("\n  residential rows whose unit count is absent from the source description:")
        for y, bucket in sorted(years.items()):
            for no in sorted(bucket["rows_without_count"]):
                rec = permits.get(no, {})
                print(f"    {y} {no}: {(rec.get('description') or '(blank)')[:70]}")

    if unresolved:
        print("\n  WARNING: no pattern or hand value resolved these rows, add them to"
              " HAND_UNITS or NO_UNIT_COUNT:")
        for y, no, desc in sorted(unresolved):
            print(f"    {y} {no}: {desc}")

    print("\n  Units are estimates parsed from free-text permit descriptions."
          "\n  HCD APR Table A is authoritative for units filed but the current year"
          "\n  is unavailable until April of the following year.")
    if 2022 in years:
        print(f"  Unresolved: {', '.join(SUSPECTED_DUPLICATES_2022)} may be duplicate"
              " plan-check filings (2022 overstated by 19 units if so).")


if __name__ == "__main__":
    main()
