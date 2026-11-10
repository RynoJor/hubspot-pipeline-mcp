"""Push the synthetic fixture data into a HubSpot developer TEST account.

This is the only part of the repo that writes to HubSpot, and it is kept apart from the MCP server
on purpose. It uses its own token (HUBSPOT_SEED_TOKEN, with write scopes), never HUBSPOT_TOKEN.

Safety:
- Dry run by default. Nothing is written unless you pass --apply.
- Refuses to write unless HubSpot reports the portal as a developer test account or a sandbox.
- Writes only the synthetic snapshot (src/hubspot_pipeline_mcp/data/snapshot.json).

Seed token scopes: crm.objects.contacts.write, crm.objects.deals.write,
crm.schemas.contacts.write, crm.schemas.deals.write (plus the matching .read scopes).

Usage, from the repo root:
    python scripts/seed_hubspot.py            # dry run: prints the plan
    python scripts/seed_hubspot.py --apply    # writes to the test account

What it creates:
1. Custom properties: contact company_size, region, lead_source, lead_channel, rle_lead_id;
   deal rle_deal_id, rle_rep_name, rle_last_activity_date.
2. A deal pipeline "Sales Pipeline (Leak Engine demo)" with six stages.
3. 2,000 contacts and 600 deals, each deal linked to its contact.
4. Each deal is created in the first stage and then moved stage by stage to where it ended, so
   HubSpot records real stage history (hs_date_entered_*), which rep_conversion reads.

Known differences from fixture mode (see README, "Limits"):
- HubSpot owners are users, and the API can't create them, so the rep goes into rle_rep_name.
- HubSpot won't store two contacts with the same email. The later copy is created without an email
  and the name match still finds it.
- Last activity can't be backdated, so it goes into rle_last_activity_date.
Run the server with HUBSPOT_PROFILE=seeded to read those properties.
"""

from __future__ import annotations

import argparse
import json
import os
import sys
import time
from pathlib import Path

import httpx
from dotenv import load_dotenv

ROOT = Path(__file__).resolve().parent.parent
SNAPSHOT = ROOT / "src" / "hubspot_pipeline_mcp" / "data" / "snapshot.json"
LOG = ROOT / "seed_log.json"
BASE = "https://api.hubapi.com"
BATCH = 100
PIPELINE_LABEL = "Sales Pipeline (Leak Engine demo)"
TEST_ACCOUNT_TYPES = {"DEVELOPER_TEST", "SANDBOX"}

CONTACT_PROPS = [
    ("company_size", "Company size", "string", "text"),
    ("region", "Region", "string", "text"),
    ("lead_source", "Lead source", "string", "text"),
    ("lead_channel", "Lead channel", "string", "text"),
    ("rle_lead_id", "Leak Engine lead id", "string", "text"),
]
DEAL_PROPS = [
    ("rle_deal_id", "Leak Engine deal id", "string", "text"),
    ("rle_rep_name", "Leak Engine rep", "string", "text"),
    ("rle_last_activity_date", "Leak Engine last activity", "date", "date"),
]
OPEN_FLOW = ["qualified", "demo", "proposal", "negotiation"]


class Api:
    def __init__(self, token: str):
        self.http = httpx.Client(base_url=BASE, timeout=60,
                                 headers={"Authorization": f"Bearer {token}"})

    def call(self, method: str, path: str, body: dict | None = None, ok=(200, 201, 204)) -> httpx.Response:
        delay = 1.0
        for _ in range(8):
            r = self.http.request(method, path, json=body)
            if r.status_code == 429 or r.status_code >= 500:
                time.sleep(float(r.headers.get("Retry-After", delay)))
                delay = min(delay * 2, 30)
                continue
            if r.status_code not in ok:
                raise SystemExit(f"{method} {path} failed: HTTP {r.status_code} {r.text[:300]}")
            return r
        raise SystemExit(f"{method} {path}: gave up after repeated rate limits")


def chunks(xs: list, n: int = BATCH):
    for i in range(0, len(xs), n):
        yield xs[i:i + n]


def day(iso: str | None) -> str | None:
    return iso[:10] if iso else None


def plan(snap: dict) -> dict:
    seen, contact_rows, emailless = set(), [], 0
    for c in snap["contacts"]:
        p = {k: v for k, v in c["properties"].items()
             if v is not None and k not in ("createdate", "hubspot_owner_id")}
        email = (p.get("email") or "").lower()
        if email and email in seen:
            p.pop("email")            # HubSpot rejects a second contact with the same email
            emailless += 1
        elif email:
            seen.add(email)
        contact_rows.append((c["id"], p))
    reps = {o["id"]: f"{o['firstName']} {o['lastName']}" for o in snap["owners"]}
    stage_of = {s["id"]: s for s in snap["pipelines"][0]["stages"]}
    deal_rows = []
    for d in snap["deals"]:
        p = d["properties"]
        path = [s for s in OPEN_FLOW if p.get(f"hs_date_entered_{s}")]
        if p["dealstage"] in ("closedwon", "closedlost"):
            path.append(p["dealstage"])
        deal_rows.append({
            "fixture_id": d["id"],
            "contact": d["associations"]["contacts"]["results"][0]["id"],
            "path": path or [p["dealstage"]],
            "final": {"closedate": p.get("closedate")} if p.get("closedate") else {},
            "props": {k: v for k, v in {
                "dealname": p["dealname"], "amount": p["amount"],
                "rle_deal_id": p["rle_deal_id"], "rle_rep_name": reps.get(p["hubspot_owner_id"]),
                "rle_last_activity_date": day(p.get("notes_last_updated")),
                "closed_lost_reason": p.get("closed_lost_reason"),
            }.items() if v is not None},
        })
    assert all(r["path"][0] in stage_of for r in deal_rows)
    return {"contacts": contact_rows, "deals": deal_rows, "emailless_duplicates": emailless}


