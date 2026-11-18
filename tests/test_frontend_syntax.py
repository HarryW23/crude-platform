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
