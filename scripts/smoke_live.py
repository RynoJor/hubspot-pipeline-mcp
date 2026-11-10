"""Live smoke test: run pipeline_summary against the HubSpot portal in .env.

Read-only (GET requests only). Prints the result and the number of API calls. Never prints the token.

    python scripts/smoke_live.py
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import replace
from pathlib import Path

from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT / "src"))

from hubspot_pipeline_mcp import checks  # noqa: E402
from hubspot_pipeline_mcp.client import HubSpotClient, HubSpotError  # noqa: E402
from hubspot_pipeline_mcp.crm import Settings, fetch_live  # noqa: E402


def main() -> None:
    load_dotenv(ROOT / ".env")
    settings = replace(Settings.from_env(), mode="live")
    if not settings.token:
        sys.exit("No HUBSPOT_TOKEN in .env. Live smoke test skipped.")
    client = HubSpotClient(settings.token)
    try:
        crm = fetch_live(settings, client)
        result = checks.pipeline_summary(crm)
    except HubSpotError as exc:
        sys.exit(f"Live smoke test failed: {exc}")
    finally:
        client.close()
    print(f"Live smoke test OK: {len(crm.deals)} deals, {len(crm.contacts)} contacts, "
          f"{client.requests_made} GET requests, profile="
          f"{os.getenv('HUBSPOT_PROFILE') or 'default'}")
    print(json.dumps(result, indent=2))


if __name__ == "__main__":
    main()
