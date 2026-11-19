"""Record parser version for reproducible reimports."""
from alembic import op
import sqlalchemy as sa
revision = "0004_parser_version"
down_revision = "0003_adjustment_idempotency"
branch_labels = None
depends_on = None

def upgrade():
    op.add_column('import_batches',sa.Column('parser_version',sa.String(20),nullable=False,server_default='1'))

def downgrade():
    with op.batch_alter_table('import_batches') as batch:batch.drop_column('parser_version')
