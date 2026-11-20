"""類型檢查的「棘輪」：每個文件的 mypy 錯誤數只能減少，不能增加。

現有的錯誤（大多是 pandas 和 Optional 的類型標註問題，已逐一審查過，見 git 歷史）記在 scripts/mypy_baseline.json。
  - 某個文件的錯誤比基線多：CI 失敗，列出新增的錯誤 —— 新代碼不能引入類型問題
  - 某個文件的錯誤比基線少：CI 也失敗，提示運行 --update 收緊基線 —— 修好的不能再壞回去
用法：python scripts/check_types.py            檢查
      python scripts/check_types.py --update   修好一些之後，把基線收緊到現在的數
"""
from __future__ import annotations

import json
import re
import subprocess
import sys
from collections import Counter
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
BASELINE = ROOT / "scripts" / "mypy_baseline.json"
LINE = re.compile(r"^(src/[^:]+):(\d+): error: (.*)$")


def run() -> tuple[Counter, list[str]]:
    done = subprocess.run([sys.executable, "-m", "mypy", "--no-error-summary", "--no-pretty"], cwd=ROOT,
                          capture_output=True, text=True)
    if done.returncode not in (0, 1) or "error: " in done.stderr:
        raise SystemExit(f"mypy 本身運行失敗：\n{done.stderr[-2000:]}")
    errors = [ln for ln in done.stdout.splitlines() if LINE.match(ln)]
    return Counter(LINE.match(ln).group(1) for ln in errors), errors


def main() -> int:
    counts, errors = run()
    if "--update" in sys.argv:
        BASELINE.write_text(json.dumps(dict(sorted(counts.items())), indent=1, ensure_ascii=False) + "\n",
                            encoding="utf-8")
        print(f"已更新基線：{sum(counts.values())} 個錯誤，{len(counts)} 個文件")
        return 0
    baseline = json.loads(BASELINE.read_text(encoding="utf-8"))
    worse = {f: (baseline.get(f, 0), n) for f, n in counts.items() if n > baseline.get(f, 0)}
    better = {f: (n, counts.get(f, 0)) for f, n in baseline.items() if counts.get(f, 0) < n}
    for f, (was, now) in sorted(worse.items()):
        print(f"✗ {f}：類型錯誤從 {was} 個變成 {now} 個。這個文件現在的錯誤：")
        print("\n".join("    " + e for e in errors if e.startswith(f + ":")))
    for f, (was, now) in sorted(better.items()):
        print(f"↓ {f}：類型錯誤從 {was} 個減到 {now} 個，請運行 python scripts/check_types.py --update 收緊基線")
    if worse or better:
        return 1
    print(f"類型檢查通過：{sum(counts.values())} 個已知問題（基線），沒有新增")
    return 0


if __name__ == "__main__":
    sys.exit(main())
