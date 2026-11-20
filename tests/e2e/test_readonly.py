"""只讀帳號看不到不能用的按鈕（#39）：開啟登錄的前台，只讀帳號逐頁檢查；研究員對照，按鈕仍在。

運行：CRUDELAB_E2E=1 pytest tests/e2e
"""
import os
import threading
import time
from datetime import date

import pytest

if not os.environ.get("CRUDELAB_E2E"):
    pytest.skip("瀏覽器巡檢默認不跑：設 CRUDELAB_E2E=1", allow_module_level=True)
playwright = pytest.importorskip("playwright.sync_api")

from .test_pages import ROOT, free_port, pages  # noqa: E402

PASSWORD = "readonly-pass-1234"
# 會改東西的按鈕上常見的字：只讀帳號在任何前台頁面都不應該看到它們
WRITE_WORDS = ("新建", "保存", "刪除", "撤銷", "記下", "加入", "建立", "套用", "生成報告", "開始回測", "抽成草稿",
               "抽取成草稿", "同步地緣事件", "確認", "不採納", "歸檔", "送出", "讓大模型寫解讀", "重寫解讀")


@pytest.fixture(scope="module")
def front(tmp_path_factory):
    import uvicorn

    from crudelab import auth, db, projection
    from crudelab.api.app import create_app
    from crudelab.config import load_config
    from crudelab.imports import commit_batch, preview_file
    from crudelab.master import sync_to_db
    from crudelab.models import Event
    from crudelab.sample import last_weekday, write_sample_workbook
    from crudelab.settings import Settings
    root = tmp_path_factory.mktemp("readonly")
    port = free_port()
    settings = Settings(config_dir=ROOT / "config", data_dir=root / "data",
                        database_url=f"sqlite:///{root / 'data' / 'ro.db'}", auth_enabled=True,
                        front_url=f"http://127.0.0.1:{port}", admin_url=f"http://127.0.0.1:{free_port()}")
    db.init_engine(settings)
    cfg = load_config(settings.config_dir)
    with db.session_scope() as s:
        sync_to_db(s, cfg)
    xlsx = root / "sample.xlsx"
    write_sample_workbook(xlsx, days=120, end=last_weekday())
    with db.session_scope() as s:
        commit_batch(s, preview_file(s, settings, xlsx.read_bytes(), "sample.xlsx", "demo")["batch_id"])
        auth.create_user(s, "admin1", PASSWORD, "admin")             # 開了登錄卻沒有管理員時程序不肯啟動
        auth.create_user(s, "viewer1", PASSWORD, "viewer")
        auth.create_user(s, "analyst1", PASSWORD, "analyst")
        s.add(Event(title="測試事件", category="other", status="confirmed", bias=1, origin="manual", draft=False,
                    created_by="t", started_on=date.today(), probability=0.2))
        projection.create(s, "測試推演", cfg=cfg)
    server = uvicorn.Server(uvicorn.Config(create_app(settings, "front"), host="127.0.0.1", port=port,
                                           log_level="warning"))
    threading.Thread(target=server.run, daemon=True).start()
    for _ in range(100):
        if server.started:
            break
        time.sleep(0.1)
    yield f"http://127.0.0.1:{port}"
    server.should_exit = True


@pytest.fixture(scope="module")
def browser():
    with playwright.sync_playwright() as p:
        b = p.chromium.launch(executable_path=os.environ.get("CRUDELAB_CHROMIUM") or None)
        yield b
        b.close()


def logged_in(browser, url: str, username: str):
    context = browser.new_context(viewport={"width": 1366, "height": 900})
    page = context.new_page()
    errors: list[str] = []
    page.on("pageerror", lambda e: errors.append(f"腳本錯誤：{e}"))
    page.on("console", lambda m: m.type == "error" and errors.append(f"控制台：{m.text}"))
    page.goto(url)
    login = page.request.post(f"{url}/api/auth/login", data={"username": username, "password": PASSWORD})
    assert login.ok, login.text()
    page.reload()                                       # 頁面在登錄前載入，要重新載入才知道是誰
    try:                                                # 確實登錄了：不然整頁是登錄框，「沒看到按鈕」是假通過
        page.wait_for_function(f"() => document.getElementById('account')?.textContent.includes('{username}')",
                               timeout=20000)
    except playwright.TimeoutError:
        site = page.request.get(f"{url}/api/site").text()[:300]
        account = page.evaluate("() => document.getElementById('account')?.outerHTML ?? '（沒有 #account）'")
        main = page.evaluate("() => document.querySelector('main')?.textContent.slice(0, 200)")
        raise AssertionError(f"登錄後頁面沒有認出帳號。錯誤：{errors}；#account：{account}；"
                             f"main：{main}；site：{site}") from None
    return context, page


def open_page(page, url: str, name: str) -> None:
    page.goto(f"{url}/#{name}")
    page.wait_for_function("() => !document.querySelector('main').textContent.startsWith('載入中')", timeout=15000)
    page.wait_for_timeout(500)


def visible_controls(page) -> list[str]:
    return page.evaluate("""() => [...document.querySelectorAll('main button, main textarea')]
        .filter((el) => el.offsetParent !== null && !el.disabled)
        .map((el) => el.textContent.trim() || el.placeholder || el.id)""")


def test_viewer_sees_no_write_controls(front, browser):
    context, page = logged_in(browser, front, "viewer1")
    errors = []
    page.on("pageerror", lambda e: errors.append(str(e)))
    leaks = {}
    for name in pages()["front"]:
        open_page(page, front, name)
        found = [c for c in visible_controls(page) if any(w in c for w in WRITE_WORDS)]
        if found:
            leaks[name] = found
    assert leaks == {} and errors == []
    open_page(page, front, "events")                                 # 表格裡可就地改的值：顯示但不能改
    fields = page.locator("[data-write-field]")
    assert fields.count() > 0 and all(fields.nth(i).is_disabled() for i in range(fields.count()))
    open_page(page, front, "chat")
    assert page.locator("[data-readonly-only]").is_visible()
    context.close()


def test_analyst_still_sees_them(front, browser):
    context, page = logged_in(browser, front, "analyst1")
    open_page(page, front, "journal")
    assert "記下" in visible_controls(page)
    open_page(page, front, "outlook")
    assert any("新建推演" in c for c in visible_controls(page))
    open_page(page, front, "chat")
    assert not page.locator("[data-readonly-only]").is_visible()                 # 只讀說明不給研究員看
    context.close()
