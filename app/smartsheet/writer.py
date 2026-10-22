"""The only code that writes to Smartsheet, and it can only fill contact cells.

Allowed: the All Subcontracts sheet, and only its contact name/email/phone columns
(billing, the sub's PM, estimator, foreman). Anything else is refused before a request is
made. Reading for the app still goes through the read-only client in client.py.
"""
from __future__ import annotations

import time

import httpx
import structlog

from app.config import get_settings
from app.smartsheet.client import column_map

log = structlog.get_logger()

SHEET_KEY = "subcontracts"
WRITABLE = (
    "billing_name", "billing_email", "billing_phone",
    "pm_name", "pm_email", "pm_phone",
    "estimator_name", "estimator_email", "estimator_phone",
    "foreman_name", "foreman_email", "foreman_phone",
)


class ContactWriter:
    def __init__(self, token: str | None = None, base_url: str | None = None):
        s = get_settings()
        self.token = token or s.smartsheet_token
        if not self.token:
            raise RuntimeError("SMARTSHEET_TOKEN is not set")
        spec = column_map()[SHEET_KEY]
        self.sheet_id = spec["id"]
        self.columns = {name: spec["columns"][name] for name in WRITABLE}
        self.http = httpx.Client(base_url=base_url or s.smartsheet_api_base,
                                 headers={"Authorization": f"Bearer {self.token}"}, timeout=60.0)

    def _request(self, method: str, path: str, **kw) -> dict:
        for attempt in range(5):
            resp = self.http.request(method, path, **kw)
            if resp.status_code == 429 or resp.status_code >= 500:
                time.sleep(2 ** attempt)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def get_row(self, row_id: int) -> dict[str, str | None]:
        """Current contact cells of one row, read fresh right before writing."""
        data = self._request("GET", f"/sheets/{self.sheet_id}/rows/{row_id}")
        by_id = {cid: name for name, cid in self.columns.items()}
        out: dict[str, str | None] = {name: None for name in WRITABLE}
        for cell in data.get("cells", []):
            name = by_id.get(cell.get("columnId"))
            if name:
                v = cell.get("value")
                out[name] = str(v).strip() if v not in (None, "") else None
        return out

    def update_row(self, row_id: int, values: dict[str, str]) -> None:
        bad = [k for k in values if k not in self.columns]
        if bad:
            raise PermissionError(f"The app may not write {', '.join(bad)} in Smartsheet")
        cells = [{"columnId": self.columns[k], "value": v} for k, v in values.items()]
        self._request("PUT", f"/sheets/{self.sheet_id}/rows", json=[{"id": row_id, "cells": cells}])
