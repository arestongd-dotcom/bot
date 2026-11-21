from __future__ import annotations

from decimal import Decimal

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from bot.db.models import Plan

DEFAULT_PLANS = [
    {
        "code": "month",
        "title": "1 месяц",
        "days": 30,
        "price_rub": Decimal("100"),
        "device_limit": 2,
        "sort": 10,
    },
    {
        "code": "quarter",
        "title": "3 месяца",
        "days": 90,
        "price_rub": Decimal("279"),
        "device_limit": 2,
        "sort": 20,
    },
    {
        "code": "halfyear",
        "title": "6 месяцев",
        "days": 180,
        "price_rub": Decimal("579"),
        "device_limit": 2,
        "sort": 30,
    },
]


async def seed_plans(session: AsyncSession) -> None:
    existing = {plan.code: plan for plan in await session.scalars(select(Plan))}

    for item in DEFAULT_PLANS:
        plan = existing.get(item["code"])
        if plan:
            for key, value in item.items():
                setattr(plan, key, value)
            plan.is_active = True
        else:
            session.add(Plan(**item))

    known = {item["code"] for item in DEFAULT_PLANS}
    for code, plan in existing.items():
        if code not in known:
            plan.is_active = False

    await session.commit()
