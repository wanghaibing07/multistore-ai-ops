"""Store shipping state separately from the transaction state."""
from alembic import op
import sqlalchemy as sa

revision = "0011_order_fulfillment_status"
down_revision = "0010_qianniu_chat_analysis"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "order_items",
        sa.Column("fulfillment_status", sa.String(length=20), nullable=False, server_default="unknown"),
    )


def downgrade():
    with op.batch_alter_table("order_items") as batch:
        batch.drop_column("fulfillment_status")
