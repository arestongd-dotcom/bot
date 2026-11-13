from __future__ import annotations

from decimal import Decimal
from html import escape

from aiogram.types import CallbackQuery, Message, User as TgUser
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.content import texts
from bot.db import repo
from bot.db.models import Payment, User
from bot.services.subscriptions import refresh_subscription
from bot.ui import Btn, photo_for, show
from bot.utils.fmt import fmt_dt, fmt_used_gb


async def ensure_user(session: AsyncSession, tg_user: TgUser) -> User:
    user, _ = await repo.get_or_create_user(
        session,
        tg_user.id,
        tg_user.username,
        tg_user.full_name,
    )
    await session.commit()
    return user


def main_rows(user=None) -> list[list[Btn]]:
    rows: list[list[Btn]] = [
        [("Подключить VPN", "vpn:connect"), ("Купить VPN", "buy:open")],
        [("Докупить устройства", "dev:buy"), ("Удаление устройств", "dev:list")],
        [("Подарить подписку", "gift:start")],
        [("Профиль", "screen:profile"), ("Партнёрка", "screen:referral")],
        [
            ("Документы", "docs:open"),
            ("Поддержка", f"url:{settings.support_url}"),
        ],
    ]
    if isinstance(user, User) and user.tg_id in settings.admins:
        rows.append([("Админка", "adm:panel")])
    return rows


def profile_rows() -> list[list[Btn]]:
    return [
        [("Купить VPN", "buy:open"), ("Подключение", "vpn:connect")],
        [("Назад", "screen:main")],
    ]


async def plans_line(session: AsyncSession) -> str:
    plans = await repo.list_plans(session)
    if not plans:
        return "тарифы уточняйте в поддержке"
    return " · ".join(
        f"{escape(p.title, quote=False)} {p.price_label}" for p in plans
    )


def discount_of(base_price, base_days: int, plan) -> tuple[int, int] | None:
    if not base_price or not base_days or plan.days <= base_days:
        return None
    old = float(base_price) * plan.days / base_days
    current = float(plan.price_rub)
    if current >= old:
        return None
    percent = round((1 - current / old) * 100)
    return int(round(old)), percent


DEAL_MIN_PERCENT = 20


def deal_of(base_price, base_days: int, plan) -> tuple[int, int] | None:
    deal = discount_of(base_price, base_days, plan)
    if deal and deal[1] >= DEAL_MIN_PERCENT:
        return deal
    return None


async def status_text(session: AsyncSession, user: User) -> str:
    sub = await repo.get_active_subscription(session, user.id)
    if not sub:
        return texts.STATUS_NONE
    return texts.STATUS_ACTIVE.format(
        until=fmt_dt(sub.expires_at),
        devices=sub.device_limit if sub.device_limit > 0 else "без лимита",
        traffic=fmt_used_gb(sub.traffic_used_bytes),
    )


async def show_main(
    event: Message | CallbackQuery, session: AsyncSession, user: User
) -> None:
    caption = texts.WELCOME.format(
        shop=settings.shop_name,
        name=escape(user.full_name or "друг", quote=False),
        status=await status_text(session, user),
        plans=await plans_line(session),
    )
    await show(event, caption, main_rows(user), photo_for("main"))


async def show_profile(
    event: Message | CallbackQuery, session: AsyncSession, user: User
) -> None:
    sub = await repo.get_active_subscription(session, user.id)
    if sub:
        await refresh_subscription(session, user, sub)
    devices: int | str = 0
    if sub:
        devices = sub.device_limit if sub.device_limit > 0 else "без лимита"
    caption = texts.PROFILE.format(
        name=escape(user.full_name or "друг", quote=False),
        tg_id=user.tg_id,
        status=await status_text(session, user),
        devices=devices,
    )
    await show(
        event, caption, profile_rows(), photo_for("profile") or photo_for("main")
    )


async def show_invoice(event: Message | CallbackQuery, payment: Payment) -> None:
    caption = texts.INVOICE.format(
        number=payment.id, amount=f"{Decimal(payment.amount):.0f}"
    )
    rows: list[list[Btn]] = [
        [
            ("QR (недоступно)", "pay:qr"),
            ("Перевод по номеру", f"pay:transfer:{payment.id}"),
        ],
        [("В меню", "screen:main")],
    ]
    await show(event, caption, rows, photo_for("pay"))
