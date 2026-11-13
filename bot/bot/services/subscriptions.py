from __future__ import annotations

import asyncio
import logging
import math
from datetime import timedelta
from decimal import Decimal

from aiogram import Bot
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.db import repo
from bot.db.models import (
    Device,
    Payment,
    PaymentKind,
    PaymentStatus,
    Plan,
    Subscription,
    SubscriptionStatus,
    User,
    utcnow,
)
from bot.services import panel, vpn_core
from bot.services.payments import sbp

log = logging.getLogger(__name__)

MAX_PENDING_PER_USER = 5

_create_lock = asyncio.Lock()
_billing_lock = asyncio.Lock()


async def start_payment(
    session: AsyncSession,
    user: User,
    *,
    amount: Decimal,
    kind: str,
    plan: Plan | None = None,
    quantity: int = 1,
    target_tg_id: int | None = None,
    description: str,
) -> Payment:
    if plan is not None and not plan.is_active:
        raise ValueError("plan is not active")
    if quantity < 1:
        quantity = 1

    plan_id = plan.id if plan else None
    async with _create_lock:
        pending = await repo.list_pending_payments(session, user.id)
        for existing in pending:
            if (
                existing.kind == kind
                and existing.plan_id == plan_id
                and existing.target_tg_id == target_tg_id
                and existing.quantity == quantity
                and Decimal(existing.amount) == Decimal(amount)
            ):
                return existing

        intent = await sbp.create_payment(
            amount=amount,
            description=description,
            metadata={"tg_id": user.tg_id, "kind": kind},
        )

        excess = len(pending) - MAX_PENDING_PER_USER + 1
        if excess > 0:
            for old in pending[:excess]:
                old.status = PaymentStatus.CANCELED
            await session.flush()

        payment = await repo.create_payment(
            session,
            user,
            amount=amount,
            kind=kind,
            provider=intent.provider,
            provider_id=intent.provider_id,
            plan_id=plan_id,
            quantity=quantity,
            target_tg_id=target_tg_id,
        )
        await session.commit()
        return payment


async def _grant_subscription(
    session: AsyncSession, user: User, plan: Plan
) -> Subscription:
    sub = await repo.get_active_subscription(session, user.id)
    if sub:
        sub.expires_at = repo.extend_expiry(sub.expires_at, plan.days)
        sub.device_limit = max(sub.device_limit, plan.device_limit or 1)
        sub.traffic_limit_bytes = max(
            sub.traffic_limit_bytes, (plan.traffic_gb or 0) * 1024**3
        )
        sub.status = SubscriptionStatus.ACTIVE
        await _ensure_device(session, sub)
        return sub

    sub = await repo.create_subscription(session, user, plan)
    await _ensure_device(session, sub)
    return sub


async def _ensure_device(session: AsyncSession, sub: Subscription) -> Device:
    devices = await repo.list_devices(session, sub.id)
    if devices:
        return devices[0]
    return await repo.add_device(session, sub, vpn_core.new_uuid(), "Устройство 1")


async def _add_devices(
    session: AsyncSession, sub: Subscription, count: int
) -> list[Device]:
    existing = await repo.list_devices(session, sub.id)
    created: list[Device] = []
    for index in range(count):
        name = f"Устройство {len(existing) + index + 1}"
        created.append(await repo.add_device(session, sub, vpn_core.new_uuid(), name))
    sub.device_limit += count
    return created


