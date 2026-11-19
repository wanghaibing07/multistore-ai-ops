"""Preserve source rows and add card purchase reconciliation."""
from alembic import op
import sqlalchemy as sa

revision = '0008_raw_and_card_matching'
down_revision = '0007_payment_basis_reviews'
branch_labels = None
depends_on = None


def upgrade():
    op.add_column('import_rows', sa.Column('raw_data', sa.JSON(), nullable=True))
    op.create_table(
        'card_purchases',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('store_id', sa.Integer(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('purchase_external_id', sa.String(128), nullable=False),
        sa.Column('product_title', sa.String(500), nullable=False),
        sa.Column('seller', sa.String(200), nullable=False),
        sa.Column('quantity', sa.Integer(), nullable=False),
        sa.Column('amount_fen', sa.Integer(), nullable=False),
        sa.Column('refund_fen', sa.Integer(), nullable=False),
        sa.Column('paid_at', sa.String(19), nullable=True),
        sa.Column('status', sa.String(40), nullable=False),
        sa.Column('source', sa.String(80), nullable=False),
        sa.Column('raw_data', sa.JSON(), nullable=False),
        sa.Column('batch_id', sa.Integer(), sa.ForeignKey('import_batches.id'), nullable=False),
        sa.Column('created_at', sa.String(40), nullable=False),
        sa.UniqueConstraint('store_id', 'purchase_external_id', name='uq_card_purchase_source'),
    )
    op.create_index('ix_card_purchases_store_id', 'card_purchases', ['store_id'])
    op.create_index('ix_card_purchases_paid_at', 'card_purchases', ['paid_at'])
    op.create_table(
        'card_matches',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('store_id', sa.Integer(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('item_external_id', sa.String(128), nullable=False),
        sa.Column('purchase_id', sa.Integer(), sa.ForeignKey('card_purchases.id'), nullable=False),
        sa.Column('score', sa.Integer(), nullable=False),
        sa.Column('status', sa.String(20), nullable=False),
        sa.Column('evidence', sa.JSON(), nullable=False),
        sa.Column('metadata', sa.JSON(), nullable=False),
        sa.Column('request_key', sa.String(36), nullable=False),
        sa.Column('created_at', sa.String(40), nullable=False),
        sa.UniqueConstraint('store_id', 'request_key', name='uq_card_match_request'),
        sa.UniqueConstraint('store_id', 'item_external_id', 'purchase_id', name='uq_card_match_pair'),
        sa.UniqueConstraint('store_id', 'purchase_id', name='uq_card_match_purchase'),
    )
    op.create_index('ix_card_matches_store_id', 'card_matches', ['store_id'])
    op.create_index('ix_card_matches_item_external_id', 'card_matches', ['item_external_id'])
    op.create_index('ix_card_matches_purchase_id', 'card_matches', ['purchase_id'])


def downgrade():
    op.drop_index('ix_card_matches_purchase_id', table_name='card_matches')
    op.drop_index('ix_card_matches_item_external_id', table_name='card_matches')
    op.drop_index('ix_card_matches_store_id', table_name='card_matches')
    op.drop_table('card_matches')
    op.drop_index('ix_card_purchases_paid_at', table_name='card_purchases')
    op.drop_index('ix_card_purchases_store_id', table_name='card_purchases')
    op.drop_table('card_purchases')
    op.drop_column('import_rows', 'raw_data')
