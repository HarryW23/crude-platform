"""本機設定：存在項目文件夾之外，更新代碼不受影響；不收密鑰；不覆蓋真正的環境變量。"""
import os

import pytest

from crudelab import cli, settings
from crudelab.settings import LocalSettingsError, apply_local_settings, read_local_settings, set_local_setting


@pytest.fixture(autouse=True)
def clean_env(monkeypatch):
    for name in settings.LOCAL_SETTINGS:
        if name != "CRUDELAB_DATA_DIR":                 # conftest 指向臨時目錄的這個要留著
            monkeypatch.delenv(name, raising=False)


def test_file_lives_outside_the_project():
    path = settings.local_settings_path()
    assert settings.PROJECT_ROOT not in path.parents


def test_set_apply_and_env_priority(monkeypatch):
    set_local_setting("CRUDELAB_AUTH", "on")
    set_local_setting("CRUDELAB_BACKUP_DIR", "/mnt/nas/backups")
    assert read_local_settings() == {"CRUDELAB_AUTH": "on", "CRUDELAB_BACKUP_DIR": "/mnt/nas/backups"}
    monkeypatch.setenv("CRUDELAB_BACKUP_DIR", "/from/env")              # 真正的環境變量優先
    applied = apply_local_settings()
    assert applied == {"CRUDELAB_AUTH": "on"}
    assert os.environ["CRUDELAB_AUTH"] == "on" and os.environ["CRUDELAB_BACKUP_DIR"] == "/from/env"
    set_local_setting("CRUDELAB_BACKUP_DIR", None)
    assert "CRUDELAB_BACKUP_DIR" not in read_local_settings()


def test_rejects_secrets_and_unknown_names(capsys):
    with pytest.raises(LocalSettingsError):
        set_local_setting("DEEPSEEK_API_KEY", "x")
    with pytest.raises(LocalSettingsError):
        set_local_setting("CRUDELAB_BACKUP_DIR", "a\nDEEPSEEK_API_KEY=b")
    settings.local_settings_path().parent.mkdir(parents=True, exist_ok=True)
    settings.local_settings_path().write_text("DEEPSEEK_API_KEY=leaked\nPATH=/evil\nCRUDELAB_AUTH=on\n")
    assert read_local_settings() == {"CRUDELAB_AUTH": "on"}                 # 只認列出的名字
    assert cli.main(["local", "set", "DEEPSEEK_API_KEY", "x"]) == 1
    assert "crudelab secret set" in capsys.readouterr().err


def test_llm_choice_is_not_a_local_setting(session, capsys):
    """大模型的選擇存在數據庫（後台和 crudelab llm use 共用），不在本機設定文件裡。"""
    with pytest.raises(LocalSettingsError):
        set_local_setting("CRUDELAB_LLM_PROVIDER", "deepseek")
    assert cli.main(["llm", "use", "deepseek", "--model", "some-model", "--thinking", "on"]) == 0
    assert read_local_settings() == {}
    from crudelab.llm import status
    st = status()
    assert (st["provider"], st["model"], st["thinking"], st["source"]) == ("deepseek", "some-model", True, "後台")
    assert cli.main(["llm", "use", "deepseek"]) == 1                     # 沒給型號：不猜
