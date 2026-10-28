"""Promo cards ship DejaVu Sans so Vercel can draw ₦ and Yoruba, Igbo, and Hausa marks."""

from __future__ import annotations

import struct
from pathlib import Path

import pytest

from convoplex.integrations.images import (
    _BUNDLED_FONTS,
    _OVERLAY_CTA,
    _OVERLAY_OFF,
    LLMError,
    build_promo_image,
    overlay_copy,
)
from convoplex.languages import CONFIRM, GLYPHS, HINT, LABELS

_NEEDED = set(GLYPHS)
for table in (LABELS, CONFIRM, _OVERLAY_CTA, _OVERLAY_OFF):
    for value in table.values():
        _NEEDED.update(ch for ch in value if ord(ch) > 127)
_NEEDED.update("₦% ")


def _cmap(path: Path) -> set[int]:
    data = path.read_bytes()
    num_tables = struct.unpack_from(">H", data, 4)[0]
    cmap_off = None
    for index in range(num_tables):
        tag, _checksum, offset, _length = struct.unpack_from(">4sIII", data, 12 + index * 16)
        if tag == b"cmap":
            cmap_off = offset
            break
    assert cmap_off is not None
    _version, subtables = struct.unpack_from(">HH", data, cmap_off)
    points: set[int] = set()
    for index in range(subtables):
        _platform, _encoding, sub_off = struct.unpack_from(">HHI", data, cmap_off + 4 + index * 8)
        start = cmap_off + sub_off
        fmt = struct.unpack_from(">H", data, start)[0]
        if fmt == 4:
            points.update(_cmap4(data, start))
        elif fmt == 12:
            points.update(_cmap12(data, start))
    return points


def _cmap4(data: bytes, start: int) -> set[int]:
    _fmt, _length, _language, seg_x2 = struct.unpack_from(">HHHH", data, start)
    segments = seg_x2 // 2
    end_at = start + 14
    ends = struct.unpack_from(f">{segments}H", data, end_at)
    start_at = end_at + segments * 2 + 2
    starts = struct.unpack_from(f">{segments}H", data, start_at)
    delta_at = start_at + segments * 2
    deltas = struct.unpack_from(f">{segments}h", data, delta_at)
    range_at = delta_at + segments * 2
    offsets = struct.unpack_from(f">{segments}H", data, range_at)
    found: set[int] = set()
    for index in range(segments):
        first = starts[index]
        last = ends[index]
        if first == 0xFFFF:
            continue
        for code in range(first, last + 1):
            if offsets[index] == 0:
                glyph = (code + deltas[index]) & 0xFFFF
            else:
                pos = range_at + index * 2 + offsets[index] + (code - first) * 2
                glyph = struct.unpack_from(">H", data, pos)[0]
                if glyph:
                    glyph = (glyph + deltas[index]) & 0xFFFF
            if glyph:
                found.add(code)
    return found


def _cmap12(data: bytes, start: int) -> set[int]:
    _fmt, _reserved, _length, _language, groups = struct.unpack_from(">HHIII", data, start)
    found: set[int] = set()
    pos = start + 16
    for _ in range(groups):
        first, last, glyph = struct.unpack_from(">III", data, pos)
        pos += 12
        for code in range(first, last + 1):
            if glyph + (code - first):
                found.add(code)
    return found


def test_bundled_dejavu_covers_naira_and_the_language_marks():
    for name in ("DejaVuSans.ttf", "DejaVuSans-Bold.ttf"):
        path = _BUNDLED_FONTS / name
        assert path.is_file(), name
        covered = _cmap(path)
        missing = [ch for ch in sorted(_NEEDED) if ord(ch) > 32 and ord(ch) not in covered]
        assert missing == [], f"{name} is missing {missing}"
    license_text = (_BUNDLED_FONTS / "LICENSE").read_text(encoding="utf-8")
    assert "Bitstream" in license_text
    assert "DejaVu" in license_text


def test_promo_card_draws_without_system_fonts(monkeypatch):
    import convoplex.integrations.images as images

    monkeypatch.setattr(images, "_BOLD", None)
    monkeypatch.setattr(images, "_REGULAR", None)

    def unavailable(_names):
        raise AssertionError("system fonts should not be required")

    monkeypatch.setattr(images, "_find_font", unavailable)
    offer = {
        "retailer_name": "Mama Nkechi Provisions",
        "city": "Lagos",
        "product_name": "Mama Gold Rice 50kg",
        "offer_price_ngn": 70200,
        "list_price_ngn": 78000,
        "discount_percent": 10,
        "category": "provisions",
        "language": "yo",
    }
    card = build_promo_image(
        offer,
        mode="mock",
        api_key=None,
        base_url="https://api.meta.ai/v1",
        model="muse-image-1.0",
    )
    assert card.image[:4] == b"RIFF"
    assert overlay_copy(offer)["cta"].startswith("Dáhùn")
    assert "₦70,200" in overlay_copy(offer)["price"]


def test_missing_bundle_and_system_font_is_a_clear_error(monkeypatch, tmp_path):
    import convoplex.integrations.images as images

    monkeypatch.setattr(images, "_BOLD", None)
    monkeypatch.setattr(images, "_REGULAR", None)
    monkeypatch.setattr(images, "_BUNDLED_FONTS", tmp_path)
    monkeypatch.setattr(images, "_find_font", lambda _names: None)
    with pytest.raises(LLMError, match="DejaVu"):
        images._font(20, bold=True)
