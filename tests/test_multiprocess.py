"""前台、後台分成兩個進程運行（#35）：真的啟動三個 crudelab serve 進程（前台一個、後台兩個副本），
共用一個數據庫和一份配置，檢查：

  - 數據互通：後台改的參數，前台馬上看得到
  - 配置同步：改了 config/ 下的文件，兩個進程各自重新載入，不用重啟
  - 登錄：前台連錯導致的鎖定，在後台也生效（計數在數據庫裡，#36）
  - 調度器：只跟著後台跑；兩個後台副本同時跑時，同一個時點的同一次任務只執行一次

要啟動子進程，比一般測試慢（約 40 秒）；設 CRUDELAB_SKIP_SLOW=1 可以跳過。
"""
from __future__ import annotations

import os
import shutil
import socket
import sqlite3
import subprocess
import sys
import time
from pathlib import Path

import httpx
import pytest

from crudelab.settings import PROJECT_ROOT

if os.environ.get("CRUDELAB_SKIP_SLOW"):
    pytest.skip("設了 CRUDELAB_SKIP_SLOW", allow_module_level=True)
if os.environ.get("CRUDELAB_TEST_PG"):
    pytest.skip("多進程測試用 SQLite 數據文件；PostgreSQL 的並發由數據庫自己保證", allow_module_level=True)

PASSWORD = "multi-process-pass-1234"
CLI = [sys.executable, "-m", "crudelab.cli"]


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def wait_up(url: str, proc: subprocess.Popen, log: Path, timeout: float = 60) -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if proc.poll() is not None:
            raise AssertionError(f"進程提前退出：{log.read_text(encoding='utf-8')[-2000:]}")
        try:
            if httpx.get(f"{url}/api/site", timeout=2).status_code == 200:
                return
        except httpx.HTTPError:
            pass
        time.sleep(0.3)
    raise AssertionError(f"{url} 沒有啟動：{log.read_text(encoding='utf-8')[-2000:]}")


def eventually(check, timeout: float = 15, what: str = "") -> None:
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if check():
            return
        time.sleep(0.3)
    raise AssertionError(f"等不到：{what}")


@pytest.fixture(scope="module")
def cluster(tmp_path_factory):
    root = tmp_path_factory.mktemp("multi")
    config = root / "config"
    shutil.copytree(PROJECT_ROOT / "config", config)
    env = {**os.environ, "CRUDELAB_CONFIG_DIR": str(config), "CRUDELAB_DATA_DIR": str(root / "data"),
           "CRUDELAB_SECRETS_FILE": str(root / "home" / "secrets.env"),
           "CRUDELAB_LOCAL_FILE": str(root / "home" / "local.env"), "CRUDELAB_AUTH": "on",
           "CRUDELAB_SCHEDULER_TICK_SECONDS": "2"}
    env.pop("DATABASE_URL", None)
    subprocess.run([*CLI, "user", "add", "admin1", "--role", "admin", "--password-stdin"], input=PASSWORD + "\n",
                   env=env, check=True, capture_output=True, text=True, timeout=120)
    front_port, admin_port, admin2_port = free_port(), free_port(), free_port()
    plan = {"front": ["--site", "front", "--port", str(front_port), "--admin-port", str(admin_port)],
            "admin": ["--site", "admin", "--port", str(front_port), "--admin-port", str(admin_port)],
            "admin2": ["--site", "admin", "--port", str(front_port), "--admin-port", str(admin2_port)]}
    procs, urls = {}, {"front": f"http://127.0.0.1:{front_port}", "admin": f"http://127.0.0.1:{admin_port}",
                       "admin2": f"http://127.0.0.1:{admin2_port}"}
    try:
        for name, args in plan.items():
            log = root / f"{name}.log"
            procs[name] = subprocess.Popen([*CLI, "serve", *args], env=env, stdout=log.open("w"),
                                           stderr=subprocess.STDOUT)
            wait_up(urls[name], procs[name], log)
        yield {"urls": urls, "config": config, "db": root / "data" / "crudelab.db", "procs": procs, "env": env}
    finally:
        for proc in procs.values():
            proc.terminate()
        for proc in procs.values():
            try:
                proc.wait(timeout=15)
            except subprocess.TimeoutExpired:
                proc.kill()


def login(url: str, username: str = "admin1", password: str = PASSWORD) -> httpx.Client:
    client = httpx.Client(base_url=url, timeout=30)
    r = client.post("/api/auth/login", json={"username": username, "password": password})
    assert r.status_code == 200, r.text
    return client


def test_only_the_admin_process_says_it_runs_the_scheduler(cluster):
    assert all(p.poll() is None for p in cluster["procs"].values())
    logs = {n: (cluster["db"].parent.parent / f"{n}.log").read_text(encoding="utf-8") for n in cluster["procs"]}
    assert "定時任務：已啟用" not in logs["front"] and "定時任務：已啟用" in logs["admin"]


def test_parameter_changed_in_admin_is_seen_by_front(cluster):
    admin, front = login(cluster["urls"]["admin"]), login(cluster["urls"]["front"])
    r = admin.post("/api/parameters/BBL_PER_T_ESPO/versions", json={"value": 7.41, "valid_from": "2020-01-01",
                                                                     "note": "多進程測試"})
    assert r.status_code == 200, r.text
    values = {p["key"]: p["value"] for p in front.get("/api/parameters").json()}
    assert values["BBL_PER_T_ESPO"] == 7.41


def test_config_change_is_picked_up_by_both_processes(cluster):
    path = cluster["config"] / "parameters" / "conversion.yaml"
    path.write_text(path.read_text(encoding="utf-8").replace("name_zh: ESPO 桶/噸", "name_zh: ESPO 桶每噸"),
                    encoding="utf-8")
    for name in ("front", "admin"):
        client = login(cluster["urls"][name])
        eventually(lambda c=client: any(p["name_zh"] == "ESPO 桶每噸" for p in c.get("/api/parameters").json()),
                   what=f"{name} 重新載入配置")


def test_lockout_is_shared_between_processes(cluster):
    front = httpx.Client(base_url=cluster["urls"]["front"], timeout=30)
    for _ in range(5):
        assert front.post("/api/auth/login", json={"username": "admin1", "password": "wrong-password-x"}
                          ).status_code == 401
    r = httpx.post(f"{cluster['urls']['admin']}/api/auth/login", json={"username": "admin1", "password": PASSWORD})
    assert r.status_code == 429
    subprocess.run([*CLI, "user", "unlock", "admin1"], env=cluster["env"], check=True, capture_output=True, timeout=60)
    assert httpx.post(f"{cluster['urls']['admin']}/api/auth/login",
                      json={"username": "admin1", "password": PASSWORD}).status_code == 200


def test_two_admin_replicas_never_run_the_same_slot_twice(cluster):
    def runs():
        with sqlite3.connect(cluster["db"]) as conn:
            return conn.execute("SELECT job_code, slot, attempt, COUNT(*) FROM job_run WHERE trigger = 'schedule' "
                                "GROUP BY job_code, slot, attempt").fetchall()
    eventually(lambda: any(code == "IMPORT_INBOX" for code, *_ in runs()), timeout=30, what="調度器跑過一次")
    time.sleep(5)                                            # 兩個副本都至少再檢查了兩輪
    assert all(count == 1 for *_, count in runs())
