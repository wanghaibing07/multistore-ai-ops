"""Record order-level payment basis evidence."""
from alembic import op
import sqlalchemy as sa

revision = '0007_payment_basis_reviews'
down_revision = '0006_cashflows'
branch_labels = None
depends_on = None

def upgrade():
    op.create_table('payment_basis_reviews',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('store_id', sa.Integer(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('item_external_id', sa.String(128), nullable=False),
        sa.Column('reviewer_id', sa.Integer(), sa.ForeignKey('users.id'), nullable=False),
        sa.Column('source_fingerprint', sa.String(64), nullable=False),
        sa.Column('basis', sa.String(10), nullable=False),
        sa.Column('note', sa.String(200), nullable=False),
        sa.Column('request_key', sa.String(36), nullable=False),
        sa.Column('created_at', sa.String(40), nullable=False),
        sa.UniqueConstraint('store_id', 'request_key', name='uq_payment_basis_review_request'))
    op.create_index('ix_payment_basis_reviews_store_id', 'payment_basis_reviews', ['store_id'])
    op.create_index('ix_payment_basis_reviews_item_external_id', 'payment_basis_reviews', ['item_external_id'])

def downgrade():
    op.drop_index('ix_payment_basis_reviews_item_external_id', table_name='payment_basis_reviews')
    op.drop_index('ix_payment_basis_reviews_store_id', table_name='payment_basis_reviews')
    op.drop_table('payment_basis_reviews')
