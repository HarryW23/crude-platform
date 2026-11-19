"""金十報價 → 序列（每天一個值）。序列在 series.yaml 裡用 jin10: 品種代碼 登記。

每天定時取一次 get_quote，取它的最新價（close）。日期用 jin10.trade_date：報價時間的紐約日期
（北京時間早上取到的是紐約前一天收盤後的價格）；落在紐約週末的報價不記。
歷史用 crudelab jin10 backfill 從 K 線補（只接受日 K：相鄰兩根相隔不到 20 小時的不要）。
額度、token、錯誤處理都走 jin10.Jin10。
"""
from __future__ import annotations

from datetime import date

from ..ingest import Obs
from ..models import Series
from .base import ConnectorError, ConnectorNotReady, ConnectorStatus, PullConnector


class Jin10QuoteConnector(PullConnector):
    code = "jin10"

    def __init__(self, cfg=None, client=None):
        self._cfg, self._client = cfg, client          # 只給測試用；正常運行時現讀配置、現建客戶端

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
        from .mcp import MCPError
        try:
            client = jin10.Jin10(self._config(), self._client)
        except jin10.Jin10NotReady as exc:
            raise ConnectorNotReady(str(exc)) from None
        out: list[Obs] = []
        try:
            for s in self.supported(series):
                code = s.external_ids["jin10"]
                try:
                    data = client.call("get_quote", code=code)
                except jin10.Jin10LimitReached as exc:
                    raise ConnectorNotReady(str(exc)) from None
                except MCPError as exc:
                    raise ConnectorError(f"{exc}（品種 {code}）") from None
                quote = data.get("data", data) if isinstance(data, dict) else {}
                value, when = jin10.number(quote.get("close")), jin10.parse_time(quote.get("time"))
                if value is None or when is None:
                    raise ConnectorError(f"金十的 {code} 報價裡沒有可用的價格或時間")
                day = jin10.trade_date(when)
                if day is not None:
                    out.append(Obs(s.code, day, value, f"jin10:quote:{code}"))
        finally:
            client.close()
        return out
