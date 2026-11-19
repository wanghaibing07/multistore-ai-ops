"""Add Qianniu chat session analysis and customer-service knowledge tables."""
from alembic import op
import sqlalchemy as sa

revision = '0010_qianniu_chat_analysis'
down_revision = '0009_card_ledger_and_settings'
branch_labels = None
depends_on = None


def upgrade():
    op.create_table(
        'chat_sessions',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('store_id', sa.Integer(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('session_hash', sa.String(64), nullable=False),
        sa.Column('buyer_nick', sa.String(120), nullable=False, server_default=''),
        sa.Column('order_external_id', sa.String(128), nullable=True),
        sa.Column('product_category', sa.String(40), nullable=False, server_default='other'),
        sa.Column('stage', sa.String(30), nullable=False, server_default='presale'),
        sa.Column('primary_topic', sa.String(50), nullable=False, server_default='general'),
        sa.Column('topics', sa.JSON(), nullable=False),
        sa.Column('demand_signals', sa.JSON(), nullable=False),
        sa.Column('sentiment', sa.String(20), nullable=False, server_default='neutral'),
        sa.Column('resolved', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('summary', sa.String(300), nullable=False, server_default=''),
        sa.Column('buyer_questions', sa.JSON(), nullable=False),
        sa.Column('seller_replies', sa.JSON(), nullable=False),
        sa.Column('messages', sa.JSON(), nullable=False),
        sa.Column('message_count', sa.Integer(), nullable=False, server_default='0'),
        sa.Column('started_at', sa.String(30), nullable=False, server_default=''),
        sa.Column('last_message_at', sa.String(30), nullable=False, server_default=''),
        sa.Column('source', sa.String(40), nullable=False, server_default='paste'),
        sa.Column('created_at', sa.String(40), nullable=False),
        sa.UniqueConstraint('store_id', 'session_hash', name='uq_chat_session_hash'),
    )
    op.create_index('ix_chat_sessions_store_id', 'chat_sessions', ['store_id'], unique=False)
    op.create_index('ix_chat_sessions_order_external_id', 'chat_sessions', ['order_external_id'], unique=False)
    op.create_index('ix_chat_sessions_product_category', 'chat_sessions', ['product_category'], unique=False)
    op.create_index('ix_chat_sessions_stage', 'chat_sessions', ['stage'], unique=False)
    op.create_index('ix_chat_sessions_primary_topic', 'chat_sessions', ['primary_topic'], unique=False)
    op.create_index('ix_chat_sessions_last_message_at', 'chat_sessions', ['last_message_at'], unique=False)

    op.create_table(
        'chat_knowledge_items',
        sa.Column('id', sa.Integer(), primary_key=True),
        sa.Column('store_id', sa.Integer(), sa.ForeignKey('stores.id'), nullable=False),
        sa.Column('topic', sa.String(50), nullable=False),
        sa.Column('product_category', sa.String(40), nullable=False, server_default='all'),
        sa.Column('title', sa.String(160), nullable=False),
        sa.Column('trigger_question', sa.String(300), nullable=False, server_default=''),
        sa.Column('standard_reply', sa.Text(), nullable=False),
        sa.Column('source_count', sa.Integer(), nullable=False, server_default='1'),
        sa.Column('is_pinned', sa.Boolean(), nullable=False, server_default=sa.text('0')),
        sa.Column('updated_at', sa.String(40), nullable=False),
    )
    op.create_index('ix_chat_knowledge_items_store_id', 'chat_knowledge_items', ['store_id'], unique=False)
    op.create_index('ix_chat_knowledge_items_topic', 'chat_knowledge_items', ['topic'], unique=False)


def downgrade():
    op.drop_index('ix_chat_knowledge_items_topic', table_name='chat_knowledge_items')
    op.drop_index('ix_chat_knowledge_items_store_id', table_name='chat_knowledge_items')
    op.drop_table('chat_knowledge_items')

    op.drop_index('ix_chat_sessions_last_message_at', table_name='chat_sessions')
    op.drop_index('ix_chat_sessions_primary_topic', table_name='chat_sessions')
    op.drop_index('ix_chat_sessions_stage', table_name='chat_sessions')
    op.drop_index('ix_chat_sessions_product_category', table_name='chat_sessions')
    op.drop_index('ix_chat_sessions_order_external_id', table_name='chat_sessions')
    op.drop_index('ix_chat_sessions_store_id', table_name='chat_sessions')
    op.drop_table('chat_sessions')
