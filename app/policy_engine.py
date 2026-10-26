import math
from datetime import datetime, date, timezone, timedelta
from typing import List, Dict, Any, Optional, Tuple
try:
    from sqlalchemy import select, func, and_
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models import Rider, Trip, PayoutLine
except ImportError:
    select = func = and_ = None
    AsyncSession = Any
    Rider = Trip = PayoutLine = None
IST = timezone(timedelta(hours=5, minutes=30))

def to_ist(dt: datetime) -> datetime:
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(IST)

def to_ist_date(dt: datetime) -> date:
    return to_ist(dt).date()

def normalize_rider_id(r_id: str) -> str:
    r_id = r_id.strip()
    if r_id.startswith("R") and r_id[1:].isdigit():
        return f"R{int(r_id[1:]):03d}"
    return r_id

def calculate_trip_fare(distance_km: float, surge_multiplier: float) -> int:
    """
    Every completed trip pays ₹25 base + ₹6 for each km after the first 2 km.
    If the trip had a surge multiplier, it applies to the whole trip fare (base + distance).
    Round each trip to the nearest rupee, 0.5 rounds up.
    """
    if distance_km > 50.0:
        distance_km = distance_km / 1000.0

    distance_fare = max(0.0, (distance_km - 2.0) * 6.0)
    base_and_dist = 25.0 + distance_fare
    fare_raw = base_and_dist * surge_multiplier
    return int(math.floor(fare_raw + 0.5))

def is_within_7_days(event_date: date, reference_date: date) -> bool:
    diff = (reference_date - event_date).days
    return 0 <= diff <= 7

class DisputeAuditResult:
    def __init__(
        self,
        item_type: str,  # "surge", "unpaid_trip", "daily_incentive", "duplicate_penalty"
        amount: int,
        date_ref: date,
        trip_id: Optional[str] = None,
        reason: str = "",
    ):
        self.item_type = item_type
        self.amount = amount
        self.date_ref = date_ref
        self.trip_id = trip_id
        self.reason = reason

    def to_dict(self) -> Dict[str, Any]:
        return {
            "item_type": self.item_type,
            "amount": self.amount,
            "date": self.date_ref.isoformat(),
            "trip_id": self.trip_id,
            "reason": self.reason,
        }

async def audit_rider_account(
    session: AsyncSession,
    rider_id: str,
    reference_time: datetime,
    filter_trip_ids: Optional[List[str]] = None,
    filter_dates: Optional[List[date]] = None,
) -> List[DisputeAuditResult]:
    """
    Audits the rider's trips and payout lines against policy for the last 7 days.
    """
    ref_date = to_ist_date(reference_time)
    results: List[DisputeAuditResult] = []

    norm_rider = normalize_rider_id(rider_id)
    rider_ids = list({rider_id, norm_rider})

    # 1. Fetch all trips for rider
    trips_query = select(Trip).where(Trip.rider_id.in_(rider_ids))
    if filter_trip_ids:
        trips_query = trips_query.where(Trip.trip_id.in_(filter_trip_ids))
    trips_res = await session.execute(trips_query)
    all_trips = trips_res.scalars().all()

    # 2. Fetch all payout lines for rider
    lines_query = select(PayoutLine).where(PayoutLine.rider_id.in_(rider_ids))
    lines_res = await session.execute(lines_query)
    all_lines = lines_res.scalars().all()

    # Group payout lines by trip_id and by date
    trip_payouts: Dict[str, List[PayoutLine]] = {}
    date_payouts: Dict[date, List[PayoutLine]] = {}
    for line in all_lines:
        if line.trip_id:
            trip_payouts.setdefault(line.trip_id, []).append(line)
        date_payouts.setdefault(line.payout_date, []).append(line)

    # Group trips by IST date
    trips_by_date: Dict[date, List[Trip]] = {}
    for trip in all_trips:
        trip_date = to_ist_date(trip.started_at)
        trips_by_date.setdefault(trip_date, []).append(trip)

    # Determine dates to inspect
    if filter_dates:
        dates_to_check = filter_dates
    elif filter_trip_ids:
        dates_to_check = list(trips_by_date.keys())
    else:
        all_dates = set(trips_by_date.keys()).union(date_payouts.keys())
        dates_to_check = [d for d in all_dates if is_within_7_days(d, ref_date)]
        dates_to_check.sort(reverse=True)

    # --- Check Trip-level discrepancies ---
    for trip in all_trips:
        trip_date = to_ist_date(trip.started_at)
        if filter_dates and trip_date not in filter_dates:
            continue
        if not is_within_7_days(trip_date, ref_date):
            continue

        if trip.status == "completed":
            expected_fare = calculate_trip_fare(trip.distance_km, trip.surge_multiplier)
            lines = trip_payouts.get(trip.trip_id, [])
            actual_paid = sum(l.amount for l in lines if l.line_type == "trip")
            discrepancy = expected_fare - actual_paid

            if discrepancy > 0:
                if len(lines) == 0:
                    results.append(
                        DisputeAuditResult(
                            item_type="unpaid_trip",
                            amount=discrepancy,
                            date_ref=trip_date,
                            trip_id=trip.trip_id,
                            reason=f"Trip {trip.trip_id} on {trip_date} was completed but not paid. Expected ₹{expected_fare}.",
                        )
                    )
                else:
                    results.append(
                        DisputeAuditResult(
                            item_type="surge" if trip.surge_multiplier > 1.0 else "underpaid_trip",
                            amount=discrepancy,
                            date_ref=trip_date,
                            trip_id=trip.trip_id,
                            reason=f"Trip {trip.trip_id} on {trip_date} (surge {trip.surge_multiplier}x): expected ₹{expected_fare}, paid ₹{actual_paid}. Difference ₹{discrepancy}.",
                        )
                    )

    # --- Check Duplicate Cancellation Penalties ---
    for d in dates_to_check:
        lines = date_payouts.get(d, [])
        penalty_lines = [l for l in lines if l.line_type == "cancellation_penalty"]
        penalties_by_trip: Dict[str, List[PayoutLine]] = {}
        for p in penalty_lines:
            if p.trip_id:
                penalties_by_trip.setdefault(p.trip_id, []).append(p)

        for t_id, p_list in penalties_by_trip.items():
            if len(p_list) > 1:
                extra_penalties = len(p_list) - 1
                refund_amount = extra_penalties * 10
                results.append(
                    DisputeAuditResult(
                        item_type="duplicate_penalty",
                        amount=refund_amount,
                        date_ref=d,
                        trip_id=t_id,
                        reason=f"Cancellation penalty of ₹10 was deducted {len(p_list)} times for trip {t_id} on {d}. ₹{refund_amount} extra deducted.",
                    )
                )

    # --- Check Daily Incentive (₹150 for 12+ completed trips) ---
    for d in dates_to_check:
        day_trips = trips_by_date.get(d, [])
        completed_trips = [t for t in day_trips if t.status == "completed"]
        if len(completed_trips) >= 12:
            lines = date_payouts.get(d, [])
            has_incentive = any(l.line_type == "daily_incentive" and l.amount >= 150 for l in lines)
            if not has_incentive:
                results.append(
                    DisputeAuditResult(
                        item_type="daily_incentive",
                        amount=150,
                        date_ref=d,
                        trip_id=None,
                        reason=f"Completed {len(completed_trips)} trips on {d}. Eligible for ₹150 daily incentive which was not paid.",
                    )
                )

    return results
