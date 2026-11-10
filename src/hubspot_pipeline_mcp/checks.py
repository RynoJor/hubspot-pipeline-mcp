"""The pipeline checks behind the six MCP tools. Pure functions over a CRM snapshot.

The rules and USD valuations mirror the Revenue Leak Engine's hygiene checks
(github.com/RynoJor/revenue-leak-engine, hygiene/check.py):

- stalled deal: open, in the stage, no activity for N days. At risk: the deal amount (measured).
- duplicate contact: same mailbox, or same company + matching name. At risk: open deals on the
  duplicate, because that pipeline is counted twice (measured).
- weak rep: stage-to-won rate well below the rest of the team (z <= -3). At risk:
  closed deals x (team rate - rep rate) x average won amount (estimated, a correlation).
- missing fields: a missing contact email means the deal cannot be worked, so the open amount is
  at risk; other gaps are a routing/reporting cost, valued at $0.
"""

from __future__ import annotations

import math
import re
from difflib import SequenceMatcher

from .crm import CRM, Contact, Deal, Stage

STALE_DAYS = 30
REP_OUTLIER_Z = -3.0
NAME_SIMILARITY = 0.80
COMPANY_SIMILARITY = 0.90

PUBLIC_DOMAINS = {"gmail.com", "yahoo.com", "outlook.com", "hotmail.com", "icloud.com",
                  "aol.com", "live.com", "msn.com", "protonmail.com", "me.com"}
LEGAL_SUFFIXES = {"llc", "inc", "co", "corp", "corporation", "ltd", "limited", "company",
                  "pty", "plc", "gmbh"}
NICKNAMES = {
    "mike": "michael", "mick": "michael", "rob": "robert", "bob": "robert", "bobby": "robert",
    "bill": "william", "will": "william", "liz": "elizabeth", "beth": "elizabeth",
    "jim": "james", "jimmy": "james", "dave": "david", "steve": "steven", "stephen": "steven",
    "chris": "christopher", "matt": "matthew", "dan": "daniel", "danny": "daniel",
    "tom": "thomas", "tony": "anthony", "kate": "katherine", "katie": "katherine",
    "jess": "jessica", "jen": "jennifer", "jenny": "jennifer", "nick": "nicholas",
    "sam": "samuel", "alex": "alexander", "andy": "andrew", "joe": "joseph", "greg": "gregory",
    "ben": "benjamin", "pat": "patricia", "sue": "susan", "rick": "richard", "dick": "richard",
    "ed": "edward", "ted": "edward", "jon": "jonathan", "josh": "joshua", "tim": "timothy",
    "ken": "kenneth", "ron": "ronald", "don": "donald", "larry": "lawrence", "jeff": "jeffrey",
}

DEFAULT_FIELDS = ["amount", "closedate", "hubspot_owner_id",
                  "contact.email", "contact.industry", "contact.company_size", "contact.region"]


class CheckError(ValueError):
    """Bad input from the caller. The message says how to fix it."""


# ----------------------------------------------------------------------------- helpers

def _usd(x: float) -> float:
    return round(float(x), 2)


def _avg_won(crm: CRM) -> float:
    won = [d.amount for d in crm.deals.values() if d.is_won and d.amount is not None]
    return sum(won) / len(won) if won else 0.0


def days_idle(deal: Deal, crm: CRM) -> int | None:
    return (crm.as_of - deal.last_activity).days if deal.last_activity else None


def _resolve_stage(crm: CRM, stage: str, pipeline: str | None = None) -> list[Stage]:
    found = crm.stage_by_name(stage, pipeline)
    if not found:
        raise CheckError(f"Unknown stage {stage!r}. Valid stages: {', '.join(crm.stage_names())}.")
    return found


def deal_brief(deal: Deal, crm: CRM) -> dict:
    return {"id": deal.id, "name": deal.name, "owner": deal.owner,
            "stage": deal.stage.label if deal.stage else deal.props.get("dealstage"),
            "amount_usd": deal.amount,
            "last_activity": deal.last_activity.isoformat() if deal.last_activity else None,
            "days_idle": days_idle(deal, crm)}


def _page(items: list, limit: int) -> tuple[list, bool]:
    return items[:limit], len(items) > limit


# ----------------------------------------------------------------------------- 1. stalled deals

