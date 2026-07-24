#!/usr/bin/env python3
"""Sync policy knowledge articles from lacrx/policy-knowledge-docs.

Fetches articles via `gh api` and saves them to knowledge/.
Idempotent — only writes files that changed.
"""

import hashlib
import subprocess
import sys
from pathlib import Path

REPO = "lacrx/policy-knowledge-docs"
DEST = Path(__file__).parent / "knowledge"

# source path -> local filename
ARTICLES = {
    "articles/ca-housing-law/ca-housing-enforcement.md": "ca-housing-enforcement.md",
    "articles/ca-housing-law/rezoning-compliance.md": "rezoning-compliance.md",
    "articles/housing-advocacy/yimby-policy-framework.md": "yimby-policy-framework.md",
    "articles/land-use-analysis/policy-impact-filing-analysis.md": "policy-impact-filing-analysis.md",
}


def fetch_article(path):
    """Fetch a single article from GitHub. Returns content or None."""
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


def file_hash(path):
    """SHA-256 of a file, or None if it doesn't exist."""
    if not path.exists():
        return None
    return hashlib.sha256(path.read_bytes()).hexdigest()


def main():
    DEST.mkdir(exist_ok=True)

    updated = []
    current = []
    failed = []

    for source_path, local_name in ARTICLES.items():
        dest_path = DEST / local_name
        print(f"Syncing {local_name}...")

        content = fetch_article(source_path)
        if content is None:
            failed.append(local_name)
            continue

        new_hash = hashlib.sha256(content.encode()).hexdigest()
        old_hash = file_hash(dest_path)

        if new_hash == old_hash:
            current.append(local_name)
            print(f"  unchanged")
        else:
            dest_path.write_text(content)
            action = "updated" if old_hash else "created"
            updated.append(local_name)
            print(f"  {action}")

    # Summary
    print()
    if updated:
        print(f"Updated: {len(updated)} ({', '.join(updated)})")
    if current:
        print(f"Already current: {len(current)}")
    if failed:
        print(f"Failed: {len(failed)} ({', '.join(failed)})")
        sys.exit(1)

    if not updated and current:
        print("Everything up to date.")


if __name__ == "__main__":
    main()
