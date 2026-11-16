"""數據接入層。

兩種接入方式：
  推送型  Excel 導入（excel.py）與後台手動錄入：由人把數據交給平台
  拉取型  API connector（base.PullConnector）：平台定時去取

無論哪種，輸出都是 ingest.Obs 列表，再交給 ingest.write_observations 入庫。
新增一個數據源 = 在 config/master/sources.yaml 加一行 + 寫一個 PullConnector 子類並註冊。
"""
from .base import ConnectorNotReady, ConnectorStatus, PullConnector
from .registry import get_connector, list_connectors

__all__ = ["ConnectorNotReady", "ConnectorStatus", "PullConnector", "get_connector", "list_connectors"]
