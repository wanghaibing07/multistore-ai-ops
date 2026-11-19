"""Store goods-income bill records separately from order profit."""
from alembic import op
import sqlalchemy as sa

revision='0006_cashflows'
down_revision='0005_refund_reviews'
branch_labels=None
depends_on=None

def upgrade():
    op.create_table('cashflows',
        sa.Column('id',sa.Integer(),primary_key=True),
        sa.Column('store_id',sa.Integer(),sa.ForeignKey('stores.id'),nullable=False),
        sa.Column('business_flow',sa.String(128),nullable=False),
        sa.Column('order_external_id',sa.String(128),nullable=False),
        sa.Column('item_external_id',sa.String(128),nullable=False),
        sa.Column('refund_external_id',sa.String(128),nullable=False),
        sa.Column('kind',sa.String(12),nullable=False),
        sa.Column('amount_fen',sa.Integer(),nullable=False),
        sa.Column('settled_at',sa.String(19),nullable=False),
        sa.Column('batch_id',sa.Integer(),sa.ForeignKey('import_batches.id'),nullable=False),
        sa.UniqueConstraint('store_id','business_flow','order_external_id','item_external_id','refund_external_id','kind',name='uq_cashflow_source'))
    op.create_index('ix_cashflows_store_id','cashflows',['store_id'])
    op.create_index('ix_cashflows_settled_at','cashflows',['settled_at'])

def downgrade():
    op.drop_index('ix_cashflows_settled_at',table_name='cashflows')
    op.drop_index('ix_cashflows_store_id',table_name='cashflows')
    op.drop_table('cashflows')
