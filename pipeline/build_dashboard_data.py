#!/usr/bin/env python3
"""
Build dashboard JSON files for stoside.org from APR + ACS data.

Produces:
  - permitted-units.json   (approved units by tract, year, category)
  - affordable-units.json  (income tier breakdown by tract, year)
  - affordability-map.json (ACS rent burden, home values, income by tract)
  - tracts.geojson         (simplified Oceanside census tract boundaries)

Data sources:
  - HCD APR reference: watchdog/data/reference/hcd-apr-oceanside.json
  - TIGER/Line:        Census Bureau tract + place shapefiles (cached)
  - Census ACS 5-Year: API (no key required for basic queries)
"""

import argparse
import json
import os
import sys
import urllib.request
import zipfile
from collections import defaultdict
from pathlib import Path

from dotenv import load_dotenv
load_dotenv(Path(__file__).parent.parent / ".env")

import geopandas as gpd
import pandas as pd
from shapely.geometry import Point, mapping

SCRIPT_DIR = Path(__file__).parent
REPO_ROOT = SCRIPT_DIR.parent
CACHE_DIR = SCRIPT_DIR / ".cache"

# Oceanside 6th Cycle RHNA (2021-2029)
# Source: SANDAG Final 6th Cycle Regional Housing Needs Assessment Plan (2020-07-10)
RHNA = {
    "veryLow": 1268,
    "low": 718,
    "moderate": 883,
    "aboveModerate": 2574,
    "total": 5443,
}
RHNA_CYCLE_START = 2021
RHNA_CYCLE_END = 2029
RHNA_CYCLE_YEARS = RHNA_CYCLE_END - RHNA_CYCLE_START + 1

STATE_FIPS = "06"
COUNTY_FIPS = "073"
OCEANSIDE_PLACE_FIPS = "53322"

TIGER_YEAR = "2024"
TIGER_TRACT_URL = f"https://www2.census.gov/geo/tiger/TIGER{TIGER_YEAR}/TRACT/tl_{TIGER_YEAR}_{STATE_FIPS}_tract.zip"
TIGER_PLACE_URL = f"https://www2.census.gov/geo/tiger/TIGER{TIGER_YEAR}/PLACE/tl_{TIGER_YEAR}_{STATE_FIPS}_place.zip"

ACS_YEAR = "2022"
ACS_BASE = f"https://api.census.gov/data/{ACS_YEAR}/acs/acs5"

UNIT_CAT_MAP = {
    "SFD": "SFD",
    "SFA": "SFA",
    "2 to 4": "2-4",
    "5+": "5+",
    "ADU": "ADU",
    "MH": "MH",
}


def download_and_cache(url: str, name: str) -> Path:
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    zip_path = CACHE_DIR / f"{name}.zip"
    extract_dir = CACHE_DIR / name

    if extract_dir.exists() and any(extract_dir.iterdir()):
        return extract_dir

    if not zip_path.exists():
        print(f"Downloading {name}...", file=sys.stderr)
        urllib.request.urlretrieve(url, zip_path)

    print(f"Extracting {name}...", file=sys.stderr)
    with zipfile.ZipFile(zip_path) as zf:
        zf.extractall(extract_dir)

    return extract_dir


def load_oceanside_tracts() -> gpd.GeoDataFrame:
    """Load TIGER/Line tracts, filter to those intersecting Oceanside city boundary."""
    tract_dir = download_and_cache(TIGER_TRACT_URL, "tracts")
    place_dir = download_and_cache(TIGER_PLACE_URL, "places")

    shp_files = list(tract_dir.glob("*.shp"))
    tracts = gpd.read_file(shp_files[0])
    tracts = tracts[tracts["COUNTYFP"] == COUNTY_FIPS].copy()

    place_files = list(place_dir.glob("*.shp"))
    places = gpd.read_file(place_files[0])
    oceanside = places[places["PLACEFP"] == OCEANSIDE_PLACE_FIPS]

    if oceanside.empty:
        print("WARNING: Oceanside place boundary not found, using county tracts", file=sys.stderr)
        return tracts

    oceanside_geom = oceanside.unary_union
    mask = tracts.intersects(oceanside_geom)
    result = tracts[mask].copy()
    print(f"Found {len(result)} tracts intersecting Oceanside", file=sys.stderr)
    return result


