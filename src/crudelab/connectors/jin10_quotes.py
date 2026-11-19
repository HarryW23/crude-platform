"""金十報價 → 序列（每個交易日一個值）。序列在 series.yaml 裡用 jin10: 品種代碼 登記。

值的定義見 jin10.py「每日收盤價」：紐約時間 16:59 那根 1 分鐘 K 線的收盤價，日期是紐約交易日。
每次拉取最近 CATCH_UP_DAYS 個已收盤的交易日：電腦關了一兩天也能補上；已經有的值不會重複寫入。
歷史用 crudelab jin10 backfill --days N 補，口徑完全一樣。額度、token、錯誤處理都走 jin10.Jin10。
"""
from __future__ import annotations

from datetime import date, datetime

from ..ingest import Obs
from ..models import Series
from .base import ConnectorError, ConnectorNotReady, ConnectorStatus, PullConnector

CATCH_UP_DAYS = 3


class Jin10QuoteConnector(PullConnector):
    code = "jin10"

    def __init__(self, cfg=None, client=None, now: datetime | None = None):
        self._cfg, self._client, self._now = cfg, client, now     # 只給測試用；正常運行時現讀配置、現建客戶端

    def _config(self):
        if self._cfg is None:
            from ..config import load_config
            from ..settings import get_settings
            self._cfg = load_config(get_settings().config_dir)
        return self._cfg

    def status(self) -> ConnectorStatus:
        from .. import credentials
        if not credentials.get("JIN10_TOKEN"):
            return ConnectorStatus(False, "還沒有設定 JIN10_TOKEN（用 crudelab secret set JIN10_TOKEN 設定）")
        return ConnectorStatus(True, "已配置 token")

    def fetch(self, series: list[Series], start: date | None, end: date | None) -> list[Obs]:
        from .. import jin10
        from ..models import utcnow
        from .mcp import MCPError
        try:
            client = jin10.Jin10(self._config(), self._client)
        except jin10.Jin10NotReady as exc:
            raise ConnectorNotReady(str(exc)) from None
        days = jin10.completed_trade_days(self._now or utcnow(), CATCH_UP_DAYS)
        out: list[Obs] = []
        try:
            for s in self.supported(series):
                code = s.external_ids["jin10"]
                for day in days:
                    try:
                        got = jin10.close_on(client, code, day)
                    except jin10.Jin10LimitReached as exc:
                        raise ConnectorNotReady(str(exc)) from None
                    except MCPError as exc:
                        raise ConnectorError(f"{exc}（品種 {code}）") from None
                    if got is not None:                           # None = 那天休市
                        out.append(Obs(s.code, day, got[0], f"jin10:close:{code}"))
        finally:
            client.close()
        return out
