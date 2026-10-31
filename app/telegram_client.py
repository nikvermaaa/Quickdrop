import logging
from typing import Optional, Dict, Any, List
try:
    import httpx
except ImportError:
    httpx = None

logger = logging.getLogger("telegram_client")

class TelegramClient:
    def __init__(self, token: str):
        self.token = token.strip()
        self.base_url = f"https://api.telegram.org/bot{self.token}"
        self._client: Any = None

    @property
    def is_configured(self) -> bool:
        return bool(self.token)

    async def _get_client(self) -> Any:
        if httpx is None:
            raise RuntimeError("httpx is required for TelegramClient network operations")
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(timeout=30.0)
        return self._client

    async def close(self):
        if self._client and hasattr(self._client, "is_closed") and not self._client.is_closed:
            await self._client.aclose()

    async def send_message(
        self,
        chat_id: str,
        text: str,
        reply_markup: Optional[Dict[str, Any]] = None,
        parse_mode: Optional[str] = None,
    ) -> Optional[Dict[str, Any]]:
        if not self.is_configured:
            return None
        client = await self._get_client()
        payload: Dict[str, Any] = {
            "chat_id": chat_id,
            "text": text,
        }
        if reply_markup:
            payload["reply_markup"] = reply_markup
        if parse_mode:
            payload["parse_mode"] = parse_mode

        try:
            resp = await client.post(f"{self.base_url}/sendMessage", json=payload)
            if resp.status_code == 200:
                return resp.json().get("result")
            else:
                logger.error(f"Telegram sendMessage failed ({resp.status_code}): {resp.text}")
                return None
        except Exception as e:
            logger.error(f"Exception sending Telegram message to {chat_id}: {e}")
            return None

    async def send_language_menu(self, chat_id: str) -> Optional[Dict[str, Any]]:
        text = (
            "🛵 <b>Welcome to QuickDrop Support Desk!</b>\n"
            "Kripya apni bhasha chunein / Please select your language:\n"
        )
        keyboard = {
            "inline_keyboard": [
                [
                    {"text": "🇬🇧 English", "callback_data": "lang_en"},
                    {"text": "🇮🇳 हिंदी / Hinglish", "callback_data": "lang_hi"},
                ]
            ]
        }
        return await self.send_message(
            chat_id=chat_id,
            text=text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )

    async def answer_callback_query(
        self, callback_query_id: str, text: Optional[str] = None
    ) -> bool:
        if not self.is_configured:
            return False
        client = await self._get_client()
        payload = {"callback_query_id": callback_query_id}
        if text:
            payload["text"] = text
        try:
            resp = await client.post(f"{self.base_url}/answerCallbackQuery", json=payload)
            return resp.status_code == 200
        except Exception as e:
            logger.error(f"Exception answering callback query {callback_query_id}: {e}")
            return False

    async def get_updates(
        self, offset: Optional[int] = None, timeout: int = 20
    ) -> List[Dict[str, Any]]:
        if not self.is_configured:
            return []
        client = await self._get_client()
        params: Dict[str, Any] = {"timeout": timeout}
        if offset is not None:
            params["offset"] = offset

        try:
            resp = await client.get(
                f"{self.base_url}/getUpdates",
                params=params,
                timeout=timeout + 5.0,
            )
            if resp.status_code == 200:
                return resp.json().get("result", [])
            else:
                logger.error(f"getUpdates returned {resp.status_code}: {resp.text}")
                return []
        except Exception as e:
            logger.error(f"Error fetching updates from Telegram: {e}")
            return []
