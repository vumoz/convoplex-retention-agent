"""Muse Image client. Upstream: POST /v1/images/generations, response data[0].b64_json (WEBP)."""

from __future__ import annotations

import base64

import httpx

from convoplex.llm import LLMError
from convoplex.money import naira

IMAGE_PATH = "/images/generations"


def promo_prompt(offer: dict) -> str:
    price = naira(offer["offer_price_ngn"])
    was = naira(offer["list_price_ngn"])
    return (
        f"A clean square retail promo card for a Nigerian shop, {offer['retailer_name']} in {offer['city']}. "
        f"Product: {offer['product_name']}. Price: {price} (was {was}). "
        "Warm market colours, simple typography, no photographs of real people, no third-party logos."
    )


def image_request_body(prompt: str, model: str) -> dict:
    return {"model": model, "prompt": prompt, "n": 1}


def generate_image(
    prompt: str,
    *,
    api_key: str,
    base_url: str,
    model: str,
    client: httpx.Client,
) -> bytes:
    response = client.post(
        f"{base_url.rstrip('/')}{IMAGE_PATH}",
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        json=image_request_body(prompt, model),
    )
    if response.status_code >= 400:
        raise LLMError(f"Muse Image returned HTTP {response.status_code}: {response.text[:500]}")
    try:
        payload = response.json()
        encoded = payload["data"][0]["b64_json"]
    except (ValueError, KeyError, IndexError, TypeError) as exc:
        raise LLMError("Muse Image response is missing data[0].b64_json.") from exc
    return base64.b64decode(encoded)


def load_offer_for_card(conn, offer_id: int) -> dict | None:
    row = conn.execute(
        """
        SELECT o.id, o.offer_price_ngn, o.list_price_ngn, o.discount_percent,
               p.name AS product_name, r.name AS retailer_name, r.city
        FROM offers o
        JOIN products p ON p.id = o.product_id
        JOIN retailers r ON r.id = o.retailer_id
        WHERE o.id = ?
        """,
        (offer_id,),
    ).fetchone()
    if row is None:
        return None
    return {
        "offer_id": int(row["id"]),
        "offer_price_ngn": int(row["offer_price_ngn"]),
        "list_price_ngn": int(row["list_price_ngn"]),
        "discount_percent": int(row["discount_percent"]),
        "product_name": row["product_name"],
        "retailer_name": row["retailer_name"],
        "city": row["city"],
    }
