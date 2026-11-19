"""Idempotency keys for manually entered adjustments."""
from alembic import op
import sqlalchemy as sa
revision = "0003_adjustment_idempotency"
down_revision = "0002_multi_item_confirmation"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('adjustments',sa.Column('request_key',sa.String(36),nullable=True))
    op.create_index('uq_adjustments_store_request','adjustments',['store_id','request_key'],unique=True)

def downgrade():
    op.drop_index('uq_adjustments_store_request',table_name='adjustments')
    with op.batch_alter_table('adjustments') as batch:batch.drop_column('request_key')
