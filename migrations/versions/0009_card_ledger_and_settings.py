"""Add store settings and allow multi-unit card match allocation."""
from alembic import op
import sqlalchemy as sa

revision = '0009_card_ledger_and_settings'
down_revision = '0008_raw_and_card_matching'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'store_settings',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('store_id', sa.Integer(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('contact_email', sa.String(200), nullable=False, server_default=''),
        sa.Column('updated_at', sa.String(40), nullable=False),
        sa.UniqueConstraint('store_id', name='uq_store_settings_store_id'),
    )
    op.create_index('ix_store_settings_store_id', 'store_settings', ['store_id'], unique=True)

    with op.batch_alter_table('card_matches') as batch_op:
        batch_op.drop_constraint('uq_card_match_purchase', type_='unique')
        batch_op.add_column(sa.Column('allocated_quantity', sa.Integer(), nullable=False, server_default='1'))


def downgrade():
    with op.batch_alter_table('card_matches') as batch_op:
        batch_op.drop_column('allocated_quantity')
        batch_op.create_unique_constraint('uq_card_match_purchase', ['store_id', 'purchase_id'])

    op.drop_index('ix_store_settings_store_id', table_name='store_settings')
    op.drop_table('store_settings')
