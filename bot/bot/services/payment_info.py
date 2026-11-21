from __future__ import annotations

from html import escape

from sqlalchemy import func, or_, select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db import repo
from bot.db.models import Payment, PaymentKind, PaymentStatus, Plan, User
from bot.utils.fmt import fmt_dt, user_link

KIND_LABELS = {
    PaymentKind.SUBSCRIPTION: "подписка",
    PaymentKind.DEVICES: "устройства",
    PaymentKind.GIFT: "подарок",
}


def kind_label(kind: str) -> str:
    return KIND_LABELS.get(kind, kind)


async def referred_by_label(session: AsyncSession, user: User) -> str:
    if not user.referred_by:
        return ""
    referrer = await repo.get_user(session, user.referred_by)
    if referrer:
        return (
            " (от "
            + user_link(
                referrer.username, referrer.tg_id, referrer.display_name
            )
            + ")"
        )
    return f" (от {user.referred_by})"


async def payment_lines(session: AsyncSession, payment: Payment) -> list[str]:
    if payment.kind == PaymentKind.DEVICES:
        return [f"Докупка устройств: <b>+{payment.quantity}</b>"]

    plan = await session.get(Plan, payment.plan_id) if payment.plan_id else None
    if not plan:
        return []
    return [
        f"Оплачено: <b>{plan.days} дн.</b> "
        f"(тариф «{escape(plan.title, quote=False)}»)"
    ]


async def latest_paid_days(session: AsyncSession, user: User) -> int | None:
    payment = await session.scalar(
        select(Payment)
        .where(
            Payment.status == PaymentStatus.SUCCEEDED,
            Payment.kind.in_((PaymentKind.SUBSCRIPTION, PaymentKind.GIFT)),
            or_(
                Payment.user_id == user.id,
                Payment.target_tg_id == user.tg_id,
            ),
        )
        .order_by(Payment.id.desc())
    )
    if not payment or not payment.plan_id:
        return None
    plan = await session.get(Plan, payment.plan_id)
    return plan.days if plan else None


async def account_lines(session: AsyncSession, user: User) -> list[str]:
    lines: list[str] = []
    sub = await repo.get_latest_subscription(session, user.id)
    if sub:
        lines.append(f"Подписка до: <b>{fmt_dt(sub.expires_at)}</b>")
        devices = sub.device_limit if sub.device_limit > 0 else "без лимита"
        lines.append(f"Устройств: <b>{devices}</b>")

    extra = await session.scalar(
        select(func.coalesce(func.sum(Payment.quantity), 0)).where(
            Payment.user_id == user.id,
            Payment.kind == PaymentKind.DEVICES,
            Payment.status == PaymentStatus.SUCCEEDED,
        )
    )
    if extra:
        lines.append(f"Докуплено устройств: <b>{int(extra)}</b>")
    return lines
