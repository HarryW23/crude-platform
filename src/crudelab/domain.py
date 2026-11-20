"""數據庫裡「只能取這幾個值」的列，允許的值集中寫在這裡。

models.py 用它們生成 CHECK 約束：代碼有 bug 寫進拼錯的狀態時，數據庫直接拒絕，而不是默默存下、讓頁面和統計出錯。
這些值在業務代碼裡也各有定義（配置的 Literal 類型、events.STATUSES、auth.ROLES …），
tests/test_domain.py 核對兩邊完全一致：改了一邊忘了另一邊，測試會失敗。

要增加一個值：改這裡和對應的業務定義，並寫一個遷移重建那個約束（見 0021 的寫法）。
這個模塊不引用其他應用模塊，遷移腳本以外誰都可以引用它。
"""
from __future__ import annotations

import re

_PLAIN = re.compile(r"[A-Za-z0-9_]+")        # 只收英文字母、數字、下劃線（isalnum 會放過 ¹、é 這類字符）

# 主數據（值來自配置文件，schema.py 的 Literal 是業務定義）
COMMODITY_CATEGORIES = ("crude", "product", "chem", "freight", "fx", "fundamental", "metals")
SOURCE_KINDS = ("manual", "excel", "api")
SOURCE_STATUSES = ("active", "reserved")
SERIES_KINDS = ("futures", "swap", "spot", "diff", "osp", "freight", "fx", "stock", "flow", "ratio", "position")
FREQUENCIES = ("daily", "weekly", "monthly", "quarterly")
MODULES = ("spread", "balance", "geo", "refinery", "positioning", "common")
ALIAS_ORIGINS = ("config", "learned")

# 導入
IMPORT_STATUSES = ("previewed", "committed")

# 判斷與產出
EVENT_CATEGORIES = ("conflict", "chokepoint", "sanction", "opec_policy", "outage", "policy", "demand", "other")
EVENT_STATUSES = ("rumor", "confirmed", "ongoing", "resolved", "dismissed")
EVENT_ORIGINS = ("manual", "llm")
EVENT_BIAS = (-2, 2)                       # 方向判斷的範圍：−2 強利空 … +2 強利多
REPORT_KINDS = ("daily", "weekly", "monthly")
BACKTEST_STATUSES = ("running", "done", "failed")
BACKTEST_MODES = ("observation", "vintage")

# 帳號
ROLES = ("viewer", "analyst", "admin")

# 調度與外部調用
JOB_TRIGGERS = ("schedule", "manual")
JOB_STATUSES = ("running", "ok", "skipped", "failed", "retry", "retried")
NOTIFICATION_EVENTS = ("failure", "reminder", "recovery", "test")
EXTERNAL_CALL_STATUSES = ("ok", "failed")
LLM_PURPOSES = ("chat", "extract", "narrative", "test", "models", "other")
LLM_CALL_STATUSES = ("running", "ok", "failed")

# 表 → 列 → 允許的值。models.py 和遷移 0021 都按這張表建約束
CHECKED: dict[str, dict[str, tuple]] = {
    "commodity": {"category": COMMODITY_CATEGORIES},
    "data_source": {"kind": SOURCE_KINDS, "status": SOURCE_STATUSES},
    "series": {"kind": SERIES_KINDS, "frequency": FREQUENCIES, "module": MODULES},
    "series_alias": {"origin": ALIAS_ORIGINS},
    "import_batch": {"status": IMPORT_STATUSES},
    "event": {"category": EVENT_CATEGORIES, "status": EVENT_STATUSES, "origin": EVENT_ORIGINS},
    "report": {"kind": REPORT_KINDS},
    "backtest_run": {"status": BACKTEST_STATUSES, "mode": BACKTEST_MODES},
    "user_account": {"role": ROLES},
    "job_run": {"trigger": JOB_TRIGGERS, "status": JOB_STATUSES},
    "notification": {"event": NOTIFICATION_EVENTS},
    "external_call": {"status": EXTERNAL_CALL_STATUSES},
    "llm_call": {"purpose": LLM_PURPOSES, "status": LLM_CALL_STATUSES},
}

# 數值範圍約束：表 → (約束名後綴, SQL 條件)
RANGES: dict[str, list[tuple[str, str]]] = {
    "event": [("bias", f"bias BETWEEN {EVENT_BIAS[0]} AND {EVENT_BIAS[1]}"),
              ("probability", "probability IS NULL OR (probability >= 0 AND probability <= 1)")],
}


def in_list(column: str, values: tuple) -> str:
    """CHECK 條件：column IN ('a', 'b')。值都是代碼裡的常量（字母、數字、下劃線），不含引號。"""
    for value in values:
        if not isinstance(value, str) or not _PLAIN.fullmatch(value):
            raise ValueError(f"不安全的枚舉值：{value!r}")
    return f"{column} IN ({', '.join(repr(v) for v in values)})"


def constraint_name(table: str, column: str) -> str:
    return f"ck_{table}_{column}"
