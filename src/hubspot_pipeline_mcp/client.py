"""A read-only HubSpot CRM v3 client.

This class only sends GET requests. There is no create, update or delete method, and `_get` refuses
any other verb, so the MCP server cannot change a portal even if a tool is misused. The private app
token should also carry read scopes only (see README), so the limit holds twice.

Handles cursor pagination (`paging.next.after`) and rate limits: on 429 it waits for `Retry-After`
(or backs off exponentially) and retries; 5xx errors are retried the same way.
"""

from __future__ import annotations

import time
from typing import Any, Callable, Iterator

import httpx

BASE_URL = "https://api.hubapi.com"
PAGE_SIZE = 100            # HubSpot's maximum for list endpoints
MAX_RETRIES = 5
READ_SCOPES = "crm.objects.contacts.read, crm.objects.deals.read, crm.objects.owners.read"


class HubSpotError(RuntimeError):
    """An error safe to show to the model. Never includes the token or request headers."""


class HubSpotClient:
    def __init__(self, token: str, *, base_url: str = BASE_URL,
                 transport: httpx.BaseTransport | None = None,
                 sleep: Callable[[float], None] = time.sleep, timeout: float = 30.0):
        if not token:
            raise HubSpotError("HUBSPOT_TOKEN is empty. Add it to .env, or set HUBSPOT_MODE=fixtures.")
        self._http = httpx.Client(base_url=base_url, transport=transport, timeout=timeout,
                                  headers={"Authorization": f"Bearer {token}",
                                           "Accept": "application/json"})
        self._sleep = sleep
        self.requests_made = 0

    def close(self) -> None:
        self._http.close()

    # ------------------------------------------------------------------ transport

    def _get(self, path: str, params: dict[str, Any] | None = None) -> dict:
        method = "GET"  # the only verb this client will ever send
        delay = 1.0
        for attempt in range(MAX_RETRIES + 1):
            try:
                resp = self._http.request(method, path, params=params)
            except httpx.TransportError as exc:
                if attempt == MAX_RETRIES:
                    raise HubSpotError(f"Network error calling HubSpot {path}: {type(exc).__name__}") from None
                self._sleep(delay)
                delay *= 2
                continue
            self.requests_made += 1
            if resp.status_code == 429 or resp.status_code >= 500:
                if attempt == MAX_RETRIES:
                    raise HubSpotError(f"HubSpot {path} still failing after {MAX_RETRIES} retries "
                                       f"(HTTP {resp.status_code}). Try again in a minute.")
                retry_after = resp.headers.get("Retry-After")
                wait = float(retry_after) if retry_after and retry_after.replace(".", "", 1).isdigit() else delay
                self._sleep(wait)
                delay *= 2
                continue
            if resp.status_code == 401:
                raise HubSpotError("HubSpot rejected the token (HTTP 401). Check HUBSPOT_TOKEN in .env "
                                   "is the private app's access token.")
            if resp.status_code == 403:
                raise HubSpotError(f"HubSpot refused {path} (HTTP 403): the private app is missing a scope. "
                                   f"It needs: {READ_SCOPES}.")
            if resp.status_code >= 400:
                raise HubSpotError(f"HubSpot {path} returned HTTP {resp.status_code}: {_short(resp.text)}")
            return resp.json()
        raise HubSpotError("unreachable")  # pragma: no cover

    def _paged(self, path: str, params: dict[str, Any]) -> Iterator[dict]:
        params = dict(params)
        while True:
            body = self._get(path, params)
            yield from body.get("results", [])
            after = (body.get("paging") or {}).get("next", {}).get("after")
            if not after:
                return
            params["after"] = after

    # ------------------------------------------------------------------ endpoints

    def deal_pipelines(self) -> list[dict]:
        return self._get("/crm/v3/pipelines/deals").get("results", [])

    def owners(self) -> list[dict]:
        return list(self._paged("/crm/v3/owners", {"limit": PAGE_SIZE, "archived": "false"}))

    def contacts(self, properties: list[str]) -> list[dict]:
        return list(self._paged("/crm/v3/objects/contacts",
                                {"limit": PAGE_SIZE, "properties": ",".join(properties)}))

    def deals(self, properties: list[str]) -> list[dict]:
        return list(self._paged("/crm/v3/objects/deals",
                                {"limit": PAGE_SIZE, "properties": ",".join(properties),
                                 "associations": "contacts"}))


def _short(text: str, n: int = 200) -> str:
    text = " ".join(text.split())
    return text if len(text) <= n else text[:n] + "..."