def find_stalled_deals(crm: CRM, stage: str = "Proposal", days_idle_min: int = STALE_DAYS,
                       limit: int = 25) -> dict:
    stages = {(s.pipeline_id, s.id) for s in _resolve_stage(crm, stage)}
    hits = [d for d in crm.deals.values()
            if d.stage and (d.pipeline_id, d.stage.id) in stages and not d.is_closed
            and (days_idle(d, crm) or 0) >= days_idle_min]
    hits.sort(key=lambda d: (-(d.amount or 0), d.id))
    rows, truncated = _page(hits, limit)
    return {
        "check": "stalled_deals",
        "as_of": crm.as_of.isoformat(),
        "rule": f"open deals in stage '{stage}' with no activity for {days_idle_min}+ days",
        "count": len(hits),
        "usd_at_risk": _usd(sum(d.amount or 0 for d in hits)),
        "usd_basis": "measured: sum of the stalled deals' amounts (pipeline at risk)",
        "deals": [deal_brief(d, crm) for d in rows],
        "truncated": truncated,
    }


# ----------------------------------------------------------------------------- 2. duplicates

def norm_company(name: str | None) -> str:
    if not name:
        return ""
    words = re.sub(r"[^a-z0-9 ]", " ", name.lower()).split()
    while words and words[-1] in LEGAL_SUFFIXES:
        words.pop()
    return "".join(words)


def email_parts(email: str | None) -> tuple[str, str]:
    if not email or "@" not in email:
        return "", ""
    local, domain = email.lower().rsplit("@", 1)
    return local, domain


def name_tokens(name: str) -> tuple[str, str]:
    parts = re.sub(r"[^a-z ]", " ", (name or "").lower()).split()
    return (parts[0], parts[-1]) if parts else ("", "")


def _similar(a: str, b: str) -> float:
    return SequenceMatcher(None, a, b).ratio() if a and b else 0.0


def _first_names_compatible(a: str, b: str) -> bool:
    if not a or not b or a[0] != b[0]:
        return False
    if a == b or a.startswith(b) or b.startswith(a):
        return True
    return NICKNAMES.get(a, a) == NICKNAMES.get(b, b) or _similar(a, b) >= 0.85


def _same_person(a: Contact, b: Contact) -> tuple[str, str] | None:
    la, da = email_parts(a.email)
    lb, db = email_parts(b.email)
    if la and la.replace(".", "") == lb.replace(".", "") and da == db:
        return "email_exact", "same email address"
    fa, sa = name_tokens(a.name)
    fb, sb = name_tokens(b.name)
    if _similar(sa, sb) >= NAME_SIMILARITY and _first_names_compatible(fa, fb):
        return "company_name_fuzzy", f"same company, name match ({a.name} ~ {b.name})"
    return None


def _id_key(cid: str):
    return (0, int(cid)) if cid.isdigit() else (1, cid)


def duplicate_pairs(crm: CRM) -> list[dict]:
    """One row per duplicate contact. Contacts are only compared inside a block that shares a
    normalised company name or a business email domain (never gmail.com alone). The oldest
    contact id in a cluster is the original."""
    contacts = sorted(crm.contacts.values(), key=lambda c: _id_key(c.id))
    ckey = {c.id: norm_company(c.company) for c in contacts}
    pos = {c.id: i for i, c in enumerate(contacts)}
    blocks: dict[tuple[str, str], list[int]] = {}
    for i, c in enumerate(contacts):
        if ckey[c.id]:
            blocks.setdefault(("company", ckey[c.id]), []).append(i)
        dom = email_parts(c.email)[1]
        if dom and dom not in PUBLIC_DOMAINS:
            blocks.setdefault(("domain", dom), []).append(i)
    candidates = set()
    for idx in blocks.values():
        for x in range(len(idx)):
            for y in range(x + 1, len(idx)):
                candidates.add((idx[x], idx[y]))

    original_of: dict[int, tuple[int, str, str]] = {}
    for i, j in sorted(candidates):
        if j in original_of:
            continue
        a, b = contacts[i], contacts[j]
        if ckey[a.id] != ckey[b.id] and _similar(ckey[a.id], ckey[b.id]) < COMPANY_SIMILARITY:
            continue
        match = _same_person(a, b)
        if match:
            root = original_of.get(i, (i, "", ""))[0]
            original_of[j] = (root, *match)

    rows = []
    for j, (i, match_type, reason) in sorted(original_of.items()):
        dup, orig = contacts[j], contacts[i]
        open_deals = [crm.deals[x] for x in dup.deal_ids if x in crm.deals and not crm.deals[x].is_closed]
        rows.append({"duplicate_id": dup.id, "original_id": orig.id, "match_type": match_type,
                     "reason": reason, "name": dup.name, "company": dup.company, "email": dup.email,
                     "open_deal_ids": [d.id for d in open_deals],
                     "usd_at_risk": _usd(sum(d.amount or 0 for d in open_deals))})
    assert all(pos[r["original_id"]] < pos[r["duplicate_id"]] for r in rows)
    return rows


