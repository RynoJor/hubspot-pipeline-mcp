"""Load a CRM snapshot (fixture file or live HubSpot) and normalise it into plain records.

Both modes produce the same raw shape (HubSpot v3 JSON), and both go through `parse()`, so the
fixture tests cover the live parsing code too.
"""

from __future__ import annotations

import json
import os
import threading
import time
from dataclasses import dataclass, field
from datetime import date, datetime, timezone
from pathlib import Path

from .client import HubSpotClient, HubSpotError

FIXTURE_PATH = Path(__file__).resolve().parent / "data" / "snapshot.json"

CONTACT_PROPS = ["email", "firstname", "lastname", "company", "industry", "company_size",
                 "region", "createdate", "hubspot_owner_id"]
DEAL_PROPS = ["dealname", "amount", "dealstage", "pipeline", "hubspot_owner_id", "createdate",
              "closedate", "notes_last_updated", "closed_lost_reason"]


# ----------------------------------------------------------------------------- settings

@dataclass(frozen=True)
class Settings:
    mode: str = "fixtures"                      # fixtures | live
    token: str = ""
    as_of: date | None = None                   # None = today (UTC) in live mode
    owner_property: str = "hubspot_owner_id"    # where the rep comes from
    last_activity_property: str = "notes_last_updated"
    cache_seconds: int = 300
    fixture_path: Path = FIXTURE_PATH

    @classmethod
    def from_env(cls) -> "Settings":
        mode = os.getenv("HUBSPOT_MODE", "fixtures").strip().lower() or "fixtures"
        if mode not in ("fixtures", "live"):
            raise ValueError(f"HUBSPOT_MODE must be 'fixtures' or 'live', not {mode!r}")
        seeded = os.getenv("HUBSPOT_PROFILE", "").strip().lower() == "seeded"
        as_of_raw = os.getenv("HUBSPOT_AS_OF", "").strip() or ("2026-10-31" if seeded else "")
        return cls(
            mode=mode,
            token=os.getenv("HUBSPOT_TOKEN", "").strip(),
            as_of=date.fromisoformat(as_of_raw) if as_of_raw else None,
            owner_property="rle_rep_name" if seeded else "hubspot_owner_id",
            last_activity_property="rle_last_activity_date" if seeded else "notes_last_updated",
            cache_seconds=int(os.getenv("HUBSPOT_CACHE_SECONDS", "300")),
        )


# ----------------------------------------------------------------------------- records

@dataclass
class Stage:
    id: str
    label: str
    order: int
    pipeline_id: str
    is_closed: bool
    is_won: bool


@dataclass
class Contact:
    id: str
    email: str | None
    firstname: str | None
    lastname: str | None
    company: str | None
    props: dict
    deal_ids: list[str] = field(default_factory=list)

    @property
    def name(self) -> str:
        return " ".join(p for p in (self.firstname, self.lastname) if p)


@dataclass
class Deal:
    id: str
    name: str | None
    amount: float | None
    pipeline_id: str
    stage: Stage | None
    owner: str                      # display name, or "unassigned"
    owner_id: str | None
    created: date | None
    close_date: date | None
    last_activity: date | None
    entered: dict[str, date]        # stage id -> date the deal entered it
    contact_ids: list[str]
    props: dict

    @property
    def is_closed(self) -> bool:
        return bool(self.stage and self.stage.is_closed)

    @property
    def is_won(self) -> bool:
        return bool(self.stage and self.stage.is_won)


@dataclass
class CRM:
    as_of: date
    source: str
    pipelines: dict[str, dict]           # id -> {"label", "stages": [Stage]}
    stages: dict[tuple[str, str], Stage]  # (pipeline id, stage id) -> Stage
    contacts: dict[str, Contact]
    deals: dict[str, Deal]

    def stage_by_name(self, name: str, pipeline: str | None = None) -> list[Stage]:
        """Stages whose label or id matches `name` (case-insensitive), across pipelines."""
        key = name.strip().lower()
        return [s for (pid, _), s in self.stages.items()
                if (pipeline is None or pid == pipeline or self.pipelines[pid]["label"].lower() == pipeline.lower())
                and key in (s.label.lower(), s.id.lower())]

    def stage_names(self) -> list[str]:
        return sorted({s.label for s in self.stages.values()})


# ----------------------------------------------------------------------------- parsing

def _to_date(value) -> date | None:
    if value in (None, ""):
        return None
    s = str(value)
    if s.isdigit():  # epoch milliseconds (some HubSpot date properties)
        return datetime.fromtimestamp(int(s) / 1000, tz=timezone.utc).date()
    try:
        return datetime.fromisoformat(s.replace("Z", "+00:00")).date()
    except ValueError:
        return date.fromisoformat(s[:10])


def _to_float(value) -> float | None:
    if value in (None, ""):
        return None
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