def apply(api: Api, work: dict, snap: dict) -> None:
    info = api.call("GET", "/account-info/v3/details").json()
    if info.get("accountType") not in TEST_ACCOUNT_TYPES:
        raise SystemExit(f"Refusing to seed: portal {info.get('portalId')} is "
                         f"{info.get('accountType')}, not a developer test account or sandbox.")
    print(f"Portal {info.get('portalId')} is {info['accountType']}. Seeding.")

    for obj, props in (("contacts", CONTACT_PROPS), ("deals", DEAL_PROPS)):
        for name, label, typ, field in props:
            group = "contactinformation" if obj == "contacts" else "dealinformation"
            api.call("POST", f"/crm/v3/properties/{obj}",
                     {"name": name, "label": label, "type": typ, "fieldType": field, "groupName": group},
                     ok=(200, 201, 409))
    print("Properties ready.")

    existing = api.call("GET", "/crm/v3/pipelines/deals").json()["results"]
    pipe = next((p for p in existing if p["label"] == PIPELINE_LABEL), None)
    if pipe is None:
        stages = snap["pipelines"][0]["stages"]
        pipe = api.call("POST", "/crm/v3/pipelines/deals", {
            "label": PIPELINE_LABEL, "displayOrder": 1,
            "stages": [{"label": s["label"], "displayOrder": s["displayOrder"], "metadata": s["metadata"]}
                       for s in stages]}).json()
    by_label = {s["label"]: s["id"] for s in pipe["stages"]}
    stage_id = {s["id"]: by_label[s["label"]] for s in snap["pipelines"][0]["stages"]}
    print(f"Pipeline {pipe['id']} ready.")

    contact_map = {}
    for batch in chunks(work["contacts"]):
        res = api.call("POST", "/crm/v3/objects/contacts/batch/create",
                       {"inputs": [{"properties": p} for _, p in batch]}).json()["results"]
        by_lead = {r["properties"]["rle_lead_id"]: r["id"] for r in res}
        for fid, p in batch:
            contact_map[fid] = by_lead[p["rle_lead_id"]]
    print(f"{len(contact_map)} contacts created.")

    deal_map = {}
    for batch in chunks(work["deals"]):
        inputs = [{"properties": {**r["props"], "pipeline": pipe["id"], "dealstage": stage_id[r["path"][0]]},
                   "associations": [{"to": {"id": contact_map[r["contact"]]},
                                     "types": [{"associationCategory": "HUBSPOT_DEFINED",
                                                "associationTypeId": 3}]}]}
                  for r in batch]
        res = api.call("POST", "/crm/v3/objects/deals/batch/create", {"inputs": inputs}).json()["results"]
        by_rle = {r["properties"]["rle_deal_id"]: r["id"] for r in res}
        for r in batch:
            deal_map[r["fixture_id"]] = by_rle[r["props"]["rle_deal_id"]]
    print(f"{len(deal_map)} deals created in their first stage.")

    for step in range(1, max(len(r["path"]) for r in work["deals"])):
        movers = [r for r in work["deals"] if len(r["path"]) > step]
        for batch in chunks(movers):
            inputs = []
            for r in batch:
                props = {"dealstage": stage_id[r["path"][step]]}
                if step == len(r["path"]) - 1:
                    props.update(r["final"])
                inputs.append({"id": deal_map[r["fixture_id"]], "properties": props})
            api.call("POST", "/crm/v3/objects/deals/batch/update", {"inputs": inputs})
        print(f"Stage step {step}: moved {len(movers)} deals.")

    with open(LOG, "w", encoding="utf-8") as fh:
        json.dump({"portal": info.get("portalId"), "pipeline": pipe["id"],
                   "contacts": contact_map, "deals": deal_map}, fh, indent=1)
    print(f"Done. Id map in {LOG.name} (git-ignored). Run the server with HUBSPOT_MODE=live "
          f"and HUBSPOT_PROFILE=seeded.")


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__.split("\n")[0])
    ap.add_argument("--apply", action="store_true", help="write to the test account (default: dry run)")
    args = ap.parse_args()

    snap = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    work = plan(snap)
    calls = (2 + len(CONTACT_PROPS) + len(DEAL_PROPS) + 2
             + -(-len(work["contacts"]) // BATCH) + -(-len(work["deals"]) // BATCH) * 6)
    print(f"Plan: {len(work['contacts'])} contacts ({work['emailless_duplicates']} duplicate emails "
          f"stored without email), {len(work['deals'])} deals, pipeline '{PIPELINE_LABEL}', "
          f"about {calls} API calls.")
    if not args.apply:
        print("Dry run. Nothing written. Add --apply to seed a developer test account.")
        return

    load_dotenv(ROOT / ".env")
    token = os.getenv("HUBSPOT_SEED_TOKEN", "").strip()
    if not token:
        sys.exit("Set HUBSPOT_SEED_TOKEN in .env (a test-account token with write scopes).")
    apply(Api(token), work, snap)


if __name__ == "__main__":
    main()
