#!/usr/bin/env python3
"""Sync policy knowledge articles from lacrx/policy-knowledge-docs.

Discovers articles via QUICK-REF.md (the KB's own discovery mechanism),
fetches via `gh api`, and saves to knowledge/<topic>/.
Idempotent — only writes files that changed.
"""

import hashlib
import re
import subprocess
import sys
from pathlib import Path

REPO = "lacrx/policy-knowledge-docs"
DEST = Path(__file__).parent / "knowledge"


def gh_fetch(path):
    """Fetch a file from the KB repo via gh api. Returns content or None."""
    try:
        result = subprocess.run(
            [
                "gh", "api",
                f"repos/{REPO}/contents/{path}",
                "-H", "Accept: application/vnd.github.raw+json",
            ],
            capture_output=True, text=True, timeout=30,
        )
        if result.returncode == 0 and result.stdout.strip():
            return result.stdout
        print(f"  FAILED: {path}")
        if result.stderr:
            print(f"    {result.stderr.strip()[:200]}")
        return None
    except FileNotFoundError:
        print("ERROR: gh CLI not found. Install GitHub CLI: https://cli.github.com/")
        sys.exit(1)
    except subprocess.TimeoutExpired:
        print(f"  TIMEOUT: {path}")
        return None


def discover_articles(quick_ref_content):
    """Parse QUICK-REF.md to extract article paths.

    Returns list of article paths like 'articles/ca-housing-law/enforcement.md'.
    """
    articles = []
    for line in quick_ref_content.splitlines():
        match = re.search(r'\[.*?\]\((articles/[^)]+\.md)\)', line)
        if match:
            articles.append(match.group(1))
    return articles


def file_hash(path):
    """SHA-256 of a file, or None if it doesn't exist."""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    DEST.mkdir(exist_ok=True)

    # Step 1: discover articles from QUICK-REF.md
    print("Fetching QUICK-REF.md...")
    quick_ref = gh_fetch("QUICK-REF.md")
    if quick_ref is None:
        print("ERROR: Could not fetch QUICK-REF.md — cannot discover articles")
        sys.exit(1)

    article_paths = discover_articles(quick_ref)
    if not article_paths:
        print("ERROR: No articles found in QUICK-REF.md")
        sys.exit(1)

    print(f"Discovered {len(article_paths)} articles\n")

    # Step 2: fetch each article
    updated = []
    current = []
    failed = []

    for source_path in article_paths:
        # articles/ca-housing-law/foo.md -> ca-housing-law/foo.md
        local_path = source_path.removeprefix("articles/")
        dest_path = DEST / local_path
        dest_path.parent.mkdir(parents=True, exist_ok=True)

        print(f"Syncing {local_path}...")

        content = gh_fetch(source_path)
        if content is None:
            failed.append(local_path)
            continue

        new_hash = hashlib.sha256(content.encode()).hexdigest()
        old_hash = file_hash(dest_path)

        if new_hash == old_hash:
            current.append(local_path)
            print(f"  unchanged")
        else:
            dest_path.write_text(content)
            action = "updated" if old_hash else "created"
            updated.append(local_path)
            print(f"  {action}")

    # Step 3: clean up articles no longer in QUICK-REF
    local_paths = {source.removeprefix("articles/") for source in article_paths}
    for md in DEST.rglob("*.md"):
        rel = str(md.relative_to(DEST))
        if rel not in local_paths:
            print(f"Removing stale article: {rel}")
            md.unlink()

    # Remove empty topic directories
    for d in sorted(DEST.rglob("*"), reverse=True):
        if d.is_dir() and not any(d.iterdir()):
            d.rmdir()

    # Summary
    topics = set(p.split("/")[0] for p in local_paths)
    print(f"\nTotal: {len(article_paths)} articles across {len(topics)} topics")
    if updated:
        print(f"Updated: {len(updated)} ({', '.join(updated)})")
    if current:
        print(f"Already current: {len(current)}")
    if failed:
        print(f"Failed: {len(failed)} ({', '.join(failed)})")
        sys.exit(1)
    if not updated and not failed:
        print("Everything up to date.")


if __name__ == "__main__":
    main()