async def activate_payment(
    session: AsyncSession, payment: Payment
) -> tuple[str, Subscription | None, User | None, bool]:
    async with _billing_lock:
        payment = await session.get(Payment, payment.id, populate_existing=True)
        if payment is None:
            return "", None, None, False
        if payment.status != PaymentStatus.PENDING:
            return payment.kind, None, None, False

        buyer = await session.get(User, payment.user_id)
        if not buyer:
            log.error("Платёж %s: покупатель не найден", payment.id)
            return payment.kind, None, None, False

        plan = await session.get(Plan, payment.plan_id) if payment.plan_id else None
        target = buyer
        if payment.kind == PaymentKind.GIFT and payment.target_tg_id:
            gifted = await repo.get_user(session, payment.target_tg_id)
            if not gifted:
                log.error("Платёж %s: получатель подарка не найден", payment.id)
                return payment.kind, None, None, False
            target = gifted

        sub: Subscription | None = None
        if payment.kind in (PaymentKind.SUBSCRIPTION, PaymentKind.GIFT):
            if not plan:
                log.error("Платёж %s: тариф не найден", payment.id)
                return payment.kind, None, None, False
            sub = await _grant_subscription(session, target, plan)
            if settings.panel_enabled:
                try:
                    client = await panel.sync_subscription(
                        target.tg_id,
                        expires_at=sub.expires_at,
                        device_limit=sub.device_limit,
                        traffic_limit_gb=int(
                            (sub.traffic_limit_bytes or 0) / 1024**3
                        ),
                        link=await repo.get_kv(
                            session, f"sublink:{target.tg_id}"
                        ),
                        username=target.username,
                        full_name=target.full_name,
                    )
                except panel.PanelError as exc:
                    log.error(
                        "Платёж %s: не удалось выдать доступ в панели: %s",
                        payment.id,
                        exc,
                    )
                    return payment.kind, None, None, False
                panel.apply_client(sub, client)
                link = panel.client_link(client)
                if link:
                    await repo.set_kv(session, f"sublink:{target.tg_id}", link)
            await _pay_referrer(session, buyer, plan, payment.id)
        elif payment.kind == PaymentKind.DEVICES:
            sub = await repo.get_active_subscription(session, buyer.id)
            if not sub:
                log.error(
                    "Платёж %s: нет активной подписки для устройств", payment.id
                )
                return payment.kind, None, None, False

            quantity = max(1, payment.quantity)
            if settings.panel_enabled:
                link = await repo.get_kv(session, f"sublink:{buyer.tg_id}")
                target_key = f"devtarget:{payment.id}"
                try:
                    client = await panel.resolve_client(buyer.tg_id, link)
                    if not client:
                        log.error("Платёж %s: клиент панели не найден", payment.id)
                        return payment.kind, None, None, False
                    name = str(client.get("name") or "")
                    if not name:
                        log.error("Платёж %s: пустое имя клиента панели", payment.id)
                        return payment.kind, None, None, False

                    stored = await repo.get_kv(session, target_key)
                    if stored and stored.isdigit():
                        new_limit = int(stored)
                    else:
                        current_limit = int(client.get("device_limit") or 0)
                        if not current_limit:
                            log.error(
                                "Платёж %s: у клиента панели нет лимита устройств",
                                payment.id,
                            )
                            return payment.kind, None, None, False
                        new_limit = max(current_limit, sub.device_limit) + quantity
                        await repo.set_kv(session, target_key, str(new_limit))
                        await session.commit()

                    updated = await panel.set_device_limit(name, new_limit)
                except panel.PanelError as exc:
                    log.error("Платёж %s: ошибка панели: %s", payment.id, exc)
                    return payment.kind, None, None, False
                sub.device_limit = int(updated.get("device_limit") or new_limit)
            else:
                await _add_devices(session, sub, quantity)

        payment.status = PaymentStatus.SUCCEEDED
        payment.paid_at = utcnow()
        await session.commit()
        return payment.kind, sub, target, True


async def _pay_referrer(
    session: AsyncSession, buyer: User, plan: Plan, payment_id: int
) -> int:
    if (
        not buyer.referred_by
        or buyer.referred_by == buyer.tg_id
        or settings.referral_percent <= 0
    ):
        return 0

    marker = f"refbonus:{payment_id}"
    if await repo.get_kv(session, marker):
        return 0

    referrer = await repo.get_user(session, buyer.referred_by)
    if not referrer:
        return 0

    bonus_days = max(1, math.ceil(plan.days * settings.referral_percent / 100))
    active = await repo.get_active_subscription(session, referrer.id)
    if active:
        active.expires_at = repo.extend_expiry(active.expires_at, bonus_days)
    else:
        created = await repo.create_subscription(
            session, referrer, plan, days=bonus_days
        )
        await _ensure_device(session, created)

    if settings.panel_enabled:
        inviter_sub = await repo.get_active_subscription(session, referrer.id)
        if inviter_sub:
            try:
                client = await panel.sync_subscription(
                    referrer.tg_id,
                    expires_at=inviter_sub.expires_at,
                    device_limit=inviter_sub.device_limit,
                    link=await repo.get_kv(
                        session, f"sublink:{referrer.tg_id}"
                    ),
                    username=referrer.username,
                    full_name=referrer.full_name,
                )
                panel.apply_client(inviter_sub, client)
                link = panel.client_link(client)
                if link:
                    await repo.set_kv(
                        session, f"sublink:{referrer.tg_id}", link
                    )
            except panel.PanelError as exc:
                log.error(
                    "Реферальный бонус: панель не обновлена для tg=%s: %s",
                    referrer.tg_id,
                    exc,
                )

    await repo.set_kv(session, marker, "1")
    log.info(
        "Реферальный бонус %s дн. → tg=%s (платёж %s)",
        bonus_days,
        referrer.tg_id,
        payment_id,
    )
    return bonus_days


async def apply_referral_bonus(session: AsyncSession, user: User) -> int:
    async with _billing_lock:
        return await _apply_referral_bonus(session, user)


async def _apply_referral_bonus(session: AsyncSession, user: User) -> int:
    if not user.referred_by:
        return 0

    payment = await session.scalar(
        select(Payment)
        .where(
            Payment.user_id == user.id,
            Payment.status == PaymentStatus.SUCCEEDED,
            Payment.kind.in_((PaymentKind.SUBSCRIPTION, PaymentKind.GIFT)),
        )
        .order_by(Payment.id.desc())
    )
    if not payment or not payment.plan_id:
        return 0

    plan = await session.get(Plan, payment.plan_id)
    if not plan:
        return 0

    days = await _pay_referrer(session, user, plan, payment.id)
    if days:
        await session.commit()
    return days


