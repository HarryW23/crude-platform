"""前端腳本的語法檢查：每個 .js 文件都要能被當作 ES 模塊解析。語法錯誤會讓整個頁面空白，而後端測試發現不了。"""
import shutil
import subprocess
from pathlib import Path

import pytest

WEB = Path(__file__).resolve().parents[1] / "src" / "crudelab" / "web"
FILES = sorted(WEB.rglob("*.js"))


@pytest.mark.skipif(shutil.which("node") is None, reason="需要 Node.js")
@pytest.mark.parametrize("path", FILES, ids=[str(p.relative_to(WEB)) for p in FILES])
def test_module_parses(path, tmp_path):
    copy = tmp_path / (path.stem + ".mjs")            # .mjs：按 ES 模塊解析（import / export）
    copy.write_text(path.read_text(encoding="utf-8"), encoding="utf-8")
    done = subprocess.run(["node", "--check", str(copy)], capture_output=True, text=True, timeout=30, check=False)
    assert done.returncode == 0, done.stderr


def test_every_front_view_that_writes_marks_its_controls():
    """前台頁面只要會發寫入請求（POST / PATCH / DELETE），就要用 data-write 標出對應的控件，
    只讀帳號才看不到點了也沒用的按鈕（#39）。不保存任何東西的試算 POST 例外。"""
    import re
    read_only_posts = ("/scenario`",)                    # 煉廠試算：api/security.py 的 READ_ONLY_POSTS
    write = re.compile(r'api\([^)]*\{[^}]*(json:|method: "(POST|PATCH|DELETE)")')
    missing = []
    for path in sorted((WEB / "js" / "views" / "front").glob("*.js")):
        text = path.read_text(encoding="utf-8")
        calls = [m.group(0) for m in write.finditer(text) if not any(r in m.group(0) for r in read_only_posts)]
        if calls and "data-write" not in text:
            missing.append(path.name)
    assert missing == []