def simplify_tracts_geojson(tracts: gpd.GeoDataFrame) -> dict:
    """Produce a lightweight GeoJSON with just GEOID and tract name."""
    simplified = tracts.to_crs(epsg=3857)
    simplified["geometry"] = simplified["geometry"].simplify(50)
    simplified = simplified.to_crs(epsg=4326)

    features = []
    for _, row in simplified.iterrows():
        geoid = row["GEOID"]
        tract_num = row.get("NAMELSAD", f"Tract {geoid[-6:]}")
        features.append({
            "type": "Feature",
            "id": geoid,
            "properties": {"GEOID": geoid, "name": tract_num},
            "geometry": mapping(row.geometry),
        })

    return {
        "type": "FeatureCollection",
        "features": features,
    }


def assign_tracts(records: list[dict], tracts: gpd.GeoDataFrame) -> list[dict]:
    """Assign tractId to each record via point-in-polygon."""
    points = []
    valid_indices = []
    for i, r in enumerate(records):
        lat, lon = r.get("latitude"), r.get("longitude")
        if lat and lon:
            try:
                points.append(Point(float(lon), float(lat)))
                valid_indices.append(i)
            except (ValueError, TypeError):
                pass

    if not points:
        return records

    points_gdf = gpd.GeoDataFrame(
        {"idx": valid_indices},
        geometry=points,
        crs="EPSG:4326",
    )

    tracts_4326 = tracts[["GEOID", "geometry"]].to_crs(epsg=4326)
    joined = gpd.sjoin(points_gdf, tracts_4326, how="left", predicate="within")

    tract_map = {}
    for _, row in joined.iterrows():
        idx = row["idx"]
        geoid = row.get("GEOID")
        if pd.notna(geoid):
            tract_map[idx] = geoid

    assigned = 0
    for i, r in enumerate(records):
        geoid = tract_map.get(i)
        if geoid:
            r["tractId"] = geoid
            assigned += 1
        else:
            r["tractId"] = None

    print(f"Assigned tracts to {assigned}/{len(records)} records ({len(records) - assigned} unlocated)", file=sys.stderr)
    return records


def build_permitted_units(records: list[dict], tracts: gpd.GeoDataFrame) -> dict:
    """Aggregate approved units by tract, year, and unit category."""
    years = sorted(set(r["year"] for r in records))
    year_min, year_max = int(years[0]), int(years[-1])

    tract_data = defaultdict(lambda: {
        "totalApproved": 0,
        "byYear": defaultdict(int),
        "byCategory": defaultdict(int),
        "byYearCategory": defaultdict(lambda: defaultdict(int)),
    })

    citywide_total = 0
    citywide_by_year = defaultdict(int)
    citywide_by_cat = defaultdict(int)
    unlocated_total = 0

    for r in records:
        approved = int(r.get("tot_approved_units") or 0)
        if approved <= 0:
            continue

        year = r["year"]
        cat = UNIT_CAT_MAP.get(r.get("unit_cat", ""), "Other")
        tract_id = r.get("tractId")

        citywide_total += approved
        citywide_by_year[year] += approved
        citywide_by_cat[cat] += approved

        if tract_id:
            tract_data[tract_id]["totalApproved"] += approved
            tract_data[tract_id]["byYear"][year] += approved
            tract_data[tract_id]["byCategory"][cat] += approved
            tract_data[tract_id]["byYearCategory"][year][cat] += approved
        else:
            unlocated_total += approved

    # RHNA pace: approved in cycle years / total RHNA
    cycle_approved = sum(
        citywide_by_year.get(str(y), 0)
        for y in range(RHNA_CYCLE_START, year_max + 1)
    )
    rhna_pct = round(cycle_approved / RHNA["total"] * 100, 1) if RHNA["total"] else 0

    # YTD: current year
    ytd = citywide_by_year.get(str(year_max), 0)

    tract_names = {}
    for _, row in tracts.iterrows():
        tract_names[row["GEOID"]] = row.get("NAMELSAD", f"Tract {row['GEOID'][-6:]}")

    tract_list = []
    for geoid in sorted(set(list(tract_data.keys()) + list(tract_names.keys()))):
        td = tract_data.get(geoid, {"totalApproved": 0, "byYear": {}, "byCategory": {}})
        tract_list.append({
            "tractId": geoid,
            "name": tract_names.get(geoid, f"Tract {geoid[-6:]}"),
            "totalApproved": td["totalApproved"],
            "byYear": dict(td["byYear"]),
            "byCategory": dict(td["byCategory"]),
            "byYearCategory": {yr: dict(cats) for yr, cats in td.get("byYearCategory", {}).items()},
        })

    return {
        "tracts": tract_list,
        "citywide": {
            "totalApproved": citywide_total,
            "rhnaPace": rhna_pct,
            "ytd": ytd,
            "byYear": dict(citywide_by_year),
            "byCategory": dict(citywide_by_cat),
        },
        "unlocated": {"totalApproved": unlocated_total},
        "yearRange": [year_min, year_max],
        "sparklineByYear": [citywide_by_year.get(str(y), 0) for y in range(year_min, year_max + 1)],
        "categories": sorted(set(UNIT_CAT_MAP.values())),
        "rhna": RHNA,
        "metadata": {
            "source": "HCD Annual Progress Report (APR) Table A",
            "notes": f"{len(records) - sum(1 for r in records if r.get('tractId'))} records could not be geocoded to a tract.",
        },
    }


