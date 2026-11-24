"""品種類別加 rates（利率）：美債收益率等宏觀序列（#17）

只放寬 commodity.category 的取值約束（多允許一個值），不改已有數據。
降級時如果已經有 rates 類的品種，停下並說明（數據不變），不會為了收緊約束去刪你的數據。

Revision ID: 0023
Revises: 0022
Create Date: 2026-11-24 12:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0023'
down_revision = '0022'
branch_labels = None
depends_on = None

NAME = "ck_commodity_category"
OLD = ("crude", "product", "chem", "freight", "fx", "fundamental", "metals")
NEW = OLD + ("rates",)


def _condition(values: tuple[str, ...]) -> str:
    return "category IN (" + ", ".join(f"'{v}'" for v in values) + ")"


def upgrade() -> None:
    with op.batch_alter_table("commodity") as batch:
        batch.drop_constraint(NAME, type_="check")
        batch.create_check_constraint(NAME, _condition(NEW))


def downgrade() -> None:
    n = op.get_bind().execute(sa.text("SELECT COUNT(*) FROM commodity WHERE category = 'rates'")).scalar()
    if n:
        raise RuntimeError(f"已有 {n} 個 rates 類的品種，沒有收緊約束（數據沒有改動）。要降級請先在配置裡刪掉它們")
    with op.batch_alter_table("commodity") as batch:
        batch.drop_constraint(NAME, type_="check")
        batch.create_check_constraint(NAME, _condition(OLD))
