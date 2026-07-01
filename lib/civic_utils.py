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
            print(f"  claude -p error: {result.stderr[:200]}")
        return None
    except FileNotFoundError:
        print("  claude CLI not found.")
        return None
    except subprocess.TimeoutExpired:
        print(f"  claude -p timed out ({timeout}s)")
        return None


def load_json(path):
    try:
        return json.loads(Path(path).read_text())
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_json(path, data):
    Path(path).write_text(json.dumps(data, indent=2, default=str))
