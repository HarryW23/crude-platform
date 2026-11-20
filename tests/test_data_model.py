"""數據字典（docs/data-model.md）：和表定義一致，每張表、每一列都有說明，每張表都在總覽裡歸了組。"""
import inspect
import re

from crudelab import data_model, models
from crudelab.settings import PROJECT_ROOT


def test_document_is_up_to_date():
    path = PROJECT_ROOT / "docs" / "data-model.md"
    assert path.read_text(encoding="utf-8") == data_model.render(), \
        "表結構或說明改了：運行 crudelab data-model --output docs/data-model.md 重新生成"


def test_every_table_and_column_is_explained():
    assert data_model.tables_without_docs() == [], "這些表的類缺少說明（docstring）"
    assert data_model.missing_descriptions() == [], "這些列沒有說明：在 models.py 那一行末尾加註釋"


def test_every_table_is_grouped_in_the_overview():
    overview = inspect.getdoc(models).split("表按用途分組", 1)[1].split("\n\n", 1)[0]
    listed = set(re.findall(r"[a-z_]+", overview))
    assert set(models.Base.metadata.tables) <= listed, set(models.Base.metadata.tables) - listed
