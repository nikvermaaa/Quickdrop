import os
import csv
from pathlib import Path
from datetime import datetime
from sqlalchemy import select, func
from sqlalchemy.ext.asyncio import AsyncSession
from app.models import Rider, Trip, PayoutLine

DATA_DIR = Path(os.getenv("DATA_DIR", Path(__file__).resolve().parent.parent / "data"))

def normalize_rider_id(r_id: str) -> str:
    r_id = r_id.strip()
    if r_id.startswith("R") and r_id[1:].isdigit():
        return f"R{int(r_id[1:]):03d}"
    return r_id

async def seed_data(session: AsyncSession):
    # 1. Seed riders (if empty)
    rider_count = await session.scalar(select(func.count()).select_from(Rider))
    if not rider_count or rider_count == 0:
        riders_path = DATA_DIR / "riders.csv"
        if riders_path.exists():
            with open(riders_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                seen_riders = set()
                riders = []
                for row in reader:
                    rid = normalize_rider_id(row["rider_id"])
                    if rid in seen_riders:
                        continue
                    seen_riders.add(rid)
                    phone = row.get("phone_number")
                    if not phone:
                        num_part = int(rid[1:]) if rid.startswith("R") and rid[1:].isdigit() else 1
                        phone = f"9800000{num_part:03d}"
                    riders.append(
                        Rider(
                            rider_id=rid,
                            name=row["name"].strip(),
                            city=row["city"].strip(),
                            joined_on=datetime.strptime(row["joined_on"].strip(), "%Y-%m-%d").date(),
                            phone_number=phone.strip(),
                        )
                    )
                session.add_all(riders)
                await session.commit()

    # Ensure existing riders have phone numbers populated
    riders_without_phone = (await session.scalars(select(Rider).where(Rider.phone_number.is_(None)))).all()
    if riders_without_phone:
        for r in riders_without_phone:
            num_part = int(r.rider_id[1:]) if r.rider_id.startswith("R") and r.rider_id[1:].isdigit() else 1
            r.phone_number = f"9800000{num_part:03d}"
        await session.commit()

    # 2. Seed trips (if empty) - deduplicating and normalizing distance/rider_id
    trip_count = await session.scalar(select(func.count()).select_from(Trip))
    if not trip_count or trip_count == 0:
        trips_path = DATA_DIR / "trips.csv"
        if trips_path.exists():
            with open(trips_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                seen_trips = set()
                trips = []
                for row in reader:
                    tid = row["trip_id"].strip()
                    if tid in seen_trips:
                        continue
                    seen_trips.add(tid)
                    rid = normalize_rider_id(row["rider_id"])
                    d_km = float(row["distance_km"].strip())
                    if d_km > 50.0:
                        d_km = d_km / 1000.0

                    trips.append(
                        Trip(
                            trip_id=tid,
                            rider_id=rid,
                            started_at=datetime.fromisoformat(row["started_at"].strip().replace("Z", "+00:00")),
                            status=row["status"].strip(),
                            distance_km=d_km,
                            surge_multiplier=float(row["surge_multiplier"].strip()),
                        )
                    )
                session.add_all(trips)
                await session.commit()

    # 3. Seed payout lines (if empty)
    payout_count = await session.scalar(select(func.count()).select_from(PayoutLine))
    if not payout_count or payout_count == 0:
        payouts_path = DATA_DIR / "payout_lines.csv"
        if payouts_path.exists():
            with open(payouts_path, "r", encoding="utf-8") as f:
                reader = csv.DictReader(f)
                seen_lines = set()
                lines = []
                for row in reader:
                    lid = row["line_id"].strip()
                    if lid in seen_lines:
                        continue
                    seen_lines.add(lid)
                    rid = normalize_rider_id(row["rider_id"])
                    lines.append(
                        PayoutLine(
                            line_id=lid,
                            payout_date=datetime.strptime(row["payout_date"].strip(), "%Y-%m-%d").date(),
                            rider_id=rid,
                            line_type=row["line_type"].strip(),
                            trip_id=row["trip_id"].strip() if row["trip_id"].strip() else None,
                            amount=int(row["amount"].strip()),
                        )
                    )
                session.add_all(lines)
                await session.commit()
