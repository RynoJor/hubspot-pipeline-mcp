"""Each tool in fixture mode. Expected numbers come from the Revenue Leak Engine's
hygiene/output/summary.json for the same synthetic data (seed 42, as of 2026-10-31)."""

import copy
import csv
import json
from pathlib import Path

import pytest
from mcp.client import Client

from hubspot_pipeline_mcp import checks
from hubspot_pipeline_mcp.server import mcp

TOOLS = {"find_stalled_deals", "find_duplicate_contacts", "pipeline_summary",
         "deals_missing_fields", "rep_conversion", "get_deal"}
ANSWER_KEY = (Path(__file__).resolve().parents[2] / "revenue-leak-engine" / "data" / "sample"
              / "answer_key" / "duplicate_pairs.csv")


# ---------------------------------------------------------------- find_stalled_deals

def test_stalled_proposals_match_leak_engine(crm):
    r = checks.find_stalled_deals(crm, "Proposal", 30)
    assert r["count"] == 40
    assert r["usd_at_risk"] == 454200.0
    assert all(d["days_idle"] >= 30 and d["stage"] == "Proposal" for d in r["deals"])
    amounts = [d["amount_usd"] for d in r["deals"]]
    assert amounts == sorted(amounts, reverse=True)


def test_stalled_limit_truncates_but_totals_cover_all(crm):
    r = checks.find_stalled_deals(crm, "proposal", 30, limit=5)
    assert len(r["deals"]) == 5 and r["truncated"] is True and r["count"] == 40


def test_stalled_threshold_is_monotonic(crm):
    counts = [checks.find_stalled_deals(crm, "Proposal", n)["count"] for n in (0, 30, 90, 3650)]
    assert counts[0] == 59 and counts == sorted(counts, reverse=True) and counts[-1] == 0


def test_stalled_unknown_stage_lists_valid_ones(crm):
    with pytest.raises(checks.CheckError, match="Valid stages: .*Proposal"):
        checks.find_stalled_deals(crm, "Contract signed", 30)


# ---------------------------------------------------------------- find_duplicate_contacts

def test_duplicates_match_leak_engine(crm):
    r = checks.find_duplicate_contacts(crm, limit=200)
    assert r["count"] == 90
    assert r["duplicates_with_open_deals"] == 12
    assert r["usd_at_risk"] == 84850.0
    assert sum(r["by_match_type"].values()) == 90


def test_duplicates_point_back_to_an_older_contact(crm):
    for row in checks.duplicate_pairs(crm):
        assert int(row["original_id"]) < int(row["duplicate_id"])


def test_duplicates_agree_with_answer_key_pairs(crm):
    # Synthetic ground truth from the leak engine. Contact id = 100000 + lead number.
    if not ANSWER_KEY.exists():
        pytest.skip("revenue-leak-engine checkout not next to this repo")
    with open(ANSWER_KEY, encoding="utf-8") as fh:
        want = {(str(100000 + int(r["duplicate_lead_id"][1:])),
                 str(100000 + int(r["original_lead_id"][1:]))) for r in csv.DictReader(fh)}
    got = {(r["duplicate_id"], r["original_id"]) for r in checks.duplicate_pairs(crm)}
    assert got == want


# ---------------------------------------------------------------- pipeline_summary

def test_pipeline_summary_totals(crm):
    p = checks.pipeline_summary(crm)["pipelines"][0]
    t = p["totals"]
    assert (t["open_deals"], t["open_value_usd"]) == (108, 1005250.0)
    assert (t["won_deals"], t["won_value_usd"]) == (109, 1026150.0)
    assert t["lost_deals"] == 383
    assert t["usd_at_risk"] == 454200.0
    assert sum(s["deals"] for s in p["stages"]) == 600


def test_pipeline_summary_stage_rates(crm):
    stages = {s["stage"]: s for s in checks.pipeline_summary(crm)["pipelines"][0]["stages"]}
    assert stages["Qualified"]["reached"] == 600
    reached = [stages[s]["reached"] for s in ("Qualified", "Demo", "Proposal", "Negotiation")]
    assert reached == sorted(reached, reverse=True)
    for s in ("Qualified", "Demo", "Proposal", "Negotiation"):
        assert 0 <= stages[s]["to_next_stage_rate"] <= 1
        assert 0 <= stages[s]["win_rate_from_stage"] <= 1
    assert "reached" not in stages["Closed Won"]