def build_affordable_units(records: list[dict], tracts: gpd.GeoDataFrame) -> dict:
    """Aggregate affordable vs market-rate units by tract, year, and income tier."""
    INCOME_TIERS = ["veryLow", "low", "moderate", "aboveModerate"]
    TIER_FIELDS = {
        "veryLow": {
            "dr": ["acutely_low_income_dr", "extremely_low_income_dr", "vlow_income_dr"],
            "ndr": ["acutely_low_income_ndr", "extremely_low_income_ndr", "vlow_income_ndr"],
        },
        "low": {
            "dr": ["low_income_dr"],
            "ndr": ["low_income_ndr"],
        },
        "moderate": {
            "dr": ["mod_income_dr"],
            "ndr": ["mod_income_ndr"],
        },
        "aboveModerate": {
            "fields": ["above_mod_income"],
        },
    }

    years = sorted(set(r["year"] for r in records))
    year_min, year_max = int(years[0]), int(years[-1])

    def extract_tiers(r: dict) -> dict:
        result = {}
        for tier, fields in TIER_FIELDS.items():
            if tier == "aboveModerate":
                result[tier] = sum(int(r.get(f, 0) or 0) for f in fields["fields"])
            else:
                dr = sum(int(r.get(f, 0) or 0) for f in fields["dr"])
                ndr = sum(int(r.get(f, 0) or 0) for f in fields["ndr"])
                result[tier] = {"dr": dr, "ndr": ndr, "total": dr + ndr}
        return result

    tract_data = defaultdict(lambda: {
        "deedRestricted": 0,
        "marketRate": 0,
        "byTier": {t: 0 for t in INCOME_TIERS},
        "byYear": defaultdict(int),
    })

    citywide_dr = 0
    citywide_mr = 0
    citywide_by_tier = {t: 0 for t in INCOME_TIERS}
    yearly_by_tier = defaultdict(lambda: {t: 0 for t in INCOME_TIERS})

    for r in records:
        tiers = extract_tiers(r)
        tract_id = r.get("tractId")
        year = r["year"]

        dr_sum = 0
        for tier in ["veryLow", "low", "moderate"]:
            dr_sum += tiers[tier]["dr"]

        total_below_mod = 0
        for tier in ["veryLow", "low", "moderate"]:
            total_below_mod += tiers[tier]["total"]

        above_mod = tiers["aboveModerate"]
        record_total = total_below_mod + above_mod

        citywide_dr += dr_sum
        citywide_mr += record_total - dr_sum

        for tier in INCOME_TIERS:
            if tier == "aboveModerate":
                val = tiers[tier]
            else:
                val = tiers[tier]["total"]
            citywide_by_tier[tier] += val
            yearly_by_tier[year][tier] += val

        if tract_id:
            tract_data[tract_id]["deedRestricted"] += dr_sum
            tract_data[tract_id]["marketRate"] += record_total - dr_sum
            for tier in INCOME_TIERS:
                if tier == "aboveModerate":
                    tract_data[tract_id]["byTier"][tier] += tiers[tier]
                else:
                    tract_data[tract_id]["byTier"][tier] += tiers[tier]["total"]
            tract_data[tract_id]["byYear"][year] += record_total

    total_units = citywide_dr + citywide_mr
    pct_affordable = round(citywide_dr / total_units * 100, 1) if total_units else 0

    # RHNA pace by tier
    rhna_pace_by_tier = {}
    for tier in INCOME_TIERS:
        rhna_target = RHNA.get(tier, 0)
        cycle_units = sum(
            yearly_by_tier.get(str(y), {}).get(tier, 0)
            for y in range(RHNA_CYCLE_START, year_max + 1)
        )
        rhna_pace_by_tier[tier] = {
            "target": rhna_target,
            "achieved": cycle_units,
            "pct": round(cycle_units / rhna_target * 100, 1) if rhna_target else 0,
        }

    tract_names = {}
    for _, row in tracts.iterrows():
        tract_names[row["GEOID"]] = row.get("NAMELSAD", f"Tract {row['GEOID'][-6:]}")

    tract_list = []
    for geoid in sorted(set(list(tract_data.keys()) + list(tract_names.keys()))):
        td = tract_data.get(geoid, {
            "deedRestricted": 0, "marketRate": 0,
            "byTier": {t: 0 for t in INCOME_TIERS}, "byYear": {},
        })
        tract_list.append({
            "tractId": geoid,
            "name": tract_names.get(geoid, f"Tract {geoid[-6:]}"),
            "deedRestricted": td["deedRestricted"],
            "marketRate": td["marketRate"],
            "byTier": td["byTier"],
            "byYear": dict(td.get("byYear", {})),
        })

    # Yearly stacked bar data
    yearly_stacked = []
    for y in range(year_min, year_max + 1):
        yr = str(y)
        for tier in INCOME_TIERS:
            yearly_stacked.append({
                "year": y,
                "tier": tier,
                "units": yearly_by_tier.get(yr, {}).get(tier, 0),
            })

    return {
        "tracts": tract_list,
        "citywide": {
            "deedRestricted": citywide_dr,
            "marketRate": citywide_mr,
            "percentAffordable": pct_affordable,
            "byTier": citywide_by_tier,
        },
        "yearRange": [year_min, year_max],
        "yearlyByTier": yearly_stacked,
        "rhnaPaceByTier": rhna_pace_by_tier,
        "rhna": RHNA,
        "incomeTiers": INCOME_TIERS,
        "metadata": {
            "source": "HCD Annual Progress Report (APR) Table A",
            "notes": "Deed-restricted = units with income restrictions recorded. Very Low includes acutely low, extremely low, and very low income categories.",
        },
    }


