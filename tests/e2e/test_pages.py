"""瀏覽器巡檢：用演示數據啟動前台和後台，逐頁打開（淺色 / 深色），檢查腳本錯誤、錯誤提示、橫向滾動、卡在載入中。

默認不跑（要瀏覽器，慢）。運行：CRUDELAB_E2E=1 pytest tests/e2e
CI 裡單獨一步運行。本機已有 Chromium 時可以用 CRUDELAB_CHROMIUM=瀏覽器路徑 指定，不用另外下載。
頁面清單從 web/js/app.js 讀，新增頁面自動納入。
"""
import os
import re
import socket
import threading
import time
from pathlib import Path

import pytest

if not os.environ.get("CRUDELAB_E2E"):
    pytest.skip("瀏覽器巡檢默認不跑：設 CRUDELAB_E2E=1", allow_module_level=True)
playwright = pytest.importorskip("playwright.sync_api")

ROOT = Path(__file__).resolve().parents[2]
APP_JS = ROOT / "src" / "crudelab" / "web" / "js" / "app.js"
WIDTH = 1366                     # 主攻電腦端：常見的最小筆記本寬度


def pages() -> dict[str, list[str]]:
    text = APP_JS.read_text(encoding="utf-8")
    out = {}
    for site in ("front", "admin"):
        block = re.search(site + r':\s*\[(.*?)\]\s*,?\s*(?:admin:|\}\[)', text, re.S).group(1)
        out[site] = re.findall(r'\["(\w+)",\s*"', block)
    return out


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


@pytest.fixture(scope="module")
def servers(tmp_path_factory):
    import uvicorn

    from crudelab import db
    from crudelab.api.app import create_app
    from crudelab.config import load_config
    from crudelab.imports import commit_batch, preview_file
    from crudelab.master import sync_to_db
    from crudelab.sample import last_weekday, write_sample_workbook
    from crudelab.settings import Settings
    root = tmp_path_factory.mktemp("e2e")
    ports = {"front": free_port(), "admin": free_port()}
    os.environ["CRUDELAB_FRONT_URL"] = f"http://127.0.0.1:{ports['front']}"
    os.environ["CRUDELAB_ADMIN_URL"] = f"http://127.0.0.1:{ports['admin']}"
    settings = Settings(config_dir=ROOT / "config", data_dir=root / "data",
                        database_url=f"sqlite:///{root / 'data' / 'e2e.db'}",
                        front_url=os.environ["CRUDELAB_FRONT_URL"], admin_url=os.environ["CRUDELAB_ADMIN_URL"])
    db.init_engine(settings)
    with db.session_scope() as s:
        sync_to_db(s, load_config(settings.config_dir))
    xlsx = root / "sample.xlsx"
    write_sample_workbook(xlsx, days=300, end=last_weekday())
    with db.session_scope() as s:
        commit_batch(s, preview_file(s, settings, xlsx.read_bytes(), "sample.xlsx", "demo")["batch_id"])
    running = []
    for site, port in ports.items():
        server = uvicorn.Server(uvicorn.Config(create_app(settings, site), host="127.0.0.1", port=port,
                                               log_level="warning"))
        threading.Thread(target=server.run, daemon=True).start()
        running.append(server)
    for server in running:
        for _ in range(100):
            if server.started:
                break
            time.sleep(0.1)
    yield {site: f"http://127.0.0.1:{port}" for site, port in ports.items()}
    for server in running:
        server.should_exit = True


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        executable = os.environ.get("CRUDELAB_CHROMIUM") or None
        b = p.chromium.launch(executable_path=executable)
        yield b
        b.close()


def check_page(page, url: str) -> list[str]:
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"腳本錯誤：{e}"))
    page.on("console", lambda m: m.type == "error" and errors.append(f"控制台錯誤：{m.text}"))
    page.goto(url)
    try:
        page.wait_for_function("() => !document.querySelector('main').textContent.startsWith('載入中')", timeout=15000)
    except playwright.TimeoutError:
        errors.append("15 秒還在載入")
    page.wait_for_timeout(300)
    errors += [f"錯誤提示：{t[:120]}" for t in page.locator(".msg.bad").all_inner_texts()]
    if page.evaluate("document.documentElement.scrollWidth > innerWidth + 1"):
        errors.append(f"寬度 {WIDTH} 時出現橫向滾動條")
    return errors


@pytest.mark.parametrize("scheme", ["light", "dark"])
@pytest.mark.parametrize("site", ["front", "admin"])
def test_every_page_opens_cleanly(servers, browser, site, scheme):
    problems = []
    for name in pages()[site]:                      # 每頁一個新分頁：錯誤監聽不會串到下一頁
        page = browser.new_page(viewport={"width": WIDTH, "height": 900}, color_scheme=scheme)
        problems += [f"{site}#{name}：{p}" for p in check_page(page, f"{servers[site]}/#{name}")]
        page.close()
    assert not problems, "\n".join(problems)


def test_key_interactions(servers, browser):
    page = browser.new_page(viewport={"width": WIDTH, "height": 900})
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    page.goto(f"{servers['front']}/#spreads")
    page.locator("tr[data-ind=\"SPR_BRENT_WTI\"]").click()      # 指標詳情：有數據才畫得出走勢圖，順帶證明數據導入了
    page.wait_for_selector("#board svg")
    page.wait_for_selector("#curves svg")                               # 期貨曲線
    page.goto(f"{servers['front']}/#outlook")
    page.locator("tr[data-module]").first.click()                        # 信號明細
    page.wait_for_selector("#signals table")
    page.select_option("#tz", "America/Los_Angeles")                     # 時區切換後頁面重畫
    page.wait_for_selector("tr[data-module]")
    page.goto(f"{servers['front']}/#positioning")
    page.wait_for_selector("tr[data-ind]")
    assert not errors, errors
    assert len(pages()["front"]) >= 10 and len(pages()["admin"]) >= 9
