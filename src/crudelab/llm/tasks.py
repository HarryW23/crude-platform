"""交給大模型的兩件事：從文字裡抽取地緣事件、為報告寫解讀。提示詞集中放在這裡。"""
from __future__ import annotations

import json
import re
from datetime import date

from pydantic import BaseModel, Field, ValidationError

from ..config import Config
from ..events import CATEGORIES
from .base import LLMError, LLMProvider
from .guard import unknown_numbers

_FENCE = re.compile(r"^```(?:json)?\s*|\s*```$", re.M)


class ExtractedEvent(BaseModel):
    title: str = Field(min_length=1, max_length=200)
    category: str = "other"
    location_code: str | None = None
    status: str = "confirmed"
    bias: int = Field(default=0, ge=-2, le=2)
    started_on: date | None = None
    summary: str | None = None


def extract_events(provider: LLMProvider, text: str, cfg: Config) -> tuple[list[ExtractedEvent], int]:
    """從一段新聞或筆記裡抽取和原油市場有關的地緣事件。回傳（通過校驗的事件, 被丟棄的條數）。

    結果只是草稿：調用方應存成 draft 事件，由人確認後才生效。
    """
    locations = "、".join(f"{loc.code}（{loc.name_zh}）" for loc in cfg.locations.values())
    system = f"""你是原油市場研究助手。從用戶給的文字裡找出會影響原油供需或價格的事件，整理成結構化記錄。

只記錄文字裡明確寫到的事，不要補充文字裡沒有的信息，不要推測數量。文字裡沒有相關事件就回傳空列表。

輸出一個 JSON 對象：{{"events": [...]}}。每個事件的字段：
- title：一句話標題，中文
- category：{' / '.join(CATEGORIES)} 之一
  （conflict 衝突，chokepoint 航道受阻，sanction 制裁，opec_policy 產油國政策，
   outage 生產或設施中斷，policy 其他政策，demand 需求側，other 其他）
- location_code：從這個列表裡選最貼近的代碼，選不出就用 null：{locations}
- status：rumor（未證實的傳聞）/ confirmed（已發生）/ ongoing（持續中）/ resolved（已結束）
- bias：對油價的方向，−2 到 2 的整數（2 強利多，−2 強利空，拿不準用 0）
- started_on：開始日期 YYYY-MM-DD，文字裡沒寫就用 null
- summary：兩三句話的摘要，只用原文裡的事實"""
    raw = provider.complete(system, text, json=True, max_tokens=4000)
    try:
        items = json.loads(_FENCE.sub("", raw.strip())).get("events", [])
    except (json.JSONDecodeError, AttributeError) as exc:
        raise LLMError("大模型的回覆不是預期的 JSON 格式，無法解析") from exc
    if not isinstance(items, list):
        raise LLMError("大模型的回覆不是預期的 JSON 格式，無法解析")
    events, dropped = [], 0
    for item in items:
        try:
            event = ExtractedEvent.model_validate(item)
        except ValidationError:
            dropped += 1
            continue
        if event.category not in CATEGORIES:
            event.category = "other"
        if event.status not in ("rumor", "confirmed", "ongoing", "resolved"):
            event.status = "rumor"
        if event.location_code not in cfg.locations:
            event.location_code = None
        events.append(event)
    return events, dropped


def write_narrative(provider: LLMProvider, markdown: str, kind_name: str) -> dict:
    """根據報告正文寫一段解讀。回傳 {text, provider, model, unknown_numbers, guard_passed}。"""
    system = f"""你是原油市場研究員，正在為一份{kind_name}寫「解讀」部分。下面是由系統計算、已經核對過的報告正文。

要求：
- 只根據正文裡的內容寫，不要引入正文以外的數據、新聞或你自己記得的行情。
- 任何數字都必須原樣取自正文，不要自己計算、換算或估計新的數字。
- 正文標明「缺數據」「缺參數」「待核對」「樣本不足」的地方，要如實說明這部分無法判斷，不要繞過。
- 說清楚各模組的信號方向是否一致、主要矛盾在哪、哪些結論依賴尚未核對的假設。
- 用繁體中文，四到六段，不要標題，不要列表，不要寫投資建議。"""
    text = provider.complete(system, markdown, max_tokens=4000).strip()
    unknown = unknown_numbers(text, markdown)
    return {"text": text, "provider": provider.name, "model": provider.model,
            "unknown_numbers": unknown, "guard_passed": not unknown}
