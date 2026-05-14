#!/usr/bin/env python3
"""Run the cookie-authed Brunata fetcher once with config from a local .env.

This is the local-dev counterpart to ``run_scraper_once.py``, but targets the
new OData flow in ``_brunata_api.py`` instead of the DOM scraper.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import logging
import os
import sys
from pathlib import Path

from _brunata_api import fetch
from _env_utils import read_env_file as _read_env_file


def _build_config(env: dict[str, str]) -> dict:
    email = env.get("BRUNATA_EMAIL", "").strip()
    password = env.get("BRUNATA_PASSWORD", "").strip()
    if not email or not password:
        raise ValueError("Missing BRUNATA_EMAIL or BRUNATA_PASSWORD in env file")

    raw_energy_types = env.get(
        "BRUNATA_ENERGY_TYPES", "Heizung,Kaltwasser,Warmwasser"
    )
    energy_types = [item.strip() for item in raw_energy_types.split(",") if item.strip()]
    if not energy_types:
        raise ValueError("No energy types configured in BRUNATA_ENERGY_TYPES")

    http_timeout = float(env.get("BRUNATA_HTTP_TIMEOUT_S", "60"))

    return {
        "email": email,
        "password": password,
        "energy_types": energy_types,
        "http_timeout": http_timeout,
    }


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Run the Brunata OData fetcher once outside HA"
    )
    parser.add_argument("--env-file", default=".env")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
        stream=sys.stdout,
    )

    env_path = Path(args.env_file)
    if not env_path.is_absolute():
        env_path = Path.cwd() / env_path

    try:
        env_file_values = _read_env_file(env_path)
        merged_env = {**os.environ, **env_file_values}
        config = _build_config(merged_env)
    except (FileNotFoundError, OSError, ValueError) as ex:
        print(f"Configuration error: {ex}", file=sys.stderr)
        sys.exit(2)

    try:
        result = asyncio.run(fetch(config))
    except RuntimeError as ex:
        if "LOGIN_FAILED" in str(ex):
            print("Login failed: check BRUNATA_EMAIL/BRUNATA_PASSWORD", file=sys.stderr)
        else:
            print(f"Fetcher runtime error: {ex}", file=sys.stderr)
        sys.exit(1)
    except (ModuleNotFoundError, TimeoutError, OSError) as ex:
        print(f"Fetcher failed: {ex}", file=sys.stderr)
        sys.exit(1)

    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    main()
