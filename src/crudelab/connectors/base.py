from __future__ import annotations

from abc import ABC, abstractmethod
from dataclasses import dataclass
from datetime import date

from ..ingest import Obs
from ..models import Series


class ConnectorNotReady(RuntimeError):
    """connector 尚未接通（缺密鑰或只是預留）。"""


@dataclass(frozen=True)
class ConnectorStatus:
    ready: bool
    message: str


class PullConnector(ABC):
    """拉取型數據源的統一接口。

    子類只需要做兩件事：
      status()  說明現在能不能用、缺什麼
      fetch()   把指定序列在指定區間的數據取回來，轉成 Obs
    序列與外部代碼的對應放在 Series.external_ids[self.code] 裡（來自 series.yaml）。
    """

    code: str  # 對應 data_source.code

    @abstractmethod
    def status(self) -> ConnectorStatus: ...

    @abstractmethod
    def fetch(self, series: list[Series], start: date | None, end: date | None) -> list[Obs]: ...

    def supported(self, series: list[Series]) -> list[Series]:
        """預設：在 external_ids 裡登記了本數據源代碼的序列。"""
        return [s for s in series if (s.external_ids or {}).get(self.code)]
