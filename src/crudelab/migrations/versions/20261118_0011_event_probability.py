"""event probability and premium; projection case event link

Revision ID: 0011
Revises: 0010
Create Date: 2026-11-18 12:00:00
"""
import sqlalchemy as sa
from alembic import op

revision = '0011'
down_revision = '0010'
branch_labels = None
depends_on = None


def upgrade() -> None:
    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.add_column(sa.Column('probability', sa.Float(), nullable=True))
        batch_op.add_column(sa.Column('price_premium', sa.Float(), nullable=True))
    with op.batch_alter_table('projection_case', schema=None) as batch_op:
        batch_op.add_column(sa.Column('event_id', sa.Integer(), nullable=True))
        batch_op.add_column(sa.Column('event_probability', sa.Float(), nullable=True))
        batch_op.create_foreign_key(batch_op.f('fk_projection_case_event_id_event'), 'event', ['event_id'], ['id'])


def downgrade() -> None:
    with op.batch_alter_table('projection_case', schema=None) as batch_op:
        batch_op.drop_constraint(batch_op.f('fk_projection_case_event_id_event'), type_='foreignkey')
        batch_op.drop_column('event_probability')
        batch_op.drop_column('event_id')
    with op.batch_alter_table('event', schema=None) as batch_op:
        batch_op.drop_column('price_premium')
        batch_op.drop_column('probability')
