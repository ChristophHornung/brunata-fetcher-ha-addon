#!/usr/bin/env python3
"""Shared helpers for the local-dev / investigation scripts.

These read the gitignored ``.env`` file used by ``run_api_once.py``,
``dump_monthly.py``, etc. Kept in their own module — with no Playwright
or httpx import — so any script can pull them in without dragging a
browser dependency along.
"""

from __future__ import annotations

from pathlib import Path


def read_env_file(env_path: Path) -> dict[str, str]:
    """Read KEY=VALUE pairs from a .env file."""
    if not env_path.is_file():
        raise FileNotFoundError(f"Env file not found: {env_path}")

    values: dict[str, str] = {}
    for line in env_path.read_text(encoding="utf-8").splitlines():
        raw = line.strip()
        if not raw or raw.startswith("#"):
            continue
        if raw.startswith("export "):
            raw = raw[7:].strip()
        if "=" not in raw:
            continue
        key, value = raw.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key:
            values[key] = value
    return values


def env_bool(value: str, default: bool) -> bool:
    """Parse boolean-ish env values."""
    if value == "":
        return default
    return value.strip().lower() in {"1", "true", "yes", "on"}


# Backwards-compatible aliases — older scripts imported these names from
# run_scraper_once.py.
_read_env_file = read_env_file
_env_bool = env_bool
