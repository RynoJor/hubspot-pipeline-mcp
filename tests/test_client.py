"""The live HubSpot client, tested against a fake HubSpot (httpx.MockTransport). No network."""

import json

import httpx
import pytest

from hubspot_pipeline_mcp import checks
from hubspot_pipeline_mcp.client import HubSpotClient, HubSpotError
from hubspot_pipeline_mcp.crm import FIXTURE_PATH, Settings, fetch_live

TOKEN = "pat-test-not-a-real-token"
SNAP = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))


def fake_hubspot(log, fail_first=None):
    """Serves the fixture snapshot the way HubSpot's v3 API pages it."""
    fails = {k: list(v) for k, v in (fail_first or {}).items()}

    def handler(req: httpx.Request) -> httpx.Response:
        log.append(req)
        assert req.method == "GET"
        assert req.headers["Authorization"] == f"Bearer {TOKEN}"
        path = req.url.path
        if fails.get(path):
            code = fails[path].pop(0)
            return httpx.Response(code, headers={"Retry-After": "0"}, json={"message": "slow down"})
        if path == "/crm/v3/pipelines/deals":
            return httpx.Response(200, json={"results": SNAP["pipelines"]})
        items = {"/crm/v3/owners": SNAP["owners"], "/crm/v3/objects/contacts": SNAP["contacts"],
                 "/crm/v3/objects/deals": SNAP["deals"]}[path]
        start = int(req.url.params.get("after", 0))
        limit = int(req.url.params["limit"])
        body = {"results": items[start:start + limit]}
        if start + limit < len(items):
            body["paging"] = {"next": {"after": str(start + limit)}}
        return httpx.Response(200, json=body)

    return handler


def client_for(handler, sleeps=None):
    return HubSpotClient(TOKEN, transport=httpx.MockTransport(handler),
                         sleep=(sleeps.append if sleeps is not None else lambda s: None))


def test_live_path_paginates_and_matches_fixture_numbers(crm):
    log = []
    s = Settings(mode="live", token=TOKEN, as_of=crm.as_of)
    live = fetch_live(s, client_for(fake_hubspot(log)))
    assert len(live.contacts) == 2000 and len(live.deals) == 600
    assert sum(r.url.path == "/crm/v3/objects/contacts" for r in log) == 20   # 2,000 / 100
    assert sum(r.url.path == "/crm/v3/objects/deals" for r in log) == 6
    deal_req = next(r for r in log if r.url.path == "/crm/v3/objects/deals")
    assert deal_req.url.params["associations"] == "contacts"
    assert "hs_date_entered_proposal" in deal_req.url.params["properties"]
    assert checks.find_stalled_deals(live)["usd_at_risk"] == 454200.0
    assert checks.rep_conversion(live)["usd_at_risk"] == 198933.28
    assert checks.find_duplicate_contacts(live)["usd_at_risk"] == 84850.0


def test_rate_limit_waits_for_retry_after_then_succeeds():
    log, sleeps = [], []
    c = client_for(fake_hubspot(log, {"/crm/v3/pipelines/deals": [429, 429, 502]}), sleeps)
    assert c.deal_pipelines()[0]["label"] == "Sales Pipeline"
    assert len(log) == 4 and sleeps == [0.0, 0.0, 0.0]


def test_rate_limit_without_retry_after_backs_off_exponentially():
    sleeps, calls = [], {"n": 0}

    def handler(req):
        calls["n"] += 1
        if calls["n"] <= 3:
            return httpx.Response(429)
        return httpx.Response(200, json={"results": []})

    client_for(handler, sleeps).deal_pipelines()
    assert sleeps == [1.0, 2.0, 4.0]


def test_rate_limit_gives_up_with_a_clear_message():
    c = client_for(fake_hubspot([], {"/crm/v3/pipelines/deals": [429] * 10}))
    with pytest.raises(HubSpotError, match="still failing after 5 retries"):
        c.deal_pipelines()


@pytest.mark.parametrize("code,msg", [(401, "rejected the token"), (403, "missing a scope")])
def test_auth_errors_explain_the_fix_and_never_echo_the_token(code, msg):
    c = client_for(lambda req: httpx.Response(code, json={"message": "no"}))
    with pytest.raises(HubSpotError, match=msg) as exc:
        c.owners()
    assert TOKEN not in str(exc.value)


def test_client_has_no_write_methods():
    public = {m for m in dir(HubSpotClient) if not m.startswith("_")}
    assert public == {"close", "deal_pipelines", "owners", "contacts", "deals"}


def test_empty_token_is_refused():
    with pytest.raises(HubSpotError, match="HUBSPOT_TOKEN is empty"):
        HubSpotClient("")
