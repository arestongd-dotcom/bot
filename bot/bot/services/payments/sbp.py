from __future__ import annotations

import secrets
from dataclasses import dataclass
from decimal import Decimal


@dataclass
class PaymentIntent:
    provider: str
    provider_id: str


async def create_payment(
    amount: Decimal, description: str, metadata: dict
) -> PaymentIntent:
    provider_id = f"stub-{secrets.token_hex(8)}"
    return PaymentIntent(provider="transfer_stub", provider_id=provider_id)


async def fetch_status(provider_id: str | None) -> str:
    return "pending"
