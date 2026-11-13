from __future__ import annotations

from datetime import datetime, timezone
from decimal import Decimal

from sqlalchemy import (
    BigInteger,
    Boolean,
    DateTime,
    ForeignKey,
    Integer,
    Numeric,
    String,
    Text,
)
from sqlalchemy.orm import Mapped, mapped_column, relationship

from bot.db.base import Base


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def as_aware(value: datetime | None) -> datetime | None:
    if value is None:
        return None
    return value if value.tzinfo else value.replace(tzinfo=timezone.utc)


class User(Base):
    __tablename__ = "users"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    tg_id: Mapped[int] = mapped_column(BigInteger, unique=True, index=True)
    username: Mapped[str | None] = mapped_column(String(64), index=True)
    full_name: Mapped[str | None] = mapped_column(String(128))
    is_banned: Mapped[bool] = mapped_column(Boolean, default=False, server_default="0")
    referred_by: Mapped[int | None] = mapped_column(BigInteger, index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    last_seen_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    subscriptions: Mapped[list[Subscription]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )
    payments: Mapped[list[Payment]] = relationship(
        back_populates="user", cascade="all, delete-orphan"
    )

    @property
    def display_name(self) -> str:
        if self.username:
            return f"@{self.username}"
        return self.full_name or str(self.tg_id)


class Plan(Base):
    __tablename__ = "plans"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    code: Mapped[str] = mapped_column(String(32), unique=True)
    title: Mapped[str] = mapped_column(String(64))
    days: Mapped[int] = mapped_column(Integer)
    price_rub: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    traffic_gb: Mapped[int] = mapped_column(Integer, default=0)
    device_limit: Mapped[int] = mapped_column(Integer, default=1)
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    sort: Mapped[int] = mapped_column(Integer, default=100)

    @property
    def price_label(self) -> str:
        value = Decimal(self.price_rub)
        return (
            f"{value:.0f} ₽" if value == value.to_integral_value() else f"{value:.2f} ₽"
        )

    @property
    def per_day_label(self) -> str:
        if not self.days:
            return ""
        return f"{Decimal(self.price_rub) / self.days:.1f} ₽/день"


class SubscriptionStatus:
    ACTIVE = "active"
    EXPIRED = "expired"
    DISABLED = "disabled"


class Subscription(Base):
    __tablename__ = "subscriptions"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"))
    status: Mapped[str] = mapped_column(
        String(16), default=SubscriptionStatus.ACTIVE, index=True
    )

    token: Mapped[str] = mapped_column(String(64), unique=True, index=True)
    device_limit: Mapped[int] = mapped_column(Integer, default=1)

    traffic_used_bytes: Mapped[int] = mapped_column(BigInteger, default=0)
    traffic_limit_bytes: Mapped[int] = mapped_column(BigInteger, default=0)

    starts_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), default=utcnow)
    expires_at: Mapped[datetime] = mapped_column(DateTime(timezone=True), index=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    user: Mapped[User] = relationship(back_populates="subscriptions", lazy="selectin")
    plan: Mapped[Plan | None] = relationship(lazy="selectin")
    devices: Mapped[list[Device]] = relationship(
        back_populates="subscription", cascade="all, delete-orphan", lazy="selectin"
    )

    @property
    def is_active(self) -> bool:
        expires = as_aware(self.expires_at)
        return self.status == SubscriptionStatus.ACTIVE and bool(
            expires and expires > utcnow()
        )


class Device(Base):
    __tablename__ = "devices"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    subscription_id: Mapped[int] = mapped_column(
        ForeignKey("subscriptions.id", ondelete="CASCADE"), index=True
    )
    uuid: Mapped[str] = mapped_column(String(64), unique=True)
    name: Mapped[str] = mapped_column(String(64), default="Устройство")
    is_active: Mapped[bool] = mapped_column(Boolean, default=True, server_default="1")
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )

    subscription: Mapped[Subscription] = relationship(back_populates="devices")


class PaymentKind:
    SUBSCRIPTION = "subscription"
    DEVICES = "devices"
    GIFT = "gift"


class PaymentStatus:
    PENDING = "pending"
    SUCCEEDED = "succeeded"
    CANCELED = "canceled"


class Payment(Base):
    __tablename__ = "payments"

    id: Mapped[int] = mapped_column(Integer, primary_key=True)
    user_id: Mapped[int] = mapped_column(
        ForeignKey("users.id", ondelete="CASCADE"), index=True
    )
    plan_id: Mapped[int | None] = mapped_column(ForeignKey("plans.id"))
    kind: Mapped[str] = mapped_column(String(16), default=PaymentKind.SUBSCRIPTION)
    quantity: Mapped[int] = mapped_column(Integer, default=1)
    target_tg_id: Mapped[int | None] = mapped_column(BigInteger)

    provider: Mapped[str] = mapped_column(String(32), default="sbp_stub")
    provider_id: Mapped[str | None] = mapped_column(String(128), unique=True)
    amount: Mapped[Decimal] = mapped_column(Numeric(10, 2))
    currency: Mapped[str] = mapped_column(String(8), default="RUB")
    status: Mapped[str] = mapped_column(
        String(16), default=PaymentStatus.PENDING, index=True
    )
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True), default=utcnow
    )
    paid_at: Mapped[datetime | None] = mapped_column(DateTime(timezone=True))

    user: Mapped[User] = relationship(back_populates="payments")
    plan: Mapped[Plan | None] = relationship()


class KV(Base):
    __tablename__ = "kv"

    key: Mapped[str] = mapped_column(String(128), primary_key=True)
    value: Mapped[str] = mapped_column(Text, default="")
