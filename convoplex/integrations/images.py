"""Muse Image client. Upstream: POST /v1/images/generations, response data[0].b64_json (WEBP).

The model draws a text-free product photo. Pillow paints the shop, product, and
naira prices so the numbers on the card match the offer row.
"""

from __future__ import annotations

import base64
import io
import time
from dataclasses import dataclass
from pathlib import Path

import httpx
from PIL import Image, ImageDraw, ImageFont

from convoplex.llm import LLMError
from convoplex.money import naira

IMAGE_PATH = "/images/generations"
CARD = 1080

_PALETTES = {
    "provisions": ((16, 62, 46), (30, 112, 74), (214, 146, 62)),
    "ankara": ((78, 28, 58), (138, 52, 78), (214, 168, 78)),
    "phones": ((18, 42, 68), (22, 96, 114), (196, 124, 68)),
    "accessories": ((18, 42, 68), (22, 96, 114), (196, 124, 68)),
}
_DEFAULT_PALETTE = _PALETTES["provisions"]

_BOLD: Path | None = None
_REGULAR: Path | None = None
_BUNDLED_FONTS = Path(__file__).resolve().parent.parent / "fonts"
_FONT_FILES = {
    True: "DejaVuSans-Bold.ttf",
    False: "DejaVuSans.ttf",
}


def _bundled_font(name: str) -> Path | None:
    path = _BUNDLED_FONTS / name
    return path if path.is_file() else None


def _find_font(names: tuple[str, ...]) -> Path | None:
    roots = (Path("/usr/share/fonts"), Path("/usr/local/share/fonts"), Path.home() / ".fonts")
    for root in roots:
        if not root.exists():
            continue
        for name in names:
            match = next(root.rglob(name), None)
            if match is not None:
                return match
    return None


def _fonts() -> tuple[Path | None, Path | None]:
    """Bundled DejaVu first, then a system install. Vercel has no system fonts."""
    global _BOLD, _REGULAR
    if _BOLD is None:
        _BOLD = _bundled_font(_FONT_FILES[True]) or _find_font(
            ("DejaVuSans-Bold.ttf", "LiberationSans-Bold.ttf", "FreeSansBold.ttf")
        )
    if _REGULAR is None:
        _REGULAR = _bundled_font(_FONT_FILES[False]) or _find_font(
            ("DejaVuSans.ttf", "LiberationSans-Regular.ttf", "FreeSans.ttf")
        )
    return _BOLD, _REGULAR


def _font(size: int, *, bold: bool) -> ImageFont.ImageFont:
    bold_path, regular_path = _fonts()
    path = bold_path if bold else regular_path
    if path is None:
        raise LLMError(
            "Promo cards need DejaVu Sans so the naira sign draws correctly. "
            "Install fonts-dejavu-core (DejaVuSans.ttf and DejaVuSans-Bold.ttf)."
        )
    return ImageFont.truetype(str(path), size=size)


def promo_prompt(offer: dict) -> str:
    """Scene description for muse-image-1.0. The model must not paint words or prices."""
    price = naira(offer["offer_price_ngn"])
    was = naira(offer["list_price_ngn"])
    return (
        "Text-free square product photograph for a Nigerian retail promo card. "
        f"Shop: {offer['retailer_name']}, {offer['city']}. "
        f"Product on the shelf: {offer['product_name']}. "
        f"Offer context, do not draw this: {price} instead of {was}, "
        f"{int(offer['discount_percent'])}% off. "
        "Warm market colours, simple background, soft daylight. "
        "No text, no letters, no digits, no prices, no logos, no watermarks, no people."
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
               o.customer_id, o.language, p.name AS product_name, p.category,
               r.name AS retailer_name, r.city
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
        "customer_id": int(row["customer_id"]),
        "offer_price_ngn": int(row["offer_price_ngn"]),
        "list_price_ngn": int(row["list_price_ngn"]),
        "discount_percent": int(row["discount_percent"]),
        "product_name": row["product_name"],
        "category": row["category"],
        "retailer_name": row["retailer_name"],
        "city": row["city"],
        "language": row["language"],
    }


