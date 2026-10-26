from datetime import datetime
from typing import Optional, Any
from sqlalchemy import (
    Column,
    String,
    Integer,
    Float,
    DateTime,
    Date,
    JSON,
    Text,
)
from sqlalchemy.orm import declarative_base

Base = declarative_base()

class Rider(Base):
    __tablename__ = "riders"

    rider_id = Column(String(32), primary_key=True, index=True)
    name = Column(String(128), nullable=False)
    city = Column(String(64), nullable=False)
    joined_on = Column(Date, nullable=False)

class Trip(Base):
    __tablename__ = "trips"

    trip_id = Column(String(32), primary_key=True, index=True)
    rider_id = Column(String(32), index=True, nullable=False)
    started_at = Column(DateTime(timezone=True), index=True, nullable=False)
    status = Column(String(32), nullable=False)  # completed, cancelled_by_rider, cancelled_by_customer
    distance_km = Column(Float, nullable=False)
    surge_multiplier = Column(Float, default=1.0)

class PayoutLine(Base):
    __tablename__ = "payout_lines"

    line_id = Column(String(32), primary_key=True, index=True)
    payout_date = Column(Date, index=True, nullable=False)
    rider_id = Column(String(32), index=True, nullable=False)
    line_type = Column(String(32), nullable=False)  # trip, daily_incentive, cancellation_penalty
    trip_id = Column(String(32), index=True, nullable=True)
    amount = Column(Integer, nullable=False)

class OpsItem(Base):
    __tablename__ = "ops_items"

    id = Column(String(64), primary_key=True, index=True)
    rider_id = Column(String(32), index=True, nullable=False)
    type = Column(String(32), nullable=False)  # approval, escalation
    amount = Column(Integer, nullable=True)
    reason = Column(Text, nullable=False)
    status = Column(String(32), default="pending", index=True)  # pending, approved, rejected
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)

class TraceStep(Base):
    __tablename__ = "trace_steps"

    id = Column(Integer, primary_key=True, autoincrement=True)
    rider_id = Column(String(32), index=True, nullable=False)
    at = Column(DateTime(timezone=True), nullable=False)
    type = Column(String(32), nullable=False)  # message_in, tool_call, decision, reply, error
    name = Column(String(64), nullable=False)
    input = Column(JSON, nullable=True)
    output = Column(JSON, nullable=True)

class ProcessedMessage(Base):
    __tablename__ = "processed_messages"

    message_id = Column(String(64), primary_key=True, index=True)
    rider_id = Column(String(32), index=True, nullable=False)
    reply = Column(Text, nullable=False)
    created_at = Column(DateTime(timezone=True), default=datetime.utcnow, nullable=False)
