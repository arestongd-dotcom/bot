from __future__ import annotations

import logging
from html import escape

from aiogram import Bot
from sqlalchemy.ext.asyncio import AsyncSession

from bot.config import settings
from bot.content import texts
from bot.db import repo
from bot.db.models import Payment, PaymentKind, PaymentStatus, User
from bot.services import panel
from bot.services.subscriptions import activate_payment
from bot.utils.fmt import fmt_dt, plural_ru

log = logging.getLogger(__name__)


async def _subscription_link(
    session: AsyncSession, tg_id: int
) -> str | None:
    link = await repo.get_kv(session, f"sublink:{tg_id}")
    if panel.valid_link(link):
        return link.strip()

    try:
        client = await panel.resolve_client(tg_id, link)
    except panel.PanelError:
        return None
    if not client:
        return None

    new_link = panel.client_link(client)
    if new_link:
        await repo.set_kv(session, f"sublink:{tg_id}", new_link)
        await session.commit()
    return new_link


async def deliver_payment(bot: Bot, session: AsyncSession, payment: Payment) -> bool:
    kind, sub, target, changed = await activate_payment(session, payment)
    if not changed:
        return payment.status == PaymentStatus.SUCCEEDED
    if kind in (PaymentKind.SUBSCRIPTION, PaymentKind.GIFT) and not sub:
        return False

    buyer = await session.get(User, payment.user_id)
    if not buyer:
        return False

    try:
        if kind == PaymentKind.SUBSCRIPTION and sub:
            link = await _subscription_link(session, buyer.tg_id)
            if link:
                await bot.send_message(
                    buyer.tg_id,
                    texts.PAYMENT_SUB_OK.format(
                        until=fmt_dt(sub.expires_at),
                        link=escape(link, quote=False),
                    ),
                )
            else:
                await bot.send_message(
                    buyer.tg_id,
                    texts.PAYMENT_SUB_OK_NO_LINK.format(
                        until=fmt_dt(sub.expires_at)
                    ),
                )
        elif kind == PaymentKind.DEVICES:
            device_word = plural_ru(
                payment.quantity,
                "дополнительное устройство",
                "дополнительных устройства",
                "дополнительных устройств",
            )
            await bot.send_message(
                buyer.tg_id,
                texts.PAYMENT_DEVICES_OK.format(
                    count=payment.quantity, device_word=device_word
                ),
            )
        elif kind == PaymentKind.GIFT and sub and target:
            await bot.send_message(
                buyer.tg_id,
                texts.PAYMENT_GIFT_BUYER.format(
                    target=escape(target.display_name, quote=False)
                ),
            )
            link = await _subscription_link(session, target.tg_id)
            if link:
                await bot.send_message(
                    target.tg_id,
                    texts.PAYMENT_GIFT_TARGET.format(
                        shop=settings.shop_name,
                        until=fmt_dt(sub.expires_at),
                        link=escape(link, quote=False),
                    ),
                )
            else:
                await bot.send_message(
                    target.tg_id,
                    texts.PAYMENT_GIFT_TARGET_NO_LINK.format(
                        shop=settings.shop_name,
                        until=fmt_dt(sub.expires_at),
                    ),
                )
    except Exception as exc:
        log.warning("Не смог отправить уведомление об оплате: %s", exc)

    referrer_id = buyer.referred_by
    if referrer_id and await repo.get_kv(session, f"refbonus:{payment.id}"):
        try:
            await bot.send_message(referrer_id, texts.REFERRAL_BONUS)
        except Exception as exc:
            log.warning("Не смог уведомить пригласившего: %s", exc)

    return True
