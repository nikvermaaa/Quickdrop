import os
import asyncio
from datetime import datetime, timezone
from typing import Optional, Dict, Any, List
from contextlib import asynccontextmanager

from fastapi import FastAPI, Depends, HTTPException, Request, Form
from fastapi.responses import HTMLResponse, JSONResponse
from fastapi.templating import Jinja2Templates
from pydantic import BaseModel
from sqlalchemy import select, desc, delete
from sqlalchemy.ext.asyncio import AsyncSession

from app.database import init_db, get_db, async_session
from app.models import ProcessedMessage, TraceStep, OpsItem, Rider, TelegramDriverSession
from app.seed import seed_data
from app.payswift_client import payswift_client
from app.agent import agent_orchestrator
from app.telegram_service import telegram_service

@asynccontextmanager
async def lifespan(app: FastAPI):
    # Initialize DB schema
    await init_db()
    # Seed data from CSVs
    async with async_session() as session:
        await seed_data(session)

    # Start Telegram background services (long-polling & 4-min inactivity monitor)
    telegram_service.start_background_tasks(async_session)

    yield

    # Gracefully stop Telegram services
    await telegram_service.stop()

app = FastAPI(
    title="QuickDrop Rider Payout Dispute Desk",
    version="1.0.0",
    lifespan=lifespan,
)

templates = Jinja2Templates(directory=os.path.join(os.path.dirname(__file__), "templates"))

# --- Pydantic Schemas ---
class MessagePayload(BaseModel):
    message_id: str
    rider_id: str
    text: str
    received_at: str

class MessageResponse(BaseModel):
    reply: str

class OpsActionPayload(BaseModel):
    notes: Optional[str] = None

# --- Required Endpoints ---

@app.get("/health")
async def health_check():
    return {"status": "ok"}

