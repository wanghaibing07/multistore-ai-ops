"""Add multi-channel order support, FX metadata, and deterministic upstream links."""
from alembic import op
import sqlalchemy as sa

revision = "0012_multi_channel_upstreams"
down_revision = "0011_order_fulfillment_status"
branch_labels = None
depends_on = None


def upgrade():
    op.add_column(
        "import_batches",
        sa.Column("channel", sa.String(length=20), nullable=False, server_default="qianniu"),
    )
    op.add_column(
        "order_items",
        sa.Column("channel", sa.String(length=20), nullable=False, server_default="qianniu"),
    )
    op.create_index("ix_order_items_channel", "order_items", ["channel"])

    with op.batch_alter_table("card_purchases") as batch_op:
        batch_op.drop_constraint("uq_card_purchase_source", type_="unique")
        batch_op.add_column(sa.Column("original_currency", sa.String(length=12), nullable=False, server_default="CNY"))
        batch_op.add_column(sa.Column("original_amount", sa.String(length=32), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("fx_rate", sa.String(length=20), nullable=False, server_default="1"))
        batch_op.add_column(sa.Column("linked_order_external_id", sa.String(length=128), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("linked_channel", sa.String(length=20), nullable=False, server_default=""))
        batch_op.add_column(sa.Column("linked_redeem_code", sa.String(length=64), nullable=False, server_default=""))
        batch_op.create_unique_constraint(
            "uq_card_purchase_source",
            ["store_id", "source", "purchase_external_id"],
        )
    op.create_index("ix_card_purchases_linked_order_external_id", "card_purchases", ["linked_order_external_id"])

    op.add_column(
        "store_settings",
        sa.Column("usd_cny_rate", sa.String(length=20), nullable=False, server_default="7.30"),
    )
    op.add_column(
        "store_settings",
        sa.Column("vnd_cny_rate", sa.String(length=20), nullable=False, server_default="0.00028"),
    )


def downgrade():
    with op.batch_alter_table("store_settings") as batch_op:
        batch_op.drop_column("vnd_cny_rate")
        batch_op.drop_column("usd_cny_rate")

    op.drop_index("ix_card_purchases_linked_order_external_id", table_name="card_purchases")
    with op.batch_alter_table("card_purchases") as batch_op:
        batch_op.drop_constraint("uq_card_purchase_source", type_="unique")
        batch_op.drop_column("linked_redeem_code")
        batch_op.drop_column("linked_channel")
        batch_op.drop_column("linked_order_external_id")
        batch_op.drop_column("fx_rate")
        batch_op.drop_column("original_amount")
        batch_op.drop_column("original_currency")
        batch_op.create_unique_constraint(
            "uq_card_purchase_source",
            ["store_id", "purchase_external_id"],
        )

    op.drop_index("ix_order_items_channel", table_name="order_items")
    with op.batch_alter_table("order_items") as batch_op:
        batch_op.drop_column("channel")
    with op.batch_alter_table("import_batches") as batch_op:
        batch_op.drop_column("channel")
