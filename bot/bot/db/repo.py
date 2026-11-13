from __future__ import annotations

import secrets
from datetime import datetime, timedelta
from decimal import Decimal

from sqlalchemy import func, select
from sqlalchemy.exc import IntegrityError
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import (
    Device,
    KV,
    Payment,
    PaymentKind,
    PaymentStatus,
    Plan,
    Subscription,
    SubscriptionStatus,
    User,
    as_aware,
    utcnow,
)


async def get_or_create_user(
    session: AsyncSession,
    tg_id: int,
    username: str | None = None,
    full_name: str | None = None,
    referred_by: int | None = None,
) -> tuple[User, bool]:
    user = await session.scalar(select(User).where(User.tg_id == tg_id))
    if user:
        user.username = username or user.username
        user.full_name = full_name or user.full_name
        if referred_by and referred_by != tg_id and not user.referred_by:
            user.referred_by = referred_by
        user.last_seen_at = utcnow()
        return user, False

    user = User(
        tg_id=tg_id,
        username=username,
        full_name=full_name,
        referred_by=referred_by if referred_by and referred_by != tg_id else None,
    )
    session.add(user)
    try:
        await session.flush()
    except IntegrityError:
        await session.rollback()
        existing = await session.scalar(select(User).where(User.tg_id == tg_id))
        if existing is None:
            raise
        return existing, False
    return user, True


async def get_user(session: AsyncSession, tg_id: int) -> User | None:
    return await session.scalar(select(User).where(User.tg_id == tg_id))


async def get_user_by_username(session: AsyncSession, username: str) -> User | None:
    return await session.scalar(
        select(User).where(func.lower(User.username) == username.lower())
    )


async def count_referrals(session: AsyncSession, tg_id: int) -> int:
    return (
        await session.scalar(
            select(func.count(User.id)).where(User.referred_by == tg_id)
        )
        or 0
    )


async def list_plans(session: AsyncSession) -> list[Plan]:
    result = await session.scalars(
        select(Plan).where(Plan.is_active.is_(True)).order_by(Plan.sort, Plan.days)
    )
    return list(result)


async def get_plan(session: AsyncSession, plan_id: int) -> Plan | None:
    return await session.get(Plan, plan_id)


async def get_active_plan(session: AsyncSession, plan_id: int) -> Plan | None:
    plan = await session.get(Plan, plan_id)
    if plan and plan.is_active:
        return plan
    return None


async def get_active_subscription(
    session: AsyncSession, user_id: int
) -> Subscription | None:
    return await session.scalar(
        select(Subscription)
        .where(
            Subscription.user_id == user_id,
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expires_at > utcnow(),
        )
        .order_by(Subscription.expires_at.desc())
    )


async def get_latest_subscription(
    session: AsyncSession, user_id: int
) -> Subscription | None:
    return await session.scalar(
        select(Subscription)
        .where(Subscription.user_id == user_id)
        .order_by(Subscription.expires_at.desc())
    )


async def get_subscription_by_token(
    session: AsyncSession, token: str
) -> Subscription | None:
    return await session.scalar(select(Subscription).where(Subscription.token == token))


async def expiring_subscriptions(
    session: AsyncSession, before: datetime
) -> list[Subscription]:
    result = await session.scalars(
        select(Subscription).where(
            Subscription.status == SubscriptionStatus.ACTIVE,
            Subscription.expires_at <= before,
        )
    )
    return list(result)


def new_token() -> str:
    return secrets.token_urlsafe(24)


async def create_subscription(
    session: AsyncSession, user: User, plan: Plan, days: int | None = None
) -> Subscription:
    sub = Subscription(
        user_id=user.id,
        plan_id=plan.id,
        token=new_token(),
        device_limit=plan.device_limit or 1,
        traffic_limit_bytes=(plan.traffic_gb or 0) * 1024**3,
        expires_at=utcnow() + timedelta(days=days or plan.days),
    )
    session.add(sub)
    await session.flush()
    return sub


async def list_devices(session: AsyncSession, subscription_id: int) -> list[Device]:
    result = await session.scalars(
        select(Device)
        .where(Device.subscription_id == subscription_id, Device.is_active.is_(True))
        .order_by(Device.id)
    )
    return list(result)


async def add_device(
    session: AsyncSession, subscription: Subscription, uuid: str, name: str
) -> Device:
    device = Device(subscription_id=subscription.id, uuid=uuid, name=name)
    session.add(device)
    await session.flush()
    return device


async def get_device(session: AsyncSession, device_id: int) -> Device | None:
    return await session.get(Device, device_id)


async def create_payment(
    session: AsyncSession,
    user: User,
    *,
    amount: Decimal,
    kind: str = PaymentKind.SUBSCRIPTION,
    provider: str = "sbp_stub",
    provider_id: str | None = None,
    plan_id: int | None = None,
    quantity: int = 1,
    target_tg_id: int | None = None,
) -> Payment:
    payment = Payment(
        user_id=user.id,
        plan_id=plan_id,
        kind=kind,
        quantity=quantity,
        target_tg_id=target_tg_id,
        provider=provider,
        provider_id=provider_id,
        amount=amount,
        status=PaymentStatus.PENDING,
    )
    session.add(payment)
    await session.flush()
    return payment


async def get_payment(session: AsyncSession, payment_id: int) -> Payment | None:
    return await session.get(Payment, payment_id)


async def list_pending_payments(
    session: AsyncSession, user_id: int
) -> list[Payment]:
    result = await session.scalars(
        select(Payment)
        .where(Payment.user_id == user_id, Payment.status == PaymentStatus.PENDING)
        .order_by(Payment.id)
    )
    return list(result)


async def get_kv(session: AsyncSession, key: str) -> str | None:
    return await session.scalar(select(KV.value).where(KV.key == key))


async def set_kv(session: AsyncSession, key: str, value: str) -> None:
    row = await session.get(KV, key)
    if row:
        row.value = value
    else:
        session.add(KV(key=key, value=value))
    await session.flush()


async def stats(session: AsyncSession) -> dict:
    users = await session.scalar(select(func.count(User.id))) or 0
    active_subs = (
        await session.scalar(
            select(func.count(Subscription.id)).where(
                Subscription.status == SubscriptionStatus.ACTIVE,
                Subscription.expires_at > utcnow(),
            )
        )
        or 0
    )
    revenue = await session.scalar(
        select(func.coalesce(func.sum(Payment.amount), 0)).where(
            Payment.status == PaymentStatus.SUCCEEDED
        )
    ) or Decimal(0)
    return {
        "users": users,
        "active_subs": int(active_subs),
        "revenue": Decimal(revenue),
    }


def extend_expiry(
    current: datetime | None, days: int, now: datetime | None = None
) -> datetime:
    now = as_aware(now) or utcnow()
    current = as_aware(current)
    base = current if current and current > now else now
    return base + timedelta(days=days)
