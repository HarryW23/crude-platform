from __future__ import annotations

from .base import PullConnector
from .cftc import CFTCConnector
from .eia import EIAConnector
from .jin10_quotes import Jin10QuoteConnector
from .reserved import CNVendorConnector, KplerConnector, ReutersAPIConnector

_REGISTRY: dict[str, type[PullConnector]] = {
    cls.code: cls for cls in (EIAConnector, CFTCConnector, Jin10QuoteConnector, KplerConnector, ReutersAPIConnector,
                              CNVendorConnector)
}


def get_connector(code: str) -> PullConnector:
    if code not in _REGISTRY:
        raise KeyError(f"沒有名為 {code} 的拉取型 connector")
    return _REGISTRY[code]()


def list_connectors() -> dict[str, PullConnector]:
    return {code: cls() for code, cls in _REGISTRY.items()}
