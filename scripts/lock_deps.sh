#!/bin/bash
# 重新生成 constraints.txt：用 uv 做「通用」解析，一份清單同時適用於 macOS（Apple 芯片 / Intel）、Windows、Linux
# 和 Python 3.11 起的各個版本；不同平台 / 版本需要不同版本號的包會帶條件標記（例如 numpy 按 Python 版本分開）。
# 只有維護代碼的人需要運行（要先裝 uv：https://docs.astral.sh/uv/）；用平台的人不需要 uv。
# 用法：bash scripts/lock_deps.sh；之後跑全部測試和 python scripts/check_lock.py，通過再提交。
set -euo pipefail
cd "$(dirname "$0")/.."
command -v uv >/dev/null || { echo "需要 uv：https://docs.astral.sh/uv/getting-started/installation/"; exit 1; }
TMP=$(mktemp)
trap 'rm -f "$TMP"' EXIT
uv pip compile pyproject.toml --extra postgres --extra anthropic --extra dev --extra e2e \
  --universal --python-version 3.11 --no-header --no-annotate -q -o "$TMP"
{
  echo "# 鎖定的依賴版本：安裝時用 pip install -e . -c constraints.txt，保證你的電腦、CI、部署機器裝的是同一組版本。"
  echo "# 不要手改：用 scripts/lock_deps.sh 重新生成（通用解析：適用於 macOS / Windows / Linux、Python 3.11 起），"
  echo "# 生成後跑全部測試和 python scripts/check_lock.py 再提交。"
  cat "$TMP"
} > constraints.txt
echo "已更新 constraints.txt（$(grep -vc '^#' constraints.txt) 行）"