def find_duplicate_contacts(crm: CRM, limit: int = 25) -> dict:
    rows = duplicate_pairs(crm)
    by_type = {t: sum(r["match_type"] == t for r in rows) for t in ("email_exact", "company_name_fuzzy")}
    shown = sorted(rows, key=lambda r: (-r["usd_at_risk"], _id_key(r["duplicate_id"])))
    page, truncated = _page(shown, limit)
    return {
        "check": "duplicate_contacts",
        "as_of": crm.as_of.isoformat(),
        "rule": "same mailbox (case and dots ignored), or same company with a matching name",
        "count": len(rows),
        "by_match_type": by_type,
        "duplicates_with_open_deals": sum(bool(r["open_deal_ids"]) for r in rows),
        "usd_at_risk": _usd(sum(r["usd_at_risk"] for r in rows)),
        "usd_basis": "measured: open deals opened on a duplicate contact (pipeline counted twice)",
        "duplicates": page,
        "truncated": truncated,
    }


# ----------------------------------------------------------------------------- 3. pipeline summary

def pipeline_summary(crm: CRM, stale_days: int = STALE_DAYS) -> dict:
    out = []
    for pid, p in crm.pipelines.items():
        deals = [d for d in crm.deals.values() if d.pipeline_id == pid]
        open_stages = [s for s in p["stages"] if not s.is_closed]
        won_stage_ids = {s.id for s in p["stages"] if s.is_won}
        rows = []
        for k, s in enumerate(p["stages"]):
            here = [d for d in deals if d.stage and d.stage.id == s.id]
            row = {"stage": s.label, "deals": len(here),
                   "value_usd": _usd(sum(d.amount or 0 for d in here))}
            if not s.is_closed:
                reached = [d for d in deals if s.id in d.entered]
                nxt = open_stages[open_stages.index(s) + 1] if s is not open_stages[-1] else None
                nxt_ids = ({nxt.id} if nxt else set()) | won_stage_ids
                advanced = [d for d in reached if nxt_ids & set(d.entered)]
                closed = [d for d in reached if d.is_closed]
                won = [d for d in closed if d.is_won]
                stalled = [d for d in here if (days_idle(d, crm) or 0) >= stale_days]
                row.update({
                    "reached": len(reached),
                    "to_next_stage_rate": round(len(advanced) / len(reached), 3) if reached else None,
                    "win_rate_from_stage": round(len(won) / len(closed), 3) if closed else None,
                    "stalled_deals": len(stalled),
                    "usd_at_risk": _usd(sum(d.amount or 0 for d in stalled)),
                })
            rows.append(row)
        open_d = [d for d in deals if not d.is_closed]
        won_d = [d for d in deals if d.is_won]
        closed_d = [d for d in deals if d.is_closed]
        out.append({
            "pipeline": p["label"],
            "stages": rows,
            "totals": {
                "open_deals": len(open_d), "open_value_usd": _usd(sum(d.amount or 0 for d in open_d)),
                "won_deals": len(won_d), "won_value_usd": _usd(sum(d.amount or 0 for d in won_d)),
                "lost_deals": len(closed_d) - len(won_d),
                "win_rate": round(len(won_d) / len(closed_d), 3) if closed_d else None,
                "usd_at_risk": _usd(sum(r.get("usd_at_risk", 0) for r in rows)),
            },
        })
    return {
        "check": "pipeline_summary",
        "as_of": crm.as_of.isoformat(),
        "source": crm.source,
        "notes": {
            "to_next_stage_rate": "of deals that entered the stage, share that entered the next open stage or won",
            "win_rate_from_stage": "of closed deals that entered the stage, share won",
            "usd_at_risk": f"open deals in the stage idle {stale_days}+ days (measured)",
        },
        "pipelines": out,
    }


