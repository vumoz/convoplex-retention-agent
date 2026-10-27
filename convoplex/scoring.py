"""Explainable RFM-style lapse scoring.

The score is a win-back priority, not a hidden probability. Every segment rule
below is covered by unit tests.

Cadence is the median number of days between consecutive completed orders,
capped at 45. A sparser history is not treated as a healthy habit.

Segments for a customer with two or more orders:
- lost, if the last order was more than 120 days ago
- active, if recency is within 35 days and within 1.25× cadence (and at least a
  14-day floor on that cadence window)
- at_risk, if recency is within 60 days and at most 2× cadence
- lapsed otherwise

A single order is new (≤45 days), at_risk (≤120), or lost. No orders is new.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import date
from statistics import median

from convoplex.money import naira

CADENCE_CAP_DAYS = 45
ACTIVE_ABSOLUTE_DAYS = 35
AT_RISK_ABSOLUTE_DAYS = 60
LOST_AFTER_DAYS = 120

SEGMENT_WEIGHT = {
    "active": 0.05,
    "new": 0.20,
    "at_risk": 0.80,
    "lapsed": 1.00,
    "lost": 0.45,
}

SEGMENT_BLURB = {
    "active": "Active: still inside the normal repurchase window.",
    "new": "New or one-time recent buyer: too early to treat as lapsed.",
    "at_risk": "At risk: past the usual repurchase window, still close enough to win back.",
    "lapsed": "Lapsed: previously buying, now well beyond twice their usual gap.",
    "lost": "Lost: quiet for over 120 days. A win-back is a long shot.",
}


@dataclass
class OrderFact:
    ordered_on: date
    total_ngn: int


@dataclass
class LapseScore:
    recency_days: int | None
    frequency: int
    monetary_ngn: int
    median_gap_days: float | None
    cadence_days: float | None
    ratio: float | None
    score: float
    segment: str
    reasons: list[str]


def _cadence(median_gap: float | None) -> float:
    if median_gap is None or median_gap <= 0:
        return 30.0
    return min(float(median_gap), float(CADENCE_CAP_DAYS))


def classify(frequency: int, recency_days: int | None, median_gap: float | None) -> str:
    if frequency <= 0 or recency_days is None:
        return "new"
    if frequency == 1:
        if recency_days <= 45:
            return "new"
        if recency_days <= LOST_AFTER_DAYS:
            return "at_risk"
        return "lost"
    cadence = _cadence(median_gap)
    ratio = recency_days / cadence
    if recency_days > LOST_AFTER_DAYS:
        return "lost"
    active_window = max(14.0, 1.25 * cadence)
    if recency_days <= ACTIVE_ABSOLUTE_DAYS and recency_days <= active_window:
        return "active"
    if ratio <= 2.0 and recency_days <= AT_RISK_ABSOLUTE_DAYS:
        return "at_risk"
    return "lapsed"


def score_orders(orders: list[OrderFact], as_of: date) -> LapseScore:
    completed = sorted(orders, key=lambda order: order.ordered_on)
    frequency = len(completed)
    monetary = sum(order.total_ngn for order in completed)
    if frequency == 0:
        return LapseScore(
            recency_days=None,
            frequency=0,
            monetary_ngn=0,
            median_gap_days=None,
            cadence_days=None,
            ratio=None,
            score=0.0,
            segment="new",
            reasons=["No completed orders in the window."],
        )

    last = completed[-1].ordered_on
    recency = (as_of - last).days
    gaps = [
        (b.ordered_on - a.ordered_on).days for a, b in zip(completed, completed[1:])
    ]
    median_gap = float(median(gaps)) if gaps else None
    cadence = _cadence(median_gap) if frequency >= 2 else None
    ratio = (recency / cadence) if cadence else (recency / 30.0)
    segment = classify(frequency, recency, median_gap)

    score_ratio = ratio
    recency_points = 0.0 if score_ratio <= 1 else min(45.0, (score_ratio - 1) * 18)
    frequency_points = min(30.0, frequency * 4)
    monetary_points = min(25.0, monetary / 12_000)
    raw = recency_points + frequency_points + monetary_points
    score = round(raw * SEGMENT_WEIGHT[segment], 2)

    reasons = [
        f"Last order was {recency} days ago on {last:%d %b %Y}.",
    ]
    if median_gap is not None:
        reasons.append(
            f"Usual gap between orders is {median_gap:.0f} days; "
            f"this silence is {ratio:.1f}× that cadence."
        )
        if median_gap > CADENCE_CAP_DAYS:
            reasons.append(
                f"The raw median gap is {median_gap:.0f} days; scoring caps cadence "
                f"at {CADENCE_CAP_DAYS} days so a long sparse history is not treated as a healthy habit."
            )
    reasons.append(f"{frequency} orders totalling {naira(monetary)} in the window.")
    reasons.append(SEGMENT_BLURB[segment])
    reasons.append(f"Win-back priority is {score:.1f} out of 100.")
    return LapseScore(
        recency_days=recency,
        frequency=frequency,
        monetary_ngn=monetary,
        median_gap_days=median_gap,
        cadence_days=cadence,
        ratio=round(ratio, 3),
        score=score,
        segment=segment,
        reasons=reasons,
    )
