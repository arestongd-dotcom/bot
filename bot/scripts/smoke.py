import asyncio
from decimal import Decimal

from bot.config import settings
from bot.db import repo
from bot.db.models import PaymentKind
from bot.db.seed import seed_plans
from bot.db.session import SessionLocal, init_db
from bot.services import vpn_core
from bot.services.subscriptions import activate_payment, start_payment
from bot.utils.fmt import fmt_dt


async def main() -> None:
    settings.panel_api_token = ""
    await init_db()
    async with SessionLocal() as session:
        await seed_plans(session)
        user, _ = await repo.get_or_create_user(
            session, 111222333, "tester", "Test User"
        )
        await session.commit()

        plans = await repo.list_plans(session)
        print("plans:", [(p.title, p.price_label) for p in plans])

        payment = await start_payment(
            session,
            user,
            amount=Decimal(plans[0].price_rub),
            kind=PaymentKind.SUBSCRIPTION,
            plan=plans[0],
            description="smoke test",
        )
        print("payment:", payment.id, payment.status)

        kind, sub, _, changed = await activate_payment(session, payment)
        print("activated:", kind, "changed:", changed, "expires:", fmt_dt(sub.expires_at))

        devices = await repo.list_devices(session, sub.id)
        print("devices:", [(d.name, d.uuid) for d in devices])
        print("sub url:", vpn_core.subscription_url(sub.token))

        dev_payment = await start_payment(
            session,
            user,
            amount=Decimal(settings.extra_device_price),
            kind=PaymentKind.DEVICES,
            quantity=2,
            description="+2 devices",
        )
        await activate_payment(session, dev_payment)
        devices = await repo.list_devices(session, sub.id)
        print("after purchase devices:", len(devices), "limit:", sub.device_limit)

        print("stats:", await repo.stats(session))


asyncio.run(main())
