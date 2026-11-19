from __future__ import annotations

from datetime import datetime, timezone
from sqlalchemy import Boolean, ForeignKey, Integer, JSON, String, UniqueConstraint, Text, Index
from sqlalchemy.orm import Mapped, mapped_column
from .db import Base

def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")

class Store(Base):
    __tablename__ = "stores"
    id: Mapped[int] = mapped_column(primary_key=True)
    name: Mapped[str] = mapped_column(String(120))

class User(Base):
    __tablename__ = "users"
    id: Mapped[int] = mapped_column(primary_key=True)
    username: Mapped[str] = mapped_column(String(100), unique=True)
    password_hash: Mapped[str] = mapped_column(String(256))
    is_admin: Mapped[bool] = mapped_column(Boolean, default=False)

class Membership(Base):
    __tablename__ = "memberships"
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"), primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), primary_key=True)

class LoginSession(Base):
    __tablename__ = "login_sessions"
    token_hash: Mapped[str] = mapped_column(String(64), primary_key=True)
    csrf_hash: Mapped[str] = mapped_column(String(64))
    user_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    expires_at: Mapped[str] = mapped_column(String(40))

class ImportBatch(Base):
    __tablename__ = "import_batches"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    kind: Mapped[str] = mapped_column(String(20))
    file_hash: Mapped[str] = mapped_column(String(64))
    basis: Mapped[str | None] = mapped_column(String(20), nullable=True)
    parser_version: Mapped[str] = mapped_column(String(20), default="2")
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    status: Mapped[str] = mapped_column(String(20), default="preview")
    row_count: Mapped[int] = mapped_column(Integer, default=0)
    item_amounts_confirmed: Mapped[bool] = mapped_column(Boolean, default=False)
    committed_at: Mapped[str | None] = mapped_column(String(40), nullable=True)