def fetch_acs_data(tracts: gpd.GeoDataFrame) -> dict:
    """Fetch Census ACS 5-Year estimates for Oceanside tracts."""
    variables = [
        "B25071_001E",  # Median gross rent as % of household income
        "B25077_001E",  # Median home value (owner-occupied)
        "B19013_001E",  # Median household income
        "B25070_007E",  # Renters paying 30-34.9% of income
        "B25070_008E",  # Renters paying 35-39.9%
        "B25070_009E",  # Renters paying 40-49.9%
        "B25070_010E",  # Renters paying 50%+
        "B25070_001E",  # Total renters (for cost-burden denominator)
        "B25064_001E",  # Median gross rent
    ]

    api_key = os.environ.get("CENSUS_API_KEY", "").strip()
    if not api_key:
        print("WARNING: CENSUS_API_KEY not set. Set it in .env or environment.", file=sys.stderr)
        print("  Get a free key at https://api.census.gov/data/key_signup.html", file=sys.stderr)
        return {}

    var_str = ",".join(variables)
    url = f"{ACS_BASE}?get=NAME,{var_str}&for=tract:*&in=state:{STATE_FIPS}%20county:{COUNTY_FIPS}&key={api_key}"

    print(f"Fetching ACS {ACS_YEAR} data...", file=sys.stderr)
    req = urllib.request.Request(url, headers={"User-Agent": "stoside-dashboard/1.0"})
    with urllib.request.urlopen(req) as resp:
        data = json.loads(resp.read())

    headers = data[0]
    rows = data[1:]

    oceanside_geoids = set(tracts["GEOID"].values)

    acs_by_tract = {}
    for row in rows:
        row_dict = dict(zip(headers, row))
        geoid = f"{STATE_FIPS}{COUNTY_FIPS}{row_dict['tract']}"
        if geoid not in oceanside_geoids:
            continue

        def safe_int(val):
            try:
                v = int(val)
                return v if v >= 0 else None
            except (ValueError, TypeError):
                return None

        rent_pct = safe_int(row_dict.get("B25071_001E"))
        home_value = safe_int(row_dict.get("B25077_001E"))
        hh_income = safe_int(row_dict.get("B19013_001E"))
        median_rent = safe_int(row_dict.get("B25064_001E"))

        total_renters = safe_int(row_dict.get("B25070_001E"))
        burdened_30_34 = safe_int(row_dict.get("B25070_007E")) or 0
        burdened_35_39 = safe_int(row_dict.get("B25070_008E")) or 0
        burdened_40_49 = safe_int(row_dict.get("B25070_009E")) or 0
        burdened_50_plus = safe_int(row_dict.get("B25070_010E")) or 0

        cost_burdened = burdened_30_34 + burdened_35_39 + burdened_40_49 + burdened_50_plus
        cost_burdened_pct = round(cost_burdened / total_renters * 100, 1) if total_renters else None

        acs_by_tract[geoid] = {
            "medianRent": median_rent,
            "medianRentPctIncome": rent_pct,
            "medianHomeValue": home_value,
            "medianHouseholdIncome": hh_income,
            "costBurdenedPct": cost_burdened_pct,
            "totalRenters": total_renters,
            "costBurdenedCount": cost_burdened,
        }

    print(f"Got ACS data for {len(acs_by_tract)} tracts", file=sys.stderr)
    return acs_by_tract


