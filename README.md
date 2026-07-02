# YIMBY Analysis

Downstream analysis layer for civic monitoring. Reads structured meeting data from `yimby.watchdog` and produces executive summaries, council member profiles, and leadership grades.

Analytical lens (default: YIMBY/Strong Towns) and jurisdiction are configurable via `config.local.yaml` or AWS SSM Parameter Store.

## What It Produces

- **Executive summaries** — yearly governance narratives from monthly digests
- **Council member summaries** — per-official housing advocacy profiles for primary city council + planning commission
- **Leadership profiles** — cross-agency official profiles with letter grades (A through F)
- **Record scoring** — deterministic scoring of individual meeting records against the advocacy framework

## Setup

```bash
# 1. Configure watchdog data path
# Edit config.yaml → watchdog_data to point at your yimby.watchdog/data/ directory

# 2. Configure jurisdiction
cp config.local.yaml.example config.local.yaml
# Edit: city name, council roster, advocacy lens, relevance keywords

# 3. Run
python analysis/executive_summaries.py --mode local
python analysis/council_member_summaries.py --mode local
python analysis/leadership_profiles.py --mode local
```

All scripts support `--mode local` (claude -p, subscription) or `--mode api` (Anthropic SDK, costs money).

## Configuration

Same pattern as `yimby.watchdog` — `config.py` loads from SSM Parameter Store (production) or `config.local.yaml` (local dev).

Key config values: `identity/primary_city`, `advocacy/lens`, `advocacy/advocate_role`, `figures/known_figures`, `analysis/relevance_keywords`.

## Data Flow

```
watchdog/data/structured/meetings-combined.jsonl  →  leadership_profiles.py
watchdog/data/structured/monthly-digests.jsonl     →  executive_summaries.py
watchdog/data/structured/all-records.jsonl         →  council_member_summaries.py
watchdog/data/intel/intel-*.json                   →  update_skill_intel.py
```

Scripts read from watchdog via `config.yaml → watchdog_data`. They do not write back.

## Grading Framework

**ACTIONS OVER WORDS.** Grades based on votes and motions, not public statements.

| Grade | Net Score |
|-------|-----------|
| A | +10 or higher |
| B | +5 to +9 |
| C | 0 to +4 |
| D | -1 to -9 |
| F | -10 or lower |

See [CLAUDE.md](CLAUDE.md) for full scoring rules and anti-hallucination constraints.

## Related Projects

- `yimby.watchdog` — upstream ETL pipeline (scraping, extraction, rollup)
- `stoside.data` — municipal fiscal intelligence