# ----------------------------------------------------------------------------- 4. missing fields

def _field_value(deal: Deal, crm: CRM, field: str):
    if field.startswith("contact."):
        name = field.split(".", 1)[1]
        vals = [crm.contacts[c].props.get(name) for c in deal.contact_ids if c in crm.contacts]
        return next((v for v in vals if v not in (None, "")), None)
    if field == "hubspot_owner_id" and deal.owner_id is None and deal.owner != "unassigned":
        return deal.owner  # seeded profile keeps the rep in a custom property
    return deal.props.get(field)


def deals_missing_fields(crm: CRM, fields: list[str] | None = None, include_closed: bool = False,
                         limit: int = 25) -> dict:
    fields = fields or DEFAULT_FIELDS
    bad = [f for f in fields if not re.fullmatch(r"(contact\.)?[a-z0-9_]+", f)]
    if bad:
        raise CheckError(f"Bad field names {bad}. Use HubSpot internal names, e.g. 'amount', "
                         f"'closedate', or 'contact.email' for the deal's associated contact.")
    hits, by_field = [], {f: 0 for f in fields}
    for d in crm.deals.values():
        if d.is_closed and not include_closed:
            continue
        missing = [f for f in fields if _field_value(d, crm, f) in (None, "")]
        if not missing:
            continue
        for f in missing:
            by_field[f] += 1
        no_email = "contact.email" in missing and not d.is_closed
        usd = (d.amount or 0) if no_email else 0.0
        note = ("no contact email: the deal cannot be worked" if no_email
                else "routing/reporting gap, valued at $0")
        hits.append({**deal_brief(d, crm), "missing": missing, "note": note, "usd_at_risk": _usd(usd)})
    hits.sort(key=lambda h: (-h["usd_at_risk"], -len(h["missing"]), -(h["amount_usd"] or 0), h["id"]))
    page, truncated = _page(hits, limit)
    return {
        "check": "deals_missing_fields",
        "as_of": crm.as_of.isoformat(),
        "scope": "all deals" if include_closed else "open deals",
        "fields": fields,
        "count": len(hits),
        "by_field": by_field,
        "open_value_affected_usd": _usd(sum(h["amount_usd"] or 0 for h in hits)),
        "usd_at_risk": _usd(sum(h["usd_at_risk"] for h in hits)),
        "usd_basis": "open amount where the contact email is missing (cannot be worked); other gaps $0",
        "deals": page,
        "truncated": truncated,
    }


# ----------------------------------------------------------------------------- 5. rep conversion

def rep_conversion(crm: CRM, from_stage: str = "Proposal") -> dict:
    stage_ids = {s.id for s in _resolve_stage(crm, from_stage)}
    post = [d for d in crm.deals.values() if d.is_closed and stage_ids & set(d.entered)]
    by_rep: dict[str, dict] = {}
    for d in post:
        r = by_rep.setdefault(d.owner, {"won": 0, "closed": 0, "deal_ids": []})
        r["closed"] += 1
        r["won"] += d.is_won
        r["deal_ids"].append(d.id)
    avg_won = _avg_won(crm)
    reps, total_usd = [], 0.0
    for rep, r in sorted(by_rep.items()):
        rest_won = sum(x["won"] for k, x in by_rep.items() if k != rep)
        rest_closed = sum(x["closed"] for k, x in by_rep.items() if k != rep)
        p1 = r["won"] / r["closed"]
        p2 = rest_won / rest_closed if rest_closed else None
        z = None
        if p2 is not None:
            pooled = (r["won"] + rest_won) / (r["closed"] + rest_closed)
            se = math.sqrt(pooled * (1 - pooled) * (1 / r["closed"] + 1 / rest_closed))
            z = round((p1 - p2) / se, 2) if se else 0.0
        flagged = z is not None and z <= REP_OUTLIER_Z
        usd = r["closed"] * (p2 - p1) * avg_won if flagged else 0.0
        total_usd += usd
        reps.append({"owner": rep, "closed": r["closed"], "won": r["won"], "win_rate": round(p1, 4),
                     "rest_of_team_rate": round(p2, 4) if p2 is not None else None, "z": z,
                     "flagged": flagged, "usd_at_risk": _usd(usd)})
    reps.sort(key=lambda x: x["win_rate"])
    return {
        "check": "rep_conversion",
        "as_of": crm.as_of.isoformat(),
        "rule": f"closed deals that entered '{from_stage}', won / closed per owner, vs the rest of the team; "
                f"flag at z <= {REP_OUTLIER_Z}",
        "average_won_amount_usd": _usd(avg_won),
        "reps": reps,
        "flagged": [r["owner"] for r in reps if r["flagged"]],
        "usd_at_risk": _usd(total_usd),
        "usd_basis": "estimated: closed deals x (team rate - rep rate) x average won amount. "
                     "A correlation, not proof the rep caused the gap.",
    }