def parse(raw: dict, *, as_of: date, source: str, owner_property: str = "hubspot_owner_id",
          last_activity_property: str = "notes_last_updated") -> CRM:
    pipelines, stages = {}, {}
    for p in raw["pipelines"]:
        plist = []
        for i, s in enumerate(sorted(p["stages"], key=lambda s: s.get("displayOrder", 0))):
            meta = s.get("metadata") or {}
            closed = str(meta.get("isClosed", "false")).lower() == "true"
            prob = _to_float(meta.get("probability"))
            st = Stage(id=s["id"], label=s["label"], order=i, pipeline_id=p["id"],
                       is_closed=closed, is_won=closed and prob is not None and prob >= 1.0)
            stages[(p["id"], s["id"])] = st
            plist.append(st)
        pipelines[p["id"]] = {"label": p["label"], "stages": plist}

    owner_names = {}
    for o in raw.get("owners", []):
        nm = " ".join(x for x in (o.get("firstName"), o.get("lastName")) if x) or o.get("email") or o["id"]
        owner_names[str(o["id"])] = nm

    contacts = {}
    for c in raw["contacts"]:
        pr = c.get("properties") or {}
        contacts[c["id"]] = Contact(id=c["id"], email=pr.get("email") or None,
                                    firstname=pr.get("firstname") or None,
                                    lastname=pr.get("lastname") or None,
                                    company=pr.get("company") or None, props=pr)

    deals = {}
    for d in raw["deals"]:
        pr = d.get("properties") or {}
        pid = pr.get("pipeline") or ""
        stage = stages.get((pid, pr.get("dealstage") or ""))
        if owner_property == "hubspot_owner_id":
            oid = pr.get("hubspot_owner_id") or None
            owner = owner_names.get(oid, f"owner {oid}") if oid else "unassigned"
        else:
            oid = None
            owner = pr.get(owner_property) or "unassigned"
        entered = {}
        for st in pipelines.get(pid, {}).get("stages", []):
            dt = _to_date(pr.get(f"hs_date_entered_{st.id}"))
            if dt:
                entered[st.id] = dt
        if stage and stage.id not in entered and _to_date(pr.get("createdate")) and stage.order == 0:
            entered[stage.id] = _to_date(pr.get("createdate"))
        assoc = ((d.get("associations") or {}).get("contacts") or {}).get("results") or []
        last = _to_date(pr.get(last_activity_property)) or _to_date(pr.get("createdate"))
        deal = Deal(id=d["id"], name=pr.get("dealname"), amount=_to_float(pr.get("amount")),
                    pipeline_id=pid, stage=stage, owner=owner, owner_id=oid,
                    created=_to_date(pr.get("createdate")), close_date=_to_date(pr.get("closedate")),
                    last_activity=last, entered=entered,
                    contact_ids=[str(a["id"]) for a in assoc], props=pr)
        deals[deal.id] = deal
        for cid in deal.contact_ids:
            if cid in contacts:
                contacts[cid].deal_ids.append(deal.id)

    return CRM(as_of=as_of, source=source, pipelines=pipelines, stages=stages,
               contacts=contacts, deals=deals)


# ----------------------------------------------------------------------------- sources

def load_fixture(path: Path = FIXTURE_PATH, as_of: date | None = None) -> CRM:
    with open(path, encoding="utf-8") as fh:
        raw = json.load(fh)
    snap_as_of = date.fromisoformat(raw["meta"]["as_of"])
    return parse(raw, as_of=as_of or snap_as_of, source="fixtures (synthetic snapshot)")


def fetch_live(settings: Settings, client: HubSpotClient | None = None) -> CRM:
    own = client is None
    client = client or HubSpotClient(settings.token)
    try:
        pipelines = client.deal_pipelines()
        entered_props = sorted({f"hs_date_entered_{s['id']}" for p in pipelines for s in p["stages"]})
        extra = [p for p in (settings.owner_property, settings.last_activity_property)
                 if p not in DEAL_PROPS]
        raw = {
            "pipelines": pipelines,
            "owners": client.owners() if settings.owner_property == "hubspot_owner_id" else [],
            "contacts": client.contacts(CONTACT_PROPS),
            "deals": client.deals(DEAL_PROPS + extra + entered_props),
        }
    finally:
        if own:
            client.close()
    as_of = settings.as_of or datetime.now(timezone.utc).date()
    return parse(raw, as_of=as_of, source="live HubSpot", owner_property=settings.owner_property,
                 last_activity_property=settings.last_activity_property)


class Source:
    """Hands tools a CRM snapshot. Fixtures load once; live reads are cached for cache_seconds."""

    def __init__(self, settings: Settings):
        self.settings = settings
        self._crm: CRM | None = None
        self._loaded_at = 0.0
        self._lock = threading.Lock()

    def get(self) -> CRM:
        with self._lock:
            fresh = self._crm is not None and (
                self.settings.mode == "fixtures"
                or time.monotonic() - self._loaded_at < self.settings.cache_seconds)
            if not fresh:
                if self.settings.mode == "fixtures":
                    self._crm = load_fixture(self.settings.fixture_path, self.settings.as_of)
                else:
                    if not self.settings.token:
                        raise HubSpotError("HUBSPOT_MODE=live but HUBSPOT_TOKEN is not set in .env. "
                                           "Add the token or use HUBSPOT_MODE=fixtures.")
                    self._crm = fetch_live(self.settings)
                self._loaded_at = time.monotonic()
            return self._crm
