#!/usr/bin/env python3
"""Filed housing units inside the D-Downtown District, the area the density cap governs.

"Downtown" means Oceanside's Article 12 D-Downtown District: parcels whose
zoning code begins "D-". That is the entity the city itself legislates against,
and it is the area the 86 du/acre mixed-use cap applies to. Never infer downtown
from street names, addresses, or ZIP codes.

Cap timeline, from watchdog meeting records (Oceanside agency only):
  2019-08-21  Reso 19-R0562-1 removed the 43 du/ac cap and FAR limits for the
              residential component of downtown mixed-use, enabling 168 du/ac.
  2022-07-11  ZA22-00002 / LCPA22-00002 proposed reinstating a cap at 75 du/ac.
  2023-10-04  Staff recommended 100 du/ac with bonus to 200; an 85 du/ac
              alternate motion tied 2-2.
  2023-10-18  Adopted 86 du/ac with no density bonus enhancement, 3-2.
  2023-11-01  Ordinance adopted, amending Article 12 and the LCP Implementation
              Plan.
  2023-12-01  Effective OUTSIDE the Coastal Zone. Coastal Zone portions were
              pending Coastal Commission certification, so the effective date
              varies by subdistrict. Unresolved which subdistricts were
              certified when; settle that before making a causal claim.

Method: HCD APR Table A is the source, because it carries real unit counts
rather than parsed prose and covers ministerial approvals. Rows are grouped by
APP_SUBMIT_DT year (filed, not approved), units from TOT_PROPOSED_UNITS, and
placed by joining APN to a parcel centroid and running point-in-polygon against
the D-District boundary GeoJSON.

Why this reports join coverage so loudly: rows whose APN is missing from the
centroid file are counted as neither downtown nor rest, they are silently
dropped. Coverage swings from 30% to 97% by year, so a downtown figure without
its coverage number is not interpretable. 2020 and 2021 in particular are soft.

Do NOT use the etrakit `is_downtown` field for a time series. Zone enrichment
has barely run for 2020-2025, and 783 of 1,965 housing permits sit on 50
parcels the city GIS cannot find, one of which (APN 1571008400) carries 406
permits by itself and is plainly a data-entry default rather than a parcel.

Usage:
    python downtown_units.py                 # table with join coverage
    python downtown_units.py --since 2022    # limit start year
    python downtown_units.py --json
    python downtown_units.py --cap-timeline  # print the ordinance history
"""

import argparse
import csv
import json
import sys
from collections import defaultdict
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(REPO_ROOT / "lib"))
sys.path.insert(0, str(REPO_ROOT))

from civic_utils import _load_downtown_boundary, _load_apn_centroids, watchdog_data_dir
import config

# Join coverage below this fraction means the year's downtown figure cannot
# carry an argument on its own.
COVERAGE_WARN = 0.90

CAP_TIMELINE = __doc__.split("Cap timeline")[1].split("Method:")[0].rstrip()


def submit_year(value):
    """Year from an APR APP_SUBMIT_DT, which appears as M/D/YYYY or YYYY-..."""
    if not value or len(value) < 4:
        return None
    try:
        return int(value.split("/")[-1]) if "/" in value else int(value[:4])
    except ValueError:
        return None


def tally(city="oceanside", since=2018):
    """Downtown vs rest filed units per year, with the APN join coverage."""
    from shapely.geometry import Point

    boundary = _load_downtown_boundary()
    if not boundary:
        raise SystemExit("No D-District boundary available (expected d-district-zoning.geojson)")
    centroids = _load_apn_centroids()
    if not centroids:
        raise SystemExit("No parcel centroids available for the APN join")
    apr = watchdog_data_dir() / "hcd-apr-tablea.csv"
    if not apr.exists():
        raise SystemExit(f"APR Table A not found: {apr}")

    years = defaultdict(lambda: {
        "downtown": 0, "rest": 0, "rows": 0, "joined": 0, "units_dropped": 0,
    })
    with open(apr) as fh:
        for row in csv.DictReader(fh):
            if city.lower() not in (row.get("JURIS_NAME", "") or "").lower():
                continue
            year = submit_year(row.get("APP_SUBMIT_DT", ""))
            if year is None or year < since:
                continue
            try:
                units = int(float(row.get("TOT_PROPOSED_UNITS", 0) or 0))
            except (ValueError, TypeError):
                units = 1
            units = max(units, 1)

            bucket = years[year]
            bucket["rows"] += 1
            apn = (row.get("APN", "") or "").replace("-", "")
            if apn not in centroids:
                bucket["units_dropped"] += units
                continue
            bucket["joined"] += 1
            lon, lat = centroids[apn]
            if boundary.contains(Point(lon, lat)):
                bucket["downtown"] += units
            else:
                bucket["rest"] += units

    for bucket in years.values():
        bucket["total"] = bucket["downtown"] + bucket["rest"]
        bucket["coverage"] = bucket["joined"] / bucket["rows"] if bucket["rows"] else 0.0
        bucket["downtown_pct"] = (
            round(bucket["downtown"] / bucket["total"] * 100) if bucket["total"] else 0
        )
    return years


def main():
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--city", default=config.get("identity/primary_city_slug", "oceanside"))
    ap.add_argument("--since", type=int, default=2018, help="first submit year to report")
    ap.add_argument("--json", action="store_true")
    ap.add_argument("--cap-timeline", action="store_true",
                    help="print the density cap ordinance history and exit")
    args = ap.parse_args()

    if args.cap_timeline:
        print("Downtown density cap timeline" + CAP_TIMELINE)
        return

    if args.city.lower() != "oceanside":
        raise SystemExit(f"Only Oceanside has a D-District boundary and parcel centroids; got '{args.city}'")

    years = tally(city=args.city, since=args.since)
    if args.json:
        print(json.dumps({
            "city": args.city,
            "metric": "filed housing units inside the Article 12 D-Downtown District",
            "source": "HCD APR Table A, APP_SUBMIT_DT year, TOT_PROPOSED_UNITS",
            "placement": "APN to parcel centroid, point-in-polygon against D-District boundary",
            "years": {str(y): b for y, b in sorted(years.items())},
        }, indent=2))
        return

    print("=== FILED HOUSING UNITS, D-DOWNTOWN DISTRICT vs REST OF CITY ===")
    print("    source: HCD APR Table A, by APP_SUBMIT_DT (filed, not approved)")
    print("    placement: APN centroid, point-in-polygon on the D-District boundary\n")
    print(f"  {'year':<6}{'downtown':>10}{'rest':>8}{'dt share':>10}{'APN join':>10}{'units unplaced':>16}")
    for y, b in sorted(years.items()):
        flag = "  <-- low coverage" if b["coverage"] < COVERAGE_WARN else ""
        print(f"  {y:<6}{b['downtown']:>10}{b['rest']:>8}{b['downtown_pct']:>9}%"
              f"{round(b['coverage'] * 100):>9}%{b['units_dropped']:>16}{flag}")

    weak = [str(y) for y, b in sorted(years.items()) if b["coverage"] < COVERAGE_WARN]
    print(f"\n  Units unplaced are rows whose APN is missing from the centroid file."
          f"\n  They count as neither downtown nor rest, so a year's downtown figure is"
          f"\n  only as good as its join coverage.")
    if weak:
        print(f"  Treat these years as soft, coverage under {round(COVERAGE_WARN * 100)}%: {', '.join(weak)}")
    print("\n  The current year is absent until the APR is filed in April of the"
          "\n  following year. Do not substitute etrakit is_downtown for it; see the"
          "\n  module docstring for why that field is unusable as a series.")


if __name__ == "__main__":
    main()