class ImportRow(Base):
    __tablename__ = "import_rows"
    id: Mapped[int] = mapped_column(primary_key=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"), index=True)
    row_number: Mapped[int] = mapped_column(Integer)
    data: Mapped[dict] = mapped_column(JSON)
    raw_data: Mapped[dict | None] = mapped_column(JSON, nullable=True)

class CostVersion(Base):
    __tablename__ = "cost_versions"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    sku: Mapped[str] = mapped_column(String(500))
    valid_from: Mapped[str] = mapped_column(String(19))
    unit_cost_fen: Mapped[int] = mapped_column(Integer)
    supplier: Mapped[str] = mapped_column(String(120), default="")
    batch_id: Mapped[int | None] = mapped_column(ForeignKey("import_batches.id"), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    __table_args__ = (UniqueConstraint("store_id", "sku", "valid_from"),)

class OrderItem(Base):
    __tablename__ = "order_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    item_external_id: Mapped[str] = mapped_column(String(128))
    order_external_id: Mapped[str] = mapped_column(String(128))
    sku: Mapped[str] = mapped_column(String(500), index=True)
    product_title: Mapped[str] = mapped_column(String(300), default="")
    quantity: Mapped[int] = mapped_column(Integer)
    paid_at: Mapped[str | None] = mapped_column(String(19), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(30))
    payment_fen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    payment_basis: Mapped[str] = mapped_column(String(10))
    refund_status: Mapped[str] = mapped_column(String(20))
    refund_snapshot_fen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_fen: Mapped[int | None] = mapped_column(Integer, nullable=True)
    cost_version_id: Mapped[int | None] = mapped_column(ForeignKey("cost_versions.id"), nullable=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"))
    __table_args__ = (UniqueConstraint("store_id", "item_external_id"),)

class Refund(Base):
    __tablename__ = "refunds"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    external_id: Mapped[str] = mapped_column(String(128))
    item_external_id: Mapped[str] = mapped_column(String(128), index=True)
    status: Mapped[str] = mapped_column(String(20))
    amount_fen: Mapped[int] = mapped_column(Integer)
    completed_at: Mapped[str | None] = mapped_column(String(19), nullable=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"))
    __table_args__ = (UniqueConstraint("store_id", "external_id"),)

class Adjustment(Base):
    __tablename__ = "adjustments"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    item_external_id: Mapped[str] = mapped_column(String(128), index=True)
    kind: Mapped[str] = mapped_column(String(30))
    amount_fen: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(200), default="")
    request_key: Mapped[str | None] = mapped_column(String(36), nullable=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    __table_args__ = (Index("uq_adjustments_store_request", "store_id", "request_key", unique=True),)

class RefundReview(Base):
    __tablename__ = "refund_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    item_external_id: Mapped[str] = mapped_column(String(128), index=True)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    refund_fen: Mapped[int] = mapped_column(Integer)
    note: Mapped[str] = mapped_column(String(200))
    request_key: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    __table_args__ = (UniqueConstraint("store_id", "request_key", name="uq_refund_review_request"),)

class PaymentBasisReview(Base):
    __tablename__ = "payment_basis_reviews"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    item_external_id: Mapped[str] = mapped_column(String(128), index=True)
    reviewer_id: Mapped[int] = mapped_column(ForeignKey("users.id"))
    source_fingerprint: Mapped[str] = mapped_column(String(64))
    basis: Mapped[str] = mapped_column(String(10))
    note: Mapped[str] = mapped_column(String(200))
    request_key: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    __table_args__ = (UniqueConstraint("store_id", "request_key", name="uq_payment_basis_review_request"),)

class Cashflow(Base):
    __tablename__ = "cashflows"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    business_flow: Mapped[str] = mapped_column(String(128))
    order_external_id: Mapped[str] = mapped_column(String(128))
    item_external_id: Mapped[str] = mapped_column(String(128))
    refund_external_id: Mapped[str] = mapped_column(String(128), default="")
    kind: Mapped[str] = mapped_column(String(12))
    amount_fen: Mapped[int] = mapped_column(Integer)
    settled_at: Mapped[str] = mapped_column(String(19), index=True)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"))
    __table_args__ = (UniqueConstraint("store_id","business_flow","order_external_id","item_external_id","refund_external_id","kind",name="uq_cashflow_source"),)

class CardPurchase(Base):
    __tablename__ = "card_purchases"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    purchase_external_id: Mapped[str] = mapped_column(String(128))
    product_title: Mapped[str] = mapped_column(String(500), default="")
    seller: Mapped[str] = mapped_column(String(200), default="")
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    amount_fen: Mapped[int] = mapped_column(Integer)
    refund_fen: Mapped[int] = mapped_column(Integer, default=0)
    paid_at: Mapped[str | None] = mapped_column(String(19), nullable=True, index=True)
    status: Mapped[str] = mapped_column(String(40), default="")
    source: Mapped[str] = mapped_column(String(80), default="16688")
    raw_data: Mapped[dict] = mapped_column(JSON)
    batch_id: Mapped[int] = mapped_column(ForeignKey("import_batches.id"))
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    __table_args__ = (UniqueConstraint("store_id", "purchase_external_id", name="uq_card_purchase_source"),)

class CardMatch(Base):
    __tablename__ = "card_matches"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    item_external_id: Mapped[str] = mapped_column(String(128), index=True)
    purchase_id: Mapped[int] = mapped_column(ForeignKey("card_purchases.id"), index=True)
    score: Mapped[int] = mapped_column(Integer)
    allocated_quantity: Mapped[int] = mapped_column(Integer, default=1)
    status: Mapped[str] = mapped_column(String(20), default="confirmed")
    evidence: Mapped[dict] = mapped_column(JSON)
    metadata_json: Mapped[dict] = mapped_column("metadata", JSON)
    request_key: Mapped[str] = mapped_column(String(36))
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    __table_args__ = (
        UniqueConstraint("store_id", "request_key", name="uq_card_match_request"),
        UniqueConstraint("store_id", "item_external_id", "purchase_id", name="uq_card_match_pair"),
    )

class StoreSetting(Base):
    __tablename__ = "store_settings"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"))
    contact_email: Mapped[str] = mapped_column(String(200), default="")
    updated_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    __table_args__ = (
        UniqueConstraint("store_id", name="uq_store_settings_store_id"),
        Index("ix_store_settings_store_id", "store_id", unique=True),
    )

class AgentRun(Base):
    __tablename__ = "agent_runs"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    model: Mapped[str] = mapped_column(String(120))
    status: Mapped[str] = mapped_column(String(30))
    trace: Mapped[dict] = mapped_column(JSON)

class ChatSession(Base):
    __tablename__ = "chat_sessions"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    session_hash: Mapped[str] = mapped_column(String(64), nullable=False)
    buyer_nick: Mapped[str] = mapped_column(String(120), default="")
    order_external_id: Mapped[str | None] = mapped_column(String(128), nullable=True, index=True)
    product_category: Mapped[str] = mapped_column(String(40), default="other", index=True)
    stage: Mapped[str] = mapped_column(String(30), default="presale", index=True)
    primary_topic: Mapped[str] = mapped_column(String(50), default="general", index=True)
    topics: Mapped[list] = mapped_column(JSON, default=list)
    demand_signals: Mapped[list] = mapped_column(JSON, default=list)
    sentiment: Mapped[str] = mapped_column(String(20), default="neutral")
    resolved: Mapped[bool] = mapped_column(Boolean, default=False)
    summary: Mapped[str] = mapped_column(String(300), default="")
    buyer_questions: Mapped[list] = mapped_column(JSON, default=list)
    seller_replies: Mapped[list] = mapped_column(JSON, default=list)
    messages: Mapped[list] = mapped_column(JSON, default=list)
    message_count: Mapped[int] = mapped_column(Integer, default=0)
    started_at: Mapped[str] = mapped_column(String(30), default="")
    last_message_at: Mapped[str] = mapped_column(String(30), default="", index=True)
    source: Mapped[str] = mapped_column(String(40), default="paste")
    created_at: Mapped[str] = mapped_column(String(40), default=now_iso)
    __table_args__ = (UniqueConstraint("store_id", "session_hash", name="uq_chat_session_hash"),)

class ChatKnowledgeItem(Base):
    __tablename__ = "chat_knowledge_items"
    id: Mapped[int] = mapped_column(primary_key=True)
    store_id: Mapped[int] = mapped_column(ForeignKey("stores.id"), index=True)
    topic: Mapped[str] = mapped_column(String(50), index=True)
    product_category: Mapped[str] = mapped_column(String(40), default="all")
    title: Mapped[str] = mapped_column(String(160))
    trigger_question: Mapped[str] = mapped_column(String(300), default="")
    standard_reply: Mapped[str] = mapped_column(Text)
    source_count: Mapped[int] = mapped_column(Integer, default=1)
    is_pinned: Mapped[bool] = mapped_column(Boolean, default=False)
    updated_at: Mapped[str] = mapped_column(String(40), default=now_iso)
