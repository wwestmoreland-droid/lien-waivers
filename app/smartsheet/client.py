"""Read-only Smartsheet client.

This module only issues GET requests, and the HTTP transport refuses any other verb. The
app's only writes (blank contact cells in All Subcontracts) go through writer.py, which
can't touch any other sheet or column.
"""
from __future__ import annotations

import time
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from typing import Any, Protocol

import httpx
import structlog
import yaml

from app.config import get_settings

log = structlog.get_logger()

COLUMNS_FILE = Path(__file__).with_name("columns.yaml")


@lru_cache
def column_map() -> dict[str, dict[str, Any]]:
    return yaml.safe_load(COLUMNS_FILE.read_text())["sheets"]


class MissingColumnError(RuntimeError):
    pass


@dataclass
class SheetRow:
    row_id: int
    values: dict[str, Any]  # keyed by our logical column name
    display: dict[str, Any]


class SheetSource(Protocol):
    def read_sheet(self, sheet_key: str) -> list[SheetRow]: ...


class _ReadOnlyTransport(httpx.HTTPTransport):
    def handle_request(self, request: httpx.Request) -> httpx.Response:
        if request.method != "GET":
            raise PermissionError(f"Smartsheet is read-only for this app; refused {request.method}")
        return super().handle_request(request)


class SmartsheetClient:
    def __init__(self, token: str | None = None, base_url: str | None = None):
        settings = get_settings()
        self.token = token or settings.smartsheet_token
        if not self.token:
            raise RuntimeError("SMARTSHEET_TOKEN is not set")
        self.http = httpx.Client(
            base_url=base_url or settings.smartsheet_api_base,
            headers={"Authorization": f"Bearer {self.token}"},
            timeout=60.0,
            transport=_ReadOnlyTransport(retries=2),
        )

    def _get(self, path: str, params: dict | None = None) -> dict:
        for attempt in range(5):
            resp = self.http.get(path, params=params)
            if resp.status_code == 429 or resp.status_code >= 500:
                wait = 2 ** attempt
                log.warning("smartsheet.retry", status=resp.status_code, wait=wait)
                time.sleep(wait)
                continue
            resp.raise_for_status()
            return resp.json()
        resp.raise_for_status()
        return resp.json()

    def read_sheet(self, sheet_key: str) -> list[SheetRow]:
        spec = column_map()[sheet_key]
        wanted: dict[str, int] = spec["columns"]
        data = self._get(
            f"/sheets/{spec['id']}",
            params={"columnIds": ",".join(str(c) for c in wanted.values()), "level": 2,
                    "include": "objectValue"},
        )
        present = {c["id"] for c in data.get("columns", [])}
        missing = [f"{name} ({cid})" for name, cid in wanted.items() if cid not in present]
        if missing:
            raise MissingColumnError(
                f"Sheet {sheet_key} ({spec['id']}) is missing columns: {', '.join(missing)}. "
                "Update app/smartsheet/columns.yaml."
            )
        by_id = {cid: name for name, cid in wanted.items()}
        rows: list[SheetRow] = []
        for r in data.get("rows", []):
            values: dict[str, Any] = {}
            display: dict[str, Any] = {}
            for cell in r.get("cells", []):
                name = by_id.get(cell.get("columnId"))
                if not name:
                    continue
                values[name] = _cell_value(cell)
                display[name] = cell.get("displayValue")
            rows.append(SheetRow(row_id=r["id"], values=values, display=display))
        return rows


def _cell_value(cell: dict) -> Any:
    """Prefer plain values; flatten contact and multi-picklist object values to text."""
    if "value" in cell and cell["value"] is not None:
        return cell["value"]
    ov = cell.get("objectValue")
    if isinstance(ov, dict):
        t = ov.get("objectType")
        if t == "CONTACT":
            return {"name": ov.get("name"), "email": ov.get("email")}
        if t == "MULTI_CONTACT":
            return [{"name": v.get("name"), "email": v.get("email")} for v in ov.get("values", [])]
        if t == "MULTI_PICKLIST":
            return ", ".join(str(v) for v in ov.get("values", []))
    return cell.get("displayValue")
