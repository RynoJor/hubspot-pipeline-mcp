# Ask Claude which deals are stuck: an MCP server over HubSpot, read-only

`hubspot-pipeline-mcp` lets Claude answer pipeline questions straight from a HubSpot CRM.
Ask "which proposals have gone quiet?" and Claude calls a tool, gets compact JSON with a USD value
at risk, and answers in plain English.

It reads. It never writes. Every tool is read-only by design (see [Why read-only](#why-read-only)).

It runs in two modes:

- **Fixture mode** (default): a synthetic HubSpot snapshot ships in the repo. No account, no token.
- **Live mode**: your HubSpot portal, through a private-app token with read scopes only.

The checks and the dollar logic come from my
[Revenue Leak Engine](https://github.com/RynoJor/revenue-leak-engine), which found $881,884
leaking from a synthetic $1.0M pipeline. This repo puts three of those checks, plus three new
pipeline views, behind an MCP server, so you can ask instead of open a dashboard.

> **Synthetic data only.** The fixture snapshot is built from the Revenue Leak Engine's generated
> sample (seed 42, as of 2026-10-31). Every company, person and deal is made up. No real CRM data.

## Tools

| Tool | What it answers | USD at risk |
|---|---|---|
| `find_stalled_deals(stage, days_idle)` | Which open deals in this stage have had no activity for N days? | Sum of their amounts (measured) |
| `find_duplicate_contacts()` | Which contacts are the same person twice? Email exact, plus company + fuzzy name. | Open deals on the duplicate: pipeline counted twice (measured) |
| `pipeline_summary()` | By stage: count, value, how many reached it, rate to the next stage, win rate from it | Stalled deals per stage (measured) |
| `deals_missing_fields(fields)` | Which deals have empty required fields? `contact.` checks the linked contact. | Open amount if the contact has no email (can't be worked); other gaps $0 |
| `rep_conversion(from_stage)` | Stage-to-won rate by owner, against the rest of the team | Gap x closed deals x average won amount (estimated) |
| `get_deal(deal_id)` | One deal: stage, owner, history, contacts, and every check it trips | Largest single flag, so nothing is counted twice |

Each result says whether its dollar figure is **measured** (real deal amounts) or **estimated**
(a win-rate gap), in a `usd_basis` field. Lists are capped by `limit`; totals always cover every match.

### Same numbers as the Revenue Leak Engine

In fixture mode the tools reproduce the leak engine's results on the same data. The tests check this.

| Leak | Revenue Leak Engine | This server |
|---|---|---|
| Deals stalled at proposal | 40 deals, $454,200 | `find_stalled_deals("Proposal", 30)`: 40 deals, $454,200 |
| Duplicate leads | 90 duplicates, $84,850 | `find_duplicate_contacts()`: 90 duplicates, $84,850 |
| Weak rep (proposal to won) | Rep R02, $198,933 | `rep_conversion("Proposal")`: Joshua Walker (R02), $198,933 |

The duplicate pairs also match the leak engine's answer key one for one.

## Demo

Claude Desktop config (`claude_desktop_config.json`). Fixture mode, no token needed:

```json
{
  "mcpServers": {
    "hubspot-pipeline": {
      "command": "uv",
      "args": ["--directory", "C:/path/to/hubspot-pipeline-mcp", "run", "hubspot-pipeline-mcp"],
      "env": { "HUBSPOT_MODE": "fixtures" }
    }
  }
}
```

Three questions, with the real tool output from fixture mode. The server sends compact JSON;
it's pretty-printed and trimmed here so you can read it.

**1. "Which proposals have gone quiet, and how much is at stake?"**

Claude calls `find_stalled_deals(stage="Proposal", days_idle=30, limit=3)`:

```json
{
  "check": "stalled_deals",
  "as_of": "2026-10-31",
  "rule": "open deals in stage 'Proposal' with no activity for 30+ days",
  "count": 40,
  "usd_at_risk": 454200.0,
  "usd_basis": "measured: sum of the stalled deals' amounts (pipeline at risk)",
  "deals": [
    {"id": "500533", "name": "Clearpoint Goods", "owner": "Danielle Johnson", "stage": "Proposal",
     "amount_usd": 42550.0, "last_activity": "2026-07-20", "days_idle": 103},
    {"id": "500476", "name": "Riverpoint Supply", "owner": "Jeffery Wagner", "stage": "Proposal",
     "amount_usd": 34150.0, "last_activity": "2026-05-14", "days_idle": 170},
    {"id": "500071", "name": "Aspenspring Market", "owner": "Patricia Miller", "stage": "Proposal",
     "amount_usd": 29100.0, "last_activity": "2026-04-12", "days_idle": 202}
  ],
  "truncated": true,
  "mode": "fixtures"
}
```

What it means: 40 proposals worth $454,200 have had no activity for a month or more. Start with
Clearpoint Goods ($42,550, quiet for 103 days).

**2. "Is any rep converting proposals well below the team?"**

Claude calls `rep_conversion(from_stage="Proposal")` (four middle reps trimmed):

```json
{
  "check": "rep_conversion",
  "as_of": "2026-10-31",
  "rule": "closed deals that entered 'Proposal', won / closed per owner, vs the rest of the team; flag at z <= -3.0",
  "average_won_amount_usd": 9414.22,
  "reps": [
    {"owner": "Joshua Walker", "closed": 60, "won": 11, "win_rate": 0.1833,
     "rest_of_team_rate": 0.5355, "z": -4.76, "flagged": true, "usd_at_risk": 198933.28},
    {"owner": "Robert Johnson", "closed": 35, "won": 25, "win_rate": 0.7143,
     "rest_of_team_rate": 0.4038, "z": 3.42, "flagged": false, "usd_at_risk": 0.0}
  ],
  "flagged": ["Joshua Walker"],
  "usd_at_risk": 198933.28,
  "usd_basis": "estimated: closed deals x (team rate - rep rate) x average won amount. A correlation, not proof the rep caused the gap.",
  "mode": "fixtures"
}
```

What it means: Joshua Walker wins 18% of proposals; the rest of the team wins 54%. If he matched
the team, that's about $199K more won. It's an estimate, so it's a reason to review his deals, not a verdict.

**3. "Are duplicate contacts inflating the pipeline?"**

Claude calls `find_duplicate_contacts(limit=2)`:

```json
{
  "check": "duplicate_contacts",
  "as_of": "2026-10-31",
  "rule": "same mailbox (case and dots ignored), or same company with a matching name",
  "count": 90,
  "by_match_type": {"email_exact": 73, "company_name_fuzzy": 17},
  "duplicates_with_open_deals": 12,
  "usd_at_risk": 84850.0,
  "usd_basis": "measured: open deals opened on a duplicate contact (pipeline counted twice)",
  "duplicates": [
    {"duplicate_id": "101975", "original_id": "101358", "match_type": "email_exact",
     "reason": "same email address", "name": "mary wiley", "company": "Embermore Coffee, Inc.",
     "email": "marywiley@embermorecoffee.com", "open_deal_ids": ["500599"], "usd_at_risk": 17750.0},
    {"duplicate_id": "101961", "original_id": "100956", "match_type": "email_exact",
     "reason": "same email address", "name": "B. Mcknight", "company": "Iron Stone Yoga",
     "email": "brycemcknight@ironstoneyoga.com", "open_deal_ids": ["500597"], "usd_at_risk": 13100.0}
  ],
  "truncated": true,
  "mode": "fixtures"
}
```

What it means: 90 contacts are duplicates. 12 of them carry an open deal, so $84,850 of pipeline
is counted twice. Merge those first.

## Setup

You need Python 3.10+ and [uv](https://docs.astral.sh/uv/) (or plain pip).

```bash
git clone https://github.com/RynoJor/hubspot-pipeline-mcp.git
```

```bash
cd hubspot-pipeline-mcp && uv sync --extra dev
```

```bash
uv run pytest
```

### Claude Desktop

Add the JSON block from [Demo](#demo) to `claude_desktop_config.json` and restart Claude Desktop.
Use the full path to your clone. For live mode, change `HUBSPOT_MODE` to `live`. Keep the token
in `.env`, not in this file.

### Claude Code

```bash
claude mcp add hubspot-pipeline -e HUBSPOT_MODE=fixtures -- uv --directory /path/to/hubspot-pipeline-mcp run hubspot-pipeline-mcp
```

Then ask: "Use hubspot-pipeline. Which deals are stuck?"

## Fixture mode vs live mode

| | Fixture mode | Live mode |
|---|---|---|
| Set with | `HUBSPOT_MODE=fixtures` (default) | `HUBSPOT_MODE=live` and `HUBSPOT_TOKEN` in `.env` |
| Data | `src/hubspot_pipeline_mcp/data/snapshot.json`: 2,000 contacts, 600 deals, 6 owners | Your HubSpot portal |
| "Today" | 2026-10-31, the snapshot date | Today (UTC), or `HUBSPOT_AS_OF` |
| Needs | Nothing | A private app with read scopes |

Both modes go through the same code. The snapshot is shaped like HubSpot's v3 API responses
(pipelines, owners, contacts, deals with contact associations), so fixture mode runs the same
parsing and checks as live mode. A test also serves the snapshot through a fake HubSpot API, with
paging, and gets the same numbers.

**Live mode details.** The client reads `/crm/v3/pipelines/deals`, `/crm/v3/owners`,
`/crm/v3/objects/contacts` and `/crm/v3/objects/deals` (with contact associations). It follows
`paging.next.after` cursors 100 records at a time. On HTTP 429 it waits for `Retry-After` (or backs
off 1s, 2s, 4s...) and retries, up to five times; 5xx errors are retried the same way. A 401 or 403
returns a message that says what to fix and never shows the token. One portal read is cached for
five minutes (`HUBSPOT_CACHE_SECONDS`).

Private app scopes: `crm.objects.contacts.read`, `crm.objects.deals.read`, `crm.objects.owners.read`.

**Seeding a test account.** `scripts/seed_hubspot.py` can push the same synthetic data into a
HubSpot developer test account, so live mode has something to read. It's a dry run unless you pass
`--apply`, it uses its own write token (`HUBSPOT_SEED_TOKEN`), and it refuses any portal that
HubSpot doesn't report as a developer test account or sandbox. After seeding, run the server with
`HUBSPOT_PROFILE=seeded` (see the script's docstring for why).

`scripts/smoke_live.py` runs `pipeline_summary` against the portal in `.env` and prints the result.

## Why read-only

An AI assistant with write access to a CRM can do real damage: one bad tool call can close the
wrong deal or merge the wrong contacts. Pipeline questions don't need write access, so this server
doesn't have it. The limit is enforced three ways:

1. **No write tools.** All six tools only read. Each one is marked `readOnlyHint: true` and
   `destructiveHint: false`, so MCP clients can tell.
2. **No write code.** The HubSpot client has five public methods, all GET. Its request helper
   only sends GET. A test fails if anyone adds a public method.
3. **No write scopes.** The private app token gets read scopes only, so even a code change
   couldn't write.

The tools find the problem and put a dollar figure on it. A person decides what to do.
Writing back (merging duplicates, nudging owners) would be a separate tool with a human approval step.

## Limits

- **Synthetic data flatters the numbers.** The leaks were planted by rules the checks encode.
  On a real portal, expect messier results and some false matches.
- **It reads the whole portal.** Each refresh pulls every contact and deal. Fine for thousands of
  records; for a large portal you'd move to HubSpot's Search API with filters.
- **Rep estimates are correlation.** `rep_conversion` dollars assume the rep could match the team.
  Small samples swing. Use it to pick deals to review.
- **Stage history comes from HubSpot.** Conversion reads `hs_date_entered_<stage>`, which HubSpot
  sets when a deal moves. Deals imported straight into a late stage have gaps.
- **Last activity is `notes_last_updated`**, with `createdate` as a fallback. Portals that log
  activity elsewhere will look more idle than they are.
- **Exact-email duplicates are rare in HubSpot.** HubSpot blocks a second contact with the same
  email, so on a live portal most hits will be dot or case variants and name matches. The fixture
  keeps the leak engine's data as it is.
- **Missing fields are checked on deals.** The leak engine's missing-field leak ($16,776) sits on
  leads that never became deals, so this tool doesn't see it. Also, the synthetic data has no
  expected close dates, so all 108 open deals report a missing `closedate`. That's a real gap in the
  data, not a planted leak.
- **USD only.** Amounts use the `amount` property. Multi-currency portals would want
  `amount_in_home_currency`.
- **Owners in seeded data.** HubSpot's API can't create users, so seeded deals keep the rep's
  name in a custom property.
- **Duplicate labels differ from the leak engine.** Here any same-mailbox match is `email_exact`
  (73), even with a different name format; the leak engine called 40 "exact" (all three fields
  identical). The 90 pairs are the same.

## Project layout

```
src/hubspot_pipeline_mcp/
  server.py       MCP server: six tools, read-only annotations, compact JSON
  checks.py       the checks and USD valuations (pure functions)
  crm.py          settings, fixture/live loading, parsing HubSpot JSON into records
  client.py       GET-only HubSpot v3 client: paging, 429/5xx retries, safe errors
  data/snapshot.json   synthetic fixture
scripts/
  build_fixture.py     rebuilds the snapshot from revenue-leak-engine/data/sample
  seed_hubspot.py      seeds a developer test account (dry run by default)
  smoke_live.py        live pipeline_summary check
tests/            33 pytest tests, all in fixture mode or against a fake HubSpot
```

Built with the official MCP Python SDK (`MCPServer`, the v2 name for FastMCP) and `httpx`.

## Safety

- No credentials in the repo. `.env` is git-ignored; `.env.example` has empty values.
- Tests force fixture mode, even if `.env` holds a token.
- Synthetic data only.

## About

Built by Ryno Jordaan, GTM engineer, HubSpot Sales Hub certified.
Related: [Revenue Leak Engine](https://github.com/RynoJor/revenue-leak-engine) ·
[Portfolio](https://rynojor.github.io) · [LinkedIn](https://www.linkedin.com/in/ryno-jordaan/)

MIT licence. See [`LICENSE`](LICENSE).
