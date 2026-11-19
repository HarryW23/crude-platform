"""繁體 → 簡體。金十的內容和搜索都是簡體；我們這邊的人寫繁體。

用在兩處：
  - crudelab check：發現配置裡（快訊關鍵詞、日曆規則）寫了繁體，指出來並給出簡體寫法，複製貼上即可
  - 以後平台上讓人輸入關鍵詞去查金十時，先用 to_simplified 轉一次再發

做法：OpenCC 的 t2s（逐字 + 常見詞組，純 Python，Apache-2.0），再套用 term_overrides 裡的用詞替換。
後者處理兩岸用詞不同、逐字轉換轉不出來的詞，例如 日圓 → 日元。替換表兩邊都可以寫繁體，會先各自轉成簡體。
已經是簡體的文本轉換後不變。
"""
from __future__ import annotations

from functools import lru_cache


@lru_cache(maxsize=1)
def _converter():
    from opencc import OpenCC
    return OpenCC("t2s")


def _t2s(text: str) -> str:
    return _converter().convert(text)


def to_simplified(text: str, overrides: dict[str, str] | None = None) -> str:
    out = _t2s(text)
    for old, new in sorted(((_t2s(k), _t2s(v)) for k, v in (overrides or {}).items()),
                           key=lambda kv: -len(kv[0])):          # 長的詞先換，避免被短詞截斷
        out = out.replace(old, new)
    return out


def traditional_issues(values: dict[str, list[str]], overrides: dict[str, str] | None = None) -> list[str]:
    """{位置: [文本]} → 每條需要改的寫法一行：「位置：歐佩克 → 应写成 欧佩克」。"""
    issues = []
    for where, texts in values.items():
        for text in texts:
            fixed = to_simplified(text, overrides)
            if fixed != text:
                issues.append(f"{where}：{text} → 應寫成 {fixed}")
    return issues
