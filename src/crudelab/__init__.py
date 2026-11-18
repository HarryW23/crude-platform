"""crudelab — 原油研究平台。

分層（下層不依賴上層）：
  connectors  數據接入：每個數據源一個 connector，只負責把外部數據變成 Observation 記錄
  ingest      入庫：統一的寫入口，負責去重、修訂記錄、審計
  master      主數據：品種 / 地區 / 單位 / 序列 / 參數，來源是 config/ 下的 YAML
  indicators  指標引擎：公式 + 參數 → 時間序列，並可逐層追溯
  llm         大模型抽象接口（預留）
  api / web   後台接口與頁面
"""

__version__ = "0.18.0"       # 和 pyproject.toml 保持一致（tests/test_backup.py 會檢查）
