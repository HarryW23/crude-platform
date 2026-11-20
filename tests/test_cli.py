"""命令行：按一個管理員實際會做的順序，把每個子命令都跑一遍（初始化 → 生成並導入數據 → 跑任務 → 備份 → 恢復 → 清理），
以及出錯時的退出碼和提示。命令行是部署、運維時用的，平時不常點，壞了往往要到出事時才發現。
"""
import socket

import pytest

from crudelab import db
from crudelab.cli import main


def run(capsys, *argv) -> tuple[int, str, str]:
    code = main(list(argv))
    out = capsys.readouterr()
    return code, out.out, out.err


def test_admin_workflow_end_to_end(tmp_path, capsys):
    assert run(capsys, "init")[0] == 0
    assert run(capsys, "check")[0] == 0
    sample = tmp_path / "sample.xlsx"
    code, out, _ = run(capsys, "make-sample", str(sample))
    assert code == 0 and sample.exists() and "假數據" in out

    code, out, _ = run(capsys, "import", str(sample), "--source", "demo", "--dry-run")
    assert code == 0
    code, out, _ = run(capsys, "import", str(sample), "--source", "demo")
    assert code == 0

    code, out, _ = run(capsys, "run-job")                                          # 列出任務
    assert code == 0 and "REPORT_DAILY" in out
    code, out, _ = run(capsys, "run-job", "REPORT_DAILY")
    assert code == 0 and out.startswith("ok")
    assert run(capsys, "run-job", "NOPE")[0] == 1

    code, out, _ = run(capsys, "backup")
    assert code == 0 and "已備份並校驗" in out
    name = out.split("：", 1)[1].split("（", 1)[0].strip().rsplit("/", 1)[-1]
    code, out, _ = run(capsys, "backup", "list")
    assert code == 0 and name in out
    assert run(capsys, "backup", "verify", name)[0] == 0
    assert run(capsys, "backup", "verify")[0] == 1                                 # 沒給文件
    assert run(capsys, "backup", "verify", "no-such.tar.gz")[0] == 1
    assert run(capsys, "backup", "prune")[0] == 0

    code, _, err = run(capsys, "restore", name)                                    # 沒加 --yes：只預覽
    assert code == 1 and "--yes" in err
    db._engine = None
    code, out, _ = run(capsys, "restore", name, "--yes", "--force")
    assert code == 0 and "恢復完成" in out

    code, out, _ = run(capsys, "purge-source", "demo")                             # 沒加 --yes：只報數
    assert code == 1 and "共有" in out and "按期記錄" in out
    code, out, _ = run(capsys, "purge-source", "demo", "--yes")
    assert code == 0 and "已刪除" in out


def test_generated_documents_print(capsys):
    for command in ("data-needs", "data-model", "lseg-rics"):
        code, out, _ = run(capsys, command)
        assert code == 0 and len(out) > 200, command


def test_pull_without_key_explains_instead_of_crashing(capsys):
    code, _, err = run(capsys, "pull", "eia")
    assert code == 1 and "無法取數" in err and "EIA_API_KEY" in err


def test_serve_refuses_a_busy_port(capsys):
    with socket.socket() as busy:
        busy.bind(("127.0.0.1", 0))
        busy.listen()
        port = busy.getsockname()[1]
        code, _, err = run(capsys, "serve", "--site", "front", "--port", str(port))
    assert code == 1 and "已經被佔用" in err


def test_standalone_scheduler_runs_a_tick_and_stops_cleanly(capsys, monkeypatch):
    import time
    ticks = []

    def stop(_seconds):
        raise KeyboardInterrupt
    monkeypatch.setattr(time, "sleep", stop)
    from crudelab import scheduler
    real = scheduler.tick
    monkeypatch.setattr(scheduler, "tick", lambda cfg, settings: ticks.append(1) or real(cfg, settings))
    code, out, _ = run(capsys, "scheduler")
    assert code == 0 and ticks == [1] and "調度器已啟動" in out


def test_jin10_without_token_explains(capsys):
    code, out, err = run(capsys, "jin10", "usage")
    assert code in (0, 1) and "Traceback" not in out + err


def test_unknown_command_and_missing_arguments(capsys):
    with pytest.raises(SystemExit) as exit_info:
        main(["no-such-command"])
    assert exit_info.value.code == 2
    with pytest.raises(SystemExit):
        main(["user", "add"])                                                      # 缺用戶名
    capsys.readouterr()


def test_import_of_an_unrecognisable_file_is_a_failure(tmp_path, capsys):
    """認不出任何數據的文件（例如只有一行日期）不能報「入庫完成」、退出碼 0：自動化腳本會以為成功了。"""
    one = tmp_path / "one.csv"
    one.write_text("Date,STEO OECD Stocks\n2026-03-01,100\n", encoding="utf-8")
    code, out, err = run(capsys, "import", str(one), "--source", "reuters_xls")
    assert code == 1 and "沒有入庫" in err and "至少要有兩個日期" in err and "入庫完成" not in out
