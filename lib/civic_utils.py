"""Slim civic utilities for analysis repo — LLM calls and JSON I/O only."""

import json
import os
import subprocess
import time
from pathlib import Path

import yaml

REPO_ROOT = Path(__file__).resolve().parent.parent
CONFIG_FILE = REPO_ROOT / "config.yaml"

_config_cache = None


def load_config():
    global _config_cache
    if _config_cache is None:
        with open(CONFIG_FILE) as f:
            _config_cache = yaml.safe_load(f)
    return _config_cache


def watchdog_data_dir():
    return Path(load_config()["watchdog_data"])


def all_meetings_dirs():
    data = watchdog_data_dir()
    dirs = []
    for d in sorted(data.iterdir()):
        md = d / "meetings"
        if md.is_dir():
            dirs.append(md)
    return dirs


def claude_api_call(client, max_retries=20, **kwargs):
    """Call Claude API with aggressive retry on rate limits."""
    import anthropic

    for attempt in range(max_retries):
        try:
            return client.messages.create(**kwargs)
        except anthropic.RateLimitError:
            wait = min(60 * (2 ** attempt), 600)
            print(f"  Rate limited (attempt {attempt+1}/{max_retries}). Waiting {wait}s...")
            time.sleep(wait)
        except anthropic.APIStatusError as e:
            if e.status_code == 529:
                wait = min(30 * (2 ** attempt), 300)
                print(f"  API overloaded (attempt {attempt+1}/{max_retries}). Waiting {wait}s...")
                time.sleep(wait)
            else:
                raise
    raise Exception(f"API call failed after {max_retries} retries")