# ----------------------------------------------------------------------------- 6. one deal

def get_deal(crm: CRM, deal_id: str) -> dict:
    deal = crm.deals.get(str(deal_id).strip())
    if deal is None:  # fixtures also accept the leak engine's own id, e.g. D0002
        deal = next((d for d in crm.deals.values() if d.props.get("rle_deal_id") == deal_id), None)
    if deal is None:
        raise CheckError(f"No deal with id {deal_id!r}. Use find_stalled_deals or "
                         f"deals_missing_fields to list deal ids.")

    flags = []
    idle = days_idle(deal, crm)
    if not deal.is_closed and idle is not None and idle >= STALE_DAYS:
        flags.append({"check": "stalled", "detail": f"no activity for {idle} days",
                      "usd_at_risk": _usd(deal.amount or 0)})
    dup_by_id = {r["duplicate_id"]: r for r in duplicate_pairs(crm)}
    for cid in deal.contact_ids:
        if cid in dup_by_id and not deal.is_closed:
            r = dup_by_id[cid]
            flags.append({"check": "duplicate_contact",
                          "detail": f"contact {cid} duplicates {r['original_id']} ({r['reason']}); "
                                    f"this pipeline is counted twice",
                          "usd_at_risk": _usd(deal.amount or 0)})
    miss = deals_missing_fields(crm, include_closed=True, limit=10**6)
    m = next((h for h in miss["deals"] if h["id"] == deal.id), None)
    if m:
        flags.append({"check": "missing_fields", "detail": f"missing {', '.join(m['missing'])}; {m['note']}",
                      "usd_at_risk": m["usd_at_risk"]})
    reps = rep_conversion(crm)
    rep = next((r for r in reps["reps"] if r["owner"] == deal.owner and r["flagged"]), None)
    if rep and deal.is_closed and any(s.id in deal.entered for s in _resolve_stage(crm, "Proposal")):
        flags.append({"check": "weak_rep_conversion",
                      "detail": f"{deal.owner} wins {rep['win_rate']:.0%} after Proposal vs team "
                                f"{rep['rest_of_team_rate']:.0%} (z={rep['z']})",
                      "usd_at_risk": _usd(rep["usd_at_risk"] / rep["closed"])})

    stage_order = {s.id: s.label for s in crm.pipelines.get(deal.pipeline_id, {}).get("stages", [])}
    contacts = [{"id": c.id, "name": c.name, "email": c.email, "company": c.company}
                for c in (crm.contacts.get(x) for x in deal.contact_ids) if c]
    return {
        "deal": {
            **deal_brief(deal, crm),
            "pipeline": crm.pipelines.get(deal.pipeline_id, {}).get("label"),
            "is_closed": deal.is_closed, "is_won": deal.is_won,
            "created": deal.created.isoformat() if deal.created else None,
            "close_date": deal.close_date.isoformat() if deal.close_date else None,
            "lost_reason": deal.props.get("closed_lost_reason"),
            "stage_history": {stage_order.get(k, k): v.isoformat()
                              for k, v in sorted(deal.entered.items(), key=lambda kv: kv[1])},
        },
        "contacts": contacts,
        "flags": flags,
        "usd_at_risk": max((f["usd_at_risk"] for f in flags), default=0.0),
        "usd_basis": "largest single flag, so one deal is never counted twice",
        "as_of": crm.as_of.isoformat(),
    }
