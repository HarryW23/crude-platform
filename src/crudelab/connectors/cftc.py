"""CFTC 持倉報告（Commitments of Traders）。公開數據，不需要密鑰。

用 CFTC 公開報告網站的 SODA 接口：https://publicreporting.cftc.gov/resource/<數據集>.json
  分類持倉、只含期貨（Disaggregated, Futures Only）：數據集 72hh-3qpy
  字段例如 m_money_positions_long_all（管理基金多頭）、open_interest_all（總持倉量）
  report_date_as_yyyy_mm_dd 是持倉日期（周二），報告在當周周五發佈。

series.yaml 裡用 cftc: {market: 合約代碼, field: 字段名} 登記；同一個合約的字段合併成一次請求。
不帶 start 時預設取最近 5 年（足夠算三年回看的擁擠度）。

⚠ 數據集編號和字段名是按 CFTC 網站的公開說明登記的，還沒有在真實接口上調用過；第一次取數後請核對。
"""
from __future__ import annotations

from collections import defaultdict
from datetime import date, timedelta

import httpx

from ..ingest import Obs
from ..models import Series
from .base import ConnectorError, ConnectorStatus, PullConnector

BASE_URL = "https://publicreporting.cftc.gov/resource/"
DATASETS = {"disaggregated_futures": "72hh-3qpy"}
DATE_FIELD = "report_date_as_yyyy_mm_dd"
DEFAULT_YEARS = 5
PAGE = 5000


class CFTCConnector(PullConnector):
    code = "cftc"

    def __init__(self, client: httpx.Client | None = None):
        self._client = client

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(True, "公開數據，不需要密鑰（每周五發佈，數據截至周二）")

    def fetch(self, series: list[Series], start: date | None, end: date | None) -> list[Obs]:
        start = start or date.today() - timedelta(days=365 * DEFAULT_YEARS)
        groups: dict[tuple[str, str], list[tuple[Series, str]]] = defaultdict(list)
        for s in self.supported(series):
            ref = s.external_ids["cftc"]
            groups[(ref.get("report", "disaggregated_futures"), ref["market"])].append((s, ref["field"]))
        client = self._client or httpx.Client(timeout=60)
        out: list[Obs] = []
        try:
            for (report, market), members in groups.items():
                dataset = DATASETS.get(report)
                if dataset is None:
                    raise ConnectorError(f"不認識的 CFTC 報告類型 {report}")
                fields = sorted({f for _, f in members})
                where = f"cftc_contract_market_code='{market}' AND {DATE_FIELD} >= '{start.isoformat()}T00:00:00'"
                if end:
                    where += f" AND {DATE_FIELD} <= '{end.isoformat()}T23:59:59'"
                params = {"$select": ",".join([DATE_FIELD, *fields]), "$where": where,
                          "$order": DATE_FIELD, "$limit": PAGE}
                try:
                    resp = client.get(f"{BASE_URL}{dataset}.json", params=params)
                except httpx.HTTPError as exc:
                    raise ConnectorError(f"連不上 CFTC（{type(exc).__name__}），合約 {market}") from None
                if resp.status_code == 400:
                    raise ConnectorError(f"CFTC 不接受這個查詢（合約 {market}）："
                                         "多半是字段名不對，請核對 series.yaml 的 cftc.field")
                if resp.status_code != 200:
                    raise ConnectorError(f"CFTC 回報錯誤 {resp.status_code}，合約 {market}")
                try:
                    rows = resp.json()
                except ValueError:
                    raise ConnectorError(f"CFTC 回覆的不是 JSON，合約 {market}") from None
                if not isinstance(rows, list):
                    raise ConnectorError(f"CFTC 回覆的格式不符合預期，合約 {market}")
                for row in rows:
                    try:
                        when = date.fromisoformat(str(row[DATE_FIELD])[:10])
                    except (KeyError, ValueError):
                        continue
                    for s, field in members:
                        value = row.get(field)
                        try:
                            number = float(value)
                        except (TypeError, ValueError):
                            continue
                        out.append(Obs(s.code, when, number, f"cftc:{dataset}:{market}:{field}"))
        finally:
            if self._client is None:
                client.close()
        return out
