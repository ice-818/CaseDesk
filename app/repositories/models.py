"""SQLAlchemy ORM models."""

from datetime import datetime

from sqlalchemy import (
    Boolean,
    CheckConstraint,
    DateTime,
    ForeignKey,
    Index,
    Integer,
    String,
    Text,
    UniqueConstraint,
    text,
)
from sqlalchemy.orm import DeclarativeBase, Mapped, mapped_column, relationship

from app.core.time import utcnow


class Base(DeclarativeBase):
    pass


class MerchantRow(Base):
    __tablename__ = "merchants"
    __table_args__ = (
        CheckConstraint("status IN ('active','disabled')", name="ck_merchants_status"),
    )

    merchant_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(String(16), default="active", server_default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class UserRow(Base):
    __tablename__ = "users"
    __table_args__ = (
        CheckConstraint("role IN ('buyer','service_agent','operator')", name="ck_users_role"),
        CheckConstraint("status IN ('active','disabled')", name="ck_users_status"),
        CheckConstraint(
            "role <> 'service_agent' OR merchant_id IS NOT NULL",
            name="ck_users_service_agent_merchant",
        ),
    )

    user_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    password_hash: Mapped[str] = mapped_column(String(128))
    role: Mapped[str] = mapped_column(String(16))
    merchant_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("merchants.merchant_id", name="fk_users_merchant_id_merchants", ondelete="RESTRICT"),
        nullable=True,
        index=True,
    )
    status: Mapped[str] = mapped_column(String(16), default="active")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    disabled_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class AuthSessionRow(Base):
    __tablename__ = "auth_session"
    __table_args__ = (
        Index("ix_auth_session_user_revoked", "user_id", "revoked"),
        Index("ix_auth_session_expires_at", "expires_at"),
        CheckConstraint("length(token_digest) = 64", name="ck_auth_session_digest"),
        CheckConstraint("expires_at > created_at", name="ck_auth_session_expiry"),
        CheckConstraint(
            "(revoked = false AND revoked_at IS NULL) OR (revoked = true AND revoked_at IS NOT NULL)",
            name="ck_auth_session_revocation",
        ),
    )

    token_digest: Mapped[str] = mapped_column(String(64), primary_key=True)
    user_id: Mapped[str] = mapped_column(ForeignKey("users.user_id", ondelete="RESTRICT"))
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True))
    revoked: Mapped[bool] = mapped_column(Boolean, default=False, server_default="false")
    revoked_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    created_via: Mapped[str] = mapped_column(String(16), default="http")
    created_ip: Mapped[str | None] = mapped_column(String(45), nullable=True)
    user_agent: Mapped[str | None] = mapped_column(String(512), nullable=True)


class OrderRow(Base):
    __tablename__ = "orders"
    __table_args__ = (Index("ix_orders_merchant_user", "merchant_id", "user_id"),)

    order_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("merchants.merchant_id", name="fk_orders_merchant_id_merchants", ondelete="RESTRICT"),
        index=True,
    )
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    status: Mapped[str] = mapped_column(String(32), index=True)
    total_cents: Mapped[int] = mapped_column(Integer)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    shipped_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    delivered_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)
    tracking_no: Mapped[str | None] = mapped_column(String(64), nullable=True)
    receiver_city: Mapped[str | None] = mapped_column(String(64), nullable=True)

    items: Mapped[list["OrderItemRow"]] = relationship(back_populates="order")


class OrderItemRow(Base):
    __tablename__ = "order_items"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    order_id: Mapped[str] = mapped_column(ForeignKey("orders.order_id"))
    sku: Mapped[str] = mapped_column(String(32))
    name: Mapped[str] = mapped_column(String(128))
    quantity: Mapped[int] = mapped_column(Integer)
    unit_price_cents: Mapped[int] = mapped_column(Integer)
    customized: Mapped[bool] = mapped_column(default=False)

    order: Mapped[OrderRow] = relationship(back_populates="items")


