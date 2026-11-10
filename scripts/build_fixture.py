"""Build the fixture snapshot from the Revenue Leak Engine's synthetic sample data.

The snapshot is shaped like HubSpot CRM v3 API responses (pipelines, owners, contacts, deals with
contact associations), so fixture mode runs the same parsing and check code as live mode.

Every row is synthetic: it comes from revenue-leak-engine/data/generate.py (seed 42, snapshot
2026-10-31). No real CRM data is used. The answer key folder is never read.

Run from the repo root:
    python scripts/build_fixture.py [path/to/revenue-leak-engine/data/sample]

Writes src/hubspot_pipeline_mcp/data/snapshot.json
"""

from __future__ import annotations

import csv
import json
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
DEFAULT_SAMPLE = ROOT.parent / "revenue-leak-engine" / "data" / "sample"
OUT = ROOT / "src" / "hubspot_pipeline_mcp" / "data" / "snapshot.json"

AS_OF = "2026-10-31"
PIPELINE_ID = "rle-sales"
# (source stage, HubSpot stage id, label, isClosed, probability)
STAGES = [
    ("qualified", "qualified", "Qualified", False, "0.2"),
    ("demo", "demo", "Demo", False, "0.4"),
    ("proposal", "proposal", "Proposal", False, "0.6"),
    ("negotiation", "negotiation", "Negotiation", False, "0.8"),
    ("won", "closedwon", "Closed Won", True, "1.0"),
    ("lost", "closedlost", "Closed Lost", True, "0.0"),
]
STAGE_ID = {src: sid for src, sid, *_ in STAGES}
DATE_COLS = {"qualified": "qualified_date", "demo": "demo_date",
             "proposal": "proposal_date", "negotiation": "negotiation_date"}


def read(path: Path) -> list[dict]:
    with open(path, encoding="utf-8", newline="") as fh:
        return [{k: (v if v != "" else None) for k, v in row.items()} for row in csv.DictReader(fh)]


def iso_day(d: str | None) -> str | None:
    """'2026-07-21' -> '2026-07-21T00:00:00Z' (HubSpot returns datetimes as ISO strings)."""
    return f"{d[:10]}T00:00:00Z" if d else None


def iso_ts(ts: str | None) -> str | None:
    """'2026-07-19 14:43:00' -> '2026-07-19T14:43:00Z'."""
    return ts.replace(" ", "T") + "Z" if ts else None


def split_name(full: str | None) -> tuple[str | None, str | None]:
    if not full:
        return None, None
    parts = full.split()
    return parts[0], " ".join(parts[1:]) or None


def build(sample: Path) -> dict:
    reps = read(sample / "reps.csv")
    leads = read(sample / "leads.csv")
    deals = read(sample / "deals.csv")

    owner_id = {r["rep_id"]: str(70000 + i) for i, r in enumerate(reps, start=1)}
    owners = []
    for r in reps:
        first, last = split_name(r["rep_name"])
        owners.append({"id": owner_id[r["rep_id"]], "firstName": first, "lastName": last,
                       "email": r["email"], "archived": False})

    contact_id = {l["lead_id"]: str(100000 + int(l["lead_id"][1:])) for l in leads}
    contacts = []
    for l in leads:
        first, last = split_name(l["contact_name"])
        contacts.append({"id": contact_id[l["lead_id"]], "properties": {
            "email": l["email"], "firstname": first, "lastname": last, "company": l["company"],
            "industry": l["industry"], "company_size": l["company_size"], "region": l["region"],
            "lead_source": l["source"], "lead_channel": l["channel"],
            "createdate": iso_ts(l["created_at"]),
            "hubspot_owner_id": owner_id.get(l["owner_rep_id"]),
            "rle_lead_id": l["lead_id"],
        }})

    out_deals = []
    for d in deals:
        n = int(d["deal_id"][1:])
        props = {
            "dealname": d["company"], "amount": d["amount_usd"], "pipeline": PIPELINE_ID,
            "dealstage": STAGE_ID[d["stage"]], "hubspot_owner_id": owner_id[d["owner_rep_id"]],
            "createdate": iso_day(d["qualified_date"]), "closedate": iso_day(d["closed_date"]),
            "notes_last_updated": iso_day(d["last_activity_date"]),
            "closed_lost_reason": d["lost_reason"], "rle_deal_id": d["deal_id"],
        }
        for stage, col in DATE_COLS.items():
            props[f"hs_date_entered_{STAGE_ID[stage]}"] = iso_day(d[col])
        for stage in ("won", "lost"):
            props[f"hs_date_entered_{STAGE_ID[stage]}"] = (
                iso_day(d["closed_date"]) if d["stage"] == stage else None)
        out_deals.append({"id": str(500000 + n), "properties": props, "associations": {
            "contacts": {"results": [{"id": contact_id[d["lead_id"]], "type": "deal_to_contact"}]}}})

    pipeline = {"id": PIPELINE_ID, "label": "Sales Pipeline", "displayOrder": 0, "stages": [
        {"id": sid, "label": label, "displayOrder": i,
         "metadata": {"isClosed": str(closed).lower(), "probability": prob}}
        for i, (_, sid, label, closed, prob) in enumerate(STAGES)]}

    return {
        "meta": {
            "description": "Synthetic HubSpot-shaped snapshot for fixture mode. Not real CRM data.",
            "source": "github.com/RynoJor/revenue-leak-engine data/sample (seed 42)",
            "as_of": AS_OF,
            "currency": "USD",
            "counts": {"owners": len(owners), "contacts": len(contacts), "deals": len(out_deals)},
        },
        "pipelines": [pipeline],
        "owners": owners,
        "contacts": contacts,
        "deals": out_deals,
    }


def main() -> None:
    sample = Path(sys.argv[1]) if len(sys.argv) > 1 else DEFAULT_SAMPLE
    if not (sample / "deals.csv").exists():
        sys.exit(f"No deals.csv in {sample}. Pass the path to revenue-leak-engine/data/sample.")
    snap = build(sample)
    OUT.parent.mkdir(parents=True, exist_ok=True)
    with open(OUT, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(snap, fh, indent=1)
        fh.write("\n")
    c = snap["meta"]["counts"]
    print(f"Wrote {OUT.relative_to(ROOT)}: {c['contacts']} contacts, {c['deals']} deals, "
          f"{c['owners']} owners (as of {AS_OF})")


if __name__ == "__main__":
    main()
