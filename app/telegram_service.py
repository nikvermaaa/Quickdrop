import asyncio
import logging
import re
import uuid
from datetime import datetime, timezone, timedelta
from typing import Optional, Dict, Any

try:
    from sqlalchemy import select, update
    from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker
except ImportError:
    select = update = AsyncSession = async_sessionmaker = None

from app.config import settings
from app.models import Rider, TelegramDriverSession, ProcessedMessage
from app.telegram_client import TelegramClient
from app.agent import agent_orchestrator

logger = logging.getLogger("telegram_service")

# In-memory tracking of pending language choice before phone is entered
_pending_languages: Dict[str, str] = {}

class TelegramService:
    def __init__(self, client: Optional[TelegramClient] = None):
        self.client = client or TelegramClient(settings.TELEGRAM_BOT_TOKEN)
        self._is_running = False
        self._poll_task: Optional[asyncio.Task] = None
        self._monitor_task: Optional[asyncio.Task] = None

    @property
    def is_configured(self) -> bool:
        return self.client.is_configured

    async def handle_update(self, update_data: Dict[str, Any], session_factory: async_sessionmaker[AsyncSession]):
        try:
            # 1. Handle Inline Keyboard Callbacks (Language Selection)
            if "callback_query" in update_data:
                cb = update_data["callback_query"]
                cb_id = cb["id"]
                chat_id = str(cb["message"]["chat"]["id"])
                data = cb.get("data", "")
                await self.client.answer_callback_query(cb_id)

                if data in ("lang_en", "lang_hi"):
                    lang = "en" if data == "lang_en" else "hi"
                    _pending_languages[chat_id] = lang

                    async with session_factory() as db:
                        existing = await db.scalar(
                            select(TelegramDriverSession).where(TelegramDriverSession.chat_id == chat_id)
                        )
                        if existing:
                            existing.preferred_language = lang
                            await db.commit()

                    if lang == "en":
                        prompt = (
                            "Language set to <b>English</b>! 🇬🇧\n\n"
                            "Please enter your 10-digit registered mobile number to verify your rider account:"
                        )
                    else:
                        prompt = (
                            "Bhasha <b>Hindi / Hinglish</b> set ho gayi hai! 🇮🇳\n\n"
                            "Apna rider account verify karne ke liye kripya apna 10-digit registered mobile number enter karein:"
                        )
                    await self.client.send_message(chat_id=chat_id, text=prompt, parse_mode="HTML")
                return

            # 2. Handle Text Messages
            message = update_data.get("message")
            if not message or "text" not in message:
                return

            chat_id = str(message["chat"]["id"])
            raw_text = message["text"].strip()
            msg_id = message.get("message_id", 0)

            async with session_factory() as db:
                session = await db.scalar(
                    select(TelegramDriverSession).where(
                        TelegramDriverSession.chat_id == chat_id,
                        TelegramDriverSession.status == "active",
                    )
                )

                # Command: /start or /language
                if raw_text in ("/start", "/language", "/lang"):
                    await self.client.send_language_menu(chat_id)
                    return

                # If NOT in an active session, treat message as phone number registration
                if not session:
                    lang = _pending_languages.get(chat_id, "hi")

                    # Extract digits from phone input
                    clean_phone = re.sub(r"[^\d]", "", raw_text)
                    if clean_phone.startswith("91") and len(clean_phone) == 12:
                        clean_phone = clean_phone[2:]

                    # Check if 10-digit phone
                    if len(clean_phone) != 10:
                        if lang == "en":
                            err_msg = (
                                "Please enter a valid 10-digit mobile number (e.g., 9800000003):\n"
                                "Or type /language to change language."
                            )
                        else:
                            err_msg = (
                                "Kripya sahi 10-digit mobile number enter karein (jaise 9800000003):\n"
                                "Bhasha badalne ke liye /language type karein."
                            )
                        await self.client.send_message(chat_id=chat_id, text=err_msg)
                        return

                    # Query database for registered rider with this phone number
                    rider = await db.scalar(
                        select(Rider).where(Rider.phone_number == clean_phone)
                    )

                    if not rider:
                        # Unregistered phone number rejection message
                        if lang == "en":
                            reject_text = (
                                "❌ <b>You are not registered with our service.</b>\n\n"
                                "We could not find an active QuickDrop rider account linked to this mobile number.\n"
                                "Please register with QuickDrop to proceed with the conversation."
                            )
                        else:
                            reject_text = (
                                "❌ <b>Aap QuickDrop service me registered nahi hain.</b>\n\n"
                                "Humare records me is mobile number se juda koi rider account nahi mila.\n"
                                "Baatchit aage badhane ke liye kripya pehle QuickDrop par register karein."
                            )
                        await self.client.send_message(chat_id=chat_id, text=reject_text, parse_mode="HTML")
                        return

                    # Registered driver found! Create or refresh active session
                    existing_session = await db.scalar(
                        select(TelegramDriverSession).where(TelegramDriverSession.chat_id == chat_id)
                    )
                    now_utc = datetime.now(timezone.utc)
                    if existing_session:
                        existing_session.rider_id = rider.rider_id
                        existing_session.phone_number = clean_phone
                        existing_session.preferred_language = lang
                        existing_session.status = "active"
                        existing_session.last_active_at = now_utc
                    else:
                        new_session = TelegramDriverSession(
                            chat_id=chat_id,
                            rider_id=rider.rider_id,
                            phone_number=clean_phone,
                            preferred_language=lang,
                            status="active",
                            last_active_at=now_utc,
                            created_at=now_utc,
                        )
                        db.add(new_session)
                    await db.commit()

                    # Welcome confirmation greeting with Name and Rider ID
                    if lang == "en":
                        welcome = (
                            f"✅ <b>Verified!</b> Hello {rider.name}!\n"
                            f"Your Rider ID is <b>{rider.rider_id}</b>. 🛵\n\n"
                            "Welcome to QuickDrop Support Desk. How can I assist you with your orders, surge, or payouts today?"
                        )
                    else:
                        welcome = (
                            f"✅ <b>Verification Safal!</b> Namaste {rider.name}!\n"
                            f"Aapka Rider ID <b>{rider.rider_id}</b> hai. 🛵\n\n"
                            "QuickDrop Payout Desk me aapka swagat hai. Aaj aapko kaunse order, surge, ya payout me samasya hai?"
                        )
                    await self.client.send_message(chat_id=chat_id, text=welcome, parse_mode="HTML")
                    return

                # Driver is already authenticated! Process dispute message
                now_utc = datetime.now(timezone.utc)
                session.last_active_at = now_utc
                await db.commit()

                wamid = f"tg_{msg_id}_{uuid.uuid4().hex[:6]}"
                msg_received_at = settings.TELEGRAM_REFERENCE_DATE if settings.TELEGRAM_REFERENCE_DATE else now_utc.isoformat()
                reply_text = await agent_orchestrator.handle_message(
                    session=db,
                    message_id=wamid,
                    rider_id=session.rider_id,
                    text=raw_text,
                    received_at=msg_received_at,
                    language=session.preferred_language,
                )

                # Store idempotency record
                db.add(
                    ProcessedMessage(
                        message_id=wamid,
                        rider_id=session.rider_id,
                        reply=reply_text,
                        created_at=now_utc,
                    )
                )
                await db.commit()

                # Send reply to Telegram
                await self.client.send_message(chat_id=chat_id, text=reply_text)

        except Exception as e:
            logger.error(f"Error handling Telegram update: {e}", exc_info=True)

    async def auto_close_sessions(self, session_factory: async_sessionmaker[AsyncSession]):
        """Background worker checking for 4-minute inactivity every 15 seconds"""
        timeout_seconds = settings.TELEGRAM_SESSION_TIMEOUT_SECONDS
        logger.info(f"Telegram auto-close worker started (Timeout: {timeout_seconds}s)")

        while self._is_running:
            try:
                await asyncio.sleep(15)
                now_utc = datetime.now(timezone.utc)
                cutoff_time = now_utc - timedelta(seconds=timeout_seconds)

                async with session_factory() as db:
                    sessions_to_close = (
                        await db.scalars(
                            select(TelegramDriverSession).where(
                                TelegramDriverSession.status == "active",
                                TelegramDriverSession.last_active_at < cutoff_time,
                            )
                        )
                    ).all()

                    for sess in sessions_to_close:
                        sess.status = "closed"
                        lang = sess.preferred_language
                        chat_id = sess.chat_id

                        if lang == "en":
                            notice = (
                                "⏰ <b>Support Session Closed</b>\n\n"
                                "Your session has been closed due to 4 minutes of inactivity.\n"
                                "If you need further assistance, simply type <b>'Hi'</b> to reconnect. Have a safe ride! 🛵"
                            )
                        else:
                            notice = (
                                "⏰ <b>Support Session Closed</b>\n\n"
                                "4 minute inactivity ke chalte support session close kar diya gaya hai.\n"
                                "Aur madad chahiye ho to dobara <b>'Hi'</b> bhejkar connect karein. Surakshit ride karein! 🛵"
                            )
                        await self.client.send_message(chat_id=chat_id, text=notice, parse_mode="HTML")

                    if sessions_to_close:
                        await db.commit()
                        logger.info(f"Auto-closed {len(sessions_to_close)} inactive Telegram driver session(s)")

            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Exception in auto_close_sessions monitor: {e}", exc_info=True)

    async def start_polling(self, session_factory: async_sessionmaker[AsyncSession]):
        if not self.is_configured:
            logger.info("Telegram Bot Token is not configured. Telegram polling worker will not run.")
            return

        self._is_running = True
        offset: Optional[int] = None
        logger.info("Starting Telegram Bot long-polling...")

        while self._is_running:
            try:
                updates = await self.client.get_updates(offset=offset, timeout=20)
                for u in updates:
                    offset = u["update_id"] + 1
                    asyncio.create_task(self.handle_update(u, session_factory))
            except asyncio.CancelledError:
                break
            except Exception as e:
                logger.error(f"Error in Telegram long-polling loop: {e}")
                await asyncio.sleep(3)

    def start_background_tasks(self, session_factory: async_sessionmaker[AsyncSession]):
        if not self.is_configured:
            logger.info("TELEGRAM_BOT_TOKEN not provided. Telegram services disabled.")
            return
        self._is_running = True
        self._poll_task = asyncio.create_task(self.start_polling(session_factory))
        self._monitor_task = asyncio.create_task(self.auto_close_sessions(session_factory))

    async def stop(self):
        self._is_running = False
        if self._poll_task:
            self._poll_task.cancel()
        if self._monitor_task:
            self._monitor_task.cancel()
        await self.client.close()
        logger.info("Telegram service stopped.")

telegram_service = TelegramService()
