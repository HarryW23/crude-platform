#!/bin/bash
# 雙擊更新 crudelab 到 GitHub 上的最新版本（Mac；Linux 也可以在終端裡運行），然後啟動。
# 不會動到：數據（data/）、密鑰（~/.crudelab/secrets.env）、本機設定（~/.crudelab/local.env）。
cd "$(dirname "$0")" || exit 1

fail() { echo; echo "✗ $1"; echo; read -r -p "按 Enter 關閉這個窗口…" _; exit 1; }

command -v git >/dev/null 2>&1 || fail "找不到 git。Mac 上在終端運行 xcode-select --install 安裝後再試。"
[ -d .git ] || fail "這個文件夾不是用 git 下載的（可能是下載的 ZIP），沒法自動更新。請改用 git clone 重新下載一份，把 data/ 文件夾複製過去。"

# 倉庫裡的文件被改過時不自動更新，免得把你的修改覆蓋掉或卡在衝突裡
CHANGED=$(git status --porcelain --untracked-files=no)
if [ -n "$CHANGED" ]; then
  echo "下面這些文件在本機被改過："
  echo "$CHANGED"
  echo
  echo "這台機器自己的設定（用哪家大模型、備份放哪）請用 crudelab local set 保存，不要直接改倉庫裡的文件。"
  echo "確定不需要這些修改的話，在終端運行 git restore 文件名 還原後再更新。"
  fail "為了不覆蓋你的修改，這次沒有更新。"
fi

BEFORE=$(git rev-parse --short HEAD)
echo "正在從 GitHub 下載最新版本…"
git pull --ff-only || fail "更新失敗。請檢查網絡；把上面的錯誤訊息保存下來。"
AFTER=$(git rev-parse --short HEAD)
if [ "$BEFORE" = "$AFTER" ]; then
  echo "已經是最新版本（$AFTER）。"
else
  echo "已從 $BEFORE 更新到 $AFTER。這次的更新內容："
  git log --oneline "$BEFORE..$AFTER" | head -20
fi
echo
exec ./start-mac.command
