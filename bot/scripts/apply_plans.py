import asyncio

from sqlalchemy import select

from bot.db.models import Plan
from bot.db.seed import seed_plans
from bot.db.session import SessionLocal, init_db


async def main() -> None:
    await init_db()
    async with SessionLocal() as session:
        await seed_plans(session)
        plans = await session.scalars(select(Plan).order_by(Plan.sort))
        for plan in plans:
            state = "active" if plan.is_active else "hidden"
            print(
                f"{plan.code:10} {plan.title:12} {plan.days:>3} дн.  {plan.price_rub} ₽  [{state}]"
            )


asyncio.run(main())
