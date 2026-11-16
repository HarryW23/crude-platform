"""EIA 公開數據（API v2）。需要免費申請的 key，放在環境變量 EIA_API_KEY。

用的是 v2 的 seriesid 兼容路徑，所以 series.yaml 裡只要寫舊式序列號（例如 PET.WCESTUS1.W）。
"""
from __future__ import annotations

import os
from datetime import date

import httpx

from ..ingest import Obs
from ..models import Series
from .base import ConnectorNotReady, ConnectorStatus, PullConnector

BASE_URL = "https://api.eia.gov/v2/seriesid/"


class EIAConnector(PullConnector):
    code = "eia"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self._api_key = api_key or os.environ.get("EIA_API_KEY")
        self._client = client

    def status(self) -> ConnectorStatus:
        if not self._api_key:
            return ConnectorStatus(False, "缺少環境變量 EIA_API_KEY（可在 eia.gov/opendata 免費申請）")
        return ConnectorStatus(True, "已配置 key")

    def fetch(self, series: list[Series], start: date | None, end: date | None) -> list[Obs]:
        if not self._api_key:
            raise ConnectorNotReady(self.status().message)
        client = self._client or httpx.Client(timeout=30)
        out: list[Obs] = []
        try:
            for s in self.supported(series):
                eia_id = s.external_ids["eia"]
                params: dict[str, str | int] = {"api_key": self._api_key, "length": 5000}
                if start:
                    params["start"] = start.isoformat()
                if end:
                    params["end"] = end.isoformat()
                resp = client.get(BASE_URL + eia_id, params=params)
                resp.raise_for_status()
                for row in resp.json().get("response", {}).get("data", []):
                    period, value = row.get("period"), row.get("value")
                    if period is None or value is None or len(str(period)) != 10:
                        continue
                    out.append(Obs(s.code, date.fromisoformat(str(period)), float(value), f"eia:{eia_id}"))
        finally:
            if self._client is None:
                client.close()
        return out
