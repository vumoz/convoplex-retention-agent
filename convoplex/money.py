"""Naira formatting. Amounts in this project are whole naira (no kobo)."""


def naira(amount: int) -> str:
    return f"₦{int(amount):,}"