def build_affordability_map(tracts: gpd.GeoDataFrame, acs_data: dict) -> dict:
    """Build affordability dashboard data from ACS estimates."""
    tract_names = {}
    for _, row in tracts.iterrows():
        tract_names[row["GEOID"]] = row.get("NAMELSAD", f"Tract {row['GEOID'][-6:]}")

    tract_list = []
    rents = []
    values = []
    burdens = []

    for geoid in sorted(tract_names.keys()):
        acs = acs_data.get(geoid, {})
        entry = {
            "tractId": geoid,
            "name": tract_names[geoid],
            "medianRent": acs.get("medianRent"),
            "medianRentPctIncome": acs.get("medianRentPctIncome"),
            "medianHomeValue": acs.get("medianHomeValue"),
            "medianHouseholdIncome": acs.get("medianHouseholdIncome"),
            "costBurdenedPct": acs.get("costBurdenedPct"),
        }
        tract_list.append(entry)

        if acs.get("medianRent"):
            rents.append(acs["medianRent"])
        if acs.get("medianHomeValue"):
            values.append(acs["medianHomeValue"])
        if acs.get("costBurdenedPct"):
            burdens.append(acs["costBurdenedPct"])

    def median(lst):
        if not lst:
            return None
        s = sorted(lst)
        n = len(s)
        mid = n // 2
        return s[mid] if n % 2 else round((s[mid - 1] + s[mid]) / 2)

    return {
        "tracts": tract_list,
        "citywide": {
            "medianRent": median(rents),
            "medianHomeValue": median(values),
            "costBurdenedPct": round(sum(burdens) / len(burdens), 1) if burdens else None,
        },
        "metrics": [
            {"key": "costBurdenedPct", "label": "% Cost-Burdened", "format": "pct", "colorScale": "sequential"},
            {"key": "medianRent", "label": "Median Rent", "format": "currency", "colorScale": "sequential"},
            {"key": "medianHomeValue", "label": "Median Home Value", "format": "currency", "colorScale": "sequential"},
            {"key": "medianHouseholdIncome", "label": "Median Income", "format": "currency", "colorScale": "sequential"},
            {"key": "medianRentPctIncome", "label": "Rent % of Income", "format": "pct", "colorScale": "sequential"},
        ],
        "metadata": {
            "source": f"U.S. Census Bureau, ACS 5-Year Estimates ({ACS_YEAR})",
            "notes": "Tract-level estimates. Cost-burdened = renters paying 30%+ of income on housing.",
        },
    }