async def check_payment(payment: Payment) -> str:
    if payment.status == PaymentStatus.SUCCEEDED:
        return "succeeded"
    if payment.status == PaymentStatus.CANCELED:
        return "canceled"
    return await sbp.fetch_status(payment.provider_id)


async def reject_payment(session: AsyncSession, payment: Payment) -> bool:
    async with _billing_lock:
        payment = await session.get(Payment, payment.id, populate_existing=True)
        if payment is None or payment.status != PaymentStatus.PENDING:
            return False
        payment.status = PaymentStatus.CANCELED
        await session.commit()
        return True


async def grant_days(
    session: AsyncSession, tg_id: int, days: int
) -> Subscription | None:
    async with _billing_lock:
        return await _grant_days(session, tg_id, days)


async def _grant_days(
    session: AsyncSession, tg_id: int, days: int
) -> Subscription | None:
    user = await repo.get_user(session, tg_id)
    if not user:
        return None
    plan = Plan(
        code=f"manual_{days}",
        title="Ручная выдача",
        days=days,
        price_rub=Decimal(0),
        device_limit=2,
    )
    sub = await _grant_subscription(session, user, plan)
    if settings.panel_enabled:
        try:
            client = await panel.sync_subscription(
                user.tg_id,
                expires_at=sub.expires_at,
                device_limit=sub.device_limit,
                link=await repo.get_kv(session, f"sublink:{user.tg_id}"),
                username=user.username,
                full_name=user.full_name,
            )
            panel.apply_client(sub, client)
            link = panel.client_link(client)
            if link:
                await repo.set_kv(session, f"sublink:{user.tg_id}", link)
        except panel.PanelError as exc:
            log.error(
                "Ручная выдача: панель не обновлена для tg=%s: %s",
                user.tg_id,
                exc,
            )
    await session.commit()
    return sub


async def refresh_subscription(
    session: AsyncSession, user: User, sub: Subscription
) -> dict | None:
    if not settings.panel_enabled:
        return None

    link = await repo.get_kv(session, f"sublink:{user.tg_id}")
    try:
        client = await panel.resolve_client(user.tg_id, link)
    except panel.PanelError:
        return None
    if not client:
        return None

    panel.apply_client(sub, client)
    if repo.as_aware(sub.expires_at) and repo.as_aware(sub.expires_at) <= utcnow():
        sub.status = SubscriptionStatus.EXPIRED
    new_link = panel.client_link(client)
    if new_link:
        await repo.set_kv(session, f"sublink:{user.tg_id}", new_link)
    await session.commit()
    return client


async def ensure_subscription_access(
    session: AsyncSession, user: User, sub: Subscription
) -> str | None:
    link = await repo.get_kv(session, f"sublink:{user.tg_id}")
    if not settings.panel_enabled:
        return link

    try:
        client = await panel.resolve_client(user.tg_id, link)
        if client:
            panel.apply_client(sub, client)
            new_link = panel.client_link(client)
            if new_link:
                await repo.set_kv(session, f"sublink:{user.tg_id}", new_link)
                link = new_link
            await session.commit()
            return link

        client = await panel.sync_subscription(
            user.tg_id,
            expires_at=sub.expires_at,
            device_limit=sub.device_limit,
            link=link,
            username=user.username,
            full_name=user.full_name,
        )
        panel.apply_client(sub, client)
        new_link = panel.client_link(client)
        if new_link:
            await repo.set_kv(session, f"sublink:{user.tg_id}", new_link)
            link = new_link
        await session.commit()
        return link
    except panel.PanelError as exc:
        log.error("Доступ для tg=%s не обновлён в панели: %s", user.tg_id, exc)
        return link


async def expiry_worker(bot: Bot, interval: int = 1800) -> None:
    from bot.db.session import SessionLocal

    warned: set[int] = set()
    while True:
        try:
            async with SessionLocal() as session:
                soon = utcnow() + timedelta(days=1)
                subs = await repo.expiring_subscriptions(session, soon)
                for sub in subs:
                    sub = await session.get(
                        Subscription, sub.id, populate_existing=True
                    )
                    if not sub:
                        continue
                    expires = repo.as_aware(sub.expires_at)
                    if expires and expires <= utcnow():
                        warned.discard(sub.id)
                        sub.status = SubscriptionStatus.EXPIRED
                        await session.commit()
                        try:
                            await bot.send_message(
                                sub.user.tg_id,
                                "Подписка закончилась.\n"
                                "Оформить новую можно в разделе «Купить VPN».",
                            )
                        except Exception:
                            pass
                    elif expires and expires <= soon and sub.id not in warned:
                        warned.add(sub.id)
                        try:
                            await bot.send_message(
                                sub.user.tg_id,
                                "Подписка заканчивается меньше чем через сутки.\n"
                                "Продлить её можно у администратора.",
                            )
                        except Exception:
                            pass
        except Exception:
            log.exception("expiry_worker iteration failed")
        await asyncio.sleep(interval)