_OVERLAY_OFF = {
    "en": "OFF",
    "pcm": "OFF",
    "ig": "mbelata",
    "yo": "ẹdinwo",
    "ha": "ragiya",
}
_OVERLAY_CTA = {
    "en": "Reply YES on WhatsApp",
    "pcm": "Reply YES on WhatsApp",
    "ig": "Zaghachi EE na WhatsApp",
    "yo": "Dáhùn BẸ́Ẹ̀NI lórí WhatsApp",
    "ha": "Amsa EE a WhatsApp",
}


def overlay_copy(offer: dict) -> dict:
    """Exact strings painted onto the card. Prices and the product name stay as stored."""
    language = str(offer.get("language") or "en")
    if language not in _OVERLAY_OFF:
        language = "en"
    return {
        "shop": offer["retailer_name"],
        "city": offer["city"],
        "product": offer["product_name"],
        "price": naira(offer["offer_price_ngn"]),
        "was": naira(offer["list_price_ngn"]),
        "off": f"{int(offer['discount_percent'])}% {_OVERLAY_OFF[language]}",
        "cta": _OVERLAY_CTA[language],
    }


def _mix(a: tuple[int, int, int], b: tuple[int, int, int], t: float) -> tuple[int, int, int]:
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _gradient(size: int, palette: tuple[tuple[int, int, int], ...]) -> Image.Image:
    column = Image.new("RGB", (1, size))
    pixels = column.load()
    top, mid, bottom = palette
    for y in range(size):
        t = y / max(size - 1, 1)
        if t < 0.58:
            pixels[0, y] = _mix(top, mid, t / 0.58)
        else:
            pixels[0, y] = _mix(mid, bottom, (t - 0.58) / 0.42)
    return column.resize((size, size), Image.Resampling.NEAREST)


