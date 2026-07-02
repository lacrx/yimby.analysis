"""Jurisdiction-specific configuration loader.

Reads from AWS SSM Parameter Store in production, falls back to
config.local.yaml for local development. All values cached for
process lifetime after first load.

SSM prefix set via SSM_PREFIX env var (default: /civic-monitor/).
Shared module — identical pattern to yimby.watchdog/config.py.
"""

import json
import os
from functools import lru_cache
from pathlib import Path

import yaml

_CONFIG_DIR = Path(__file__).resolve().parent


@lru_cache(maxsize=1)
def _load():
    local_file = _CONFIG_DIR / "config.local.yaml"
    if local_file.exists():
        with open(local_file) as f:
            raw = yaml.safe_load(f) or {}
        return _flatten(raw)

    prefix = os.environ.get("SSM_PREFIX", "/civic-monitor/")
    if not prefix.endswith("/"):
        prefix += "/"

    import boto3
    ssm = boto3.client("ssm", region_name=os.environ.get("AWS_REGION", "us-west-2"))

    params = {}
    paginator = ssm.get_paginator("get_parameters_by_path")
    for page in paginator.paginate(Path=prefix, Recursive=True, WithDecryption=True):
        for p in page["Parameters"]:
            key = p["Name"][len(prefix):]
            value = p["Value"]
            try:
                value = json.loads(value)
            except (json.JSONDecodeError, TypeError):
                pass
            params[key] = value

    return params


def _flatten(d, prefix="", depth=0):
    """Flatten nested dict to slash-separated keys, max 2 levels deep.

    Matches SSM layout: identity/primary_city, feeds/rss_feeds, etc.
    Values below depth 2 stay as-is (dicts, lists).
    """
    out = {}
    for k, v in d.items():
        key = f"{prefix}{k}" if prefix else k
        if isinstance(v, dict) and depth < 1:
            out.update(_flatten(v, f"{key}/", depth + 1))
        else:
            out[key] = v
    return out


def get(key, default=None):
    """Get config value by slash-separated key (e.g., 'identity/primary_city')."""
    return _load().get(key, default)


def get_all():
    """Return full config dict."""
    return dict(_load())
