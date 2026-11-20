"""數據字典：從 models.py 自動生成 docs/data-model.md（crudelab data-model --output docs/data-model.md）。

每張表的用途（類的說明）、每一列的類型、可否為空、主鍵 / 外鍵 / 唯一 / 取值約束、說明。
說明的來源，按優先順序：
  1. models.py 裡這一列行尾的註釋
  2. 常見列名的統一含義（GLOSSARY）
  3. 外鍵：自動寫「→ 表.列」
  4. 有取值約束的列：自動列出允許的值
tests/test_data_model.py 檢查：文檔和模型一致（改了模型忘了重新生成會失敗）、每張表有說明、每一列都有說明。
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import sqlalchemy as sa

from . import domain, models

GLOSSARY = {
    "id": "自增主鍵", "code": "代碼（主鍵，來自配置）", "key": "鍵", "name_zh": "中文名稱", "name": "名稱",
    "title": "標題", "description": "說明", "notes": "備註", "note": "備註", "summary": "摘要",
    "created_at": "記錄建立時間（UTC）", "created_by": "建立者（登錄用戶名；未開登錄時為 local）",
    "updated_at": "最後修改時間（UTC）", "updated_by": "最後修改者", "archived_at": "歸檔時間（UTC；空 = 未歸檔）",
    "removed_at": "撤銷時間（UTC；空 = 有效）", "started_at": "開始時間（UTC）",
    "finished_at": "結束時間（UTC；空 = 未結束）",
    "first_seen_at": "第一次收到的時間（UTC）", "at": "時間（UTC）", "ts": "時間（UTC）",
    "active": "是否仍在配置裡（移除的置為 false，數據保留）", "message": "結果說明（已脫敏）",
    "detail": "詳細信息（JSON）",
    "obs_date": "數據日期（業務時間：這個值說的是哪一天）", "value": "數值", "source_ref": "來源裡的位置（工作表、列）",
    "start_date": "開始日期", "end_date": "結束日期", "source": "來源", "category": "類別", "kind": "類型",
    "status": "狀態", "provider": "大模型供應商", "model": "模型名", "job_code": "定時任務代碼（schedule.yaml）",
    "results": "各渠道的發送結果 {渠道: ok 或原因}", "body": "正文", "url": "原文網址", "content": "正文",
    "tool": "調用的工具名", "day": "日期", "region": "地區", "dimension": "量綱", "priority": "優先級（越小越優先）",
}


def _comments() -> dict[tuple[str, str], str]:
    """(表, 列) → models.py 裡那一列行尾的註釋。"""
    source = inspect.getsource(models)
    lines = source.splitlines()
    out: dict[tuple[str, str], str] = {}
    for node in ast.parse(source).body:
        if not isinstance(node, ast.ClassDef):
            continue
        table = next((str(n.value.value) for n in node.body if isinstance(n, ast.Assign)
                      and isinstance(n.value, ast.Constant) and getattr(n.targets[0], "id", "") == "__tablename__"),
                     None)
        if table is None:
            continue
        for n in node.body:
            if isinstance(n, ast.AnnAssign) and isinstance(n.target, ast.Name):
                line = lines[n.lineno - 1]
                tail = line.split("mapped_column", 1)[-1]
                if "#" in tail:
                    out[(table, n.target.id)] = tail.split("#", 1)[1].strip()
    renamed = {}                                         # 屬性名和列名不同的（如 group → grp）：換成列名
    for mapper in models.Base.registry.mappers:
        name = _table_name(mapper)
        for attr in mapper.column_attrs:
            column = attr.columns[0]
            if attr.key != column.name and (name, attr.key) in out:
                renamed[(name, column.name)] = out[(name, attr.key)]
    return {**out, **renamed}


def _table_name(mapper) -> str:
    table = mapper.local_table
    assert isinstance(table, sa.Table)
    return table.name


def _table_docs() -> dict[str, str]:
    out = {}
    for mapper in models.Base.registry.mappers:
        doc = inspect.getdoc(mapper.class_)
        if doc and doc != inspect.getdoc(models.Base):
            out[_table_name(mapper)] = " ".join(doc.split())
    return out


def describe(table: sa.Table, column: sa.Column, comments: dict) -> str:
    parts = []
    allowed = domain.CHECKED.get(table.name, {}).get(column.name)
    text = comments.get((table.name, column.name))
    if allowed and text and set(text.replace("/", " ").split()) <= set(allowed):
        text = None                                      # 註釋只是在列舉取值：下面會自動列出，不重複
    text = text or GLOSSARY.get(column.name)
    if text:
        parts.append(text)
    for fk in column.foreign_keys:
        parts.append(f"→ {fk.target_fullname}")
    if allowed:
        parts.append("取值：" + " / ".join(allowed))
    return "；".join(parts)


def missing_descriptions() -> list[str]:
    comments = _comments()
    return [f"{t.name}.{c.name}" for t in models.Base.metadata.sorted_tables for c in t.columns
            if not describe(t, c, comments)]


def tables_without_docs() -> list[str]:
    docs = _table_docs()
    return [t for t in models.Base.metadata.tables if t not in docs]


def render() -> str:
    comments, docs = _comments(), _table_docs()
    head = inspect.getdoc(models) or ""
    lines = ["# 數據字典", "",
             "由 `crudelab data-model --output docs/data-model.md` 從 `src/crudelab/models.py` 生成，不要手改。",
             "表結構的變更一律通過遷移腳本（`src/crudelab/migrations/versions/`）。", "",
             "## 總覽", "", "```", head, "```", "",
             f"共 {len(models.Base.metadata.tables)} 張表。取值約束由 `src/crudelab/domain.py` 統一定義。", ""]
    for table in sorted(models.Base.metadata.tables.values(), key=lambda t: t.name):
        lines += [f"## {table.name}", "", docs.get(table.name, ""), "",
                  "| 列 | 類型 | 可空 | 鍵 | 說明 |", "|---|---|---|---|---|"]
        for column in table.columns:
            keys = []
            if column.primary_key:
                keys.append("PK")
            if column.foreign_keys:
                keys.append("FK")
            if column.unique:
                keys.append("UQ")
            if column.index:
                keys.append("IX")
            lines.append(f"| `{column.name}` | {column.type} | {'是' if column.nullable else '否'} | {' '.join(keys)} "
                         f"| {describe(table, column, comments).replace('|', '/')} |")
        extra = []
        for constraint in sorted(table.constraints, key=lambda c: str(c.name)):
            if isinstance(constraint, sa.UniqueConstraint) and len(constraint.columns) > 1:
                extra.append(f"- 唯一：{', '.join(c.name for c in constraint.columns)}")
            elif isinstance(constraint, sa.CheckConstraint) and str(constraint.name or "").startswith("ck_"):
                rule = str(constraint.sqltext)
                if " IN (" not in rule:                     # 取值列表已寫在列說明裡，這裡只列範圍類約束
                    extra.append(f"- 約束：`{rule}`")
        for index in sorted(table.indexes, key=lambda i: str(i.name)):
            if len(index.columns) > 1:
                extra.append(f"- 索引：{', '.join(c.name for c in index.columns)}")
        lines += ["", *extra, ""] if extra else [""]
    return "\n".join(lines).rstrip() + "\n"


def write(path: Path) -> None:
    path.write_text(render(), encoding="utf-8")