def claude_local_call(prompt, system=None, timeout=300):
    """Call Claude via claude -p (subscription, no API cost)."""
    full_prompt = ""
    if system:
        full_prompt = f"SYSTEM CONTEXT:\n{system}\n\n---\n\n"
    full_prompt += prompt

    env = {k: v for k, v in os.environ.items() if k != "ANTHROPIC_API_KEY"}
    prompt_kb = len(full_prompt.encode("utf-8")) / 1024
    try:
        result = subprocess.run(
            ["claude", "-p", "--output-format", "text"],
            input=full_prompt,
            capture_output=True, text=True, timeout=timeout,
            env=env,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout.strip()
        if result.stderr:
            print(f"  claude -p error (rc={result.returncode}, {prompt_kb:.0f}KB prompt): {result.stderr[:300]}")
        else:
            print(f"  claude -p failed silently (rc={result.returncode}, {prompt_kb:.0f}KB prompt, stdout={len(result.stdout)}B)")
        return None
    except FileNotFoundError:
        print("  claude CLI not found.")
        return None
    except subprocess.TimeoutExpired:
        print(f"  claude -p timed out ({timeout}s)")
        return None


def load_analysis_context(topics=None, skill_names=None):
    """Load analytical knowledge + watchdog skills for LLM system context.

    Combines:
      1. Knowledge articles from REPO_ROOT/knowledge/<topic>/*.md
      2. Watchdog skills: SKILL.md + recent-developments.md per skill (intel feed)

    Args:
        topics: List of topic directory names to load (e.g. ["ca-housing-law",
                "housing-advocacy"]). None loads all topics.
        skill_names: Watchdog skill directories to load. Default: ["ca-housing-law"].
    """
    if skill_names is None:
        skill_names = ["ca-housing-law"]

    parts = []

    # 1. Knowledge articles (this repo) — organized by topic subdirectory
    knowledge_dir = REPO_ROOT / "knowledge"
    if knowledge_dir.is_dir():
        if topics is None:
            article_paths = sorted(knowledge_dir.rglob("*.md"))
        else:
            article_paths = []
            for topic in topics:
                topic_dir = knowledge_dir / topic
                if topic_dir.is_dir():
                    article_paths.extend(sorted(topic_dir.glob("*.md")))
        for md in article_paths:
            text = md.read_text().strip()
            if text:
                parts.append(text)

    # 2. Watchdog skills
    skills_dir = watchdog_data_dir().parent / ".claude" / "skills"
    for name in skill_names:
        skill_path = skills_dir / name / "SKILL.md"
        if skill_path.exists():
            parts.append(skill_path.read_text())
        supplement = skills_dir / name / "recent-developments.md"
        if supplement.exists():
            parts.append(supplement.read_text())

    return "\n\n---\n\n".join(parts)


def load_scored_records():
    """Load pre-scored records from scored-records.jsonl."""
    scored_path = REPO_ROOT / "output" / "scored" / "scored-records.jsonl"
    records = {}
    if not scored_path.exists():
        return records
    for line in scored_path.read_text().splitlines():
        if line.strip():
            try:
                r = json.loads(line)
                records[str(r.get("meeting_id", ""))] = r
            except Exception:
                continue
    return records


def load_etrakit_stats(year, city="oceanside"):
    """Load etrakit filing and permit stats for a given year and city.

    Returns dict with 'filings' (discretionary projects) and 'permits' (building permits)
    aggregated by type, plus housing-specific breakdowns.
    """
    from collections import Counter
    from datetime import datetime

    permits_dir = watchdog_data_dir() / city.lower() / "permits"

    HOUSING_PERMIT_TYPES = {
        "BLD SFD OR DUPLEX", "BLD ACCESSORY DWELLING", "BLD MULTI FAMILY",
        "BLD MOBILE HOME",
    }
    HOUSING_PROJECT_TYPES = {
        "DEVELOPMENT PLAN", "R DEVELOPMENT PLAN", "DENSITY BONUS APPLICATION",
        "CONDITIONAL USE PERM",
    }

    result = {
        "year": year,
        "city": city.lower(),
        "filings": {"total": 0, "by_type": {}, "housing_related": 0, "descriptions": []},
        "permits": {"total_housing": 0, "by_type": {}, "completions": 0, "issued": 0},
    }

    projects_file = permits_dir / f"etrakit-projects-{year}.jsonl"
    if projects_file.exists():
        for line in projects_file.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            ptype = r.get("type", "")
            result["filings"]["total"] += 1
            result["filings"]["by_type"][ptype] = result["filings"]["by_type"].get(ptype, 0) + 1
            if ptype in HOUSING_PROJECT_TYPES:
                result["filings"]["housing_related"] += 1
                desc = r.get("description", "").strip()
                if desc:
                    result["filings"]["descriptions"].append({
                        "project_no": r.get("project_no", ""),
                        "type": ptype,
                        "description": desc,
                        "applied": r.get("applied", ""),
                        "address": r.get("address", ""),
                        "approved": r.get("approved", ""),
                    })

    permits_file = permits_dir / f"etrakit-permits-{year}.jsonl"
    if permits_file.exists():
        for line in permits_file.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            ptype = r.get("type", "")
            status = r.get("status", "")
            if ptype not in HOUSING_PERMIT_TYPES:
                continue
            result["permits"]["total_housing"] += 1
            result["permits"]["by_type"][ptype] = result["permits"]["by_type"].get(ptype, 0) + 1
            if status == "FINALED":
                result["permits"]["completions"] += 1
            if status in ("ISSUED", "FINALED", "TEMP CERT OF OCC"):
                result["permits"]["issued"] += 1

    return result


def _etrakit_latest_date(year, city="oceanside"):
    """Find the latest filing date in etrakit data for a year."""
    from datetime import datetime
    permits_dir = watchdog_data_dir() / city.lower() / "permits"
    latest = None
    for fname in [f"etrakit-projects-{year}.jsonl", f"etrakit-permits-{year}.jsonl"]:
        f = permits_dir / fname
        if not f.exists():
            continue
        for line in f.read_text().splitlines():
            if not line.strip():
                continue
            r = json.loads(line)
            d = r.get("applied", "")
            if not d:
                continue
            for fmt in ["%m/%d/%Y", "%Y-%m-%d"]:
                try:
                    dt = datetime.strptime(d, fmt)
                    if dt.year == year and (latest is None or dt > latest):
                        latest = dt
                    break
                except ValueError:
                    pass
    return latest


def format_etrakit_for_prompt(stats, prev_stats=None):
    """Format etrakit stats as text for inclusion in LLM prompts."""
    year = stats['year']
    city = stats.get('city', 'oceanside')
    latest = _etrakit_latest_date(year, city=city)
    is_partial = False
    coverage_note = ""
    if latest and latest.month < 12:
        is_partial = True
        coverage_note = f" (PARTIAL YEAR — data through {latest.strftime('%B %d, %Y')} only)"

    lines = [f"## Development Pipeline Data ({year}){coverage_note}"]
    if is_partial:
        lines.append(f"WARNING: This is partial-year data. Do NOT compare raw totals to prior full-year totals as YoY changes. If you cite these numbers, clearly state they cover only {latest.month} months.")

    f = stats["filings"]
    lines.append(f"\n### Discretionary Project Filings: {f['total']} total, {f['housing_related']} housing-related")
    if f["by_type"]:
        for t, c in sorted(f["by_type"].items(), key=lambda x: -x[1]):
            lines.append(f"  {t}: {c}")

    if prev_stats and not is_partial:
        pf = prev_stats["filings"]
        if pf["total"] > 0:
            pct = ((f["total"] - pf["total"]) / pf["total"]) * 100
            lines.append(f"  YoY change (total): {pct:+.0f}% ({pf['total']} → {f['total']})")
        if pf["housing_related"] > 0:
            pct = ((f["housing_related"] - pf["housing_related"]) / pf["housing_related"]) * 100
            lines.append(f"  YoY change (housing): {pct:+.0f}% ({pf['housing_related']} → {f['housing_related']})")

    if f["descriptions"]:
        lines.append("\n### Housing Project Filings Detail:")
        for d in f["descriptions"]:
            line = f"  {d['project_no']} ({d['type']}): {d['description']}"
            if d["address"]:
                line += f" @ {d['address']}"
            line += f" [applied {d['applied']}]"
            if d["approved"]:
                line += f" [approved {d['approved']}]"
            lines.append(line)

    p = stats["permits"]
    lines.append(f"\n### Housing Building Permits: {p['total_housing']} total, {p['issued']} issued/finaled, {p['completions']} completed")
    if p["by_type"]:
        for t, c in sorted(p["by_type"].items(), key=lambda x: -x[1]):
            lines.append(f"  {t}: {c}")

    if prev_stats and not is_partial:
        pp = prev_stats["permits"]
        if pp["total_housing"] > 0:
            pct = ((p["total_housing"] - pp["total_housing"]) / pp["total_housing"]) * 100
            lines.append(f"  YoY change (housing permits): {pct:+.0f}% ({pp['total_housing']} → {p['total_housing']})")
        if pp["completions"] > 0:
            pct = ((p["completions"] - pp["completions"]) / pp["completions"]) * 100
            lines.append(f"  YoY change (completions): {pct:+.0f}% ({pp['completions']} → {p['completions']})")

    return "\n".join(lines)


_downtown_cache = None


def _load_downtown_boundary():
    global _downtown_cache
    if _downtown_cache is not None:
        return _downtown_cache
    try:
        from shapely.geometry import shape
        from shapely.ops import unary_union
        gj_path = watchdog_data_dir() / "d-district-zoning.geojson"
        if not gj_path.exists():
            _downtown_cache = False
            return False
        with open(gj_path) as f:
            gj = json.load(f)
        polys = [shape(feat['geometry']) for feat in gj['features']]
        _downtown_cache = unary_union(polys)
        return _downtown_cache
    except ImportError:
        _downtown_cache = False
        return False


_apn_centroid_cache = None


def _load_apn_centroids():
    global _apn_centroid_cache
    if _apn_centroid_cache is not None:
        return _apn_centroid_cache
    try:
        from shapely.geometry import shape
        parcel_path = Path(__file__).resolve().parent.parent.parent / "stoside.data" / "fiscal_analysis" / "parcels_oceanside.geojson"
        if not parcel_path.exists():
            _apn_centroid_cache = {}
            return _apn_centroid_cache
        with open(parcel_path) as f:
            pg = json.load(f)
        centroids = {}
        for feat in pg['features']:
            apn = feat['properties'].get('APN', '')
            geom = shape(feat['geometry'])
            if geom.is_valid and not geom.is_empty and apn:
                c = geom.centroid
                centroids[apn] = (c.x, c.y)
        _apn_centroid_cache = centroids
        return centroids
    except ImportError:
        _apn_centroid_cache = {}
        return {}


def load_downtown_apr_stats(city="oceanside"):
    """Load downtown vs rest-of-city housing filing stats from APR Table A.

    Returns dict keyed by year: {year: {downtown: N, rest: N, total: N, downtown_pct: N}}.
    Uses D-District GeoJSON boundary and parcel APN centroids for geocoding.
    Currently only Oceanside has boundary/parcel data; returns empty for other cities.
    """
    if city.lower() != "oceanside":
        return {}

    import csv
    from collections import defaultdict
    from shapely.geometry import Point

    downtown = _load_downtown_boundary()
    if not downtown:
        return {}
    centroids = _load_apn_centroids()
    if not centroids:
        return {}

    apr_path = watchdog_data_dir() / "hcd-apr-tablea.csv"
    if not apr_path.exists():
        return {}

    counts = defaultdict(lambda: {'downtown': 0, 'rest': 0})

    with open(apr_path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            if city.lower() not in (row.get('JURIS_NAME', '') or '').lower():
                continue
            submit = row.get('APP_SUBMIT_DT', '')
            if not submit or len(submit) < 4:
                continue
            try:
                if '/' in submit:
                    year = int(submit.split('/')[-1])
                else:
                    year = int(submit[:4])
            except ValueError:
                continue

            try:
                units = int(float(row.get('TOT_PROPOSED_UNITS', 0) or 0))
            except (ValueError, TypeError):
                units = 1
            if units < 1:
                units = 1

            apn = (row.get('APN', '') or '').replace('-', '')
            if apn in centroids:
                lon, lat = centroids[apn]
                pt = Point(lon, lat)
                if downtown.contains(pt):
                    counts[year]['downtown'] += units
                else:
                    counts[year]['rest'] += units

    result = {}
    for year in sorted(counts.keys()):
        d, r = counts[year]['downtown'], counts[year]['rest']
        total = d + r
        result[year] = {
            'downtown': d, 'rest': r, 'total': total,
            'downtown_pct': round(d / total * 100) if total else 0,
        }
    return result


def format_downtown_apr_for_prompt(apr_stats, year):
    """Format downtown vs rest-of-city APR stats as text for LLM prompts.

    Shows the target year plus preceding years for trend context.
    """
    if not apr_stats:
        return ""

    lines = ["\n### Downtown vs Rest-of-City Housing Filings (HCD APR Table A)"]
    lines.append("Units filed by application date, geocoded via D-District zoning boundary.")
    lines.append(f"{'Year':<6} {'Downtown':>10} {'Rest of City':>14} {'Total':>8} {'Downtown%':>10}")

    for yr in range(max(2018, year - 5), year + 1):
        if yr not in apr_stats:
            continue
        s = apr_stats[yr]
        lines.append(f"{yr:<6} {s['downtown']:>10} {s['rest']:>14} {s['total']:>8} {s['downtown_pct']:>9}%")

    # Add YoY change for target year
    if year in apr_stats and year - 1 in apr_stats:
        cur = apr_stats[year]
        prev = apr_stats[year - 1]
        if prev['downtown'] > 0:
            pct = ((cur['downtown'] - prev['downtown']) / prev['downtown']) * 100
            lines.append(f"  Downtown YoY: {pct:+.0f}% ({prev['downtown']} → {cur['downtown']})")
        if prev['rest'] > 0:
            pct = ((cur['rest'] - prev['rest']) / prev['rest']) * 100
            lines.append(f"  Rest-of-city YoY: {pct:+.0f}% ({prev['rest']} → {cur['rest']})")

    return "\n".join(lines)


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, default=str))
