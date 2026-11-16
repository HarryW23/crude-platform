"""預留的數據源。接口已定，尚未接通。

接通時要做的事（每個類都一樣）：
  1. 給相關序列登記對方的代碼：在 schema.SeriesDef 加一個字段（參照 eia_id），
     並在 master.sync_to_db 寫進 external_ids[<code>]
  2. 實現 status()：檢查密鑰等環境變量
  3. 實現 fetch()：調用對方接口，把結果轉成 Obs(series_code, obs_date, value, source_ref)
  4. 在 sources.yaml 把 status 從 reserved 改成 active
上層（入庫、指標、後台）都不需要改。
"""
from __future__ import annotations

from datetime import date

from ..ingest import Obs
from ..models import Series
from .base import ConnectorNotReady, ConnectorStatus, PullConnector


class _Reserved(PullConnector):
    hint = ""

    def status(self) -> ConnectorStatus:
        return ConnectorStatus(False, f"預留，尚未接通。{self.hint}")

    def fetch(self, series: list[Series], start: date | None, end: date | None) -> list[Obs]:
        raise ConnectorNotReady(self.status().message)


class KplerConnector(_Reserved):
    code = "kpler"
    hint = "預期用途：船期、到港量、浮倉、分國別進出口流量。"


class ReutersAPIConnector(_Reserved):
    code = "reuters_api"
    hint = "預期用途：以 RIC 直接取期貨與現貨報價，取代 Excel 人工整理。"


class CNVendorConnector(_Reserved):
    code = "cn_vendor"
    hint = "預期用途：山東地煉成品油、化工品現貨、開工率、庫存。"