def _placeholder(category: str | None) -> Image.Image:
    palette = _PALETTES.get(category or "", _DEFAULT_PALETTE)
    base = _gradient(CARD, palette).convert("RGBA")
    pattern = Image.new("RGBA", (CARD, CARD), (0, 0, 0, 0))
    draw = ImageDraw.Draw(pattern)
    step = 78
    for y in range(-step, CARD + step, step):
        for x in range(-step, CARD + step, step):
            shift = 0 if (y // step) % 2 == 0 else step // 2
            cx = x + shift
            diamond = [(cx, y - 26), (cx + 18, y), (cx, y + 26), (cx - 18, y)]
            draw.polygon(diamond, fill=(255, 236, 204, 42))
    spot = Image.new("RGBA", (CARD, CARD), (0, 0, 0, 0))
    ImageDraw.Draw(spot).ellipse((180, 70, 900, 760), fill=(255, 220, 160, 78))
    composed = Image.alpha_composite(base, pattern)
    return Image.alpha_composite(composed, spot)


def _cover(image: Image.Image) -> Image.Image:
    image = image.convert("RGB")
    width, height = image.size
    scale = max(CARD / max(width, 1), CARD / max(height, 1))
    resized = image.resize(
        (max(1, int(width * scale)), max(1, int(height * scale))),
        Image.Resampling.LANCZOS,
    )
    left = max(0, (resized.width - CARD) // 2)
    top = max(0, (resized.height - CARD) // 2)
    return resized.crop((left, top, left + CARD, top + CARD)).convert("RGBA")


def _scrim(base: Image.Image) -> Image.Image:
    """Shade the shop name and the bottom copy. Leave the product in the middle clear."""
    shade = Image.new("RGBA", base.size, (0, 0, 0, 0))
    draw = ImageDraw.Draw(shade)
    for y in range(0, 280, 4):
        alpha = int(140 * (1 - y / 280))
        draw.rectangle((0, y, CARD, y + 4), fill=(8, 28, 22, alpha))
    # The copy panel sits in the bottom ~280px. Start the scrim just above it.
    for y in range(760, CARD, 4):
        alpha = int(40 + 160 * ((y - 760) / (CARD - 760)))
        draw.rectangle((0, y, CARD, y + 4), fill=(8, 24, 18, min(alpha, 190)))
    return Image.alpha_composite(base, shade)


def _open_background(raw: bytes) -> Image.Image:
    try:
        image = Image.open(io.BytesIO(raw))
        image.load()
    except Exception as exc:
        raise LLMError("Muse Image returned bytes that are not a readable image.") from exc
    return image


def _wrap(draw: ImageDraw.ImageDraw, text: str, font: ImageFont.ImageFont, width: int) -> list[str]:
    words = str(text).split()
    if not words:
        return []
    lines: list[str] = []
    current = words[0]
    for word in words[1:]:
        trial = f"{current} {word}"
        if draw.textlength(trial, font=font) <= width:
            current = trial
        else:
            lines.append(current)
            current = word
    lines.append(current)
    return lines


def _fit(lines: list[str], limit: int) -> list[str]:
    if len(lines) <= limit:
        return lines
    kept = lines[:limit]
    kept[-1] = kept[-1].rstrip(" .") + "…"
    return kept


def render_promo_card(offer: dict, background: bytes | None = None) -> bytes:
    """Paint shop, product, naira prices, and percent off onto a square WEBP."""
    copy = overlay_copy(offer)
    if background:
        canvas = _scrim(_cover(_open_background(background)))
    else:
        canvas = _placeholder(offer.get("category"))
    draw = ImageDraw.Draw(canvas)
    draw.rounded_rectangle((16, 16, CARD - 16, CARD - 16), radius=28, outline=(226, 176, 67, 230), width=3)

    shop_font = _font(54, bold=True)
    city_font = _font(28, bold=False)
    product_font = _font(40, bold=True)
    price_font = _font(68, bold=True)
    was_font = _font(30, bold=False)
    badge_font = _font(26, bold=True)
    cta_font = _font(30, bold=True)
    ink = (255, 248, 238, 255)
    gold = (242, 193, 78, 255)
    muted = (226, 210, 186, 255)
    max_width = CARD - 160

    y = 58
    for line in _fit(_wrap(draw, copy["shop"], shop_font, max_width), 2):
        draw.text((72, y + 2), line, font=shop_font, fill=(0, 0, 0, 80))
        draw.text((72, y), line, font=shop_font, fill=ink)
        y += 64
    draw.text((72, y), copy["city"], font=city_font, fill=(240, 215, 164, 255))
    y += 36
    draw.rectangle((72, y, 196, y + 4), fill=gold)

    product_lines = _fit(_wrap(draw, copy["product"], product_font, CARD - 200), 2)
    price = copy["price"]
    badge = copy["off"]
    was = f"was {copy['was']}"
    cta = copy["cta"]
    price_width = draw.textlength(price, font=price_font)
    badge_width = draw.textlength(badge, font=badge_font)
    badge_beside = 80 + price_width + 28 + badge_width + 36 <= CARD - 80
    line_h = 48
    pad_top = 20
    pad_bottom = 20
    price_h = 74
    was_h = 36
    cta_h = 38
    extra_badge = 0 if badge_beside else 54
    panel_height = (
        pad_top
        + max(1, len(product_lines)) * line_h
        + 6
        + price_h
        + extra_badge
        + 2
        + was_h
        + 10
        + cta_h
        + pad_bottom
    )
    panel_bottom = CARD - 28
    panel_top = panel_bottom - panel_height
    draw.rounded_rectangle((48, panel_top, CARD - 48, panel_bottom), radius=28, fill=(10, 36, 28, 235))

    y = panel_top + pad_top
    for line in product_lines:
        draw.text((80, y), line, font=product_font, fill=ink)
        y += line_h
    y += 6
    draw.text((80, y), price, font=price_font, fill=gold)
    if badge_beside:
        badge_x = 80 + price_width + 28
        badge_y = y + 16
    else:
        badge_x = 80
        badge_y = y + price_h
    draw.rounded_rectangle(
        (badge_x, badge_y, badge_x + badge_width + 36, badge_y + 44),
        radius=22,
        fill=(226, 176, 67, 255),
    )
    draw.text((badge_x + 18, badge_y + 7), badge, font=badge_font, fill=(28, 20, 8, 255))
    y += price_h + extra_badge + 2
    draw.text((80, y), was, font=was_font, fill=muted)
    was_width = draw.textlength(was, font=was_font)
    box = draw.textbbox((80, y), was, font=was_font)
    mid = (box[1] + box[3]) / 2
    draw.line((80, mid, 80 + was_width, mid), fill=muted, width=3)
    y += was_h + 10
    draw.text((80, y), cta, font=cta_font, fill=gold)

    buffer = io.BytesIO()
    canvas.convert("RGB").save(buffer, format="WEBP", quality=82, method=4)
    return buffer.getvalue()


@dataclass
class PromoImage:
    image: bytes
    prompt: str
    model: str
    latency_ms: int
    mode: str
    overlay: dict


def build_promo_image(
    offer: dict,
    *,
    mode: str,
    api_key: str | None,
    base_url: str,
    model: str,
    client: httpx.Client | None = None,
) -> PromoImage:
    """Mock mode draws a local placeholder. Live mode calls muse-image-1.0, then overlays."""
    prompt = promo_prompt(offer)
    overlay = overlay_copy(offer)
    if mode != "live":
        started = time.perf_counter()
        image = render_promo_card(offer, None)
        latency_ms = int((time.perf_counter() - started) * 1000)
        return PromoImage(image, prompt, "mock", latency_ms, "mock", overlay)
    if not api_key:
        raise LLMError(
            "META_API_KEY is not set. Export it in your shell, or run with CONVOPLEX_LLM=mock."
        )
    owns = client is None
    http = client or httpx.Client(timeout=120.0)
    started = time.perf_counter()
    try:
        raw = generate_image(prompt, api_key=api_key, base_url=base_url, model=model, client=http)
    finally:
        if owns:
            http.close()
    image = render_promo_card(offer, raw)
    latency_ms = int((time.perf_counter() - started) * 1000)
    return PromoImage(image, prompt, model, latency_ms, "live", overlay)


def promo_filename(offer_id: int) -> str:
    return f"offer-{int(offer_id)}.webp"


def save_promo_image(image: bytes, output_dir: Path | str, offer_id: int) -> Path:
    folder = Path(output_dir) / "promo"
    folder.mkdir(parents=True, exist_ok=True)
    path = folder / promo_filename(offer_id)
    path.write_bytes(image)
    return path


def save_promo_blob(conn, offer_id: int, image: bytes, media_type: str = "image/webp") -> None:
    """Persist the card in the database. The serverless disk does not keep files."""
    conn.execute(
        """
        INSERT INTO promo_cards (offer_id, image, media_type)
        VALUES (?, ?, ?)
        ON CONFLICT(offer_id) DO UPDATE SET
            image = excluded.image,
            media_type = excluded.media_type
        """,
        (int(offer_id), image, media_type),
    )
    conn.commit()


def load_promo_blob(conn, offer_id: int) -> bytes | None:
    row = conn.execute(
        "SELECT image FROM promo_cards WHERE offer_id = ?",
        (int(offer_id),),
    ).fetchone()
    if row is None or row["image"] is None:
        return None
    blob = row["image"]
    if isinstance(blob, memoryview):
        return bytes(blob)
    return bytes(blob)


def remember_promo(conn, image: bytes, output_dir: Path | str, offer_id: int) -> Path:
    """Write the file when the disk allows it, and always store the bytes in the DB."""
    path = Path(output_dir) / "promo" / promo_filename(offer_id)
    try:
        path = save_promo_image(image, output_dir, offer_id)
    except OSError:
        pass
    save_promo_blob(conn, offer_id, image)
    return path


def read_promo_bytes(conn, output_dir: Path | str, offer_id: int) -> bytes | None:
    path = Path(output_dir) / "promo" / promo_filename(offer_id)
    if path.is_file():
        try:
            data = path.read_bytes()
            if data:
                return data
        except OSError:
            pass
    if conn is None:
        return None
    return load_promo_blob(conn, offer_id)


def clear_promo_cards(output_dir: Path | str) -> None:
    folder = Path(output_dir) / "promo"
    if not folder.exists():
        return
    for item in folder.glob("*.webp"):
        item.unlink()