def main():
    parser = argparse.ArgumentParser(description="Build dashboard data files")
    parser.add_argument("--output", "-o", default=str(REPO_ROOT / "output" / "dashboard"),
                        help="Output directory")
    parser.add_argument("--apr", default=None,
                        help="Path to APR reference JSON (default: from config.yaml)")
    parser.add_argument("--skip-acs", action="store_true",
                        help="Skip ACS data fetch (affordability dashboard will have empty tracts)")
    args = parser.parse_args()

    output_dir = Path(args.output)
    output_dir.mkdir(parents=True, exist_ok=True)

    if args.apr:
        apr_path = Path(args.apr)
    else:
        import yaml
        with open(REPO_ROOT / "config.yaml") as f:
            cfg = yaml.safe_load(f)
        apr_path = Path(cfg["watchdog_data"]) / "reference" / "hcd-apr-oceanside.json"

    print(f"Loading APR data from {apr_path}...", file=sys.stderr)
    with open(apr_path) as f:
        records = json.load(f)
    print(f"Loaded {len(records)} APR records", file=sys.stderr)

    print("Loading census tract boundaries...", file=sys.stderr)
    tracts = load_oceanside_tracts()

    print("Assigning census tracts to APR records...", file=sys.stderr)
    records = assign_tracts(records, tracts)

    print("Building permitted-units.json...", file=sys.stderr)
    permitted = build_permitted_units(records, tracts)
    with open(output_dir / "permitted-units.json", "w") as f:
        json.dump(permitted, f, indent=2)

    print("Building affordable-units.json...", file=sys.stderr)
    affordable = build_affordable_units(records, tracts)
    with open(output_dir / "affordable-units.json", "w") as f:
        json.dump(affordable, f, indent=2)

    if not args.skip_acs:
        print("Fetching ACS data...", file=sys.stderr)
        acs_data = fetch_acs_data(tracts)
        print("Building affordability-map.json...", file=sys.stderr)
        affordability = build_affordability_map(tracts, acs_data)
    else:
        print("Skipping ACS data (--skip-acs)", file=sys.stderr)
        affordability = build_affordability_map(tracts, {})

    with open(output_dir / "affordability-map.json", "w") as f:
        json.dump(affordability, f, indent=2)

    print("Building tracts.geojson...", file=sys.stderr)
    geojson = simplify_tracts_geojson(tracts)
    with open(output_dir / "tracts.geojson", "w") as f:
        json.dump(geojson, f)
    geojson_size = os.path.getsize(output_dir / "tracts.geojson") / 1024
    print(f"tracts.geojson: {geojson_size:.0f} KB ({len(geojson['features'])} features)", file=sys.stderr)

    print(f"\nAll files written to {output_dir}/", file=sys.stderr)
    print(f"  permitted-units.json  ({os.path.getsize(output_dir / 'permitted-units.json') / 1024:.0f} KB)", file=sys.stderr)
    print(f"  affordable-units.json ({os.path.getsize(output_dir / 'affordable-units.json') / 1024:.0f} KB)", file=sys.stderr)
    print(f"  affordability-map.json ({os.path.getsize(output_dir / 'affordability-map.json') / 1024:.0f} KB)", file=sys.stderr)
    print(f"  tracts.geojson        ({geojson_size:.0f} KB)", file=sys.stderr)


if __name__ == "__main__":
    main()
