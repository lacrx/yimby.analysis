# Agent Instructions

This repo generates advocacy intelligence from structured municipal meeting data. It produces executive summaries, council member housing advocacy profiles, and leadership profiles across agencies. The analytical lens (default: YIMBY/Strong Towns) and jurisdiction are configurable via `config.local.yaml` or SSM Parameter Store.

## Architecture

**Config:** Jurisdiction-specific settings (city name, council roster, advocacy lens, relevance keywords) load from `config.py` — SSM Parameter Store in production, `config.local.yaml` for local dev. Same pattern as `yimby.watchdog`.

**Key files:**
- `config.py` — SSM/YAML config loader (identical to watchdog's)
- `config.local.yaml` — local dev config with figures, keywords, lens (gitignored)
- `config.yaml` — path to watchdog data directory
- `analysis/executive_summaries.py` — yearly governance summaries
- `analysis/leadership_profiles.py` — per-official profiles with grading
- `analysis/council_member_summaries.py` — council-specific profiles
- `analysis/score_records.py` — deterministic scoring of meeting records

## Related Projects
- `yimby.watchdog`: upstream pipeline — scrapes agendas, transcribes video, extracts structured JSONL. This repo reads from watchdog's `data/` directory via `config.yaml → watchdog_data`
- `stoside.data`: municipal fiscal intelligence, budget/CIP/vote history

## Knowledge Base Repos (upstream, read-only)

Two external repos own all reusable knowledge — articles for context and Claude Code skills for session-level tooling. Fetch articles from them as needed; skills are handled by the Claude Code system, not by our analysis scripts.

- **`lacrx/policy-knowledge-docs`** — policy articles and skills. Articles: CA housing law enforcement (`articles/ca-housing-law/ca-housing-enforcement.md`), PRA strategy, fiscal productivity, crash data methodology. Skills: `draft-pra-request`, `fetch-policy-bundle`, `evaluate-crash-study`.
- **`lacrx/agent-knowledge-docs`** — engineering articles and skills. Articles: AWS deployment, Fargate, SDK patterns, testing. Skills: `scaffold-fastapi`, `provision-fargate-task`, etc.

Note: the `load_skills()` function in analysis scripts reads local `.claude/skills/ca-housing-law/` files and injects them as system context in LLM API calls. This is separate from the Claude Code skill system.

## Analytical Framework

All analysis in this repo follows a single doctrine: **ACTIONS OVER WORDS**.

### Scoring System
- **Strong Pro-Housing (+2)**: remove/raise density caps, eliminate parking minimums, legalize missing middle by-right, approve housing projects (market-rate OR affordable), support state preemption (SB 9/10/35)
- **Moderate Pro-Housing (+1)**: inclusionary zoning requirements, tenant protections (relocation assistance, just-cause eviction, rent stabilization), streamline discretionary approvals, transit-oriented density
- **Anti-Housing (-1)**: maintain parking requirements 1+ space/unit, cite "community character" against density, oppose state mandates under "local control"
- **Strong Anti-Housing (-2)**: add/maintain density caps, vote AGAINST housing projects, weaponize inclusionary rates as poison pill against specific compliant projects, weaponize CEQA against housing

### Grade Scale (by net score)
A: +10 or higher | B: +5 to +9 | C: 0 to +4 | D: -1 to -9 | F: -10 or lower

### Critical Analytical Distinctions
- Tenant protections are genuinely pro-housing. Score them +1 individually. Never penalize tenant protection votes.
- Supply skepticism is a PATTERN-level assessment: a member who ONLY does tenant protections and NEVER approves projects is noted as incomplete, but individual tenant votes stay positive.
- Inclusionary weaponization: voting to raise citywide inclusionary rates = pro-housing (+1). Demanding a specific compliant project exceed existing requirements as grounds for denial = anti-housing (-2). The test: did they cite inclusionary shortfall to DENY a project that met existing rules?
- Both market-rate and affordable housing add supply. Blocking either is anti-housing.

## Anti-Hallucination Rules

These are hard constraints, not guidelines:

1. **NEVER invent names.** Only name individuals who appear BY NAME in source data. If a vote record lacks individual names, say "individual votes not documented." Hallucinating names is worse than leaving a gap.
2. **NEVER invent votes, meetings, or actions.** Every claim must trace to source data. If the record is thin, say so.
3. **NEVER fill gaps with plausible guesses.** A thin record should produce a thin profile with a caveat, not a complete-sounding profile with fabricated detail.
4. When generating profiles or summaries, include this constraint in every prompt to the LLM.

## Data Flow

```
watchdog/data/structured/meetings-combined.jsonl  →  leadership_profiles.py
watchdog/data/structured/monthly-digests.jsonl     →  executive_summaries.py
watchdog/data/structured/all-records.jsonl         →  council_member_summaries.py
watchdog/data/intel/intel-*.json                   →  update_skill_intel.py
```

**Etrakit filing data** (distinct from meeting records):
```
watchdog/data/oceanside/permits/etrakit-projects-{year}.jsonl  — discretionary project applications (RD, DB, CUP, etc.)
watchdog/data/oceanside/permits/etrakit-permits-{year}.jsonl   — building permits
```
Each row has `project_no`, `applied` (date), `address`, `description`, `apn`, `status`. These are the authoritative source for when discretionary applications were filed — `all-records.jsonl` only captures projects after they reach a hearing body. Use etrakit files for filing volume analysis, policy impact measurement, and pipeline tracking. Etrakit does NOT capture ministerial approvals (ADUs, SB 9 lot splits, by-right small projects) — those appear only in building permits or HCD APR data.

**HCD APR data**:
```
watchdog/data/hcd-apr-tablea.csv                               — statewide APR Table A (384K rows)
watchdog/data/reference/hcd-apr-oceanside.json                 — Oceanside-specific APR reference
```
APR Table A has `APP_SUBMIT_DT`, unit counts by income category, `UNIT_CAT` (ADU/SFD/2-4/5+), and application status. Covers 2018-2025 (2026 APR due April 2027). APR captures ALL unit types including ministerial (ADUs, by-right), so totals will exceed etrakit discretionary counts. Current-year ministerial counts are unknown until APR is filed (April of following year).

**Geographic boundaries**:
```
watchdog/data/d-district-zoning.geojson                        — official D-District (Downtown) zoning boundary
```
GeoJSON with 36 polygons for subdistricts D-1 through D-14. Use this for downtown geographic filtering — do NOT guess from street names. Cross-reference etrakit APNs or APR lat/lon coordinates against this boundary using shapely point-in-polygon.

### Data source hierarchy for filing questions
1. **"When was X filed?"** → etrakit `applied` date or APR `APP_SUBMIT_DT`
2. **"How many discretionary filings?"** → etrakit-projects-{year}.jsonl (RD/DB prefixes)
3. **"How many total units filed?"** → HCD APR Table A (includes ministerial). Current year unavailable until April next year.
4. **"Is X in downtown?"** → d-district-zoning.geojson point-in-polygon, not street name matching
5. **"What happened at a hearing?"** → all-records.jsonl (meeting records). Never use this to determine filing dates.

Scripts read from watchdog via `config.yaml → watchdog_data`. They do NOT write back to watchdog.

## LLM Call Modes

All analysis scripts support two modes:
- `--mode local`: uses `claude -p` (subscription, $0 marginal cost). Default for most scripts.
- `--mode api`: uses Claude API via `anthropic` SDK. Costs money. Use for batch runs or when `claude -p` is unavailable.

Both modes inject local `.claude/skills/ca-housing-law/` files as LLM system context when available.

## Fetching KB Articles

Both KBs use the same discovery flow. Fetch articles for context — not skills, which are loaded by Claude Code automatically.

```
gh api repos/lacrx/{repo}/contents/{path}?ref=main -H "Accept: application/vnd.github.raw+json"
```

1. Fetch `QUICK-REF.md` first. Find the row matching the task's topic.
2. Extract the article path from the matched row and fetch it.
3. If no match, fetch `TOPIC-INDEX.md` and retry.
4. If still no match, continue without KB.

### When to Fetch Which

This repo is entirely policy-adjacent. Fetch **policy KB** (`policy-knowledge-docs`) **whenever relevant** — not just during planning, but during analysis design, prompt engineering, grading framework changes, or any work touching housing law, land use, transit, municipal governance, or advocacy strategy.

Fetch **engineering KB** (`agent-knowledge-docs`) for infrastructure work only. Skip both for pure code cleanup.

## Working With Analysis Scripts

When modifying analysis scripts:
- Preserve the anti-hallucination constraints in every LLM prompt
- Preserve the ACTIONS OVER WORDS doctrine in grading prompts
- Test prompt changes against real data — the structured JSONL has real vote records with real names
- Jurisdiction-specific values (city names, officials, keywords) come from `config.py` — never hardcode them
- Known figures and agency group labels are in `config.local.yaml` under `figures/`
- Relevance keywords are in `config.local.yaml` under `analysis/relevance_keywords`
- Change detection via content hashing (`leadership_profiles.py`) avoids re-running unchanged profiles — respect this optimization
