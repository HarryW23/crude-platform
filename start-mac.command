#!/bin/bash
# 雙擊啟動 crudelab（Mac；Linux 也可以在終端裡運行）。
# 第一次運行會建立 Python 環境並安裝依賴，需要聯網，約一兩分鐘；之後直接啟動。
cd "$(dirname "$0")" || exit 1

fail() { echo; echo "✗ $1"; echo; read -r -p "按 Enter 關閉這個窗口…" _; exit 1; }

PY=""
for candidate in python3.13 python3.12 python3.11 python3 python; do
  if command -v "$candidate" >/dev/null 2>&1 &&
     "$candidate" -c 'import sys; sys.exit(0 if sys.version_info >= (3, 11) else 1)' 2>/dev/null; then
    PY="$candidate"; break
  fi
done
[ -n "$PY" ] || fail "找不到 Python 3.11 或更新版本。請到 https://www.python.org/downloads/ 下載安裝後再試。"

if [ ! -x .venv/bin/crudelab ]; then
  echo "第一次運行：正在安裝，需要聯網，請稍候…"
  "$PY" -m venv .venv || fail "建立 Python 環境失敗。"
fi

# 依賴清單（pyproject.toml）變了就重新安裝一次：更新代碼後新加的依賴會自動裝上
STAMP=.venv/.crudelab-installed
WANT=$(.venv/bin/python -c 'import hashlib; print(hashlib.sha256(open("pyproject.toml", "rb").read()).hexdigest())')
if [ ! -x .venv/bin/crudelab ] || [ "$(cat "$STAMP" 2>/dev/null)" != "$WANT" ]; then
  [ -f "$STAMP" ] && echo "依賴有變化，正在更新，需要聯網，請稍候…"
  .venv/bin/pip install --quiet --disable-pip-version-check -e . ||
    fail "安裝依賴失敗。請檢查網絡後再試；把上面的錯誤訊息保存下來。"
  echo "$WANT" > "$STAMP"
  echo "安裝完成。"
fi

.venv/bin/crudelab serve --open || fail "啟動失敗，原因見上面的訊息。"