# ---------------------------------------------------------------- deals_missing_fields

def test_missing_fields_defaults(crm):
    r = checks.deals_missing_fields(crm)
    # The synthetic data has no expected close dates, so every open deal lacks closedate.
    assert r["count"] == 108 and r["by_field"]["closedate"] == 108
    assert r["by_field"]["contact.email"] == 0 and r["usd_at_risk"] == 0.0


def test_missing_contact_email_puts_open_amount_at_risk(crm):
    c2 = copy.deepcopy(crm)
    deal = c2.deals["500002"]
    c2.contacts[deal.contact_ids[0]].props["email"] = None
    r = checks.deals_missing_fields(c2, ["contact.email"])
    assert r["count"] == 1 and r["usd_at_risk"] == deal.amount
    assert r["deals"][0]["note"].startswith("no contact email")


def test_missing_fields_rejects_bad_names(crm):
    with pytest.raises(checks.CheckError, match="internal names"):
        checks.deals_missing_fields(crm, ["Deal Amount!"])


# ---------------------------------------------------------------- rep_conversion

def test_rep_conversion_flags_the_weak_rep(crm):
    r = checks.rep_conversion(crm, "Proposal")
    assert r["flagged"] == ["Joshua Walker"]
    assert r["usd_at_risk"] == 198933.28
    jw = next(x for x in r["reps"] if x["owner"] == "Joshua Walker")
    assert (jw["won"], jw["closed"], jw["z"]) == (11, 60, -4.76)
    assert sum(x["closed"] for x in r["reps"]) == 243


def test_rep_conversion_other_stage(crm):
    r = checks.rep_conversion(crm, "Demo")
    assert sum(x["closed"] for x in r["reps"]) > 243


# ---------------------------------------------------------------- get_deal

def test_get_deal_flags_a_stalled_proposal(crm):
    r = checks.get_deal(crm, "500002")
    assert r["deal"]["stage"] == "Proposal" and r["deal"]["days_idle"] == 185
    assert "stalled" in {f["check"] for f in r["flags"]}
    assert r["usd_at_risk"] == 8350.0
    assert r["contacts"][0]["company"] == "Oakstone Homes"


def test_get_deal_accepts_source_id_and_flags_weak_rep(crm):
    weak = next(d for d in crm.deals.values()
                if d.owner == "Joshua Walker" and d.is_closed and "proposal" in d.entered)
    r = checks.get_deal(crm, weak.props["rle_deal_id"])
    assert r["deal"]["id"] == weak.id
    assert any(f["check"] == "weak_rep_conversion" for f in r["flags"])


def test_get_deal_unknown_id(crm):
    with pytest.raises(checks.CheckError, match="No deal"):
        checks.get_deal(crm, "999")


# ---------------------------------------------------------------- over MCP

@pytest.mark.anyio
async def test_server_lists_six_read_only_tools():
    async with Client(mcp) as client:
        tools = (await client.list_tools()).tools
    assert {t.name for t in tools} == TOOLS
    for t in tools:
        assert t.annotations.read_only_hint is True
        assert t.annotations.destructive_hint is False


@pytest.mark.anyio
@pytest.mark.parametrize("name,args,key,value", [
    ("find_stalled_deals", {"stage": "Proposal", "days_idle": 30}, "usd_at_risk", 454200.0),
    ("find_duplicate_contacts", {}, "usd_at_risk", 84850.0),
    ("pipeline_summary", {}, "mode", "fixtures"),
    ("deals_missing_fields", {"fields": ["amount"]}, "count", 0),
    ("rep_conversion", {}, "usd_at_risk", 198933.28),
    ("get_deal", {"deal_id": "500002"}, "usd_at_risk", 8350.0),
])
async def test_tool_over_mcp_returns_compact_json(name, args, key, value):
    async with Client(mcp) as client:
        res = await client.call_tool(name, args)
    assert not res.is_error
    text = res.content[0].text
    assert "\n" not in text
    assert json.loads(text)[key] == value


@pytest.mark.anyio
async def test_bad_stage_is_a_helpful_tool_error():
    async with Client(mcp) as client:
        res = await client.call_tool("find_stalled_deals", {"stage": "nope"})
    assert res.is_error and "Valid stages" in res.content[0].text
