"""hubspot_pipeline_mcp: ask Claude which deals are stuck. Read-only MCP server over HubSpot CRM.

Modes (HUBSPOT_MODE in .env):
    fixtures  default. Serves a synthetic HubSpot-shaped snapshot. No account needed.
    live      Reads your HubSpot portal with a private-app token (HUBSPOT_TOKEN), GET requests only.

Every tool is read-only: no tool creates, updates or deletes anything, and the HubSpot client has
no write method. Tools return compact JSON with a USD value at risk where it makes sense.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Annotated

from dotenv import load_dotenv
from mcp.server.mcpserver import MCPServer
from mcp.server.mcpserver.exceptions import ToolError
from mcp.types import ToolAnnotations
from pydantic import Field

from . import checks
from .client import HubSpotError
from .crm import Settings, Source

_REPO_ENV = Path(__file__).resolve().parents[2] / ".env"
load_dotenv(_REPO_ENV if _REPO_ENV.exists() else None)

SETTINGS = Settings.from_env()
SOURCE = Source(SETTINGS)

READ_ONLY = ToolAnnotations(readOnlyHint=True, destructiveHint=False, idempotentHint=True,
                            openWorldHint=SETTINGS.mode == "live")

mcp = MCPServer(
    "hubspot_pipeline_mcp",
    instructions=(
        "Read-only tools over a HubSpot CRM pipeline. Start with pipeline_summary for the big "
        "picture, then drill in with find_stalled_deals, rep_conversion, find_duplicate_contacts "
        "or deals_missing_fields, and get_deal for one deal. Amounts are USD. usd_basis says "
        "whether a figure is measured (deal amounts) or estimated (win-rate gaps). "
        f"Current mode: {SETTINGS.mode}."
    ),
)


def _run(fn, *args, **kwargs) -> str:
    try:
        crm = SOURCE.get()
        result = fn(crm, *args, **kwargs)
    except (checks.CheckError, HubSpotError) as exc:
        raise ToolError(str(exc)) from None
    result.setdefault("mode", SETTINGS.mode)
    return json.dumps(result, separators=(",", ":"), default=str)


Limit = Annotated[int, Field(ge=1, le=200, description="Max rows to list (totals always cover every match).")]


@mcp.tool(title="Find stalled deals", annotations=READ_ONLY)
def find_stalled_deals(
    stage: Annotated[str, Field(description="Deal stage label or id, e.g. 'Proposal'. Case-insensitive.")] = "Proposal",
    days_idle: Annotated[int, Field(ge=0, le=3650, description="Minimum days since last activity.")] = 30,
    limit: Limit = 25,
) -> str:
    """Open deals sitting in a stage with no activity for `days_idle`+ days.

    Returns count, usd_at_risk (sum of their amounts) and the largest deals first:
    id, name, owner, amount_usd, last_activity, days_idle.
    """
    return _run(checks.find_stalled_deals, stage=stage, days_idle_min=days_idle, limit=limit)


@mcp.tool(title="Find duplicate contacts", annotations=READ_ONLY)
def find_duplicate_contacts(limit: Limit = 25) -> str:
    """Duplicate contacts: the same mailbox (case and dots ignored), or the same company with a
    matching name ('Steve' ~ 'Steven', 'N. Galloway' ~ 'Nina Galloway').

    usd_at_risk is the open deal value sitting on the duplicates (pipeline counted twice).
    Rows list duplicate_id, original_id, match_type, reason and open_deal_ids.
    """
    return _run(checks.find_duplicate_contacts, limit=limit)


@mcp.tool(title="Pipeline summary", annotations=READ_ONLY)
def pipeline_summary() -> str:
    """By stage: deal count, value, how many reached it, the rate to the next stage, the win rate
    from that stage, and stalled deals (idle 30+ days) as usd_at_risk. Plus pipeline totals:
    open value, won value, win rate.
    """
    return _run(checks.pipeline_summary)


@mcp.tool(title="Deals missing fields", annotations=READ_ONLY)
def deals_missing_fields(
    fields: Annotated[list[str] | None, Field(
        description="HubSpot internal property names to require. Prefix with 'contact.' to check the "
                    "deal's associated contact. Default: amount, closedate, hubspot_owner_id, "
                    "contact.email, contact.industry, contact.company_size, contact.region.",
        max_length=30)] = None,
    include_closed: Annotated[bool, Field(description="Also check closed deals.")] = False,
    limit: Limit = 25,
) -> str:
    """Deals with empty required fields, and which fields. An open deal whose contact has no email
    cannot be worked, so its amount is at risk; other gaps are routing/reporting costs, valued at $0.
    """
    return _run(checks.deals_missing_fields, fields=fields, include_closed=include_closed, limit=limit)


@mcp.tool(title="Rep conversion", annotations=READ_ONLY)
def rep_conversion(
    from_stage: Annotated[str, Field(description="Stage to measure from, e.g. 'Proposal' or 'Demo'.")] = "Proposal",
) -> str:
    """Stage-to-won rate by owner: of each owner's closed deals that entered `from_stage`, the share
    won, against the rest of the team. Flags owners at z <= -3 and estimates usd_at_risk as
    closed deals x (team rate - rep rate) x average won amount.
    """
    return _run(checks.rep_conversion, from_stage=from_stage)


@mcp.tool(title="Get deal", annotations=READ_ONLY)
def get_deal(
    deal_id: Annotated[str, Field(min_length=1, max_length=40, description="HubSpot deal id, e.g. '500002'.")],
) -> str:
    """One deal: stage, owner, amount, days idle, stage history, contacts, and every check it
    trips (stalled, duplicate contact, missing fields, weak rep), with usd_at_risk.
    """
    return _run(checks.get_deal, deal_id=deal_id)


def main() -> None:
    mcp.run()


if __name__ == "__main__":
    main()
