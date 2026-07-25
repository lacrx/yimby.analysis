# Policy Knowledge Context

Load policy knowledge into this conversation for informed analysis work.

## When to use

Invoke this skill (`/policy-context`) whenever working on:
- Reviewing or critiquing analysis output (summaries, profiles, grades)
- Writing or editing LLM prompts in analysis scripts
- Answering policy questions about housing, land use, transportation, or municipal governance
- Designing new analysis features that need doctrinal grounding

## How to fetch

Follow the KB repo's own discovery flow. Never clone. One `gh api` call per file.

### Step 1: Discover available articles

```bash
gh api repos/lacrx/policy-knowledge-docs/contents/QUICK-REF.md?ref=main -H "Accept: application/vnd.github.raw+json"
```

QUICK-REF.md has a table: `| Topics | Article | Skills |`. Find rows matching the task's topic tags.

### Step 2: Fetch relevant articles

```bash
gh api repos/lacrx/policy-knowledge-docs/contents/{article_path}?ref=main -H "Accept: application/vnd.github.raw+json"
```

Fetch only articles relevant to the current task. Don't fetch everything — the full KB is 280KB+.

### Step 3: Fetch skills if needed

If QUICK-REF lists a companion skill for the matched article, fetch it too:

```bash
gh api repos/lacrx/policy-knowledge-docs/contents/{skill_path}?ref=main -H "Accept: application/vnd.github.raw+json"
```

### If no match in QUICK-REF

Fetch `TOPIC-INDEX.md` and retry:

```bash
gh api repos/lacrx/policy-knowledge-docs/contents/TOPIC-INDEX.md?ref=main -H "Accept: application/vnd.github.raw+json"
```

If still no match, note "not covered in KB" and continue.

## Topic guide — what to fetch when

All paths below are ready for `gh api` — use as `{article_path}` in Step 2.

### Scoring/grading work
- `articles/housing-advocacy/yimby-policy-framework.md` — scoring doctrine, YIMBY/Strong Towns lens
- `articles/housing-advocacy/complete-housing-position.md` — full advocacy position (supply + tenants + social)

### CA housing law questions
- `articles/ca-housing-law/ca-housing-enforcement.md` — HAA violations, SB 35/330 enforcement
- `articles/ca-housing-law/rezoning-compliance.md` — rezoning requirements, housing element law

### Filing/density analysis
- `articles/land-use-analysis/policy-impact-filing-analysis.md` — density cap impact methodology
- `articles/land-use-analysis/apr-proposal-estimation.md` — HCD APR data, proposal estimation
- `articles/land-use-analysis/fiscal-productivity.md` — revenue-per-acre, fiscal impact

### International comparisons
- `articles/housing-models/` — Vienna, Japan, Finland, Singapore, Montreal, South Korea, Sweden
- `articles/housing-advocacy/europe-vs-us-housing-crisis.md`
- `articles/housing-advocacy/european-rent-regulation-reference.md`

### Safety/infrastructure arguments
- `articles/building-safety/building-safety-by-type.md` — fire/safety by building type
- `articles/active-transport/bike-lane-economic-impact.md`
- `articles/transportation-safety/crash-data-methodology.md`

### PRA/records requests
- `articles/pra-strategy/cpra-compliance.md`

## Current events context

For recent 90-day housing/policy developments, read the watchdog intel feed:
```
/home/thomas/repos/yimby.watchdog/.claude/skills/ca-housing-law/recent-developments.md
```
264KB — skim headings first. Load only when current events context matters.

## Local cache (for analysis scripts)

Analysis scripts use a local cache in `knowledge/<topic>/` loaded by `load_analysis_context()` in `lib/civic_utils.py`. Run `python3 sync_knowledge.py` to update. The cache exists for script performance — Claude Code sessions should use `gh api` directly.
