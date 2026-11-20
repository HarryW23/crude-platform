"""瀏覽器巡檢的測試配置。

上一層 conftest 的 isolated_credentials 會在每個測試開始時清掉模塊級的數據庫連接（防止測試之間串數據），
但巡檢的服務器是整組共用、在測試開始前就啟動的：連接被清掉後，它會退回去讀默認位置的空數據庫。
所以這裡用同名夾具覆蓋：照樣隔離密鑰和本機設定，但不動數據庫連接。
"""
import os

import pytest


@pytest.fixture(autouse=True)
def isolated_credentials(tmp_path, monkeypatch):
    from crudelab import credentials
    monkeypatch.setenv("CRUDELAB_SECRETS_FILE", str(tmp_path / "home" / "secrets.env"))
    monkeypatch.setenv("CRUDELAB_LOCAL_FILE", str(tmp_path / "home" / "local.env"))
    for name in [*credentials.KNOWN, *(n for n in os.environ if credentials.is_known(n))]:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setattr(credentials, "_cache", None)
