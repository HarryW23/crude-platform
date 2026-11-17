"""EIA 公開數據（API v2）。需要免費申請的 key，放在環境變量 EIA_API_KEY。

用的是 v2 的 seriesid 兼容路徑，所以 series.yaml 裡只要寫舊式序列號（例如 PET.WCESTUS1.W）。
"""
from __future__ import annotations

from datetime import date

import httpx

from .. import credentials
from ..ingest import Obs
from ..models import Series
from ..periods import parse_period
from .base import ConnectorError, ConnectorNotReady, ConnectorStatus, PullConnector

BASE_URL = "https://api.eia.gov/v2/seriesid/"


def _period_date(period: str) -> date | None:
    """EIA 的 period：日 / 周是 YYYY-MM-DD，月是 YYYY-MM，季是 YYYY-Qn，年是 YYYY。期間型記在期間第一天。"""
    if len(period) == 10:
        try:
            return date.fromisoformat(period)
        except ValueError:
            return None
    if len(period) == 4 and period.isdigit():
        return date(int(period), 1, 1)
    return parse_period(period)


class EIAConnector(PullConnector):
    code = "eia"

    def __init__(self, api_key: str | None = None, client: httpx.Client | None = None):
        self._explicit_key = api_key          # 只給測試用；正常運行時每次取數才向 credentials 要
        self._client = client

    @property
    def _api_key(self) -> str | None:
        return self._explicit_key or credentials.get("EIA_API_KEY")

    def status(self) -> ConnectorStatus:
        if not self._api_key:
            return ConnectorStatus(False, "還沒有設定 EIA_API_KEY（可在 eia.gov/opendata 免費申請；"
                                          "用 crudelab secret set EIA_API_KEY 設定）")
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
                # EIA 的 key 只能放在網址參數裡，所以這裡的任何錯誤都不能把異常原文或網址帶出去
                try:
                    resp = client.get(BASE_URL + eia_id, params=params)
                except httpx.HTTPError as exc:
                    raise ConnectorError(f"連不上 EIA（{type(exc).__name__}），序列 {s.code}") from None
                if resp.status_code in (401, 403):
                    raise ConnectorError("EIA 拒絕了請求：EIA_API_KEY 無效或已停用")
                if resp.status_code != 200:
                    raise ConnectorError(f"EIA 回報錯誤 {resp.status_code}，序列 {s.code}（{eia_id}）")
                try:
                    rows = resp.json().get("response", {}).get("data", [])
                except ValueError:
                    raise ConnectorError(f"EIA 回覆的不是 JSON，序列 {s.code}（{eia_id}）") from None
                for row in rows:
                    period, value = row.get("period"), row.get("value")
                    when = _period_date(str(period)) if period is not None else None
                    if when is None or value is None:
                        continue
                    out.append(Obs(s.code, when, float(value), f"eia:{eia_id}"))
        finally:
            if self._client is None:
                client.close()
        return out