@app.post("/test/reset")
async def reset_test_state(rider_id: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    """Reset transient test state to run each scenario against a fresh state as per data/README.md"""
    if rider_id:
        await db.execute(delete(ProcessedMessage).where(ProcessedMessage.rider_id == rider_id))
        await db.execute(delete(TraceStep).where(TraceStep.rider_id == rider_id))
        await db.execute(delete(OpsItem).where(OpsItem.rider_id == rider_id))
    else:
        await db.execute(delete(ProcessedMessage))
        await db.execute(delete(TraceStep))
        await db.execute(delete(OpsItem))
    await db.commit()
    return {"status": "ok"}

@app.post("/messages", response_model=MessageResponse)
async def post_message(payload: MessagePayload, db: AsyncSession = Depends(get_db)):
    # 1. Idempotency Check: if message_id was already processed, return stored reply
    existing = await db.scalar(
        select(ProcessedMessage).where(ProcessedMessage.message_id == payload.message_id)
    )
    if existing:
        return MessageResponse(reply=existing.reply)

    # 2. Process message through agent orchestrator
    reply_text = await agent_orchestrator.handle_message(
        session=db,
        message_id=payload.message_id,
        rider_id=payload.rider_id,
        text=payload.text,
        received_at=payload.received_at,
    )

    # 3. Store processed message for idempotency
    msg_log = ProcessedMessage(
        message_id=payload.message_id,
        rider_id=payload.rider_id,
        reply=reply_text,
        created_at=datetime.now(timezone.utc),
    )
    db.add(msg_log)
    await db.commit()

    return MessageResponse(reply=reply_text)

@app.get("/trace/{rider_id}")
async def get_rider_trace(rider_id: str, db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(TraceStep)
        .where(TraceStep.rider_id == rider_id)
        .order_by(TraceStep.id.asc())
    )
    steps = result.scalars().all()
    return [
        {
            "at": step.at.isoformat() if step.at else datetime.now(timezone.utc).isoformat(),
            "type": step.type,
            "name": step.name,
            "input": step.input,
            "output": step.output,
        }
        for step in steps
    ]

@app.get("/ops/pending")
async def get_ops_pending(db: AsyncSession = Depends(get_db)):
    result = await db.execute(
        select(OpsItem)
        .where(OpsItem.status == "pending")
        .order_by(OpsItem.created_at.asc())
    )
    items = result.scalars().all()
    return [
        {
            "id": item.id,
            "rider_id": item.rider_id,
            "type": item.type,
            "amount": item.amount,
            "reason": item.reason,
            "created_at": item.created_at.isoformat() if item.created_at else datetime.now(timezone.utc).isoformat(),
        }
        for item in items
    ]

@app.post("/ops/{item_id}/approve")
async def approve_ops_item(item_id: str, db: AsyncSession = Depends(get_db)):
    item = await db.scalar(select(OpsItem).where(OpsItem.id == item_id))
    if not item:
        raise HTTPException(status_code=404, detail="Ops item not found")

    if item.status != "pending":
        return {"status": item.status, "message": f"Item already {item.status}"}

    # If item has an amount, disburse through PaySwift
    if item.amount and item.amount > 0:
        payout_res = await payswift_client.create_payout(
            rider_id=item.rider_id,
            amount=item.amount,
            reference=f"Ops manual approval for {item.id}",
            idempotency_key=f"ops_approval_{item.id}",
        )
        if "error" in payout_res:
            raise HTTPException(status_code=502, detail=f"PaySwift payout failed: {payout_res['error']}")

    item.status = "approved"
    await db.commit()

    # Outbound Telegram notification if rider has an active chat session
    if telegram_service.is_configured:
        tg_session = await db.scalar(
            select(TelegramDriverSession).where(
                TelegramDriverSession.rider_id == item.rider_id,
                TelegramDriverSession.status == "active",
            )
        )
        if tg_session:
            amount_str = f"₹{item.amount or 0}"
            if tg_session.preferred_language == "en":
                notice = f"🎉 <b>Good News!</b> Your dispute claim of {amount_str} has been approved by our Ops team and disbursed via PaySwift."
            else:
                notice = f"🎉 <b>Aapka claim approve ho gaya hai!</b> Ops team ne {amount_str} approve karke PaySwift ke dwara bhej diye hain."
            asyncio.create_task(
                telegram_service.client.send_message(
                    chat_id=tg_session.chat_id, text=notice, parse_mode="HTML"
                )
            )

    return {"status": "approved", "id": item.id, "amount": item.amount}

@app.post("/ops/{item_id}/reject")
async def reject_ops_item(item_id: str, db: AsyncSession = Depends(get_db)):
    item = await db.scalar(select(OpsItem).where(OpsItem.id == item_id))
    if not item:
        raise HTTPException(status_code=404, detail="Ops item not found")

    item.status = "rejected"
    await db.commit()
    return {"status": "rejected", "id": item.id}

# --- Ops Dashboard Web Interface ---

@app.get("/ops", response_class=HTMLResponse)
async def ops_dashboard(request: Request, rider_id: Optional[str] = None, db: AsyncSession = Depends(get_db)):
    pending_items = (
        await db.execute(select(OpsItem).where(OpsItem.status == "pending").order_by(OpsItem.created_at.desc()))
    ).scalars().all()

    resolved_items = (
        await db.execute(select(OpsItem).where(OpsItem.status != "pending").order_by(OpsItem.created_at.desc()).limit(15))
    ).scalars().all()

    trace_steps = []
    if rider_id:
        trace_steps = (
            await db.execute(select(TraceStep).where(TraceStep.rider_id == rider_id).order_by(TraceStep.id.asc()))
        ).scalars().all()

    approval_count = sum(1 for it in pending_items if it.type == "approval")
    escalation_count = sum(1 for it in pending_items if it.type == "escalation")

    return templates.TemplateResponse(
        request=request,
        name="ops.html",
        context={
            "pending_items": pending_items,
            "resolved_items": resolved_items,
            "active_rider_id": rider_id,
            "trace_steps": trace_steps,
            "approval_count": approval_count,
            "escalation_count": escalation_count,
        },
    )