class ProductRow(Base):
    __tablename__ = "products"

    sku: Mapped[str] = mapped_column(String(32), primary_key=True)
    merchant_id: Mapped[str] = mapped_column(
        String(64),
        ForeignKey("merchants.merchant_id", name="fk_products_merchant_id_merchants", ondelete="RESTRICT"),
        index=True,
    )
    name: Mapped[str] = mapped_column(String(128))
    category: Mapped[str] = mapped_column(String(64), index=True)
    price_cents: Mapped[int] = mapped_column(Integer)
    inventory: Mapped[int] = mapped_column(Integer)
    tags: Mapped[str] = mapped_column(String(256), default="")
    description: Mapped[str] = mapped_column(Text, default="")


class RefundRow(Base):
    __tablename__ = "refunds"
    __table_args__ = (
        CheckConstraint(
            "status IN ('requested','approved','rejected','completed')",
            name="ck_refunds_status",
        ),
        Index(
            "uq_refunds_active_order",
            "order_id",
            unique=True,
            sqlite_where=text("status IN ('requested','approved')"),
            postgresql_where=text("status IN ('requested','approved')"),
        ),
    )

    refund_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32))
    amount_cents: Mapped[int] = mapped_column(Integer)
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReturnRow(Base):
    __tablename__ = "returns"
    __table_args__ = (
        CheckConstraint(
            "status IN ('requested','approved','rejected','completed')",
            name="ck_returns_status",
        ),
        Index(
            "uq_returns_active_order",
            "order_id",
            unique=True,
            sqlite_where=text("status IN ('requested','approved')"),
            postgresql_where=text("status IN ('requested','approved')"),
        ),
    )

    return_id: Mapped[str] = mapped_column(String(32), primary_key=True)
    order_id: Mapped[str] = mapped_column(String(32), index=True)
    status: Mapped[str] = mapped_column(String(32))
    reason: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class SessionRow(Base):
    __tablename__ = "sessions"
    __table_args__ = (
        CheckConstraint("channel IN ('platform','shop')", name="ck_sessions_channel"),
        CheckConstraint(
            "(channel = 'platform' AND merchant_id IS NULL) OR "
            "(channel = 'shop' AND merchant_id IS NOT NULL)",
            name="ck_sessions_channel_merchant",
        ),
        Index("ix_sessions_merchant_user", "merchant_id", "user_id"),
    )

    session_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    channel: Mapped[str] = mapped_column(String(16), default="platform", server_default="platform")
    merchant_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("merchants.merchant_id", name="fk_sessions_merchant_id_merchants", ondelete="RESTRICT"),
        nullable=True,
    )
    user_id: Mapped[str] = mapped_column(String(64), index=True)
    summary: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ReceptionRow(Base):
    """One service reception per chat session; history survives closure."""

    __tablename__ = "receptions"

    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id"), primary_key=True)
    status: Mapped[str] = mapped_column(String(16), default="active")
    end_reason: Mapped[str | None] = mapped_column(String(24), nullable=True)
    last_activity_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    closed_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True), nullable=True)


