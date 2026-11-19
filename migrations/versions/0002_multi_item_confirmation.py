"""Record explicit confirmation for multi-item order amounts."""
from alembic import op
import sqlalchemy as sa
revision = "0002_multi_item_confirmation"
down_revision = "0001_initial"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('import_batches',sa.Column('item_amounts_confirmed',sa.Boolean(),nullable=False,server_default=sa.false()))

def downgrade():
    with op.batch_alter_table('import_batches') as batch:
        batch.drop_column('item_amounts_confirmed')
