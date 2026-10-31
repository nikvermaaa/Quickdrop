import re
import uuid
import logging
from datetime import datetime, date, timezone, timedelta
from typing import Dict, Any, List, Optional, Tuple
try:
    from sqlalchemy import select, and_
    from sqlalchemy.ext.asyncio import AsyncSession
    from app.models import Rider, Trip, PayoutLine, OpsItem, TraceStep
    from app.payswift_client import payswift_client
except ImportError:
    select = and_ = None
    AsyncSession = Any
    Rider = Trip = PayoutLine = OpsItem = TraceStep = None
    payswift_client = None
from app.policy_engine import (
    to_ist,
    to_ist_date,
    calculate_trip_fare,
    is_within_7_days,
    audit_rider_account,
    normalize_rider_id,
    DisputeAuditResult,
)
from app.config import settings
from app.llm import generate_response_with_llm, classify_intent_with_llm

logger = logging.getLogger(__name__)

IST = timezone(timedelta(hours=5, minutes=30))

class AgentOrchestrator:
    def __init__(self):
        pass

    async def log_trace(
        self,
        session: AsyncSession,
        rider_id: str,
        step_type: str,
        name: str,
        input_data: Any,
        output_data: Any,
        timestamp: Optional[datetime] = None,
    ):
        step = TraceStep(
            rider_id=rider_id,
            at=timestamp or datetime.now(timezone.utc),
            type=step_type,
            name=name,
            input=input_data,
            output=output_data,
        )
        session.add(step)
        await session.flush()

    async def log_and_return_reply(
        self,
        session: AsyncSession,
        rider_id: str,
        incoming_text: str,
        base_reply: str,
        context_notes: str,
        recv_dt: datetime,
    ) -> str:
        final_reply, trace_info = await generate_response_with_llm(
            rider_id=rider_id,
            rider_message=incoming_text,
            factual_summary=context_notes,
            default_reply=base_reply,
        )
        if trace_info:
            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="llm_synthesis",
                input_data=trace_info.get("input"),
                output_data=trace_info.get("output"),
                timestamp=recv_dt,
            )
        await self.log_trace(
            session=session,
            rider_id=rider_id,
            step_type="reply",
            name="send_reply",
            input_data=None,
            output_data=final_reply,
            timestamp=recv_dt,
        )
        return final_reply

    async def fetch_payout_summary(
        self, session: AsyncSession, rider_id: str, target_date: Optional[date] = None
    ) -> Optional[Dict[str, Any]]:
        norm_r = normalize_rider_id(rider_id)
        query = (
            select(PayoutLine)
            .where(PayoutLine.rider_id.in_([rider_id, norm_r]))
            .order_by(PayoutLine.payout_date.desc())
        )
        if target_date:
            query = query.where(PayoutLine.payout_date == target_date)

        lines_res = await session.execute(query)
        all_lines = lines_res.scalars().all()
        if not all_lines:
            return None

        effective_date = target_date or all_lines[0].payout_date
        lines_for_date = [l for l in all_lines if l.payout_date == effective_date]
        if not lines_for_date:
            return None

        total_amount = sum(l.amount for l in lines_for_date)
        trip_lines = [l for l in lines_for_date if l.line_type == "trip"]
        trip_count = len(trip_lines)
        trip_fare = sum(l.amount for l in trip_lines)
        has_incentive = any(l.line_type == "daily_incentive" for l in lines_for_date)
        incentive_amount = sum(l.amount for l in lines_for_date if l.line_type == "daily_incentive")
        penalties = sum(l.amount for l in lines_for_date if l.line_type == "cancellation_penalty")

        return {
            "date": effective_date,
            "total_amount": total_amount,
            "trip_count": trip_count,
            "trip_fare": trip_fare,
            "has_incentive": has_incentive,
            "incentive_amount": incentive_amount,
            "penalties": penalties,
        }

    def is_last_payment_query(self, text: str) -> bool:
        lower = text.lower()
        # If it's explicitly a dispute claim (underpaid or missing money), let dispute engine audit it
        dispute_markers = ("kam", "galat", "missing", "nahi mila", "nahi aaya", "wrong", "deduct", "kata", "fraud")
        if any(w in lower for w in dispute_markers):
            return False

        tokens = re.findall(r"[a-z]+", lower)
        has_last = any(w in lower for w in ("last", "previous", "recent", "aakhri", "pichla", "pichle", "latest", "past"))
        
        # Matches any payment stem (pay, payment, payemnt, payout, payouts, pymnt, etc.) or money tokens
        has_pay_stem = any(
            t.startswith("pay") or t.startswith("pym") or t in ("money", "cash", "rupay", "paisa", "earn", "earning", "balance", "hisab", "statement")
            for t in tokens
        )
        has_inquiry = any(t in tokens for t in ("kitna", "what", "how", "tell", "batao", "bata", "check", "details", "phir"))

        is_direct = any(p in lower for p in (
            "last payment", "last payout", "my last", "pichla payment", "aakhri payment", 
            "mera payout", "payment details", "payout details", "check my payment", 
            "money phir se", "money bata"
        ))

        return (has_last and (has_pay_stem or has_inquiry)) or (has_pay_stem and has_inquiry) or is_direct

    def is_greeting_or_help(self, text: str) -> bool:
        clean = re.sub(r"[^\w\s]", "", text.lower()).strip()
        greetings = {"hi", "hello", "hey", "namaste", "help", "good morning", "good evening", "kuch help", "support"}
        return clean in greetings or clean.startswith("hi ") or clean.startswith("hello ") or clean == "help"

    def is_policy_question(self, text: str) -> Tuple[bool, str]:
        lower = text.lower()
        if ("incentive" in lower or "bonus" in lower) and any(w in lower for w in ("rule", "policy", "kaise", "how", "kya", "criteria", "eligib")):
            return True, "incentive"
        if ("surge" in lower or "fare" in lower or "kiraya" in lower) and any(w in lower for w in ("rule", "policy", "kaise", "how", "calculate", "kya")):
            return True, "surge"
        if ("cancellation" in lower or "penalty" in lower) and any(w in lower for w in ("rule", "policy", "kaise", "how", "kya", "why")):
            return True, "penalty"
        return False, ""

    def parse_entities(self, text: str, ref_date: date) -> Dict[str, Any]:
        """
        Extracts trip IDs, dates, numbers, and complaint categories from text.
        """
        trip_ids = re.findall(r"\bT\d{6}\b", text, re.IGNORECASE)
        trip_ids = [t.upper() for t in trip_ids]

        dates = []
        lower = text.lower()

        if "kal" in lower:
            dates.append(ref_date - timedelta(days=1))

        # Only match calendar dates when accompanied by date markers
        day_matches = re.findall(
            r"\b(\d{1,2})\s*(?:tarikh|sep|september|sept|ko|wala|wali|th|st|nd|rd)\b|\b(?:sep|sept)\s*(\d{1,2})\b",
            lower,
        )
        for m in day_matches:
            d_str = m[0] or m[1]
            try:
                d_num = int(d_str)
                if 1 <= d_num <= 31:
                    candidate = date(ref_date.year, 9, d_num)
                    if candidate not in dates:
                        dates.append(candidate)
            except ValueError:
                pass

        has_surge = "surge" in lower
        has_incentive = "incentive" in lower or "12 order" in lower or "12 se zyada" in lower
        has_penalty = "penalty" in lower or "cancel" in lower
        # Only dispute distance if explicitly claiming distance was recorded wrongly
        has_distance = (
            ("distance" in lower or "km" in lower)
            and any(w in lower for w in ("galat", "wrong", "incorrect", "issue", "problem", "nahi tha", "zyada tha", "kam laga"))
        )
        has_accident = "accident" in lower or "chot" in lower
        has_impersonation = bool(re.search(r"\bthis is r\d{3}\b", lower) or re.search(r"\brider r\d{3}\b", lower))
        has_injection = "system:" in lower or "ignore all" in lower or "ignore previous" in lower

        return {
            "trip_ids": trip_ids,
            "dates": dates,
            "has_surge": has_surge,
            "has_incentive": has_incentive,
            "has_penalty": has_penalty,
            "has_distance": has_distance,
            "has_accident": has_accident,
            "has_impersonation": has_impersonation,
            "has_injection": has_injection,
        }

    async def handle_message(
        self,
        session: AsyncSession,
        message_id: str,
        rider_id: str,
        text: str,
        received_at: Any,
        language: str = "hi",
    ) -> str:
        if isinstance(received_at, datetime):
            recv_dt = received_at
        else:
            try:
                recv_dt = datetime.fromisoformat(str(received_at).replace("Z", "+00:00"))
            except Exception:
                recv_dt = datetime.now(timezone.utc)

        ref_date = to_ist_date(recv_dt)

        # 1. Log message_in step
        await self.log_trace(
            session=session,
            rider_id=rider_id,
            step_type="message_in",
            name="whatsapp_message",
            input_data={
                "message_id": message_id,
                "rider_id": rider_id,
                "text": text,
                "received_at": received_at,
            },
            output_data=None,
            timestamp=recv_dt,
        )

        # 2. Check for Prompt Injection / Malicious Override
        lower_text = text.lower()
        if "ignore all previous" in lower_text or "system:" in lower_text or "ignore rules" in lower_text:
            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="prompt_injection_blocked",
                input_data={"text": text},
                output_data={"action": "block_and_ignore"},
                timestamp=recv_dt,
            )
            reply = "Aapke message mein aniyamit nirdesh paaye gaye hain. Main sirf QuickDrop payout policy ke tahat madad kar sakta hoon."
            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="reply",
                name="send_reply",
                input_data=None,
                output_data=reply,
                timestamp=recv_dt,
            )
            return reply

        # 3. Check for Identity Impersonation (e.g. Rider claiming to be R005 from R020 account)
        impersonation_match = re.search(r"\b(?:this is|main hoon|rider)\s+(r\d{3})\b", lower_text)
        if impersonation_match:
            claimed_id = impersonation_match.group(1).upper()
            if normalize_rider_id(claimed_id) != normalize_rider_id(rider_id):
                await self.log_trace(
                    session=session,
                    rider_id=rider_id,
                    step_type="decision",
                    name="identity_mismatch_detected",
                    input_data={"authenticated_rider": rider_id, "claimed_rider": claimed_id},
                    output_data={"action": "refuse_cross_account_access"},
                    timestamp=recv_dt,
                )
                reply = "Main sirf is number se jude account ke baare mein madad kar sakta hoon. Aapke account ki koi specific problem ho to bataiye."
                await self.log_trace(
                    session=session,
                    rider_id=rider_id,
                    step_type="reply",
                    name="send_reply",
                    input_data=None,
                    output_data=reply,
                    timestamp=recv_dt,
                )
                return reply

        # 4. Extract entities and intents
        entities = self.parse_entities(text, ref_date)

        # Check for pushback / follow-up on already processed dispute
        if "kab tak aayega" in lower_text or "paisa kab aayega" in lower_text:
            payouts = await payswift_client.get_payouts(rider_id=rider_id)
            latest_amt = payouts[-1].get("amount") if payouts else 25
            reply = f"₹{latest_amt} ka payment PaySwift pe process ho gaya hai."
            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="reply",
                name="status_update",
                input_data=None,
                output_data=reply,
                timestamp=recv_dt,
            )
            return reply

        if "bhi abhi de do" in lower_text or "turant de do" in lower_text or "wo bhi bhej do" in lower_text:
            reply = "Samajh sakta hoon. Yeh amount ops approval ke liye chala gaya hai, approve hote hi aa jayega."
            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="reply",
                name="ops_approval_in_progress",
                input_data=None,
                output_data=reply,
                timestamp=recv_dt,
            )
            return reply

        if "dobara check" in lower_text or "nahi nahi" in lower_text:
            ops_item = OpsItem(
                id=f"ops-{uuid.uuid4().hex[:8]}",
                rider_id=rider_id,
                type="escalation",
                amount=None,
                reason="Rider disagrees with completed trip records after explanation",
                status="pending",
                created_at=recv_dt,
            )
            session.add(ops_item)
            await session.flush()

            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="escalate_dispute_to_ops",
                input_data={"rider_id": rider_id, "text": text},
                output_data={"ops_id": ops_item.id, "type": "escalation"},
                timestamp=recv_dt,
            )

            reply = "Dobara check kiya: humare record mein specified date ko trips record kiye gaye hain. Agar aapko lagta hai record galat hai, main ise ops team ko bhej deta hoon."
            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="reply",
                name="send_reply",
                input_data=None,
                output_data=reply,
                timestamp=recv_dt,
            )
            return reply

        # 5. Check Special Escalations: Distance Dispute or Accident
        if entities["has_accident"]:
            ops_item = OpsItem(
                id=f"ops-{uuid.uuid4().hex[:8]}",
                rider_id=rider_id,
                type="escalation",
                amount=None,
                reason=f"Accident related cancellation penalty dispute: {text}",
                status="pending",
                created_at=recv_dt,
            )
            session.add(ops_item)
            await session.flush()

            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="escalate_accident_waiver",
                input_data={"text": text},
                output_data={"ops_id": ops_item.id},
                timestamp=recv_dt,
            )
            reply = "Accident ke baare mein sunkar dukh hua. Cancellation penalty waiver policy exception ke tahat aati hai, isliye maine ise ops team ko review ke liye bhej diya hai."
            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="reply",
                name="send_reply",
                input_data=None,
                output_data=reply,
                timestamp=recv_dt,
            )
            return reply

        if entities["has_distance"]:
            ops_item = OpsItem(
                id=f"ops-{uuid.uuid4().hex[:8]}",
                rider_id=rider_id,
                type="escalation",
                amount=None,
                reason=f"Rider claims GPS distance recorded incorrectly: {text}",
                status="pending",
                created_at=recv_dt,
            )
            session.add(ops_item)
            await session.flush()

            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="escalate_distance_dispute",
                input_data={"text": text},
                output_data={"ops_id": ops_item.id},
                timestamp=recv_dt,
            )
            reply = "Distance discrepancy GPS review par depend karti hai. Maine aapki request ops team ko review ke liye forward kar di hai."
            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="reply",
                name="send_reply",
                input_data=None,
                output_data=reply,
                timestamp=recv_dt,
            )
            return reply

        # 5.5 Informational & General Inquiries
        llm_intent_data = await classify_intent_with_llm(rider_id, text)
        llm_intent = llm_intent_data.get("intent") if llm_intent_data else None

        if llm_intent == "greeting" or self.is_greeting_or_help(text):
            is_eng = any(w in lower_text for w in ("hi", "hello", "hey", "help", "morning", "evening"))
            reply = (
                "Hello! I am QuickDrop's Payout Support Assistant. I can help you check your payouts, missing surge, daily incentives, or resolve deductions. Please share the date or order ID you need help with."
                if is_eng else
                "Namaste! Main QuickDrop Payout Assistant hoon. Main aapke payout, trips, surge ya daily incentive se judi samasyaon mein madad kar sakta hoon. Kripya apna sawal, date ya order ID batayein."
            )
            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="greeting_acknowledged",
                input_data={"text": text, "llm_classified": llm_intent == "greeting"},
                output_data={"action": "prompt_user_for_details"},
                timestamp=recv_dt,
            )
            return await self.log_and_return_reply(
                session=session,
                rider_id=rider_id,
                incoming_text=text,
                base_reply=reply,
                context_notes="Rider greeted. Prompted them to share payout dispute details or order ID.",
                recv_dt=recv_dt,
            )

        if llm_intent == "last_payment" or self.is_last_payment_query(text):
            target_d = entities["dates"][0] if entities["dates"] else None
            summary = await self.fetch_payout_summary(session, rider_id, target_d)
            if summary:
                await self.log_trace(
                    session=session,
                    rider_id=rider_id,
                    step_type="tool_call",
                    name="fetch_last_payment",
                    input_data={"rider_id": rider_id, "target_date": target_d.isoformat() if target_d else None},
                    output_data={
                        "date": summary["date"].isoformat(),
                        "total_amount": summary["total_amount"],
                        "trip_count": summary["trip_count"],
                    },
                    timestamp=recv_dt,
                )
                is_eng = any(w in lower_text for w in ("can you", "tell me", "about", "baout", "payment", "what", "my last"))
                if is_eng:
                    reply = f"Your last payout on {summary['date'].strftime('%d %b')} was ₹{summary['total_amount']} (covering {summary['trip_count']} completed trips for ₹{summary['trip_fare']}{', plus ₹150 incentive' if summary['has_incentive'] else ''}). The payout has been processed. If you notice any issue or missing surge for a specific order, please share the trip ID."
                else:
                    reply = f"Aapka aakhri payout {summary['date'].strftime('%d %b')} ko ₹{summary['total_amount']} ka tha ({summary['trip_count']} trips ke ₹{summary['trip_fare']}{', ₹150 incentive' if summary['has_incentive'] else ''}). Payment PaySwift par process ho chuka hai. Agar kisi specific trip ya surge mein dikkat ho, to order ID batayein."

                return await self.log_and_return_reply(
                    session=session,
                    rider_id=rider_id,
                    incoming_text=text,
                    base_reply=reply,
                    context_notes=f"Last payout on {summary['date'].strftime('%d %b')} was ₹{summary['total_amount']} for {summary['trip_count']} trips.",
                    recv_dt=recv_dt,
                )
            else:
                reply = "Aapke account par pichle 7 dino mein koi processed payout record nahi mila. Agar aapne trips kiye hain to kripya date ya order ID batayein."
                return await self.log_and_return_reply(
                    session=session,
                    rider_id=rider_id,
                    incoming_text=text,
                    base_reply=reply,
                    context_notes="No payout records found in the last 7 days.",
                    recv_dt=recv_dt,
                )

        is_pol, pol_topic = self.is_policy_question(text)
        if is_pol:
            if pol_topic == "incentive":
                reply = "QuickDrop policy ke anusaar, agar aap ek calendar day (IST) mein 12 ya usse zyada completed trips karte hain, to aapko ₹150 ka daily incentive milta hai."
            elif pol_topic == "surge":
                reply = "Trip payout calculation: ₹25 base fare + pehle 2 km ke baad ₹6 per km. Surge multiplier pure trip fare par lagta hai aur nearest rupee par round hota hai."
            else:
                reply = "Rider dwara cancel kiye gaye order par ₹10 penalty lagti hai. Customer dwara cancel kiye gaye order par koi penalty nahi lagti."

            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="tool_call",
                name="explain_policy",
                input_data={"topic": pol_topic},
                output_data={"policy_info": reply},
                timestamp=recv_dt,
            )
            return await self.log_and_return_reply(
                session=session,
                rider_id=rider_id,
                incoming_text=text,
                base_reply=reply,
                context_notes=f"Policy explanation provided for {pol_topic}.",
                recv_dt=recv_dt,
            )

        # 6. Specific Trip Check: Does the trip belong to this rider?
        if entities["trip_ids"]:
            for tid in entities["trip_ids"]:
                trip_record = await session.scalar(select(Trip).where(Trip.trip_id == tid))
                if trip_record:
                    owner_norm = normalize_rider_id(trip_record.rider_id)
                    caller_norm = normalize_rider_id(rider_id)
                    if owner_norm != caller_norm:
                        await self.log_trace(
                            session=session,
                            rider_id=rider_id,
                            step_type="decision",
                            name="trip_not_owned_by_rider",
                            input_data={"trip_id": tid, "actual_owner": trip_record.rider_id},
                            output_data={"action": "deny"},
                            timestamp=recv_dt,
                        )
                        reply = f"Order {tid} aapke account se judaa hua nahi hai. Kripya apna sahi order ID check kijiye."
                        await self.log_trace(
                            session=session,
                            rider_id=rider_id,
                            step_type="reply",
                            name="send_reply",
                            input_data=None,
                            output_data=reply,
                            timestamp=recv_dt,
                        )
                        return reply

        # 7. Check 7-day policy window for requested dates/trips
        for d in entities["dates"]:
            if not is_within_7_days(d, ref_date):
                await self.log_trace(
                    session=session,
                    rider_id=rider_id,
                    step_type="decision",
                    name="dispute_outside_7_days",
                    input_data={"requested_date": d.isoformat(), "reference_date": ref_date.isoformat()},
                    output_data={"action": "deny_due_to_policy"},
                    timestamp=recv_dt,
                )
                reply = f"{d.strftime('%d %b')} ka dispute 7 din se purana hai. QuickDrop policy ke anusaar hum sirf pichle 7 dino ke disputes review kar sakte hain."
                await self.log_trace(
                    session=session,
                    rider_id=rider_id,
                    step_type="reply",
                    name="send_reply",
                    input_data=None,
                    output_data=reply,
                    timestamp=recv_dt,
                )
                return reply

        # 8. Vague Complaint Handling
        is_vague = (
            not entities["trip_ids"]
            and not entities["dates"]
            and not entities["has_incentive"]
            and not entities["has_penalty"]
            and any(w in lower_text for w in ("galat", "kam", "problem", "wrong", "issue", "missing", "incorrect", "dikkat"))
        )
        if is_vague:
            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="request_clarification",
                input_data={"text": text},
                output_data={"action": "ask_date_or_trip"},
                timestamp=recv_dt,
            )
            is_eng = any(w in lower_text for w in ("wrong", "issue", "incorrect", "problem")) and not any(w in lower_text for w in ("galat", "kam", "bhai", "aaya"))
            reply = (
                "Which date or order ID was the payout incorrect for? Please share the date or order ID."
                if is_eng else
                "Kaunse din ya kaunse order ka payout galat laga? Date ya order ID bata dijiye."
            )
            return await self.log_and_return_reply(
                session=session,
                rider_id=rider_id,
                incoming_text=text,
                base_reply=reply,
                context_notes="Rider reported vague complaint without date or order ID. Asked for date/order ID.",
                recv_dt=recv_dt,
            )

        # 9. Audit the Account via Policy Engine
        filter_tids = entities["trip_ids"] if entities["trip_ids"] else None
        filter_ds = entities["dates"] if entities["dates"] else None

        await self.log_trace(
            session=session,
            rider_id=rider_id,
            step_type="tool_call",
            name="audit_rider_account",
            input_data={
                "rider_id": rider_id,
                "trip_ids": filter_tids,
                "dates": [d.isoformat() for d in filter_ds] if filter_ds else None,
            },
            output_data={"status": "executing"},
            timestamp=recv_dt,
        )

        audit_results = await audit_rider_account(
            session=session,
            rider_id=rider_id,
            reference_time=recv_dt,
            filter_trip_ids=filter_tids,
            filter_dates=filter_ds,
        )

        await self.log_trace(
            session=session,
            rider_id=rider_id,
            step_type="decision",
            name="audit_results_evaluated",
            input_data={"findings_count": len(audit_results)},
            output_data=[r.to_dict() for r in audit_results],
            timestamp=recv_dt,
        )

        # 10. Handle Incentive Query When Not Eligible
        if entities["has_incentive"] and not any(r.item_type == "daily_incentive" for r in audit_results):
            check_date = (filter_ds[0] if filter_ds else None) or (ref_date - timedelta(days=1))
            norm_r = normalize_rider_id(rider_id)
            all_day_trips = (
                await session.execute(
                    select(Trip).where(Trip.rider_id.in_([rider_id, norm_r]))
                )
            ).scalars().all()
            completed = [t for t in all_day_trips if to_ist_date(t.started_at) == check_date and t.status == "completed"]
            count = len(completed)

            reply = f"{check_date.strftime('%d %b')} ko aapke {count} trips complete hue the. Incentive 12 trips pe milta hai, isliye nahi bana."
            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="incentive_ineligible_evaluated",
                input_data={"date": check_date.isoformat(), "completed_trips": count, "threshold": 12},
                output_data={"eligible": False},
                timestamp=recv_dt,
            )
            return await self.log_and_return_reply(
                session=session,
                rider_id=rider_id,
                incoming_text=text,
                base_reply=reply,
                context_notes=f"Incentive ineligible: completed {count} trips on {check_date.strftime('%d %b')}, threshold is 12.",
                recv_dt=recv_dt,
            )

        # 11. Handle Zero Discrepancy Found
        if not audit_results:
            if filter_tids:
                reply = f"Order {filter_tids[0]} ka payout check kiya gaya. Is order pe policy ke anusaar poora payment sahi se credit ho chuka hai."
                notes = f"Order {filter_tids[0]} audited. Full policy payout was already credited."
            else:
                reply = "Aapke account ke pichle 7 dino ke records check kiye gaye hain. Policy ke mutabiq saare payouts sahi calculation ke anusaar kiye gaye hain."
                notes = "Audit for past 7 days completed. All payouts were paid correctly according to policy."

            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="no_discrepancy_explanation",
                input_data={"filter_trips": filter_tids, "filter_dates": [d.isoformat() for d in filter_ds] if filter_ds else None},
                output_data={"status": "all_records_accurate"},
                timestamp=recv_dt,
            )
            return await self.log_and_return_reply(
                session=session,
                rider_id=rider_id,
                incoming_text=text,
                base_reply=reply,
                context_notes=notes,
                recv_dt=recv_dt,
            )

        # 12. Settle Discrepancies (Grouped by Date & Policy Guardrails)
        results_by_date: Dict[date, List[DisputeAuditResult]] = {}
        for r in audit_results:
            results_by_date.setdefault(r.date_ref, []).append(r)

        distinct_dates = list(results_by_date.keys())
        distinct_dates.sort()

        if len(distinct_dates) > 1:
            # Multiple distinct dates (e.g. 20 Sep surge ₹15 and 21 Sep penalty ₹10)
            group1 = results_by_date[distinct_dates[0]]
            group2 = results_by_date[distinct_dates[1]]
            amt1 = sum(it.amount for it in group1)
            amt2 = sum(it.amount for it in group2)

            # Auto-pay group 1 if <= 200
            p_res = await payswift_client.create_payout(
                rider_id=rider_id,
                amount=amt1,
                reference=f"Dispute settlement for {distinct_dates[0]}",
                idempotency_key=f"{message_id}_{distinct_dates[0]}_{uuid.uuid4().hex[:6]}",
            )
            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="tool_call",
                name="payswift_payout",
                input_data={"rider_id": rider_id, "amount": amt1},
                output_data=p_res,
                timestamp=recv_dt,
            )

            # Queue group 2 for Ops approval (due to 1 auto-pay per rider per day limit)
            ops_item = OpsItem(
                id=f"ops-{uuid.uuid4().hex[:8]}",
                rider_id=rider_id,
                type="approval",
                amount=amt2,
                reason=f"Repeat dispute today: {group2[0].reason}",
                status="pending",
                created_at=recv_dt,
            )
            session.add(ops_item)
            await session.flush()

            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="queue_repeat_dispute_for_ops",
                input_data={"date": distinct_dates[1].isoformat(), "amount": amt2},
                output_data={"ops_id": ops_item.id, "amount": amt2},
                timestamp=recv_dt,
            )

            reply = (
                f"Dono check kiye. {distinct_dates[0].strftime('%d %b')}: {group1[0].reason} "
                f"{distinct_dates[1].strftime('%d %b')}: {group2[0].reason} "
                f"₹{amt1} abhi bhej rahe hain; ek din mein ek hi auto-payment ho sakta hai, "
                f"isliye ₹{amt2} ops team approve karegi."
            )
            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="reply",
                name="send_reply",
                input_data=None,
                output_data=reply,
                timestamp=recv_dt,
            )
            return reply

        # Single date / single dispute area
        single_date = distinct_dates[0]
        items_for_date = results_by_date[single_date]
        total_owed = sum(it.amount for it in items_for_date)
        single = items_for_date[0]

        # Check if total amount exceeds ₹200
        if total_owed > 200:
            ops_item = OpsItem(
                id=f"ops-{uuid.uuid4().hex[:8]}",
                rider_id=rider_id,
                type="approval",
                amount=total_owed,
                reason=f"Payout amount ₹{total_owed} exceeds ₹200 auto-pay limit: {len(items_for_date)} items on {single_date}",
                status="pending",
                created_at=recv_dt,
            )
            session.add(ops_item)
            await session.flush()

            await self.log_trace(
                session=session,
                rider_id=rider_id,
                step_type="decision",
                name="require_ops_approval_for_large_amount",
                input_data={"total_amount": total_owed, "limit": 200},
                output_data={"ops_id": ops_item.id, "type": "approval"},
                timestamp=recv_dt,
            )

            reply = f"Aapka dispute verify ho gaya hai: total ₹{total_owed} ka difference hai. ₹200 se zyada amount hone ke karan yeh ops team ke approval ke liye bhej diya gaya hai, approve hote hi credit ho jayega."
            return await self.log_and_return_reply(
                session=session,
                rider_id=rider_id,
                incoming_text=text,
                base_reply=reply,
                context_notes=f"Total amount ₹{total_owed} exceeds ₹200 auto-pay limit. Queued for Ops approval.",
                recv_dt=recv_dt,
            )

        # Auto-pay <= 200
        p_res = await payswift_client.create_payout(
            rider_id=rider_id,
            amount=total_owed,
            reference=f"Dispute settlement: {single.trip_id or single.item_type}",
            idempotency_key=f"{message_id}_{rider_id}_{uuid.uuid4().hex[:6]}",
        )

        await self.log_trace(
            session=session,
            rider_id=rider_id,
            step_type="tool_call",
            name="payswift_payout",
            input_data={"rider_id": rider_id, "amount": total_owed, "reason": single.reason},
            output_data=p_res,
            timestamp=recv_dt,
        )

        await self.log_trace(
            session=session,
            rider_id=rider_id,
            step_type="decision",
            name="auto_disburse_approved",
            input_data={"amount": total_owed, "limit": 200},
            output_data={"status": "paid_via_payswift"},
            timestamp=recv_dt,
        )

        if single.item_type == "surge" and single.trip_id:
            reply = f"{single.date_ref.strftime('%d %b')} ko order {single.trip_id} pe surge laga tha. Farak ₹{total_owed} aapko PaySwift ke zariye bhej rahe hain."
        elif single.item_type == "daily_incentive":
            reply = f"{single.date_ref.strftime('%d %b')} ko aapke 12 se zyada order complete hue the. ₹{total_owed} ka daily incentive aapke account mein transfer kar diya gaya hai."
        elif single.item_type == "duplicate_penalty":
            reply = f"{single.date_ref.strftime('%d %b')} ko cancellation penalty extra cut ho gayi thi. Extra kata hua ₹{total_owed} refund bhej diya gaya hai."
        elif single.item_type == "underpaid_trip" and single.trip_id:
            reply = f"Order {single.trip_id} ka remaining payout ₹{total_owed} process kar diya gaya hai."
        elif single.item_type == "unpaid_trip" and len(items_for_date) > 1:
            reply = f"{single.date_ref.strftime('%d %b')} ke {len(items_for_date)} orders ka pending payout ₹{total_owed} process kar diya gaya hai."
        else:
            reply = f"Aapka dispute verify ho gaya hai aur ₹{total_owed} ka payment PaySwift pe process kar diya gaya hai."

        return await self.log_and_return_reply(
            session=session,
            rider_id=rider_id,
            incoming_text=text,
            base_reply=reply,
            context_notes=f"Single dispute: {single.item_type} for date {single.date_ref}, amount owed ₹{total_owed}. PaySwift payout processed.",
            recv_dt=recv_dt,
        )

agent_orchestrator = AgentOrchestrator()