class DatasetVersionRow(Base):
    __tablename__ = "dataset_versions"
    __table_args__ = (UniqueConstraint("name", "version", name="uq_dataset_version"),)

    dataset_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    name: Mapped[str] = mapped_column(String(80), index=True)
    version: Mapped[str] = mapped_column(String(32))
    task: Mapped[str] = mapped_column(String(32))
    split: Mapped[str] = mapped_column(String(24))
    status: Mapped[str] = mapped_column(String(16), default="draft")
    content_hash: Mapped[str] = mapped_column(String(64))
    payload_json: Mapped[str] = mapped_column(Text)
    reviewer: Mapped[str | None] = mapped_column(String(80), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class EvaluationRunRow(Base):
    __tablename__ = "evaluation_runs"

    run_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    dataset_id: Mapped[str] = mapped_column(ForeignKey("dataset_versions.dataset_id"), index=True)
    candidate_label: Mapped[str] = mapped_column(String(80))
    status: Mapped[str] = mapped_column(String(16), default="running")
    manifest_json: Mapped[str] = mapped_column(Text)
    report_json: Mapped[str] = mapped_column(Text, default="{}")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ShoppingCheckpointRow(Base):
    __tablename__ = "shopping_checkpoints"

    checkpoint_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id"), index=True)
    user_id: Mapped[str] = mapped_column(String(64))
    status: Mapped[str] = mapped_column(String(20), default="running")
    snapshot_json: Mapped[str] = mapped_column(Text)
    parent_id: Mapped[str | None] = mapped_column(String(64), nullable=True)
    updated_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ShoppingTaskRow(Base):
    __tablename__ = "shopping_tasks"

    session_id: Mapped[str] = mapped_column(ForeignKey("sessions.session_id"), primary_key=True)
    requirements_json: Mapped[str] = mapped_column(Text)


class ReceptionFeedbackRow(Base):
    __tablename__ = "reception_feedback"

    session_id: Mapped[str] = mapped_column(ForeignKey("receptions.session_id"), primary_key=True)
    score: Mapped[int] = mapped_column(Integer)
    resolved: Mapped[bool] = mapped_column()
    reason: Mapped[str] = mapped_column(Text, default="")
    last_assistant_message_id: Mapped[int | None] = mapped_column(
        ForeignKey("messages.id"), nullable=True
    )
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class MessageRow(Base):
    __tablename__ = "messages"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    session_id: Mapped[str] = mapped_column(String(64), index=True)
    role: Mapped[str] = mapped_column(String(16))
    content: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class AuditLogRow(Base):
    __tablename__ = "audit_logs"

    id: Mapped[int] = mapped_column(Integer, primary_key=True, autoincrement=True)
    trace_id: Mapped[str] = mapped_column(String(64), index=True)
    action: Mapped[str] = mapped_column(String(64))
    payload: Mapped[str] = mapped_column(Text, default="")
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class IdempotencyRow(Base):
    __tablename__ = "idempotency_keys"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    fingerprint: Mapped[str] = mapped_column(String(64))
    response_json: Mapped[str] = mapped_column(Text)
    created_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)


class ImprovementProposalRow(Base):
    """Human-in-the-loop self-evolution proposal lifecycle."""

    __tablename__ = "improvement_proposals"
    __table_args__ = (
        CheckConstraint(
            "status IN ('collect','attribute','regress','gate','review',"
            "'promoted','rejected','blocked')",
            name="ck_improvement_status",
        ),
        CheckConstraint(
            "review_decision IS NULL OR review_decision IN ('approved','rejected')",
            name="ck_improvement_review_decision",
        ),
    )

    proposal_id: Mapped[str] = mapped_column(String(64), primary_key=True)
    title: Mapped[str] = mapped_column(String(128))
    status: Mapped[str] = mapped_column(
        String(16), default="collect", server_default="collect"
    )
    candidate_label: Mapped[str] = mapped_column(String(80))
    source_dataset_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey(
            "dataset_versions.dataset_id", name="fk_improvement_source_dataset"
        ),
        nullable=True,
        index=True,
    )
    baseline_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("evaluation_runs.run_id", name="fk_improvement_baseline_run"),
        nullable=True,
    )
    candidate_run_id: Mapped[str | None] = mapped_column(
        String(64),
        ForeignKey("evaluation_runs.run_id", name="fk_improvement_candidate_run"),
        nullable=True,
    )
    attribution_json: Mapped[str] = mapped_column(
        Text, default="", server_default=""
    )
    gate_json: Mapped[str] = mapped_column(Text, default="", server_default="")
    review_decision: Mapped[str | None] = mapped_column(String(16), nullable=True)
    reviewer: Mapped[str | None] = mapped_column(String(80), nullable=True)
    review_note: Mapped[str | None] = mapped_column(Text, nullable=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
